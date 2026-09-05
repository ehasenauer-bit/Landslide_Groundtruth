"""Per-pixel cloud masking for the optical half of the fusion.

The gap this fills
------------------
Nothing upstream masks cloud per pixel. `imagery._composite` masks only fill
classes and says so in its own docstring; `imagery._aoi_cloud_fractions` produces
a single PERCENTAGE per scene at ~100 m for the candidate table, never a mask.
So a dBright raster carries cloud at full value, and the two failure modes are
symmetric and both fatal to an automatic detector:

    cloud in the post scene    -> post is bright -> large POSITIVE dBright
    cloud SHADOW in the post   -> post is dark   -> large NEGATIVE dBright,
                                  the same sign and magnitude as fresh debris
                                  on snow, which is exactly what we are hunting

OmniCloudMask — the one method in this project that works over glaciers — cannot
run here: it needs torch, lives in the venv, and its MPS dispatch is serialised
behind a lock because concurrent use segfaults the interpreter uncatchably. So
this module uses the classification band that ships with the imagery itself:
Sentinel-2's SCL, or Landsat C2's qa_pixel.

Known and deliberate limitation: over bright ice SCL frequently bins cloud tops
as SNOW (class 11), which this module does NOT mask — masking snow in a snow-
covered AOI would mask everything. The result is therefore a LOWER BOUND on
cloud, and the tab says so rather than implying the optical layer is clean.

Which scenes to fetch is recovered from the `<event>_metadata.json` the Run
writes next to its layers (review_package.write_metadata), which carries
`pre_scenes` / `post_scenes` as STAC item ids plus the sensor.
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request

import numpy as np

from . import fusion_grid

PC_DATA_URL = "https://planetarycomputer.microsoft.com/api/data/v1"

# Sentinel-2 L2A scene classification. 8/9 = cloud medium/high probability,
# 10 = thin cirrus, 3 = cloud shadow, 0/1 = nodata/defective.
# Deliberately NOT masked: 11 (snow/ice) — the whole AOI is snow — and 2
# (dark area / cast shadow), which over mountains is mostly TERRAIN shadow and
# would remove a large fraction of every steep AOI. Both are exposed as options.
SCL_CLOUD = (0, 1, 3, 8, 9, 10)
SCL_DARK = (2,)
SCL_SNOW = (11,)

# Landsat Collection-2 qa_pixel bit flags.
QA_BITS = {"fill": 0, "dilated_cloud": 1, "cirrus": 2, "cloud": 3,
           "cloud_shadow": 4, "snow": 5, "clear": 6, "water": 7}
QA_CLOUD_BITS = ("fill", "dilated_cloud", "cirrus", "cloud", "cloud_shadow")

# Keyed by NORMALISED sensor token. The Run writes imagery._composite_result's
# `sensor`, which is the short form "s2" / "landsat" (imagery.py) — NOT the
# "Sentinel-2" / "Landsat" long form that appears on the search-table candidate
# dicts. Both spellings are accepted here because both exist in this codebase and
# getting it wrong makes the whole cloud mask silently unreachable.
SENSOR_ASSET = {
    "s2": ("sentinel-2-l2a", "SCL"),
    "landsat": ("landsat-c2-l2", "qa_pixel"),
}

# qa_pixel's own fill VALUE is 1 (bit 0), so a literal 0 is not a valid Landsat
# QA word — it is what the render puts outside the scene footprint. SCL's fill
# value is 0 and is already in SCL_CLOUD. Mirrors imagery.py's `cls_fill`.
QA_FILL_VALUE = 0


def normalize_sensor(sensor):
    """Any spelling this repo uses -> 's2' | 'landsat' | None."""
    t = str(sensor or "").strip().lower()
    if t in ("s2", "sentinel-2", "sentinel2", "sentinel-2-l2a"):
        return "s2"
    if t.startswith("landsat"):
        return "landsat"
    return None


def find_metadata(raster_path):
    """The Run's <event>_metadata.json for THIS raster. Returns (meta, path).

    A sibling scan is not enough: run_groundtruth writes every event into one
    shared `<out>/qgis_packages/` directory, so the folder normally holds many
    `<event_id>_metadata.json` files. Taking the first would attach another
    event's scene ids to this raster and fetch cloud masks for the wrong place
    and date — wrong, and wrong in a way that still produces a plausible mask.

    Two matches, strongest first:
      1. the raster's path appears in the metadata's own `layers` list, which
         write_metadata records verbatim — exact, and survives renaming;
      2. otherwise the longest `<event_id>` that prefixes the raster's basename
         (review_package builds every layer name as `<event_id>_<kind>_…`).
    """
    ap = os.path.abspath(raster_path)
    d = os.path.dirname(ap)
    base = os.path.basename(ap)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith("_metadata.json"))
    except OSError:
        return None, None

    candidates = []
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if "post_scenes" not in meta and "pre_scenes" not in meta:
            continue
        layers = [os.path.basename(str(x)) for x in (meta.get("layers") or [])]
        if base in layers:
            return meta, p                       # exact
        eid = str(meta.get("event_id") or "")
        if eid and base.startswith(eid + "_"):
            candidates.append((len(eid), meta, p))
    if candidates:
        candidates.sort(key=lambda t: t[0], reverse=True)
        return candidates[0][1], candidates[0][2]
    return None, None


def bbox_4326(gt, shape, proj):
    """Lon/lat bounding box of a grid, whatever CRS it is in. The Planetary
    Computer bbox endpoint only speaks lon/lat, but the fusion grid is the
    coarser input and could be either EPSG:4326 (SAR) or UTM (Landsat)."""
    minx, miny, maxx, maxy = fusion_grid.grid_bbox(gt, shape)
    if not proj:
        return minx, miny, maxx, maxy
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromWkt(proj)
    if src.IsGeographic():
        return minx, miny, maxx, maxy
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    for s in (src, dst):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
        lon, lat, _ = tr.TransformPoint(float(x), float(y))
        xs.append(lon)
        ys.append(lat)
    return min(xs), min(ys), max(xs), max(ys)


def _fetch_class_band(collection, item_id, asset, bbox, width, height,
                      timeout=180):
    """Download one scene's classification band over the AOI as a GeoTIFF.

    Synchronous stdlib urllib, matching layover_dim.fetch_dem_on_grid's fallback
    path rather than the tab's async QgsNetworkAccessManager fan-in: this runs
    inside an already-synchronous compute, and a class band is small.

    `resampling=nearest` is requested because averaging class CODES is
    meaningless; if the endpoint rejects the parameter the request is retried
    without it, and the nearest-neighbour guarantee then comes from asking for
    the native-ish size and resampling locally instead."""
    minx, miny, maxx, maxy = bbox
    base = (f"{PC_DATA_URL}/item/bbox/"
            f"{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}.tif")
    params = {"collection": collection, "item": item_id, "assets": asset,
              "width": str(int(width)), "height": str(int(height))}
    for extra in ({"resampling": "nearest"}, {}):
        q = dict(params)
        q.update(extra)
        url = base + "?" + urllib.parse.urlencode(q)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if getattr(r, "status", 200) != 200:
                    continue
                data = r.read()
        except Exception:                            # noqa: BLE001
            continue
        fd, path = tempfile.mkstemp(suffix=".tif", prefix="landslide_cls_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    return None


def _decode(arr, sensor, mask_dark=False):
    """Class band -> boolean 'this pixel is unusable' (cloud, shadow, or fill).

    NaN is always unusable. For Landsat a literal 0 is unusable too: the PC bbox
    render fills outside the scene footprint with 0, and 0 has no QA bit set, so
    treating it as data would declare every off-footprint pixel CLEAR — a
    confidently wrong mask over exactly the area with no observation."""
    a = np.asarray(arr)
    sensor = normalize_sensor(sensor) or sensor
    if sensor == "s2":
        codes = list(SCL_CLOUD) + (list(SCL_DARK) if mask_dark else [])
        bad = ~np.isfinite(a)
        for c in codes:
            bad |= np.isclose(a, float(c))
        return bad
    bad = ~np.isfinite(a)
    bad |= np.isclose(np.nan_to_num(a, nan=1.0), float(QA_FILL_VALUE))
    q = np.nan_to_num(a, nan=1.0).astype(np.int64)   # NaN -> fill bit set
    for name in QA_CLOUD_BITS:
        bad |= (q >> QA_BITS[name]) & 1 > 0
    return bad


def cloud_mask_on_grid(meta, gt, shape, proj, *, sides=("pre", "post"),
                       frac_thresh=0.5, mask_dark=False, log=None):
    """Boolean cloud mask on the fusion grid, from the scenes the Run composited.

    Returns (mask, note). `mask` is True where the optical change value must be
    treated as MISSING rather than as no-change — scoring a cloud as 'no change'
    would be as wrong as scoring it as a detection.

    A pixel is contaminated when at least `frac_thresh` of a side's scenes flag
    it. The Run takes a per-pixel MEDIAN over up to six scenes, so a single
    cloudy scene out of five does not corrupt the composite, but three out of
    five does. With one scene per side (the common case) any flag counts.

    Contamination on EITHER side matters: dBright is post minus pre, so a cloud
    in the pre composite is just as fatal as one in the post."""
    raw_sensor = meta.get("sensor")
    sensor = normalize_sensor(raw_sensor)
    if sensor is None:
        return None, (f"unknown sensor {raw_sensor!r} — no classification band "
                      "known, so optical is left unmasked")
    collection, asset = SENSOR_ASSET[sensor]
    frac_thresh = max(float(frac_thresh), 1e-6)   # 0.0 would mask every pixel
    bbox = bbox_4326(gt, shape, proj)
    h, w = shape

    any_bad = np.zeros(shape, dtype=bool)
    fetched = failed = 0
    for side in sides:
        ids = [s for s in (meta.get(f"{side}_scenes") or []) if s]
        if not ids:
            continue
        votes = np.zeros(shape, dtype=np.float32)
        got = 0
        for item_id in ids:
            path = _fetch_class_band(collection, item_id, asset, bbox, w, h)
            if not path:
                failed += 1
                if log:
                    log(f"    {side}: could not fetch {asset} for {item_id}")
                continue
            try:
                arr = fusion_grid.warp_to_reference(path, gt, shape, proj,
                                                    resample="nearest")
                votes += _decode(arr, sensor, mask_dark).astype(np.float32)
                got += 1
                fetched += 1
            except Exception as e:                   # noqa: BLE001
                failed += 1
                if log:
                    log(f"    {side}: {item_id}: {type(e).__name__}: {e}")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if got:
            # Denominator is how many scenes the COMPOSITE used, not how many we
            # managed to fetch. The threshold's whole meaning is "enough of the
            # median's inputs were cloudy to corrupt it"; dividing by `got` would
            # turn one failed fetch into a stronger claim about the composite.
            any_bad |= (votes / float(len(ids))) >= frac_thresh

    if not fetched:
        return None, (f"no {asset} bands could be fetched "
                      f"({failed} attempt(s) failed) — optical left unmasked")
    pct = 100.0 * any_bad.mean() if any_bad.size else 0.0
    note = (f"{asset} cloud mask from {fetched} scene band(s): "
            f"{pct:.1f}% of the AOI masked")
    if failed:
        note += (f" ({failed} band(s) unavailable — those scenes count as CLEAR, "
                 "so the mask is weaker than it looks)")
    note += ". Over bright ice SCL bins cloud tops as snow, so this is a LOWER bound."
    return any_bad, note
