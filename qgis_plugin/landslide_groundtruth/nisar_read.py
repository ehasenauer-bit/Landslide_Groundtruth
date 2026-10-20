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

The look geometry comes from the product too (look_geometry): NISAR looks LEFT,
so the SAR tab's Sentinel-1 orbit model would put every layover slope on the
wrong side, and the granule's radarGrid cube carries the real line of sight and
incidence angle at the target instead.

Blocking network I/O, 10-25 s per scene (~5 s of it the look geometry; measured
2026-10-19, an 8 km AOI read cold in 23 s): call this from a background task
(nisar_task.NisarReadTask), never on the QGIS GUI thread. GDAL only; no QGIS
imports, so it unit-tests against a synthetic GCOV-shaped file.
"""
import hashlib
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

import numpy as np

GRID = "/science/LSAR/GCOV/grids/frequencyA"
IDENT = "/science/LSAR/identification"
RADAR_GRID = "/science/LSAR/GCOV/metadata/radarGrid"
# Terrain height the look geometry is read at: ONE layer of the cube, the one
# nearest this. The cube runs -1 to 9 km above the ellipsoid in 21 layers, and
# each layer is its own 512x512 HDF5 chunk, so reading the AOI centre at every
# height cost 24 s per scene over /vsicurl where one layer costs ~2 s (measured
# on the Iliamna granule, 2026-10-19). Nothing is lost: there incidence moves
# 45.95 -> 46.10 deg from 0 to 4 km and the look azimuth <0.01 deg, so neither
# a DEM lookup nor a height average would change what a layover mask can see.
_GEOM_HEIGHT_M = 2000.0
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


_LOGIN_FIX = ("Log in again in the 'NASA Earthdata Login' box (Environment header), "
              "and check that your Earthdata profile has approved the 'Alaska "
              "Satellite Facility Data Access' application.")
RETRY_PAUSE_S = 3.0
_STATUS_RE = re.compile(r"CURL_INFO_HEADER_IN: HTTP/[\d.]+ (\d{3})[ \t]*([^\r\n]*)")
# the host a request went to: its own Host: header (only that token is read —
# never the Authorization/Cookie lines in the same message), or curl's
# connection notes when a trace has no request headers
_HOST_OUT_RE = re.compile(r"(?im)^host:[ \t]*([\w.-]+)")
_HOST_TXT_RE = re.compile(r"(?:Established connection to|Connected to|"
                          r"Re-?using existing \w+: connection with host) ([\w.-]+)")
_CTYPE_RE = re.compile(r"(?i)CURL_INFO_HEADER_IN: content-type:[ \t]*([^;\s]+)")
_LOOP_RE = re.compile(r"(?i)maximum \(\d+\) redirects followed")
_NET_ERR_RE = re.compile(r"(?i)(could not resolve host[^.\n]*|connection timed out|"
                         r"operation timed out[^\n]{0,60}|connection refused|"
                         r"ssl[^\n]{0,40}error[^\n]{0,60})")


def explain_http(messages, head=None):
    """Plain-language reason a remote GCOV open failed, from GDAL's curl trace.

    GDAL's own error for ANY failed /vsicurl open is "does not exist in the file
    system, and is not recognized as a supported dataset name" — an Earthdata
    refusal, an ASF outage, a login web page served with HTTP 200 and a missing
    file all read the same. `messages` are the CPLDebug lines of one traced
    attempt (CPL_CURL_VERBOSE); `head` the first bytes it got back. Only status
    codes, host names and content types are read: the credential and cookie
    lines in the same messages are never looked at, let alone returned.

    Returns (reason, retry_worth_it). One detour is normal and not a failure:
    ASF answers GDAL's anonymous size probe with a HEAD-only CloudFront link,
    GDAL tries it for the first range read, gets 403, and goes back to ASF."""
    chain, host, ctype, net, loop = [], None, None, None, False
    for m in messages:
        if m.startswith("CURL_INFO_HEADER_OUT"):
            h = _HOST_OUT_RE.search(m)
            if h:
                host = h.group(1)
            continue                              # request lines: nothing else read
        h = _HOST_TXT_RE.search(m)
        if h:
            host = h.group(1)
        st = _STATUS_RE.search(m)
        if st:
            chain.append((host or "?", int(st.group(1)), st.group(2).strip()))
        ct = _CTYPE_RE.search(m)
        if ct:
            ctype = ct.group(1).lower()
        loop = loop or bool(_LOOP_RE.search(m))
        ne = _NET_ERR_RE.search(m)
        if ne and net is None:
            net = ne.group(1).strip()
    short = [(h.split(".")[0] if h.endswith("cloudfront.net") else h, c)
             for h, c, _r in chain]
    path = " → ".join(f"{h} {c}" for h, c in short[-6:])
    trail = f" (ASF said: {path})" if path else ""
    head = bytes(head or b"")
    if head.startswith(b"\x89HDF"):
        return "the file reads fine now — ASF was briefly unavailable" + trail, True
    codes = [c for _h, c, _r in chain]
    final = codes[-1] if codes else None
    if any(c >= 500 for c in codes):
        h, c, r = next(x for x in chain if x[1] >= 500)
        return (f"ASF's server is failing (HTTP {c}{' ' + r if r else ''} from {h}) "
                f"— nothing is wrong on your side; try again in a few minutes"
                + trail), True
    if final == 404:
        return ("ASF has no file at that address (HTTP 404) — the search listed a "
                "granule that is not published (yet); pick another scene" + trail), False
    if 401 in codes:
        return ("Earthdata Login refused the credentials (HTTP 401). " + _LOGIN_FIX
                + trail), False
    if loop or sum(1 for c in codes if 300 <= c < 400) >= 10:
        return ("Earthdata Login kept redirecting without letting the read through — "
                "the login is not being accepted. " + _LOGIN_FIX + trail), False
    if ctype == "text/html" or head.lstrip()[:1] == b"<":
        return ("a web page came back instead of the file — Earthdata's sign-in "
                "page, so the login was not accepted. " + _LOGIN_FIX + trail), False
    real403 = [x for i, x in enumerate(chain) if x[1] == 403
               and not ("cloudfront" in x[0] and i + 1 < len(chain))]
    if real403:
        return ("access refused (HTTP 403). " + _LOGIN_FIX + " If it persists, "
                "delete landslide_edl_cookies.txt in the temp folder to drop a "
                "stale session." + trail), False
    if net:
        return f"could not reach ASF: {net} — check the network connection" + trail, True
    if not chain:
        return "no answer from ASF at all — check the network connection", True
    return "the read failed" + trail, True


def _traced_probe(vsi_path):
    """Read the first bytes of a remote granule with curl tracing on, for THIS
    thread only, and return (messages, head). Thread-local config options and a
    thread-local error handler, so a trace never leaks into QGIS's own logging
    or into another task's reads."""
    from osgeo import gdal
    msgs, head = [], None
    gdal.VSICurlPartialClearCache(vsi_path)
    gdal.SetThreadLocalConfigOption("CPL_DEBUG", "ON")
    gdal.SetThreadLocalConfigOption("CPL_CURL_VERBOSE", "YES")
    gdal.PushErrorHandler(lambda _cls, _no, msg: msgs.append(str(msg)))
    try:
        f = gdal.VSIFOpenL(vsi_path, "rb")
        if f is not None:
            try:
                head = gdal.VSIFReadL(1, 8, f)
            finally:
                gdal.VSIFCloseL(f)
    except Exception as e:                       # noqa: BLE001 — diagnosis only
        msgs.append(str(e))
    finally:
        gdal.PopErrorHandler()
        gdal.SetThreadLocalConfigOption("CPL_DEBUG", None)
        gdal.SetThreadLocalConfigOption("CPL_CURL_VERBOSE", None)
    return msgs, head


def _open_granule(url):
    """gdal.OpenEx on a GCOV granule, once more after a pause if it fails, and
    with a real reason attached if it still does.

    GDAL caches what it learned about a /vsicurl file for the life of the
    process, so each retry first clears that file's entry; the second attempt
    also starts a fresh Earthdata cookie jar, in case a stale session is what
    is being refused. Caller holds _HDF5_LOCK and gdal.ExceptionMgr."""
    from osgeo import gdal
    path = _vsi(url)

    def attempt():
        return gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER, allowed_drivers=["HDF5"])

    try:
        return attempt()
    except RuntimeError:
        if "://" not in url:
            raise                                # a local file: nothing to retry
    gdal.VSICurlPartialClearCache(path)
    jar = gdal.GetPathSpecificOption(path, "GDAL_HTTP_COOKIEJAR")
    if jar and os.path.exists(jar):
        try:
            os.remove(jar)
        except OSError:
            pass
    time.sleep(RETRY_PAUSE_S)
    try:
        return attempt()
    except RuntimeError:
        pass
    reason, worth = explain_http(*_traced_probe(path))
    if worth and reason.startswith("the file reads fine now"):
        gdal.VSICurlPartialClearCache(path)
        try:
            return attempt()
        except RuntimeError:
            reason = "the file answers, but GDAL still cannot open it as HDF5"
    raise RuntimeError(f"could not open {os.path.basename(urlsplit(url).path)}: "
                       f"{reason}")


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


# ---------------------------------------------------------------- look geometry
def los_look_azimuth(los_east, los_north):
    """Compass azimuth the radar LOOKS along (sensor out across the ground, the
    direction of increasing ground range — what layover_dim calls the look
    azimuth) from GCOV's line-of-sight vector.

    GCOV's LOS points from the target TO the sensor, so the look is its
    horizontal part turned around. Its X/Y are local EAST/NORTH (ENU at the
    target, as the file's own attributes say), NOT components along the
    granule's map grid, even though the cube is laid out on that grid. Measured
    on the Iliamna track-150 granule: as ENU the look is 241.4 deg, and the
    slant-range cube's own gradient points 240.0 deg; read as EPSG:3413 grid
    components (rotated ~108 deg from north at 153 W) it would be 133 deg."""
    return float(np.degrees(np.arctan2(-float(los_east), -float(los_north))) % 360.0)


def _bilinear(block, fy, fx):
    """Weighted mean over the 2x2 corners of `block` (..., 2, 2) at fractional
    offsets (fy, fx) in [0, 1], ignoring NaN corners — the cube is NaN off the
    swath, and an AOI on the swath edge should still get its valid neighbours."""
    w = np.array([[(1 - fy) * (1 - fx), (1 - fy) * fx],
                  [fy * (1 - fx), fy * fx]], dtype=np.float64)
    v = np.asarray(block, dtype=np.float64)
    ok = np.isfinite(v)
    num = np.where(ok, v, 0.0) * w
    den = ok * w
    with np.errstate(invalid="ignore", divide="ignore"):
        return num.sum(axis=(-2, -1)) / den.sum(axis=(-2, -1))


def look_geometry(root, bbox, look=None, grid_epsg=None):
    """(look_azimuth_deg, incidence_deg, note) at the AOI centre, from the
    granule's radarGrid cube — or (None, None, why) when it cannot be trusted.

    The cube is coarse (500 m on the Iliamna granule) and 3-D: heights x y x x.
    It is interpolated bilinearly at the AOI centre on the one height layer
    nearest _GEOM_HEIGHT_M (2 km; see there for why one layer is enough).

    Cross-checked before it is returned, because a wrong look direction does
    not fail, it silently paints the layover on the opposite slopes: the LOS's
    horizontal length must equal sin(incidence) (a unit vector in ENU), and
    when the cube carries the along-track vector the look must sit ~90 deg to
    the side the identification's lookDirection names (left = counter-
    clockwise of along-track). Must be called with the dataset open and
    _HDF5_LOCK held, like every other HDF5 call here."""
    from osgeo import osr
    g = root.OpenGroupFromFullname(RADAR_GRID)
    if g is None:
        return None, None, "no radarGrid cube in the granule"
    names = set(g.GetMDArrayNames() or [])
    need = {"losUnitVectorX", "losUnitVectorY", "incidenceAngle",
            "xCoordinates", "yCoordinates"}
    if not need <= names:
        return None, None, "radarGrid lacks " + ", ".join(sorted(need - names))
    epsg = None
    if "projection" in names:
        for a in g.OpenMDArray("projection").GetAttributes():
            if a.GetName() == "epsg_code":
                epsg = int(a.Read())
    epsg = epsg or grid_epsg
    if not epsg:
        return None, None, "radarGrid has no projection@epsg_code"
    x = g.OpenMDArray("xCoordinates").ReadAsArray().astype(np.float64)
    y = g.OpenMDArray("yCoordinates").ReadAsArray().astype(np.float64)
    if x.size < 2 or y.size < 2:
        return None, None, "radarGrid is a single point"
    minx, miny, maxx, maxy = bbox
    tr = osr.CoordinateTransformation(_srs(4326), _srs(epsg))
    cx, cy, _ = tr.TransformPoint(0.5 * (minx + maxx), 0.5 * (miny + maxy))
    fx = (cx - x[0]) / (x[1] - x[0])
    fy = (cy - y[0]) / (y[1] - y[0])
    if not (0.0 <= fx <= x.size - 1 and 0.0 <= fy <= y.size - 1):
        return None, None, "the AOI centre is outside the radarGrid cube"
    c0 = int(min(np.floor(fx), x.size - 2))
    r0 = int(min(np.floor(fy), y.size - 2))
    level = None
    if "heightAboveEllipsoid" in names:
        h = g.OpenMDArray("heightAboveEllipsoid").ReadAsArray().astype(np.float64)
        if h.size:
            level = int(np.argmin(np.abs(h - _GEOM_HEIGHT_M)))

    def corners(name):
        """The 2x2 cells around the AOI centre on ONE height layer (every
        layer is a separate chunk to fetch), interpolated to the centre."""
        a = g.OpenMDArray(name)
        nd = a.GetDimensionCount()
        lead = [d.GetSize() for d in a.GetDimensions()[:nd - 2]]
        if nd == 3 and level is not None and level < lead[0]:
            start, count = [level, r0, c0], [1, 2, 2]
        else:                               # no height axis to pick from
            start, count = [0] * (nd - 2) + [r0, c0], lead + [2, 2]
        v = a.ReadAsArray(array_start_idx=start, count=count)
        return _bilinear(v.reshape((-1, 2, 2)), fy - r0, fx - c0)

    lx, ly, inc = (corners(n) for n in
                   ("losUnitVectorX", "losUnitVectorY", "incidenceAngle"))
    sel = np.isfinite(lx) & np.isfinite(ly) & np.isfinite(inc)
    if not sel.any():
        return None, None, "the radarGrid cube is empty at the AOI centre"
    e, n, theta = float(lx[sel].mean()), float(ly[sel].mean()), float(inc[sel].mean())
    if not 1.0 <= theta <= 89.0:
        return None, None, f"implausible incidence {theta:.1f} deg"
    horiz = float(np.hypot(e, n))
    if abs(horiz - np.sin(np.radians(theta))) > 0.05:
        return None, None, (f"LOS horizontal length {horiz:.3f} does not match "
                            f"sin(incidence) {np.sin(np.radians(theta)):.3f} — "
                            "not an ENU unit vector")
    az = los_look_azimuth(e, n)
    side = None
    if {"alongTrackUnitVectorX", "alongTrackUnitVectorY"} <= names:
        ax = corners("alongTrackUnitVectorX")
        ay = corners("alongTrackUnitVectorY")
        ok = sel & np.isfinite(ax) & np.isfinite(ay)
        if ok.any():
            along = float(np.degrees(np.arctan2(ax[ok].mean(), ay[ok].mean())) % 360.0)
            gap = (az - along + 540.0) % 360.0 - 180.0   # + = clockwise = right
            if not 60.0 <= abs(gap) <= 120.0:
                return None, None, (f"look {az:.0f} deg is {gap:+.0f} deg from "
                                    f"along-track {along:.0f} deg, not ~90")
            side = "Left" if gap < 0 else "Right"
            want = str(look or "").strip().capitalize()
            if want in ("Left", "Right") and want != side:
                return None, None, (f"the LOS says {side}-looking but the "
                                    f"granule says {want}")
    return az, theta, (f"from the granule's radarGrid (EPSG:{epsg}"
                       + (f", {side}-looking" if side else "") + ")")


# ---------------------------------------------------------------- native read
def read_native(url, pol, bbox, max_side=MAX_SIDE):
    """The AOI window of one GCOV polarization in the granule's OWN grid.

    Returns dict(arr float32 with NaN where invalid, gt, wkt, step, epsg,
    look, pass_direction, look_azimuth_deg, incidence_deg, geometry_note) — the
    last three from look_geometry, the two numbers None when the radarGrid
    could not be read or trusted (a geometry failure never fails the pixel
    read: the tab then skips the layover fade and terrain masks for this
    scene, as it did before they existed). Raises LookupError when the granule does not cover
    the box or lacks the polarization, PermissionError/RuntimeError for access
    problems. Every GDAL object is held in a local until the read is done:
    letting the dataset be collected while an array or band from it is still in
    use invalidates that child."""
    from osgeo import gdal
    term = covariance_term(pol)
    with _HDF5_LOCK, gdal.ExceptionMgr(useExceptions=True):
        ds = _open_granule(url)
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
        try:
            az, inc, gnote = look_geometry(root, bbox, look, epsg)
        except Exception as e:              # noqa: BLE001 — never fails the read
            az, inc, gnote = None, None, f"radarGrid unreadable ({type(e).__name__}: {e})"
        wkt = _srs(epsg).ExportToWkt()
        del grp, ident, root, ds
    wgt = (gt[0] + c0 * gt[1], gt[1] * step, 0.0,
           gt[3] + r0 * gt[5], 0.0, gt[5] * step)
    return dict(arr=arr, gt=wgt, wkt=wkt, step=step, epsg=epsg,
                look=str(look) if look else None,
                pass_direction=str(pdir) if pdir else None,
                look_azimuth_deg=az, incidence_deg=inc, geometry_note=gnote)


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


# Bumped whenever the cached window's metadata gains something a reader needs:
# a window cached before the look geometry existed would otherwise answer
# "no geometry" for that scene forever. v2 = look_azimuth_deg / incidence_deg.
_CACHE_VERSION = "v2"


def _cache_path(cache_dir, url, pol, bbox, max_side):
    key = "|".join([os.path.basename(urlsplit(url).path) or url, pol.lower(),
                    ",".join(f"{v:.5f}" for v in bbox), str(max_side),
                    _CACHE_VERSION])
    return os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest()[:20] + ".tif")


def read_aoi(url, pol, bbox, px, cache_dir=None, log=None):
    """One NISAR scene on the AOI grid: (float32 gamma-naught px x px with NaN
    where invalid, geotransform, WKT, info dict). The info carries the look
    geometry (look_azimuth_deg, incidence_deg, geometry_note) — see read_native.

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
    info = {k: native.get(k) for k in ("step", "epsg", "look", "pass_direction",
                                       "look_azimuth_deg", "incidence_deg",
                                       "geometry_note")}
    info["native_res"] = abs(native["gt"][1])
    info["valid_frac"] = float(np.isfinite(out).mean()) if out.size else 0.0
    return out, gt, wkt, info


def _write_cached(path, native):
    """Write beside the final name, then rename: a Preview task and a change-
    detection task can cache the same window at once, and a reader must never
    open a half-written file (os.replace is atomic on one filesystem)."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part.tif"

    def num(v):
        return "" if v is None else repr(float(v))     # repr: round-trips exactly

    try:
        _write_tif(tmp, native["arr"], native["gt"], native["wkt"], np.float32,
                   NODATA, {"look": native.get("look") or "",
                            "step": str(native["step"]),
                            "epsg": str(native["epsg"]),
                            "pass_direction": native.get("pass_direction") or "",
                            "look_azimuth_deg": num(native.get("look_azimuth_deg")),
                            "incidence_deg": num(native.get("incidence_deg")),
                            "geometry_note": native.get("geometry_note") or ""})
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

        def num(k):
            return float(md[k]) if md.get(k) else None

        out = dict(arr=arr, gt=ds.GetGeoTransform(), wkt=ds.GetProjection(),
                   step=int(md.get("step") or 1), epsg=int(md.get("epsg") or 0),
                   look=md.get("look") or None,
                   pass_direction=md.get("pass_direction") or None,
                   look_azimuth_deg=num("look_azimuth_deg"),
                   incidence_deg=num("incidence_deg"),
                   geometry_note=md.get("geometry_note") or None)
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
