"""Radar shadow and layover, and what handing them to the asc+desc merge changes.

Run with tests/run_all.sh, or directly:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_radar_shadow.py

Shadow cannot be found in the pixels: `sentinel-1-rtc` leaves shadowed pixels
finite and ordinary-looking, never NoData, which is also why the merge's
confidence-1 "recovered, other orbit blind" only ever fired on frame edges. So it
is PREDICTED from the DEM and the look geometry, which is deterministic.

It is not noise. This was first built on the belief that a shadowed pixel is
noise over noise and so reads as a large, false change. On a quiet real Iliamna
pair shadowed pixels were ~10x QUIETER than lit ground, and masking them moved
the merged background 17.91% -> 17.89%. What the mask does change is the
confidence: a quiet shadowed pass no longer counts as an orbit that looked and
"saw nothing", so a real change the other pass saw is a recovery (1) rather than
a disagreement (3) that the agreeing-only heat map would hide.

What is pinned here: the shadow falls on the correct side of a ridge and is the
right length, the self-shadow threshold is 90deg - incidence, ascending and
descending are genuinely complementary on east/west slopes and genuinely
identical on north/south ones, the look direction and incidence match real
footprints, and the merge sets a masked geometry aside — which, for the quiet
shadow real data produces, turns a disagreement into a recovery.

Layover is the mirror (a radar-facing slope steeper than the incidence folds
onto the ground in front of it), and it is the one that decides what the other
pass can fill: at Iliamna 97-98% of each pass's shadow is layover in the other,
so steep east/west faces are lost to Sentinel-1 from both sides, while 77-85%
of layover is seen cleanly by the other pass. Sections 13-14 pin that.
"""
import math, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

import numpy as np
from landslide_groundtruth import layover_dim as ld
from landslide_groundtruth import sar_change

GT = (0.0, 20.0, 0.0, 0.0, 0.0, -20.0)      # 20 m projected grid, north-up
THETA = ld.IW_INCIDENCE_DEG                 # 39 deg
fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def ramp(tilt_deg, n=60, down_east=True):
    """A planar slope of `tilt_deg`, falling toward the east (or the west)."""
    rise = np.arange(n) * 20.0 * math.tan(math.radians(tilt_deg))
    row = rise[::-1] if down_east else rise
    return np.tile(row, (n, 1)).astype(np.float32)


print("=== 1. the shadow falls on the far side of the ridge, and is H*tan(theta) long ===")
# Sentinel-1 is right-looking: descending is lit from the EAST, so the ground
# behind a ridge — to its WEST — is hidden. Ascending is the mirror. Getting this
# backwards would mask exactly the pixels that are fine and keep the bad ones.
wall = np.zeros((100, 100), dtype=np.float32)
wall[:, 50] = 1000.0
want_px = round(1000.0 * math.tan(math.radians(THETA)) / 20.0)      # 40 px
for state, side, lo, hi in (("DESCENDING", "west", 50 - want_px, 49),
                            ("ASCENDING", "east", 51, 50 + want_px)):
    m, meta = ld.radar_shadow(wall, GT, state, dem_smooth=0)
    cols = np.where(m[50])[0]
    check(f"{state:11s} lit from the {'east' if state[0] == 'D' else 'west'}, "
          f"shadow to the {side}: cols {cols.min()}..{cols.max()}",
          cols.min() == lo and cols.max() == hi, f"expected {lo}..{hi}")
    check(f"{state:11s} range azimuth {meta['range_azimuth']:.0f} deg",
          meta["range_azimuth"] == (270.0 if state[0] == "D" else 90.0))
check(f"a 1000 m wall casts {want_px} px ({want_px * 20} m) of shadow at "
      f"{THETA:.0f} deg", want_px == 40)

print("\n=== 2. self-shadow begins at 90deg - incidence, not somewhere else ===")
cut = 90.0 - THETA                           # 51 deg
for tilt, want in ((cut - 6, False), (cut + 6, True)):
    # falling toward the east = tilted AWAY from an ascending look (lit from west)
    m, _ = ld.radar_shadow(ramp(tilt), GT, "ASCENDING", dem_smooth=0)
    got = m.mean() > 0.5
    check(f"a {tilt:.0f}deg slope tilted away is "
          f"{'shadowed' if got else 'lit'} (cut is {cut:.0f}deg)", got == want,
          f"{m.mean():.0%} shadowed")

# and the cut MOVES with the incidence angle, which is why it is a parameter:
# S1 IW runs ~29deg near range to ~46deg far range, so the same 55deg slope is
# lit at near range and shadowed at far range
for inc, want in ((29.0, False), (46.0, True)):
    m, _ = ld.radar_shadow(ramp(55.0), GT, "ASCENDING", incidence_deg=inc,
                           dem_smooth=0)
    check(f"55deg slope at {inc:.0f}deg incidence (cut {90 - inc:.0f}deg) -> "
          f"{'shadowed' if m.mean() > 0.5 else 'lit'}", (m.mean() > 0.5) == want,
          f"{m.mean():.0%}")

print("\n=== 3. asc and desc are complementary east/west, IDENTICAL north/south ===")
# This is the real limit on "a complete set", and it is worth pinning because it
# is the thing people assume away. Both S1 geometries are near-polar and look
# nearly due east/west, so a north-facing slope has almost no slope component in
# the range direction and is imaged the SAME by both passes. No merge rule fixes
# a north-facing face that neither geometry resolves.
ew = ramp(60.0)                                   # falls to the east
a_ew, _ = ld.radar_shadow(ew, GT, "ASCENDING", dem_smooth=0)
d_ew, _ = ld.radar_shadow(ew, GT, "DESCENDING", dem_smooth=0)
check(f"east/west 60deg slope: asc {a_ew.mean():.0%} shadowed, "
      f"desc {d_ew.mean():.0%}", a_ew.mean() > 0.5 and d_ew.mean() == 0.0,
      "the two passes must disagree here — that is the whole point")
check("so every shadowed pixel is recoverable from the other pass",
      not (a_ew & d_ew).any())

ns = ramp(60.0).T                                 # same slope, rotated to face north
a_ns, _ = ld.radar_shadow(ns, GT, "ASCENDING", dem_smooth=0)
d_ns, _ = ld.radar_shadow(ns, GT, "DESCENDING", dem_smooth=0)
check(f"north/south 60deg slope: asc {a_ns.mean():.0%}, desc {d_ns.mean():.0%} "
      "— both passes see it the same", a_ns.mean() == d_ns.mean() == 0.0)

print("\n=== 4. it refuses to invent geometry it does not have ===")
for state, why in (("", "no orbit direction"), (None, "orbit is None"),
                   ("sideways", "unrecognised orbit")):
    m, meta = ld.radar_shadow(wall, GT, state, dem_smooth=0)
    check(f"{why:22s} -> nothing masked", not m.any(), meta["note"])
m, _ = ld.radar_shadow(np.zeros((40, 40), np.float32), GT, "ASCENDING",
                       dem_smooth=0)
check("flat ground -> nothing masked", not m.any())
m, meta = ld.radar_shadow(np.full((20, 20), np.nan, np.float32), GT, "ASCENDING")
check("all-NoData DEM -> nothing masked, and it says so", not m.any(),
      meta["note"])

print("\n=== 5. a ridge on one edge does not shadow the opposite edge ===")
# np.roll would wrap, which would paint a shadow across the AOI from terrain that
# is nowhere near it. _shift pads instead.
edge = np.zeros((60, 60), dtype=np.float32)
edge[:, -1] = 3000.0                        # 3 km wall on the EASTERN edge
m, _ = ld.radar_shadow(edge, GT, "ASCENDING", dem_smooth=0)   # lit from the west
check("ascending: nothing west of a wall on the east edge is shadowed",
      not m.any(), f"{m.sum()} px wrapped around")
m, _ = ld.radar_shadow(edge, GT, "DESCENDING", dem_smooth=0)  # lit from the east
check("descending: that same wall shadows the AOI to its west",
      m.mean() > 0.9, f"{m.mean():.0%}")

print("\n=== 6. what the mask changes in the merge ===")
# Each column is one pixel. The loud shadowed values in cases 1-3 are MECHANISM
# tests — whatever a masked sample holds, the merge must not use it. Real shadow
# is quiet (measured ~10x quieter than lit ground), so case 5 is the one real
# data produces, and it is where the mask earns its keep.
N = np.nan
THR = 3.0
#        asc     desc    asc shadowed?  desc shadowed?
CASES = [("clean both, asc louder",         -5.0, -4.0, False, False),
         ("asc shadowed, loud (mechanism)", -8.0,  0.2, True,  False),
         ("asc shadowed + loud, desc scar",  9.0, -5.0, True,  False),
         ("desc shadowed, loud (mechanism)", 0.1,  7.0, False, True),
         ("shadowed in BOTH geometries",     8.0, -8.0, True,  True),
         ("asc shadowed QUIET, desc scar",   0.3, -5.0, True,  False)]
asc = np.array([[c[1] for c in CASES]], dtype=np.float32)
desc = np.array([[c[2] for c in CASES]], dtype=np.float32)
m_asc = np.array([[c[3] for c in CASES]], dtype=bool)
m_desc = np.array([[c[4] for c in CASES]], dtype=bool)
asc_before, desc_before = asc.copy(), desc.copy()

plain, cplain, _mp = sar_change.merge_geometries([asc, desc], "logratio", THR)
masked, cmask, mm = sar_change.merge_geometries([asc, desc], "logratio", THR,
                                                masks=[m_asc, m_desc])
#                                       unmasked, masked, confidence after
WANT = [(-5.0, -5.0, 2.0),      # nothing shadowed: unchanged
        (-8.0,  0.2, 0.0),      # the spurious -8 dB goes; desc saw no change
        ( 9.0, -5.0, 1.0),      # the scar desc saw survives, as a real recovery
        ( 7.0,  0.1, 0.0),      # mirror case
        ( 8.0,    N, N),        # no geometry can see it: honest NoData
        (-5.0, -5.0, 1.0)]      # value unchanged — but see the conf checks below
for i, (name, *_rest) in enumerate(CASES):
    want_plain, want_masked, want_conf = WANT[i]
    got_p, got_m, got_c = float(plain[0, i]), float(masked[0, i]), float(cmask[0, i])
    check(f"unmasked {name:30s} -> {got_p:5.1f}", abs(got_p - want_plain) < 1e-6,
          f"expected {want_plain}")
    ok_m = np.isnan(got_m) if np.isnan(want_masked) else abs(got_m - want_masked) < 1e-6
    check(f"masked   {name:30s} -> {got_m:5.1f}", ok_m, f"expected {want_masked}")
    ok_c = np.isnan(got_c) if np.isnan(want_conf) else got_c == want_conf
    check(f"conf     {name:30s} -> {got_c}", ok_c, f"expected {want_conf}")

check(f"it reports what the mask did ({mm['n_masked']} set aside, "
      f"{mm['n_mask_recovered']} recovered, {mm['n_mask_blind']} blind)",
      (mm["n_masked"], mm["n_mask_recovered"], mm["n_mask_blind"]) == (6, 4, 1),
      f"{mm['n_masked']}/{mm['n_mask_recovered']}/{mm['n_mask_blind']}")
# Case 5, the one real data produces: the shadowed pass is quiet (+0.3 dB) and
# desc sees a real -5 dB scar. The VALUE is -5 either way — desc was louder, so
# it already won. What the mask changes is the label. Unmasked, the quiet asc
# sample counts as an orbit that looked and saw nothing, so the pixel is a
# disagreement (3), and the agreeing-only heat map hides a real scar. Masked,
# asc is blind there, and the pixel is what confidence 1 claims: recovered.
check(f"quiet shadow + real scar, unmasked: disagreement (conf {cplain[0, 5]:.0f}) "
      "— hidden from the agreeing-only heat map", float(cplain[0, 5]) == 3.0)
check("masked: a TERRAIN recovery (conf 1), kept on the map — not a frame edge, "
      "since asc had perfectly finite data there",
      float(cmask[0, 5]) == 1.0 and bool(np.isfinite(asc_before[0, 5])),
      f"conf {cmask[0, 5]}")
check("and the merged value — what Fusion reads — did not move",
      float(plain[0, 5]) == float(masked[0, 5]) == -5.0)
# the loud-shadow mechanism case: a sign conflict becomes a recovery the same way
check(f"loud shadow vs real scar: sign conflict (conf {cplain[0, 2]:.0f}) -> "
      f"recovery (conf {cmask[0, 2]:.0f})",
      float(cplain[0, 2]) == 3.0 and float(cmask[0, 2]) == 1.0)
check("the caller's change arrays are not mutated",
      np.array_equal(asc, asc_before, equal_nan=True)
      and np.array_equal(desc, desc_before, equal_nan=True),
      "merge_geometries wrote through to the tab's live _cd_results arrays")
check("masks=None is exactly the old behaviour",
      np.array_equal(plain, sar_change.merge_geometries(
          [asc, desc], "logratio", THR, masks=None)[0], equal_nan=True))
check("and no mask counts are reported when there are no masks",
      "n_masked" not in _mp)

print("\n=== 7. it refuses a mask it cannot line up ===")
for bad, why in (([m_asc], "one mask for two maps"),
                 ([np.zeros((9, 9), bool), m_desc], "a mask of the wrong shape")):
    try:
        sar_change.merge_geometries([asc, desc], "logratio", THR, masks=bad)
        check(f"{why} is rejected", False, "it was accepted silently")
    except ValueError as e:
        check(f"{why} is rejected: {e}", True)

print("\n=== 8. the look direction is the TRUE heading, not due east/west ===")
# Sentinel-1 is sun-synchronous at 98.18 deg: its ground track leans west of
# north on an ascending pass, more so with latitude, and the Earth turning
# underneath leans it further. Net of the convergence back at the target, due
# E/W put the look ~10 deg wrong at 60N, which alone moved ~22% of the real
# Iliamna shadow mask.
for lat, want_a, want_d in ((0.0, 348.0, 192.0),      # S1's quoted ~-12 deg heading
                            (60.0, 341.6, 198.4)):
    ha = ld.ground_heading_deg("ascending", lat)
    hd = ld.ground_heading_deg("descending", lat)
    check(f"heading at {lat:4.1f}N: asc {ha:.1f}, desc {hd:.1f}",
          abs(ha - want_a) < 0.5 and abs(hd - want_d) < 0.5,
          f"expected ~{want_a} / ~{want_d}")
tilts = [360.0 - ld.ground_heading_deg("ascending", la) for la in (0, 30, 50, 60, 70)]
check("the track leans further from north the further north you go",
      all(b > a for a, b in zip(tilts, tilts[1:])), [round(t, 1) for t in tilts])
# ...but that is the NADIR track, and the target sits 340-620 km to its right,
# where local north has rotated (meridian convergence, dlon*sin(lat)). Leaving
# that out over-corrected due-E/W by about as much as the tilt it fixed.
la, ld_ = ld.look_azimuth_deg("ascending", 60), ld.look_azimuth_deg("descending", 60)
check(f"look azimuth AT THE TARGET: asc {la:.1f} (~79), desc {ld_:.1f} (~281) at 60N",
      abs(la - 79.2) < 0.5 and abs(ld_ - 280.8) < 0.5,
      "nadir heading + 90 alone would give ~72 / ~288")
check("the convergence grows with range: far-swath looks turn further than near",
      ld.look_azimuth_deg("ascending", 60, 46.0) > ld.look_azimuth_deg("ascending", 60, 29.1)
      and ld.look_azimuth_deg("descending", 60, 46.0) < ld.look_azimuth_deg("descending", 60, 29.1))
check("layover faces the radar: illumination aspect = look + 180",
      abs(ld.illumination_aspect_deg("descending", 60) - (ld_ - 180.0)) < 1e-9)
check("no latitude -> the old due-E/W fallback, not a guess",
      (ld.look_azimuth_deg("ascending"), ld.look_azimuth_deg("descending"),
       ld.illumination_aspect_deg("ascending")) == (90.0, 270.0, 270.0))
check("unknown direction -> None at any latitude",
      ld.look_azimuth_deg("", 60) is None and ld.ground_heading_deg(None, 60) is None)

print("\n=== 9. checked against two REAL footprints (Iliamna, 2026-09-12) ===")
# The STAC geometries of the scenes the review fetched. The descending one is a
# quadrilateral with a corner cut; the ascending one is the truncated last slice
# of its datatake. Their long track-parallel edges ARE the along-track direction
# at near and far range, which makes them a direct, independent check on the
# model — and a discriminating one. (Swath WIDTH is not: it is a cosine effect,
# and due-E/W measures within 3% of the true heading. A first version of this
# test checked width, and passed for the wrong model.)
DESC = {"type": "Polygon", "coordinates": [[
    [-149.9823, 59.2311], [-149.4263, 60.582], [-153.9304, 60.9976],
    [-154.3868, 59.5063], [-150.0431, 59.0937], [-149.9823, 59.2311]]]}
ASC = {"type": "Polygon", "coordinates": [[
    [-153.3048, 59.769], [-150.9221, 59.9737], [-151.3939, 61.464],
    [-155.9961, 61.0424], [-155.333, 59.5586], [-153.3048, 59.769]]]}
LAT, LON = 60.032, -153.090


def track_at_target(fp, st, frac):
    """The footprint's own along-track direction where the target sits: the two
    long near-N/S edges, each measured in its OWN local frame, interpolated
    between near range (0) and far range (1)."""
    ring, edges = fp["coordinates"][0], []
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        k = math.cos(math.radians((y0 + y1) / 2))
        de, dn = (x1 - x0) * 111.32 * k, (y1 - y0) * 110.54
        h = math.degrees(math.atan2(de, dn)) % 360
        if math.hypot(de, dn) > 100 and min(abs((h + 180) % 360 - 180),
                                            abs(h - 180)) < 30:
            if (st == "ascending") != (math.cos(math.radians(h)) > 0):
                h = (h + 180) % 360
            edges.append(((x0 + x1) / 2, h))
    # near range is the side nearer the sensor: west ascending, east descending
    edges.sort(key=lambda e: e[0], reverse=(st == "descending"))
    (_, hn), (_, hf) = edges
    return (hn + frac * (((hf - hn + 180) % 360) - 180)) % 360


for nm, fp in (("ascending", ASC), ("descending", DESC)):
    inc = ld.iw_incidence_deg(fp, LON, LAT, nm)
    frac = (ld._ground_range_at_incidence(inc) - ld._IW_NEAR_KM) / \
        (ld._IW_FAR_KM - ld._IW_NEAR_KM)
    obs = track_at_target(fp, nm, frac)
    gap = lambda a: abs((a - obs + 180) % 360 - 180)
    model = (ld.look_azimuth_deg(nm, LAT, inc) - 90.0) % 360
    nadir = ld.ground_heading_deg(nm, LAT)
    ew = 0.0 if nm == "ascending" else 180.0
    check(f"{nm:10s} along-track at the target: footprint {obs:.1f}, model "
          f"{model:.1f} (off {gap(model):.1f}); nadir-only off {gap(nadir):.1f}, "
          f"due-E/W off {gap(ew):.1f}",
          gap(model) < 1.0 and gap(nadir) > 5.0 and gap(ew) > 5.0,
          "the model must beat both simplifications, and by a margin")

model_sw = ld._IW_FAR_KM - ld._IW_NEAR_KM
for nm, fp in (("descending", DESC), ("ascending", ASC)):
    look = ld.look_azimuth_deg(nm, LAT, ld.iw_incidence_deg(fp, LON, LAT, nm))
    pr = [d * math.cos(math.radians(b - look))
          for b, d in (ld._bearing_km(LON, LAT, x, y) for x, y in fp["coordinates"][0])]
    w = max(pr) - min(pr)
    check(f"{nm:10s} footprint measures {w:.0f} km of swath (ESA: 250; the model's "
          f"29.1-46.0 deg span is {model_sw:.0f} — documented, not hidden)",
          240 < w < 265, f"{w:.0f} km")
for nm, fp, want in (("descending", DESC, 42.33), ("ascending", ASC, 38.82)):
    got = ld.iw_incidence_deg(fp, LON, LAT, nm)
    check(f"{nm:10s} incidence at Iliamna {got:.2f} deg (not the 39 constant)",
          got is not None and abs(got - want) < 0.1, f"expected {want}")

print("\n=== 10. the incidence model, edge cases ===")
check("near edge is 29.1 deg, far edge 46.0 deg",
      abs(ld._incidence_at_ground_range(ld._IW_NEAR_KM) - 29.1) < 0.01
      and abs(ld._incidence_at_ground_range(ld._IW_FAR_KM) - 46.0) < 0.01)
xs = [ld._IW_NEAR_KM + f * model_sw for f in (0, .25, .5, .75, 1)]
inc = [ld._incidence_at_ground_range(x) for x in xs]
check(f"monotonic across the swath: {[round(v, 1) for v in inc]}",
      all(b > a for a, b in zip(inc, inc[1:])))
check("and NOT linear — mid-swath is 38.3, not the 37.55 a straight line gives",
      abs(inc[2] - 38.3) < 0.1, inc[2])
# a point exactly on a footprint's near or far edge lands on 29.1 / 46.0
look = ld.look_azimuth_deg("descending", LAT, ld.iw_incidence_deg(DESC, LON, LAT, "descending"))
proj = []
for x, y in DESC["coordinates"][0]:
    b, d = ld._bearing_km(LON, LAT, x, y)
    proj.append((d * math.cos(math.radians(b - look)), (x, y)))
(_, near_pt), (_, far_pt) = min(proj), max(proj)
for nm, pt, want in (("near", near_pt, 29.1), ("far", far_pt, 46.0)):
    got = ld.iw_incidence_deg(DESC, pt[0], pt[1], "descending")
    check(f"a point on the footprint's {nm} edge -> {got:.1f} deg", abs(got - want) < 1.0,
          f"expected ~{want}")
# antimeridian: the same footprint shifted to straddle 180 must give the same angle
shift = 180.0 - (-151.0)                      # move the scene's middle onto 180
wrap = lambda x: (x + shift + 180.0) % 360.0 - 180.0
DESC_AM = {"type": "Polygon", "coordinates": [[[wrap(x), y] for x, y in DESC["coordinates"][0]]]}
got_am = ld.iw_incidence_deg(DESC_AM, wrap(LON), LAT, "descending")
check(f"a scene straddling the antimeridian measures the same ({got_am:.2f})",
      got_am is not None and abs(got_am - 42.33) < 0.1)
check("MultiPolygon footprints are read", abs(ld.iw_incidence_deg(
    {"type": "MultiPolygon", "coordinates": [DESC["coordinates"]]},
    LON, LAT, "descending") - 42.33) < 0.1)
for fp, why in ((None, "no footprint"), ({}, "empty geometry"),
                ({"type": "Polygon", "coordinates": [[[-153.1, 60.0], [-153.0, 60.0],
                                                      [-153.0, 60.1], [-153.1, 60.0]]]},
                 "a footprint nowhere near swath-sized")):
    check(f"{why:38s} -> None (caller falls back to 39)",
          ld.iw_incidence_deg(fp, LON, LAT, "descending") is None)
check("unknown direction -> None", ld.iw_incidence_deg(DESC, LON, LAT, "") is None)

print("\n=== 11. radar_shadow and layover_alpha pick the heading up from the grid ===")
geo = (LON - 0.05, 0.0002, 0.0, LAT + 0.025, 0.0, -0.0001)     # degrees: has a latitude
_m, meta = ld.radar_shadow(np.zeros((50, 50), np.float32), geo, "ascending")
check(f"geographic grid -> look azimuth {meta['range_azimuth']:.1f} (true heading)",
      abs(meta["range_azimuth"] - ld.look_azimuth_deg("ascending", LAT)) < 0.2)
_m, meta = ld.radar_shadow(np.zeros((50, 50), np.float32), GT, "ascending")
check("projected grid, no lat_hint -> 90 (it carries no latitude to use)",
      meta["range_azimuth"] == 90.0)
_m, meta = ld.radar_shadow(np.zeros((50, 50), np.float32), GT, "ascending",
                           lat_hint=LAT)
check("projected grid WITH lat_hint -> the true heading",
      abs(meta["range_azimuth"] - ld.look_azimuth_deg("ascending", LAT)) < 0.2)
_a, lmeta = ld.layover_alpha(np.ones((50, 50), np.float32), np.ones((50, 50), bool),
                             np.zeros((50, 50), np.float32), geo, "descending")
check(f"the layover fade uses it too: layover aspect {lmeta['layover_aspect']:.1f} "
      f"(was 90)", abs(lmeta["layover_aspect"]
                       - ld.illumination_aspect_deg("descending", geo[3] + geo[5] * 25)) < 0.1)

print("\n=== 12. the SAR tab hands each scene's footprint to the incidence model ===")
import ast
tsrc = open(os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth",
                         "sar_tab.py")).read()
tree = ast.parse(tsrc)
rec = [k.arg for c in ast.walk(tree) if isinstance(c, ast.Call)
       and getattr(c.func, "id", "") == "dict" for k in c.keywords]
check("_cd_results keeps the post scene's footprint", "footprint" in rec,
      "without it every merge falls back to the 39 deg constant, silently")
helper = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "_terrain_masks"), None)
calls = {c.func.attr for c in ast.walk(helper) if isinstance(c, ast.Call)
         and isinstance(c.func, ast.Attribute)} if helper else set()
check("_terrain_masks estimates incidence per scene", "iw_incidence_deg" in calls)
check("and masks shadow AND layover — never shadow alone",
      {"radar_shadow", "radar_layover"} <= calls,
      "97-98% of one pass's shadow is layover in the other: shadow alone "
      "'recovers' pixels from a pass that folded them over")
kw = {k.arg for c in ast.walk(helper) if isinstance(c, ast.Call)
      and getattr(c.func, "attr", "") == "radar_shadow" for k in c.keywords} if helper else set()
check("and passes it, with the latitude, to radar_shadow",
      {"incidence_deg", "lat_hint"} <= kw, kw)

print("\n=== 13. layover: a radar-facing slope folds onto the ground in front of it ===")
# The mirror of shadow. Slant range grows with ground range as x.sin(t) - h.cos(t);
# a nearer point a and a farther point b swap order when h_b - h_a > d.tan(t),
# and then BOTH are mixed. On a wall that is the wall plus the ground on the
# radar's side of it, out to H / tan(t) — the opposite side from its shadow.
reach = math.floor(1000.0 / (20.0 * math.tan(math.radians(THETA))))   # 61 px
wall2 = np.zeros((100, 150), dtype=np.float32)
wall2[:, 75] = 1000.0
for state, lo, hi, sh_side in (("DESCENDING", 75, 75 + reach, "west"),
                               ("ASCENDING", 75 - reach, 75, "east")):
    L, lmeta = ld.radar_layover(wall2, GT, state, dem_smooth=0)
    cols = np.where(L[50])[0]
    check(f"{state:11s} layover cols {cols.min()}..{cols.max()} — the radar's side, "
          f"{reach} px ({reach * 20} m); its shadow falls {sh_side}",
          cols.min() == lo and cols.max() == hi, f"expected {lo}..{hi}")

up = lambda t, n=60: np.tile(np.arange(n) * 20.0 * math.tan(math.radians(t)),
                             (n, 1)).astype(np.float32)   # rises to the east
for tilt, want in ((THETA - 4, False), (THETA + 6, True)):
    # rising to the east = facing WEST = facing an ascending look
    L, _ = ld.radar_layover(up(tilt), GT, "ASCENDING", dem_smooth=0)
    check(f"a {tilt:.0f}deg slope facing the radar is "
          f"{'laid over' if L.mean() > 0.5 else 'clean'} (cut is the incidence, "
          f"{THETA:.0f}deg)", (L.mean() > 0.5) == want, f"{L.mean():.0%}")
L, _ = ld.radar_layover(up(42.0), GT, "ASCENDING", incidence_deg=46.0, dem_smooth=0)
check("the cut moves with incidence: 42deg is clean at 46deg incidence",
      not L.any(), f"{L.mean():.0%}")
L, _ = ld.radar_layover(up(45.0), GT, "DESCENDING", dem_smooth=0)
check("a slope facing AWAY from the radar never lays over", not L.any())
L1, _ = ld.radar_layover(up(60.0).T, GT, "ASCENDING", dem_smooth=0)
L2, _ = ld.radar_layover(up(60.0).T, GT, "DESCENDING", dem_smooth=0)
check("north/south 60deg slope: no layover in either pass", not L1.any() and not L2.any())
for state, why in (("", "no orbit"), ("sideways", "unrecognised orbit")):
    L, meta = ld.radar_layover(wall2, GT, state, dem_smooth=0)
    check(f"{why:18s} -> nothing masked", not L.any(), meta["note"])
L, _ = ld.radar_layover(np.zeros((40, 40), np.float32), GT, "ASCENDING", dem_smooth=0)
check("flat ground -> nothing masked", not L.any())
edge2 = np.zeros((60, 60), dtype=np.float32)
edge2[:, 0] = 3000.0                        # 3 km wall on the WESTERN edge
L, _ = ld.radar_layover(edge2, GT, "DESCENDING", dem_smooth=0)   # lit from the east
check("descending: a wall on the west edge lays over eastward, not around the "
      "array", L[:, 1:].mean() > 0.9 and L[:, 0].all(), f"{L.mean():.0%}")
L, _ = ld.radar_layover(edge2, GT, "ASCENDING", dem_smooth=0)    # lit from the west
check("ascending: that wall's layover falls off the grid, nothing wraps",
      not L[:, 2:].any(), f"{L[:, 2:].sum()} px wrapped")

print("\n=== 14. what one pass loses the other fills — except steep east/west faces ===")
# A slope steep enough to face AWAY from one look by more than 90 - t_a faces
# the other look by more than t_b whenever t_a + t_b < 90 — true at Iliamna
# (38.8 + 42.3) and for these defaults, NOT for two far-range passes (46 + 46).
# So there, shadow in one pass is layover in the other: that face is lost to
# Sentinel-1 from both sides. Measured on real Iliamna terrain, 97-98% of each
# pass's shadow is layover in the other.
dn = lambda t, n=60: up(t, n)[:, ::-1].copy()          # falls to the east
for tilt in (50.0, 55.0, 60.0, 70.0, 80.0):
    fa = dn(tilt)
    a_sh, _ = ld.radar_shadow(fa, GT, "ascending", dem_smooth=0)
    d_lo, _ = ld.radar_layover(fa, GT, "descending", dem_smooth=0)
    if a_sh.mean() > 0.5:
        check(f"{tilt:.0f}deg east-facing: shadowed to ascending AND laid over to "
              f"descending — lost to both", d_lo.mean() > 0.5, f"{d_lo.mean():.0%}")
    else:
        check(f"{tilt:.0f}deg east-facing: lit for ascending, laid over for "
              f"descending — ascending fills it", d_lo.mean() > 0.5)

# the merge, with the tab's real mask (shadow | layover), on one pixel of each
PIX = {"recoverable (45deg E-facing)": dn(45.0), "lost (60deg E-facing)": dn(60.0)}
for name, fa in PIX.items():
    blind = {st: ld.radar_shadow(fa, GT, st, dem_smooth=0)[0]
             | ld.radar_layover(fa, GT, st, dem_smooth=0)[0]
             for st in ("ascending", "descending")}
    r, c = 30, 30
    a_px = np.array([[-5.0]], np.float32)          # ascending sees a real scar
    d_px = np.array([[0.4]], np.float32)           # descending "sees" nothing
    mg, cf, _m = sar_change.merge_geometries(
        [a_px, d_px], "logratio", THR,
        masks=[blind["ascending"][r:r + 1, c:c + 1], blind["descending"][r:r + 1, c:c + 1]])
    pl, cp, _p = sar_change.merge_geometries([a_px, d_px], "logratio", THR)
    if name.startswith("recoverable"):
        check(f"{name}: unmasked a disagreement (conf {cp[0, 0]:.0f}), masked a "
              f"recovery (conf {cf[0, 0]:.0f}) keeping ascending's {mg[0, 0]:.1f} dB",
              float(cp[0, 0]) == 3.0 and float(cf[0, 0]) == 1.0 and float(mg[0, 0]) == -5.0)
    else:
        check(f"{name}: blind to both passes -> NaN, not a guess "
              f"(unmasked it would have claimed {pl[0, 0]:.1f} dB)",
              bool(np.isnan(mg[0, 0])) and bool(np.isnan(cf[0, 0])))

print()
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("RADAR SHADOW + LAYOVER + TERRAIN-AWARE MERGE VERIFIED")
