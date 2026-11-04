"""Pairing two change rasters by the ground they cover, not by the name they got.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_footprint_pairing.py

Two questions, and one threshold used to answer both — wrongly in each direction.

WHICH candidate is the best partner (a RANKING). Mt Logan, 2026-08-29: the project
held three dBright rasters for one event and one 90 km SAR change raster, and the
tab paired the SAR scene with a dBright tile from an earlier, much smaller run
(20 km), reporting "100% footprint overlap", because the test was one-directional

    intersection / area(optical)

and a 20 km box inside a 90 km box fills 100% of ITSELF. Ranking must therefore be
SYMMETRIC — min(a-in-b, b-in-a) — so being swallowed cannot win.

CAN the pair be fused at all (a GATE). Using that same symmetric score as the veto
then refused pairs that were perfectly good: a 20 km-radius optical against the
28 km-radius SAR of the SAME event, concentric to 400 m and wholly covered, scored
54% and was greyed out. The fused grid is cropped to the intersection
(fusion_grid.crop_to_common), so a SAR scene bigger than the optical AOI is not a
problem — it is trimmed. What the gate must ask is one-directional and about the
optical AOI only: how much of it has SAR underneath.

So three things are pinned here, and they are separate fixes:
  * RANKING is symmetric, so a swallowing scene cannot score perfectly;
  * the GATE is coverage of the optical AOI, so a larger scene is trimmed rather
    than refused, and only a scene centred somewhere ELSE is turned away;
  * the fused EXTENT is the intersection, because "how fine" and "how far" are
    different questions and the coarser raster is not the answer to both.

The bboxes below are the real ones, measured off the files on disk. Two distinct
events 50 km apart both got called "Mt Logan" that day, which is what made the
mismatch so easy to hit:
    AOI A  centre -139.99, 60.25   event_260829_1050, "(1.5)"
    AOI B  centre -140.90, 60.30   event_260829_1121, "(0.8)"
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

import numpy as np
from osgeo import gdal, osr
import tempfile
gdal.UseExceptions()

from landslide_groundtruth import fusion_grid as fg
from landslide_groundtruth.fusion_tab import (FusionTab, FOOTPRINT_MATCH_MIN,
                                              AOI_COVERAGE_MIN)

# ---- the real Mt Logan footprints, lon/lat -------------------------------
# AOI B -- event_260829_1121, the 45 km run
SAR      = (-141.7159, 59.8958, -140.0841, 60.7042)   # 90.0 x 89.4 km, 43.9 m
BIG_OPT  = (-141.7340, 59.8945, -140.0612, 60.7055)   # 91.3 x 90.2 km, 10 m
SMALL_20 = (-141.0819, 60.2099, -140.7171, 60.3902)   # 20.1 x 20.1 km, inside SAR
# AOI A -- event_260829_1050, 50 km east. STALE_40 is the 20 km-radius run of it;
# from AOI B's point of view it is simply a different place.
STALE_40 = (-140.3693, 60.0649, -139.6230, 60.4352)   # 40.7 x 40.6 km
OPT_57   = (-140.5146, 59.9908, -139.4699, 60.5092)   # 57.0 x 56.9 km, 28 km rad
SAR_28   = (-140.5069, 59.9985, -139.4931, 60.5015)   # 56.0 x 55.6 km, 28 km rad

match = FusionTab._footprint_match
one_way = FusionTab._overlap_fraction
# the gate asks how much of the OPTICAL AOI the SAR raster covers
cover = lambda opt, sar: FusionTab._overlap_fraction(opt, sar)

print("=== 1. RANKING: the one-directional test scored the WRONG pair perfectly ===")
old = one_way(SMALL_20, SAR)
new_ = match(SMALL_20, SAR)
print(f"   20 km optical inside the 90 km SAR: one-way {old:.0%}, symmetric {new_:.0%}")
assert old > 0.99, "the old metric really did call this a perfect pair"
assert new_ < 0.10, "the symmetric metric must see it for what it is"
assert new_ < match(BIG_OPT, SAR), "and must rank below the run that fits"

print("=== 2. the pair that IS right still scores high ===")
good = match(BIG_OPT, SAR)
print(f"   91 km optical vs 90 km SAR: {good:.0%}")
assert good > 0.90, good
assert good >= FOOTPRINT_MATCH_MIN

print("=== 3. RANKING orders the three candidates, best first ===")
scores = {"91 km (this run)": match(BIG_OPT, SAR),
          "20 km (same event, smaller)": match(SMALL_20, SAR),
          "40 km (the OTHER event)": match(STALE_40, SAR)}
for k, v in scores.items():
    print(f"   {k:28s} {v:5.0%}")
assert max(scores.values()) == scores["91 km (this run)"], \
    "the run that actually fits must rank first, whatever else is offered"

print("=== 3b. the GATE admits a larger scene, refuses a displaced one ===")
gate = {"91 km (this run)": cover(BIG_OPT, SAR),
        "20 km (same event, smaller)": cover(SMALL_20, SAR),
        "40 km (the OTHER event)": cover(STALE_40, SAR)}
for k, v in gate.items():
    print(f"   {k:28s} {v:5.0%} of the optical AOI covered  "
          f"{'pair' if v >= AOI_COVERAGE_MIN else 'greyed out'}")
assert gate["20 km (same event, smaller)"] > 0.99, \
    "the 90 km scene covers the 20 km AOI completely — crop_to_common trims it"
assert gate["40 km (the OTHER event)"] < AOI_COVERAGE_MIN, \
    "a raster centred 50 km away is still refused — this is what the gate is FOR"
assert gate["91 km (this run)"] >= AOI_COVERAGE_MIN

print("=== 3c. AOI A: the concentric pair the symmetric veto used to refuse ===")
# 20 km-radius optical against the 28 km-radius SAR of the SAME event, centres
# 400 m apart. This is the case that sent a user hunting for a missing layer.
sym, cov = match(STALE_40, SAR_28), cover(STALE_40, SAR_28)
print(f"   40.7 km optical vs 56.0 km SAR: symmetric {sym:.0%}, coverage {cov:.0%}")
assert sym < FOOTPRINT_MATCH_MIN, "the symmetric score really does miss the old bar"
assert cov >= AOI_COVERAGE_MIN, "but the SAR covers the optical AOI, so it pairs"

print("=== 3d. AOI A: the same-radius pair is unambiguous, and ranks first ===")
print(f"   57.0 km optical vs 56.0 km SAR: symmetric {match(OPT_57, SAR_28):.0%}, "
      f"coverage {cover(OPT_57, SAR_28):.0%}")
assert match(OPT_57, SAR_28) > 0.90
assert cover(OPT_57, SAR_28) >= AOI_COVERAGE_MIN
assert match(OPT_57, SAR_28) > match(STALE_40, SAR_28), \
    "with both loaded, the 28 km optical must out-rank the 20 km one"

print("=== 3e. the two events never pair with each other's SAR ===")
for opt, nm in ((OPT_57, "AOI A 57 km"), (STALE_40, "AOI A 40.7 km")):
    print(f"   {nm:14s} vs AOI B 90 km SAR: covers {cover(opt, SAR):.0%}")
    assert cover(opt, SAR) < AOI_COVERAGE_MIN, nm
for opt, nm in ((BIG_OPT, "AOI B 91 km"), (SMALL_20, "AOI B 20 km")):
    print(f"   {nm:14s} vs AOI A 28 km SAR: covers {cover(opt, SAR_28):.0%}")
    assert cover(opt, SAR_28) < AOI_COVERAGE_MIN, nm

print("=== 4. the match is symmetric, whichever way round it is asked ===")
for a, b in ((SMALL_20, SAR), (BIG_OPT, SAR), (STALE_40, SAR)):
    assert abs(match(a, b) - match(b, a)) < 1e-12
assert match(None, SAR) == 0.0 and match(SAR, None) == 0.0
print("   symmetric, and a missing box scores 0 rather than raising")

print("=== 5. disjoint boxes score 0, touching boxes score 0 ===")
far = (-120.0, 40.0, -119.0, 41.0)
assert match(far, SAR) == 0.0

# ---- crop_to_common, on real rasters -------------------------------------
tmp = tempfile.mkdtemp(prefix="fusion_extent_")
g4 = osr.SpatialReference(); g4.ImportFromEPSG(4326)


def write(name, bb, n, proj):
    """A 4326 raster covering `bb` with n x n pixels."""
    path = os.path.join(tmp, name)
    gt = (bb[0], (bb[2] - bb[0]) / n, 0.0, bb[3], 0.0, -(bb[3] - bb[1]) / n)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32,
                                              options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt); ds.SetProjection(proj.ExportToWkt())
    b = ds.GetRasterBand(1); b.SetNoDataValue(-9999.0)
    b.WriteArray(np.zeros((n, n), dtype=np.float32)); b.FlushCache()
    b = None; ds = None
    return path


p_sar = write("sar_90km.tif", SAR, 2048, g4)
p_big = write("opt_91km.tif", BIG_OPT, 2048, g4)
p_small = write("opt_20km.tif", SMALL_20, 512, g4)

print("=== 6. the mismatched pair no longer yields a 90 km blind raster ===")
ref, gt, shape, proj, _info = fg.pick_reference([p_small, p_sar])
full_km = fg.extent_km(gt, shape)
gt2, shape2, xi = fg.crop_to_common(gt, shape, proj, [p_small, p_sar])
crop_km = fg.extent_km(gt2, shape2)
print(f"   reference {full_km[0]:.1f}x{full_km[1]:.1f} km -> "
      f"cropped {crop_km[0]:.1f}x{crop_km[1]:.1f} km ({xi['kept']:.1%} kept)")
assert xi["cropped"]
assert 19.0 < crop_km[0] < 21.5, crop_km          # the 20 km optical, not 90 km
assert 19.0 < crop_km[1] < 21.5, crop_km
assert xi["kept"] < 0.06, xi["kept"]

print("=== 7. the crop lands on reference pixel edges (nothing resampled twice) ===")
dx = (gt2[0] - gt[0]) / gt[1]
dy = (gt[3] - gt2[3]) / abs(gt[5])
print(f"   origin moved {dx:.6f} x {dy:.6f} reference pixels")
assert abs(dx - round(dx)) < 1e-6 and abs(dy - round(dy)) < 1e-6
assert gt2[1] == gt[1] and gt2[5] == gt[5], "the resolution must not change"
assert gt2[2] == 0.0 and gt2[4] == 0.0, "and the grid must stay axis-aligned"

print("=== 8. a well-matched pair is left essentially alone ===")
ref, gt, shape, proj, _i = fg.pick_reference([p_big, p_sar])
gt3, shape3, xi3 = fg.crop_to_common(gt, shape, proj, [p_big, p_sar])
print(f"   91 km optical + 90 km SAR keeps {xi3['kept']:.1%} of the reference")
# not 100%: the optical is a touch wider and a touch further west, so a thin
# margin of the SAR scene has no optical behind it. Trimming that IS the fix
# working — what matters is that a real pair loses a margin, not a footprint.
assert xi3["kept"] > 0.95, xi3["kept"]

print("=== 9. a single input is never cropped at all ===")
ref, gt, shape, proj, _i = fg.pick_reference([p_big])
gt4, shape4, xi4 = fg.crop_to_common(gt, shape, proj, [p_big])
assert not xi4["cropped"] and shape4 == shape and gt4 == gt, "optical-only run"

print("=== 10. inputs that share no ground are refused, not fused ===")
p_far = write("far.tif", far, 128, g4)
for paths, frag in (([p_far, p_sar], "do not overlap"),
                    ([p_sar, p_far], "do not overlap")):
    ref, gt, shape, proj, _i = fg.pick_reference(paths)
    try:
        fg.crop_to_common(gt, shape, proj, paths)
        raise AssertionError("a disjoint pair must raise, not produce a raster")
    except ValueError as e:
        assert frag in str(e), str(e)
print("   disjoint inputs raise ValueError")

print("=== 11. a slivered overlap is refused rather than fused on 3 pixels ===")
sliver = (SAR[2] - 0.002, SAR[1], SAR[2] + 1.0, SAR[3])   # a few px of column
p_sliv = write("sliver.tif", sliver, 64, g4)
ref, gt, shape, proj, _i = fg.pick_reference([p_sliv, p_sar])
try:
    fg.crop_to_common(gt, shape, proj, [p_sliv, p_sar])
    raise AssertionError("a sliver must raise")
except ValueError as e:
    assert "too little to fuse" in str(e), str(e)
print("   sliver refused:", "too little to fuse")

print("=== 12. UTM against 4326: the footprint is densified, not corner-sampled ===")
u7 = osr.SpatialReference(); u7.ImportFromEPSG(32607)
ds = gdal.Warp(os.path.join(tmp, "opt_utm.tif"), p_big, dstSRS="EPSG:32607",
               xRes=10, yRes=10, format="GTiff")
ds = None
p_utm = os.path.join(tmp, "opt_utm.tif")
bb = fg.bbox_in_crs(p_utm, g4.ExportToWkt())
print(f"   UTM 7N tile -> 4326 {bb[0]:.4f},{bb[1]:.4f} .. {bb[2]:.4f},{bb[3]:.4f}")
assert bb[0] < bb[2] and bb[1] < bb[3]
assert match(bb, BIG_OPT) > 0.95, match(bb, BIG_OPT)
ref, gt, shape, proj, _i = fg.pick_reference([p_utm, p_sar])
assert ref == p_sar, "the 43.9 m SAR is still the coarser grid"
gt5, shape5, xi5 = fg.crop_to_common(gt, shape, proj, [p_utm, p_sar])
km5 = fg.extent_km(gt5, shape5)
print(f"   cross-CRS crop: {km5[0]:.1f}x{km5[1]:.1f} km, {xi5['kept']:.1%} kept")
assert xi5["kept"] > 0.90, xi5["kept"]

print("=== 13. the shipped thresholds still admit the real pairs ===")
print(f"   AOI_COVERAGE_MIN = {AOI_COVERAGE_MIN:.2f}  (gate)")
print(f"   FOOTPRINT_MATCH_MIN = {FOOTPRINT_MATCH_MIN:.2f}  (ranking / 'say so')")
for v in (AOI_COVERAGE_MIN, FOOTPRINT_MATCH_MIN):
    assert 0.4 <= v <= 0.85, "a bar this far out is not a bar"
assert cover(BIG_OPT, SAR) > AOI_COVERAGE_MIN + 0.25, \
    "the real pair must clear the gate with room, not squeak past it"
assert match(BIG_OPT, SAR) > FOOTPRINT_MATCH_MIN + 0.25

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("\nFOOTPRINT PAIRING + EXTENT VERIFIED")
