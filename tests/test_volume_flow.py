"""The Volume tab, driven the way it is used: one slide, two outlines, one Measure.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_volume_flow.py

The tab was rebuilt (2026-10-23) around how it is actually used: the outlines
are drawn here, total + source best, one slide per project, and BOTH area ×
thickness and the source-scar fit are wanted on every slide. So:

  * the sections run in working order — Outlines, Settings, Measure, Volumes,
    Centerline & drop, Verdict — with the results table and log collapsed below;
  * one Measure runs both fits, neither the headline, and records the row (no
    "Add to results"); re-measuring the same slide replaces its row and keeps
    its centerline;
  * Write to layer stamps everything onto the TOTAL outline;
  * the explanatory text sits behind one "? Help" toggle, remembered per user;
  * the Fit combo, ∫Δh and DEM differencing are gone from the tab.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsProject, QgsVectorLayer, QgsFeature,
                       QgsGeometry, QgsSettings)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
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
        s.active = None
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return s.active
    def setActiveLayer(s, l): s.active = l


from landslide_groundtruth import volume_tab as VT
_help_before = QgsSettings().value(VT.HELP_SETTING, None)


def _restore():
    s = QgsSettings()
    if _help_before is None:
        s.remove(VT.HELP_SETTING)
    else:
        s.setValue(VT.HELP_SETTING, _help_before)


import atexit
atexit.register(_restore)
QgsSettings().setValue(VT.HELP_SETTING, False)

from landslide_groundtruth.dock import LandslideDock
dock = LandslideDock(FakeIface())
vol = dock.volume_tab
project = QgsProject.instance()
project.clear()
dock.project_state._restore_now()

print("=== the sections run in working order ===")
boxes = [w for w in vol.findChildren(QgsCollapsibleGroupBox)
         if w.parent() is vol]
titles = [b.title() for b in boxes]
print("  ", " → ".join(titles))
check("Outlines, Settings, Volumes, Centerline, Verdict, Results, Log",
      titles == ["Outlines", "Settings", "Volumes", "Centerline && elevation drop",
                 "Verdict — what did you conclude?", "Results table", "Log"])
order = [vol.layout().indexOf(w) for w in boxes]
mi = vol.layout().indexOf(vol.measure_btn.parentWidget())
check("Measure sits between Settings and Volumes",
      order[1] < mi < order[2])
check("results table and log start collapsed; the working sections open",
      boxes[5].isCollapsed() and boxes[6].isCollapsed()
      and not any(b.isCollapsed() for b in boxes[:5]))
check("the thickness setting is ABOVE Measure (it used to sit below the answer)",
      vol.layout().indexOf(boxes[1]) < mi)
check("no Fit combo, no ∫Δh or DEM-differencing widgets, no Add-to-results",
      not any(hasattr(vol, a) for a in ("fit_combo", "_ddem_combo", "_pre_dem_combo",
                                        "diff_btn", "add_btn", "_layer_combo")))

print("\n=== explanatory text behind one toggle ===")
check(f"the tab registers its prose ({len(vol._help_labels)} labels)",
      len(vol._help_labels) >= 6)
check("off: every help label hidden", not any(l.isVisibleTo(vol) for l in vol._help_labels))
vol._help_btn.setChecked(True)
check("on: every help label shown", all(l.isVisibleTo(vol) for l in vol._help_labels
                                        if l.parentWidget().isVisibleTo(vol)))
check("remembered per user, in QgsSettings", VT._truthy(QgsSettings().value(VT.HELP_SETTING)))
check("not in the project file",
      not [k for k, _w in dock.project_state._widgets if "help" in k])
vol._help_btn.setChecked(False)
check("live warnings are not help text", vol.vol_warn_lbl not in vol._help_labels
      and vol.role_lbl not in vol._help_labels)


def poly(name, wkt):
    lyr = QgsVectorLayer("Polygon?crs=EPSG:3338", name, "memory")
    f = QgsFeature(); f.setGeometry(QgsGeometry.fromWkt(wkt))
    lyr.dataProvider().addFeatures([f])
    project.addMapLayer(lyr)
    return lyr


print("\n=== the outlines are matched by name and read live ===")
total = poly("Total Area", "POLYGON((0 1000000, 1000 1000000, 1000 1001000, "
                           "0 1001000, 0 1000000))")              # 1 km²
source = poly("Source Area (best)", "POLYGON((300 1000500, 700 1000500, "
                                    "700 1001000, 300 1001000, 300 1000500))")  # 0.2 km²
check("Total Area → total, Source Area (best) → best",
      vol._role_layer("total") is total and vol._role_layer("best") is source)
check(f"the readout shows both areas ({vol.role_lbl.text()!r})",
      "TOTAL 1 km²" in vol.role_lbl.text() and "src best 0.2 km²" in vol.role_lbl.text())

print("\n=== one Measure: both fits, one row ===")
vol.name_edit.setText("Test slide")
vol._measure()
c = vol._current
check(f"area × thickness: 1 km² × 2.5 m = {c and c.get('v_thick'):.4g} m³",
      c is not None and abs(c["v_thick"] - 2.5e6) / 2.5e6 < 0.01
      and c["thick_from"] == "total")
check(f"source scar on the 0.2 km² scar: {c['v_larsen']:.4g} m³ "
      f"({c['v_larsen_lo']:.3g}–{c['v_larsen_hi']:.3g})",
      c["v_larsen"] > 0 and c["v_larsen_lo"] < c["v_larsen"] < c["v_larsen_hi"]
      and abs(c["larsen_area_m2"] - 2.0e5) / 2.0e5 < 0.01)
check("recorded as the slide's row without another click",
      vol._rows == [c] and vol.table.rowCount() == 1)
check(f"both on screen ({vol.vol_thick_out.text()!r} / {vol.vol_scar_out.text()!r})",
      vol.vol_thick_out.text().startswith("2.5 Mm³")
      and "Mm³" in vol.vol_scar_out.text() and "bedrock" in vol.vol_scar_out.text())
check(f"implied depth spreads the scar volume over the TOTAL ({vol.depth_out.text()!r})",
      vol.depth_out.text().startswith(f"{c['v_larsen'] / 1.0e6:,.1f} m")
      and "total 1 km²" in vol.depth_out.text())
check("no warnings on a clean slide", not vol.vol_warn_lbl.isVisibleTo(vol))
check("the verdict already lists both",
      "Area × thickness" in vol.verdict_summary.text()
      and "Source scar" in vol.verdict_summary.text())
check("the centerline can be drawn now", vol.centerline_btn.isEnabled())

print("\n=== the centerline lands on the row ===")
vol._draw_centerline()
check(f"length {c.get('length')} m, in the table too",
      c.get("length") and c.get("cl_fid") is not None
      and vol.table.item(0, VT.TABLE_COLS.index("Length (m)")).text() != "—")

print("\n=== re-measuring the same slide replaces its row and keeps the centerline ===")
vol.thick_best_edit.setText("3"); vol.thick_low_edit.setText("2")
vol.thick_high_edit.setText("4")
length = c["length"]
vol.log.clear()
vol._measure()
c2 = vol._current
check(f"still one row, now 1 km² × 3 m ({c2['v_thick']:.4g})",
      len(vol._rows) == 1 and vol._rows[0] is c2 and abs(c2["v_thick"] - 3e6) < 3e4)
check("the centerline length carried over", c2.get("length") == length
      and vol.remeasure_btn.isEnabled())
check("and the log says so", "centerline was kept" in vol.log.toPlainText())

print("\n=== Write to layer stamps everything on the TOTAL outline ===")
vol._write_to_layer()
f = next(total.getFeatures())
check(f"vthk_m3 = {f['vthk_m3']}, vsrc_m3 = {f['vsrc_m3']}",
      abs(f["vthk_m3"] - c2["v_thick"]) < 1 and abs(f["vsrc_m3"] - c2["v_larsen"]) < 1)
check("with the thickness, both areas, the length and the slide name",
      f["thick_m"] == 3 and abs(f["src_m2"] - 2e5) < 2e3 and f["length_m"]
      and f["slide"] == "Test slide")
check("the source outline is left alone", source.fields().indexOf("vthk_m3") < 0)

print("\n=== the 3D viewer pulls both volumes, each named ===")
v3 = dock.viewer3d_tab
v3._pull_volume_details()
txt = v3.det_volume.text()
check(f"figure volume: {txt!r}", "area × thickness" in txt and "source scar" in txt)

print("\n=== a new slide name adds a row ===")
vol.name_edit.setText("Second slide")
vol._measure()
check("two rows now", len(vol._rows) == 2 and vol.table.rowCount() == 2)
check("a new slide starts without a centerline",
      vol._current.get("length") is None and not vol.remeasure_btn.isEnabled())

print("\n=== only one outline: the other fit says why it did not run ===")
vol._best_combo.setCurrentIndex(0)                 # — none —
vol._measure()
c3 = vol._current
check("area × thickness still runs", c3["v_thick"] and c3["v_larsen"] is None)
check(f"the skipped fit is explained on screen ({vol.vol_warn_lbl.text()[:50]!r}…)",
      vol.vol_warn_lbl.isVisibleTo(vol) and "Source scar skipped" in vol.vol_warn_lbl.text()
      and "not run" in vol.vol_scar_out.text())
vol._total_combo.setCurrentIndex(0)
vol._best_combo.setCurrentIndex(vol._best_combo.findData(source.id()))
vol.thick_best_edit.setText("2.5"); vol.thick_low_edit.setText("1.5")
vol.thick_high_edit.setText("3.5")             # back on a deposit setting
vol._measure()
c4 = vol._current
check("no total: area × thickness falls back to the scar, and warns that a "
      "deposit thickness there reads low",
      c4["thick_from"] == "source" and "reads low" in vol.vol_warn_lbl.text())

print("\n=== swapped outlines are refused, not measured ===")
vol._total_combo.setCurrentIndex(vol._total_combo.findData(source.id()))
vol._best_combo.setCurrentIndex(vol._best_combo.findData(total.id()))
n = len(vol._rows)
before = vol._current
vol._measure()
check("nothing recorded", len(vol._rows) == n and vol._current is before)
check(f"and the readout says why ({vol.role_lbl.text()[-60:]!r})",
      "⚠" in vol.role_lbl.text())

print("\n=== removing the slide on screen clears it ===")
vol.table.selectRow(vol._rows.index(vol._current))
vol._remove_rows()
check("the readouts clear rather than edit a row that is gone",
      vol._current is None and vol.vol_thick_out.text() == "press Measure"
      and not vol.centerline_btn.isEnabled())

print("\n=== another project: the last one's slide does not follow it ===")
vol.name_edit.setText("Test slide")
vol._total_combo.setCurrentIndex(vol._total_combo.findData(total.id()))
vol._best_combo.setCurrentIndex(vol._best_combo.findData(source.id()))
vol._measure()
check("a slide on screen before the switch", vol._current is not None and vol._rows)
project.clear()
check("rows and readouts cleared, so no verdict mixes two events",
      vol._rows == [] and vol._current is None
      and vol.vol_thick_out.text() == "press Measure"
      and "Area × thickness" not in vol.verdict_summary.text())

project.clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
