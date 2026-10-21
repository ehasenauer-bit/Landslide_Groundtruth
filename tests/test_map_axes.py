#!/usr/bin/env python3
"""The map figure's "Distance E" title must fit inside its panel.

The slide crops each map panel to its own canvas and prints the caption
("BEFORE · 2026-08-24") immediately underneath, so anything drawn past the
bottom of that canvas is not merely tight — it is gone, sliced mid-glyph.

That is what happened: `mapLayout` reserved a flat 11.5% of the panel HEIGHT
for the easting labels while `drawMapAxes` sized the text from the panel WIDTH.
On the 1848x898 panel the slide actually uses (a ~2:1 cell), 11.5% is 103 px and
the title needed ~118 — so it ran off the canvas on every single export.

This reads the real expressions out of the shipped viewer JS rather than
restating them, so the two cannot drift apart again without failing here.
"""
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
sys.path.insert(0, PKG)

import web3d_export as w3d                                    # noqa: E402

JS = w3d._VIEWER_JS


def grab(pattern, what):
    m = re.search(pattern, JS)
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


# ---- the real expressions, straight out of the file --------------------------
_METRICS = grab(r"var (fs = Math\.max\(13.*?, tl = [^;]+);", "mapAxisMetrics fs/tl")
_FS, TL = _METRICS.split(", tl = ")
FS = _FS.split("fs = ", 1)[1]
BELOW = grab(r"return \{ fs: fs, tl: tl, below: ([^}]+?) \};", "mapAxisMetrics below")
MB = grab(r"var mB = ([^;]+);", "mapLayout mB")
# the axis title's y, as passed to mapHalo, relative to the bottom of the frame
TITLE_Y = grab(r'mapHalo\(ctx, "Distance E \("\+fE\.unit\+"\)", L\.x0\+L\.iw/2,\s*'
               r'L\.y0\+L\.ih\+([^,]+),', "Distance E title y")

# A top-baselined sans-serif glyph box runs ~1.15em deep (ascent + descent); the
# "(km)" parentheses are the deepest thing in this string.
GLYPH_DEPTH = 1.15
CLEARANCE = 4.0          # px of panel canvas that must stay blank under the title


def worst_case_overhang(pxW, pxH):
    """Panel px the title's glyph box overshoots the canvas, when the map is not
    letterboxed (ih == ah) — the tightest case mapLayout has to survive."""
    fs = jseval(FS, pxW=pxW)
    tl = jseval(TL, pxW=pxW)
    below = jseval(BELOW, fs=fs, tl=tl)
    mB = jseval(MB, pxW=pxW, pxH=pxH, mapAxisMetrics=lambda _: type(
        "A", (), {"below": below})())
    frame_bottom = pxH - mB
    title_top = frame_bottom + jseval(TITLE_Y, fs=fs, tl=tl)
    return (title_top + GLYPH_DEPTH * fs + CLEARANCE) - pxH, mB, fs


# ---- the geometry the plugin actually exports --------------------------------
FW, FH, m, TH, capH = 1920, 1080, 24, 54, 28
colW = (FW - 3 * m) // 2
rowH = (FH - TH - 3 * m) // 2
SHIP = (colW * 2, (rowH - capH) * 2)                          # 2x supersampled panel
assert SHIP == (1848, 898), SHIP

over, mB, fs = worst_case_overhang(*SHIP)
assert over <= 0, ("the shipping %dx%d panel still clips the easting title by "
                   "%.1f px (mB=%d, fs=%d)" % (SHIP[0], SHIP[1], over, mB, fs))

# ...and every other panel shape the same code can be handed: tall, square, wide.
shapes = [(1848, 898), (1848, 1400), (900, 900), (2400, 700), (600, 400), (3840, 1200)]
for w, h in shapes:
    o, mb, f = worst_case_overhang(w, h)
    assert o <= 0, "%dx%d clips the easting title by %.1f px (mB=%d, fs=%d)" % (
        w, h, o, mb, f)

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
assert "mapAxisMetrics(pxW).below" in JS, "mapLayout stopped reserving from mapAxisMetrics"
assert re.search(r"var A = mapAxisMetrics\(pxW\), fs = A\.fs, tl = A\.tl", JS), \
    "drawMapAxes stopped drawing with the metrics mapLayout reserved"

# The Details block of the map figure: no "View", no "Ground extent".
mapfig = JS[JS.index("function exportMapFigure()"):JS.index("function exportFigure()")]
for gone in ('"View"', '"Ground extent"', "mapExtentText"):
    assert gone not in mapfig, "%s is back in the map figure's Details rows" % gone
assert "mapExtentText" not in JS, "mapExtentText left behind as dead code"

print("map axes OK — %d panel shapes clear the easting title (ship %dx%d: mB=%d, "
      "fs=%d, %.1f px to spare); pre-fix margin clipped by %.1f px; Details rows trimmed"
      % (len(shapes), SHIP[0], SHIP[1], mB, fs, -over, old_over))
