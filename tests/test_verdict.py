"""The analyst's verdict and the three-way volume reconciliation.

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
for m in ("_build_verdict_box","_latest_row","_estimates","_scar_centroid","_verdict_row","_refresh_verdict"):
    setattr(Stub, m, getattr(VT.VolumeTab, m))

s = Stub()
box = s._build_verdict_box()
print("1. box:", repr(box.title()), "| collapsed:", box.isCollapsed())
print("   calls offered:")
for i in range(s.verdict_combo.count()):
    print("     ", repr(s.verdict_combo.itemData(i)), s.verdict_combo.itemText(i)[:56])
assert s.verdict_combo.count() == 7

print("\n2. no detection, no measurement")
print("  ", s.verdict_summary.text().replace("<br>","\n   "))

print("\n3. detection loaded + a good measurement")
det=DET.Detection(event_id="AK2026-0204", lat=60.50, lon=-140.60, loc_error_km=17.0,
                  vol_best_m3=1.3e6, vol_low_m3=0.9e6, vol_high_m3=1.7e6,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection = det
s._rows = [{"name":"Slide 1","material":"bedrock","v_best":1.30e6,
            "a_conv":4.2e5,"src_best":4.2e5,"v_erosion":1.1e6}]
rec = s._refresh_verdict()
print("  ", s.verdict_summary.text().replace("<br>","\n   "))
assert rec["agreement"]=="agree"

print("\n4. graded disagreement")
for vb, want in ((3.0e6,"agree"), (9.0e6,"marginal"), (3.0e7,"disagree")):
    s._rows = [{"name":"Slide 1","material":"bedrock","v_best":vb,"a_conv":4.2e5}]
    rec = s._refresh_verdict()
    print("   larsen %-9.3g vs seismic 1.3e6 -> x%-5.1f %s"
          % (vb, vb/1.3e6, rec["agreement"]))
    assert rec["agreement"]==want, (vb, rec["agreement"], want)

print("\n5. record a verdict and export")
s._rows = [{"name":"Slide 1","material":"bedrock","v_best":1.30e6,
            "a_conv":4.2e5,"src_best":4.2e5,"v_erosion":1.1e6}]
s.verdict_combo.setCurrentIndex(1)          # confirmed
s.analyst_edit.setText("EH")
s.verdict_note.setPlainText("Clear scar on the\nnorth headwall.")
row = s._verdict_row()
for k in ("event_id","verdict","analyst","vol_seismic_m3","d_larsen","agreement",
          "implied_depth","recorded_utc","note"):
    print(f"   {k:18s} {row.get(k)!r}")
assert row["verdict"]=="confirmed" and row["analyst"]=="EH"
assert row["vol_seismic_m3"]=="1300000" and row["agreement"]=="agree"
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
for c in ("vol_seismic_m3","vol_larsen_m3","vol_dh_erosion_m3","agreement","verdict","event_id"):
    print(f"   {c:20s} = {out[c]!r}")
assert out["vol_seismic_m3"]=="1300000"
assert out["vol_larsen_m3"]=="1300000.0"
assert out["vol_dh_erosion_m3"]=="1100000.0"
assert out["vol_seismic_m3"] != out["vol_dh_erosion_m3"], "must be separate columns"
hdrs=[h for h,_ in VT.CSV_FIELDS]
assert len(hdrs)==len(set(hdrs)), "duplicate header!"
print("\nVERDICT SMOKE TEST PASSED")
