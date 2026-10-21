#!/usr/bin/env python3
"""The map slide's fourth cell is a location map, and it has to be a real one.

The Details block it replaced could not be wrong — it printed strings it was
handed. A map can: a projection centred on the wrong place, an Aleutian event
whose window straddles the antimeridian and comes back empty, rings that miss
the frame entirely, or a payload so fat it doubles the exported file. So this
checks the geometry locator_map actually produces, and that the viewer JS is
still wired to draw it (and to keep the legend, which the Details block used to
carry).

Runs head-less: locator_map is GDAL/OGR only.
"""
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
sys.path.insert(0, PKG)

import locator_map as lm                                       # noqa: E402
import web3d_export as w3d                                     # noqa: E402

GPKG = lm.default_world_map()
assert GPKG and os.path.exists(GPKG), \
    "no QGIS world map found — locator_map.default_world_map() returned %r" % GPKG

ILIAMNA = (60.0197, -153.0723)          # 2025-10-10 event: mainland, mid-Alaska
AMCHITKA = (51.88, 179.30)              # west of the antimeridian: the split case
MID_PACIFIC = (35.0, -140.0)            # nothing but water and graticule


def payload_kb(d):
    return len(json.dumps(d, separators=(",", ":"))) / 1024.0


# ---- the mainland case -------------------------------------------------------
d = lm.build_locator(*ILIAMNA, label="AK2025-1010")
assert d is not None
assert d["place"] == "Alaska, United States", d["place"]
assert d["label"] == "AK2025-1010"
assert d["land"], "no land around an event 60 km inland"
half = d["half_m"]

# Every ring point has to be in the neighbourhood of the frame: clipped in
# lon/lat with a pad (so the canvas clip, not the polygon, ends the coastline),
# never the whole country carried along by a spatial filter that does not clip.
pts = [p for poly in d["land"] for ring in poly for p in ring]
far = [p for p in pts if abs(p[0]) > 2.2 * half or abs(p[1]) > 2.2 * half]
assert not far, "%d ring point(s) miles outside the window, e.g. %r" % (len(far), far[0])
assert max(abs(p[0]) for p in pts) > half * 0.9, "the land stops short of the frame"

# ...and it must reach the frame edges, i.e. the window really is filled.
assert any(p[0] < -half * 0.9 for p in pts) and any(p[1] > half * 0.9 for p in pts), \
    "land only covers part of the window — the lon/lat clip is too tight"

labs = [g["lab"] for g in d["grat"]]
assert "60° N" in labs and "155° W" in labs, labs
assert all(g["axis"] in ("lat", "lon") for g in d["grat"])
for g in d["grat"]:                     # a line, not a point, and inside the margin
    assert len(g["pts"]) >= 2
    assert all(abs(p[0]) < 1.5 * half and abs(p[1]) < 1.5 * half for p in g["pts"])
kb_land = payload_kb(d)
assert kb_land < 250, "the locator adds %.0f KB to every exported viewer" % kb_land

# The projection is centred ON the event: the star is drawn at (0, 0), so a
# shifted centre would silently mark the wrong valley.
_, _, fwd, _ = lm._crs_pair(*ILIAMNA)
x, y, _ = fwd.TransformPoint(ILIAMNA[1], ILIAMNA[0])
assert abs(x) < 1.0 and abs(y) < 1.0, (x, y)
# and it is equal-area-ish at this size: 100 km east of the event lands ~100 km out
x2, y2, _ = fwd.TransformPoint(ILIAMNA[1] + 100000.0 / (111320 * math.cos(
    math.radians(ILIAMNA[0]))), ILIAMNA[0])
assert 98000 < math.hypot(x2, y2) < 102000, math.hypot(x2, y2)

# ---- the antimeridian case ---------------------------------------------------
a = lm.build_locator(*AMCHITKA)
assert a["land"], "no land in the central Aleutians — the ±180 split dropped it"
assert any("E" in g["lab"] for g in a["grat"]) and any("W" in g["lab"] for g in a["grat"]), \
    "the window spans the antimeridian but no meridian was labelled on both sides"
assert any(g["lab"] == "180°" for g in a["grat"]), \
    "the antimeridian itself must be labelled 180°, not 180° W"
apts = [p for poly in a["land"] for ring in poly for p in ring]
assert any(p[0] < 0 for p in apts) and any(p[0] > 0 for p in apts), \
    "land only on one side of the event: half the split rect was lost"

# ---- open water, and no world map -------------------------------------------
o = lm.build_locator(*MID_PACIFIC)
assert o["land"] == [] and o["grat"], "mid-ocean should still get a graticule"
assert o["place"] == ""
assert lm.build_locator(*ILIAMNA, gpkg="/nope/world_map.gpkg") is None, \
    "a missing world map must return None so the figure falls back to Details"

# ---- the viewer JS that draws it ---------------------------------------------
JS = w3d._VIEWER_JS
mapfig = JS[JS.index("function exportMapFigure()"):JS.index("function exportFigure()")]
assert "locatorPanel(pxW, pxH)" in mapfig and "locatorCaption()" in mapfig
assert "hideDetails: hasLoc" in mapfig
assert '"map_location.png"' in mapfig
# The 3D slide keeps its Details block: only the map slide gives up the cell.
d3 = JS[JS.index("function exportFigure()"):JS.index("function composeFigure(")]
assert "hideDetails" not in d3, "the 3D figure lost its Details block too"
assert "if (!opts.hideDetails) {" in JS, "the details cell is no longer optional"
assert "drawFigureLegend(g, L, g.measureText(ttl).width)" in JS, \
    "nothing moves the overlay legend into the title bar when Details goes away"

# The locator frame must line up with the map frame in the cell beside it, which
# only holds while both derive their top/bottom margins the same way.
def margins(fn):
    body = re.search(r"function %s\(pxW, pxH\) \{(.*?)\n  \}" % fn, JS, re.S).group(1)
    return (re.search(r"var mT = ([^;]+);", body).group(1),
            re.search(r"var mB = ([^;]+);", body).group(1))
assert margins("locLayout") == margins("mapLayout"), \
    "locLayout and mapLayout no longer reserve the same top/bottom margins:\n  %r\n  %r" % (
        margins("locLayout"), margins("mapLayout"))

print("locator OK — Alaska: %d polygons / %d points / %d graticule lines, %.0f KB; "
      "antimeridian split keeps land on both sides; open water degrades to a "
      "graticule; no world map falls back to Details" % (
          len(d["land"]), len(pts), len(d["grat"]), kb_land))
