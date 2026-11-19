"""Planet tile previews are clipped to the search box, and stay clipped.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_preview_clip.py

A Planet preview is an XYZ layer, whose extent is the whole world, so QGIS asked
Planet for every tile in view: the whole ~32 x 20 km scene, and more whenever the
view went past it. The tab now sets each preview's extent to its search box (in the
layer's own CRS, EPSG:3857), as the Maxar tab does for its streams. This builds the
previews with the tab's real _add_tile_layer against a local tile server and counts
the tiles QGIS asks for: none beside the box, few around it, the same inside it.
QGIS does not save a layer extent, and QGIS 4's reload() resets it, so the box is
stored on the layer and put back on project read and on canvas renderStarting.
No network: anything not bound for this machine is diverted and recorded.
"""
import os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
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
sys.path.insert(0, PKG)

TMP = tempfile.mkdtemp(prefix="planet_clip_")
for _d in ("auth", "config"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsMapRendererParallelJob, QgsMapSettings, QgsNetworkAccessManager,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings)
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
# That prefix resolves the provider plugins to …/MacOS/Contents/PlugIns (absent), which
# leaves only gdal; the tile preview is an XYZ layer and needs the real 'wms' provider.
# The bundle under test first (QGIS_PREFIX, set by run_all.sh QGIS_APP=...).
for _plug in ([os.path.join(os.environ["QGIS_PREFIX"], "..", "PlugIns", "qgis")]
              if os.environ.get("QGIS_PREFIX") else []) + [
        "/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
        "/Applications/QGIS.app/Contents/PlugIns/qgis"]:
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import (QBuffer, QByteArray, QEventLoop, QIODevice, QObject, QSize,
                              QTimer, QUrl, pyqtSignal)
from qgis.PyQt.QtGui import QColor, QImage
from qgis.PyQt.QtWidgets import QMainWindow, QWidget

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
    sys.exit(2)
if not QgsCoordinateReferenceSystem("EPSG:3857").isValid():
    print("EPSG:3857 does not resolve (no proj.db: set PROJ_DATA); the renders would ask "
          "for no tiles and pass for the wrong reason")
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


# ---- offline: anything not bound for this machine is diverted and recorded ----
OFFHOST = []


def _offline(request):
    host = request.url().host().lower()
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        OFFHOST.append(host)
        request.setUrl(QUrl("http://127.0.0.1:9/diverted"))


QgsNetworkAccessManager.setRequestPreprocessor(_offline)

# ---- a local stand-in for tiles{0-3}.planet.com ----
_img = QImage(256, 256, QImage.Format.Format_ARGB32); _img.fill(QColor(200, 120, 40))
_ba = QByteArray(); _buf = QBuffer(_ba); _buf.open(QIODevice.OpenModeFlag.WriteOnly); _img.save(_buf, "PNG")
PNG = bytes(_ba)
SEEN = []
LK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        with LK:
            SEEN.append(urlparse(self.path).path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)


SRV = ThreadingHTTPServer(("127.0.0.1", 0), H)
SRV.daemon_threads = True
threading.Thread(target=SRV.serve_forever, daemon=True).start()
PORT = SRV.server_address[1]


def take():
    """The tile paths requested since the last call (after the server threads settle)."""
    lp = QEventLoop(); QTimer.singleShot(300, lp.quit); lp.exec()
    with LK:
        got = list(SEEN)
        SEEN.clear()
    return got


def render(layer, rect4326, size=256):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    dst = QgsCoordinateReferenceSystem("EPSG:3857")
    ms.setDestinationCrs(dst)
    ms.setExtent(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), dst,
                                        QgsProject.instance()).transformBoundingBox(rect4326))
    ms.setBackgroundColor(QColor(0, 0, 0, 0))   # transparent: alpha 0 = nothing drawn
    ms.setOutputSize(QSize(size, size))
    job = QgsMapRendererParallelJob(ms)
    lp = QEventLoop(); job.finished.connect(lp.quit)
    job.start()
    QTimer.singleShot(30000, lp.quit)
    lp.exec()
    return job.renderedImage()


# ---- the dock, with a canvas that can emit renderStarting ----
class FakeBar(QObject):
    def pushMessage(s, *a, **k): pass
    def pushInfo(s, *a): pass
    def pushWarning(s, *a): pass
    def pushCritical(s, *a): pass


class FakeCanvas(QWidget):
    renderStarting = pyqtSignal()

    def mapSettings(s):
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


from landslide_groundtruth import planet_tab as pt  # noqa: E402
from landslide_groundtruth.dock import LandslideDock  # noqa: E402

# Tiles from the local server; the template keeps Planet's own path shape.
pt.TILE_XYZ_URL = f"http://127.0.0.1:{PORT}/data/v1/layers/{{hash}}/{{{{z}}}}/{{{{x}}}}/{{{{y}}}}"
iface = FakeIface()
dock = LandslideDock(iface)
tab = dock.planet_tab
tab._preview_group = "Planet test preview"

LAT, LON, R_KM = 60.0265, -153.0559, 5.0        # Iliamna, a 5 km search
tab._search_result = {"lat": LAT, "lon": LON, "params": {"radius_km": R_KM},
                      "pre": [], "post": []}
W3857 = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                               QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
bbox = tab._aoi_bbox()
BOX_3857 = W3857.transformBoundingBox(QgsRectangle(*bbox))


def preview(name, tile_hash):
    tab._add_tile_layer(name, tile_hash)
    return tab._preview_layers[-1]


def same(a, b, tol=0.01):
    return all(abs(x - y) < tol for x, y in (
        (a.xMinimum(), b.xMinimum()), (a.yMinimum(), b.yMinimum()),
        (a.xMaximum(), b.xMaximum()), (a.yMaximum(), b.yMaximum())))


print("=== a preview is clipped to the search box ===")
# Separate hashes per layer: QGIS caches tiles by URL, and a shared cache would hide
# the requests being counted.
clip_l = preview("PlanetScope after 2026-08-26", "clipA")
check("the preview is a valid XYZ layer with no key in its source",
      clip_l.isValid() and "type=xyz" in clip_l.source() and "api_key" not in clip_l.source())
check(f"its extent is the search box in EPSG:3857 ({BOX_3857.width() / 1000:.1f} km wide; "
      f"{2 * R_KM:.0f} km on the ground at 60°N)",
      same(clip_l.extent(), BOX_3857) and 1.5e4 < BOX_3857.width() < 2.5e4)
check("... and the box is stored on the layer", same(tab._stored_clip(clip_l), BOX_3857))
world_l = QgsRasterLayer(clip_l.source().replace("clipA", "worldB"), "unclipped", "wms")
check("an unclipped twin spans the whole world (the XYZ default)",
      world_l.isValid() and world_l.extent().width() > 4e7)

AWAY = QgsRectangle(LON + 0.5, LAT - 0.1, LON + 0.7, LAT + 0.1)    # ~28 km east
WIDE = QgsRectangle(LON - 0.5, LAT - 0.25, LON + 0.5, LAT + 0.25)  # ~5x the box
INSIDE = QgsRectangle(LON - 0.01, LAT - 0.005, LON + 0.01, LAT + 0.005)
take()
render(world_l, AWAY, 1024)
n_world_away = len(take())
img = render(clip_l, AWAY, 1024)
n_clip_away = len(take())
check(f"a view beside the box: {n_clip_away} tiles clipped vs {n_world_away} unclipped",
      n_clip_away == 0 and n_world_away > 0 and img.pixelColor(512, 512).alpha() == 0)
render(world_l, WIDE, 1024)
n_world_wide = len(take())
img = render(clip_l, WIDE, 1024)
n_clip_wide = len(take())
check(f"a view 5x the box: {n_clip_wide} tiles clipped vs {n_world_wide} unclipped, "
      "drawn in the middle only",
      0 < n_clip_wide < n_world_wide / 2 and img.pixelColor(512, 512).alpha() == 255
      and img.pixelColor(20, 20).alpha() == 0)
render(world_l, INSIDE)
n_world_in = len(take())
img = render(clip_l, INSIDE)
n_clip_in = len(take())
check(f"a view inside the box: drawn edge to edge, same tiles ({n_clip_in} vs {n_world_in})",
      img.pixelColor(2, 2).alpha() == 255 and img.pixelColor(253, 253).alpha() == 255
      and n_clip_in == n_world_in > 0)
# Where the box edge falls inside the view, the drawing stops at it, not at a tile edge.
EDGE = QgsRectangle(bbox[2] - 0.02, LAT - 0.01, bbox[2] + 0.02, LAT + 0.01)
img = render(clip_l, EDGE, 400)
take()
check("the drawing stops at the box edge, not at a tile edge",
      img.pixelColor(150, 200).alpha() == 255 and img.pixelColor(250, 200).alpha() == 0)

# Why the box is transformed: the same box left in lon/lat degrees is a few metres
# at (0, 0) in EPSG:3857, and the preview draws nothing over the event.
deg_l = QgsRasterLayer(clip_l.source().replace("clipA", "degC"), "degrees", "wms")
deg_l.setExtent(QgsRectangle(*bbox))
img = render(deg_l, INSIDE)
take()
check("(a box in degrees instead draws nothing over the event)",
      img.pixelColor(128, 128).alpha() == 0)

print("\n=== Zoom to scene frames what is drawn: the footprint cut to the box ===")
tab._preview_extent = None
tab._grow_preview_extent([LON - 0.5, LAT - 0.3, LON + 0.02, LAT + 0.02])  # SW corner
z = tab._preview_extent
check(f"frame = footprint ∩ box ({z.toString(3)})",
      z is not None and abs(z.xMinimum() - bbox[0]) < 1e-9 and abs(z.yMinimum() - bbox[1]) < 1e-9
      and abs(z.xMaximum() - (LON + 0.02)) < 1e-9 and abs(z.yMaximum() - (LAT + 0.02)) < 1e-9)

print("\n=== no search AOI: left unclipped, as before ===")
saved = tab._search_result
tab._search_result, tab._last_render = None, None
bare = preview("PlanetScope before 2026-08-01", "bareD")
check("whole-world extent, nothing stored",
      bare.extent().width() > 4e7 and tab._stored_clip(bare) is None)
tab._search_result = saved

print("\n=== the box comes back after a reopen, a reload() and on renderStarting ===")
prj = QgsProject.instance()
check("the clipped preview is in the project", prj.mapLayer(clip_l.id()) is not None)
path = os.path.join(TMP, "clip.qgz")
prj.write(path)
prj.clear()
prj.read(path)          # readProject -> tab._reclip_previews
back = next((l for l in prj.mapLayers().values() if l.name() == "PlanetScope after 2026-08-26"),
            None)
check("the box is saved on the layer (custom property)",
      back is not None and same(tab._stored_clip(back), BOX_3857))
check("... and it is clipped again on project read, with no call from the test",
      back is not None and same(back.extent(), BOX_3857))
again = next((l for l in prj.mapLayers().values() if l.name() == "PlanetScope before 2026-08-01"),
             None)
check("the unclipped preview stays unclipped after the reopen",
      again is not None and again.extent().width() > 4e7)
back.reload()
back.setExtent(QgsRectangle(-2e7, -2e7, 2e7, 2e7))   # what QGIS 4's reload() leaves
iface.mapCanvas().renderStarting.emit()
check("renderStarting puts the box back after a reset to the whole world",
      same(back.extent(), BOX_3857))
check("... and has nothing to do when the box is already there",
      not tab._reapply_clip(back))
tab.teardown()
back.setExtent(QgsRectangle(-2e7, -2e7, 2e7, 2e7))
iface.mapCanvas().renderStarting.emit()
check("teardown disconnects it (an unloaded plugin re-clips nothing)",
      back.extent().width() > 3.9e7)
prj.clear()

check(f"no request left this machine ({sorted(set(OFFHOST)) or 'none'})", not OFFHOST)
SRV.shutdown()
print(f"\n{OK} ok, {FAIL} failed")
sys.exit(1 if FAIL else 0)
