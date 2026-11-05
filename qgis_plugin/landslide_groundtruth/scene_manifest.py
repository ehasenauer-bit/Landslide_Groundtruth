"""Every scene this project downloaded, as the rows of one CSV.

The exported 3D viewer carries this table behind its "Export scene CSV" button,
so a figure that gets passed around still says which acquisitions it was built
from. One row per downloaded scene, from every imagery tab, under the headings
those tabs' scene tables use (Side, Date (UTC), Gap (d), Cloud, Coverage,
Source, Radar, Orbit, Track, Sensor, Off-nadir°, Sun°, Strip cloud %, Scene ID).

"Downloaded" is read from what each tab leaves in the project folder, not from
the tables on screen: those are empty again after the project is reopened, and
a re-search can list scenes that were never fetched.

  * Sentinel-2 / Landsat -- qgis_packages/<event>_metadata.json names the
    scenes each Run composited; their table columns come from the tab's
    search.json beside the project.
  * PlanetScope -- the rendered GeoTIFFs in planet/ are named for their scene
    (one scene -> its id; several -> a hash, resolved through render.json).
  * SAR -- one amplitude GeoTIFF per downloaded scene in sar/amplitude, named
    radar_date_orbit_track; matched back to sar/search.json.
  * Maxar -- streamed, never written to disk: the plugin passes those rows in
    from its stream layers (see viewer3d_tab._maxar_scene_rows).

A scene whose search entry is gone (the tab was searched again since, over a
different window) still gets a row, with what its file name says.

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

__all__ = ["COLUMNS", "TABS", "collect", "to_csv", "aoi_box", "coverage_pct"]

COLUMNS = (
    "Event", "Tab", "Side", "Date (UTC)", "Gap (d)", "Cloud", "Coverage (% AOI)",
    "Source", "Radar", "Orbit", "Track", "Sensor", "Off-nadir°", "Sun°",
    "Strip cloud %", "Scene ID", "In 3D viewer", "File",
)

# The dock's own tab titles, so a row says where to look for it.
TAB_OPTICAL = "Sentinel-2 / Landsat"
TAB_PLANET = "PlanetScope"
TAB_MAXAR = "Maxar VHR (GEGD)"
TAB_SAR = "SAR (Sentinel-1)"
TABS = (TAB_OPTICAL, TAB_PLANET, TAB_MAXAR, TAB_SAR)     # the dock's order

# Mirrors of dock.SNOW_UNRELIABLE_PCT and theme.CLOUD_SNOW_MARK (both import
# Qt): an AOI this snowy reads its cloud % as a lower bound, and the
# Sentinel-2 table marks that cell with the snowflake.
SNOW_UNRELIABLE_PCT = 50.0
CLOUD_SNOW_MARK = "❄"

SENSOR_SOURCE = {"s2": "Sentinel-2", "landsat": "Landsat", "planet": "PlanetScope"}

# <event>_<side>_<scene tag>_planet_<tone>[_toa].tif  (run_single._write_render_json)
_PLANET_RE = re.compile(
    r"^(?P<event>.+?)_(?P<side>pre|post)_(?P<tag>.+)_planet_[a-z]+(?:_toa)?\.tif$")
# <S1|NISAR>_<date>_<orbit>_t<track>_<pol>_<res>m.tif  (sar_tab._amp_save_path)
_SAR_RE = re.compile(
    r"^(?P<tag>S1|NISAR)_(?P<date>\d{4}-\d{2}-\d{2}|undated)_(?P<orbit>[a-z]+)"
    r"_t(?P<track>\w+?)_(?P<pol>[A-Za-z]+)_(?P<res>[\d.]+)m\.tif$")
_PLANET_ID_RE = re.compile(r"^(\d{8})_(\d{6})_")


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


# ---------------------------------------------------------------- cells

def _date(c):
    return (c.get("date") or "")[:16].replace("T", " ")


def _gap(c):
    return "" if c.get("gap_days") is None else str(c["gap_days"])


def _cloud(c, snow_mark=True):
    """The Cloud cell as the optical tables print it: AOI cloud, '~' for the
    whole-scene fallback, the snowflake where snow makes it a lower bound."""
    aoi = c.get("aoi_cloud_pct")
    scene = c.get("cloud_pct")
    if aoi is not None:
        txt = f"{aoi:.0f}%"
        snow = c.get("aoi_snow_pct")
        if snow_mark and snow is not None and snow >= SNOW_UNRELIABLE_PCT:
            txt = f"{CLOUD_SNOW_MARK} " + txt
        return txt
    if scene is not None:
        return f"~{scene:.0f}%"
    return ""


def _cover(c, box):
    pct = coverage_pct(c, box)
    return "" if pct is None else f"{pct:.0f}"


def _row(**kv):
    r = dict.fromkeys(COLUMNS, "")
    r.update({k: ("" if v is None else v) for k, v in kv.items()})
    return r


def _id_date(scene_id):
    """'YYYY-MM-DD HH:MM' out of a PlanetScope id, or ''."""
    m = _PLANET_ID_RE.match(scene_id or "")
    if not m:
        return ""
    d, t = m.groups()
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"


# ---------------------------------------------------------------- per tab

def _matching(docs, event_id):
    """The (path, doc) pairs for this event, or all of them when none match."""
    if event_id:
        mine = [(p, d) for p, d in docs if str(d.get("event_id") or "") == event_id]
        if mine:
            return mine
    return docs


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
        ev = str(meta.get("event_id") or "")
        layers = [str(x) for x in meta.get("layers") or []]
        for side in ("pre", "post"):
            ids = [str(x) for x in meta.get(f"{side}_scenes") or []]
            dates = list(meta.get(f"{side}_dates") or [])
            files = [f for f in layers if f"_{side}_" in os.path.basename(f)
                     and f.lower().endswith(".tif")]
            for i, sid in enumerate(ids):
                hit = index.get(sid)
                if hit is not None:
                    c = hit[1]
                    rows.append(_row(
                        Event=ev, Tab=TAB_OPTICAL, Side=side, **{
                            "Date (UTC)": _date(c), "Gap (d)": _gap(c),
                            "Cloud": _cloud(c), "Coverage (% AOI)": _cover(c, box),
                            "Source": c.get("source", ""), "Scene ID": sid},
                        _files=files))
                else:
                    rows.append(_row(
                        Event=ev, Tab=TAB_OPTICAL, Side=side, **{
                            "Date (UTC)": dates[i] if i < len(dates) else "",
                            "Source": SENSOR_SOURCE.get(meta.get("sensor"),
                                                        meta.get("sensor") or ""),
                            "Scene ID": sid},
                        _files=files))
    return rows


def _planet_rows(results_dir, event_id):
    """PlanetScope: one row per scene in each rendered GeoTIFF."""
    pdir = os.path.join(results_dir, "planet")
    found = []
    for p in sorted(glob.glob(os.path.join(pdir, "*.tif"))):
        m = _PLANET_RE.match(os.path.basename(p))
        if m:
            found.append((p, m.group("event"), m.group("side"), m.group("tag")))
    if not found:
        return []
    if event_id and any(ev == event_id for _, ev, _, _ in found):
        found = [f for f in found if f[1] == event_id]
    search = _load_json(os.path.join(pdir, "search.json")) or {}
    index = _search_index(search)
    box = aoi_box(search)
    render = _load_json(os.path.join(pdir, "render.json")) or {}
    rscenes = render.get("scenes") or {}
    by_scene = {}                       # (side, id) -> row; a re-tone is the same scene
    for path, ev, side, tag in found:
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
            if hit is not None:
                c = hit[1]
                row = _row(Event=ev, Tab=TAB_PLANET, Side=side, **{
                    "Date (UTC)": _date(c), "Gap (d)": _gap(c),
                    "Cloud": _cloud(c, snow_mark=False),
                    "Coverage (% AOI)": _cover(c, box),
                    "Source": c.get("source") or "PlanetScope", "Scene ID": sid})
            else:
                row = _row(Event=ev, Tab=TAB_PLANET, Side=side, **{
                    "Date (UTC)": _id_date(sid), "Source": "PlanetScope",
                    "Scene ID": sid})
            row["_files"] = [path]
            by_scene[key] = row
    return list(by_scene.values())


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
    event_day = str(search.get("datetime_utc") or "")[:10]
    cands = [(side, c) for side in ("pre", "post")
             for c in search.get(side) or [] if isinstance(c, dict)]
    rows = []
    for path, m in files:
        tag, date, orbit, track = (m.group("tag"), m.group("date"),
                                   m.group("orbit"), m.group("track"))
        radar = "NISAR L" if tag == "NISAR" else "S1 C"
        hits = []
        for side, c in cands:
            is_nisar = c.get("source") == "NISAR"
            if ((tag == "NISAR") == is_nisar
                    and (c.get("date") or "")[:10] == date
                    and (c.get("orbit_state") or "")[:4] == orbit
                    and str(c.get("relative_orbit")) == track):
                hits.append((side, c))
        if hits:
            # Two frames of one pass can both match; the file is clipped to the
            # AOI, so it is the one that covers it.
            side, c = max(hits, key=lambda h: coverage_pct(h[1], box) or 0.0)
            cov = coverage_pct(c, box)
            row = _row(Event=ev, Tab=TAB_SAR, Side=side, **{
                "Date (UTC)": _date(c), "Gap (d)": _gap(c),
                "Coverage (% AOI)": "—" if not cov else f"{cov:.0f}",
                "Source": c.get("source", ""), "Radar": radar,
                "Orbit": (c.get("orbit_state") or "")[:4],
                "Track": "" if c.get("relative_orbit") is None else str(c["relative_orbit"]),
                "Scene ID": c.get("id", "")})
        else:
            side = ""
            if event_day and date != "undated":
                side = "pre" if date < event_day else ("post" if date > event_day else "")
            row = _row(Event=ev, Tab=TAB_SAR, Side=side, **{
                "Date (UTC)": "" if date == "undated" else date,
                "Source": "NISAR" if tag == "NISAR" else "Sentinel-1",
                "Radar": radar, "Orbit": orbit, "Track": track})
        row["_files"] = [path]
        rows.append(row)
    return rows


# ---------------------------------------------------------------- together

def collect(results_dir, event_id="", viewer=None, extra=()):
    """Every downloaded scene in `results_dir`, as dicts keyed by COLUMNS.

    viewer: {"Before": [paths], "After": [paths]} -- the files draped in the 3D
    viewer, which fill the "In 3D viewer" column. extra: rows built elsewhere
    (Maxar streams), dicts keyed by COLUMNS, appended under their own Tab.
    Sorted by tab, then pre before post, then date."""
    viewer = viewer or {}
    where = {}
    for label in ("Before", "After"):
        for p in viewer.get(label) or []:
            where.setdefault(_norm(p), []).append(label)
    rows = []
    if results_dir and os.path.isdir(results_dir):
        for build in (_optical_rows, _planet_rows, _sar_rows):
            try:
                rows += build(results_dir, event_id or "")
            except Exception:           # one tab's odd file never costs the others
                continue
    for r in rows:
        files = r.pop("_files", []) or []
        labels = []
        for f in files:
            for lab in where.get(_norm(f), []):
                if lab not in labels:
                    labels.append(lab)
        r["In 3D viewer"] = " + ".join(labels)
        r["File"] = "; ".join(sorted({os.path.basename(f) for f in files}))
    for e in extra or ():
        rows.append(_row(**{k: e.get(k, "") for k in COLUMNS}))
    order = {t: i for i, t in enumerate(TABS)}
    rows.sort(key=lambda r: (order.get(r["Tab"], len(order)),
                             0 if r["Side"] == "pre" else 1 if r["Side"] == "post" else 2,
                             r["Date (UTC)"], r["Scene ID"]))
    return rows


def to_csv(rows):
    """CSV text (header + rows), Excel-safe quoting. No BOM: the viewer adds it
    when it saves the file, where Excel needs it to read ❄ and ° as UTF-8."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r.get(k, "") for k in COLUMNS])
    return buf.getvalue()
