"""Shared cache + ledger of PlanetScope orders already paid for, so a scene is
ordered ONCE and every later look at it is free.

Planet quota is spent when an order is CREATED, not when its files are fetched:
re-downloading an order that already exists in the account costs nothing, and
re-reading clips already on disk costs nothing and needs no network at all.
Everything here exists to make the second look at an event use one of those two
free paths instead of placing a new order.

Layout — deliberately OUTSIDE any project tree, so a different --out, a second
project on the same event, or a fresh clone all share one pool (and so
multi-hundred-MB GeoTIFFs don't sync through Google Drive):

    $LANDSLIDE_PLANET_CACHE  or  ~/.cache/landslide_planet/
        ledger.json              one record per order (see `record`)
        orders/<order_id>/       the order tree exactly as Planet delivers it

ledger.json is the index. It maps event / AOI / scene-ids -> order id, which is
what lets a recall answer "what have I already ordered here?" without listing the
Planet account, and lets it re-download (free) an order whose local files were
deleted rather than re-order it (not free).

Nothing in this module imports the Planet SDK, rasterio or xarray: it is pure
path/JSON bookkeeping, so the plugin and the CLI can read the ledger without the
heavy imagery stack, and a recall that hits on-disk clips never touches the network.
"""
from __future__ import annotations
import datetime as dt
import glob
import json
import math
import os

LEDGER_VERSION = 1
ENV_ROOT = "LANDSLIDE_PLANET_CACHE"

# Directory names the pipeline used for per-project Planet downloads before this
# shared cache existed: <out>/planet_cache/<event_id>/<side>/<order_id>/ (batch) and
# <out>/planet_render/<event_id>/<side>/<order_id>/ (plugin renders). `adopt` scans
# for these and registers what it finds IN PLACE, so orders you already paid for
# become recallable without re-downloading or moving a single byte.
LEGACY_PARENTS = ("planet_cache", "planet_render")

# How far the AOI of a cached order may sit from a requested AOI and still count as
# "the same place". PlanetScope AOIs here are a few km across and the epicentre is
# typed to 4-6 decimals, so a 1 km slop absorbs re-typed/rounded coordinates without
# matching a genuinely different slide.
DEFAULT_TOL_KM = 1.0


def cache_root():
    """The shared cache directory, created if needed. $LANDSLIDE_PLANET_CACHE wins."""
    root = os.environ.get(ENV_ROOT) or os.path.join(
        os.path.expanduser("~"), ".cache", "landslide_planet")
    os.makedirs(root, exist_ok=True)
    return root


def order_dir(order_id, create=False):
    """Where a given order's files live in the shared cache."""
    d = os.path.join(cache_root(), "orders", str(order_id))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def ledger_path():
    return os.path.join(cache_root(), "ledger.json")


def load():
    """Every ledger record, newest order first. Missing/corrupt ledger -> []."""
    try:
        with open(ledger_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    recs = data.get("orders", []) if isinstance(data, dict) else list(data)
    return sorted((r for r in recs if isinstance(r, dict) and r.get("order_id")),
                  key=lambda r: r.get("created_utc") or "", reverse=True)


def _save(recs):
    """Write the ledger atomically: a half-written index would strand paid orders."""
    path = ledger_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": LEDGER_VERSION, "orders": recs}, f, indent=2,
                  default=str)
    os.replace(tmp, path)


def record(order_id, path, side=None, event_id=None, lat=None, lon=None,
           radius_km=None, bbox=None, scene_ids=None, bundle=None, source="order",
           created_utc=None):
    """Upsert one order into the ledger and return the stored record.

    Keyed on order_id, so re-recording an order (e.g. after a resumed download
    completes it) updates the entry instead of duplicating it. `path` is the
    directory the order's files were unpacked into — absolute, and allowed to live
    outside the cache root so `adopt` can register pre-existing project downloads
    where they already are.

    Two different geometries are stored on purpose:
      radius_km - the AOI radius the order was REQUESTED for, when known. Only the
                  request tells you whether a wider AOI would deliver more pixels, so
                  this is what the "is the cached clip big enough?" test uses.
      lat/lon/bbox - what was actually DELIVERED (see aoi_from_disk). This is what the
                  "is this the same place?" test uses, and it's all an adopted order
                  can know about itself.
    """
    recs = [r for r in load() if r.get("order_id") != order_id]
    rec = dict(
        order_id=str(order_id),
        path=os.path.abspath(path) if path else None,
        side=side,
        event_id=event_id,
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        radius_km=float(radius_km) if radius_km is not None else None,
        bbox=[float(v) for v in bbox] if bbox else None,
        scene_ids=sorted(set(scene_ids or [])),
        bundle=bundle,
        source=source,
        created_utc=created_utc or dt.datetime.utcnow().isoformat(timespec="seconds"),
    )
    recs.append(rec)
    _save(recs)
    return rec


def forget(order_id):
    """Drop an order from the ledger (leaves any files alone)."""
    recs = load()
    kept = [r for r in recs if r.get("order_id") != order_id]
    if len(kept) != len(recs):
        _save(kept)
    return len(recs) - len(kept)


def resolve_path(rec):
    """The best guess at where this order's files are: the recorded path if it still
    holds tifs, else the shared-cache location for the id (covers a ledger written
    before the files were moved into the cache, or a re-download after a cleanup)."""
    p = rec.get("path")
    if p and scene_tifs(p):
        return p
    d = order_dir(rec["order_id"])
    if scene_tifs(d):
        return d
    return p or d


def scene_tifs(path):
    """Every GeoTIFF under a downloaded order tree (SR clips and UDM2 masks)."""
    if not path or not os.path.isdir(path):
        return []
    return sorted(glob.glob(os.path.join(path, "**", "*.tif"), recursive=True))


def has_files(rec):
    """True when this order's clips are still on disk (a free, offline recall)."""
    return bool(scene_tifs(resolve_path(rec)))


def scene_ids_on_disk(path):
    """PSScene ids present in a downloaded order tree, read from the file names.

    Planet clip deliveries name files '<item_id>_3B_AnalyticMS_SR_clip.tif', so the
    item id is the part before '_3B'. Same convention planet_imagery._pair_downloads
    keys on; duplicated here (rather than imported) to keep this module free of the
    imagery stack.
    """
    ids = set()
    for t in scene_tifs(path):
        b = os.path.basename(t)
        ids.add(b.split("_3B")[0] if "_3B" in b else os.path.splitext(b)[0])
    return sorted(ids)


def _flatten_coords(o, out):
    """Collect every [lon, lat] pair out of any GeoJSON coordinates nesting."""
    if isinstance(o, (list, tuple)):
        if len(o) >= 2 and all(isinstance(v, (int, float)) for v in o[:2]):
            out.append((float(o[0]), float(o[1])))
        else:
            for v in o:
                _flatten_coords(v, out)


def aoi_from_disk(path):
    """What an order actually DELIVERED, read from its own files, or None:
    {'lat', 'lon', 'bbox': (w, s, e, n)} — the centre and lon/lat bounds of the clip.

    Planet writes the clipped footprint into each <item>_metadata.json's geometry, so a
    downloaded order tree knows where it is even when the ledger doesn't. Pure JSON, no
    rasterio, which keeps this module importable anywhere (including QGIS's Python).

    This is what makes matching safe: the default event id is derived from the event
    TIMESTAMP and is not location-unique, so without a footprint two unrelated slides
    recorded at the same minute would look like the same event.

    Note this is the DELIVERED clip, not the AOI that was asked for. A PlanetScope strip
    covering only part of the AOI clips to the intersection, so the delivered box can be
    smaller than — and off-centre from — the requested one. That's why the two are kept
    as separate ledger fields (`bbox` vs `radius_km`); see `find`."""
    pts = []
    for m in sorted(glob.glob(os.path.join(path or "", "**", "*_metadata.json"),
                              recursive=True)):
        try:
            with open(m) as f:
                geom = (json.load(f) or {}).get("geometry") or {}
        except (OSError, ValueError, AttributeError):
            continue
        _flatten_coords(geom.get("coordinates"), pts)
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return dict(lat=(min(lats) + max(lats)) / 2, lon=(min(lons) + max(lons)) / 2,
                bbox=(min(lons), min(lats), max(lons), max(lats)))


def _bbox_of(lat, lon, radius_km=0.0):
    """(w, s, e, n) around a point, matching imagery._bbox's flat-degree convention."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _overlaps(a, b, tol_km=DEFAULT_TOL_KM):
    """Do two (w, s, e, n) boxes intersect, allowing `tol_km` of slop?

    Overlap — not centre-to-centre distance — is the right "same place?" test, because a
    PlanetScope strip that covers only part of the AOI delivers a clip whose centre can
    sit many km from the event while still being exactly the imagery ordered for it."""
    lat = ((a[1] + a[3]) + (b[1] + b[3])) / 4
    dlat = tol_km / 111.32
    dlon = tol_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    return not (a[0] - dlon > b[2] or a[2] + dlon < b[0]
                or a[1] - dlat > b[3] or a[3] + dlat < b[1])


def _contains_point(box, lon, lat):
    """Does (w, s, e, n) `box` bracket the point? The strict "covers the epicentre"
    test — the delivered clip's footprint must actually surround the exact event
    location, vs `_overlaps`, which passes any clip merely touching the AOI box. A
    PlanetScope strip that clipped to one side of the AOI overlaps but does NOT contain
    the epicentre, so this drops it where `_overlaps` would keep it."""
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def record_bbox(rec):
    """(w, s, e, n) of what a record delivered: its stored bbox, else its centre."""
    if rec.get("bbox"):
        return tuple(rec["bbox"])
    if rec.get("lat") is not None and rec.get("lon") is not None:
        return _bbox_of(rec["lat"], rec["lon"], rec.get("radius_km") or 0.0)
    return None


def find(event_id=None, lat=None, lon=None, side=None, scene_ids=None,
         radius_km=None, require_radius_km=None, tol_km=DEFAULT_TOL_KM,
         require_files=False, require_point=False):
    """Ledger records for one event/AOI, newest first.

    Match rules:
      lat/lon   - LOCATION WINS. Whenever both the query and the record know where they
                  are, the record's delivered footprint must OVERLAP the query box (see
                  _overlaps) — even if the event ids agree. The default event id comes
                  from the event TIMESTAMP and is not location-unique, so trusting it
                  over geometry would happily load an unrelated slide's imagery.
                  radius_km sizes the query box (omit it and the query is the point plus
                  `tol_km` of slop).
      event_id  - the fallback for a record whose delivery carried no footprint: the
                  event ids must then match exactly. Such a record is never matched on
                  location alone, since its location is unknown.
      side      - 'pre'/'post' filter; records with no side recorded are kept, since
                  an adopted directory may not say which side it was.
      scene_ids - require the record to CONTAIN every requested scene id. This is the
                  strict test used before deciding not to place an order for specific
                  hand-picked scenes.
      require_radius_km - require the order to have been REQUESTED for an AOI at least
                  this wide. Only a caller deciding whether to spend quota should pass
                  this: it's the one case where a wider box could genuinely deliver more
                  pixels. It deliberately tests the requested radius, not the delivered
                  footprint — a strip that only partly covered the AOI delivered all it
                  ever will, so re-ordering it would buy identical pixels twice.
    require_point: with a query lat/lon, require the record's delivered footprint to
                  CONTAIN that point (see _contains_point), not merely overlap the AOI
                  box. The strict "covers the exact epicentre" filter — mirrors the
                  search side's --coverage point — used by the recall picker so a strip
                  that clipped to one edge of the AOI, missing the event, is dropped.
    require_files: keep only records whose clips are still on disk (an offline recall).
    """
    want = set(scene_ids or [])
    have_query_aoi = lat is not None and lon is not None
    query_box = _bbox_of(lat, lon, radius_km or 0.0) if have_query_aoi else None
    out = []
    for r in load():
        if side and r.get("side") and r["side"] != side:
            continue
        if want and not want.issubset(set(r.get("scene_ids") or [])):
            continue
        if require_radius_km is not None and r.get("radius_km") is not None \
                and r["radius_km"] + 1e-9 < require_radius_km:
            continue
        rec_box = record_bbox(r)
        if query_box and rec_box:
            if require_point:
                if not _contains_point(rec_box, lon, lat):
                    continue
            elif not _overlaps(query_box, rec_box, tol_km):
                continue
        elif event_id:
            if r.get("event_id") != event_id:
                continue
        elif query_box:
            continue        # we know where we are, the record doesn't — don't guess
        if require_files and not has_files(r):
            continue
        out.append(r)
    return out


def newest_by_side(recs):
    """{'pre': rec, 'post': rec} keeping the newest record per side.

    Records with no side recorded fill a side only if nothing else claimed it, so an
    adopted directory of unknown side is a fallback rather than a hijack. `recs` is
    expected newest-first (what `find` returns)."""
    out = {}
    for r in recs:
        s = r.get("side")
        if s in ("pre", "post") and s not in out:
            out[s] = r
    for r in recs:
        if r.get("side") in ("pre", "post"):
            continue
        for s in ("pre", "post"):
            if s not in out:
                out[s] = r
                break
    return out


def _scene_date(sid):
    """'20260625_215817_62_2538' -> '2026-06-25'; None if not date-prefixed.

    A PlanetScope scene id begins with its acquisition date, which is how the imagery is
    named everywhere the user sees it (layer names, the candidate table) — so it, not the
    order-placement time, is what makes an order recognisable in the picker."""
    d = (sid or "")[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None


def describe(rec):
    """One-line human summary for logs and the plugin's order picker.

    Leads with the SCENE acquisition date(s), NOT the order-placement time: that date is
    what the layer names and candidate table show, so it is how the user matches an order
    to imagery they already have. The narrow picker elides the MIDDLE of the label, so a
    leading scene date and the trailing order-id/disk status stay visible while the raw
    scene ids between them are what get truncated (they were the old label's lead, which
    made an order read as its 2026-08-19 order date instead of its 2026-06-25 scene)."""
    ids = rec.get("scene_ids") or []
    dates = sorted({d for d in (_scene_date(s) for s in ids) if d})
    if not dates:
        head = "ordered " + (rec.get("created_utc") or "?")[:10]
    elif len(dates) == 1:
        head = dates[0]
    else:
        head = f"{dates[0]}…{dates[-1]} ({len(dates)})"
    scenes = ", ".join(ids[:2]) + (f" +{len(ids) - 2}" if len(ids) > 2 else "")
    disk = "on disk" if has_files(rec) else "re-download (free)"
    return (f"{rec.get('side') or '?'} · {head} · {scenes or 'scenes unknown'} · "
            f"order {rec['order_id'][:12]} · {disk}")


def entries(event_id=None, lat=None, lon=None, radius_km=None,
            tol_km=DEFAULT_TOL_KM, require_point=False):
    """Serializable ledger listing for the plugin's picker / --planet-list-orders.

    With no event_id/lat/lon the whole ledger is returned, so you can see everything
    the account has already paid for across every project. require_point restricts a
    located query to orders whose delivered footprint contains the epicentre (see find)."""
    recs = (find(event_id=event_id, lat=lat, lon=lon, radius_km=radius_km,
                 tol_km=tol_km, require_point=require_point)
            if (event_id or (lat is not None and lon is not None)) else load())
    return [dict(r, path=resolve_path(r), on_disk=has_files(r), label=describe(r))
            for r in recs]


def adopt(roots, log=print):
    """Register pre-existing per-project Planet downloads into the ledger, in place.

    Walks each root for the legacy layouts (<...>/planet_cache/<event_id>/<side>/
    <order_id>/ and the same under planet_render/) and records every order directory
    that still holds clips, reading each one's AOI out of its own delivered metadata so
    it can be matched by location afterwards. Nothing is moved, copied or deleted —
    these are orders already paid for, so the only goal is making them findable.

    An order already in the ledger is skipped, except that a record still missing its
    AOI gets one backfilled from disk — so re-running this repairs entries adopted
    before the footprint was being read. Safe (and cheap) to call on every recall.

    Returns the number of orders newly adopted.
    """
    known = {r["order_id"]: r for r in load()}
    added = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            if os.path.basename(dirpath) not in LEGACY_PARENTS:
                continue
            events = list(dirnames)
            dirnames[:] = []            # handled manually below; don't re-walk it
            for event_id in events:
                for side in ("pre", "post"):
                    side_dir = os.path.join(dirpath, event_id, side)
                    if not os.path.isdir(side_dir):
                        continue
                    for order_id in sorted(os.listdir(side_dir)):
                        od = os.path.join(side_dir, order_id)
                        if not os.path.isdir(od):
                            continue
                        prev = known.get(order_id)
                        if prev is not None:
                            if not prev.get("bbox"):
                                aoi = aoi_from_disk(od)
                                if aoi:
                                    known[order_id] = record(
                                        order_id, prev.get("path") or od,
                                        side=prev.get("side") or side,
                                        event_id=prev.get("event_id") or event_id,
                                        lat=aoi["lat"], lon=aoi["lon"],
                                        bbox=aoi["bbox"],
                                        radius_km=prev.get("radius_km"),
                                        scene_ids=prev.get("scene_ids"),
                                        bundle=prev.get("bundle"),
                                        source=prev.get("source"),
                                        created_utc=prev.get("created_utc"))
                            continue
                        ids = scene_ids_on_disk(od)
                        if not ids:
                            continue
                        aoi = aoi_from_disk(od) or {}
                        # mtime, not now(): keeps adopted orders in true age order
                        # against ones recorded at download time.
                        stamp = dt.datetime.utcfromtimestamp(
                            os.path.getmtime(od)).isoformat(timespec="seconds")
                        # radius_km stays None: the directory says what was delivered,
                        # never how wide an AOI was asked for (see `record`).
                        known[order_id] = record(
                            order_id, od, side=side, event_id=event_id,
                            lat=aoi.get("lat"), lon=aoi.get("lon"),
                            bbox=aoi.get("bbox"), scene_ids=ids, source="adopted",
                            created_utc=stamp)
                        added += 1
                        where = (f" at {aoi['lat']:.4f},{aoi['lon']:.4f}"
                                 if aoi.get("lat") is not None else "")
                        log(f"    [planet-cache] adopted {side} order "
                            f"{order_id[:12]} ({len(ids)} scene(s)) for "
                            f"{event_id}{where}")
    return added


def stamp_footprint(order_id, event_id=None):
    """Backfill a record's delivered footprint from its files, so it can be matched by
    location from then on.

    Only ever reads the order's OWN delivery — never the AOI it happens to be loaded
    for. The delivery is evidence; the AOI of whoever recalled it is a guess, and a
    wrong guess here would silently bind the record to the wrong site. A record that
    already has a bbox is left alone."""
    for r in load():
        if r["order_id"] != order_id:
            continue
        if r.get("bbox"):
            return r
        aoi = aoi_from_disk(resolve_path(r))
        if not aoi:
            return r
        return record(order_id, r.get("path"), side=r.get("side"),
                      event_id=r.get("event_id") or event_id,
                      lat=aoi["lat"], lon=aoi["lon"], bbox=aoi["bbox"],
                      radius_km=r.get("radius_km"),
                      scene_ids=r.get("scene_ids"), bundle=r.get("bundle"),
                      source=r.get("source"), created_utc=r.get("created_utc"))
    return None
