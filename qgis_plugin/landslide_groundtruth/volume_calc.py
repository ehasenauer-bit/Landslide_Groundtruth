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

# Which calibration the area is run through. "scar" is the supplied script's fit
# and always available; "total" needs LARSEN_TOTAL filled in (see above).
FITS = [
    ("Source scar (Larsen Table S1)", "scar"),
    ("Total landslide area", "total"),
]

# What each fit expects the digitized polygon to BE — shown on the tab so the
# outline and the calibration can't silently disagree.
FIT_OUTLINE = {
    "scar": ("Outline the SOURCE SCAR only — this fit is calibrated on source "
             "area, so including the runout track or deposit inflates the volume."),
    "total": ("Outline the TOTAL landslide area — source, runout track and "
              "deposit together, matching this fit's calibration."),
}


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
