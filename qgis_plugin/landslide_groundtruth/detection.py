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

The Detection box takes the event ID, origin time and epicentre. It used to
take the location error (which set the search radius) and the seismic volume
(the Volume tab's cross-check) too; both were dropped on 2026-10-30, with the
cross-check, and each tab now gets its own fixed radius from SEARCH_RADIUS_KM.

The record arrives as a FIGURE — a station map with the numbers printed in the
corner — so there is nothing to paste and no text to parse. The analyst reads
the values off the image; this class exists so they do that ONCE instead of
four times across four tabs.

Nothing here imports Qt — it is plain stdlib, so it can be unit-tested outside
QGIS.
"""

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


# ---------------------------------------------------------------- search radius

# The radius 'Use it in every tab' gives each tab, in km — Ethan's choice of
# 2026-10-30, when the location error left the Detection box. SAR and the 3D
# viewer take Sentinel-2's, as their own copy-from-Sentinel-2 does.
SEARCH_RADIUS_KM = {
    "sentinel2": 15.0,
    "planet": 10.0,
    "maxar": 15.0,
    "sar": 15.0,
    "viewer3d": 15.0,
}


def radius_summary():
    """'Sentinel-2 15 km · PlanetScope 10 km · Maxar 15 km' for the read-back."""
    r = SEARCH_RADIUS_KM
    return (f"Sentinel-2 {r['sentinel2']:g} km · PlanetScope {r['planet']:g} km · "
            f"Maxar {r['maxar']:g} km")


# ---------------------------------------------------------------- the record

@dataclass
class Detection:
    """One seismic landslide detection."""
    event_id: str = ""
    origin_utc: datetime = None
    lat: float = None
    lon: float = None
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
            out.append(f"{self.lat:.4f}, {self.lon:.4f}")
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
        measured columns."""
        return {
            "event_id": self.event_id or "",
            "origin_utc": self.origin_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if self.origin_utc else "",
            "det_lat": "" if self.lat is None else f"{self.lat:.6f}",
            "det_lon": "" if self.lon is None else f"{self.lon:.6f}",
            "det_coherency": "" if self.coherency is None else f"{self.coherency:g}",
            "det_hf_lf": "" if self.hf_lf is None else f"{self.hf_lf:g}",
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
