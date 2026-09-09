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
from osgeo import gdal, ogr, osr

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
    """Warp a remote COG (or a MOSAIC of them) onto the shared AOI grid;
    returns (array, geotransform, proj).

    `url` may be a single COG href or a list of hrefs — a list is mosaicked by
    GDAL onto the one AOI grid, which is how tiled sources (e.g. 3DEP seamless,
    tiled 1x1 deg) cover an AOI that straddles tile boundaries. `bounds` must
    come from utm_bounds so every call returns the same grid.
    bilinear for elevations; use 'near' AND nodata=None for the bitmask —
    interpolating flag bits invents values, and a float nodata clamped into
    the mask's Byte range would collide with real flag values (GDAL then
    shifts the good-ground 0s to 1, flagging everything). A mask warped
    without nodata reads 0 ('good') outside the strip footprint, which is
    harmless because those pixels are already nodata in the DEM itself.
    Reads via /vsicurl — only the intersecting tiles of each source are
    fetched."""
    urls = [url] if isinstance(url, str) else list(url)
    srcs = [u if u.startswith("/vsi") else "/vsicurl/" + u for u in urls]
    opts = dict(format="MEM", dstSRS=f"EPSG:{epsg}",
                outputBounds=bounds, xRes=res, yRes=res,
                resampleAlg=resample,
                multithread=False, errorThreshold=0.125)
    if nodata is not None:
        opts["dstNodata"] = nodata
    ds = gdal.Warp("", srcs, **opts)
    if ds is None:
        raise IOError(f"gdal.Warp failed for {urls}")
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
    """(offset_m, stable_px, sigma_m) — robust vertical bias between two strips.

    sigma_m is the scatter of the difference over the quasi-stable ground the
    clipping converged on: the per-pixel vertical error of the Δh field. It was
    already computed here to drive the clip and then thrown away, which left the
    most direct volume estimate the plugin can make with no uncertainty at all,
    and therefore unable to take part in any agreement test.

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
        return 0.0, 0, 0.0
    keep = vals
    clipped = False       # did any sigma-clip iteration actually remove outliers?
    thin = None           # the size a clip WOULD have produced when stopped for <100
    for _ in range(iters):
        med = np.median(keep)
        sd = keep.std()
        if not np.isfinite(sd) or sd == 0:
            break
        nxt = keep[np.abs(keep - med) <= nsig * sd]
        if nxt.size == keep.size:
            break                      # converged: no outliers left to drop
        if nxt.size < 100:
            thin = int(nxt.size)       # clipping wanted to continue but too few remain
            break
        keep = nxt
        clipped = True
    # If the FIRST clip was refused for being too thin, no outliers were ever removed:
    # the offset is the slide-contaminated unclipped median. Report the thin would-be
    # count as stable_px (not the full unclipped size) so the tab's thin-stable-ground
    # warning fires instead of trusting a contaminated offset.
    stable = thin if (thin is not None and not clipped) else keep.size
    # Scatter of the SURVIVING (quasi-stable) pixels — the per-pixel vertical
    # error. Robust (MAD-based), because a few unclipped outliers would inflate a
    # plain std and quietly widen every volume error bar downstream.
    sigma = (float(np.median(np.abs(keep - np.median(keep))) * 1.4826)
             if keep.size else 0.0)
    if not np.isfinite(sigma):
        sigma = 0.0
    return float(np.median(keep)), int(stable), sigma


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


# ===========================================================================
# Elevation-change (Δh) -> volume, for the Volume tab's "∫Δh over outline" fit.
#
# The one thing MOSART (and any dDEM workflow) leaves to the caller: turn a map
# of elevation change into a volume by summing dh over the ground area that
# moved. Everything here is metric — the change field is warped to a UTM grid so
# a pixel is res×res square metres regardless of whether the input arrived in
# radar/degree/foot units, then dh·res² is summed inside the outline. Kept in
# dem_diff, not volume_tab, because the tab is deliberately numpy/GDAL-free.
# ===========================================================================
def _resolve_source(u):
    """A GDAL-openable string. Remote http(s)/ftp get the /vsicurl/ prefix;
    local paths, a QgsRasterLayer.source() and existing /vsi paths pass through.

    Distinct from `warp`, which prefixes /vsicurl onto everything non-/vsi
    because its inputs are always remote PGC COGs — here the inputs are usually
    LOCAL rasters (a project DEM, a differenced Δh, a saved MOSART GeoTIFF).

    A QGIS raster layer's source() can carry provider decorations GDAL can't
    open (e.g. "…/dem.tif|band=1"); the part before the first '|' is the
    GDAL dataset string, so keep only that."""
    u = u.split("|", 1)[0]
    if u.startswith("/vsi"):
        return u
    if u.startswith(("http://", "https://", "ftp://")):
        return "/vsicurl/" + u
    return u


def _source_nodata(srcs):
    """The common nodata value when the sources AGREE on one, else None.

    Passed to gdal.Warp as srcNodata so a declared fill is MASKED rather than
    resampled: without it, bilinear blends an undeclared but real fill value
    (a bare -9999 or 0 with no band NoData set) into neighbouring valid pixels,
    and valid_heights would then keep the smeared fringe.

    Only returned when every declaring source uses the SAME value, because a single
    srcNodata is applied to the WHOLE warp: forcing the first tile's fill (say
    -9999) onto a mosaic tile that fills with 0 would both mask real 0-height data
    and suppress GDAL's per-source NoData. When the sources disagree, return None
    and let gdal.Warp honour each source's own internal NoData."""
    found = set()
    for s in srcs:
        try:
            ds = gdal.Open(s)
        except Exception:
            continue
        if ds is None:
            continue
        nd = ds.GetRasterBand(1).GetNoDataValue()
        ds = None
        if nd is not None:
            found.add(nd)
    return next(iter(found)) if len(found) == 1 else None


def warp_to_grid(sources, bounds, epsg, res, resample="bilinear", nodata=NODATA):
    """Warp local file(s) (or remote COGs, or a mosaic list) onto the shared AOI
    grid; returns (array float32, geotransform, proj).

    Like `warp` but does NOT force /vsicurl onto local inputs, so it accepts a
    project raster's .source() and a list of on-disk tiles (e.g. the 11 tiles of
    a lidar DSM, mosaicked by GDAL onto the one grid). `bounds`/`res`/`epsg`
    define the metric target grid every call lands on."""
    srcs = [sources] if isinstance(sources, str) else list(sources)
    srcs = [_resolve_source(u) for u in srcs]
    opts = dict(format="MEM", dstSRS=f"EPSG:{epsg}",
                outputBounds=bounds, xRes=res, yRes=res,
                resampleAlg=resample, multithread=False, errorThreshold=0.125)
    if nodata is not None:
        opts["dstNodata"] = nodata
    src_nd = _source_nodata(srcs)
    if src_nd is not None:
        opts["srcNodata"] = src_nd     # mask a declared fill, don't blend it
    ds = gdal.Warp("", srcs, **opts)
    if ds is None:
        raise IOError(f"gdal.Warp failed for {srcs}")
    arr = ds.GetRasterBand(1).ReadAsArray()
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    ds = None
    if arr is None:
        raise IOError(f"no raster data warped from {srcs}")
    return arr.astype(np.float32), gt, proj


def difference_dems(pre_sources, post_sources, bounds, epsg, res,
                    coregister=True):
    """(dh, gt, proj, stats) — post minus pre on one shared UTM grid.

    Both DEMs are warped to the identical (bounds, epsg, res) grid so they
    subtract pixel-aligned. dh is metres of surface change, POSITIVE where the
    surface rose (deposition), negative where it dropped (erosion). With
    coregister=True the DC vertical bias between the two surfaces is removed by a
    sigma-clipped median over the co-valid pixels (`coregister_offset`) — real
    change falls out of that estimate so what is subtracted is the
    geolocation/datum offset, de-meaning the quasi-stable ground to ~0.

    `stats` carries offset_m, stable_px (pixels that defined the offset) and
    valid_px, so the caller can flag a co-registration resting on a thin or
    unstable (e.g. a glacier that itself moved between epochs) slice of ground."""
    pre, gt, proj = warp_to_grid(pre_sources, bounds, epsg, res)
    post, _gt2, _proj2 = warp_to_grid(post_sources, bounds, epsg, res)
    valid = valid_heights(pre) & valid_heights(post)
    raw = np.where(valid, post - pre, np.nan)
    offset, stable, sigma = (coregister_offset(raw, valid) if coregister
                             else (0.0, 0, 0.0))
    dh = np.where(valid, raw - offset, np.nan).astype(np.float32)
    stats = {"offset_m": float(offset), "stable_px": int(stable),
             "sigma_dh_m": float(sigma),
             "valid_px": int(valid.sum()), "res_m": float(res),
             "epsg": int(epsg)}
    return dh, gt, proj, stats


def _polygon_mask(wkt, epsg, gt, shape):
    """Boolean mask, True inside the polygon, on the grid defined by gt/shape.

    `wkt` is interpreted in EPSG:epsg — the SAME CRS as the warped grid — so the
    burn lands pixel-aligned with the Δh array. Pixels are burned by centre
    (GDAL's default), which is the right convention for area/volume integration:
    each ground pixel is counted once."""
    h, w = shape
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    vds = ogr.GetDriverByName("Memory").CreateDataSource("mask")
    lyr = vds.CreateLayer("poly", srs=srs, geom_type=ogr.wkbPolygon)
    geom = ogr.CreateGeometryFromWkt(wkt)
    if geom is None:
        raise ValueError("could not parse the outline geometry (WKT)")
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(geom)
    lyr.CreateFeature(feat)
    mds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    mds.SetGeoTransform(gt)
    mds.SetProjection(srs.ExportToWkt())
    err = gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1])
    m = mds.GetRasterBand(1).ReadAsArray().astype(bool)
    mds = None
    vds = None
    if err != 0:
        raise IOError("rasterizing the outline onto the Δh grid failed")
    return m


def stable_ground_stats(dh_source, outline_wkt, epsg, bounds, res,
                        resample="bilinear", exclude_buffer_px=3):
    """Residual bias and per-pixel noise of an IMPORTED Δh, from ground OUTSIDE
    the outline. Returns {"offset_m", "sigma_m", "stable_px", "ok"}.

    Why this exists, specifically for MOSART. MOSART reconstructs elevation
    change from Sentinel-1 AMPLITUDE by a least-squares shape-from-shading
    inversion (its `sfs` / `lsquares` modules), and the notebook writes
    `demdefs[post] - demdefs[ref]` straight to GeoTIFF. An inversion of that kind
    constrains the SHAPE of the change field far better than its absolute datum,
    so the product carries a DC offset that nothing upstream removes — and volume
    is LINEAR in that offset. Half a metre of residual bias over a 1 km² outline
    integrates to 500,000 m³, which is a large fraction of a real event's whole
    volume, reported as signal.

    `difference_dems` already solves this for a pair it computed itself
    (coregister_offset over the whole AOI). An imported Δh never went through
    that path, so the same estimate is made here from the pixels around the
    slide: sigma-clipped so the slide's own signal, snow and rivers fall out.

    The outline is dilated by `exclude_buffer_px` before being excluded, because
    the deposit usually runs past the digitized scar and would otherwise pull the
    "stable" median toward the event.
    """
    dh, gt, _proj = warp_to_grid(dh_source, bounds, epsg, res, resample=resample)
    inside = _polygon_mask(outline_wkt, epsg, gt, dh.shape)
    if exclude_buffer_px > 0:
        # cheap binary dilation without scipy: shift-and-OR in 8 directions
        grown = inside.copy()
        for _ in range(int(exclude_buffer_px)):
            g = grown
            grown = (g | np.roll(g, 1, 0) | np.roll(g, -1, 0)
                     | np.roll(g, 1, 1) | np.roll(g, -1, 1))
        inside = grown
    outside = valid_heights(dh) & ~inside
    n = int(outside.sum())
    if n < 100:
        # Too little surrounding ground to say anything. Explicitly NOT falling
        # back to "offset 0, sigma 0": that would silently claim the product is
        # unbiased and noiseless, which is the failure this function exists to
        # stop.
        return {"offset_m": 0.0, "sigma_m": 0.0, "stable_px": n, "ok": False}
    offset, stable, sigma = coregister_offset(dh, outside)
    return {"offset_m": float(offset), "sigma_m": float(sigma),
            "stable_px": int(stable), "ok": True}


def integrate_dh(dh_source, outline_wkt, epsg, bounds, res,
                 sign_deposit_positive=True, offset=0.0, resample="bilinear",
                 sigma_dh_m=0.0, outline_area_m2=None):
    """Integrate an elevation-change raster over an outline -> volumes (m³).

    `dh_source` is any GDAL-openable Δh raster in metres. It is warped onto the
    metric grid (bounds, epsg, res) — reprojecting a geographic (e.g. MOSART
    lon/lat degree) product to equal-area square pixels on the way — the outline
    (WKT, in EPSG:epsg) is rasterized to a mask, and Δh is summed over the
    covered valid pixels:

        V_net = Σ Δh · res²        ( = V_deposit + V_erosion )

    With `sign_deposit_positive` (default) a positive Δh is deposition (surface
    rose) and negative is erosion; pass False for a pre-minus-post product, which
    flips the sign. `offset` is subtracted from every Δh first (e.g. a residual
    stable-ground bias). Returns a dict of volumes (m³), covered area (m²), the
    pixel count and the Δh extremes.

    Resampling: bilinear is right for a real (continuous) Δh field, and is an
    identity when the source already sits on the target grid (a Δh made by
    difference_dems). The net volume is resampling-invariant; only the
    erosion/deposition SPLIT is mildly sensitive where a source pixel straddles
    the zero crossing after reprojection, since a blended value lands on one side
    of zero. That is a fraction-of-a-pixel effect along the zero contour, not a
    bias in the totals."""
    dh, gt, _proj = warp_to_grid(dh_source, bounds, epsg, res, resample=resample)
    mask = _polygon_mask(outline_wkt, epsg, gt, dh.shape)
    valid = valid_heights(dh) & mask
    vals = dh[valid].astype(np.float64) - float(offset)
    if not sign_deposit_positive:
        vals = -vals
    # Pixel area from the ACTUAL warped grid, not the requested res: if `bounds`
    # weren't a whole-pixel multiple of res, gdal.Warp nudges the pixel size to
    # fit outputBounds, and gt[1]/gt[5] are then the truth.
    px = abs(gt[1] * gt[5])
    n = int(vals.size)
    v_deposit = float(vals[vals > 0].sum() * px)
    v_erosion = float(vals[vals < 0].sum() * px)
    covered = float(n * px)

    # Volume uncertainty from the per-pixel vertical error. Two bounds, because
    # which one applies depends on how the Δh was made and they differ by
    # sqrt(N) — for a 1 km² outline at 10 m that is a factor of 10, so quoting
    # the wrong one is not a detail:
    #
    #   correlated  σ_V = σ_h · A          a DC/long-wavelength bias. This is
    #                                      the realistic case for MOSART, whose
    #                                      shape-from-shading inversion produces
    #                                      a smooth error field, not white noise.
    #   random      σ_V = σ_h · px · √N    independent per-pixel noise.
    #
    # The correlated bound is reported as `v_sigma_m3` because it is the honest
    # one for this plugin's inputs; the random bound is carried alongside so a
    # caller with a genuinely uncorrelated product can use it instead.
    sig = max(0.0, float(sigma_dh_m or 0.0))
    v_sigma_corr = sig * covered
    v_sigma_rand = sig * px * (n ** 0.5)

    # How much of the outline the Δh actually covers. Only ZERO coverage used to
    # be caught, so a layer overlapping 40% of the slide returned 40% of the
    # volume with nothing said — indistinguishable from a small landslide.
    coverage = None
    if outline_area_m2:
        try:
            coverage = float(covered) / float(outline_area_m2)
        except (TypeError, ZeroDivisionError):
            coverage = None

    return {
        "v_net": float(vals.sum() * px),
        "v_sigma_m3": float(v_sigma_corr),
        "v_sigma_random_m3": float(v_sigma_rand),
        "sigma_dh_m": sig,
        "coverage_frac": coverage,
        "offset_applied_m": float(offset),
        "v_deposit": v_deposit,
        "v_erosion": v_erosion,
        "covered_area_m2": float(n * px),
        "pixel_count": n,
        "mean_dh_m": float(vals.mean()) if n else 0.0,
        "max_rise_m": float(vals.max()) if n else 0.0,
        "max_drop_m": float(vals.min()) if n else 0.0,
        "res_m": float(res),
        "epsg": int(epsg),
    }
