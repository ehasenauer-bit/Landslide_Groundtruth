"""The whole plugin must import and build the way QGIS loads it.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_plugin_loads.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

import traceback, warnings
from qgis.core import QgsApplication, QgsProject
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app=QgsApplication([],True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject, pyqtSignal

print("STEP 1 — import every module in the package")
import importlib, pkgutil
import landslide_groundtruth as P
failed=[]
for m in sorted(x.name for x in pkgutil.iter_modules(P.__path__)):
    try:
        importlib.import_module(f"landslide_groundtruth.{m}")
        print(f"   ok   {m}")
    except Exception as e:
        print(f"   FAIL {m}: {type(e).__name__}: {e}"); failed.append(m)
assert not failed, failed

print("\nSTEP 2 — build the real dock (a fake iface, everything else real)")
class FakeBar(QObject):
    def __init__(s): super().__init__(); s.msgs=[]
    def pushMessage(s,*a,**k): s.msgs.append(a)
    def pushInfo(s,t,m): s.msgs.append(("info",t,m))
    def pushWarning(s,t,m): s.msgs.append(("warn",t,m)); print("   [messageBar WARN]",m[:100])
    def pushCritical(s,t,m): s.msgs.append(("crit",t,m))
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s,t): pass
    def unsetMapTool(s,t): pass
    def center(s):
        from qgis.core import QgsPointXY
        return QgsPointXY(0,0)
    def extent(s):
        from qgis.core import QgsRectangle
        return QgsRectangle(0,0,1,1)
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar=FakeBar(); s._c=FakeCanvas(); s._w=QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s,*a): pass
    def removeDockWidget(s,*a): pass
    def addToolBarIcon(s,*a): pass
    def addPluginToMenu(s,*a): pass
    def removeToolBarIcon(s,*a): pass
    def removePluginMenu(s,*a): pass
    def activeLayer(s): return None

warnings.simplefilter("error", SyntaxWarning)
from landslide_groundtruth.dock import LandslideDock
iface=FakeIface()
dock=LandslideDock(iface)
print("   dock built:", dock.windowTitle())

print("\nSTEP 3 — all six tabs present")
from qgis.PyQt.QtWidgets import QTabWidget
tabs=dock.findChild(QTabWidget)
names=[tabs.tabText(i) for i in range(tabs.count())]
print("   tabs:", names)
assert len(names)==6, names

print("\nSTEP 4 — the new boxes are on screen")
from qgis.PyQt.QtWidgets import QGroupBox
boxes=[b.title() for b in dock.findChildren(QGroupBox)]
for want in ("Environment", "Detection"):
    hit=[b for b in boxes if want in b]
    print(f"   {want:12s} -> {hit[0] if hit else 'MISSING'}")
    assert hit, (want, boxes)

print("\nSTEP 5 — exercise the new controls on the real dock")
dock.det_lat_edit.setText("60.50"); dock.det_lon_edit.setText("-140.60")
dock.det_err_spin.setValue(17.0); dock.det_vol_edit.setText("1.3")
print("   detection read:", (dock.detection.event_id, dock.detection.lat,
                             dock.detection.suggested_radius_km()))
assert dock.detection.suggested_radius_km()==17.0
dock._apply_detection()
print("   radius pushed to S2 tab:", dock.radius_spin.value())
assert dock.radius_spin.value()==17.0
print("   env status:", dock.env_status.text()[:70])
print("   env_gate() with no setup:", dock.env_gate())
assert dock.env_gate() is False
dock.lat_edit.setText("59.9, -149.8")
print("   pasted pair split ->", dock.lat_edit.text(), dock.lon_edit.text())
assert dock.lon_edit.text()=="-149.800000"
print("   default scenes ticked:", sorted(k for k,cb in dock.scene_checks.items() if cb.isChecked()))
print("   window warning:", dock.window_warn.text()[:80] or "(none)")

print("\nSTEP 6 — teardown (must release the map tool cleanly)")
dock.teardown()
print("   teardown ok")

errs=[m for m in iface._bar.msgs if m[0] in ("warn","crit")]
print(f"\nmessage-bar warnings raised during build/use: {len(errs)}")
print("\nPLUGIN LOADS AND RUNS")
