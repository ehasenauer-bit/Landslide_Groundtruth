"""The seismic detection record — the plugin's input, as one object.

The analyst does not arrive with a latitude and a date. They arrive holding a
seismic landslide-detection record: an approximate epicentre, an origin time, a
LOCATION ERROR, a volume estimate with a range, and the detector's own quality
metrics. Everything the plugin does is downstream of that record, so it is worth
having exactly once instead of retyped into four tabs.

    Detection = Y
    Coherency = 0.61
    HF / LF   = 11.8
    Org time  = 08:48:37
    Latitude  =  60.50°
    Longitude = -140.60°
    Loc error = 17 km
    Vol       = 1.3 M m³
    Vol range = 0.9 - 1.7 M m³

Two fields in there are load-bearing and the plugin used to ignore both:

* **loc_error_km** is the radius the scar is actually somewhere inside. A 17 km
  error against the tabs' 5 km default search radius is about one twelfth of the
  uncertainty area, so the default quietly looks in the wrong place and an empty
  result reads as "no landslide" when it means "never searched there".
* **vol_best_m3** is an INDEPENDENT volume estimate. The Volume tab derives a
  second one from the digitized scar area, and DEM differencing a third. Two or
  three independent volumes of the same event is the actual validation, and it
  cannot happen if the seismic one has nowhere to live.

`parse()` is deliberately tolerant: the record gets pasted from a figure caption,
an email, a PDF or a terminal, so field order, separators, unicode degree/minus
signs and stray labels all vary. Nothing here imports Qt — it is plain stdlib so
it can be unit-tested outside QGIS.
"""

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- Alaska time

_AK_STD = timedelta(hours=-9)      # AKST
_AK_DST = timedelta(hours=-8)      # AKDT


def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (0=Mon) of `month` — used for the US DST boundaries."""
    d = datetime(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _ak_offset(utc_dt):
    """Alaska UTC offset for a UTC datetime, DST included.

    US rule since 2007: DST runs from 02:00 local on the second Sunday in March
    to 02:00 local on the first Sunday in November. Computed by hand rather than
    via zoneinfo because a QGIS install may ship without the tzdata package, and
    a silently wrong timezone here is exactly the bug this class exists to stop.
    """
    y = utc_dt.year
    start = _nth_weekday(y, 3, 6, 2) + timedelta(hours=2) - _AK_STD   # -> UTC
    end = _nth_weekday(y, 11, 6, 1) + timedelta(hours=2) - _AK_DST    # -> UTC
    naive = utc_dt.replace(tzinfo=None)
    return _AK_DST if start <= naive < end else _AK_STD


def to_alaska(utc_dt):
    """(local_datetime, 'AKDT'|'AKST') for a UTC datetime."""
    off = _ak_offset(utc_dt)
    name = "AKDT" if off == _AK_DST else "AKST"
    return utc_dt.replace(tzinfo=None) + off, name


# ---------------------------------------------------------------- the record

@dataclass
class Detection:
    """One seismic landslide detection. All distances in km, volumes in m³."""
    event_id: str = ""
    origin_utc: datetime = None
    lat: float = None
    lon: float = None
    loc_error_km: float = None
    vol_best_m3: float = None
    vol_low_m3: float = None
    vol_high_m3: float = None
    # provenance only — displayed so the analyst can judge how hard to look,
    # never used to compute anything.
    coherency: float = None
    hf_lf: float = None
    detection: str = None
    notes: str = ""
    raw: str = field(default="", repr=False)

    # ---- derived -------------------------------------------------------
    def is_locatable(self):
        return self.lat is not None and self.lon is not None

    def suggested_radius_km(self, floor=3.0, cap=50.0):
        """The search radius that actually covers the uncertainty.

        The scar is somewhere in a disc of radius `loc_error_km`; a radius
        smaller than that is searching a fraction of it. Falls back to the
        historical 5 km only when the record carries no error at all, and is
        clamped to the spinboxes' own 0.2-50 km range."""
        if not self.loc_error_km:
            return 5.0
        return max(floor, min(cap, float(self.loc_error_km)))

    def local_str(self):
        """'2026-02-03 23:48 AKST (previous day)' — the timezone trap, spelled out."""
        if not self.origin_utc:
            return ""
        loc, name = to_alaska(self.origin_utc)
        day = ""
        if loc.date() < self.origin_utc.date():
            day = " (previous day)"
        elif loc.date() > self.origin_utc.date():
            day = " (next day)"
        return f"{loc:%Y-%m-%d %H:%M} {name}{day}"

    def vol_str(self):
        """'1.3 ×10⁶ m³ (0.9 – 1.7)' — or '' when the record carries no volume."""
        if self.vol_best_m3 is None:
            return ""
        s = f"{self.vol_best_m3 / 1e6:.3g} ×10⁶ m³"
        if self.vol_low_m3 is not None and self.vol_high_m3 is not None:
            s += f" ({self.vol_low_m3 / 1e6:.2g} – {self.vol_high_m3 / 1e6:.2g})"
        return s

    def summary_lines(self):
        """The read-back block. Echoing the local time is the point: an analyst
        reading 23:48 off the figure and typing it into a UTC field moves the
        pre/post boundary nine hours and silently reclassifies the bracketing
        scene."""
        out = []
        if self.event_id:
            out.append(self.event_id)
        if self.origin_utc:
            out.append(f"{self.origin_utc:%Y-%m-%d %H:%M:%S} UTC   =   {self.local_str()}")
        if self.is_locatable():
            pos = f"{self.lat:.4f}, {self.lon:.4f}"
            if self.loc_error_km:
                pos += f"  ± {self.loc_error_km:g} km"
            out.append(pos)
        v = self.vol_str()
        if v:
            out.append(f"Seismic volume {v}")
        q = []
        if self.detection:
            q.append(f"Detection {self.detection}")
        if self.coherency is not None:
            q.append(f"coherency {self.coherency:g}")
        if self.hf_lf is not None:
            q.append(f"HF/LF {self.hf_lf:g}")
        if q:
            out.append(" · ".join(q))
        return out

    def as_row(self):
        """Flat dict for the CSV export, prefixed so it never collides with the
        measured columns — the whole point is that the seismic estimate and the
        area-derived estimate never share a column."""
        return {
            "event_id": self.event_id or "",
            "origin_utc": self.origin_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if self.origin_utc else "",
            "det_lat": "" if self.lat is None else f"{self.lat:.6f}",
            "det_lon": "" if self.lon is None else f"{self.lon:.6f}",
            "det_loc_error_km": "" if self.loc_error_km is None else f"{self.loc_error_km:g}",
            "det_coherency": "" if self.coherency is None else f"{self.coherency:g}",
            "det_hf_lf": "" if self.hf_lf is None else f"{self.hf_lf:g}",
            "vol_seismic_m3": "" if self.vol_best_m3 is None else f"{self.vol_best_m3:.0f}",
            "vol_seismic_lo_m3": "" if self.vol_low_m3 is None else f"{self.vol_low_m3:.0f}",
            "vol_seismic_hi_m3": "" if self.vol_high_m3 is None else f"{self.vol_high_m3:.0f}",
        }

    def to_dict(self):
        d = asdict(self)
        d.pop("raw", None)
        d["origin_utc"] = self.origin_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if self.origin_utc else ""
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        ts = d.pop("origin_utc", "") or ""
        det = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if ts:
            try:
                det.origin_utc = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                det.origin_utc = None
        return det


# ---------------------------------------------------------------- parsing

# Unicode the record picks up from PDFs and figure captions: minus sign, en/em
# dash, non-breaking and thin spaces. Normalised before matching so the patterns
# below only ever see ASCII.
_SUBS = {
    "−": "-", "–": "-", "—": "-", "‒": "-",
    " ": " ", " ": " ", " ": " ", "°": " ",
    # superscripts: the record almost always writes m³, not m^3 or m3
    "³": "3", "²": "2", "⁴": "4",
}

_NUM = r"[-+]?\d+(?:\.\d+)?"

_PAT = {
    "lat": rf"\blat(?:itude)?\b\s*[=:]?\s*({_NUM})",
    "lon": rf"\blon(?:g|gitude)?\b\s*[=:]?\s*({_NUM})",
    "loc_error": rf"\bloc\.?\s*(?:ation)?\s*error\b\s*[=:]?\s*({_NUM})",
    "coherency": rf"\bcoherenc(?:y|e)\b\s*[=:]?\s*({_NUM})",
    "hf_lf": rf"\bhf\s*/?\s*lf\b\s*[=:]?\s*({_NUM})",
    "detection": r"\bdetection\b\s*[=:]?\s*([YN])\b",
    "event_id": r"\bevent(?:\s*id)?\b\s*[=:]\s*([A-Za-z0-9_\-]+)",
}

# "Vol range = 0.9 - 1.7 M m^3" must be tried BEFORE the bare "Vol = 1.3",
# otherwise the range's first number is read as the best estimate.
_RE_VOL_RANGE = re.compile(
    rf"\bvol(?:ume)?\s*range\b\s*[=:]?\s*({_NUM})\s*-\s*({_NUM})\s*(M|k)?\s*m\s*\^?3",
    re.I)
_RE_VOL_BEST = re.compile(
    rf"\bvol(?:ume)?\b\s*(?!range)[=:]?\s*({_NUM})\s*(M|k)?\s*m\s*\^?3", re.I)
# full timestamp with an explicit UTC marker — the only form we trust for origin
_RE_ORIGIN_UTC = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\s*(?:UTC|Z)\b", re.I)
# ... and a bare timestamp, used only if no UTC-marked one is present
_RE_ORIGIN_ANY = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
# "Org time = 08:48:37" — a time with no date, to refine the date we already have
_RE_ORG_TIME = re.compile(r"\borg(?:in)?\.?\s*time\b\s*[=:]?\s*(\d{2}):(\d{2})(?::(\d{2}))?", re.I)

_MULT = {"m": 1e6, "k": 1e3, None: 1.0, "": 1.0}


def _norm(text):
    for a, b in _SUBS.items():
        text = text.replace(a, b)
    return text


def _f(m, g=1):
    try:
        return float(m.group(g))
    except (TypeError, ValueError):
        return None


def _dt(m):
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    except (TypeError, ValueError):
        return None


def parse(text):
    """Read a pasted detection record into a Detection.

    Returns (detection, warnings). Never raises: a record that parses partially
    is more useful than an exception, and the warnings tell the analyst exactly
    which fields they still have to fill in by hand."""
    det = Detection(raw=text or "")
    warn = []
    t = _norm(text or "")

    for key in ("lat", "lon", "loc_error", "coherency", "hf_lf"):
        m = re.search(_PAT[key], t, re.I)
        if m:
            setattr(det, {"loc_error": "loc_error_km"}.get(key, key), _f(m))

    m = re.search(_PAT["detection"], t, re.I)
    if m:
        det.detection = m.group(1).upper()
    m = re.search(_PAT["event_id"], t, re.I)
    if m:
        det.event_id = m.group(1)

    # volumes: range first, then the best estimate
    m = _RE_VOL_RANGE.search(t)
    if m:
        k = _MULT.get((m.group(3) or "").lower(), 1.0)
        det.vol_low_m3, det.vol_high_m3 = _f(m, 1) * k, _f(m, 2) * k
    m = _RE_VOL_BEST.search(t)
    if m:
        det.vol_best_m3 = _f(m, 1) * _MULT.get((m.group(2) or "").lower(), 1.0)
    if det.vol_best_m3 is None and det.vol_low_m3 and det.vol_high_m3:
        det.vol_best_m3 = (det.vol_low_m3 * det.vol_high_m3) ** 0.5   # log-mid

    # origin time: prefer the UTC-marked stamp. A record that shows both UTC and
    # local (they differ by a calendar day) must never be read local-first.
    m = _RE_ORIGIN_UTC.search(t)
    if m:
        det.origin_utc = _dt(m)
    else:
        m = _RE_ORIGIN_ANY.search(t)
        if m:
            det.origin_utc = _dt(m)
            warn.append("The timestamp had no 'UTC' marker — it was read as UTC. "
                        "Check it against the record before searching.")
    # "Org time" is the authoritative seconds-resolution origin; if it disagrees
    # with the header stamp's clock, trust it for the time-of-day.
    mo = _RE_ORG_TIME.search(t)
    if mo and det.origin_utc:
        det.origin_utc = det.origin_utc.replace(
            hour=int(mo.group(1)), minute=int(mo.group(2)),
            second=int(mo.group(3) or 0))

    # sanity — a dropped minus sign on longitude is the classic error, and in
    # Alaska/Yukon it puts the AOI in Siberia or the Bering Sea without failing.
    if det.lat is not None and not (-90 <= det.lat <= 90):
        warn.append(f"Latitude {det.lat} is out of range — ignored.")
        det.lat = None
    if det.lon is not None and not (-180 <= det.lon <= 180):
        warn.append(f"Longitude {det.lon} is out of range — ignored.")
        det.lon = None
    if det.lon is not None and det.lon > 0 and det.lat is not None and det.lat > 50:
        warn.append(f"Longitude {det.lon:+g} is POSITIVE (eastern hemisphere). "
                    "Alaska and the Yukon are negative — check for a dropped minus sign.")

    if not det.event_id and det.origin_utc:
        det.event_id = f"AK{det.origin_utc:%Y-%m%d}"
    if not det.is_locatable():
        warn.append("No latitude/longitude found — enter them by hand.")
    if det.origin_utc is None:
        warn.append("No origin time found — enter it by hand.")
    if det.loc_error_km is None:
        warn.append("No location error found — the search radius was left at its "
                    "current value.")
    return det, warn
