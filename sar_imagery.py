"""Search Sentinel-1 RTC (SAR amplitude) scenes around a landslide event.

Source: Planetary Computer's `sentinel-1-rtc` collection — Sentinel-1 IW-mode
GRD backscatter, Radiometrically Terrain Corrected by Catalyst. One float32 COG
per polarization (VV/VH over most land) of terrain-flattened gamma-naught in
LINEAR power at 10 m pixel spacing (native resolution ~20 m range x 22 m
azimuth). Because RTC normalizes both geometry and radiometry against a DEM, a
single fixed brightness stretch reads consistently across scenes and dates —
which is what makes simple pre/post visual amplitude comparison meaningful in
steep terrain.

Access: fully anonymous. The collection description still says an account key
is required to read RTC pixels, but Microsoft retired account registration and
anonymous SAS tokens / data-API renders now work (verified 2026-07; a key, via
PC_SDK_SUBSCRIPTION_KEY, only raises rate limits for those who still have one
— see github.com/microsoft/PlanetaryComputer issue #464).

Scope: search/preview only. SAR amplitude is a visual product here — there is
no composite/dNDVI Run path for it (the optical pipeline stays optical).

Unlike the optical sources there is no cloud filtering (SAR sees through
cloud), so candidates rank purely by temporal distance to the event. Orbit
metadata (ascending/descending + relative orbit) is included in each candidate
because meaningful pre/post comparison requires the SAME viewing geometry:
comparing an ascending scene to a descending one shows geometry differences,
not ground change. The plugin pairs scenes by relative orbit by default.
"""
from __future__ import annotations
import datetime as dt

from shapely.geometry import shape, Point

import imagery as im   # shared STAC client, retry, bbox and window helpers

COLLECTION = "sentinel-1-rtc"

# floor for how many candidates to return per side. SAR revisit over Alaska is
# ~6-12 days per relative orbit and several orbits usually cover a point, so the
# candidates split across 3-4 tracks — and the plugin's multi-temporal change
# detection needs 3+ before-scenes on ONE track. 12 only stretches that far
# because same-pass duplicate frames are collapsed first (_one_frame_per_pass)
# and the cap grows with the window (_limit_for).
DEFAULT_LIMIT = 12
# ceiling once the window is widened (see _limit_for). Each extra candidate is
# another server-side quicklook render in the plugin's gallery, so the growth
# is capped rather than unbounded.
MAX_LIMIT = 24


def _limit_for(days):
    """Candidate cap for a window of `days` — grows so widening actually helps.

    With a fixed cap the nearest-N sort silently truncates the window (a 60-day
    slider returned scenes spanning only ~45 days), which made 'widen the before
    window' useless advice when change detection wanted more same-track scenes.
    A given point is imaged every ~3-5 days once every covering track is counted,
    so days/4 tracks the real supply."""
    return max(DEFAULT_LIMIT, min(MAX_LIMIT, round(days / 4)))


def _one_frame_per_pass(items, lat, lon):
    """Keep one frame per (track, acquisition day) — the one imaging the event.

    A Sentinel-1 pass is sliced into ~250 km frames that share the track number
    and the acquisition day but cover different ground along-track. They are NOT
    extra temporal samples: an adjacent frame can share zero pixels with the
    event point. Left in, they burn half the candidate slots — and because the
    plugin's multi-temporal change detection needs 3+ *distinct dates* on ONE
    track, those wasted slots are what starve it (observed: track 123 returning
    3 dates as 6 rows, of which the covering frames were only 2).

    Frames are ranked event-covering first, then by footprint distance to the
    event point, so a pass whose frames all miss the point still contributes its
    nearest frame. Input order (nearest in time first) is preserved."""
    pt = Point(lon, lat)

    def rank(item):
        try:
            geom = shape(item.geometry) if item.geometry else None
        except (AttributeError, ValueError, TypeError):
            geom = None
        if geom is None or geom.is_empty:
            return (1, 0.0)          # no footprint metadata — don't penalize
        return (0, 0.0) if geom.contains(pt) else (1, geom.distance(pt))

    best = {}                        # pass key -> (input position, rank, item)
    for i, item in enumerate(items):
        key = (item.properties.get("sat:relative_orbit"),
               item.datetime.date() if item.datetime else i)
        r, prev = rank(item), best.get(key)
        if prev is None:
            best[key] = (i, r, item)
        elif r < prev[1]:
            best[key] = (prev[0], r, item)   # keep the pass's original position
    return [item for _pos, _r, item in sorted(best.values(), key=lambda t: t[0])]


def _candidate(item, event_time):
    """One STAC item -> a JSON-able candidate row for the dry-run preview.

    Same contract as imagery._stac_candidate (id/date/gap_days/cloud_pct/
    source/thumb_url/geometry/bbox) so the plugin's table/gallery/footprint
    code applies unchanged, plus SAR-specific fields:
      orbit_state     'ascending' | 'descending'
      relative_orbit  int — Sentinel-1 track number; scenes from the same
                      relative orbit share viewing geometry and are the ones
                      that should be compared pre vs post
      polarizations   e.g. ['VV', 'VH'] — which grayscale assets exist
    cloud_pct is None (SAR is cloud-blind); the tab has no cloud column.
    thumb_url is the item's rendered_preview (the plugin appends the
    subscription key and may swap in its own render for stretch control)."""
    d = item.datetime.replace(tzinfo=None) if item.datetime is not None else None
    thumb = None
    for key in ("rendered_preview", "thumbnail"):
        asset = item.assets.get(key)
        if asset is not None:
            thumb = asset.href
            break
    props = item.properties
    return dict(id=item.id, date=d.isoformat() if d else None,
                cloud_pct=None,
                gap_days=abs((d - event_time).days) if d else None,
                source="Sentinel-1", thumb_url=thumb, cog_url=None,
                geometry=item.geometry, bbox=list(item.bbox) if item.bbox else None,
                orbit_state=props.get("sat:orbit_state"),
                relative_orbit=props.get("sat:relative_orbit"),
                polarizations=props.get("sar:polarizations"))


def search_scenes(lat, lon, radius_km, start, end, event_time, limit=DEFAULT_LIMIT):
    """Sentinel-1 RTC scenes intersecting the AOI in [start, end], nearest first.

    IW mode only (the land acquisition mode the RTC archive is processed from —
    other modes have no RTC product). No cloud metric exists or is needed;
    ranking is purely |acquired - event_time|. Adjacent frames of one pass are
    collapsed to the frame that images the event point BEFORE the limit applies
    (see _one_frame_per_pass) — the limit is meant to bound distinct looks, not
    to be spent twice on the same acquisition."""
    cat = im._client()
    items = im._search_items(
        cat,
        collections=[COLLECTION],
        bbox=im._bbox(lat, lon, radius_km),
        datetime=f"{start.date()}/{end.date()}",
        query={"sar:instrument_mode": {"eq": "IW"}},
    )
    items = [i for i in items if i.datetime is not None]
    items.sort(key=lambda i: abs(
        (i.datetime.replace(tzinfo=None) - event_time).total_seconds()))
    return _one_frame_per_pass(items, lat, lon)[:limit]


def search_event(lat, lon, radius_km, event_time: dt.datetime, pre_days=60,
                 post_days=60, seasonal=False, auto_window=False, **_ignored):
    """Free dry-run: candidate Sentinel-1 RTC scenes per side, nothing read.

    Same windowing as the optical sources (imagery.windows) so the SAR tab's
    day sliders mean the same thing as the other tabs'. auto_window trims each
    side to the single nearest scene; extra optical-only kwargs (cloud caps
    etc.) are accepted and ignored so the dispatcher can call every source
    uniformly. Each side's candidate cap scales with its own window so a widened
    slider returns more scenes (see _limit_for). Returns
    dict(source, pre=[...], post=[...])."""
    pre0, pre1, post0, post1 = im.windows(event_time, pre_days, post_days, seasonal)
    pre_lim = 1 if auto_window else _limit_for(pre_days)
    post_lim = 1 if auto_window else _limit_for(post_days)
    pre_items = search_scenes(lat, lon, radius_km, pre0, pre1, event_time,
                              limit=pre_lim)
    post_items = search_scenes(lat, lon, radius_km, post0, post1, event_time,
                               limit=post_lim)
    return dict(source="Sentinel-1",
                pre=[_candidate(i, event_time) for i in pre_items],
                post=[_candidate(i, event_time) for i in post_items])
