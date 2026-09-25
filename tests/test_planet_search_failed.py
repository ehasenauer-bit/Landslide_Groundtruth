"""A Planet search that FAILED must not be reported as zero scenes found.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_search_failed.py

On 2026-09-25 13:31 the Planet SDK couldn't fetch its bundle spec, the search
raised before searching anything, and the tab said "Found 0 pre / 0 post
candidate scene(s)" — which reads as an empty archive and sends you off
loosening filters that were never applied. run_single now marks the failed
source in search.json's 'failed'; the tab must warn, and still say "Found 0"
only for a search that actually ran. Drives the real _on_search_done.
"""
import os, sys, tempfile
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
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
    def __init__(s): super().__init__(); s.msgs = []
    def pushMessage(s, *a, **k): s.msgs.append(("msg",) + a)
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
iface = FakeIface()
dock = LandslideDock(iface)
tab = dock.planet_tab
# point the tab's project dir somewhere empty, so Recall's ledger lookup is a no-op
dock.project_edit.setText(tempfile.mkdtemp(prefix="planet_search_failed_"))
bar = iface._bar
ERR = ("planet: FetchBundlesSpecError: Unable to fetch spec from API to generate "
       "valid item types and bundles. Please retry!")


def search_done(**result):
    bar.msgs.clear()
    base = dict(pre=[], post=[], notes=[], lat=60.26, lon=-140.817,
                params=dict(radius_km=5.0))
    base.update(result)
    tab.task = SimpleNamespace(result=base)
    tab._on_search_done()
    return [m for m in bar.msgs if m[0] in ("info", "warn")]


print("=== the 13:31 search: Planet's spec fetch raised, nothing was searched ===")
msgs = search_done(notes=[ERR], failed=[ERR])
warns = [m for m in msgs if m[0] == "warn"]
check("it is reported as a FAILED search", warns and "Search failed" in warns[0][2])
check("with the reason", warns and "FetchBundlesSpecError" in warns[0][2])
check("and never as 'Found 0 … scenes'", not any("Found 0" in m[2] for m in msgs))

print("\n=== a search that ran and matched nothing still says so ===")
msgs = search_done(notes=["PlanetScope: incomplete coverage (0 pre, 0 post)"])
check("'Found 0 pre / 0 post' as info, no warning",
      any(m[0] == "info" and "Found 0 pre / 0 post" in m[2] for m in msgs)
      and not any(m[0] == "warn" for m in msgs))

print("\n=== an older search.json without 'failed' behaves as before ===")
msgs = search_done(notes=[ERR])
check("no crash, and the old 'Found 0' info", any(m[0] == "info" for m in msgs))

dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
