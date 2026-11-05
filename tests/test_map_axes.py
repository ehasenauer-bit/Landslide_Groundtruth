#!/usr/bin/env python3
"""The map figure's axis labels must fit inside their panel.

The slide crops each map panel to what was drawn on its canvas and prints the
caption ("BEFORE · 2026-08-24") immediately underneath, so anything drawn past
the bottom of that canvas is not merely tight — it is gone, sliced mid-glyph.

That is what happened once: `mapLayout` reserved a flat 11.5% of the panel
HEIGHT for the easting labels while `drawMapAxes` sized the text from the panel
WIDTH. On the 1848x898 panel the slide renders (a ~2:1 cell), 11.5% is 103 px
and the title needed ~118 — so it ran off the canvas on every single export.

Two label modes now share that reservation: degrees (one row of "151.10° W",
the default whenever the plugin could build a graticule) and the old distance
axes with their "Distance E (km)" title (the fallback). Both are checked.

This reads the real expressions out of the shipped viewer JS rather than
restating them, so the two cannot drift apart again without failing here.
"""
import math
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
sys.path.insert(0, PKG)

import web3d_export as w3d                                    # noqa: E402

JS = w3d._VIEWER_JS


def grab(pattern, what, src=None):
    m = re.search(pattern, JS if src is None else src)
    assert m, "could not find %s in the viewer JS — did it get renamed?" % what
    return m.group(1).strip()


def jseval(expr, **names):
    """Evaluate a JS arithmetic expression lifted verbatim from the source."""
    py = (expr.replace("Math.max", "max").replace("Math.min", "min")
             .replace("Math.round", "_round").replace("Math.ceil", "math.ceil")
             .replace("Math.floor", "math.floor"))
    env = {"max": max, "min": min, "math": math,
           # JS rounds .5 away from zero; Python rounds it to even.
           "_round": lambda v: math.floor(v + 0.5)}
    env.update(names)
    return eval(py, {"__builtins__": {}}, env)                # noqa: S307


def body(fn):
    m = re.search(r"function %s\([^)]*\) \{(.*?)\n  \}" % fn, JS, re.S)
    assert m, "function %s is gone from the viewer JS" % fn
    return m.group(1)


# ---- the real expressions, straight out of the file --------------------------
_METRICS = grab(r"var (fs = Math\.max\(13.*?, tl = [^;]+);", "mapAxisMetrics fs/tl")
_FS, TL = _METRICS.split(", tl = ")
FS = _FS.split("fs = ", 1)[1]
_below = re.search(r"below: tl \+ 5 \+ fs\*\(ll \? ([\d.]+) : ([\d.]+)\)", JS)
assert _below, "could not find mapAxisMetrics' below reservation"
K_DEG, K_DIST = float(_below.group(1)), float(_below.group(2))
LAYOUT = body("mapLayout")
MB = grab(r"var mB = ([^;]+);", "mapLayout mB", LAYOUT)
MT = grab(r"var mT = ([^;]+);", "mapLayout mT", LAYOUT)
# where each mode's lowest text starts, relative to the bottom of the frame
LON_Y = grab(r"mapHalo\(ctx, h\.lab, h\.u, yb \+ ([^,]+),", "longitude label y")
TITLE_Y = grab(r'mapHalo\(ctx, "Distance E \("\+fE\.unit\+"\)", L\.x0\+L\.iw/2,\s*'
               r'L\.y0\+L\.ih\+([^,]+),', "Distance E title y")

# A top-baselined sans-serif glyph box runs ~1.15em deep (ascent + descent); the
# "(km)" parentheses are the deepest thing in the distance title.
GLYPH_DEPTH = 1.15
CLEARANCE = 4.0          # px of panel canvas that must stay blank under the text


def worst_case_overhang(pxW, pxH, degrees):
    """Panel px the lowest text's glyph box overshoots the canvas, when the map is
    not letterboxed (ih == ah) — the tightest case mapLayout has to survive."""
    fs = jseval(FS, pxW=pxW)
    tl = jseval(TL, pxW=pxW)
    below = tl + 5 + fs * (K_DEG if degrees else K_DIST)
    mB = jseval(MB, pxW=pxW, pxH=pxH, A=types.SimpleNamespace(below=below, fs=fs, tl=tl))
    frame_bottom = pxH - mB
    top = frame_bottom + jseval(LON_Y if degrees else TITLE_Y, fs=fs, tl=tl)
    return (top + GLYPH_DEPTH * fs + CLEARANCE) - pxH, mB, fs


# ---- the geometry the plugin actually exports --------------------------------
FW, FH, m, TH, capH = 1920, 1080, 24, 54, 28
colW = (FW - 3 * m) // 2
rowH = (FH - TH - 3 * m) // 2
SHIP = (colW * 2, (rowH - capH) * 2)                          # 2x supersampled panel
assert SHIP == (1848, 898), SHIP
fp = body("figPanelSize")
assert all(s in fp for s in ("FW=1920", "FH=1080", "m=24", "TH=54", "capH=28")), \
    "figPanelSize no longer renders panels at the size this test checks"

shapes = [(1848, 898), (1848, 1400), (900, 900), (2400, 700), (600, 400), (3840, 1200)]
spare = {}
for degrees in (True, False):
    over, mB, fs = worst_case_overhang(*SHIP, degrees=degrees)
    assert over <= 0, ("the shipping %dx%d panel clips its %s labels by %.1f px "
                       "(mB=%d, fs=%d)" % (SHIP[0], SHIP[1],
                                           "degree" if degrees else "distance", over, mB, fs))
    spare[degrees] = -over
    # ...and every other panel shape the same code can be handed: tall, square, wide.
    for w, h in shapes:
        o, mb, f = worst_case_overhang(w, h, degrees)
        assert o <= 0, "%dx%d clips the %s labels by %.1f px (mB=%d, fs=%d)" % (
            w, h, "degree" if degrees else "distance", o, mb, f)
        # A parallel right at the top edge puts half a label above the frame.
        mT = jseval(MT, pxW=w, pxH=h, A=types.SimpleNamespace(fs=f, tl=jseval(TL, pxW=w)))
        assert mT >= 0.5 * f + 2, "%dx%d: mT=%d cuts the top latitude label (fs=%d)" % (
            w, h, mT, f)

# Assert the negative: the margin this replaced must FAIL at the shipping size,
# otherwise the check above proves nothing.
old_mB = jseval("Math.round(pxH*0.115)", pxH=SHIP[1])
old_fs = jseval(FS, pxW=SHIP[0])
old_tl = jseval(TL, pxW=SHIP[0])
old_over = ((SHIP[1] - old_mB) + jseval(TITLE_Y, fs=old_fs, tl=old_tl)
            + GLYPH_DEPTH * old_fs + CLEARANCE) - SHIP[1]
assert old_over > 0, ("the flat 11.5%% margin no longer clips (%.1f px) — this "
                      "test has stopped guarding anything" % old_over)

# Both halves must read the same numbers, or the reservation is a coincidence.
assert "A = mapAxisMetrics(pxW)" in LAYOUT and "A.below" in MB, \
    "mapLayout stopped reserving from mapAxisMetrics"
assert re.search(r"var A = mapAxisMetrics\(pxW\), fs = A\.fs, tl = A\.tl", JS), \
    "drawMapAxes stopped drawing with the metrics mapLayout reserved"
# Degree mode is chosen by the data the plugin sends, and the ticks come from the
# real meridians/parallels, not from degrees spread evenly along an edge.
assert "CFG.map_grat" in body("mapAxisMetrics")
assert "mapGratHits(L, CFG.map_grat)" in body("drawMapAxes")

# The Details block of the map figure: no "View", no "Ground extent".
mapfig = JS[JS.index("function exportMapFigure()"):JS.index("function exportFigure()")]
for gone in ('"View"', '"Ground extent"', "mapExtentText"):
    assert gone not in mapfig, "%s is back in the map figure's Details rows" % gone
assert "mapExtentText" not in JS, "mapExtentText left behind as dead code"

# ---- fit-to-content: no letterboxing into a fixed 16:9 slide -----------------
fig3d = JS[JS.index("function exportFigure()"):JS.index("function legendWidth(")]
for name, src in (("map", mapfig), ("3D", fig3d)):
    assert "inkBox(" in src and "cropCanvas(" in src, \
        "the %s figure stopped cropping its panels to what was drawn" % name
compose = body("composeFigure")
assert "FW: 3*m + 2*colW" in compose, "composeFigure no longer sizes the slide to its panels"
assert "1920" not in compose, "composeFigure is back to a fixed-width slide"
# the compass and callout follow the crop, or they float in the trimmed-off margin
assert "drawNorth(g, s.mvp, pxW, pxH, R)" in fig3d
assert "reserveCorner(E, R, nb, nb" in fig3d

print("map axes OK — %d panel shapes clear the degree and distance labels (ship "
      "%dx%d: %.1f / %.1f px to spare); pre-fix margin clipped by %.1f px; figures "
      "crop to content" % (len(shapes), SHIP[0], SHIP[1], spare[True], spare[False],
                           old_over))
