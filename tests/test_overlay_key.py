#!/usr/bin/env python3
"""Centerlines stay on the terrain, and the standalone overlay PNG has a key.

1. A centerline used to be draped only at its own vertices. Its few, long
   straight segments then cut under convex terrain and the depth test hid whole
   stretches of the runout (Denali 2023-09-14). The line drape now densifies
   each path to half the mesh spacing first; this checks it still does, and
   that landslideMetrics still measures the RAW path, not the densified one.
2. The "AFTER + overlays" PNG in each figure ZIP travels without the slide's
   Details legend, so it draws its own "Key" with short names: "Centerline"
   for the centerline layer, "(best)" dropped from the source area. The slide
   itself is untouched.

Reads the real expressions out of the shipped viewer JS, like test_map_axes.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth"))

import web3d_export as w3d                                    # noqa: E402

JS = w3d._VIEWER_JS


def body(fn):
    m = re.search(r"function %s\([^)]*\) ?\{(.*?)\n  \}" % fn, JS, re.S)
    assert m, "function %s is gone from the viewer JS" % fn
    return m.group(1)


# ---- 1. densified centerline drape -------------------------------------------
lines_loop = JS[JS.index("(CFG.lines || []).forEach(function (L) {"):]
lines_loop = lines_loop[:lines_loop.index("var labelsEl")]
assert "densifyPath(path, 0.5*Math.min(dx, dy))" in lines_loop, \
    "lines are draped at their own vertices again -- the centerline will vanish into slopes"
assert re.search(r"drape\(dp, [\d.]+\), dp\.length, gl\.LINE_STRIP", lines_loop), \
    "the line drape no longer uses the densified path"
assert "densifyPath" not in body("landslideMetrics"), \
    "runout must be measured on the raw centerline, not the densified one"


def densify(pa, step):                       # the JS densifyPath, line for line
    import math
    out = [pa[0]]
    for a, b in zip(pa, pa[1:]):
        n = max(1, math.ceil(math.hypot(b[0]-a[0], b[1]-a[1]) / step))
        out += [[a[0] + (b[0]-a[0])*k/n, a[1] + (b[1]-a[1])*k/n] for k in range(1, n+1)]
    return out


dp = densify([[0, 0], [100, 0], [100, 30]], 4.0)
assert dp[0] == [0, 0] and dp[-1] == [100, 30], "endpoints moved"
assert max(((q[0]-p[0])**2 + (q[1]-p[1])**2) ** 0.5 for p, q in zip(dp, dp[1:])) <= 4.0
assert [100, 0] in dp, "an original vertex was dropped"
print("densify OK -- %d points for a 130 m path at 4 m steps, endpoints and corners kept"
      % len(dp))

# ---- 2. the key on the standalone overlay PNG --------------------------------
kn = body("keyName")
cl = re.search(r"if \(/(.+?)/i\.test\(name\)\) return \"Centerline\";", kn)
best = re.search(r"\.replace\(/(.+?)/i, \"\"\)", kn)
assert cl and best, "keyName's two rules changed shape"


def key_name(name):
    if re.search(cl.group(1), name, re.I):
        return "Centerline"
    return re.sub(best.group(1), "", name, flags=re.I)


cases = {
    "Landslide centerlines": "Centerline",
    "landslide_centerlines": "Centerline",
    "Landslide centreline": "Centerline",
    "Source Area (Best)": "Source Area",
    "Source Area (best)": "Source Area",
    "Source Area (low)": "Source Area (low)",
    "Landslide scars": "Landslide scars",
    "Peaks": "Peaks",
}
for src, want in cases.items():
    got = key_name(src)
    assert got == want, "%r -> %r, want %r" % (src, got, want)
assert '"Key"' in body("drawKey"), "the key's title is no longer 'Key'"

lp = body("labelledPanel")
assert "s.ov ? panelKey(s.img) : null" in lp, "the key is not limited to the overlay panel"
assert "panelKey(" not in body("composeFigure"), "the slide must keep its own legend"
assert 'g.fillText("Overlays", ix, yy)' in body("composeFigure"), \
    "the slide's Details legend changed -- Ethan asked for the key on the PNG only"
assert "cap: pn.cap, ov: pn.ov" in body("exportMapFigure"), \
    "map panels lost their ov flag, so map_after_overlays.png gets no key"
print("key OK -- %d layer names shortened as asked; overlay PNG only, slide legend unchanged"
      % len(cases))
print("OVERLAY KEY VERIFIED")
