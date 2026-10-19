"""A saved project with Maxar streams must hold authcfg=gegdpro and never the key.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_project_has_no_key.py

The Planet key reached 22 of 60 shared .qgz files because a layer URL carried it.
The GEGD key must not repeat that: it lives in the QGIS password store as the
APIHeader auth config 'gegdpro', and every Maxar layer source names only
'authcfg=gegdpro'. This saves a project with streamed strips (XYZ and the WMS
fallback), a tab whose key field still holds a pasted-but-unsaved key, and a
cached search, then unzips the .qgz and greps every byte for the DUMMY key. It
also checks the settings file and the auth DB itself, and that the fallback mode
(no APIHeader method) builds sources with no authcfg at all.

Throwaway profile and auth DB; Keychain helper off BEFORE the master password is
set; a synthetic key. The real key is never read, and NOTHING leaves the machine:
the WMS fallback layer (whose provider fetches GetCapabilities the moment it is
built) points at a local stub, and a request preprocessor diverts anything bound
for any other host to a closed local port and fails the test.
"""
import os, re, secrets, shutil, sys, tempfile, threading, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _base in ("/Applications/QGIS-LTR.app", "/Applications/QGIS.app"):
    _proj = os.path.join(_base, "Contents", "Resources", "qgis", "proj")
    if os.path.isdir(_proj):
        os.environ.setdefault("PROJ_DATA", _proj)
        os.environ.setdefault("PROJ_LIB", _proj)
        os.environ.setdefault("GDAL_DATA", os.path.join(_base, "Contents", "Resources", "qgis", "gdal"))
        break
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
sys.path.insert(0, PKG)

TMP = tempfile.mkdtemp(prefix="gegd_nokey_")
for _d in ("auth", "config", "cache", "proj"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")
os.environ["LANDSLIDE_GEGD_CACHE"] = os.path.join(TMP, "cache")

from qgis.core import (QgsApplication, QgsNetworkAccessManager, QgsProject, QgsRasterLayer,
                       QgsSettings)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import QObject, QUrl
from qgis.PyQt.QtWidgets import QMainWindow, QWidget

# ---- offline: nothing may leave this machine, least of all to pro.gegd.com ----
OFFHOST = []                     # (host, carried the key header?) of every diverted request


def _offline(request):
    host = request.url().host().lower()
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        OFFHOST.append((host, request.hasRawHeader(b"maxar-api-key")))
        request.setUrl(QUrl("http://127.0.0.1:9/diverted"))     # closed port: never sent


QgsNetworkAccessManager.setRequestPreprocessor(_offline)

STUB_SEEN = []                   # (path, maxar-api-key header) per request the stub got


class Stub(BaseHTTPRequestHandler):
    """A minimal WMS, so the fallback layer is valid without the network."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        STUB_SEEN.append((self.path, self.headers.get("maxar-api-key")))
        if "getcapabilities" not in self.path.lower():
            body, ctype, code = b"not here", "text/plain", 404
        else:
            base = "http://%s%s" % (self.headers.get("Host"), self.path.split("?")[0])
            body = ('<?xml version="1.0" encoding="UTF-8"?><WMS_Capabilities version="1.3.0" '
                    'xmlns="http://www.opengis.net/wms" xmlns:xlink="http://www.w3.org/1999/xlink">'
                    '<Service><Name>WMS</Name><Title>stub</Title><OnlineResource xlink:href="%s"/>'
                    '</Service><Capability><Request><GetCapabilities><Format>text/xml</Format>'
                    '<DCPType><HTTP><Get><OnlineResource xlink:href="%s"/></Get></HTTP></DCPType>'
                    '</GetCapabilities><GetMap><Format>image/png</Format><DCPType><HTTP><Get>'
                    '<OnlineResource xlink:href="%s"/></Get></HTTP></DCPType></GetMap></Request>'
                    '<Exception><Format>XML</Format></Exception><Layer><Title>root</Title>'
                    '<CRS>EPSG:3857</CRS><Layer queryable="0"><Name>Maxar:Imagery</Name>'
                    '<Title>L</Title><CRS>EPSG:3857</CRS><BoundingBox CRS="EPSG:3857" '
                    'minx="-20037508" miny="-20037508" maxx="20037508" maxy="20037508"/>'
                    '</Layer></Layer></Capability></WMS_Capabilities>'
                    % (base, base, base)).encode()
            ctype, code = "text/xml", 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


STUB = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
STUB.daemon_threads = True
threading.Thread(target=STUB.serve_forever, daemon=True).start()
STUB_HOST = "http://127.0.0.1:%d" % STUB.server_address[1]

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
    sys.exit(2)
am = QgsApplication.authManager()
am.setPasswordHelperEnabled(False)            # BEFORE any master-password call
if am.passwordHelperEnabled() or not os.path.realpath(
        am.authenticationDatabasePath()).startswith(os.path.realpath(TMP)):
    print("auth DB not isolated or Keychain helper still on; refusing to continue")
    sys.exit(2)
if not am.setMasterPassword(secrets.token_urlsafe(24), True):
    print("could not set the throwaway master password")
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


class FakeBar(QObject):
    def pushMessage(s, *a, **k): pass
    def pushInfo(s, *a): pass
    def pushWarning(s, *a): pass
    def pushCritical(s, *a): pass
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s, t): pass
    def unsetMapTool(s, t): pass
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar = FakeBar(); s._c = FakeCanvas(); s._w = QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


from landslide_groundtruth import gegd_api as api, gegd_auth, gegd_layers as gl
from landslide_groundtruth import gegd_rank as R, layer_group as lg
from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth.gegd_tab import strip_feature

KEY = "dummy-gegd-saved-key-" + secrets.token_hex(12)       # stored in the auth DB
PASTED = "dummy-gegd-pasted-key-" + secrets.token_hex(12)   # typed, never saved

print("=== the key goes into the password store only ===")
check("ensure_ready() unlocks the throwaway store", gegd_auth.ensure_ready())
check("store_key() saves the APIHeader config 'gegdpro'",
      gegd_auth.store_key(KEY) and gegd_auth.has_key() and gegd_auth.mode() == "authcfg")

dock = LandslideDock(FakeIface())
tab = dock.gegd_tab
project = QgsProject.instance()
PROJ = os.path.join(TMP, "proj")
project.write(os.path.join(PROJ, "Iliamna.qgz"))      # results_dir() = PROJ from now on
tab.lat_edit.setText("60.0265")
tab.lon_edit.setText("-153.0559")
tab.key_edit.setText(PASTED)                          # pasted, Save key never pressed

print("\n=== a project with streams, the WMS fallback and a cached search ===")
strips = api.parse_features(open(os.path.join(FIX, "wfs_iliamna.json"), "rb").read())
origin = R._utc("2026-08-08T13:00:00Z")
ranked = R.rank(strips, origin, 60.0265, -153.0559, 5.0)
made = tab._stream_strips([ranked.star_pre, ranked.star_post], replace=True)
check(f"two stream layers on the map ({[l.name() for l in made]})",
      len(made) == 2 and all(project.mapLayer(l.id()) for l in made))
# The WMS provider fetches GetCapabilities as soon as the layer is built, so this
# one is aimed at the local stub (its source still names authcfg=gegdpro).
wms = QgsRasterLayer(gl.strip_wms_uri(ranked.star_post.strip.feature_id, host=STUB_HOST),
                     "Maxar after (WMS fallback)", "wms")
lg.add_to_group(wms, "Maxar test")
check("the WMS fallback layer is valid and registered too",
      wms.isValid() and project.mapLayer(wms.id()) is not None)
check(f"… its GetCapabilities went to the local stub, without the key "
      f"(HostGuard: not pro.gegd.com) ({len(STUB_SEEN)} request(s))",
      STUB_SEEN and all(k is None for _p, k in STUB_SEEN))
t0, t1 = R.search_window(origin, 365, 90)
tab._search = dict(event_id="AK2026-0808", lat=60.0265, lon=-153.0559, r_km=5.0,
                   origin=origin, t0=t0, t1=t1, pre_days=365, post_days=90,
                   host=api.HOST_DEFAULT, strips=strips,
                   features=[strip_feature(s) for s in strips],
                   nearest_pre=None, nearest_post=None, canary_hits=None,
                   searched_utc=origin, product_clause="not_in", restored=False)
tab._rerank_now(reset_picks=True)
sj = tab._save_search()
check("search.json written beside the project", sj and os.path.isfile(sj))

QGZ = os.path.join(PROJ, "Iliamna.qgz")
check("project saved", project.write(QGZ))

print("\n=== nothing key-shaped in the saved project ===")
with zipfile.ZipFile(QGZ) as z:
    members = {n: z.read(n) for n in z.namelist()}
blob = b"".join(members.values())
qgs = b"".join(v for n, v in members.items() if n.endswith(".qgs"))
check(f"unzipped .qgz ({', '.join(members)}): the stored key appears 0 times",
      KEY.encode() not in blob)
check("… the pasted-but-unsaved key appears 0 times (key_edit is never persisted)",
      PASTED.encode() not in blob)
check("… 'authcfg=gegdpro' is what the stream sources carry",
      blob.count(b"authcfg=gegdpro") >= 3)
check("… and no header or query form of a key anywhere",
      b"maxar-api-key" not in blob and b"maxar_api_key" not in blob)
check("the tab's own fields were saved (so the absence above is not an empty project)",
      b"lat_edit" in qgs and b"60.0265" in qgs and b"gegd" in qgs)
# Project entries are XML elements named after the widget (<gegd><lat_edit …>).
# (det_id_edit, the detection's event id, is a legitimate dock entry.)
check("no project entry is named after a key field (key_edit, _id_edit)",
      not re.search(rb"<(key_edit|_id_edit)\b", qgs))
for l in made + [wms]:
    src = l.source()
    check(f"source of '{l.name()}' holds authcfg=gegdpro and no key",
          "authcfg=gegdpro" in src and KEY not in src and PASTED not in src)

print("\n=== nor anywhere else on disk this test wrote ===")
s = open(sj, "rb").read()
check("search.json: no key", KEY.encode() not in s and PASTED.encode() not in s)
QgsSettings().sync()
ini = open(QgsSettings().fileName(), "rb").read()
check("the settings file: no key (only the time it was saved)",
      KEY.encode() not in ini and PASTED.encode() not in ini
      and b"gegd_key_set_utc" in ini)
db = open(am.authenticationDatabasePath(), "rb").read()
check("the auth DB holds the key encrypted, not in clear", KEY.encode() not in db)

print("\n=== fallback mode (no APIHeader method): sources carry no authcfg ===")
_real_mode = gegd_auth.mode
gegd_auth.mode = lambda: "preprocessor"
try:
    xyz = gl.strip_xyz_uri(ranked.star_pre.strip.feature_id)
    w = gl.strip_wms_uri(ranked.star_pre.strip.feature_id)
finally:
    gegd_auth.mode = _real_mode
check("XYZ and WMS sources without authcfg (a missing config would block the "
      "injector) and without a key", "authcfg" not in xyz and "authcfg" not in w
      and KEY not in xyz and KEY not in w)

print("\n=== offline throughout ===")
check(f"not one request left this machine ({OFFHOST})", OFFHOST == [])

dock.teardown()
project.clear()
gegd_auth.forget_key()
STUB.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
