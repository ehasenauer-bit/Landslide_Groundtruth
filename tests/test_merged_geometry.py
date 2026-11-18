"""A merged asc+desc raster: canonical names, a durable file, and a heat map that
drops disagreement only for display.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_merged_geometry.py

The SAR tab's "Merge geometries (asc + desc)" button once failed in two ways
that were both silent:

  * the merge named its detectors "brightness-z" and "MT-corr" — spellings no
    other part of the plugin uses — so a merged layer did not read like the
    single-geometry layers it was made from.

  * the merged raster was written only to tempfile.mkstemp(), never through
    _cd_export_float like every single-geometry product, so the layer went stale
    when the temp dir was swept.

What is pinned here: the merged names are the canonical ones, the merged file
name carries every track that went in (so it is never confused with a
single-geometry file), the merge writes durably, and the disagreement filter
touches only the display copy, never the full merged raster.
"""
import ast, os, re, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

import numpy as np

from landslide_groundtruth import sar_change
from landslide_groundtruth.sar_tab import SarTab

# what _merge_geometries_action and _cd_compute each call their detectors
MERGED_NAME = {"logratio": "log-ratio", "intcorr": "int-corr",
               "tsint": "brightness z", "mtcorr": "MT int-corr"}
PRE, POST, DIRS, TRACKS, POL, K = "2026-08-07", "2026-08-19", "asce+desc", "36+131", "VV", 7
META = dict(radius=8.0, eff_res=20.0, res=20, speckle=("lee", 5),
            radionorm=True, min_area=8)

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def merged_label(mkey):
    return (f"S1 change {MERGED_NAME[mkey]} MERGED {DIRS} {PRE}→{POST} "
            f"(t{TRACKS} {POL}, {K}×{K})")


def filename(label):
    """Exactly what _cd_export_float writes."""
    return f"{SarTab._safe_name(label)}_{SarTab._cd_settings_tag(META)}.tif"


print("=== 1. the merged file name carries the post date and every track ===")
KEY = re.compile(r"_to_(\d{4}-\d{2}-\d{2})(_t[0-9+]+_[A-Z]{2}_.*\.tif)$")
for mkey in MERGED_NAME:
    fn = filename(merged_label(mkey))
    m = KEY.search(fn)
    check(f"{mkey:8s} {fn}", bool(m), "no _t<tracks>_<POL>_ tail")
    if m:
        check(f"{mkey:8s} post date {m.group(1)}", m.group(1) == POST)
        check(f"{mkey:8s} names both tracks", TRACKS in m.group(2),
              f"suffix {m.group(2)!r} reads like a single-geometry raster")

print("\n=== 2. the merge's own NAME table, read off the source ===")
# read the literal rather than grepping the file: the comment above it quotes
# the two dead spellings on purpose, and a substring search would find those
src = open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth",
                        "sar_tab.py")).read()
name_map = None
for node in ast.walk(ast.parse(src)):
    if (isinstance(node, ast.FunctionDef)
            and node.name == "_merge_geometries_action"):
        for stmt in ast.walk(node):
            if (isinstance(stmt, ast.Assign)
                    and getattr(stmt.targets[0], "id", "") == "NAME"):
                name_map = ast.literal_eval(stmt.value)
check("found the merge's NAME table", name_map is not None)
if name_map:
    check(f"it is {name_map}", name_map == MERGED_NAME,
          f"expected {MERGED_NAME}")

print("\n=== 3. the merge still routes through the durable export ===")
# if this call goes away the raster is a tempfile again and the layer goes stale
# when the temp dir is swept, with nothing else to notice it
merge_fn = next((n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_merge_geometries_action"), None)
check("found _merge_geometries_action", merge_fn is not None)
if merge_fn:
    called = {c.func.attr for c in ast.walk(merge_fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    check("it calls _cd_export_float", "_cd_export_float" in called,
          "a merged raster written only to mkstemp cannot be reloaded")
    check("mkstemp is kept only as a fallback", "mkstemp" in called,
          "the no-output-folder path still needs it")

print("\n=== 4. the heat map drops asc/desc disagreement; the full raster does not ===")
# Two different questions, and they were answered differently on purpose.
#
# On the MAP, "strongest anomaly wins" paints a conf-3 contradiction — one orbit
# flags a pixel, the other looked at the same ground and saw nothing — exactly
# like a both-orbits-agree detection, and over ice that is most of the AOI.
#
# In the full merged raster, holding those pixels out was measured over six
# truthed events and REJECTED: conf-3 is enriched inside the scar (mean lift
# 2.0), and dropping it moved scar area 90.0% -> 80.5% and the worst candidate
# rank 3 -> 7 in the optical+SAR score of the (since removed) Fusion tab.
#
# So merge_geometries must keep returning the full max-pool, and the filtering
# must live in a separate display-only copy. Pinned here because the cheap
# "fix" — suppressing inside merge_geometries — silently undoes that benchmark.
THR = 3.0                                  # the log-ratio significance, = SIG
N = np.nan
#          asc   desc   conf  full merged (UNCHANGED)   shown on the map
CASES = [
    ("agreement (both orbits, same sign)", -5.0, -4.0, 2.0, -5.0, -5.0),
    ("layover recovery (other orbit blind)", -5.0, N, 1.0, -5.0, -5.0),
    ("contradicted (other orbit saw nothing)", -5.0, 0.2, 3.0, -5.0, N),
    ("sign conflict (deposit vs scar)", 5.0, -5.0, 3.0, 5.0, N),
    ("background (neither orbit)", 0.4, -0.3, 0.0, 0.4, 0.4),
    ("no data at all", N, N, N, N, N),
]
asc = np.array([[c[1] for c in CASES]], dtype=np.float32)
desc = np.array([[c[2] for c in CASES]], dtype=np.float32)
merged, conf, meta = sar_change.merge_geometries([asc, desc], "logratio", THR)
shown, n_hidden = sar_change.agreeing_only(merged, conf)


def same(got, want):
    return np.isnan(got) if np.isnan(want) else abs(got - want) < 1e-6


for i, (name, _a, _d, want_conf, want_full, want_shown) in enumerate(CASES):
    got_c = float(conf[0, i])
    check(f"conf      {name:40s} -> {got_c}",
          np.isnan(got_c) if np.isnan(want_conf) else got_c == want_conf,
          f"expected {want_conf}")
    check(f"full      {name:40s} -> {float(merged[0, i])}",
          same(float(merged[0, i]), want_full), f"expected {want_full}")
    check(f"map shows {name:40s} -> {float(shown[0, i])}",
          same(float(shown[0, i]), want_shown), f"expected {want_shown}")

check(f"agreeing_only reports what it hid ({n_hidden})",
      n_hidden == 2 == meta["n_conflict"], f"{n_hidden} vs {meta['n_conflict']}")
check("it is a copy — the full merged array is not mutated",
      same(float(merged[0, 2]), -5.0) and same(float(merged[0, 3]), 5.0),
      "agreeing_only wrote through to its input")
check("nothing but conf-3 is hidden",
      np.array_equal(np.isnan(shown) & ~np.isnan(merged), conf == 3.0))
check("agreement and recovery survive on the map",
      meta["n_agree"] == 1 and meta["n_single"] == 1 and same(float(shown[0, 1]), -5.0))

# the same rule for an unsigned detector, which has no sign to clash
ia = np.array([[0.9, 0.9, 0.9, 0.05]], dtype=np.float32)
id_ = np.array([[0.8, 0.05, N, 0.04]], dtype=np.float32)
m2, c2, _meta2 = sar_change.merge_geometries([ia, id_], "intcorr", 0.3)
s2, _n2 = sar_change.agreeing_only(m2, c2)
check("int-corr: contradicted pixel leaves the map", np.isnan(s2[0, 1]),
      float(s2[0, 1]))
check("int-corr: contradicted pixel STAYS in the full merged raster",
      abs(float(m2[0, 1]) - 0.9) < 1e-6, float(m2[0, 1]))
check("int-corr: the recovery survives both", abs(float(s2[0, 2]) - 0.9) < 1e-6)

# a single geometry has nothing to disagree WITH — the display copy must not
# quietly erase the only detector the merge has
solo = np.array([[-5.0, 0.2, N]], dtype=np.float32)
m3, c3, meta3 = sar_change.merge_geometries([solo], "logratio", THR)
s3, n3 = sar_change.agreeing_only(m3, c3)
check("single geometry: nothing is hidden", n3 == 0 and meta3["n_conflict"] == 0)
check("single geometry: the anomaly survives", abs(float(s3[0, 0]) + 5.0) < 1e-6)

print("\n=== 5. the merge writes all three, and only filters the display one ===")
called = {c.func.attr for c in ast.walk(merge_fn)
          if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
check("it calls agreeing_only", "agreeing_only" in called,
      "the heat map would show the disagreements again")
n_exports = sum(1 for c in ast.walk(merge_fn)
                if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") == "_cd_export_float")
check(f"it durably exports {n_exports} rasters (full, confidence, display)",
      n_exports == 3, "a display-only copy in mkstemp would go stale")
mg = next((n for n in ast.walk(ast.parse(
    open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth",
                      "sar_change.py")).read()))
    if isinstance(n, ast.FunctionDef) and n.name == "merge_geometries"), None)
check("merge_geometries itself does NOT filter",
      not any(getattr(c.func, "id", "") == "agreeing_only"
              for c in ast.walk(mg) if isinstance(c, ast.Call)),
      "filtering there would reach the full raster and undo the 6-event benchmark")

print()
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("MERGED-GEOMETRY OUTPUTS VERIFIED")
