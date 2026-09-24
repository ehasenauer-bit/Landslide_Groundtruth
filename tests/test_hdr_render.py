"""The dock's Sentinel-2 HDR render must BE the PlanetScope tab's HDR curve.

hdr_render.py is a numpy port of review_package._highlight_hdr — the plugin can't
import the project (xarray/rioxarray/geopandas aren't in QGIS's Python). A port that
drifts would quietly give Sentinel-2 and PlanetScope different tone under the same
name, so this pins it:

  1. every constant equals review_package's;
  2. the rendered DNs agree with review_package._rolloff_rgb to within 1 DN
     (float32-vs-float64 rounding), and the pooled white anchor is identical;
  3. the structural promises: nodata -> 0, every valid pixel in 1..254 (no crushed
     shadow, no blown snow, no transparent hole), luminance monotonic in reflectance;
  4. the Sentinel-2 BOA offset: -1000 DN from baseline 04.00 (2022-01-25), 0 before,
     and a real dark pixel with one band at 0 is NOT nodata.

Needs review_package (xarray), which lives in the project venv, so re-exec there if we
were started under QGIS's Python.
"""
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import rioxarray  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with rioxarray; HDR render parity not checked")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import numpy as np
import xarray as xr

sys.path.insert(0, ROOT)
import review_package as rp

spec = importlib.util.spec_from_file_location(
    "hdr_render", os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth",
                               "hdr_render.py"))
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


def da(a):
    return xr.DataArray(a, dims=("band", "y", "x"),
                        coords={"band": ["red", "green", "blue"]})


# The single-curve HDR (HDR_LIFT & co.) replaced a Mertens exposure fusion in
# review_package; against a review_package that predates it there is nothing to be
# in step with, so sections 1-2 are skipped and the structural checks still run.
PARITY = hasattr(rp, "HDR_LIFT")
if not PARITY:
    print("NOTE: review_package has no single-curve HDR yet; parity checks skipped")

# 1. constants ------------------------------------------------------------------
for name in () if not PARITY else ("KNEE", "CONTRAST", "DESAT", "DESAT_ONSET", "BLUE_DESAT", "BLUE_DESAT_K",
             "BLUE_DESAT_LO", "BLUE_DESAT_HI", "CYAN_STRIP", "PINK_STRIP",
             "CAST_STRIP_K", "PINK_GATE_LO", "PINK_GATE_HI", "HDR_LIFT",
             "HDR_LIFT_TAPER", "HDR_WHITE_PCT", "HDR_WHITE_MIN", "HDR_WHITE_MAX",
             "HDR_WHITE_FALLBACK", "HDR_MIN_PIXELS", "HDR_SATURATION", "AUTO_SAMPLE"):
    check(getattr(hr, name) == getattr(rp, name),
          f"{name}: plugin {getattr(hr, name)} != review_package {getattr(rp, name)}")

# 2. parity on a scene with shadow, terrain, snow, over-bright snow, a cyan cast ------
rng = np.random.default_rng(7)
H = W = 240
refl = np.empty((3, H, W), "float32")
refl[:, :60] = rng.uniform(0.01, 0.08, (3, 60, W))           # shadow
refl[:, 60:120] = rng.uniform(0.08, 0.30, (3, 60, W))        # rock / vegetation
refl[:, 120:180] = rng.uniform(0.55, 0.95, (3, 60, W))       # snow
refl[:, 180:] = rng.uniform(0.90, 1.25, (3, 60, W))          # over-corrected snow
refl[2, 100:140] += 0.12                                     # cyan/blue cast band
refl[:, :4, :4] = np.nan                                     # nodata corner
pre, post = refl, np.clip(refl * 0.8, 0, None)
post[:, :4, :4] = np.nan

if PARITY:
    w_rp = rp.hdr_white([da(pre), da(post)])[0]
    w_hr = hr.hdr_white([pre, post])[0]
    check(abs(w_rp - w_hr) < 1e-6, f"pooled white differs: {w_rp} vs {w_hr}")

    s = rp.HDR_SATURATION
    ref = rp._rolloff_rgb(da(refl), white=w_rp, contrast=rp.CONTRAST, desat=rp.DESAT * s,
                          blue_desat=rp.BLUE_DESAT * s, cyan_strip=rp.CYAN_STRIP * s,
                          pink_strip=rp.PINK_STRIP * s, lift=rp.HDR_LIFT).clip(1, 254).values
    ref8 = np.nan_to_num(ref).astype("uint8")
    mine = hr.hdr_rgb(refl, w_hr)
    diff = np.abs(ref8.astype(int) - mine.astype(int))
    check(diff.max() <= 1, f"render differs from review_package by up to {diff.max()} DN")
    check((diff > 0).mean() < 0.001,
          f"{(diff > 0).mean():.3%} of pixels differ from review_package (rounding only "
          f"should touch <0.1%)")

# 3. structural promises --------------------------------------------------------
mine = hr.hdr_rgb(refl, hr.hdr_white([pre, post])[0])
valid = np.all(np.isfinite(refl), axis=0)
check(np.all(mine[:, ~valid] == 0), "nodata did not render as 0")
v = mine[:, valid]
check(v.min() >= 1 and v.max() <= 254,
      f"valid pixels reach {v.min()}..{v.max()} — 0 is a hole, 255 is blown")
ramp = np.tile(np.linspace(0.0, 1.6, 400, dtype="float32"), (3, 1, 1))
lum = hr.hdr_rgb(ramp, 1.0).astype(int).mean(axis=0)[0]
check(np.all(np.diff(lum) >= 0), "grey-ramp luminance is not monotonic")
# snow keeps separation: the top of the ramp (0.8..1.6) spans real DN, not a flat 254
top = lum[200:]
check(top.max() - top.min() >= 20,
      f"0.8-1.6 reflectance spans only {top.max() - top.min()} DN")

# 4. Sentinel-2 offset ----------------------------------------------------------
check(hr.s2_boa_offset({"date": "2026-07-10T21:35:31"}) == -1000.0, "2026 scene offset")
check(hr.s2_boa_offset({"date": "2021-08-01T21:00:00"}) == 0.0, "2021 scene offset")
check(hr.s2_boa_offset({"date": "2021-08-01", "boa_offset": -1000}) == -1000.0,
      "record's own boa_offset must win over the date rule")
check(hr.s2_boa_offset(None) == 0.0, "missing record")
dn = np.array([[[0, 2000]], [[1050, 2000]], [[0, 0]]], dtype="uint16")  # (3,1,2)
r = hr.s2_reflectance(dn, -1000.0)
check(np.all(np.isfinite(r[:, 0, 0])),
      "a dark pixel with two bands at 0 was treated as nodata")
check(abs(r[1, 0, 0] - 0.005) < 1e-6, f"offset not applied: {r[1, 0, 0]}")
hole = hr.s2_reflectance(np.zeros((3, 1, 1), "uint16"), -1000.0)
check(np.all(np.isnan(hole)), "all-zero pixel must be nodata")

print("FAIL" if fails else "PASS", f"— {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
