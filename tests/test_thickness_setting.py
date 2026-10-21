"""The Volume tab's deposit Setting fills mean/low/high, and follows them back.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_thickness_setting.py

The area × thickness fit's answer is the thickness, and the right one depends on
the deposit: ~2.5 m for a rock avalanche spread over a glacier, 1.5 m for an
ice-rich Iliamna-type sheet, under 1 m for a tiny one, 5–10 m when confined, a
5–30 m detachment depth for a scar outline. The Setting combo fills the three
numbers from volume_calc.THICKNESS_SETTINGS. It is NOT stored in the project — it
is read back off the numbers — so a combo saved apart from them can never
disagree with what Measure multiplies by, and an older project shows whichever
setting its values are.

Checks: the first setting is the fields' default; a pick fills all three, and an
edit away from every setting reads Custom; a project restore lands the combo on
its numbers' setting without refilling them; the combo never reaches the
project; and Measure records the setting and says when it does not fit the
outline it converted.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsProject, QgsVectorLayer, QgsFeature,
                       QgsGeometry)
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


from landslide_groundtruth.dock import LandslideDock
from landslide_groundtruth import project_state as ps, volume_calc
dock = LandslideDock(FakeIface())
vol = dock.volume_tab
state = dock.project_state
project = QgsProject.instance()
combo = vol._thick_setting_combo
SETTINGS = volume_calc.THICKNESS_SETTINGS


def numbers():
    return (vol.thick_best_edit.text(), vol.thick_low_edit.text(),
            vol.thick_high_edit.text())


def pick(key):
    """What a user's pick does: the combo moves, then `activated` fires."""
    idx = combo.findData(key)
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)


print("=== the settings themselves ===")
check(f"five settings plus Custom (got {combo.count()} items)",
      combo.count() == len(SETTINGS) + 1 == 6
      and combo.itemData(combo.count() - 1) == "custom")
check("every setting has low < mean < high",
      all(lo < best < hi for _l, _k, best, lo, hi, _n in SETTINGS))
check("the keys are unique", len({s[1] for s in SETTINGS}) == len(SETTINGS))
ice = next(s for s in SETTINGS if s[1] == "ice_rich")
check("the ice-rich note says it is the rock sheet and the 2–3× failed volume",
      "ROCK sheet" in ice[5] and "2–3×" in ice[5])
check("thickness_setting reads 1 / 1.0 / 1.00 as the same number",
      volume_calc.thickness_setting(1.5, 1, 2.00)[1] == "ice_rich")
check("thickness_setting is None off every setting and with a blank bound",
      volume_calc.thickness_setting(2.0, 1.5, 3.5) is None
      and volume_calc.thickness_setting(2.5, None, 3.5) is None)

print("\n=== a new project ===")
project.clear(); state._restore_now()
check(f"opens on 2.5 / 1.5 / 3.5 (got {numbers()})",
      numbers() == ("2.5", "1.5", "3.5"))
check(f"... which is the first setting, on rock avalanche on a glacier "
      f"(got {combo.currentData()})", combo.currentData() == SETTINGS[0][1] == "glacier")
check("the note under the combo is that setting's evidence",
      vol._thick_note_lbl.text() == SETTINGS[0][5])

print("\n=== picking a setting fills the numbers ===")
for label, key, best, low, high, note in SETTINGS:
    pick(key)
    want = (f"{best:g}", f"{low:g}", f"{high:g}")
    check(f"{key}: fills {want} (got {numbers()}), combo stays on it",
          numbers() == want and combo.currentData() == key
          and vol._thick_note_lbl.text() == note)
pick("ice_rich")
vol.thick_best_edit.setText("1.8")
check(f"editing the mean away from every setting reads Custom (got "
      f"{combo.currentData()}), the numbers kept",
      combo.currentData() == "custom" and numbers() == ("1.8", "1", "2"))
pick("custom")
check("picking Custom leaves the numbers alone",
      numbers() == ("1.8", "1", "2") and combo.currentData() == "custom")
vol.thick_best_edit.setText("7"); vol.thick_low_edit.setText("5.0")
vol.thick_high_edit.setText("10")
check(f"typing a setting's numbers by hand lands on it (got {combo.currentData()})",
      combo.currentData() == "confined")
pick("custom"); pick("confined")
check("Custom, then the setting whose numbers are already there, shows it again",
      combo.currentData() == "confined"
      and vol._thick_note_lbl.text() == next(s[5] for s in SETTINGS
                                             if s[1] == "confined"))

print("\n=== project restore ===")
check("the combo is not stored in the project",
      not [k for k, _w in state._widgets if "setting" in k])


def seed(version, best, low, high):
    project.clear()
    project.writeEntry(ps.SCOPE, ps.VERSION_KEY, version)
    for k, v in (("volume/thick_best_edit", best), ("volume/thick_low_edit", low),
                 ("volume/thick_high_edit", high)):
        project.writeEntry(ps.SCOPE, k, v)
    state._restore_now()


seed(ps.VERSION, "0.7", "0.4", "1")
check(f"a project on 0.7 / 0.4 / 1 opens on Very small (got {combo.currentData()}, "
      f"{numbers()})", combo.currentData() == "small"
      and numbers() == ("0.7", "0.4", "1"))
seed(ps.VERSION, "4", "3", "8")
check(f"a project on 4 / 3 / 8 opens on Custom with its own numbers "
      f"(got {combo.currentData()}, {numbers()})",
      combo.currentData() == "custom" and numbers() == ("4", "3", "8"))
seed(ps.VERSION, "1.5", "1.0", "2.0")
check(f"an Iliamna project saved as 1.5 / 1.0 / 2.0 opens on Ice-rich "
      f"(got {combo.currentData()})", combo.currentData() == "ice_rich")
seed("2", "1.5", "1.0", "2.5")
check(f"a v2 project on the retired 1.5 / 1.0 / 2.5 opens on the glacier "
      f"default (got {combo.currentData()}, {numbers()})",
      combo.currentData() == "glacier" and numbers() == ("2.5", "1.5", "3.5"))

print("\n=== Measure records the setting and flags a mismatch ===")
project.clear(); state._restore_now()
layer = QgsVectorLayer("Polygon?crs=EPSG:3338", "total_area", "memory")
f = QgsFeature()
f.setGeometry(QgsGeometry.fromWkt(
    "POLYGON((0 1000000, 1000 1000000, 1000 1001000, 0 1001000, 0 1000000))"))
layer.dataProvider().addFeatures([f])
project.addMapLayer(layer)
vol._refresh_layers()
vol._total_combo.setCurrentIndex(vol._total_combo.findData(layer.id()))
vol.fit_combo.setCurrentIndex(
    [k for _l, k in volume_calc.FITS].index("thickness"))


def measure():
    vol.log.clear()
    vol._current = None
    vol._measure()
    return getattr(vol, "_current", None) or {}, vol.log.toPlainText()


cur, log = measure()
v = cur.get("v_best") or 0
check(f"1 km² × 2.5 m ≈ 2.5 Mm³ (got {v:.4g})", abs(v - 2.5e6) / 2.5e6 < 0.01)
check(f"the result names the setting (got {cur.get('calc')!r})",
      "Rock avalanche on a glacier" in (cur.get("calc") or ""))
check("no ice or scar warning on the default", "ROCK sheet" not in log
      and "detachment depth" not in log)
pick("ice_rich")
cur, log = measure()
check("Ice-rich: logs that it is the rock sheet and the 2–3× failed volume",
      "ROCK sheet" in log and "2–3×" in log)
pick("scar")
cur, log = measure()
check("Source scar on the TOTAL outline: says it reads too high",
      "detachment depth" in log and "too high" in log)
vol.thick_best_edit.setText("3"); vol.thick_low_edit.setText("2")
vol.thick_high_edit.setText("4")
cur, log = measure()
check(f"Custom numbers: no setting in the result (got {cur.get('calc')!r}), "
      "no setting warning",
      cur.get("calc") == "deposit area × thickness (3 m, 2–4)"
      and "detachment depth" not in log and "ROCK sheet" not in log)

project.removeMapLayer(layer.id())
project.clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
