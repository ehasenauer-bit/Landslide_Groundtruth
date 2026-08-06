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

Quota: only step 2 costs anything, and it costs it at order CREATION. Every order
placed here is therefore downloaded into the shared cache and written to the ledger
in `planet_cache`, and every entry point checks that ledger before ordering — so a
second look at an event recalls what was already paid for (`recall_preview`) instead
of buying it twice. See `planet_cache` for the layout and match rules.

Auth: run `planet auth login` once (OAuth) or set the PL_API_KEY env var. The
password is never read by this code.
"""
from __future__ import annotations
import datetime as dt
import glob
import os
import warnings
import numpy as np
import rioxarray  # noqa: F401  (registers .rio accessor)
import xarray as xr

import imagery as im  # reuse _bbox, _utm_epsg, _ndvi
import planet_cache as pc  # shared order cache + ledger (never re-order what we own)

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


def _wait_download(pl, order_id, meta=None, log=print, delay=10, max_attempts=180):
    """Block until `order_id` reaches a final state, download it into the shared
    cache, record it in the ledger, and return its paired SR/UDM2 files.

    Downloads land in `planet_cache.order_dir(order_id)` rather than a per-project
    directory, and the ledger entry written afterwards is what lets any later run —
    any project, any --out — recall this order instead of placing a new one. That
    also makes `overwrite=False` the right choice: re-running against an order whose
    files are already cached costs no quota AND no bytes.

    `meta` is the {side, event_id, lat, lon, radius_km} the order was placed for, so
    the ledger can be searched by event/AOI later. Scene ids come from the delivered
    file names instead of `meta` so the record describes what actually arrived.

    Logs order-state transitions (queued -> running -> success) via `log` so a
    slow or queued order shows progress instead of blocking silently — the state
    lines stream to the plugin log. The wait budget is ~max_attempts*delay seconds
    (default ~30 min): Planet orders can sit queued for many minutes under load,
    and the SDK default (200 x 5 s ~ 16 min) was timing out on busy days. If the
    order still hasn't finished, the SDK raises ClientError; that and a non-success
    final state propagate to the caller (per-side note), which keeps one slow order
    from killing the other side."""
    dest = pc.order_dir(order_id, create=True)
    last = [None]

    def _on_state(state):
        if state != last[0]:
            last[0] = state
            log(f"    [planet] order {order_id[:12]} state: {state}")

    final = pl.orders.wait(order_id, delay=delay, max_attempts=max_attempts,
                           callback=_on_state)
    if final != "success":
        # failed/partial: the item(s) didn't deliver, so there's nothing to download
        raise RuntimeError(f"order finished in state {final!r} (not 'success')")
    pl.orders.download_order(order_id, directory=dest, overwrite=False)
    pairs = _pair_downloads(dest)
    # The delivered clip footprint beats the AOI we asked for: it's what actually
    # arrived, so it's the honest answer to "does this order cover that box?" — and it
    # keeps a re-download of someone else's order id from being filed at our AOI.
    meta = dict(meta or {})
    aoi = pc.aoi_from_disk(dest)
    if aoi:
        meta.update(lat=aoi["lat"], lon=aoi["lon"], bbox=aoi["bbox"])
    pc.record(order_id, dest, bundle=BUNDLE,
              scene_ids=pc.scene_ids_on_disk(dest), **meta)
    return pairs


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


def _open_scene(sr_path, udm_path, epsg, transform, shape, mask_clouds=True):
    """One clipped PSScene -> reflectance DataArray (bands blue/green/red/nir),
    reprojected onto the shared (transform, shape) target grid.

    mask_clouds: UDM2-mask non-clear pixels to NaN (True, the default — what a
    quantitative NDVI/composite run wants). The on-map SR detail preview passes
    False so it shows EVERY real pixel: UDM2 can misflag bright snow/ice as cloud,
    and punching those to NaN would blank out exactly the overexposed terrain the
    preview exists to reveal. Clouds, if any, are then just visible in the render."""
    da = rioxarray.open_rasterio(sr_path, masked=True).astype("float32")
    if "_sr" in os.path.basename(sr_path).lower():
        da = da * PS_SR_SCALE            # SR DN -> reflectance; DN bundle left as-is
    da = da.assign_coords(band=["blue", "green", "red", "nir"])
    if mask_clouds and udm_path and os.path.exists(udm_path):
        udm = rioxarray.open_rasterio(udm_path)
        clear = udm.sel(band=1).rio.reproject_match(da)   # UDM2 band 1: 1 = clear
        da = da.where(clear == 1)
    # warp straight onto the AOI grid; pixels the scene doesn't reach become NaN
    return da.rio.reproject(epsg, transform=transform, shape=shape)


def _composite(pairs, lat, lon, radius_km, epsg, mask_clouds=True):
    """Median composite over the scenes in one window, on the AOI grid. UDM2
    cloud-masking is applied unless mask_clouds=False (see _open_scene)."""
    transform, shape = _target_grid(lat, lon, radius_km, epsg)
    scenes = [_open_scene(sr, udm, epsg, transform, shape, mask_clouds=mask_clouds)
              for sr, udm in pairs]
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


def _event_result(pre, post, pre_ids, post_ids):
    """Build the fetch_event return contract from two finished composites.

    Returns None when either side has no usable pixels over the AOI (all
    cloud-masked, or the clips landed outside it), so the caller falls back to
    Sentinel-2/Landsat rather than exporting a blank package. Shared by the ordering
    path and the cache-recall path so both produce identical results."""
    if pre is None or post is None or not im._has_coverage(pre) \
            or not im._has_coverage(post):
        bad = [s for s, c in (("pre", pre), ("post", post))
               if c is None or not im._has_coverage(c)]
        print(f"    [planet] scenes clipped to no clear pixels over the AOI on "
              f"the {' and '.join(bad)} side (cloud-masked out or a scene nodata gap)")
        return None
    ndvi_pre, ndvi_post = im._ndvi(pre), im._ndvi(post)
    dndvi = (ndvi_post - ndvi_pre).rename("dndvi")
    bright_pre, bright_post = im._brightness(pre), im._brightness(post)
    dbright = (bright_post - bright_pre).rename("dbright")
    return dict(pre=pre, post=post, ndvi_pre=ndvi_pre, ndvi_post=ndvi_post,
                dndvi=dndvi, bright_pre=bright_pre, bright_post=bright_post,
                dbright=dbright, sensor="planet",
                pre_scenes=list(pre_ids), post_scenes=list(post_ids))


def _cached_event(event_id, lat, lon, radius_km):
    """fetch_event's result built entirely from orders already in the ledger, or None.

    No search, no order, no Planet client, no network — the whole point is that a
    re-run of a project whose PlanetScope scenes are already paid for spends nothing.
    Requires a pre AND a post order for the event whose clips are still on disk and
    whose AOI covers the requested radius (see planet_cache.find); anything less is a
    miss and the caller goes on to search/order normally."""
    recs = pc.find(event_id=event_id, lat=lat, lon=lon, radius_km=radius_km,
                   require_radius_km=radius_km, require_files=True)
    sides = pc.newest_by_side(recs)
    if not (sides.get("pre") and sides.get("post")):
        return None
    epsg = im._utm_epsg(lat, lon)
    comps, ids = {}, {}
    for side, rec in sides.items():
        pairs = _pair_downloads(pc.resolve_path(rec))
        if not pairs:
            return None
        print(f"    [planet-cache] reusing {side} order {rec['order_id'][:12]} "
              f"({len(pairs)} clip(s)) already ordered for this event — no quota")
        comps[side] = _composite(pairs, lat, lon, radius_km, epsg)
        ids[side] = rec.get("scene_ids") or []
    return _event_result(comps["pre"], comps["post"], ids["pre"], ids["post"])


def fetch_event(lat, lon, radius_km, event_time: dt.datetime,
                pre_days=60, post_days=60, seasonal=False, workdir=None,
                auto_window=False, cloud_weight=0.5,
                max_cloud_pct=None, require_point=False, allow_test_quality=False,
                event_id=None, reuse=True):
    """PlanetScope pre/post composites + dNDVI, or None if no usable coverage.

    Returns None (so the caller falls back to Sentinel-2/Landsat) when either
    window has no orderable scenes. Auth/SDK errors propagate to the caller,
    which logs them and falls back.

    reuse: check the order ledger FIRST and, when this event already has a pre and a
    post order cached, composite those and place no order at all (see
    _cached_event). This is why re-running a project doesn't spend quota twice. Pass
    reuse=False to force a fresh search + order — e.g. after widening the window or
    changing the cloud cap, where the cached scene choice is no longer what you want.
    event_id: the event's id, used to key the ledger. Without it, cached orders are
    matched by AOI proximity alone.
    workdir: legacy, unused. Orders are downloaded into the shared cache
    (planet_cache.cache_root()) so they are recallable from any project.

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
    # Cache first, before the client is even constructed: a fully-cached event needs
    # no auth and no network, so a re-run works offline and cannot touch quota.
    if reuse:
        hit = _cached_event(event_id, lat, lon, radius_km)
        if hit is not None:
            return hit

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

    epsg = im._utm_epsg(lat, lon)
    pre_ids = [i["id"] for i in pre_items]
    post_ids = [i["id"] for i in post_items]
    meta = dict(event_id=event_id, lat=lat, lon=lon, radius_km=radius_km)
    # Submit BOTH orders before waiting on either, so Planet processes the pre and
    # post clips concurrently. Total order latency then ~max(pre, post) instead of
    # the old pre+post (each order's blocking wait runs ~1-5 min). Waiting pre
    # first is fine — post is already cooking server-side meanwhile.
    pre_order = _create_order(pl, pre_ids, aoi)
    post_order = _create_order(pl, post_ids, aoi)
    pre_pairs = _wait_download(pl, pre_order, dict(meta, side="pre"))
    post_pairs = _wait_download(pl, post_order, dict(meta, side="post"))

    return _event_result(_composite(pre_pairs, lat, lon, radius_km, epsg),
                         _composite(post_pairs, lat, lon, radius_km, epsg),
                         pre_ids, post_ids)


def _finish_orders(pl, orders, lat, lon, radius_km, epsg, event_id=None):
    """Wait on each {side: order_id}, download it, and composite that side.

    Shared by render_preview (orders it just created) and resume_preview (orders an
    earlier render placed but didn't finish waiting on). Returns
    (comps, notes, pending): comps is {'pre': comp|None, 'post': comp|None}; pending
    is {side: order_id} for orders STILL processing after the wait budget. Those are
    resumable — the order is already placed and paid for, it just hasn't finished
    server-side — so the caller persists the id and can hand it back here later
    WITHOUT re-ordering (no extra quota). A hard failure (bad state, download error)
    is a per-side note and is NOT marked pending, because re-waiting won't help.

    Every download that lands here is written to the order ledger keyed on
    event_id + AOI, which is what makes it recallable for free later
    (recall_preview) instead of ordered a second time."""
    comps = {"pre": None, "post": None}
    notes, pending = [], {}
    meta = dict(event_id=event_id, lat=lat, lon=lon, radius_km=radius_km)
    for side, order_id in orders.items():
        try:
            pairs = _wait_download(pl, order_id, dict(meta, side=side))
            # mask_clouds=False: a visual detail preview should show every real pixel
            # (esp. bright snow UDM2 may misflag), not punch cloud-masked holes.
            comps[side] = _composite(pairs, lat, lon, radius_km, epsg,
                                     mask_clouds=False)
            if comps[side] is None:
                notes.append(
                    f"{side}: ordered scene(s) clipped to no pixels over the AOI")
        except Exception as e:
            if "Maximum number of attempts" in str(e):
                pending[side] = order_id
                notes.append(
                    f"{side}: Planet is still processing order {order_id} after the "
                    f"wait budget (~30 min). It's placed and saved in your Planet "
                    f"account — resume it (don't re-order) to finish without spending "
                    f"quota again.")
            else:
                notes.append(
                    f"{side}: order/download failed: {type(e).__name__}: {e}")
    return comps, notes, pending


class _LazyClient:
    """A Planet client built only if something actually needs the network.

    Every cache path here is meant to be free AND offline when the clips are already
    on disk; deferring _client() keeps it that way (no auth prompt, no requests) while
    still allowing a free re-download when a cached order's files have gone missing."""

    def __init__(self):
        self._pl = None

    def get(self):
        if self._pl is None:
            self._pl = _client()
        return self._pl


def _cached_side(side, ids, event_id, lat, lon, radius_km):
    """Newest ledger order that already contains EVERY id in `ids` for this side, or None.

    Prefers a record whose clips are still on disk (free and offline) over one that
    would need a re-download (free, but needs auth). The second pass is an id-only
    search restricted to records that know NEITHER where they were delivered nor what
    AOI they were asked for: PSScene ids are globally unique, so an order containing
    them holds the right pixels, and a location-blind record is the one case where
    that's the only evidence available. If such a clip turns out not to reach this AOI,
    _composite_record says so and the caller orders instead."""
    recs = pc.find(event_id=event_id, lat=lat, lon=lon, side=side, scene_ids=ids,
                   radius_km=radius_km, require_radius_km=radius_km)
    if not recs:
        recs = [r for r in pc.find(side=side, scene_ids=ids)
                if not r.get("bbox") and r.get("radius_km") is None]
    on_disk = [r for r in recs if pc.has_files(r)]
    return (on_disk or recs or [None])[0]


def _composite_record(rec, side, lat, lon, radius_km, epsg, client=None):
    """(comp|None, note|None) for one cached order, composited on this AOI's grid.

    When the order's files are missing locally, re-download it first: the order
    already exists in the Planet account, so fetching it again costs NO quota — only
    time. Needs `client` (a _LazyClient) to do that; without one, a missing-file
    record becomes a note so a caller that must stay offline still degrades cleanly.
    mask_clouds=False matches _finish_orders, so a recalled render is pixel-identical
    to the original."""
    oid = rec["order_id"]
    path = pc.resolve_path(rec)
    pairs = _pair_downloads(path)
    if not pairs:
        if client is None:
            return None, (f"{side}: order {oid[:12]} is in the ledger but its files are "
                          f"gone from {path} — Planet auth is needed to re-download it "
                          f"(free, no new order)")
        try:
            print(f"    [planet-cache] {side}: local clips missing, re-downloading "
                  f"order {oid[:12]} — already paid for, no new order")
            pairs = _wait_download(client.get(), oid,
                                   dict(side=side, event_id=rec.get("event_id"),
                                        lat=lat, lon=lon, radius_km=radius_km))
        except Exception as e:
            return None, (f"{side}: re-downloading order {oid[:12]} failed: "
                          f"{type(e).__name__}: {e}. Planet only keeps order results "
                          f"available for a limited time, so these scenes may have to "
                          f"be ordered again.")
    if not pairs:
        return None, f"{side}: cached order {oid[:12]} holds no usable SR clips"
    comp = _composite(pairs, lat, lon, radius_km, epsg, mask_clouds=False)
    if comp is None:
        return None, (f"{side}: cached order {oid[:12]} covers no pixels over this AOI "
                      f"— it was clipped to a different box")
    print(f"    [planet-cache] {side}: reusing order {oid[:12]} ({len(pairs)} clip(s)) "
          f"— no order, no quota")
    return comp, None


def recall_preview(lat, lon, radius_km, event_id=None, orders=None):
    """Put PlanetScope imagery ALREADY ORDERED for this event back on the canvas.
    No search, no new order, NO QUOTA.

    This is the "I paid for these pixels once" path. It takes the newest cached order
    per side for the event — matched at EVENT level (any cached scenes for this
    event/AOI), not against whatever is ticked in the table — composites it on the
    current AOI grid, and hands back render_preview's shape so the caller renders and
    loads it identically to a fresh render.

    orders: {side: order_id} to pin exact orders instead of the newest, which is how
    the plugin's order picker replays a specific one. An id that isn't in the ledger
    is still attempted — Planet holds the order, so an id copied from Planet Explorer
    can be pulled in and cached (free) as well.

    Returns render_preview's keys plus:
      reused    - {side: order_id} actually loaded (the free sides; here, all of them)
      available - every cached order for this event, newest first, each with a
                  human label and an on_disk flag, for a picker to offer.
    'pending' is always {} — nothing was ordered, so there is nothing to wait on.
    """
    epsg = im._utm_epsg(lat, lon)
    comps = {"pre": None, "post": None}
    notes, reused = [], {}
    client = _LazyClient()
    recs = pc.find(event_id=event_id, lat=lat, lon=lon, radius_km=radius_km)
    available = [dict(r, path=pc.resolve_path(r), on_disk=pc.has_files(r),
                      label=pc.describe(r)) for r in recs]

    # Start from the newest per side, then let explicit ids override. Pinning ONE side
    # therefore still fills the other from the cache, so picking a specific order can
    # never cost you the before/after pair.
    picked = pc.newest_by_side(recs)
    if orders:
        by_id = {r["order_id"]: r for r in pc.load()}
        for side, oid in orders.items():
            if not oid or side not in ("pre", "post"):
                continue
            # an unknown id is not an error: Planet still has the order, so let the
            # download path pull it in and record it on the way through
            picked[side] = by_id.get(oid) or dict(order_id=oid, path=None, side=side,
                                                  event_id=event_id)

    for side in ("pre", "post"):
        rec = picked.get(side)
        if rec is None:
            continue
        comp, note = _composite_record(rec, side, lat, lon, radius_km, epsg,
                                       client=client)
        if note:
            notes.append(note)
        if comp is not None:
            comps[side] = comp
            reused[side] = rec["order_id"]
            pc.stamp_footprint(rec["order_id"], event_id=event_id)

    if not reused and not notes:
        notes.append(
            f"nothing cached for this event: no PlanetScope order recorded near "
            f"{lat:.4f},{lon:.4f}" + (f" or for event {event_id}" if event_id else "")
            + f". The ledger lives in {pc.cache_root()}; 'Render detail' places the "
            f"first order, and every order after that is recallable for free.")
    return dict(pre=comps["pre"], post=comps["post"], epsg=epsg, notes=notes,
                pending={}, reused=reused, available=available)


def render_preview(lat, lon, radius_km, pre_ids=None, post_ids=None,
                   event_id=None, reuse=True):
    """Composite EXACTLY the given PlanetScope scene IDs (AOI-clipped) and return
    {'pre': comp|None, 'post': comp|None, 'epsg', 'notes', 'pending', 'reused'}.
    'pending' is {side: order_id} for any order still processing when the wait ran
    out — those are resumable via resume_preview without re-ordering (see
    _finish_orders). 'reused' is {side: order_id} for sides served from a cached
    order, i.e. the sides that cost nothing.

    This backs the plugin's on-map "SR detail" preview. Planet's free tile service
    streams pre-rendered 8-bit RGB that clips bright terrain (snow/ice) to flat
    white with no recoverable detail; to get the raw surface-reflectance pixels and
    apply our own tone curve we have to actually order the analytic_sr bundle. So,
    unlike the free tile preview, this MAY place a Planet order and consume quota —
    but only for a side whose scenes aren't in the cache already.

    reuse: for each side, if an order in the ledger already contains ALL the
    requested scene ids over an AOI at least this wide, composite it from disk and
    place no order. Deliberately stricter than recall_preview's event-level match:
    'Render detail' is asked for SPECIFIC ticked scenes, and quietly substituting a
    different cached scene would put the wrong pixels on the canvas. To load whatever
    was previously ordered for the event regardless of what's ticked, use
    recall_preview. reuse=False forces a fresh order even when cached.

    Differs from fetch_event: no windowed search or ranking — it composites the
    exact ids handed in — and it renders whichever side(s) were requested (one side
    alone is fine), so a single ticked scene can be previewed. Per-side failures
    become notes rather than aborting the whole render, so one bad order still lets
    the other side load."""
    epsg = im._utm_epsg(lat, lon)
    comps = {"pre": None, "post": None}
    notes, reused, to_order = [], {}, {}
    client = _LazyClient()
    for side, ids in (("pre", pre_ids), ("post", post_ids)):
        ids = [i for i in (ids or []) if i]
        if not ids:
            continue
        rec = _cached_side(side, ids, event_id, lat, lon, radius_km) if reuse else None
        if rec is None:
            to_order[side] = ids
            continue
        comp, note = _composite_record(rec, side, lat, lon, radius_km, epsg,
                                       client=client)
        if note:
            notes.append(note)
        if comp is None:
            # a cached order that won't composite is no use; buy the scenes instead
            to_order[side] = ids
            continue
        comps[side] = comp
        reused[side] = rec["order_id"]
        pc.stamp_footprint(rec["order_id"], event_id=event_id)

    pending = {}
    if to_order:
        pl = client.get()
        aoi = _bbox_geojson(lat, lon, radius_km)
        # Submit BOTH orders before waiting on either, so Planet clips the pre and
        # post concurrently server-side (same trick as fetch_event); total latency
        # ~max(pre, post) instead of pre+post.
        orders = {}
        for side, ids in to_order.items():
            try:
                orders[side] = _create_order(pl, ids, aoi)
            except Exception as e:
                notes.append(f"{side}: order create failed: {type(e).__name__}: {e}")
        fresh, dl_notes, pending = _finish_orders(pl, orders, lat, lon, radius_km,
                                                  epsg, event_id=event_id)
        notes += dl_notes
        for side in ("pre", "post"):
            if fresh[side] is not None:
                comps[side] = fresh[side]
    return dict(pre=comps["pre"], post=comps["post"], epsg=epsg, notes=notes,
                pending=pending, reused=reused)


def retone_preview(lat, lon, radius_km, workdir=None, event_id=None):
    """Recomposite PlanetScope clips ALREADY on disk. No Planet API call, no order,
    NO QUOTA — and no network at all.

    Backs the plugin's tone-mode switch: changing the tone curve is purely a local
    re-render, so read the clips back, composite them on the same AOI grid, and hand
    the composites to the caller to write with whichever curve is now selected.
    Reading the same files with the same target grid and the same mask_clouds=False as
    the original render makes the composites identical — the ONLY difference between
    two tone modes is the curve applied on the way to 8-bit.

    Looks for the clips in two places, in order: <workdir>/<side>/, where renders left
    them before the shared cache existed, and then the order ledger for this
    event/AOI, which is where they land now. That means a re-tone works for old
    per-project downloads and new cached orders alike.

    Same return shape as render_preview so run_single renders it identically. 'pending'
    is always {}: there is no order to wait on. A side with nothing on disk comes back
    None with a note rather than raising, so one downloaded side still re-tones."""
    epsg = im._utm_epsg(lat, lon)
    comps = {"pre": None, "post": None}
    notes, reused = [], {}
    cached = pc.newest_by_side(pc.find(event_id=event_id, lat=lat, lon=lon,
                                       radius_km=radius_km, require_files=True))
    for side in ("pre", "post"):
        side_dir = os.path.join(workdir or "", side)
        pairs = _pair_downloads(side_dir) if os.path.isdir(side_dir) else []
        rec = cached.get(side)
        if not pairs and rec is not None:
            pairs = _pair_downloads(pc.resolve_path(rec))
            if pairs:
                reused[side] = rec["order_id"]
        if not pairs:
            continue
        try:
            # mask_clouds=False mirrors _finish_orders: same pixels in, so only the
            # tone curve differs between modes (see docstring).
            comps[side] = _composite(pairs, lat, lon, radius_km, epsg,
                                     mask_clouds=False)
            if comps[side] is None:
                notes.append(f"{side}: downloaded clip(s) cover no pixels over the AOI")
            else:
                print(f"    [planet-retone] {side}: recomposited {len(pairs)} clip(s) "
                      f"from disk")
        except Exception as e:
            notes.append(f"{side}: re-render failed: {type(e).__name__}: {e}")
    if comps["pre"] is None and comps["post"] is None and not notes:
        notes.append(f"nothing to re-tone: no PlanetScope clips on disk for this event, "
                     f"in {workdir} or in the order cache ({pc.cache_root()}). Run "
                     f"'Render detail' once, or 'Recall order' if you've ordered it "
                     f"before.")
    return dict(pre=comps["pre"], post=comps["post"], epsg=epsg, notes=notes,
                pending={}, reused=reused)


def resume_preview(lat, lon, radius_km, orders, event_id=None):
    """Finish EXISTING Planet orders (given by id) instead of creating new ones.

    Backs the plugin's 'Resume pending order' button. When a render_preview order is
    still processing when the wait budget runs out, its id is saved (see
    render_preview's 'pending') rather than lost, and handed here to be waited on,
    downloaded, clipped and composited. This places NO new order and spends NO extra
    quota — it only finishes orders Planet is already processing. `orders` is
    {side: order_id} (side in 'pre'/'post'). Same return shape as render_preview, so
    the caller renders it identically (and a resume that times out AGAIN comes back
    with its own 'pending', so it can be retried once more)."""
    pl = _client()
    epsg = im._utm_epsg(lat, lon)
    orders = {s: o for s, o in (orders or {}).items() if o and s in ("pre", "post")}
    comps, notes, pending = _finish_orders(pl, orders, lat, lon, radius_km, epsg,
                                           event_id=event_id)
    return dict(pre=comps["pre"], post=comps["post"], epsg=epsg, notes=notes,
                pending=pending, reused={})
