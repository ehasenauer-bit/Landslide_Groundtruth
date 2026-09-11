"""One row must be able to hold all three volumes.

The Fit combo runs one calibration per Measure, so a row used to be Larsen OR
∫Δh — and measuring the same slide twice auto-renamed it, so the two
independent estimates landed as "slide 1" and "slide 2" with nothing recording
they were one event. Recording the cross-check destroyed it.

Run with tests/run_all.sh.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)
import csv, io as _io
from qgis.core import QgsApplication
app = QgsApplication([], False)
from landslide_groundtruth import volume_tab as VT, detection as DET, verdict as V
from datetime import datetime

class FakeDock: detection = None
class Stub:
    def __init__(s): s.dock = FakeDock(); s._rows = []
    def _role_layer(s, r): return None
    def _append_log(s, t): pass
for m in ("_build_verdict_box", "_latest_row", "_estimates", "_scar_centroid",
          "_verdict_row", "_refresh_verdict"):
    setattr(Stub, m, getattr(VT.VolumeTab, m))

s = Stub(); _box = s._build_verdict_box()
det = DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6, loc_error_km=17.0,
                    vol_best_m3=1.3e6, vol_low_m3=0.9e6, vol_high_m3=1.7e6,
                    origin_utc=datetime(2026, 2, 4, 8, 48, 5))
s.dock.detection = det

print("=== 1. ONE row carrying both estimates ===")
s._rows = [{"name": "Iliamna", "fit": "scar", "material": "bedrock",
            "v_best": 1.30e6, "v_larsen": 1.30e6, "v_larsen_lo": 0.7e6,
            "v_larsen_hi": 2.4e6, "larsen_area_m2": 4.2e5,
            "v_dh_erosion": 1.1e6, "v_dh_net": 2.0e4, "v_dh_sigma": 9.0e4,
            "a_conv": 4.2e5}]
larsen, eros, src = s._estimates()
print(f"   larsen {larsen:,.0f}   dh erosion {eros:,.0f}   from row {src['name']!r}")
assert larsen == 1.30e6 and eros == 1.1e6
rec = s._refresh_verdict()
assert rec["d_larsen"] is not None and rec["d_dh"] is not None, rec
print("   both compared from a SINGLE Measure:", rec["agreement"])
print("  ", s.verdict_summary.text().replace("<br>", "\n   ").replace("&nbsp;", " "))

print("\n=== 2. the old two-row shape still reconciles (back-compat) ===")
s._rows = [{"name": "S1", "fit": "scar", "material": "bedrock", "v_best": 1.3e6,
            "a_conv": 4.2e5, "src_best": 4.2e5},
           {"name": "S1", "fit": "ddem", "v_best": 1.1e6, "v_net": 2.0e4,
            "v_erosion": 1.1e6, "v_deposit": 1.08e6}]
l2, e2, _ = s._estimates()
print(f"   larsen {l2:,.0f}   dh erosion {e2:,.0f}")
assert l2 == 1.3e6 and e2 == 1.1e6
assert s._refresh_verdict()["d_dh"] is not None

print("\n=== 3. v_best is NOT used as the Larsen estimate ===")
# a ddem-only row whose v_best is |erosion| must not masquerade as area scaling
s._rows = [{"name": "S1", "fit": "ddem", "v_best": 1.1e6, "v_net": 2.0e4,
            "v_dh_erosion": 1.1e6}]
l3, e3, _ = s._estimates()
print(f"   larsen {l3}   dh erosion {e3:,.0f}")
assert l3 is None, "a ddem row must not supply a Larsen volume"
assert e3 == 1.1e6
assert s._refresh_verdict()["d_larsen"] is None

print("\n=== 4. implied depth uses the LARSEN area, not the Δh covered area ===")
s._rows = [{"name": "S1", "fit": "ddem", "v_larsen": 1.3e6, "larsen_area_m2": 4.2e5,
            "v_dh_erosion": 1.1e6, "a_conv": 9.9e9}]   # a_conv here is Δh coverage
row = s._verdict_row()
print(f"   implied depth {row['implied_depth']} m   (1.3e6 / 4.2e5 = 3.10)")
assert abs(float(row["implied_depth"]) - 3.10) < 0.01, row["implied_depth"]

print("\n=== 5. the CSV shows the cross-check on one line ===")
s._rows = [{"name": "Iliamna", "fit": "scar", "material": "bedrock",
            "v_best": 1.30e6, "v_larsen": 1.30e6, "v_larsen_lo": 0.7e6,
            "v_larsen_hi": 2.4e6, "larsen_area_m2": 4.2e5,
            "v_dh_erosion": 1.1e6, "v_dh_net": 2.0e4, "sigma_dh": 0.25,
            "v_sigma": 9.0e4, "dh_offset": 0.5, "dh_coverage": 0.97,
            "a_conv": 4.2e5}]
s.verdict_combo.setCurrentIndex(1); s.analyst_edit.setText("EH")
stamp = s._verdict_row()
merged = dict(s._rows[0])
for k, v in stamp.items():
    if merged.get(k) in (None, ""): merged[k] = v
buf = _io.StringIO(); w = csv.writer(buf)
w.writerow([h for h, _k in VT.CSV_FIELDS])
w.writerow(["" if merged.get(k) is None else merged.get(k) for _h, k in VT.CSV_FIELDS])
buf.seek(0); out = list(csv.DictReader(buf))[0]
for c in ("vol_seismic_m3", "vol_area_scaling_m3", "vol_dh_erosion_row_m3",
          "vol_dh_net_row_m3", "dh_sigma_m", "dh_bias_removed_m",
          "dh_coverage_frac", "implied_depth_m", "agreement", "verdict"):
    print(f"   {c:24s} {out[c]!r}")
    assert out[c] != "", c
vals = {out["vol_seismic_m3"], out["vol_area_scaling_m3"], out["vol_dh_erosion_row_m3"]}
assert len(vals) == 3, "the three volumes must not collapse into one column"
hdrs = [h for h, _ in VT.CSV_FIELDS]
assert len(hdrs) == len(set(hdrs)), "duplicate CSV header"
print(f"   {len(hdrs)} columns, three distinct volumes")

print("\n=== 6. no estimates at all -> no crash, no false agreement ===")
s._rows = []
l, e, src = s._estimates()
assert l is None and e is None and src == {}
assert s._refresh_verdict()["agreement"] == ""
print("   empty table handled")

print("\n=== 7. the labels stopped overclaiming ===")
src = open(os.path.join(PLUG, "volume_tab.py")).read()
assert 'f.addRow("Volume (±1σ)"' not in src, "the sigma is not a prediction interval"
assert '"Likely range"' in src
assert '"Implied mean depth"' in src, "V/A must be on screen"
assert 'form.addRow("What failed?"' in src, "the material row asks a user-answerable question"
assert "rock-and-ice avalanche" in src, "the case this plugin is for must be named"
assert "1e6:,.4f} Mm" not in src, "4 decimal places of Mm3 is precision theatre"
print("   ±1σ -> 'Likely range' with the honest tooltip")
print("   implied mean depth is shown")
print("   'Hillslope material' -> 'What failed?' + the ice caveat")
print("   volumes print at 3 significant figures")

print("\n=== 8. implied depth catches an outline that is obviously wrong ===")
for area, vol, verdict_ in ((4.2e5, 1.3e6, "plausible"), (2.0e6, 1.3e6, "shallow"),
                            (1.4e4, 1.3e6, "absurd")):
    d = V.implied_depth_m(vol, area)
    print(f"   {vol/1e6:.2g} Mm3 over {area/1e6:.3g} km2 -> {d:6.1f} m  ({verdict_})")
assert V.implied_depth_m(1.3e6, 4.2e5) > 3.0
assert V.implied_depth_m(1.3e6, 1.4e4) > 90.0, "a tiny outline must read as absurd"

print("\n=== 9. verdict colours come from the theme, not literals ===")
assert "#1b7f37" not in src, "hardcoded green failed WCAG on the dark theme"
assert "status_color" in src
from landslide_groundtruth import theme
for k in ("success", "warn", "error"):
    assert theme.status_color(k)
print("   agree/marginal/disagree use theme.status_color")

print("\nONE-ROW CROSS-CHECK VERIFIED")
