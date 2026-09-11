"""Grid alignment for the fusion tab: putting two rasters that were never meant
to meet onto one pixel grid.

Why this module has to exist
----------------------------
Nothing else in this plugin aligns rasters, because nothing else needed to: the
SAR change detectors get co-registration free (every input is the same
bbox/width/height data-API render, sar_change's docstring says so) and the DEM
is fetched pre-warped onto that grid. The fusion tab is the first consumer of two
INDEPENDENTLY produced rasters, and they disagree on everything that matters:

  optical dBright/dNDSI   UTM (EPSG:326xx), 10 m (S2) or 30 m (Landsat),
                          float64, and — verified on disk — NO nodata tag, with
                          NaN carried in-band
  SAR log-ratio           EPSG:4326 lon/lat degrees, ~10-30 m ground, float32,
                          nodata -9999

Two hazards follow, and both are silent if unhandled:

1. NaN with no nodata tag. GDAL cannot know those pixels are invalid, so a
   resampling warp averages NaN into every neighbouring output pixel and the
   hole grows by a kernel width. `_as_nodata_source` rewrites the band with an
   explicit nodata value before any warp touches it.

2. Downsampling a continuous field by point-sampling. Going 10 m -> 20 m with
   nearest or bilinear throws away three quarters of the measurements and keeps
   whichever pixel happened to land under the sample point. `average` is the
   correct aggregation, and it is what this module defaults to for continuous
   data; categorical inputs (an SCL class band) must use nearest instead, since
   averaging class codes is meaningless.

The reference grid is the COARSER of the two inputs, on the principle that a
fused score is only as trustworthy as its worst input — upsampling SAR to 10 m
would manufacture detail out of speckle.
"""
import numpy as np

NODATA = -9999.0


def describe_raster(path):
    """(is_usable, reason) for a candidate fusion input.

    Rejects DISPLAY rasters. When the SAR tab's layover fade fires it writes the
    change map as a 4-band uint8 RGBA with the colour ramp BAKED INTO THE PIXELS
    (sar_change.write_rgba), and the QGIS layer keeps the same
    "S1 change log-ratio ..." name as the float32 product. Reading band 1 of that
    is reading the red channel of a diverging colour ramp, not decibels — and
    because the ramp is non-monotone in dB, the strongest apparent evidence lands
    on the WRONG polarity. Measured on a synthetic scene: 48% of the AOI cleared
    a 3 dB floor instead of 1.6%, with the top ranks on the opposite sign, and no
    warning anywhere. Silent and fatal, so it is refused rather than warned about.

    On success `reason` is the band's GDAL data type name."""
    from osgeo import gdal
    try:
        ds = gdal.Open(path)
        if ds is None:
            return False, "GDAL cannot open it"
        n = ds.RasterCount
        dt = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
        ds = None
    except Exception as e:                           # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if n >= 3:
        return False, (f"{n}-band colour image, not measurement values "
                       "(the layover fade bakes the ramp into the pixels)")
    if dt == "Byte":
        return False, "8-bit colour image, not measurement values"
    return True, dt


def reference_grid(path):
    """(geotransform, (rows, cols), projection WKT) of a raster."""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise IOError(f"GDAL could not open {path}")
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    shape = (ds.RasterYSize, ds.RasterXSize)
    ds = None
    return gt, shape, proj


def require_north_up(gt, path=""):
    """Raise unless the geotransform is north-up and unrotated.

    gdal.Warp with outputBounds+width/height ALWAYS emits a north-up result, so a
    south-up or rotated reference would be silently written back out under its
    original geotransform — a vertically mirrored score raster that still opens,
    still overlays, and is simply wrong. Every raster this plugin produces is
    north-up, so refusing is better than guessing."""
    if gt is None:
        raise ValueError(f"{path or 'raster'} has no geotransform")
    if gt[2] or gt[4]:
        raise ValueError(f"{path or 'raster'} has a rotated geotransform "
                         f"({gt[2]}, {gt[4]}); the fusion grid must be axis-aligned")
    if gt[5] > 0:
        raise ValueError(f"{path or 'raster'} is south-up (pixel height {gt[5]} > 0); "
                         "reproject it to a north-up grid before fusing")


def grid_bbox(gt, shape):
    """(minx, miny, maxx, maxy) of a north-up grid, in its own CRS units."""
    h, w = shape
    minx = gt[0]
    maxy = gt[3]
    maxx = gt[0] + gt[1] * w
    miny = gt[3] + gt[5] * h
    return (min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy))


def ground_pixel_m(gt, shape):
    """Coarser ground axis of a grid in metres, whatever its CRS. Routes through
    layover_dim.metric_pixel_size so a degrees grid is scaled by latitude — the
    plugin's SAR products are all EPSG:4326, so this is the normal case, not the
    exception."""
    from . import layover_dim
    dx, dy = layover_dim.metric_pixel_size(gt, shape[0])
    return float(max(dx, dy))


def pick_reference(paths):
    """Choose the coarsest of several rasters as the fusion grid.

    Returns (path, gt, shape, proj, info) where info lists each candidate's
    ground pixel size so the tab can log what it picked and why."""
    info = []
    best = None
    for p in paths:
        gt, shape, proj = reference_grid(p)
        require_north_up(gt, p)
        m = ground_pixel_m(gt, shape)
        info.append({"path": p, "ground_m": m, "shape": shape,
                     "geographic": abs(gt[1]) < 0.5})
        if best is None or m > best[4]:
            best = (p, gt, shape, proj, m)
    if best is None:
        raise ValueError("no rasters given")
    return best[0], best[1], best[2], best[3], info


def _as_nodata_source(gdal, path, band=1):
    """Copy one band into an in-memory GeoTIFF with an explicit nodata value.

    The Run writes dBright with a bare .rio.to_raster(): float64, no nodata tag,
    NaN in-band. Warping that directly lets NaN bleed into neighbours. Returns a
    /vsimem path the caller must gdal.Unlink."""
    ds = gdal.Open(path)
    if ds is None:
        raise IOError(f"GDAL could not open {path}")
    b = ds.GetRasterBand(int(band))
    arr = b.ReadAsArray()
    if arr is None:
        raise IOError(f"no raster data in {path}")
    arr = np.asarray(arr, dtype=np.float32)
    nd = b.GetNoDataValue()
    bad = ~np.isfinite(arr)
    if nd is not None:
        bad |= arr == np.float32(nd)
    arr = np.where(bad, NODATA, arr).astype(np.float32)

    vpath = f"/vsimem/_fusion_src_{abs(hash((path, band))) % (10 ** 9)}.tif"
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(vpath, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out.SetGeoTransform(ds.GetGeoTransform())
    out.SetProjection(ds.GetProjection())
    ob = out.GetRasterBand(1)
    # nodata BEFORE the write, and never after. On GDAL 3.12 an uncompressed
    # GTiff never physically writes all-zero blocks, and a SetNoDataValue that
    # arrives afterwards backfills every one of them with the nodata value —
    # measured here at 72000 of 90000 pixels destroyed. A change raster is mostly
    # zeros ("no change"), so that would silently delete the quiet background and
    # leave only the anomalies. Compression happens to mask the bug, which makes
    # it worse, not better: it would reappear the moment the options changed.
    ob.SetNoDataValue(NODATA)
    ob.WriteArray(arr)
    ob.FlushCache()
    out = None
    ds = None
    return vpath


def warp_to_reference(path, gt, shape, proj, resample="average", band=1):
    """Warp one band of `path` onto the exact reference grid.

    Returns float32 with NaN where the source had no valid data. `resample` is
    'average' for continuous fields being downsampled (the default and the right
    choice for dBright/dNDSI at 10 m -> a 20-30 m SAR grid), 'bilinear' when the
    scales are close, or 'nearest' for categorical class bands."""
    from osgeo import gdal
    h, w = shape
    minx, miny, maxx, maxy = grid_bbox(gt, shape)
    vsrc = _as_nodata_source(gdal, path, band)
    try:
        ds = gdal.Warp("", vsrc, format="MEM",
                       outputBounds=(minx, miny, maxx, maxy),
                       width=int(w), height=int(h),
                       dstSRS=proj or "EPSG:4326",
                       srcNodata=NODATA, dstNodata=NODATA,
                       resampleAlg=str(resample))
        if ds is None:
            raise IOError(f"could not warp {path} onto the fusion grid")
        arr = np.asarray(ds.GetRasterBand(1).ReadAsArray(), dtype=np.float32)
        ds = None
    finally:
        try:
            gdal.Unlink(vsrc)
        except Exception:                            # noqa: BLE001
            pass
    return np.where(arr == np.float32(NODATA), np.nan, arr).astype(np.float32)


def write_multiband(path, bands, names, gt, proj, nodata=NODATA):
    """Write a multiband float32 deflate GeoTIFF, one band per name, each tagged
    with its name as the band description so QGIS's band picker shows
    'optical_rank' rather than 'Band 1'. NaN -> nodata."""
    from osgeo import gdal
    first = bands[names[0]]
    h, w = first.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, len(names), gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    for i, nm in enumerate(names, start=1):
        a = np.asarray(bands[nm], dtype=np.float32)
        b = ds.GetRasterBand(i)
        b.SetNoDataValue(float(nodata))      # before the write — see
        b.WriteArray(np.where(np.isfinite(a), a, nodata).astype(np.float32))
        b.SetDescription(nm)
        b.FlushCache()
    ds = None
    return path
