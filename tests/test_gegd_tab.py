"""The Maxar VHR (GEGD) tab: built into the dock, tied to the event, offline.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_tab.py

Everything runs in a throwaway QGIS profile and auth DB (Keychain helper off
BEFORE the master password is set), under TZ=America/Anchorage so a time-zone
slip shows, against a local mini-WFS that answers from the SYNTHETIC fixtures in
tests/fixtures/gegd/ with a DUMMY key. The real GEGD key is never read.

What it pins:
  * the dock has seven tabs, Maxar VHR (GEGD) right after PlanetScope;
  * building the tab (and its deferred restore) never calls authManager();
  * the persisted widget set, and its defaults;
  * 'Use it in every tab' fills the tab, and the origin is the detection's (with
    seconds) when it matches, else the field's UTC wall clock — never .toUTC();
  * Search -> ★ 08-01 WV01 -> 08-26 WV02 on the Iliamna-shaped fixture, footprints
    and search circle on the map, the ★ pair streamed (after on top, authcfg only);
  * filter changes re-rank with ZERO requests; Flip toggles the after strip;
  * Stream picks replaces only the previous streams; double-click adds a strip as
    the shown before/after without breaking Flip (after stays on top), and never
    twice; the search-area outlines stay drawn above the strips;
  * Test names the newest Vantor optical strip (never third-party SAR or Vivid);
  * a key test superseded by Forget key never reports "Key works";
  * Save and Forget pressed together: only the newer one runs;
  * a failing hint request does not cost the search;
  * the honest empty states (archive floor, Knik's missing pre side);
  * a failed search keeps the table;
  * search.json beside the project restores the table with no request, and
    reopening the project hands the stream layers back;
  * _out_dir() follows results_dir(); no gegd_*.py reads the Environment output field;
  * teardown cancels a search in flight and leaves the HostGuard installed.
"""
import io, json, os, re, secrets, shutil, sys, tempfile, threading, time, tokenize
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

os.environ["TZ"] = "America/Anchorage"
time.tzset()
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
PLUG = os.path.join(PKG, "landslide_groundtruth")
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
sys.path.insert(0, PKG)

# A throwaway profile and auth DB, set BEFORE QgsApplication exists.
TMP = tempfile.mkdtemp(prefix="gegd_tab_")
for _d in ("auth", "config", "cache", "tools"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")
os.environ["LANDSLIDE_GEGD_CACHE"] = os.path.join(TMP, "cache")

from qgis.core import QgsApplication, QgsLayerTreeGroup, QgsProject, QgsSettings, QgsVectorLayer
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)       # the APIHeader auth method lives here
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import QDate, QDateTime, QObject, QTime, Qt
from qgis.PyQt.QtWidgets import QLineEdit, QMainWindow, QTabWidget, QWidget

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
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


def wait_until(pred, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        QgsApplication.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    QgsApplication.processEvents()
    return pred()


def pump(seconds=0.2):
    t0 = time.time()
    while time.time() - t0 < seconds:
        QgsApplication.processEvents()
        time.sleep(0.01)


# ---------------------------------------------------------------- mini-WFS
SEEN = []                      # one dict per request the server received
LK = threading.Lock()
MODE = {"name": "iliamna", "delay": 0.0, "fail_nearest": False}
FIXTURE = {"iliamna": "wfs_iliamna.json", "knik": "wfs_knik.json",
           "peters": "wfs_empty_petersdome.json"}
_DOCS = {k: json.load(open(os.path.join(FIX, v), encoding="utf-8")) for k, v in FIXTURE.items()}
CANARY = open(os.path.join(FIX, "wfs_hits_canary.xml"), "rb").read()
ERR401 = open(os.path.join(FIX, "err_401_key_not_sent.txt"), "rb").read()


def _ts(s):
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(\+00:00)?$", s)
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def mini_wfs(doc, q):
    """GetFeature over a fixture: the CQL time window, sortBy and count honoured."""
    cql = q.get("cql_filter", [""])[0]
    feats = list(doc["features"])
    ge = re.search(r"acquisitionDate>='([^']+)'", cql)
    lt = re.search(r"acquisitionDate<'([^']+)'", cql)
    acq = lambda f: _ts(f["properties"]["acquisitionDate"])
    if ge:
        feats = [f for f in feats if acq(f) >= _ts(ge.group(1))]
    if lt:
        feats = [f for f in feats if acq(f) < _ts(lt.group(1))]
    asc = "acquisitionDate A" in q.get("sortBy", [""])[0]
    feats.sort(key=acq, reverse=not asc)
    feats = feats[:int(q.get("count", ["5000"])[0])]
    out = dict(doc, features=feats, numberReturned=len(feats))
    return json.dumps(out).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        with LK:
            SEEN.append({"path": u.path, "q": q, "key": self.headers.get("maxar-api-key")})
        if MODE["delay"]:
            time.sleep(MODE["delay"])
        mode = MODE["name"]
        if mode == "401":
            return self._send(401, "text/plain", ERR401)
        if q.get("resultType") == ["hits"]:
            return self._send(200, "text/xml", CANARY)
        if MODE["fail_nearest"] and q.get("count") == ["10"]:     # the nearest-outside query
            return self._send(400, "text/plain", b"nope")
        if q.get("request", [""])[0] == "GetFeature":
            return self._send(200, "application/json", mini_wfs(_DOCS[mode], q))
        return self._send(404, "text/plain", b"not here")

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
SRV.daemon_threads = True
threading.Thread(target=SRV.serve_forever, daemon=True).start()
HOST = f"http://127.0.0.1:{SRV.server_address[1]}"


def n_requests():
    with LK:
        return len(SEEN)


# ---------------------------------------------------------------- the dock
class FakeBar(QObject):
    def __init__(s): super().__init__(); s.msgs = []
    def pushMessage(s, *a, **k): s.msgs.append(a)
    def pushInfo(s, t, m): s.msgs.append(("info", t, m))
    def pushWarning(s, t, m): s.msgs.append(("warn", t, m))
    def pushCritical(s, t, m): s.msgs.append(("crit", t, m))
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s, t): pass
    def unsetMapTool(s, t): pass
    def setExtent(s, r): s.extent_set = r
    def refresh(s): pass
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar = FakeBar(); s._c = FakeCanvas(); s._w = QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


print("=== building the dock never opens the password store ===")
from landslide_groundtruth import gegd_auth, gegd_layers, gegd_net, gegd_rank as R
from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth.gegd_tab import GegdTab, strip_feature, _parse_feature_list
from landslide_groundtruth import gegd_api as api

AM_CALLS = []
_real_am, _real_gam = QgsApplication.authManager, gegd_auth._am


def _no_am(*_a, **_k):
    AM_CALLS.append(1)
    raise AssertionError("authManager() called while building the tab")


QgsApplication.authManager = staticmethod(_no_am)
gegd_auth._am = _no_am
try:
    iface = FakeIface()
    dock = LandslideDock(iface)
    pump(0.3)                       # the deferred project_state + tab restore run here
finally:
    QgsApplication.authManager = _real_am
    gegd_auth._am = _real_gam
check(f"LandslideDock + GegdTab + deferred restore: authManager() calls = {len(AM_CALLS)}",
      not AM_CALLS)
tab = dock.gegd_tab
check("the tab is a GegdTab", isinstance(tab, GegdTab))
tabs = dock.findChild(QTabWidget)
names = [tabs.tabText(i) for i in range(tabs.count())]
check(f"7 tabs, Maxar VHR (GEGD) right after PlanetScope ({names})",
      len(names) == 7 and names.index("Maxar VHR (GEGD)") == names.index("PlanetScope") + 1)

# now the password store may open: isolated, helper off BEFORE the master password
am = QgsApplication.authManager()
am.setPasswordHelperEnabled(False)
if am.passwordHelperEnabled() or not os.path.realpath(
        am.authenticationDatabasePath()).startswith(os.path.realpath(TMP)):
    print("auth DB not isolated or Keychain helper still on; refusing to continue")
    sys.exit(2)
if not am.setMasterPassword(secrets.token_urlsafe(24), True):
    print("could not set the throwaway master password")
    sys.exit(2)
DUMMY = "dummy-gegd-tab-key-" + secrets.token_hex(12)

print("\n=== what the project file keeps ===")
EXPECT = {
    "gegd/lat_edit", "gegd/lon_edit", "gegd/radius_spin", "gegd/dt_edit",
    "gegd/pre_days_spin", "gegd/post_days_spin", "gegd/ona_max_spin",
    "gegd/show_visual_check", "gegd/sun_min_spin", "gegd/cloud_max_spin",
    "gegd/sensor_checks/GE01", "gegd/sensor_checks/WV01", "gegd/sensor_checks/WV02",
    "gegd/sensor_checks/WV03", "gegd/sensor_checks/other", "gegd/third_party_check",
    "gegd/auto_stream_check", "gegd/show_outlines_check",
}
keys = {k for k, _w in dock.project_state._widgets if k.startswith("gegd/")}
check(f"persisted widget set is exactly the pinned one (extra {sorted(keys - EXPECT)}, "
      f"missing {sorted(EXPECT - keys)})", keys == EXPECT)
check("key_edit is a password-echo field and never persisted",
      tab.key_edit.echoMode() == QLineEdit.PasswordEchoOnEdit
      and not any("key" in k for k in keys))
d = {k: w for k, w in dock.project_state._widgets if k.startswith("gegd/")}
defaults = (d["gegd/radius_spin"].value(), d["gegd/pre_days_spin"].value(),
            d["gegd/post_days_spin"].value(), d["gegd/ona_max_spin"].value(),
            d["gegd/show_visual_check"].isChecked(), d["gegd/sun_min_spin"].value(),
            d["gegd/cloud_max_spin"].value(),
            all(cb.isChecked() for cb in tab.sensor_checks.values()),
            d["gegd/third_party_check"].isChecked(), d["gegd/auto_stream_check"].isChecked(),
            d["gegd/show_outlines_check"].isChecked())
check(f"defaults 5 km / 365 d / 90 d / 30° / visual on / 15° / 100% (no strip-cloud cap) / "
      f"all sensors / no third-party / auto-stream / outlines shown ({defaults})",
      defaults == (5.0, 365, 90, 30.0, True, 15.0, 100, True, False, True, True))

print("\n=== results go beside the project ===")
# The unsaved-project fallback is <tools folder>/out/interactive (dock.shared_dir);
# aim it at the throwaway folder so nothing lands in the real tools folder.
TOOLS = os.path.join(TMP, "tools")
dock.project_edit.setText(TOOLS)
project = QgsProject.instance()
project.clear(); pump()
check("unsaved project: _out_dir() = <shared fallback>/gegd",
      tab._out_dir() == os.path.join(TOOLS, "out", "interactive", "gegd")
      and tab._out_dir() == os.path.join(dock.shared_dir(), "gegd"))
PROJ = os.path.join(TMP, "event Iliamna")          # a space, like the real folders
os.makedirs(PROJ)
project.write(os.path.join(PROJ, "Iliamna.qgz"))
check("saved project: _out_dir() = <project folder>/gegd, under results_dir()",
      tab._out_dir() == os.path.join(PROJ, "gegd")
      and tab._out_dir().startswith(dock.results_dir()))
bad = [f for f in sorted(os.listdir(PLUG)) if f.startswith("gegd_") and f.endswith(".py")
       and "out_" + "edit" in open(os.path.join(PLUG, f), encoding="utf-8").read()]
check(f"no gegd_*.py reads the Environment output field (offenders: {bad})", not bad)

print("\n=== 'Use it in every tab' fills the tab; origin time handling ===")
dock.det_id_edit.setText("AK2026-0808")
# What a user typing 13:00:42 into the UTC field produces: the wall clock, LocalTime spec.
dock.det_time_edit.setDateTime(QDateTime(QDate(2026, 8, 8), QTime(13, 0, 42)))
dock.det_lat_edit.setText("60.0265")
dock.det_lon_edit.setText("-153.0559")
dock.det_err_spin.setValue(17.0)
dock._apply_detection()
check(f"lat/lon/radius filled ({tab.lat_edit.text()}, {tab.lon_edit.text()}, "
      f"{tab.radius_spin.value()} km from the 17 km location error)",
      tab.lat_edit.text() == "60.026500" and tab.lon_edit.text() == "-153.055900"
      and tab.radius_spin.value() == 17.0)
origin, det = tab._origin_utc()
check(f"matching detection: origin keeps its seconds, as aware UTC ({origin.isoformat()})",
      det is not None and origin == datetime(2026, 8, 8, 13, 0, 42, tzinfo=timezone.utc))
check(f"event id is the detection's ({tab._event_id()})", tab._event_id() == "AK2026-0808")
lbl = tab._event_label.text()
check("event line names the record and spells out Alaska time",
      "AK2026-0808" in lbl and "AKDT" in lbl and "±17 km" in lbl)
tab.lat_edit.setText("61.0")                   # no longer the detection's place
origin2, det2 = tab._origin_utc()
check(f"unmatched: 'No detection record matches', origin = field wall clock as UTC "
      f"({origin2.isoformat()})",
      det2 is None and "No detection record matches" in tab._event_label.text()
      and origin2 == datetime(2026, 8, 8, 13, 0, 42, tzinfo=timezone.utc))
check("… and NOT the .toUTC() reading (which would add Anchorage's 8 h)",
      tab.dt_edit.dateTime().toUTC().toPyDateTime().replace(tzinfo=timezone.utc) != origin2)
check(f"unmatched event id follows run_single's convention ({tab._event_id()})",
      tab._event_id() == "event_260808_1300")
toks = [t.string for t in tokenize.generate_tokens(io.StringIO(
    open(os.path.join(PLUG, "gegd_tab.py"), encoding="utf-8").read()).readline)
    if t.type == tokenize.NAME]
check("gegd_tab.py never calls .toUTC() (code, not comments)", "toUTC" not in toks)
tab.lat_edit.setText("60.026500")
tab.radius_spin.setValue(5.0)
tab.dt_edit.setDateTime(QDateTime(QDate(2026, 8, 8), QTime(13, 0, 0)))  # the fixture's origin
dock.det_time_edit.setDateTime(QDateTime(QDate(2026, 8, 8), QTime(13, 0, 0)))
check("detection still matches after the edit", tab._matching_detection() is not None)
dock.lat_edit.setText("59.1"); dock.lon_edit.setText("-150.2")
dock.radius_spin.setValue(3.0)
dock.dt_edit.setDateTime(QDateTime(QDate(2025, 1, 2), QTime(3, 4)))
tab.pre_days_spin.setValue(200)
saved = (tab.lat_edit.text(), tab.lon_edit.text(), tab.radius_spin.value(),
         tab.dt_edit.dateTime(), tab.pre_days_spin.value())
tab._copy_from_main()
check("Copy from Sentinel-2 copies location, radius and the UTC wall clock, not the windows",
      (tab.lat_edit.text(), tab.lon_edit.text(), tab.radius_spin.value(),
       tab.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm"), tab.pre_days_spin.value())
      == ("59.1", "-150.2", 3.0, "2025-01-02 03:04", 200))
tab.lat_edit.setText(saved[0]); tab.lon_edit.setText(saved[1])
tab.radius_spin.setValue(saved[2]); tab.dt_edit.setDateTime(saved[3])
tab.pre_days_spin.setValue(365)

print("\n=== Search with no key: a clear message, nothing sent ===")
gegd_auth.forget_key()
tab._host = HOST
n0 = n_requests()
tab._search_clicked()
pump(0.3)
check("no key -> 'No … GEGD Pro key is stored' and no request",
      "key is stored" in tab._summary_label.text() and n_requests() == n0)

print("\n=== Save key, Search: the ★ pair, footprints, streams ===")
tab.key_edit.setText(DUMMY)
tab._save_key()
check("Save key stores it and clears the field", gegd_auth.has_key() and tab.key_edit.text() == "")
ok = wait_until(lambda: tab.test_key_btn.isEnabled() and "Key works" in tab._key_status.text())
check(f"Test: 'Key works · …' ({tab._key_status.text()[:100]})", ok)
# What the status must name: the fixture's newest Vantor optical strip inside the
# key test's window (computed, so the check does not go stale as 'now' moves on).
from landslide_groundtruth.gegd_tasks import KeyTestTask, vantor_optical  # noqa: E402
_win = datetime.now(timezone.utc).timestamp() - KeyTestTask().days * 86400
_inwin = [s for s in api.parse_features(json.dumps(_DOCS["iliamna"]).encode())
          if s.acq_utc.timestamp() >= _win]
_own = sorted((s for s in _inwin if vantor_optical(s)), key=lambda s: s.acq_utc)
_want = ((f"newest Vantor optical strip over Alaska {_own[-1].acq_utc:%Y-%m-%d %H:%M}Z "
          f"({_own[-1].sensor})") if _own
         else "no Vantor optical strip" if _inwin else "no Alaska strip")
check(f"… naming the newest Vantor optical strip, never the third-party SAR or Vivid "
      f"record ({_want})", _want in tab._key_status.text())
n0 = n_requests()
tab._search_clicked()
ok = wait_until(lambda: not tab._busy_on and tab._ranked is not None)
check("the search finished", ok)
rk = tab._ranked
sp, so = rk.star_pre, rk.star_post
check(f"★ before = 2026-08-01 WV01 ONA 29.6 LV3D ({sp and sp.strip.label})",
      sp is not None and sp.strip.label == "2026-08-01 WV01" and sp.strip.ona == 29.6)
check(f"★ after = 2026-08-26 WV02, not the lower-ONA 08-31 ({so and so.strip.label})",
      so is not None and so.strip.label == "2026-08-26 WV02")
check(f"one request for a two-sided result ({n_requests() - n0})", n_requests() - n0 == 1)
check("the key never reached the local server (HostGuard: not pro.gegd.com)",
      all(r["key"] is None for r in SEEN))
labels = [r.strip.label for r in tab._row_strips]
check(f"08-31 and the 09-22 stereo pair are listed; no Vivid ({len(labels)} rows)",
      "2026-08-31 WV02" in labels and labels.count("2026-09-22 WV02") == 2
      and not any("MULTIPLE" in l for l in labels))
starred = [i for i in range(tab._table.rowCount())
           if tab._table.item(i, 0).text().startswith("★")]
check(f"table: 7 columns; exactly the two ★ rows, both ticked ({starred})",
      tab._table.columnCount() == 7 and len(starred) == 2
      and all(tab._table.item(i, 0).checkState() == Qt.Checked for i in starred)
      and sum(tab._table.item(i, 0).checkState() == Qt.Checked
              for i in range(tab._table.rowCount())) == 2)
stereo_rows = [i for i, r in enumerate(tab._row_strips) if r.stereo]
check("stereo mates carry a badge", stereo_rows and all(
    "stereo" in tab._table.item(i, 2).text() for i in stereo_rows))
tip = tab._table.item(0, 0).toolTip()
check("row tooltip: Alaska time, the 50 m shift line, featureId",
      "AKDT" in tip and "50 m" in tip and "featureId" in tip)
fp, aoi = tab._footprint_layer, tab._aoi_layer
check("footprints and search circle are on the map (memory layers, tagged)",
      fp is not None and aoi is not None and fp.featureCount() == len(tab._row_strips)
      and gegd_layers.get_prop(fp, "kind") == "footprints"
      and gegd_layers.get_prop(aoi, "kind") == "aoi"
      and project.mapLayer(fp.id()) is not None)
streams = list(tab._stream_layers)
names = [l.name() for l in streams]
check(f"the ★ pair streamed ({names})",
      names == ["Maxar before · 2026-08-01 WV01 · ONA 30° (stream)",
                "Maxar after · 2026-08-26 WV02 · ONA 20° (stream)"])
check("stream names carry no _pre_/_post_ token (3D never pairs them)",
      not any(re.search(r"_(pre|post)_", n) for n in names))
srcs = [l.source() for l in streams]
check("stream sources: XYZ over WMTS with CQL_FILTER, authcfg=gegdpro, no key",
      all("type=xyz" in s and "CQL_FILTER" in s and "authcfg=gegdpro" in s
          and DUMMY not in s for s in srcs))
root = project.layerTreeRoot()


def root_groups():
    return [c.name() for c in root.children() if isinstance(c, QgsLayerTreeGroup)]


def outlines_on_top():
    """The search-area folder (outline-only footprints and circle) sits above the
    stream folder, so the opaque strips never hide them."""
    g = root_groups()
    sa = [i for i, n in enumerate(g) if n.endswith("search area")]
    st = [i for i, n in enumerate(g) if n.endswith("stream")]
    return len(sa) == 1 and len(st) == 1 and sa[0] < st[0]


def top_visible_stream():
    for n in root.findLayers():
        if n.isVisible() and gegd_layers.get_prop(n.layer(), "kind") == "stream":
            return n.layerId()
    return None


order = [n.layerId() for n in root.findLayers()]
b, a = tab._compare
check("after is above before in the layer tree, both visible",
      order.index(a.id()) < order.index(b.id())
      and root.findLayer(a.id()).itemVisibilityChecked()
      and root.findLayer(b.id()).itemVisibilityChecked())
check(f"the search-area folder is above the stream folder ({root_groups()})", outlines_on_top())
check("rights metadata names Vantor and AEC internal use",
      "Vantor" in " ".join(a.metadata().rights()) and "AEC internal" in " ".join(a.metadata().rights()))
SJ = os.path.join(PROJ, "gegd", "AK2026-0808", "search.json")
check("search.json written beside the project", os.path.isfile(SJ))
sj = open(SJ, encoding="utf-8").read() if os.path.isfile(SJ) else ""
check("search.json holds no key", sj and DUMMY not in sj)

print("\n=== filters re-rank with ZERO requests ===")
calls = []
_real_fetch = gegd_net.fetch
gegd_net.fetch = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("fetch"))
n0 = n_requests()
tab.ona_max_spin.setValue(25.0)
tab._rerank_now()
sp2 = tab._ranked.star_pre
check(f"ONA 30 -> 25: 08-01 WV01 (29.6°) drops to visual; ★ before is now "
      f"{sp2 and sp2.strip.label}", sp2 is None or sp2.strip.label != "2026-08-01 WV01")
wv01 = [r for r in tab._ranked.pre if r.strip.label == "2026-08-01 WV01"]
check("… listed as visual, greyed", wv01 and wv01[0].grade == "visual")
tab.ona_max_spin.setValue(30.0)
tab.sun_min_spin.setValue(45.0)
tab._rerank_now()
check(f"sun ≥45°: most strips hidden, 'Show hidden' offered ({tab._ranked.n_hidden} hidden)",
      tab._ranked.n_hidden > 0 and not tab._show_hidden_btn.isHidden())
tab._show_hidden_btn.setChecked(True)
check("Show hidden lists them (greyed) with no request",
      len(tab._row_strips) == len(tab._ranked.pre) + len(tab._ranked.post))
tab._show_hidden_btn.setChecked(False)
tab.sun_min_spin.setValue(15.0)
pump(0.5)                                   # let the debounce timer fire too
check(f"no request and no fetch() for any of it ({n_requests() - n0}, {len(calls)})",
      n_requests() == n0 and not calls)
gegd_net.fetch = _real_fetch
check("back to ★ 08-01 → 08-26", tab._ranked.star_pre.strip.label == "2026-08-01 WV01"
      and tab._ranked.star_post.strip.label == "2026-08-26 WV02")

print("\n=== Flip ===")
b, a = tab._compare
node_a = root.findLayer(a.id())
tab._flip()
check("Flip hides the after strip", not node_a.itemVisibilityChecked())
tab._flip()
check("Flip again shows it", node_a.itemVisibilityChecked())

print("\n=== Stream picks replaces only the previous streams ===")
other = QgsVectorLayer("Point?crs=EPSG:4326", "another tab's layer", "memory")
project.addMapLayer(other)
old_ids = {l.id() for l in tab._stream_layers}
row31 = next(i for i, r in enumerate(tab._row_strips) if r.strip.label == "2026-08-31 WV02")
tab._table.item(row31, 0).setCheckState(Qt.Checked)
tab._stream_picks()
new = tab._stream_layers
check(f"three streams now ({[l.name() for l in new]})", len(new) == 3)
check("the previous stream layers are gone, the other tab's layer is not",
      not any(project.mapLayer(i) for i in old_ids) and project.mapLayer(other.id()) is not None)
extra = [l for l in new if "08-31" in l.name()]
check("the extra pick is added switched off (fetches nothing)",
      extra and not root.findLayer(extra[0].id()).itemVisibilityChecked())
check("the footprints survived the stream swap", project.mapLayer(tab._footprint_layer.id()) is not None)
check(f"… and stay drawn above the new strips (folders: {root_groups()})", outlines_on_top())

print("\n=== double-click adds a strip without breaking Flip ===")
old_after = tab._compare[1]
row22 = next(i for i, r in enumerate(tab._row_strips) if r.strip.label == "2026-09-22 WV02")
tab._on_double_click(tab._table.item(row22, 1))
check("double-click adds one stream without replacing", len(tab._stream_layers) == 4)
b, a = tab._compare
check("… it is the shown after-strip now; the one it took over from is kept, switched off",
      "2026-09-22" in a.name() and project.mapLayer(old_after.id()) is not None
      and not root.findLayer(old_after.id()).itemVisibilityChecked()
      and top_visible_stream() == a.id())
streamed = {gegd_layers.get_prop(l, "fid") for l in tab._stream_layers}
rowb = next(i for i, r in enumerate(tab._row_strips) if r.side == "pre" and r.grade == "mapping"
            and r.hidden_reason is None and r.strip.feature_id not in streamed)
label_b = tab._row_strips[rowb].strip.label
tab._on_double_click(tab._table.item(rowb, 1))
b, a = tab._compare
ids = [n.layerId() for n in root.findLayers()]
check(f"double-click on a BEFORE row ({label_b}): it is the shown before-strip, directly "
      "below the after-strip (after stays on top)",
      label_b in b.name() and ids.index(a.id()) + 1 == ids.index(b.id())
      and top_visible_stream() == a.id() and len(tab._stream_layers) == 5)
tab._flip()
check("Flip still works: hiding the after-strip reveals the new before-strip",
      top_visible_stream() == b.id())
tab._flip()
check("… and Flip again puts the after-strip back on top", top_visible_stream() == a.id())
row31 = next(i for i, r in enumerate(tab._row_strips) if r.strip.label == "2026-08-31 WV02")
tab._on_double_click(tab._table.item(row31, 1))      # streamed already, switched off
b2, a2 = tab._compare
ids = [n.layerId() for n in root.findLayers()]
check("double-click on a strip already streamed: no duplicate, it moves to the top and shows",
      len(tab._stream_layers) == 5 and "2026-08-31" in a2.name() and b2 is b
      and top_visible_stream() == a2.id() and ids.index(a2.id()) < ids.index(b2.id()))
tab._flip()
check("… and Flip reveals the before-strip under it", top_visible_stream() == b2.id())
tab._flip()
tab._on_double_click(tab._table.item(rowb, 1))
check("double-clicking the shown before-strip again changes nothing",
      len(tab._stream_layers) == 5 and tab._compare == (b2, a2))
check(f"the search-area folder is still above the stream folder ({root_groups()})",
      outlines_on_top())

print("\n=== table and footprints select each other ===")
tab._table.selectRow(row31)
sel = [f["feature_id"] for f in tab._footprint_layer.selectedFeatures()]
check("selecting a row selects its footprint",
      sel == [tab._row_strips[row31].strip.feature_id])
check("… and fills the detail line", "2026-08-31" in tab._detail_label.text())
fid22 = tab._row_strips[row22].strip.feature_id
tab._footprint_layer.selectByIds([tab._fp_ids[fid22]])
rows = sorted({i.row() for i in tab._table.selectionModel().selectedRows()})
check("selecting a footprint selects its row", rows == [row22])

print("\n=== the selection highlight, Clear selection, hiding the outlines ===")
try:
    from qgis.core import Qgis
    sp = tab._footprint_layer.selectionProperties()
    check("a selected footprint is drawn as an outline, not QGIS's yellow fill",
          sp.selectionRenderingMode() == Qgis.SelectionRenderingMode.CustomSymbol
          and sp.selectionSymbol() is not None
          and sp.selectionSymbol().symbolLayer(0).brushStyle() == Qt.NoBrush)
except AttributeError:                  # QGIS < 3.34: no per-layer selection symbol
    print("  (skipped: no per-layer selection symbol in this QGIS)")
tab._clear_selection()
check("Clear selection empties both the table and the map selection",
      not tab._table.selectionModel().selectedRows()
      and tab._footprint_layer.selectedFeatureCount() == 0
      and not tab._detail_label.text())
fp_node = project.layerTreeRoot().findLayer(tab._footprint_layer.id())
aoi_node = project.layerTreeRoot().findLayer(tab._aoi_layer.id())
tab._table.selectRow(row31)
n0 = n_requests()
tab.show_outlines_check.setChecked(False)
check("unticking hides the footprints and the search circle, and clears the selection",
      not fp_node.itemVisibilityChecked() and not aoi_node.itemVisibilityChecked()
      and tab._footprint_layer.selectedFeatureCount() == 0
      and not tab._table.selectionModel().selectedRows())
tab._rerank_now()                       # rebuilds the footprint layer
fp_node = project.layerTreeRoot().findLayer(tab._footprint_layer.id())
check("a rebuilt footprint layer stays hidden while the box is unticked",
      not fp_node.itemVisibilityChecked())
tab.show_outlines_check.setChecked(True)
aoi_node = project.layerTreeRoot().findLayer(tab._aoi_layer.id())
check("ticking shows both again", fp_node.itemVisibilityChecked()
      and aoi_node.itemVisibilityChecked())
check("hiding and showing outlines costs no requests", n_requests() == n0)

print("\n=== a failed search keeps the table ===")
before_rows = [r.strip.feature_id for r in tab._row_strips]
MODE["name"] = "401"
tab._search_clicked()
wait_until(lambda: not tab._busy_on)
check(f"401 -> the key message ({tab._summary_label.text()[:80]}…)",
      "valid key" in tab._summary_label.text())
check("… and the previous rows are still there",
      [r.strip.feature_id for r in tab._row_strips] == before_rows)
MODE["name"] = "iliamna"

print("\n=== a key test that is superseded never reports ===")
MODE["delay"] = 1.5
tab._test_key()
check("Cancel is offered while a key test runs, Test is not",
      tab.cancel_btn.isEnabled() and not tab.test_key_btn.isEnabled())
tab._forget_key()
pump(2.5)
st = tab._key_status.text()
check(f"Forget key during a test: the old test cannot print 'Key works' ({st[:50]}…)",
      "Key removed" in st and "Key works" not in st)
check("… and the buttons are back", tab.test_key_btn.isEnabled() and not tab.cancel_btn.isEnabled())
# Removing 'gegdpro' while a worker loads it can deadlock QGIS's auth locks, so
# Forget key cancels the test and removes the key only once it has wound down.
check("… the cancelled test wound down before the key was removed (nothing left running)",
      not tab._tasks)
MODE["delay"] = 0.0
tab.key_edit.setText(DUMMY)
tab._save_key()
check("Save key again: tested, works",
      wait_until(lambda: "Key works" in tab._key_status.text() and tab.test_key_btn.isEnabled()))

print("\n=== Save and Forget pressed together: the newer one wins ===")
# Both wait for the test in flight to wind down. Run in turn, the Save used to
# start a key test the Forget then waited on (and after 10 s removed the key under
# it), and a Forget used to clear the field a following Save reads. The test must
# be RUNNING when they are pressed: a still-queued task ends inside cancel(), and
# then nothing waits at all.
MODE["delay"] = 1.5
n0 = n_requests()
tab._test_key()
wait_until(lambda: n_requests() > n0, 5)
tab.key_edit.setText(DUMMY)
tab._save_key()
t0 = time.time()
tab._forget_key()
ok = wait_until(lambda: "Key removed" in tab._key_status.text() and not tab._tasks, 5)
check(f"Save then Forget: only the Forget runs, without a key test to wait on "
      f"({time.time() - t0:.1f} s)", ok and not gegd_auth.has_key() and time.time() - t0 < 1.0)
MODE["delay"] = 0.0
tab.key_edit.setText(DUMMY)
tab._save_key()
wait_until(lambda: "Key works" in tab._key_status.text() and not tab._tasks)
MODE["delay"] = 1.5
n0 = n_requests()
tab._test_key()
wait_until(lambda: n_requests() > n0, 5)
tab._set_key_status("info", "(marker: the earlier success line is gone)")
tab._forget_key()
tab.key_edit.setText(DUMMY)
tab._save_key()
ok = wait_until(lambda: "Key works" in tab._key_status.text() and not tab._tasks, 8)
check("Forget then Save: the Save runs and the key is stored and works",
      ok and gegd_auth.has_key() and tab.key_edit.text() == "")
MODE["delay"] = 0.0

print("\n=== which Vivid-exclusion form is remembered ===")
from landslide_groundtruth.gegd_tab import S_PRODUCT_CLAUSE  # noqa: E402
QgsSettings().setValue(S_PRODUCT_CLAUSE, "none")
check("a remembered 'none' is never a starting point (Vivid would download every time)",
      tab._product_clause() == "not_in")
tab._remember_clause("neq")
check("'neq' (the <> chain) is remembered once a search got through with it",
      tab._product_clause() == "neq")
tab._remember_clause("none")
check("a search that had to fall back to 'none' clears it: NOT IN is tried again next time",
      not QgsSettings().contains(S_PRODUCT_CLAUSE) and tab._product_clause() == "not_in")

print("\n=== search.json restores the table with no request ===")
tab._picks = {tab._ranked.star_pre.strip.feature_id, tab._ranked.star_post.strip.feature_id}
tab._save_search()
n_live = len(tab._stream_layers)
# What the dock does when it opens onto a project: forget, then adopt + restore.
tab._on_project_cleared()
check("cleared: no table, no streams held", tab._ranked is None
      and tab._table.rowCount() == 0 and not tab._stream_layers)
n0 = n_requests()
tab._restore_from_project()
check("restored from search.json", tab._search is not None and tab._search.get("restored"))
check(f"the stream layers already on the map are adopted ({len(tab._stream_layers)}/{n_live})",
      len(tab._stream_layers) == n_live and None not in tab._compare)
check("same ★ pair, same rows, zero requests",
      tab._ranked.star_pre.strip.label == "2026-08-01 WV01"
      and tab._ranked.star_post.strip.label == "2026-08-26 WV02"
      and [r.strip.feature_id for r in tab._row_strips] == before_rows
      and n_requests() == n0)
check("badged 'From the last search …'", "From the last search" in tab._summary_label.text())

print("\n=== reopen the project: streams adopted, table restored ===")
project.write(os.path.join(PROJ, "Iliamna.qgz"))
n_streams = len(tab._stream_layers)
pair_names = tuple(l.name() for l in tab._compare)
project.clear(); pump()
project.read(os.path.join(PROJ, "Iliamna.qgz"))
ok = wait_until(lambda: tab._ranked is not None and len(tab._stream_layers) == n_streams, 10)
check(f"stream layers handed back by custom property ({len(tab._stream_layers)}/{n_streams})", ok)
check(f"the compare pair is the one on screen before the reopen ({pair_names})",
      None not in tab._compare and tuple(l.name() for l in tab._compare) == pair_names)
check("the table came back from search.json",
      [r.strip.feature_id for r in tab._row_strips] == before_rows)
import zipfile
with zipfile.ZipFile(os.path.join(PROJ, "Iliamna.qgz")) as z:
    blob = b"".join(z.read(n) for n in z.namelist())
check("the saved project carries authcfg=gegdpro and not the key (more in "
      "test_gegd_project_has_no_key)", DUMMY.encode() not in blob and b"authcfg=gegdpro" in blob)

print("\n=== honest empty states ===")
MODE["name"] = "peters"
tab.lat_edit.setText("63.07"); tab.lon_edit.setText("-151.0")
tab.dt_edit.setDateTime(QDateTime(QDate(2023, 9, 13), QTime(12, 0)))
check("the archive note warns before searching", not tab._archive_note.isHidden()
      and "predates" in tab._archive_note.text())
n0 = n_requests()
tab._search_clicked()
wait_until(lambda: not tab._busy_on)
txt = tab._summary_label.text()
check(f"Peters Dome 2023: the archive-floor message, one request ({n_requests() - n0})",
      "archive starts around 2024-07-20" in txt and n_requests() - n0 == 1)
check("… and an empty table", tab._table.rowCount() == 0)
MODE["name"] = "knik"
tab.lat_edit.setText("61.15"); tab.lon_edit.setText("-148.25")
tab.dt_edit.setDateTime(QDateTime(QDate(2026, 8, 9), QTime(9, 30)))
tab.pre_days_spin.setValue(60)
n0 = n_requests()
tab._search_clicked()
wait_until(lambda: not tab._busy_on)
txt = tab._summary_label.text()
labels = [r.strip.label for r in tab._row_strips]
check(f"Knik: the 09-02 post is listed and ★ ({labels})",
      "2026-09-02 WV03" in labels and tab._ranked.star_post is not None
      and tab._ranked.star_post.strip.label == "2026-09-02 WV03")
check("… and the pre side says where the nearest earlier strip is (one extra request)",
      "nearest earlier strip 2026-05-02" in txt and n_requests() - n0 == 2)
MODE["fail_nearest"] = True
tab._log.clear()
tab._search_clicked()
wait_until(lambda: not tab._busy_on)
txt = tab._summary_label.text()
labels = [r.strip.label for r in tab._row_strips]
check("the nearest-strip request failing does not cost the search: same table, plain "
      "pre-side message, the failure logged",
      "2026-09-02 WV03" in labels and "No before-event VHR in 60 d" in txt
      and "nearest earlier" not in txt and "Search failed" not in tab._log.toPlainText()
      and "follow-up request" in tab._log.toPlainText())
MODE["fail_nearest"] = False
tab.pre_days_spin.setValue(365)
tab._search_clicked()
wait_until(lambda: not tab._busy_on)
check("Knik with a year: no mapping-grade before-strip, said so, nothing starred before",
      tab._ranked.star_pre is None and "No mapping-grade before-strip"
      in tab._summary_label.text())
MODE["name"] = "iliamna"

print("\n=== badges ===")
ref = tab._ranked.star_post.strip
ing = api.Strip(feature_id="00000000-0000-4000-8000-0000000000ee", acq_utc=ref.acq_utc,
                sensor="WV02", source="WV02", product_name="PRIVATE_FEATURE",
                sensor_type="Optical", ona=15.0, processing_level="LV3D", rings=ref.rings)
r_ing = R.rank([ing], R._utc("2026-08-08T13:00:00Z"), 61.15, -148.25, 5.0).post[0]
items = tab._row_items(r_ing)
check("a strip ingested on request carries 'ingested' in the Sensor column and its tooltip",
      "ingested" in items[2].text() and "Ingested on request" in items[2].toolTip())

print("\n=== teardown cancels a search in flight ===")
tab.lat_edit.setText("60.0265"); tab.lon_edit.setText("-153.0559")
tab.dt_edit.setDateTime(QDateTime(QDate(2026, 8, 8), QTime(13, 0)))
MODE["delay"] = 4.0
tab._search_clicked()
inflight = list(tab._tasks)
pump(0.3)
t0 = time.time()
dock.teardown()
check("a task was in flight", len(inflight) == 1)
check("teardown cancelled it", inflight and inflight[0].isCanceled())
done = wait_until(lambda: inflight[0].status() in (inflight[0].Complete, inflight[0].Terminated), 10)
check(f"… and it stopped well before the server answered ({time.time() - t0:.1f} s)",
      done and time.time() - t0 < 3.5)
pump(0.3)
check("teardown left the HostGuard installed (it belongs to the plugin)",
      gegd_auth.host_guard() is not None)
MODE["delay"] = 0.0

project.clear()
SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
