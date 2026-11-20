"""Scenes the Planet account can't download yet are shown, and kept out of orders;
paid orders at the edge of the AOI stay recallable.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_preview_only.py

Since 2026-11-19 the search lists every scene, the ones this account can't download
yet included (downloadable=False in search.json; see test_planet_nothing_hidden.py).
The tab must mark them, still preview them, and never put one in an order: Planet
refuses a whole order over one scene it won't deliver. A Render detail with nothing
ticked falls back past a preview-only ★ to the nearest orderable scene.

The recall picker listed only orders whose clip contains the epicentre, so a paid
strip that clipped one edge of the AOI vanished, and when it was the only order the
Recall button was disabled too. It is now listed after them, marked.

Drives the real dock; no network, no key, and a throwaway order ledger.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LANDSLIDE_PLANET_CACHE"] = tempfile.mkdtemp(prefix="planet_preview_only_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsProject
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject, Qt

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


from landslide_groundtruth.dock import LandslideDock
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
tab._api_key = lambda: "test-key"                       # no key lookup
sent = []
tab._request_tile_layer = lambda name, ids, key: sent.append((name, list(ids)))
warned = []
tab._warn = warned.append
launched = []
dock.pipeline_launch = lambda: launched.append(1) or None   # never start a subprocess

LAT, LON = 60.26, -140.817


def cand(cid, d, gap, cloud, dl=True, after=None):
    return dict(id=cid, date=d, gap_days=gap, cloud_pct=cloud, geometry=None,
                downloadable=dl, orderable_after=after, thumb_url=None)


PRE = "20260812_213534_30_251b"
POST_LOCKED = "20260818_211500_00_2527"     # 11 h after the slide, not orderable yet
POST_LOCKED_2 = "20260818_211503_00_2527"   # same strip, also locked
POST_OK = "20260818_211459_00_2530"         # same day, orderable
POST_LATER = "20260825_211355_42_2530"
tab._search_result = dict(
    lat=LAT, lon=LON, params=dict(radius_km=5.0),
    pre=[cand(PRE, "2026-08-12T21:35:34", 6, 1.0)],
    post=[cand(POST_LOCKED, "2026-08-18T21:15:00", 0, 2.0, dl=False, after="2026-09-17"),
          cand(POST_LOCKED_2, "2026-08-18T21:15:03", 0, 2.0, dl=False),
          cand(POST_OK, "2026-08-18T21:14:59", 0, 30.0),
          cand(POST_LATER, "2026-08-25T21:13:55", 7, 1.0)])
tab._fill_table(tab._search_result)


def row(cid):
    for r in range(tab.table.rowCount()):
        if tab.table.item(r, 0).data(Qt.ItemDataRole.UserRole + 1) == cid:
            return r
    return None


def tick(*ids):
    for r in range(tab.table.rowCount()):
        cid = tab.table.item(r, 0).data(Qt.ItemDataRole.UserRole + 1)
        tab.table.item(r, 0).setCheckState(
            Qt.CheckState.Checked if cid in ids else Qt.CheckState.Unchecked)


print("=== the table lists preview-only scenes and marks them ===")
check("all 5 scenes are rows", tab.table.rowCount() == 5)
side_cell = tab.table.item(row(POST_LOCKED), 0)
check(f"locked row says so: {side_cell.text()!r}", "preview only" in side_cell.text())
check("... with the reason and its date in the tooltip",
      "can't download" in side_cell.toolTip() and "2026-09-17" in side_cell.toolTip())
check("an orderable row is not marked",
      "preview only" not in tab.table.item(row(POST_OK), 0).text())
check("the ★ on the post side is the best scene, locked or not (preview is free)",
      tab.table.item(row(POST_LOCKED), 0).text().startswith("★"))

print("\n=== Preview on map still streams them, in a layer of their own ===")
tick(POST_LOCKED, POST_LOCKED_2, POST_OK)
picks = dict(tab._preview_picks())
check(f"3 same-day ticks -> 2 layers, locked apart ({picks})", len(picks) == 2)
check("the two locked frames are stitched together",
      sorted(next(v for v in picks.values() if POST_LOCKED in v))
      == sorted([POST_LOCKED, POST_LOCKED_2]))
check("the orderable frame is its own layer", [POST_OK] in picks.values())
sent.clear()
tab._render_picks(tab._preview_picks())
check("both layers were requested from the tile service",
      sorted(i for _, ids in sent for i in ids)
      == sorted([POST_LOCKED, POST_LOCKED_2, POST_OK]))

print("\n=== Render detail never orders a preview-only scene ===")
tick(PRE, POST_LOCKED, POST_OK)
picks, skipped = tab._detail_picks_by_side()
check(f"ticked locked scene skipped ({skipped})", skipped == [POST_LOCKED])
check("the orderable ticks are kept", picks == {"pre": [PRE], "post": [POST_OK]})

tick()                                      # nothing ticked -> ★ fallback
picks, skipped = tab._detail_picks_by_side()
# rank = gap + 0.5 * cloud: POST_LATER (7 d, 1%) 7.5 beats POST_OK (0 d, 30%) 15
check(f"nothing ticked: the locked ★ is passed over for the best-ranked orderable "
      f"scene ({picks['post']})", picks["post"] == [POST_LATER] and picks["pre"] == [PRE])

tick(POST_LOCKED, POST_LOCKED_2)
warned.clear(); launched.clear()
tab._render_detail()
check("only locked ticks: a warning and no order launched",
      warned and "can't download" in warned[-1] and not launched)

print("\n=== the recall picker keeps an edge-of-AOI paid order ===")
sys.path.insert(0, ROOT)
import planet_cache as pc
dock.shared_dir = lambda: os.environ["LANDSLIDE_PLANET_CACHE"]   # don't adopt real folders
dock.pipeline_dir = lambda: ROOT        # this checkout's planet_cache.py; project_edit
dock.project_edit.setText("")           # stays empty so no real out/ gets adopted
# AOI box: lon -140.907..-140.727, lat 60.215..60.305; this clip misses the point
pc.record("edge-order-0001", None, side="post", lat=60.26, lon=-140.75,
          radius_km=5.0, bbox=(-140.80, 60.22, -140.70, 60.30),
          scene_ids=[POST_LATER], bundle="analytic_sr_udm2")
tab.task = None
n = tab._refresh_recall_combo()
labels = [tab.recall_combo.itemText(i) for i in range(tab.recall_combo.count())]
check(f"the edge order is listed ({labels})", n == 1 and len(labels) == 2)
check("... marked as missing the epicentre", labels[-1].endswith("misses epicentre"))
check("... and Recall is usable", tab.recall_btn.isEnabled())
pc.record("centre-order-0002", None, side="pre", lat=LAT, lon=LON, radius_km=5.0,
          bbox=(-140.85, 60.24, -140.78, 60.28), scene_ids=[PRE],
          bundle="analytic_sr_udm2")
n = tab._refresh_recall_combo()
labels = [tab.recall_combo.itemText(i) for i in range(1, tab.recall_combo.count())]
check(f"an order covering the epicentre comes first ({labels})",
      n == 2 and "centre-ord" in labels[0] and labels[1].endswith("misses epicentre"))
pc.record("far-order-0003", None, side="post", lat=61.5, lon=-145.0, radius_km=5.0,
          bbox=(-145.1, 61.45, -144.9, 61.55), scene_ids=["20260101_000000_00_0000"])
check("an order somewhere else is still left out", tab._refresh_recall_combo() == 2)

QgsProject.instance().clear()
dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
