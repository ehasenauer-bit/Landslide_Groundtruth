"""The two outlines a fit converts are saved in the project's folder, not memory.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_outline_saved.py

"New scar layer" makes a memory layer: close QGIS without Export > Save Features
As… and the afternoon's digitizing is gone, and the project that reopens has
nothing to measure. The Volume tab's "New total area" and "New source area
(best)" buttons write a GeoPackage beside the .qgz instead (dock.results_dir),
name it for its role and assign it to that role in one press.

Checks: the file is really on disk and the layer reads from it; the role is set
and pinned; the name carries the role so a reopened project re-matches it; a
second press never writes over the first outline; and with nowhere to save
(unsaved project, no Results folder) it refuses rather than quietly dropping the
outline in a temp dir.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsProject, QgsVectorLayer, QgsWkbTypes
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject

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
        s.active = None
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return s.active
    def setActiveLayer(s, l): s.active = l


from qgis.core import QgsSettings
_KEYS = ("landslide/python", "landslide/project", "landslide/out")
_saved = {k: QgsSettings().value(k, "", type=str) for k in _KEYS}


def _restore_settings():
    s = QgsSettings()
    for k, v in _saved.items():
        s.setValue(k, v)
    s.sync()


import atexit
atexit.register(_restore_settings)

from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth.volume_tab import ROLE_LAYERS

dock = LandslideDock(FakeIface())
tab = dock.volume_tab
project = QgsProject.instance()
dock.out_edit.setText("")
dock.project_edit.setText("")

print("=== nowhere to save: the button says so instead of using a temp dir ===")
project.clear()
tab.name_edit.setText("test slide")
before = len(project.mapLayers())
tab._new_role_layer("total")
check("an unsaved project with no Results folder creates no layer",
      len(project.mapLayers()) == before)
check("the log says to save the project first",
      "Save this QGIS project first" in tab.log.toPlainText())

print("\n=== New total area: a GeoPackage in the project's folder ===")
A = tempfile.mkdtemp(prefix="proj outline ")     # a space, like the real folders
project.write(os.path.join(A, "event A.qgz"))
tab._new_role_layer("total")
total = tab._role_layer("total")
check("the Total area role is filled in", isinstance(total, QgsVectorLayer))
src = total.source().split("|", 1)[0] if total is not None else ""
check(f"it reads from a file, not memory ({total.providerType() if total else None})",
      total is not None and total.providerType() == "ogr")
check(f"the file is in the project's folder ({src})",
      os.path.isfile(src) and os.path.dirname(src) == A)
check(f"named for the slide and the role ({os.path.basename(src)})",
      os.path.basename(src) == "test_slide_total_area.gpkg")
check("the layer is a polygon layer, with a slide field",
      total is not None
      and total.geometryType() == QgsWkbTypes.PolygonGeometry
      and total.fields().indexOf("slide") >= 0)
check("the role is pinned, so a layer loaded later can't take it over",
      tab._role_pinned.get("total"))
check("it is also the draw target", tab._chosen_layer() is total)
check("and QGIS is digitizing into it", dock.iface.activeLayer() is total
      and total.isEditable())

print("\n=== the NAME carries the role, so a reopened project re-matches it ===")
name = total.name()
check(f"the layer name ends in the role words ({name})",
      name == "test slide Total Area")
check("name matching alone would assign it to total",
      tab._match_role_layers([total]).get("total") is total)

print("\n=== New source area (best): its own file, its own role ===")
tab._new_role_layer("best")
best = tab._role_layer("best")
bsrc = best.source().split("|", 1)[0] if best is not None else ""
check("the Source area (best) role is filled in", isinstance(best, QgsVectorLayer))
check(f"a second, separate GeoPackage ({os.path.basename(bsrc)})",
      os.path.isfile(bsrc) and bsrc != src
      and os.path.basename(bsrc) == "test_slide_source_area_best.gpkg")
check("it did not disturb the total role", tab._role_layer("total") is total)
check("name matching alone would assign it to best",
      tab._match_role_layers([best]).get("best") is best)

print("\n=== a second press never writes over the first outline ===")
stamp = os.path.getmtime(src)
tab._new_role_layer("total")
again = tab._role_layer("total")
check("a new layer takes the role", again is not None and again is not total)
asrc = again.source().split("|", 1)[0]
check(f"written to a different file ({os.path.basename(asrc)})",
      os.path.isfile(asrc) and asrc != src)
check("the first file is untouched", os.path.isfile(src)
      and os.path.getmtime(src) == stamp)
check("both files are still there",
      {os.path.basename(p) for p in (src, asrc, bsrc)}
      <= set(os.listdir(A)))

print("\n=== the files survive the project: reopening finds them ===")
ids = {l.source().split("|", 1)[0] for l in project.mapLayers().values()}
project.write(os.path.join(A, "event A.qgz"))
project.clear()
check("clearing the project does not delete the outlines",
      all(os.path.isfile(p) for p in (src, asrc, bsrc)))
project.read(os.path.join(A, "event A.qgz"))
reloaded = {l.source().split("|", 1)[0] for l in project.mapLayers().values()}
check("and the reopened project still points at them",
      {src, asrc, bsrc} <= reloaded)
tab._refresh_layers()
check("with the roles matched back from the layer names",
      tab._role_layer("total") is not None and tab._role_layer("best") is not None)

print("\n=== every role button knows where it writes ===")
check("ROLE_LAYERS covers exactly the two converted outlines",
      set(ROLE_LAYERS) == {"total", "best"})
check("each entry is (label, layer suffix, file stem, style)",
      all(len(v) == 4 and isinstance(v[3], dict) for v in ROLE_LAYERS.values()))

project.clear()
dock.teardown()
_restore_settings()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
