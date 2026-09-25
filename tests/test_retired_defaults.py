"""A project saved under an old default must open on today's default.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_retired_defaults.py

project_state saves EVERY widget, untouched ones included, so a default that was
later changed stays frozen into each project saved before the change. On
2026-09-25 the St. Elias project still restored Max cloud 80, "Cover the exact
epicentre" and "Standard quality only" a day after those defaults were changed,
and between them they emptied its post-event list — the scene Planet Explorer
showed never came back. RETIRED_DEFAULTS swaps exactly those stored values for
today's defaults, once, in any project stamped before VERSION 2. The seeded
strings below are the ones that project file actually holds.
"""
import os, sys
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


from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth import project_state as ps
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
state = dock.project_state
project = QgsProject.instance()
SCOPE = ps.SCOPE

# exactly what "2026-08-18 St.Elais.qgz" stores under <landslide_groundtruth><planet>
ST_ELIAS = {
    "planet/cloud_slider": "80",
    "planet/coverage_combo": "Cover the exact epicentre (stricter)",
    "planet/quality_combo": "Standard quality only",
    "planet/pre_slider": "60",
    "planet/post_slider": "90",
    "planet/radius_spin": "5.0",
}


def seed(version, entries):
    """A fresh project holding `entries`, stamped `version` (None = no stamp)."""
    project.clear()
    for k in list(entries):
        project.removeEntry(SCOPE, k)
    if version is not None:
        project.writeEntry(SCOPE, ps.VERSION_KEY, version)
    for k, v in entries.items():
        project.writeEntry(SCOPE, k, v)
    state._restore_now()


def filters():
    return (tab.cloud_slider.value(), tab.coverage_combo.currentData(),
            tab.quality_combo.currentData())


print("=== the defaults asked for: AOI overlap + test quality (and no cloud cap) ===")
project.clear(); state._restore_now()
check(f"a new project opens on 100 / aoi / any (got {filters()})",
      filters() == (100, "aoi", "any"))

print("\n=== the St. Elias project (version 1) opens on today's defaults ===")
seed("1", ST_ELIAS)
check(f"retired 80 / epicentre / standard -> 100 / aoi / any (got {filters()})",
      filters() == (100, "aoi", "any"))
check("its real choices are kept: 60 d before, 90 d after, 5 km",
      (tab.pre_slider.value(), tab.post_slider.value(), tab.radius_spin.value())
      == (60, 90, 5.0))
ver, _ = project.readEntry(SCOPE, ps.VERSION_KEY, "")
cov, _ = project.readEntry(SCOPE, "planet/coverage_combo", "")
qual, _ = project.readEntry(SCOPE, "planet/quality_combo", "")
cloud, _ = project.readEntry(SCOPE, "planet/cloud_slider", "")
check(f"the swap is written into the project at once, stamped v{ps.VERSION} "
      f"(got v{ver}: {cloud} / {cov!r} / {qual!r})",
      ver == ps.VERSION and cloud == "100" and cov.startswith("AOI overlap")
      and qual.startswith("Include test-quality"))

print("\n=== ... and only once: after the swap, a choice is a choice ===")
state._restore_now()          # reopening the migrated project
check(f"reopening the migrated project keeps 100 / aoi / any (got {filters()})",
      filters() == (100, "aoi", "any"))
seed(ps.VERSION, {"planet/coverage_combo": "Cover the exact epicentre (stricter)",
                  "planet/quality_combo": "Standard quality only",
                  "planet/cloud_slider": "80"})
check(f"a v{ps.VERSION} project that CHOSE epicentre / standard / 80 keeps them "
      f"(got {filters()})", filters() == (80, "point", "standard"))

print("\n=== only the exact retired values move ===")
seed("1", {"planet/cloud_slider": "50",
           "planet/coverage_combo": "AOI overlap (match Planet Explorer)",
           "planet/quality_combo": "Include test-quality (match Planet Explorer)"})
check(f"a v1 project with cloud 50 (never a default) keeps 50 (got {filters()})",
      filters() == (50, "aoi", "any"))
ver, _ = project.readEntry(SCOPE, ps.VERSION_KEY, "")
check("nothing retired -> the project is left as it was (still v1, not dirtied)",
      ver == "1")
seed(None, {"planet/quality_combo": "Standard quality only"})
check(f"an unstamped project counts as the oldest version (got {filters()})",
      filters()[2] == "any")
check("retired_since('2') is empty, retired_since('1') has all three",
      ps.retired_since("2") == {} and len(ps.retired_since("1")) == 3)
check("retired_since tolerates garbage", len(ps.retired_since("x")) == 3)

project.clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
