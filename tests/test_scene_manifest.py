#!/usr/bin/env python3
"""The 3D viewer's metadata CSV: the figure's scenes plus every SAR scene, as
small per-mission tables someone outside the project can fetch the scenes from.

One CSV, <event_id>_imagery.csv, with six columns (event_id, side,
acquired_utc, scene_id, archive, dataset), rows grouped by mission; how each
source was searched goes once per source into imagery_details.txt. Ethan cut it
to this on 2026-11-18 from per-mission tables of 26-28 columns.

scene_manifest reads what each imagery tab leaves in the project folder, not
the tables on screen (empty again after a reopen). These checks pin the ways a
row could go wrong without anyone noticing in the CSV itself:

  * the wrong scenes -- only the figure's optical / Planet / Maxar scenes, but
    every downloaded SAR scene; a search candidate never downloaded must not
    appear, and a downloaded one whose search entry is gone must not vanish;
  * an empty cell -- a value the project never recorded reads "n/a";
  * the wrong identity -- the right archive + dataset, the Maxar catalog ID
    (not GEGD's UUID), the loaded Detection's event id on every row whatever a
    file was named under;
  * the wrong match -- a NISAR and a Sentinel-1 file on the same track number
    and date must each find their own radar's scene, and of two frames of one
    pass, the one that covers the AOI.

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
NA = M.NA


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


def ids(sections, key):
    return [r["scene_id"] for r in sections.get(key, [])]


def cells(r):
    return tuple(r[c] for c in M.COLUMNS)


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

# ---------------------------------------------------------------- the tables
check("six columns, the same in every table",
      M.COLUMNS == ("event_id", "side", "acquired_utc", "scene_id", "archive", "dataset"))
check("one table per mission, SAR split by radar",
      [k for k, _t in M.SECTIONS] == ["sentinel2", "landsat", "planetscope", "maxar",
                                      "sar_sentinel1", "sar_nisar"])

with tempfile.TemporaryDirectory() as proj:
    ev = "AK2099-0101"                  # the loaded Detection's id
    old = "event_990102_0000"           # what some files were written under
    params = {"radius_km": R, "pre_days": 30, "post_days": 60, "max_cloud": 50.0}
    # ------------------------------------------------ Sentinel-2 / Landsat
    S2_PRE = "S2A_MSIL2A_20990101T213529_R086_T05VPG_20990101T233244"
    S2_GONE = "S2B_MSIL2A_20990120T213531_R086_T05VPG_20990121T003818"
    LS_POST = "LC09_L2SP_062018_20990120_02_T1"
    pkg = os.path.join(proj, "qgis_packages")
    pre_tif = os.path.join(pkg, f"{old}_pre_2099-01-01_hdr.tif")
    post_tif = os.path.join(pkg, f"{old}_post_2099-01-20_hdr.tif")
    ls_tif = os.path.join(pkg, f"{old}_post_2099-01-20_l_hdr.tif")
    write(os.path.join(pkg, f"{old}_metadata.json"), {
        "event_id": old, "sensor": "s2", "datetime_utc": "2099-01-02 00:00:00",
        "pre_scenes": [S2_PRE], "post_scenes": [S2_GONE],
        "pre_dates": ["2099-01-01"], "post_dates": ["2099-01-20"],
        "layers": [pre_tif, post_tif, os.path.join(pkg, f"{old}_dbright_x_vs_y.tif"),
                   os.path.join(pkg, f"{old}_point.gpkg")]})
    write(os.path.join(pkg, f"{old}_l_metadata.json"), {
        "event_id": old, "sensor": "landsat", "datetime_utc": "2099-01-02 00:00:00",
        "pre_scenes": [], "post_scenes": [LS_POST], "post_dates": ["2099-01-20"],
        "layers": [ls_tif]})
    write(os.path.join(proj, "search.json"), {
        "lat": LAT, "lon": LON, "params": params, "event_id": old,
        "datetime_utc": "2099-01-02T00:00:00",
        "pre": [{"id": S2_PRE, "date": "2099-01-01T21:35:29.024", "gap_days": 1,
                 "aoi_cloud_pct": 3.4, "aoi_snow_pct": 80.0, "aoi_cloud_method": "ocm",
                 "cloud_pct": 40.0, "source": "Sentinel-2", "geometry": full,
                 "bbox": [-141.5, 59.5, -138.5, 60.5]},
                {"id": "S2A_NEVER_DOWNLOADED", "date": "2099-01-02T21:35:29",
                 "gap_days": 0, "source": "Sentinel-2", "geometry": full}],
        "post": [{"id": LS_POST, "date": "2099-01-20T21:40:00", "gap_days": 18,
                  "aoi_cloud_pct": None, "cloud_pct": 44.6, "source": "Landsat",
                  "geometry": half}]})

    # ------------------------------------------------ PlanetScope
    pdir = os.path.join(proj, "planet")
    p_single = write(os.path.join(pdir, f"{old}_pre_20981225_211844_55_2534_planet_hdr.tif"), "x")
    p_retone = write(os.path.join(pdir, f"{old}_pre_20981225_211844_55_2534_planet_knee.tif"), "x")
    p_multi = write(os.path.join(pdir, f"{old}_post_20990105_2scenes_1a2b3c4d_planet_hdr_toa.tif"), "x")
    p_lost = write(os.path.join(pdir, f"{old}_post_20990110_3scenes_deadbeef_planet_hdr.tif"), "x")
    write(os.path.join(pdir, f"{old}_dbright_a_vs_b.tif"), "x")      # a change product, not a scene
    write(os.path.join(pdir, "render.json"), {
        "pre": p_single, "post": p_multi,
        "scenes": {"pre": ["20981225_211844_55_2534"],
                   "post": ["20990105_200000_10_aaaa", "20990105_200001_10_bbbb"]}})
    write(os.path.join(pdir, "search.json"), {
        "lat": LAT, "lon": LON, "params": dict(params, max_cloud=100.0),
        "datetime_utc": "2099-01-02T00:00:00",
        "pre": [{"id": "20981225_211844_55_2534", "date": "2098-12-25T21:18:44.55",
                 "gap_days": 7, "cloud_pct": 20.0, "source": "PlanetScope",
                 "geometry": str(half)}],
        "post": [{"id": "20990105_200000_10_aaaa", "date": "2099-01-05T20:00:00",
                  "gap_days": 4, "cloud_pct": 1.0, "source": "PlanetScope", "geometry": full}]})

    # ------------------------------------------------ SAR
    S1_EDGE = "S1A_IW_GRDH_1SDV_20981228T030000_20981228T030025_012345_00ABCD_rtc"
    S1_FULL = "S1A_IW_GRDH_1SDV_20981228T030020_20981228T030045_012345_00ABCE_rtc"
    NI = "NISAR_L2_PR_GCOV_030_130_D_057_4005_DHDH_A_20981228T050257_20981228T050336_P05023_N_F_J_001"
    sdir = os.path.join(proj, "sar")
    s1 = write(os.path.join(sdir, "amplitude", "S1_2098-12-28_desc_t130_VV_20m.tif"), "x")
    ni = write(os.path.join(sdir, "amplitude", "NISAR_2098-12-28_desc_t130_HH_20m.tif"), "x")
    s1x = write(os.path.join(sdir, "amplitude", "S1_2099-01-09_asce_t64_VV_20m.tif"), "x")
    write(os.path.join(sdir, "change", "S1_change_log-ratio_x.tif"), "x")
    write(os.path.join(sdir, "search.json"), {
        "lat": LAT, "lon": LON, "params": dict(params, max_cloud=None), "event_id": old,
        "datetime_utc": "2099-01-02T00:00:00",
        "pre": [{"id": S1_EDGE, "date": "2098-12-28T03:00:00", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130,
                 "polarizations": ["VV", "VH"], "source": "Sentinel-1", "geometry": half},
                {"id": S1_FULL, "date": "2098-12-28T03:00:20", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130,
                 "polarizations": ["VV", "VH"], "source": "Sentinel-1", "geometry": full},
                {"id": NI, "date": "2098-12-28T05:02:57", "gap_days": 5,
                 "orbit_state": "descending", "relative_orbit": 130, "frame": 57, "cycle": 30,
                 "mode": "4005", "polarizations": ["HH", "HV"], "band": "L",
                 "product": "provisional", "collection": "NISAR_L2_GCOV_PROVISIONAL_V1",
                 "source": "NISAR", "geometry": full}],
        "post": []})

    # ------------------------------------------------ Maxar (cached search)
    FID_FIG, FID_OTHER, FID_LOST = "f1g-uuid", "0th-uuid", "lost-uuid"

    def feature(fid, acq, cat):
        return {"type": "Feature", "id": fid, "geometry": half, "properties": {
            "featureId": fid, "legacyIdentifier": cat, "acquisitionDate": acq,
            "source": "WV03_VNIR", "offNadirAngle": 21.04, "sunElevation": 12.3,
            "sunAzimuth": 171.4, "groundSampleDistance": 0.312, "processingLevel": "LV3D",
            "productName": "DAILY_TAKE", "cloudCover": 0.007}}
    write(os.path.join(proj, "gegd", old, "search.json"), {
        "format": "landslide-gegd-search", "version": 1, "searched_utc": "2099-01-30T00:00:00Z",
        "event_id": old, "lat": LAT, "lon": LON, "radius_km": R,
        "origin_utc": "2099-01-02T00:00:00.000000Z",
        "t0": "2098-01-02T00:00:00.000000Z", "t1": "2099-04-02T00:00:00.000000Z",
        "features": [feature(FID_FIG, "2099-01-04T12:00:00.000Z", "10400100ABCDEF00"),
                     feature(FID_OTHER, "2098-12-01T12:00:00.000Z", "10400100ABCDEF01")]})
    streams = [
        {"fid": FID_FIG, "side": "post", "in_figure": True},
        {"fid": FID_OTHER, "side": "pre", "in_figure": False},
        {"fid": FID_LOST, "side": "pre", "in_figure": True,
         "props": {"acq_utc": "2098-12-20T12:00:00Z", "sensor": "WV02", "ona": 9.87,
                   "level": "LV2A", "catalog_id": "10300100LOST000"}}]

    figure = {"Before": [pre_tif], "After": [p_multi]}
    secs = M.collect(proj, ev, viewer=figure, maxar=streams)
    every = M.collect(proj, ev, maxar=streams)

    # ---- the right scenes
    check("the figure's Sentinel-2 scene and nothing else optical",
          ids(secs, "sentinel2") == [S2_PRE] and "landsat" not in secs, ids(secs, "sentinel2"))
    check("the figure's multi-scene Planet render lists each scene render.json names",
          sorted(ids(secs, "planetscope"))
          == ["20990105_200000_10_aaaa", "20990105_200001_10_bbbb"], ids(secs, "planetscope"))
    check("every downloaded SAR scene, though none is in the figure",
          len(secs.get("sar_sentinel1", [])) == 2 and len(secs.get("sar_nisar", [])) == 1)
    check("Maxar: the figure's strips only (one with a record, one without)",
          sorted(ids(secs, "maxar")) == ["10300100LOST000", "10400100ABCDEF00"],
          ids(secs, "maxar"))
    check("no figure given -> every downloaded scene",
          sorted(ids(every, "sentinel2")) == sorted([S2_PRE, S2_GONE])
          and ids(every, "landsat") == [LS_POST] and len(every["planetscope"]) == 4
          and len(every["maxar"]) == 3, {k: len(v) for k, v in every.items()})
    check("a search candidate that was never downloaded is not a row",
          "S2A_NEVER_DOWNLOADED" not in ids(every, "sentinel2"))
    check("a re-toned Planet render is the same scene, not a second row",
          ids(every, "planetscope").count("20981225_211844_55_2534") == 1)
    check("a hashed Planet render render.json no longer knows keeps its tag",
          "20990110_3scenes_deadbeef" in ids(every, "planetscope"))
    check("tables come in SECTIONS order, empty ones left out",
          list(every) == [k for k, _t in M.SECTIONS if k in every] and "landsat" not in secs)

    # ---- no empty cell; the six columns plus the search line, nothing else
    for name, ss in (("figure", secs), ("everything", every)):
        bad = [(k, c) for k, rows in ss.items() for r in rows for c, v in r.items()
               if v in ("", None)]
        check(f"no empty cell anywhere ({name})", not bad, bad[:5])
        check(f"each row is the six columns plus its search line ({name})",
              all(set(r) == set(M.COLUMNS) | {"_search"} for rows in ss.values() for r in rows))
    check("every row has the loaded Detection's event id, not the file's",
          all(r["event_id"] == ev for rows in every.values() for r in rows))

    # ---- the right values
    check("Sentinel-2 row",
          cells(secs["sentinel2"][0]) == (ev, "before", "2099-01-01T21:35:29Z", S2_PRE,
                                          M.ARCHIVE_MPC, "sentinel-2-l2a"),
          secs["sentinel2"][0])
    gone_row = next(r for r in every["sentinel2"] if r["scene_id"] == S2_GONE)
    check("a downloaded scene whose search entry is gone keeps the Run's date",
          cells(gone_row) == (ev, "after", "2099-01-20", S2_GONE, M.ARCHIVE_MPC,
                              "sentinel-2-l2a"), gone_row)
    check("Landsat row",
          cells(every["landsat"][0]) == (ev, "after", "2099-01-20T21:40:00Z", LS_POST,
                                         M.ARCHIVE_MPC, "landsat-c2-l2"), every["landsat"][0])
    pa = next(r for r in secs["planetscope"] if r["scene_id"] == "20990105_200000_10_aaaa")
    pb = next(r for r in secs["planetscope"] if r["scene_id"] == "20990105_200001_10_bbbb")
    check("PlanetScope row", cells(pa) == (ev, "after", "2099-01-05T20:00:00Z",
                                           "20990105_200000_10_aaaa", M.ARCHIVE_PLANET,
                                           "PSScene"), pa)
    check("a Planet id the search no longer lists still gets its time from the id",
          pb["acquired_utc"] == "2099-01-05T20:00:01Z", pb)
    s1rows = {r["acquired_utc"][:10]: r for r in secs["sar_sentinel1"]}
    check("an S1 file matches its own radar's scene, the frame that covers the AOI",
          cells(s1rows.get("2098-12-28", {c: "" for c in M.COLUMNS}))
          == (ev, "before", "2098-12-28T03:00:20Z", S1_FULL, M.ARCHIVE_MPC, "sentinel-1-rtc"),
          s1rows.get("2098-12-28"))
    check("a NISAR file on the same track number + date matches NISAR, ASF dataset",
          cells(secs["sar_nisar"][0]) == (ev, "before", "2098-12-28T05:02:57Z", NI,
                                          M.ARCHIVE_ASF, "NISAR_L2_GCOV_PROVISIONAL_V1"),
          secs["sar_nisar"][0])
    sx = s1rows.get("2099-01-09", {})
    check("an unmatched SAR file: date from its name, side from the event date, id n/a",
          (sx.get("side"), sx.get("acquired_utc"), sx.get("scene_id"))
          == ("after", "2099-01-09", NA), sx)
    mx = {r["scene_id"]: r for r in secs["maxar"]}
    check("Maxar from the cached search: the catalog ID, not GEGD's UUID",
          cells(mx.get("10400100ABCDEF00", {c: "" for c in M.COLUMNS}))
          == (ev, "after", "2099-01-04T12:00:00Z", "10400100ABCDEF00", M.ARCHIVE_GEGD,
              "Maxar:FinishedFeature"), mx)
    check("Maxar strip with no cached record: the layer's catalog ID and time",
          (mx.get("10300100LOST000") or {}).get("acquired_utc") == "2098-12-20T12:00:00Z")
    check("rows sort before -> after inside a table",
          [r["side"] for r in every["sar_sentinel1"]] == ["before", "after"]
          and [r["side"] for r in every["maxar"]][-1] == "after")

    # ---- how each source was searched
    line = secs["sentinel2"][0]["_search"]
    check("optical search line: radius, centre, window from the event time, cloud limit",
          line == "within 10 km of 60.00000, -140.00000, 2098-12-03 to 2099-03-03 (UTC), "
                  "scenes up to 50% cloud", line)
    check("Planet at 100% says no cloud limit",
          pa["_search"].endswith("no cloud limit"), pa["_search"])
    check("SAR search line has no cloud limit at all",
          secs["sar_nisar"][0]["_search"]
          == "within 10 km of 60.00000, -140.00000, 2098-12-03 to 2099-03-03 (UTC)",
          secs["sar_nisar"][0]["_search"])
    check("Maxar search line uses the cached search's own window",
          mx["10400100ABCDEF00"]["_search"]
          == "within 10 km of 60.00000, -140.00000, 2098-01-02 to 2099-04-02 (UTC)",
          mx["10400100ABCDEF00"]["_search"])

    # ---- another event's run in the same folder is left out
    write(os.path.join(pkg, "AK2099-9999_metadata.json"), {
        "event_id": "AK2099-9999", "sensor": "s2",
        "pre_scenes": ["S2A_MSIL2A_20990101T213529_R086_T05VPH_20990101T233244"],
        "post_scenes": []})
    write(os.path.join(pkg, f"{old}_metadata.json"), dict(json.load(open(
        os.path.join(pkg, f"{old}_metadata.json"))), event_id=ev))
    check("another event's run is not listed",
          not any("T05VPH" in s for s in ids(M.collect(proj, ev), "sentinel2")))
    check("...unless no run matches the event at all",
          any("T05VPH" in s for s in ids(M.collect(proj, "AK2099-0000"), "sentinel2")))

    # ---- one CSV, grouped by mission
    titles = [t for k, t in M.SECTIONS if k in secs]
    text = M.to_csv(secs)
    back = list(csv.DictReader(io.StringIO(text)))
    flat = [r for k, _t in M.SECTIONS for r in secs.get(k, [])]
    check("one CSV: the six columns, every row of every mission, grouped in SECTIONS order",
          tuple(back[0]) == M.COLUMNS and len(back) == len(flat)
          and [tuple(b.values()) for b in back] == [cells(r) for r in flat])
    colls = [b["dataset"] for b in back]
    check("SAR rows come last, together",
          colls[-3:] == ["sentinel-1-rtc", "sentinel-1-rtc", "NISAR_L2_GCOV_PROVISIONAL_V1"],
          colls)
    check("no blank cell and no search line in the CSV",
          ",," not in text and not re.search(r",\r\n", text) and "within" not in text)
    lines = M.search_lines(secs)
    check("search lines: once per source, titled",
          lines[0] == f"Sentinel-2: {line}" and sum(l.startswith("Sentinel-2:") for l in lines) == 1
          and any(l.startswith("SAR - NISAR: within") for l in lines), lines)
    check("the Maxar search line says scene_id is the catalog ID",
          any(l.startswith("Maxar / WorldView: within") and "Maxar catalog ID" in l
              for l in lines), lines)

    # ---- what the viewer embeds
    pl = M.payload(secs, "AK2099-0101")
    check("payload: <event_id>_imagery.csv, the CSV, the count, the missions, the searches",
          pl["name"] == "AK2099-0101_imagery.csv" and pl["csv"] == text
          and pl["n"] == len(flat) and pl["tables"] == titles and pl["searches"] == lines)
    check("an event id unfit for a file name is made safe",
          M.payload(secs, "AK 2099/0101")["name"] == "AK_2099_0101_imagery.csv"
          and M.payload(secs, "")["name"] == "event_imagery.csv")

    # ---- one tab's odd file never costs the others
    write(os.path.join(sdir, "search.json"), "{not json")
    rows3 = M.collect(proj, ev)
    check("an unreadable SAR search still lists the optical scenes and the SAR files",
          "sentinel2" in rows3 and len(rows3.get("sar_sentinel1", [])) == 2
          and len(rows3.get("sar_nisar", [])) == 1)

check("an empty / missing folder is no tables, not an error",
      M.collect("") == {} and M.collect("/nonexistent/folder/x") == {})

# ---------------------------------------------------------------- the page
evil = M._row("sentinel2", NA, scene_id="</script><b>x")
evil.pop("_section")
scene_cfg = M.payload({"sentinel2": [evil]}, "AK2099-0101")
base = {"title": "t", "ncols": 2, "nrows": 2, "width_m": 1, "height_m": 1,
        "zmin": 0, "zmax": 1, "exaggeration": 1, "elev_b64": "",
        "before_uri": "", "after_uri": "", "event_id": "AK2099-0101",
        "scene_csv": scene_cfg}
html = W.build_viewer_html(base)
check("the page has the CSV button", 'id="csvBtn"' in html)
m = re.search(r'<script id="cfg" type="application/json">(.*?)</script>', html, re.S)
cfg = json.loads(m.group(1)) if m else {}
check("a '</script>' inside a scene id cannot end the payload early, and round-trips",
      (cfg.get("scene_csv") or {}).get("csv") == scene_cfg["csv"])
check("the event id rides along for imagery_details.txt", cfg.get("event_id") == "AK2099-0101")
check("the CSV button saves the one CSV, not a ZIP",
      '"\\ufeff" + S.csv' in html and "zipStore(S.files" not in html)
check("both figure ZIPs get imagery_details.txt from the Details rows",
      'name:"imagery_details.txt"' in html and "function detailsText()" in html
      and "figureDetailRows({})" in html)
check("imagery_details.txt ends with how each source was searched",
      "How each source was searched:" in html and "CFG.scene_csv.searches" in html)

node = shutil.which("node")
if node:
    js = html.split('<script>\n', 1)[1].rsplit("</script>", 1)[0]
    with tempfile.TemporaryDirectory() as d:
        p = write(os.path.join(d, "viewer.js"), js)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
    check("the viewer script still parses", r.returncode == 0, r.stderr[-400:])

print(("FAILED: " + ", ".join(fails)) if fails else "all scene_manifest checks passed")
sys.exit(1 if fails else 0)
