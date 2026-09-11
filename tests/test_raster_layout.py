"""Every raster the review package writes must be PANNABLE and LOSSLESS.

The bug this guards: a bare `.rio.to_raster(path, driver="GTiff")` takes GDAL's
defaults — one-row strips, no overviews, no compression. On a stitched
multi-tile output (9344 x 9329 dBright) that layout made QGIS unusable to pan,
because the block is a whole raster-width row: filling a 1900 px window drags in
the full width once per row and thrashes GDAL's block cache, so the next pan
step re-reads everything.

So: assert the layout is tiled and overviewed, that the 8-bit imagery survives
bit-identically, and that the float64 -> float32 narrowing lands where it is
meant to and NOWHERE else. Real GeoTIFFs through the real writer — no mocking;
the point is what lands on disk.

Needs the imagery stack (rioxarray), which lives in the project venv, not in
QGIS's Python — so re-exec there if we were started under the wrong one.
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
        print("SKIP: no venv with rioxarray; raster-layout check not run")
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
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _grid(arr):
    """Wrap a 2-D or 3-D array as a georeferenced DataArray on a 10 m UTM grid."""
    ny, nx = arr.shape[-2:]
    coords = {"y": np.arange(ny) * -10.0 + 7_000_000.0,
              "x": np.arange(nx) * 10.0 + 500_000.0}
    dims = ("y", "x")
    if arr.ndim == 3:
        dims = ("band",) + dims
        coords["band"] = list(range(1, arr.shape[0] + 1))
    return xr.DataArray(arr, dims=dims, coords=coords).rio.write_crs("EPSG:32607")


# ---------------------------------------------------------------- float change raster
# float64 with NaN holes, like a real dBright/dNDSI.
rng = np.random.default_rng(0)
f64 = rng.normal(0.0, 0.2, (2600, 2600)).astype("float64")
f64[500:560, :] = np.nan                       # a nodata stripe
path = os.path.join(TMP, "_layout_float.tif")
rp._to_tif(_grid(f64), path, "EPSG:32607")

with rasterio.open(path) as ds:
    check(ds.block_shapes[0] == (rp.TILE, rp.TILE),
          f"float raster not tiled: block {ds.block_shapes[0]}, want {(rp.TILE, rp.TILE)}")
    check(ds.overviews(1) != [], "float raster has no overviews")
    check(ds.dtypes[0] == "float32",
          f"float64 change raster should be narrowed to float32, got {ds.dtypes[0]}")
    check(ds.shape == f64.shape, f"full resolution lost: {ds.shape} vs {f64.shape}")
    check((ds.compression.name if ds.compression else "") .lower() == "zstd",
          f"unexpected compression: {ds.compression}")
    back = ds.read(1)

# the ONLY loss allowed is the float32 narrowing itself: what comes back must equal
# the float32 cast exactly (ZSTD is lossless), not merely be close to it.
want = f64.astype("float32")
check(np.array_equal(np.nan_to_num(back, nan=-12345.0).view(np.uint32),
                     np.nan_to_num(want, nan=-12345.0).view(np.uint32)),
      "round trip is not bit-identical to the float32 cast — ZSTD must be lossless")
check(np.array_equal(np.isnan(back), np.isnan(f64)), "NaN/nodata pattern changed")

# ... and the narrowing must stay far inside the thresholds anything downstream uses
# (dock.CHANGE_RAMPS breaks, fusion_core.DEFAULT_FLOORS). See the measurement in
# review_package: worst real-scar disagreement was one 10 m pixel.
err = np.nanmax(np.abs(f64 - want.astype("float64")))
check(err < 1e-5, f"float32 error {err:.2e} is too large for a -0.05 threshold")

# the escape hatch has to actually work, or the comment promising it is a lie
keep = os.path.join(TMP, "_layout_keep64.tif")
rp._to_tif(_grid(f64), keep, "EPSG:32607", keep_float64=True)
with rasterio.open(keep) as ds:
    check(ds.dtypes[0] == "float64",
          f"keep_float64=True did not preserve float64: {ds.dtypes[0]}")

# ---------------------------------------------------------------- uint8 RGB imagery
u8 = (rng.random((3, 2600, 2600)) * 255).astype("uint8")
rgb_path = os.path.join(TMP, "_layout_rgb.tif")
rp._to_tif(_grid(u8), rgb_path, "EPSG:32607", nodata=0)

with rasterio.open(rgb_path) as ds:
    check(ds.block_shapes[0] == (rp.TILE, rp.TILE),
          f"RGB raster not tiled: block {ds.block_shapes[0]}")
    check(ds.overviews(1) != [], "RGB raster has no overviews")
    check(ds.count == 3, f"band count changed: {ds.count}")
    check(ds.dtypes[0] == "uint8", f"RGB dtype changed: {ds.dtypes[0]}")
    check(ds.nodata == 0, f"nodata not preserved: {ds.nodata}")
    check(np.array_equal(ds.read(), u8),
          "uint8 RGB round trip is NOT bit-identical — the imagery you LOOK at "
          "must survive the rewrite untouched")

# ---------------------------------------------------------------- overviews stop sensibly
small = os.path.join(TMP, "_layout_small.tif")
rp._to_tif(_grid(rng.random((300, 300)).astype("float32")), small, "EPSG:32607")
with rasterio.open(small) as ds:
    check(ds.overviews(1) == [],
          f"a 300 px raster should get no overviews, got {ds.overviews(1)}")

# ---------------------------------------------------------------- predictor by dtype
check(rp._tif_opts("float32")["predictor"] == 3, "float dtype must use PREDICTOR=3")
check(rp._tif_opts("uint8")["predictor"] == 2, "integer dtype must use PREDICTOR=2")

for p in (path, rgb_path, small, keep):
    try:
        os.unlink(p)
    except OSError:
        pass

if fails:
    print("RASTER LAYOUT CHECK FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"RASTER LAYOUT VERIFIED — {rp.TILE}x{rp.TILE} tiles, overviews built, "
      f"uint8 bit-identical, float64->float32 narrowed losslessly "
      f"(max err {err:.1e}), keep_float64 honoured")
