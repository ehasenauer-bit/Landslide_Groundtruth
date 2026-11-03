"""A scene whose bands were cut to 8 bits must be flagged, never rendered black.

On 2026-11-02 the Willow2 post layer (S2C 2026-10-28, tile 06VUP) came out solid
black. Planetary Computer had served that whole datatake with every reflectance band
equal to the true DN mod 256 — checked pixel-for-pixel against AWS Earth Search's copy
of the same tile. The -1000 BOA offset then made every pixel negative, the HDR mapped
them all to DN 1, and dBright became just the pre brightness with its sign flipped.
Worse, OmniCloudMask read the noise as 0% cloud, so the broken scene won the ★.

Pinned here, all on synthetic stacks (stackstac.stack is swapped for a fake, so no
network and no licensed pixels):
  1. the decision rule fires on wrapped data and NEVER on honest dark data — a
     pre-04.00 scene over dark water legitimately sits under 255, so it is not judged;
  2. a Run drops the corrupt scene and composites the rest, and the dropped scene's
     id and date leave the result (they name the layer files);
  3. a side with nothing but corrupt scenes raises CorruptSceneError naming them (a
     hand-picked Run reports why), while the auto path treats it as no scenes;
  4. the search flags the scene as bad_data and keeps it away from OmniCloudMask.

Needs the imagery stack (xarray/stackstac), which lives in the project venv, not in
QGIS's Python — so re-exec there if we were started under the wrong one.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import stackstac  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with stackstac; wrapped-band check not run")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import datetime as dt
import warnings
from types import SimpleNamespace

import numpy as np
import xarray as xr

warnings.simplefilter("ignore")
sys.path.insert(0, ROOT)
import imagery as im

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


rng = np.random.default_rng(7)
N = 40                                           # a 40 x 40 px AOI
LAT, LON, R = 62.05, -149.2, 5.0


def scene(iid, day, baseline="05.13"):
    return SimpleNamespace(id=iid, datetime=dt.datetime(2026, 10, day, 21, 36),
                           properties={"s2:processing_baseline": baseline},
                           assets={}, geometry=None, bbox=None)


def true_dn():
    """Plausible baseline-04.00+ L2A DN (offset 1000 baked in): snow in the top half,
    rock and shadow below — the brightest pixels ~10 000, the darkest just over 1000."""
    a = np.empty((N, N))
    a[: N // 2] = rng.normal(9000, 800, (N // 2, N))
    a[N // 2:] = rng.normal(1500, 300, (N - N // 2, N))
    return np.clip(a, 1001, 20000).round()


GOOD_PRE, GOOD_POST = scene("S2B_good_pre", 1), scene("S2C_good_post", 8)
WRAPPED = scene("S2C_wrapped_post", 28)
DARK_OLD = scene("S2A_dark_water_2019", 20, baseline="02.12")   # zero reflectance = DN 0
PIXELS = {GOOD_PRE.id: true_dn(), GOOD_POST.id: true_dn(),
          WRAPPED.id: true_dn() % 256,                 # what MPC served on 2026-10-28
          DARK_OLD.id: rng.normal(150, 40, (N, N)).clip(1, 255).round()}


def fake_stack(items, assets, epsg=None, resolution=None, bounds_latlon=None,
               chunksize=None, rescale=False, fill_value=np.nan):
    """stackstac.stack's shape and its one habit that matters here: the time axis is
    sorted by DATE, whatever order the items came in, and carries an `id` coord."""
    items = sorted(items, key=lambda i: i.datetime)
    cube = np.empty((len(items), len(assets), N, N))
    for t, it in enumerate(items):
        for b, a in enumerate(assets):
            # SCL 4 (vegetation) = measured and clear; QA_PIXEL 0 = not fill
            cube[t, b] = 4 if a == "SCL" else 0 if a == "qa_pixel" else PIXELS[it.id]
    x0, y0 = 500000.0, 6880000.0
    return xr.DataArray(
        cube, dims=("time", "band", "y", "x"),
        coords=dict(time=[np.datetime64(i.datetime) for i in items],
                    id=("time", [i.id for i in items]), band=list(assets),
                    y=y0 - 10.0 * np.arange(N), x=x0 + 10.0 * np.arange(N))).chunk()


im.stackstac.stack = fake_stack

print("=== 1. the rule: wrapped data trips it, honest dark data never does ===")
s2, old, ls = 1000.0, 0.0, im._zero_dn(None, "landsat")
check(round(ls) == 7273, f"Landsat C2 zero reflectance is DN {ls:.0f}")
check(im._zero_dn(WRAPPED, "s2") == 1000.0 and im._zero_dn(DARK_OLD, "s2") == 0.0,
      "S2 zero reflectance is DN 1000 from baseline 04.00, DN 0 before it")
check(im._is_wrapped(255, 5000, s2), "modern S2, nothing above 255 -> wrapped")
check(im._is_wrapped(267, 5000, s2), "... 267, the overview overshoot seen on MPC -> wrapped")
check(not im._is_wrapped(1001, 5000, s2),
      "modern S2 whose brightest pixel is reflectance 0.0001 -> NOT wrapped")
check(im._is_wrapped(255, 5000, ls), "Landsat, nothing above 255 -> wrapped")
check(not im._is_wrapped(200, 5000, old),
      "pre-04.00 S2 over dark water (DN 200 = reflectance 0.02) -> NOT judged")
check(not im._is_wrapped(255, 50, s2), "fewer than WRAPPED_MIN_PIXELS -> not judged")
check(not im._is_wrapped(float("nan"), 0, s2), "no data at all -> not judged")

print("\n=== 2. a Run drops the corrupt scene and composites the rest ===")
try:
    im._composite([WRAPPED, GOOD_POST], LAT, LON, R, "s2")   # NOT date order
    check(False, "_composite must raise on a wrapped scene")
except im.CorruptSceneError as e:
    check(e.ids == [WRAPPED.id], f"raised naming only the wrapped scene: {e.ids}")
res = im._composite_result([GOOD_PRE], [WRAPPED, GOOD_POST], LAT, LON, R, "s2")
check(res is not None, "the run still produces a result")
check(res["post_scenes"] == [GOOD_POST.id],
      f"the dropped scene left post_scenes: {res['post_scenes']}")
check(res["post_dates"] == ["2026-10-08"],
      f"... and post_dates, which name the layer files: {res['post_dates']}")
alone = im._composite([GOOD_POST], LAT, LON, R, "s2")
check(np.allclose(res["post"].values, alone.values, equal_nan=True),
      "the post composite is the good scene alone, not a blend with the noise")
red = res["post"].sel(band="red").values
check(np.nanmax(red) > 0.5 and np.nanmin(red) > 0,
      f"... with real reflectance (red {np.nanmin(red):.3f}..{np.nanmax(red):.3f}), "
      f"not the all-negative black")
check(float(res["dbright"].max()) > 0,
      "dBright is no longer just -pre (its max was exactly 0 on Willow2)")
check(im._composite([DARK_OLD], LAT, LON, R, "s2") is not None,
      "an honest pre-04.00 dark-water scene still composites")

print("\n=== 3. nothing usable left on a side ===")
try:
    im._composite_result([GOOD_PRE], [WRAPPED], LAT, LON, R, "s2")
    check(False, "a side of only corrupt scenes must raise")
except im.CorruptSceneError as e:
    check(e.ids == [WRAPPED.id] and WRAPPED.id in str(e) and "pick another" in str(e),
          f"raised with a message a hand-picked run can show: {str(e)[:70]}...")
real_search = im.search_scenes
EVENT = dt.datetime(2026, 10, 3)
# fetch_event passes (lat, lon, radius, start, end, ...) positionally; pre ends before the event
im.search_scenes = lambda lat, lon, r, start, end, *a, **k: (
    [GOOD_PRE] if end < EVENT else [WRAPPED])
try:
    out = im.fetch_event(LAT, LON, R, EVENT, prefer="s2")
    check(out is None, "the auto path treats an all-corrupt side as no imagery (no raise)")
except im.CorruptSceneError:
    check(False, "the auto path must not raise CorruptSceneError")
finally:
    im.search_scenes = real_search

print("\n=== 4. the search flags it and keeps it away from OmniCloudMask ===")


def no_ocm():
    raise RuntimeError("OCM disabled in this test")


im._ocm_models = no_ocm
frac, _note = im._aoi_cloud_fractions([GOOD_POST, WRAPPED, DARK_OLD], LAT, LON, R, "s2")
check(all(len(v) == 4 for v in frac.values()), "every row is (cloud, snow, source, bad_data)")
check(frac[WRAPPED.id][3] and "corrupt" in frac[WRAPPED.id][3],
      f"wrapped scene flagged: {str(frac[WRAPPED.id][3])[:60]}...")
check(frac[GOOD_POST.id][3] is None and frac[DARK_OLD.id][3] is None,
      "the good scene and the honest dark one are not flagged")
cand = im._stac_candidate(WRAPPED, EVENT, "Sentinel-2",
                          *frac[WRAPPED.id])
check(cand["bad_data"] == frac[WRAPPED.id][3], "the candidate row carries bad_data")
check(im._stac_candidate(GOOD_POST, EVENT, "Sentinel-2",
                         *frac[GOOD_POST.id])["bad_data"] is None,
      "... and a clean row carries None")

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)
