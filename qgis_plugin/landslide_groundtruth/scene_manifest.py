"""The scenes behind an exported figure, as small tables someone else can search with.

The exported 3D viewer carries these tables behind its "Export metadata CSV"
button. They are written for a reader who has the figure but not this project
(an analyst, or an agent at another organisation) and wants to fetch the same
acquisitions again. So a row says only that: which event the scene was pulled
for, before or after, when it was acquired, its id, and the archive + dataset
that serve it. How each source was searched (centre, radius, dates, cloud limit)
is said once per source, in the figure ZIPs' imagery_details.txt, not on every
row. Nothing that only means something on this machine: no dock tab names, file
names or viewer state.

Ethan cut the tables to these six columns on 2026-11-18. Before that each table
had 26-28: footprint bounds, the search repeated on every row, and fields the
scene ID already spells out (mission, tile, orbit, frame).

Which scenes: the before/after scenes draped in the figure, plus every SAR scene
the project downloaded (radar is never draped, but it is half the evidence).
With no figure given (viewer=None) every downloaded scene is listed.

One CSV, rows grouped by mission (Sentinel-2, Landsat, PlanetScope, Maxar /
WorldView, SAR Sentinel-1, SAR NISAR); the dataset column says which. (It was
one CSV per mission in a ZIP until the columns became the same for all of them,
2026-11-18.) A value the project never recorded reads "n/a".

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
    layer's own properties. Their scene_id is the Maxar catalog ID, which other
    archives know, not GEGD's own feature UUID.

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

__all__ = ["SECTIONS", "COLUMNS", "NA", "collect", "to_csv", "search_lines", "payload",
           "aoi_box", "coverage_pct"]

NA = "n/a"          # the project never recorded it

COLUMNS = ("event_id", "side", "acquired_utc", "scene_id", "archive", "dataset")

# (key, title), in the order the tables are written.
SECTIONS = (
    ("sentinel2", "Sentinel-2"),
    ("landsat", "Landsat"),
    ("planetscope", "PlanetScope"),
    ("maxar", "Maxar / WorldView"),
    ("sar_sentinel1", "SAR - Sentinel-1"),
    ("sar_nisar", "SAR - NISAR"),
)
_TITLES = dict(SECTIONS)

ARCHIVE_MPC = "Microsoft Planetary Computer"
ARCHIVE_PLANET = "Planet Labs"
ARCHIVE_GEGD = "NGA GEGD Pro"
ARCHIVE_ASF = "NASA ASF DAAC"

# <event>_<side>_<scene tag>_planet_<tone>[_toa].tif  (run_single._write_render_json)
_PLANET_RE = re.compile(
    r"^(?P<event>.+?)_(?P<side>pre|post)_(?P<tag>.+)_planet_[a-z]+(?:_toa)?\.tif$")
# <S1|NISAR>_<date>_<orbit>_t<track>_<pol>_<res>m.tif  (sar_tab._amp_save_path)
_SAR_RE = re.compile(
    r"^(?P<tag>S1|NISAR)_(?P<date>\d{4}-\d{2}-\d{2}|undated)_(?P<orbit>[a-z]+)"
    r"_t(?P<track>\w+?)_(?P<pol>[A-Za-z]+)_(?P<res>[\d.]+)m\.tif$")
_PLANET_ID_RE = re.compile(r"^(\d{8})_(\d{6})(?:_\d{2})?_([0-9a-f]{4})$")
_LANDSAT_ID_RE = re.compile(r"^L[COTEM]\d{2}_")


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
# Only used to pick, of two frames of one SAR pass, the one that covers the AOI.

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


def _acquired(text):
    """'2026-09-13T20:40:41Z' -- or just the date when that is all there is."""
    s = str(text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    d = _parse_time(s)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d is not None else NA


def _side(side):
    return {"pre": "before", "post": "after"}.get(side, NA)


def _search_line(lat, lon, radius, start, end, cloud=False, max_cloud=None):
    """'within 15 km of 60.30452, -140.32928, 2026-07-28 to 2026-12-25 (UTC),
    scenes up to 50% cloud' -- the parts that were recorded."""
    parts = []
    try:
        parts.append(f"within {float(radius):g} km of {float(lat):.5f}, {float(lon):.5f}")
    except (TypeError, ValueError):
        pass
    if start is not None and end is not None:
        parts.append(f"{start:%Y-%m-%d} to {end:%Y-%m-%d} (UTC)")
    if cloud:
        try:
            mc = float(max_cloud)
            parts.append(f"scenes up to {mc:g}% cloud" if mc < 100 else "no cloud limit")
        except (TypeError, ValueError):
            parts.append("no cloud limit")
    return ", ".join(parts) or NA


def _tab_search(search, cloud):
    """The search line of one tab's search.json (event time -/+ pre/post days)."""
    s = search or {}
    p = s.get("params") or {}
    t = _parse_time(s.get("datetime_utc"))
    start = end = None
    if t is not None:
        try:
            start = t - timedelta(days=float(p["pre_days"]))
            end = t + timedelta(days=float(p["post_days"]))
        except (KeyError, TypeError, ValueError):
            start = end = None
    return _search_line(s.get("lat"), s.get("lon"), p.get("radius_km"), start, end,
                        cloud, p.get("max_cloud"))


def _row(section, search_line, **kv):
    """A row of COLUMNS, NA where empty; _section / _search ride along until written."""
    r = dict.fromkeys(COLUMNS, NA)
    r.update({k: (NA if v in (None, "") else str(v)) for k, v in kv.items() if k in r})
    r["_section"] = section
    r["_search"] = search_line
    return r


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
    line = _tab_search(search, cloud=True)
    rows = []
    for _path, meta in _matching(docs, event_id):
        ev = event_id or str(meta.get("event_id") or "")
        layers = [str(x) for x in meta.get("layers") or []]
        for side in ("pre", "post"):
            ids = [str(x) for x in meta.get(f"{side}_scenes") or []]
            dates = list(meta.get(f"{side}_dates") or [])
            files = [f for f in layers if f"_{side}_" in os.path.basename(f)
                     and f.lower().endswith(".tif")]
            for i, sid in enumerate(ids):
                hit = index.get(sid)
                date = (hit[1].get("date") if hit else None) or (dates[i] if i < len(dates) else "")
                if _LANDSAT_ID_RE.match(sid):
                    landsat = True
                elif sid.startswith("S2"):
                    landsat = False
                else:
                    landsat = meta.get("sensor") == "landsat"
                row = _row("landsat" if landsat else "sentinel2", line,
                           event_id=ev, side=_side(side),
                           acquired_utc=_acquired(date) if date else NA, scene_id=sid,
                           archive=ARCHIVE_MPC,
                           dataset="landsat-c2-l2" if landsat else "sentinel-2-l2a")
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
            found.append((p, m.group("event"), m.group("side"), m.group("tag")))
    if not found:
        return []
    if event_id and any(f[1] == event_id for f in found):
        found = [f for f in found if f[1] == event_id]
    search = _load_json(os.path.join(pdir, "search.json")) or {}
    index = _search_index(search)
    line = _tab_search(search, cloud=True)
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
            date = hit[1].get("date") if hit else None
            m = _PLANET_ID_RE.match(sid)
            if not date and m:
                d, t = m.group(1), m.group(2)
                date = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"
            row = _row("planetscope", line, event_id=event_id or ev, side=_side(side),
                       acquired_utc=_acquired(date) if date else NA, scene_id=sid,
                       archive=ARCHIVE_PLANET, dataset="PSScene")
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
    line = _tab_search(search, cloud=False)
    cands = [(side, c) for side in ("pre", "post")
             for c in search.get(side) or [] if isinstance(c, dict)]
    rows = []
    for path, m in files:
        tag, date, orbit, track = (m.group("tag"), m.group("date"), m.group("orbit"),
                                   m.group("track"))
        hits = []
        for side, c in cands:
            is_nisar = c.get("source") == "NISAR"
            if ((tag == "NISAR") == is_nisar
                    and (c.get("date") or "")[:10] == date
                    and (c.get("orbit_state") or "")[:4] == orbit
                    and str(c.get("relative_orbit")) == track):
                hits.append((side, c))
        c = {}
        if hits:
            # Two frames of one pass can both match; the file is clipped to the
            # AOI, so it is the one that covers it.
            side, c = max(hits, key=lambda h: coverage_pct(h[1], box) or 0.0)
        else:
            side = ""
            if event_day and date != "undated":
                side = "pre" if date < event_day else ("post" if date > event_day else "")
        when = c.get("date") or ("" if date == "undated" else date)
        common = dict(event_id=ev, side=_side(side),
                      acquired_utc=_acquired(when) if when else NA, scene_id=c.get("id"))
        if tag == "NISAR":
            row = _row("sar_nisar", line, archive=ARCHIVE_ASF,
                       dataset=c.get("collection") or "NISAR_L2_GCOV", **common)
        else:
            row = _row("sar_sentinel1", line, archive=ARCHIVE_MPC,
                       dataset="sentinel-1-rtc", **common)
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
            line = _search_line(doc.get("lat"), doc.get("lon"), doc.get("radius_km"),
                                _parse_time(doc.get("t0")), _parse_time(doc.get("t1")))
            row = _row("maxar", line, event_id=event_id or doc.get("event_id"),
                       side=_side(s.get("side")),
                       acquired_utc=_acquired(p.get("acquisitionDate")),
                       scene_id=p.get("legacyIdentifier") or lp.get("catalog_id") or fid,
                       archive=ARCHIVE_GEGD, dataset="Maxar:FinishedFeature")
        else:                           # the search is gone: what the layer remembers
            row = _row("maxar", NA, event_id=event_id or lp.get("event_id"),
                       side=_side(s.get("side")),
                       acquired_utc=_acquired(lp.get("acq_utc")) if lp.get("acq_utc") else NA,
                       scene_id=lp.get("catalog_id") or fid,
                       archive=ARCHIVE_GEGD, dataset="Maxar:FinishedFeature")
        row["_in_figure"] = bool(s.get("in_figure"))
        rows.append(row)
    return rows


# ---------------------------------------------------------------- together

def collect(results_dir, event_id="", viewer=None, maxar=()):
    """{section key: [rows]} in SECTIONS order; tables with no rows are left out.

    Each row is COLUMNS plus "_search", the line saying how its source was
    searched (for search_lines; to_csv leaves it out of the CSV).
    viewer: {"Before": [paths], "After": [paths]} -- the files draped in the
    figure. Given, the optical / PlanetScope / Maxar tables keep only the
    figure's scenes; SAR keeps every downloaded scene. None lists everything.
    maxar: the streamed strips, [{"fid", "side", "in_figure", "props"}] (props:
    the layer's landslide/gegd_* values -- event_id, acq_utc, catalog_id -- the
    fallback when no cached search knows the strip). event_id: stamped on every
    row (the loaded Detection's), whatever id a file was written under.
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
    out = {key: [] for key, _t in SECTIONS}
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


def to_csv(sections):
    """One CSV text: COLUMNS, then every row, grouped by mission in SECTIONS
    order (before -> after inside each). No BOM: the viewer adds it when it
    saves the file, for Excel."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for key, _t in SECTIONS:
        for r in sections.get(key, []):
            w.writerow([r.get(c, NA) for c in COLUMNS])
    return buf.getvalue()


def search_lines(sections):
    """['Sentinel-2: within 15 km of ..., 2026-07-28 to 2026-12-25 (UTC), ...'],
    one per source searched, for imagery_details.txt."""
    out = []
    for key, title in SECTIONS:
        for line in dict.fromkeys(r.get("_search") or NA for r in sections.get(key, [])):
            note = "; scene_id in the CSV is the Maxar catalog ID" if key == "maxar" else ""
            out.append(f"{title}: {line}{note}")
    return out


def payload(sections, event_id):
    """The viewer's scene_csv config: {"name" (<event_id>_imagery.csv, the file
    the button saves), "csv", "n" (scenes), "tables" (the missions in it),
    "searches" (for imagery_details.txt)}."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(event_id or "")).strip("._") or "event"
    return {"name": stem + "_imagery.csv", "csv": to_csv(sections), "n": count(sections),
            "tables": [t for k, t in SECTIONS if k in sections],
            "searches": search_lines(sections)}
