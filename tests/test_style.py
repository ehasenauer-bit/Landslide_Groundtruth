"""Colour ramps for the three change rasters.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_style.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsRasterLayer
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app=QgsApplication([],False); app.initQgis()
from landslide_groundtruth import dock as D

print("=== 1. filename -> product ===")
CASES=[("ev_dbright_2025-09-12_vs_2026-02-19.tif","dbright"),
       ("ev_dndsi_a_vs_b.tif","dndsi"), ("ev_dndvi_a_vs_b.tif","dndvi"),
       ("EV_DNDSI_X.TIF","dndsi"), ("ev_pre_rgb.tif",""),
       ("ev_ndvi_pre.tif",""), ("ev_highlight.tif",""),
       ("dndsi_and_dndvi.tif","dndsi")]
for n,w in CASES:
    g=D._change_kind(n); ok=g==w
    print(("   ok   " if ok else "   FAIL ")+f"{n:42s} -> {g!r}")
    assert ok,(n,g,w)
# the ordering trap: 'ndvi' is a substring of nothing here, but confirm dndsi wins
assert D._change_kind("x_dndsi_dndvi.tif")=="dndsi"

print("\n=== 2. ramps are well formed and distinct ===")
R=D.LandslideDock.CHANGE_RAMPS
seen=set()
for kind,(breaks,cols,what) in R.items():
    lo=breaks[0]
    assert lo<0, (kind,lo)
    assert len(cols)==3 and len(breaks)==3
    stops=list(breaks)+[0.0]
    assert stops==sorted(stops), (kind,stops)   # must ascend for the shader
    print(f"   {kind:8s} lo={lo:<6g} stops={[round(x,3) for x in stops]}  {cols[0]}")
    assert tuple(cols) not in seen, f"{kind} reuses another product's colours"
    seen.add(tuple(cols))
print("   three distinct hue families")

print("\n=== 3. apply to a REAL raster and read the renderer back ===")
# build a tiny float GeoTIFF via GDAL
from osgeo import gdal, osr
gdal.UseExceptions()
path="/tmp/_dndsi_test.tif"
drv=gdal.GetDriverByName("GTiff")
ds=drv.Create(path,8,8,1,gdal.GDT_Float32)
# no SRS: the ramp does not depend on a CRS, and it keeps PROJ out of the test
ds.SetGeoTransform([500000,10,0,6700000,0,-10])
import array
vals=array.array("f",[(-0.6+0.02*i) for i in range(64)])
ds.GetRasterBand(1).WriteArray(__import__("numpy").array(vals).reshape(8,8))
ds=None
class Stub: pass
Stub.CHANGE_RAMPS=D.LandslideDock.CHANGE_RAMPS
Stub._style_change=D.LandslideDock._style_change
Stub._style_dbright=D.LandslideDock._style_dbright
st=Stub()
for kind in ("dbright","dndsi","dndvi"):
    lyr=QgsRasterLayer(path,"t","gdal"); assert lyr.isValid()
    st._style_change(lyr,kind)
    r=lyr.renderer()
    items=r.shader().rasterShaderFunction().colorRampItemList()
    lo=D.LandslideDock.CHANGE_RAMPS[kind][0][0]
    print(f"   {kind:8s} renderer={type(r).__name__} min={r.classificationMin():g} "
          f"max={r.classificationMax():g} stops={len(items)}")
    assert r.classificationMin()==lo and r.classificationMax()==0.0
    assert len(items)==4
    assert items[-1].color.alpha()==0, "the 0 stop must be fully transparent"
    assert items[0].color.alpha()==255
    assert [i.value for i in items]==sorted(i.value for i in items)

print("\n=== 4. the shim still works (old callers) ===")
lyr=QgsRasterLayer(path,"t","gdal"); st._style_dbright(lyr)
assert lyr.renderer().classificationMin()==-0.30
print("   _style_dbright -> dbright ramp, unchanged (-0.30)")

print("\n=== 5. dbright ramp is byte-identical to before ===")
br,cols,_=D.LandslideDock.CHANGE_RAMPS["dbright"]
assert br==(-0.30,-0.15,-0.05), br
assert cols==["#08306b","#2171b5","#6baed6"]
print("   -0.30/-0.15/-0.05/0 with the original blues — byte-identical to before")
print("\nSTYLING VERIFIED")
