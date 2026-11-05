#!/usr/bin/env python3
"""The map figure's degree ticks have to sit where the meridians really are.

`web3d_export.map_graticule` hands the viewer each meridian and parallel as a
polyline in the scene's local metres, and the viewer labels the frame where
those cross its bottom and left edges (`mapGratHits`). For a ~4 km scene at
62.79° N, 151.08° W in UTM 5N this checks that:

  - the spacing is a 1/2/5 step with at least three lines across each axis,
    labelled with only the decimals that step needs;
  - the crossing the viewer will compute lands within 1 m of the true one;

and asserts the negative: meridians there lean far enough off grid north that
one longitude per column would put a tick tens of metres off. Then the same
for a scene straddling the antimeridian, where an unwrapped longitude range
is the whole game.

Runs head-less: osr only.
"""
import os
import re
import sys

from osgeo import osr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
sys.path.insert(0, PKG)

import web3d_export as w3d                                     # noqa: E402

osr.UseExceptions()


def scene(epsg, lat, lon):
    """(to_local, to_lonlat) for a scene centred on lat/lon in that CRS."""
    s, w = osr.SpatialReference(), osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    w.ImportFromEPSG(4326)
    for r in (s, w):
        r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    fwd, inv = osr.CoordinateTransformation(w, s), osr.CoordinateTransformation(s, w)
    cx, cy, _ = fwd.TransformPoint(lon, lat)

    def to_local(lo, la):
        x, y, _ = fwd.TransformPoint(lo, la)
        return x - cx, y - cy

    def to_lonlat(x, y):
        lo, la, _ = inv.TransformPoint(x + cx, y + cy)
        return lo, la
    return to_local, to_lonlat


def crossing(pts, along, c):
    """The viewer's mapGratHits, in metres: where the polyline meets coord
    `along` (0 = x, 1 = y) == c, as the other coordinate."""
    other = 1 - along
    for a, b in zip(pts, pts[1:]):
        av, bv = a[along], b[along]
        if av == bv or (av - c) * (bv - c) > 0:
            continue
        t = (c - av) / (bv - av)
        return a[other] + (b[other] - a[other]) * t
    return None


def value(lab):
    """'151.08° W' -> -151.08; '180.00°' -> 180.0."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)°(?: ([NSEW]))?", lab)
    assert m, "unexpected label %r" % lab
    v = float(m.group(1))
    return -v if m.group(2) in ("W", "S") else v


def bisect(f, target, lo, hi):
    for _ in range(64):
        mid = (lo + hi) / 2
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---- the step and label rules ------------------------------------------------
assert w3d._deg_step(-151.122, -151.042) == 0.02
assert w3d._deg_step(62.773, 62.809) == 0.01
assert w3d._deg_step(-140.797, -140.651) == 0.02, "8 km: 0.05 gives only two lines"
assert w3d._deg_step(60.0, 60.3) == 0.1
assert (w3d._deg_dec(0.02), w3d._deg_dec(0.005), w3d._deg_dec(0.1),
        w3d._deg_dec(1.0)) == (2, 3, 1, 0)

# ---- a 4 km scene near Denali, UTM 5N ----------------------------------------
HALF = 2015.0
to_local, to_lonlat = scene(32605, 62.791, -151.082)
G = w3d.map_graticule(to_local, to_lonlat, 2 * HALF, 2 * HALF)
lons = [g for g in G if g["axis"] == "lon"]
lats = [g for g in G if g["axis"] == "lat"]


def on_edge(lines, along, c):
    out = []
    for g in lines:
        u = crossing(g["pts"], along, c)
        if u is not None and -HALF <= u <= HALF:
            out.append((g, u))
    return out


bottom = on_edge(lons, 1, -HALF)            # meridians leaving the bottom edge
left = on_edge(lats, 0, -HALF)              # parallels leaving the left edge
assert len(bottom) >= 3 and len(left) >= 3, (
    "too few labelled lines: %s / %s" % ([g["lab"] for g, _ in bottom],
                                          [g["lab"] for g, _ in left]))
assert all(re.fullmatch(r"\d{3}\.\d\d° W", g["lab"]) for g in lons), [g["lab"] for g in lons]
assert all(re.fullmatch(r"\d\d\.\d\d° N", g["lab"]) for g in lats), [g["lab"] for g in lats]

worst = 0.0
for g, x in bottom:
    true_x = bisect(lambda xx: to_lonlat(xx, -HALF)[0], value(g["lab"]), -3 * HALF, 3 * HALF)
    worst = max(worst, abs(x - true_x))
for g, y in left:
    true_y = bisect(lambda yy: to_lonlat(-HALF, yy)[1], value(g["lab"]), -3 * HALF, 3 * HALF)
    worst = max(worst, abs(y - true_y))
assert worst < 1.0, "a degree tick is %.2f m from where its line crosses the frame" % worst

# The negative: here the meridians lean (UTM convergence ~1.7°), so labelling by
# one longitude per column — the meridian's x at the TOP used for the bottom
# edge — would misplace the tick by tens of metres. Otherwise this proves nothing.
g0 = bottom[0][0]
lean = abs(crossing(g0["pts"], 1, HALF) - crossing(g0["pts"], 1, -HALF))
assert lean > 50.0, "meridians only lean %.1f m across the scene" % lean

# ---- straddling the antimeridian (Amchitka-ish, UTM 60N) ---------------------
to_local, to_lonlat = scene(32660, 51.6, 179.995)
A = w3d.map_graticule(to_local, to_lonlat, 2 * HALF, 2 * HALF)
am = sorted((crossing(g["pts"], 1, -HALF), g["lab"]) for g in A if g["axis"] == "lon")
am = [(x, lab) for x, lab in am if x is not None and -HALF <= x <= HALF]
labs = [lab for _, lab in am]
assert len(am) >= 3, "antimeridian scene lost its meridians: %r" % labs
assert "180.00°" in labs and any(l.endswith("E") for l in labs) \
    and any(l.endswith("W") for l in labs), labs
# west to east across the frame: E longitudes climb to 180, then W ones fall
signed = [value(l) if value(l) > 0 else value(l) + 360.0 for l in labs]
assert signed == sorted(signed), "meridians out of order across 180°: %r" % labs

print("map graticule OK — UTM 5N 4 km: %s / %s, ticks within %.2f m, meridians lean "
      "%.0f m; antimeridian: %s" % (", ".join(g["lab"] for g, _ in bottom),
                                    ", ".join(g["lab"] for g, _ in left), worst, lean,
                                    ", ".join(labs)))
