"""One Measure, one row, every volume.

The Fit combo used to run one calibration per Measure, so comparing area ×
thickness with the source-scar fit meant measuring twice and reading two rows —
and the cross-check only ever saw one of them. Now one Measure runs both and the
row carries both beside the seismic volume, each in its own field and column.

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
    def __init__(s): s.dock = FakeDock(); s._rows = []; s._current = None
    def _role_layer(s, r): return None
    def _append_log(s, t): pass
for m in ("_build_verdict_box", "_estimates", "_scar_centroid",
          "_verdict_row", "_refresh_verdict"):
    setattr(Stub, m, getattr(VT.VolumeTab, m))

s = Stub(); _box = s._build_verdict_box()
det = DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6, loc_error_km=17.0,
                    vol_best_m3=1.3e6, vol_low_m3=0.9e6, vol_high_m3=1.7e6,
                    origin_utc=datetime(2026, 2, 4, 8, 48, 5))
s.dock.detection = det
ROW = {"name": "Iliamna", "material": "bedrock", "material_label": "Bedrock",
       "v_larsen": 1.30e6, "v_larsen_lo": 0.7e6, "v_larsen_hi": 2.4e6,
       "larsen_area_m2": 4.2e5, "src_best": 4.2e5,
       "v_thick": 2.0e6, "v_thick_lo": 1.2e6, "v_thick_hi": 2.8e6,
       "thick_area_m2": 8.0e5, "a_total": 8.0e5, "t_best": 2.5, "t_low": 1.5,
       "t_high": 3.5, "thick_setting": "Glacier, far date (ice melted)"}

print("=== 1. ONE row carrying both estimates ===")
s._rows = [dict(ROW)]
thick, larsen, src = s._estimates()
print(f"   thickness {thick:,.0f}   source scar {larsen:,.0f}   from row {src['name']!r}")
assert thick == 2.0e6 and larsen == 1.30e6
rec = s._refresh_verdict()
assert rec["d_larsen"] is not None and rec["d_thickness"] is not None, rec
print("   both compared from a SINGLE Measure:", rec["agreement"])
print("  ", s.verdict_summary.text().replace("<br>", "\n   ").replace("&nbsp;", " "))

print("\n=== 2. the slide on screen wins over older rows ===")
s._rows = [dict(ROW, name="old", v_larsen=9.9e7), dict(ROW)]
s._current = s._rows[0]
assert s._estimates()[1] == 9.9e7, "the verdict is about the slide on screen"
s._current = None
assert s._estimates()[1] == 1.30e6, "with nothing on screen, the last row"
print("   _current first, else the last row")

print("\n=== 3. implied depth spreads the scar volume over the TOTAL outline ===")
s._rows = [dict(ROW)]
row = s._verdict_row()
print(f"   implied depth {row['implied_depth']} m   (1.3e6 / 8.0e5 = 1.625)")
assert abs(float(row["implied_depth"]) - 1.625) < 0.01, row["implied_depth"]
s._rows = [dict(ROW, a_total=None)]
assert s._verdict_row()["implied_depth"] == "", "no total outline -> no depth, not the scar's"
print("   and none at all without a total outline")

print("\n=== 4. the CSV shows the cross-check on one line ===")
s._rows = [dict(ROW)]
s.verdict_combo.setCurrentIndex(1); s.analyst_edit.setText("EH")
stamp = s._verdict_row()
merged = dict(s._rows[0])
for k, v in stamp.items():
    if merged.get(k) in (None, ""): merged[k] = v
buf = _io.StringIO(); w = csv.writer(buf)
w.writerow([h for h, _k in VT.CSV_FIELDS])
w.writerow(["" if merged.get(k) is None else merged.get(k) for _h, k in VT.CSV_FIELDS])
buf.seek(0); out = list(csv.DictReader(buf))[0]
for c in ("vol_seismic_m3", "vol_thickness_m3", "vol_source_scar_m3",
          "thickness_m", "thickness_setting", "implied_depth_m",
          "agreement_thickness", "agreement_larsen", "agreement", "verdict"):
    print(f"   {c:24s} {out[c]!r}")
    assert out[c] != "", c
vals = {out["vol_seismic_m3"], out["vol_thickness_m3"], out["vol_source_scar_m3"]}
assert len(vals) == 3, "the three volumes must not collapse into one column"
hdrs = [h for h, _ in VT.CSV_FIELDS]
assert len(hdrs) == len(set(hdrs)), "duplicate CSV header"
print(f"   {len(hdrs)} columns, three distinct volumes")

print("\n=== 5. no estimates at all -> no crash, no false agreement ===")
s._rows = []
t, l, src = s._estimates()
assert t is None and l is None and src == {}
assert s._refresh_verdict()["agreement"] == ""
print("   empty table handled")

print("\n=== 6. the labels stopped overclaiming ===")
src = open(os.path.join(PLUG, "volume_tab.py")).read()
assert 'f.addRow("Volume (±1σ)"' not in src, "the sigma is not a prediction interval"
assert "is NOT a prediction interval" in src, "the range tooltip says what it is"
assert '"Implied mean depth"' in src, "V/A must be on screen"
assert 'form.addRow("What failed?"' in src, "the material row asks a user-answerable question"
assert "rock-and-ice avalanche" in src, "the case this plugin is for must be named"
assert "1e6:,.4f} Mm" not in src, "4 decimal places of Mm3 is precision theatre"
print("   the range is labelled as what it is, not ±1σ")
print("   implied mean depth is shown")
print("   'What failed?' + the ice caveat")
print("   volumes print at 3 significant figures")

print("\n=== 7. implied depth catches an outline that is obviously wrong ===")
for area, vol, verdict_ in ((4.2e5, 1.3e6, "plausible"), (2.0e6, 1.3e6, "shallow"),
                            (1.4e4, 1.3e6, "absurd")):
    d = V.implied_depth_m(vol, area)
    print(f"   {vol/1e6:.2g} Mm3 over {area/1e6:.3g} km2 -> {d:6.1f} m  ({verdict_})")
assert V.implied_depth_m(1.3e6, 4.2e5) > 3.0
assert V.implied_depth_m(1.3e6, 1.4e4) > 90.0, "a tiny outline must read as absurd"

print("\n=== 8. verdict colours come from the theme, not literals ===")
assert "#1b7f37" not in src, "hardcoded green failed WCAG on the dark theme"
assert "status_color" in src
from landslide_groundtruth import theme
for k in ("success", "warn", "error"):
    assert theme.status_color(k)
print("   agree/marginal/disagree use theme.status_color")

print("\nONE-ROW CROSS-CHECK VERIFIED")
