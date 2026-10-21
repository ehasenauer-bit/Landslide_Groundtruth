"""A Planet preview or render must never take downloaded scenes off the map.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_keeps_renders.py

The free tile preview and the paid SR render used to share one layer list, and
each cleared it before adding its own — so a Preview on map removed the renders
you had already ordered and downloaded, and so did the next render. Worse, every
render of an event wrote the same `<event>_<side>_planet_<tone>.tif`, so even a
layer left on the map would have been showing the NEW scene's pixels. Now the two
lists are separate, a render adds beside what is there, and run_single names each
file for its scenes and product. The one layer a render may replace is a copy
reading the very file it rewrites (a re-tone of the same scenes).

Drives the real dock and the real _on_detail_done / _render_picks on real
GeoTIFFs; no network (the tile-hash request is stubbed) and no order ledger.
"""
import os, re, sys, tempfile
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsProject
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
# That prefix resolves the provider plugins to …/MacOS/Contents/PlugIns (absent), which
# leaves only gdal; the tile preview is an XYZ layer and needs the real 'wms' provider.
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject
from osgeo import gdal, osr
import numpy as np

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


class FakeBar(QObject):
    def pushMessage(s, *a, **k): pass
    def pushInfo(s, *a): pass
    def pushWarning(s, *a): pass
    def pushCritical(s, *a): pass
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s, t): pass
    def unsetMapTool(s, t): pass
    def setExtent(s, *a): pass
    def refresh(s): pass
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar = FakeBar(); s._c = FakeCanvas(); s._w = QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


TMP = tempfile.mkdtemp(prefix="planet_keeps_")


def tif(name, bands, dtype=gdal.GDT_Byte):
    """A tiny real GeoTIFF in UTM 7N (the St. Elias zone)."""
    path = os.path.join(TMP, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, 8, 8, bands, dtype)
    ds.SetGeoTransform((500000, 3, 0, 6680000, 0, -3))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(32607)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(1, bands + 1):
        ds.GetRasterBand(b).WriteArray(np.full((8, 8), 40 * b, dtype=np.uint8)
                                       if dtype == gdal.GDT_Byte
                                       else np.full((8, 8), -0.1, dtype=np.float32))
    ds = None
    return path


def reading(path):
    """Layers in the project whose source is exactly `path`."""
    return [l for l in QgsProject.instance().mapLayers().values()
            if l.source().split("|")[0] == path]


# ---- the run_single naming, lifted from source (it imports the imagery stack) ----
_src = open(os.path.join(ROOT, "run_single.py")).read()
_ns = {}
for fn in ("_scene_tag", "_tagged"):
    m = re.search(rf"\ndef {fn}\(.*?(?=\n\n\n|\n\ndef )", _src, re.S)
    assert m, f"{fn} not found in run_single.py — was it renamed?"
    exec(m.group(0), _ns)
scene_tag, tagged = _ns["_scene_tag"], _ns["_tagged"]

print("=== run_single names each render file for its scenes and product ===")
base = "/out/event_260818_1022"
pre_ids = ["20260814_211739_40_2527", "20260813_211928_10_251d"]   # the 10:36 render
post_a, post_b = ["20260824_211813_10_2527"], ["20260826_211634_56_2549"]
p_a = tagged(base, "post", scene_tag(post_a), "planet_hdr", toa=True)
p_b = tagged(base, "post", scene_tag(post_b), "planet_hdr", toa=True)
check(f"one scene is named by its id: {os.path.basename(p_a)}",
      p_a == base + "_post_20260824_211813_10_2527_planet_hdr_toa.tif")
check("a different post scene gets a different file (nothing overwritten)", p_a != p_b)
check("the same scene + tone + product gets the same file (re-tone replaces it)",
      p_a == tagged(base, "post", scene_tag(list(post_a)), "planet_hdr", toa=True))
check("SR and TOA of one scene are separate files",
      p_a != tagged(base, "post", scene_tag(post_a), "planet_hdr", toa=False))
t2 = scene_tag(pre_ids)
check(f"a composite is named date + count + hash: {t2}",
      re.fullmatch(r"20260813_2scenes_[0-9a-f]{8}", t2) is not None)
check("... independent of tick order", t2 == scene_tag(list(reversed(pre_ids))))
check("... and two sets sharing a first scene and a count still differ",
      scene_tag(["20260813_211928_10_251d", "20260815_212747_07_24db"]) != t2)
check("unknown scenes keep the old event-only name",
      tagged(base, "post", scene_tag(None), "planet_knee") == base + "_post_planet_knee.tif")
check("the old shared name is gone from _write_render_json",
      'f"{base}_{side}_planet_{a.planet_tone}.tif"' not in _src
      and 'f"{base}_dbright.tif"' not in _src)

# ---- the tab ----
from landslide_groundtruth.dock import LandslideDock
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
tab.dbright_check.setChecked(True)
tab._api_key = lambda: "test-key"                  # no key lookup
tab._request_tile_layer = lambda name, ids, key: None   # no network


def render(pre=None, post=None, dbright=None, scenes=None, tone="hdr"):
    """What run_single hands back; available=[] keeps the ledger out of it."""
    tab.task = SimpleNamespace(result=dict(
        pre=pre, post=post, dbright=dbright, tone=tone, toa=True, notes=[],
        pending={}, reused={}, available=[], scenes=scenes or {}))
    tab._on_detail_done()


def tile_preview(name="PlanetScope after 2026-08-26"):
    tab._add_tile_layer(name, "0123abcd")   # the real tile-layer builder
    return tab._preview_layers[-1] if tab._preview_layers else None


pre1, post1 = tif("pre_A.tif", 3), tif("post_B.tif", 3)
dbr1 = tif("dbright_AB.tif", 1, gdal.GDT_Float32)
post2 = tif("post_C.tif", 3)

print("\n=== render 1: a downloaded before/after pair lands on the map ===")
render(pre1, post1, dbr1, {"pre": pre_ids, "post": post_a})
check("pre, post and dBrightness all loaded",
      all(len(reading(p)) == 1 for p in (pre1, post1, dbr1)))

print("\n=== Preview on map must not remove them (the reported bug) ===")
t1 = tile_preview()
check("a tile preview layer was added", t1 is not None and len(tab._preview_layers) == 1)
t1_id = t1.id()
tab._render_picks([("PlanetScope after 2026-08-26", ["20260826_211634_56_2549"])])
check("the next Preview on map replaced the old tile preview",
      QgsProject.instance().mapLayer(t1_id) is None)
check("... and every downloaded layer is still on the map",
      all(len(reading(p)) == 1 for p in (pre1, post1, dbr1)))
check("Zoom to scene stays usable while renders remain", tab.zoom_btn.isEnabled())

print("\n=== render 2 of OTHER scenes adds beside render 1 ===")
t2l = tile_preview()
t2_id = t2l.id() if t2l else None
render(post=post2, scenes={"post": post_b})
check("render 1's pre, post and dBrightness are all still loaded",
      all(len(reading(p)) == 1 for p in (pre1, post1, dbr1)))
check("render 2's post is loaded too", len(reading(post2)) == 1)
check("the tile preview it superseded is gone (tiles only)",
      t2_id is not None and QgsProject.instance().mapLayer(t2_id) is None)

print("\n=== re-rendering the SAME file replaces that one layer, no duplicate ===")
render(post=post2, scenes={"post": post_b}, tone="hdr")
check("exactly one layer reads the re-rendered file", len(reading(post2)) == 1)
check("render 1 is still untouched",
      all(len(reading(p)) == 1 for p in (pre1, post1, dbr1)))

print("\n=== a failed render removes nothing ===")
t3 = tile_preview()
t3_id = t3.id() if t3 else None
render(post=os.path.join(TMP, "never_written.tif"), scenes={"post": post_b})
check("every downloaded layer survives a render that loaded nothing",
      all(len(reading(p)) == 1 for p in (pre1, post1, dbr1, post2)))
check("... and so does the tile preview (nothing replaced it)",
      t3_id is not None and QgsProject.instance().mapLayer(t3_id) is not None)

print("\n=== same-day ticks are stitched into one tile layer ===")
cand = lambda cid, d: dict(id=cid, date=d, gap_days=6, cloud_pct=1.0, geometry=None)
tab._search_result = dict(lat=60.26, lon=-140.817, params=dict(radius_km=5.0),
    pre=[cand("20260812_213534_30_251b", "2026-08-12T21:35:34")],
    post=[cand("20260824_211813_10_2527", "2026-08-24T21:18:13"),
          cand("20260824_211810_91_2527", "2026-08-24T21:18:10"),
          cand("20260825_211355_42_2530", "2026-08-25T21:13:55")])
tab._fill_table(tab._search_result)
from qgis.PyQt.QtCore import Qt as _Qt
for r in range(tab.table.rowCount()):
    tab.table.item(r, 0).setCheckState(_Qt.Checked)
picks = dict(tab._preview_picks())
check(f"4 ticked scenes -> 3 layers ({list(picks)})", len(picks) == 3)
check("the two 08-24 frames are ONE layer holding both ids",
      picks.get("PlanetScope after 2026-08-24 (2 scenes stitched)")
      == ["20260824_211813_10_2527", "20260824_211810_91_2527"])
check("other days stay their own layers",
      picks.get("PlanetScope after 2026-08-25") == ["20260825_211355_42_2530"]
      and picks.get("PlanetScope before 2026-08-12") == ["20260812_213534_30_251b"])

QgsProject.instance().clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
