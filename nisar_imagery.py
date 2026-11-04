"""Search NISAR L-band GCOV (SAR amplitude) granules around a landslide event.

NISAR (NASA-ISRO, launched 2025-07-30) is L-band — ~24 cm, four times
Sentinel-1's wavelength — on a 12-day exact repeat, free through ASF DAAC. Its
L2 GCOV product is terrain-corrected gamma-naught in linear power, the same
quantity as the Sentinel-1 RTC the SAR tab already differences, so the tab's
amplitude detectors apply unchanged. Measured once (Iliamna 2026-08-08, track
150 ascending): |log-ratio| HH peaks at 13.2 dB 0.1 km from the seismic point,
~3x the scene background. One event — L-band HH is not yet a validated product
the way C-band VV is, which is why the tab labels every NISAR layer as such.
Amplitude only: GUNW coherence was measured and fails on this glaciated
terrain (NISAR_ASSESSMENT.md §11).

This module is the SEARCH half and runs in the venv like sar_imagery: NASA CMR,
public, no login. The pixels are read later, in QGIS, by
qgis_plugin/landslide_groundtruth/nisar_read.py — that half needs Earthdata
Login and GDAL's multidimensional API, which the venv does not have.

Availability is not what the collection metadata says. Both GCOV collections
advertise "2025-10-01 -> ongoing"; measured 2026-09-16, beta holds 2025-10-17 to
2026-01-20, provisional is sparse until 2026-06-17, and nothing at all was
acquired anywhere 2026-07-28 to 2026-08-09. So the only decision made from
dates here is the permissive one — a window that ends before first science is
not searched — and the granule search itself is the availability test. The hole
table only shapes the EXPLANATION of an empty side, never whether to search,
so a stale table degrades into a slightly unhelpful note rather than a wrongly
refused search while ASF backfills the archive.

Candidates honour sar_imagery._candidate's contract (id/date/gap_days/cloud_pct/
source/thumb_url/geometry/bbox + orbit_state/relative_orbit/polarizations), so
the tab's table, gallery, footprints and pairing take them as they are, plus:
  h5_url      the granule's HTTPS HDF5 (what nisar_read opens remotely)
  browse_url  ASF's north-up browse of the whole frame (thumb_url is its 100 px
              thumbnail)
  frame/cycle/mode, collection, product ('beta' | 'provisional' | ...)
"""
from __future__ import annotations

import datetime as dt
import math
import re

import requests
from shapely.geometry import Point, shape

CMR = "https://cmr.earthdata.nasa.gov/search"
SOURCE = "NISAR"
# Fallback when collection discovery fails. Discovery (collections()) finds
# whatever NISAR_L2_GCOV* exists, so a reprocessed collection that ASF adds
# later is searched without a code change.
KNOWN_COLLECTIONS = ("NISAR_L2_GCOV_PROVISIONAL_V1", "NISAR_L2_GCOV_BETA_V1")

LAUNCH = dt.datetime(2025, 7, 30)
FIRST_SCIENCE = dt.datetime(2025, 10, 17)   # hard floor: nothing exists before
# Advisory only — they word the note for an empty side, never gate a search.
# Measured 2026-09-16; the reprocessing hole is expected to close by end of
# 2026, the instrument gap never will.
INSTRUMENT_GAP = (dt.datetime(2026, 7, 28), dt.datetime(2026, 8, 10))
REPROCESSING = (dt.datetime(2026, 1, 21), dt.datetime(2026, 6, 17))
LATENCY_DAYS = 3                            # 36-72 h from acquisition to CMR

# Same caps as sar_imagery: a point is imaged every few days once every
# covering track is counted, and the tab's multi-temporal detectors need 3+
# before-dates on ONE track, so the cap grows with the window.
DEFAULT_LIMIT = 12
MAX_LIMIT = 24

# NISAR_L2_PR_GCOV_027_150_A_032_4005_DHDH_A_20260811T142220_20260811T142259_P05023_N_F_J_001
#                  cyc trk P frm mode pols   start           end
_NAME_RE = re.compile(
    r"NISAR_L2_(?P<ptype>[A-Z]{2})_GCOV_(?P<cycle>\d{3})_(?P<track>\d{3})_"
    r"(?P<dir>[AD])_(?P<frame>\d{3})_(?P<mode>\d{4})_(?P<pols>[A-Z]{4})_"
    r"[A-Z]_(?P<start>\d{8}T\d{6})_(?P<end>\d{8}T\d{6})")
# frequency-A polarization mode (the first two letters of the pols field)
_POL_MODES = {"DH": ["HH", "HV"], "DV": ["VV", "VH"], "SH": ["HH"], "SV": ["VV"],
              "QP": ["HH", "HV", "VH", "VV"], "QD": ["HH", "HV", "VH", "VV"],
              "QQ": ["HH", "HV", "VH", "VV"]}


def _limit_for(days):
    return max(DEFAULT_LIMIT, min(MAX_LIMIT, round(days / 4)))


def applicable(event_time, pre_days, post_days):
    """Could this search window hold ANY NISAR science data? Only the floor is
    tested: a window that ends before first science is impossible, everything
    else is worth one CMR query (which is the real availability test)."""
    return event_time + dt.timedelta(days=post_days) >= FIRST_SCIENCE


def parse_name(granule_ur):
    """Decode a GCOV granule name into its fields, or None if it is not one.

    Returns dict(cycle, track, orbit_state, frame, mode, pol_mode,
    polarizations, start, end, processing). `mode` is the bandwidth code —
    '4005' = 40 MHz on frequency A (10 m posting) + 5 MHz on B; '2005' is the
    20 MHz mode — which matters when one pass was processed both ways."""
    m = _NAME_RE.search(granule_ur or "")
    if not m:
        return None
    g = m.groupdict()
    return dict(
        cycle=int(g["cycle"]), track=int(g["track"]),
        orbit_state="ascending" if g["dir"] == "A" else "descending",
        frame=int(g["frame"]), mode=g["mode"], pol_mode=g["pols"],
        polarizations=_POL_MODES.get(g["pols"][:2]),
        start=dt.datetime.strptime(g["start"], "%Y%m%dT%H%M%S"),
        end=dt.datetime.strptime(g["end"], "%Y%m%dT%H%M%S"),
        processing=g["ptype"])


def _windows_overlap(a0, a1, b0, b1):
    return max(dt.timedelta(0), min(a1, b1) - max(a0, b0))


def explain_absence(lo, hi, now=None):
    """Why a NISAR search window [lo, hi] came back empty — a sentence that says
    what to DO — or None when no archive-wide reason applies (the AOI simply
    was not imaged in that window: widen it)."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if hi < FIRST_SCIENCE:
        return (f"NISAR launched {LAUNCH:%Y-%m-%d} and began science acquisition "
                f"{FIRST_SCIENCE:%Y-%m-%d}; this window predates it, so widening "
                f"it cannot help")
    if lo >= now - dt.timedelta(days=LATENCY_DAYS):
        return ("too recent: NISAR products reach the archive 36-72 h after "
                "acquisition — search again in a few days")
    g0, g1 = INSTRUMENT_GAP
    if lo >= g0 and hi < g1:
        return (f"NISAR acquired nothing anywhere on Earth {g0:%Y-%m-%d} to "
                f"{g1 - dt.timedelta(days=1):%Y-%m-%d} (instrument gap) — widen the "
                f"window past it: before on or before "
                f"{g0 - dt.timedelta(days=1):%Y-%m-%d}, after on or after {g1:%Y-%m-%d}")
    r0, r1 = REPROCESSING
    span = max(hi - lo, dt.timedelta(seconds=1))
    if _windows_overlap(lo, hi, r0, r1) / span > 0.5:
        return (f"NISAR was observing, but {r0:%Y-%m-%d} to {r1:%Y-%m-%d} is still "
                "being reprocessed and only a sparse subset is public (ASF expects "
                "the full record by end of 2026) — search again later")
    return None


def _get(url, params, headers=None):
    """GET with one retry on a server error or timeout. CMR returns sporadic
    500s (all granule searches failed for an hour on 2026-10-19 while
    collection search worked), and a NISAR failure must cost one note, not the
    Sentinel-1 half of the same search."""
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=headers or {},
                             timeout=(10, 45))
            if r.status_code >= 500 and attempt == 0:
                last = requests.HTTPError(f"CMR HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
    raise last


def collections():
    """[(concept_id, short_name)] of every NISAR L2 GCOV collection CMR knows."""
    try:
        r = _get(f"{CMR}/collections.umm_json",
                 [("short_name", "NISAR_L2_GCOV*"),
                  ("options[short_name][pattern]", "true"),
                  ("provider", "ASF"), ("page_size", "50")])
        out = [(it["meta"]["concept-id"], it["umm"]["ShortName"])
               for it in r.json().get("items", [])]
        if out:
            return out
    except Exception:                        # noqa: BLE001 — fall back below
        pass
    return [(None, s) for s in KNOWN_COLLECTIONS]


def _granules(colls, bbox, start, end):
    """Every GCOV granule over `bbox` in [start, end], across all collections
    (beta and provisional are disjoint in time — searching one silently loses
    the other's months). Paged with CMR-Search-After."""
    params = [("bounding_box", ",".join(f"{v:.4f}" for v in bbox)),
              ("temporal", f"{start:%Y-%m-%dT%H:%M:%SZ},{end:%Y-%m-%dT%H:%M:%SZ}"),
              ("page_size", "2000")]
    by_id = {cid: name for cid, name in colls if cid}
    if by_id:
        params += [("collection_concept_id", cid) for cid in by_id]
    else:
        params += [("short_name", name) for _cid, name in colls]
    items, after = [], None
    for _page in range(10):
        r = _get(f"{CMR}/granules.umm_json", params,
                 {"CMR-Search-After": after} if after else None)
        page = r.json().get("items", [])
        for it in page:
            it["_collection"] = by_id.get(it.get("meta", {}).get(
                "collection-concept-id")) or ""
        items += page
        after = r.headers.get("CMR-Search-After")
        if not after or not page:
            break
    return items


def _geometry(umm):
    """GeoJSON footprint from UMM-G SpatialExtent, or None."""
    geo = ((umm.get("SpatialExtent") or {}).get("HorizontalSpatialDomain") or {}
           ).get("Geometry") or {}
    polys = []
    for gp in geo.get("GPolygons") or []:
        pts = (gp.get("Boundary") or {}).get("Points") or []
        ring = [[p["Longitude"], p["Latitude"]] for p in pts]
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            polys.append([ring])
    if not polys:
        for br in geo.get("BoundingRectangles") or []:
            w, s = br["WestBoundingCoordinate"], br["SouthBoundingCoordinate"]
            e, n = br["EastBoundingCoordinate"], br["NorthBoundingCoordinate"]
            polys.append([[[w, s], [e, s], [e, n], [w, n], [w, s]]])
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def _urls(umm):
    """(h5_url, thumbnail_png, browse_png) from RelatedUrls — HTTPS only (s3://
    direct access works only from inside AWS us-west-2).

    ASF lists seven browse PNGs per granule (measured 2026-10-19): _LATLON
    (north-up colour HH/HV composite of the whole frame, ~2.6 MB),
    _LATLON_thumbnail (the same at 100 px, ~10 KB) and five native-grid ones
    (_NATIVE*, polar-stereo orientation). All anonymous. The thumbnail feeds the
    gallery — a 24-tile gallery of full browses would be 60 MB — and the north-up
    browse the preview pane, so it is oriented like the map."""
    h5 = thumb = browse = other = None
    for ru in umm.get("RelatedUrls") or []:
        url = ru.get("URL") or ""
        if not url.startswith("https://"):
            continue
        name = url.rsplit("/", 1)[-1].lower()
        if name.endswith(".h5") and h5 is None:
            h5 = url
        elif name.endswith("_latlon_thumbnail.png"):
            thumb = thumb or url
        elif name.endswith("_latlon.png"):
            browse = browse or url
        elif name.endswith(".png"):
            other = other or url
    browse = browse or other
    return h5, thumb or browse, browse or thumb


def _product(collection):
    s = (collection or "").upper()
    for tag in ("BETA", "PROVISIONAL", "VALIDATED"):
        if tag in s:
            return tag.lower()
    return s.lower() or None


def _candidate(item, event_time):
    """One CMR granule -> a candidate row, or None if it cannot be used (no
    parseable name or no HTTPS HDF5 to read)."""
    umm = item.get("umm") or {}
    ur = umm.get("GranuleUR") or ""
    meta = parse_name(ur)
    h5, thumb, browse = _urls(umm)
    if meta is None or h5 is None:
        return None
    d = meta["start"]
    geom = _geometry(umm)
    bbox = None
    if geom is not None:
        try:
            bbox = list(shape(geom).bounds)
        except Exception:                    # noqa: BLE001
            bbox = None
    coll = item.get("_collection") or ""
    return dict(id=ur.removesuffix(".h5"), date=d.isoformat(), cloud_pct=None,
                gap_days=round(abs((d - event_time).total_seconds()) / 86400.0),
                source=SOURCE, thumb_url=thumb, browse_url=browse, cog_url=None,
                h5_url=h5,
                geometry=geom, bbox=bbox, orbit_state=meta["orbit_state"],
                relative_orbit=meta["track"], polarizations=meta["polarizations"],
                frame=meta["frame"], cycle=meta["cycle"], mode=meta["mode"],
                collection=coll, product=_product(coll), band="L")


def _one_frame_per_pass(cands, lat, lon):
    """One granule per (track, acquisition day) — the one that images the event.

    Adjacent frames of one pass share track and day but not ground, and the same
    pass can come back twice in two bandwidth modes (2026-06-22 06:01 as both
    '2005' and '4005' — measured). Neither is an extra look. Ranked: footprint
    contains the event point, then nearest footprint, then wider bandwidth
    (finer pixels). Input order (nearest in time first) is preserved."""
    pt = Point(lon, lat)

    def rank(c):
        try:
            g = shape(c["geometry"]) if c.get("geometry") else None
        except Exception:                    # noqa: BLE001
            g = None
        mode_rank = -int(c.get("mode") or 0)
        if g is None or g.is_empty:
            return (1, 0.0, mode_rank)
        return ((0, 0.0, mode_rank) if g.contains(pt)
                else (1, g.distance(pt), mode_rank))

    best = {}
    for i, c in enumerate(cands):
        key = (c.get("relative_orbit"), (c.get("date") or "")[:10])
        r, prev = rank(c), best.get(key)
        if prev is None:
            best[key] = (i, r, c)
        elif r < prev[1]:
            best[key] = (prev[0], r, c)
    return [c for _i, _r, c in sorted(best.values(), key=lambda t: t[0])]


def _bbox(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def search_event(lat, lon, radius_km, event_time: dt.datetime, pre_days=60,
                 post_days=60, auto_window=False, **_ignored):
    """Free dry-run: candidate NISAR GCOV scenes per side, nothing read.

    Same windows as every other source (imagery.windows' arithmetic: pre is
    [event - pre_days, event), post is [event, event + post_days]). An empty
    side carries a note saying WHY (explain_absence) when an archive-wide
    reason applies. Returns dict(source, pre, post, notes)."""
    notes = []
    pre0, pre1 = event_time - dt.timedelta(days=pre_days), event_time
    post0, post1 = event_time, event_time + dt.timedelta(days=post_days)
    if not applicable(event_time, pre_days, post_days):
        return dict(source=SOURCE, pre=[], post=[], notes=[
            f"NISAR not searched: the window ends before its first science "
            f"acquisition ({FIRST_SCIENCE:%Y-%m-%d})"])
    lo = max(pre0, FIRST_SCIENCE - dt.timedelta(days=1))
    items = _granules(collections(), _bbox(lat, lon, radius_km), lo, post1)
    cands = [c for c in (_candidate(i, event_time) for i in items) if c]
    cands.sort(key=lambda c: abs((dt.datetime.fromisoformat(c["date"]) -
                                  event_time).total_seconds()))
    cands = _one_frame_per_pass(cands, lat, lon)
    pre = [c for c in cands if dt.datetime.fromisoformat(c["date"]) < event_time]
    post = [c for c in cands if dt.datetime.fromisoformat(c["date"]) >= event_time]
    pre = pre[:1 if auto_window else _limit_for(pre_days)]
    post = post[:1 if auto_window else _limit_for(post_days)]
    for side, rows, w0, w1 in (("before", pre, pre0, pre1),
                               ("after", post, post0, post1)):
        if not rows:
            why = explain_absence(w0, w1)
            notes.append(f"NISAR: no {side}-scene — " +
                         (why or "no granule images this AOI in the window; widen it"))
    products = {c.get("product") for c in pre + post if c.get("product")}
    if len(products) > 1:
        notes.append("NISAR: the window mixes " + " and ".join(sorted(products)) +
                     " products — their calibration differs, so a pre/post pair "
                     "across the two can read a radiometric offset as change")
    return dict(source=SOURCE, pre=pre, post=post, notes=notes)
