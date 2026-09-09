"""The seismic Detection record: fields, the UTC/Alaska rollover, and the push to every tab.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_detection.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
app=QgsApplication([],False)
from landslide_groundtruth import dock as D
from landslide_groundtruth.detection import Detection, to_alaska
from datetime import datetime

class FakeBar:
    def __init__(s): s.msgs=[]
    def pushInfo(s,a,b): s.msgs.append(b); print("   [bar]",b)
    def pushWarning(s,a,b): s.msgs.append("WARN "+b)
class FakeIface:
    def __init__(s): s.bar=FakeBar()
    def messageBar(s): return s.bar
class Stub:
    def __init__(s): s.detection=None; s.iface=FakeIface()
    def _warn(s,t): s.iface.messageBar().pushWarning("L",t)
    def _append_log(s,t): pass
for m in ("_build_detection_box","_num_or_none","_read_detection","_clear_detection",
          "_render_detection","_apply_detection"):
    setattr(Stub,m,getattr(D.LandslideDock,m))

s=Stub(); box=s._build_detection_box()
print("1. box:", repr(box.title()), "| starts expanded:", not box.isCollapsed())
assert not box.isCollapsed()
assert not hasattr(s,"det_paste"), "the paste box must be gone"
assert s.det_apply_btn.isEnabled() is False

print("\n2. type the numbers off the figure")
from qgis.PyQt.QtCore import QDateTime
s.det_time_edit.setDateTime(QDateTime.fromString("2026-02-04 08:48:37","yyyy-MM-dd HH:mm:ss"))
s.det_lat_edit.setText("60.50")
s.det_lon_edit.setText("-140.60")
s.det_err_spin.setValue(17.0)
s.det_vol_edit.setText("1.3")
s.det_vol_lo_edit.setText("0.9")
s.det_vol_hi_edit.setText("1.7")
print("  ", s.det_summary.text().replace("<br>","\n   "))
d=s.detection
assert d is not None
assert (d.lat,d.lon,d.loc_error_km)==(60.5,-140.6,17.0)
assert d.vol_best_m3==1.3e6 and (d.vol_low_m3,d.vol_high_m3)==(0.9e6,1.7e6)
assert d.event_id=="AK2026-0204", d.event_id
assert d.suggested_radius_km()==17.0
assert "previous day" in s.det_summary.text()
assert s.det_apply_btn.isEnabled()
print("   -> radius 17 km, event id auto-derived, AKST rollover shown")

print("\n3. the dropped minus sign is caught")
s.det_lon_edit.setText("140.60")
assert "POSITIVE" in s.det_summary.text()
print("  ", [l for l in s.det_summary.text().split("<br>") if "POSITIVE" in l][0][:96])
s.det_lon_edit.setText("-140.60")

print("\n4. out-of-range values are refused, not half-accepted")
s.det_lat_edit.setText("999")
assert s.detection is None and "out of range" in s.det_summary.text()
assert not s.det_apply_btn.isEnabled()
print("   lat 999 -> detection cleared, Apply disabled")
s.det_lat_edit.setText("60.50")

print("\n5. push to all four tabs")
from qgis.PyQt.QtWidgets import QLineEdit, QDoubleSpinBox, QDateTimeEdit
class FakeTab:
    def __init__(s):
        s.lat_edit=QLineEdit(); s.lon_edit=QLineEdit()
        s.radius_spin=QDoubleSpinBox(); s.radius_spin.setRange(0.2,50.0); s.radius_spin.setValue(5.0)
        s.dt_edit=QDateTimeEdit()
s.lat_edit=QLineEdit(); s.lon_edit=QLineEdit()
s.radius_spin=QDoubleSpinBox(); s.radius_spin.setRange(0.2,50.0); s.radius_spin.setValue(5.0)
s.dt_edit=QDateTimeEdit()
s.planet_tab=FakeTab(); s.sar_tab=FakeTab(); s.viewer3d_tab=FakeTab()
s._apply_detection()
for n,t in (("dock",s),("planet",s.planet_tab),("sar",s.sar_tab),("viewer3d",s.viewer3d_tab)):
    got=(t.lat_edit.text(),t.lon_edit.text(),t.radius_spin.value(),
         t.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm:ss"))
    print(f"   {n:9s} {got}")
    assert got==("60.500000","-140.600000",17.0,"2026-02-04 08:48:37"), (n,got)

print("\n6. volume with no range still works; blank volume is fine")
s.det_vol_lo_edit.clear(); s.det_vol_hi_edit.clear()
assert s.detection.vol_best_m3==1.3e6 and s.detection.vol_low_m3 is None
s.det_vol_edit.clear()
assert s.detection.vol_best_m3 is None and s.detection is not None
print("   ok")

print("\n7. no location error -> radius falls back to 5 km")
s.det_err_spin.setValue(0.0)
assert s.detection.suggested_radius_km()==5.0
assert "no location error entered" in s.det_summary.text()
print("   ok")

print("\n8. clear")
s._clear_detection()
assert s.detection is None and not s.det_apply_btn.isEnabled()
assert s.det_lat_edit.text()=="" and s.det_err_spin.value()==0.0
print("   ok")

print("\n9. Alaska DST still correct")
for iso,want in [("2026-01-15 20:00:00","AKST"),("2026-07-15 20:00:00","AKDT"),
                 ("2026-03-08 10:00:00","AKST"),("2026-03-08 12:00:00","AKDT"),
                 ("2026-11-01 09:00:00","AKDT"),("2026-11-01 11:00:00","AKST")]:
    loc,name=to_alaska(datetime.strptime(iso,"%Y-%m-%d %H:%M:%S"))
    assert name==want,(iso,name); print(f"   {iso}Z -> {loc:%m-%d %H:%M} {name}")

print("\n10. parse() is gone")
import landslide_groundtruth.detection as DT
assert not hasattr(DT,"parse") and not hasattr(DT,"_PAT")
print("   parser and its regexes removed")
print("\nDETECTION (TYPED) VERIFIED")
