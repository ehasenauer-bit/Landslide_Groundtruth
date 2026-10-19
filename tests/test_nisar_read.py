"""NISAR GCOV window reads land on the SAR tab's AOI grid, in the right place
(nisar_read.py — the QGIS half).

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_nisar_read.py

No network: a SYNTHETIC GCOV-shaped file is written with GDAL's netCDF driver
(netCDF-4 is HDF5) and opened with the HDF5 multidimensional driver — the same
code path that reads the real 6 GB granule remotely. It mirrors the real
layout measured on the Iliamna granule (2026-10-19): /science/LSAR/GCOV/grids/
frequencyA/{HHHH, HVHV, mask, xCoordinates, yCoordinates, projection@epsg_code},
pixel-centre coordinates, EPSG:3413 at 60N.

What it protects:
  * georeferencing rebuilt from the coordinate arrays — a half-pixel slip or a
    flipped axis puts a scar in the wrong place on the map;
  * the AOI grid is exactly the Planetary Computer render's, so a NISAR scene
    and a Sentinel-1 scene of one AOI line up pixel for pixel;
  * the product's own subswath mask (0 and 255) becomes NaN, not dark ground;
  * the display stretch matches the Sentinel-1 one, with 0 kept for nodata;
  * the raw-float output is what sar_change.read_band expects.
"""
import math
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from landslide_groundtruth import nisar_read as nr  # noqa: E402
from landslide_groundtruth import sar_change as sc  # noqa: E402

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


TMP = tempfile.mkdtemp(prefix="test_nisar_read_")
LAT, LON = 60.0, -153.0
N, RES = 600, 10.0                       # 6 km of 10 m grid around the point


def to3413(lon, lat):
    s4326, s3413 = osr.SpatialReference(), osr.SpatialReference()
    s4326.ImportFromEPSG(4326)
    s3413.ImportFromEPSG(3413)
    for s in (s4326, s3413):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x, y, _ = osr.CoordinateTransformation(s4326, s3413).TransformPoint(lon, lat)
    return x, y


CX, CY = to3413(LON, LAT)
# pixel CENTRES, north-up: y decreasing, as in the real granule
XS = CX - N / 2 * RES + RES / 2 + np.arange(N) * RES
YS = CY + N / 2 * RES - RES / 2 - np.arange(N) * RES
# A bright 300 m square whose CENTRE is the event point, on a -10 dB background,
# a masked-invalid strip (mask 0) on the west, fill (255) on the north edge,
# and a NaN hole.
HH = np.full((N, N), 0.1, np.float32)
HH[285:315, 285:315] = 1.0
HH[500:520, 500:520] = np.nan
MASK = np.ones((N, N), np.uint8)
MASK[:, :200] = 0                       # 2 km wide: reaches into the rotated AOI
MASK[:20, :] = 255


def build(path):
    with gdal.ExceptionMgr(useExceptions=True):
        ds = gdal.GetDriverByName("netCDF").CreateMultiDimensional(path)
        science = ds.GetRootGroup().CreateGroup("science")
        lsar = science.CreateGroup("LSAR")
        g = lsar.CreateGroup("GCOV").CreateGroup("grids").CreateGroup("frequencyA")
        dy = g.CreateDimension("yCoordinates", None, None, N)
        dx = g.CreateDimension("xCoordinates", None, None, N)
        f64 = gdal.ExtendedDataType.Create(gdal.GDT_Float64)
        f32 = gdal.ExtendedDataType.Create(gdal.GDT_Float32)
        u8 = gdal.ExtendedDataType.Create(gdal.GDT_Byte)
        i32 = gdal.ExtendedDataType.Create(gdal.GDT_Int32)
        g.CreateMDArray("xCoordinates", [dx], f64).Write(XS)
        g.CreateMDArray("yCoordinates", [dy], f64).Write(YS)
        g.CreateMDArray("HHHH", [dy, dx], f32).Write(HH)
        g.CreateMDArray("HVHV", [dy, dx], f32).Write(HH * 0.1)
        g.CreateMDArray("mask", [dy, dx], u8).Write(MASK)
        pr = g.CreateMDArray("projection", [], i32)
        pr.Write(np.array(3413, dtype=np.int32))
        pr.CreateAttribute("epsg_code", [], i32).Write(3413)
        ident = lsar.CreateGroup("identification")
        sd = gdal.ExtendedDataType.CreateString()
        try:
            ident.CreateMDArray("lookDirection", [], sd).Write(["Left"])
        except Exception:                    # noqa: BLE001 — metadata is a bonus
            pass


SRC = os.path.join(TMP, "synthetic_gcov.nc")
build(SRC)


def aoi(radius_km):
    """The SAR tab's _aoi_bbox, without the radius."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(LAT)))
    return (LON - dlon, LAT - dlat, LON + dlon, LAT + dlat)


print("=== georeferencing from pixel-centre coordinates ===")
gt = nr.grid_geotransform(XS, YS)
check("corner is half a pixel out from the first centre",
      abs(gt[0] - (XS[0] - 5.0)) < 1e-6 and abs(gt[3] - (YS[0] + 5.0)) < 1e-6)
check("north-up: +10 m east, -10 m north", gt[1] == 10.0 and gt[5] == -10.0)
check("'hh' -> HHHH, 'hv' -> HVHV",
      nr.covariance_term("hh") == "HHHH" and nr.covariance_term("HV") == "HVHV")

print("\n=== a window lands on the AOI grid ===")
bbox, px = aoi(2.0), 200
out, ogt, wkt, info = nr.read_aoi(SRC, "hh", bbox, px)
want = nr.aoi_geotransform(bbox, px)
check("px x px", out.shape == (px, px))
check("geotransform == the Planetary Computer render's grid",
      all(abs(a - b) < 1e-9 for a, b in zip(ogt, want)))
srs = osr.SpatialReference(wkt=wkt)
check("EPSG:4326", srs.GetAuthorityCode(None) == "4326")
check("granule CRS read from the file, not assumed", info["epsg"] == 3413)
check("look direction read when present", info["look"] in ("Left", None))
r = c = px // 2                           # the AOI centre is the event point
check("the bright square sits at the event point (AOI centre)",
      abs(float(np.nanmean(out[r - 3:r + 4, c - 3:c + 4])) - 1.0) < 0.05)
check("background reads -10 dB (0.1 linear)",
      abs(float(np.nanmedian(out)) - 0.1) < 1e-3)

print("\n=== validity: the product's mask, NaN, non-positive ===")
big = aoi(2.0)
o2, g2, _w, i2 = nr.read_aoi(SRC, "hh", big, 400)


def at_grid(row, col):
    """The AOI-grid value at a granule pixel — found through the real transform:
    EPSG:3413 at 153W is rotated ~108 deg from north, so 'west strip of the
    granule' is not the AOI's west edge."""
    s3413, s4326 = osr.SpatialReference(), osr.SpatialReference()
    s3413.ImportFromEPSG(3413)
    s4326.ImportFromEPSG(4326)
    for s_ in (s3413, s4326):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lon, lat, _ = osr.CoordinateTransformation(s3413, s4326).TransformPoint(
        float(XS[col]), float(YS[row]))
    c = int((lon - g2[0]) / g2[1])
    r = int((lat - g2[3]) / g2[5])
    if 0 <= r < o2.shape[0] and 0 <= c < o2.shape[1]:
        return o2[r, c]
    return None


inside = [at_grid(r, c) for r in range(150, 450, 30) for c in (150, 180)]
inside = [v for v in inside if v is not None]
check("pixels inside the mask-0 strip sampled", len(inside) >= 3)
check("  mask 0 (partially focused) -> NaN, not dark ground",
      all(np.isnan(v) for v in inside))
ok_side = [at_grid(r, c) for r in range(150, 450, 30) for c in (230, 260)]
ok_side = [v for v in ok_side if v is not None]
check("  just outside the strip -> valid -10 dB",
      ok_side and all(abs(v - 0.1) < 1e-3 for v in ok_side))
hole = at_grid(510, 510)
check("a NaN in the product stays NaN", hole is None or np.isnan(hole))
check("valid fraction reported below 1", i2["valid_frac"] < 1.0)

print("\n=== cache: one remote read per granule/pol/AOI ===")
cache = os.path.join(TMP, "cache")
logs = []
a1, *_ = nr.read_aoi(SRC, "hh", bbox, 120, cache_dir=cache, log=logs.append)
a2, *_ = nr.read_aoi(SRC, "hh", bbox, 120, cache_dir=cache, log=logs.append)
check("second read comes from the cache", any("cached window" in m for m in logs))
check("  and is the same pixels", np.allclose(a1, a2, equal_nan=True))
a3, *_ = nr.read_aoi(SRC, "hh", bbox, 60, cache_dir=cache, log=logs.append)
check("  a new Pixel size reuses the cached window too",
      a3.shape == (60, 60) and sum("cached window" in m for m in logs) == 2)
hv, *_ = nr.read_aoi(SRC, "hv", bbox, 120, cache_dir=cache)
check("another polarization is its own read",
      abs(float(np.nanmedian(hv)) - 0.01) < 1e-3)

print("\n=== big AOIs are read strided, still onto the full grid ===")
nat = nr.read_native(SRC, "hh", aoi(2.5), max_side=100)
check("step > 1 when the window exceeds max_side", nat["step"] > 1)
check("  native pixel scaled by the step", abs(nat["gt"][1] - 10.0 * nat["step"]) < 1e-9)
o4, *_ = nr.warp_to_aoi(nat["arr"], nat["gt"], nat["wkt"], aoi(2.5), 50)
check("  and still warps to px x px", o4.shape == (50, 50))

print("\n=== two background tasks reading at once ===")
# A Preview task and a change-detection task can read side by side. Without
# nisar_read's HDF5 lock, four threads like these SEGFAULT this GDAL build
# (measured: exit 139) — so a regression here kills the process, and the runner
# reports the file as failed rather than any single check.
import threading  # noqa: E402
errs, done = [], []
shared = os.path.join(TMP, "shared_cache")


def worker(pol):
    for _ in range(10):
        try:
            o, *_r = nr.read_aoi(SRC, pol, bbox, 80, cache_dir=shared)
            done.append(abs(float(np.nanmedian(o)) - (0.1 if pol == "hh" else 0.01)) < 1e-3)
        except Exception as e:               # noqa: BLE001
            errs.append(repr(e))


threads = [threading.Thread(target=worker, args=(p,)) for p in ("hh", "hv", "hh", "hv")]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(f"40 concurrent reads, shared cache: no crash, no error ({errs[:1]})",
      not errs and len(done) == 40 and all(done))
check("  no half-written cache file left behind",
      not [f for f in os.listdir(shared) if f.endswith(".part.tif")])

print("\n=== failures say what is wrong ===")
try:
    nr.read_aoi(SRC, "vv", bbox, 50)
    check("a missing polarization raises", False)
except LookupError as e:
    check("a missing polarization raises, naming what IS there",
          "VVVV" in str(e) and "HHHH" in str(e))
try:
    nr.read_aoi(SRC, "hh", (10.0, 10.0, 10.1, 10.1), 50)
    check("an AOI off the grid raises", False)
except LookupError as e:
    check("an AOI off the grid raises", "does not reach" in str(e))

print("\n=== outputs ===")
fpath = os.path.join(TMP, "float.tif")
nr.write_float(fpath, out, ogt, wkt)
arr, valid, fgt, _p = sc.read_band(fpath)
check("raw float reads back through sar_change.read_band",
      arr.shape == out.shape and bool(valid.sum() == np.isfinite(out).sum()))
d = nr.to_display(np.array([[0.1, np.nan, 50.0, 1e-6, 0.0]]), "db", -25.0, 0.0)
check("dB stretch: -10 dB on -25..0 -> 153", int(d[0, 0]) == 153)
check("  nodata/NaN and non-positive -> 0", int(d[0, 1]) == 0 and int(d[0, 4]) == 0)
check("  above white clips to 255, below black to 1",
      int(d[0, 2]) == 255 and int(d[0, 3]) == 1)
d = nr.to_display(np.array([[0.1]]), "linear", stretch=0.2)
check("linear stretch: 0.1 of 0.2 -> 128", int(d[0, 0]) == 128)
dpath = os.path.join(TMP, "disp.tif")
nr.write_display(dpath, nr.to_display(out), ogt, wkt)
ds = gdal.Open(dpath)
check("display GeoTIFF: one byte band, nodata 0",
      ds.RasterCount == 1 and ds.GetRasterBand(1).DataType == gdal.GDT_Byte
      and ds.GetRasterBand(1).GetNoDataValue() == 0)
ds = None

print("\n=== credentials are scoped and never guessed ===")
home, tok = os.environ.get("HOME"), os.environ.pop("EARTHDATA_TOKEN", None)
os.environ["HOME"] = TMP                  # a home with no ~/.netrc
try:
    how, detail = nr.credentials()
    check("no netrc, no token -> None, with the fix spelled out",
          how is None and "Earthdata Login" in detail)
    try:
        nr.configure_auth("https://nisar.example.test/x.h5")
        check("a remote read without credentials refuses", False)
    except PermissionError:
        check("a remote read without credentials refuses", True)
    check("a local file needs none", nr.configure_auth(SRC) == "local")
    os.environ["EARTHDATA_TOKEN"] = "not-a-real-token"
    check("a token is used when set", nr.configure_auth(
        "https://nisar.example.test/x.h5") == "token")
    check("  scoped to that host only", gdal.GetPathSpecificOption(
        "/vsicurl/https://nisar.example.test/x.h5", "GDAL_HTTP_AUTH") == "BEARER"
        and gdal.GetPathSpecificOption(
            "/vsicurl/https://other.example.test/y.tif", "GDAL_HTTP_AUTH") is None)
finally:
    gdal.ClearPathSpecificOptions("/vsicurl/https://nisar.example.test/")
    os.environ.pop("EARTHDATA_TOKEN", None)
    if tok:
        os.environ["EARTHDATA_TOKEN"] = tok
    if home:
        os.environ["HOME"] = home

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
