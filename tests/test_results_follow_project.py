"""Every tab must save its results in the open project's folder, beside the .qgz.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_results_follow_project.py

Results used to go to one global folder (<tools>/out/interactive, 12 GB by
2026-09-25), so a project's files were mixed in with every other project's and
did not travel with its .qgz. Now dock.results_dir() is the saved project's own
folder, and each tab keeps its subfolder under it (planet/, sar/, fusion/,
viewer3d/, and the Sentinel run at the top). Switching projects switches the
folder. Two things deliberately stay shared (dock.shared_dir): an unsaved
project's results, which have no folder yet, and the RGI glacier archive (the
same 67 MB for every project, and it needs an Earthdata login to fetch).
Checks the real --out each Run would pass to run_single.
"""
import os, re, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsProject
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
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


print("=== nothing but the dock and project_state reads the output field ===")
readers = [f for f in sorted(os.listdir(PLUG)) if f.endswith(".py")
           and f not in ("dock.py", "project_state.py")
           and "out_edit" in open(os.path.join(PLUG, f)).read()]
check(f"every tab goes through dock.results_dir() (readers of out_edit: {readers})",
      not readers)

from qgis.core import QgsSettings
# dock._collect() saves the Environment paths to QgsSettings; the other tests share
# that store and expect a blank first run, so put back whatever was there.
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
dock = LandslideDock(FakeIface())
project = QgsProject.instance()
dock.python_edit.setText(sys.executable)       # any existing file will do
dock.project_edit.setText(ROOT)                # holds run_single.py
dock.out_edit.setText("")
SHARED = os.path.join(ROOT, "out", "interactive")
for tab in (dock, dock.planet_tab, dock.sar_tab):
    tab.lat_edit.setText("60.26")
    tab.lon_edit.setText("-140.817")


def outs():
    """(label, where it would write) for every tab."""
    rows = [("Sentinel-2 run", dock._collect()[3]),
            ("PlanetScope search", dock.planet_tab._collect()[3]),
            ("SAR search", dock.sar_tab._collect()[3]),
            ("SAR amplitude", dock.sar_tab._amp_out_dir()),
            ("SAR change", dock.sar_tab._cd_out_dir()),
            ("Fusion", dock.fusion_tab._out_dir()),
            ("3D flip cache", dock.viewer3d_tab._flip_cache_dir())]
    web = getattr(dock.viewer3d_tab, "_web_out_dir", None)   # newer 3D tab only
    if web is not None:
        rows.append(("3D web export", web()))
    return rows


def all_under(base):
    rows = outs()
    bad = [(k, v) for k, v in rows if not (v and os.path.normpath(v).startswith(
        os.path.normpath(base)))]
    return rows, bad


print("\n=== an unsaved project: results go to the shared fallback ===")
project.clear()
rows, bad = all_under(SHARED)
check(f"results_dir() is <tools>/out/interactive ({dock.results_dir()})",
      dock.results_dir() == SHARED)
check(f"all {len(rows)} tabs write under it (off: {bad})", not bad)
check("the Environment box says the project isn't saved yet",
      "isn't saved yet" in dock.results_label.text())

print("\n=== save it: every tab now writes beside the .qgz ===")
A = tempfile.mkdtemp(prefix="proj A ")          # a space, like the real folders
project.write(os.path.join(A, "event A.qgz"))
rows, bad = all_under(A)
check(f"results_dir() is the project's folder", dock.results_dir() == A)
check(f"all {len(rows)} tabs write under it (off: {bad})", not bad)
check("with the same subfolders as before: planet/, sar/, fusion/, viewer3d/",
      dict(rows)["PlanetScope search"] == os.path.join(A, "planet")
      and dict(rows)["SAR change"] == os.path.join(A, "sar", "change")
      and dict(rows)["Fusion"] == os.path.join(A, "fusion")
      and dict(rows).get("3D web export", os.path.join(A, "viewer3d", "web"))
      == os.path.join(A, "viewer3d", "web"))
check("the Environment box names this project's folder",
      A in dock.results_label.text() and "this project's folder" in dock.results_label.text())
check("the RGI glacier archive stays shared, not per project",
      dock.fusion_tab._glacier_cache_dir() == os.path.join(SHARED, "glacier_outlines"))

print("\n=== switching projects switches the folder ===")
B = tempfile.mkdtemp(prefix="proj B ")
project.write(os.path.join(B, "event B.qgz"))          # Save As
check("Save As moves results to the new folder", dock.results_dir() == B
      and B in dock.results_label.text())
project.read(os.path.join(A, "event A.qgz"))          # open the other one
rows, bad = all_under(A)
check(f"opening project A brings every tab back to A's folder (off: {bad})",
      dock.results_dir() == A and not bad)
project.clear()                                       # File > New
check("a new project falls back to the shared folder again",
      dock.results_dir() == SHARED and "isn't saved yet" in dock.results_label.text())

print("\n=== the fallback field still works for unsaved projects ===")
custom = tempfile.mkdtemp(prefix="unsaved results ")
dock.out_edit.setText(custom)
check("an unsaved project uses 'Results (unsaved projects)' when it's set",
      dock.results_dir() == custom and custom in dock.results_label.text())
project.write(os.path.join(A, "event A.qgz"))
check("... but a saved project still keeps its own folder", dock.results_dir() == A)

project.clear()
dock.teardown()
_restore_settings()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
