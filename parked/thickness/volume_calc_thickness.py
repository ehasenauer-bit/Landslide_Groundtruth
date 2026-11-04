"""PARKED 2026-11-04 — deposit thickness settings and V = A × t̄.

Removed verbatim from qgis_plugin/landslide_groundtruth/volume_calc.py (commit
cae8034) with the Volume tab's area × thickness fit. See README.md here.
"""
import math

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
