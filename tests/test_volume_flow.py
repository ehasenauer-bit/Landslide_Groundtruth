"""The Volume tab, driven the way it is used: one slide, two outlines, one Measure.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_volume_flow.py

The tab was rebuilt (2026-10-23) around how it is actually used: the outlines
are drawn here, one slide per project. Since 2026-11-04 its volume is Larsen et
al. (2010) — area × thickness is parked at parked/thickness/ — and the source
low and high outlines sit beside total and best in the Outlines box, each giving
its own Larsen volume. So:

  * the sections run in working order — Outlines, Settings, Measure, Volumes,
    Centerline & drop, then Write to layer / Copy results — with the results
    table and log collapsed below (the Verdict box went on 2026-10-30);
  * one Measure runs Larsen on every source outline and records the row (no
    "Add to results"); re-measuring the same slide replaces its row and keeps
    its centerline;
  * Write to layer stamps everything onto the TOTAL outline;
  * the explanatory text sits behind one "? Help" toggle, remembered per user;
  * the Fit combo, ∫Δh, DEM differencing and area × thickness are gone.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsProject, QgsVectorLayer, QgsFeature,
                       QgsGeometry, QgsSettings)
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
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
from landslide_groundtruth import volume_calc as VC
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
check("Outlines, Settings, Volumes, Centerline, Results, Log — no Verdict box",
      titles == ["Outlines", "Settings", "Volumes", "Centerline && elevation drop",
                 "Results table", "Log"])
check("no Verdict widgets are left on the tab",
      not any(hasattr(vol, a) for a in ("verdict_combo", "analyst_edit",
                                        "verdict_note", "verdict_summary")))
wi = vol.layout().indexOf(vol.write_btn.parentWidget())
check("Write to layer / Copy results sit between Centerline and the results table",
      vol.copy_btn.parentWidget() is vol.write_btn.parentWidget()
      and vol.layout().indexOf(boxes[3]) < wi < vol.layout().indexOf(boxes[4]))
order = [vol.layout().indexOf(w) for w in boxes]
mi = vol.layout().indexOf(vol.measure_btn.parentWidget())
check("Measure sits between Settings and Volumes",
      order[1] < mi < order[2])
check("results table and log start collapsed; the working sections open",
      boxes[4].isCollapsed() and boxes[5].isCollapsed()
      and not any(b.isCollapsed() for b in boxes[:4]))
check("the material setting is ABOVE Measure (it used to sit below the answer)",
      vol.layout().indexOf(boxes[1]) < mi
      and boxes[1].isAncestorOf(vol.material_combo))
check("no area × thickness widgets are left on the tab",
      not any(hasattr(vol, a) for a in ("thick_best_edit", "thick_low_edit",
                                        "thick_high_edit", "_thick_setting_combo",
                                        "_thick_note_lbl", "vol_thick_out")))
check("source low and high sit in the Outlines box itself, not a collapsed box",
      boxes[0].isAncestorOf(vol._low_combo) and boxes[0].isAncestorOf(vol._high_combo)
      and not boxes[0].findChildren(QgsCollapsibleGroupBox)
      and vol._low_combo.isVisibleTo(vol) and vol._high_combo.isVisibleTo(vol))
check("no Fit combo, no ∫Δh or DEM-differencing widgets, no Add-to-results",
      not any(hasattr(vol, a) for a in ("fit_combo", "_ddem_combo", "_pre_dem_combo",
                                        "diff_btn", "add_btn", "_layer_combo")))

print("\n=== explanatory text behind one toggle ===")
check(f"the tab registers its prose ({len(vol._help_labels)} labels)",
      len(vol._help_labels) >= 4)
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
source = poly("Source Area (best)", "POLYGON((300 1000750, 700 1000750, "
                                    "700 1001000, 300 1001000, 300 1000750))")  # 0.1 km²
check("Total Area → total, Source Area (best) → best",
      vol._role_layer("total") is total and vol._role_layer("best") is source)
check(f"the readout shows both areas ({vol.role_lbl.text()!r})",
      "TOTAL 1 km²" in vol.role_lbl.text() and "src best 0.1 km²" in vol.role_lbl.text())

print("\n=== one Measure: Larsen et al., one row ===")
vol.name_edit.setText("Test slide")
vol._measure()
c = vol._current
want = VC.volume_source(1.0e5, material="bedrock")[0]
check(f"Larsen on the 0.1 km² scar: {c and c['v_larsen']:.4g} m³ "
      f"({c['v_larsen_lo']:.3g}–{c['v_larsen_hi']:.3g})",
      c is not None and abs(c["v_larsen"] - want) / want < 0.01
      and c["v_larsen_lo"] < c["v_larsen"] < c["v_larsen_hi"]
      and abs(c["larsen_area_m2"] - 1.0e5) / 1.0e5 < 0.01)
check("no area × thickness volume on the row",
      not [k for k in c if k.startswith(("v_thick", "thick", "t_"))])
check("recorded as the slide's row without another click",
      vol._rows == [c] and vol.table.rowCount() == 1)
check(f"on screen, bold ({vol.vol_scar_out.text()!r})",
      vol.vol_scar_out.text().startswith(f"{c['v_larsen'] / 1e6:,.3g} Mm³")
      and "bedrock" in vol.vol_scar_out.text() and vol.vol_scar_out.font().bold())
check(f"no source low/high yet, and the readouts say so "
      f"({vol.vol_src_low_out.text()!r})",
      c["v_larsen_srclo"] is None and c["v_larsen_srchi"] is None
      and "no source low outline" in vol.vol_src_low_out.text()
      and "no source high outline" in vol.vol_src_high_out.text())
check(f"implied depth spreads the scar volume over the TOTAL ({vol.depth_out.text()!r})",
      vol.depth_out.text().startswith(f"{c['v_larsen'] / 1.0e6:,.1f} m")
      and "total 1 km²" in vol.depth_out.text())
check("no warnings on a clean slide", not vol.vol_warn_lbl.isVisibleTo(vol))
check("the centerline can be drawn now", vol.centerline_btn.isEnabled())

print("\n=== the centerline lands on the row ===")
vol._draw_centerline()
check(f"length {c.get('length')} m, in the table too",
      c.get("length") and c.get("cl_fid") is not None
      and vol.table.item(0, VT.TABLE_COLS.index("Length (m)")).text() != "—")

print("\n=== re-measuring the same slide replaces its row and keeps the centerline ===")
vol.material_combo.setCurrentIndex(vol.material_combo.findText("Soil"))
length = c["length"]
vol.log.clear()
vol._measure()
c2 = vol._current
soil = VC.volume_source(1.0e5, material="soil")[0]
check(f"still one row, now soil ({c2['v_larsen']:.4g} m³)",
      len(vol._rows) == 1 and vol._rows[0] is c2
      and abs(c2["v_larsen"] - soil) / soil < 0.01 and c2["material"] == "soil")
check("the centerline length carried over", c2.get("length") == length
      and vol.remeasure_btn.isEnabled())
check("and the log says so", "centerline was kept" in vol.log.toPlainText())
vol.material_combo.setCurrentIndex(vol.material_combo.findText("Bedrock"))
vol._measure()
c2 = vol._current

print("\n=== Write to layer stamps everything on the TOTAL outline ===")
vol._write_to_layer()
f = next(total.getFeatures())
check(f"vsrc_m3 = {f['vsrc_m3']}", abs(f["vsrc_m3"] - c2["v_larsen"]) < 1)
check("with both areas, the length and the slide name",
      abs(f["src_m2"] - 1e5) < 1e3 and abs(f["total_m2"] - 1e6) < 1e4
      and f["length_m"] and f["slide"] == "Test slide")
check("no area × thickness fields are added",
      total.fields().indexOf("vthk_m3") < 0 and total.fields().indexOf("thick_m") < 0)
check("the source outline is left alone", source.fields().indexOf("vsrc_m3") < 0)

print("\n=== the 3D viewer pulls the Larsen volume ===")
v3 = dock.viewer3d_tab
v3._pull_volume_details()
txt = v3.det_volume.text()
check(f"figure volume: {txt!r}", "Larsen et al." in txt and "thickness" not in txt)

print("\n=== source low and high: their own volumes, and a wider best range ===")
low = poly("Source Area (low)", "POLYGON((350 1000800, 650 1000800, "
                                "650 1001000, 350 1001000, 350 1000800))")    # 0.06 km²
high = poly("Source Area (high)", "POLYGON((250 1000700, 750 1000700, "
                                  "750 1001000, 250 1001000, 250 1000700))")  # 0.15 km²
check("matched by name to Source (low) / Source (high)",
      vol._role_layer("low") is low and vol._role_layer("high") is high)
vol.log.clear()
vol._measure()
c5 = vol._current
lo_want = VC.volume_source(6.0e4, material="bedrock")[0]
hi_want = VC.volume_source(1.5e5, material="bedrock")[0]
check(f"low {c5['v_larsen_srclo']:.4g} < best {c5['v_larsen']:.4g} < "
      f"high {c5['v_larsen_srchi']:.4g} m³, each Larsen on its own outline",
      abs(c5["v_larsen_srclo"] - lo_want) / lo_want < 0.01
      and abs(c5["v_larsen_srchi"] - hi_want) / hi_want < 0.01
      and c5["v_larsen_srclo"] < c5["v_larsen"] < c5["v_larsen_srchi"])
check(f"the best estimate's range widens with the area span "
      f"({c2['v_larsen_lo']:.3g}–{c2['v_larsen_hi']:.3g} → "
      f"{c5['v_larsen_lo']:.3g}–{c5['v_larsen_hi']:.3g})",
      c5["v_larsen_lo"] < c2["v_larsen_lo"] and c5["v_larsen_hi"] > c2["v_larsen_hi"])
check(f"on screen ({vol.vol_src_low_out.text()!r} / {vol.vol_src_high_out.text()!r})",
      vol.vol_src_low_out.text().startswith(f"{lo_want / 1e6:,.3g} Mm³")
      and "0.06 km²" in vol.vol_src_low_out.text()
      and vol.vol_src_high_out.text().startswith(f"{hi_want / 1e6:,.3g} Mm³")
      and "0.15 km²" in vol.vol_src_high_out.text())
check("in the table", vol.table.item(0, VT.TABLE_COLS.index("V src low (m³)")).text()
      != "—" and vol.table.item(0, VT.TABLE_COLS.index("V src high (m³)")).text() != "—")
check("and the log", "the low estimate" in vol.log.toPlainText()
      and "the high estimate" in vol.log.toPlainText())
vol._write_to_layer()
f = next(total.getFeatures())
check(f"written back: vlowsrc_m3 {f['vlowsrc_m3']:.4g}, vhisrc_m3 {f['vhisrc_m3']:.4g}",
      abs(f["vlowsrc_m3"] - c5["v_larsen_srclo"]) < 1
      and abs(f["vhisrc_m3"] - c5["v_larsen_srchi"]) < 1)
vol._low_combo.setCurrentIndex(0)                  # — none — (and pinned there)
vol._high_combo.setCurrentIndex(0)
vol._measure()

print("\n=== a new slide name adds a row ===")
vol.name_edit.setText("Second slide")
vol._measure()
check("two rows now", len(vol._rows) == 2 and vol.table.rowCount() == 2)
check("a new slide starts without a centerline",
      vol._current.get("length") is None and not vol.remeasure_btn.isEnabled())

print("\n=== only a total outline: no volume, but the slide is still recorded ===")
vol._best_combo.setCurrentIndex(0)                 # — none —
check(f"the readout warns before Measure ({vol.role_lbl.text()[-60:]!r})",
      "No source (best) outline" in vol.role_lbl.text())
vol._measure()
c3 = vol._current
check("a row with the areas and no volume",
      c3["v_larsen"] is None and c3["a_total"] and c3["name"] == "Second slide")
check(f"the skipped fit is explained on screen ({vol.vol_warn_lbl.text()[:50]!r}…)",
      vol.vol_warn_lbl.isVisibleTo(vol) and "Larsen et al. (2010) skipped" in vol.vol_warn_lbl.text()
      and "not run" in vol.vol_scar_out.text())
check("its centerline can still be drawn", vol.centerline_btn.isEnabled())
vol._total_combo.setCurrentIndex(0)
vol._best_combo.setCurrentIndex(vol._best_combo.findData(source.id()))
vol._measure()
c4 = vol._current
check("no total: Larsen still runs, Write to layer falls back to the scar",
      c4["v_larsen"] and c4["out_role"] == "source")
check(f"and there is no implied depth to show ({vol.depth_out.text()!r})",
      "no total outline" in vol.depth_out.text())

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
      vol._current is None and vol.vol_scar_out.text() == "press Measure"
      and vol.vol_src_low_out.text() == ""
      and not vol.centerline_btn.isEnabled())

print("\n=== another project: the last one's slide does not follow it ===")
vol.name_edit.setText("Test slide")
vol._total_combo.setCurrentIndex(vol._total_combo.findData(total.id()))
vol._best_combo.setCurrentIndex(vol._best_combo.findData(source.id()))
vol._measure()
check("a slide on screen before the switch", vol._current is not None and vol._rows)

print("\n=== a Larsen volume too thick for a deposit on ice is flagged ===")
check("the threshold is strict: 5.0 m passes, 5.1 m is flagged",
      VC.implied_depth_note(5.0) is None and VC.implied_depth_note(5.1)
      and VC.implied_depth_note(None) is None)
check("the 0.1 km² scar over 1 km² (~2.6 m) carries no depth note",
      "probably too high" not in vol.vol_warn_lbl.text())
src_fid = next(source.getFeatures()).id()
small_scar = QgsGeometry(next(source.getFeatures()).geometry())
source.dataProvider().changeGeometryValues({src_fid: QgsGeometry.fromWkt(
    "POLYGON((0 1000000, 1000 1000000, 1000 1000500, 0 1000500, 0 1000000))")})  # 0.5 km²
vol._measure()
big = vol._current
depth = big["v_larsen"] / big["a_total"]
check(f"a 0.5 km² scar over the same 1 km² implies {depth:.1f} m, and says so on screen",
      depth > VC.LARSEN_DEPTH_FLAG_M and vol.vol_warn_lbl.isVisibleTo(vol)
      and f"{depth:.1f} m mean thickness" in vol.vol_warn_lbl.text()
      and "probably too high" in vol.vol_warn_lbl.text())
check("and in the log", "probably too high" in vol.log.toPlainText())
source.dataProvider().changeGeometryValues({src_fid: small_scar})
vol._measure()
check("back on the small scar the note goes away",
      "probably too high" not in vol.vol_warn_lbl.text())
project.clear()
check("rows and readouts cleared, so no export mixes two events",
      vol._rows == [] and vol._current is None
      and vol.vol_scar_out.text() == "press Measure")

project.clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
