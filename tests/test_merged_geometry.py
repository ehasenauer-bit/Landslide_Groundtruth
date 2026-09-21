"""A merged asc+desc raster has to survive the trip to the Fusion tab.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_merged_geometry.py

The SAR tab's "Merge geometries (asc + desc)" button produces the one change
product the Fusion tab could not read properly, and it failed in two separate
ways that were both silent:

  * the merge named its detectors "brightness-z" and "MT-corr" — spellings no
    other part of the plugin uses. FusionTab.MEASURE_HINTS knows "brightness z"
    and "MT int-corr", matched neither, and left the measure combo on its default
    log-ratio with a 3.0 floor. MT int-corr values live in 0-1, so that floor
    admits NOTHING and the SAR channel silently contributes nothing at all.

  * the merged raster was written only to tempfile.mkstemp(), never through
    _cd_export_float like every single-geometry product. _sar_siblings globs the
    change folder for the other detectors of the same pair, a temp name matches
    no pattern, and the tab logged "no other detectors found for this pair" and
    fused ONE detector. Measured over six truthed events, restoring the pooling
    moves the scar's worst candidate rank from 7th to 3rd.

What is pinned here: the merged names are the canonical ones, the merged file
lands where the Fusion tab looks, the merged detectors pool with EACH OTHER, and
a merged raster is never pooled with a single-geometry one (their pixels come
from different viewing geometries; the track list in the name keeps them apart).
"""
import os, re, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

from landslide_groundtruth.sar_tab import SarTab
from landslide_groundtruth.fusion_tab import FusionTab, SAR_HINTS, SAR_EXCLUDE

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


def single_label(mkey, track=36):
    """_cd_compute's own labels, for the pairs that must NOT pool together."""
    body = {"logratio": f"log-ratio {PRE}→{POST}",
            "intcorr": f"int-corr 2026-07-26+{PRE}→{POST}",
            "tsint": f"brightness z 3×pre→{POST}",
            "mtcorr": f"MT int-corr 3×pre→{POST}"}[mkey]
    return f"S1 change {body} (t{track} {POL}, {K}×{K})"


def filename(label):
    """Exactly what _cd_export_float writes."""
    return f"{SarTab._safe_name(label)}_{SarTab._cd_settings_tag(META)}.tif"


def detect_measure(label, path):
    """FusionTab._autodetect_measure's matching rule."""
    text = (label + " " + os.path.basename(path)).lower()
    for frag, key in FusionTab.MEASURE_HINTS["sar"]:
        if frag in text:
            return key
    return None


print("=== 1. a merged layer auto-detects its OWN measure, not log-ratio ===")
for mkey in MERGED_NAME:
    lbl = merged_label(mkey)
    got = detect_measure(lbl, filename(lbl))
    check(f"{mkey:8s} {lbl[:46]:46s} -> {got}", got == mkey,
          f"detected {got!r}; the floor would be "
          f"{ {'logratio': 3.0}.get(got, 'its own') } instead of this detector's")

print("\n=== 2. and so does a single-geometry layer (unchanged behaviour) ===")
for mkey in MERGED_NAME:
    lbl = single_label(mkey)
    got = detect_measure(lbl, filename(lbl))
    check(f"{mkey:8s} -> {got}", got == mkey, f"detected {got!r}")

print("\n=== 3. the merged file name carries a sibling key _sar_siblings can read ===")
KEY = re.compile(r"_to_(\d{4}-\d{2}-\d{2})(_t[0-9+]+_[A-Z]{2}_.*\.tif)$")
for mkey in MERGED_NAME:
    fn = filename(merged_label(mkey))
    m = KEY.search(fn)
    check(f"{mkey:8s} {fn}", bool(m), "no _t<tracks>_<POL>_ tail — pooling stays off")
    if m:
        check(f"{mkey:8s} post date {m.group(1)}", m.group(1) == POST)
        check(f"{mkey:8s} names both tracks", TRACKS in m.group(2),
              f"suffix {m.group(2)!r} would pool with single-geometry rasters")

print("\n=== 4. merged rasters are still offered as a SAR input ===")
for mkey in ("logratio", "tsint"):        # the two the hints are meant to catch
    n = merged_label(mkey).lower()
    check(f"{mkey:8s} preselectable",
          any(h in n for h in SAR_HINTS) and not any(x in n for x in SAR_EXCLUDE))


def tiny_tif(path):
    ds = gdal.GetDriverByName("GTiff").Create(path, 8, 8, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-140.0, 0.001, 0, 60.5, 0, -0.001))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.ones((8, 8), dtype=np.float32))
    ds = None


print("\n=== 5. merged pools with merged; never with a single geometry ===")
shim = type("Shim", (), {"SAR_FILE_KIND": FusionTab.SAR_FILE_KIND})()
with tempfile.TemporaryDirectory() as d:
    made = {}
    for mkey in MERGED_NAME:
        for lbl, tag in ((merged_label(mkey), "merged"),
                         (single_label(mkey), "single")):
            p = os.path.join(d, filename(lbl))
            tiny_tif(p)
            made[(tag, mkey)] = p
    got = FusionTab._sar_siblings(shim, made[("merged", "logratio")])
    kinds = sorted(k for _p, k in got)
    check("merged log-ratio finds the other 3 merged detectors",
          kinds == ["intcorr", "mtcorr", "tsint"], f"found {kinds}")
    check("and every one of them is a MERGED file",
          all("_MERGED_" in os.path.basename(p) for p, _k in got),
          [os.path.basename(p) for p, _k in got])
    got1 = FusionTab._sar_siblings(shim, made[("single", "logratio")])
    kinds1 = sorted(k for _p, k in got1)
    check("single-geometry log-ratio still finds its own 3 siblings",
          kinds1 == ["intcorr", "mtcorr", "tsint"], f"found {kinds1}")
    check("and none of THOSE is a merged file",
          all("_MERGED_" not in os.path.basename(p) for p, _k in got1),
          [os.path.basename(p) for p, _k in got1])

print("\n=== 6. the merge's own NAME table, read off the source ===")
# read the literal rather than grepping the file: the comment above it quotes
# the two dead spellings on purpose, and a substring search would find those
import ast
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
    for mkey, spelling in name_map.items():
        lbl = f"S1 change {spelling} MERGED {DIRS} {PRE}→{POST}"
        check(f"{spelling!r} is a spelling MEASURE_HINTS knows",
              detect_measure(lbl, "") == mkey,
              f"detects as {detect_measure(lbl, '')!r}")

print("\n=== 7. the merge still routes through the durable export ===")
# the regression that matters most: if this call goes away the raster is a
# tempfile again and pooling turns off silently, with nothing else to notice it
merge_fn = next((n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_merge_geometries_action"), None)
check("found _merge_geometries_action", merge_fn is not None)
if merge_fn:
    called = {c.func.attr for c in ast.walk(merge_fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    check("it calls _cd_export_float", "_cd_export_float" in called,
          "a merged raster written only to mkstemp cannot be pooled or reloaded")
    check("mkstemp is kept only as a fallback", "mkstemp" in called,
          "the no-output-folder path still needs it")

print("\n=== 8. the heat map drops asc/desc disagreement; the FUSION input does not ===")
# Two different questions, and they were answered differently on purpose.
#
# On the MAP, "strongest anomaly wins" paints a conf-3 contradiction — one orbit
# flags a pixel, the other looked at the same ground and saw nothing — exactly
# like a both-orbits-agree detection, and over ice that is most of the AOI.
#
# In the FUSED SCORE, holding those pixels out was measured over six truthed
# events and REJECTED: conf-3 is enriched inside the scar (mean lift 2.0), and
# dropping it moved scar area 90.0% -> 80.5% and the worst candidate rank 3 -> 7.
#
# So merge_geometries must keep returning the full max-pool, and the filtering
# must live in a separate display-only copy. Pinned here because the cheap
# "fix" — suppressing inside merge_geometries — silently undoes that benchmark.
from landslide_groundtruth import sar_change

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
    check(f"fused in  {name:40s} -> {float(merged[0, i])}",
          same(float(merged[0, i]), want_full), f"expected {want_full}")
    check(f"map shows {name:40s} -> {float(shown[0, i])}",
          same(float(shown[0, i]), want_shown), f"expected {want_shown}")

check(f"agreeing_only reports what it hid ({n_hidden})",
      n_hidden == 2 == meta["n_conflict"], f"{n_hidden} vs {meta['n_conflict']}")
check("it is a copy — the fusion array is not mutated",
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
check("int-corr: contradicted pixel STAYS in the fusion raster",
      abs(float(m2[0, 1]) - 0.9) < 1e-6, float(m2[0, 1]))
check("int-corr: the recovery survives both", abs(float(s2[0, 2]) - 0.9) < 1e-6)

# a single geometry has nothing to disagree WITH — the display copy must not
# quietly erase the only detector the merge has
solo = np.array([[-5.0, 0.2, N]], dtype=np.float32)
m3, c3, meta3 = sar_change.merge_geometries([solo], "logratio", THR)
s3, n3 = sar_change.agreeing_only(m3, c3)
check("single geometry: nothing is hidden", n3 == 0 and meta3["n_conflict"] == 0)
check("single geometry: the anomaly survives", abs(float(s3[0, 0]) + 5.0) < 1e-6)

print("\n=== 9. the display copy can never be MISTAKEN for the fusion input ===")
# It carries a detector name, so SAR_HINTS matches it; it shares the whole
# _to_<post>_t<tracks>_<POL>_ tail, so the sibling glob would match it. Both
# would feed the map's filtered pixels to the score that was measured to need
# the unfiltered ones.
DISPLAY = {mkey: (f"S1 MERGED agreeing only {MERGED_NAME[mkey]} {DIRS} "
                  f"{PRE}\u2192{POST} (t{TRACKS} {POL}, {K}\u00d7{K})")
           for mkey in MERGED_NAME}
CONF_LABEL = (f"S1 MERGED confidence {DIRS} {PRE}\u2192{POST} "
              f"(t{TRACKS} {POL}, {K}\u00d7{K})")
for mkey, lbl in DISPLAY.items():
    fn = filename(lbl)
    check(f"{mkey:8s} display file is not a change raster: {fn[:48]}",
          not fn.startswith("S1_change_"),
          "_sar_siblings globs S1_change_* and would pool it with its own source")
    check(f"{mkey:8s} display copy is never auto-preselected",
          any(x in lbl.lower() for x in SAR_EXCLUDE),
          "SAR_EXCLUDE must carry an 'agreeing only' marker")
    # the full raster must stay eligible. Only log-ratio and brightness z are in
    # SAR_HINTS (int-corr / MT int-corr never were — section 4), so what every
    # detector has to clear is the EXCLUDE list: the new "agreeing only" marker
    # must catch the display copy without catching the raster beside it.
    check(f"{mkey:8s} the FULL merged raster is not excluded",
          not any(x in merged_label(mkey).lower() for x in SAR_EXCLUDE),
          "the raster fusion must use became unselectable")
check("the confidence raster is not a change raster either",
      not filename(CONF_LABEL).startswith("S1_change_"))

with tempfile.TemporaryDirectory() as d:
    for mkey in MERGED_NAME:
        for lbl in (merged_label(mkey), DISPLAY[mkey]):
            tiny_tif(os.path.join(d, filename(lbl)))
    tiny_tif(os.path.join(d, filename(CONF_LABEL)))
    got = FusionTab._sar_siblings(
        shim, os.path.join(d, filename(merged_label("logratio"))))
    check("pooling still finds exactly the 3 other FULL detectors",
          sorted(k for _p, k in got) == ["intcorr", "mtcorr", "tsint"],
          sorted(k for _p, k in got))
    check("and pools no display copy",
          not any("agreeing" in os.path.basename(p).lower() for p, _k in got),
          [os.path.basename(p) for p, _k in got])

fsrc = open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth",
                         "fusion_tab.py")).read()
check("the fusion tab refuses the confidence raster outright",
      '"merged_confidence" in ' in fsrc)
check("and says so in the log when the display copy is hand-picked",
      '"agreeing_only" in ' in fsrc)

print("\n=== 10. the merge writes all three, and only filters the display one ===")
msrc = next((n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef)
             and n.name == "_merge_geometries_action"), None)
called = {c.func.attr for c in ast.walk(msrc)
          if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
check("it calls agreeing_only", "agreeing_only" in called,
      "the heat map would show the disagreements again")
n_exports = sum(1 for c in ast.walk(msrc)
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
      "filtering there would reach fusion and undo the 6-event benchmark")

print()
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("MERGED-GEOMETRY HANDOFF TO FUSION VERIFIED")
