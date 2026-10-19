"""NISAR search: when it runs, what an empty side says, and how granules
become candidate rows  (nisar_imagery.py — the venv half).

Run with tests/run_all.sh, or directly with any python that has requests and
shapely (QGIS's bundled one does):
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_nisar_search.py

Offline: CMR is replaced by a stub returning SYNTHETIC records shaped like the
real UMM-G (field layout checked against a live Iliamna granule 2026-10-19).
What it protects:
  * the date gate is the permissive floor only — a pre-2025 event is not
    searched, but nothing after first science is refused by date;
  * each archive-wide reason for an empty side gets its own advice, because
    the analyst's next step differs (wait, widen, give up);
  * granule names decode to the track/direction the SAR tab pairs on — a
    wrong track is a wrong "same track" pair downstream;
  * one row per pass, preferring the frame over the event and the finer mode;
  * the gallery gets the 10 KB thumbnail, not the 2.6 MB browse.
"""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nisar_imagery as ni  # noqa: E402

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


HOST = "https://nisar.example.test"


def granule(track, direction, frame, mode, when, lon0=-155.0, lat0=59.0,
            size=3.0, collection="C-PROV"):
    """A synthetic UMM-G item: real field layout, invented values."""
    ur = (f"NISAR_L2_PR_GCOV_027_{track:03d}_{direction}_{frame:03d}_{mode}_DHDH_A_"
          f"{when:%Y%m%dT%H%M%S}_{when + dt.timedelta(seconds=39):%Y%m%dT%H%M%S}"
          f"_P05023_N_F_J_001")
    pts = [(lon0, lat0), (lon0 + size, lat0), (lon0 + size, lat0 + size),
           (lon0, lat0 + size), (lon0, lat0)]
    base = f"{HOST}/NISAR/COLL/{ur}/{ur}"
    browse = f"{HOST}/BROWSE/COLL/{ur}/{ur}"
    return {"meta": {"collection-concept-id": collection},
            "_collection": {"C-PROV": "NISAR_L2_GCOV_PROVISIONAL_V1",
                            "C-BETA": "NISAR_L2_GCOV_BETA_V1"}[collection],
            "umm": {"GranuleUR": ur,
                    "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {
                        "GPolygons": [{"Boundary": {"Points": [
                            {"Longitude": x, "Latitude": y} for x, y in pts]}}]}}},
                    "RelatedUrls": [
                        {"Type": "GET DATA", "URL": base + ".h5"},
                        {"Type": "GET DATA VIA DIRECT ACCESS",
                         "URL": "s3://bucket/" + ur + ".h5"},
                        {"Type": "GET RELATED VISUALIZATION", "URL": browse + "_LATLON.png"},
                        {"Type": "GET RELATED VISUALIZATION",
                         "URL": browse + "_LATLON_thumbnail.png"},
                        {"Type": "GET RELATED VISUALIZATION",
                         "URL": browse + "_NATIVE_A_HHHH.png"},
                        {"Type": "EXTENDED METADATA", "URL": base + "_LATLON.kml"}]}}


print("=== the date gate refuses only the impossible ===")
check("a 2023 event (window ends 2023-10) is not searched",
      not ni.applicable(dt.datetime(2023, 9, 13), 30, 30))
check("an event 10 days before first science IS searched when post reaches past it",
      ni.applicable(ni.FIRST_SCIENCE - dt.timedelta(days=10), 30, 30))
check("an event inside the reprocessing hole is searched (archive is backfilling)",
      ni.applicable(dt.datetime(2026, 3, 15), 30, 30))
check("the Iliamna event is searched", ni.applicable(dt.datetime(2026, 8, 8, 11, 37), 30, 30))

print("\n=== an empty side says why, and what to do ===")
now = dt.datetime(2026, 10, 19)
why = ni.explain_absence(dt.datetime(2025, 8, 1), dt.datetime(2025, 9, 1), now)
check("pre-mission -> 'cannot help'", why and "cannot help" in why)
why = ni.explain_absence(dt.datetime(2026, 7, 29), dt.datetime(2026, 8, 8), now)
check("inside the instrument gap -> bracket advice with both dates",
      why and "instrument gap" in why and "2026-07-27" in why and "2026-08-10" in why)
why = ni.explain_absence(dt.datetime(2026, 2, 13), dt.datetime(2026, 3, 15), now)
check("mostly inside the reprocessing hole -> 'search again later'",
      why and "reprocessed" in why)
why = ni.explain_absence(dt.datetime(2026, 10, 18), dt.datetime(2026, 11, 17), now)
check("a window starting yesterday -> archive latency", why and "36-72 h" in why)
check("an ordinary covered window -> None (the AOI just wasn't imaged)",
      ni.explain_absence(dt.datetime(2026, 8, 20), dt.datetime(2026, 9, 10), now) is None)

print("\n=== granule names decode to what pairing keys on ===")
m = ni.parse_name("NISAR_L2_PR_GCOV_027_150_A_032_4005_DHDH_A_20260811T142220_"
                  "20260811T142259_P05023_N_F_J_001")
check("track 150", m and m["track"] == 150)
check("ascending", m and m["orbit_state"] == "ascending")
check("frame 32, mode 4005", m and m["frame"] == 32 and m["mode"] == "4005")
check("DH -> HH + HV", m and m["polarizations"] == ["HH", "HV"])
check("start time parsed", m and m["start"] == dt.datetime(2026, 8, 11, 14, 22, 20))
m = ni.parse_name("NISAR_L2_PR_GCOV_003_015_D_058_2005_SVNA_A_20260721T055200_"
                  "20260721T055239_P05023_N_F_J_001")
check("descending, single VV", m and m["orbit_state"] == "descending"
      and m["polarizations"] == ["VV"])
check("a non-GCOV name is rejected", ni.parse_name("S1A_IW_GRDH_1SDV_2026") is None)

print("\n=== candidate rows ===")
ev = dt.datetime(2026, 8, 8, 11, 37)
c = ni._candidate(granule(150, "A", 32, "4005", dt.datetime(2026, 8, 11, 14, 22, 20)), ev)
check("source is NISAR (the tab's radar key)", c["source"] == "NISAR")
check("gap 3 days", c["gap_days"] == 3)
check("h5 is the HTTPS one, not s3://", c["h5_url"].startswith("https://")
      and c["h5_url"].endswith(".h5"))
check("thumb_url is the 100 px thumbnail", c["thumb_url"].endswith("_LATLON_thumbnail.png"))
check("browse_url is the north-up browse", c["browse_url"].endswith("_LATLON.png"))
check("footprint is a closed GeoJSON polygon",
      c["geometry"]["type"] == "Polygon"
      and c["geometry"]["coordinates"][0][0] == c["geometry"]["coordinates"][0][-1])
check("bbox from the footprint", c["bbox"] == [-155.0, 59.0, -152.0, 62.0])
check("product from the collection name", c["product"] == "provisional")
check("id has no .h5 suffix", not c["id"].endswith(".h5"))
bad = granule(150, "A", 32, "4005", dt.datetime(2026, 8, 11))
bad["umm"]["RelatedUrls"] = [u for u in bad["umm"]["RelatedUrls"]
                             if not u["URL"].endswith(".h5")]
check("no HTTPS .h5 -> not a usable candidate", ni._candidate(bad, ev) is None)

print("\n=== one row per pass ===")
lat, lon = 60.0, -153.0
day = dt.datetime(2026, 8, 11, 14, 22, 20)
cands = [ni._candidate(g, ev) for g in [
    granule(150, "A", 31, "4005", day, lon0=-150.0, lat0=62.0),   # misses the point
    granule(150, "A", 32, "2005", day),                            # covers, 20 MHz
    granule(150, "A", 32, "4005", day),                            # covers, 40 MHz
    granule(78, "A", 32, "4005", dt.datetime(2026, 8, 18, 14, 30)),
]]
kept = ni._one_frame_per_pass(cands, lat, lon)
t150 = [k for k in kept if k["relative_orbit"] == 150]
check("three t150 granules on one day -> one row", len(t150) == 1)
check("  the one over the event, in the finer 4005 mode",
      t150 and t150[0]["mode"] == "4005" and t150[0]["frame"] == 32)
check("another track's pass survives", any(k["relative_orbit"] == 78 for k in kept))

print("\n=== search_event, CMR stubbed ===")
calls = []
ni.collections = lambda: [("C-PROV", "NISAR_L2_GCOV_PROVISIONAL_V1"),
                          ("C-BETA", "NISAR_L2_GCOV_BETA_V1")]


def fake_granules(colls, bbox, start, end):
    calls.append((colls, start, end))
    return [granule(78, "A", 32, "4005", dt.datetime(2026, 7, 25, 14, 30)),
            granule(150, "A", 32, "4005", dt.datetime(2026, 7, 18, 14, 22)),
            granule(150, "A", 32, "4005", dt.datetime(2026, 8, 11, 14, 22)),
            granule(6, "A", 32, "4005", dt.datetime(2026, 8, 13, 14, 39))]


ni._granules = fake_granules
r = ni.search_event(60.0272, -153.061, 8, ev, pre_days=30, post_days=30)
check("both collections queried (beta and provisional are disjoint in time)",
      calls and len(calls[0][0]) == 2)
check("split at the event: 2 before, 2 after", len(r["pre"]) == 2 and len(r["post"]) == 2)
check("nearest first on each side", r["pre"][0]["date"].startswith("2026-07-25")
      and r["post"][0]["date"].startswith("2026-08-11"))
check("no notes when both sides have scenes", r["notes"] == [])
calls.clear()
r = ni.search_event(60.0, -153.0, 8, dt.datetime(2023, 9, 13), 30, 30)
check("a pre-mission event never reaches CMR", not calls)
check("  and says why", r["notes"] and "first science" in r["notes"][0])

ni._granules = lambda *a: [granule(150, "A", 32, "4005", dt.datetime(2026, 8, 11),
                                   collection="C-PROV"),
                           granule(150, "A", 32, "4005", dt.datetime(2026, 7, 18),
                                   collection="C-BETA")]
r = ni.search_event(60.0272, -153.061, 8, ev, 30, 30)
check("a beta + provisional mix is flagged (calibration differs)",
      any("calibration" in n for n in r["notes"]))

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
