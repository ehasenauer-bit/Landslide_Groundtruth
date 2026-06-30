"""Fetch pre/post PlanetScope imagery around a landslide event via the Planet APIs.

PlanetScope (~3 m, 4-band BGRN surface reflectance) is the highest-resolution
source in this pipeline and is tried FIRST; `imagery.fetch_event` falls back to
Sentinel-2 (~10 m) then Landsat (~30 m) when Planet has no coverage for the
event/date window or when the account is not authenticated.

Flow per window (pre and post):
  1. Data API search PSScene by AOI + date + cloud_cover (+ permission/quality).
  2. Orders API: order the best N scenes (ranked by a blend of distance to the
     event date and cloud cover) as the `analytic_sr_udm2` bundle, server-side
     clipped to the AOI (minimal download), then download.
  3. Cloud/shadow-mask each clip with its UDM2 'clear' band, reproject to the
     event UTM zone, and median-composite. NDVI/dNDVI as in `imagery.py`.

Returns the SAME dict contract as `imagery.fetch_event` (pre/post composites,
ndvi_pre/post, dndvi, dbright, sensor, pre_scenes/post_scenes) so the review-package
export is identical regardless of which optical source was used.

Auth: run `planet auth login` once (OAuth) or set the PL_API_KEY env var. The
password is never read by this code.
"""
from __future__ import annotations
import datetime as dt
import glob
import os
import tempfile
import warnings
import numpy as np
import rioxarray  # noqa: F401  (registers .rio accessor)
import xarray as xr

import imagery as im  # reuse _bbox, _utm_epsg, _ndvi

ITEM_TYPE = "PSScene"
BUNDLE = "analytic_sr_udm2"          # 4-band surface reflectance + usable-data mask
FALLBACK_BUNDLE = "analytic_udm2"    # non-SR (DN) if SR not licensed on the account
PS_SR_SCALE = 1e-4                   # PlanetScope SR DN -> reflectance
PS_RES = 3.0                         # PlanetScope native ground sample distance (~3 m)

DEFAULT_MAX_CLOUD_PCT = 80.0         # whole-scene cloud cap when not overridden
AUTO_WINDOW_MAX_CLOUD_PCT = 20.0     # stricter cap under --auto-window (nearest CLEAR scene)


def _resolve_cloud_frac(max_cloud_pct, auto_window):
    """User-facing 'max cloud %' (0-100) -> Planet's 0-1 cloud_cover cap.

    cloud_cover is a WHOLE-SCENE metric over the full PSScene strip (hundreds of
    km²), NOT the AOI: a scene can be clear over a 3 km event box yet cloudy
    scene-wide. Per-pixel UDM2 masking + the AOI clip still remove cloud later,
    so a high cap mainly surfaces scenes clear over the AOI but cloudy elsewhere
    (what Planet Explorer shows). Defaults to a stricter cap under --auto-window
    so 'nearest' doesn't grab a clouded scene a day closer than a clear one."""
    pct = max_cloud_pct if max_cloud_pct is not None else (
        AUTO_WINDOW_MAX_CLOUD_PCT if auto_window else DEFAULT_MAX_CLOUD_PCT)
    return pct / 100.0


def _client():
    import planet
    return planet.Planet()


def _bbox_geojson(lat, lon, radius_km):
    w, s, e, n = im._bbox(lat, lon, radius_km)
    return {"type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _point_geojson(lat, lon):
    """The event epicentre as a GeoJSON point, for requiring scene coverage of it."""
    return {"type": "Point", "coordinates": [lon, lat]}


def _acquired(item):
    """Parse a PSScene 'acquired' timestamp to a naive UTC datetime."""
    s = item["properties"]["acquired"].replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s).replace(tzinfo=None)


def search_scenes(pl, aoi, start, end, event_time, max_cloud=0.6, limit=6,
                  cloud_weight=0.5, cover=None, allow_test_quality=False):
    """PSScene items the account can download, in a window, ranked best-first.

    cloud_weight ranks the candidates against the event date:
      float -> BLEND by a combined cost  gap_days + cloud_weight * cloud_pct,
        where gap_days = |acquired - event_time| and cloud_pct is on a 0-100
        scale (Planet reports cloud_cover as 0-1). A small weight favours scenes
        near the event date; a large weight recovers the old least-cloudy order.
      None  -> rank by temporal proximity to event_time alone (the tightest
        --auto-window mode).
    cover: if a GeoJSON geometry (the event point) is given, REQUIRE each scene's
    footprint to contain it instead of merely intersecting the AOI bbox. A single
    PlanetScope strip only covers a few km, so over a large search box the plain
    bbox filter can return a scene clipping only the AOI's edge — leaving the
    epicentre uncovered. Requiring coverage of the point guarantees the chosen
    scenes actually overlap the location being ground-truthed (and, when none do,
    the caller falls back to Sentinel-2/Landsat rather than mapping an off-centre
    strip).
    """
    from planet import data_filter as df
    filters = [
        df.geometry_filter(cover if cover is not None else aoi),
        df.date_range_filter("acquired", gte=start, lte=end),
        df.range_filter("cloud_cover", lte=max_cloud),
        df.permission_filter(),     # only items we have download rights to
    ]
    if not allow_test_quality:
        # std_quality_filter keeps only quality_category == "standard". Recent
        # PlanetScope acquisitions (and some sensors) are published as "test"
        # quality, so near a fresh event the nearest/clearest scenes are often
        # test-only; allow opting them in. Test = looser geo/radiometric
        # calibration — fine for the visual review, but eyeball it before trusting
        # reflectance/NDVI values quantitatively.
        filters.append(df.std_quality_filter())
    flt = df.and_filter(filters)
    # Retrieve EVERY in-window match (limit=0 = no cap), then rank client-side
    # below. The Data API defaults to 'published desc', so a small cap (the old
    # limit*4) returned only the most recently PUBLISHED scenes — which skew to the
    # far/reprocessed end of a wide window — meaning the temporally-NEAREST scenes
    # (the ones we want) could never be fetched before the gap/cost sort. This
    # mirrors the STAC path, which materializes all items, so PlanetScope ranks
    # over the same full candidate set and can find a scene 1 day off the event.
    items = list(pl.data.search([ITEM_TYPE], search_filter=flt, limit=0))

    def gap_days(i):
        return abs((_acquired(i) - event_time).total_seconds()) / 86400.0

    def cloud_pct(i):                                   # cloud_cover is 0-1 -> 0-100
        c = i["properties"].get("cloud_cover")
        return 100.0 if c is None else c * 100.0

    if cloud_weight is None:
        # auto_window: nearest DAY first, then clearest among same-day acquisitions.
        # PlanetScope images a small AOI several times a day, so a pure time sort
        # would pick whichever scene passed seconds nearer regardless of cloud.
        items.sort(key=lambda i: (round(gap_days(i)), cloud_pct(i)))
    else:
        items.sort(key=lambda i: gap_days(i) + cloud_weight * cloud_pct(i))
    return items[:limit]


def _geom_bbox(geom):
    """[minx, miny, maxx, maxy] of a GeoJSON geometry, or None. Walks the nested
    coordinate lists so it handles Polygon and MultiPolygon without shapely."""
    if not geom:
        return None
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                xs.append(c[0])
                ys.append(c[1])
            else:
                for sub in c:
                    walk(sub)

    walk(geom.get("coordinates"))
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def _candidate(item, event_time):
    """One PSScene item -> a JSON-able candidate row for the dry-run preview.

    cloud_pct is normalized to 0-100 (Planet reports cloud_cover as 0-1, unlike
    STAC's eo:cloud_cover). thumb_url is the free browse PNG link (no order).
    geometry/bbox are the scene footprint, so the plugin can draw it on the map
    and you can see whether the strip actually covers the AOI."""
    d = _acquired(item)
    cloud = item["properties"].get("cloud_cover")
    thumb = (item.get("_links") or {}).get("thumbnail")
    geom = item.get("geometry")
    return dict(id=item["id"], date=d.isoformat(),
                cloud_pct=round(cloud * 100, 1) if cloud is not None else None,
                gap_days=abs((d - event_time).days),
                source="PlanetScope", thumb_url=thumb,
                geometry=geom, bbox=_geom_bbox(geom))


def search_event(lat, lon, radius_km, event_time: dt.datetime, pre_days=60,
                 post_days=60, seasonal=False, auto_window=False, cloud_weight=0.5,
                 max_cloud_pct=None, require_point=False, allow_test_quality=False):
    """Free dry-run: candidate PlanetScope scenes per side, NO orders placed.

    Data API search is free and consumes no quota; only Orders do. Uses the same
    window/ranking logic as fetch_event so the preview matches a real run.
    Returns dict(source, pre=[...], post=[...]). Auth/SDK errors propagate to the
    caller (the dry-run dispatcher), which records them as a per-source note.

    max_cloud_pct / require_point / allow_test_quality: see fetch_event — kept
    identical here so the preview shows exactly the scenes a Run would consider."""
    pl = _client()
    aoi = _bbox_geojson(lat, lon, radius_km)
    point = _point_geojson(lat, lon)
    pre0, pre1, post0, post1 = im.windows(event_time, pre_days, post_days, seasonal)
    weight = None if auto_window else cloud_weight   # None = nearest-only (auto_window)
    lim = 1 if auto_window else 6
    cloud = _resolve_cloud_frac(max_cloud_pct, auto_window)
    cover = point if require_point else None         # default: AOI overlap (Planet Explorer-like)
    pre_items = search_scenes(pl, aoi, pre0, pre1, event_time, max_cloud=cloud,
                              limit=lim, cloud_weight=weight, cover=cover,
                              allow_test_quality=allow_test_quality)
    post_items = search_scenes(pl, aoi, post0, post1, event_time, max_cloud=cloud,
                               limit=lim, cloud_weight=weight, cover=cover,
                               allow_test_quality=allow_test_quality)
    return dict(source="PlanetScope",
                pre=[_candidate(i, event_time) for i in pre_items],
                post=[_candidate(i, event_time) for i in post_items])


def _create_order(pl, item_ids, aoi):
    """Create an AOI-clipped SR+UDM2 order for `item_ids` and return its id.

    Deliberately does NOT wait: separating order creation from the blocking wait
    lets the caller submit the pre AND post orders up front so Planet processes
    them concurrently server-side, instead of waiting out the first order (~1-5
    min) before the second is even queued."""
    from planet import order_request as orq
    req = orq.build_request(
        name=f"landslide_{dt.datetime.now():%Y%m%d_%H%M%S}",
        products=[orq.product(item_ids, BUNDLE, ITEM_TYPE,
                              fallback_bundle=FALLBACK_BUNDLE)],
        tools=[orq.clip_tool(aoi)],
    )
    return pl.orders.create_order(req)["id"]


def _wait_download(pl, order_id, out_dir):
    """Block until `order_id` is ready, download it to out_dir, pair SR/UDM2 files."""
    os.makedirs(out_dir, exist_ok=True)
    pl.orders.wait(order_id)             # blocks until success/failure
    pl.orders.download_order(order_id, directory=out_dir, overwrite=True)
    return _pair_downloads(out_dir)


def _pair_downloads(out_dir):
    """Map item id -> (analytic tif, udm2 tif) from a downloaded order tree.

    Planet clip deliveries name files '<item_id>_3B_AnalyticMS_SR_clip.tif' and
    '<item_id>_3B_udm2_clip.tif'. Match case-insensitively and key on the item-id
    prefix so each SR scene pairs with its UDM2 mask. If the naming ever changes
    and nothing matches, warn loudly instead of silently returning no scenes
    (which would look like 'no coverage' and trigger a needless fallback)."""
    tifs = glob.glob(os.path.join(out_dir, "**", "*.tif"), recursive=True)
    sr, udm = {}, {}
    for t in tifs:
        b = os.path.basename(t)
        low = b.lower()
        key = b.split("_3B")[0] if "_3B" in b else os.path.splitext(b)[0]
        if "udm2" in low:
            udm[key] = t
        elif "analyticms" in low:
            sr[key] = t
    pairs = [(sr[k], udm.get(k)) for k in sr]
    if not pairs and tifs:
        print(f"    [planet] warning: downloaded {len(tifs)} tif(s) in {out_dir} but none "
              f"matched the expected AnalyticMS / UDM2 naming — check the order bundle")
    return pairs


def _target_grid(lat, lon, radius_km, epsg, res=PS_RES):
    """Fixed (transform, (height, width)) covering the AOI bbox in `epsg` at `res` m.

    Every scene is reprojected onto THIS grid instead of onto whichever scene
    happens to be processed first, so the composite always spans the full search
    box and stays centred on the event point — even when a PlanetScope strip only
    covers part of the AOI (the empty remainder is just NaN). This mirrors the
    Sentinel-2 path's `bounds_latlon=_bbox(...)`, whose output is centred on the
    point for exactly the same reason.
    """
    from rasterio.transform import from_origin
    from pyproj import Transformer
    w, s, e, n = im._bbox(lat, lon, radius_km)
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = tf.transform([w, e, e, w], [s, s, n, n])     # all four corners
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    width = max(1, round((maxx - minx) / res))
    height = max(1, round((maxy - miny) / res))
    return from_origin(minx, maxy, res, res), (height, width)


def _open_scene(sr_path, udm_path, epsg, transform, shape):
    """One clipped PSScene -> reflectance DataArray (bands blue/green/red/nir),
    cloud-masked and reprojected onto the shared (transform, shape) target grid."""
    da = rioxarray.open_rasterio(sr_path, masked=True).astype("float32")
    if "_sr" in os.path.basename(sr_path).lower():
        da = da * PS_SR_SCALE            # SR DN -> reflectance; DN bundle left as-is
    da = da.assign_coords(band=["blue", "green", "red", "nir"])
    if udm_path and os.path.exists(udm_path):
        udm = rioxarray.open_rasterio(udm_path)
        clear = udm.sel(band=1).rio.reproject_match(da)   # UDM2 band 1: 1 = clear
        da = da.where(clear == 1)
    # warp straight onto the AOI grid; pixels the scene doesn't reach become NaN
    return da.rio.reproject(epsg, transform=transform, shape=shape)


def _composite(pairs, lat, lon, radius_km, epsg):
    """Cloud-masked median composite over the scenes in one window, on the AOI grid."""
    transform, shape = _target_grid(lat, lon, radius_km, epsg)
    scenes = [_open_scene(sr, udm, epsg, transform, shape) for sr, udm in pairs]
    if not scenes:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        comp = xr.concat(scenes, dim="scene").median(dim="scene", skipna=True)
    # Drop inherited per-scene rasterio metadata (e.g. a 4-band 'long_name' and a
    # float NaN nodata) so derived rasters — dNDVI, dBrightness, RGB — don't carry
    # band-count/dtype-mismatched attrs into rioxarray's GeoTIFF writer downstream.
    comp.attrs = {}
    return comp.rio.write_crs(epsg)


def fetch_event(lat, lon, radius_km, event_time: dt.datetime,
                pre_days=60, post_days=60, seasonal=False, workdir=None,
                auto_window=False, cloud_weight=0.5,
                max_cloud_pct=None, require_point=False, allow_test_quality=False):
    """PlanetScope pre/post composites + dNDVI, or None if no usable coverage.

    Returns None (so the caller falls back to Sentinel-2/Landsat) when either
    window has no orderable scenes. Auth/SDK errors propagate to the caller,
    which logs them and falls back.

    cloud_weight: gap-days one will travel from the event date to avoid 1% cloud
    when ranking which scenes to order/composite (gap_days + cloud_weight *
    cloud_pct); a small weight keeps the composite close to the event date.
    auto_window: if True, order only the single clear scene nearest the event on
    each side (tightest window, and the fewest orders), ignoring cloud_weight;
    pre_days/post_days then act as the maximum search range each side.
    max_cloud_pct: whole-scene cloud-cover cap (0-100). None -> the mode default
    (80, or 20 under auto_window). The cap is scene-wide; per-pixel UDM2 masking
    still applies, so a high cap recovers scenes clear over the AOI but cloudy
    elsewhere (matching Planet Explorer).
    require_point: if True, require each scene footprint to CONTAIN the epicentre;
    if False (default) accept any scene overlapping the AOI search box (Planet
    Explorer-like). False surfaces partial-coverage scenes near the event date;
    the composite is built on a fixed AOI grid and gaps are filled by the median
    of the other scenes (or trigger the S2/Landsat fallback if truly uncovered).
    allow_test_quality: if True, also order scenes Planet publishes as 'test'
    quality (else only quality_category == 'standard'). Near a fresh event the
    nearest/clearest PlanetScope scenes are frequently test-only; they're fine for
    the visual review but carry looser geo/radiometric calibration.
    """
    pl = _client()
    aoi = _bbox_geojson(lat, lon, radius_km)
    point = _point_geojson(lat, lon)

    pre0, pre1, post0, post1 = im.windows(event_time, pre_days, post_days, seasonal)

    # default: blend ranking (gap + cloud_weight*cloud) keeps the composite near
    # the event date. auto_window instead orders only the nearest scene each side,
    # among reasonably clear ones (stricter cloud cap) so "nearest" doesn't pick a
    # clouded scene one day closer than a clear one.
    weight = None if auto_window else cloud_weight
    lim = 1 if auto_window else 6
    cloud = _resolve_cloud_frac(max_cloud_pct, auto_window)
    cover = point if require_point else None         # default: AOI overlap (Planet Explorer-like)
    pre_items = search_scenes(pl, aoi, pre0, pre1, event_time, max_cloud=cloud,
                              limit=lim, cloud_weight=weight, cover=cover,
                              allow_test_quality=allow_test_quality)
    post_items = search_scenes(pl, aoi, post0, post1, event_time, max_cloud=cloud,
                               limit=lim, cloud_weight=weight, cover=cover,
                               allow_test_quality=allow_test_quality)
    if not pre_items or not post_items:
        # Name the empty side(s) so the log/banner says WHY Planet is being skipped
        # (no orderable scene there within the window + cloud cap + quality filter),
        # rather than a bare "no coverage".
        sides = [s for s, items in (("pre", pre_items), ("post", post_items)) if not items]
        print(f"    [planet] no orderable scene in the {' and '.join(sides)} window "
              f"(within the date range, cloud cap, coverage, and quality filters)")
        return None

    workdir = workdir or tempfile.mkdtemp(prefix="planet_")
    epsg = im._utm_epsg(lat, lon)
    # Submit BOTH orders before waiting on either, so Planet processes the pre and
    # post clips concurrently. Total order latency then ~max(pre, post) instead of
    # the old pre+post (each order's blocking wait runs ~1-5 min). Waiting pre
    # first is fine — post is already cooking server-side meanwhile.
    pre_order = _create_order(pl, [i["id"] for i in pre_items], aoi)
    post_order = _create_order(pl, [i["id"] for i in post_items], aoi)
    pre_pairs = _wait_download(pl, pre_order, os.path.join(workdir, "pre"))
    post_pairs = _wait_download(pl, post_order, os.path.join(workdir, "post"))

    pre = _composite(pre_pairs, lat, lon, radius_km, epsg)
    post = _composite(post_pairs, lat, lon, radius_km, epsg)
    # None = no scenes composited; no coverage = clips landed entirely outside the
    # AOI / all cloud-masked. Either way fall back to Sentinel-2/Landsat.
    if pre is None or post is None or not im._has_coverage(pre) or not im._has_coverage(post):
        bad = [s for s, c in (("pre", pre), ("post", post))
               if c is None or not im._has_coverage(c)]
        print(f"    [planet] ordered scenes clipped to no clear pixels over the AOI on "
              f"the {' and '.join(bad)} side (cloud-masked out or a scene nodata gap)")
        return None

    ndvi_pre, ndvi_post = im._ndvi(pre), im._ndvi(post)
    dndvi = (ndvi_post - ndvi_pre).rename("dndvi")
    bright_pre, bright_post = im._brightness(pre), im._brightness(post)
    dbright = (bright_post - bright_pre).rename("dbright")
    return dict(pre=pre, post=post, ndvi_pre=ndvi_pre, ndvi_post=ndvi_post,
                dndvi=dndvi, bright_pre=bright_pre, bright_post=bright_post,
                dbright=dbright, sensor="planet",
                pre_scenes=[i["id"] for i in pre_items],
                post_scenes=[i["id"] for i in post_items])
