"""Candidate tables are as tall as the scenes the search found.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_table_fit.py

The table used to take every spare pixel of its tab, so a search that found a
handful of scenes left a page of blank white table under them (Ethan,
2026-11-16). Checks, on the real dock shown offscreen, per tab: a short list
ends at its last row with the next box right under it; the room it gives up
goes below the tab, not into the results box; a long list still fills the tab
and scrolls; an empty table keeps one blank row.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget, QTabWidget, QTableWidgetItem
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
tabs = [t for t in dock.findChildren(QTabWidget) if t.indexOf(dock.planet_tab) >= 0][0]


def settle():
    for _ in range(8):
        app.processEvents()


def fill(table, n):
    table.setRowCount(n)
    for r in range(n):
        for c in range(table.columnCount()):
            table.setItem(r, c, QTableWidgetItem(f"S2B_MSIL2A_20261115T214749_{r}_{c}"))
    table.resizeColumnsToContents()
    table.horizontalHeader().setStretchLastSection(True)
    settle()


def below(page, w):
    return w.mapTo(page, w.rect().bottomLeft()).y()


def top(page, w):
    return w.mapTo(page, w.rect().topLeft()).y()


def gallery(page):
    return [g for g in page.findChildren(QgsCollapsibleGroupBox)
            if g.title().startswith("Quicklook gallery")][0]


s2_page = tabs.widget(0)
CASES = [
    ("Sentinel-2 / Landsat", s2_page, dock.table, gallery(s2_page)),
    ("PlanetScope", dock.planet_tab, dock.planet_tab.table, gallery(dock.planet_tab)),
    ("SAR", dock.sar_tab, dock.sar_tab.table, gallery(dock.sar_tab)),
    ("Maxar", dock.gegd_tab, dock.gegd_tab._table, dock.gegd_tab._detail_label),
]

dock.resize(460, 1600)
dock.show()
for name, page, table, nxt in CASES:
    print(f"=== {name} ===")
    tabs.setCurrentWidget(page)
    settle()
    fit = table._row_fit

    fill(table, 3)
    h, want = table.height(), fit.content_height()
    check(f"3 scenes: table {h} px = its rows {want} px", abs(h - want) <= 1)
    gap = top(page, nxt) - below(page, table)
    check(f"3 scenes: next box {gap} px under the last row (was the whole tab)", 0 <= gap < 24)
    row_h = table.rowHeight(0)
    check(f"3 scenes: every row shown, no scrolling ({table.verticalScrollBar().maximum()})",
          table.verticalScrollBar().maximum() == 0 and want >= 3 * row_h)

    fill(table, 0)
    h = table.height()
    check(f"empty: header + one blank row ({h} px)",
          abs(h - fit.content_height()) <= 1 and h < 3 * row_h + 40)

    if name != "Maxar":        # its table never stretched: fixed 200 px, unchanged
        fill(table, 80)
        h = table.height()
        slack = page.height() - max(below(page, w) for w in page.findChildren(QWidget)
                                    if w.isVisible() and w.parent() is page)
        check(f"80 scenes: table fills the tab ({h} px, {slack} px left below)",
              h > 10 * row_h and slack < 30)
        check("80 scenes: it scrolls inside", table.verticalScrollBar().maximum() > 0)
    else:
        fill(table, 80)
        check(f"80 scenes: keeps its 200 px floor ({table.height()} px)", table.height() >= 199)
    fill(table, 0)

print("\n=== a narrow dock: the columns overflow, the scrollbar is counted ===")
dock.resize(380, 1600)
tabs.setCurrentWidget(s2_page)
fill(dock.table, 3)
bar = dock.table.horizontalScrollBar()
check(f"columns overflow ({bar.maximum()} px to scroll)", bar.maximum() > 0)
check(f"all 3 rows still shown ({dock.table.verticalScrollBar().maximum()})",
      dock.table.verticalScrollBar().maximum() == 0)
dock.resize(460, 1600)

print("\n=== SAR pair summary wrapping over three lines ===")
sar = dock.sar_tab
tabs.setCurrentWidget(sar)
sar.pair_summary.setText("Pre 2026-11-08 and post 2026-11-14 bracket the seismic time "
                         "by 5.6 d and 0.4 d on track 160, descending; " * 3)
sar.pair_summary.setVisible(True)
fill(sar.table, 3)
gap = top(sar, gallery(sar)) - below(sar, sar.table)
check(f"next box {gap} px under the last row", 0 <= gap < 24)
check(f"rows not squeezed ({sar.table.height()} of {sar.table._row_fit.content_height()} px)",
      sar.table.verticalScrollBar().maximum() == 0)
sar.pair_summary.setVisible(False)

print("\n=== an opened gallery still gets its room ===")
tabs.setCurrentWidget(s2_page)
fill(dock.table, 3)
g = gallery(s2_page)
g.setCollapsed(False)
settle()
check(f"gallery open: {g.height()} px (its scroll area wants 280)", g.height() >= 280)
check(f"table unchanged ({dock.table.height()} px)",
      abs(dock.table.height() - dock.table._row_fit.content_height()) <= 1)
g.setCollapsed(True)

print(f"\n{OK} ok, {FAIL} failed")
sys.exit(1 if FAIL else 0)
