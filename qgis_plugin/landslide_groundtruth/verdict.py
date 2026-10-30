"""The analyst's conclusion — the deliverable ground-truthing actually produces.

Everything else in the plugin gathers evidence. Nothing recorded the finding, so
a season of work left no machine-readable trace and could not be joined back to
the seismic catalogue it came from.

Deliberately free of Qt so it can be tested outside QGIS. **VERDICTS** is the
closed list of calls an analyst can make. "Not found" and "Cannot tell" are
separate on purpose: a scar that is absent and a scar hidden under cloud are
completely different results for whoever tunes the detector, and collapsing them
into one "no" throws that away.

The seismic-volume cross-check (reconcile) was dropped on 2026-10-30 along with
the Detection box's volume fields.
"""

import math

VERDICTS = [
    ("confirmed", "Confirmed — a scar is visible and I have delineated it"),
    ("probable", "Probable — something changed here but the scar is not clean"),
    ("not_found", "Not found — I searched the area and see no scar"),
    ("obscured", "Cannot tell — cloud, snow or darkness obscured the area"),
    ("wrong_place", "Scar found, but well outside the reported location"),
    ("not_landslide",
     "Change is real but is not a landslide (glacier surge, avalanche, flood…)"),
]

VERDICT_KEYS = [k for k, _ in VERDICTS]


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


# Columns appended to the Volume tab's CSV: who the event was and what the
# analyst found. The tab's own columns carry vol_thickness_* and vol_larsen_*.
VERDICT_CSV_FIELDS = [
    # identity — from the Detection, never typed
    ("event_id", "event_id"),
    ("origin_utc", "origin_utc"),
    ("det_lat", "det_lat"),
    ("det_lon", "det_lon"),
    ("det_coherency", "det_coherency"),
    ("det_hf_lf", "det_hf_lf"),
    # what the analyst found
    ("verdict", "verdict"),
    ("scar_lat", "scar_lat"),
    ("scar_lon", "scar_lon"),
    ("offset_from_det_km", "offset_km"),
    ("implied_depth_m", "implied_depth"),
    ("analyst_note", "note"),
    ("analyst", "analyst"),
    ("recorded_utc", "recorded_utc"),
]
