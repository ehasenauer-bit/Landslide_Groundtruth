"""Fetch pre/post satellite imagery around a landslide event via STAC.

Data sources (all free, no full-scene downloads — COGs streamed by window):
  - Sentinel-2 L2A   (2015-07 onward; ~10 m)   Microsoft Planetary Computer
  - Landsat C2 L2    (Landsat 8: 2013-03 on, 9: 2021-11 on; ~30 m)  Planetary Computer

Strategy per event:
  pre  window: [t - pre_days, t - 1 day]
  post window: [t + 1 day, t + post_days]
  Rank scenes in each window by a blend of temporal distance to the event and
  cloud cover (gap_days + cloud_weight * cloud_pct) so the composite stays close
  to the event date, take the best N, cloud/snow-mask them, median-composite,
  compute NDVI, then dNDVI = post - pre.

Alaska caveats handled here:
  - Winter/shoulder-season events: snow makes dNDVI useless. If --seasonal is
    set, the pre window is shifted to the same calendar window one year earlier
    and the post window to the first snow-reduced period after the event
    (you can also just widen post_days to reach the next summer).
  - Pre-2015 events fall back to Landsat automatically.
"""
from __future__ import annotations
import datetime as dt
import threading
import time
import warnings
import numpy as np
import pystac_client
from pystac_client.exceptions import APIError
from pystac_client.stac_api_io import StacApiIO
from urllib3.util.retry import Retry
import planetary_computer as pc
import stackstac
import rioxarray  # noqa: F401  (registers .rio accessor)
from pyproj import CRS

PC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# The Planetary Computer STAC API intermittently returns a server-side timeout
# ("The request exceeded the maximum allowed time"). urllib3's default retry
# does not cover POST search requests or these status codes, so be explicit:
# retry the transient 5xx/429/408 responses on every method (incl. POST) with
# exponential backoff, and cap how long a single request may hang.
_RETRY = Retry(
    total=5,
    backoff_factor=2,                       # 0, 2, 4, 8, 16 s between tries
    status_forcelist=[408, 429, 500, 502, 503, 504],
    allowed_methods=None,                   # retry all methods, including POST
    respect_retry_after_header=True,
)
_TIMEOUT = (15, 120)                         # (connect, read) seconds


_local = threading.local()


def _client():
    """Open (once per thread) and reuse the Planetary Computer STAC client.

    Opening a client fetches the catalog landing page + conformance over HTTP, so
    a fresh client per search added a redundant round-trip on every call: 2 per
    STAC run (pre+post), and up to 4 more when `_ids_collection` probes
    collections for hand-picked scene IDs. The client is reused for the life of
    the (short-lived) process. The cache is thread-local so the parallel dry-run
    preview, which searches Sentinel-2 and Landsat on separate threads, never
    shares one client's `requests` session across threads."""
    c = getattr(_local, "client", None)
    if c is None:
        stac_io = StacApiIO(timeout=_TIMEOUT, max_retries=_RETRY)
        c = pystac_client.Client.open(PC_URL, modifier=pc.sign_inplace,
                                      stac_io=stac_io, timeout=_TIMEOUT)
        _local.client = c
    return c


def _search_items(cat, *, attempts=3, **search_kwargs):
    """Run a STAC search and fully materialize its items, retrying the whole
    paged fetch on transient APIErrors that slip past the HTTP-level retry."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return list(cat.search(**search_kwargs).items())
        except APIError as e:
            last_exc = e
            if attempt < attempts:
                wait = 2 ** attempt
                print(f"    [stac] search timed out ({type(e).__name__}); "
                      f"retry {attempt}/{attempts - 1} in {wait}s")
                time.sleep(wait)
    raise last_exc

S2_BANDS = ["B04", "B08", "B03", "B02", "SCL"]          # red, nir, green, blue, scene class
LS_BANDS = ["red", "nir08", "green", "blue", "qa_pixel"]

S2_BAD_SCL = [0, 1, 3, 8, 9, 10]   # nodata, saturated, cloud shadow, cloud med/high, cirrus
                                    # NOTE: 11 = snow/ice intentionally kept (see mask note below)


def _utm_epsg(lat, lon):
    zone = int((lon + 180) // 6) + 1
    return CRS.from_dict({"proj": "utm", "zone": zone, "south": lat < 0}).to_epsg()


def _bbox(lat, lon, radius_km):
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * np.cos(np.radians(lat)))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def windows(event_time: dt.datetime, pre_days=60, post_days=60, seasonal=False):
    """Pre/post search windows (pre0, pre1, post0, post1) for an event.

    Shared by the optical and PlanetScope paths so the dry-run preview, the
    Sentinel-2/Landsat run, and the PlanetScope run all bound the same dates.
    seasonal: winter event -> shift the pre window to the same calendar window
    one year earlier (snow makes a same-season dNDVI useless)."""
    if seasonal:
        pre0 = event_time - dt.timedelta(days=365 + pre_days // 2)
        pre1 = event_time - dt.timedelta(days=365 - pre_days // 2)
    else:
        pre0, pre1 = event_time - dt.timedelta(days=pre_days), event_time - dt.timedelta(days=1)
    post0, post1 = event_time + dt.timedelta(days=1), event_time + dt.timedelta(days=post_days)
    return pre0, pre1, post0, post1


def search_scenes(lat, lon, radius_km, start, end, collection, event_time,
                  max_cloud=60, limit=6, cloud_weight=0.5):
    """Cloud-acceptable scenes in [start, end], ranked best-first.

    All candidates are first filtered to eo:cloud_cover < max_cloud, then ranked
    against the event date by `cloud_weight`:
      float -> BLEND by a combined cost  gap_days + cloud_weight * cloud_pct,
        where gap_days = |acquired - event_time|. A small weight favours scenes
        near the event date; a large weight recovers the old least-cloudy order.
      None  -> rank by temporal proximity to event_time alone (the tightest
        --auto-window mode: pick the single nearest acceptably-clear scene).
    """
    cat = _client()
    query = {"eo:cloud_cover": {"lt": max_cloud}}
    items = _search_items(
        cat,
        collections=[collection],
        bbox=_bbox(lat, lon, radius_km),
        datetime=f"{start.date()}/{end.date()}",
        query=query,
    )
    items = [i for i in items if i.datetime is not None]

    def gap_days(i):
        return abs((i.datetime.replace(tzinfo=None) - event_time).total_seconds()) / 86400.0

    def cloud_pct(i):                                   # eo:cloud_cover is already 0-100
        c = i.properties.get("eo:cloud_cover")
        return 100.0 if c is None else c

    if cloud_weight is None:
        # auto_window: nearest day first, then clearest among same-day acquisitions
        items.sort(key=lambda i: (round(gap_days(i)), cloud_pct(i)))
    else:
        items.sort(key=lambda i: gap_days(i) + cloud_weight * cloud_pct(i))
    return items[:limit]


def fetch_items_by_ids(collection, ids):
    """Materialize specific STAC items by ID from one collection (signed).

    Used by the manual-selection path: when the caller hand-picks exact scene IDs
    to composite, we fetch just those instead of running the windowed search.
    Items come back in the requested order; IDs not present in the collection are
    silently dropped, so probing a Sentinel-2 ID against the Landsat collection
    simply returns nothing (which is how `_ids_collection` finds the right one)."""
    ids = [i for i in ids if i]
    if not ids:
        return []
    cat = _client()
    items = _search_items(cat, collections=[collection], ids=ids)
    by_id = {i.id: i for i in items}
    return [by_id[i] for i in ids if i in by_id]


def _ids_collection(prefer, ids):
    """The STAC collection that holds `ids`, probing only the streamable sources.

    Tries prefer's collection first when it's Sentinel-2 / Landsat, otherwise
    probes Sentinel-2 then Landsat. Returns the collection name, or None when no
    requested ID is found in either (e.g. a stale or PlanetScope id)."""
    streamable = {"s2": "sentinel-2-l2a", "landsat": "landsat-c2-l2"}
    order = ([streamable[prefer]] if prefer in streamable
             else ["sentinel-2-l2a", "landsat-c2-l2"])
    # if prefer pinned one collection, still fall back to the other as a safety net
    for coll in order + [c for c in streamable.values() if c not in order]:
        if fetch_items_by_ids(coll, ids):
            return coll
    return None


# label per collection, for the preview's "source" column
_STAC_SOURCE = {"sentinel-2-l2a": "Sentinel-2", "landsat-c2-l2": "Landsat"}


def _stac_candidate(item, event_time, source):
    """One STAC item -> a JSON-able candidate row for the dry-run preview.

    thumb_url is the item's free rendered preview / browse PNG (no download or
    order needed); the plugin can render it later for a visual preview.
    cog_url is the true-colour COG (Sentinel-2 'visual'/TCI asset) for the
    on-map preview, stored UNSIGNED (query string stripped): Planetary Computer
    SAS tokens expire in ~30-60 min, so the plugin re-signs this fresh at
    preview time via the public /api/sas/v1/sign endpoint. None when the source
    has no single true-colour COG (e.g. Landsat, PlanetScope).
    geometry/bbox are the scene footprint (GeoJSON + lon/lat bounds); the plugin
    draws them on the map so you can see whether the scene actually covers the
    AOI (vs. leaving the epicentre in a diagonal nodata gap)."""
    d = item.datetime.replace(tzinfo=None) if item.datetime is not None else None
    cloud = item.properties.get("eo:cloud_cover")
    thumb = None
    for key in ("rendered_preview", "thumbnail"):
        asset = item.assets.get(key)
        if asset is not None:
            thumb = asset.href
            break
    visual = item.assets.get("visual")
    cog = visual.href.split("?")[0] if visual is not None else None
    return dict(id=item.id, date=d.isoformat() if d else None,
                cloud_pct=round(cloud, 1) if cloud is not None else None,
                gap_days=abs((d - event_time).days) if d else None,
                source=source, thumb_url=thumb, cog_url=cog,
                geometry=item.geometry, bbox=list(item.bbox) if item.bbox else None)


def search_event(lat, lon, radius_km, event_time: dt.datetime, pre_days=60,
                 post_days=60, seasonal=False, auto_window=False, sensor="s2",
                 cloud_weight=0.5, max_cloud_pct=None):
    """Free dry-run: candidate Sentinel-2 OR Landsat scenes per side, no download.

    Searches exactly one sensor ('s2' or 'landsat') using the SAME window and
    ranking logic as fetch_event, so what you preview is what a real run would
    composite. Returns dict(source, pre=[...], post=[...]). STAC search is free;
    no scenes are streamed or composited here. The caller chooses which sensor(s)
    to query (e.g. both, for prefer='auto').
    max_cloud_pct: whole-tile cloud cap (0-100); None -> mode default (60, or 20
    under auto_window). Tile-wide metric; per-pixel SCL/QA masking still applies."""
    coll = "sentinel-2-l2a" if sensor == "s2" else "landsat-c2-l2"
    src = _STAC_SOURCE[coll]
    pre0, pre1, post0, post1 = windows(event_time, pre_days, post_days, seasonal)
    weight = None if auto_window else cloud_weight   # None = nearest-only (auto_window)
    lim = 1 if auto_window else 6
    cloud = max_cloud_pct if max_cloud_pct is not None else (20 if auto_window else 60)
    pre_items = search_scenes(lat, lon, radius_km, pre0, pre1, coll, event_time,
                              max_cloud=cloud, limit=lim, cloud_weight=weight)
    post_items = search_scenes(lat, lon, radius_km, post0, post1, coll, event_time,
                               max_cloud=cloud, limit=lim, cloud_weight=weight)
    return dict(source=src,
                pre=[_stac_candidate(i, event_time, src) for i in pre_items],
                post=[_stac_candidate(i, event_time, src) for i in post_items])


def _composite(items, lat, lon, radius_km, sensor):
    """Cloud-masked median composite. Returns dataset with red/nir/green/blue (reflectance 0-1)."""
    epsg = _utm_epsg(lat, lon)
    bands = S2_BANDS if sensor == "s2" else LS_BANDS
    res = 10 if sensor == "s2" else 30
    stack = stackstac.stack(
        items, assets=bands, epsg=epsg, resolution=res,
        bounds_latlon=_bbox(lat, lon, radius_km),
        chunksize=2048, rescale=False,
    )
    if sensor == "s2":
        scl = stack.sel(band="SCL")
        good = ~scl.isin(S2_BAD_SCL)
        data = stack.sel(band=["B04", "B08", "B03", "B02"]).where(good) / 10000.0
        data = data.assign_coords(band=["red", "nir", "green", "blue"])
    else:
        qa = stack.sel(band="qa_pixel").astype("uint16")
        # QA_PIXEL bits: 1 dilated cloud, 3 cloud, 4 cloud shadow  (bit 5 snow kept)
        bad = ((qa & (1 << 1)) > 0) | ((qa & (1 << 3)) > 0) | ((qa & (1 << 4)) > 0)
        data = stack.sel(band=["red", "nir08", "green", "blue"]).where(~bad)
        data = data * 0.0000275 - 0.2  # Landsat C2 L2 scale/offset
        data = data.assign_coords(band=["red", "nir", "green", "blue"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        comp = data.median(dim="time", skipna=True).compute()
    comp = comp.rio.write_crs(epsg)
    return comp


def _ndvi(comp):
    r, n = comp.sel(band="red"), comp.sel(band="nir")
    return ((n - r) / (n + r)).clip(-1, 1)


def _brightness(comp):
    """Broadband surface reflectance brightness (albedo proxy), 0-1.

    Mean of the four surface-reflectance bands. Fresh landslide scars expose
    bare soil/rock, which is markedly brighter than the vegetation it replaced,
    so a positive pre->post brightness change is an independent scar signal that
    complements the NDVI drop (and catches sparsely-vegetated rock/scree where
    the NDVI signal alone is weak)."""
    return comp.sel(band=["red", "green", "blue", "nir"]).mean(dim="band").clip(0, 1)


def _has_coverage(comp):
    """True if the composite has any usable (finite) pixels over the AOI.

    A scene can be returned by the STAC search because its TILE FOOTPRINT bbox
    intersects the AOI, yet the AOI falls in that acquisition's diagonal nodata
    gap — the composite is then all-NaN. That must not be treated as a successful
    fetch, or we emit a blank (all-black) review package."""
    return bool(np.isfinite(comp.sel(band="red").values).any())


def _composite_result(pre_items, post_items, lat, lon, radius_km, sensor,
                      fallback_note=None, auto_window=False):
    """Composite the given pre/post items and derive the review products.

    Shared by the ranked search path and the manual-selection path so both produce
    the identical result dict. Returns None (caller falls back / reports
    no_imagery) when either composite has no usable pixels over the AOI."""
    pre = _composite(pre_items, lat, lon, radius_km, sensor)
    post = _composite(post_items, lat, lon, radius_km, sensor)
    if not _has_coverage(pre) or not _has_coverage(post):
        hint = " — try without --auto-window to composite more scenes" if auto_window else ""
        print(f"    [{sensor}] scenes found but no usable pixels over the AOI "
              f"(scene nodata gap){hint}")
        return None
    ndvi_pre, ndvi_post = _ndvi(pre), _ndvi(post)
    dndvi = (ndvi_post - ndvi_pre).rename("dndvi")
    bright_pre, bright_post = _brightness(pre), _brightness(post)
    dbright = (bright_post - bright_pre).rename("dbright")
    return dict(pre=pre, post=post, ndvi_pre=ndvi_pre, ndvi_post=ndvi_post,
                dndvi=dndvi, bright_pre=bright_pre, bright_post=bright_post,
                dbright=dbright, sensor=sensor,
                pre_scenes=[i.id for i in pre_items],
                post_scenes=[i.id for i in post_items],
                fallback_note=fallback_note)


def fetch_event(lat, lon, radius_km, event_time: dt.datetime,
                pre_days=60, post_days=60, seasonal=False, prefer="auto",
                workdir=None, auto_window=False, cloud_weight=0.5,
                max_cloud_pct=None, require_point=False, allow_test_quality=False,
                pre_ids=None, post_ids=None):
    """Returns dict with pre/post composites, ndvi_pre/post, dndvi, sensor, scene lists.

    prefer: 'auto' | 's2' | 'landsat'. 'auto' uses Sentinel-2 (~10 m) when the
    event is in the Sentinel-2 era (>= 2016) and falls back to Landsat (~30 m),
    otherwise Landsat. (PlanetScope now lives in the plugin's own PlanetScope tab,
    a separate Data/Orders/Tiles system, and is no longer part of this pipeline;
    workdir/require_point/allow_test_quality are accepted but unused here.)
    auto_window: if True, use only the single clear scene nearest the event on
    each side (tightest possible window) instead of compositing the whole window;
    pre_days/post_days then act as the maximum search range each side.
    cloud_weight: gap-days one will travel from the event date to avoid 1% cloud
    when ranking which scenes to composite (gap_days + cloud_weight * cloud_pct);
    a small weight keeps the composite close to the event date. Ignored when
    auto_window is set (that mode ranks by proximity only).
    max_cloud_pct: whole-scene/tile cloud-cover cap (0-100); None -> the source
    default. Scene-wide metric; per-pixel masking still applies, so a higher cap
    recovers scenes clear over the AOI but cloudy elsewhere.
    require_point: PlanetScope only — require each scene to cover the exact
    epicentre (True) vs. merely overlap the AOI box (False, default). Passed
    through to planet_imagery.fetch_event; ignored by the STAC sources.
    allow_test_quality: PlanetScope only — also order 'test'-quality scenes, not
    just 'standard'. Passed through; the STAC sources have no quality filter.
    pre_ids/post_ids: hand-picked scene IDs to composite for each side, overriding
    the automatic ranking. When both are given we composite exactly those scenes
    (Sentinel-2 OR Landsat — the streamable STAC sources) and skip PlanetScope and
    the windowed search entirely; `prefer` then only hints which collection to
    probe first.

    Returns None (caller falls back / reports no_imagery) when no window has
    scenes OR when the chosen scenes composite to no usable pixels over the AOI
    (a scene-nodata gap) — never a blank composite.
    """
    # --- manual override: composite exactly the hand-picked scenes -------------
    if pre_ids and post_ids:
        coll = _ids_collection(prefer, list(pre_ids) + list(post_ids))
        if coll is None:
            print("    [manual] none of the requested scene IDs were found on the "
                  "Planetary Computer (Sentinel-2 / Landsat)")
            return None
        sensor = "s2" if coll == "sentinel-2-l2a" else "landsat"
        pre_items = fetch_items_by_ids(coll, pre_ids)
        post_items = fetch_items_by_ids(coll, post_ids)
        if not pre_items or not post_items:
            print(f"    [manual] requested scene IDs not all found in {coll} "
                  f"({len(pre_items)} pre, {len(post_items)} post)")
            return None
        print(f"    [manual] compositing {len(pre_items)} pre + {len(post_items)} "
              f"post hand-picked {sensor} scene(s)")
        return _composite_result(pre_items, post_items, lat, lon, radius_km, sensor)

    # PlanetScope has been split out of this pipeline into the plugin's dedicated
    # PlanetScope tab (its own Data/Orders/Tiles system). This module now handles
    # only the Planetary Computer STAC sources — Sentinel-2 and Landsat. A stray
    # prefer='planet' (there should be none) degrades to the S2->Landsat default.
    if prefer == "planet":
        print("    [imagery] prefer='planet' is not handled here anymore "
              "(PlanetScope lives in its own tab); using Sentinel-2 -> Landsat")
        prefer = "auto"

    use_s2 = prefer in ("auto", "s2") and event_time >= dt.datetime(2016, 1, 1)
    # sensors to try in order: the chosen optical source, then Landsat as a
    # fallback when 'auto'. This covers BOTH "no scenes found" and "scenes found
    # but their nodata gap falls over the AOI" (an all-NaN composite).
    first = "s2" if use_s2 else "landsat"
    candidates = [first] + (["landsat"] if prefer == "auto" and first == "s2" else [])

    pre0, pre1, post0, post1 = windows(event_time, pre_days, post_days, seasonal)

    # default: blend ranking (gap + cloud_weight*cloud) keeps the composite near
    # the event date. auto_window instead picks the single nearest scene on each
    # side, among reasonably clear ones only (stricter cloud cap) so "nearest"
    # doesn't grab a clouded-over scene one day closer than a clear one.
    weight = None if auto_window else cloud_weight
    lim = 1 if auto_window else 6
    cloud = max_cloud_pct if max_cloud_pct is not None else (20 if auto_window else 60)

    for sensor in candidates:
        coll = "sentinel-2-l2a" if sensor == "s2" else "landsat-c2-l2"
        pre_items = search_scenes(lat, lon, radius_km, pre0, pre1, coll, event_time,
                                  max_cloud=cloud, limit=lim, cloud_weight=weight)
        post_items = search_scenes(lat, lon, radius_km, post0, post1, coll, event_time,
                                   max_cloud=cloud, limit=lim, cloud_weight=weight)
        if not pre_items or not post_items:
            continue
        res = _composite_result(pre_items, post_items, lat, lon, radius_km, sensor,
                                fallback_note=None, auto_window=auto_window)
        if res is not None:
            return res
        # scenes found but their nodata gap fell over the AOI — try the next sensor
    return None
