"""Tests for the Fusion tab's computation modules.

Two halves, because the code deliberately splits that way:

  * fusion_core is pure numpy and runs anywhere numpy does — the venv is fine:
        ./venv/bin/python test_fusion.py
  * fusion_grid / fusion_cloud / fusion_glacier need GDAL, which lives only
    inside QGIS on this machine. To run those too:
        PYTHONHOME=/Applications/QGIS-LTR.app/Contents/Frameworks \
        PROJ_DATA=/Applications/QGIS-LTR.app/Contents/Resources/qgis/proj \
        GDAL_DATA=/Applications/QGIS-LTR.app/Contents/Resources/qgis/gdal \
        /Applications/QGIS-LTR.app/Contents/MacOS/python3.12 test_fusion.py
    Without osgeo the GDAL half is skipped rather than failed.

The modules are loaded by path into a synthetic 'lgt' package so the relative
imports inside them resolve without importing the QGIS plugin itself (which
would pull in qgis.PyQt).
"""
import importlib.util
import os
import sys
import tempfile
import types

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "qgis_plugin", "landslide_groundtruth")

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def load(*names):
    pkg = types.ModuleType("lgt")
    pkg.__path__ = [ROOT]
    sys.modules.setdefault("lgt", pkg)
    out = []
    for n in names:
        key = f"lgt.{n}"
        if key not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                key, os.path.join(ROOT, f"{n}.py"))
            m = importlib.util.module_from_spec(spec)
            sys.modules[key] = m
            spec.loader.exec_module(m)
        out.append(sys.modules[key])
    return out


fc, = load("fusion_core")
print("=== fusion_core (pure numpy) ===")
print("orient_evidence")
check("dndsi decrease -> positive", fc.orient_evidence(np.array([-0.4]), "dndsi")[0] > 0)
check("logratio negative -> positive", fc.orient_evidence(np.array([-5.0]), "logratio")[0] > 0)
check("tsint positive stays positive", fc.orient_evidence(np.array([4.0]), "tsint")[0] > 0)
check("logratio agrees with sar_change.DEPOSIT_SIGN", fc.EVIDENCE_SIGN["logratio"] == -1)
try:
    fc.orient_evidence(np.array([1.0]), "not_a_detector")
    check("an unknown kind raises rather than guessing a sign", False)
except ValueError:
    check("an unknown kind raises rather than guessing a sign", True)

# |log-ratio|: polarity inside a real slide is a coin flip (measured on Iliamna
# 2026-08-08: 53% brighten, 47% darken), so the magnitude is the usable signal.
_mag = fc.orient_evidence(np.array([-6.0, 6.0, -0.2, 0.2]), "logratio_mag")
check("magnitude kind takes |value|",
      np.allclose(_mag, [6.0, 6.0, 0.2, 0.2], atol=1e-6), _mag.tolist())
check("both polarities score identically", _mag[0] == _mag[1])
check("magnitude has a measured 1 dB floor",
      fc.DEFAULT_FLOORS["logratio_mag"] == 1.0, fc.DEFAULT_FLOORS.get("logratio_mag"))
check("signed log-ratio keeps its 3 dB floor", fc.DEFAULT_FLOORS["logratio"] == 3.0)
check("magnitude is declared, not hard-coded", "logratio_mag" in fc.MAGNITUDE_KINDS)

# --- correlation-family detectors: no polarity to get wrong --------------------
for _k in ("intcorr", "mtcorr"):
    check(f"{_k} is accepted as a fusion channel", _k in fc.EVIDENCE_SIGN)
    check(f"{_k} is evidence-positive as-is", fc.EVIDENCE_SIGN[_k] == +1)
    check(f"{_k} has a floor", _k in fc.DEFAULT_FLOORS)
_mt = fc.orient_evidence(np.array([0.5, 0.95]), "mtcorr")
check("mtcorr passes through unchanged", np.allclose(_mt, [0.5, 0.95]), _mt.tolist())
check("mtcorr floor is sar_tab's SIG value", fc.DEFAULT_FLOORS["mtcorr"] == 0.8)
check("intcorr floor is sar_tab's SIG value", fc.DEFAULT_FLOORS["intcorr"] == 0.3)
_r, _i = fc.robust_rank(_mt, "mtcorr")
check("mtcorr admits only above 0.8", _i["n_admitted"] == 1, _i)
check("its 'normal' 0.5 scores nothing", _r[0] == 0.0, _r[0])

print("max_filter vs brute force")
rng = np.random.default_rng(0)
a = rng.normal(size=(37, 41)).astype(np.float32)
for k in (3, 5, 9):
    pad = k // 2
    ap = np.pad(a, pad, mode="edge")
    ref = np.empty_like(a)
    for i in range(a.shape[0]):
        for j in range(a.shape[1]):
            ref[i, j] = ap[i:i+k, j:j+k].max()
    check(f"k={k}", np.allclose(fc.max_filter(a, k), ref),
          f"maxdiff={np.abs(fc.max_filter(a,k)-ref).max()}")
b = a.copy(); b[5, 5] = np.nan
check("NaN does not win a max", np.isfinite(fc.max_filter(b, 3)[5, 5]))
allnan = np.full((5, 5), np.nan, dtype=np.float32)
check("all-NaN window stays NaN", np.isnan(fc.max_filter(allnan, 3)).all())

print("smoothstep")
check("below lo -> 0", fc.smoothstep(np.array([5.0]), 10, 30)[0] == 0)
check("above hi -> 1", fc.smoothstep(np.array([40.0]), 10, 30)[0] == 1)
check("midpoint -> 0.5", abs(fc.smoothstep(np.array([20.0]), 10, 30)[0] - 0.5) < 1e-6)
check("degenerate hi<=lo -> 0", fc.smoothstep(np.array([20.0]), 30, 30)[0] == 0)

print("robust_rank")
v = np.array([[0.0, 1.0, 2.0], [5.0, 10.0, np.nan]], dtype=np.float32)
r, info = fc.robust_rank(v, "logratio", floor=3.0)
check("below floor -> exactly 0", r[0, 0] == 0 and r[0, 1] == 0 and r[0, 2] == 0)
check("NaN stays NaN", np.isnan(r[1, 2]))
check("strongest -> 1.0", r[1, 1] == 1.0)
check("weakest admitted > 0", r[1, 0] > 0, f"got {r[1,0]}")
check("n_admitted counted", info["n_admitted"] == 2, info)
r2, info2 = fc.robust_rank(np.array([[0.1, 0.2]], dtype=np.float32), "logratio", floor=3.0)
check("quiet AOI admits nothing", info2["n_admitted"] == 0)
check("quiet AOI scores all zero", np.nanmax(r2) == 0.0)

print("detrend_median")
d, off = fc.detrend_median(np.array([[1.0, 2.0, 3.0, 100.0]], dtype=np.float32))
check("median removed", abs(off - 2.5) < 1e-6, off)
check("outlier survives", d[0, 3] > 90)

print("fuse")
o = np.array([[1.0, 0.0, np.nan, np.nan]], dtype=np.float32)
s = np.array([[1.0, 1.0, 0.8, np.nan]], dtype=np.float32)
bands, meta = fc.fuse(o, s, smooth_k=1)   # default mode is now 'mean' 
check("both-high -> 1", bands["score"][0, 0] == 1.0)
check("the default mode is the mean", meta["mode"] == "mean", meta["mode"])
# the multiplicative modes veto; the default mean does not — that difference is
# the whole reason the default changed (a channel blind to an event zeroed the
# rest, and background at 50% recall hit 100% on all three truthed events)
check("a zero kills it under the geometric AND",
      fc.fuse(o, s, mode="geometric", smooth_k=1)[0]["score"][0, 1] == 0.0)
check("but only drags it down under the mean",
      0.0 < bands["score"][0, 1] < 1.0, bands["score"][0, 1])
check("SAR-only falls back, scaled by the single-sensor cap",
      abs(bands["score"][0, 2] - 0.8 * fc.SAR_ONLY_WEIGHT) < 1e-6,
      bands["score"][0, 2])
check("neither -> NaN", np.isnan(bands["score"][0, 3]))
check("conf both", bands["confidence"][0, 0] == fc.CONF_BOTH)
check("conf sar-only", bands["confidence"][0, 2] == fc.CONF_SAR_ONLY)
check("conf none -> NaN", np.isnan(bands["confidence"][0, 3]))
check("n_both", meta["n_both"] == 2, meta)
check("n_sar_only", meta["n_sar_only"] == 1, meta)
b2, _ = fc.fuse(o, s, allow_sar_only=False, smooth_k=1)
check("fallback off -> NaN", np.isnan(b2["score"][0, 2]))
bp, _ = fc.fuse(np.array([[0.25]],dtype=np.float32), np.array([[0.25]],dtype=np.float32), mode="product", smooth_k=1)
bg, _ = fc.fuse(np.array([[0.25]],dtype=np.float32), np.array([[0.25]],dtype=np.float32), mode="geometric", smooth_k=1)
check("product = o*s", abs(bp["score"][0,0]-0.0625) < 1e-6, bp["score"][0,0])
check("geometric = sqrt", abs(bg["score"][0,0]-0.25) < 1e-6, bg["score"][0,0])
bn, _ = fc.fuse(np.array([[1.0]],dtype=np.float32), np.array([[1.0]],dtype=np.float32),
                slope_w=np.array([[np.nan]],dtype=np.float32), smooth_k=1)
check("NaN slope weight does not delete score", bn["score"][0,0] == 1.0, bn["score"][0,0])
try:
    fc.fuse(np.zeros((2,2),dtype=np.float32), np.zeros((3,3),dtype=np.float32), smooth_k=1); check("shape mismatch raises", False)
except ValueError: check("shape mismatch raises", True)

load("layover_dim")
print("slope_weight (real layover_dim)")
yy, xx = np.mgrid[0:80, 0:80]
dem = np.where(xx < 40, 1000.0 + (40 - xx) * 40.0, 1000.0).astype(np.float32)  # cliff then flat
# ISOTROPIC in metres at 62N (dx == dy == ~22 m), so "reach" means the same thing
# along both axes; the anisotropic case is exercised separately below.
gt = (-150.0, 0.000423, 0.0, 62.0, 0.0, -0.0002)
w_far, slope, m = fc.slope_weight(dem, gt, radius_m=400.0)
w_near, _, _ = fc.slope_weight(dem, gt, radius_m=0.0)
flat_far_col = 75      # ~36 px past the cliff, well beyond a 400 m reach
check("steep terrain -> weight 1", w_far[40, 5] > 0.9, w_far[40, 5])
check("radius reaches onto the flat", w_far[40, 45] > w_near[40, 45],
      f"far={w_far[40,45]} near={w_near[40,45]}")
check("far-away flat -> weight 0", w_far[40, flat_far_col] < 0.05, w_far[40, flat_far_col])
# a fully valid DEM must not grow a fake steep corner: _win_mean nulls any window
# with under half its samples, which at a corner is 4 of 9, and filling those from
# a global statistic used to invent a cliff the focal max then smeared inland
_flat_corner = w_far[0, 79]
check("clean DEM: far flat CORNER is not spuriously steep", _flat_corner < 0.05,
      _flat_corner)
check("clean DEM produces no NaN weights", int(np.isnan(w_far).sum()) == 0,
      int(np.isnan(w_far).sum()))
check("window is odd", m["window_px"] % 2 == 1, m)

print("glacier_weight")
mask = np.zeros((10, 10), dtype=bool); mask[3:7, 3:7] = True
gw = fc.glacier_weight(mask, 0.3, feather_k=1)
check("inside downweighted", abs(gw[5, 5] - 0.3) < 1e-6, gw[5, 5])
check("outside untouched", abs(gw[0, 0] - 1.0) < 1e-6)
gwf = fc.glacier_weight(mask, 0.3, feather_k=3)
check("edge feathered", 0.3 < gwf[3, 5] < 1.0, gwf[3, 5])



print()
print("=== GDAL-dependent modules ===")
try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
    _HAVE_GDAL = True
except ImportError as e:
    print(f"  SKIPPED — no osgeo available ({e})")
    _HAVE_GDAL = False

if _HAVE_GDAL:
    fcl, fgl, fg = load("fusion_cloud", "fusion_glacier", "fusion_grid")
    tmp = tempfile.mkdtemp(prefix="fusiontest_")

    def utm_epsg(lat, lon):
        return (32600 if lat >= 0 else 32700) + int((lon + 180) / 6) + 1

    LAT, LON = 60.03, -153.09          # Iliamna
    def utm_epsg(lat, lon): return (32600 if lat >= 0 else 32700) + int((lon + 180) / 6) + 1

    # ---------- optical: UTM 10 m, float64, NaN in band, NO nodata tag (as the Run writes it)
    E = utm_epsg(LAT, LON)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(E); srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    g4 = osr.SpatialReference(); g4.ImportFromEPSG(4326); g4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_utm = osr.CoordinateTransformation(g4, srs)
    cx, cy, _ = to_utm.TransformPoint(LON, LAT)
    N = 600; RES = 10.0
    gt_opt = (cx - N*RES/2, RES, 0.0, cy + N*RES/2, 0.0, -RES)
    opt = np.zeros((N, N), dtype=np.float64)
    opt[250:350, 250:350] = -0.55          # a dark deposit: dNDSI drops
    opt[:, :40] = np.nan                    # a nodata stripe, NaN with no tag
    p_opt = os.path.join(tmp, "iliamna_dndsi.tif")
    ds = gdal.GetDriverByName("GTiff").Create(p_opt, N, N, 1, gdal.GDT_Float64,
                                              options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt_opt); ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(opt)     # deliberately NO SetNoDataValue
    ds.FlushCache(); ds = None

    # ---------- SAR: EPSG:4326 degrees, ~20 m ground, nodata -9999 (as sar_change writes it)
    M = 300
    dlat = (N*RES/2) / 111320.0; dlon = (N*RES/2) / (111320.0*np.cos(np.radians(LAT)))
    gt_sar = (LON - dlon, 2*dlon/M, 0.0, LAT + dlat, 0.0, -2*dlat/M)
    sar = np.zeros((M, M), dtype=np.float32)
    sar[125:175, 125:175] = -6.0            # brighter after -> NEGATIVE log-ratio
    p_sar = os.path.join(tmp, "S1_change_log-ratio_2026-07-28_to_2026-08-09_t94_VV_7x7.tif")
    ds = gdal.GetDriverByName("GTiff").Create(p_sar, M, M, 1, gdal.GDT_Float32,
                                              options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt_sar); ds.SetProjection(g4.ExportToWkt())
    b = ds.GetRasterBand(1); b.SetNoDataValue(-9999.0); b.WriteArray(sar)
    b.FlushCache(); b = None; ds = None

    print("pick_reference")
    ref, gt, shape, proj, info = fg.pick_reference([p_opt, p_sar])
    check("coarser (SAR) wins", ref == p_sar, [(os.path.basename(i['path']), round(i['ground_m'],1)) for i in info])
    check("optical ~10 m", abs([i for i in info if i['path']==p_opt][0]['ground_m'] - 10.0) < 0.5)
    check("SAR ~20 m", 15 < [i for i in info if i['path']==p_sar][0]['ground_m'] < 25,
          [i['ground_m'] for i in info])

    print("warp_to_reference across CRS")
    o = fg.warp_to_reference(p_opt, gt, shape, proj, resample="average")
    s = fg.warp_to_reference(p_sar, gt, shape, proj, resample="average")
    check("shapes match reference", o.shape == shape == s.shape, (o.shape, shape, s.shape))
    check("NaN stripe survives as NaN", np.isnan(o[:, :10]).all(), np.isnan(o[:, :10]).mean())
    check("deposit reprojected into place", np.nanmin(o) < -0.5, np.nanmin(o))
    check("no NaN bleed into the deposit", np.isfinite(o[145:155, 145:155]).all())
    finite_o = np.isfinite(o)
    check("most of the grid is valid", finite_o.mean() > 0.85, finite_o.mean())
    check("SAR nodata -> NaN", not np.isnan(s).all())

    print("zero-background survival (GDAL 3.12 nodata-ordering regression)")
    check("SAR zeros stay zero, not nodata", np.isfinite(s).mean() > 0.99,
          f"finite frac {np.isfinite(s).mean():.3f}")
    check("background reads 0.0", s[10, 150] == 0.0, s[10, 150])
    # the failure mode directly: an uncompressed GTiff written nodata-LAST
    _bad = os.path.join(tmp, "uncompressed_nodata_last.tif")
    _d = gdal.GetDriverByName("GTiff").Create(_bad, M, M, 1, gdal.GDT_Float32)
    _d.SetGeoTransform(gt_sar); _d.SetProjection(g4.ExportToWkt())
    _b = _d.GetRasterBand(1); _b.WriteArray(sar); _b.SetNoDataValue(-9999.0)
    _b.FlushCache(); _b = None; _d = None
    _chk = gdal.Open(_bad); _v = _chk.GetRasterBand(1).ReadAsArray(); _chk = None
    check("(documents the hazard) nodata-last on uncompressed DOES corrupt",
          int((_v == -9999.0).sum()) > 0,
          "GDAL no longer exhibits it — the ordering fix is now belt-and-braces")

    print("no-nodata-tag hazard")
    raw = gdal.Open(p_opt); check("source really has no nodata tag",
                                  raw.GetRasterBand(1).GetNoDataValue() is None); raw = None

    print("end-to-end fuse on the synthetic event")
    oe = fc.orient_evidence(o, "dndsi"); se = fc.orient_evidence(s, "logratio")
    o_rank, oi = fc.robust_rank(oe, "dndsi", floor=0.10)
    s_rank, si = fc.robust_rank(se, "logratio", floor=3.0)
    check("optical admits the deposit", oi["n_admitted"] > 100, oi)
    check("SAR admits the deposit", si["n_admitted"] > 100, si)
    bands, meta = fc.fuse(o_rank, s_rank, smooth_k=1)
    check("peak score is 1.0", abs(meta["max_score"] - 1.0) < 1e-6, meta)
    peak = np.unravel_index(np.nanargmax(bands["score"]), bands["score"].shape)
    check("peak lands in the overlap", 120 <= peak[0] <= 180 and 120 <= peak[1] <= 180, peak)
    check("background scores 0", bands["score"][10, 150] == 0.0, bands["score"][10, 150])

    print("quiet AOI must NOT look like a detection")
    qo = np.random.default_rng(1).normal(0, 0.005, shape).astype(np.float32)
    qs = np.random.default_rng(2).normal(0, 0.8, shape).astype(np.float32)
    qor, qoi = fc.robust_rank(fc.orient_evidence(qo, "dndsi"), "dndsi", floor=0.10)
    qsr, qsi = fc.robust_rank(fc.orient_evidence(qs, "logratio"), "logratio", floor=3.0)
    qb, qm = fc.fuse(qor, qsr, smooth_k=1)
    check("optical admits nothing", qoi["n_admitted"] == 0, qoi)
    # Under the mean, one channel admitting nothing no longer forces 0 — it
    # abstains and the surviving channel is halved. What must still hold is that
    # a quiet AOI cannot reach the top of the ramp on one channel alone.
    check("a single channel cannot exceed its share of the mean",
          qm["max_score"] <= 0.5 + 1e-6, qm["max_score"])
    _qb2, _qm2 = fc.fuse(qor, qor, smooth_k=1)
    check("nothing admitted anywhere -> peak 0", _qm2["max_score"] == 0.0, _qm2)
    _qg, _qgm = fc.fuse(qor, qsr, mode="geometric", smooth_k=1)
    check("the geometric mode still zeroes a quiet AOI", _qgm["max_score"] == 0.0)

    print("write_multiband")
    p_out = os.path.join(tmp, "fused.tif")
    fg.write_multiband(p_out, bands, list(fc.BAND_NAMES), gt, proj)
    d = gdal.Open(p_out)
    check("band count", d.RasterCount == len(fc.BAND_NAMES), d.RasterCount)
    check("band names tagged", d.GetRasterBand(1).GetDescription() == "optical_rank",
          d.GetRasterBand(1).GetDescription())
    check("score band is #6", d.GetRasterBand(6).GetDescription() == "score")
    check("nodata set", d.GetRasterBand(6).GetNoDataValue() == -9999.0)
    rb = d.GetRasterBand(6).ReadAsArray()
    check("round-trip preserves the peak", abs(np.nanmax(rb) - 1.0) < 1e-6, np.nanmax(rb))
    d = None

    print("bbox_4326")
    lo0, la0, lo1, la1 = fcl.bbox_4326(gt_opt, (N, N), srs.ExportToWkt())
    check("UTM grid -> sane lon/lat", -154 < lo0 < -152 and 59.5 < la0 < 60.5, (lo0, la0, lo1, la1))
    lo0b, la0b, lo1b, la1b = fcl.bbox_4326(gt, shape, proj)
    check("geographic grid passes through", abs(lo0b - gt[0]) < 1e-9, (lo0b, gt[0]))

    print("SCL / qa_pixel decoding")
    scl = np.array([[0, 3, 4, 8, 9, 10, 11, 2]], dtype=np.float32)
    bad = fcl._decode(scl, "Sentinel-2")
    check("cloud+shadow+nodata flagged", bad.tolist() == [[True, True, False, True, True, True, False, False]], bad)
    bad_dark = fcl._decode(scl, "Sentinel-2", mask_dark=True)
    check("dark opt-in flags class 2", bad_dark[0, 7])
    check("snow never masked", not bad[0, 6] and not bad_dark[0, 6])
    qa = np.array([[1 << 3, 1 << 4, 1 << 5, 1 << 6, 0]], dtype=np.float32)
    qbad = fcl._decode(qa, "Landsat")
    check("qa cloud/shadow flagged", qbad[0, 0] and qbad[0, 1])
    check("qa snow/clear not flagged", not qbad[0, 2] and not qbad[0, 3])

    print("a scene only votes on pixels it SAW (MGRS tiling)")
    # Mt Logan, 45 km radius: the Run composited FOUR MGRS tiles a side, each
    # covering roughly a quadrant. Outside its own footprint a tile renders as
    # SCL 0, which _decode calls unusable — correct for one scene alone, fatal
    # as a vote. Every pixel then collected three "cloudy" votes out of four,
    # 0.75 cleared the 0.5 threshold, and 99.5% of a cloud-free AOI was masked.
    # The survivor was the small square at the centre where all four overlap.
    obs = fcl._observed(np.array([[0, np.nan, 4, 9, 11]], dtype=np.float32), "s2")
    check("fill and NaN are 'not observed'", obs.tolist() == [[False, False, True, True, True]], obs)
    check("a cloud class IS observed", bool(obs[0, 3]))

    _G = 40
    def _quadrant_tile(ix, iy, cloudy=False):
        """One tile: SCL 4 (clear) over its own quadrant + overlap, 0 elsewhere."""
        a = np.zeros((_G, _G), dtype=np.float32)
        r0, r1 = (0, _G * 3 // 5) if iy == 0 else (_G * 2 // 5, _G)
        c0, c1 = (0, _G * 3 // 5) if ix == 0 else (_G * 2 // 5, _G)
        a[r0:r1, c0:c1] = 9.0 if cloudy else 4.0
        return a

    def _fake_fetch(_coll, item_id, _asset, _bbox, _w, _h, timeout=180):
        ix, iy, cloudy = _TILES[item_id]
        fd, path = tempfile.mkstemp(suffix=".tif", prefix="t_scl_")
        os.close(fd)
        d = gdal.GetDriverByName("GTiff").Create(path, _G, _G, 1, gdal.GDT_Float32)
        d.SetGeoTransform(_gt_s); d.SetProjection(g4.ExportToWkt())
        bnd = d.GetRasterBand(1); bnd.SetNoDataValue(-9999.0)
        bnd.WriteArray(_quadrant_tile(ix, iy, cloudy)); bnd.FlushCache()
        bnd = None; d = None
        return path

    _gt_s = (-141.7, (1.6 / _G), 0.0, 60.70, 0.0, -(0.8 / _G))
    _TILES = {"A": (0, 0, False), "B": (1, 0, False),
              "C": (0, 1, False), "D": (1, 1, False)}
    _meta4 = {"sensor": "s2", "pre_scenes": list(_TILES), "post_scenes": list(_TILES)}
    _real_fetch = fcl._fetch_class_band
    try:
        fcl._fetch_class_band = _fake_fetch
        m4, note4 = fcl.cloud_mask_on_grid(_meta4, _gt_s, (_G, _G),
                                           g4.ExportToWkt(), frac_thresh=0.5)
        check("four clear tiles mask almost nothing", m4.mean() < 0.02,
              f"{m4.mean():.1%} masked — {note4}")
        # the old rule: every scene votes on every pixel, len(ids) denominator
        _old = np.zeros((_G, _G), dtype=np.float32)
        for _k in _TILES:
            _old += fcl._decode(_quadrant_tile(*_TILES[_k][:2]), "s2").astype(np.float32)
        check("the old rule really did mask ~everything", ((_old / 4.0) >= 0.5).mean() > 0.95,
              f"{((_old / 4.0) >= 0.5).mean():.1%}")

        # and a tile that IS cloudy must still mask its own quadrant
        _TILES["A"] = (0, 0, True)
        m5, _n5 = fcl.cloud_mask_on_grid(_meta4, _gt_s, (_G, _G),
                                         g4.ExportToWkt(), frac_thresh=0.5)
        check("a genuinely cloudy tile still masks its own ground",
              m5[2, 2] and m5.mean() > 0.20, f"{m5.mean():.1%} masked")
        check("and leaves the far corner alone", not m5[_G - 2, _G - 2])
    finally:
        fcl._fetch_class_band = _real_fetch

    print("glacier rasterize across CRS")
    shp = os.path.join(tmp, "glaciers.shp")
    drv = ogr.GetDriverByName("ESRI Shapefile"); vds = drv.CreateDataSource(shp)
    lyr = vds.CreateLayer("glaciers", srs=g4, geom_type=ogr.wkbPolygon)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in [(LON-0.01, LAT-0.01), (LON+0.01, LAT-0.01), (LON+0.01, LAT+0.01), (LON-0.01, LAT+0.01), (LON-0.01, LAT-0.01)]:
        ring.AddPoint(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(poly); lyr.CreateFeature(f)
    vds = None
    mask, gmeta = fgl.rasterize_outlines(shp, gt, shape, proj)
    check("polygon found", gmeta["n_features"] == 1, gmeta)
    check("mask covers part of the AOI", 0.01 < mask.mean() < 0.9, mask.mean())
    check("centre inside", bool(mask[shape[0]//2, shape[1]//2]))
    # the hard case: same outlines burned onto the UTM grid (reprojection required)
    mask_utm, gm2 = fgl.rasterize_outlines(shp, gt_opt, (N, N), srs.ExportToWkt())
    check("reprojected onto UTM grid", 0.01 < mask_utm.mean() < 0.9, mask_utm.mean())
    check("UTM centre inside", bool(mask_utm[N//2, N//2]))
    gw = fc.glacier_weight(mask, 0.3)
    check("glacier weight applied", abs(gw[shape[0]//2, shape[1]//2] - 0.3) < 0.05, gw[shape[0]//2, shape[1]//2])
    far = fgl.rasterize_outlines(shp, (0.0, 0.001, 0, 5.0, 0, -0.001), (50, 50), g4.ExportToWkt())
    check("AOI with no glaciers -> empty mask", far[1]["n_features"] == 0 and not far[0].any(), far[1])


# =====================================================================
# Regression tests for the defects the adversarial review turned up.
# Each one failed before the fix; none is hypothetical.
# =====================================================================
print()
print("=== regressions: fusion_core ===")

# --- a DEM void must not poison the whole terrain gate -----------------
# layover_dim._box_mean is an integral image; one NaN used to turn 87% of the
# smoothed DEM into NaN, and fuse() maps a NaN weight back to 1.0, so the
# terrain gate went silently inert over most of the AOI.
yy, xx = np.mgrid[0:60, 0:60]
_dem = np.where(xx < 30, 1000.0 + (30 - xx) * 40.0, 1000.0).astype(np.float32)
_gt = (-150.0, 0.000423, 0.0, 62.0, 0.0, -0.0002)   # isotropic ~22 m
_w_clean, _, _m_clean = fc.slope_weight(_dem, _gt, radius_m=200.0)
_dem_void = _dem.copy()
_dem_void[5, 5] = np.nan
_w_void, _, _m_void = fc.slope_weight(_dem_void, _gt, radius_m=200.0)
check("one DEM void does not poison the grid",
      int(np.isnan(_w_void).sum()) <= 4, f"{int(np.isnan(_w_void).sum())} NaN")
check("the void pixel itself has no terrain opinion", np.isnan(_w_void[5, 5]))
# The void legitimately perturbs its own neighbourhood: 3x3 smoothing, then the
# gradient stencil, then the focal-max window. What must NOT happen is the cumsum
# poisoning that used to wipe out the whole grid, so compare beyond that reach.
_reach = max(_m_void["k_row"], _m_void["k_col"]) // 2 + 3
_far = np.ones_like(_w_void, dtype=bool)
_far[max(0, 5 - _reach):5 + _reach + 1, max(0, 5 - _reach):5 + _reach + 1] = False
_ok = _far & np.isfinite(_w_clean) & np.isfinite(_w_void)
check("weights outside the void's reach are unchanged",
      _ok.any() and np.allclose(_w_clean[_ok], _w_void[_ok], atol=1e-3),
      f"{int(_ok.sum())} px compared")
check("the void's influence is bounded, not global",
      float(_far.mean()) > 0.5, f"only {100*_far.mean():.0f}% of the grid is 'far'")
check("void fraction is reported", _m_void["n_void"] == 1, _m_void["n_void"])
check("clean DEM reports no voids", _m_clean["n_void"] == 0)

# --- anisotropic grids get a per-axis window ---------------------------
_gt_aniso = (-150.0, 0.0004, 0.0, 62.0, 0.0, -0.0001)   # dx_m ~= 4x dy_m
_, _, _m_an = fc.slope_weight(_dem, _gt_aniso, radius_m=300.0)
check("row and column windows differ on an anisotropic grid",
      _m_an["k_row"] != _m_an["k_col"], _m_an)
check("both windows are odd", _m_an["k_row"] % 2 == 1 and _m_an["k_col"] % 2 == 1)

# --- degenerate slope ramp must not blank the map ----------------------
_step = fc.smoothstep(np.array([5.0, 20.0, 40.0], dtype=np.float32), 30.0, 30.0)
check("hi == lo behaves as a step, not all-zero",
      _step.tolist() == [0.0, 0.0, 1.0], _step.tolist())

# --- the floor stays ABSOLUTE even when ranking on detrended values ----
# raw evidence is entirely below the 3 dB floor, but detrending makes some of it
# look large; admission must still be judged on the raw values.
_raw = np.array([[0.0, 0.5, 1.0, 1.5]], dtype=np.float32)
_detr, _ = fc.detrend_median(_raw)
_r_bad, _i_bad = fc.robust_rank(_detr, "logratio", floor=0.5)
_r_good, _i_good = fc.robust_rank(_detr, "logratio", floor=0.5, admit_values=_raw)
check("detrended-only admission over-admits", _i_bad["n_admitted"] > 0, _i_bad)
check("raw-value admission is unaffected by detrending",
      _i_good["n_admitted"] == 3, _i_good)
_r_hi, _i_hi = fc.robust_rank(_detr, "logratio", floor=3.0, admit_values=_raw)
check("nothing clears an absolute 3 dB floor here", _i_hi["n_admitted"] == 0, _i_hi)
check("and the ranks are all zero", np.nanmax(_r_hi) == 0.0)

# --- geometric and product must rank pixels IDENTICALLY ----------------
# they did not once a SAR-only pixel existed: product left the fallback branch
# un-squared, so an uncorroborated 0.5 outranked a corroborated 0.7/0.7 (0.49).
_o = np.array([[0.7, np.nan, 0.9, 0.2]], dtype=np.float32)
_s = np.array([[0.7, 0.50, 0.9, 0.99]], dtype=np.float32)
_bg, _ = fc.fuse(_o, _s, mode="geometric", smooth_k=1)
_bp, _ = fc.fuse(_o, _s, mode="product", smooth_k=1)
_fg = _bg["score"][np.isfinite(_bg["score"])]
_fp = _bp["score"][np.isfinite(_bp["score"])]
check("same pixels scored in both modes", _fg.size == _fp.size == 4, (_fg.size, _fp.size))
check("geometric and product give the same ORDER",
      np.array_equal(np.argsort(_fg), np.argsort(_fp)),
      f"geo={np.argsort(_fg).tolist()} prod={np.argsort(_fp).tolist()}")
check("corroborated 0.7/0.7 outranks uncorroborated 0.5 (geometric)",
      _bg["score"][0, 0] > _bg["score"][0, 1])
check("corroborated 0.7/0.7 outranks uncorroborated 0.5 (product)",
      _bp["score"][0, 0] > _bp["score"][0, 1],
      f"{_bp['score'][0,0]} vs {_bp['score'][0,1]}")
check("product is the geometric score squared",
      np.allclose(_fp, _fg ** 2, atol=1e-6))

# --- optical-only is its own state, and is never scored ----------------
_o2 = np.array([[0.9, np.nan, 0.5, np.nan]], dtype=np.float32)
_s2 = np.array([[0.9, 0.60, np.nan, np.nan]], dtype=np.float32)
_b2, _m2 = fc.fuse(_o2, _s2, smooth_k=1)
check("optical-only is labelled CONF_OPTICAL_ONLY",
      _b2["confidence"][0, 2] == fc.CONF_OPTICAL_ONLY, _b2["confidence"][0, 2])
check("optical-only is NOT scored", np.isnan(_b2["score"][0, 2]))
check("optical-only is counted", _m2["n_optical_only"] == 1, _m2)
# the optical-only SCORE band (a different thing from the optical_only mask)
check("an optical-only score band ships on every run",
      "optical_only" in fc.BAND_NAMES and "optical_only" in _b2)
_oo = _b2["optical_only"]
check("optical-only scores where SAR is missing but optical is not",
      np.isfinite(_oo[0, 2]) and np.isnan(_b2["score"][0, 2]),
      f"oo={_oo[0,2]} score={_b2['score'][0,2]}")
check("optical-only ignores the SAR channel entirely",
      abs(float(_oo[0, 0]) - 0.9) < 1e-6, _oo[0, 0])

# --- spatial smoothing (validated to help on both real events) --------------
_sig = np.zeros((40, 40), dtype=np.float32); _sig[18:23, 18:23] = 1.0
_noise = np.zeros((40, 40), dtype=np.float32); _noise[5, 30] = 1.0; _noise[30, 8] = 1.0
_pat = _sig + _noise
_b_raw, _m_raw = fc.fuse(_pat, _pat, smooth_k=1)
_b_sm, _m_sm = fc.fuse(_pat, _pat, smooth_k=3)
check("smoothing is reported", _m_sm["smooth_k"] == 3, _m_sm.get("smooth_k"))
check("off means off", _m_raw["smooth_k"] == 1)
_coh_raw = float(_b_raw["score"][20, 20]); _iso_raw = float(_b_raw["score"][5, 30])
_coh_sm = float(_b_sm["score"][20, 20]);  _iso_sm = float(_b_sm["score"][5, 30])
check("an isolated speck is suppressed", _iso_sm < _iso_raw, f"{_iso_raw}->{_iso_sm}")
check("a coherent patch survives", _coh_sm > 0.9 * _coh_raw, f"{_coh_raw}->{_coh_sm}")
check("so the patch/speck contrast improves",
      (_coh_sm / max(_iso_sm, 1e-9)) > (_coh_raw / max(_iso_raw, 1e-9)))

# --- pairing two optical channels -------------------------------------------
_a = np.array([[0.8, 0.9, np.nan, np.nan]], dtype=np.float32)
_bb = np.array([[0.2, 0.9, 0.7, np.nan]], dtype=np.float32)
_c = fc.combine_optical(_a, _bb)
check("pairing is the arithmetic mean", abs(float(_c[0, 0]) - 0.5) < 1e-6, _c[0, 0])
check("agreement is preserved", abs(float(_c[0, 1]) - 0.9) < 1e-6, _c[0, 1])
check("disagreement is penalised", _c[0, 0] < _a[0, 0])
check("a missing channel falls back, not voids", abs(float(_c[0, 2]) - 0.7) < 1e-6,
      _c[0, 2])
check("both missing stays NaN", np.isnan(_c[0, 3]))
# the veto that cost Valdez the whole event: a blind channel must ABSTAIN, and a
# geometric mean would return 0.0 here instead of 0.495
_blind = fc.combine_optical(np.array([[0.99]], dtype=np.float32),
                            np.array([[0.00]], dtype=np.float32))
check("a channel at rank 0 cannot veto a strong one",
      float(_blind[0, 0]) > 0.4, _blind[0, 0])
check("but it does pull the score down", float(_blind[0, 0]) < 0.99)
# SAR detectors are pooled by MAX, not averaged: they measure different physics
# (backscattered POWER vs loss of the scattering PATTERN) and are blind to
# different events, so either firing counts. Measured worst-case background at
# 50% recall over three events: log-ratio alone 4.57%, max over detectors 3.38%.
_sa = np.array([[0.9, 0.1, np.nan, np.nan]], dtype=np.float32)
_sb = np.array([[0.1, 0.8, 0.6, np.nan]], dtype=np.float32)
_sc2 = fc.combine_sar(_sa, _sb)
check("SAR pooling takes the max", abs(float(_sc2[0, 0]) - 0.9) < 1e-6, _sc2[0, 0])
check("either detector firing counts", abs(float(_sc2[0, 1]) - 0.8) < 1e-6, _sc2[0, 1])
check("a missing detector falls back", abs(float(_sc2[0, 2]) - 0.6) < 1e-6, _sc2[0, 2])
check("both missing stays NaN", np.isnan(_sc2[0, 3]))
check("SAR pooling is symmetric",
      np.allclose(fc.combine_sar(_sa, _sb), fc.combine_sar(_sb, _sa), equal_nan=True))
check("optical AVERAGES where SAR MAXES (different physics, different rule)",
      float(fc.combine_optical(_sa, _sb)[0, 0]) < float(fc.combine_sar(_sa, _sb)[0, 0]))

check("pairing is symmetric",
      np.allclose(fc.combine_optical(_a, _bb), fc.combine_optical(_bb, _a),
                  equal_nan=True))
check("nothing-measured stays NaN in the confidence band",
      np.isnan(_b2["confidence"][0, 3]))
check("CONF codes are all distinct",
      len({fc.CONF_NONE, fc.CONF_SAR_ONLY, fc.CONF_BOTH, fc.CONF_OPTICAL_ONLY}) == 4)

# --- single-sensor evidence must never outrank corroborated evidence ---------
# Unscaled, the fallback branch handed back the raw SAR rank, so a SAR-only
# pixel at 1.0 beat a fully corroborated pixel at 0.9/0.9 — and 56% of SAR-only
# pixels beat the MEDIAN corroborated pixel. On a coastal AOI that filled the top
# of the ramp with tidal-flat backscatter noise.
_o3 = np.array([[0.90, np.nan]], dtype=np.float32)
_s3 = np.array([[0.90, 1.00]], dtype=np.float32)
_b3, _m3 = fc.fuse(_o3, _s3, smooth_k=1)
check("a perfect SAR-only pixel loses to a strong corroborated one",
      _b3["score"][0, 1] < _b3["score"][0, 0],
      f"sar_only={_b3['score'][0,1]:.3f} both={_b3['score'][0,0]:.3f}")
check("the cap is reported", abs(_m3["sar_only_weight"] - fc.SAR_ONLY_WEIGHT) < 1e-9)
check("SAR-only is capped at the weight",
      _b3["score"][0, 1] <= fc.SAR_ONLY_WEIGHT + 1e-6, _b3["score"][0, 1])
_b4, _ = fc.fuse(_o3, _s3, sar_only_weight=1.0, smooth_k=1)
check("cap 1.0 restores the old (undesirable) behaviour",
      _b4["score"][0, 1] > _b4["score"][0, 0])
# the ordering guarantee must survive both modes
for _mode in ("geometric", "product"):
    _bm, _ = fc.fuse(_o3, _s3, mode=_mode, smooth_k=1)
    check(f"ordering holds in {_mode} mode", _bm["score"][0, 1] < _bm["score"][0, 0])

# --- low-lying veto ----------------------------------------------------------
_dem = np.array([[0.0, 10.0, 15.0, 20.0, 200.0, np.nan]], dtype=np.float32)
_lw = fc.lowland_weight(_dem, min_elev_m=15.0, ramp_m=10.0)
check("sea level vetoed", _lw[0, 0] == 0.0, _lw[0, 0])
check("below the threshold vetoed", _lw[0, 1] == 0.0, _lw[0, 1])
check("at the threshold still vetoed", _lw[0, 2] == 0.0, _lw[0, 2])
check("well above is fully kept", _lw[0, 4] == 1.0, _lw[0, 4])
check("the ramp is gradual, not a cliff", 0.0 < _lw[0, 3] < 1.0, _lw[0, 3])
check("the ramp is done by min+ramp",
      fc.lowland_weight(np.array([[25.0]], dtype=np.float32), 15.0, 10.0)[0, 0] == 1.0)
check("no DEM -> no veto (never suppress on missing data)", _lw[0, 5] == 1.0, _lw[0, 5])
# the coastal failure it exists to stop: a flat pixel beside a steep hill
_coast = np.zeros((40, 40), dtype=np.float32)
_coast[:, :12] = 400.0                        # a steep coastal hill
_gtc = (-153.0, 0.000423, 0.0, 60.0, 0.0, -0.0002)
_wc2, _, _ = fc.slope_weight(_coast, _gtc, radius_m=400.0)
check("terrain rule alone keeps the tidal flat next to the hill",
      _wc2[20, 16] > 0.5, _wc2[20, 16])
_veto = fc.lowland_weight(_coast, 15.0)
check("the lowland veto removes it", (_wc2 * _veto)[20, 16] < 0.05,
      (_wc2 * _veto)[20, 16])
check("and leaves the hill itself alone", (_wc2 * _veto)[20, 4] > 0.9,
      (_wc2 * _veto)[20, 4])

if _HAVE_GDAL:
    print()
    print("=== regressions: GDAL modules ===")
    import json as _json

    # --- an RGBA display raster must be refused as a fusion input ------
    _rgba = os.path.join(tmp, "S1_change_log-ratio_faded.tif")
    _d = gdal.GetDriverByName("GTiff").Create(_rgba, 40, 40, 4, gdal.GDT_Byte,
                                              options=["COMPRESS=DEFLATE", "ALPHA=YES"])
    _d.SetGeoTransform((-153.0, 0.0002, 0.0, 60.05, 0.0, -0.0002))
    _d.SetProjection(g4.ExportToWkt())
    for _i in range(1, 5):
        _d.GetRasterBand(_i).WriteArray(np.full((40, 40), 128, dtype=np.uint8))
    _d.FlushCache(); _d = None
    _ok, _why = fg.describe_raster(_rgba)
    check("baked RGBA raster is refused", not _ok, _why)
    check("refusal explains it is an image, not measurements",
          "image" in _why.lower() and "band" in _why.lower(), _why)
    # an 8-bit single-band raster (a hillshade) is refused for the same reason
    _gray = os.path.join(tmp, "hillshade.tif")
    _d = gdal.GetDriverByName("GTiff").Create(_gray, 40, 40, 1, gdal.GDT_Byte,
                                              options=["COMPRESS=DEFLATE"])
    _d.SetGeoTransform((-153.0, 0.0002, 0.0, 60.05, 0.0, -0.0002))
    _d.SetProjection(g4.ExportToWkt())
    _d.GetRasterBand(1).WriteArray(np.full((40, 40), 100, dtype=np.uint8))
    _d.FlushCache(); _d = None
    _okg, _whyg = fg.describe_raster(_gray)
    check("8-bit single-band image is refused", not _okg, _whyg)
    _okf, _dtf = fg.describe_raster(p_sar)
    check("the real float32 SAR product is accepted", _okf, _dtf)
    check("and reports its dtype", _dtf == "Float32", _dtf)

    # --- a south-up reference must be refused, not silently mirrored ---
    _su = os.path.join(tmp, "southup.tif")
    _d = gdal.GetDriverByName("GTiff").Create(_su, 40, 40, 1, gdal.GDT_Float32,
                                              options=["COMPRESS=DEFLATE"])
    _d.SetGeoTransform((-153.0, 0.0002, 0.0, 60.0, 0.0, 0.0002))   # gt[5] > 0
    _d.SetProjection(g4.ExportToWkt())
    _b = _d.GetRasterBand(1); _b.SetNoDataValue(-9999.0)
    _b.WriteArray(np.zeros((40, 40), dtype=np.float32)); _b.FlushCache()
    _b = None; _d = None
    try:
        fg.pick_reference([_su])
        check("south-up grid is refused", False, "no exception raised")
    except ValueError as e:
        check("south-up grid is refused", "south-up" in str(e), str(e))

    # --- find_metadata must match the raster, not the first file -------
    _pkg = os.path.join(tmp, "qgis_packages"); os.makedirs(_pkg, exist_ok=True)
    _mine = os.path.join(_pkg, "zz_iliamna_dndsi_2026-08-01_vs_2026-08-13.tif")
    open(_mine, "wb").close()
    _json.dump({"event_id": "aa_bagley", "sensor": "s2",
                "pre_scenes": ["X"], "post_scenes": ["Y"], "layers": []},
               open(os.path.join(_pkg, "aa_bagley_metadata.json"), "w"))
    _json.dump({"event_id": "zz_iliamna", "sensor": "s2",
                "pre_scenes": ["A"], "post_scenes": ["B"],
                "layers": [_mine]},
               open(os.path.join(_pkg, "zz_iliamna_metadata.json"), "w"))
    _meta, _mp = fcl.find_metadata(_mine)
    check("metadata matched via the layers list",
          _meta and _meta["event_id"] == "zz_iliamna",
          _meta and _meta.get("event_id"))
    # and by event_id prefix when `layers` is empty
    _mine2 = os.path.join(_pkg, "zz_iliamna_dbright_2026-08-01_vs_2026-08-13.tif")
    open(_mine2, "wb").close()
    _meta2, _ = fcl.find_metadata(_mine2)
    check("metadata matched via the event_id prefix",
          _meta2 and _meta2["event_id"] == "zz_iliamna",
          _meta2 and _meta2.get("event_id"))

    # --- the sensor token the Run actually writes must resolve ---------
    check("'s2' normalises", fcl.normalize_sensor("s2") == "s2")
    check("'landsat' normalises", fcl.normalize_sensor("landsat") == "landsat")
    check("long form still accepted",
          fcl.normalize_sensor("Sentinel-2") == "s2")
    check("planet has no classification band",
          fcl.normalize_sensor("planet") is None)
    check("SENSOR_ASSET is keyed by the normalised token",
          set(fcl.SENSOR_ASSET) == {"s2", "landsat"}, set(fcl.SENSOR_ASSET))
    _dead, _note = fcl.cloud_mask_on_grid({"sensor": "planet"}, gt, shape, proj)
    check("an unknown sensor returns None, not a silent empty mask",
          _dead is None and "unmasked" in _note, _note)

    # --- Landsat off-footprint fill (0) must not read as CLEAR ---------
    _qa = np.array([[0, 1, 1 << 3, 1 << 5, 1 << 6]], dtype=np.float32)
    _bad = fcl._decode(_qa, "landsat")
    check("qa_pixel 0 (render fill) is unusable", bool(_bad[0, 0]))
    check("qa_pixel fill bit is unusable", bool(_bad[0, 1]))
    check("qa_pixel cloud bit is unusable", bool(_bad[0, 2]))
    check("qa_pixel snow is usable", not bool(_bad[0, 3]))
    check("qa_pixel clear is usable", not bool(_bad[0, 4]))
    check("NaN is always unusable",
          bool(fcl._decode(np.array([[np.nan]], dtype=np.float32), "s2")[0, 0]))


print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
