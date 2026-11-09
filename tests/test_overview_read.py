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
  4. with the data API unreachable, _aoi_cloud_fractions uses it for Sentinel-2 on the
     classification read's own grid, keeps stackstac for Landsat, and every search read
     carries the HTTP timeout;
  5. normally the pixels come through Planetary Computer's data API instead (2026-11-09:
     the blob store did ~0.5 MB/s per connection from Fairbanks, a 15 km search was
     ~10 MB a scene): class band at native resolution + mask, R/G/NIR at half the grid
     pixel then a no-data-aware 2x2 mean.

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

# Off-SWATH zeros inside the file (MPC's COGs declare no nodata value). Without
# src_nodata=0 the warper averages them as data and nudges the result to 1, and OCM
# took a 4.6% sliver for a full frame (read a 32%-cloudy scene as 0.5%).
swath = os.path.join(TMP, "swath.tif")
half = checker.copy()
half[:, : W // 2] = 0
with rasterio.open(swath, "w", driver="GTiff", width=W, height=H, count=1, dtype="uint16",
                   crs="EPSG:32606", transform=Affine(10.0, 0, x0, 0, -10.0, y0),
                   tiled=True, blockxsize=512, blockysize=512) as dst:
    dst.write(half, 1)
    dst.build_overviews(S2, Resampling.average)
out = im._overview_read(swath, 32606, grid, (60, 60), 100.0)       # cols 0-59 = 2-8 km
check(out[:, :58].max() == 0,
      f"off-swath zeros INSIDE the file stay 0, not 1 (max {out[:, :58].max()})")
wide = im._overview_read(swath, 32606, grid, (60, 120), 100.0)     # cols 0-119 = 2-14 km
check(wide[:, :58].max() == 0 and abs(float(wide[:, 62:].mean()) - 2000) < 5,
      "... and the on-swath half still reads ~2000")

print("\n=== 4. data API down: the direct reads, Sentinel-2 overview on the SCL grid ===")
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


def api_down(*a, **k):
    raise ConnectionError("data API unreachable in this test")


real_api_read = im._api_read
im.stackstac.stack, im._overview_read, im._ocm_models = fake_stack, fake_overview, no_ocm
im._api_read = api_down                    # section 4 pins the FALLBACK (direct) reads
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
      "Landsat's direct read is still stackstac")
envs = [s[1] for s in stacks]
check(all(e is im._SEARCH_ENV for e in envs) and
      im._SEARCH_ENV._always.get("GDAL_HTTP_TIMEOUT") == "60",
      "every search stackstac read carries the 60 s HTTP timeout")

print("\n=== 5. the data API read (the normal path) ===")
# A fake server: whatever is asked for, answer the right shape and record the request.
api_calls = []


def fake_api(collection, item_id, assets, epsg, transform, shape, width, height,
             resampling, mask):
    api_calls.append(dict(collection=collection, assets=list(assets), size=(height, width),
                          resampling=resampling, mask=mask, transform=transform,
                          shape=tuple(shape)))
    if mask:                                    # class band + mask: left half off-swath
        a = np.zeros((2, height, width), "uint16")
        a[0] = 4
        a[1, :, width // 2:] = 255
        return a
    a = np.zeros((len(assets), height, width), "uint16")
    a[:, :, width // 2:] = np.where(np.indices((height, width - width // 2)).sum(0) % 2, 3000, 1000)
    a[:, :, width // 2] = 0                     # one no-data column inside a 2x2 block
    return a


im._api_read = fake_api
stacks.clear(); overview_calls.clear()
out, _note = im._aoi_cloud_fractions([item("S2", ["B04", "B03", "B08"])], 62.05, -149.2,
                                     15.0, "s2")
check(not overview_calls, "Sentinel-2 with the API up: no direct overview read")
# the two requests are in flight at once, so pick them by band, not by order
cls_call = next(c for c in api_calls if c["assets"] == ["SCL"])
rgn_call = next(c for c in api_calls if c["assets"] != ["SCL"])
check(cls_call["assets"] == ["SCL"] and cls_call["resampling"] == "nearest"
      and cls_call["mask"] and cls_call["size"] == (N * 5, N * 5),
      f"SCL asked for at its native 20 m, nearest, with a mask: {cls_call['size']}")
check(rgn_call["assets"] == ["B04", "B03", "B08"] and rgn_call["resampling"] == "average"
      and rgn_call["size"] == (N * 2, N * 2),
      f"R/G/NIR asked for at HALF the grid pixel, averaged: {rgn_call['size']}")
check(all(c["transform"] == TF and c["shape"] == (N, N) for c in api_calls),
      "... both on the stack's own grid")
check(out["S2"][0] is not None and out["S2"][2] == "scl",
      f"off-swath half dropped from the denominator, cloud % still measured: {out['S2']}")

rgn = im._api_rgn("sentinel-2-l2a", "S2", ["B04", "B03", "B08"], 32606, TF, (N, N))
check(rgn.shape == (3, N, N) and rgn[:, :, : N // 2].max() == 0,
      "R/G/NIR: 0 where the scene has no data")
check(abs(float(rgn[:, :, N // 2 + 1:].mean()) - 2000) < 1,
      "R/G/NIR: 2x2 mean of the 1000/3000 checkerboard is 2000")
check(float(rgn[:, :, N // 2].min()) >= 1000,
      "R/G/NIR: a no-data pixel inside a 2x2 block is left out of the mean, not averaged in")
cls = im._api_class("sentinel-2-l2a", "S2", "SCL", 32606, TF, (N, N), 20, 0)
check(cls.shape == (N, N) and (cls[:, : N // 2] == 0).all() and (cls[:, N // 2:] == 4).all(),
      "SCL: sampled back onto the grid, fill value off the footprint")
api_calls.clear()
im._api_class("landsat-c2-l2", "LS", "qa_pixel", 32606, TF, (N, N), 30, 1)
check(api_calls[0]["size"] == (100, 100), f"Landsat QA at its native 30 m: {api_calls[0]['size']}")

im._api_read = real_api_read
try:
    im._api_read("sentinel-2-l2a", "S2", ["B04"], 32606, TF, (N, N), 5000, 5000, "nearest", True)
    check(False, "an over-size request must raise before touching the network")
except ValueError:
    check(True, "an over-size request raises (-> direct read) before touching the network")

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)
