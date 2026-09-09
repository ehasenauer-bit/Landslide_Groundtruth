"""The analyst's conclusion — the deliverable ground-truthing actually produces.

Everything else in the plugin gathers evidence. Nothing recorded the finding, so
a season of work left no machine-readable trace and could not be joined back to
the seismic catalogue it came from.

Two things live here, both deliberately free of Qt so they can be tested outside
QGIS:

* **VERDICTS** — the closed list of calls an analyst can make. "Not found" and
  "Cannot tell" are separate on purpose: a scar that is absent and a scar hidden
  under cloud are completely different results for whoever tunes the detector,
  and collapsing them into one "no" throws that away.
* **reconcile()** — the volume cross-check. The detection carries a seismic
  volume; the Volume tab derives one from the digitized area via Larsen; DEM
  differencing gives a third. Agreement between independent estimates is the
  actual validation, and it is compared in LOG10 space because area-to-volume
  scaling is a power law whose spread is multiplicative — a factor of two, not
  a fixed number of cubic metres.
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

# |log10(V_a / V_b)| below this counts as agreement. 0.42 ≈ a factor of 2.6,
# which is about the honest spread of the Larsen area-volume relation for a
# single slide — tightening it would manufacture disagreement out of the
# scaling's own scatter.
AGREE_LOG10 = 0.42
MARGINAL_LOG10 = 0.85          # ≈ a factor of 7


def d_log10(a, b):
    """|log10(a/b)|, or None if either side is missing or non-positive."""
    try:
        if a is None or b is None or a <= 0 or b <= 0:
            return None
        return abs(math.log10(float(a) / float(b)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def classify(d):
    """agree / marginal / disagree from a log10 separation."""
    if d is None:
        return ""
    if d <= AGREE_LOG10:
        return "agree"
    if d <= MARGINAL_LOG10:
        return "marginal"
    return "disagree"


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Used for the offset between the reported
    epicentre and the centroid of the scar the analyst actually digitized —
    which, against the record's location error, is the single number that says
    whether the detection was well located."""
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


def reconcile(seismic=None, larsen=None, dh_erosion=None):
    """Compare the independent volume estimates of one slide.

    `seismic` is the detection's inversion, `larsen` the area-scaling result and
    `dh_erosion` the DEM-differencing EROSION volume — the erosion side, not the
    net, because the net is a near-zero difference of two large numbers and is
    not the quantity a seismic inversion estimates.

    Returns a dict with the separations, a verdict string, and a human sentence.
    """
    out = {
        "d_larsen": d_log10(larsen, seismic),
        "d_dh": d_log10(dh_erosion, seismic),
        "agreement": "",
        "text": "",
    }
    ds = [d for d in (out["d_larsen"], out["d_dh"]) if d is not None]
    if not ds:
        out["text"] = ("Only one volume estimate is available — no cross-check "
                       "is possible.")
        return out
    worst = max(ds)
    out["agreement"] = classify(worst)
    ratio = 10 ** worst
    label = {"agree": "agree", "marginal": "only marginally agree",
             "disagree": "disagree"}[out["agreement"]]
    out["text"] = (f"The independent estimates {label} "
                   f"(largest separation ×{ratio:.1f}, |D| = {worst:.2f}; "
                   f"agreement threshold {AGREE_LOG10:.2f}).")
    return out


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


# Columns appended to the Volume tab's CSV. Every volume gets its OWN column:
# the seismic, area-scaling and DEM-differencing estimates must never share one,
# or the export cannot show that they were compared.
VERDICT_CSV_FIELDS = [
    # identity — from the Detection, never typed
    ("event_id", "event_id"),
    ("origin_utc", "origin_utc"),
    ("det_lat", "det_lat"),
    ("det_lon", "det_lon"),
    ("det_loc_error_km", "det_loc_error_km"),
    ("det_coherency", "det_coherency"),
    ("det_hf_lf", "det_hf_lf"),
    # what the analyst found
    ("verdict", "verdict"),
    ("scar_lat", "scar_lat"),
    ("scar_lon", "scar_lon"),
    ("offset_from_det_km", "offset_km"),
    ("offset_vs_loc_error", "offset_ratio"),
    # the three volumes, never sharing a column
    ("vol_seismic_m3", "vol_seismic_m3"),
    ("vol_seismic_lo_m3", "vol_seismic_lo_m3"),
    ("vol_seismic_hi_m3", "vol_seismic_hi_m3"),
    ("vol_larsen_m3", "v_best"),
    ("vol_larsen_lo_m3", "v_low"),
    ("vol_larsen_hi_m3", "v_high"),
    ("vol_dh_erosion_m3", "v_erosion"),
    ("vol_dh_deposit_m3", "v_deposit"),
    ("implied_depth_m", "implied_depth"),
    # the comparison
    ("D_larsen_log10", "d_larsen"),
    ("D_dh_log10", "d_dh"),
    ("agreement", "agreement"),
    ("analyst_note", "note"),
    ("analyst", "analyst"),
    ("recorded_utc", "recorded_utc"),
]
