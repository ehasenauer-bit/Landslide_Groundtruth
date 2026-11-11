"""Every colour the plugin uses, named for what it MEANS.

Colours were scattered across dock.py, sar_tab.py, planet_tab.py, fusion_tab.py
and volume_tab.py, some as module constants and some as hex literals inline. The
same idea had several values — a warning was #e65100 in one place and #b3541e in
another, "ok" was both #2e7d32 and #1b7f37 — so the panel could not read as one
piece of software, and there was nowhere to fix contrast once.

Two rules this module exists to enforce:

**Contrast is checked, not guessed.** QGIS ships a light default AND a dark
Night Mapping theme, and a colour picked against one can vanish in the other.
Anything drawn on the plugin's OWN pastel row tints (PRE_BG / POST_BG) is paired
with an explicit dark foreground (ROW_FG) so it never depends on the theme's
text colour. Anything drawn on the THEME's background uses `palette(...)` roles,
which follow the theme, or a hue dark enough to hold up on both — the ratios are
recorded beside each token below.

**Colour is never the only signal.** Every status colour here has a glyph in
STATUS_GLYPH, and every cloud tint has a text value beside it in the table.
Someone who cannot separate the red from the green still gets the answer.

Nothing here imports Qt beyond QColor, so the tokens can be read in a test
without starting a QGIS application.
"""

from qgis.PyQt.QtGui import QColor

# --------------------------------------------------------------------------
# Table row tints — the plugin's own backgrounds
# --------------------------------------------------------------------------
# Pre = blue, post = green, with an EXPLICIT dark foreground: these pastels are
# fixed values, so the theme's own text colour (near-white under Night Mapping)
# would be unreadable on them. Pairing every tint with ROW_FG is what keeps the
# tables legible under both themes.
# Contrast of ROW_FG on each tint: 15.6:1 (PRE_BG), 16.1:1 (POST_BG) — both far
# above the 4.5:1 needed for body text.
PRE_BG = QColor(220, 235, 252)
POST_BG = QColor(224, 244, 226)
ROW_FG = QColor(20, 20, 20)

# Rows the run will NOT composite (ranked below the cutoff). Grey rather than
# hidden, because "considered and rejected" is information.
MUTED_FG = QColor(120, 120, 120)

# --------------------------------------------------------------------------
# Cloud cover over the AOI — severity in the "Cloud" column
# --------------------------------------------------------------------------
# Drawn ON the pale pre/post row tints above, not on the theme background, so
# these are measured against PRE_BG and POST_BG. The previous values were picked
# by eye and three of the four missed 4.5:1 (clear 3.65:1, some 3.08:1, unknown
# 3.42:1); these are the nearest shade of the SAME hue that clears it, so the
# green/amber/red reading is unchanged and only the depth moved.
#   clear   #1a7930 — 4.54:1 on PRE_BG, 4.76:1 on POST_BG
#   some    #8a5f00 — 4.66:1 / 4.90:1
#   heavy   #c71e2a — 4.75:1 / 4.99:1
#   unknown #696969 — 4.53:1 / 4.76:1
# The number itself is always printed, so the colour only ranks it.
CLOUD_CLEAR = QColor(0x1a, 0x79, 0x30)   # <= CLOUD_GREEN_MAX % of the AOI cloudy
CLOUD_SOME = QColor(0x8a, 0x5f, 0x00)    # <= CLOUD_AMBER_MAX %
CLOUD_HEAVY = QColor(0xc7, 0x1e, 0x2a)   # above that
CLOUD_UNKNOWN = QColor(0x69, 0x69, 0x69)  # no cloud number at all / snow-swamped
# Ethan's bands (2026-11-11): 0–10 % green, 11–50 % amber, over 50 % red. Applied
# to whatever number the cell prints — the AOI measurement, or the whole-scene
# value behind a "~" (the ~ is what says which one it is).
CLOUD_GREEN_MAX = 10.0
CLOUD_AMBER_MAX = 50.0

# --------------------------------------------------------------------------
# AOI coverage — severity in the "Coverage" column
# --------------------------------------------------------------------------
# How much of the AOI box the footprint fills. Same three colours as cloud (drawn
# on the same row tints, so the same measured contrast), under their own names so
# "amber" in the Coverage column means partial coverage, not cloud.
# Ethan's bands (2026-11-11): 75 % and up green, 45–74 % amber, below 45 % red.
COVER_FULL = CLOUD_CLEAR
COVER_PARTIAL = CLOUD_SOME
COVER_POOR = CLOUD_HEAVY
COVER_GREEN_MIN = 75.0
COVER_AMBER_MIN = 45.0


def cloud_color(pct):
    """Text colour for a Cloud cell showing `pct` %.

    Judged on the WHOLE number the cell prints (f"{pct:.0f}"), so a cell reading
    "10%" is green even when the measurement was 10.4 — the colour never
    disagrees with the text beside it. None (no cloud metric) -> grey."""
    if pct is None:
        return CLOUD_UNKNOWN
    shown = round(pct)
    if shown <= CLOUD_GREEN_MAX:
        return CLOUD_CLEAR
    if shown <= CLOUD_AMBER_MAX:
        return CLOUD_SOME
    return CLOUD_HEAVY


def coverage_color(pct):
    """Text colour for a Coverage cell showing `pct` % of the AOI box filled.

    Judged on the printed whole number, like cloud_color."""
    shown = round(pct)
    if shown >= COVER_GREEN_MIN:
        return COVER_FULL
    if shown >= COVER_AMBER_MIN:
        return COVER_PARTIAL
    return COVER_POOR

# --------------------------------------------------------------------------
# Maxar VHR table — warnings that are NOT about cloud
# --------------------------------------------------------------------------
# A visual-only off-nadir value (coarse DEM / steep view: look, don't trace) and a
# footprint that misses the epicentre. Same amber and red as above because they
# are drawn on the same PRE_BG / POST_BG tints and so keep the same measured
# contrast (4.66:1 / 4.90:1 and 4.75:1 / 4.99:1) — but under names that say what
# they mean, so a Cloud column's amber keeps meaning cloud. Text carries both as
# well (the angle and level, "(misses epicentre)", the row tooltip); the colour
# only flags them.
GRADE_VISUAL_FG = CLOUD_SOME
MISSES_EPICENTRE_FG = CLOUD_HEAVY

# --------------------------------------------------------------------------
# Status — one vocabulary for every panel, in BOTH QGIS themes
# --------------------------------------------------------------------------
# No single colour can clear 4.5:1 against both the QGIS light default (#F0F0F0)
# and Night Mapping (#333333): the first needs relative luminance <= 0.155 and
# the second needs >= 0.324, and those do not overlap. So there are two sets and
# status_color() picks by the running palette. The old single set failed badly in
# the dark theme (success 2.46:1, error 2.25:1) and warn failed in BOTH (3.33:1).
STATUS_COLORS_LIGHT = {
    # Deepened 2026-11-11 so each still clears 4.5:1 on the step tints below
    # (STEP_TINTS), which are darker than the bare #F0F0F0 window. Ratios are the
    # WORST case over the window and every light tint.
    "success": "#29702d",   # 4.56:1  (was #2d7a31: 3.99:1 on the Advanced tint)
    "error": "#bc2626",     # 4.56:1  (was #c62828)
    "warn": "#ac3c00",      # 4.62:1  (was #bd4200)
    "info": "palette(mid)",
}
STATUS_COLORS_DARK = {
    # Lightened 2026-11-11 for the same reason: the dark step tints are brighter
    # than #333333. Worst case over the window and every dark tint.
    "success": "#6cc470",   # 4.58:1  (was #42b348: 3.65:1 on the Options tint)
    "error": "#eb9c9c",     # 4.58:1  (was #e58080)
    "warn": "#ff955b",      # 4.55:1  (was #ff6e1f)
    "info": "palette(mid)",
}
# Back-compat: sar_tab and planet_tab import this name directly. It stays the
# light set; anything new should call status_color()/status_css() instead.
STATUS_COLORS = STATUS_COLORS_LIGHT

# Colour is never the only carrier: pair the tint with one of these.
STATUS_GLYPH = {
    "success": "✓",
    "error": "✗",
    "warn": "⚠",
    "info": "",
}

CLOUD_SNOW_MARK = "❄"             # snowflake prefix on a snow-swamped cell


def is_dark_theme():
    """True when QGIS is running a dark palette (Night Mapping and friends).

    Read from the live application palette rather than a setting, so it follows
    whatever the user actually has, including custom themes."""
    try:
        from qgis.PyQt.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return False
        c = app.palette().window().color()
        # Rec. 601 luma is plenty for a light/dark decision.
        return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) < 128
    except Exception:
        return False


def status_color(kind):
    """The colour for a status, for the theme that is actually running."""
    table = STATUS_COLORS_DARK if is_dark_theme() else STATUS_COLORS_LIGHT
    return table.get(kind, table["info"])


# A required field the user still has to fill in — the same red as `error`, as a
# 1px border rather than text, so it reads as "this one" without shouting.
def status_css(kind, extra=""):
    """A QLabel stylesheet for a status line. `kind` is a STATUS_COLORS key."""
    return "QLabel { color: %s; %s}" % (status_color(kind), extra)


def status_text(kind, msg):
    """'✓ Ready' / '⚠ …' / '✗ …' — the glyph is what survives a colour deficit."""
    glyph = STATUS_GLYPH.get(kind, "")
    return f"{glyph} {msg}" if glyph else msg


def status_html(kind, msg):
    """The same, inline, for a rich-text QLabel that mixes several lines."""
    return f'<span style="color:{status_color(kind)};">{status_text(kind, msg)}</span>'


def invalid_field_css():
    """Mark the one input that is stopping the user."""
    return "QLineEdit { border: 1px solid %s; }" % status_color("error")


# --------------------------------------------------------------------------
# Workflow-step tints — what KIND of box this is, the same on every tab
# --------------------------------------------------------------------------
# Ethan, 2026-11-11: the whole box is tinted, and the colour says which step of
# the work the box belongs to, so every tab reads the same way once one is
# learned. The hues deliberately avoid the ones that already mean something:
# red / amber / green are status and the Cloud / Coverage columns, blue and
# green are the pre / post table rows.
#
#   setup     accounts, keys, the imagery tools        grey
#   inputs    what the work starts from                 violet
#   options   choices about what is produced            teal
#   results   what came back: tables, numbers, previews warm sand
#   advanced  rarely-changed controls, and the log      darker grey
#
# Each is (background, border) per theme. The theme's own text colour is drawn
# on them (>= 13.8:1 light, >= 8.0:1 dark), and so are the status colours above,
# which is why those were re-measured against these. A tint goes on a QFrame
# AROUND the box (step_box.tinted), never on the QgsCollapsibleGroupBox itself:
# a stylesheet on the box replaces the one QGIS uses to place its title.
STEP_TINTS_LIGHT = {
    "setup": ("#E9E9E6", "#B4B2A9"),
    "inputs": ("#E9E8FB", "#AFA9EC"),
    "options": ("#DCF1E9", "#5DCAA5"),
    "results": ("#F6EBD6", "#D9B87A"),
    "advanced": ("#DFDFDB", "#A3A199"),
}
STEP_TINTS_DARK = {
    "setup": ("#3D3D3B", "#888780"),
    "inputs": ("#3A3760", "#7F77DD"),
    "options": ("#254A40", "#1D9E75"),
    "results": ("#4D4028", "#BA7517"),
    "advanced": ("#2B2B2A", "#6E6D68"),
}
STEPS = tuple(STEP_TINTS_LIGHT)
# What each step is called, for a tooltip — colour is never the only signal.
STEP_NAMES = {
    "setup": "Setup and accounts",
    "inputs": "Inputs",
    "options": "Options",
    "results": "Results",
    "advanced": "Advanced and log",
}


def step_tint(step):
    """(background, border) for a workflow step, in the running theme."""
    table = STEP_TINTS_DARK if is_dark_theme() else STEP_TINTS_LIGHT
    return table[step]


# --------------------------------------------------------------------------
# Change-raster ramps — the plugin's own output styling
# --------------------------------------------------------------------------
# Break values are EXPLICIT rather than derived, so the dBright ramp stays
# exactly the validated one (-0.30 / -0.15 / -0.05). All three products are
# signed so a landslide makes them NEGATIVE (fusion_core.EVIDENCE_SIGN), hence
# only the negative side is painted and 0 is fully transparent.
#
# Three distinct hue families, because these layers exist to be stacked and
# compared and must stay tellable apart in the legend. Blue / purple / orange
# survives the common red-green colour deficiencies.
CHANGE_RAMPS = {
    "dbright": ((-0.30, -0.15, -0.05), ["#08306b", "#2171b5", "#6baed6"],
                "brightness drop (dark debris on bright snow)"),
    "dndsi": ((-0.50, -0.25, -0.10), ["#3f007d", "#6a51a3", "#9e9ac8"],
              "snow-index drop (debris is not snow)"),
    "dndvi": ((-0.60, -0.30, -0.10), ["#7f2704", "#d94801", "#fd8d3c"],
              "vegetation loss"),
}
