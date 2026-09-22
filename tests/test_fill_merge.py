"""The fill-only merge: one pass leads, the other only fills where it was blind.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_fill_merge.py

merge_geometries keeps the LOUDER pass at every pixel, so it inherits both
passes' false change: on a quiet real Iliamna pair (2026-08-31 -> 09-12, no
slide) it read 17.5% of the AOI as a >=3 dB change where ascending alone read
5.7% and descending 14.3%. fill_geometries never compares two samples at a
pixel: the primary pass everywhere it can see, the other only where the primary
is blind (outside its frame, shadow, layover). On that pair it read 5.5% with
ascending leading, 14.2% with descending — so the primary choice is most of the
result, and choose_primary picks the QUIETER pass, on ground both can see.

The price is pinned too: a change only the non-primary pass registered, at a
pixel the primary could see, is not rescued — the stronger-wins merge keeps it.
That is why stronger-wins stays the default until fill-only is benchmarked on
the truthed events.
"""
import ast, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

from landslide_groundtruth import sar_change
from landslide_groundtruth.sar_tab import SarTab
from landslide_groundtruth.fusion_tab import FusionTab, SAR_HINTS, SAR_EXCLUDE

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


N = np.nan
print("=== 1. the primary where it can see, the other only where it cannot ===")
#                 primary is ...                 asc    desc   asc blind?  desc blind?
CASES = [("seeing, other seeing too",            -1.0,  -6.0,  False, False),
         ("seeing, other blind",                  0.4,   N,    False, False),
         ("outside its frame (NaN)",              N,    -5.0,  False, False),
         ("in shadow/layover (masked)",           9.0,  -4.0,  True,  False),
         ("blind, and so is the other",           2.0,   3.0,  True,  True),
         ("outside every frame",                  N,     N,    False, False)]
asc = np.array([[c[1] for c in CASES]], np.float32)
desc = np.array([[c[2] for c in CASES]], np.float32)
m_a = np.array([[c[3] for c in CASES]], bool)
m_d = np.array([[c[4] for c in CASES]], bool)
a0, d0 = asc.copy(), desc.copy()
f, src, fm = sar_change.fill_geometries([asc, desc], 0, masks=[m_a, m_d])
#        value   source
WANT = [(-1.0,   1.0),     # the primary's -1, NOT the louder -6: no comparison
        ( 0.4,   1.0),
        (-5.0,   2.0),     # filled
        (-4.0,   2.0),     # filled: the primary's 9.0 was a blind sample
        (   N,     N),
        (   N,     N)]
for i, (name, *_r) in enumerate(CASES):
    wv, ws = WANT[i]
    gv, gs = float(f[0, i]), float(src[0, i])
    # float32 storage: 0.4 comes back as 0.40000000596..., so compare to 1e-6
    ok = (np.isnan(gv) if np.isnan(wv) else abs(gv - wv) < 1e-6) and \
         (np.isnan(gs) if np.isnan(ws) else gs == ws)
    check(f"{name:30s} -> {gv:5.1f} (source {gs})", ok, f"expected {wv} / {ws}")
check(f"meta: {fm['n_primary']} primary, {fm['n_filled']} filled, "
      f"{fm['n_blind']} blind, {fm['n_nodata']} outside every frame",
      (fm["n_primary"], fm["n_filled"], fm["n_blind"], fm["n_nodata"]) == (2, 2, 1, 1))
check("inputs are not modified", np.array_equal(asc, a0, equal_nan=True)
      and np.array_equal(desc, d0, equal_nan=True))
f2, src2, _ = sar_change.fill_geometries([asc, desc], 1, masks=[m_a, m_d])
check("with descending leading, the first pixel is descending's -6",
      float(f2[0, 0]) == -6.0 and float(src2[0, 0]) == 1.0)
f3, _s3, _ = sar_change.fill_geometries([asc, desc], 0)
check("without masks, a masked-only-in-intent sample is used (9.0)",
      float(f3[0, 3]) == 9.0)
try:
    sar_change.fill_geometries([asc, desc], 2)
    check("a primary that is not one of the maps is rejected", False)
except ValueError:
    check("a primary that is not one of the maps is rejected", True)
# three passes: fill order is by how much each can see
p0 = np.array([[N, N, 1.0]], np.float32)
p1 = np.array([[N, 7.0, N]], np.float32)       # sees 1 pixel
p2 = np.array([[5.0, 6.0, N]], np.float32)     # sees 2 pixels -> fills first
f4, s4, _ = sar_change.fill_geometries([p0, p1, p2], 0)
check("three passes: the better-covered one fills first",
      f4.tolist() == [[5.0, 6.0, 1.0]] and s4.tolist() == [[2.0, 2.0, 1.0]], f4.tolist())

print("\n=== 2. the point of it: the background stays at the primary's level ===")
rng = np.random.default_rng(7)
quiet = rng.normal(0, 1.2, (400, 400)).astype(np.float32)   # ~1.2% over 3 dB
noisy = rng.normal(0, 2.0, (400, 400)).astype(np.float32)   # ~13% over 3 dB
bg = lambda m: float(np.mean(np.abs(m[np.isfinite(m)]) >= 3.0))
mx, _c, _m = sar_change.merge_geometries([quiet, noisy], "logratio", 3.0)
fq, _s, _ = sar_change.fill_geometries([quiet, noisy], 0)
fn, _s, _ = sar_change.fill_geometries([quiet, noisy], 1)
print(f"   quiet alone {bg(quiet):.1%} | noisy alone {bg(noisy):.1%} | stronger-wins "
      f"{bg(mx):.1%} | fill, quiet leads {bg(fq):.1%} | fill, noisy leads {bg(fn):.1%}")
check("stronger-wins is noisier than EITHER pass alone", bg(mx) > bg(noisy) > bg(quiet))
check("fill-only with the quiet pass leading keeps the quiet pass's background",
      bg(fq) == bg(quiet))
check("...so which pass leads is most of the result", bg(fn) > 5 * bg(fq))

print("\n=== 3. the price, stated plainly ===")
# the primary looked at this pixel and saw nothing; the other pass saw a scar
pa = np.array([[0.3]], np.float32)
pb = np.array([[-6.0]], np.float32)
mx1, _c, _m = sar_change.merge_geometries([pa, pb], "logratio", 3.0)
ff1, _s, _ = sar_change.fill_geometries([pa, pb], 0)
check(f"stronger-wins keeps the scar the other pass saw ({float(mx1[0, 0]):.1f} dB)",
      float(mx1[0, 0]) == -6.0)
check(f"fill-only does NOT ({float(ff1[0, 0]):.1f} dB) — why it is not the default",
      abs(float(ff1[0, 0]) - 0.3) < 1e-6)

print("\n=== 4. choosing the primary ===")
idx, meta = sar_change.choose_primary([quiet, noisy], "logratio")
check(f"the quieter pass leads (median |change| {meta['noise'][0]:.2f} vs "
      f"{meta['noise'][1]:.2f}): {meta['reason']}", idx == 0)
# Knik: the ascending frame covered 8.6% of the AOI and was quiet BECAUSE it
# was mostly missing. It must not lead.
knik = np.full((400, 400), np.nan, np.float32)
knik[:, :34] = rng.normal(0, 0.5, (400, 34))
idx, meta = sar_change.choose_primary([knik, noisy], "logratio")
check(f"a very quiet pass that sees only {meta['coverage'][0]:.0%} of the AOI does "
      f"not lead", idx == 1, meta["reason"])
tiny = [np.where(np.arange(400)[None, :] < 40, noisy, np.nan).astype(np.float32),
        np.where(np.arange(400)[None, :] < 120, quiet, np.nan).astype(np.float32)]
idx, meta = sar_change.choose_primary(tiny, "logratio")
check("if no pass sees half the AOI, the best-covered one leads",
      idx == 1 and "best-covered" in meta["reason"], meta["reason"])
# fairness: B looks quieter on its own only because it also covers calm ground
# that A cannot see. On the ground both see, A is quieter, and A must lead.
A = np.full((400, 400), np.nan, np.float32); A[:, :240] = rng.normal(0, 1.0, (400, 240))
B = np.zeros((400, 400), np.float32);        B[:, :240] = rng.normal(0, 1.5, (400, 240))
B[:, 240:] = rng.normal(0, 0.05, (400, 160))
own = [float(np.nanmedian(np.abs(x))) for x in (A, B)]
idx, meta = sar_change.choose_primary([A, B], "logratio")
check(f"compared on common ground: A leads (A {meta['noise'][0]:.2f} vs B "
      f"{meta['noise'][1]:.2f}), though B's own-area median is lower "
      f"({own[1]:.2f} < {own[0]:.2f})", idx == 0 and own[1] < own[0])
# unsigned detectors: larger means more change, so the quiet one has the LOWER value
c_lo = rng.uniform(0.0, 0.3, (200, 200)).astype(np.float32)
c_hi = rng.uniform(0.2, 0.6, (200, 200)).astype(np.float32)
idx, _m = sar_change.choose_primary([c_hi, c_lo], "intcorr")
check("int-corr: the pass with less correlation loss leads", idx == 1)
# masks count as not-seeing
mask_most = np.zeros((400, 400), bool); mask_most[:, 60:] = True
idx, meta = sar_change.choose_primary([quiet, noisy], "logratio", masks=[mask_most, None])
check(f"a pass mostly in shadow/layover sees only {meta['coverage'][0]:.0%} and "
      f"does not lead", idx == 1)

print("\n=== 5. filled rasters reach the Fusion tab apart from merged ones ===")
NAME = {"logratio": "log-ratio", "intcorr": "int-corr",
        "tsint": "brightness z", "mtcorr": "MT int-corr"}
PRE, POST, TR, POL, K = "2026-08-31", "2026-09-12", "36+131", "VV", 7
META = dict(radius=10.0, eff_res=20.0, res=20, speckle=("lee", 5),
            radionorm=True, min_area=8)


def filled_label(mkey, pdir="asce", odir="desc"):
    """What _emit_filled names the filled raster."""
    return (f"S1 change {NAME[mkey]} FILLED {pdir} from {odir} {PRE}→{POST} "
            f"(t{TR} {POL} fill-{pdir}, {K}×{K})")


def merged_label(mkey):
    return f"S1 change {NAME[mkey]} MERGED asce+desc {PRE}→{POST} (t{TR} {POL}, {K}×{K})"


def source_label(pdir="asce", odir="desc"):
    return f"S1 FILLED source {pdir} from {odir} {PRE}→{POST} (t{TR} {POL} fill-{pdir}, {K}×{K})"


fname = lambda lbl: f"{SarTab._safe_name(lbl)}_{SarTab._cd_settings_tag(META)}.tif"


def detect(label):
    text = (label + " " + fname(label)).lower()
    for frag, key in FusionTab.MEASURE_HINTS["sar"]:
        if frag in text:
            return key


for mkey in NAME:
    check(f"{mkey:8s} {fname(filled_label(mkey))[:64]}… detects as its own measure",
          detect(filled_label(mkey)) == mkey, detect(filled_label(mkey)))
for mkey in ("logratio", "tsint"):
    lbl = filled_label(mkey).lower()
    check(f"{mkey:8s} filled raster is offered as a SAR input",
          any(h in lbl for h in SAR_HINTS) and not any(x in lbl for x in SAR_EXCLUDE))
src_fn = fname(source_label())
check(f"source map {src_fn[:40]}… is not a change raster (never pooled)",
      not src_fn.startswith("S1_change_"))
check("source map is never preselected",
      not any(h in source_label().lower() for h in SAR_HINTS))
fsrc = open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth", "fusion_tab.py")).read()
check("and the Fusion tab refuses it by hand, too", '"filled_source" in ' in fsrc)


def tiny_tif(path):
    ds = gdal.GetDriverByName("GTiff").Create(path, 8, 8, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-153.2, 0.001, 0, 60.1, 0, -0.001))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.ones((8, 8), np.float32))
    ds = None


shim = type("Shim", (), {"SAR_FILE_KIND": FusionTab.SAR_FILE_KIND})()
with tempfile.TemporaryDirectory() as d:
    made = {}
    for mkey in NAME:
        for tag, lbl in (("fa", filled_label(mkey)),
                         ("fd", filled_label(mkey, "desc", "asce")),
                         ("mg", merged_label(mkey))):
            made[(tag, mkey)] = os.path.join(d, fname(lbl))
            tiny_tif(made[(tag, mkey)])
    tiny_tif(os.path.join(d, src_fn))
    got = FusionTab._sar_siblings(shim, made[("fa", "logratio")])
    names = [os.path.basename(p) for p, _k in got]
    check("a filled log-ratio pools with the other 3 filled detectors",
          sorted(k for _p, k in got) == ["intcorr", "mtcorr", "tsint"], names)
    check("all led by the SAME pass", all("fill-asce" in n for n in names), names)
    check("never with a strongest-wins MERGED raster",
          not any("_MERGED_" in n for n in names), names)
    got_m = FusionTab._sar_siblings(shim, made[("mg", "logratio")])
    check("and a MERGED raster never pools a FILLED one",
          not any("FILLED" in os.path.basename(p) for p, _k in got_m))

print("\n=== 6. the tab: one primary per Merge, decided before the detectors ===")
tsrc = open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth", "sar_tab.py")).read()
tree = ast.parse(tsrc)
fn_ = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
action = fn_.get("_merge_geometries_action")
# the PER-DETECTOR loop, `for mkey, sel in by_prod.items()` — not the first `for`
# in the function, which groups the results (an earlier version of this check
# picked that one and passed for the wrong reason)
loop = next((n for n in ast.walk(action) if isinstance(n, ast.For)
             and isinstance(n.iter, ast.Call)
             and getattr(n.iter.func, "attr", "") == "items"
             and getattr(n.iter.func.value, "id", "") == "by_prod"), None)
check("found the per-detector loop", loop is not None)
in_loop = {id(c) for c in ast.walk(loop)} if loop else set()
prim_calls = [c for c in ast.walk(action) if isinstance(c, ast.Call)
              and getattr(c.func, "attr", "") == "_fill_primary"]
check("the primary is chosen once, OUTSIDE the per-detector loop",
      prim_calls and all(id(c) not in in_loop for c in prim_calls),
      "per-detector primaries would put the cross-pass max back at Fusion's pooling")
emit = [c for c in ast.walk(loop) if isinstance(c, ast.Call)
        and getattr(c.func, "attr", "") == "_emit_filled"] if loop else []
check("each detector is filled with that one primary", bool(emit))
calls = lambda f: {c.func.attr for c in ast.walk(fn_[f]) if isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Attribute)}
check("_fill_primary asks choose_primary", "choose_primary" in calls("_fill_primary"))
check("_emit_filled fills, and exports durably",
      {"fill_geometries", "_cd_export_float"} <= calls("_emit_filled"))
n_exp = sum(1 for c in ast.walk(fn_["_emit_filled"]) if isinstance(c, ast.Call)
            and getattr(c.func, "attr", "") == "_cd_export_float")
check(f"two durable rasters per detector (filled + source): {n_exp}", n_exp == 2)
check("stronger-wins stays the default rule",
      tsrc.index('"max")') < tsrc.index('"fill")'),
      "the first item of the rule combo must be the benchmarked rule")

print()
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("FILL-ONLY MERGE VERIFIED")
