"""Event-level helpers for the Volume tab's CSV, free of Qt so they can be
tested outside QGIS: the scar's distance from the reported epicentre, the
implied depth, and the event columns stamped onto every exported row.

The analyst's verdict (a closed list of calls, an analyst name and a note) lived
here, with a Verdict box on the Volume tab, until 2026-10-30, when both were
removed; the seismic-volume cross-check went the same day.
"""

import math


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Used for the offset between the reported
    epicentre and the centroid of the scar the analyst actually digitized —
    the single number that says whether the detection was well located."""
    try:
        r = 6371.0088
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        h = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        return 2 * r * math.asin(min(1.0, math.sqrt(h)))
    except (TypeError, ValueError):
        return None


def implied_depth_m(volume_m3, area_m2):
    """Mean scar depth V/A — the cheapest sanity check there is.

    A 1.3 ×10⁶ m³ slide over a 0.42 km² scar implies about 3 m of mean depth,
    which is plausible; 40 m would not be, and the number makes that obvious
    without any further modelling."""
    try:
        if not volume_m3 or not area_m2 or area_m2 <= 0:
            return None
        return float(volume_m3) / float(area_m2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# Columns appended to the Volume tab's CSV: who the event was and where the scar
# sits. The tab's own columns carry vol_larsen_*.
EVENT_CSV_FIELDS = [
    # identity — from the Detection, never typed
    ("event_id", "event_id"),
    ("origin_utc", "origin_utc"),
    ("det_lat", "det_lat"),
    ("det_lon", "det_lon"),
    ("det_coherency", "det_coherency"),
    ("det_hf_lf", "det_hf_lf"),
    # where the digitized scar sits
    ("scar_lat", "scar_lat"),
    ("scar_lon", "scar_lon"),
    ("offset_from_det_km", "offset_km"),
    ("implied_depth_m", "implied_depth"),
]
