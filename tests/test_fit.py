"""The volume must be read from the field that holds it.

The Volume tab's volume is Larsen et al. (2010): v_larsen* for the best source
outline, v_larsen_srclo / _srchi for the low and high ones. Area × thickness
(v_thick*) was parked on 2026-11-04. The estimates, the CSV and the write-back
must read v_larsen, and nothing may fall back to a shared "best" volume — under
the old one-fit-per-Measure rows that field meant whatever the row's fit made it
mean — or to a stale v_thick from a row measured before the park.

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
for m in ("_estimates","_scar_centroid","_event_row"):
    setattr(Stub,m,getattr(VT.VolumeTab,m))
s=Stub()
det=DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection=det

print("=== a row measured before the park: v_thick is not the volume ===")
s._rows=[{"name":"S1","v_thick":1.1e6,"v_thick_lo":0.7e6,"v_thick_hi":1.5e6,"t_best":2.5}]
l,_=s._estimates()
print(f"   Larsen {l}")
assert l is None, "an area × thickness volume must not be read as Larsen's"

print("\n=== the source scar ran ===")
s._rows=[{"name":"S1","material":"bedrock","v_larsen":1.3e6,"larsen_area_m2":4.2e5}]
l,_=s._estimates()
print(f"   Larsen {l}")
assert l==1.3e6

print("\n=== a shared 'v_best' is never read as the estimate ===")
s._rows=[{"name":"S1","v_best":1.3e6,"a_conv":4.2e5}]     # an old one-fit row
l,_=s._estimates()
assert l is None, l
print("   v_best alone -> no estimate")

print("\n=== best, low and high on one row: each column from its own field ===")
s._rows=[{"name":"S1","material":"bedrock","v_larsen":1.3e6,"v_larsen_lo":0.7e6,
          "v_larsen_hi":2.4e6,"larsen_area_m2":4.2e5,"v_larsen_srclo":0.8e6,
          "v_larsen_srchi":2.1e6,"a_total":1.04e6}]
l,_=s._estimates()
assert l==1.3e6, l
row=s._event_row()
print(f"   Larsen {l:g}  depth {row['implied_depth']}")
assert abs(float(row["implied_depth"])-1.25)<0.01, "depth is scar volume / TOTAL area"
cols=dict(VT.CSV_FIELDS)
assert cols["vol_larsen_m3"]=="v_larsen"
assert cols["vol_larsen_src_low_m3"]=="v_larsen_srclo"
assert cols["vol_larsen_src_high_m3"]=="v_larsen_srchi"
assert "v_best" not in cols.values(), "no CSV column may read a shared best"
assert not [k for k in cols.values() if k.startswith(("v_thick","t_","thick"))], \
    "no area × thickness column after the park"
hdrs=[h for h,_ in VT.CSV_FIELDS]; assert len(hdrs)==len(set(hdrs))
assert hdrs.index("vol_larsen_m3") == 2, "the volume leads, after slide and material"
print("   columns:",len(VT.CSV_FIELDS),"| best, low and high are separate: OK")

print("\n=== the write-back carries Larsen's three, nothing of area × thickness ===")
wb={n:k for n,_t,k in VT.WRITEBACK_FIELDS}
assert wb["vsrc_m3"]=="v_larsen" and wb["vlowsrc_m3"]=="v_larsen_srclo" \
    and wb["vhisrc_m3"]=="v_larsen_srchi"
assert "vthk_m3" not in wb and "thick_m" not in wb
assert {"vthk_m3","thick_m"} <= set(VT.LEGACY_WRITEBACK), \
    "a layer written before the park has them explained, not mistaken for current"
assert "vol_m3" not in wb and "v_best" not in wb.values()
assert all(len(n)<=10 for n in wb), "Shapefile field names are 10 characters"
print("   vsrc_* / vlowsrc_m3 / vhisrc_m3, all <= 10 characters: OK")

print("\nFIT-AWARENESS FIX VERIFIED")
