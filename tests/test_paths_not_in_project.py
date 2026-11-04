"""A saved project must not carry the Setup box's paths.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_paths_not_in_project.py

Before 1.0 the own-Python, tools-folder and results-folder fields were saved into
every .qgz and won on open. A project shared with a colleague then overwrote
their paths with the sender's — paths that don't exist on their machine, and
that carry the sender's user name. They now live only in QgsSettings: never
written, ignored when an older project still holds them, and scrubbed from it on
the next save.
"""
import os, sys, tempfile, zipfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsProject, QgsSettings
QgsApplication.setPrefixPath(os.environ.get(
    "QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
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
    def pushSuccess(s, *a): pass
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


saved = {k: QgsSettings().value(f"landslide/{k}", "") for k in ("python", "project", "out")}
from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth.project_state import SCOPE, GLOBAL_PATH_ATTRS

dock = LandslideDock(FakeIface())
project = QgsProject.instance()
D = tempfile.mkdtemp(prefix="paths ")
MINE = "/Users/someone/landslide_groundtruth/venv/bin/python3"


def project_xml(path):
    with zipfile.ZipFile(path) as z:
        name = [n for n in z.namelist() if n.endswith(".qgs")][0]
        return z.read(name).decode("utf-8", "replace")


print("=== saving: the paths stay out of the .qgz ===")
dock.python_edit.setText(MINE)
dock.out_edit.setText(os.path.join(D, "results"))
dock.det_lat_edit.setText("60.0265")              # an ordinary input, for contrast
check("typing a path saves it to QgsSettings at once",
      QgsSettings().value("landslide/python") == MINE)
qgz = os.path.join(D, "shared.qgz")
dock.project_state.save()
project.write(qgz)
xml = project_xml(qgz)
check("the own-Python path is not in the project file", MINE not in xml)
check("nor the results folder", os.path.join(D, "results") not in xml)
check("an ordinary input still is (the plugin's state IS saved)", "60.0265" in xml)

print("\n=== opening a project from before 1.0 that still carries paths ===")
project.clear()
project.writeEntry(SCOPE, "python_edit", "/Users/sender/venv/bin/python3")
project.writeEntry(SCOPE, "project_edit", "/Users/sender/landslide_groundtruth")
old = os.path.join(D, "old.qgz")
# written the way 0.x wrote it: without today's save hook, which would scrub them
project.writeProject.disconnect(dock.project_state.save)
project.write(old)
project.writeProject.connect(dock.project_state.save)
check("(the old project really holds the sender's path)",
      "/Users/sender/venv" in project_xml(old))
project.read(old)
dock.project_state._restore_now()
check("the recipient's own Python is kept", dock.python_edit.text() == MINE)
check("the sender's tools folder is not applied",
      dock.project_edit.text() != "/Users/sender/landslide_groundtruth")
dock.project_state.save()
project.write(old)
check("saving scrubs the stale paths from the project",
      "/Users/sender" not in project_xml(old))
check("every Setup path field is excluded",
      GLOBAL_PATH_ATTRS == {"python_edit", "project_edit", "out_edit"})

for k, v in saved.items():
    QgsSettings().setValue(f"landslide/{k}", v)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
