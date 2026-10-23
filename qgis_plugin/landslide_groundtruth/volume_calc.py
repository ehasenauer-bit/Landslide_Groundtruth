"""Landslide volume from scar area — Larsen et al. (2010) area–volume scaling.

    V = alpha * A^gamma      (fitted in log10 space, per hillslope material)

Larsen, I.J., Montgomery, D.R., Korup, O., 2010, Landslide erosion controlled by
hillslope material: Nature Geoscience, v. 3, doi:10.1038/NGEO776 (Table S1).

The project's `larsen_BR_volume.py` is the CANONICAL implementation — this
module prefers it, loaded by explicit file path out of the configured project
dir (same pattern as PlanetTab._ledger), so editing the coefficients there is
picked up by the plugin without touching plugin code. `_fallback_volume_source`
below is a stdlib mirror of that function, used when the project dir isn't set
yet (or the file has moved) so the Volume tab still works out of the box. Keep
the two in step if the coefficients change.

Both forms return (V_best, V_low, V_high) in the cube of the area's unit — m^2
in, m^3 out — where low/high are +/- 1 standard deviation propagated in log10
units from the published coefficient uncertainties and, when min/max scar
outlines are supplied, from the area uncertainty as well.
"""
import math
import os

# Larsen et al. Table S1: log10(alpha), std(log10 alpha), gamma, std(gamma).
# Mirrors larsen_BR_volume.py — see module docstring. These are the SCAR (source
# area) fits: the supplied script names them "soil scar"/"bedrock scar" and calls
# its function volume_source.
LARSEN = {
    "bedrock": (-0.63, 0.06, 1.41, 0.02),
    "soil": (-0.649, 0.021, 1.262, 0.009),
}

# ---------------------------------------------------------------------------
# TOTAL-AREA fits — DELIBERATELY EMPTY, fill these in to enable the "Total
# landslide area" option in the Volume tab.
#
# Why empty: the relation above is calibrated on landslide SOURCE (scar) area.
# Running it on a total-area outline — source + runout track + deposit — applies
# a source-area fit to a larger polygon and overestimates the volume. Larsen et
# al. publish separate coefficients for total landslide area; those constants are
# not reproduced here because guessing published values would be worse than
# refusing, so the tab reports "not configured" until you paste them in.
#
# Add one entry per material, in the SAME 4-tuple order as LARSEN above:
#
#     LARSEN_TOTAL = {
#         "bedrock": (log10_alpha, std_log10_alpha, gamma, std_gamma),
#         "soil":    (log10_alpha, std_log10_alpha, gamma, std_gamma),
#     }
#
# A project's own larsen_BR_volume.py may instead define a module-level
# LARSEN_TOTAL dict of the same shape; it takes precedence over this one, so the
# constants can live with the rest of the science rather than in plugin code.
# ---------------------------------------------------------------------------
LARSEN_TOTAL = {}

# UI labels for the material choice -> the `type` argument the science module takes
MATERIALS = [
    ("Bedrock", "bedrock"),
    ("Soil", "soil"),
]

# Which calibration the area is run through, in the order the tab offers them —
# the FIRST entry is what the Fit combo comes up on.
#
# "thickness" leads because it is the only fit that runs on an outline ALONE.
# The others each want something that usually isn't there: "ddem" needs two DEM
# epochs bracketing the event (or an imported Δh), which for these events
# rarely exists; "total" needs LARSEN_TOTAL coefficients that ship empty; and
# "scar" — the supplied script's fit, always available — is calibrated on soil
# and bedrock hillslope failures, a population a rock-and-ice avalanche off a
# glacierized headwall is not in. Area × thickness makes its one assumption
# (the mean thickness) visible on screen instead of burying it in a regression.
FITS = [
    ("Deposit area × thickness", "thickness"),
    ("Source scar (Larsen Table S1)", "scar"),
    ("Total landslide area", "total"),
    ("Elevation change (∫Δh over outline)", "ddem"),
]

# What each fit expects the digitized polygon to BE — shown on the tab so the
# outline and the calibration can't silently disagree.
FIT_OUTLINE = {
    "scar": ("Outline the SOURCE SCAR only — this fit is calibrated on source "
             "area, so including the runout track or deposit inflates the volume."),
    "total": ("Outline the TOTAL landslide area — source, runout track and "
              "deposit together, matching this fit's calibration."),
    "ddem": ("Outline the WHOLE affected area — source scar, runout track and "
             "deposit together. This fit sums the elevation change (Δh) over "
             "that outline instead of scaling an area, so it must cover "
             "everything that moved; erosion (loss) and deposition (gain) are "
             "reported separately. Needs a Δh raster assigned below."),
    "thickness": ("Outline the DEPOSIT / total affected area — this fit is just "
                  "its plan area × a mean thickness (no DEM needed), the Toney "
                  "et al. (2021) estimate for a rock/ice avalanche. Pick the "
                  "deposit's setting below (it fills the mean thickness and its "
                  "low/high): ~2.5 m (1.5–3.5 m) for an Alaska rock avalanche on a "
                  "glacier seen after the ice melted, ~3.5 m (3–5 m) soon after."),
}

# Mean deposit thickness by setting, for the area × thickness fit, in the order the
# tab's Setting combo offers them — the FIRST entry is the default and must match
# the thickness fields' construction values. Each is (label, key, mean, low, high,
# note), thickness in metres.
#
# "Near date" and "far date" are WHEN the outlined imagery was taken, because that
# decides what the deposit still holds. Soon after the event it carries the snow
# and ice picked up on the way down (30–60% of the deposit at Black Rapids and
# Sherman), which is what a seismic volume counts too. After a melt season only the
# rock sheet is left, and that is what nearly every published thickness measured.
#
# Sources: Deline et al. 2015, "Rock avalanches onto glaciers", Table 9.1 and
# pp. 283–284; Sosio et al. 2012, QSR 47, Tables 2 and 4 and p. 24; Shugar & Clague
# 2011, Sedimentology 58, Table 1; Caplan-Auerbach & Huggel 2007, J. Glaciol.,
# pp. 129–133; Toney et al. 2021; Toney et al. 2026, JGR-ES (Peters Dome); Schaefer
# et al. 2023, JGR-ES (Barry Arm). Those last two are the only Alaska volumes in the
# USGS seismogenic-landslide catalogue (doi 10.5066/P90VGCSK, v4.0) that are measured
# AND sit on a usable outline — most of its others are Larsen scaling of the source
# area or area × an assumed thickness. Where a source gives only a range, the mean
# is its rounded middle.
#
# The combo is not stored in the project. It is read back off the three numbers
# (thickness_setting), so an older project shows whichever setting its values are,
# and an edit away from every setting reads as Custom.
THICKNESS_SETTINGS = [
    ("Glacier, far date (ice melted) — 2.5 m (1.5–3.5)", "glacier_far", 2.5, 1.5, 3.5,
     "A rock avalanche spread over a glacier, seen on imagery after a melt season: "
     "the rock sheet left once its snow and ice melt out. Measured Alaska sheets of "
     "~2–70 Mm³ average 1.5–3.2 m, median ~2.5 m (Deline et al. 2015, Table 9.1), "
     "\"generally 2–3 m\" after the melt (Sosio et al. 2012, p. 24). A seismic "
     "volume also counts the ice, so this reads 1.4–2.5× low against one."),
    ("Glacier, near date — 3.5 m (3–5)", "glacier_near", 3.5, 3.0, 5.0,
     "A rock avalanche spread over a glacier, seen on imagery soon after the event, "
     "while the deposit still holds the snow and ice it picked up — closest to what "
     "a seismic volume counts. Sherman's 25–30 Mm³ over 8.3 km² is 3.0–3.6 m and Red "
     "Glacier 2003's 12–20 Mm³ over 4 km² is 3–5 m (Sosio et al. 2012, Table 2). "
     "In Alaska, Peters Dome 2023 (Denali) measured 6.1 Mm³ by DEM differencing "
     "over ~1.4 km² of deposit, ~4.5 m (Toney et al. 2026)."),
    ("Ice-rich, near date — 2 m (1–3)", "ice_rich_near", 2.0, 1.0, 3.0,
     "An Iliamna-type ice-rich avalanche, the whole of it — ice, snow and rock — "
     "over its full path: 1–2 m at Umbrella Glacier and 2–3 m at Lateral Glacier in "
     "2004 (Caplan-Auerbach & Huggel 2007, pp. 132–133); Toney et al. 2021 use "
     "1.5 ± 1 m. The big Red Glacier events reach 3–5 m (12–20 Mm³ over ~4 km²)."),
    ("Ice-rich, far date (ice melted) — 1.5 m (1–2)", "ice_rich_far", 1.5, 1.0, 2.0,
     "An Iliamna-type ice-rich avalanche once its ice has melted: the rock left at "
     "Red Glacier 2003 is 1–2 m (Deline et al. 2015, Table 9.1; Sosio et al. 2012, "
     "Table 2). Over half of what failed there was ice, so the failed volume can be "
     "2–3× this."),
    ("Very small, under ~0.2 Mm³ — 0.7 m (0.4–1)", "small", 0.7, 0.4, 1.0,
     "Mueller Glacier 0.5–1 m and Jarvis Glacier under 0.5 m (Deline et al. 2015, "
     "pp. 283–284); Brenndalsbreen 2010 came out near 0.4 m."),
    ("Confined or off-glacier — 5.5 m (4–8)", "confined", 5.5, 4.0, 8.0,
     "Thurwieser 4–5 m by GPS, Becca di Luseney 6–7 m and Brenva 5–6 m (Sosio et "
     "al. 2012, Tables 2 and 4), Kshwan 5 m and Morsárjökull 5.5 m (Deline et al. "
     "2015, Table 9.1). Off the ice, deposits run several times thicker than the "
     "thin sheets on it. An unconfined slide down an open slope runs thinner: Barry "
     "Arm's October 2020 headscarp slide was ~2.5 m (0.65 Mm³ over 0.26 km², lidar "
     "differencing; Schaefer et al. 2023)."),
    ("Source scar — 15 m (5–50)", "scar", 15.0, 5.0, 50.0,
     "A detachment depth, for an outline of the evacuated source scar alone. Red "
     "Glacier 2003 failed ~15 m deep and Lateral Glacier 2004 5–30 m (Caplan-"
     "Auerbach & Huggel 2007, pp. 131–133); the Black Rapids rock slabs were "
     "30–50 m thick (Shugar & Clague 2011, p. 1764)."),
]


def thickness_setting(t_best, t_low, t_high):
    """The THICKNESS_SETTINGS entry whose mean/low/high these are, or None.

    Compared as numbers, so "1", "1.0" and "1.00" are all the same setting."""
    def _same(a, b):
        return a is not None and math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
    for entry in THICKNESS_SETTINGS:
        _label, _key, best, low, high, _note = entry
        if _same(t_best, best) and _same(t_low, low) and _same(t_high, high):
            return entry
    return None


def _fallback_volume_source(A_best, A_low=None, A_high=None, type="bedrock"):
    """stdlib mirror of larsen_BR_volume.volume_source (no numpy).

    A_low/A_high are optional: given both, std(log10 A) is estimated by treating
    log10(A_low)..log10(A_high) as the +/- 2 sigma (95%) range, i.e. the mean of
    the two log-space half-widths divided by 2. Given neither, std(log10 A) = 0
    and the range reflects the fit uncertainty alone."""
    try:
        values = LARSEN[type]
    except KeyError:
        raise Exception("type must be bedrock or soil")
    return _coefficient_volume(values, A_best, A_low, A_high)


def _coefficient_volume(values, A_best, A_low=None, A_high=None):
    """The area->volume arithmetic for one 4-tuple of coefficients.

    Shared by the scar mirror and the total-area fit so there is exactly one
    copy of the log-space error propagation, whichever calibration is in use."""
    logalpha, stdlogalpha, gamma, stdgamma = values
    logA = math.log10(A_best)
    logV = logalpha + gamma * logA

    if A_high is not None and A_low is not None:
        if A_high < A_best:
            raise Exception("A_high must be larger than A_best")
        if A_low > A_best:
            raise Exception("A_low must be smaller than A_best")
        d1 = logA - math.log10(A_low)
        d2 = math.log10(A_high) - logA
        stdlogA = ((d1 + d2) / 2.0) / 2.0
        K = (stdlogalpha ** 2 + (logA ** 2 * stdgamma ** 2)
             + (gamma ** 2 * stdlogA ** 2))
    else:
        K = stdlogalpha ** 2 + (logA ** 2 * stdgamma ** 2)

    stdlogV = math.sqrt(K)
    return 10.0 ** logV, 10.0 ** (logV - stdlogV), 10.0 ** (logV + stdlogV)


def load_project_module(project_dir):
    """The project's larsen_BR_volume module, or None.

    Executed in QGIS's own Python — safe, unlike the imagery stack, because the
    module is numpy-only arithmetic with no I/O. Loaded by file path rather than
    via sys.path so it always tracks the CURRENT project dir instead of a stale
    copy cached from a previous one."""
    if not project_dir:
        return None
    path = os.path.join(project_dir, "larsen_BR_volume.py")
    if not os.path.exists(path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("_landslide_larsen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "volume_source"):
        return None
    return mod


class NotConfigured(Exception):
    """Raised when the chosen fit has no coefficients yet (see LARSEN_TOTAL)."""


def total_coefficients(material, project_dir=None):
    """The total-area 4-tuple for `material`, or None if it isn't configured.

    A project's larsen_BR_volume.py may define LARSEN_TOTAL itself, which wins —
    so the constants can be kept with the science rather than in plugin code."""
    try:
        mod = load_project_module(project_dir)
    except Exception:
        mod = None
    for table in (getattr(mod, "LARSEN_TOTAL", None), LARSEN_TOTAL):
        if isinstance(table, dict) and table.get(material):
            values = table[material]
            if values is not None and len(tuple(values)) == 4:
                return tuple(float(v) for v in values)
    return None


def volume_source(A_best, A_low=None, A_high=None, material="bedrock",
                  fit="scar", project_dir=None):
    """(V_best, V_low, V_high, source_label) for an area in m^2.

    fit="scar" is the supplied calibration: prefers the project's own
    larsen_BR_volume.py and falls back to the stdlib mirror above.
    fit="total" uses the total-area coefficients, which ship EMPTY — it raises
    NotConfigured rather than quietly reusing the scar fit on a bigger polygon.

    `source_label` names which implementation and which fit produced the
    numbers, so the provenance is never left implicit."""
    if fit == "total":
        values = total_coefficients(material, project_dir)
        if values is None:
            raise NotConfigured(
                f"No total-area coefficients for '{material}'. The supplied "
                "larsen_BR_volume.py only carries the SOURCE-SCAR fit, and "
                "published total-area constants are not guessed here. Add "
                "LARSEN_TOTAL to larsen_BR_volume.py (or to the plugin's "
                "volume_calc.py) as {'" + material + "': (log10_alpha, "
                "std_log10_alpha, gamma, std_gamma)}, then measure again — or "
                "switch Fit to 'Source scar'.")
        V, Vlow, Vhigh = _coefficient_volume(values, A_best, A_low, A_high)
        return V, Vlow, Vhigh, f"total-area fit ({material})"

    try:
        mod = load_project_module(project_dir)
    except Exception:
        mod = None                      # unreadable/broken copy: use the mirror
    if mod is not None:
        V, Vlow, Vhigh = mod.volume_source(
            A_best, A_low=A_low, A_high=A_high, type=material)
        return float(V), float(Vlow), float(Vhigh), "larsen_BR_volume.py (scar fit)"
    V, Vlow, Vhigh = _fallback_volume_source(
        A_best, A_low=A_low, A_high=A_high, type=material)
    return V, Vlow, Vhigh, "built-in scar fit (project dir not set)"


def volume_thickness(area, t_best, t_low=None, t_high=None):
    """(V, Vlow, Vhigh, label) for a deposit AREA times a mean THICKNESS.

    The Toney et al. (2021) style estimate for a rock/ice avalanche when no
    stereo DEM is available to difference: V = plan area × mean deposit
    thickness. Unlike the Larsen fits there is no fitted coefficient — the range
    is just the low/high thickness carried straight through, because the mean
    thickness IS the dominant uncertainty. `area` in m², thickness in m, V in m³.
    A low ≥ best or high ≤ best is treated as 'not given' (no range on that side).
    The label names the THICKNESS_SETTINGS entry the three values are, if any, so
    the results row records which setting the volume assumed."""
    V = area * t_best
    Vlow = area * t_low if (t_low is not None and t_low < t_best) else None
    Vhigh = area * t_high if (t_high is not None and t_high > t_best) else None
    rng = (f", {t_low:g}–{t_high:g}"
           if (Vlow is not None and Vhigh is not None) else "")
    setting = thickness_setting(t_best, t_low, t_high)
    named = f"; {setting[0].split(' — ')[0]}" if setting else ""
    return V, Vlow, Vhigh, f"deposit area × thickness ({t_best:g} m{rng}{named})"
