"""A scene flagged bad_data must never be the ★, a ✓, a tick or the preview default.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_bad_data_row.py

Willow2, 2026-11-02: Planetary Computer served the S2C 2026-10-28 datatake with its
bands cut to 8 bits (see imagery.WRAPPED_DN_MAX). OmniCloudMask read that noise as 0%
cloud over the AOI, so the dock starred the one scene that could only render black.
The search now marks such a row `bad_data`; this pins what the dock does with it —
the decision itself is pinned in test_wrapped_bands.py.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
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

BAD = ("Planetary Computer's copy of this scene is corrupt: no reflectance value over "
       "the AOI exceeds 255 (the 16-bit data was cut to its low byte), so it renders "
       "black. Pick another scene.")


def cand(cid, date, cloud, bad=None, gap=28):
    return dict(id=cid, date=date, gap_days=gap, cloud_pct=cloud, aoi_cloud_pct=cloud,
                aoi_snow_pct=60.0, aoi_cloud_method="ocm", source="Sentinel-2",
                geometry=None, bad_data=bad)


# The Willow2 shape: the corrupt scene reads 0% cloud, the honest one 6%.
corrupt = cand("S2C_MSIL2A_20261028T213611_R086_T06VUP", "2026-10-28T21:36:11", 0.0, BAD)
honest = cand("S2C_MSIL2A_20261008T213531_R086_T06VVP", "2026-10-08T21:35:31", 6.2, gap=8)
pre = cand("S2B_MSIL2A_20260913T213529_R086_T06VUP", "2026-09-13T21:35:29", 0.0, gap=17)
result = dict(lat=62.05, lon=-149.2, params=dict(radius_km=15.0, prefer="s2"),
              pre=[pre], post=[corrupt, honest])
dock._search_result = result
dock._fill_table(result)


def row_of(cid):
    for r in range(dock.table.rowCount()):
        if dock.table.item(r, 0).data(Qt.UserRole + 2) == cid:
            return r
    return None


print("=== the corrupt row ===")
r_bad, r_ok = row_of(corrupt["id"]), row_of(honest["id"])
side = dock.table.item(r_bad, 0)
check(f"its Cloud cell says 'bad data', not its 0% ({dock.table.item(r_bad, 3).text()!r})",
      dock.table.item(r_bad, 3).text() == "bad data")
check("the cell's tooltip says why", dock.table.item(r_bad, 3).toolTip() == BAD)
check("it has no checkbox: not user-checkable ...",
      not (side.flags() & Qt.ItemIsUserCheckable))
check("... and no check indicator drawn", side.data(Qt.CheckStateRole) is None)
check("it is not the ★ despite the lowest cloud", not side.text().startswith("★"))
check("nor a ✓", not side.text().startswith("✓"))
check("the honest 6% scene is the ★ instead",
      dock.table.item(r_ok, 0).text().startswith("★"))
check("the honest row is still tickable",
      bool(dock.table.item(r_ok, 0).flags() & Qt.ItemIsUserCheckable))

print("\n=== Preview on map and the ranking skip it ===")
best = dock._best_streamable("post")
check(f"Preview-on-map leads with the honest scene ({best and best['id'][:24]})",
      best is not None and best["id"] == honest["id"])
ranked = dock._rank_like_run([corrupt, honest], 0.5, False)
check("_rank_like_run leaves it out entirely", [c["id"] for c in ranked] == [honest["id"]])
check("a side holding ONLY a corrupt scene gets no ★ at all",
      dock._run_selection([pre], [corrupt], result["params"])["post"]["top"] is None)

print("\n=== the gallery tile ===")
tile = dock._make_gallery_tile(corrupt)
check(f"its caption says bad data ({tile.text().splitlines()[-1]!r})",
      "bad data" in tile.text() and "0%" not in tile.text())
check("its tooltip says why", BAD in tile.toolTip())

print("\n=== an old search.json with no bad_data key is untouched ===")
legacy = {k: v for k, v in corrupt.items() if k != "bad_data"}
check("an old row still ranks on its cloud % (nothing to flag it with)",
      [c["id"] for c in dock._rank_like_run([legacy, honest], 0.5, False)][0]
      == legacy["id"])

dock.teardown()
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
