"""The search's cloud read must come from the COG overview, area-averaged, on the SCL grid.

A Sentinel-2 search spent ~94% of its time on one step: stackstac warping every full-res
10 m R/G/NIR pixel down to the 100 m grid OmniCloudMask runs on (Willow2: 698 s of 742 s
of reads, 24 scenes, 196 s wall). Reading the COG's own 40 m overview and averaging it
onto the grid cut the read 6.1x on 127 scenes over six AOIs, and its cloud % sat closer to
OCM run at twice the resolution (mean error 4.2 vs 5.6 points). The 80 m overview was
worse than both, so the level rule matters.

Pinned here, on a synthetic local COG (no network, no licensed pixels):
  1. the level rule — coarsest overview at most half the grid (40 m for 100, 20 m for 50),
     full resolution when no overview qualifies;
  2. the read is AREA-AVERAGED, not point-sampled: a 10 m checkerboard of 1000/3000 must
     come back ~2000, where every-10th-pixel sampling returns 1000 or 3000;
  3. it opens the overview it picked, lands on the grid it was given, and is 0 off the
     footprint (OCM's no-data);
  4. _aoi_cloud_fractions uses it for Sentinel-2 on the classification read's own grid,
     keeps stackstac for Landsat, and every search read carries the HTTP timeout.

Needs the imagery stack (rasterio/stackstac), which lives in the project venv, not in
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
        print("SKIP: no venv with stackstac; overview-read check not run")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import datetime as dt
import tempfile
import warnings
from types import SimpleNamespace

import numpy as np
import rasterio
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling

warnings.simplefilter("ignore")
sys.path.insert(0, ROOT)
import imagery as im

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("=== 1. which overview ===")
S2 = [2, 4, 8, 16]                               # every MPC Sentinel-2 10 m band
check(im._overview_level(S2, 10.0, 100.0) == 1, "100 m grid -> the 40 m overview (index 1)")
check(im._overview_level(S2, 10.0, 50.0) == 0, "50 m grid (a 5 km AOI) -> the 20 m overview")
check(im._overview_level(S2, 10.0, 30.0) is None, "30 m grid -> full resolution")
check(im._overview_level([], 10.0, 100.0) is None, "a COG with no overviews -> full resolution")
check(im._overview_level(S2, 10.0, 100.0) != 2,
      "never the 80 m overview on a 100 m grid (it read 3% against a 57% reference)")

print("\n=== 2-3. the read itself, on a synthetic 10 m COG ===")
TMP = tempfile.mkdtemp(prefix="ovr_read_")
path = os.path.join(TMP, "B04.tif")
W = H = 1600                                     # 16 km at 10 m
x0, y0 = 500000.0, 6880000.0
yy, xx = np.mgrid[0:H, 0:W]
checker = np.where((yy + xx) % 2 == 0, 1000, 3000).astype("uint16")
with rasterio.open(path, "w", driver="GTiff", width=W, height=H, count=1, dtype="uint16",
                   crs="EPSG:32606", transform=Affine(10.0, 0, x0, 0, -10.0, y0),
                   tiled=True, blockxsize=512, blockysize=512) as dst:
    dst.write(checker, 1)
    dst.build_overviews(S2, Resampling.average)

opened = []
real_open = im.rasterio.open
im.rasterio.open = lambda *a, **k: (opened.append(k.get("OVERVIEW_LEVEL")), real_open(*a, **k))[1]
grid = Affine(100.0, 0, x0 + 2000.0, 0, -100.0, y0 - 2000.0)        # 2 km inside the COG
out = im._overview_read(path, 32606, grid, (60, 60), 100.0)
im.rasterio.open = real_open
check(out.shape == (60, 60), f"lands on the grid it was given: {out.shape}")
check(1 in opened, f"opened the 40 m overview (OVERVIEW_LEVEL 1), opens: {opened}")
check(abs(float(out.mean()) - 2000) < 5 and float(out.std()) < 5,
      f"area-averaged: the 1000/3000 checkerboard reads {out.mean():.0f} +/- {out.std():.1f} "
      f"(point sampling gives 1000 or 3000)")

edge = Affine(100.0, 0, x0 + W * 10.0 - 3000.0, 0, -100.0, y0 - 2000.0)   # last 3 km inside
out = im._overview_read(path, 32606, edge, (20, 60), 100.0)
check(out[:, :28].min() > 0 and out[:, 32:].max() == 0,
      "0 off the footprint (OCM's no-data), data on it")

print("\n=== 4. the search uses it for Sentinel-2 only, on the SCL grid ===")
N = 30
TF = Affine(100.0, 0, x0, 0, -100.0, y0)
stacks, overview_calls = [], []


def fake_stack(items, assets, **kw):
    stacks.append((list(assets), kw.get("gdal_env")))
    fill = 1 if assets == ["qa_pixel"] else 4 if assets == ["SCL"] else 2000
    return xr.DataArray(
        np.full((1, len(assets), N, N), float(fill)), dims=("time", "band", "y", "x"),
        coords=dict(time=[np.datetime64("2026-10-08")], id=("time", [items[0].id]),
                    band=list(assets), y=y0 - 100.0 * np.arange(N),
                    x=x0 + 100.0 * np.arange(N)),
        attrs=dict(transform=TF))


def fake_overview(href, epsg, transform, shape, res):
    overview_calls.append((href, transform, tuple(shape), res))
    return np.full(shape, 2000.0)


def item(iid, bands):
    return SimpleNamespace(id=iid, datetime=dt.datetime(2026, 10, 8),
                           properties={"s2:processing_baseline": "05.13"},
                           assets={b: SimpleNamespace(href=f"https://example/{iid}/{b}")
                                   for b in bands})


def no_ocm():
    raise RuntimeError("OCM disabled in this test")


im.stackstac.stack, im._overview_read, im._ocm_models = fake_stack, fake_overview, no_ocm
im._aoi_cloud_fractions([item("S2", ["B04", "B03", "B08"])], 62.05, -149.2, 15.0, "s2")
check([c[0].rsplit("/", 1)[1] for c in overview_calls] == ["B04", "B03", "B08"],
      f"Sentinel-2 R/G/NIR via the overview read: {[c[0] for c in overview_calls]}")
check(all(c[1] == TF and c[2] == (N, N) and c[3] == 100.0 for c in overview_calls),
      "... on the classification read's own transform and shape, at the AOI grid")
check([s[0] for s in stacks] == [["SCL"]], f"... and stackstac reads only the SCL: {stacks}")
stacks.clear(); overview_calls.clear()
im._aoi_cloud_fractions([item("LS", ["red", "green", "nir08"])], 62.05, -149.2, 15.0,
                        "landsat")
check(not overview_calls and [s[0] for s in stacks] == [["qa_pixel"], ["red", "green", "nir08"]],
      "Landsat keeps its stackstac read (not measured, so not switched)")
envs = [s[1] for s in stacks]
check(all(e is im._SEARCH_ENV for e in envs) and
      im._SEARCH_ENV._always.get("GDAL_HTTP_TIMEOUT") == "60",
      "every search stackstac read carries the 60 s HTTP timeout")

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)
