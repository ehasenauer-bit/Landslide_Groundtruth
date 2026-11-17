"""Draw centerline saves the line as a shapefile in the project's shp_files folder.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_centerline_saved.py

The centerline used to go into a memory layer, lost with any QGIS restart. It is
now shp_files/landslide_centerlines.shp beside the .qgz — one file holding every
slide's line, like the outlines it is spined from.

Checks: the line is in the FILE the moment it is drawn (read back through a
separate layer), with its slide, length and method; the layer is left in edit
mode for trimming, with nothing unsaved; a trim's new length waits for the trim's
own Save, while numbers for an untouched line go straight to disk; drawing the
next slide never saves a trim behind the user's back; a shapefile renumbering its
features after a delete doesn't lose the current slide's line; a file already in
the folder is added to rather than written over (and a foreign file of that name
is left alone); an old memory layer of the same name is not reused; and the
reopened project reads the file.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsProject, QgsVectorLayer, QgsFeature,
                       QgsGeometry, QgsCoordinateReferenceSystem, QgsPointXY)
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
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
from landslide_groundtruth import volume_tab as VT

dock = LandslideDock(FakeIface())
tab = dock.volume_tab
project = QgsProject.instance()
project.clear()


def on_disk(path):
    """The file's features as a fresh layer reads them — not the edit buffer."""
    probe = QgsVectorLayer(path, "probe", "ogr")
    return {f.id(): f for f in probe.getFeatures()} if probe.isValid() else {}


def outline(name, wkt):
    lyr = QgsVectorLayer("Polygon?crs=EPSG:3338", name, "memory")
    f = QgsFeature(); f.setGeometry(QgsGeometry.fromWkt(wkt))
    lyr.dataProvider().addFeatures([f])
    project.addMapLayer(lyr)
    return lyr


def measure(slide):
    tab.name_edit.setText(slide)
    tab._measure()
    return tab._current


A = tempfile.mkdtemp(prefix="proj centerline ")    # a space, like the real folders
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3338"))
QGZ = os.path.join(A, "event A.qgz")
project.write(QGZ)
SHP = os.path.join(A, "shp_files")
PATH = os.path.join(SHP, "landslide_centerlines.shp")

# a long, bent slide in Alaska Albers: 3 km down, a dog-leg, ~400 m wide
total = outline("Total Area", "POLYGON((0 1000000, 400 1000000, 400 1002000, "
                              "1400 1003000, 1100 1003300, 0 1002200, 0 1000000))")
source = outline("Source Area (best)", "POLYGON((0 1000000, 400 1000000, "
                                       "400 1000500, 0 1000500, 0 1000000))")

print("=== Draw centerline writes the line into shp_files/ at once ===")
c = measure("Slide one")
check("measured, so the centerline can be drawn",
      c is not None and tab.centerline_btn.isEnabled())
tab._draw_centerline()
cl = tab._existing_centerline_layer()
check(f"the file is shp_files/landslide_centerlines.shp in the project folder",
      os.path.isfile(PATH))
check("the layer reads from it, as an ESRI Shapefile",
      cl is not None and cl.providerType() == "ogr"
      and cl.dataProvider().storageType() == "ESRI Shapefile"
      and cl.source().split("|", 1)[0] == PATH)
check(f"named “{VT.CENTERLINE_LAYER}”", cl is not None and cl.name() == VT.CENTERLINE_LAYER)
disk = on_disk(PATH)
f1 = disk.get(c.get("cl_fid"))
check(f"the line is IN the file already, under the row's id ({c.get('cl_fid')})",
      len(disk) == 1 and f1 is not None)
check(f"with its slide, length and method ({f1 and f1['slide']}, "
      f"{f1 and f1['length_m']:.1f} m, {f1 and f1['method']})",
      f1 is not None and f1["slide"] == "Slide one"
      and abs(f1["length_m"] - c["length"]) < 0.01 and f1["method"])
check("a red line style stored beside it (.qml)",
      os.path.isfile(os.path.join(SHP, "landslide_centerlines.qml")))
check("left in edit mode for trimming, with nothing unsaved",
      cl.isEditable() and not cl.isModified()
      and dock.iface.activeLayer() is cl)
check("the log names the file", PATH in tab.log.toPlainText())

print("\n=== numbers for an untouched line go straight to the file ===")
tab._stamp_centerline(cl, {"drop_m": 812.5, "reach_deg": 14.2})
f1 = on_disk(PATH)[c["cl_fid"]]
check(f"drop and travel angle on disk ({f1['drop_m']}, {f1['reach_deg']})",
      abs(f1["drop_m"] - 812.5) < 1e-6 and abs(f1["reach_deg"] - 14.2) < 1e-6)
check("without touching the edit session", cl.isEditable() and not cl.isModified())

print("\n=== a trim's new length waits for the trim's own Save ===")
g = QgsGeometry(cl.getFeature(c["cl_fid"]).geometry())
n_v = len(list(g.vertices()))
g.deleteVertex(n_v - 1)                        # trim the toe end
cl.changeGeometry(c["cl_fid"], g)
before = c["length"]
tab._remeasure_centerline()
check(f"re-measured shorter ({before:.0f} → {c['length']:.0f} m)",
      c["length"] < before)
check("the file still holds the untrimmed line and length (nothing saved for you)",
      abs(on_disk(PATH)[c["cl_fid"]]["length_m"] - before) < 0.01
      and cl.isModified())
cl.commitChanges()
f1 = on_disk(PATH)[c["cl_fid"]]
check(f"saving the trim saves its length with it ({f1['length_m']:.0f} m)",
      abs(f1["length_m"] - c["length"]) < 0.01
      and abs(f1.geometry().length() - g.length()) < 0.01)

print("\n=== the next slide never saves a pending trim behind your back ===")
cl.startEditing()
g = QgsGeometry(cl.getFeature(c["cl_fid"]).geometry())
g.deleteVertex(0)                              # trim slide one's crown, unsaved
cl.changeGeometry(c["cl_fid"], g)
one_fid = c["cl_fid"]
c2 = measure("Slide two")
tab._draw_centerline()
disk = on_disk(PATH)
f2 = disk.get(c2.get("cl_fid"))
check(f"slide two's line is in the file ({len(disk)} lines)",
      len(disk) == 2 and f2 is not None and f2["slide"] == "Slide two")
check("slide one's trim is still pending, not saved",
      cl.isModified() and one_fid in cl.editBuffer().changedGeometries()
      and abs(disk[one_fid].geometry().length() - g.length()) > 1)
cl.rollBack()
cl.startEditing()

print("\n=== a shapefile renumbers on delete: the slide's line is still found ===")
cl.deleteFeature(one_fid)
cl.commitChanges()
fids = {f["slide"]: f.id() for f in on_disk(PATH).values()}
print(f"       (after the delete the file's ids are {fids}; the row held {c2['cl_fid']})")
tab.log.clear()
tab._remeasure_centerline()
check("Re-measure finds slide two's line, not a stale id",
      "gone" not in tab.log.toPlainText()
      and c2["cl_fid"] == fids.get("Slide two")
      and tab._centerline_feature(cl)["slide"] == "Slide two")

print("\n=== the file already there is added to, never written over ===")
project.removeMapLayer(cl.id())
cl = None
check("removing the layer leaves the file", os.path.isfile(PATH))
c3 = measure("Slide three")
tab._draw_centerline()
cl = tab._existing_centerline_layer()
disk = on_disk(PATH)
check(f"the same file is reloaded and appended to ({len(disk)} lines)",
      cl is not None and cl.source().split("|", 1)[0] == PATH and len(disk) == 2
      and {f["slide"] for f in disk.values()} == {"Slide two", "Slide three"})
check("no second file was started",
      not os.path.exists(os.path.join(SHP, "landslide_centerlines_2.shp")))

print("\n=== the reopened project reads the lines from the file ===")
cl.commitChanges()
project.write(QGZ)
project.clear()
project.read(QGZ)
again = [l for l in project.mapLayersByName(VT.CENTERLINE_LAYER)]
check("one centerline layer, from the file, with both lines",
      len(again) == 1 and again[0].providerType() == "ogr"
      and again[0].source().split("|", 1)[0] == PATH and again[0].featureCount() == 2)

print("\n=== an old scratch layer of the same name is not reused ===")
B = tempfile.mkdtemp(prefix="proj old ")
project.clear()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3338"))
project.write(os.path.join(B, "event B.qgz"))
old = QgsVectorLayer("LineString?crs=EPSG:3338&field=slide:string&field=length_m:double",
                     VT.CENTERLINE_LAYER, "memory")
project.addMapLayer(old)
total = outline("Total Area", "POLYGON((0 1000000, 400 1000000, 400 1002000, "
                              "0 1002000, 0 1000000))")
measure("Slide B")
tab._draw_centerline()
cl = tab._existing_centerline_layer()
check("the line went to shp_files/ in this project's folder, not the memory layer",
      cl is not None and cl is not old and cl.providerType() == "ogr"
      and old.featureCount() == 0
      and len(on_disk(os.path.join(B, "shp_files", "landslide_centerlines.shp"))) == 1)
check("and the log says the old layer was left alone",
      "older scratch layer" in tab.log.toPlainText())

print("\n=== a foreign file of that name is left alone ===")
C = tempfile.mkdtemp(prefix="proj foreign ")
os.makedirs(os.path.join(C, "shp_files"))
from qgis.core import QgsVectorFileWriter
pts = QgsVectorLayer("Point?crs=EPSG:3338&field=name:string", "pts", "memory")
p = QgsFeature(pts.fields()); p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 1e6)))
pts.dataProvider().addFeatures([p])
opts = QgsVectorFileWriter.SaveVectorOptions(); opts.driverName = "ESRI Shapefile"
foreign = os.path.join(C, "shp_files", "landslide_centerlines.shp")
QgsVectorFileWriter.writeAsVectorFormatV3(pts, foreign, project.transformContext(), opts)
stamp = os.path.getmtime(foreign)
project.clear()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3338"))
project.write(os.path.join(C, "event C.qgz"))
total = outline("Total Area", "POLYGON((0 1000000, 400 1000000, 400 1002000, "
                              "0 1002000, 0 1000000))")
measure("Slide C")
tab._draw_centerline()
cl = tab._existing_centerline_layer()
check("the point file is untouched and the line goes to landslide_centerlines_2.shp",
      os.path.getmtime(foreign) == stamp and len(on_disk(foreign)) == 1
      and cl is not None
      and os.path.basename(cl.source().split("|", 1)[0]) == "landslide_centerlines_2.shp")

project.clear()
dock.teardown()
_restore_settings()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
# Shut down in QGIS's own order (see test_outline_saved.py): QGIS 4 segfaults
# if the dock's editing layers outlive their providers at interpreter exit.
del dock, tab
import gc; gc.collect()
app.exitQgis()
sys.exit(1 if FAIL else 0)
