"""Map marks: typed coordinates -> a red X / blue circle kept in the project folder.

Run with tests/run_all.sh, or directly with QGIS's bundled python.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from qgis.core import QgsApplication, QgsProject, QgsVectorLayer
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
app = QgsApplication([], True); app.initQgis()
from landslide_groundtruth import map_marks as M

fails = 0
def check(name, ok):
    global fails
    print(f"   [{'ok ' if ok else 'FAIL'}] {name}")
    fails += (not ok)

print("=== coordinates ===")
check("a plain pair parses", M.parse_coords(" 60.5 ", "-153.1") == (60.5, -153.1))
for bad in (("", ""), ("abc", "1"), ("91", "0"), ("0", "181")):
    try:
        M.parse_coords(*bad); check(f"{bad} refused", False)
    except M.MarkError as e:
        check(f"{bad} refused: {e}", True)

print("=== both kinds land in ONE file in the project folder ===")
d = tempfile.mkdtemp(prefix="marks_")
proj = QgsProject.instance()
layer = M.add_mark(d, M.NOT_HERE, 60.03, -153.09, "cloud shadow", proj)
layer2 = M.add_mark(d, M.COME_BACK, 60.05, -153.02, "", proj)
path = os.path.join(d, M.FILE_NAME)
check("map_marks.gpkg written beside the project", os.path.exists(path))
check("the second mark reuses the same layer", layer2.id() == layer.id())
check("one marks layer in the project",
      len([l for l in proj.mapLayers().values() if l.customProperty(M.PROPERTY)]) == 1)
check("layer is named 'Map marks' and is points in EPSG:4326",
      layer.name() == M.LAYER_NAME and layer.crs().authid() == "EPSG:4326")

print("=== the file itself holds them (not just the open layer) ===")
disk = QgsVectorLayer(f"{path}|layername={M.TABLE}", "check", "ogr")
feats = {f["kind"]: f for f in disk.getFeatures()}
check("two features on disk", disk.featureCount() == 2 and set(feats) == {M.NOT_HERE, M.COME_BACK})
p = feats[M.NOT_HERE].geometry().asPoint()
check("X is where it was typed (lon, lat order right)",
      abs(p.x() + 153.09) < 1e-9 and abs(p.y() - 60.03) < 1e-9)
check("note, lat, lon and a UTC timestamp are stored",
      feats[M.NOT_HERE]["note"] == "cloud shadow"
      and abs(feats[M.COME_BACK]["lat"] - 60.05) < 1e-9
      and abs(feats[M.COME_BACK]["lon"] + 153.02) < 1e-9
      and len(feats[M.COME_BACK]["added_utc"]) == 19)

print("=== red X vs blue circle ===")
r = layer.renderer()
cats = {c.value(): c for c in r.categories()}
check("categorised on 'kind'", r.classAttribute() == "kind" and set(cats) == set(M.KINDS))
# clone and HOLD each symbol: a symbol layer read off a temporary dangles
sx = cats[M.NOT_HERE].symbol().clone(); so = cats[M.COME_BACK].symbol().clone()
x = sx.symbolLayer(0); o = so.symbolLayer(0)
check("not_here is a red cross", "cross" in repr(x.shape()).lower() and x.strokeColor().red() > 180
      and x.strokeColor().blue() < 80)
check("come_back is a hollow blue circle", "circle" in repr(o.shape()).lower()
      and o.strokeColor().blue() > 180 and o.strokeColor().red() < 80 and o.color().alpha() == 0)

print("=== reopening: an existing file is reused, never overwritten ===")
proj.removeMapLayer(layer.id())
layer3 = M.add_mark(d, M.COME_BACK, 60.07, -153.00, "second look", proj)
check("the earlier marks are still there", layer3.featureCount() == 3)

print("=== nowhere to save ===")
proj.removeMapLayer(layer3.id())
try:
    M.add_mark("", M.NOT_HERE, 1, 2, "", proj); check("refused", False)
except M.MarkError as e:
    check(f"says so: {str(e)[:60]}…", "Save this QGIS project" in str(e))

print("\nMAP MARKS VERIFIED" if not fails else f"\n{fails} FAILED")
sys.stdout.flush()
os._exit(1 if fails else 0)
