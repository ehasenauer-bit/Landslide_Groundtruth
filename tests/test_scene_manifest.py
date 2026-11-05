#!/usr/bin/env python3
"""The 3D viewer's metadata CSV must list exactly the scenes the project downloaded.

scene_manifest reads what each imagery tab leaves in the project folder, not
the tables on screen (empty again after a reopen). These checks pin the three
ways a row could go wrong without anyone noticing in the CSV itself:

  * the wrong scenes -- a search candidate that was never downloaded must not
    appear, and a downloaded one whose search entry is gone must not vanish;
  * the wrong numbers -- Cloud / Coverage must read as the tab's table does
    (the "~" whole-scene fallback, the snow mark, % of the AOI box);
  * the wrong match -- a NISAR and a Sentinel-1 file on the same track number
    and date must each find their own radar's scene.

Synthetic fixtures only (the repo is public).
"""
import csv
import io
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from landslide_groundtruth import scene_manifest as M                # noqa: E402
from landslide_groundtruth import web3d_export as W                  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        fails.append(name)


def box_poly(minx, miny, maxx, maxy):
    return {"type": "Polygon", "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy],
                                                [minx, maxy], [minx, miny]]]}


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, (dict, list)):
            json.dump(obj, f)
        else:
            f.write(obj)
    return path


LAT, LON, R = 60.0, -140.0, 10.0
BOX = M.aoi_box({"lat": LAT, "lon": LON, "params": {"radius_km": R}})
MINX, MINY, MAXX, MAXY = BOX
MIDX = (MINX + MAXX) / 2

# ---------------------------------------------------------------- coverage
full = box_poly(MINX - 1, MINY - 1, MAXX + 1, MAXY + 1)
half = box_poly(MINX - 1, MINY - 1, MIDX, MAXY + 1)
check("a footprint over the whole box covers 100%",
      abs(M.coverage_pct({"geometry": full}, BOX) - 100) < 1e-6)
check("a footprint over the west half covers 50%",
      abs(M.coverage_pct({"geometry": half}, BOX) - 50) < 1e-6)
check("a footprint stored as the str() of a dict still parses",
      abs(M.coverage_pct({"geometry": str(half)}, BOX) - 50) < 1e-6)
check("no geometry -> untestable (None), not 0",
      M.coverage_pct({}, BOX) is None)
diamond = {"type": "Polygon", "coordinates": [[[MIDX, MINY - 5], [MAXX + 5, (MINY + MAXY) / 2],
                                               [MIDX, MAXY + 5], [MINX - 5, (MINY + MAXY) / 2]]]}
check("a big tilted footprint around the box covers 100%",
      abs(M.coverage_pct({"geometry": diamond}, BOX) - 100) < 1e-6,
      M.coverage_pct({"geometry": diamond}, BOX))

with tempfile.TemporaryDirectory() as proj:
    ev = "AK2099-0101"
    params = {"radius_km": R}
    # ------------------------------------------------ Sentinel-2 / Landsat
    pkg = os.path.join(proj, "qgis_packages")
    pre_tif = os.path.join(pkg, f"{ev}_pre_2099-01-01_hdr.tif")
    post_tif = os.path.join(pkg, f"{ev}_post_2099-01-20_hdr.tif")
    write(os.path.join(pkg, f"{ev}_metadata.json"), {
        "event_id": ev, "sensor": "s2",
        "pre_scenes": ["S2A_PRE"], "post_scenes": ["S2B_POST_1", "S2B_POST_GONE"],
        "pre_dates": ["2099-01-01"], "post_dates": ["2099-01-20", "2099-01-20"],
        "layers": [pre_tif, post_tif, os.path.join(pkg, f"{ev}_dbright_x_vs_y.tif"),
                   os.path.join(pkg, f"{ev}_point.gpkg")]})
    write(os.path.join(proj, "search.json"), {
        "lat": LAT, "lon": LON, "params": params, "event_id": ev,
        "pre": [{"id": "S2A_PRE", "date": "2099-01-01T21:35:29.0", "gap_days": 9,
                 "aoi_cloud_pct": 3.4, "aoi_snow_pct": 80.0, "cloud_pct": 40.0,
                 "source": "Sentinel-2", "geometry": full},
                {"id": "S2A_NEVER_DOWNLOADED", "date": "2099-01-02T21:35:29",
                 "gap_days": 8, "source": "Sentinel-2", "geometry": full}],
        "post": [{"id": "S2B_POST_1", "date": "2099-01-20T21:40:00", "gap_days": 10,
                  "aoi_cloud_pct": None, "cloud_pct": 44.6, "source": "Landsat",
                  "geometry": half}]})

    # ------------------------------------------------ PlanetScope
    pdir = os.path.join(proj, "planet")
    p_single = write(os.path.join(pdir, f"{ev}_pre_20981225_211844_55_2534_planet_hdr.tif"), "x")
    p_retone = write(os.path.join(pdir, f"{ev}_pre_20981225_211844_55_2534_planet_knee_toa.tif"), "x")
    p_multi = write(os.path.join(pdir, f"{ev}_post_20990105_2scenes_1a2b3c4d_planet_hdr.tif"), "x")
    p_lost = write(os.path.join(pdir, f"{ev}_post_20990110_3scenes_deadbeef_planet_hdr.tif"), "x")
    write(os.path.join(pdir, f"{ev}_dbright_a_vs_b.tif"), "x")      # a change product, not a scene
    write(os.path.join(pdir, "render.json"), {
        "pre": p_single, "post": p_multi,
        "scenes": {"pre": ["20981225_211844_55_2534"],
                   "post": ["20990105_200000_10_aaaa", "20990105_200001_10_bbbb"]}})
    write(os.path.join(pdir, "search.json"), {
        "lat": LAT, "lon": LON, "params": params,
        "pre": [{"id": "20981225_211844_55_2534", "date": "2098-12-25T21:18:44.55",
                 "gap_days": 7, "cloud_pct": 20.0, "aoi_cloud_pct": None,
                 "source": "PlanetScope", "geometry": str(half)}],
        "post": [{"id": "20990105_200000_10_aaaa", "date": "2099-01-05T20:00:00",
                  "gap_days": 4, "cloud_pct": 1.0, "aoi_cloud_pct": 0.4,
                  "aoi_snow_pct": 90.0, "source": "PlanetScope", "geometry": full}]})

    # ------------------------------------------------ SAR
    sdir = os.path.join(proj, "sar")
    s1 = write(os.path.join(sdir, "amplitude", "S1_2098-12-28_desc_t130_VV_20m.tif"), "x")
    ni = write(os.path.join(sdir, "amplitude", "NISAR_2098-12-28_desc_t130_HH_20m.tif"), "x")
    s1x = write(os.path.join(sdir, "amplitude", "S1_2099-01-09_asce_t64_VV_20m.tif"), "x")
    write(os.path.join(sdir, "change", "S1_change_log-ratio_x.tif"), "x")
    write(os.path.join(sdir, "search.json"), {
        "lat": LAT, "lon": LON, "params": params, "event_id": ev,
        "datetime_utc": "2099-01-02T00:00:00",
        "pre": [{"id": "S1A_IW_GRDH_EDGE", "date": "2098-12-28T03:00:00", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130,
                 "source": "Sentinel-1", "geometry": half},
                {"id": "S1A_IW_GRDH_FULL", "date": "2098-12-28T03:00:20", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130,
                 "source": "Sentinel-1", "geometry": full},
                {"id": "NISAR_L2_GCOV_X", "date": "2098-12-28T05:02:57", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130,
                 "source": "NISAR", "geometry": full}],
        "post": []})

    rows = M.collect(proj, ev, viewer={"Before": [pre_tif], "After": [p_multi]})
    by_id = {r["Scene ID"]: r for r in rows}

    # ---- the right scenes
    check("a search candidate that was never downloaded is not a row",
          "S2A_NEVER_DOWNLOADED" not in by_id)
    check("a downloaded scene whose search entry is gone still has a row",
          "S2B_POST_GONE" in by_id and by_id["S2B_POST_GONE"]["Date (UTC)"] == "2099-01-20"
          and by_id["S2B_POST_GONE"]["Source"] == "Sentinel-2", by_id.get("S2B_POST_GONE"))
    check("change products are not scenes",
          not any("dbright" in r["File"] or "change" in r["File"] for r in rows),
          [r["File"] for r in rows])
    planet = [r for r in rows if r["Tab"] == M.TAB_PLANET]
    check("a re-toned Planet render is the same scene, not a second row",
          sum(r["Scene ID"] == "20981225_211844_55_2534" for r in planet) == 1
          and "planet_knee_toa" in by_id["20981225_211844_55_2534"]["File"],
          [r["Scene ID"] for r in planet])
    check("a multi-scene Planet render lists each scene render.json names",
          {"20990105_200000_10_aaaa", "20990105_200001_10_bbbb"} <= set(by_id))
    check("a hashed Planet render render.json no longer knows keeps its tag",
          "20990110_3scenes_deadbeef" in by_id)

    # ---- the right numbers
    pre = by_id.get("S2A_PRE", {})
    check("S2 row carries the table's columns",
          (pre.get("Side"), pre.get("Date (UTC)"), pre.get("Gap (d)"), pre.get("Source"))
          == ("pre", "2099-01-01 21:35", "9", "Sentinel-2"), pre)
    check("snowy AOI cloud gets the table's snowflake", pre.get("Cloud") == "❄ 3%", pre.get("Cloud"))
    check("whole-scene fallback cloud keeps the '~'",
          by_id["S2B_POST_1"]["Cloud"] == "~45%", by_id["S2B_POST_1"]["Cloud"])
    check("coverage is % of the AOI box",
          pre.get("Coverage (% AOI)") == "100" and by_id["S2B_POST_1"]["Coverage (% AOI)"] == "50")
    check("Planet cloud has no snowflake (its table never shows one)",
          by_id["20990105_200000_10_aaaa"]["Cloud"] == "0%",
          by_id["20990105_200000_10_aaaa"]["Cloud"])
    check("an unmatched Planet id still gets its date from the id",
          by_id["20990105_200001_10_bbbb"]["Date (UTC)"] == "2099-01-05 20:00")

    # ---- the right match
    sar = [r for r in rows if r["Tab"] == M.TAB_SAR]
    s1row = next((r for r in sar if r["File"] == os.path.basename(s1)), {})
    nirow = next((r for r in sar if r["File"] == os.path.basename(ni)), {})
    check("an S1 file matches its own radar's scene, the frame that covers the AOI",
          s1row.get("Scene ID") == "S1A_IW_GRDH_FULL" and s1row.get("Radar") == "S1 C", s1row)
    check("a NISAR file on the same track number + date matches NISAR",
          nirow.get("Scene ID") == "NISAR_L2_GCOV_X" and nirow.get("Radar") == "NISAR L", nirow)
    check("SAR row has Orbit / Track / Coverage",
          (s1row.get("Orbit"), s1row.get("Track"), s1row.get("Coverage (% AOI)"))
          == ("desc", "130", "100"), s1row)
    sx = next((r for r in sar if r["File"] == os.path.basename(s1x)), {})
    check("an unmatched SAR file keeps what its name says, side from the event date",
          (sx.get("Side"), sx.get("Date (UTC)"), sx.get("Track"), sx.get("Scene ID"))
          == ("post", "2099-01-09", "64", ""), sx)

    # ---- the 3D-viewer column
    check("the draped Before layer marks its scene",
          pre.get("In 3D viewer") == "Before", pre.get("In 3D viewer"))
    check("a draped multi-scene render marks every scene in it",
          by_id["20990105_200000_10_aaaa"]["In 3D viewer"] == "After"
          and by_id["20990105_200001_10_bbbb"]["In 3D viewer"] == "After")
    check("scenes not draped are blank", by_id["S2B_POST_1"]["In 3D viewer"] == "")

    # ---- Maxar rows come in from the plugin and sort under their tab
    rows2 = M.collect(proj, ev, extra=[{"Tab": M.TAB_MAXAR, "Side": "post",
                                        "Scene ID": "d9afe541", "Sun°": "12"}])
    tabs = [r["Tab"] for r in rows2]
    check("rows sort by the dock's tab order",
          tabs == sorted(tabs, key=M.TABS.index) and M.TAB_MAXAR in tabs, tabs)
    check("pre sorts before post inside a tab",
          [r["Side"] for r in rows2 if r["Tab"] == M.TAB_OPTICAL] == ["pre", "post", "post"])

    # ---- another event's run in the same folder is left out
    write(os.path.join(pkg, "AK2099-9999_metadata.json"), {
        "event_id": "AK2099-9999", "sensor": "s2", "pre_scenes": ["OTHER"], "post_scenes": []})
    check("another event's run is not listed",
          "OTHER" not in {r["Scene ID"] for r in M.collect(proj, ev)})
    check("...unless no run matches the event at all",
          "OTHER" in {r["Scene ID"] for r in M.collect(proj, "AK2099-0000")})

    # ---- CSV text
    text = M.to_csv(rows)
    back = list(csv.reader(io.StringIO(text)))
    check("CSV header is COLUMNS, one line per row",
          tuple(back[0]) == M.COLUMNS and len(back) == len(rows) + 1)
    check("every heading the tabs' tables show is a column",
          {"Side", "Date (UTC)", "Gap (d)", "Cloud", "Source", "Radar", "Orbit", "Track",
           "Sensor", "Off-nadir°", "Sun°", "Strip cloud %", "Scene ID"} <= set(M.COLUMNS))

    # ---- one tab's odd file never costs the others
    write(os.path.join(sdir, "search.json"), "{not json")
    rows3 = M.collect(proj, ev)
    check("an unreadable SAR search still lists the optical scenes and the SAR files",
          any(r["Tab"] == M.TAB_OPTICAL for r in rows3)
          and sum(r["Tab"] == M.TAB_SAR for r in rows3) == 3)

check("an empty / missing folder is no rows, not an error",
      M.collect("") == [] and M.collect("/nonexistent/folder/x") == [])

# ---------------------------------------------------------------- the page
csv_text = M.to_csv([M._row(Tab=M.TAB_OPTICAL, Side="pre", **{"Scene ID": "</script><b>x"})])
html = W.build_viewer_html({"title": "t", "ncols": 2, "nrows": 2, "width_m": 1, "height_m": 1,
                            "zmin": 0, "zmax": 1, "exaggeration": 1, "elev_b64": "",
                            "before_uri": "", "after_uri": "",
                            "scene_csv": {"name": "AK_scenes.csv", "csv": csv_text, "n": 1}})
check("the page has the CSV button", 'id="csvBtn"' in html)
m = re.search(r'<script id="cfg" type="application/json">(.*?)</script>', html, re.S)
cfg = json.loads(m.group(1)) if m else {}
check("a '</script>' inside a scene id cannot end the payload early, and round-trips",
      (cfg.get("scene_csv") or {}).get("csv") == csv_text)

node = shutil.which("node")
if node:
    js = html.split('<script>\n', 1)[1].rsplit("</script>", 1)[0]
    with tempfile.TemporaryDirectory() as d:
        p = write(os.path.join(d, "viewer.js"), js)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
    check("the viewer script still parses", r.returncode == 0, r.stderr[-400:])

print(("FAILED: " + ", ".join(fails)) if fails else "all scene_manifest checks passed")
sys.exit(1 if fails else 0)
