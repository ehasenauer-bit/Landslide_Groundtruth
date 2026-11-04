"""HDR must clip at NEITHER end, on every scene — and that has to be structural.

This replaces a Mertens exposure fusion (three bracketed renders blended through a
Laplacian pyramid). The fusion could not make that promise. Three separate hard clips
sat in its chain, and which one fired depended on the scene, so the same setting gave a
clean frame on one AOI and a ruined one on the next:

  * the pyramid collapse oversteps the input range — the documented main limitation of
    exposure fusion — putting 10.6% of a mixed rock-and-glacier frame above 1.0, every
    one of those pixels then flattened to paper white by the clip after it;
  * the contrast-normalising finish was a linear gain about the mean with a hard clip at
    both ends, which on the 2026-09-08 Wrangell frame crushed BOTH at once: 0.81% of
    valid pixels at DN >= 254 and 1.45% at DN <= 1, in one image;
  * and stacking two tone curves in sequence is a known way to get unpredictable tone.

What replaced it is one pass: _shoulder (asymptotic, so u < 1 for any finite input) then
log1p (monotonic, 0 only at 0). Neither endpoint is reachable, so "no DN 0, no DN 255"
is a property of the curve rather than of the scene — which is the only way to promise
it on EVERY run. The DN 1/254 clamp in _highlight_hdr absorbs rounding; section 3 asserts
the curve does not need it. DN 0 matters twice over, because nodata is 0: a valid pixel
written as 0 is a transparent HOLE exactly where the deepest shadow is.

The trade this makes is deliberate and also asserted: holding both ends costs global
contrast. Section 4 pins the win against HONC's cube root at both ends of the range.

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
        print("SKIP: no venv with rioxarray; HDR curve check not run")
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
rng = np.random.default_rng(5)
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _fbm(n, octaves=6):
    f = np.zeros((n, n))
    for o in range(octaves):
        s = 2 ** o
        g = rng.normal(size=(s + 1, s + 1))
        i = (np.arange(n) * s / n).astype(int)
        f += g[np.ix_(i, i)] / (o + 1)
    return (f - f.min()) / (f.max() - f.min())


def scene(snow_frac, n=384, hi=1.10, lo=0.010):
    """(snow mask, composite). Terrain spans `lo`..0.11 and snow 0.55..`hi`, so the frame
    really does carry both ends of the range the curve has to hold."""
    f = _fbm(n)
    thr = np.quantile(f, 1.0 - snow_frac)
    snow = f >= thr
    tex = rng.normal(0, 1, (n, n))
    refl = np.where(snow, 0.55 + (hi - 0.55) * (f - thr) / max(1 - thr, 1e-6) + 0.012 * tex,
                          lo + (0.11 - lo) * f / max(thr, 1e-6) + 0.004 * tex)
    refl = np.clip(refl, 0.003, 1.45)
    arr = np.stack([refl * 1.05, refl, refl * 0.95])
    return snow, xr.DataArray(
        arr, dims=("band", "y", "x"),
        coords={"band": ["red", "green", "blue"],
                "y": np.arange(n) * -10.0 + 7_000_000.0,
                "x": np.arange(n) * 10.0 + 500_000.0})


# ---------------------------------------------------------------- the surface table
SURF = {"deep shadow": 0.015, "dark rock": 0.040, "lit rock": 0.110, "moraine": 0.200,
        "dirty ice": 0.350, "firn": 0.620, "fresh snow": 0.850, "sunlit snow": 1.050,
        "atco overshoot": 1.250}


def curve_dn(refl, white):
    """The HDR curve alone, on bare reflectance — no desat/cast-strip, which touch chroma."""
    t = xr.DataArray(np.asarray(refl, float) / white)
    u = rp._shoulder(t, rp.KNEE).clip(0.0, None).values
    lifted = np.log1p(rp.HDR_LIFT * u) / float(np.log1p(rp.HDR_LIFT))
    return 255.0 * (u + (lifted - u) * np.clip(1.0 - u, 0.0, None) ** rp.HDR_LIFT_TAPER)


print("=== 1. lift=0 is an exact no-op, so every other render is untouched ===")
_, comp = scene(0.5)
a = rp._rolloff_rgb(comp).values
b = rp._rolloff_rgb(comp, lift=0.0).values
check(np.array_equal(np.nan_to_num(a, nan=-1.0), np.nan_to_num(b, nan=-1.0)),
      "lift=0 must be bit-identical to not passing lift at all")
print(f"   knee render bit-identical with and without lift=0: "
      f"{np.array_equal(np.nan_to_num(a, nan=-1), np.nan_to_num(b, nan=-1))}")

print("\n=== 2. the white anchor is fitted once across BOTH sides ===")
# A pre/post pair whose post side is brighter must still share one anchor, or the swipe
# reads the tone step as change. Same rationale as auto_stretch.
_, pre = scene(0.45)
_, post = scene(0.80)
w_both, note = rp.hdr_white([pre, post])
w_pre = rp.hdr_white([pre])[0]
w_post = rp.hdr_white([post])[0]
print(f"   pre-only {w_pre:.3f}, post-only {w_post:.3f}, pooled {w_both:.3f}")
print(f"   note: {note}")
check(min(w_pre, w_post) <= w_both <= max(w_pre, w_post),
      f"pooled white {w_both:.3f} is not between the two per-side fits")
check(rp.HDR_WHITE_MIN <= w_both <= rp.HDR_WHITE_MAX,
      f"pooled white {w_both:.3f} escaped its clamps")
check(rp.hdr_white([None, None])[0] == rp.HDR_WHITE_FALLBACK,
      "an all-None side list must fall back, not raise")
tiny = pre.isel(y=slice(0, 8), x=slice(0, 8))
check(rp.hdr_white([tiny])[0] == rp.HDR_WHITE_FALLBACK,
      "a frame too small for a percentile must fall back")

print("\n=== 3. neither endpoint is reachable across the PHYSICAL range ===")
# 2.0 is already double any real surface reflectance (snow tops out near 1.1, and an
# atmospheric-correction overshoot near 1.3). Past ~10x the white point the shoulder's
# exp() underflows and pins at exactly 1.0 — that is the one case the DN 254 clamp in
# _highlight_hdr is load-bearing for, and section 5 proves it holds on a written raster.
for white in (0.30, 0.60, 0.90, 1.30):
    dn = curve_dn(list(SURF.values()) + [0.0, 2.0], white)
    print(f"   white={white:4.2f}: " + " ".join(f"{d:5.1f}" for d in dn[:-2])
          + f"   | refl 0 -> {dn[-2]:.3f}, refl 2.0 -> {dn[-1]:.4f}")
    check(dn[-1] < 255.0, f"curve reached DN 255 at white={white} on reflectance 2.0, "
                          f"which is inside the range a real scene can produce")
    check(int(dn[-1]) <= 254, f"reflectance 2.0 truncates to {int(dn[-1])} at white={white}")
    check(dn[-2] == 0.0, f"curve must map reflectance 0 to 0, got {dn[-2]}")
    check(np.all(np.diff(dn[:-2]) > 0), f"curve is not monotonic at white={white}")

print("\n=== 4. both ends stay readable — the point of the whole exercise ===")
w = 1.20
dn = {k: float(curve_dn([v], w)[0]) for k, v in SURF.items()}
honc = {k: 255.0 * min(np.cbrt(0.6 * v), 1.0) for k, v in SURF.items()}
knee = {k: float(255.0 * min(rp._shoulder(xr.DataArray(np.array([v / rp.WHITE])),
                                          rp.KNEE).values[0], 1.0))
        for k, v in SURF.items()}
print(f"   {'surface':15} {'refl':>5} {'knee W=.30':>11} {'HONC cbrt':>10} {'HDR curve':>10}")
for k, v in SURF.items():
    print(f"   {k:15} {v:5.3f} {knee[k]:11.0f} {honc[k]:10.0f} {dn[k]:10.0f}")
snow_span = dn["sunlit snow"] - dn["firn"]
knee_span = knee["sunlit snow"] - knee["firn"]
shadow_span = dn["lit rock"] - dn["deep shadow"]
honc_shadow = honc["lit rock"] - honc["deep shadow"]
print(f"\n   snow   0.62->1.05 : knee {knee_span:5.1f} DN  ->  HDR {snow_span:5.1f} DN")
print(f"   shadow 0.015->0.11: HONC {honc_shadow:5.1f} DN  ->  HDR {shadow_span:5.1f} DN")
honc_snow = honc["sunlit snow"] - honc["firn"]
print(f"   snow   0.62->1.05 : HONC {honc_snow:5.1f} DN  ->  HDR {snow_span:5.1f} DN")
check(knee_span < 8, "the knee curve is supposed to smear snow; this test is not "
                     f"exercising the problem (span {knee_span:.1f} DN)")
check(snow_span > 20, f"snow spans only {snow_span:.1f} DN — still a white smear")
# The taper exists so the lift does not buy shadows by flattening snow. On two real S2
# frames it beats HONC at BOTH ends; on the surface table it must at least not lose either.
check(shadow_span >= honc_shadow,
      f"shadow separation {shadow_span:.1f} DN is below HONC's {honc_shadow:.1f} DN")
check(snow_span >= honc_snow,
      f"snow separation {snow_span:.1f} DN is below HONC's {honc_snow:.1f} DN — the lift "
      f"is buying shadows by flattening the snow, which HDR_LIFT_TAPER exists to prevent")

print("\n=== 4b. the curve is monotonic over its whole domain ===")
u = np.linspace(0.0, 1.0, 200001)
lifted = np.log1p(rp.HDR_LIFT * u) / float(np.log1p(rp.HDR_LIFT))
y = u + (lifted - u) * np.clip(1.0 - u, 0.0, None) ** rp.HDR_LIFT_TAPER
print(f"   lift={rp.HDR_LIFT} taper={rp.HDR_LIFT_TAPER}: y(0)={y[0]:.3f} y(1)={y[-1]:.3f} "
      f"min step {np.diff(y).min():+.3e}")
check(np.all(np.diff(y) > -1e-15),
      "the tapered lift is not monotonic — it would reorder tones and invert texture")
check(y[0] == 0.0 and abs(y[-1] - 1.0) < 1e-12,
      f"the lift must pin both endpoints, got y(0)={y[0]} y(1)={y[-1]}")

print("\n=== 5. a written raster, on scenes from all-terrain to all-snow ===")
for snow_frac, hi, lo in ((0.05, 0.30, 0.010), (0.45, 1.05, 0.010),
                          (0.80, 1.15, 0.008), (0.97, 1.30, 0.020)):
    snow, comp = scene(snow_frac, hi=hi, lo=lo)
    comp = comp.rio.write_crs("EPSG:32607")
    white, _ = rp.hdr_white([comp])
    path = os.path.join(TMP, "_hdr_curve.tif")
    rp._highlight_hdr(comp, path, "EPSG:32607", white=white)
    with rasterio.open(path) as ds:
        arr = ds.read()
        nodata = ds.nodata
    os.remove(path)
    holes = int((arr == 0).all(0).sum())
    blown = int((arr == 255).all(0).sum())
    y = arr.mean(0)
    dark = y[~snow] if (~snow).any() else y
    bright = y[snow] if snow.any() else y
    print(f"   snow {snow_frac * 100:5.1f}% white={white:4.2f}: holes {holes}, blown {blown}, "
          f"p1 {np.percentile(y, 1):5.1f} p50 {np.percentile(y, 50):5.1f} "
          f"p99 {np.percentile(y, 99):5.1f} | terrain std {dark.std():5.1f} "
          f"snow std {bright.std():5.1f}")
    check(nodata == 0, f"HDR product must keep nodata=0; got {nodata}")
    check(holes == 0, f"{holes} valid pixels at DN 0 — transparent holes in the shadows")
    check(blown == 0, f"{blown} valid pixels at DN 255 — blown highlights")
    if snow.any() and (~snow).any():
        check(dark.std() > 8, f"terrain came out flat ({dark.std():.1f} DN std)")
        check(bright.std() > 4, f"snow came out flat ({bright.std():.1f} DN std)")

if fails:
    print("\nFAILURES:")
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("\nHDR CURVE VERIFIED — one pass, no reachable endpoint, both ends readable")
