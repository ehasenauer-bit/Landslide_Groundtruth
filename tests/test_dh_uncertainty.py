"""Elevation-change volumes must carry an uncertainty, a coverage fraction, and
a bias correction — the three things an imported MOSART Δh needs to be trusted.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_dh_uncertainty.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

import tempfile
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
from landslide_groundtruth import dem_diff as D

EPSG = 32607
RES = 10.0
# a 2 km square AOI on a metric grid
X0, Y0 = 500000.0, 6700000.0
SIDE = 2000.0
BOUNDS = (X0, Y0, X0 + SIDE, Y0 + SIDE)
N = int(SIDE / RES)

def write_dh(arr, path, nodata=-9999.0):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(EPSG)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform([X0, RES, 0, Y0 + SIDE, 0, -RES])
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)                  # nodata BEFORE WriteArray
    b.WriteArray(np.where(np.isfinite(arr), arr, nodata).astype("float32"))
    ds = None
    return path

# a 400 m square scar in the middle: 5 m of erosion, so 400*400*-5 = -800,000 m3
def scar_field(depth=-5.0, bias=0.0, noise=0.0, seed=0):
    a = np.full((N, N), bias, dtype="float64")
    if noise:
        a += np.random.default_rng(seed).normal(0.0, noise, a.shape)
    c0, c1 = N // 2 - 20, N // 2 + 20          # 40 px = 400 m
    a[c0:c1, c0:c1] += depth
    return a

CX, CY = X0 + SIDE / 2, Y0 + SIDE / 2
OUTLINE = ("POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
    CX-200, CY-200, CX+200, CY-200, CX+200, CY+200, CX-200, CY+200, CX-200, CY-200))
OUTLINE_AREA = 400.0 * 400.0
TRUE_EROSION = 400.0 * 400.0 * 5.0            # 800,000 m3

tmp = tempfile.mkdtemp()

print("=== 1. a clean Δh integrates to the true volume ===")
p = write_dh(scar_field(), os.path.join(tmp, "clean.tif"))
r = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, outline_area_m2=OUTLINE_AREA)
print(f"   erosion {r['v_erosion']:>12,.0f} m3   (true -{TRUE_EROSION:,.0f})")
print(f"   coverage {r['coverage_frac']:.3f}   net {r['v_net']:,.0f}")
assert abs(abs(r["v_erosion"]) - TRUE_EROSION) / TRUE_EROSION < 0.02
assert abs(r["coverage_frac"] - 1.0) < 0.05

print("\n=== 2. THE MOSART CASE: a 0.5 m DC bias is pure signal without a fix ===")
p = write_dh(scar_field(bias=0.5), os.path.join(tmp, "biased.tif"))
raw = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, outline_area_m2=OUTLINE_AREA)
err = abs(abs(raw["v_erosion"]) - TRUE_EROSION)
print(f"   uncorrected erosion {raw['v_erosion']:>12,.0f}  -> off by {err:,.0f} m3"
      f" ({err/TRUE_EROSION:.0%})")
st = D.stable_ground_stats(p, OUTLINE, EPSG, BOUNDS, RES)
print(f"   stable ground says: offset {st['offset_m']:+.3f} m  sigma {st['sigma_m']:.3f} m"
      f"  ({st['stable_px']:,} px, ok={st['ok']})")
assert st["ok"] and abs(st["offset_m"] - 0.5) < 0.02, st
fix = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, offset=st["offset_m"],
                     sigma_dh_m=st["sigma_m"], outline_area_m2=OUTLINE_AREA)
print(f"   corrected   erosion {fix['v_erosion']:>12,.0f}  -> off by "
      f"{abs(abs(fix['v_erosion'])-TRUE_EROSION):,.0f} m3")
assert abs(abs(fix["v_erosion"]) - TRUE_EROSION) / TRUE_EROSION < 0.02
assert err > 10 * abs(abs(fix["v_erosion"]) - TRUE_EROSION)

print("\n=== 3. the uncertainty is reported and scales with the noise ===")
prev = None
for noise in (0.0, 0.25, 1.0):
    p = write_dh(scar_field(noise=noise, seed=1), os.path.join(tmp, f"n{noise}.tif"))
    st = D.stable_ground_stats(p, OUTLINE, EPSG, BOUNDS, RES)
    r = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, offset=st["offset_m"],
                       sigma_dh_m=st["sigma_m"], outline_area_m2=OUTLINE_AREA)
    print(f"   noise {noise:>4} m -> sigma_h {r['sigma_dh_m']:.3f} m   "
          f"sigma_V correlated {r['v_sigma_m3']:>10,.0f}   random {r['v_sigma_random_m3']:>9,.0f}")
    assert abs(r["sigma_dh_m"] - noise) < 0.05, (noise, r["sigma_dh_m"])
    if prev is not None:
        assert r["v_sigma_m3"] > prev
    prev = r["v_sigma_m3"]
# the two bounds must differ by ~sqrt(N_pixels_in_outline)
n_px = OUTLINE_AREA / (RES * RES)
ratio = r["v_sigma_m3"] / r["v_sigma_random_m3"]
print(f"   correlated/random = {ratio:.1f}  (sqrt(N)={n_px**0.5:.1f})")
assert abs(ratio - n_px**0.5) / n_px**0.5 < 0.1

print("\n=== 4. partial coverage is reported, not silently returned as a small slide ===")
half = scar_field()
half[:, N//2:] = np.nan                        # right half of the AOI has no data
p = write_dh(half, os.path.join(tmp, "half.tif"))
r = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, outline_area_m2=OUTLINE_AREA)
print(f"   coverage {r['coverage_frac']:.2f}   erosion {r['v_erosion']:,.0f}"
      f"  (~half of {TRUE_EROSION:,.0f})")
assert 0.45 < r["coverage_frac"] < 0.55, r["coverage_frac"]

print("\n=== 5. nodata never enters the sum ===")
# -9999 fill would swamp everything if it leaked
p = write_dh(scar_field(), os.path.join(tmp, "nd.tif"))
r = D.integrate_dh(p, OUTLINE, EPSG, BOUNDS, RES, outline_area_m2=OUTLINE_AREA)
assert abs(r["v_net"]) < 1e7 and r["max_drop_m"] > -100, r
print(f"   max drop {r['max_drop_m']:.1f} m (not -9999), net {r['v_net']:,.0f}")

print("\n=== 6. too little surrounding ground -> refuses to claim it is unbiased ===")
tiny = ("POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
    X0, Y0, X0+SIDE, Y0, X0+SIDE, Y0+SIDE, X0, Y0+SIDE, X0, Y0))   # whole AOI
p = write_dh(scar_field(bias=0.5), os.path.join(tmp, "all.tif"))
st = D.stable_ground_stats(p, tiny, EPSG, BOUNDS, RES)
print(f"   outline covers the whole AOI -> ok={st['ok']}, stable_px={st['stable_px']}")
assert st["ok"] is False and st["offset_m"] == 0.0

print("\n=== 7. coregister_offset still works for a computed pair, and reports sigma ===")
rng = np.random.default_rng(3)
diff = rng.normal(0.7, 0.3, (200, 200))        # 0.7 m bias, 0.3 m noise
diff[80:120, 80:120] -= 8.0                    # a slide
valid = np.ones_like(diff, dtype=bool)
off, stable, sig = D.coregister_offset(diff, valid)
print(f"   offset {off:.3f} (true 0.7)   sigma {sig:.3f} (true 0.3)   stable {stable:,}")
assert abs(off - 0.7) < 0.05 and abs(sig - 0.3) < 0.05

print("\nΔh UNCERTAINTY / BIAS / COVERAGE VERIFIED")
