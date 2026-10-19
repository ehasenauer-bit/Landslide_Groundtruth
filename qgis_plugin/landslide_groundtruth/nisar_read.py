"""NISAR L-band GCOV: one polarization over the SAR tab's AOI, read remotely.

A GCOV granule is one ~6 GB HDF5 file of terrain-corrected gamma-naught (linear
power, the same quantity as Sentinel-1 RTC) on a 10 m map grid covering a whole
~240 km frame. The SAR tab needs a few km of it, so nothing is downloaded: GDAL
reads just the AOI window over /vsicurl with HTTP range requests, behind NASA
Earthdata Login. Measured on the Iliamna granule (2026-10-19): open 6 s,
coordinate arrays 2 s, then the window itself.

Georeferencing has to be rebuilt by hand. GDAL only gained a NISAR driver in
3.13 and QGIS-LTR here ships 3.12, whose HDF5 driver returns the pixels with an
identity geotransform and no CRS — and ASF's documented NETCDF: workaround is
Linux-only for /vsi paths. The file's own xCoordinates / yCoordinates (pixel
CENTRES) and `projection@epsg_code` say everything needed. Never infer the CRS
from latitude: ASF documents UTM, but the Alaska granules measured so far are
EPSG:3413 polar stereographic, and two tracks over one AOI need not agree.

Output contract: the SAR tab's AOI grid — EPSG:4326, the search box, px x px —
which is exactly what the Planetary Computer bbox render returns for Sentinel-1.
So sar_change.read_band, the detectors and the durable export take a NISAR
scene unchanged; only how the pixels arrive differs.

Validity comes from the product, not a guess: GCOV ships a per-pixel subswath
`mask` whose 0 marks samples built from partially focused or invalid radar
data (and 255 fill), and those are dropped together with non-finite and
non-positive gamma-naught.

Blocking network I/O, 5-15 s per scene: call this from a background task
(nisar_task.NisarReadTask), never on the QGIS GUI thread. GDAL only; no QGIS
imports, so it unit-tests against a synthetic GCOV-shaped file.
"""
import hashlib
import os
import tempfile
import threading
from urllib.parse import urlsplit

import numpy as np

GRID = "/science/LSAR/GCOV/grids/frequencyA"
IDENT = "/science/LSAR/identification"
EDL_HOST = "urs.earthdata.nasa.gov"
NODATA = -9999.0          # sar_change.NODATA: what read_band already excludes
# Largest window read at full resolution, per side. A 20 km radius at 10 m is
# 4000 px; past that the read is strided (nearest-neighbour decimation) so a
# 50 km AOI cannot pull 400 MB per scene into QGIS. The warp to the AOI grid,
# which is coarser than native by then anyway, averages what is left.
MAX_SIDE = 4096
# Mask values that are not data: 0 = partially focused / invalid, 255 = fill.
_MASK_INVALID = (0, 255)
# Every HDF5 call in the process goes through this. QGIS-LTR's GDAL 3.12 HDF5
# driver is NOT thread-safe: four threads reading a GCOV file at once segfault
# the interpreter (measured 2026-10-19, exit 139) — and a Preview and a Compute
# change map on NISAR scenes are two background tasks that would do exactly
# that. Holding it costs nothing real: the remote reads are network-bound and
# HDF5 would serialise them inside a thread-safe build anyway.
_HDF5_LOCK = threading.Lock()


def covariance_term(pol):
    """'hh' -> 'HHHH': GCOV stores power as the diagonal covariance term."""
    p = str(pol or "").strip().upper()
    if len(p) != 2:
        raise ValueError(f"not a polarization: {pol!r}")
    return p + p


# ---------------------------------------------------------------- credentials
def credentials():
    """('token' | 'netrc' | None, human detail) — how a read would authenticate.

    EARTHDATA_TOKEN wins (a bearer token, portable); otherwise a ~/.netrc entry
    for urs.earthdata.nasa.gov, which is what the dock's 'NASA Earthdata Login'
    box writes. Never reads or returns the secret itself."""
    if os.environ.get("EARTHDATA_TOKEN"):
        return "token", "EARTHDATA_TOKEN bearer token"
    path = os.path.expanduser("~/.netrc")
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            if EDL_HOST in f.read():
                return "netrc", f"~/.netrc entry for {EDL_HOST}"
    except OSError:
        pass
    return None, ("no NASA Earthdata login — open the 'NASA Earthdata Login' box "
                  "in the plugin's Environment header and log in (it writes "
                  f"~/.netrc), or set EARTHDATA_TOKEN. Free account: https://{EDL_HOST}/")


def _vsi(url):
    """GDAL path for a granule: remote URLs go through /vsicurl, local files and
    explicit /vsi paths (the tests) are used as they are."""
    if url.startswith("/vsi") or "://" not in url:
        return url
    return "/vsicurl/" + url


def configure_auth(url, cookie_jar=None):
    """Point GDAL's HTTP layer at Earthdata Login for THIS granule's host only.

    Path-specific options (GDAL >= 3.6) scope the credentials to the URL's own
    scheme://host/ prefix, so they can never ride along on QGIS's other /vsicurl
    traffic — a global GDAL_HTTP_HEADERS would hand an Earthdata token to every
    COG and tile server the project touches. The cookie jar is not optional:
    EDL's redirect chain sets a session cookie, and without one each range
    request bounces back to the login page.

    Returns the method used ('token' | 'netrc' | 'local'); raises PermissionError
    with the fix spelled out when there are no credentials at all."""
    from osgeo import gdal
    parts = urlsplit(url)
    if not parts.scheme.startswith("http"):
        return "local"
    how, detail = credentials()
    if how is None:
        raise PermissionError(detail)
    prefix = f"/vsicurl/{parts.scheme}://{parts.netloc}/"
    jar = cookie_jar or os.path.join(tempfile.gettempdir(), "landslide_edl_cookies.txt")
    opts = {"GDAL_HTTP_COOKIEFILE": jar, "GDAL_HTTP_COOKIEJAR": jar,
            # no directory listing / sidecar probing: each probe is another trip
            # through the EDL redirect for a file that does not exist
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "2"}
    if how == "token":
        opts.update(GDAL_HTTP_AUTH="BEARER",
                    GDAL_HTTP_BEARER=os.environ["EARTHDATA_TOKEN"])
    else:
        opts.update(GDAL_HTTP_NETRC="YES",
                    GDAL_HTTP_NETRC_FILE=os.path.expanduser("~/.netrc"))
    for k, v in opts.items():
        gdal.SetPathSpecificOption(prefix, k, v)
    return how


def _auth_hint(err):
    """An Earthdata rejection reads as a bare HDF5 open failure; say what it is."""
    s = str(err)
    if any(t in s for t in ("401", "403", "Unauthorized", "Forbidden", EDL_HOST)):
        return (f"{s} — Earthdata Login refused the read. Log in again in the "
                "'NASA Earthdata Login' box (Environment header), and make sure "
                "your Earthdata profile has approved the 'Alaska Satellite "
                "Facility Data Access' application.")
    return s


# ---------------------------------------------------------------- geometry
def grid_geotransform(x, y):
    """GDAL geotransform from GCOV coordinate arrays, which hold pixel CENTRES:
    the corner is half a pixel out from the first centre on each axis."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    px, py = float(x[1] - x[0]), float(y[1] - y[0])
    return (float(x[0]) - px / 2.0, px, 0.0, float(y[0]) - py / 2.0, 0.0, py)


def _srs(epsg):
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(int(epsg))
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)   # x=lon/east always
    return s


def aoi_window(gt, nx, ny, epsg, bbox, margin=2):
    """(row0, col0, nrows, ncols) of the granule grid covering a lon/lat box, or
    None when the box misses the grid entirely.

    The box edges are densified before transforming: in polar stereographic a
    straight lon/lat edge is a curve, and transforming only the four corners
    clips the bulge off the sides of the window. `margin` pixels of slack on
    every side so the later average-resampling has full support at the edge."""
    from osgeo import osr
    minx, miny, maxx, maxy = bbox
    t = np.linspace(0.0, 1.0, 21)
    lon = np.concatenate([minx + (maxx - minx) * t, np.full(21, maxx),
                          maxx - (maxx - minx) * t, np.full(21, minx)])
    lat = np.concatenate([np.full(21, miny), miny + (maxy - miny) * t,
                          np.full(21, maxy), maxy - (maxy - miny) * t])
    tr = osr.CoordinateTransformation(_srs(4326), _srs(epsg))
    pts = np.array(tr.TransformPoints(np.column_stack([lon, lat]).tolist()))
    xs, ys = pts[:, 0], pts[:, 1]
    cols = (xs - gt[0]) / gt[1]
    rows = (ys - gt[3]) / gt[5]
    c0 = int(np.floor(cols.min())) - margin
    c1 = int(np.ceil(cols.max())) + margin
    r0 = int(np.floor(rows.min())) - margin
    r1 = int(np.ceil(rows.max())) + margin
    c0, r0 = max(c0, 0), max(r0, 0)
    c1, r1 = min(c1, nx), min(r1, ny)
    if c1 <= c0 or r1 <= r0:
        return None
    return r0, c0, r1 - r0, c1 - c0


def aoi_geotransform(bbox, px):
    """The AOI grid's geotransform: north-up EPSG:4326, `px` square cells over
    the box — the same grid the Planetary Computer bbox render produces."""
    minx, miny, maxx, maxy = bbox
    return (minx, (maxx - minx) / px, 0.0, maxy, 0.0, -(maxy - miny) / px)


def _read_str(group, name):
    """A scalar string/number from an identification group, or None."""
    try:
        arr = group.OpenMDArray(name)
        if arr is None:
            return None
        v = arr.Read()
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if isinstance(v, (bytes, bytearray)):
            v = int.from_bytes(bytes(v), "little")
        return v
    except Exception:                       # noqa: BLE001 — metadata is a bonus
        return None


# ---------------------------------------------------------------- native read
def read_native(url, pol, bbox, max_side=MAX_SIDE):
    """The AOI window of one GCOV polarization in the granule's OWN grid.

    Returns dict(arr float32 with NaN where invalid, gt, wkt, step, epsg,
    look, pass_direction). Raises LookupError when the granule does not cover
    the box or lacks the polarization, PermissionError/RuntimeError for access
    problems. Every GDAL object is held in a local until the read is done:
    letting the dataset be collected while an array or band from it is still in
    use invalidates that child."""
    from osgeo import gdal
    term = covariance_term(pol)
    with _HDF5_LOCK, gdal.ExceptionMgr(useExceptions=True):
        try:
            ds = gdal.OpenEx(_vsi(url), gdal.OF_MULTIDIM_RASTER,
                             allowed_drivers=["HDF5"])
        except RuntimeError as e:
            raise RuntimeError(_auth_hint(e)) from None
        root = ds.GetRootGroup()
        grp = root.OpenGroupFromFullname(GRID)
        if grp is None:
            raise LookupError(f"no {GRID} group — not a GCOV granule?")
        names = grp.GetMDArrayNames() or []
        if term not in names:
            have = sorted(n for n in names if len(n) == 4 and n.isupper()
                          and n[:2] == n[2:])
            raise LookupError(f"granule has no {term} (it carries "
                              f"{', '.join(have) or 'no covariance terms'})")
        x = grp.OpenMDArray("xCoordinates").ReadAsArray()
        y = grp.OpenMDArray("yCoordinates").ReadAsArray()
        epsg = None
        for a in grp.OpenMDArray("projection").GetAttributes():
            if a.GetName() == "epsg_code":
                epsg = int(a.Read())
        if not epsg:
            raise LookupError("granule has no projection@epsg_code")
        gt = grid_geotransform(x, y)
        win = aoi_window(gt, x.size, y.size, epsg, bbox)
        if win is None:
            raise LookupError("the granule's grid does not reach the AOI")
        r0, c0, nr, nc = win
        step = max(1, int(np.ceil(max(nr, nc) / float(max_side))))
        cnt = [int(np.ceil(nr / step)), int(np.ceil(nc / step))]
        kw = dict(array_start_idx=[r0, c0], count=cnt)
        if step > 1:
            kw["array_step"] = [step, step]
        arr = grp.OpenMDArray(term).ReadAsArray(**kw).astype(np.float32)
        valid = np.isfinite(arr) & (arr > 0)
        if "mask" in names:
            m = grp.OpenMDArray("mask").ReadAsArray(**kw)
            valid &= ~np.isin(m, _MASK_INVALID)
        arr[~valid] = np.nan
        ident = root.OpenGroupFromFullname(IDENT)
        look = _read_str(ident, "lookDirection") if ident is not None else None
        pdir = _read_str(ident, "orbitPassDirection") if ident is not None else None
        wkt = _srs(epsg).ExportToWkt()
        del grp, root, ds
    wgt = (gt[0] + c0 * gt[1], gt[1] * step, 0.0,
           gt[3] + r0 * gt[5], 0.0, gt[5] * step)
    return dict(arr=arr, gt=wgt, wkt=wkt, step=step, epsg=epsg,
                look=str(look) if look else None,
                pass_direction=str(pdir) if pdir else None)


# ---------------------------------------------------------------- AOI grid
def warp_to_aoi(arr, gt, wkt, bbox, px):
    """Resample a native window onto the AOI grid (EPSG:4326, px x px).

    'average' over valid samples, in LINEAR power — the multilook that the
    coarser AOI pixel implies, and the right mean for gamma-naught (averaging dB
    would bias it low). NaN is nodata on both sides, so a pixel the swath edge
    or the subswath mask emptied stays empty instead of reading as dark."""
    from osgeo import gdal
    h, w = arr.shape
    with gdal.ExceptionMgr(useExceptions=True):
        src = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Float32)
        src.SetGeoTransform(gt)
        src.SetProjection(wkt)
        b = src.GetRasterBand(1)
        b.SetNoDataValue(float("nan"))
        b.WriteArray(arr)
        dst_wkt = _srs(4326).ExportToWkt()
        minx, miny, maxx, maxy = bbox
        dst = gdal.Warp("", src, format="MEM", dstSRS=dst_wkt,
                        outputBounds=(minx, miny, maxx, maxy), width=px, height=px,
                        resampleAlg="average", srcNodata=float("nan"),
                        dstNodata=float("nan"), outputType=gdal.GDT_Float32)
        out = dst.GetRasterBand(1).ReadAsArray().astype(np.float32)
        out_gt = dst.GetGeoTransform()
        del b, src, dst
    return out, out_gt, dst_wkt


def _cache_path(cache_dir, url, pol, bbox, max_side):
    key = "|".join([os.path.basename(urlsplit(url).path) or url, pol.lower(),
                    ",".join(f"{v:.5f}" for v in bbox), str(max_side)])
    return os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest()[:20] + ".tif")


def read_aoi(url, pol, bbox, px, cache_dir=None, log=None):
    """One NISAR scene on the AOI grid: (float32 gamma-naught px x px with NaN
    where invalid, geotransform, WKT, info dict).

    The NATIVE window is cached per (granule, pol, box) when `cache_dir` is
    given, so the quick preview, the full-detail Run and every change-detection
    rerun at a new Pixel size or window pay for the remote read once."""
    native = None
    path = _cache_path(cache_dir, url, pol, bbox, MAX_SIDE) if cache_dir else None
    if path and os.path.exists(path):
        try:
            native = _read_cached(path)
            if log:
                log(f"    cached window: {os.path.basename(path)}")
        except Exception:                   # noqa: BLE001 — a bad cache re-reads
            native = None
    if native is None:
        native = read_native(url, pol, bbox)
        if path:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                _write_cached(path, native)
            except Exception as e:          # noqa: BLE001 — the cache is optional
                if log:
                    log(f"    (window not cached: {type(e).__name__}: {e})")
    out, gt, wkt = warp_to_aoi(native["arr"], native["gt"], native["wkt"], bbox, px)
    info = {k: native.get(k) for k in ("step", "epsg", "look", "pass_direction")}
    info["native_res"] = abs(native["gt"][1])
    info["valid_frac"] = float(np.isfinite(out).mean()) if out.size else 0.0
    return out, gt, wkt, info


def _write_cached(path, native):
    """Write beside the final name, then rename: a Preview task and a change-
    detection task can cache the same window at once, and a reader must never
    open a half-written file (os.replace is atomic on one filesystem)."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part.tif"
    try:
        _write_tif(tmp, native["arr"], native["gt"], native["wkt"], np.float32,
                   NODATA, {"look": native.get("look") or "",
                            "step": str(native["step"]),
                            "epsg": str(native["epsg"]),
                            "pass_direction": native.get("pass_direction") or ""})
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_cached(path):
    from osgeo import gdal
    with gdal.ExceptionMgr(useExceptions=True):
        ds = gdal.Open(path)
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray().astype(np.float32)
        nd = band.GetNoDataValue()
        if nd is not None:
            arr[arr == nd] = np.nan
        md = ds.GetMetadata() or {}
        out = dict(arr=arr, gt=ds.GetGeoTransform(), wkt=ds.GetProjection(),
                   step=int(md.get("step") or 1), epsg=int(md.get("epsg") or 0),
                   look=md.get("look") or None,
                   pass_direction=md.get("pass_direction") or None)
        del band, ds
    return out


# ---------------------------------------------------------------- writers
def _write_tif(path, arr, gt, wkt, dtype, nodata, metadata=None):
    from osgeo import gdal
    gdt = gdal.GDT_Byte if dtype == np.uint8 else gdal.GDT_Float32
    h, w = arr.shape
    with gdal.ExceptionMgr(useExceptions=True):
        ds = gdal.GetDriverByName("GTiff").Create(
            path, w, h, 1, gdt, options=["COMPRESS=DEFLATE"])
        ds.SetGeoTransform(gt)
        ds.SetProjection(wkt)
        if metadata:
            ds.SetMetadata(metadata)
        band = ds.GetRasterBand(1)
        # nodata BEFORE the write — on GDAL 3.12 the reverse order backfills
        # all-zero blocks with nodata (see sar_change.write_gtiff)
        band.SetNoDataValue(nodata)
        if dtype == np.uint8:
            band.WriteArray(arr.astype(np.uint8))
        else:
            band.WriteArray(np.where(np.isfinite(arr), arr, nodata).astype(np.float32))
        band.FlushCache()
        del band, ds


def write_float(path, arr, gt, wkt):
    """Raw gamma-naught for change detection: float32, NODATA where invalid —
    what sar_change.read_band reads for a Sentinel-1 scene."""
    _write_tif(path, arr, gt, wkt, np.float32, NODATA)


def to_display(arr, scale="db", lo=-25.0, hi=0.0, stretch=0.20):
    """Gamma-naught -> display bytes with the tab's own stretch.

    The SAME mapping the Planetary Computer render applies to Sentinel-1
    (dB: 10*log10 onto black..white; linear: 0..stretch), so the Display panel
    means one thing for both radars — except that 0 is kept for nodata and the
    ramp runs 1..255."""
    a = np.asarray(arr, dtype=np.float64)
    ok = np.isfinite(a) & (a > 0)
    if scale == "db":
        hi = max(hi, lo + 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (10.0 * np.log10(np.where(ok, a, 1.0)) - lo) / (hi - lo)
    else:
        u = np.where(ok, a, 0.0) / max(float(stretch), 1e-6)
    out = (1.0 + np.round(np.clip(u, 0.0, 1.0) * 254.0)).astype(np.uint8)
    out[~ok] = 0
    return out


def write_display(path, u8, gt, wkt):
    """Grayscale byte GeoTIFF, 0 = nodata, for the amplitude preview layers."""
    _write_tif(path, u8, gt, wkt, np.uint8, 0)
