"""DEM differencing math for the DEM tab: warp two PGC DEM strips to one grid,
mask, vertically co-register, subtract.

Everything here is pure numpy + GDAL (both ship inside QGIS; no scipy, no
network library — GDAL's /vsicurl does the ranged HTTP reads straight out of
the anonymous pgc-opendata-dems bucket, and COG overviews/tiling mean only the
AOI's bytes are actually fetched, not the multi-GB strips).

The geodesy that matters:

  * Both strips are SETSM s2s041 products with heights on the WGS84 ELLIPSOID.
    Differencing two of them cancels the geoid entirely — the result is true
    surface change in metres, no datum work needed.
  * Each strip carries an absolute vertical bias of up to a few metres
    (satellite geolocation error; worse for cross-track pairs). Over an AOI
    that is MOSTLY stable ground, that bias is exactly the robust central
    tendency of the difference map — so `coregister_offset` estimates it with
    a sigma-clipped median and the tab subtracts it by default. This is the
    standard first-order dDEM co-registration (the vertical piece of Nuth &
    Kääb 2011); the horizontal piece is skipped because s2s041 strips are
    already registered well under the render resolutions used here.
  * The strip `bitmask` flags pixels whose heights are untrustworthy —
    edge artifacts (bit 0), water (bit 1, stereo matching fails on it) and
    cloud (bit 2, matches on cloud tops). Differencing unmasked water/cloud
    pixels produces spectacular fake elevation change, so the tab applies the
    mask by default.

All warps go to a common UTM grid (metres) so the difference is pixel-aligned
by construction — same trick as the SAR tab's shared bbox renders, done
client-side with gdal.Warp because PGC has no render API.
"""
import numpy as np
from osgeo import gdal, osr

NODATA = -9999.0

# strip bitmask bits (PGC s2s041 stripmeta): 1 = edge, 2 = water, 4 = cloud
MASK_EDGE, MASK_WATER, MASK_CLOUD = 1, 2, 4


# ---------- grid / CRS helpers ----------
def utm_epsg(lat, lon):
    """EPSG of the WGS84 UTM zone containing (lat, lon)."""
    zone = int((lon + 180) // 6) + 1
    return (32700 if lat < 0 else 32600) + zone


def utm_bounds(minx, miny, maxx, maxy, epsg, res):
    """Lon/lat bbox -> (xmin, ymin, xmax, ymax) in the UTM CRS, expanded
    outward onto a whole-pixel grid of `res` metres so every warp of these
    bounds lands on the identical grid."""
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(epsg)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
        px, py, _ = tr.TransformPoint(x, y)
        xs.append(px)
        ys.append(py)
    snap = lambda v, up: (np.ceil(v / res) if up else np.floor(v / res)) * res
    return (snap(min(xs), False), snap(min(ys), False),
            snap(max(xs), True), snap(max(ys), True))


def warp(url, bounds, epsg, res, resample="bilinear", nodata=NODATA):
    """Warp a remote COG onto the shared AOI grid; (array, geotransform, proj).

    `bounds` must come from utm_bounds so every call returns the same grid.
    bilinear for elevations; use 'near' AND nodata=None for the bitmask —
    interpolating flag bits invents values, and a float nodata clamped into
    the mask's Byte range would collide with real flag values (GDAL then
    shifts the good-ground 0s to 1, flagging everything). A mask warped
    without nodata reads 0 ('good') outside the strip footprint, which is
    harmless because those pixels are already nodata in the DEM itself.
    Reads via /vsicurl — only the intersecting tiles of the strip are
    fetched."""
    src = url if url.startswith("/vsi") else "/vsicurl/" + url
    opts = dict(format="MEM", dstSRS=f"EPSG:{epsg}",
                outputBounds=bounds, xRes=res, yRes=res,
                resampleAlg=resample,
                multithread=False, errorThreshold=0.125)
    if nodata is not None:
        opts["dstNodata"] = nodata
    ds = gdal.Warp("", src, **opts)
    if ds is None:
        raise IOError(f"gdal.Warp failed for {url}")
    arr = ds.GetRasterBand(1).ReadAsArray()
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    ds = None
    if arr is None:
        raise IOError(f"no raster data warped from {url}")
    return arr.astype(np.float32), gt, proj


def valid_heights(arr):
    """Finite, non-nodata elevation pixels. No positivity test (unlike SAR
    gamma-naught): ellipsoidal heights are legitimately negative wherever the
    geoid sits above the ellipsoid — which includes Alaskan sea level."""
    return np.isfinite(arr) & (arr != NODATA)


# ---------- vertical co-registration ----------
def coregister_offset(diff, valid, iters=5, nsig=3.0):
    """(offset_m, stable_px) — robust vertical bias between the two strips.

    Sigma-clipped median of the difference over valid pixels: start from all
    of them, then iteratively drop pixels beyond nsig·σ of the current median
    so real surface change (the slide, snow drifts, rivers) falls out of the
    estimate and only quasi-stable ground defines the offset. Subtracting the
    returned offset from the difference map removes each strip pair's DC
    geolocation bias. stable_px is how many pixels survived the clipping —
    the tab warns when that's a thin slice of the AOI (offset then dubious)."""
    vals = diff[valid]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0, 0
    keep = vals
    for _ in range(iters):
        med = np.median(keep)
        sd = keep.std()
        if not np.isfinite(sd) or sd == 0:
            break
        nxt = keep[np.abs(keep - med) <= nsig * sd]
        if nxt.size == keep.size or nxt.size < 100:
            break
        keep = nxt
    return float(np.median(keep)), int(keep.size)


# ---------- GeoTIFF output ----------
def write_gtiff(path, arr, gt, proj):
    """Write float32 `arr` (NaN → NODATA) as a single-band deflate GeoTIFF."""
    h, w = arr.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    band = ds.GetRasterBand(1)
    band.WriteArray(np.where(np.isfinite(arr), arr, NODATA).astype(np.float32))
    band.SetNoDataValue(NODATA)
    band.FlushCache()
    ds = None


# ---------- browse thumbnails (hillshade COG overviews) ----------
def hillshade_thumb(url, max_px=256):
    """(uint8 bytes, width, height) of a decimated hillshade read, for QImage.

    PGC publishes no PNG browse, but the 10 m hillshade COG has internal
    overviews — asking GDAL for a small buf_xsize/buf_ysize makes it read the
    coarsest sufficient overview, so a thumbnail costs a few ranged requests
    (~100 kB), not the whole strip. Runs in a worker thread; returns plain
    bytes so the GUI thread does all the Qt work."""
    src = url if url.startswith("/vsi") else "/vsicurl/" + url
    ds = gdal.Open(src)
    if ds is None:
        raise IOError(f"could not open {url}")
    w, h = ds.RasterXSize, ds.RasterYSize
    scale = max(w, h) / float(max_px)
    ow = max(1, int(round(w / scale)))
    oh = max(1, int(round(h / scale)))
    arr = ds.GetRasterBand(1).ReadAsArray(0, 0, w, h,
                                          buf_xsize=ow, buf_ysize=oh)
    ds = None
    if arr is None:
        raise IOError(f"no overview data in {url}")
    return np.ascontiguousarray(arr.astype(np.uint8)).tobytes(), ow, oh
