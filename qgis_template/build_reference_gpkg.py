#!/usr/bin/env python3
"""Build the reference GeoPackage the QGIS template project points at.

Produces one .gpkg with four layers, all EPSG:4326:

  peaks                    named summits, merged from three gazetteers
  borders_international    Natural Earth 10m admin-0 land boundary lines
  borders_state_province   Natural Earth 10m admin-1 state/province lines
  protected_areas          OSM national parks / protected areas (polygons)

Why a GeoPackage and not shapefiles: one file to keep next to the template,
no 10-character field-name truncation, and UTF-8 names survive intact.

Only the standard library is needed to fetch and stage GeoJSON; the GeoJSON is
then handed to ogr2ogr (found in the QGIS bundle) for the actual .gpkg write and
the clip to the region. That keeps this runnable from system python — QGIS's own
interpreter does not start standalone on macOS without a pile of env setup.

Re-run with a different --bbox to retarget the template at another region:

    python3 build_reference_gpkg.py --bbox 59 -146 63 -134

Downloads are staged under <out>/_staging. --reuse-cache picks up whatever is
already staged instead of re-fetching, which matters because a full OSM sweep of
Alaska + Yukon is ~50 Overpass queries and about twelve minutes.

PEAKS come from three sources because no single one is complete:

  OSM        natural=peak / volcano. The only source carrying elevation, but
             its Alaska coverage is patchy.
  GNIS       USGS Domestic Names, feature_class=Summit. Authoritative for
             Alaska; no elevation column in the current release.
  CGNDB      NRCan Canadian Geographical Names, CONCISE=MTN. Authoritative for
             Yukon and BC; also no elevation.

They are merged with a name + 600 m proximity dedup, OSM winning ties so the
elevation survives. Even so the union is not exhaustive: unofficial climbing
names used on the Logan massif (Catenary Peak, Hubsew Peak) appear in none of
the three and have to be added by hand.

Alaska/Yukon boundary survey monuments are dropped — the survey put numbered
markers into OSM tagged natural=peak ("Monument 144", "Boundary Point 157").
They are survey markers, not summits. See JUNK_NAME.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Alaska + Yukon + the northern BC panhandle. Generous on purpose: the layer is
# clipped once here, then every project reuses it without another network call.
DEFAULT_BBOX = (54.0, -173.0, 72.0, -123.0)          # S, W, N, E

# Rotated on failure. overpass-api.de intermittently returns a dispatcher error
# ("Dispatcher_Client::request_read_and_idx") under load, which is transient —
# the same query on another mirror succeeds immediately.
#
# Every entry MUST serve the whole planet. overpass.osm.ch was in this list and
# caused a silent data loss: it carries only a Switzerland extract, so it
# answers an Alaska query with HTTP 200, valid JSON, no remark, and zero
# elements. Two Brooks Range tiles holding ~400 named peaks were recorded as
# empty. Verify global coverage before adding a mirror here.
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

NE_BASE = "https://naciscdn.org/naturalearth/10m/cultural"
NE_LAYERS = (
    ("ne_10m_admin_0_boundary_lines_land", "borders_international"),
    ("ne_10m_admin_1_states_provinces_lines", "borders_state_province"),
)

GNIS_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames"
            "/DomesticNames/DomesticNames_{state}_Text.zip")
CGNDB_URL = ("https://ftp.maps.canada.ca/pub/nrcan_rncan/vector"
             "/geobase_cgn_toponyme/prov_shp_eng/cgn_{prov}_shp_eng.zip")

# Overpass caps a single query by area and by result size, so the region is
# walked in tiles. 4 deg lat x 12 deg lon returns ~700 peaks / ~150 kB in the
# St. Elias, comfortably inside the limits even where OSM is densest.
TILE_LAT, TILE_LON = 4.0, 12.0

# Two summits with the same name this close together are the same summit.
# Generous because gazetteers disagree on where a peak "is" — GNIS often
# records the survey station, OSM the visual apex.
DEDUP_METRES = 600.0

# Boundary survey monuments mistagged as summits — see module docstring.
JUNK_NAME = re.compile(
    r"^\s*(unmarked\s+)?(boundary\s+(point|monument)|monument|bp)\s*[-#]?\s*\d+[a-z]?\s*$",
    re.IGNORECASE,
)

PEAK_QUERY = (
    '[out:json][timeout:250];'
    'node["natural"~"^(peak|volcano)$"]["name"]({s},{w},{n},{e});'
    'out body;'
)

# Relations carry the big parks (Wrangell-St. Elias, Kluane, Glacier Bay);
# smaller reserves are often a single closed way. "out geom" inlines each member
# way's coordinates so the rings can be stitched without a second node lookup.
PARK_QUERY = (
    '[out:json][timeout:250];'
    '('
    'relation["boundary"~"^(protected_area|national_park)$"]["name"]({s},{w},{n},{e});'
    'way["boundary"~"^(protected_area|national_park)$"]["name"]({s},{w},{n},{e});'
    'relation["leisure"="nature_reserve"]["name"]({s},{w},{n},{e});'
    ');'
    'out geom;'
)


# ------------------------------------------------------------------ fetching ---
def download(url, path, label):
    """Cache-aware GET. Big national datasets are re-fetched only if missing."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  {label}: cached")
        return path
    print(f"  {label}: downloading", flush=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "landslide-groundtruth/1.0 (QGIS template build)"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return path


# Overpass signals a timeout or memory exhaustion by adding a "remark" to an
# otherwise well-formed 200 response that carries PARTIAL results. Silently
# accepting one costs a few hundred features with no error anywhere — two
# identical sweeps of Alaska came back 3639 and 2932 before this was checked.
PARTIAL = re.compile(r"timed out|out of memory|runtime error", re.IGNORECASE)


def _overpass_once(url, query):
    """One POST. Raises on anything short of a complete, parseable answer."""
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "landslide-groundtruth/1.0 (QGIS template build)"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    remark = result.get("remark", "")
    if remark and PARTIAL.search(remark):
        raise RuntimeError(f"partial result: {remark.strip()[:120]}")
    return result


def overpass(query, attempts=3):
    """POST a query, rotating mirrors, returning the first non-empty answer.

    An empty answer is only believed once a second mirror independently agrees.
    Most tiles in an Alaska bbox are ocean and legitimately return nothing, so
    empty cannot simply be treated as failure — but neither can it be trusted
    from a single mirror, because a mirror serving a regional extract reports
    "no data here" and "nothing exists here" identically. Confirming costs one
    extra query per genuinely empty tile.
    """
    last, empty_hosts, empty_result = None, set(), None
    for attempt in range(attempts):
        for url in OVERPASS:
            host = urllib.parse.urlsplit(url).netloc
            if host in empty_hosts:
                continue                       # already answered empty
            try:
                result = _overpass_once(url, query)
            except (urllib.error.URLError, json.JSONDecodeError,
                    OSError, RuntimeError) as exc:
                last = f"{host}: {exc}"
                print(f"      retrying — {last}", flush=True)
                time.sleep(3)
                continue
            if result.get("elements"):
                return result
            empty_hosts.add(host)
            empty_result = result
            if len(empty_hosts) >= 2:
                return result                  # two mirrors agree: really empty
        time.sleep(5 * (attempt + 1))

    if empty_result is not None:
        # Only one mirror ever answered, and it said empty. Take it, but say so
        # — this is exactly the shape the osm.ch data loss had.
        print(f"      WARNING: unconfirmed empty result from "
              f"{', '.join(empty_hosts)} — no second mirror responded", flush=True)
        return empty_result
    raise RuntimeError(f"Overpass failed after {attempts} rounds — last: {last}")


def tiles(bbox):
    s, w, n, e = bbox
    lat = s
    while lat < n:
        lon = w
        while lon < e:
            yield (lat, lon, min(lat + TILE_LAT, n), min(lon + TILE_LON, e))
            lon += TILE_LON
        lat += TILE_LAT


def in_bbox(bbox, lon, lat):
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


# --------------------------------------------------------------------- peaks ---
def parse_ele(raw):
    """OSM ele is metres but is written loosely — '1234', '1234 m', '1,234'."""
    if not raw:
        return None
    m = re.match(r"\s*(-?[\d,]+(?:\.\d+)?)", str(raw).replace(",", ""))
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if -500.0 <= val <= 9000.0 else None


def peak_feature(name, lon, lat, ele, kind, source):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "name": name,
            "kind": kind,
            "source": source,
            "ele_m": round(ele) if ele is not None else None,
            "ele_ft": round(ele * 3.280839895) if ele is not None else None,
            # Pre-built so the label expression in the .qml stays trivial and
            # the same string is reusable in a print layout table.
            "label": f"{name}\n{round(ele):,} m" if ele is not None else name,
        },
    }


def fetch_osm_peaks(bbox):
    seen, features, junk = set(), [], 0
    todo = list(tiles(bbox))
    for i, (s, w, n, e) in enumerate(todo, 1):
        elements = overpass(PEAK_QUERY.format(s=s, w=w, n=n, e=e)).get("elements", [])
        for el in elements:
            if el.get("id") in seen:
                continue                       # tile edges overlap on the boundary
            seen.add(el.get("id"))
            tags = el.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name or JUNK_NAME.match(name):
                junk += 1
                continue
            features.append(peak_feature(
                name, el["lon"], el["lat"], parse_ele(tags.get("ele")),
                tags.get("natural", "peak"), "osm"))
        # Per-tile counts, so a tile that comes back short is visible in the log
        # rather than just quietly shrinking the total.
        print(f"    tile {i}/{len(todo)}  {s:.0f}..{n:.0f}N {w:.0f}..{e:.0f}E"
              f"  {len(elements):>5} raw", flush=True)
        time.sleep(2)                          # be polite to a free public API
    print(f"    -> {len(features)} peaks ({junk} survey monuments dropped)")
    return features


def fetch_gnis_peaks(bbox, stage, states):
    """USGS Domestic Names: pipe-delimited, one file per state, no elevation."""
    features = []
    for state in states:
        zpath = download(GNIS_URL.format(state=state),
                         os.path.join(stage, f"gnis_{state}.zip"), f"GNIS {state}")
        with zipfile.ZipFile(zpath) as zf:
            inner = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            with zf.open(inner) as fh:
                header = fh.readline().decode("utf-8-sig").rstrip("\n").split("|")
                cols = {name: i for i, name in enumerate(header)}
                for raw in fh:
                    row = raw.decode("utf-8", "replace").rstrip("\n").split("|")
                    if len(row) <= cols["prim_long_dec"]:
                        continue
                    if row[cols["feature_class"]] != "Summit":
                        continue
                    try:
                        lon = float(row[cols["prim_long_dec"]])
                        lat = float(row[cols["prim_lat_dec"]])
                    except ValueError:
                        continue               # a few records carry no coordinate
                    name = row[cols["feature_name"]].strip()
                    if not name or not in_bbox(bbox, lon, lat):
                        continue
                    features.append(peak_feature(name, lon, lat, None, "peak", "gnis"))
    print(f"    -> {len(features)} peaks")
    return features


def fetch_cgndb_peaks(bbox, stage, provs, ogr, env):
    """NRCan Canadian Geographical Names: a shapefile per province, no elevation.
    Converted through ogr2ogr rather than parsed, so the NAD83(CSRS) source CRS
    is handled properly instead of being assumed equal to WGS 84."""
    s, w, n, e = bbox
    features = []
    for prov in provs:
        zpath = download(CGNDB_URL.format(prov=prov),
                         os.path.join(stage, f"cgn_{prov}.zip"), f"CGNDB {prov}")
        shp_dir = os.path.join(stage, f"cgn_{prov}")
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(shp_dir)
        shp = next((os.path.join(root, f)
                    for root, _, files in os.walk(shp_dir)
                    for f in files if f.lower().endswith(".shp")), None)
        if shp is None:
            print(f"    CGNDB {prov}: no shapefile in archive, skipped")
            continue
        gj = os.path.join(stage, f"cgn_{prov}.geojson")
        if os.path.exists(gj):
            os.remove(gj)
        run([ogr, "-f", "GeoJSON", gj, shp, "-t_srs", "EPSG:4326",
             "-where", "CONCISE='MTN'", "-spat", str(w), str(s), str(e), str(n)], env)
        with open(gj, encoding="utf-8") as fh:
            for feat in json.load(fh)["features"]:
                lon, lat = feat["geometry"]["coordinates"][:2]
                name = (feat["properties"].get("GEONAME") or "").strip()
                if name:
                    features.append(peak_feature(name, lon, lat, None, "peak", "cgndb"))
    print(f"    -> {len(features)} peaks")
    return features


def norm_name(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def near(a, b):
    """Equirectangular distance test — exact enough at a 600 m threshold, and
    it avoids a trig call per candidate pair."""
    (lon1, lat1), (lon2, lat2) = a, b
    mid = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * 111320.0 * math.cos(mid)
    dy = (lat2 - lat1) * 110540.0
    return dx * dx + dy * dy <= DEDUP_METRES * DEDUP_METRES


def merge_peaks(*groups):
    """Concatenate the sources, dropping a peak whose name already exists within
    DEDUP_METRES. Sources are merged in the order given, so pass OSM first — it
    is the only one carrying elevation and should win every tie."""
    kept, by_name, dropped = [], {}, 0
    for group in groups:
        for feat in group:
            key = norm_name(feat["properties"]["name"])
            here = feat["geometry"]["coordinates"]
            if any(near(here, prev) for prev in by_name.get(key, ())):
                dropped += 1
                continue
            by_name.setdefault(key, []).append(here)
            kept.append(feat)
    print(f"  merged -> {len(kept)} peaks ({dropped} cross-source duplicates)")
    return kept


# ---------------------------------------------------------- protected areas ---
def assemble_rings(segments):
    """Chain open way-segments end-to-end into closed rings.

    Overpass emits the exact same float coordinates for a node shared by two
    ways, so endpoints are matched by equality rather than by a tolerance.
    Segments that never close (a park clipped by the query bbox) are discarded —
    an unclosed ring is not a valid polygon.
    """
    rings, pool = [], [list(seg) for seg in segments if len(seg) >= 2]
    while pool:
        cur = pool.pop(0)
        joined = True
        while joined and cur[0] != cur[-1]:
            joined = False
            for i, seg in enumerate(pool):
                if seg[0] == cur[-1]:
                    cur.extend(seg[1:])
                elif seg[-1] == cur[-1]:
                    cur.extend(reversed(seg[:-1]))
                elif seg[-1] == cur[0]:
                    cur[:0] = seg[:-1]
                elif seg[0] == cur[0]:
                    cur[:0] = list(reversed(seg[1:]))
                else:
                    continue
                pool.pop(i)
                joined = True
                break
        if cur[0] == cur[-1] and len(cur) >= 4:
            rings.append(cur)
    return rings


def point_in_ring(pt, ring):
    """Ray casting. Used only to decide which outer ring owns an inner ring."""
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def build_polygon(outers, inners):
    """MultiPolygon coordinates: each outer ring plus the inner rings it
    contains. Assignment is by first vertex, which is sound here because OSM
    park relations do not nest one park's hole inside another's outer ring."""
    parts = [[o] for o in outers]
    for inner in inners:
        for part in parts:
            if point_in_ring(inner[0], part[0]):
                part.append(inner)
                break
    return parts


def fetch_parks(bbox):
    features, seen = [], set()
    todo = list(tiles(bbox))
    for i, (s, w, n, e) in enumerate(todo, 1):
        print(f"    tile {i}/{len(todo)}  {s:.0f}..{n:.0f}N {w:.0f}..{e:.0f}E",
              flush=True)
        for el in overpass(PARK_QUERY.format(s=s, w=w, n=n, e=e)).get("elements", []):
            key = (el["type"], el["id"])
            if key in seen:
                continue
            seen.add(key)
            tags = el.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name:
                continue

            if el["type"] == "way":
                coords = [[p["lon"], p["lat"]] for p in el.get("geometry") or []]
                parts = [[r] for r in assemble_rings([coords])]
            else:
                outer_segs, inner_segs = [], []
                for member in el.get("members", []):
                    geom = member.get("geometry")
                    if member.get("type") != "way" or not geom:
                        continue
                    seg = [[p["lon"], p["lat"]] for p in geom]
                    (inner_segs if member.get("role") == "inner"
                     else outer_segs).append(seg)
                parts = build_polygon(assemble_rings(outer_segs),
                                      assemble_rings(inner_segs))
            if not parts:
                continue                       # nothing closed — clipped by bbox

            features.append({
                "type": "Feature",
                "geometry": {"type": "MultiPolygon", "coordinates": parts},
                "properties": {
                    "name": name,
                    "kind": tags.get("boundary") or tags.get("leisure") or "",
                    "operator": tags.get("operator", ""),
                    "osm_id": el["id"],
                },
            })
        time.sleep(2)
    print(f"    -> {len(features)} protected areas")
    return features


# ------------------------------------------------------------------ ogr2ogr ---
def find_ogr2ogr():
    """QGIS bundles its own GDAL; prefer it so PROJ/GDAL data dirs are known."""
    bundled = "/Applications/QGIS-LTR.app/Contents/MacOS/ogr2ogr"
    for path in (bundled, bundled.replace("QGIS-LTR", "QGIS"), shutil.which("ogr2ogr")):
        if path and os.path.exists(path):
            return path
    sys.exit("ogr2ogr not found — install QGIS or GDAL, or add ogr2ogr to PATH.")


def ogr_env(ogr):
    """ogr2ogr from the QGIS bundle needs GDAL_DATA/PROJ_LIB pointed at the
    bundle's Resources, otherwise every call warns 'Cannot find proj.db'."""
    env = dict(os.environ)
    resources = os.path.normpath(
        os.path.join(os.path.dirname(ogr), "..", "Resources", "qgis"))
    if os.path.isdir(os.path.join(resources, "proj")):
        env.setdefault("GDAL_DATA", os.path.join(resources, "gdal"))
        env.setdefault("PROJ_LIB", os.path.join(resources, "proj"))
    # The GeoJSON reader rejects any single feature above a default size cap.
    # Alaska's larger protected areas blow straight past it — Wrangell-St. Elias
    # alone is tens of MB of coordinates — so the cap comes off.
    env.setdefault("OGR_GEOJSON_MAX_OBJ_SIZE", "0")
    return env


def run(cmd, env):
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"ogr2ogr failed:\n{' '.join(cmd)}\n{proc.stderr}")
    for line in proc.stderr.splitlines():
        if "ERROR" in line or ("Warning" in line and "not normally allowed" not in line):
            print(f"    {line}")


def cached_geojson(path, reuse, build):
    """Return the features in a staging GeoJSON, building and writing it first
    unless a cached copy is being reused. Only the OSM sweeps are worth caching
    — they are the twelve-minute half; the gazetteers are a few seconds each."""
    if reuse and os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"    reusing cached {os.path.basename(path)}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["features"]
    features = build()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
    return features


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"),
                    default=DEFAULT_BBOX, help="region to cover (default: AK + YT)")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/Library/Application Support/QGIS/QGIS3/profiles/default/project_templates"),
        help="directory to write the .gpkg into (default: the QGIS template folder)")
    ap.add_argument("--name", default="landslide_reference.gpkg")
    ap.add_argument("--gnis-states", default="AK",
                    help="comma-separated USGS state codes (default: AK)")
    ap.add_argument("--cgndb-provs", default="yt,bc",
                    help="comma-separated NRCan province codes (default: yt,bc)")
    ap.add_argument("--park-simplify", type=float, default=0.0002,
                    help="park outline simplify tolerance in degrees (~22 m); "
                         "0 keeps every vertex and a ~100 MB layer")
    ap.add_argument("--skip-parks", action="store_true",
                    help="peaks and borders only (parks are the slow half)")
    ap.add_argument("--reuse-cache", action="store_true",
                    help="reuse staged downloads instead of re-querying")
    ap.add_argument("--keep-cache", action="store_true",
                    help="keep <out>/_staging so the next run can --reuse-cache")
    args = ap.parse_args()

    s, w, n, e = args.bbox
    out_dir = os.path.expanduser(args.out)
    os.makedirs(out_dir, exist_ok=True)
    gpkg = os.path.join(out_dir, args.name)
    stage = os.path.join(out_dir, "_staging")
    os.makedirs(stage, exist_ok=True)

    ogr = find_ogr2ogr()
    env = ogr_env(ogr)
    print(f"region  {s}..{n} N, {w}..{e} E")
    print(f"ogr2ogr {ogr}")
    print(f"output  {gpkg}\n")

    if os.path.exists(gpkg):
        os.remove(gpkg)                        # -overwrite can't drop a stale layer set

    print("Natural Earth boundaries")
    for source, layer in NE_LAYERS:
        run([ogr, "-f", "GPKG", gpkg,
             f"/vsizip//vsicurl/{NE_BASE}/{source}.zip",
             "-nln", layer, "-nlt", "MULTILINESTRING",
             "-t_srs", "EPSG:4326", "-clipsrc", str(w), str(s), str(e), str(n),
             "-update" if os.path.exists(gpkg) else "-overwrite"], env)
        print(f"  -> {layer}")

    print("\nPeaks")
    print("  OSM (Overpass)")
    osm = cached_geojson(os.path.join(stage, "osm_peaks.geojson"),
                         args.reuse_cache, lambda: fetch_osm_peaks(args.bbox))
    print("  GNIS (USGS)")
    gnis = fetch_gnis_peaks(
        args.bbox, stage,
        [c.strip().upper() for c in args.gnis_states.split(",") if c.strip()])
    print("  CGNDB (NRCan)")
    cgndb = fetch_cgndb_peaks(
        args.bbox, stage,
        [c.strip().lower() for c in args.cgndb_provs.split(",") if c.strip()],
        ogr, env)
    peaks_path = os.path.join(stage, "peaks.geojson")
    with open(peaks_path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection",
                   "features": merge_peaks(osm, gnis, cgndb)}, fh)
    run([ogr, "-f", "GPKG", gpkg, peaks_path, "-nln", "peaks", "-nlt", "POINT",
         "-a_srs", "EPSG:4326", "-update"], env)

    if not args.skip_parks:
        print("\nProtected areas (OSM)")
        parks_path = os.path.join(stage, "protected_areas.geojson")
        # Not routed through cached_geojson: the park GeoJSON is ~100 MB and
        # ogr2ogr reads it straight off disk, so there is no reason to parse it
        # back into python just to hand the path along.
        if args.reuse_cache and os.path.exists(parks_path):
            print(f"    reusing cached {os.path.basename(parks_path)}")
        else:
            with open(parks_path, "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection",
                           "features": fetch_parks(args.bbox)}, fh)
        cmd = [ogr, "-f", "GPKG", gpkg, parks_path, "-nln", "protected_areas",
               "-nlt", "MULTIPOLYGON", "-a_srs", "EPSG:4326",
               "-clipsrc", str(w), str(s), str(e), str(n), "-update"]
        if args.park_simplify > 0:
            # Park outlines are context, not measurement targets. Full OSM
            # detail is ~100 MB and invisible at any scale this template is
            # used at; ~22 m keeps the shape and drops most of the vertices.
            cmd += ["-simplify", str(args.park_simplify)]
        run(cmd, env)

    if not args.keep_cache:
        shutil.rmtree(stage, ignore_errors=True)
    print(f"\nDone: {gpkg} ({os.path.getsize(gpkg) / 1e6:.1f} MB)")
    subprocess.run([ogr.replace("ogr2ogr", "ogrinfo"), "-so", gpkg], env=env)


if __name__ == "__main__":
    main()
