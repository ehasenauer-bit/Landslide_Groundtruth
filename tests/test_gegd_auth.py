"""The GEGD Pro key must reach pro.gegd.com, and nothing else, ever.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_auth.py

The key lives in the QGIS password store as the APIHeader auth config 'gegdpro',
and QGIS adds it to any request or layer that names authcfg=gegdpro. That is
what keeps it out of saved projects, and it is also the danger: an auth config
sends its header to ANY host a layer names, so a crafted .qgz pointing
authcfg=gegdpro at another server would collect the key (the M0 check caught a
local server doing exactly that on 15 QGIS request paths). gegd_auth.HostGuard
strips the header from everything except https on pro.gegd.com:443.

Everything here is offline, in a throwaway profile and auth DB with the macOS
Keychain helper switched off BEFORE the master password is set, against two local
HTTP servers and synthetic keys. The guard is aimed at http://127.0.0.1:<A> as a
stand-in for pro.gegd.com (HostGuard(allowed=...)); the second server, reached as
'localhost' and as 127.0.0.1 on another port, plays every host the key must
never reach. Each "arrives" check is paired with a "stripped" one, and the guard
is removed once at the end to prove it was the thing doing the stripping. The
plugin's unload (release_host_guard) is checked to keep stripping, and a reload to
replace the released guard rather than stack a second one.
"""
import os, secrets, shutil, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The XYZ and WMS renders need proj.db to resolve EPSG:3857; without it they ask
# for no tiles at all. run_all.sh exports these; a direct run takes the bundle's.
for _base in ("/Applications/QGIS-LTR.app", "/Applications/QGIS.app"):
    _proj = os.path.join(_base, "Contents", "Resources", "qgis", "proj")
    if os.path.isdir(_proj):
        os.environ.setdefault("PROJ_DATA", _proj)
        os.environ.setdefault("PROJ_LIB", _proj)
        os.environ.setdefault("GDAL_DATA", os.path.join(_base, "Contents", "Resources", "qgis", "gdal"))
        break
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

# A throwaway profile and auth DB, set BEFORE QgsApplication exists: QGIS reads
# both once, so the real QGIS3.ini and qgis-auth.db are never opened.
TMP = tempfile.mkdtemp(prefix="gegd_auth_")
for _d in ("auth", "config", "cache"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")
os.environ["LANDSLIDE_GEGD_CACHE"] = os.path.join(TMP, "cache")

from qgis.core import (QgsApplication, QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem,
                       QgsDataSourceUri, QgsFeedback, QgsFileDownloader, QgsMapRendererParallelJob,
                       QgsMapSettings, QgsNetworkAccessManager, QgsNetworkRequestParameters,
                       QgsRasterLayer, QgsRectangle, QgsSettings, QgsTask)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)       # the APIHeader auth method lives here
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QSize, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QImage
from qgis.PyQt.QtNetwork import QNetworkRequest

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
    sys.exit(2)
if not QgsCoordinateReferenceSystem("EPSG:3857").isValid():
    print("EPSG:3857 does not resolve (no proj.db: set PROJ_DATA); the render checks "
          "would fail for the wrong reason")
    sys.exit(2)

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


# ---- the modules must not touch the auth manager while importing ----
print("\n=== importing never opens the password store ===")
_real_am = QgsApplication.authManager


def _no_am(*_a, **_k):
    raise AssertionError("authManager() called at import time")


QgsApplication.authManager = staticmethod(_no_am)
try:
    from landslide_groundtruth import gegd_auth, gegd_net
    from landslide_groundtruth.gegd_api import GegdError
    imported = True
except AssertionError as e:
    print(f"        {e}")
    imported = False
finally:
    QgsApplication.authManager = _real_am
check("gegd_auth and gegd_net import without calling authManager()", imported)
if not imported:
    sys.exit(1)

am = QgsApplication.authManager()
am.setPasswordHelperEnabled(False)            # BEFORE any master-password call
if am.passwordHelperEnabled() or not os.path.realpath(
        am.authenticationDatabasePath()).startswith(os.path.realpath(TMP)):
    print("auth DB not isolated or Keychain helper still on; refusing to continue")
    sys.exit(2)
if not am.setMasterPassword(secrets.token_urlsafe(24), True):
    print("could not set the throwaway master password")
    sys.exit(2)

# Synthetic keys, shaped like nothing real. Never a real GEGD key.
KEY1 = "dummy-gegd-key-ONE-" + "0f1e2d3c4b5a69788796a5b4"
KEY2 = "dummy-gegd-key-TWO-" + "a1b2c3d4e5f60718293a4b5c"
KEY3 = "dummy-gegd-key-THREE-" + "5e4d3c2b1a09f8e7d6c5b4"
HDR = gegd_auth.KEY_HEADER

# ---- the network: two local servers that record what each request carried ----
_img = QImage(256, 256, QImage.Format_ARGB32); _img.fill(QColor(30, 140, 200))
_ba = QByteArray(); _buf = QBuffer(_ba); _buf.open(QIODevice.WriteOnly); _img.save(_buf, "PNG")
PNG = bytes(_ba)
SEEN = []                       # {"srv", "tag", "key"} per request received
LK = threading.Lock()
COUNTS = {}                     # per-tag request counter for the flaky routes


def caps_xml(getmap_base):
    bb = ('<EX_GeographicBoundingBox><westBoundLongitude>-180</westBoundLongitude>'
          '<eastBoundLongitude>180</eastBoundLongitude><southBoundLatitude>-85</southBoundLatitude>'
          '<northBoundLatitude>85</northBoundLatitude></EX_GeographicBoundingBox>')
    return ('<?xml version="1.0" encoding="UTF-8"?><WMS_Capabilities version="1.3.0" '
            'xmlns="http://www.opengis.net/wms" xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<Service><Name>WMS</Name><Title>t</Title><OnlineResource xlink:href="%s"/></Service>'
            '<Capability><Request><GetCapabilities><Format>text/xml</Format><DCPType><HTTP><Get>'
            '<OnlineResource xlink:href="%s"/></Get></HTTP></DCPType></GetCapabilities>'
            '<GetMap><Format>image/png</Format><DCPType><HTTP><Get><OnlineResource xlink:href="%s"/>'
            '</Get></HTTP></DCPType></GetMap></Request><Exception><Format>XML</Format></Exception>'
            '<Layer><Title>root</Title><CRS>EPSG:3857</CRS><CRS>CRS:84</CRS>%s'
            '<Layer queryable="0"><Name>Maxar:Imagery</Name><Title>L</Title><CRS>EPSG:3857</CRS>%s'
            '<BoundingBox CRS="EPSG:3857" minx="-20037508" miny="-20037508" maxx="20037508" '
            'maxy="20037508"/></Layer></Layer></Capability></WMS_Capabilities>'
            ) % (getmap_base, getmap_base, getmap_base, bb, bb)


OGC_EXC = (b'<?xml version="1.0" encoding="UTF-8"?><ServiceExceptionReport version="1.3.0" '
           b'xmlns="http://www.opengis.net/ogc"><ServiceException code="InvalidParameterValue">'
           b'Could not parse CQL filter list.</ServiceException></ServiceExceptionReport>')


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        tag = self.path.lstrip("/").split("/")[0].split("?")[0]
        with LK:
            SEEN.append({"srv": self.server.label, "tag": tag, "key": self.headers.get(HDR)})
            n = COUNTS[tag] = COUNTS.get(tag, 0) + 1
        low = self.path.lower()
        if tag.startswith("redir"):
            self.send_response(302)
            self.send_header("Location", "http://localhost:%d/after_%s%s"
                             % (SRV_B.server_port, tag, self.path[len("/" + tag):]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "request=getcapabilities" in low:
            host = self.headers.get("Host")
            self._send(200, caps_xml("http://%s/%s?" % (host, tag)).encode(), "text/xml")
        elif tag.startswith("flaky"):                  # 503, 503, then 200
            if n <= 2:
                self._send(503, b"busy", "text/plain")
            else:
                self._send(200, b'{"ok": 1}', "application/json")
        elif tag == "ra_short":                        # 429 asking for 1 s, then 200
            if n == 1:
                self._send(429, b"slow down", "text/plain", [("Retry-After", "1")])
            else:
                self._send(200, b'{"ok": 2}', "application/json")
        elif tag == "ra_long":                         # 429 asking for an hour
            self._send(429, b"slow down", "text/plain", [("Retry-After", "3600")])
        elif tag == "always503":
            self._send(503, b"busy", "text/plain")
        elif tag == "slow":                            # longer than the test's timeout
            time.sleep(2.5)
            self._send(200, b'{"late": 1}', "application/json")
        elif tag == "echo":                            # GEGD copies a query-form key into links
            self._send(400, ("Bad request https://pro.gegd.com/x?maxar_api_key=%s&y=1 "
                             "(maxar-api-key: %s)" % (KEY3, KEY3)).encode(), "text/plain")
        elif tag == "nojwt":
            self._send(401, b'{"message":"No JWT bearer token found in request"}', "application/json")
        elif tag == "ogc":
            self._send(200, OGC_EXC, "application/vnd.ogc.se_xml")
        elif tag.startswith("json") or tag.startswith("blk") or tag.startswith("after_redir_fetch"):
            self._send(200, b'{"type": "FeatureCollection", "features": []}', "application/json")
        else:
            self._send(200, PNG, "image/png")


def serve(bind, label):
    srv = ThreadingHTTPServer((bind, 0), H)
    srv.label = label
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SRV_A = serve("127.0.0.1", "A")       # the stand-in for pro.gegd.com
SRV_B = serve("127.0.0.1", "B")       # every other host
PA, PB = SRV_A.server_port, SRV_B.server_port
ALLOW_A = ("http", "127.0.0.1", PA)
A = "http://127.0.0.1:%d" % PA
B_LOCAL = "http://localhost:%d" % PB
B_IP = "http://127.0.0.1:%d" % PB


def wait(sec):
    lp = QEventLoop(); QTimer.singleShot(int(sec * 1000), lp.quit); lp.exec_()


def seen(tag_prefix, srv=None):
    """Requests received whose first path segment starts with tag_prefix."""
    wait(0.3)                                    # queued signals and server threads settle
    with LK:
        return [s for s in SEEN if s["tag"].startswith(tag_prefix) and (srv is None or s["srv"] == srv)]


def keys(rows):
    return [r["key"] for r in rows]


# The Network Logger's feed: what each request looked like after the preprocessors.
TAPS = []


def _tap(p):
    r = p.request()
    TAPS.append({"url": r.url().toString(), "has": r.hasRawHeader(HDR.encode()),
                 "policy": r.attribute(QNetworkRequest.RedirectPolicyAttribute)})


QgsNetworkAccessManager.instance().requestAboutToBeCreated[QgsNetworkRequestParameters].connect(_tap)


def blocking(url, authcfg=gegd_auth.AUTHCFG_ID):
    r = QgsBlockingNetworkRequest()
    if authcfg:
        r.setAuthCfg(authcfg)
    return int(r.get(QNetworkRequest(QUrl(url)), True))


EXT = QgsRectangle(-17040000, 8420000, -17035000, 8425000)


def render(layer):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    ms.setExtent(EXT)
    ms.setOutputSize(QSize(256, 256))
    job = QgsMapRendererParallelJob(ms)
    lp = QEventLoop(); job.finished.connect(lp.quit)
    job.start()
    QTimer.singleShot(30000, lp.quit)
    lp.exec_()
    return job.renderedImage()


def xyz(base, tag):
    d = QgsDataSourceUri()
    d.setParam("type", "xyz")
    d.setParam("url", "%s/%s/{z}/{x}/{y}.png" % (base, tag))
    d.setParam("zmin", "0")
    d.setParam("zmax", "19")
    d.setAuthConfigId(gegd_auth.AUTHCFG_ID)
    return QgsRasterLayer(bytes(d.encodedUri()).decode(), tag, "wms")


def wms(base, tag):
    d = QgsDataSourceUri()
    d.setParam("url", "%s/%s?" % (base, tag))
    d.setParam("layers", "Maxar:Imagery")
    d.setParam("styles", "")
    d.setParam("format", "image/png")
    d.setParam("crs", "EPSG:3857")
    d.setAuthConfigId(gegd_auth.AUTHCFG_ID)
    return QgsRasterLayer(bytes(d.encodedUri()).decode(), tag, "wms")


def profile_files_holding(*secrets_):
    hits = []
    for dp, _dn, fn in os.walk(TMP):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            if any(s.encode() in data for s in secrets_):
                hits.append(os.path.relpath(p, TMP))
    return hits


print("\n=== the store: probe, validate, save ===")
check("this QGIS has the APIHeader auth method", gegd_auth.has_apiheader())
check("mode() is 'authcfg'", gegd_auth.mode() == "authcfg")
check("no key before one is saved", not gegd_auth.has_key() and gegd_auth.key_set_utc() is None
      and gegd_auth.key_age_days() is None)
for bad in ("", "   ", "two words", "tab\there", "ключ-not-ascii"):
    try:
        gegd_auth.store_key(bad)
        raised, msg = False, ""
    except ValueError as e:
        raised, msg = True, str(e)
    check(f"store_key refuses {bad!r} without echoing it",
          raised and (not bad.strip() or bad.strip() not in msg) and not gegd_auth.has_key())

t0 = time.time()
check("store_key saves a pasted key (surrounding whitespace dropped)",
      gegd_auth.store_key("  " + KEY1 + "\n") is True)
check("has_key() sees it", gegd_auth.has_key())
ks = gegd_auth.key_set_utc()
check("the save time is recorded in UTC", ks is not None and ks.tzinfo is not None
      and abs(ks.timestamp() - t0) < 60 and 0 <= gegd_auth.key_age_days() < 0.01)
QgsSettings().sync()
check(f"no profile file holds the key in clear (auth DB is encrypted) "
      f"({profile_files_holding(KEY1)})", profile_files_holding(KEY1) == [])

print("\n=== ensure_ready: GUI thread only, installs the guard ===")
gegd_auth.remove_host_guard()
check("ensure_ready() on the GUI thread is True", gegd_auth.ensure_ready() is True)
g = gegd_auth.host_guard()
check("ensure_ready installed the HostGuard, aimed at https://pro.gegd.com:443",
      g is not None and g.installed and g.allowed == ("https", "pro.gegd.com", 443))
off_thread = []
th = threading.Thread(target=lambda: off_thread.append(gegd_auth.ensure_ready()))
th.start(); th.join(10)
check("ensure_ready() off the GUI thread refuses (False)", off_thread == [False])
g2 = gegd_auth.install_host_guard(allowed=ALLOW_A)
check("install_host_guard() again re-aims the same guard, no second one",
      g2 is g and g.allowed == ALLOW_A)

print("\n=== the key arrives on the allowed host ===")
n_taps = len(TAPS)
check("blocking request on the GUI thread succeeds", blocking(A + "/blk_main/x") == 0)
check("... and carries the key", keys(seen("blk_main")) == [KEY1])
t = [x for x in TAPS[n_taps:] if "/blk_main/" in x["url"]]
check("... with ManualRedirectPolicy set on it (Qt never follows a redirect with the key)",
      len(t) == 1 and t[0]["has"] and t[0]["policy"] == QNetworkRequest.ManualRedirectPolicy)


class FetchTask(QgsTask):
    def __init__(self, url, raw=False):
        super().__init__("gegd test fetch", QgsTask.CanCancel)
        self.url, self.raw, self.result_, self.error_ = url, raw, None, None
        self.fb = QgsFeedback()                      # the plan's tasks pass one to fetch()

    def cancel(self):
        self.fb.cancel()
        super().cancel()

    def run(self):
        try:
            if self.raw:
                self.result_ = blocking(self.url)
            else:
                self.result_ = gegd_net.fetch(self.url, "wfs", feedback=self.fb)
            return True
        except Exception as e:                       # noqa: BLE001 -- reported below
            self.error_ = e
            return False


def run_task(task):
    lp = QEventLoop()
    task.taskCompleted.connect(lp.quit)
    task.taskTerminated.connect(lp.quit)
    QgsApplication.taskManager().addTask(task)
    QTimer.singleShot(30000, lp.quit)
    lp.exec_()
    return task


tk = run_task(FetchTask(A + "/blk_task/x", raw=True))
check("blocking request inside a QgsTask carries the key",
      tk.result_ == 0 and keys(seen("blk_task")) == [KEY1])
tk = run_task(FetchTask(A + "/json_task?x=1"))
check(f"gegd_net.fetch inside a QgsTask returns the reply ({tk.error_!r})",
      tk.error_ is None and tk.result_ is not None and tk.result_.status == 200
      and b"FeatureCollection" in tk.result_.body and "json" in tk.result_.ctype)
check("... and the request carried the key", keys(seen("json_task")) == [KEY1])

u0 = gegd_auth.usage_today()
img = render(xyz(A, "xyz_a"))
tiles = seen("xyz_a")
check(f"XYZ render: every tile carries the key ({len(tiles)} tiles)",
      len(tiles) >= 1 and all(k == KEY1 for k in keys(tiles)))
check("XYZ render drew the tiles", img.pixelColor(128, 128).alpha() == 255)
u1 = gegd_auth.usage_today()
check(f"usage counts each XYZ tile once, not twice ({u1[0] - u0[0]} counted, {len(tiles)} served)",
      u1[0] - u0[0] == len(tiles))
check(f"usage adds the bytes received ({u1[1] - u0[1]} B)", u1[1] - u0[1] >= len(tiles) * len(PNG))

lyr = wms(A, "wms_a")
check("WMS layer on the allowed host is valid", lyr.isValid())
render(lyr)
caps = [r for r in seen("wms_a")]
check(f"WMS GetCapabilities and GetMap both carry the key ({len(caps)} requests)",
      len(caps) >= 2 and all(k == KEY1 for k in keys(caps)))

print("\n=== the key is stripped everywhere else ===")
render(xyz(B_LOCAL, "xyz_other"))
t = seen("xyz_other")
check(f"XYZ tiles to localhost arrive without the key ({len(t)} tiles)",
      len(t) >= 1 and all(k is None for k in keys(t)))
render(xyz(B_IP, "xyz_port"))
t = seen("xyz_port")
check(f"XYZ tiles to the allowed host name on another port: no key ({len(t)} tiles)",
      len(t) >= 1 and all(k is None for k in keys(t)))
blocking(B_LOCAL + "/blk_other/x")
check("blocking request to localhost: no key", keys(seen("blk_other")) == [None])
blocking(B_IP + "/blk_port/x")
check("blocking request to another port: no key", keys(seen("blk_port")) == [None])
lyr = wms(B_LOCAL, "wms_other")
render(lyr)
t = seen("wms_other")
check(f"WMS caps + GetMap to localhost: no key ({len(t)} requests)",
      len(t) >= 1 and all(k is None for k in keys(t)))

gegd_auth.install_host_guard(allowed=("https", "127.0.0.1", PA))
blocking(A + "/blk_http/x")
render(xyz(A, "xyz_http"))
t = seen("blk_http") + seen("xyz_http")
check(f"plain http to the allowed host name and port: no key ({len(t)} requests)",
      len(t) >= 2 and all(k is None for k in keys(t)))
gegd_auth.install_host_guard(allowed=ALLOW_A)

print("\n=== ... and across a redirect to another host ===")
blocking(A + "/redir_blk/x")
check("blocking: first hop carries the key, the redirected hop does not",
      keys(seen("redir_blk", "A")) == [KEY1] and keys(seen("after_redir_blk", "B")) == [None])
render(xyz(A, "redir_xyz"))
first, after = seen("redir_xyz", "A"), seen("after_redir_xyz", "B")
check(f"XYZ: redirected tiles arrive without the key ({len(after)} of {len(first)})",
      len(first) >= 1 and all(k == KEY1 for k in keys(first)) and all(k is None for k in keys(after)))
dl = QgsFileDownloader(QUrl(A + "/redir_dl/x"), os.path.join(TMP, "dl.bin"), gegd_auth.AUTHCFG_ID)
lp = QEventLoop(); dl.downloadExited.connect(lp.quit); QTimer.singleShot(15000, lp.quit); lp.exec_()
check("QgsFileDownloader: Qt does not carry the key across the redirect",
      keys(seen("redir_dl", "A")) == [KEY1] and all(k is None for k in keys(seen("after_redir_dl", "B"))))
try:
    gegd_net.fetch(A + "/redir_fetch/x", "wfs", retries=0)
    kind = None
except GegdError as e:
    kind = e.kind
check(f"fetch: an off-host redirect is refused, not used ({kind})", kind == "endpoint_moved")
check("... and the redirected hop carried no key", all(k is None for k in keys(seen("after_redir_fetch"))))

print("\n=== the guard's rule, request by request (no network) ===")
guard = gegd_auth.HostGuard()
for url, keep in [("https://pro.gegd.com/x", True), ("https://PRO.GEGD.COM/x", True),
                  ("https://pro.gegd.com:443/x", True), ("https://user@pro.gegd.com/x", True),
                  ("http://pro.gegd.com/x", False), ("https://pro.gegd.com:8443/x", False),
                  ("https://pro.gegd.com./x", False), ("https://pro.gegd.com@evil.example/x", False),
                  ("https://evil.example/?h=pro.gegd.com", False),
                  ("https://pro.gegd.com.evil.example/x", False), ("ftp://pro.gegd.com/x", False),
                  ("https://xn--pro-gegd.com/x", False), ("http://127.0.0.1/x", False)]:
    rq = QNetworkRequest(QUrl(url)); rq.setRawHeader(HDR.encode(), b"DUMMY")
    guard.preprocess(rq)
    kept = rq.hasRawHeader(HDR.encode())
    pol = rq.attribute(QNetworkRequest.RedirectPolicyAttribute)
    check(f"{url}: {'kept, manual redirects' if keep else 'stripped'}",
          kept == keep and (pol == QNetworkRequest.ManualRedirectPolicy) == keep)
rq = QNetworkRequest(QUrl("https://evil.example/x"))
guard.preprocess(rq)
check("a request without the header is left alone",
      not rq.hasRawHeader(HDR.encode()) and rq.attribute(QNetworkRequest.RedirectPolicyAttribute) is None)
check("allowed_for: an https host only",
      gegd_auth.allowed_for("https://pro.gegd.com") == ("https", "pro.gegd.com", 443)
      and gegd_auth.allowed_for("http://pro.gegd.com") is None and gegd_auth.allowed_for("") is None)
s = QgsSettings()
s.setValue(gegd_auth.S_HOST, "http://evil.example")
cleartext = gegd_auth.configured_allowed()
s.setValue(gegd_auth.S_HOST, "https://pro.gegd.com")
default = gegd_auth.configured_allowed()
s.setValue(gegd_auth.S_HOST, "https://evil.example")
evil = gegd_auth.configured_allowed()
evil_guard = gegd_auth.install_host_guard().allowed
check("a cleartext host in settings is ignored; the default stands",
      cleartext == default == gegd_auth.DEFAULT_ALLOWED)
check("… and so is an https host nobody confirmed (no host field or dialog yet): "
      "the guard stays on https://pro.gegd.com:443",
      evil == evil_guard == gegd_auth.DEFAULT_ALLOWED == ("https", "pro.gegd.com", 443))
s.remove(gegd_auth.S_HOST)
gegd_auth.install_host_guard(allowed=ALLOW_A)

print("\n=== no auth config, nothing sent ===")
err = blocking(A + "/missing_cfg/x", authcfg="nosuch1")
check("a request naming a missing authcfg fails", err != 0)
check("... before anything reaches the server", seen("missing_cfg") == [])

print("\n=== a new key replaces the old one ===")
check("store_key overwrites the fixed id", gegd_auth.store_key(KEY2) is True)
check("exactly one 'gegdpro' config", am.configIds().count(gegd_auth.AUTHCFG_ID) == 1)
blocking(A + "/blk_rotated/x")
check("the next request carries the NEW key, not a cached old one",
      keys(seen("blk_rotated")) == [KEY2])

print("\n=== fetch: retries, waits and honest errors ===")
gegd_net.BACKOFF_S = (0.05, 0.1, 0.2)        # the real 5/10/20 s, shrunk
r = gegd_net.fetch(A + "/json_main", "wfs")
check("fetch returns Reply(status, ctype, body, error)",
      r.status == 200 and r.error is None and r.body.startswith(b'{"type"'))
r = gegd_net.fetch(A + "/flaky1", "wfs")
check("503, 503, then 200: retried to success", r.status == 200 and len(seen("flaky1")) == 3)
t1 = time.time()
r = gegd_net.fetch(A + "/ra_short", "wfs")
check(f"429 with Retry-After: 1 waits it out ({time.time() - t1:.1f} s)",
      r.status == 200 and time.time() - t1 >= 0.9 and len(seen("ra_short")) == 2)


LAST = {}


def fetch_kind(url, service="wfs", **kw):
    """(kind, every text the error carries); the error itself lands in LAST."""
    LAST.clear()
    try:
        gegd_net.fetch(url, service, **kw)
        return None, ""
    except GegdError as e:
        LAST["e"] = e
        return e.kind, " | ".join(str(x) for x in (e, e.user_message, getattr(e, "detail", "")))


k, _m = fetch_kind(A + "/ra_long")
check(f"429 asking for an hour is reported at once, not waited out ({k})",
      k is not None and len(seen("ra_long")) == 1)
k, _m = fetch_kind(A + "/always503", retries=2)
check(f"a server that stays busy: 1 + 2 retries, then an error ({k})",
      k is not None and len(seen("always503")) == 3)
t1 = time.time()
k, m = fetch_kind(A + "/slow", timeout_s=1, retries=1)
check(f"no answer within timeout_s: retried once, then reported ({k}, {time.time() - t1:.1f} s)",
      k == "network" and len(seen("slow")) == 2 and time.time() - t1 < 4.5 and "1 s" in m)
k, _m = fetch_kind(A + "/nojwt/x")
check(f"WFS 401 'No JWT bearer token' reads as key not sent ({k})", k == "key_not_sent")
k, _m = fetch_kind(A + "/ogc")
check(f"an OGC exception inside an HTTP 200 is an error ({k})", k == "ogc_exception")
fb = QgsFeedback(); fb.cancel()
k, _m = fetch_kind(A + "/json_cancel", feedback=fb)
check(f"a cancelled feedback sends nothing ({type(LAST.get('e')).__name__})",
      isinstance(LAST.get("e"), gegd_net.Cancelled) and LAST["e"].cancelled
      and seen("json_cancel") == [])
_dead = ThreadingHTTPServer(("127.0.0.1", 0), H); dead_port = _dead.server_port; _dead.server_close()
t1 = time.time()
k, m = fetch_kind("http://127.0.0.1:%d/x" % dead_port)
check(f"a refused connection fails at once, without retries ({k}, {time.time() - t1:.1f} s)",
      k == "network" and time.time() - t1 < 5 and m.strip() != "")
k, m = fetch_kind(A + "/echo")
check(f"a server echoing the key back: the error text is redacted ({k})",
      k == "query_error" and KEY3 not in m and "<KEY>" in m)
try:
    gegd_net.fetch(A + "/json_badsvc", "nosuch")
    refused = False
except ValueError:
    refused = True
check("fetch refuses an unknown service before anything is sent",
      refused and seen("json_badsvc") == [])

print("\n=== forget the key ===")
check("forget_key() removes it", gegd_auth.forget_key() is True)
check("has_key() and the save time are gone", not gegd_auth.has_key() and gegd_auth.key_set_utc() is None)
blocking(A + "/after_forget/x")
check("a request naming gegdpro now sends nothing at all", seen("after_forget") == [])
k, m = fetch_kind(A + "/fetch_forget")
check(f"fetch says no key is stored ({k})", k == "key_not_sent" and seen("fetch_forget") == [])
check("forget_key() again is harmless", gegd_auth.forget_key() is True)

print("\n=== usage meter ===")
before = gegd_auth.usage_today()
gegd_auth.flush_usage()
day = gegd_auth._today()
stored = int(QgsSettings().value(f"{gegd_auth.S_USAGE}/{day}/requests", 0))
check(f"flush writes today's count to QgsSettings ({stored} requests)",
      stored == before[0] and gegd_auth.usage_today() == before and stored > 0)
QgsSettings().setValue(f"{gegd_auth.S_USAGE}/2000-01-01/requests", 5)
gegd_auth.flush_usage()
check("days older than the keep window are pruned",
      not QgsSettings().contains(f"{gegd_auth.S_USAGE}/2000-01-01/requests"))
blocking(B_LOCAL + "/blk_not_counted/x", authcfg="")
check("requests to other hosts are not counted",
      len(seen("blk_not_counted")) == 1 and gegd_auth.usage_today()[0] == before[0])

print("\n=== fallback: no APIHeader method, the injector adds the key ===")
gegd_auth.store_key(KEY3)
saved_state = dict(gegd_auth._STATE)
gegd_auth._STATE.update(apiheader=False, disabled=False)
real_probe = gegd_auth._probe
gegd_auth._probe = lambda: None
try:
    check("mode() is 'preprocessor' without APIHeader", gegd_auth.mode() == "preprocessor")
    check("ensure_ready() loads the key into the injector",
          gegd_auth.ensure_ready() and gegd_auth._INJECTOR is not None)
    blocking(A + "/inj_allowed/x", authcfg="")
    blocking(B_LOCAL + "/inj_other/x", authcfg="")
    check("injector: the allowed host gets the key, another host does not",
          keys(seen("inj_allowed")) == [KEY3] and keys(seen("inj_other")) == [None])
    r = gegd_net.fetch(A + "/json_inj", "wfs")
    check("fetch in fallback mode (no authcfg) still authenticates",
          r.status == 200 and keys(seen("json_inj")) == [KEY3])
    gegd_auth.forget_key()
    blocking(A + "/inj_forgot/x", authcfg="")
    check("forget_key() drops the injector", gegd_auth._INJECTOR is None
          and keys(seen("inj_forgot")) == [None])
    gegd_auth.store_key(KEY3)
    check("store_key() in fallback mode installs the injector", gegd_auth._INJECTOR is not None)
    gegd_auth.remove_host_guard()
    blocking(A + "/inj_removed/x", authcfg="")
    check("remove_host_guard() drops the injector and its in-memory key too",
          gegd_auth._INJECTOR is None and keys(seen("inj_removed")) == [None])
    gegd_auth.install_host_guard(allowed=ALLOW_A)
    gegd_auth.ensure_ready()
    had = gegd_auth._INJECTOR is not None
    gegd_auth.release_host_guard()
    blocking(A + "/inj_released/x", authcfg="")
    check("release_host_guard() (plugin unload) drops it as well: a reloaded plugin's "
          "Forget key could not reach it", had and gegd_auth._INJECTOR is None
          and keys(seen("inj_released")) == [None])
    gegd_auth.install_host_guard(allowed=ALLOW_A)
    gegd_auth.forget_key()
finally:
    gegd_auth._probe = real_probe
    gegd_auth._STATE.update(saved_state)

print("\n=== install/remove: idempotent, and the guard is what strips ===")
gegd_auth.store_key(KEY3)
g = gegd_auth.host_guard()
s0 = g.stripped
blocking(B_LOCAL + "/one_pass/x")
check("one stripped request = one preprocessor pass", g.stripped - s0 == 1)
gegd_auth.remove_host_guard()
gegd_auth.remove_host_guard()
check("remove_host_guard() twice is harmless", gegd_auth.host_guard() is None and not g.installed)
blocking(B_LOCAL + "/unguarded/x")
check("control: without the guard the auth config DOES send the key off-host",
      keys(seen("unguarded")) == [KEY3])
g = gegd_auth.install_host_guard(allowed=ALLOW_A)
gegd_auth.install_host_guard(allowed=ALLOW_A)
blocking(B_LOCAL + "/reguarded/x")
check("re-installed (twice): stripped again", keys(seen("reguarded")) == [None])
gegd_auth.remove_host_guard()
blocking(B_LOCAL + "/unguarded2/x")
check("one remove undoes two installs (no stacked preprocessor)",
      keys(seen("unguarded2")) == [KEY3])

print("\n=== plugin unload: the strip-only guard outlives the plugin ===")
g = gegd_auth.install_host_guard(allowed=ALLOW_A)
u0 = gegd_auth.usage_today()
gegd_auth.release_host_guard()
check("release_host_guard(): no active guard, counting and its timer stopped",
      gegd_auth.host_guard() is None and not g._connected and g._timer is None)
blocking(B_LOCAL + "/released/x")
check("… but a layer naming gegdpro still cannot send the key off-host",
      keys(seen("released")) == [None])
blocking(A + "/released_ok/x")
check("… the allowed host still gets it, and it is no longer counted",
      keys(seen("released_ok")) == [KEY3] and gegd_auth.usage_today()[0] == u0[0])
gegd_auth.release_host_guard()
check("release twice is harmless", gegd_auth.host_guard() is None)
g2 = gegd_auth.install_host_guard(allowed=ALLOW_A)            # what a plugin reload does
check("a reload installs a fresh guard", g2 is not g and g2.installed)
gegd_auth.remove_host_guard()
blocking(B_LOCAL + "/reload_removed/x")
check("… having replaced the released one: one remove leaves nothing stacked",
      keys(seen("reload_removed")) == [KEY3])

gegd_auth.forget_key()
SRV_A.shutdown(); SRV_B.shutdown()
leftover = profile_files_holding(KEY1, KEY2, KEY3)
check(f"no file in the profile holds any test key ({leftover})", leftover == [])
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
