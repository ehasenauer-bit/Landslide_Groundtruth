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

Its RESOLUTION only. The extent is the intersection of every input's footprint
(`crop_to_common`), because reach and detail are different questions and the
coarser raster is not the right answer to both.
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


def extent_km(gt, shape):
    """(width, height) of a grid in km, whatever CRS it is in.

    For labels and log lines. "20x20 km" is the one number that tells two change
    rasters of the same event apart when their layer names cannot."""
    from . import layover_dim
    dx, dy = layover_dim.metric_pixel_size(gt, shape[0])
    return (dx * shape[1] / 1000.0, dy * shape[0] / 1000.0)


def bbox_in_crs(path, dst_proj):
    """A raster's footprint as (minx, miny, maxx, maxy), expressed in `dst_proj`.

    The edges are densified before transforming. Sending only the four corners of
    a UTM tile into lon/lat understates the box, because the edges between them
    bow; over a 90 km scene at 60N that is several pixel rows, and it would be
    absorbed silently into the intersection below."""
    from osgeo import osr
    gt, shape, proj = reference_grid(path)
    bb = grid_bbox(gt, shape)
    if not proj or not dst_proj:
        return bb
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromWkt(proj)
    dst.ImportFromWkt(dst_proj)
    if src.IsSame(dst):
        return bb
    for s in (src, dst):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    n = 32
    for i in range(n + 1):
        f = i / float(n)
        x = bb[0] + (bb[2] - bb[0]) * f
        y = bb[1] + (bb[3] - bb[1]) * f
        for ex, ey in ((x, bb[1]), (x, bb[3]), (bb[0], y), (bb[2], y)):
            u, v, _ = tr.TransformPoint(float(ex), float(ey))
            xs.append(u)
            ys.append(v)
    return (min(xs), min(ys), max(xs), max(ys))


def crop_to_common(gt, shape, proj, paths, min_px=16):
    """Shrink a reference grid to the ground EVERY input actually covers.

    pick_reference answers "how fine should the fused grid be". It must not also
    answer "how far does it reach" — but it did, because the reference raster's
    own extent was handed straight to the warp. The two questions have different
    answers, and conflating them is silent:

      a 20 km optical tile fused against a 90 km SAR scene produced a 90 km
      raster carrying optical evidence over 5% of itself. The rest scored from
      SAR alone or not at all, and the result looked exactly as if the small
      optical raster had been the one used -- which, over 5% of the output, it
      was.

    So the extent is the INTERSECTION: the ground where both inputs have
    something to say. Pixel edges are inherited from the reference grid, so the
    crop is a whole number of reference pixels and nothing is resampled twice.

    Returns (gt, (rows, cols), info)."""
    require_north_up(gt)
    px, py = abs(gt[1]), abs(gt[5])
    if px <= 0 or py <= 0:
        raise ValueError("the reference grid has a zero pixel size")
    if gt[1] < 0:
        raise ValueError(f"the reference grid is mirrored east-west (pixel "
                         f"width {gt[1]}); reproject it before fusing")
    x0, y0, x1, y1 = grid_bbox(gt, shape)
    for p in paths:
        b = bbox_in_crs(p, proj)
        x0, y0 = max(x0, b[0]), max(y0, b[1])
        x1, y1 = min(x1, b[2]), min(y1, b[3])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("the inputs do not overlap at all - they describe "
                         "different places")
    ox, oy = gt[0], gt[3]                    # north-up origin: min x, max y
    eps = 1e-6                               # in PIXELS; the divides normalise
    c0 = max(0, int(np.ceil((x0 - ox) / px - eps)))
    c1 = min(shape[1], int(np.floor((x1 - ox) / px + eps)))
    r0 = max(0, int(np.ceil((oy - y1) / py - eps)))
    r1 = min(shape[0], int(np.floor((oy - y0) / py + eps)))
    w, h = c1 - c0, r1 - r0
    if w < min_px or h < min_px:
        raise ValueError(
            f"the inputs overlap on only {max(w, 0)}x{max(h, 0)} pixels of the "
            f"{shape[1]}x{shape[0]} reference grid - too little to fuse")
    gt2 = (ox + c0 * px, gt[1], 0.0, oy - r0 * py, 0.0, gt[5])
    return gt2, (h, w), {"kept": (w * h) / float(shape[0] * shape[1]),
                         "cropped": (h, w) != tuple(shape)}


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
