#!/usr/bin/env python3
"""The 3D viewer's metadata CSV: the figure's scenes plus every SAR scene, as
per-mission tables someone outside the project can search with.

scene_manifest reads what each imagery tab leaves in the project folder, not
the tables on screen (empty again after a reopen). These checks pin the ways a
row could go wrong without anyone noticing in the CSV itself:

  * the wrong scenes -- only the figure's optical / Planet / Maxar scenes, but
    every downloaded SAR scene; a search candidate never downloaded must not
    appear, and a downloaded one whose search entry is gone must not vanish;
  * an empty cell -- every column of a table applies to every row of it, and a
    value that applies but was not recorded reads "n/a";
  * the wrong identity -- tile / orbit / frame parsed from the right part of
    each mission's id, the right archive + collection, the loaded Detection's
    event id on every row whatever a file was named under;
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
hdr = {key: cols for key, _t, cols in M.SECTIONS}
check("every table starts with event_id, side, acquired_utc, days_from_event",
      all(cols[:4] == ("event_id", "side", "acquired_utc", "days_from_event")
          for cols in hdr.values()))
check("every table says where the scene lives and what it is",
      all({"scene_id", "archive", "collection", "footprint_west", "footprint_north",
           "search_center_lat", "search_radius_km", "search_start_utc"} <= set(cols)
          for cols in hdr.values()))
only = {"off_nadir_deg": {"maxar"}, "sun_elevation_deg": {"maxar"}, "catalog_id": {"maxar"},
        "mgrs_tile": {"sentinel2"}, "wrs_path": {"landsat"}, "satellite_id": {"planetscope"},
        "absolute_orbit": {"sar_sentinel1"}, "frame": {"sar_nisar"},
        "polarization_used": {"sar_sentinel1", "sar_nisar"},
        "search_max_cloud_pct": {"sentinel2", "landsat", "planetscope"}}
for col, where in only.items():
    check(f"{col} is a column of {sorted(where)} only",
          {k for k, cols in hdr.items() if col in cols} == where)
gone = {"Event", "Tab", "In 3D viewer", "File", "Side", "Scene ID"}
check("no machine-only or old flat-table headings",
      not any(gone & set(cols) for cols in hdr.values()))
check("no duplicate column inside a table", all(len(c) == len(set(c)) for c in hdr.values()))

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
          sorted(ids(secs, "maxar")) == sorted([FID_FIG, FID_LOST]), ids(secs, "maxar"))
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
          list(every) == [k for k, _t, _c in M.SECTIONS if k in every]
          and "landsat" not in secs)

    # ---- no empty cell, and each row has exactly its table's columns
    for name, ss in (("figure", secs), ("everything", every)):
        bad = [(k, c) for k, rows in ss.items() for r in rows for c, v in r.items()
               if v in ("", None)]
        check(f"no empty cell anywhere ({name})", not bad, bad[:5])
        check(f"each row carries exactly its table's columns ({name})",
              all(tuple(r) == hdr[k] for k, rows in ss.items() for r in rows))
    check("every row has the loaded Detection's event id, not the file's",
          all(r["event_id"] == ev for rows in every.values() for r in rows))

    # ---- the right numbers + identity
    pre = secs["sentinel2"][0]
    check("Sentinel-2: mission, level, MGRS tile, relative orbit from the id",
          (pre["mission"], pre["product_level"], pre["mgrs_tile"], pre["relative_orbit"])
          == ("Sentinel-2A", "L2A", "05VPG", "86"), pre)
    check("Sentinel-2: AOI cloud / snow / method and the archive's scene cloud",
          (pre["aoi_cloud_pct"], pre["aoi_snow_pct"], pre["aoi_cloud_method"],
           pre["scene_cloud_pct"]) == ("3.4", "80.0", "ocm", "40.0"), pre)
    check("Sentinel-2: before, ISO time, days, coverage",
          (pre["side"], pre["acquired_utc"], pre["days_from_event"], pre["aoi_coverage_pct"])
          == ("before", "2099-01-01T21:35:29Z", "1", "100"), pre)
    check("Sentinel-2: archive + collection",
          (pre["archive"], pre["collection"]) == (M.ARCHIVE_MPC, "sentinel-2-l2a"))
    check("footprint is the stored bbox",
          (pre["footprint_west"], pre["footprint_south"], pre["footprint_east"],
           pre["footprint_north"]) == ("-141.50000", "59.50000", "-138.50000", "60.50000"), pre)
    check("search settings: centre, radius, window from the event time, cloud limit",
          (pre["search_center_lat"], pre["search_radius_km"], pre["search_start_utc"],
           pre["search_end_utc"], pre["search_max_cloud_pct"])
          == ("60.00000", "10.0", "2098-12-03T00:00:00Z", "2099-03-03T00:00:00Z", "50"), pre)
    gone_row = next(r for r in every["sentinel2"] if r["scene_id"] == S2_GONE)
    check("a downloaded scene whose search entry is gone: date from the run, id parsed, "
          "the rest n/a",
          (gone_row["acquired_utc"], gone_row["side"], gone_row["mgrs_tile"],
           gone_row["scene_cloud_pct"], gone_row["footprint_west"], gone_row["days_from_event"])
          == ("2099-01-20", "after", "05VPG", NA, NA, "18"), gone_row)
    ls = every["landsat"][0]
    check("Landsat: mission, WRS path/row, category from the id",
          (ls["mission"], ls["product_level"], ls["wrs_path"], ls["wrs_row"],
           ls["collection_category"]) == ("Landsat 9", "L2SP", "062", "018", "T1"), ls)
    check("Landsat: no AOI cloud -> n/a, not blank; scene cloud and coverage kept",
          (ls["aoi_cloud_pct"], ls["aoi_snow_pct"], ls["aoi_cloud_method"],
           ls["scene_cloud_pct"], ls["aoi_coverage_pct"], ls["collection"])
          == (NA, NA, NA, "44.6", "50", "landsat-c2-l2"), ls)
    pa = next(r for r in secs["planetscope"] if r["scene_id"] == "20990105_200000_10_aaaa")
    pb = next(r for r in secs["planetscope"] if r["scene_id"] == "20990105_200001_10_bbbb")
    check("Planet: satellite id from the id, TOA render -> analytic_udm2, PSScene",
          (pa["satellite_id"], pa["product_bundle"], pa["collection"], pa["archive"],
           pa["scene_cloud_pct"], pa["search_max_cloud_pct"])
          == ("aaaa", "analytic_udm2", "PSScene", M.ARCHIVE_PLANET, "1.0", "100"), pa)
    check("Planet: an id the search no longer lists still gets its time from the id",
          pb["acquired_utc"] == "2099-01-05T20:00:01Z" and pb["scene_cloud_pct"] == NA, pb)
    psr = next(r for r in every["planetscope"] if r["scene_id"] == "20981225_211844_55_2534")
    check("Planet: an SR render -> analytic_sr_udm2", psr["product_bundle"] == "analytic_sr_udm2")

    s1rows = {r["acquired_utc"][:10]: r for r in secs["sar_sentinel1"]}
    s1row = s1rows.get("2098-12-28", {})
    check("an S1 file matches its own radar's scene, the frame that covers the AOI",
          s1row.get("scene_id") == S1_FULL, s1row)
    check("Sentinel-1: mission, mode, absolute orbit, datatake, ESA granule from the id",
          (s1row.get("mission"), s1row.get("acquisition_mode"), s1row.get("absolute_orbit"),
           s1row.get("datatake_id"), s1row.get("esa_granule"))
          == ("Sentinel-1A", "IW", "12345", "00ABCE", S1_FULL[:-4]), s1row)
    check("Sentinel-1: band, polarizations, direction, track, MPC collection",
          (s1row.get("band"), s1row.get("polarization_used"),
           s1row.get("polarizations_available"), s1row.get("orbit_direction"),
           s1row.get("relative_orbit"), s1row.get("collection"))
          == ("C", "VV", "VV+VH", "descending", "130", "sentinel-1-rtc"), s1row)
    nirow = secs["sar_nisar"][0]
    check("a NISAR file on the same track number + date matches NISAR",
          nirow["scene_id"] == NI, nirow)
    check("NISAR: band, product, mode, frame, cycle, polarizations, ASF collection",
          (nirow["band"], nirow["product"], nirow["acquisition_mode"], nirow["frame"],
           nirow["cycle"], nirow["polarization_used"], nirow["polarizations_available"],
           nirow["archive"], nirow["collection"])
          == ("L", "L2 GCOV (provisional)", "4005", "57", "30", "HH", "HH+HV",
              M.ARCHIVE_ASF, "NISAR_L2_GCOV_PROVISIONAL_V1"), nirow)
    check("SAR rows carry the search window but no cloud limit",
          s1row.get("search_start_utc") == "2098-12-03T00:00:00Z"
          and "search_max_cloud_pct" not in s1row)
    sx = s1rows.get("2099-01-09", {})
    check("an unmatched SAR file keeps what its name says, side from the event date",
          (sx.get("side"), sx.get("acquired_utc"), sx.get("relative_orbit"),
           sx.get("orbit_direction"), sx.get("scene_id"), sx.get("mission"))
          == ("after", "2099-01-09", "64", "ascending", NA, "Sentinel-1"), sx)

    mx = {r["scene_id"]: r for r in secs["maxar"]}
    mf = mx[FID_FIG]
    check("Maxar from the cached search: sensor, catalog id, off-nadir, sun, GSD, level",
          (mf["sensor"], mf["catalog_id"], mf["off_nadir_deg"], mf["sun_elevation_deg"],
           mf["sun_azimuth_deg"], mf["gsd_m"], mf["processing_level"], mf["product_name"])
          == ("WV03", "10400100ABCDEF00", "21.0", "12.3", "171.4", "0.31", "LV3D",
              "DAILY_TAKE"), mf)
    check("Maxar: strip cloud as a %, coverage of the search box, days, search window",
          (mf["strip_cloud_pct"], mf["aoi_coverage_pct"], mf["days_from_event"],
           mf["search_start_utc"], mf["search_end_utc"], mf["side"])
          == ("0.7", "50", "2.5", "2098-01-02T00:00:00Z", "2099-04-02T00:00:00Z", "after"), mf)
    ml = mx[FID_LOST]
    check("Maxar strip with no cached record: what the layer remembers, the rest n/a",
          (ml["sensor"], ml["catalog_id"], ml["off_nadir_deg"], ml["processing_level"],
           ml["acquired_utc"], ml["sun_elevation_deg"], ml["archive"])
          == ("WV02", "10300100LOST000", "9.9", "LV2A", "2098-12-20T12:00:00Z", NA,
              M.ARCHIVE_GEGD), ml)
    check("rows sort before -> after inside a table",
          [r["side"] for r in every["sar_sentinel1"]] == ["before", "after"]
          and [r["side"] for r in every["maxar"]][-1] == "after")

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

    # ---- one CSV per table, plus a README
    titles = [t for k, t, _c in M.SECTIONS if k in secs]
    files = M.to_csv_files(secs, "AK")
    want = {f"AK_{k}.csv" for k in secs} | {"AK_README.txt"}
    check("one CSV per table plus a README", set(files) == want, sorted(files))
    for k in secs:
        back = list(csv.DictReader(io.StringIO(files[f"AK_{k}.csv"])))
        check(f"AK_{k}.csv is one plain table: its header, one line per scene",
              tuple(back[0]) == hdr[k] and len(back) == len(secs[k])
              and not files[f"AK_{k}.csv"].startswith("#"))
    nb = list(csv.DictReader(io.StringIO(files["AK_sar_nisar.csv"])))
    check("the NISAR file reads back with its values", nb[0]["frame"] == "57")
    allcsv = "".join(v for k, v in files.items() if k.endswith(".csv"))
    check("no blank cell in any written file",
          ",," not in allcsv and not re.search(r",\r\n", allcsv) and "\r\n," not in allcsv)
    readme = files["AK_README.txt"]
    check("README names every file and table, and carries the column notes",
          all(t in readme for t in titles) and all(f"AK_{k}.csv" in readme for k in secs)
          and all(line in readme for line in M.ABOUT))

    # ---- what the viewer embeds
    pl = M.payload(secs, "AK")
    check("payload: the .zip name, the scene count, the table titles, every file",
          pl["name"] == "AK_scenes.zip" and pl["n"] == sum(len(v) for v in secs.values())
          and pl["tables"] == titles
          and {f["name"]: f["csv"] for f in pl["files"]} == files)

    # ---- one tab's odd file never costs the others
    write(os.path.join(sdir, "search.json"), "{not json")
    rows3 = M.collect(proj, ev)
    check("an unreadable SAR search still lists the optical scenes and the SAR files",
          "sentinel2" in rows3 and len(rows3.get("sar_sentinel1", [])) == 2
          and len(rows3.get("sar_nisar", [])) == 1)

check("an empty / missing folder is no tables, not an error",
      M.collect("") == {} and M.collect("/nonexistent/folder/x") == {})

# ---------------------------------------------------------------- the page
evil = M._row("sentinel2", scene_id="</script><b>x")
evil.pop("_section")
scene_cfg = M.payload({"sentinel2": [evil]}, "AK")
base = {"title": "t", "ncols": 2, "nrows": 2, "width_m": 1, "height_m": 1,
        "zmin": 0, "zmax": 1, "exaggeration": 1, "elev_b64": "",
        "before_uri": "", "after_uri": "", "event_id": "AK2099-0101",
        "scene_csv": scene_cfg}
html = W.build_viewer_html(base)
check("the page has the CSV button", 'id="csvBtn"' in html)
m = re.search(r'<script id="cfg" type="application/json">(.*?)</script>', html, re.S)
cfg = json.loads(m.group(1)) if m else {}
check("a '</script>' inside a scene id cannot end the payload early, and round-trips",
      (cfg.get("scene_csv") or {}).get("files") == scene_cfg["files"])
check("the event id rides along for imagery_details.txt", cfg.get("event_id") == "AK2099-0101")
check("the CSV button saves the per-table files as one ZIP",
      "zipStore(S.files" in html and "S.csv" not in html)
check("both figure ZIPs get imagery_details.txt from the Details rows",
      'name:"imagery_details.txt"' in html and "function detailsText()" in html
      and "figureDetailRows({})" in html)

node = shutil.which("node")
if node:
    js = html.split('<script>\n', 1)[1].rsplit("</script>", 1)[0]
    with tempfile.TemporaryDirectory() as d:
        p = write(os.path.join(d, "viewer.js"), js)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
    check("the viewer script still parses", r.returncode == 0, r.stderr[-400:])

print(("FAILED: " + ", ".join(fails)) if fails else "all scene_manifest checks passed")
sys.exit(1 if fails else 0)
