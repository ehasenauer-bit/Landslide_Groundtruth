"""HDR's three exposures must BRACKET the scene, not just sample where its pixels are.

_hdr_whites picked all three exposure white points as percentiles (30/62/94) of the
scene's own reflectance. Percentiles say where the pixels ARE, not what range they
SPAN — so on a frame that is mostly snow or cloud the 30th percentile is already
snow, and all three "exposures" land in the highlights within ~1.6x of each other.
None of them exposes the terrain: on an 85%-snow frame, reflectance 0.02 rendered at
DN 7.7 / 6.2 / 5.0 in the three exposures, so the Mertens fusion had nothing but
black to choose from at the dark end. The shadows came out as one flat block —
crushed, then lifted bodily by _hdr_finish's sub-1 gain into a featureless grey
wash. Exactly the failure HDR mode exists to prevent, in exactly the alpine
snow-and-rock scene it is reached for.

So the bright exposure carries two one-sided clamps (HDR_DARK_PCT / HDR_MIN_SPAN):
it must expose the scene's own dark tail, and it must leave the bracket spanning the
scene. One-sided, so a scene whose p30 is already dark renders bit-for-bit as before
— asserted below, because the no-op guarantee is what makes this safe to ship over
packages already on disk.

Also guards the nodata floor: nodata is 0, so a fused pixel driven to DN 0 would
punch a TRANSPARENT HOLE where the deepest shadow is. A better bracket makes that
reachable (1.0% of a 92%-snow frame before the floor), so the two ship together.

Needs the imagery stack (rioxarray), which lives in the project venv, not in QGIS's
Python — so re-exec there if we were started under the wrong one.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import rioxarray  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with rioxarray; HDR bracket check not run")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import numpy as np
import rasterio
import xarray as xr

sys.path.insert(0, ROOT)
import review_package as rp

TMP = os.environ.get("TMPDIR", "/tmp")
rng = np.random.default_rng(3)
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _fbm(n, octaves=6):
    """Fractal terrain in 0-1 — ridges and valleys, so the snow line is a shape and
    not a checkerboard, which is what the pyramid fusion actually responds to."""
    f = np.zeros((n, n))
    for o in range(octaves):
        s = 2 ** o
        g = rng.normal(size=(s + 1, s + 1))
        i = (np.arange(n) * s / n).astype(int)
        f += g[np.ix_(i, i)] / (o + 1)
    return (f - f.min()) / (f.max() - f.min())


def scene(snow_frac, n=512):
    """(snow mask, composite): alpine reflectance with `snow_frac` of the frame in snow
    at 0.60-0.88 and the rest terrain spanning 0.012 (shadowed rock) to 0.11 (lit scree)
    — a factor of 9, i.e. real, readable structure that a working render must show."""
    f = _fbm(n)
    thr = np.quantile(f, 1.0 - snow_frac)
    snow = f >= thr
    tex = rng.normal(0, 1, (n, n))
    refl = np.where(snow, 0.60 + 0.28 * (f - thr) / max(1 - thr, 1e-6) + 0.015 * tex,
                          0.012 + 0.10 * f / max(thr, 1e-6) + 0.005 * tex)
    refl = np.clip(refl, 0.004, 1.3)
    arr = np.stack([refl * 1.05, refl, refl * 0.95])
    return snow, xr.DataArray(
        arr, dims=("band", "y", "x"),
        coords={"band": ["red", "green", "blue"],
                "y": np.arange(n) * -10.0 + 7_000_000.0,
                "x": np.arange(n) * 10.0 + 500_000.0})


def percentile_only(comp):
    """What _hdr_whites did before the clamps: percentiles, kept distinct."""
    v = comp.sel(band=["red", "green", "blue"]).max(dim="band").values
    v = v[np.isfinite(v)]
    q = np.clip(np.percentile(v, rp.HDR_EXPOSURE_PCTS).astype(float), 0.05, 1.3)
    q[1] = max(q[1], q[0] * 1.25)
    q[2] = max(q[2], q[1] * 1.25)
    return tuple(float(x) for x in q)


def fuse(comp, whites):
    """_highlight_hdr's pipeline without the write, so the fused DN is measurable."""
    s = rp.HDR_SATURATION
    exps = [rp._rolloff_rgb(comp, white=w, desat=rp.DESAT * s,
                            blue_desat=rp.BLUE_DESAT * s, cyan_strip=rp.CYAN_STRIP * s,
                            pink_strip=rp.PINK_STRIP * s).transpose("band", "y", "x").values
            for w in whites]
    nod = np.isnan(exps[0]).all(axis=0)
    imgs = [np.moveaxis(np.nan_to_num(e, nan=0.0), 0, 2) / 255.0 for e in exps]
    return rp._hdr_finish(rp._exposure_fusion(imgs), ~nod) * 255.0


print("=== 1. a dark-dominated scene is bit-identical to the pure-percentile bracket ===")
# The clamps are one-sided. Wherever the percentiles already bracket the scene they must
# not fire at all, or every review package already on disk changes look for no reason.
for sf in (0.15, 0.35, 0.60):
    _, comp = scene(sf)
    now, before = rp._hdr_whites(comp), percentile_only(comp)
    print(f"   snow {sf * 100:3.0f}%: {tuple(round(x, 3) for x in now)}"
          f"{'  (unchanged)' if now == before else '  CHANGED from ' + str(before)}")
    check(now == before, f"the clamps must be a no-op at {sf * 100:.0f}% snow, "
                         f"got {now} instead of {before}")

print("\n=== 2. a snow-dominated scene still gets a bracket that spans it ===")
for sf in (0.75, 0.85, 0.92):
    _, comp = scene(sf)
    now, before = rp._hdr_whites(comp), percentile_only(comp)
    print(f"   snow {sf * 100:3.0f}%: {tuple(round(x, 3) for x in before)} span "
          f"{before[2] / before[0]:4.1f}x  ->  {tuple(round(x, 3) for x in now)} span "
          f"{now[2] / now[0]:4.1f}x")
    check(before[2] / before[0] < 2.0,
          f"this test is not exercising the bug at {sf * 100:.0f}% snow: the old bracket "
          f"already spanned {before[2] / before[0]:.1f}x")
    check(now[2] / now[0] >= rp.HDR_MIN_SPAN - 1e-9,
          f"bracket spans only {now[2] / now[0]:.1f}x at {sf * 100:.0f}% snow, "
          f"want >= {rp.HDR_MIN_SPAN}")
    check(now[1] == before[1] and now[2] == before[2],
          "only the BRIGHT exposure may move; the mid/dark ones are the scene's own")

print("\n=== 3. the bright exposure actually exposes shadowed rock ===")
# A white point of ~R renders reflectance R at mid-grey, where the well-exposedness
# weight peaks. Shadowed rock at 0.02 must land somewhere the fusion can use it.
for sf in (0.75, 0.92):
    _, comp = scene(sf)
    dn_now = 255 * min(0.02 / rp._hdr_whites(comp)[0], 1.0)
    dn_old = 255 * min(0.02 / percentile_only(comp)[0], 1.0)
    print(f"   snow {sf * 100:3.0f}%: reflectance 0.02 renders at DN {dn_old:5.1f} "
          f"-> {dn_now:5.1f} in the bright exposure")
    check(dn_old < 12, "the old bracket is supposed to render 0.02 as near-black here")
    check(dn_now > 25, f"bright exposure still renders 0.02 at DN {dn_now:.1f}; the fusion "
                       f"has nothing to recover the shadows from")

print("\n=== 4. shadowed terrain comes back with tonal SEPARATION, not one flat block ===")
for sf in (0.75, 0.85):
    snow, comp = scene(sf)
    now, before = fuse(comp, rp._hdr_whites(comp)), fuse(comp, percentile_only(comp))
    d_now, d_old = now.mean(2)[~snow], before.mean(2)[~snow]
    rng_now = np.percentile(d_now, 95) - np.percentile(d_now, 5)
    rng_old = np.percentile(d_old, 95) - np.percentile(d_old, 5)
    print(f"   snow {sf * 100:3.0f}%: terrain std {d_old.std():4.1f} -> {d_now.std():4.1f} DN, "
          f"p5-p95 spread {rng_old:5.1f} -> {rng_now:5.1f} DN "
          f"({d_now.std() / d_old.std():.1f}x)")
    check(d_old.std() < 8.0, "this test is not exercising the bug: the old bracket already "
                             f"gave the terrain {d_old.std():.1f} DN of contrast")
    check(d_now.std() > 3.0 * d_old.std(),
          f"terrain contrast only {d_now.std() / d_old.std():.1f}x better at "
          f"{sf * 100:.0f}% snow — the shadows are still crushed")
    # Nine stops of terrain in a 16 DN window is the crush; a quarter of the range is not.
    check(rng_now > 60, f"terrain still spans only {rng_now:.0f} DN of the 0-255 range")

print("\n=== 5. no valid pixel is written as DN 0, which nodata=0 would turn into a HOLE ===")
snow, comp = scene(0.92)
path = os.path.join(TMP, "_hdr_bracket.tif")
rp._highlight_hdr(comp.rio.write_crs("EPSG:32607"), path, "EPSG:32607")
with rasterio.open(path) as ds:
    a = ds.read()
    check(ds.nodata == 0, f"the HDR product is written with nodata=0; got {ds.nodata}")
holes = int(((a == 0).all(0)).sum())
print(f"   {a.shape[1]}x{a.shape[2]} fully-valid frame: {holes} transparent pixels, "
      f"darkest DN {int(a.max(0).min())}")
check(holes == 0, f"{holes} valid pixels written as nodata — transparent holes in the "
                  f"darkest terrain, exactly where a scar is read")
os.remove(path)

if fails:
    print("\nFAILURES:")
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("\nHDR BRACKET VERIFIED — no-op below ~70% snow, spans the scene above it, no nodata holes")
