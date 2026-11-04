"""Event-tied triage for the Maxar VHR tab: which strips, which side, which pair.

One WFS call returns every GEGD strip around an epicentre. This module turns that
list into what the analyst acts on: the strips that actually see the search
circle, split into before/after on the full UTC origin time, graded for what they
can be trusted for, ranked, and a recommended ★ before/after pair to stream.

Plain stdlib and module-level functions only. Pure functions mean a filter change
re-ranks with zero requests, and module functions can never collide with the
tabs' own ``_aoi_bbox`` methods (two of those already disagree on tuple size, and
a duplicate method silently overrides the earlier one).

**Before/after** is ``acq < origin`` = before, on aware UTC datetimes; a naive
datetime is taken as UTC (``Detection.origin_utc`` is naive UTC). Iliamna's
2026-08-01 00:18Z WV01 strip is before an 08-08 event even though Alaska calls
that afternoon 07-31: the date an analyst reads is not the date that decides.

**Grades.** VHR geometry error grows with the view angle and with the DEM the
strip was orthorectified on:
  * ``mapping`` — LV3D (fine DEM, off-nadir ≤30°), or no level recorded (records
    older than ~April 2026) with off-nadir within the limit. Safe to trace on.
  * ``visual`` — LV2A (coarse DEM, off-nadir >30°: tens of metres off in relief),
    or beyond the off-nadir limit. Look, don't trace.
  * ``context`` — SAR, third-party (separate licence), RDOG_BASE and any other
    multi-date mosaic, unknown products. Never a before/after image.
Only ``mapping`` strips are ever ★.

**Relief shift.** A strip is orthorectified on a PRE-event DEM, so where the
surface dropped by Δh the image puts the new surface Δh·tan(incidence) sideways:
about 21 m per 50 m drop for Iliamna's 08-26 strip (off-nadir 20.2°), about 34 m
at 30°. Incidence exceeds off-nadir because the Earth curves away under the
satellite: sin(inc) = (R+h)/R · sin(ona). Scars here are 84-100 % on ice, so this
is the error that matters, and it is why the ranking prices tan(incidence) and the
★ pair prefers a common-mode shift.

**Score** (lower is better; mapping always lists above visual above context):
    gap_d/τ_side + 2.0·tan(inc) + 0.5·max(0, 25-sun)/10 + 1.0·(1-AOI cover)
    + 3.0·[misses epicentre] + 0.5·strip cloud
τ is 30 d before and 15 d after: the FIRST good after-strip wins, because early
snow can bury a deposit (Iliamna: 08-26 at 20.2° over 08-31 at 14.1°). The ★ pair
minimises s_pre + s_post + 0.1·|Δsun_el| + 0.0167·|Δsun_az| + 0.5·|Δtan inc| over
the top five mapping strips per side; the last term keeps the relief shift the
same in both images, so it cancels in a flip.

**Strip cloud** is GEGD's whole-strip fraction (≤ ~0.35 seen). It is weak
evidence for a 5 km circle, so it is weighted lightly; thumbnails cut to the scar
are the confirmation.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .gegd_api import (ARCHIVE_FLOOR_UTC, OPTICAL_PRODUCTS, is_excluded,
                       parse_time)
from .detection import to_alaska

__all__ = [
    "Filters", "RankedStrip", "Ranked", "Disc", "as_filters",
    "event_bbox", "aoi_disc", "aoi_cover", "split_sides", "search_window",
    "incidence_deg", "relief_shift_m", "grade", "strip_score", "pair_cost",
    "best_pair", "stereo_mates", "empty_state_message", "rank", "alaska_str",
    "hint_candidate", "NOT_FOUND", "MAX_PRE_DAYS", "MAX_POST_DAYS",
]

# ---------------------------------------------------------------- constants

KM_PER_DEG = 111.32               # the same degree conversion every tab uses
R_EARTH_KM = 6371.0
# Nominal orbit altitudes. Only the incidence-angle correction uses them, and a
# ±50 km error moves a 30° incidence by under 0.5°.
SENSOR_ALT_KM = {"WV01": 496.0, "WV02": 770.0, "WV03": 617.0, "WV04": 617.0,
                 "GE01": 681.0}
ALT_DEFAULT_KM = 650.0
SENSOR_GROUPS = ("WV01", "WV02", "WV03", "GE01")    # the rest are "other"

TAU_PRE_D, TAU_POST_D = 30.0, 15.0
W_TANINC, W_SUN, W_COVER, W_MISS_EPI, W_CLOUD = 2.0, 0.5, 1.0, 3.0, 0.5
W_DSUN_EL, W_DSUN_AZ, W_DTAN = 0.1, 0.0167, 0.5
SUN_SOFT_DEG = 25.0               # below this, DRA shadows start to swallow detail
TAN_UNKNOWN = 1.0                 # a strip with no off-nadir is priced as ~45°
K_PAIR = 5
STEREO_MAX_S = 300.0              # in-track stereo mates are seconds apart
VISUAL_ONA_CAP = 45.0             # "Also list 30–45° as visual only": never past 45
WINTER_MONTHS = (11, 12, 1, 2)    # no Daily Take Dec–Jan above ~55°N (sun < 15°)
WINTER_LAT = 55.0
GRADE_ORDER = {"mapping": 0, "visual": 1, "context": 2}
# The widest windows the tab offers (its spin-box maxima). A nearest strip beyond
# them is named as such, never as advice to type a number the box refuses.
MAX_PRE_DAYS, MAX_POST_DAYS = 1095, 730

# Passed as nearest_pre/nearest_post when the outside-window query ran and found
# nothing (None means it was not run).
NOT_FOUND = "not_found"


# ---------------------------------------------------------------- data

@dataclass
class Filters:
    """The Scene filters. Client-side only, so changing one never costs a request.

    ``cloud_max`` is a PERCENT of the strip (the spin box's unit); 100 = no cap,
    the default, because a whole-strip figure cannot say the scar is cloudy (the
    PlanetScope tab learned that a scene-wide cap empties windows of scenes that
    were clear over the AOI); strip cloud is priced in the score instead.
    ``sensors`` is None for all, or the ticked names among SENSOR_GROUPS + 'other'."""
    ona_max: float = 30.0
    show_visual: bool = True
    sun_min: float = 15.0
    cloud_max: float = 100.0
    sensors: frozenset = None
    third_party: bool = False


@dataclass
class RankedStrip:
    """A Strip as the table shows it: side, grade, score and the reasons."""
    strip: object
    side: str                       # 'pre' | 'post'
    grade: str                      # 'mapping' | 'visual' | 'context'
    score: float
    cover: float                    # fraction of the search circle it covers
    covers_epicentre: bool
    incidence: float = None         # degrees at the ground; None for SAR
    shift50: float = None           # metres of sideways shift per 50 m of drop
    hidden_reason: str = None       # None = listed; else why the filters hide it
    stereo: tuple = ()              # featureIds of in-track stereo mates
    gap_days: float = 0.0           # |acq - origin| in days
    star: bool = False


@dataclass
class Ranked:
    """Everything the tab needs from one search, with no further requests."""
    pre: list
    post: list
    star_pre: RankedStrip = None
    star_post: RankedStrip = None
    n_hidden: int = 0
    messages: list = field(default_factory=list)
    n_found: int = 0                # records the server returned
    n_excluded: int = 0             # Vivid / BASE_MOSAIC dropped
    n_outside: int = 0              # footprint misses the search circle
    n_duplicate: int = 0


def as_filters(filters):
    """Filters from None (defaults), a dict of field values, or a Filters."""
    if filters is None:
        return Filters()
    if isinstance(filters, Filters):
        f = filters
    elif isinstance(filters, dict):
        f = Filters(**{k: v for k, v in filters.items()
                       if k in Filters.__dataclass_fields__})
    else:
        raise TypeError(f"filters must be a Filters, dict or None, not {type(filters).__name__}")
    s = f.sensors
    if s is not None and not isinstance(s, frozenset):
        # accept {"WV01": True, …} as the tab's checkbox dict hands it over
        names = [k for k, v in s.items() if v] if isinstance(s, dict) else list(s)
        f = Filters(f.ona_max, f.show_visual, f.sun_min, f.cloud_max,
                    frozenset(str(n).upper() if str(n).lower() != "other" else "other"
                              for n in names), f.third_party)
    return f


# ---------------------------------------------------------------- time

def _utc(d):
    """Aware UTC; naive is UTC (never local — see the module docstring)."""
    if isinstance(d, str):
        d = parse_time(d)
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _strip_of(x):
    return getattr(x, "strip", x)


def _acq(x):
    return _utc(getattr(_strip_of(x), "acq_utc", None))


def split_sides(strips, origin_utc):
    """(before, after): before is acq < origin, after is acq >= origin, exactly.

    Works on Strips or RankedStrips; keeps input order; skips undated records."""
    o = _utc(origin_utc)
    if o is None:
        raise ValueError("origin_utc is required to split before/after")
    pre, post = [], []
    for s in strips:
        a = _acq(s)
        if a is None:
            continue
        (pre if a < o else post).append(s)
    return pre, post


def search_window(origin_utc, pre_days, post_days, now_utc=None):
    """(t0, t1) = [origin - pre_days, min(origin + post_days, now)), aware UTC."""
    o = _utc(origin_utc)
    now = _utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    t0 = o - timedelta(days=float(pre_days))
    t1 = min(o + timedelta(days=float(post_days)), now)
    return t0, t1


def alaska_str(when):
    """'2026-07-31 16:18 AKDT (previous day)' for a UTC time — the trap, spelled out.

    Independent of the computer's own time zone (hand-computed US DST rule)."""
    u = _utc(when)
    if u is None:
        return ""
    loc, name = to_alaska(u.replace(tzinfo=None))
    day = ""
    if loc.date() < u.date():
        day = " (previous day)"
    elif loc.date() > u.date():
        day = " (next day)"
    return f"{loc:%Y-%m-%d %H:%M} {name}{day}"


# ---------------------------------------------------------------- geometry

def event_bbox(lat, lon, r_km, pad=0.10):
    """(lonmin, latmin, lonmax, latmax) around the search circle, padded.

    Lon FIRST — the order gegd_api.bbox_cql emits with 'CRS:84'. Clamped to the
    valid range (an antimeridian-crossing circle loses its far side; no Alaska
    landslide target sits there)."""
    lat, lon, r = float(lat), float(lon), float(r_km) * (1.0 + float(pad))
    dlat = r / KM_PER_DEG
    dlon = r / (KM_PER_DEG * max(math.cos(math.radians(lat)), 0.01))
    return (max(-180.0, lon - dlon), max(-90.0, lat - dlat),
            min(180.0, lon + dlon), min(90.0, lat + dlat))


class Disc(tuple):
    """The search circle as a closed (lon, lat) ring, carrying its centre.

    Iterates like any ring (so a layer can be built from it directly), and keeps
    ``lat``/``lon``/``r_km`` so coverage is measured from the true epicentre."""

    def __new__(cls, ring, lat, lon, r_km):
        self = super().__new__(cls, ring)
        self.lat, self.lon, self.r_km = float(lat), float(lon), float(r_km)
        return self


def aoi_disc(lat, lon, r_km, n=64):
    """Regular n-gon (counter-clockwise, closed) of radius r_km around the point.

    Built in local km and converted with the same 111.32 km/° as every tab, so
    the circle is round on the ground, not in degrees."""
    lat, lon, r = float(lat), float(lon), float(r_km)
    if r <= 0:
        raise ValueError(f"radius must be positive, got {r_km}")
    n = max(8, int(n))
    kx = KM_PER_DEG * max(math.cos(math.radians(lat)), 0.01)
    ring = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        ring.append((lon + r * math.cos(a) / kx, lat + r * math.sin(a) / KM_PER_DEG))
    ring.append(ring[0])
    return Disc(ring, lat, lon, r)


def _projector(lat0, lon0):
    """(lon, lat) -> local (x, y) km around (lat0, lon0); wraps the antimeridian."""
    kx = KM_PER_DEG * max(math.cos(math.radians(lat0)), 0.01)

    def f(pt):
        dlon = (pt[0] - lon0 + 180.0) % 360.0 - 180.0
        return (dlon * kx, (pt[1] - lat0) * KM_PER_DEG)
    return f


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _area(pts):
    """Signed shoelace area (positive = counter-clockwise)."""
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _side(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _cross_point(s, e, a, b):
    """Where segment s->e crosses the infinite line a->b."""
    dx, dy = e[0] - s[0], e[1] - s[1]
    ex, ey = b[0] - a[0], b[1] - a[1]
    den = dx * ey - dy * ex
    if den == 0:
        return e
    t = ((a[0] - s[0]) * ey - (a[1] - s[1]) * ex) / den
    return (s[0] + t * dx, s[1] + t * dy)


def _clip(subject, clip_ccw):
    """Sutherland–Hodgman: subject polygon (any shape) ∩ a CONVEX ccw polygon.

    Exact in area for a concave subject too (it may leave zero-width bridges,
    which add no area)."""
    out = list(subject)
    n = len(clip_ccw)
    for i in range(n):
        a, b = clip_ccw[i], clip_ccw[(i + 1) % n]
        inp, out = out, []
        if not inp:
            break
        s = inp[-1]
        for p in inp:
            p_in, s_in = _side(a, b, p) >= 0, _side(a, b, s) >= 0
            if p_in:
                if not s_in:
                    out.append(_cross_point(s, p, a, b))
                out.append(p)
            elif s_in:
                out.append(_cross_point(s, p, a, b))
            s = p
    return out


def _inside(pt, poly):
    """Ray-casting point-in-polygon."""
    x, y = pt
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def aoi_cover(rings, disc):
    """(fraction of the search circle the footprint covers, covers the epicentre).

    ``rings`` are (lon, lat) exterior rings (Strip.rings); parts are summed and
    capped at 1. The circle is round on the GROUND because aoi_disc builds it in
    km (at 60°N a degree of longitude is half a degree of latitude); after that
    the ratio is scale-free, so the local km frame here is for readability and
    for wrapping the antimeridian."""
    lat0 = getattr(disc, "lat", None)
    lon0 = getattr(disc, "lon", None)
    dpts = _open(disc)
    if lat0 is None or lon0 is None:
        lon0 = sum(p[0] for p in dpts) / len(dpts)
        lat0 = sum(p[1] for p in dpts) / len(dpts)
    f = _projector(lat0, lon0)
    clip = [f(p) for p in dpts]
    if _area(clip) < 0:
        clip.reverse()
    total = abs(_area(clip))
    if total <= 0:
        return 0.0, False
    inter, covers = 0.0, False
    for ring in rings or ():
        pts = [f(p) for p in _open(ring)]
        if len(pts) < 3:
            continue
        inter += abs(_area(_clip(pts, clip)))
        if not covers and _inside((0.0, 0.0), pts):
            covers = True
    return max(0.0, min(1.0, inter / total)), covers


# ---------------------------------------------------------------- view geometry

def _alt_km(sensor):
    return SENSOR_ALT_KM.get((sensor or "").upper(), ALT_DEFAULT_KM)


def incidence_deg(ona, sensor):
    """Incidence at the ground (degrees) for an off-nadir angle, or None.

    sin(inc) = (R+h)/R · sin(ona). WV02 at 20.2° -> ~22.8°; at 30° -> ~34.1°."""
    if ona is None:
        return None
    x = (R_EARTH_KM + _alt_km(sensor)) / R_EARTH_KM * math.sin(math.radians(abs(float(ona))))
    if x >= 1.0:
        return 90.0
    return math.degrees(math.asin(x))


def _tan_inc(inc):
    if inc is None:
        return TAN_UNKNOWN
    return math.tan(math.radians(min(inc, 89.0)))


def relief_shift_m(ona, sensor, dh=50.0):
    """Sideways shift (m) of a surface that moved ``dh`` metres vertically.

    The strip is orthorectified on the pre-event DEM, so a source zone that
    dropped 50 m appears ``50·tan(inc)`` metres off: ~21 m for WV02 at 20°,
    ~34 m at 30°. None when the off-nadir is unknown."""
    inc = incidence_deg(ona, sensor)
    if inc is None:
        return None
    return float(dh) * _tan_inc(inc)


# ---------------------------------------------------------------- grading

def _is_context(s):
    if getattr(s, "is_third_party", False) or getattr(s, "multi_date", False):
        return True
    if (getattr(s, "sensor_type", None) or "").lower() == "radar":
        return True
    p = (getattr(s, "product_name", None) or "").upper()
    return bool(p) and p not in OPTICAL_PRODUCTS


def grade(strip, ona_max=30.0):
    """'mapping' | 'visual' | 'context' (see the module docstring)."""
    if _is_context(strip):
        return "context"
    lvl = (strip.processing_level or "").upper()
    if lvl == "LV2A":
        return "visual"
    if lvl not in ("", "LV3D"):
        return "visual"                    # a level nobody vouched for
    if strip.ona is None:
        return "mapping" if lvl == "LV3D" else "visual"
    return "mapping" if float(strip.ona) <= float(ona_max) else "visual"


def strip_score(strip, origin_utc, cover=1.0, covers_epicentre=True):
    """The per-strip cost (lower is better); the formula is in the module docstring."""
    o, a = _utc(origin_utc), _acq(strip)
    gap_d = abs((o - a).total_seconds()) / 86400.0
    tau = TAU_PRE_D if a < o else TAU_POST_D
    tinc = _tan_inc(incidence_deg(strip.ona, strip.sensor))
    sun_pen = 0.0 if strip.sun_el is None else max(0.0, SUN_SOFT_DEG - strip.sun_el) / 10.0
    cov = 0.0 if cover is None else max(0.0, min(1.0, float(cover)))
    cloud = 0.0 if strip.cloud is None else max(0.0, float(strip.cloud))
    return (gap_d / tau + W_TANINC * tinc + W_SUN * sun_pen + W_COVER * (1.0 - cov)
            + W_MISS_EPI * (0.0 if covers_epicentre else 1.0) + W_CLOUD * cloud)


def pair_cost(pre, post):
    """s_pre + s_post + the illumination and common-mode-shift mismatch terms."""
    a, b = _strip_of(pre), _strip_of(post)
    c = pre.score + post.score
    if a.sun_el is not None and b.sun_el is not None:
        c += W_DSUN_EL * abs(a.sun_el - b.sun_el)
    if a.sun_az is not None and b.sun_az is not None:
        d = abs(a.sun_az - b.sun_az) % 360.0
        c += W_DSUN_AZ * min(d, 360.0 - d)
    c += W_DTAN * abs(_tan_inc(incidence_deg(a.ona, a.sensor))
                      - _tan_inc(incidence_deg(b.ona, b.sensor)))
    return c


def _eligible(r):
    return r.hidden_reason is None and r.grade == "mapping"


def best_pair(pre, post, k=K_PAIR):
    """(★ before, ★ after) among the top ``k`` listed mapping-grade strips per side.

    With strips on one side only, that side's best is ★ alone. Visual and
    context strips are never ★: a coarse-DEM strip is tens of metres off in
    relief, and a mosaic or SAR scene is not a before/after image."""
    ep = sorted((r for r in pre if _eligible(r)), key=lambda r: (r.score, r.strip.feature_id))[:k]
    eq = sorted((r for r in post if _eligible(r)), key=lambda r: (r.score, r.strip.feature_id))[:k]
    if ep and eq:
        best = min(((a, b) for a in ep for b in eq),
                   key=lambda ab: (pair_cost(*ab), ab[0].strip.feature_id, ab[1].strip.feature_id))
        return best
    return (ep[0] if ep else None), (eq[0] if eq else None)


def stereo_mates(strips):
    """{featureId: (mate featureIds…)} for same-sensor optical strips < 5 min apart.

    In-track stereo pairs (Iliamna 09-22, 40 s apart) are one look from two
    angles, not two dates: badge them, don't count them twice."""
    items = [_strip_of(s) for s in strips]
    items = [s for s in items
             if s.acq_utc is not None and (s.sensor_type or "").lower() != "radar"]
    mates = {}
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.feature_id == b.feature_id or (a.sensor or "") != (b.sensor or ""):
                continue
            if abs((_utc(a.acq_utc) - _utc(b.acq_utc)).total_seconds()) < STEREO_MAX_S:
                mates.setdefault(a.feature_id, []).append(b.feature_id)
                mates.setdefault(b.feature_id, []).append(a.feature_id)
    return {k: tuple(v) for k, v in mates.items()}


def _sensor_group(s):
    sen = (s.sensor or "").upper()
    return sen if sen in SENSOR_GROUPS else "other"


def _visual_why(s, f):
    lvl = (s.processing_level or "").upper()
    if lvl == "LV2A":
        return "LV2A, coarse DEM"
    if s.ona is not None and s.ona > f.ona_max:
        return f"off-nadir {s.ona:.0f}° > {f.ona_max:g}°"
    return "no off-nadir or processing level recorded"


def hint_candidate(strip):
    """True when ``strip`` could be the before/after image a widened window finds.

    For the nearest-outside-the-window hint: never a Vivid/base mosaic, a context
    strip (SAR, third-party, RDOG_BASE or any multi-date mosaic, unknown product),
    or one steeper than 45° (always hidden). Pointing at one of those sends the
    analyst to widen a window and find nothing they can use."""
    if is_excluded(strip) or _is_context(strip):
        return False
    return strip.ona is None or float(strip.ona) <= VISUAL_ONA_CAP


def _hidden_reason(s, g, f):
    why = []
    if s.is_third_party and not f.third_party:
        why.append("third-party imagery (separate licence)")
    if g == "visual":
        if s.ona is not None and s.ona > VISUAL_ONA_CAP:
            why.append(f"off-nadir {s.ona:.0f}° > {VISUAL_ONA_CAP:.0f}°")
        elif not f.show_visual:
            why.append(f"visual only ({_visual_why(s, f)})")
    if s.sun_el is not None and s.sun_el < f.sun_min:
        why.append(f"sun {s.sun_el:.0f}° < {f.sun_min:g}°")
    if s.cloud is not None and s.cloud * 100.0 > f.cloud_max:
        why.append(f"strip cloud {s.cloud * 100.0:.0f}% > {f.cloud_max:g}%")
    if f.sensors is not None and _sensor_group(s) not in f.sensors:
        why.append(f"sensor {s.sensor or '?'} not ticked")
    return "; ".join(why) or None


def _order(r):
    return (r.hidden_reason is not None, GRADE_ORDER.get(r.grade, 9), r.score,
            r.strip.feature_id)


# ---------------------------------------------------------------- messages

def _days_ceil(seconds):
    return int(math.ceil(seconds / 86400.0 - 1e-9))


def _strip_hint(s):
    """'2025-06-14 (WV02, ONA 22°)'."""
    s = _strip_of(s)
    bits = [s.sensor or "?"]
    if s.ona is not None:
        bits.append(f"ONA {s.ona:.0f}°")
    return f"{s.acq_utc:%Y-%m-%d} ({', '.join(bits)})"


# hidden_reason prefix -> the short name used when summarising many rows
_REASON_NAMES = (("third-party", "third-party"), ("visual only", "visual only"),
                 ("off-nadir", f"off-nadir > {VISUAL_ONA_CAP:.0f}°"),
                 ("sun", "low sun"), ("strip cloud", "strip cloud"),
                 ("sensor", "sensor not ticked"))


def _reasons_text(rows, n=3):
    """'low sun ×3, strip cloud ×1' — the commonest hide reasons among rows."""
    counts = {}
    for r in rows:
        for part in (r.hidden_reason or "").split("; "):
            name = next((v for k, v in _REASON_NAMES if part.startswith(k)), part)
            if name:
                counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{k} ×{v}" for k, v in top)


def empty_state_message(ranked, origin_utc, *, lat=None, t0=None, t1=None,
                        nearest_pre=None, nearest_post=None, canary_hits=None,
                        now_utc=None):
    """The honest sentence(s) for an empty or one-sided result; '' when all is well.

    ``t0``/``t1`` are the window the server was asked for. ``nearest_pre`` /
    ``nearest_post`` are the nearest strips OUTSIDE it (a Strip), NOT_FOUND
    when that query found nothing, or None when it was not run.
    ``canary_hits`` is numberMatched of the 50 km / 1 year canary, or None.
    Lines are separated by newlines; each says what to do next."""
    o = _utc(origin_utc)
    now = _utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    t0u, t1u = _utc(t0), _utc(t1)
    # Never advise widening a window to a strip it could not use (SAR, third-party,
    # a legacy mosaic): SearchTask already skips those; this also covers a
    # search.json cached before it did.
    if nearest_pre not in (None, NOT_FOUND) and not hint_candidate(_strip_of(nearest_pre)):
        nearest_pre = None
    if nearest_post not in (None, NOT_FOUND) and not hint_candidate(_strip_of(nearest_post)):
        nearest_post = None
    lines = []
    vis_pre = [r for r in ranked.pre if r.hidden_reason is None]
    vis_post = [r for r in ranked.post if r.hidden_reason is None]
    # Context rows (SAR, third-party, mosaics) are never a before/after image, so a
    # side holding only those is as empty as one holding nothing.
    ctx_pre = sum(1 for r in ranked.pre if r.grade == "context")
    ctx_post = sum(1 for r in ranked.post if r.grade == "context")
    use_pre, use_post = len(ranked.pre) - ctx_pre, len(ranked.post) - ctx_post
    listed = len(ranked.pre) + len(ranked.post)
    # Vivid/base mosaics are dropped unseen: a search that returned only those
    # returned nothing usable, and must read that way, not as an axis-order bug.
    returned = listed + ranked.n_outside
    floor_txt = f"{ARCHIVE_FLOOR_UTC:%Y-%m-%d}"
    winter = (o.month in WINTER_MONTHS and (lat is None or abs(float(lat)) >= WINTER_LAT))
    winter_txt = ("No Maxar Daily Take is collected in Dec–Jan at this latitude (sun "
                  "below ~15°); the first possible after-strip is about February. Try "
                  "the SAR tab, or tick Include third-party for X-band SAR.")
    future_txt = "The event time is in the future; check the date and that it is UTC."
    age = (now - o).total_seconds() / 86400.0
    has_later = nearest_post is not None and nearest_post != NOT_FOUND

    def canary_line():
        if canary_hits is None:
            return None
        if canary_hits == 0:
            return ("No GEGD coverage here: not one strip within 50 km in the last "
                    "year. Use the Sentinel-2 / SAR tabs.")
        return (f"No GEGD strips in this circle and window, although GEGD holds "
                f"{canary_hits} within 50 km over the last year: the area is "
                "covered. Widen the windows or the radius.")

    def context_note(n):
        return (f" (only {n} context record(s): SAR, third-party or multi-date mosaic)"
                if n else "")

    def later_hint():
        # '+1': the window is acq < origin + post_days, so the strip's own day
        # must be inside it (verified by feeding the number back in the tests).
        need = int(math.floor((_acq(nearest_post) - o).total_seconds() / 86400.0)) + 1
        if need > MAX_POST_DAYS:
            return (f"nearest later strip {_strip_hint(nearest_post)}, {need} days after "
                    f"the event: beyond the longest after-window ({MAX_POST_DAYS} days).")
        return (f"nearest later strip {_strip_hint(nearest_post)}. Widen the "
                f"after-window to {need} days to include it.")

    def no_pre_line():
        """The before side holds nothing usable (not merely hidden)."""
        span = f"in {(o - t0u).total_seconds() / 86400.0:.0f} d" if t0u else "in the window"
        msg = f"No before-event VHR {span}" + context_note(ctx_pre)
        if t0u is not None and t0u < ARCHIVE_FLOOR_UTC:
            msg += f" (GEGD's archive starts around {floor_txt})"
        if nearest_pre == NOT_FOUND:
            return msg + "; GEGD has no earlier strip here at all."
        if nearest_pre is not None:
            need = _days_ceil((o - _acq(nearest_pre)).total_seconds())
            if need > MAX_PRE_DAYS:
                return msg + (f"; nearest earlier strip {_strip_hint(nearest_pre)}, {need} "
                              f"days before the event: beyond the longest before-window "
                              f"({MAX_PRE_DAYS} days).")
            return msg + (f"; nearest earlier strip {_strip_hint(nearest_pre)}. Widen the "
                          f"before-window to {need} days to include it.")
        return msg + "."

    def no_post_line():
        """The after side holds nothing usable (not merely hidden)."""
        msg = "No after-event VHR"
        if t1u is not None and t1u > o:
            msg += f" in {(t1u - o).total_seconds() / 86400.0:.0f} d"
        msg += context_note(ctx_post)
        if age < 0:
            return msg + " — " + future_txt[0].lower() + future_txt[1:]
        if winter:
            # The winter gap explains it; a strip found beyond the window still helps.
            return winter_txt + (" The " + later_hint() if has_later else "")
        if nearest_post == NOT_FOUND or (nearest_post is None and t1u is not None
                                         and t1u >= now - timedelta(minutes=5)):
            return msg + (f" yet: the event is {age:.0f} d old. New strips appear 2–8 h "
                          "after collection; search again later, or use the SAR tab.")
        if has_later:
            return msg + "; " + later_hint()
        return msg + "."

    if returned == 0:
        if t0u is not None and t0u < ARCHIVE_FLOOR_UTC:
            lines.append(
                f"GEGD's online archive starts around {floor_txt}; nothing older is "
                "searchable with this key, and nothing newer came back here. Use the "
                "Sentinel-2 / SAR tabs, or ask GEGD for an ingest in the GEGD Pro web "
                "app.")
            if t1u is not None and t1u <= ARCHIVE_FLOOR_UTC:
                return "\n".join(lines)         # wholly before the archive: that is all
            # The window reaches into the archive too, so the canary still tells "no
            # coverage here" from "covered, just not in this window".
            line = canary_line()
            if line:
                lines.append(line)
        else:
            lines.append(canary_line() or "No GEGD strips in this search circle and window.")
        if ranked.n_excluded:
            lines.append(f"({ranked.n_excluded} Vivid/base-mosaic record(s) ignored: "
                         "multi-year composites, never a before/after image.)")
        if age < 0:
            lines.append(future_txt)
        elif winter:
            lines.append(winter_txt)
        return "\n".join(lines)

    if listed == 0:
        lines.append(f"GEGD returned {ranked.n_outside} strip(s) for the search box, but "
                     "no footprint touches the search circle. Widen the radius. (If the "
                     "footprints sit far from the event, the query's axis order is "
                     "wrong: report it.)")
        return "\n".join(lines)

    if not vis_pre and not vis_post:
        lines.append(f"{listed} strip(s) found, all hidden by Scene filters "
                     f"({_reasons_text(ranked.pre + ranked.post)}). Show them, or loosen "
                     "the filters.")
        # "Show them" can only fix a side that HAS usable rows; say so for one
        # that has none.
        if not use_pre:
            lines.append(no_pre_line())
        if not use_post:
            lines.append(no_post_line())
        return "\n".join(lines)

    if not use_pre:
        lines.append(no_pre_line())
    elif not vis_pre:
        lines.append(f"No usable before-strip: {len(ranked.pre)} hidden by Scene "
                     f"filters ({_reasons_text(ranked.pre)}).")

    if not use_post:
        lines.append(no_post_line())
    elif not vis_post:
        lines.append(f"No usable after-strip: {len(ranked.post)} hidden by Scene "
                     f"filters ({_reasons_text(ranked.post)}).")
    return "\n".join(lines)


# ---------------------------------------------------------------- rank

def rank(strips, origin_utc, lat, lon, r_km, filters=None, *, t0=None, t1=None,
         nearest_pre=None, nearest_post=None, canary_hits=None, now_utc=None,
         k=K_PAIR):
    """Rank one search's strips for the event at (lat, lon) with radius r_km.

    Drops Vivid/BASE_MOSAIC (never candidates), duplicate featureIds, and any
    strip whose footprint misses the search circle (the client-side guard against
    a silently wrong spatial query). Everything else is listed — hidden rows
    carry ``hidden_reason`` so "Show them" needs no request. The keyword-only
    arguments only sharpen ``messages`` (see empty_state_message)."""
    f = as_filters(filters)
    o = _utc(origin_utc)
    if o is None:
        raise ValueError("origin_utc is required")
    disc = aoi_disc(lat, lon, r_km)
    strips = list(strips or ())
    seen, kept = set(), []
    n_excl = n_out = n_dup = 0
    for s in strips:
        if is_excluded(s):
            n_excl += 1
            continue
        if s.feature_id in seen:
            n_dup += 1
            continue
        seen.add(s.feature_id)
        if s.acq_utc is None:
            continue
        cover, covers = aoi_cover(s.rings, disc)
        if cover <= 0.0 and not covers:
            n_out += 1
            continue
        kept.append((s, cover, covers))

    mates = stereo_mates([s for s, _, _ in kept])
    pre, post = [], []
    for s, cover, covers in kept:
        a = _utc(s.acq_utc)
        g = grade(s, f.ona_max)
        radar = (s.sensor_type or "").lower() == "radar"
        inc = None if radar else incidence_deg(s.ona, s.sensor)
        r = RankedStrip(
            strip=s, side="pre" if a < o else "post", grade=g,
            score=strip_score(s, o, cover, covers), cover=cover,
            covers_epicentre=covers, incidence=inc,
            shift50=None if radar else relief_shift_m(s.ona, s.sensor),
            hidden_reason=_hidden_reason(s, g, f),
            stereo=mates.get(s.feature_id, ()),
            gap_days=abs((o - a).total_seconds()) / 86400.0)
        (pre if r.side == "pre" else post).append(r)
    pre.sort(key=_order)
    post.sort(key=_order)

    star_pre, star_post = best_pair(pre, post, k)
    for r in (star_pre, star_post):
        if r is not None:
            r.star = True

    ranked = Ranked(pre=pre, post=post, star_pre=star_pre, star_post=star_post,
                    n_hidden=sum(1 for r in pre + post if r.hidden_reason is not None),
                    n_found=len(strips), n_excluded=n_excl, n_outside=n_out,
                    n_duplicate=n_dup)

    msgs = []
    text = empty_state_message(ranked, o, lat=lat, t0=t0, t1=t1,
                               nearest_pre=nearest_pre, nearest_post=nearest_post,
                               canary_hits=canary_hits, now_utc=now_utc)
    if text:
        msgs.extend(text.split("\n"))
    for side, star, rows in (("before", star_pre, pre), ("after", star_post, post)):
        listed = [r for r in rows if r.hidden_reason is None]
        if star is None and listed:
            msgs.append(f"No mapping-grade {side}-strip (only visual-only or context "
                        f"imagery): nothing is starred on that side. Visual-only strips "
                        "sit tens of metres off in steep relief; look at them, don't "
                        "trace on them.")
    if n_out and (pre or post):
        msgs.append(f"{n_out} strip(s) whose footprint misses the search circle were "
                    "left out.")
    ranked.messages = msgs
    return ranked
