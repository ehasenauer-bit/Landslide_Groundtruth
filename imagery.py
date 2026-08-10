"""Fetch pre/post satellite imagery around a landslide event via STAC.

Data sources (all free, no full-scene downloads — COGs streamed by window):
  - Sentinel-2 L2A   (2015-07 onward; ~10 m)   Microsoft Planetary Computer
  - Landsat C2 L2    (Landsat 8: 2013-03 on, 9: 2021-11 on; ~30 m)  Planetary Computer

Strategy per event:
  pre  window: [t - pre_days, t - 1 day]
  post window: [t + 1 day, t + post_days]
  Rank scenes in each window by a blend of temporal distance to the event and
  cloud cover (gap_days + cloud_weight * cloud_pct) so the composite stays close
  to the event date, take the best N, median-composite them AS ACQUIRED,
  compute NDVI, then dNDVI = post - pre.

  No cloud removal: pixels are never dropped for cloud, shadow or cirrus (only
  scene fill/nodata is), so a downloaded layer shows the scene the way it was
  acquired — clouds included — instead of holes where a mask fired. See
  `_composite`.

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

S2_BANDS = ["B04", "B08", "B03", "B02", "B11", "B12", "SCL"]   # red, nir, green, blue, swir1, swir2, scene class
LS_BANDS = ["red", "nir08", "green", "blue", "swir16", "swir22", "qa_pixel"]

# Scene-classification values that mean "there is no measurement here": 0 nodata,
# 1 saturated/defective. Those are the ONLY classes dropped — the cloud classes
# (3 shadow, 8/9 cloud medium/high, 10 cirrus) and 11 snow/ice are deliberately
# kept, so a run downloads the scene as acquired rather than a cloud-masked one.
S2_NODATA_SCL = [0, 1]

# Dry-run (search_event) candidate cap per side, mirroring sar_imagery: the
# preview's job is to show what was actually acquired near the event so the
# scenes can be judged by eye, so it lists more than the 6 a Run composites, and
# the cap grows with the window instead of silently truncating it. Each extra
# candidate costs one more server-side quicklook render in the plugin's gallery,
# hence the ceiling. Sentinel-2's revisit is ~5 days (longer where neighbouring
# orbits don't overlap), so days/5 tracks the real supply.
PREVIEW_LIMIT = 12
PREVIEW_MAX_LIMIT = 24


def _preview_limit_for(days):
    return max(PREVIEW_LIMIT, min(PREVIEW_MAX_LIMIT, round(days / 5)))


def _no_cloud_cap(max_cloud):
    """True when 'max cloud %' means no filtering at all (None or >= 100)."""
    return max_cloud is None or max_cloud >= 100


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

    max_cloud None or >= 100 drops the cloud predicate ENTIRELY rather than
    querying 'lt 100'. The two are not the same: a property query also discards
    items that don't carry eo:cloud_cover at all, and an overcast scene reports
    exactly 100. "No cap" has to mean every acquisition in the window is
    listed — that's what lets a scene be judged by eye instead of by metadata.
    """
    cat = _client()
    query = None if _no_cloud_cap(max_cloud) else {"eo:cloud_cover": {"lt": max_cloud}}
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

    Searches exactly one sensor ('s2' or 'landsat') over the SAME window as
    fetch_event. Returns dict(source, pre=[...], post=[...]). STAC search is
    free; no scenes are streamed or composited here. The caller chooses which
    sensor(s) to query (e.g. both, for prefer='auto').

    Deliberately does NOT reuse the run's cloud-weighted ranking to choose WHICH
    candidates to list. That blend (gap_days + cloud_weight*cloud_pct) is the
    right way to auto-pick scenes to composite, but as a listing rule it hides
    the cloudy near-date acquisitions — the very scenes you need to see to judge
    whether a run's automatic pick was sensible, or to hand-pick a scene whose
    cloud sits off the AOI. So the preview lists the nearest-in-time
    _preview_limit_for(days) scenes per side; `cloud_weight` is accepted for
    symmetry with fetch_event but not applied here — the plugin re-ranks the
    returned rows with that same blend to mark the run's pick (★) among them.

    max_cloud_pct: whole-tile cloud cap (0-100); None or >= 100 -> no cap, every
    acquisition in the window is listed. Tile-wide metric, and the only cloud
    handling there is: a Run composites the scenes as acquired, with no per-pixel
    cloud masking (see _composite), so what you see listed is what you get."""
    coll = "sentinel-2-l2a" if sensor == "s2" else "landsat-c2-l2"
    src = _STAC_SOURCE[coll]
    pre0, pre1, post0, post1 = windows(event_time, pre_days, post_days, seasonal)
    # cloud_weight=None -> nearest day first, clearest as the tie-break: both what
    # auto_window's single pick needs (it is fetch_event's own rule there) and what
    # the browse listing wants.
    pre_lim = 1 if auto_window else _preview_limit_for(pre_days)
    post_lim = 1 if auto_window else _preview_limit_for(post_days)
    cloud = 20 if (max_cloud_pct is None and auto_window) else max_cloud_pct
    pre_items = search_scenes(lat, lon, radius_km, pre0, pre1, coll, event_time,
                              max_cloud=cloud, limit=pre_lim, cloud_weight=None)
    post_items = search_scenes(lat, lon, radius_km, post0, post1, coll, event_time,
                               max_cloud=cloud, limit=post_lim, cloud_weight=None)
    return dict(source=src,
                pre=[_stac_candidate(i, event_time, src) for i in pre_items],
                post=[_stac_candidate(i, event_time, src) for i in post_items])


def _composite(items, lat, lon, radius_km, sensor):
    """Median composite of the scenes AS ACQUIRED — no cloud removal.

    Returns a dataset with red/nir/green/blue/swir1/swir2 (reflectance 0-1).

    Cloud, cloud-shadow and cirrus pixels are kept: the scenes are chosen by eye
    in the plugin (or by the cloud-weighted ranking), and a mask that punches
    them out leaves transparent holes exactly where you are trying to look, which
    reads as terrain change rather than as weather. Only pixels that carry no
    measurement at all — the scene fill/nodata classes — are dropped, so an
    acquisition's diagonal nodata gap stays transparent instead of turning into a
    black wedge of "reflectance 0". With one scene per side (--auto-window, or a
    single ticked scene) the output is therefore that scene verbatim."""
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
        measured = ~scl.isin(S2_NODATA_SCL)   # fill/defective only — never cloud
        # B11/B12 are natively 20 m; stackstac has resampled them to the 10 m grid
        # above, so they align with red/nir/green/blue for the SWIR products.
        data = stack.sel(band=["B04", "B08", "B03", "B02", "B11", "B12"]).where(measured) / 10000.0
        data = data.assign_coords(band=["red", "nir", "green", "blue", "swir1", "swir2"])
    else:
        qa = stack.sel(band="qa_pixel").astype("uint16")
        # QA_PIXEL bit 0 = fill (outside the imaged swath) — the only bit applied.
        # The cloud bits (1 dilated cloud, 3 cloud, 4 cloud shadow) are left alone
        # on purpose, as is bit 5 snow; see the docstring.
        fill = (qa & 1) > 0
        # swir16/swir22 are Landsat's SWIR1/SWIR2; same C2 L2 scale/offset as the
        # other surface-reflectance bands, so they rescale together below.
        data = stack.sel(band=["red", "nir08", "green", "blue", "swir16", "swir22"]).where(~fill)
        data = data * 0.0000275 - 0.2  # Landsat C2 L2 scale/offset
        data = data.assign_coords(band=["red", "nir", "green", "blue", "swir1", "swir2"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        comp = data.median(dim="time", skipna=True).compute()
    comp = comp.rio.write_crs(epsg)
    return comp


def _ndvi(comp):
    r, n = comp.sel(band="red"), comp.sel(band="nir")
    return ((n - r) / (n + r)).clip(-1, 1)


def _has_swir(comp):
    """True if the composite carries the SWIR bands (both S2 and Landsat do now).

    A guard for the old manual-selection / cached paths and any future sensor that
    might composite without SWIR, so the SWIR products degrade to 'skip' instead of
    raising a KeyError deep in the export."""
    return "swir1" in list(comp.coords["band"].values)


def _ndsi(comp):
    """Normalised-Difference Snow Index, (green - swir1) / (green + swir1).

    Snow/ice is bright in green and near-zero in SWIR1, so clean snow sits high
    (~0.4-1.0); bare rock, fresh landslide/rock-avalanche debris and debris-covered
    ice sit low. On a glacier a pre->post NDSI DROP is therefore a direct 'new dark
    debris on snow' signal — the complement to the brightness/NDVI change, and the
    one that works where there is no vegetation to lose (Sentinel-2 uses B03/B11,
    Landsat green/swir16)."""
    g, s = comp.sel(band="green"), comp.sel(band="swir1")
    return ((g - s) / (g + s)).clip(-1, 1)


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


def _item_dates(items):
    """Acquisition dates ('YYYY-MM-DD') of the given STAC items, in item order.

    The review package puts these in the exported layer FILENAMES (and hence the
    QGIS layer names), so a layer says which day it was imaged without opening the
    metadata; items with no datetime are skipped rather than named 'None'."""
    return [i.datetime.strftime("%Y-%m-%d") for i in items if i.datetime is not None]


def _composite_result(pre_items, post_items, lat, lon, radius_km, sensor,
                      fallback_note=None, auto_window=False):
    """Composite the given pre/post items and derive the review products.

    Shared by the ranked search path and the manual-selection path so both produce
    the identical result dict. Returns None (caller falls back / reports
    no_imagery) when a requested composite has no usable pixels over the AOI.

    One side may be empty (a hand-picked one-sided run — see fetch_event). That
    side's composite and NDVI/NDSI come back None, and so do the three change
    rasters, which are differences and need both sides to exist."""
    pre = _composite(pre_items, lat, lon, radius_km, sensor) if pre_items else None
    post = _composite(post_items, lat, lon, radius_km, sensor) if post_items else None
    for side, comp in (("pre", pre), ("post", post)):
        if comp is not None and not _has_coverage(comp):
            hint = " — try without --auto-window to composite more scenes" if auto_window else ""
            print(f"    [{sensor}] {side} scenes found but no usable pixels over "
                  f"the AOI (scene nodata gap){hint}")
            return None
    ndvi_pre = _ndvi(pre) if pre is not None else None
    ndvi_post = _ndvi(post) if post is not None else None
    bright_pre = _brightness(pre) if pre is not None else None
    bright_post = _brightness(post) if post is not None else None
    # NDSI is per-side; guarded on the SWIR bands so a SWIR-less composite still
    # returns a valid (SWIR-free) result.
    ndsi_pre = _ndsi(pre) if pre is not None and _has_swir(pre) else None
    ndsi_post = _ndsi(post) if post is not None and _has_swir(post) else None
    # change rasters — differences, so every one of them needs both sides
    both = pre is not None and post is not None
    dndvi = (ndvi_post - ndvi_pre).rename("dndvi") if both else None
    dbright = (bright_post - bright_pre).rename("dbright") if both else None
    dndsi = ((ndsi_post - ndsi_pre).rename("dndsi")
             if both and ndsi_pre is not None and ndsi_post is not None else None)
    return dict(pre=pre, post=post, ndvi_pre=ndvi_pre, ndvi_post=ndvi_post,
                dndvi=dndvi, bright_pre=bright_pre, bright_post=bright_post,
                dbright=dbright, ndsi_pre=ndsi_pre, ndsi_post=ndsi_post,
                dndsi=dndsi, sensor=sensor,
                pre_scenes=[i.id for i in pre_items],
                post_scenes=[i.id for i in post_items],
                pre_dates=_item_dates(pre_items),
                post_dates=_item_dates(post_items),
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
    default. Scene-wide metric, so a higher cap recovers scenes clear over the AOI
    but cloudy elsewhere; the scenes it admits are composited as acquired (no
    per-pixel cloud masking — see _composite).
    require_point: PlanetScope only — require each scene to cover the exact
    epicentre (True) vs. merely overlap the AOI box (False, default). Passed
    through to planet_imagery.fetch_event; ignored by the STAC sources.
    allow_test_quality: PlanetScope only — also order 'test'-quality scenes, not
    just 'standard'. Passed through; the STAC sources have no quality filter.
    pre_ids/post_ids: hand-picked scene IDs to composite for each side, overriding
    the automatic ranking. Given EITHER or both, we composite exactly those scenes
    (Sentinel-2 OR Landsat — the streamable STAC sources) and skip PlanetScope and
    the windowed search entirely; `prefer` then only hints which collection to
    probe first. One side alone yields a one-sided result: that side's composite
    and indices, with the pre->post change rasters None (see _composite_result).

    Returns None (caller falls back / reports no_imagery) when no window has
    scenes OR when the chosen scenes composite to no usable pixels over the AOI
    (a scene-nodata gap) — never a blank composite.
    """
    # --- manual override: composite exactly the hand-picked scenes -------------
    # Either side alone is enough. A one-sided run is the honest answer when only
    # one side of the event has usable imagery (commonly a fresh event with no
    # pre-scene yet): it exports that side and skips the change rasters, instead of
    # refusing to run or quietly substituting a scene nobody picked.
    if pre_ids or post_ids:
        coll = _ids_collection(prefer, list(pre_ids or []) + list(post_ids or []))
        if coll is None:
            print("    [manual] none of the requested scene IDs were found on the "
                  "Planetary Computer (Sentinel-2 / Landsat)")
            return None
        sensor = "s2" if coll == "sentinel-2-l2a" else "landsat"
        pre_items = fetch_items_by_ids(coll, pre_ids) if pre_ids else []
        post_items = fetch_items_by_ids(coll, post_ids) if post_ids else []
        # only the sides that were ASKED for have to resolve
        if (pre_ids and not pre_items) or (post_ids and not post_items):
            print(f"    [manual] requested scene IDs not all found in {coll} "
                  f"({len(pre_items)} pre, {len(post_items)} post)")
            return None
        sides = " + ".join(f"{len(i)} {s}" for s, i in
                           (("pre", pre_items), ("post", post_items)) if i)
        print(f"    [manual] compositing {sides} hand-picked {sensor} scene(s)")
        if not pre_items or not post_items:
            print(f"    [manual] one-sided run ({'post' if post_items else 'pre'} "
                  f"only) — dNDVI / dNDSI / dBrightness need both sides and are "
                  f"skipped")
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
