"""Fade radar-layover slopes in the SAR display.

With single-geometry coverage (common in interior Alaska — see the descending-
only Denali case), the slopes facing the radar illumination are foreshortened /
laid over and pile up energy, reading as artificially HIGH amplitude that isn't
ground signal. This module builds a per-pixel opacity (alpha) that dims those
pixels so they stop dominating the map.

Geometry-aware, not hard-coded to "east": Sentinel-1 is right-looking, so the
illumination arrives from —
  descending pass → from the EAST  → east-facing slopes lay over (bright)
  ascending  pass → from the WEST  → west-facing slopes lay over (bright)
so the layover-facing aspect is picked from the scene's orbit_state.

A pixel is dimmed only when it is (a) on a slope FACING the illumination, (b)
STEEP enough to actually foreshorten/lay over (flat ground of any aspect is
left alone), and (c) HIGH-valued (the actual layover pile-up / strong change),
per the user's choice. Pure numpy so it unit-tests without QGIS/GDAL.
"""
import numpy as np

__all__ = ["illumination_aspect_deg", "slope_aspect_deg", "metric_pixel_size",
           "layover_alpha"]


def illumination_aspect_deg(orbit_state):
    """Compass aspect (deg, 0=N 90=E 180=S 270=W) of the slopes that face the
    radar and therefore lay over, from the pass direction. None if unknown —
    the caller then does not dim anything (no reliable geometry)."""
    if not orbit_state:
        return None
    s = str(orbit_state).lower()
    if s.startswith("desc"):
        return 90.0        # descending, right-looking → illuminated from the east
    if s.startswith("asc"):
        return 270.0       # ascending,  right-looking → illuminated from the west
    return None


def slope_aspect_deg(dem, dx_m, dy_m):
    """(slope_deg, aspect_deg) for a north-up DEM (row 0 = north). Aspect is the
    compass direction the surface FACES downhill (0=N, 90=E, 180=S, 270=W)."""
    dem = np.asarray(dem, dtype=np.float64)
    gy_row = np.gradient(dem, float(dy_m), axis=0)   # dz/d(row); row increases south
    gx_col = np.gradient(dem, float(dx_m), axis=1)   # dz/d(col); col increases east
    # downhill vector in map coords: east = -dz/deast = -gx_col; north = -dz/dnorth
    # and dz/dnorth = -gy_row, so downhill_north = gy_row
    aspect = np.degrees(np.arctan2(-gx_col, gy_row)) % 360.0
    slope = np.degrees(np.arctan(np.hypot(gx_col, gy_row)))
    return slope.astype(np.float32), aspect.astype(np.float32)


def metric_pixel_size(gt, n_rows, lat_hint=None):
    """(dx_m, dy_m) ground pixel size from a GDAL geotransform. Handles a
    geographic (degrees) grid by scaling with latitude, or a projected (metres)
    grid directly."""
    px_w, px_h = abs(gt[1]), abs(gt[5])
    if px_w < 0.5:                       # degrees (geographic)
        lat = lat_hint
        if lat is None:
            lat = gt[3] + gt[5] * (n_rows / 2.0)   # centre latitude
        dx = px_w * 111320.0 * max(0.05, np.cos(np.radians(lat)))
        dy = px_h * 110540.0
        return float(dx), float(dy)
    return float(px_w), float(px_h)      # already metres


def _angular_gap(a, b):
    """Smallest absolute compass angle between arrays a and scalar b (degrees)."""
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


def _box_mean(a, k):
    """Fast k×k edge-padded box mean via an integral image (no scipy). Used to
    (1) smooth the DEM before slope/aspect — bilinear-upsampled tiles come out as
    flat facets whose derivatives terrace into blocky aspect — and (2) feather the
    dim mask so its opacity ramps over a few pixels instead of a hard 25%/100%
    edge (the 'blocky artifact')."""
    a = np.asarray(a, dtype=np.float64)
    if k <= 1:
        return a
    pad = k // 2
    ap = np.pad(a, ((pad, k - 1 - pad), (pad, k - 1 - pad)), mode="edge")
    ii = np.zeros((ap.shape[0] + 1, ap.shape[1] + 1))
    ii[1:, 1:] = ap.cumsum(0).cumsum(1)
    return (ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]) / (k * k)


def layover_alpha(value, valid, dem, gt, orbit_state, *, lat_hint=None,
                  mode="amplitude", thr=None, high_percentile=75.0,
                  dim=0.25, slope_min_deg=12.0, aspect_tol_deg=55.0,
                  dem_smooth=3, feather=5):
    """Per-pixel opacity in [dim, 1] (float32): `dim` where a pixel is layover-
    facing AND steep AND high-valued, else 1.0.

    value/valid/dem must share the same grid. `orbit_state` picks the layover
    aspect; if it is unknown the whole array is opaque (no dimming). `mode`:
      'amplitude' → high = value ≥ its `high_percentile` among valid pixels
      'change'    → high = |value| ≥ `thr` (the detector's significance cut)
    Returns (alpha, meta). meta reports the layover aspect and dimmed-pixel count.
    """
    value = np.asarray(value, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    alpha = np.ones(value.shape, dtype=np.float32)

    lay_aspect = illumination_aspect_deg(orbit_state)
    if lay_aspect is None:
        return alpha, {"orbit_state": orbit_state, "layover_aspect": None,
                       "n_dimmed": 0, "note": "unknown orbit — not dimmed"}

    dx, dy = metric_pixel_size(gt, value.shape[0], lat_hint)
    # smooth the DEM first: a bilinear-upsampled tile is a lattice of flat facets
    # whose slope/aspect derivatives terrace into blocky patches
    dem_s = _box_mean(dem, dem_smooth) if dem_smooth and dem_smooth > 1 else dem
    slope, aspect = slope_aspect_deg(dem_s, dx, dy)
    facing = _angular_gap(aspect, lay_aspect) <= aspect_tol_deg
    steep = slope >= slope_min_deg
    if mode == "change":
        t = abs(float(thr)) if thr is not None else 0.0
        high = np.abs(value) >= t
    else:
        finite = value[valid & np.isfinite(value)]
        cut = np.percentile(finite, high_percentile) if finite.size else np.inf
        high = value >= cut
    dim_mask = valid & facing & steep & high & np.isfinite(slope)
    # feather the hard dim/keep edge so opacity ramps over a few pixels instead of
    # a blocky 25%/100% step; strength in [0,1] → alpha in [dim, 1]
    if feather and feather > 1:
        strength = np.clip(_box_mean(dim_mask.astype(np.float64), feather), 0.0, 1.0)
    else:
        strength = dim_mask.astype(np.float64)
    alpha = (1.0 - strength * (1.0 - float(dim))).astype(np.float32)
    return alpha, {"orbit_state": str(orbit_state), "layover_aspect": lay_aspect,
                   "n_dimmed": int(dim_mask.sum()),
                   "note": f"dimmed layover-facing (aspect~{lay_aspect:.0f}°) "
                           f"steep high-value pixels to {dim:.0%}"}


def as_alpha_band(alpha):
    """float alpha in [0,1] → uint8 0..255 for a GeoTIFF alpha band."""
    return np.clip(np.rint(np.asarray(alpha) * 255.0), 0, 255).astype(np.uint8)


# Public, no-login Copernicus GLO-30 DEM bucket. Tiles are 1°×1°, named by their
# SW-corner integer lat/lon. GDAL reads the COGs directly over /vsicurl — no
# planetary_computer / pystac / requests (none of which QGIS's Python has), only
# osgeo + stdlib urllib, both always present in QGIS.
_COP_DEM_BASE = "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com"


def _cop_dem_tile_url(sw_lat, sw_lon):
    ns = f"N{sw_lat:02d}" if sw_lat >= 0 else f"S{abs(sw_lat):02d}"
    ew = f"E{sw_lon:03d}" if sw_lon >= 0 else f"W{abs(sw_lon):03d}"
    name = f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"
    return f"{_COP_DEM_BASE}/{name}/{name}.tif"


def _url_exists(url, timeout=30):
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _warp_sources_to_grid(gdal, sources, minx, miny, maxx, maxy, width, height):
    """VRT over `sources` (GDAL-openable paths) → warp to the EXACT AOI grid."""
    vrt = gdal.BuildVRT("/vsimem/_layover_dem.vrt", sources)
    if vrt is None:
        return None
    ds = gdal.Warp("", vrt, format="MEM", outputBounds=(minx, miny, maxx, maxy),
                   width=int(width), height=int(height), dstSRS="EPSG:4326",
                   resampleAlg="bilinear")
    try:
        gdal.Unlink("/vsimem/_layover_dem.vrt")
    except Exception:
        pass
    if ds is None:
        return None
    return np.asarray(ds.GetRasterBand(1).ReadAsArray(), dtype=np.float32)


def fetch_dem_on_grid(minx, miny, maxx, maxy, width, height):
    """Copernicus GLO-30 DEM warped to the EXACT AOI grid (EPSG:4326 bbox at the
    given width/height), so it lines up pixel-for-pixel with the SAR renders. Uses
    only osgeo + stdlib (importable inside QGIS, unlike planetary_computer).

    Two strategies over the open, no-login Copernicus DEM bucket:
      1. FAST — GDAL /vsicurl range reads. GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
         stops the sidecar-probing that is the usual cause of a generic /vsicurl
         failure.
      2. ROBUST FALLBACK — download whole tiles with stdlib urllib (which works
         where GDAL's own curl does not: proxy / SSL / offline-curl builds) into
         GDAL's in-memory filesystem, then warp locally.
    A VRT over every intersecting 1°×1° tile handles an AOI that straddles the grid.
    Returns a float32 ndarray, or None if no DEM tile covers the AOI.
    """
    import math
    import urllib.request
    from osgeo import gdal

    tiles = [(la, lo)
             for la in range(math.floor(miny), math.floor(maxy) + 1)
             for lo in range(math.floor(minx), math.floor(maxx) + 1)]
    urls = [u for u in (_cop_dem_tile_url(la, lo) for la, lo in tiles)
            if _url_exists(u)]                        # skip ocean/missing tiles
    if not urls:
        return None

    for k, v in (("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"),
                 ("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif"),
                 ("GDAL_HTTP_MAX_RETRY", "3"), ("GDAL_HTTP_RETRY_DELAY", "1")):
        gdal.SetConfigOption(k, v)

    # 1. fast path
    try:
        arr = _warp_sources_to_grid(gdal, ["/vsicurl/" + u for u in urls],
                                    minx, miny, maxx, maxy, width, height)
        if arr is not None:
            return arr
    except Exception:
        pass                                          # fall through to download

    # 2. robust fallback: stdlib download → GDAL in-memory FS → warp
    mem = []
    try:
        for i, u in enumerate(urls):
            with urllib.request.urlopen(u, timeout=180) as r:
                if getattr(r, "status", 200) != 200:
                    continue
                data = r.read()
            p = f"/vsimem/_layover_dem_{i}.tif"
            gdal.FileFromMemBuffer(p, data)
            mem.append(p)
        if not mem:
            return None
        return _warp_sources_to_grid(gdal, mem, minx, miny, maxx, maxy,
                                     width, height)
    finally:
        for p in mem:
            try:
                gdal.Unlink(p)
            except Exception:
                pass
