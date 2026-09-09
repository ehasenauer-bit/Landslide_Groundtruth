"""The volume cross-check must read each estimate from the fit that produced it.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_fit.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
app=QgsApplication([],False)
from landslide_groundtruth import volume_tab as VT, detection as DET
class FakeDock: detection=None
class Stub:
    def __init__(self): self.dock=FakeDock(); self._rows=[]
    def _role_layer(self,r): return None
    def _append_log(self,t): pass
for m in ("_build_verdict_box","_latest_row","_scar_centroid","_verdict_row","_refresh_verdict"):
    setattr(Stub,m,getattr(VT.VolumeTab,m))
s=Stub(); _box=s._build_verdict_box()  # keep a ref: Qt deletes children otherwise
det=DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6, loc_error_km=17.0,
                  vol_best_m3=1.3e6,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection=det

print("=== THE BUG: a ∫Δh measure only ===")
print("   (rows now carry v_best=|erosion|, v_net separately)")
s._rows=[{"name":"S1","fit":"ddem","v_best":1.1e6,"v_net":2.0e4,
          "v_erosion":1.1e6,"v_deposit":1.08e6}]
rec=s._refresh_verdict()
txt=s.verdict_summary.text()
print("  ", txt.replace("<br>","\n   ").replace("&nbsp;"," "))
assert rec["d_larsen"] is None, "must NOT treat net as a Larsen estimate"
assert rec["d_dh"] is not None and rec["agreement"]=="agree"
print("   -> larsen=None (net correctly NOT used), dh compared: OK")

print("\n=== an area-scaling measure only ===")
s._rows=[{"name":"S1","fit":"scar","material":"bedrock","v_best":1.3e6,
          "v_low":0.7e6,"v_high":2.4e6,"a_conv":4.2e5,"src_best":4.2e5}]
rec=s._refresh_verdict()
print("   d_larsen=%.3f d_dh=%s -> %s"%(rec["d_larsen"],rec["d_dh"],rec["agreement"]))
assert rec["d_dh"] is None and rec["agreement"]=="agree"

print("\n=== BOTH fits run, as two separate Measures ===")
s._rows=[{"name":"S1","fit":"scar","material":"bedrock","v_best":1.3e6,
          "v_low":0.7e6,"v_high":2.4e6,"a_conv":4.2e5,"src_best":4.2e5},
         {"name":"S1","fit":"ddem","v_best":1.1e6,"v_net":2.0e4,
          "v_erosion":1.1e6,"v_deposit":1.08e6}]
rec=s._refresh_verdict()
print("  ", s.verdict_summary.text().replace("<br>","\n   ").replace("&nbsp;"," "))
assert rec["d_larsen"] is not None and rec["d_dh"] is not None, "three-way must work"
print("   -> THREE-WAY comparison achieved across two Measure runs")

print("\n=== export columns split correctly ===")
s.verdict_combo.setCurrentIndex(1); s.analyst_edit.setText("EH")
row=s._verdict_row()
for k in ("vol_larsen_m3","vol_dh_net_m3","v_erosion","implied_depth"):
    print(f"   {k:16s} {row.get(k, s._rows[-1].get(k))!r}")
assert row["vol_larsen_m3"]==1.3e6, row["vol_larsen_m3"]
assert row["vol_dh_net_m3"]==2.0e4, row["vol_dh_net_m3"]
assert row["vol_larsen_m3"]!=row["vol_dh_net_m3"], "must never share a column"
assert abs(float(row["implied_depth"])-3.10)<0.01
hdrs=[h for h,_ in VT.CSV_FIELDS]; assert len(hdrs)==len(set(hdrs))
print("   columns:",len(VT.CSV_FIELDS),"| larsen and dh-net are separate: OK")
print("\n=== 7. THE SIGN TRAP: dem_diff returns erosion NEGATIVE ===")
print("   volume_tab must store abs(); a negative would make d_log10 return None")
print("   and the dh side would vanish from the cross-check with no warning.")
from landslide_groundtruth import verdict as V
assert V.d_log10(-1.1e6, 1.3e6) is None, "a negative volume must not compare"
assert V.d_log10(1.1e6, 1.3e6) is not None
src=open(os.path.join(PLUG,"volume_tab.py")).read()
assert '"v_erosion": abs(r["v_erosion"])' in src, "the row must store |erosion|"
assert '"v_best": abs(r["v_erosion"])' in src, "v_best must be |erosion|, not the net"
print("   volume_tab stores abs(erosion) for both v_best and v_erosion: OK")

print("\nFIT-AWARENESS FIX VERIFIED")
