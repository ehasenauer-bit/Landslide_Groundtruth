"""A saved project must never hold the Planet API key.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_key_not_saved.py

Until 2026-10-13 the tile preview was an XYZ layer whose url ended in ?api_key=<KEY>,
and QGIS saves every layer source into the project: 22 of the 61 projects on the
shared drive held the key in plain text. Planet doesn't need it there. The tile hash
is the credential, and tiles came back byte-identical with no key, a bogus key and
the real one, so the url no longer carries it. Only the POST that creates the hash
sends the key, at request time.

Drives the real Preview on map chain (_render_picks -> _request_tile_layer -> the
hash reply -> _add_tile_layer) with only the network faked, saves the project as a
.qgz and searches every member for the key: raw, and as an HTTP Basic token. The
same search must flag an old-style keyed layer, so it cannot pass by finding nothing.
Then strip_planet_keys.py cleans such a project, and QGIS opens the result.
The key is synthetic; nothing here touches the network or a real key.
"""
import base64, contextlib, io, json, os, re, shutil, sys, tempfile, zipfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
# That prefix resolves the provider plugins to …/MacOS/Contents/PlugIns (absent), which
# leaves only gdal; the tile preview is an XYZ layer and needs the real 'wms' provider.
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject, QByteArray, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

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
    def setExtent(s, *a): pass
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


# ---- the network: one fake tile-hash POST, answered by hand ----
class FakeReply(QObject):
    finished = pyqtSignal()
    body = b""
    def attribute(s, attr):
        return 201 if attr == QNetworkRequest.HttpStatusCodeAttribute else None
    def error(s): return QNetworkReply.NoError
    def readAll(s): return QByteArray(s.body)
    def abort(s): pass


posts = []                                   # [(QNetworkRequest, FakeReply)]
class FakeNAM:
    @staticmethod
    def instance(): return FakeNAM
    @staticmethod
    def post(req, body):
        reply = FakeReply()
        posts.append((req, reply))
        return reply


KEY = "PLAK" + "5ca1ab1e0ddba11c0ffee0dd5eedbed5"     # synthetic, Planet-shaped
BASIC = base64.b64encode(f"{KEY}:".encode())
HASH = "SyntheticTileHash_not-a-real-scene0000"   # synthetic: a real hash streams its scene
TMP = tempfile.mkdtemp(prefix="planet_key_")


def keyed_members(path, key=KEY):
    """Members of a saved .qgz that still hold `key`, raw or as a Basic token."""
    token = base64.b64encode(f"{key}:".encode())
    with zipfile.ZipFile(path) as z:
        return [i.filename for i in z.infolist()
                if key.encode() in z.read(i) or token in z.read(i)]


def qgs_text(path):
    with zipfile.ZipFile(path) as z:
        return z.read([n for n in z.namelist() if n.endswith(".qgs")][0]).decode()


def save(path):
    check(f"QGIS wrote {os.path.basename(path)}", QgsProject.instance().write(path))
    return path


print("\n=== Preview on map: the real chain, network faked ===")
from landslide_groundtruth import planet_tab as pt
from landslide_groundtruth.dock import LandslideDock
pt.QgsNetworkAccessManager = FakeNAM
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
tab._api_key = lambda: KEY
tab.key_edit.setText(KEY)          # also in the widget, in case it is ever persisted
tab._render_picks([("PlanetScope after 2026-08-26", ["20260826_211715_89_2534"])])
check("Preview on map sent one tile-hash POST", len(posts) == 1)
req, reply = posts[-1]
sent_url = req.url().toString()
sent_auth = bytes(req.rawHeader(b"Authorization"))
check("... and it still carries the key, at request time",
      KEY in sent_url or sent_auth == b"Basic " + BASIC)
reply.body = json.dumps({"name": HASH, "bounds": [-141.2, 60.1, -140.6, 60.4]}).encode()
reply.finished.emit()
check("the hash reply added one tile layer", len(tab._preview_layers) == 1)
lyr = tab._preview_layers[-1] if tab._preview_layers else None
src = lyr.source() if lyr else ""
check("the layer is a valid XYZ layer on the wms provider",
      lyr is not None and lyr.isValid() and lyr.providerType() == "wms")
check(f"it streams the scene by hash: {src[:90]}…",
      re.search(r"url=https://tiles[0-3]\.planet\.com/data/v1/layers/"
                + re.escape(HASH) + r"/\{z\}/\{x\}/\{y\}$", src) is not None)
check("its source holds no key", KEY not in src and "api_key" not in src)

print("\n=== save the project: the key must be nowhere in it ===")
new_qgz = save(os.path.join(TMP, "preview.qgz"))
check("the saved project holds the preview layer", HASH in qgs_text(new_qgz))
check(f"no member of the .qgz holds the key ({keyed_members(new_qgz)})",
      keyed_members(new_qgz) == [])
check("no api_key on any planet.com url in the .qgs",
      not re.search(r"planet\.com[^\"<]*api_key", qgs_text(new_qgz), re.I))

print("\n=== the same search catches an old-style keyed layer ===")
QgsProject.instance().clear()
old_src = ("type=xyz&crs=EPSG:3857&format=&url="
           + pt.TILE_XYZ_URL.format(shard=0, hash=HASH) + "?api_key=" + KEY)
OTHER = "0a1b2c3d-e207-4b4a-a72c-27fdd3d08629"          # another vendor's key shape
other_src = ("type=xyz&crs=EPSG:3857&format=&url=https://tiles.stadiamaps.com/"
             "tiles/stamen_terrain/{z}/{x}/{y}.png?api_key=" + OTHER)
for s, n in ((old_src, "PlanetScope before 2024-09-13"), (other_src, "Stamen Terrain")):
    QgsProject.instance().addMapLayer(QgsRasterLayer(s, n, "wms"))
old_qgz = save(os.path.join(TMP, "old_style.qgz"))
check(f"the keyed layer IS found ({keyed_members(old_qgz)})",
      keyed_members(old_qgz) == [os.path.basename(old_qgz)[:-4] + ".qgs"])
QgsProject.instance().clear()

print("\n=== strip_planet_keys.py on that project ===")
import strip_planet_keys as spk
original = open(old_qgz, "rb").read()


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = spk.main(list(args))
    return rc, out.getvalue()


rc, out = run(old_qgz)
check("a dry run exits 0 and reports the file", rc == 0 and "1 hold a Planet key" in out)
check("... and changes nothing", open(old_qgz, "rb").read() == original)
bak = os.path.join(TMP, "backups")
rc, out = run(old_qgz, "--apply", "--backup-dir", bak)
check("--apply exits 0", rc == 0)
check("nothing the script printed holds the key",
      KEY not in out and BASIC.decode() not in out and OTHER not in out)
backup = os.path.join(bak, os.path.basename(old_qgz))
check("the original was backed up byte-for-byte first",
      os.path.exists(backup) and open(backup, "rb").read() == original)
check("the backup is private (0600, folder 0700)",
      os.path.exists(backup) and os.stat(backup).st_mode & 0o777 == 0o600
      and os.stat(bak).st_mode & 0o777 == 0o700)
check(f"the rewritten project holds no Planet key ({keyed_members(old_qgz)})",
      keyed_members(old_qgz) == [])
check("the other vendor's key is left alone (its basemap needs it)",
      keyed_members(old_qgz, OTHER) != [])
QgsProject.instance().read(old_qgz)
by_name = {l.name(): l for l in QgsProject.instance().mapLayers().values()}
planet = by_name.get("PlanetScope before 2024-09-13")
check("QGIS opens it and the Planet layer is valid", planet is not None and planet.isValid())
shard = lambda s: re.sub(r"tiles[0-3]\.planet", "tiles.planet", s)
check("... in exactly the form the plugin now writes",
      planet is not None and shard(planet.source()) == shard(src))
check("... and the other layer is untouched",
      "Stamen Terrain" in by_name and by_name["Stamen Terrain"].source() == other_src)
QgsProject.instance().clear()
cleaned = open(old_qgz, "rb").read()
rc, out = run(old_qgz, "--apply", "--backup-dir", os.path.join(TMP, "backups2"))
check("a second --apply finds nothing to do and changes nothing",
      rc == 0 and "0 hold a Planet key" in out and open(old_qgz, "rb").read() == cleaned
      and not os.path.exists(os.path.join(TMP, "backups2")))

dock.teardown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
