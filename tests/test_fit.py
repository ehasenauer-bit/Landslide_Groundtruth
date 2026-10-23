"""The volume cross-check must read each estimate from the field that holds it.

One Measure runs both area-based fits, and neither is the headline: area ×
thickness lives in v_thick*, the source-scar fit in v_larsen*. The cross-check,
the CSV and the write-back must each read the one they mean, and nothing may
fall back to a shared "best" volume — under the old one-fit-per-Measure rows that
field meant whatever the row's fit made it mean, which is how a net ∫Δh once got
compared with a seismic inversion as if it were area scaling.

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
for m in ("_build_verdict_box","_estimates","_scar_centroid","_verdict_row","_refresh_verdict"):
    setattr(Stub,m,getattr(VT.VolumeTab,m))
s=Stub(); _box=s._build_verdict_box()  # keep a ref: Qt deletes children otherwise
det=DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6, loc_error_km=17.0,
                  vol_best_m3=1.3e6,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection=det

print("=== only area × thickness ran (no source outline) ===")
s._rows=[{"name":"S1","v_thick":1.1e6,"v_thick_lo":0.7e6,"v_thick_hi":1.5e6,"t_best":2.5}]
rec=s._refresh_verdict()
print("  ", s.verdict_summary.text().replace("<br>","\n   ").replace("&nbsp;"," "))
assert rec["d_larsen"] is None, "no source-scar volume must be invented"
assert rec["d_thickness"] is not None and rec["agreement"]=="agree"

print("\n=== only the source scar ran ===")
s._rows=[{"name":"S1","material":"bedrock","v_larsen":1.3e6,"larsen_area_m2":4.2e5}]
rec=s._refresh_verdict()
print("   d_larsen=%.3f d_thickness=%s -> %s"%(rec["d_larsen"],rec["d_thickness"],rec["agreement"]))
assert rec["d_thickness"] is None and rec["agreement"]=="agree"

print("\n=== a shared 'v_best' is never read as either estimate ===")
s._rows=[{"name":"S1","v_best":1.3e6,"a_conv":4.2e5}]     # an old one-fit row
t,l,_=s._estimates()
assert t is None and l is None, (t, l)
assert s._refresh_verdict()["agreement"]==""
print("   v_best alone -> no estimate, no false agreement")

print("\n=== both on one row: each column from its own field ===")
s._rows=[{"name":"S1","material":"bedrock","v_larsen":1.3e6,"v_larsen_lo":0.7e6,
          "v_larsen_hi":2.4e6,"larsen_area_m2":4.2e5,"v_thick":2.6e6,
          "v_thick_lo":1.6e6,"v_thick_hi":3.6e6,"t_best":2.5,"a_total":1.04e6}]
row=s._verdict_row()
print(f"   D thickness {row['d_thickness']}  D larsen {row['d_larsen']}  "
      f"depth {row['implied_depth']}")
assert row["d_larsen"]=="0.000" and abs(float(row["d_thickness"])-0.301)<0.001
assert abs(float(row["implied_depth"])-1.25)<0.01, "depth is scar volume / TOTAL area"
cols=dict(VT.CSV_FIELDS)
assert cols["vol_thickness_m3"]=="v_thick" and cols["vol_source_scar_m3"]=="v_larsen"
assert "v_best" not in cols.values(), "no CSV column may read a shared best"
hdrs=[h for h,_ in VT.CSV_FIELDS]; assert len(hdrs)==len(set(hdrs))
print("   columns:",len(VT.CSV_FIELDS),"| thickness and scar are separate: OK")

print("\n=== the write-back names both, and neither as 'the' volume ===")
wb={n:k for n,_t,k in VT.WRITEBACK_FIELDS}
assert wb["vthk_m3"]=="v_thick" and wb["vsrc_m3"]=="v_larsen"
assert "vol_m3" not in wb and "v_best" not in wb.values()
assert all(len(n)<=10 for n in wb), "Shapefile field names are 10 characters"
print("   vthk_* / vsrc_*, all <= 10 characters: OK")

print("\nFIT-AWARENESS FIX VERIFIED")
