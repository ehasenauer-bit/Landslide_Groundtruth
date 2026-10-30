"""The analyst's verdict and its CSV export (the seismic comparison is gone).

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_verdict.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)
import io as _io
import csv

from qgis.core import QgsApplication
app = QgsApplication([], False)
from landslide_groundtruth import volume_tab as VT
from landslide_groundtruth import detection as DET

class FakeDock:
    def __init__(self): self.detection = None
class Stub:
    def __init__(self):
        self.dock = FakeDock()
        self._rows = []
    def _role_layer(self, role): return None
    def _append_log(self, t): pass
for m in ("_build_verdict_box","_estimates","_scar_centroid","_verdict_row","_refresh_verdict"):
    setattr(Stub, m, getattr(VT.VolumeTab, m))

s = Stub()
box = s._build_verdict_box()
print("1. box:", repr(box.title()), "| collapsed:", box.isCollapsed())
assert not box.isCollapsed(), "the verdict is part of the flow now, not tucked away"
print("   calls offered:")
for i in range(s.verdict_combo.count()):
    print("     ", repr(s.verdict_combo.itemData(i)), s.verdict_combo.itemText(i)[:56])
assert s.verdict_combo.count() == 7

print("\n2. no detection, no measurement")
print("  ", s.verdict_summary.text().replace("<br>","\n   "))

print("\n3. detection loaded + a good measurement (both fits)")
det=DET.Detection(event_id="AK2026-0204", lat=60.50, lon=-140.60,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection = det
GOOD = {"name":"Slide 1","material":"bedrock","v_larsen":1.30e6,"larsen_area_m2":4.2e5,
        "src_best":4.2e5,"v_thick":2.0e6,"v_thick_lo":1.2e6,"v_thick_hi":2.8e6,
        "t_best":2.5,"a_total":8.0e5}
s._rows = [dict(GOOD)]
assert s._refresh_verdict() is None
txt = s.verdict_summary.text()
print("  ", repr(txt))
for gone in ("seismic", "agree", "Area × thickness", "Larsen"):
    assert gone.lower() not in txt.lower(), gone
assert not any(b.text() == "Check volumes" for b in box.findChildren(VT.QPushButton))
print("   no seismic comparison, no repeated volumes, no Check volumes button")

print("\n4. the comparison is gone from verdict.py and the Detection record")
from landslide_groundtruth import verdict as V
for gone in ("reconcile", "classify", "d_log10", "AGREE_LOG10", "MARGINAL_LOG10"):
    assert not hasattr(V, gone), gone
for gone in ("loc_error_km", "vol_best_m3", "vol_low_m3", "vol_high_m3"):
    assert gone not in DET.Detection.__dataclass_fields__, gone
assert not hasattr(DET.Detection, "vol_str")
print("   ok")

print("\n5. record a verdict and export")
s._rows = [dict(GOOD)]
s.verdict_combo.setCurrentIndex(1)          # confirmed
s.analyst_edit.setText("EH")
s.verdict_note.setPlainText("Clear scar on the\nnorth headwall.")
row = s._verdict_row()
for k in ("event_id","verdict","analyst","implied_depth","recorded_utc","note"):
    print(f"   {k:18s} {row.get(k)!r}")
assert row["verdict"]=="confirmed" and row["analyst"]=="EH"
assert row["event_id"]=="AK2026-0204" and row["implied_depth"]
for gone in ("vol_seismic_m3","d_larsen","d_thickness","agreement","offset_ratio",
             "det_loc_error_km"):
    assert gone not in row, gone
assert "\n" not in row["note"], "newline must not break the CSV row"
assert row["recorded_utc"]

print("\n6. full CSV round-trip")
buf = _io.StringIO(); w = csv.writer(buf)
w.writerow([h for h,_k in VT.CSV_FIELDS])
merged = dict(s._rows[0])
for k,v in row.items():
    if merged.get(k) in (None,""): merged[k]=v
w.writerow(["" if merged.get(k) is None else merged.get(k) for _h,k in VT.CSV_FIELDS])
buf.seek(0); rd=list(csv.DictReader(buf)); out=rd[0]
print("   columns:", len(VT.CSV_FIELDS))
for c in ("vol_thickness_m3","vol_larsen_m3","implied_depth_m","verdict","event_id"):
    print(f"   {c:20s} = {out[c]!r}")
assert out["vol_larsen_m3"]=="1300000.0"
assert out["vol_thickness_m3"]=="2000000.0"
hdrs=[h for h,_ in VT.CSV_FIELDS]
for gone in ("vol_seismic_m3","vol_seismic_lo_m3","vol_seismic_hi_m3","det_loc_error_km",
             "offset_vs_loc_error","D_thickness_log10","D_larsen_log10","agreement",
             "agreement_thickness","agreement_larsen"):
    assert gone not in hdrs, gone
assert len(hdrs)==len(set(hdrs)), "duplicate header!"
print("\nVERDICT SMOKE TEST PASSED")
