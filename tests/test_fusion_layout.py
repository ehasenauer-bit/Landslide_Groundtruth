"""Fuse sits right under the Outputs box, without scrolling past blank page.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_fusion_layout.py

The Run/Advanced pages are a tab widget, which Qt sizes to its TALLEST page —
and the Advanced page reports its four boxes at full expanded height until it is
first shown (QgsCollapsibleGroupBox only collapses once visible). That padded the
Run page to ~1220 px for ~540 px of content, so Fuse sat ~640 px of empty page
below Outputs. Qt asks the tab widget for its height three ways (sizeHint,
minimumSizeHint, heightForWidth) and all three had to change; heightForWidth is
also asked at the tab's MINIMUM width, wider than a side dock gives it.

Checks, on the real dock shown offscreen: the gap from Outputs to Fuse on first
open, after expanding an Advanced box and coming back, at a narrow and a wide
dock; the Advanced page still growing to show an expanded box; the Run page's
rows not squeezed to make the numbers work; and the Outputs box itself on one
compact row of ticks.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget, QTabWidget, QScrollArea, QComboBox
from qgis.PyQt.QtCore import QObject
from qgis.gui import QgsCollapsibleGroupBox

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


from landslide_groundtruth.dock import LandslideDock
dock = LandslideDock(FakeIface())
ft = dock.fusion_tab
tabs = [t for t in dock.findChildren(QTabWidget) if t.indexOf(ft) >= 0][0]
inner = dock.findChildren(QScrollArea)[0].widget()


def settle():
    for _ in range(6):
        app.processEvents()


def box(title):
    return [g for g in ft.findChildren(QgsCollapsibleGroupBox) if g.title() == title][0]


def gap():
    """Pixels from the bottom of Outputs to the top of Fuse."""
    settle()
    out = box("Outputs")
    bottom = out.mapTo(inner, out.rect().bottomLeft()).y()
    return ft.run_btn.mapTo(inner, ft.run_btn.rect().topLeft()).y() - bottom


def rows():
    return [c.height() for c in ft.pages.widget(0).findChildren(QComboBox) if c.isVisible()]


dock.resize(440, 1000)
dock.show()
tabs.setCurrentWidget(ft)
settle()

print("=== a side dock (440 px): Fuse right under Outputs ===")
g = gap()
check(f"on first open: {g} px from Outputs to Fuse (was ~640 of blank page)", 0 < g < 30)
narrow_rows = rows()

print("\n=== the Advanced page still sizes to what it shows ===")
ft.pages.setCurrentIndex(1)
settle()
shut = ft.pages.height()
box("Detection").setCollapsed(False)
settle()
check(f"expanding Detection grows the pages ({shut} -> {ft.pages.height()} px)",
      ft.pages.height() > shut + 100)
ft.pages.setCurrentIndex(0)
g = gap()
check(f"back on Run with Detection still open: {g} px", 0 < g < 30)

print("\n=== at other widths ===")
for w in (380, 700):
    dock.resize(w, 1000)
    g = gap()
    check(f"{w} px wide: {g} px", 0 < g < 30)
dock.resize(440, 1000)
settle()
check(f"no row squeezed to make room: combo heights {rows()} == {narrow_rows}",
      rows() == narrow_rows and min(rows()) >= 20)

print("\n=== the Outputs box itself is one compact row ===")
dock.resize(700, 1000)
settle()
out = box("Outputs")
check(f"both ticks on one row at 700 px ({out.height()} px tall)",
      abs(ft.out_fused_check.y() - ft.out_optical_check.y()) < 3 and out.height() < 100)
check("what the note said is in the tooltips",
      "always written to the GeoTIFF" in ft.out_fused_check.toolTip()
      and "always written to the GeoTIFF" in out.toolTip())

dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
