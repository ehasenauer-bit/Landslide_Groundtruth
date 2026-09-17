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

print()
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("MERGED-GEOMETRY HANDOFF TO FUSION VERIFIED")
