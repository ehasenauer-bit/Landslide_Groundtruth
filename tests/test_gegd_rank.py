"""GEGD triage: which side a strip is on, what it is good for, and the ★ pair.

Run with tests/run_all.sh, or any python at all — gegd_rank is plain stdlib:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_rank.py

The whole file runs under TZ=America/Anchorage. The plugin keeps event times as
naive UTC, and the one way to break the before/after split silently is to let
Python read a naive time as the Mac's local clock: Iliamna's 08-01 00:18Z WV01
strip is 07-31 in Alaska, and an 8-hour slip moves strips across the origin.

What else it pins:
  * relief shift: a pre-event DEM puts a 50 m drop ~34 m sideways at 30° off-nadir
    (incidence, not off-nadir: the Earth curves away under the satellite);
  * coverage measured on the ground, against shapes whose answers are known;
  * the golden Iliamna-shaped pair — ★ 08-01 WV01 -> 08-26 WV02, the FIRST good
    after-strip over the lower off-nadir 08-31 (early snow can bury a deposit);
  * visual (coarse DEM / steep view) and context (SAR, third-party, mosaics) are
    never ★; RDOG_BASE is a multi-date mosaic; Vivid is never a candidate;
  * every empty or one-sided result says why, and its advice works when followed.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ["TZ"] = "America/Anchorage"
time.tzset()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
sys.path.insert(0, PKG)

from landslide_groundtruth import gegd_api as A  # noqa: E402
from landslide_groundtruth import gegd_rank as R  # noqa: E402

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def load(name):
    with open(os.path.join(FIX, name), "rb") as f:
        data = f.read()
    return A.parse_features(data), json.loads(data)["_event"]


UTC = timezone.utc
N = [0]


def mk(acq, ona=20.0, level="LV3D", sensor="WV02", sun_el=40.0, sun_az=170.0, cloud=0.0,
       rings=None, **kw):
    """A Strip with a footprint that covers the test circle unless told otherwise."""
    N[0] += 1
    if rings is None:
        rings = (((-153.3, 59.9), (-153.3, 60.2), (-152.8, 60.2), (-152.8, 59.9), (-153.3, 59.9)),)
    return A.Strip(feature_id=kw.pop("fid", f"00000000-0000-4000-8000-{N[0]:012d}"),
                   acq_utc=acq, sensor=sensor, source=sensor, ona=ona, processing_level=level,
                   sun_el=sun_el, sun_az=sun_az, cloud=cloud, rings=rings,
                   product_name=kw.pop("product_name", "DAILY_TAKE"),
                   sensor_type=kw.pop("sensor_type", "Optical"), **kw)


LAT, LON = 60.0265, -153.0559

print("=== 0. the test really runs on Alaska time ===")
t = datetime(2026, 8, 8, 12, tzinfo=UTC).timestamp()
check("TZ=America/Anchorage is in effect (AKDT, UTC-8 in August)",
      time.strftime("%Z", time.localtime(t)) == "AKDT" and time.localtime(t).tm_gmtoff == -28800)

print("\n=== 1. before/after is a strict split on full UTC time ===")
O = datetime(2026, 8, 8, 3, 30, 0, tzinfo=UTC)           # 08-07 19:30 AKDT
minus1 = mk(O - timedelta(seconds=1))
exact = mk(O)
plus1 = mk(O + timedelta(seconds=1))
pre, post = R.split_sides([minus1, exact, plus1], O)
check("origin − 1 s is BEFORE", pre == [minus1])
check("exactly the origin is AFTER (a strip at the failure sees the slide)", post == [exact, plus1])
naive_o = datetime(2026, 8, 8, 3, 30, 0)                 # how Detection.origin_utc holds it
early = mk(datetime(2026, 8, 8, 0, 18, 47, tzinfo=UTC))  # 08-07 16:18 AKDT
late = mk(datetime(2026, 8, 8, 5, 0, 0, tzinfo=UTC))     # 08-07 21:00 AKDT
pre, post = R.split_sides([early, late], naive_o)
check("a NAIVE origin is read as UTC, not as the Mac's Alaska clock "
      "(else 05:00Z would land before an 11:30Z 'origin')", pre == [early] and post == [late])
akdt = timezone(timedelta(hours=-8))
pre2, post2 = R.split_sides([early, late], datetime(2026, 8, 7, 19, 30, tzinfo=akdt))
check("an aware AKDT origin is the same instant: same split", (pre2, post2) == (pre, post))
pre3, _ = R.split_sides([early, late], "2026-08-08T03:30:00Z")
check("an ISO 'Z' origin works too", pre3 == [early])
ili, ILI = load("wfs_iliamna.json")
s0801 = next(s for s in ili if s.label == "2026-08-01 WV01")
pre, post = R.split_sides(ili, ILI["origin_utc"])
check("Iliamna 08-01 00:18Z WV01 is BEFORE an 08-08 event…", s0801 in pre)
check("…though Alaska calls that afternoon 07-31: the tooltip says so",
      R.alaska_str(s0801.acq_utc) == "2026-07-31 16:18 AKDT (previous day)")
check("alaska_str of the event itself reads the same day when it is",
      R.alaska_str(datetime(2026, 8, 8, 20, 0, tzinfo=UTC)) == "2026-08-08 12:00 AKDT")
check("winter offset is AKST (UTC-9)", R.alaska_str(datetime(2026, 1, 15, 3, 0)) ==
      "2026-01-14 18:00 AKST (previous day)")
check("rank puts every strip on the side split_sides gives it",
      all(r.side == ("pre" if r.strip in pre else "post")
          for r in R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0).pre
          + R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0).post))
rk_edge = R.rank([minus1, exact], O, LAT, LON, 5.0)
check("rank itself: origin − 1 s listed BEFORE, exactly-origin listed AFTER",
      [r.strip for r in rk_edge.pre] == [minus1] and [r.strip for r in rk_edge.post] == [exact])
t0, t1 = R.search_window(naive_o, 365, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))
check("window: [origin − 365 d, min(origin + 90 d, now)), aware UTC",
      t0 == O - timedelta(days=365) and t1 == datetime(2026, 10, 13, tzinfo=UTC))

print("\n=== 2. view geometry: incidence and relief shift ===")
check("relief shift at 30° off-nadir, WV02: 33.8 ± 0.5 m per 50 m drop",
      abs(R.relief_shift_m(30.0, "WV02") - 33.8) <= 0.5)
check("Iliamna 08-26 (WV02, 20.2°): ~21 m", abs(R.relief_shift_m(20.2, "WV02") - 21.0) <= 0.5)
check("Iliamna 08-01 (WV01, 29.6°): ~31 m", abs(R.relief_shift_m(29.6, "WV01") - 31.4) <= 0.5)
check("incidence > off-nadir (Earth curvature): WV02 20.2° -> 22.8°",
      abs(R.incidence_deg(20.2, "WV02") - 22.8) <= 0.1)
check("nadir: no shift", R.relief_shift_m(0.0, "WV03") == 0.0)
check("unknown off-nadir: no number invented", R.relief_shift_m(None, "WV02") is None
      and R.incidence_deg(None, "WV02") is None)
check("shift scales with the drop", abs(R.relief_shift_m(30.0, "WV02", dh=100) - 2 * R.relief_shift_m(30.0, "WV02")) < 1e-9)
check("monotonic in off-nadir", all(R.relief_shift_m(a, "WV02") < R.relief_shift_m(a + 5, "WV02")
                                     for a in range(0, 40, 5)))
check("unknown sensor falls back to a nominal altitude (no crash)",
      20 < R.relief_shift_m(25.0, "LG01") < 30)

print("\n=== 3. coverage on shapes with known answers ===")
disc = R.aoi_disc(LAT, LON, 5.0)
kx = R.KM_PER_DEG * math.cos(math.radians(LAT))


def km_box(x0, y0, x1, y1):
    """A lon/lat ring from a km box around the epicentre."""
    p = lambda x, y: (LON + x / kx, LAT + y / R.KM_PER_DEG)  # noqa: E731
    return (p(x0, y0), p(x0, y1), p(x1, y1), p(x1, y0), p(x0, y0))


def km_poly(pts):
    ring = [(LON + x / kx, LAT + y / R.KM_PER_DEG) for x, y in pts]
    return tuple(ring + [ring[0]])


check("the disc is a closed ring that is round on the ground (radius 5 km in km, not °)",
      disc[0] == disc[-1] and all(abs(math.hypot((p[0] - LON) * kx, (p[1] - LAT) * R.KM_PER_DEG) - 5.0) < 1e-6
                                  for p in disc))
f, c = R.aoi_cover([km_box(-20, -20, 20, 20)], disc)
check(f"full: {f:.4f}, covers the epicentre", abs(f - 1.0) < 1e-9 and c)
f, c = R.aoi_cover([km_box(-30, -30, -10, -10)], disc)
check(f"none: {f:.4f}, misses", f == 0.0 and not c)
f, _ = R.aoi_cover([km_box(0, -20, 20, 20)], disc)
check(f"half (east of the epicentre): {f:.4f}", abs(f - 0.5) < 1e-9)
f, _ = R.aoi_cover([km_box(0, 0, 20, 20)], disc)
check(f"quarter: {f:.4f}", abs(f - 0.25) < 1e-9)
# a N–S band 2.5 km either side: the true width counts (it would be twice as wide
# in degrees of longitude at 60°N if anything worked in degrees)
f, c = R.aoi_cover([km_box(-2.5, -20, 2.5, 20)], disc)
band = 2 * (0.5 * math.sqrt(1 - 0.25) + math.asin(0.5)) / math.pi   # unit-disc |x|<0.5 / π
check(f"band |x| < r/2 measured in km: {f:.4f} vs analytic {band:.4f}", abs(f - band) < 0.01 and c)
# non-convex: an L that leaves out the corner region x<1, y<1 (the epicentre sits in the notch)
L = km_poly([(-20, 1), (-20, 20), (20, 20), (20, -20), (1, -20), (1, 1)])
f, c = R.aoi_cover([L], disc)
# independent oracle: fine grid over the same 64-gon
inside_disc = lambda x, y: R._inside((x, y), [((p[0] - LON) * kx, (p[1] - LAT) * R.KM_PER_DEG)  # noqa: E731
                                              for p in disc[:-1]])
n = tot = hit = 0
step = 0.05
y = -5 + step / 2
while y < 5:
    x = -5 + step / 2
    while x < 5:
        if inside_disc(x, y):
            tot += 1
            hit += not (x < 1 and y < 1)
        x += step
    y += step
check(f"non-convex L: {f:.4f} vs grid {hit / tot:.4f}; epicentre in the notch -> misses",
      abs(f - hit / tot) < 0.005 and not c)
f, c = R.aoi_cover([km_box(-20, -20, 0.0001, 20), km_box(0.0001, -20, 20, 20)], disc)
check(f"two abutting parts (MultiPolygon) sum: {f:.4f}", abs(f - 1.0) < 1e-6 and c)
f, c = R.aoi_cover([km_box(-20, -20, 20, 20)[::-1]], disc)
check("ring orientation doesn't matter", abs(f - 1.0) < 1e-9 and c)
check("no geometry -> 0, misses", R.aoi_cover((), disc) == (0.0, False))
check("plain ring (no Disc attributes) works off its vertex mean",
      abs(R.aoi_cover([km_box(-20, -20, 20, 20)], tuple(disc))[0] - 1.0) < 1e-9)
bb = R.event_bbox(LAT, LON, 5.0)
check("event_bbox is lon-first and holds the circle plus 10 %",
      bb[0] < LON < bb[2] and bb[1] < LAT < bb[3]
      and abs((bb[3] - LAT) * R.KM_PER_DEG - 5.5) < 1e-6 and abs((bb[2] - LON) * kx - 5.5) < 1e-6)
check("event_bbox feeds the CQL builder without complaint", "'CRS:84'" in A.bbox_cql(bb))

print("\n=== 4. grades ===")
D = datetime(2026, 8, 20, tzinfo=UTC)
check("LV3D -> mapping", R.grade(mk(D, 28.0, "LV3D")) == "mapping")
check("LV2A -> visual, whatever the angle", R.grade(mk(D, 12.0, "LV2A")) == "visual")
check("no level, ONA 25 -> mapping", R.grade(mk(D, 25.0, None)) == "mapping")
check("no level, ONA 33 -> visual", R.grade(mk(D, 33.0, None)) == "visual")
check("no level, no ONA -> visual (nothing vouches for it)", R.grade(mk(D, None, None)) == "visual")
check("a stricter limit demotes LV3D 28° to visual", R.grade(mk(D, 28.0, "LV3D"), ona_max=25) == "visual")
check("SAR -> context", R.grade(mk(D, None, None, sensor_type="Radar", product_name=None)) == "context")
check("third-party optical -> context", R.grade(mk(D, 10.0, "LV3D", is_third_party=True)) == "context")
check("multi-date mosaic -> context", R.grade(mk(D, 10.0, "LV3D", multi_date=True)) == "context")
check("an unknown product -> context", R.grade(mk(D, 10.0, "LV3D", product_name="SOMETHING_NEW")) == "context")
rd = next(s for s in ili if s.product_name == "RDOG_BASE")
check("RDOG_BASE (5-day mosaic) is flagged multi-date and graded context",
      rd.multi_date and R.grade(rd) == "context")
bagley_like = A.parse_features(json.dumps({"features": [{"type": "Feature", "geometry": None,
    "properties": {"featureId": "f0000000-0000-4000-8000-0000000000b1",
                   "acquisitionDate": "2019-06-26T20:51:59Z", "productName": "RDOG_BASE",
                   "source": "GE01", "earliestAcquisitionTime": "2019-06-26T20:51:59.000000",
                   "latestAcquisitionTime": "2019-06-26T20:52:34.000000"}}]}).encode())[0]
check("…even one whose span is only 35 s (Bagley's): the product says mosaic",
      bagley_like.multi_date and R.grade(bagley_like) == "context")

print("\n=== 5. the golden Iliamna-shaped pair ===")
t0, t1 = R.search_window(ILI["origin_utc"], ILI["pre_days"], ILI["post_days"],
                         now_utc=datetime(2026, 10, 13, tzinfo=UTC))
rk = R.rank(ili, ILI["origin_utc"], ILI["lat"], ILI["lon"], ILI["r_km"], None, t0=t0, t1=t1)
sp, sq = rk.star_pre, rk.star_post
check("★ before = 2026-08-01 WV01, ONA 29.6, LV3D",
      sp is not None and sp.strip.label == "2026-08-01 WV01" and sp.strip.ona == 29.6
      and sp.strip.processing_level == "LV3D")
check("★ after = 2026-08-26 WV02, ONA 20.2",
      sq is not None and sq.strip.label == "2026-08-26 WV02" and sq.strip.ona == 20.2)
check("both carry star=True, nothing else does",
      sp.star and sq.star and sum(r.star for r in rk.pre + rk.post) == 2)
by = {r.strip.label + f" {r.strip.ona}": r for r in rk.post}
r0826, r0831 = by["2026-08-26 WV02 20.2"], by["2026-08-31 WV02 14.1"]
check(f"08-26 (s={r0826.score:.3f}) outranks the lower off-nadir 08-31 (s={r0831.score:.3f}): "
      "first good after-strip wins", r0826.score < r0831.score
      and rk.post.index(r0826) < rk.post.index(r0831))
check("…and the pair term agrees (08-26 pairs better with 08-01 than 08-31 does)",
      R.pair_cost(sp, r0826) < R.pair_cost(sp, r0831))
listed = [r for r in rk.post if r.hidden_reason is None]
s0922 = [r for r in rk.post if r.strip.label == "2026-09-22 WV02"]
check("08-31 and both 09-22 strips are listed", r0831 in listed and len(s0922) == 2
      and all(r in listed for r in s0922))
check("the 09-22 pair carry the stereo badge, naming each other",
      s0922[0].stereo == (s0922[1].strip.feature_id,) and s0922[1].stereo == (s0922[0].strip.feature_id,))
check("…and rank below 08-26 and 08-31", all(rk.post.index(r) > rk.post.index(r0831) for r in s0922))
check("no Vivid row anywhere (and it is counted, not hidden)",
      not any((r.strip.product_name or "").startswith("VIVID") for r in rk.pre + rk.post)
      and rk.n_excluded == 1)
rdr = next(r for r in rk.pre if r.strip.product_name == "RDOG_BASE")
check("RDOG_BASE listed as context, multi-date, missing the epicentre, not ★",
      rdr.grade == "context" and rdr.strip.multi_date and not rdr.covers_epicentre and not rdr.star)
sar = next(r for r in rk.post if r.strip.sensor_type == "Radar")
check("third-party SAR is hidden by default with the licence reason, no shift computed",
      sar.hidden_reason and "third-party" in sar.hidden_reason and sar.shift50 is None
      and sar.grade == "context")
check("the strip inside the search BOX but outside the circle was dropped",
      rk.n_outside == 1 and not any(r.strip.label == "2026-07-15 WV03" for r in rk.pre + rk.post))
check("the low-sun strip is listed hidden with its reason",
      any(r.hidden_reason == "sun 12° < 15°" for r in rk.pre))
check("rows: listed mapping, then visual, then context, then hidden",
      [(r.hidden_reason is not None, R.GRADE_ORDER[r.grade]) for r in rk.pre]
      == sorted((r.hidden_reason is not None, R.GRADE_ORDER[r.grade]) for r in rk.pre))
check("the shift line is there for the ★ after-strip (~21 m)", abs(sq.shift50 - 21.0) < 0.5)
check("no empty-state warning for a good search",
      not any(("archive" in m) or ("hidden by Scene filters" in m) or ("No before" in m)
              or ("No after" in m) for m in rk.messages))
check("one note says a strip outside the circle was left out",
      any("misses the search circle" in m for m in rk.messages))
check("re-ranking is pure: same answer twice",
      R.rank(ili, ILI["origin_utc"], ILI["lat"], ILI["lon"], 5.0).star_post.strip
      == rk.star_post.strip)

print("\n=== 6. filters re-rank with no requests ===")
rk25 = R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0, {"ona_max": 25.0})
check("ONA ≤25: 08-01 (29.6°) drops to visual and is no longer ★",
      rk25.star_pre is not None and rk25.star_pre.strip.label != "2026-08-01 WV01"
      and next(r for r in rk25.pre if r.strip.label == "2026-08-01 WV01").grade == "visual")
check("…its replacement is a mapping-grade before-strip within 25°",
      rk25.star_pre.grade == "mapping" and rk25.star_pre.strip.ona <= 25)
rk_nv = R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0, R.Filters(show_visual=False))
check("visual hidden when 'also list visual' is off, with the reason",
      all(r.hidden_reason and "visual only" in r.hidden_reason for r in rk_nv.pre if r.grade == "visual"))
rk_tp = R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0, {"third_party": True})
check("Include third-party lists the SAR strip — still context, still not ★",
      any(r.strip.sensor_type == "Radar" and r.hidden_reason is None for r in rk_tp.post)
      and rk_tp.star_post.strip.sensor_type == "Optical")
rk_s = R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0, {"sensors": {"WV01": True, "WV02": False}})
check("sensor ticks: WV02 untick hides every WV02 (★ after gone), WV01 ★ stays",
      rk_s.star_post is None and rk_s.star_pre.strip.sensor == "WV01")
rk_c = R.rank(ili, ILI["origin_utc"], LAT, LON, 5.0, {"cloud_max": 5.0})
check("strip cloud ≤5 %: the 31 % and 14.8 % strips hide with a percent reason",
      sum(1 for r in rk_c.pre if r.hidden_reason and "strip cloud" in r.hidden_reason) == 2)
check("a strip past 45° is hidden even with visual listed",
      R.rank([mk(D, 47.0, None)], datetime(2026, 8, 1, tzinfo=UTC), LAT, LON, 5.0).post[0].hidden_reason
      == "off-nadir 47° > 45°")

print("\n=== 7. visual and context are never ★ ===")
O2 = datetime(2026, 8, 8, 13, tzinfo=UTC)
base_pre = mk(O2 - timedelta(days=7), 20.0, "LV3D")
for name, strip in (
        ("LV2A after", mk(O2 + timedelta(days=10), 34.0, "LV2A")),
        ("steep no-level after", mk(O2 + timedelta(days=10), 38.0, None)),
        ("SAR after", mk(O2 + timedelta(days=2), None, None, sensor_type="Radar", product_name=None,
                         is_third_party=True)),
        ("mosaic after", mk(O2 + timedelta(days=2), 5.0, "LV3D", multi_date=True)),
        ("unknown product after", mk(O2 + timedelta(days=2), 5.0, "LV3D", product_name="SOMETHING_NEW"))):
    r = R.rank([base_pre, strip], O2, LAT, LON, 5.0, {"third_party": True})
    check(f"{name}: listed but not ★; the before-side ★ stands alone",
          r.star_post is None and r.star_pre is not None and r.star_pre.strip == base_pre
          and r.post and r.post[0].hidden_reason is None)
    check(f"  …and a message says nothing is starred after", any("No mapping-grade after" in m
                                                                   for m in r.messages))
knik, KN = load("wfs_knik.json")
rkk = R.rank(knik, KN["origin_utc"], KN["lat"], KN["lon"], KN["r_km"])
check("Knik: the 09-02 WV03 (WV03_VNIR) after-strip is ★",
      rkk.star_post is not None and rkk.star_post.strip.label == "2026-09-02 WV03")
check("Knik: the only listed before-strip is coarse-DEM LV2A -> no ★ before, and it says so",
      rkk.star_pre is None and [r.grade for r in rkk.pre if r.hidden_reason is None] == ["visual"]
      and any("No mapping-grade before" in m for m in rkk.messages))

print("\n=== 8. ranking details ===")
cov = mk(O2 + timedelta(days=5), 20.0)
miss = mk(O2 + timedelta(days=5), 20.0, rings=(km_box(0.5, -20, 20, 20),))
r = R.rank([miss, cov], O2, LAT, LON, 5.0)
check("a strip that misses the epicentre ranks below one covering it",
      r.post[0].strip == cov and not r.post[1].covers_epicentre and r.star_post.strip == cov)
check("…by the epicentre penalty plus the lost cover",
      abs((r.post[1].score - r.post[0].score)
          - (R.W_MISS_EPI + R.W_COVER * (1 - r.post[1].cover))) < 1e-9)
# a footprint covering ~97 % of the circle but with a 400 m slot through the
# epicentre (a nodata gap): one day nearer, it still loses to a strip that sees it
notch = km_poly([(-20, -20), (-20, 20), (20, 20), (20, -20), (0.2, -20), (0.2, 0.2),
                 (-0.2, 0.2), (-0.2, -20)])
gap = mk(O2 + timedelta(days=4), 20.0, rings=(notch,))
sees = mk(O2 + timedelta(days=5), 20.0)
r = R.rank([gap, sees], O2, LAT, LON, 5.0)
g = next(x for x in r.post if x.strip == gap)
check(f"missing the epicentre costs on its own (cover {g.cover:.3f}, a day nearer, still loses)",
      g.cover > 0.95 and not g.covers_epicentre and r.star_post.strip == sees
      and R.W_COVER * (1 - g.cover) < 1 / R.TAU_POST_D)     # cover alone would not do it
check("weights are the plan's (§5; decision 8: change them together with the golden test)",
      (R.TAU_PRE_D, R.TAU_POST_D, R.W_TANINC, R.W_SUN, R.W_COVER, R.W_MISS_EPI, R.W_CLOUD,
       R.W_DSUN_EL, R.W_DSUN_AZ, R.W_DTAN, R.K_PAIR)
      == (30.0, 15.0, 2.0, 0.5, 1.0, 3.0, 0.5, 0.1, 0.0167, 0.5, 5))
dup = mk(O2 + timedelta(days=3), 15.0, fid="d0000000-0000-4000-8000-000000000001")
r = R.rank([dup, dup, dup], O2, LAT, LON, 5.0)
check("duplicate featureIds are listed once", len(r.post) == 1 and r.n_duplicate == 2)
low = mk(O2 + timedelta(days=5), 20.0, sun_el=18.0)
high = mk(O2 + timedelta(days=5), 20.0, sun_el=40.0)
r = R.rank([low, high], O2, LAT, LON, 5.0)
check("sun below 25° costs (0.5 × 7/10 = 0.35)", abs(r.post[1].score - r.post[0].score - 0.35) < 1e-9
      and r.post[0].strip == high)
# common-mode shift: two after-strips equal in score, the one whose view angle
# matches the ★ before wins the pair
pre_steep = mk(O2 - timedelta(days=3), 28.0)
a_match = mk(O2 + timedelta(days=4), 27.0)
a_off = mk(O2 + timedelta(days=4), 27.0)
r = R.rank([pre_steep, a_match, a_off], O2, LAT, LON, 5.0)
check("ties broken deterministically (featureId), never by list order",
      R.rank([pre_steep, a_off, a_match], O2, LAT, LON, 5.0).star_post.strip == r.star_post.strip)
near_flat = mk(O2 + timedelta(days=3, hours=12), 5.0)
steep = mk(O2 + timedelta(days=4), 27.0)
rs = R.rank([pre_steep, near_flat, steep], O2, LAT, LON, 5.0)
sc = {x.strip: x.score for x in rs.post}
check("pair term: |Δtan inc| makes the steep after-strip pair better with a steep before "
      "than its own score alone would",
      R.pair_cost(rs.pre[0], next(x for x in rs.post if x.strip == steep))
      - sc[steep] < R.pair_cost(rs.pre[0], next(x for x in rs.post if x.strip == near_flat)) - sc[near_flat])
# the best PAIR can be a strip that is not the best on its own: an after-strip
# one day later with a 70° sun-azimuth mismatch loses to one three days later
# that shares the before-strip's light — but only if it is inside the top k
p_k = mk(O2 - timedelta(days=4), 20.0, sun_az=170.0)
a_near = mk(O2 + timedelta(days=1), 20.0, sun_az=100.0)
a_lit = mk(O2 + timedelta(days=3), 20.0, sun_az=170.0)
rk_k = R.rank([p_k, a_near, a_lit], O2, LAT, LON, 5.0)
check("the pair can beat the per-strip order (shared sun azimuth wins)",
      rk_k.post[0].strip == a_near and rk_k.star_post.strip == a_lit)
check("…but only among the top k: with k=1 the nearest is ★",
      R.best_pair(rk_k.pre, rk_k.post, k=1)[1].strip == a_near)
m = R.stereo_mates([mk(O2, sensor="WV02"), mk(O2 + timedelta(seconds=40), sensor="WV02"),
                    mk(O2 + timedelta(seconds=40), sensor="WV03"),
                    mk(O2 + timedelta(minutes=6), sensor="WV02")])
check("stereo: same sensor < 5 min only (not another sensor, not 6 min later)",
      len(m) == 2 and all(len(v) == 1 for v in m.values()))

print("\n=== 9. empty and one-sided results say why ===")
pd_strips, PD = load("wfs_empty_petersdome.json")
t0, t1 = R.search_window(PD["origin_utc"], PD["pre_days"], PD["post_days"],
                         now_utc=datetime(2026, 10, 13, tzinfo=UTC))
r = R.rank(pd_strips, PD["origin_utc"], PD["lat"], PD["lon"], PD["r_km"], t0=t0, t1=t1)
msg = " ".join(r.messages)
check("Peters Dome 2023: the archive-floor message, naming the floor date",
      "2024-07-20" in msg and "archive" in msg and "fail" not in msg.lower())
check("…pointing at the other tabs and an ingest", "Sentinel-2" in msg and "ingest" in msg)
check("…and no ★, no rows", r.star_pre is None and r.star_post is None and not r.pre and not r.post)
O3 = datetime(2026, 7, 1, tzinfo=UTC)
t0, t1 = R.search_window(O3, 365, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))
for hits, want in ((0, "No GEGD coverage here"), (34, "holds 34 within 50 km"),
                   (None, "No GEGD strips in this search circle")):
    m_ = " ".join(R.rank([], O3, LAT, LON, 5.0, t0=t0, t1=t1, canary_hits=hits).messages)
    check(f"zero after the archive floor, canary {hits}: {want!r}", want in m_ and "archive" not in m_)
# Knik with a 60-day before-window: nothing in it; the outside query found 05-02
kt0, kt1 = R.search_window(KN["origin_utc"], 60, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))
in_win = [s for s in knik if kt0 <= s.acq_utc < kt1]
nearest, _ = load("wfs_knik_nearest_pre.json")
r = R.rank(in_win, KN["origin_utc"], KN["lat"], KN["lon"], KN["r_km"], t0=kt0, t1=kt1,
           nearest_pre=nearest[0])
msg = " ".join(r.messages)
check("Knik, 60 d: the post is still ★ (09-02 WV03)", r.star_post.strip.label == "2026-09-02 WV03")
check("…and the before side names the nearest earlier strip",
      "No before-event VHR in 60 d" in msg and "nearest earlier strip 2026-05-02 (WV01, ONA 38°)" in msg)
need = int(msg.split("before-window to ")[1].split(" days")[0])
o = R._utc(KN["origin_utc"])
check(f"verify advice: widening to {need} days really includes that strip "
      "(and one day less would not)",
      R.search_window(o, need, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))[0] <= nearest[0].acq_utc
      < R.search_window(o, need - 1, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))[0])
r = R.rank(in_win, KN["origin_utc"], KN["lat"], KN["lon"], KN["r_km"], t0=kt0, t1=kt1,
           nearest_pre=R.NOT_FOUND)
check("nothing earlier at all: said plainly", "no earlier strip here at all" in " ".join(r.messages))
later = mk(O2 + timedelta(days=120), 15.0)
r = R.rank([base_pre], O2, LAT, LON, 5.0, t0=O2 - timedelta(days=365), t1=O2 + timedelta(days=90),
           nearest_post=later, now_utc=datetime(2027, 3, 1, tzinfo=UTC))
msg = " ".join(r.messages)
need = int(msg.split("after-window to ")[1].split(" days")[0])
check("no after-strip in 90 d: the nearest later one is named",
      "No after-event VHR in 90 d" in msg and "nearest later strip" in msg)
check(f"verify advice: an after-window of {need} days includes it, {need - 1} would not",
      later.acq_utc < O2 + timedelta(days=need) and not later.acq_utc < O2 + timedelta(days=need - 1))
fresh = datetime(2026, 10, 11, tzinfo=UTC)
r = R.rank([mk(fresh - timedelta(days=20), 15.0)], fresh, LAT, LON, 5.0,
           t0=fresh - timedelta(days=365), t1=datetime(2026, 10, 13, tzinfo=UTC),
           now_utc=datetime(2026, 10, 13, tzinfo=UTC))
check("a 2-day-old event with no after-strip: 'yet', how fast strips appear",
      any("yet: the event is 2 d old" in m for m in r.messages))
winter = datetime(2026, 12, 10, 4, tzinfo=UTC)
r = R.rank([mk(winter - timedelta(days=40), 15.0, sun_el=16.0)], winter, LAT, LON, 5.0,
           t0=winter - timedelta(days=365), t1=winter + timedelta(days=90),
           now_utc=datetime(2027, 6, 1, tzinfo=UTC))
msg = " ".join(r.messages)
check("December event, no after-strip: the Dec–Jan gap, February, SAR",
      "Dec–Jan" in msg and "February" in msg and "SAR" in msg)
summer = datetime(2026, 7, 10, tzinfo=UTC)
r = R.rank([mk(summer - timedelta(days=20), 15.0)], summer, LAT, LON, 5.0,
           t0=summer - timedelta(days=365), t1=summer + timedelta(days=90))
check("…but never for a July event", not any("Dec–Jan" in m for m in r.messages))
south = (((-118.5, 34.7), (-118.5, 35.3), (-117.5, 35.3), (-117.5, 34.7), (-118.5, 34.7)),)
r = R.rank([mk(winter - timedelta(days=40), 15.0, rings=south)], winter, 35.0, -118.0, 5.0,
           t0=winter - timedelta(days=365), t1=winter + timedelta(days=90),
           now_utc=datetime(2027, 6, 1, tzinfo=UTC))
check("…nor at a latitude that has winter collections (the strip IS listed there)",
      len(r.pre) == 1 and not r.post and not any("Dec–Jan" in m for m in r.messages)
      and any("No after-event VHR" in m for m in r.messages))
r = R.rank([mk(winter - timedelta(days=40), 15.0, rings=south)], winter, 35.0, -118.0, 5.0,
           t0=winter - timedelta(days=365), t1=datetime(2026, 10, 13, tzinfo=UTC),
           now_utc=datetime(2026, 10, 13, tzinfo=UTC))
check("an event time later than now is called out, never 'the event is -57 d old'",
      any("in the future" in m for m in r.messages) and not any(" -" in m for m in r.messages))
allhid = [mk(O2 - timedelta(days=5), 20.0, sun_el=8.0), mk(O2 + timedelta(days=5), 20.0, sun_el=9.0),
          mk(O2 + timedelta(days=6), 20.0, cloud=0.34)]
r = R.rank(allhid, O2, LAT, LON, 5.0, {"cloud_max": 30.0})
msg = " ".join(r.messages)
check("everything filtered: '3 strip(s) found, all hidden by Scene filters' with reasons",
      "3 strip(s) found, all hidden by Scene filters" in msg and "low sun ×2" in msg
      and "strip cloud ×1" in msg and r.n_hidden == 3)
check("…and no misleading 'no before-event VHR' line on top of it", "No before-event" not in msg)
r = R.rank([mk(O2 + timedelta(days=5), 20.0, rings=(km_box(5.5, 5.5, 9, 9),))], O2, LAT, LON, 5.0,
           t0=O2 - timedelta(days=30), t1=O2 + timedelta(days=30))
check("strips returned but none touches the circle: says so (and how it could be a bug)",
      any("no footprint touches the search circle" in m for m in r.messages) and r.n_outside == 1)

# a window that STRADDLES the archive floor: the floor explains the old part, and the
# canary still has to say whether the new part is uncovered or merely empty
O4 = datetime(2024, 9, 1, 12, tzinfo=UTC)
s0, s1 = R.search_window(O4, 365, 90, now_utc=datetime(2026, 10, 13, tzinfo=UTC))
check("(the straddling window really starts before the floor and ends after it)",
      s0 < A.ARCHIVE_FLOOR_UTC < s1)
for hits, want in ((0, "No GEGD coverage here"), (12, "holds 12 within 50 km")):
    m_ = " ".join(R.rank([], O4, LAT, LON, 5.0, t0=s0, t1=s1, canary_hits=hits).messages)
    check(f"zero across the floor, canary {hits}: archive date AND {want!r}",
          "2024-07-20" in m_ and want in m_)
m_ = " ".join(R.rank(pd_strips, PD["origin_utc"], PD["lat"], PD["lon"], PD["r_km"],
                     t0=R.search_window(PD["origin_utc"], 365, 90)[0],
                     t1=R.search_window(PD["origin_utc"], 365, 90)[1], canary_hits=0).messages)
check("…but a window wholly before the floor gets the floor alone (Peters Dome)",
      "2024-07-20" in m_ and "No GEGD coverage" not in m_)
fut = datetime(2027, 3, 1, tzinfo=UTC)
m_ = " ".join(R.rank([], fut, LAT, LON, 5.0, t0=fut - timedelta(days=365),
                     t1=datetime(2026, 10, 13, tzinfo=UTC), canary_hits=7,
                     now_utc=datetime(2026, 10, 13, tzinfo=UTC)).messages)
check("zero results for a future event time: the date is called out", "in the future" in m_)

# all hidden, but one side had NOTHING at all: "Show them" cannot fix that side
r = R.rank([mk(O2 - timedelta(days=5), 20.0, sun_el=8.0)], O2, LAT, LON, 5.0,
           t0=O2 - timedelta(days=365), t1=O2 + timedelta(days=90),
           nearest_post=R.NOT_FOUND, now_utc=datetime(2026, 11, 20, tzinfo=UTC))
msg = " ".join(r.messages)
check("all hidden + an empty after-side: both said",
      "1 strip(s) found, all hidden by Scene filters" in msg and "No after-event VHR in 90 d" in msg
      and "No before-event" not in msg)

# winter, with a strip found beyond the window: the gap AND where the next strip is
wlater = mk(winter + timedelta(days=75), 15.0, sun_el=16.0)
r = R.rank([mk(winter - timedelta(days=40), 15.0, sun_el=16.0)], winter, LAT, LON, 5.0,
           t0=winter - timedelta(days=365), t1=winter + timedelta(days=60),
           nearest_post=wlater, now_utc=datetime(2027, 6, 1, tzinfo=UTC))
msg = " ".join(r.messages)
check("December event, after-strip beyond the window: Dec–Jan gap and the nearest later strip",
      "Dec–Jan" in msg and f"nearest later strip {wlater.acq_utc:%Y-%m-%d}" in msg)
need = int(msg.split("after-window to ")[1].split(" days")[0])
check(f"verify advice: {need} days includes it, {need - 1} would not",
      wlater.acq_utc < winter + timedelta(days=need)
      and not wlater.acq_utc < winter + timedelta(days=need - 1))


print("\n=== 10. the hints only ever point at a strip that could be used ===")
post_only = [mk(O2 + timedelta(days=18), 20.0)]
w60 = dict(t0=O2 - timedelta(days=60), t1=O2 + timedelta(days=60),
           now_utc=datetime(2027, 1, 1, tzinfo=UTC))
umbra = mk(O2 - timedelta(days=70), None, None, sensor="UMBRA", product_name="THIRD_PARTY",
           is_third_party=True, sensor_type="Radar", sun_el=None)
rdog = mk(datetime(2021, 3, 27, tzinfo=UTC), 10.0, None, sensor="WV01",
          product_name="RDOG_BASE", multi_date=True)
check("hint_candidate: never SAR/third-party, a legacy mosaic, Vivid, or past 45°",
      not R.hint_candidate(umbra) and not R.hint_candidate(rdog)
      and not R.hint_candidate(mk(D, 10.0, product_name="VIVID_STANDARD_30"))
      and not R.hint_candidate(mk(D, 47.0, None))
      and R.hint_candidate(mk(D, 38.0, "LV2A")) and R.hint_candidate(mk(D, 20.0)))
for name, near in (("third-party SAR", umbra), ("RDOG_BASE 2021 mosaic", rdog)):
    msg = " ".join(R.rank(post_only, O2, LAT, LON, 5.0, nearest_pre=near, **w60).messages)
    check(f"a {name} handed in as the nearest earlier strip is not advertised",
          "No before-event VHR in 60 d" in msg and "nearest earlier" not in msg
          and "Widen" not in msg)
far = mk(O2 - timedelta(days=1200), 20.0)
msg = " ".join(R.rank(post_only, O2, LAT, LON, 5.0, nearest_pre=far, **w60).messages)
check("a nearest strip beyond the widest before-window is named, not turned into "
      "advice the spin box would refuse",
      f"beyond the longest before-window ({R.MAX_PRE_DAYS} days)" in msg and "Widen" not in msg)
farl = mk(O2 + timedelta(days=800), 20.0)
msg = " ".join(R.rank([base_pre], O2, LAT, LON, 5.0, t0=O2 - timedelta(days=365),
                      t1=O2 + timedelta(days=90), nearest_post=farl,
                      now_utc=datetime(2029, 1, 1, tzinfo=UTC)).messages)
check("… and the same after the event",
      f"beyond the longest after-window ({R.MAX_POST_DAYS} days)" in msg and "Widen" not in msg)
ctx_side = [mk(O2 - timedelta(days=9), None, None, sensor="ICEYE", product_name="THIRD_PARTY",
               is_third_party=True, sensor_type="Radar", sun_el=None)] + post_only
near_ok, _ = load("wfs_knik_nearest_pre.json")
for tp in (False, True):
    r = R.rank(ctx_side, O2, LAT, LON, 5.0, {"third_party": tp}, nearest_pre=near_ok[0], **w60)
    msg = " ".join(r.messages)
    check(f"a before-side holding only a context record (third-party {'shown' if tp else 'hidden'}) "
          "is empty for the hint: the nearest usable strip is named",
          "No before-event VHR in 60 d (only 1 context record(s)" in msg
          and "nearest earlier strip 2026-05-02" in msg and r.star_pre is None)

print("\n=== 11. only Vivid came back: nothing usable, said so, not an axis-order bug ===")
viv = [mk(O2 - timedelta(days=10), 5.0, product_name="VIVID_STANDARD_30"),
       mk(O2 + timedelta(days=10), 5.0, product_name="VIVID_NEXT")]
r = R.rank(viv, O2, LAT, LON, 5.0, t0=O2 - timedelta(days=365), t1=O2 + timedelta(days=60),
           canary_hits=12, now_utc=datetime(2027, 1, 1, tzinfo=UTC))
msg = " ".join(r.messages)
check(f"two Vivid records: counted as excluded, nothing listed ({r.n_excluded})",
      r.n_excluded == 2 and not r.pre and not r.post)
check("… the zero-result wording with the canary, plus the Vivid count — no axis-order text",
      "holds 12 within 50 km" in msg and "2 Vivid/base-mosaic record(s) ignored" in msg
      and "axis order" not in msg and "returned 0 strip(s)" not in msg)

print("\n=== 12. strip cloud: no cap by default, priced in the score ===")
check("Filters() default cloud_max is 100 (no cap): a whole-strip figure cannot say the "
      "scar is cloudy", R.Filters().cloud_max == 100.0)
cloudy = mk(O2 + timedelta(days=5), 20.0, cloud=0.62)
r = R.rank([base_pre, cloudy], O2, LAT, LON, 5.0)
check("a 62 %-cloud strip is listed and can still be ★ by default",
      r.post[0].hidden_reason is None and r.star_post is not None and r.star_post.strip == cloudy)
clear = mk(O2 + timedelta(days=5), 20.0, cloud=0.0)
r = R.rank([base_pre, cloudy, clear], O2, LAT, LON, 5.0)
check("… but ranks below an otherwise identical clear one",
      r.star_post.strip == clear and r.post[0].strip == clear)

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
