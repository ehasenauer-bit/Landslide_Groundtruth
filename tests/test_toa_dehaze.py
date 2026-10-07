"""A Planet TOA scene's haze floor must come off BEFORE the white balance — but only when
the frame has a dark object to measure it from.

The TOA path (planet_imagery, --planet-toa) applies no atmospheric correction, so path
radiance sits under every band as an additive floor: blue p0.1 measured 0.04-0.16 TOA on
the cached scenes, highest at low sun. _balance_cast then multiplies blue by up to 1.74x,
the floor becomes ~0.27, and the darkest shadow in the frame renders as mid-grey — the HDR
curve's log lift is steepest below ~0.05, where nothing was left (2024-09-11 Denali, sun
31 deg: the whole render sat in DN 89-222). _dehaze subtracts the per-band floor first.

Checked here:
  1. the floor comes off per band, the shadow keeps a small toe, snow keeps its spread;
  2. an all-snow frame (no dark object) is left bit-identical — the reason DOS was once
     rejected outright is that it crushes exactly that frame;
  3. order: dehaze-then-balance leaves the shadow dark where balance-alone amplifies
     the haze;
  4. _open_scene really runs it, on a written DN GeoTIFF + metadata sidecar;
  5. if the 2024-09-11 Denali scene is in the local order cache, its real floor drops
     from ~0.27 to near 0 (skipped when the cache doesn't have it).

Needs the imagery stack (rioxarray), which lives in the project venv, not in QGIS's
Python — so re-exec there if we were started under the wrong one.
"""
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import rioxarray  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with rioxarray; TOA dehaze check not run")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import numpy as np
import xarray as xr
from rasterio.transform import from_origin

sys.path.insert(0, ROOT)
import planet_imagery as pi

rng = np.random.default_rng(11)
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


HAZE = np.array([0.11, 0.08, 0.07, 0.06])         # blue, green, red, nir — the 2024-09-11 floors


def scene(n=300, shadow_frac=0.3, haze=HAZE, snow_lo=0.55):
    """A TOA DataArray (rasterio band coords 1..4): shadowed terrain 0.005-0.06 and snow
    `snow_lo`..1.1, with `haze` added on top of every pixel, plus a NaN nodata corner."""
    surf = np.empty((4, n, n), dtype="float32")
    shadow = rng.random((n, n)) < shadow_frac
    for b in range(4):
        surf[b] = np.where(shadow, rng.uniform(0.005, 0.06, (n, n)),
                           rng.uniform(snow_lo, 1.1, (n, n)))
    toa = surf + haze[:, None, None].astype("float32")
    toa[:, :20, :20] = np.nan
    da = xr.DataArray(toa, dims=("band", "y", "x"),
                      coords={"band": [1, 2, 3, 4], "y": np.arange(n), "x": np.arange(n)})
    return da, shadow


# ---- 1. the floor comes off, per band ------------------------------------------------
print("1. floor removal")
da, shadow = scene()
floor, note = pi._haze_floor(da)
print("  ", note)
check(floor is not None, "a frame with real shadow must yield a floor")
if floor is not None:
    for b, h in zip([1, 2, 3, 4], HAZE):
        f = float(floor.sel(band=b))
        # floor = (haze + darkest surface) * (1 - margin): just under haze + 0.005
        check(0.85 * h < f < h + 0.006, f"band {b}: floor {f:.3f} should sit just under the "
                                        f"{h:.3f} haze")
out = pi._dehaze(da)
v = out.values
check(np.isnan(v[:, :20, :20]).all(), "nodata must stay NaN, not become 0")
fin = np.isfinite(v)
check((v[fin] >= 0).all(), "dehaze must never go negative")
for b in range(4):
    sh = v[b][shadow & fin[b]]
    p1 = float(np.percentile(sh, 1))
    check(p1 < 0.03, f"band {b + 1}: shadow p1 {p1:.3f} — floor not removed")
    check(float(np.percentile(sh, 50)) > 0.01,
          f"band {b + 1}: shadow median crushed to ~0 — the margin should leave a toe")
    sn_in = da.values[b][~shadow & fin[b]]
    sn_out = v[b][~shadow & fin[b]]
    check(abs(sn_out.std() - sn_in.std()) < 1e-4,
          f"band {b + 1}: snow spread changed ({sn_in.std():.4f} -> {sn_out.std():.4f}); "
          f"a pure offset must not touch it")

# ---- 2. no dark object -> identity ---------------------------------------------------
print("2. all-snow frame left alone")
snow_only, _ = scene(shadow_frac=0.0, haze=np.zeros(4), snow_lo=0.55)
floor, note = pi._haze_floor(snow_only)
print("  ", note)
check(floor is None, "an all-snow frame has no dark object; dehaze must stand down")
out = pi._dehaze(snow_only)
check(np.array_equal(out.values, snow_only.values, equal_nan=True),
      "an all-snow frame must come back bit-identical")

# ---- 3. order: dehaze before balance -------------------------------------------------
print("3. dehaze before balance")
# A magenta cast like the real 2024 scene: red reads high on the neutral snow, so the
# balance lifts blue ~1.7x — which, applied alone, lifts blue's haze with it.
cast = scene(haze=HAZE)[0] * xr.DataArray([0.6, 0.7, 1.05, 1.0], dims="band",
                                          coords={"band": [1, 2, 3, 4]})
bal_only = pi._balance_cast(cast)
both = pi._balance_cast(pi._dehaze(cast))
blue_floor_bal = float(np.nanpercentile(bal_only.sel(band=1).values, 0.5))
blue_floor_both = float(np.nanpercentile(both.sel(band=1).values, 0.5))
print(f"   blue p0.5: balance alone {blue_floor_bal:.3f}, dehaze+balance {blue_floor_both:.3f}")
check(blue_floor_bal > 0.09, f"setup: balance alone should amplify the haze "
                             f"(got {blue_floor_bal:.3f})")
check(blue_floor_both < 0.02, f"dehaze+balance left blue's floor at {blue_floor_both:.3f}")

# ---- 4. _open_scene runs it ----------------------------------------------------------
print("4. _open_scene, from a DN GeoTIFF + metadata")
tmp = tempfile.mkdtemp(prefix="toa_dehaze_")
coef = 2.5e-5
da, _ = scene(n=200)
dn = (da / coef).fillna(0).round().astype("uint16")
tr = from_origin(500000.0, 7000000.0, 3.0, 3.0)
dn = dn.rename({"y": "y", "x": "x"}).assign_coords(
    x=500000.0 + 1.5 + 3.0 * np.arange(200), y=7000000.0 - 1.5 - 3.0 * np.arange(200))
dn = dn.rio.write_crs(32606).rio.write_nodata(0)
tif = os.path.join(tmp, "20240911_000000_00_0000_3B_AnalyticMS_clip.tif")
dn.rio.to_raster(tif)
with open(os.path.join(tmp, "20240911_000000_00_0000_3B_AnalyticMS_metadata_clip.xml"),
          "w") as fh:
    fh.write("<x>" + "".join(
        f"<ps:bandSpecificMetadata><ps:bandNumber>{b}</ps:bandNumber>"
        f"<ps:reflectanceCoefficient>{coef}</ps:reflectanceCoefficient>"
        f"</ps:bandSpecificMetadata>" for b in (1, 2, 3, 4)) + "</x>")
saved = pi.TOA_DEHAZE
try:
    floors = {}
    for on in (False, True):
        pi.TOA_DEHAZE = on
        o = pi._open_scene(tif, None, 32606, tr, (200, 200))
        floors[on] = float(np.nanpercentile(
            o.sel(band=["red", "green", "blue"]).max("band").values, 0.5))
finally:
    pi.TOA_DEHAZE = saved
print(f"   channel-max p0.5: off {floors[False]:.3f}, on {floors[True]:.3f}")
check(floors[True] < floors[False] - 0.05,
      f"_open_scene did not dehaze (p0.5 {floors[False]:.3f} -> {floors[True]:.3f})")
check(pi.TOA_DEHAZE is True, "TOA_DEHAZE should ship on")

# ---- 5. the real scene, if cached ----------------------------------------------------
print("5. 2024-09-11 Denali from the order cache")
cache = os.environ.get("LANDSLIDE_PLANET_CACHE",
                       os.path.expanduser("~/.cache/landslide_planet"))
hits = glob.glob(os.path.join(cache, "orders", "*", "*", "PSScene",
                              "20240911_213425_75_24bd_3B_AnalyticMS_clip.tif"))
if not hits:
    print("   SKIP: scene not in the local order cache")
else:
    d = rioxarray.open_rasterio(hits[0], masked=True).astype("float32")[:, ::4, ::4]
    toa = pi._toa_from_dn(d, hits[0]).astype("float32")
    vis = [1, 2, 3]
    old = float(np.nanpercentile(pi._balance_cast(toa).sel(band=vis).max("band"), 0.5))
    new = float(np.nanpercentile(
        pi._balance_cast(pi._dehaze(toa)).sel(band=vis).max("band"), 0.5))
    print(f"   channel-max p0.5: balance alone {old:.3f}, dehaze+balance {new:.3f}")
    check(old > 0.2, f"cached scene floor {old:.3f} — expected the ~0.27 that was diagnosed")
    check(new < 0.12, f"dehaze left the real scene's floor at {new:.3f}")

if fails:
    print("\nFAILURES:")
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("\nTOA DEHAZE VERIFIED — floor off before the balance, all-snow frames untouched")
