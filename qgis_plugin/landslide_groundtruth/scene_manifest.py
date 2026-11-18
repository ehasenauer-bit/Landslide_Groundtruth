"""The scenes behind an exported figure, as tables someone else can search with.

The exported 3D viewer carries these tables behind its "Export metadata CSV"
button. They are written for a reader who has the figure but not this project
(an analyst, or an agent at another organisation) and wants to find the same
acquisitions in the same archives. So every row says where the scene lives
(archive + collection), what identifies it (scene ID, tile / orbit / frame,
catalog ID), where it lies (footprint bounds) and how it was searched for
(centre, radius, date window, cloud limit). Nothing that only means something
on this machine: no dock tab names, file names or viewer state.

Which scenes: the before/after scenes draped in the figure, plus every SAR scene
the project downloaded (radar is never draped, but it is half the evidence).
With no figure given (viewer=None) every downloaded scene is listed.

One table per mission family, so every column applies to every row of its table
and no cell is structurally empty: Sentinel-2, Landsat, PlanetScope,
Maxar / WorldView, and under SAR, Sentinel-1 and NISAR. A value that applies but
was not recorded (a scene whose search entry has gone) reads "n/a".

"Downloaded" is read from what each tab leaves in the project folder, not from
the tables on screen (empty again after a reopen):

  * Sentinel-2 / Landsat -- qgis_packages/<event>_metadata.json names the
    scenes each Run composited and the GeoTIFFs it wrote; search.json beside the
    project has their metadata.
  * PlanetScope -- the rendered GeoTIFFs in planet/ are named for their scene
    (one scene -> its id; several -> a hash, resolved through render.json).
  * SAR -- one amplitude GeoTIFF per downloaded scene in sar/amplitude, named
    radar_date_orbit_track_pol; matched back to sar/search.json.
  * Maxar -- streamed, never written to disk: the plugin names the streamed
    strips (viewer3d_tab._maxar_streams); their records come from the Maxar
    tab's cached searches, gegd/<event>/search.json, else from the stream
    layer's own properties.

Written as one plain CSV per table plus a README that says how to read the
columns (to_csv_files); the viewer saves them together as one ZIP. One file per
table, not one file with a block per table, so every file opens as a clean
table in a spreadsheet or pandas (Ethan's pick, 2026-11-17).

No QGIS imports, so it is testable on its own.
"""
import ast
import csv
import glob
import io
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

__all__ = ["SECTIONS", "NA", "collect", "to_csv_files", "payload", "aoi_box",
           "coverage_pct"]

NA = "n/a"          # applies to this scene, but the project never recorded it

_HEAD = ("event_id", "side", "acquired_utc", "days_from_event")
_WHERE = ("aoi_coverage_pct", "scene_id", "archive", "collection",
          "footprint_west", "footprint_south", "footprint_east", "footprint_north")
_SEARCH = ("search_center_lat", "search_center_lon", "search_radius_km",
           "search_start_utc", "search_end_utc")
_SEARCH_CLOUD = _SEARCH + ("search_max_cloud_pct",)
_OPTICAL_CLOUD = ("aoi_cloud_pct", "aoi_snow_pct", "aoi_cloud_method", "scene_cloud_pct")
_SAR = ("mission", "band", "product", "acquisition_mode", "polarization_used",
        "polarizations_available", "orbit_direction", "relative_orbit")

# (key, title, columns), in the order the tables are written.
SECTIONS = (
    ("sentinel2", "Sentinel-2",
     _HEAD + ("mission", "product_level", "mgrs_tile", "relative_orbit")
     + _OPTICAL_CLOUD + _WHERE + _SEARCH_CLOUD),
    ("landsat", "Landsat",
     _HEAD + ("mission", "product_level", "wrs_path", "wrs_row", "collection_category")
     + _OPTICAL_CLOUD + _WHERE + _SEARCH_CLOUD),
    ("planetscope", "PlanetScope",
     _HEAD + ("satellite_id", "product_bundle", "scene_cloud_pct") + _WHERE + _SEARCH_CLOUD),
    ("maxar", "Maxar / WorldView",
     _HEAD + ("sensor", "catalog_id", "off_nadir_deg", "sun_elevation_deg", "sun_azimuth_deg",
              "gsd_m", "processing_level", "product_name", "strip_cloud_pct")
     + _WHERE + _SEARCH),
    ("sar_sentinel1", "SAR - Sentinel-1",
     _HEAD + _SAR + ("absolute_orbit", "datatake_id", "esa_granule") + _WHERE + _SEARCH),
    ("sar_nisar", "SAR - NISAR",
     _HEAD + _SAR + ("frame", "cycle") + _WHERE + _SEARCH),
)
_COLUMNS = {key: cols for key, _title, cols in SECTIONS}
_TITLES = {key: title for key, title, _cols in SECTIONS}

# What a reader needs to read the columns right; the body of the README.
ABOUT = (
    "One table per mission; every column applies to every row of its table.",
    "side: before / after the event. days_from_event: days between the "
    "acquisition and the event origin time.",
    "aoi_*: inside the square search box, search_center_lat/lon +- search_radius_km. "
    "aoi_cloud_method: ocm = OmniCloudMask, scl = Sentinel-2 Scene Classification.",
    "scene_cloud_pct / strip_cloud_pct: the archive's own whole-scene figure.",
    "footprint_*: the scene's bounding box, degrees (WGS84).",
    "search_*: the search that found the scene (dates UTC).",
    "n/a: applies to this scene but was not recorded.",
)

ARCHIVE_MPC = "Microsoft Planetary Computer"
ARCHIVE_PLANET = "Planet Labs"
ARCHIVE_GEGD = "NGA GEGD Pro"
ARCHIVE_ASF = "NASA ASF DAAC"

# Mirror of dock.SNOW_UNRELIABLE_PCT (dock imports Qt): an AOI this snowy reads
# its cloud % as a lower bound. Kept for callers that want to flag it.
SNOW_UNRELIABLE_PCT = 50.0

# <event>_<side>_<scene tag>_planet_<tone>[_toa].tif  (run_single._write_render_json)
_PLANET_RE = re.compile(
    r"^(?P<event>.+?)_(?P<side>pre|post)_(?P<tag>.+)_planet_[a-z]+(?P<toa>_toa)?\.tif$")
# <S1|NISAR>_<date>_<orbit>_t<track>_<pol>_<res>m.tif  (sar_tab._amp_save_path)
_SAR_RE = re.compile(
    r"^(?P<tag>S1|NISAR)_(?P<date>\d{4}-\d{2}-\d{2}|undated)_(?P<orbit>[a-z]+)"
    r"_t(?P<track>\w+?)_(?P<pol>[A-Za-z]+)_(?P<res>[\d.]+)m\.tif$")
_PLANET_ID_RE = re.compile(r"^(\d{8})_(\d{6})(?:_\d{2})?_([0-9a-f]{4})$")
# S2A_MSIL2A_20990101T213529_R086_T05VPG_20990101T233244 (optional _N0511_ baseline)
_S2_ID_RE = re.compile(
    r"^(S2[A-D])_MSI(L[12][AC])_(\d{8}T\d{6})_(?:N\d{4}_)?R(\d{3})_T(\d{2}[A-Z]{3})_")
# LC09_L2SP_062018_20990120_02_T1 (MPC) or ..._20990120_20990125_02_T1 (USGS)
_LANDSAT_ID_RE = re.compile(
    r"^L([COTEM])(\d{2})_(L\d\w{2})_(\d{3})(\d{3})_(\d{8})_(?:\d{8}_)?(\d{2})_(T1|T2|RT)")
# S1A_IW_GRDH_1SDV_20981228T030020_20981228T030045_012345_00ABCE[_rtc]
_S1_ID_RE = re.compile(
    r"^(S1[A-D])_([A-Z0-9]{2})_(\w{4})_(\w{4})_(\d{8}T\d{6})_(\d{8}T\d{6})"
    r"_(\d{6})_([0-9A-F]{6})")
# NISAR_L2_PR_GCOV_<cycle>_<track>_<A|D>_<frame>_<mode>_<pols>_A_<start>_<end>_...
_NISAR_ID_RE = re.compile(
    r"^NISAR_L(\d)_(\w{2})_(\w{4})_(\d{3})_(\d{3})_([AD])_(\d{3})_(\w{4})_(\w{4})_")

_POL_PAIRS = {"DV": "VV+VH", "DH": "HH+HV", "SV": "VV", "SH": "HH",
              "DHDH": "HH+HV", "DVDV": "VV+VH", "SHNA": "HH", "SVNA": "VV",
              "QPQP": "HH+HV+VH+VV"}


# ---------------------------------------------------------------- reading

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _search_index(search):
    """{scene id: (side, candidate)} over a tab's search.json."""
    out = {}
    for side in ("pre", "post"):
        for c in (search or {}).get(side) or []:
            if isinstance(c, dict) and c.get("id"):
                out[str(c["id"])] = (side, c)
    return out


# ---------------------------------------------------------------- coverage

def aoi_box(search):
    """(minx, miny, maxx, maxy) of a tab's search box in lon/lat, or None.

    The same lat/lon +- radius_km box every tab's _aoi_bbox builds from its
    search result, so the percentages match the tables."""
    try:
        lat = float(search["lat"])
        lon = float(search["lon"])
        radius = float((search.get("params") or {})["radius_km"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return _box(lat, lon, radius)


def _box(lat, lon, radius):
    dlat = radius / 111.32
    dlon = radius / (111.32 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _geometry(g):
    """A GeoJSON dict. search.json has stored some as the str() of one."""
    if isinstance(g, str):
        for parse in (json.loads, ast.literal_eval):
            try:
                g = parse(g)
                break
            except (ValueError, SyntaxError):
                continue
    return g if isinstance(g, dict) else None


def _polygons(g):
    g = _geometry(g)
    if not g:
        return []
    if g.get("type") == "Polygon":
        return [g.get("coordinates") or []]
    if g.get("type") == "MultiPolygon":
        return list(g.get("coordinates") or [])
    return []


def _ring_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def _clip(ring, box):
    """Sutherland-Hodgman: the part of `ring` inside the axis-aligned `box`."""
    minx, miny, maxx, maxy = box
    pts = [(float(p[0]), float(p[1])) for p in ring]
    edges = ((0, minx, True), (0, maxx, False), (1, miny, True), (1, maxy, False))
    for axis, v, keep_ge in edges:
        if not pts:
            break

        def inside(p):
            return p[axis] >= v if keep_ge else p[axis] <= v

        def cut(p, q):
            t = (v - p[axis]) / (q[axis] - p[axis])
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)

        out = []
        for i in range(len(pts)):
            p, q = pts[i - 1], pts[i]
            if inside(q):
                if not inside(p):
                    out.append(cut(p, q))
                out.append(q)
            elif inside(p):
                out.append(cut(p, q))
        pts = out
    return pts


def coverage_pct(candidate, box):
    """% (0-100) of `box` the scene footprint fills, or None when untestable.

    area(footprint ∩ box) / area(box) in lon/lat, like the tabs' _aoi_coverage
    (the box's degree anisotropy cancels top and bottom)."""
    if box is None:
        return None
    polys = _polygons(candidate.get("geometry"))
    if not polys:
        return None
    box_area = (box[2] - box[0]) * (box[3] - box[1])
    if box_area <= 0:
        return None
    try:
        area = 0.0
        for rings in polys:
            if not rings:
                continue
            area += _ring_area(_clip(rings[0], box))
            for hole in rings[1:]:
                area -= _ring_area(_clip(hole, box))
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
    return max(0.0, min(100.0, 100.0 * area / box_area))


def _bounds(candidate):
    """(west, south, east, north) of a scene: its stored bbox, else its footprint."""
    bb = candidate.get("bbox")
    if isinstance(bb, (list, tuple)) and len(bb) == 4:
        try:
            return tuple(float(v) for v in bb)
        except (TypeError, ValueError):
            pass
    xs, ys = [], []
    for rings in _polygons(candidate.get("geometry")):
        for p in (rings[0] if rings else []):
            try:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
            except (TypeError, ValueError, IndexError):
                continue
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# ---------------------------------------------------------------- cells

def _num(v, nd=1):
    """A number at `nd` decimals, NA when absent or unreadable."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return NA
    if math.isnan(f):
        return NA
    return f"{f:.{nd}f}" if nd else str(int(round(f)))


def _parse_time(text):
    """An aware UTC datetime from an ISO-ish string (or a date), or None."""
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for cut in (s, s[:26], s[:19], s[:10]):
        try:
            d = datetime.fromisoformat(cut)
        except ValueError:
            continue
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    return None


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d is not None else NA


def _acquired(text):
    """'2026-09-13T20:40:41Z' -- or just the date when that is all there is."""
    s = str(text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return _iso(_parse_time(s))


def _days(c, scene_time, event_time):
    if c is not None and c.get("gap_days") is not None:
        return _num(c["gap_days"], 0)
    a, b = _parse_time(scene_time), event_time
    if a is None or b is None:
        return NA
    return str(int(round(abs((a - b).total_seconds()) / 86400.0)))


def _side(side):
    return {"pre": "before", "post": "after"}.get(side, NA)


def _where(c, box, scene_id, archive, collection):
    out = {"scene_id": scene_id or NA, "archive": archive, "collection": collection}
    pct = coverage_pct(c, box) if c is not None else None
    out["aoi_coverage_pct"] = _num(pct, 0) if pct is not None else NA
    bb = _bounds(c) if c is not None else None
    for k, v in zip(("footprint_west", "footprint_south", "footprint_east", "footprint_north"),
                    bb or (None,) * 4):
        out[k] = _num(v, 5)
    return out


def _search_cols(search, with_cloud):
    """The search_* cells of one tab's search.json."""
    s = search or {}
    p = s.get("params") or {}
    t = _parse_time(s.get("datetime_utc"))
    out = {"search_center_lat": _num(s.get("lat"), 5),
           "search_center_lon": _num(s.get("lon"), 5),
           "search_radius_km": _num(p.get("radius_km"), 1)}
    for key, sign, days in (("search_start_utc", -1, p.get("pre_days")),
                            ("search_end_utc", 1, p.get("post_days"))):
        try:
            out[key] = _iso(t + sign * timedelta(days=float(days))) if t else NA
        except (TypeError, ValueError):
            out[key] = NA
    if with_cloud:
        mc = p.get("max_cloud")
        out["search_max_cloud_pct"] = _num(mc, 0) if mc is not None else "none"
    return out


def _cloud_cols(c):
    if c is None:
        return dict.fromkeys(_OPTICAL_CLOUD, NA)
    aoi = c.get("aoi_cloud_pct")
    method = {"ocm": "ocm", "scl": "scl"}.get(str(c.get("aoi_cloud_method") or "").lower())
    return {"aoi_cloud_pct": _num(aoi),
            "aoi_snow_pct": _num(c.get("aoi_snow_pct")) if aoi is not None else NA,
            "aoi_cloud_method": (method or NA) if aoi is not None else NA,
            "scene_cloud_pct": _num(c.get("cloud_pct"))}


def _row(section, **kv):
    r = dict.fromkeys(_COLUMNS[section], NA)
    r.update({k: (NA if v in (None, "") else v) for k, v in kv.items() if k in r})
    r["_section"] = section
    return r


# ---------------------------------------------------------------- per tab

def _matching(docs, event_id):
    """The (path, doc) pairs for this event, or all of them when none match."""
    if event_id:
        mine = [(p, d) for p, d in docs if str(d.get("event_id") or "") == event_id]
        if mine:
            return mine
    return docs


def _optical_row(sid, c, side, ev, box, search, meta_date, event_time, sensor_hint):
    """One Sentinel-2 or Landsat scene; `c` is its search entry or None."""
    date = (c or {}).get("date") or meta_date
    head = dict(event_id=ev, side=_side(side), acquired_utc=_acquired(date),
                days_from_event=_days(c, date, event_time))
    cloud = _cloud_cols(c)
    srch = _search_cols(search, True)
    m = _S2_ID_RE.match(sid)
    if m or (not _LANDSAT_ID_RE.match(sid) and sensor_hint == "s2"):
        kv = dict(head, **cloud, **srch,
                  **_where(c, box, sid, ARCHIVE_MPC, "sentinel-2-l2a"))
        if m:
            kv.update(mission="Sentinel-" + m.group(1)[1:], product_level=m.group(2),
                      mgrs_tile=m.group(5), relative_orbit=str(int(m.group(4))))
        return _row("sentinel2", **kv)
    kv = dict(head, **cloud, **srch, **_where(c, box, sid, ARCHIVE_MPC, "landsat-c2-l2"))
    m = _LANDSAT_ID_RE.match(sid)
    if m:
        kv.update(mission=f"Landsat {int(m.group(2))}", product_level=m.group(3),
                  wrs_path=m.group(4), wrs_row=m.group(5), collection_category=m.group(8))
    return _row("landsat", **kv)


def _optical_rows(results_dir, event_id):
    """Sentinel-2 / Landsat: each Run's composited scenes."""
    docs = []
    for p in sorted(glob.glob(os.path.join(results_dir, "qgis_packages", "*_metadata.json"))):
        d = _load_json(p)
        if d is not None:
            docs.append((p, d))
    if not docs:
        return []
    search = _load_json(os.path.join(results_dir, "search.json")) or {}
    index = _search_index(search)
    box = aoi_box(search)
    rows = []
    for _path, meta in _matching(docs, event_id):
        ev = event_id or str(meta.get("event_id") or "")
        event_time = _parse_time(meta.get("datetime_utc") or search.get("datetime_utc"))
        layers = [str(x) for x in meta.get("layers") or []]
        for side in ("pre", "post"):
            ids = [str(x) for x in meta.get(f"{side}_scenes") or []]
            dates = list(meta.get(f"{side}_dates") or [])
            files = [f for f in layers if f"_{side}_" in os.path.basename(f)
                     and f.lower().endswith(".tif")]
            for i, sid in enumerate(ids):
                hit = index.get(sid)
                row = _optical_row(sid, hit[1] if hit else None, side, ev, box, search,
                                   dates[i] if i < len(dates) else "", event_time,
                                   meta.get("sensor"))
                row["_files"] = files
                rows.append(row)
    return rows


def _planet_rows(results_dir, event_id):
    """PlanetScope: one row per scene in each rendered GeoTIFF."""
    pdir = os.path.join(results_dir, "planet")
    found = []
    for p in sorted(glob.glob(os.path.join(pdir, "*.tif"))):
        m = _PLANET_RE.match(os.path.basename(p))
        if m:
            found.append((p, m.group("event"), m.group("side"), m.group("tag"),
                          bool(m.group("toa"))))
    if not found:
        return []
    if event_id and any(f[1] == event_id for f in found):
        found = [f for f in found if f[1] == event_id]
    search = _load_json(os.path.join(pdir, "search.json")) or {}
    index = _search_index(search)
    box = aoi_box(search)
    event_time = _parse_time(search.get("datetime_utc"))
    render = _load_json(os.path.join(pdir, "render.json")) or {}
    rscenes = render.get("scenes") or {}
    by_scene = {}                       # (side, id) -> row; a re-tone is the same scene
    for path, ev, side, tag, toa in found:
        if tag in index:
            ids = [tag]
        elif _norm(render.get(side)) == _norm(path) and rscenes.get(side):
            ids = [str(x) for x in rscenes[side]]
        else:
            ids = [tag]                 # a hashed multi-scene name render.json no longer knows
        for sid in ids:
            key = (side, sid)
            if key in by_scene:
                by_scene[key]["_files"].append(path)
                continue
            hit = index.get(sid)
            c = hit[1] if hit else None
            m = _PLANET_ID_RE.match(sid)
            date = (c or {}).get("date")
            if not date and m:
                d, t = m.group(1), m.group(2)
                date = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"
            row = _row("planetscope",
                       event_id=event_id or ev, side=_side(side),
                       acquired_utc=_acquired(date) if date else NA,
                       days_from_event=_days(c, date, event_time),
                       satellite_id=m.group(3) if m else NA,
                       # what run_single ordered: DN for the TOA path, else SR
                       product_bundle="analytic_udm2" if toa else "analytic_sr_udm2",
                       scene_cloud_pct=_num((c or {}).get("cloud_pct")),
                       **_where(c, box, sid, ARCHIVE_PLANET, "PSScene"),
                       **_search_cols(search, True))
            row["_files"] = [path]
            by_scene[key] = row
    return list(by_scene.values())


def _pols(c, tag_hint=""):
    p = (c or {}).get("polarizations")
    if isinstance(p, (list, tuple)) and p:
        return "+".join(str(x) for x in p)
    return _POL_PAIRS.get(tag_hint, NA)


def _sar_rows(results_dir, event_id):
    """SAR: one row per saved amplitude GeoTIFF (one scene each)."""
    sdir = os.path.join(results_dir, "sar")
    files = []
    for p in sorted(glob.glob(os.path.join(sdir, "amplitude", "*.tif"))):
        m = _SAR_RE.match(os.path.basename(p))
        if m:
            files.append((p, m))
    if not files:
        return []
    search = _load_json(os.path.join(sdir, "search.json")) or {}
    box = aoi_box(search)
    # sar/ holds this project's files only and they carry no event id, so the
    # loaded detection names them; the search's own id is often a datetime one
    # from before the detection was loaded.
    ev = str(event_id or search.get("event_id") or "")
    event_time = _parse_time(search.get("datetime_utc"))
    event_day = str(search.get("datetime_utc") or "")[:10]
    cands = [(side, c) for side in ("pre", "post")
             for c in search.get(side) or [] if isinstance(c, dict)]
    srch = _search_cols(search, False)
    rows = []
    for path, m in files:
        tag, date, orbit, track, pol = (m.group("tag"), m.group("date"), m.group("orbit"),
                                        m.group("track"), m.group("pol").upper())
        hits = []
        for side, c in cands:
            is_nisar = c.get("source") == "NISAR"
            if ((tag == "NISAR") == is_nisar
                    and (c.get("date") or "")[:10] == date
                    and (c.get("orbit_state") or "")[:4] == orbit
                    and str(c.get("relative_orbit")) == track):
                hits.append((side, c))
        c = None
        if hits:
            # Two frames of one pass can both match; the file is clipped to the
            # AOI, so it is the one that covers it.
            side, c = max(hits, key=lambda h: coverage_pct(h[1], box) or 0.0)
        else:
            side = ""
            if event_day and date != "undated":
                side = "pre" if date < event_day else ("post" if date > event_day else "")
        sid = str((c or {}).get("id") or "")
        when = (c or {}).get("date") or ("" if date == "undated" else date)
        direction = (c or {}).get("orbit_state") or \
            {"asce": "ascending", "desc": "descending"}.get(orbit, orbit)
        common = dict(event_id=ev, side=_side(side), acquired_utc=_acquired(when) if when else NA,
                      days_from_event=_days(c, when, event_time),
                      polarization_used=pol, orbit_direction=direction,
                      relative_orbit=str((c or {}).get("relative_orbit") or track),
                      **srch)
        if tag == "NISAR":
            n = _NISAR_ID_RE.match(sid)
            prod = (c or {}).get("product")
            kind = n.group(3) if n else "GCOV"
            row = _row("sar_nisar", mission="NISAR", band=(c or {}).get("band") or "L",
                       product=f"L2 {kind}" + (f" ({prod})" if prod else ""),
                       acquisition_mode=(c or {}).get("mode") or (n.group(8) if n else NA),
                       polarizations_available=_pols(c, n.group(9) if n else ""),
                       frame=str((c or {}).get("frame") or (int(n.group(7)) if n else NA)),
                       cycle=str((c or {}).get("cycle") or (int(n.group(4)) if n else NA)),
                       **common,
                       **_where(c, box, sid, ARCHIVE_ASF,
                                (c or {}).get("collection") or "NISAR_L2_GCOV"))
        else:
            s = _S1_ID_RE.match(sid)
            row = _row("sar_sentinel1",
                       mission=("Sentinel-" + s.group(1)[1:]) if s else "Sentinel-1",
                       band="C", product="GRD RTC (terrain-corrected)",
                       acquisition_mode=s.group(2) if s else NA,
                       polarizations_available=_pols(c, s.group(4)[2:] if s else ""),
                       absolute_orbit=str(int(s.group(7))) if s else NA,
                       datatake_id=s.group(8) if s else NA,
                       # MPC's RTC item id is the ESA GRD granule + "_rtc"; ASF and
                       # the Copernicus browser search by the granule.
                       esa_granule=re.sub(r"_rtc$", "", sid) if s else NA,
                       **common,
                       **_where(c, box, sid, ARCHIVE_MPC, "sentinel-1-rtc"))
        row["_files"] = [path]
        rows.append(row)
    return rows


def _gegd_index(results_dir):
    """{featureId: (feature, search doc)} over every cached Maxar search."""
    out = {}
    docs = []
    for p in glob.glob(os.path.join(results_dir, "gegd", "*", "search.json")):
        d = _load_json(p)
        if d is not None and d.get("format") == "landslide-gegd-search":
            docs.append(d)
    docs.sort(key=lambda d: str(d.get("searched_utc") or ""))      # newest wins
    for d in docs:
        for f in d.get("features") or []:
            if not isinstance(f, dict):
                continue
            props = f.get("properties") or {}
            fid = str(props.get("featureId") or f.get("id") or "").strip()
            if fid:
                out[fid] = (f, d)
    return out


def _maxar_sensor(props):
    raw = str(props.get("source") or props.get("sensorCode") or props.get("sensorName") or "")
    return re.sub(r"_(VNIR|SWIR|PAN|MS|CAVIS)$", "", raw.upper()) or NA


def _maxar_rows(results_dir, event_id, streams):
    """Maxar: one row per streamed strip, its record from the cached search."""
    if not streams:
        return []
    index = _gegd_index(results_dir) if results_dir and os.path.isdir(results_dir) else {}
    rows = []
    for s in streams:
        fid = str(s.get("fid") or "")
        if not fid:
            continue
        feat, doc = index.get(fid, (None, None))
        lp = s.get("props") or {}
        if feat is not None:
            p = feat.get("properties") or {}
            origin = _parse_time(doc.get("origin_utc"))
            acq = _parse_time(p.get("acquisitionDate"))
            box = None
            try:
                box = _box(float(doc["lat"]), float(doc["lon"]), float(doc["radius_km"]))
            except (KeyError, TypeError, ValueError):
                pass
            cloud = p.get("cloudCover")
            row = _row(
                "maxar", event_id=event_id or doc.get("event_id"), side=_side(s.get("side")),
                acquired_utc=_iso(acq),
                days_from_event=(f"{abs((acq - origin).total_seconds()) / 86400.0:.1f}"
                                 if acq and origin else NA),
                sensor=_maxar_sensor(p), catalog_id=p.get("legacyIdentifier"),
                off_nadir_deg=_num(p.get("offNadirAngle")),
                sun_elevation_deg=_num(p.get("sunElevation")),
                sun_azimuth_deg=_num(p.get("sunAzimuth")),
                gsd_m=_num(p.get("groundSampleDistance"), 2),
                processing_level=p.get("processingLevel"), product_name=p.get("productName"),
                strip_cloud_pct=_num(100.0 * float(cloud)) if cloud is not None else NA,
                search_center_lat=_num(doc.get("lat"), 5),
                search_center_lon=_num(doc.get("lon"), 5),
                search_radius_km=_num(doc.get("radius_km"), 1),
                search_start_utc=_iso(_parse_time(doc.get("t0"))),
                search_end_utc=_iso(_parse_time(doc.get("t1"))),
                **_where({"geometry": feat.get("geometry")}, box, fid, ARCHIVE_GEGD,
                         "Maxar:FinishedFeature"))
        else:                           # the search is gone: what the layer remembers
            row = _row("maxar", event_id=event_id or lp.get("event_id"),
                       side=_side(s.get("side")),
                       acquired_utc=_acquired(lp.get("acq_utc")) if lp.get("acq_utc") else NA,
                       sensor=lp.get("sensor"), catalog_id=lp.get("catalog_id"),
                       off_nadir_deg=_num(lp.get("ona")), processing_level=lp.get("level"),
                       scene_id=fid, archive=ARCHIVE_GEGD, collection="Maxar:FinishedFeature")
        row["_in_figure"] = bool(s.get("in_figure"))
        rows.append(row)
    return rows


# ---------------------------------------------------------------- together

def collect(results_dir, event_id="", viewer=None, maxar=()):
    """{section key: [rows]} in SECTIONS order; tables with no rows are left out.

    viewer: {"Before": [paths], "After": [paths]} -- the files draped in the
    figure. Given, the optical / PlanetScope / Maxar tables keep only the
    figure's scenes; SAR keeps every downloaded scene. None lists everything.
    maxar: the streamed strips, [{"fid", "side", "in_figure", "props"}] (props:
    the layer's landslide/gegd_* values, the fallback when no cached search
    knows the strip). event_id: stamped on every row (the loaded Detection's),
    whatever id a file was written under.
    Rows sort before -> after, then by acquisition time."""
    figure = None
    if viewer is not None:
        figure = {_norm(p) for label in ("Before", "After") for p in viewer.get(label) or []}
    rows = []
    if results_dir and os.path.isdir(results_dir):
        for build in (_optical_rows, _planet_rows, _sar_rows):
            try:
                rows += build(results_dir, event_id or "")
            except Exception:           # one tab's odd file never costs the others
                continue
    try:
        rows += _maxar_rows(results_dir, event_id or "", maxar)
    except Exception:
        pass
    out = {key: [] for key, _t, _c in SECTIONS}
    seen = set()
    for r in rows:
        sec = r.pop("_section")
        files = r.pop("_files", None)
        in_fig = r.pop("_in_figure", None)
        if figure is not None and not sec.startswith("sar_"):
            if in_fig is None:
                in_fig = any(_norm(f) in figure for f in files or [])
            if not in_fig:
                continue
        key = (sec, r["side"], r["scene_id"]) if r["scene_id"] != NA else None
        if key is not None:
            if key in seen:             # one scene in two runs' composites
                continue
            seen.add(key)
        out[sec].append(r)
    order = {"before": 0, "after": 1}
    for sec in out:
        out[sec].sort(key=lambda r: (order.get(r["side"], 2), r["acquired_utc"], r["scene_id"]))
    return {k: v for k, v in out.items() if v}


def count(sections):
    return sum(len(v) for v in sections.values())


def _table(key, rows, w):
    cols = _COLUMNS[key]
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, NA) for c in cols])


def to_csv_files(sections, stem):
    """{file name: CSV text}, one plain table per mission, plus a README.txt.
    No BOM: the viewer adds it to each CSV when it saves the ZIP, for Excel."""
    out = {}
    for key, _t, _c in SECTIONS:
        if key not in sections:
            continue
        buf = io.StringIO()
        _table(key, sections[key], csv.writer(buf, lineterminator="\r\n"))
        out[f"{stem}_{key}.csv"] = buf.getvalue()
    if out:
        out[f"{stem}_README.txt"] = "\r\n".join(
            ["Scene metadata, one CSV per mission:"]
            + [f"  {stem}_{k}.csv  {_TITLES[k]} ({len(sections[k])} scene"
               f"{'' if len(sections[k]) == 1 else 's'})"
               for k, _t, _c in SECTIONS if k in sections]
            + [""] + list(ABOUT)) + "\r\n"
    return out


def payload(sections, stem):
    """The viewer's scene_csv config: {"name" (the .zip the button saves),
    "files": [{"name", "csv"}], "n" (scenes), "tables" (their titles)}."""
    files = to_csv_files(sections, stem)
    return {"name": stem + "_scenes.zip", "n": count(sections),
            "tables": [_TITLES[k] for k, _t, _c in SECTIONS if k in sections],
            "files": [{"name": k, "csv": v} for k, v in files.items()]}
