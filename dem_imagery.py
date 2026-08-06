"""Search time-stamped DEM strips around a landslide event, for DEM differencing.

Source: the Polar Geospatial Center's public STAC API (stac.pgc.umn.edu) over
the pgc-opendata-dems bucket on AWS Open Data — no account or token needed.
Three strip collections cover the globe with the SAME product (SETSM stereo
DEMs from Maxar imagery, s2s041 processing, 2 m posting):

  arcticdem-strips-s2s041-2m   all land north of 60°N plus ALL of Alaska,
                               Greenland and Kamchatka
  earthdem-strips-s2s041-2m    mid-latitudes (fills between ArcticDEM and REMA)
  rema-strips-s2s041-2m        Antarctica

Unlike the mosaic collections (one blended surface per location), every STRIP
is a single stereo acquisition with its own timestamp — which is what makes
pre/post elevation differencing possible: subtract a before-event strip from an
after-event strip and a landslide reads directly as elevation change (negative
at the evacuated source, positive over the deposit).

Caveats the plugin surfaces to the user:
  * Heights are ellipsoidal (WGS84), NOT orthometric — irrelevant for
    differencing (the geoid cancels), but don't compare against sea level.
  * Strips carry a few metres of absolute vertical bias each (satellite
    geolocation), so a raw difference has a DC offset; the plugin removes a
    robust median offset over the (assumed mostly stable) AOI by default.
  * Temporal coverage is opportunistic tasking, not a revisit schedule — the
    nearest pre-event strip may be years old, hence the tab's much wider
    day-window sliders.

Scope: search/preview only, like the SAR source — there is no composite Run
path for DEMs (differencing happens inside the plugin, see dem_diff.py).

Candidates rank purely by temporal distance to the event (strips have a
cloud metric — pgc:cloud_area_percent, clouds break stereo matching and
become data gaps — but a cloudy strip can still be perfect over the AOI, so
it is reported, not filtered on).
"""
from __future__ import annotations
import datetime as dt
import threading

import pystac_client
from pystac_client.stac_api_io import StacApiIO

import imagery as im   # shared retry policy, bbox and window helpers

PGC_URL = "https://stac.pgc.umn.edu/api/v1"

# collection id -> the source label shown in the plugin's table/gallery
COLLECTIONS = {
    "arcticdem-strips-s2s041-2m": "ArcticDEM",
    "earthdem-strips-s2s041-2m": "EarthDEM",
    "rema-strips-s2s041-2m": "REMA",
}

# how many candidates to return per side. Strip coverage is opportunistic:
# a well-imaged Alaska geocell can hold dozens of strips over a decade while
# a remote one holds two — 16 rows give the plugin enough to find a covering
# pre/post pair without flooding the table.
DEFAULT_LIMIT = 16

_local = threading.local()


def _client():
    """Open (once per thread) and reuse the PGC STAC client. Same rationale as
    imagery._client — skip the landing-page round-trip on every search — but a
    separate cache: this is a different API host and needs NO signing modifier
    (the DEM bucket is anonymous AWS Open Data)."""
    c = getattr(_local, "pgc_client", None)
    if c is None:
        stac_io = StacApiIO(timeout=im._TIMEOUT, max_retries=im._RETRY)
        c = pystac_client.Client.open(PGC_URL, stac_io=stac_io,
                                      timeout=im._TIMEOUT)
        _local.pgc_client = c
    return c


def _collections_for(lat):
    """Which strip collections can cover this latitude. Boundaries are fuzzy
    (ArcticDEM dips to ~50°N to take in all of Alaska/Kamchatka), so search the
    plausible pair — a non-covering collection just contributes zero items."""
    if lat >= 0:
        return ["arcticdem-strips-s2s041-2m", "earthdem-strips-s2s041-2m"]
    return ["rema-strips-s2s041-2m", "earthdem-strips-s2s041-2m"]


def _asset_href(item, key):
    a = item.assets.get(key)
    return a.href if a is not None else None


def _candidate(item, event_time):
    """One STAC strip item -> a JSON-able candidate row for the dry-run preview.

    Same contract as imagery._stac_candidate (id/date/gap_days/cloud_pct/
    source/thumb_url/geometry/bbox) so the plugin's table/footprint code
    applies unchanged, plus DEM-specific fields:
      sensor         'WV01'/'WV02'/'WV03'/'GE01' — the stereo pair's platform
      is_xtrack      True = cross-track stereo (two satellites/orbits): more
                     vertical bias than in-track pairs; fine after
                     co-registration but worth flagging
      rmse           pgc:rmse (m) — strip-internal segment alignment error
      valid_pct      pgc:valid_area_percent — how much of the strip footprint
                     has data (stereo matching fails over cloud/water)
      cloud_pct      pgc:cloud_area_percent — clouds become DATA GAPS in a
                     stereo DEM (not wrong heights), so it's context, not a
                     filter
      dem_url / hillshade_url / mask_url  the 2 m elevation COG, its 10 m
                     hillshade browse COG, and the edge/water/cloud bitmask
                     COG — all anonymous /vsicurl-readable
    thumb_url is None: PGC publishes no PNG browse; the plugin renders its own
    thumbnails from the hillshade COG's overviews."""
    d = item.datetime.replace(tzinfo=None) if item.datetime is not None else None
    props = item.properties
    pairname = props.get("pgc:pairname") or ""
    sensor = pairname.split("_")[0] if pairname else None

    def pct(key):
        # PGC stores its *_percent properties as fractions (0.62 = 62%);
        # the candidate contract uses 0-100 like every other source
        v = props.get(key)
        return None if v is None else round(100.0 * v, 1)

    return dict(id=item.id, date=d.isoformat() if d else None,
                cloud_pct=pct("pgc:cloud_area_percent"),
                gap_days=abs((d - event_time).days) if d else None,
                source=COLLECTIONS.get(item.collection_id, "DEM strip"),
                thumb_url=None, cog_url=_asset_href(item, "dem"),
                geometry=item.geometry, bbox=list(item.bbox) if item.bbox else None,
                sensor=sensor,
                is_xtrack=bool(props.get("pgc:is_xtrack")),
                rmse=props.get("pgc:rmse"),
                valid_pct=pct("pgc:valid_area_percent"),
                gsd=props.get("gsd"),
                dem_url=_asset_href(item, "dem"),
                hillshade_url=_asset_href(item, "hillshade"),
                hillshade_masked_url=_asset_href(item, "hillshade_masked"),
                mask_url=_asset_href(item, "mask"))


def search_scenes(lat, lon, radius_km, start, end, event_time, limit=DEFAULT_LIMIT):
    """DEM strips intersecting the AOI in [start, end], nearest-in-time first.

    No cloud/quality filtering: with acquisitions years apart, a partly-cloudy
    strip that covers the AOI is usually still the best (or only) choice —
    quality fields ride along on the candidate for the plugin to display."""
    cat = _client()
    items = im._search_items(
        cat,
        collections=_collections_for(lat),
        bbox=im._bbox(lat, lon, radius_km),
        datetime=f"{start.date()}/{end.date()}",
    )
    items = [i for i in items if i.datetime is not None]
    items.sort(key=lambda i: abs(
        (i.datetime.replace(tzinfo=None) - event_time).total_seconds()))
    return items[:limit]


def _nearest_outside_note(lat, lon, radius_km, event_time, side):
    """One empty side deserves an explanation: is the window too narrow, or
    does the archive simply have nothing on that side of the event here?

    Runs a second, window-free search on that side and reports the nearest
    strip's date and gap — so 'widen the slider to N days' is actionable —
    or states that the archive holds nothing at all (common on the post side:
    stereo tasking and strip processing lag an event by months to years)."""
    word = "before" if side == "pre" else "after"
    if side == "pre":
        rng = f"2007-01-01/{(event_time - dt.timedelta(days=1)).date()}"
    else:
        rng = (f"{(event_time + dt.timedelta(days=1)).date()}/"
               f"{(event_time + dt.timedelta(days=10 * 365)).date()}")
    items = im._search_items(
        _client(),
        collections=_collections_for(lat),
        bbox=im._bbox(lat, lon, radius_km),
        datetime=rng,
    )
    items = [i for i in items if i.datetime is not None]
    if not items:
        note = (f"DEM strips: the archive has NO {word}-event strip at this "
                "location at all")
        if side == "post":
            note += (" (yet — stereo tasking and processing lag an event by "
                     "months to years; re-search later)")
        return note
    nearest = min(items, key=lambda i: abs(
        (i.datetime.replace(tzinfo=None) - event_time).total_seconds()))
    gap = abs((nearest.datetime.replace(tzinfo=None) - event_time).days)
    return (f"DEM strips: nearest {word}-event strip is "
            f"{nearest.datetime.date()} ({gap} d {word} the event) — outside "
            f"the current window; set the '{word}' slider past {gap} days to "
            "reach it")


def search_event(lat, lon, radius_km, event_time: dt.datetime, pre_days=1825,
                 post_days=730, seasonal=False, auto_window=False, **_ignored):
    """Free dry-run: candidate pre/post DEM strips, nothing downloaded.

    Same windowing helper as every other source (imagery.windows) so the day
    sliders mean the same thing — the tab just defaults them far wider (years,
    not weeks) because stereo DEM tasking is opportunistic. auto_window trims
    each side to the single nearest strip; extra optical-only kwargs (cloud
    caps etc.) are accepted and ignored so the dispatcher can call every
    source uniformly. Returns dict(source, pre=[...], post=[...], notes=[...])
    — when a side is empty, notes says whether widening the slider would help
    (see _nearest_outside_note)."""
    pre0, pre1, post0, post1 = im.windows(event_time, pre_days, post_days, seasonal)
    lim = 1 if auto_window else DEFAULT_LIMIT
    pre_items = search_scenes(lat, lon, radius_km, pre0, pre1, event_time, limit=lim)
    post_items = search_scenes(lat, lon, radius_km, post0, post1, event_time, limit=lim)
    notes = []
    for side, found in (("pre", pre_items), ("post", post_items)):
        if not found:
            notes.append(_nearest_outside_note(lat, lon, radius_km,
                                               event_time, side))
    return dict(source="DEM strips", notes=notes,
                pre=[_candidate(i, event_time) for i in pre_items],
                post=[_candidate(i, event_time) for i in post_items])
