"""Pairing two change rasters by the ground they cover, not by the name they got.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_footprint_pairing.py

Why it matters, in the numbers this file uses. Mt Logan, 2026-08-29: the project
held three dBright rasters for the same event and one SAR change raster, and the
Fusion tab paired the SAR scene (90 km) with a dBright tile from an earlier,
much smaller run (20 km). It did so with no warning at all — it reported "100%
footprint overlap" — because the overlap test was one-directional:

    intersection / area(optical)

and a 20 km box sitting wholly inside a 90 km box fills 100% of ITSELF. The
fused raster then came out 90 km wide (the reference grid was the coarser SAR
scene, and its extent was handed straight to the warp) carrying optical evidence
over 5% of its own area. It looked exactly like the small dBright had been used.

So two things are pinned here, and they are separate fixes:
  * the match is SYMMETRIC — min of the two containments, so being swallowed
    scores low;
  * the fused EXTENT is the intersection, because "how fine" and "how far" are
    different questions and the coarser raster is not the answer to both.

The bboxes below are the real ones, measured off the four files on disk.
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
from landslide_groundtruth.fusion_tab import FusionTab, FOOTPRINT_MATCH_MIN

# ---- the real Mt Logan footprints, lon/lat -------------------------------
SAR      = (-141.7159, 59.8958, -140.0841, 60.7042)   # 90.0 x 89.4 km, 43.9 m
BIG_OPT  = (-141.7340, 59.8945, -140.0612, 60.7055)   # 91.3 x 90.2 km, 10 m
SMALL_20 = (-141.0819, 60.2099, -140.7171, 60.3902)   # 20.1 x 20.1 km, inside SAR
STALE_40 = (-140.3693, 60.0649, -139.6230, 60.4352)   # 40.7 x 40.6 km, offset

match = FusionTab._footprint_match
one_way = FusionTab._overlap_fraction

print("=== 1. the one-directional test scored the WRONG pair perfectly ===")
old = one_way(SMALL_20, SAR)
new = match(SMALL_20, SAR)
print(f"   20 km optical inside the 90 km SAR: one-way {old:.0%}, symmetric {new:.0%}")
assert old > 0.99, "the old metric really did call this a perfect pair"
assert new < 0.10, "the symmetric metric must see it for what it is"
assert new < FOOTPRINT_MATCH_MIN, "and must fall below the pairing bar"

print("=== 2. the pair that IS right still scores high ===")
good = match(BIG_OPT, SAR)
print(f"   91 km optical vs 90 km SAR: {good:.0%}")
assert good > 0.90, good
assert good >= FOOTPRINT_MATCH_MIN

print("=== 3. the filter separates the three candidates cleanly ===")
scores = {"91 km (this run)": match(BIG_OPT, SAR),
          "20 km (stale)": match(SMALL_20, SAR),
          "40 km (stale, offset)": match(STALE_40, SAR)}
for k, v in scores.items():
    print(f"   {k:24s} {v:5.0%}  {'pair' if v >= FOOTPRINT_MATCH_MIN else 'greyed out'}")
assert sum(v >= FOOTPRINT_MATCH_MIN for v in scores.values()) == 1, \
    "exactly one of the three may be offered"
assert max(scores.values()) == scores["91 km (this run)"]

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

print("=== 13. the shipped threshold still admits the real pair ===")
print(f"   FOOTPRINT_MATCH_MIN = {FOOTPRINT_MATCH_MIN:.2f}")
assert 0.4 <= FOOTPRINT_MATCH_MIN <= 0.85, "a bar this far out is not a bar"
assert match(BIG_OPT, SAR) > FOOTPRINT_MATCH_MIN + 0.25, \
    "the real pair must clear the bar with room, not squeak past it"

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("\nFOOTPRINT PAIRING + EXTENT VERIFIED")
