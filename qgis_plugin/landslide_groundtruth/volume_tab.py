"""The Volume tab: a digitized outline -> a landslide volume.

Where this sits in the workflow: the other tabs get you imagery and elevation
change; this one turns the polygons you digitize over that imagery into numbers.

The tab reads top to bottom in the order the work is done — one slide per
project is the normal case:

  Outlines  Draw (or assign) the TOTAL outline and the source scar three ways —
            BEST, LOW (conservative) and HIGH (generous); each has its own
            New / Draw buttons.
  Settings  What failed (bedrock/soil), for Larsen et al. (2010).
  Measure   Larsen et al. (2010), Table S1 — THE volume — and records the row:
              the BEST source outline through the calibrated relation, its range
                the published fit spread widened by source low/high;
              source low and source high each through the same relation, a
                volume per reading of the scar.
            The total outline gives the implied mean depth (Larsen ÷ total
            area), the centerline and the feature Write to layer stamps.
  Centerline & drop  A medial-axis centerline of the total outline (see
            centerline.py) and, with an elevation raster assigned, the crown-to-
            toe fall height and travel angle read along it. A HILLSHADE is
            shading (0-255), not elevation, so a byte raster is refused.
  Write to layer  Stamps every number onto the TOTAL outline's own feature;
            Copy results puts the table on the clipboard.
  Results / Log  The row table (one row per slide name: re-measuring a slide
            replaces its row) with CSV export, and the running log.

The explanatory text lives behind the "? Help" toggle in the header; tooltips
carry the detail either way, and live warnings always show.

WHICH OUTLINE LARSEN READS is fixed: the relation is calibrated on source-scar
area, so feeding it a total outline reads high.

Roles are assigned by layer rather than by map selection, so the input is
explicit and re-measurable, and layers named for their role — "Total Area",
"Source Area (low)" — are recognised automatically, so a project that already
holds the outlines comes up ready to measure. The New buttons create each
outline already named and assigned, as a GeoPackage in the project's folder
rather than a scratch layer — they are the measurement, so they are kept like
one (see ROLE_LAYERS). A role layer holding several polygons contributes its
largest; the rest are reported, not combined.

Area is ellipsoidal plan-view area (QgsDistanceArea + the project ellipsoid) —
the same thing $area gives, and the same quantity Larsen et al. measured from
imagery, so the published coefficients stay valid.

The elevation-change (∫Δh) fit and DEM differencing were removed from this tab
on 2026-10-23 and parked verbatim in parked/ddem/ (dem_diff.py stays — the 3D
viewer uses it). The total-area Larsen fit, whose coefficients ship empty, is
no longer offered either; volume_calc still carries it.

Deposit area × thickness (Toney et al. 2021), which ran beside Larsen with
neither the headline, was removed on 2026-11-04 and parked verbatim in
parked/thickness/: unlike Larsen et al.'s relation, which earlier studies of
these landslides used, its thickness had not been checked against measured
volumes.

The volume arithmetic itself lives in volume_calc, which prefers the project's
canonical larsen_BR_volume.py and names whichever implementation ran.
"""
import csv
import math
import os
import re
from functools import partial

from qgis.PyQt.QtCore import QMetaType, Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QFileDialog, QApplication, QHeaderView, QToolButton, QSizePolicy,
)
from qgis.core import (
    Qgis, QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsDistanceArea, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsGeometry, QgsPointXY,
    QgsField, QgsFeature, QgsLineSymbol, QgsFillSymbol,
    QgsVectorFileWriter, QgsDefaultValue, QgsSingleSymbolRenderer,
)
from qgis.gui import QgsCollapsibleGroupBox

from . import centerline as centerline_mod
from . import volume_calc
from . import layer_group as lg
from .flow_layout import FlowRow
from .theme import status_css


def _utm_epsg(lat, lon):
    """EPSG code of the UTM zone containing a point.

    Same formula as dem_diff.utm_epsg, kept local on purpose: dem_diff pulls in
    numpy and the GDAL bindings for the differencing math, and this tab needs
    nothing but Qt and QGIS core — no reason for a three-line helper to make it
    fail to load if those are unavailable."""
    zone = int((lon + 180.0) / 6.0) + 1
    return (32700 if lat < 0 else 32600) + zone

# Scratch layer the derived centerlines are collected in. Reused across slides
# (looked up by name if its id goes stale) so a session leaves one tidy layer
# rather than one per measurement.
CENTERLINE_LAYER = "Landslide centerlines"

# Whether the explanatory text is showing. Per user, not per project: it is about
# how well you know the tab, not about the slide — so QgsSettings, not the .qgz.
HELP_SETTING = "landslide_groundtruth/volume_show_help"

# ---------------------------------------------------------------------------
# Recognising role layers by name, so a project that already carries the
# outlines — "Total Area", "Source Area (low)", "Source Area (high)" — comes up
# ready to measure instead of needing the drop-downs set by hand every session.
#
# Matched on TOKENS of the case-folded name with punctuation flattened, so all of
# "Source Area (low)", "source area - LOW", "SOURCE_AREA_LOW" and a prefixed
# "Barry Arm Source Area (low)" hit the same rule. Tokens rather than substrings
# because substring matching would let "lowland" count as "low".
#
# A name has to carry a SUBJECT phrase and, for the source roles, a role token.
# A source layer with NO role token at all is taken as the best source estimate,
# since a bare "Source Area" is the natural name for it — but only when nothing
# matched "best" outright.
TOTAL_SUBJECTS = (frozenset({"total", "area"}), frozenset({"total", "landslide"}),
                  frozenset({"landslide", "area"}), frozenset({"total", "extent"}))
SOURCE_SUBJECT = frozenset({"source", "area"})
ROLE_NAME_TOKENS = {
    "best": frozenset({"best", "mid", "middle", "medium"}),
    "low": frozenset({"low", "lower", "min", "minimum", "conservative"}),
    "high": frozenset({"high", "higher", "max", "maximum", "generous"}),
}
# every role word, so "has a role token" can be tested without picking one
ROLE_NAME_ANY = frozenset().union(*ROLE_NAME_TOKENS.values())

# The source roles, in panel order: (key, role word used in messages). The total
# role is handled separately — it is the one the volume is computed from.
SOURCE_ROLES = (("best", "best"), ("low", "low"), ("high", "high"))

# Every outline role, and what its New button produces: (role label as the
# drop-down spells it, layer-name suffix, file stem, fill style). GeoPackages in
# the project's own folder rather than memory layers — they ARE the measurement,
# so leaving them in memory puts the afternoon's digitizing one forgotten "Save
# Features As…" away from being lost. The suffix carries the role word (see
# ROLE_NAME_TOKENS above), so the project reopens with the drop-down already
# pointing at the file, and the file name mirrors the layer name so the folder
# reads like the layer panel.
#
# The style is a fixed convention, the same on every outline the tab creates:
# total RED, source BLUE (low/high thinner), outline only so the imagery under
# the scar stays readable. A layer the tab created takes its role's colour
# whenever it is assigned one (_colour_by_role).
ROLE_LAYERS = {
    "total": ("Total area", "Total Area", "total_area",
              {"color": "0,0,0,0", "style": "no", "outline_color": "220,30,30",
               "outline_width": "0.8"}),
    "best": ("Source (best)", "Source Area (best)", "source_area_best",
             {"color": "0,0,0,0", "style": "no", "outline_color": "30,90,230",
              "outline_width": "0.8"}),
    "low": ("Source (low)", "Source Area (low)", "source_area_low",
            {"color": "0,0,0,0", "style": "no", "outline_color": "30,90,230",
             "outline_width": "0.4", "outline_style": "dash"}),
    "high": ("Source (high)", "Source Area (high)", "source_area_high",
             {"color": "0,0,0,0", "style": "no", "outline_color": "30,90,230",
              "outline_width": "0.4", "outline_style": "dot"}),
}

# Marks a layer this tab created, so its colour may follow its role. Nothing
# else is ever restyled: a layer you made or styled yourself keeps its look.
OUTLINE_PROP = "landslide_groundtruth/outline"

# Every outline the tab creates carries its own plan area, kept current by QGIS
# as the polygon is drawn or reshaped, so it is ready to label or export with no
# Measure. In km² because it is for display; the measurement itself
# (_measure_area) never reads it. Measured in equal-area EPSG:6933, so it is an
# ellipsoidal area in m² whatever the layer's CRS — $area would follow the
# project's area-unit setting instead — and it agrees with _measure_area to
# ~0.01%. Read-only in the attribute form, since an edit would be recomputed.
AREA_FIELD = "area_km2"
AREA_EXPRESSION = ("round(area(transform($geometry, @layer_crs, 'EPSG:6933'))"
                   " / 1e6, 4)")

# What each role's New button says it does.
_SAVED = ("Saved as a GeoPackage in this project's folder, not a scratch layer — "
          "the outline survives closing QGIS, and the project reopens measuring "
          "it. Press it again for another attempt: an existing file is never "
          "written over.")
NEW_TIPS = {
    "total": ("Create the TOTAL landslide outline, assign it to “Total area” and "
              "start digitizing. Drawn as a red outline, with an "
              f"“{AREA_FIELD}” field QGIS keeps current as you draw.\n\n" + _SAVED),
    "best": ("Create the BEST source-scar outline — the evacuated source area "
             "alone — assign it to “Source (best)” and start digitizing. This is "
             "the outline Larsen et al. (2010) converts. Drawn as a blue outline, "
             f"with the same “{AREA_FIELD}” field.\n\n" + _SAVED),
    "low": ("Create the CONSERVATIVE (smaller) source-scar outline, assign it to "
            "“Source (low)” and start digitizing. Larsen et al. (2010) gives it "
            "its own volume, the low estimate. Dashed blue.\n\n" + _SAVED),
    "high": ("Create the GENEROUS (larger) source-scar outline, assign it to "
             "“Source (high)” and start digitizing. Larsen et al. (2010) gives it "
             "its own volume, the high estimate. Dotted blue.\n\n" + _SAVED),
}

# Attribute fields the write-back adds to the TOTAL outline. (name, type, source
# key in a results row.) Kept short and lower-case so they survive a Shapefile
# round-trip (10-character field-name limit) as well as GeoPackage. vsrc_* is the
# Larsen et al. volume of the best source outline with its range; vlowsrc_m3 and
# vhisrc_m3 are the same relation on the source low and high outlines.
WRITEBACK_FIELDS = [
    ("slide", QMetaType.Type.QString, "name"),
    ("material", QMetaType.Type.QString, "material_label"),
    ("total_m2", QMetaType.Type.Double, "a_total"),
    ("src_m2", QMetaType.Type.Double, "src_best"),
    ("src_lo_m2", QMetaType.Type.Double, "src_low"),
    ("src_hi_m2", QMetaType.Type.Double, "src_high"),
    ("length_m", QMetaType.Type.Double, "length"),
    ("z_top_m", QMetaType.Type.Double, "z_top"),
    ("z_bot_m", QMetaType.Type.Double, "z_bottom"),
    ("drop_m", QMetaType.Type.Double, "drop"),
    ("reach_deg", QMetaType.Type.Double, "reach_angle"),
    ("vsrc_m3", QMetaType.Type.Double, "v_larsen"),
    ("vsrc_lo_m3", QMetaType.Type.Double, "v_larsen_lo"),
    ("vsrc_hi_m3", QMetaType.Type.Double, "v_larsen_hi"),
    ("vlowsrc_m3", QMetaType.Type.Double, "v_larsen_srclo"),
    ("vhisrc_m3", QMetaType.Type.Double, "v_larsen_srchi"),
]
# What an older write-back left on a layer: one fit per Measure (to 2026-10-23),
# then area × thickness beside Larsen (to 2026-11-04, parked/thickness/). Never
# touched now — just named in the log, so a stale vol_m3 or vthk_m3 beside the
# current fields is explained rather than mistaken for one of them.
LEGACY_WRITEBACK = ("fit", "area_m2", "area_lo_m2", "area_hi_m2", "vol_m3",
                    "vol_lo_m3", "vol_hi_m3", "vol_ero_m3", "vol_dep_m3",
                    "thick_m", "thick_lo_m", "thick_hi_m",
                    "vthk_m3", "vthk_lo_m3", "vthk_hi_m3")

# Measurement units we refuse to convert from. Square degrees because QGIS only
# has one crude global factor for them, and "unknown" because that is what a CRS
# the install cannot resolve reports — in both cases the number is not an area in
# any usable sense, and feeding it to the volume relation would produce a
# confident answer that is wrong by orders of magnitude. See _measure_area.
UNUSABLE_AREA_UNITS = (Qgis.AreaUnit.SquareDegrees, Qgis.AreaUnit.Unknown)
UNUSABLE_LENGTH_UNITS = (Qgis.DistanceUnit.Degrees,
                         Qgis.DistanceUnit.Unknown)

TABLE_COLS = ["Slide", "Material",
              "V Larsen (m³)", "V Larsen low", "V Larsen high",
              "V src low (m³)", "V src high (m³)",
              "Total area (m²)", "Src best (m²)", "Src low (m²)", "Src high (m²)",
              "Length (m)", "Drop (m)", "Layers"]
# columns right-aligned as numbers
TABLE_NUMERIC = range(2, 13)

# CSV header + the row keys behind it, so the export carries raw numbers rather
# than the table's thousands-separated display strings. vol_larsen_lo/hi are the
# best outline's range; vol_larsen_src_low/high the volumes of the other two
# source outlines.
CSV_FIELDS = [
    ("slide", "name"), ("material", "material"),
    ("vol_larsen_m3", "v_larsen"), ("vol_larsen_lo_m3", "v_larsen_lo"),
    ("vol_larsen_hi_m3", "v_larsen_hi"),
    ("vol_larsen_src_low_m3", "v_larsen_srclo"),
    ("vol_larsen_src_high_m3", "v_larsen_srchi"),
    ("total_area_m2", "a_total"),
    ("source_best_m2", "src_best"), ("source_low_m2", "src_low"),
    ("source_high_m2", "src_high"),
    ("centerline_m", "length"), ("centerline_method", "length_method"),
    ("elev_top_m", "z_top"), ("elev_bottom_m", "z_bottom"),
    ("drop_m", "drop"), ("reach_angle_deg", "reach_angle"),
    ("hl_ratio", "hl_ratio"), ("elevation_layer", "dem_name"),
    ("total_layer", "total_layer"), ("source_best_layer", "best_layer"),
    ("source_low_layer", "low_layer"), ("source_high_layer", "high_layer"),
    ("written_layer", "layer_name"), ("written_feature_id", "fid"),
    ("larsen_calculator", "larsen_calc"),
]

# The event columns: who the event was and where the scar sits relative to it.
# Appended (never interleaved).
from .verdict import EVENT_CSV_FIELDS
CSV_FIELDS = CSV_FIELDS + EVENT_CSV_FIELDS


class VolumeTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings
        self._current = None        # the slide on screen — the same dict as its row
        self._rows = []             # result rows (raw values), one per slide name
        self._cl_layer_id = None    # scratch centerline layer
        self._cl_fid = None         # centerline feature for the current slide
        self._planar_noted = False  # measurement-mode note already logged
        self._watched = []          # layer ids whose edit signals we follow
        self._help_labels = []      # explanatory text the "? Help" toggle shows
        # role -> set by hand (stop name-matching it) / last name-matched layer
        self._role_pinned = {"total": False, "best": False,
                             "low": False, "high": False}
        self._role_auto = {}
        self._build_ui()
        # after every combo exists: fill them and show the assignment readout
        self._refresh_layers()

    # ---------- UI ----------
    def _build_ui(self):
        """Top to bottom in the order the work is done: outlines, settings,
        Measure, the volumes, centerline & drop, Write to layer. The results
        table and the log are collapsed underneath — with one slide per project
        the Volumes box already shows the row."""
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # Header: the help toggle and the one layer re-sync. Everything the tab
        # explains sits behind the toggle; tooltips carry the detail regardless,
        # and live warnings (swapped outlines, a skipped fit) always show.
        head = QHBoxLayout()
        head.addStretch(1)
        self._help_btn = QToolButton()
        self._help_btn.setText("? Help")
        self._help_btn.setCheckable(True)
        self._help_btn.setToolTip(
            "Show or hide the explanations on this tab. Remembered for next "
            "time. Warnings about the measurement always show.")
        head.addWidget(self._help_btn)
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip(
            "Re-sync every layer picker on this tab — for a layer you just "
            "renamed, or one QGIS added without telling the tab. A DEM from "
            "elsewhere has to be added to QGIS first (Layer ▸ Add Raster Layer); "
            "this tab only lists what the project already holds.")
        refresh.clicked.connect(self._refresh_layers)
        head.addWidget(refresh)
        root.addLayout(head)

        root.addWidget(self._help(
            "Draw the TOTAL outline and the source scar — best, and a low and "
            "high reading of it — set what failed, then Measure. The volume is "
            "Larsen et al. (2010): the best source outline gives the estimate, "
            "source low and high give the low and high estimates. Measure also "
            "records the slide's row."))

        root.addWidget(self._build_outline_box())
        root.addWidget(self._build_settings_box())

        btns = FlowRow()
        self.measure_btn = QPushButton("Measure")
        self.measure_btn.setToolTip(
            "Measure the assigned outlines and run Larsen et al. (2010) on each "
            "source outline: BEST for the estimate (source low/high widen its "
            "range), LOW and HIGH for their own volumes. The TOTAL outline gives "
            "the implied mean depth. Records the result as this slide's row — "
            "measuring the same slide again replaces it.")
        self.measure_btn.clicked.connect(self._measure)
        f = self.measure_btn.font()
        f.setBold(True)
        self.measure_btn.setFont(f)
        self.measure_btn.setDefault(True)
        btns.addWidget(self.measure_btn)
        root.addWidget(btns)

        root.addWidget(self._build_volumes_box())
        root.addWidget(self._build_centerline_box())

        act = FlowRow()
        self.write_btn = QPushButton("Write to layer")
        self.write_btn.setToolTip(
            "Stamp the slide's numbers — the Larsen et al. volumes (best with "
            "its range, source low, source high), every area, length and drop — "
            "onto the TOTAL outline's "
            "own feature as attributes (fields are added if missing), so they "
            "travel with the geometry. The source outline stands in when no "
            "total is drawn. Needs a layer that accepts attribute edits.")
        self.write_btn.clicked.connect(self._write_to_layer)
        self.copy_btn = QPushButton("Copy results")
        self.copy_btn.setToolTip(
            "Copy the results table (this slide's row, plus any others) to the "
            "clipboard as tab-separated text, with a header row — pastes "
            "straight into a spreadsheet.")
        self.copy_btn.clicked.connect(self._copy_table)
        for b in (self.write_btn, self.copy_btn):
            act.addWidget(b)
        root.addWidget(act)

        root.addWidget(self._build_results_box())
        root.addWidget(self._build_log_box())
        root.addStretch(1)

        on = _truthy(self.settings.value(HELP_SETTING, False))
        self._help_btn.setChecked(on)
        self._help_btn.toggled.connect(self._set_help_visible)
        self._set_help_visible(on)

    def _help(self, text=""):
        """A grey explanatory label the "? Help" toggle shows and hides."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("QLabel { color: palette(mid); }")
        self._help_labels.append(lbl)
        return lbl

    def _set_help_visible(self, on):
        for lbl in self._help_labels:
            lbl.setVisible(bool(on))
        try:
            self.settings.setValue(HELP_SETTING, bool(on))
        except Exception:
            pass

    def _build_outline_box(self):
        """Which outlines are measured — and the buttons that make them.

        Each role is one row: its layer picker, New (create the outline as a
        GeoPackage beside the project, named and assigned, and start drawing)
        and Draw (digitize another polygon into the assigned layer). Creating,
        naming, saving and assigning an outline is one intention, and splitting
        it across panels is how an afternoon's digitizing ends up in a memory
        layer with no role. Everything stays reversible from the drop-downs.

        Every button hands off to QGIS's own digitizing tools rather than
        reimplementing them."""
        box = QgsCollapsibleGroupBox("Outlines")
        box.setSaveCollapsedState(False)
        v = QVBoxLayout(box)
        v.addWidget(self._help(
            "SOURCE (blue) — the evacuated scar alone, three ways: best, low "
            "(conservative, dashed) and high (generous, dotted). Larsen et al. "
            "(2010) converts each into a volume, and taking in the track or "
            "deposit inflates it. TOTAL (red) — the whole affected area: source "
            "scar, runout track and deposit; it gives the implied mean depth, "
            "the centerline, and the feature Write to layer stamps. New makes "
            "the outline as a GeoPackage beside the project, assigned and ready "
            "to draw; Draw adds another polygon to the assigned layer (a layer "
            "is measured by its largest polygon)."))

        form = QFormLayout()
        self.name_edit = QLineEdit("slide 1")
        self.name_edit.setToolTip(
            "Label for this slide in its results row, the attributes Write to "
            "layer stamps and the centerline layer — and the start of the New "
            "outlines' file names. Measuring again under the same name replaces "
            "the slide's row; a new name adds a row.")
        form.addRow("Slide", self.name_edit)

        # underscore-prefixed: these hold layer references, rebuilt from the
        # project on every refresh, so persisting them would only write project
        # entries (marking it dirty) for values that are rediscovered anyway.
        self._total_combo = QComboBox()
        self._total_combo.setToolTip(
            "Layer holding the TOTAL landslide outline — the whole affected "
            "area, source through runout to deposit. The Larsen et al. volume "
            "is spread over it for the implied mean depth; the centerline is "
            "spined from it and Write to layer stamps it.\n\n"
            "Filled in automatically from a layer named “Total Area”, “Total "
            "Landslide Area” or “Landslide Area”. Capitalisation and punctuation "
            "don't matter.")
        form.addRow("Total area", self._role_row("total", self._total_combo))
        self._best_combo = QComboBox()
        self._best_combo.setToolTip(
            "Layer holding the BEST source-scar interpretation — the evacuated "
            "scar alone. Larsen et al. (2010) converts THIS outline.\n\n"
            "Filled in automatically from “Source Area (best)”, or a plain "
            "“Source Area”.")
        form.addRow("Source (best)", self._role_row("best", self._best_combo))
        # In the main form, not a collapsed box, since 2026-11-04: with Larsen et
        # al. the tab's one volume, the low and high readings of the scar are the
        # low and high estimates, not an optional extra.
        self._low_combo = QComboBox()
        self._low_combo.setToolTip(
            "Layer holding the CONSERVATIVE (smaller) source-scar "
            "interpretation, from “Source Area (low)”. Larsen et al. (2010) "
            "converts it into the LOW estimate; paired with source high it also "
            "widens the best estimate's range.")
        form.addRow("Source (low)", self._role_row("low", self._low_combo))
        self._high_combo = QComboBox()
        self._high_combo.setToolTip(
            "Layer holding the GENEROUS (larger) source-scar interpretation, "
            "from “Source Area (high)”. Larsen et al. (2010) converts it into the "
            "HIGH estimate; paired with source low it also widens the best "
            "estimate's range.")
        form.addRow("Source (high)", self._role_row("high", self._high_combo))
        v.addLayout(form)

        self.role_lbl = QLabel()
        self.role_lbl.setWordWrap(True)
        self.role_lbl.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(self.role_lbl)

        for role, combo in (("total", self._total_combo),
                            ("best", self._best_combo), ("low", self._low_combo),
                            ("high", self._high_combo)):
            combo.currentIndexChanged.connect(self._update_role_label)
            # a genuine user change pins the role; programmatic fills are done
            # with signals blocked, so they don't
            combo.currentIndexChanged.connect(partial(self._pin_role, role))
            combo.currentIndexChanged.connect(partial(self._colour_by_role, role))
            combo.currentIndexChanged.connect(self._watch_role_layers)

        project = QgsProject.instance()
        project.layersAdded.connect(self._refresh_layers)
        # layersAdded fires BEFORE the layer reaches the layer tree, which is
        # what _polygon_layers reads — so on its own every picker lagged one
        # layer behind until ⟳ was pressed. legendLayersAdded comes after.
        project.legendLayersAdded.connect(self._refresh_layers)
        project.layersRemoved.connect(self._refresh_layers)
        # A role set by hand (or by a New button) is pinned so name matching
        # leaves it alone — but the pin belongs to THAT project's layers. Opening
        # another one has to retire it, or the new project's correctly-named
        # outlines stay out of the drop-downs for the rest of the session and the
        # tab looks like it can't see them.
        project.readProject.connect(self._project_changed)
        project.cleared.connect(self._project_changed)
        return box

    def _role_row(self, role, combo):
        """[layer picker][New][Draw] for one outline role."""
        label = ROLE_LAYERS[role][0]
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        # layer names run long ("Iliamna 2026 Source Area (best)"); elide the
        # closed combo rather than let the longest set the dock's minimum width.
        # Short, too: with New and Draw beside it a longer minimum crowds the
        # row's own label out of a 400 px dock.
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(4)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        h.addWidget(combo, 1)
        new = QToolButton()
        new.setText("New")
        new.setToolTip(NEW_TIPS[role])
        new.clicked.connect(partial(self._new_role_layer, role))
        draw = QToolButton()
        draw.setText("Draw")
        draw.setToolTip(
            f"Digitize another polygon into the layer assigned to {label}: puts "
            "it in edit mode and arms QGIS's Add Polygon tool. A layer holding "
            "several polygons is measured by its largest.")
        draw.clicked.connect(partial(self._draw_role, role))
        h.addWidget(new)
        h.addWidget(draw)
        return w

    def _project_changed(self, *_args):
        """A different project is open: forget which roles were set deliberately
        and which we matched, then match again against the layers it brought.

        The results go too. They belong to the project they were measured in —
        their layer ids die with it — and leaving the last slide on screen would
        set the next project's detection against the last project's volumes."""
        self._role_pinned = {role: False for role in self._role_pinned}
        self._role_auto = {}
        if self._rows:
            self._append_log(
                f"New project: cleared {len(self._rows)} result row(s) measured "
                "in the previous one.")
        self._rows = []
        self._current = None
        self._cl_fid = None
        for b in (self.centerline_btn, self.remeasure_btn, self.sample_btn):
            b.setEnabled(False)
        self._show_current()
        self._refresh_layers()

    # ---------- layer plumbing ----------
    def _polygon_layers(self):
        """Polygon layers in the order they appear in the layer tree, so the
        list reads the same way round as the panel the user is looking at."""
        project = QgsProject.instance()
        try:
            ordered = project.layerTreeRoot().layerOrder()
        except Exception:
            ordered = list(project.mapLayers().values())
        return [l for l in ordered
                if isinstance(l, QgsVectorLayer)
                and l.geometryType() == Qgis.GeometryType.Polygon
                and l.name() != CENTERLINE_LAYER]

    def _refresh_layers(self, *_args):
        """Repopulate the four role drop-downs and the relief pickers.

        Each keeps whatever layer it was already pointing at, so adding a layer
        (or pressing ⟳) never silently reassigns a role you had set. A role that
        is still unset gets filled in from the layer NAMES (see
        ROLE_NAME_TOKENS), which is what makes an existing project measurable
        without setting three drop-downs by hand."""
        if not hasattr(self, "_best_combo"):
            return                       # still constructing; __init__ calls back
        layers = self._polygon_layers()
        # Every role carries an explicit "— none —", the total included. Without
        # one it would fall back to whichever layer happened to sort first and
        # Measure would silently report a layer nobody chose; unset has to be a
        # state the tab can be in.
        roles = (("total", self._total_combo), ("best", self._best_combo),
                 ("low", self._low_combo), ("high", self._high_combo))
        for _role, combo in roles:
            keep = combo.currentData() if combo.count() else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— none —", None)
            for layer in layers:
                combo.addItem(layer.name(), layer.id())
            idx = combo.findData(keep)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

        matches = self._match_role_layers(layers)
        for role, combo in roles:
            # A role you set yourself is never touched, and one you deliberately
            # cleared is never re-filled — that is what _role_pinned records.
            # A role WE filled in stays open to revision, so a better-named layer
            # loaded afterwards can still claim it: otherwise the result depends
            # on the order the layers happened to be added, and a bare "Source
            # Area" seen first would keep the source-best role from an explicit
            # "Source Area (best)" arriving second.
            current = combo.currentData()
            ours = current is not None and self._role_auto.get(role) == current
            if self._role_pinned.get(role) or (current is not None and not ours):
                continue
            layer = matches.get(role)
            if layer is None:
                continue
            idx = combo.findData(layer.id())
            if idx < 0:
                continue
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)
            if self._role_auto.get(role) != layer.id():
                self._role_auto[role] = layer.id()
                self._append_log(
                    f"{ROLE_LAYERS[role][0]}: matched “{layer.name()}” by name. "
                    "Change its drop-down to override.")

        self._watch_role_layers()
        self._refresh_relief_combos(layers)
        self._update_role_label()

    def _project_rasters(self):
        """Raster layers in layer-tree drawing order — the DEM candidates."""
        project = QgsProject.instance()
        try:
            ordered = project.layerTreeRoot().layerOrder()
        except Exception:
            ordered = list(project.mapLayers().values())
        return [l for l in ordered if isinstance(l, QgsRasterLayer)]

    def _refresh_relief_combos(self, layers=None):
        """Repopulate the "Centerline from" and "Elevation (DEM)" pickers,
        each keeping its current selection so a layer add never reassigns them.

        The centerline picker offers Auto (data None — follow the measurement)
        plus every polygon layer; the DEM picker offers "— none —" plus every
        single-band raster, the same DEM filter the 3D viewer tab uses."""
        if not hasattr(self, "_cl_source_combo"):
            return                        # still constructing
        if layers is None:
            layers = self._polygon_layers()

        combo = self._cl_source_combo
        keep = combo.currentData() if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Auto — total outline (else source)", None)
        for layer in layers:
            combo.addItem(layer.name(), layer.id())
        idx = combo.findData(keep)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

        dem = self._dem_combo
        keep_dem = dem.currentData() if dem.count() else None
        dem.blockSignals(True)
        dem.clear()
        dem.addItem("— none —", None)
        for lyr in self._project_rasters():
            # single-band rasters are the elevation candidates; a hillshade is
            # single-band too and is caught later, at sample time (_is_hillshade)
            if lyr.bandCount() == 1:
                dem.addItem(lyr.name(), lyr.id())
        idx = dem.findData(keep_dem)
        dem.setCurrentIndex(idx if idx >= 0 else 0)
        dem.blockSignals(False)

    def _match_role_layers(self, layers):
        """{role: layer} for layers whose NAME identifies their role.

        Roles are "total" plus the three source ones. First match in layer-tree
        order wins, and a layer already claimed by one role can't be claimed by
        another — otherwise a name carrying two role words would land in two
        drop-downs and quietly measure itself against itself."""
        matches, claimed = {}, set()
        tokenised = [(layer, _name_tokens(layer.name())) for layer in layers]

        # the total outline first: it drives the volume, so it gets first refusal
        for layer, tokens in tokenised:
            if any(subject <= tokens for subject in TOTAL_SUBJECTS):
                matches["total"] = layer
                claimed.add(layer.id())
                break

        for role, _label in SOURCE_ROLES:
            wanted = ROLE_NAME_TOKENS[role]
            for layer, tokens in tokenised:
                if layer.id() in claimed or not SOURCE_SUBJECT <= tokens:
                    continue
                if tokens & wanted:
                    matches[role] = layer
                    claimed.add(layer.id())
                    break
        if "best" not in matches:
            # a bare "Source Area" with no role word is the best source estimate
            for layer, tokens in tokenised:
                if layer.id() in claimed or not SOURCE_SUBJECT <= tokens:
                    continue
                if not tokens & ROLE_NAME_ANY:
                    matches["best"] = layer
                    claimed.add(layer.id())
                    break
        return matches

    def _pin_role(self, role, *_args):
        """Record that this role was set deliberately, so name matching stops
        touching it — including when it was deliberately set back to none."""
        self._role_pinned[role] = True

    def _layer_from(self, combo):
        layer = QgsProject.instance().mapLayer(combo.currentData() or "")
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _watch_role_layers(self, *_args):
        """Follow every assigned outline's own edit signals, so the area readout
        stays true while you digitize — not only when a drop-down changes."""
        signals = ("featureAdded", "featuresDeleted", "geometryChanged")
        for lid in list(self._watched):
            watched = QgsProject.instance().mapLayer(lid)
            if watched is None:
                continue
            for signal in signals:
                try:
                    getattr(watched, signal).disconnect(self._update_role_label)
                except (TypeError, RuntimeError, AttributeError):
                    pass
        self._watched = []
        for role in ROLE_LAYERS:
            layer = self._role_layer(role)
            if layer is None or layer.id() in self._watched:
                continue
            for signal in signals:
                try:
                    getattr(layer, signal).connect(self._update_role_label)
                except (TypeError, RuntimeError, AttributeError):
                    pass
            self._watched.append(layer.id())

    # ---------- the measured layers (roles) ----------
    def _role_combo(self, role):
        return {"total": self._total_combo, "best": self._best_combo,
                "low": self._low_combo, "high": self._high_combo}[role]

    def _role_layer(self, role):
        """The layer assigned to 'total' / 'best' / 'low' / 'high', or None."""
        return self._layer_from(self._role_combo(role))

    def _layer_areas(self, layer):
        """[(area_m2, feature)] for a layer, LARGEST FIRST.

        The per-polygon breakdown, used for reporting and for the nesting check.
        What a role layer actually CONTRIBUTES is the total of these — see
        _layer_total."""
        out = []
        for f in layer.getFeatures():
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            area = self._measure_area(geom, layer.crs())
            if area and area > 0:
                out.append((area, f))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def _layer_total(self, layer):
        """(area_m2, largest_feature, [(area, feature)…]) for one role layer.

        A role layer contributes its LARGEST polygon: every role here is one
        interpretation of one outline, so several polygons in a layer are
        alternative or superseded attempts at the same thing, not parts of it.
        They are reported rather than combined — adding them up, or unioning
        them, would merge three interpretations into one inflated area."""
        measured = self._layer_areas(layer)
        if not measured:
            return None, None, []
        return measured[0][0], measured[0][1], measured

    def _update_role_label(self, *_args):
        """Live readout of the assigned areas, so a mis-assignment is visible
        before Measure rather than after — including the ordering, which is the
        easy mistake to make once the roles are picked by hand — and of which
        fit a missing outline will leave out."""
        if not hasattr(self, "role_lbl"):
            return
        parts, areas = [], {}
        for role, label in (("total", "TOTAL"), ("low", "src low"),
                            ("best", "src best"), ("high", "src high")):
            layer = self._role_layer(role)
            if layer is None:
                continue
            measured = self._layer_areas(layer)
            if not measured:
                parts.append(f"{label}: “{layer.name()}” has no polygon yet")
                continue
            areas[role] = measured[0][0]
            extra = f" (largest of {len(measured)})" if len(measured) > 1 else ""
            parts.append(f"{label} {_km2(measured[0][0])}{extra}")
        if not parts:
            self.role_lbl.setText(
                "No outline yet — press New beside Total area and Source (best) "
                "to draw them, or pick layers that already hold them.")
            self.role_lbl.setStyleSheet("QLabel { color: palette(mid); }")
            return
        text = "  ·  ".join(parts)
        problem = self._role_order_problem(areas)
        if problem:
            self.role_lbl.setText(f"{text}\n⚠ {problem}")
            self.role_lbl.setStyleSheet(status_css("error"))
            return
        note = ""
        if "best" not in areas:
            note = ("No source (best) outline — Larsen et al. (2010) needs it, "
                    "so Measure will record the areas but no volume.")
        elif "total" not in areas:
            note = ("No total outline — no implied mean depth, and the "
                    "centerline and Write to layer fall back to the source scar.")
        self.role_lbl.setText(f"{text}\n{note}" if note else text)
        self.role_lbl.setStyleSheet(status_css("warn") if note
                                    else "QLabel { color: palette(mid); }")

    def _role_order_problem(self, areas):
        """Plain-language description of an inconsistent assignment, or None.

        Checked here rather than left to the science module, which raises a bare
        'A_low must be smaller than A_best' — that doesn't say WHICH layer is the
        problem when the roles were assigned by hand. The total-vs-source check
        catches the assignment most likely to go unnoticed: a source outline in
        the Total area slot, which would silently under-report the volume."""
        total, best = areas.get("total"), areas.get("best")
        low, high = areas.get("low"), areas.get("high")
        if best is not None:
            if low is not None and low > best:
                return (f"Source low ({_fmt(low)} m²) is larger than source best "
                        f"({_fmt(best)} m²) — those two layers look swapped.")
            if high is not None and high < best:
                return (f"Source high ({_fmt(high)} m²) is smaller than source "
                        f"best ({_fmt(best)} m²) — those two look swapped.")
            if low is not None and high is not None and low > high:
                return (f"Source low ({_fmt(low)} m²) is larger than source high "
                        f"({_fmt(high)} m²).")
        if total is not None:
            biggest_source = max([a for a in (best, low, high) if a is not None],
                                 default=None)
            if biggest_source is not None and total < biggest_source:
                return (f"The total area ({_fmt(total)} m²) is smaller than a "
                        f"source area ({_fmt(biggest_source)} m²) — the total "
                        "outline should contain the source, so the Total area "
                        "layer may be pointing at a source outline.")
        return None

    # ---------- handing off to QGIS's own tools ----------
    def _trigger(self, action_name):
        """Fire one of QGIS's toolbar actions, if this build exposes it."""
        try:
            getter = getattr(self.iface, action_name, None)
            action = getter() if callable(getter) else None
            if action is not None:
                action.trigger()
                return True
        except Exception:
            pass
        return False

    def _outline_dir(self):
        """Folder the saved outlines are written to: the open project's own (see
        dock.results_dir), created if it isn't there yet. "" when there is
        nowhere to write — a project that has never been saved AND no Results
        folder set in Environment. This does NOT fall back to a temp dir: an outline dropped in /tmp is the scratch layer the
        button exists to avoid, so it says so instead."""
        out_dir = self.dock.results_dir()
        if not out_dir:
            return ""
        try:
            os.makedirs(out_dir, exist_ok=True)   # <tools>/out/interactive may not exist yet
        except OSError:
            return ""
        return out_dir if os.path.isdir(out_dir) else ""

    def _new_role_layer(self, role):
        """Create one role's outline on disk, assign it to that role and start
        digitizing — in one press.

        A GeoPackage beside the project rather than a memory layer, because
        these two ARE the measurement: a scratch layer loses the digitizing to
        any QGIS restart, and the project that reopens has nothing to measure.
        Digitizing into a GeoPackage writes to the file every time editing is
        toggled off, so the outline is kept by the same gesture that finishes
        it. Assigning the role here is the point of the button — the layer is
        also NAMED for its role, so the drop-down refills itself when the
        project is opened again months later."""
        label, suffix, stem, style = ROLE_LAYERS[role]
        out_dir = self._outline_dir()
        if not out_dir:
            self._notify(
                "Nowhere to save the outline. Save this QGIS project first — "
                "outlines are written beside its .qgz — or set “Results "
                "(unsaved projects)” in Environment.", level=Qgis.MessageLevel.Warning)
            return

        slide = self.name_edit.text().strip() or "slide"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", slide).strip("_") or "slide"
        name = f"{slide} {suffix}"
        path = os.path.join(out_dir, f"{safe}_{stem}.gpkg")
        # Never write over an outline that is already there — it may be the
        # last session's work, and digitizing is slow to redo. File and layer
        # name step up together so the folder still reads like the layer panel.
        n = 1
        while os.path.exists(path) or QgsProject.instance().mapLayersByName(name):
            n += 1
            path = os.path.join(out_dir, f"{safe}_{stem}_{n}.gpkg")
            name = f"{slide} {suffix} {n}"

        crs = self._target_crs()
        scratch = QgsVectorLayer(f"Polygon?crs={crs.authid()}", name, "memory")
        if not scratch.isValid():
            self._append_log(f"Could not create the {label} outline.")
            return
        scratch.dataProvider().addAttributes(self._outline_fields())
        scratch.updateFields()

        # One layer per GeoPackage, named after the file, so "which layer is in
        # there" is never a question a reader of the folder has to ask.
        table = os.path.splitext(os.path.basename(path))[0]
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.fileEncoding = "UTF-8"
        try:
            ctx = QgsProject.instance().transformContext()
            if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
                err, msg = QgsVectorFileWriter.writeAsVectorFormatV3(
                    scratch, path, ctx, opts)[:2]
            else:                                    # QGIS < 3.20
                err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                    scratch, path, ctx, opts)[:2]
        except Exception as e:
            err, msg = -1, str(e)
        if err != QgsVectorFileWriter.WriterError.NoError:
            self._notify(f"Could not write {path}: {msg}", level=Qgis.MessageLevel.Warning)
            return

        layer = QgsVectorLayer(f"{path}|layername={table}", name, "ogr")
        if not layer.isValid():
            self._notify(
                f"Wrote {path} but QGIS could not load it back — add it by "
                f"hand and assign it to {label}.", level=Qgis.MessageLevel.Warning)
            return
        self._prepare_outline(layer, style)
        # Also into the GeoPackage as its default style, so the file opens red
        # or blue, with its area kept current, in any project it is dropped into.
        try:
            save = (getattr(layer, "saveStyleToDatabaseV2", None)
                    or layer.saveStyleToDatabase)
            save(table, f"{label} outline", True, "")
        except Exception as e:                       # noqa: BLE001 — a nicety
            self._append_log(f"Could not store the style in {path}: {e}")
        # read before the layer is added: adding it runs the name match, which
        # may hand the role over on its own, and then nothing would be left to
        # say which layer this one displaced.
        previous = self._role_layer(role)
        lg.add_to_group(layer, "Volume")
        self._refresh_layers()

        combo = self._role_combo(role)
        role_idx = combo.findData(layer.id())
        if role_idx >= 0:
            combo.setCurrentIndex(role_idx)
            # the name match may have claimed the role already, in which case the
            # line above changes nothing and emits nothing — pin it either way,
            # so a layer loaded later can't take a role the button just set.
            self._pin_role(role)
        took = ""
        if previous is not None and previous.id() != layer.id():
            took = f" It takes the role over from “{previous.name()}”."
        self._notify(
            f"“{name}” → {label}, saved as {path}.{took}\n"
            "Click each vertex of the outline, right-click to finish, then "
            "toggle editing off (or press Save) to write it to the file.")
        self._arm_digitizing(layer)

    @staticmethod
    def _outline_fields():
        """The attribute fields every outline the tab creates starts with."""
        return [QgsField("slide", QMetaType.Type.QString),
                QgsField(AREA_FIELD, QMetaType.Type.Double)]

    def _prepare_outline(self, layer, style):
        """Colour a layer this tab created, keep its area field current, and
        mark it as ours so its colour may follow its role (_colour_by_role)."""
        self._style_outline(layer, style)
        idx = layer.fields().indexOf(AREA_FIELD)
        if idx >= 0:
            # apply-on-update: recomputed whenever the polygon is reshaped,
            # not only when it is first drawn
            layer.setDefaultValueDefinition(
                idx, QgsDefaultValue(AREA_EXPRESSION, True))
            form = layer.editFormConfig()
            form.setReadOnly(idx, True)
            layer.setEditFormConfig(form)
        layer.setCustomProperty(OUTLINE_PROP, True)

    @staticmethod
    def _style_outline(layer, style):
        symbol = QgsFillSymbol.createSimple(style)
        if symbol is None:
            return
        # a new renderer rather than setSymbol on the old one, so the layer
        # panel's swatch follows (setRenderer emits rendererChanged)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()

    def _colour_by_role(self, role, *_args):
        """A layer this tab created takes its role's colour — red for the total
        outline, blue for the best source — however it got the role. Layers the
        tab did not create are never restyled."""
        if role not in ROLE_LAYERS:
            return
        layer = self._role_layer(role)
        if layer is not None and layer.customProperty(OUTLINE_PROP):
            self._style_outline(layer, ROLE_LAYERS[role][3])

    def _arm_digitizing(self, layer):
        """Make `layer` the active one, put it in edit mode and arm QGIS's Add
        Polygon tool. True when the tool was armed; False when QGIS wouldn't
        give it up and the user has to pick it from the toolbar."""
        self.iface.setActiveLayer(layer)
        if not layer.isEditable():
            layer.startEditing()
        return self._trigger("actionAddFeature")

    def _draw_role(self, role):
        """Digitize into the layer assigned to `role`, with QGIS's own tools."""
        label = ROLE_LAYERS[role][0]
        layer = self._role_layer(role)
        if layer is None:
            self._notify(
                f"No layer is assigned to {label} — press New beside it to make "
                "one, or pick a layer in its drop-down.")
            return
        if self._arm_digitizing(layer):
            self._append_log(
                f"Digitizing on “{layer.name()}” ({label}): click each vertex of "
                "the outline, right-click to finish, then toggle editing off (or "
                "Save) and Measure.")
        else:
            self._append_log(
                f"“{layer.name()}” is in edit mode — use QGIS's Add Polygon "
                "Feature tool to draw the outline.")

    def _build_settings_box(self):
        """What Larsen et al. (2010) needs beyond the outlines: what failed.

        The deposit thickness for area × thickness sat here too until
        2026-11-04; it is parked, with the rest of that fit, in
        parked/thickness/."""
        box = QgsCollapsibleGroupBox("Settings")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        self.material_combo = QComboBox()
        for label, _key in volume_calc.MATERIALS:
            self.material_combo.addItem(label)
        self.material_combo.setToolTip(
            "Read by Larsen et al. (2010) only. Bedrock failures are deeper for a "
            "given area than soil failures. This is the single biggest control on "
            "that answer: at a typical scar size the two options differ by about "
            "a factor of 5 — roughly twenty times the ±range shown beside the "
            "volume. Pick it from what actually failed, not from the surrounding "
            "cover.")
        form.addRow("What failed?", self.material_combo)
        # The case this plugin exists for is the one Larsen does not cover.
        form.addRow(self._help(
            "For Larsen et al. (2010). Neither option covers a "
            "rock-and-ice avalanche: if much of what moved was ice or entrained "
            "snow, that relation is outside its calibration — and the seismic "
            "volume is a mass divided by an assumed density, so check both "
            "numbers assume the same thing before comparing them."))

        return box

    def _build_volumes_box(self):
        """What Measure found — Larsen et al. (2010) on each source outline,
        read-only and copyable. The best outline's volume is the answer and is
        bold; source low and high give the low and high estimates under it.

        Read-only output, which is also why the per-project state save skips it
        (see project_state._persistable). The warnings line under the numbers is
        not help text: it says what is wrong with THIS measurement (a skipped
        fit, an implausible implied depth), so it always shows."""
        box = QgsCollapsibleGroupBox("Volumes")
        box.setSaveCollapsedState(False)
        f = QFormLayout(box)
        self.vol_scar_out = self._ro("press Measure")
        bold = self.vol_scar_out.font(); bold.setBold(True)   # the answer
        self.vol_scar_out.setFont(bold)
        scar_tip = (
            "Larsen et al. (2010), Table S1, on the best source outline. "
            "The range in brackets is NOT a prediction interval for this "
            "landslide. It propagates the uncertainty in WHERE THE REGRESSION "
            "LINE SITS (the published spread on the coefficients) plus your own "
            "low/high outlines. It contains no term for how far individual "
            "landslides scatter about that line, which is the dominant "
            "uncertainty — roughly a factor of 2 to 3 in Larsen's own data, "
            "several times wider than the range shown here.\n\n"
            "Larsen's published spread is itself ambiguous: their main text "
            "calls it a standard deviation and the supplement calls it a "
            "standard error.\n\n"
            "Your low/high outlines are also read as a ±2σ span, so a narrower "
            "pair tightens this range faster than you may intend. Their own "
            "volumes are the two lines below. Exact m³ are in the results "
            "table, the CSV and the written attributes.")
        self.vol_scar_out.setToolTip(scar_tip)
        scar_lbl = QLabel("Larsen et al. (2010)")
        scar_lbl.setFont(bold)
        scar_lbl.setToolTip(scar_tip)
        f.addRow(scar_lbl, self.vol_scar_out)
        # One volume per reading of the scar: the same relation, material and
        # implementation as the best estimate, at its central value — the
        # coefficient spread is already shown once, on the line above.
        self.vol_src_low_out = self._ro()
        self.vol_src_high_out = self._ro()
        for out, word, role in ((self.vol_src_low_out, "LOW", "low"),
                                (self.vol_src_high_out, "HIGH", "high")):
            tip = (f"Larsen et al. (2010) on the source {role} outline — the "
                   f"{word} estimate: the volume this reading of the scar gives "
                   "through the same relation and material as the best one, at "
                   "the fit's central value.")
            out.setToolTip(tip)
            lbl = QLabel(f"Source {role}")
            lbl.setToolTip(tip)
            f.addRow(lbl, out)
        # V/A — the cheapest sanity check in the tab. Taken over the TOTAL outline
        # (Ethan's call, 2026-10-23), so it is the mean thickness the Larsen
        # volume implies across the whole footprint, comparable with measured
        # deposit thicknesses.
        self.depth_out = self._ro()
        self.depth_out.setToolTip(
            "Larsen et al. (2010) volume ÷ TOTAL outline area: the mean thickness "
            "over the whole landslide footprint that the Larsen volume implies. "
            "Rock-avalanche deposits on glaciers run ~2.5 m (middle half "
            "1.9–3.0 m; Deline et al. 2015, Table 9.1), and above "
            f"{volume_calc.LARSEN_DEPTH_FLAG_M:g} m the tab warns that the volume "
            "is probably too high on ice. Tens of metres means the outline or "
            "the material is wrong.")
        f.addRow("Implied mean depth", self.depth_out)
        self.areas_out = self._ro()
        self.areas_out.setToolTip(
            "Ellipsoidal plan-view areas of the assigned outlines — the quantity "
            "Larsen et al. measured from imagery.")
        f.addRow("Areas", self.areas_out)
        self.vol_warn_lbl = QLabel()
        self.vol_warn_lbl.setWordWrap(True)
        self.vol_warn_lbl.setStyleSheet(status_css("warn"))
        self.vol_warn_lbl.setVisible(False)
        f.addRow(self.vol_warn_lbl)
        return box

    def _show_depth(self, c):
        d, area = _implied_depth(c)
        if d is not None:
            self.depth_out.setText(
                f"{d:,.1f} m   (Larsen volume ÷ total {_km2(area)})")
        elif c.get("v_larsen") is not None:
            self.depth_out.setText("— (no total outline to spread it over)")
        else:
            self.depth_out.clear()

    def _build_centerline_box(self):
        """Centerline + elevation-drop tools, open by default — most slides get
        a length and a fall height, though neither feeds the volume.

        Two optional inputs live here. "Centerline from" chooses which outline
        the medial-axis spine is taken from; left on Auto it follows _measure's
        choice (the total outline, or the source one when no total is
        assigned). "Elevation (DEM)" names a single-band elevation raster to
        read the slide's fall height from — sampled along the centerline, so its
        highest reading is the source crown and its lowest the toe. A HILLSHADE
        is 0-255 shading, not elevation, so a byte raster is refused rather than
        reported as metres (see _is_hillshade)."""
        box = QgsCollapsibleGroupBox("Centerline && elevation drop")
        box.setSaveCollapsedState(False)
        v = QVBoxLayout(box)
        v.addWidget(self._help(
            "Derives the medial-axis spine of the chosen outline (the total "
            "landslide outline by default) — it follows the slide's bends "
            f"instead of cutting across them — into “{CENTERLINE_LAYER}”, left "
            "in edit mode so you can trim the ends with the Vertex Tool. "
            "Re-measure afterwards to pick up your edits. With an elevation "
            "raster assigned it also reads the crown-to-toe fall height along "
            "that spine. Reported for reference; the volumes come from area "
            "alone."))

        form = QFormLayout()
        # underscore-prefixed like the role combos: rebuilt from the project on
        # every refresh, so persisting them would only mark the project dirty.
        self._cl_source_combo = QComboBox()
        self._cl_source_combo.setToolTip(
            "Which outline the centerline is spined from. Auto follows the "
            "measurement: the TOTAL outline when one is assigned, otherwise the "
            "source outline. Pick a specific polygon layer to override.")
        self._cl_source_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._cl_source_combo.setMinimumContentsLength(8)
        form.addRow("Centerline from", self._cl_source_combo)

        self._dem_combo = QComboBox()
        self._dem_combo.setToolTip(
            "Optional. A single-band ELEVATION raster (DEM/DSM) to read the "
            "slide's fall height from — sampled along the centerline, crown to "
            "toe. Gives the drop H and the travel angle atan(H/L).\n\n"
            "A hillshade is shading (0-255), NOT elevation, and is refused. Load "
            "a DEM/DSM, or let the 3D viewer tab fetch ArcticDEM, then pick it "
            "here.")
        self._dem_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._dem_combo.setMinimumContentsLength(8)
        self._dem_combo.currentIndexChanged.connect(self._on_dem_changed)
        form.addRow("Elevation (DEM)", self._dem_combo)
        v.addLayout(form)

        row = FlowRow()
        self.centerline_btn = QPushButton("Draw centerline")
        self.centerline_btn.setEnabled(False)
        self.centerline_btn.setToolTip(
            "Derive the runout centerline for the measured slide, spined from the "
            "outline above. Enabled after a successful Measure.")
        self.centerline_btn.clicked.connect(self._draw_centerline)
        self.remeasure_btn = QPushButton("Re-measure (after editing)")
        self.remeasure_btn.setEnabled(False)
        self.remeasure_btn.setToolTip(
            "Recompute the centerline length and elevation drop after you edit the "
            "drawn line's vertices. Enabled once a centerline has been drawn.")
        self.remeasure_btn.clicked.connect(self._remeasure_centerline)
        self.sample_btn = QPushButton("Sample elevation")
        self.sample_btn.setEnabled(False)
        self.sample_btn.setToolTip(
            "Read the fall height from the assigned DEM along the centerline "
            "drawn above. Runs automatically when a DEM is set; use this after "
            "picking a DEM, or after editing the line.")
        self.sample_btn.clicked.connect(self._sample_elevation_current)
        for b in (self.centerline_btn, self.remeasure_btn, self.sample_btn):
            row.addWidget(b)
        v.addWidget(row)

        out = QFormLayout()
        self.length_out = self._ro()
        out.addRow("Length", self.length_out)
        self.drop_out = self._ro()
        out.addRow("Elevation drop", self.drop_out)
        v.addLayout(out)
        return box

    def _build_results_box(self):
        """Every measured slide, one row per slide name. Collapsed: with one
        slide per project the Volumes box already shows the row."""
        box = QgsCollapsibleGroupBox("Results table")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        v = QVBoxLayout(box)
        self.table = QTableWidget(0, len(TABLE_COLS))
        self.table.setHorizontalHeaderLabels(TABLE_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(110)
        v.addWidget(self.table)

        rbtns = FlowRow()
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.setToolTip(
            "Write every row to CSV with raw unrounded values, plus the "
            "detection and the scar's offset from its epicentre.")
        self.csv_btn.clicked.connect(self._export_csv)
        self.remove_btn = QPushButton("Remove row")
        self.remove_btn.setToolTip("Drop the selected row(s) from the table.")
        self.remove_btn.clicked.connect(self._remove_rows)
        for b in (self.csv_btn, self.remove_btn):
            rbtns.addWidget(b)
        v.addWidget(rbtns)
        return box

    def _build_log_box(self):
        box = QgsCollapsibleGroupBox("Log")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        v = QVBoxLayout(box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setMinimumHeight(140)
        v.addWidget(self.log)
        return box

    def _ro(self, text=""):
        """A read-only readout: a label rather than a line edit, so the whole
        value wraps into view on a narrow dock (a line edit scrolls to its end
        and hides the number) while staying selectable for copying."""
        e = QLabel(text)
        e.setWordWrap(True)
        e.setTextFormat(Qt.TextFormat.PlainText)
        e.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return e

    def _append_log(self, msg):
        self.log.appendPlainText(msg)

    def _notify(self, msg, level=None):
        """Log to the tab AND flash the QGIS message bar, so an action button
        never looks like it did nothing when the Log panel is scrolled out of
        view. The first line goes to the bar; the full message to the log."""
        self._append_log(msg)
        try:
            self.iface.messageBar().pushMessage(
                "Volume", msg.split("\n")[0].strip(),
                level=Qgis.MessageLevel.Info if level is None else level, duration=7)
        except Exception:
            pass

    def _material(self):
        return dict(volume_calc.MATERIALS).get(
            self.material_combo.currentText(), "bedrock")

    # ---------- measuring ----------
    def _role_area(self, role, label):
        """(area, feature, layer) for one role, or (None, None, None).

        Reports the layer's other polygons rather than hiding them: a role layer
        contributes its LARGEST outline, and a stray extra polygon would
        otherwise change the answer invisibly."""
        layer = self._role_layer(role)
        if layer is None:
            return None, None, None
        area, feature, measured = self._layer_total(layer)
        if area is None:
            self._append_log(
                f"{label}: “{layer.name()}” has no measurable polygon — it is "
                "empty, or its CRS could not be resolved (see above).")
            return None, None, layer
        if len(measured) > 1:
            others = ", ".join(_fmt(a) + " m²" for a, _f in measured[1:])
            self._append_log(
                f"{label}: “{layer.name()}” holds {len(measured)} polygons — "
                f"using the largest ({_fmt(area)} m²). Not used: {others}.")
        return area, feature, layer

    def _measure(self):
        """The Larsen et al. (2010) volumes, from the source outlines its
        calibration is defined on.

        The BEST source outline gives the estimate, its range widened by source
        low/high when both are assigned; source low and source high each go
        through the same relation for the low and high estimates. The TOTAL
        outline is not converted — it gives the implied mean depth, and it is
        what the centerline is spined from and Write to layer stamps. A slide
        with only a total outline is still recorded, with its areas and no
        volume, so its centerline and drop can be taken. The result is this
        slide's row straight away (_record: re-measuring the same slide name
        replaces it)."""
        if not self._polygon_layers():
            self._notify(
                "This project has no polygon layer yet — press New beside Total "
                "area to make one and start drawing.")
            return
        a_total, total_feat, total_layer = self._role_area("total", "Total area")
        a_best, best_feat, best_layer = self._role_area("best", "Source best")
        a_low, _lf, low_layer = self._role_area("low", "Source low")
        a_high, _hf, high_layer = self._role_area("high", "Source high")

        problem = self._role_order_problem(
            {"total": a_total, "best": a_best, "low": a_low, "high": a_high})
        if problem:
            self._notify(
                f"{problem} Reassign the drop-downs — measuring was skipped "
                "rather than reporting a number from the wrong outline.",
                level=Qgis.MessageLevel.Warning)
            return
        if total_feat is None and best_feat is None:
            self._notify(
                "No outline to measure — draw (or assign) the Total area and "
                "Source (best) outlines first.", level=Qgis.MessageLevel.Warning)
            return

        warnings = []
        scar = self._scar_volume(a_best, a_low, a_high, warnings)
        if total_feat is not None and best_feat is not None:
            note = self._warn_if_total_excludes_source(
                total_feat, total_layer, best_layer)
            if note:
                warnings.append(note)

        # Written back to — and the centerline spined from — the TOTAL outline:
        # it is the whole slide. The source scar only stands in when no total is
        # drawn.
        if total_feat is not None:
            out_feat, out_layer, out_role = total_feat, total_layer, "total"
        else:
            out_feat, out_layer, out_role = best_feat, best_layer, "source"
        row = {
            "name": self.name_edit.text().strip() or "slide",
            "material": self._material(),
            "material_label": self.material_combo.currentText(),
            "a_total": a_total,
            "src_best": a_best, "src_low": a_low, "src_high": a_high,
            # None where Larsen et al. could not run (no source best outline)
            "v_larsen": None, "v_larsen_lo": None, "v_larsen_hi": None,
            "v_larsen_srclo": None, "v_larsen_srchi": None,
            "larsen_area_m2": None, "larsen_calc": "",
            "length": None, "length_method": "",
            "z_top": None, "z_bottom": None, "drop": None,
            "reach_angle": None, "hl_ratio": None, "dem_name": "", "cl_fid": None,
            "layer_id": out_layer.id(), "layer_name": out_layer.name(),
            "fid": out_feat.id(), "out_role": out_role,
            "len_layer_id": out_layer.id(), "len_fid": out_feat.id(),
            "len_from": out_role,
            "total_layer": total_layer.name() if a_total is not None else "",
            "best_layer": best_layer.name() if a_best is not None else "",
            "low_layer": low_layer.name() if a_low is not None else "",
            "high_layer": high_layer.name() if a_high is not None else "",
        }
        row.update(scar)
        depth_note = volume_calc.implied_depth_note(_implied_depth(row)[0])
        if depth_note:
            warnings.append(depth_note)
        row["warnings"] = warnings          # on screen only; not a CSV column
        kept = self._record(row)

        if scar:
            self._append_log(
                f"Larsen et al. (2010): {_fmt(a_best)} m² from “{best_layer.name()}” → "
                f"{_fmt(row['v_larsen'])} m³ ({_fmt(row['v_larsen_lo'])} – "
                f"{_fmt(row['v_larsen_hi'])}), {row['material_label'].lower()}; "
                f"via {row['larsen_calc']}.")
            for word, area, layer, key in (
                    ("low", a_low, low_layer, "v_larsen_srclo"),
                    ("high", a_high, high_layer, "v_larsen_srchi")):
                if row.get(key) is not None:
                    self._append_log(
                        f"  Source {word}: {_fmt(area)} m² from “{layer.name()}” → "
                        f"{_fmt(row[key])} m³ — the {word} estimate.")
            if a_low is not None and a_high is not None:
                self._append_log(
                    "  Its range combines the published fit uncertainty with the "
                    "source low/high area spread.")
            else:
                missing = ("both source low and high outlines"
                           if a_low is None and a_high is None
                           else "the other source outline")
                self._append_log(
                    "  Its range is the published fit uncertainty — assign "
                    f"{missing} to add area uncertainty to it.")
        for w in warnings:
            self._append_log(f"Note: {w}")
        if kept:
            self._append_log(
                f"Replaced “{row['name']}” in the results — its centerline was "
                "kept; Re-measure it if you reshaped the outline.")

    def _scar_volume(self, a_best, a_low, a_high, warnings):
        """{v_larsen…} from Larsen et al. (2010), or {} with the reason.

        v_larsen with its range is the best outline (low/high folded in as an
        area span when both are there); v_larsen_srclo / _srchi are the low and
        high outlines run through the same relation on their own, at the fit's
        central value. Those two are only taken beside a best estimate: on
        their own they would be a range with nothing in it."""
        if a_best is None:
            warnings.append(
                "Larsen et al. (2010) skipped: no Source (best) outline — draw the "
                "evacuated scar to get the volume.")
            return {}
        material, project_dir = self._material(), self.dock.pipeline_dir()
        try:
            v, v_lo, v_hi, calc = volume_calc.volume_source(
                a_best, A_low=a_low, A_high=a_high, material=material,
                fit="scar", project_dir=project_dir)
        except Exception as e:
            warnings.append(f"Larsen et al. (2010) calculation failed: {e}")
            return {}
        out = {"v_larsen": v, "v_larsen_lo": v_lo, "v_larsen_hi": v_hi,
               "larsen_area_m2": a_best, "larsen_calc": calc,
               "v_larsen_srclo": None, "v_larsen_srchi": None}
        for key, area, word in (("v_larsen_srclo", a_low, "low"),
                                ("v_larsen_srchi", a_high, "high")):
            if area is None:
                continue
            try:
                out[key] = volume_calc.volume_source(
                    area, material=material, fit="scar",
                    project_dir=project_dir)[0]
            except Exception as e:
                warnings.append(
                    f"Larsen et al. (2010) on the source {word} outline failed: {e}")
        return out

    def _show_warnings(self, warnings):
        self.vol_warn_lbl.setText("\n".join(f"⚠ {w}" for w in warnings))
        self.vol_warn_lbl.setVisible(bool(warnings))

    def _warn_if_total_excludes_source(self, total_feat, total_layer, best_layer):
        """The total outline should contain the source scar. If it doesn't, the
        two are probably from different slides, or the Total area layer is
        pointing at the wrong outline. Flagged, not blocked: a source mapped from
        different imagery can legitimately poke outside the total. Returns the
        note to show, or None."""
        try:
            if best_layer is None:
                return None
            measured = self._layer_areas(best_layer)
            if not measured:
                return None
            total = total_feat.geometry()
            src_geom = QgsGeometry(measured[0][1].geometry())   # copy before transform
            # The two layers are matched by NAME and can carry DIFFERENT CRSs (e.g.
            # total in EPSG:4326 degrees, source in UTM metres); contains() across
            # mismatched CRSs is meaningless, so reproject the source into the total
            # layer's CRS first.
            if (total_layer is not None and best_layer is not None
                    and total_layer.crs() != best_layer.crs()):
                xform = QgsCoordinateTransform(best_layer.crs(), total_layer.crs(),
                                               QgsProject.instance())
                src_geom.transform(xform)
            if not total.contains(src_geom):
                return (f"The source outline in “{best_layer.name()}” is not "
                        "fully inside the total outline. Check both belong to "
                        "the same slide.")
        except Exception:
            pass
        return None

    def _show_current(self):
        """Put the slide's numbers on screen, and keep its row in step — the
        row IS self._current, so a centerline drawn after Measure
        lands in the table without another click.

        3 significant figures of Mm³, not 4 decimal places: the honest spread on
        a Larsen et al. estimate is a factor of 2-3, so printing 1.3021 Mm³
        asserts a precision the method does not have. Exact m³ are in the table
        and the CSV."""
        c = self._current
        if c is None:
            self.vol_scar_out.setText("press Measure")
            for e in (self.vol_src_low_out, self.vol_src_high_out,
                      self.depth_out, self.areas_out,
                      self.length_out, self.drop_out):
                e.clear()
            self._show_warnings([])
        else:
            if c.get("v_larsen") is not None:
                self.vol_scar_out.setText(
                    f"{_vol_range(c['v_larsen'], c['v_larsen_lo'], c['v_larsen_hi'])}"
                    f"   ·  {_km2(c['larsen_area_m2'])}, "
                    f"{(c.get('material_label') or '').lower()}")
            else:
                self.vol_scar_out.setText("— not run (see the note below)")
            for out, key, area_key, word in (
                    (self.vol_src_low_out, "v_larsen_srclo", "src_low", "low"),
                    (self.vol_src_high_out, "v_larsen_srchi", "src_high", "high")):
                if c.get(key) is not None:
                    out.setText(f"{c[key] / 1e6:,.3g} Mm³   ·  {_km2(c[area_key])}")
                elif c.get("v_larsen") is None:
                    out.clear()
                else:
                    out.setText(f"— (no source {word} outline)")
            self._show_depth(c)
            areas = []
            if c.get("a_total") is not None:
                areas.append(f"total {_km2(c['a_total'])}")
            if c.get("src_best") is not None:
                src = f"source {_km2(c['src_best'])}"
                rng = [f"{k} {_km2(c[key])}" for k, key in
                       (("low", "src_low"), ("high", "src_high"))
                       if c.get(key) is not None]
                areas.append(src + (f" ({', '.join(rng)})" if rng else ""))
            self.areas_out.setText("  ·  ".join(areas))
            self._show_warnings(c.get("warnings") or [])

            if c.get("length") is None:
                self.length_out.setText("— (draw the centerline)")
            else:
                self.length_out.setText(
                    f"{_fmt(c['length'])} m   ({c['length_method']})")
            if c.get("drop") is None:
                self.drop_out.setText("— (no elevation raster sampled)")
            else:
                txt = (f"crown {_fmt(c['z_top'])} m → toe {_fmt(c['z_bottom'])} m  "
                       f"=  {_fmt(c['drop'])} m fall")
                if c.get("reach_angle") is not None:
                    txt += (f"   (H/L {c['hl_ratio']:.3f}, "
                            f"{c['reach_angle']:.1f}° travel angle)")
                self.drop_out.setText(txt)
        self._refresh_table()

    def _distance_area(self, crs):
        """Ellipsoidal measurement in the given CRS.

        The project's ellipsoid is used when it has one; a project set to
        planar measurement ("NONE") would otherwise return areas in the layer's
        own units — degrees² for a geographic layer, which is meaningless as an
        input to the volume relation. WGS84 stands in for that case."""
        da = QgsDistanceArea()
        project = QgsProject.instance()
        da.setSourceCrs(crs, project.transformContext())
        ellipsoid = project.ellipsoid()
        if not ellipsoid or ellipsoid.upper() in ("NONE", ""):
            ellipsoid = "WGS84"
        da.setEllipsoid(ellipsoid)
        return da

    def _measure_area(self, geom, crs):
        """Plan-view area in m², or None if it genuinely cannot be measured.

        Ellipsoidal when the project can measure that way, which is the normal
        case and the quantity Larsen et al. fitted. The guard is on the UNIT of
        the answer rather than on the CRS's flags, because that is what actually
        determines whether the number means anything: a project set to planar
        measurement reports square DEGREES for a lat/lon layer, and a CRS the
        install can't resolve reports "unknown". Either would sail straight into
        the volume relation and come out as a confident answer that is wrong by
        orders of magnitude, so both are rejected and the geometry is measured
        in its local UTM zone instead — planar there, but agreeing with the
        ellipsoidal figure to far better than a tenth of a percent at landslide
        scale. If even that fails, None: no number beats a wrong one."""
        da = self._distance_area(crs)
        if da.areaUnits() not in UNUSABLE_AREA_UNITS:
            return da.convertAreaMeasurement(
                da.measureArea(geom), Qgis.AreaUnit.SquareMeters)
        self._note_planar_fallback()
        g, _work = self._to_utm(geom, crs)
        return None if g is None else g.area()

    def _measure_length(self, line, crs):
        """Length in m, or None — same reasoning as _measure_area."""
        da = self._distance_area(crs)
        if da.lengthUnits() not in UNUSABLE_LENGTH_UNITS:
            return da.convertLengthMeasurement(
                da.measureLength(line), Qgis.DistanceUnit.Meters)
        self._note_planar_fallback()
        g, _work = self._to_utm(line, crs)
        return None if g is None else g.length()

    def _note_planar_fallback(self):
        """Say once per session which way the measurements were taken — the
        numbers are equivalent at this scale, but nobody should have to guess."""
        if self._planar_noted:
            return
        self._planar_noted = True
        self._append_log(
            "This project can't measure ellipsoidally on this layer's CRS "
            "(planar measurement, or a CRS this install can't resolve), so "
            "areas and lengths are computed in the local UTM zone instead — "
            "equivalent to within ~0.1 % at landslide scale.")

    # ---------- centerline ----------
    def _draw_centerline(self):
        c = self._current
        if c is None:
            return
        layer, geom = self._centerline_source(c)
        if layer is None:
            return

        # the skeleton is metric: work in the local UTM zone, then hand the
        # result back in the project's CRS so it edits naturally on the canvas
        work, work_crs = self._to_utm(geom, layer.crs())
        if work is None:
            return

        line, method = centerline_mod.centerline(work)
        if line is None or line.isEmpty():
            self._append_log(f"Centerline failed: {method}")
            return

        target_crs = self._target_crs()
        if work_crs != target_crs:
            back = QgsCoordinateTransform(
                work_crs, target_crs, QgsProject.instance())
            if line.transform(back) != 0:
                self._append_log("Could not project the centerline back.")
                return

        cl_layer = self._centerline_layer(target_crs)
        if cl_layer is None:
            return
        length = self._measure_length(line, target_crs)

        was_editing = cl_layer.isEditable()
        if not was_editing and not cl_layer.startEditing():
            self._append_log(f"Could not open “{CENTERLINE_LAYER}” for editing.")
            return
        f = QgsFeature(cl_layer.fields())
        f.setGeometry(line)
        f.setAttribute("slide", c["name"])
        f.setAttribute("length_m", float(length))
        f.setAttribute("method", method)
        if not cl_layer.addFeature(f):
            self._append_log("Could not add the centerline feature.")
            return
        self._cl_fid = f.id()
        c["cl_fid"] = self._cl_fid          # survives a re-measure (_record)

        c["length"] = length
        c["length_method"] = method
        self._show_current()
        self.remeasure_btn.setEnabled(True)
        self.sample_btn.setEnabled(True)

        self.iface.setActiveLayer(cl_layer)
        cl_layer.removeSelection()
        cl_layer.select(self._cl_fid)
        cl_layer.triggerRepaint()
        self._append_log(
            f"Centerline ({method}) = {_fmt(length)} m, added to "
            f"“{CENTERLINE_LAYER}” as feature {self._cl_fid}. The layer is in "
            "edit mode — trim it with the Vertex Tool, then Re-measure.")
        if method == "straight major axis":
            self._append_log(
                "The medial-axis skeleton degenerated (very narrow or very "
                "simple outline), so this is the longest straight chord — it "
                "cuts corners on a curving slide.")
        # read the fall height straight away when a DEM is assigned, so the drop
        # appears with the length rather than needing a second click
        if self._dem_layer() is not None:
            self._sample_elevation_current(auto=True)

    def _centerline_source(self, c):
        """(layer, geometry) of the outline to spine, or (None, None) with a log.

        An explicit "Centerline from" choice wins; on Auto it is the outline
        _measure recorded — the TOTAL when one was assigned, otherwise the
        source one."""
        chosen = self._layer_from(self._cl_source_combo)
        if chosen is not None:
            _area, feat, _measured = self._layer_total(chosen)
            if feat is None:
                self._append_log(
                    f"“{chosen.name()}” has no measurable polygon to spine — "
                    "pick another layer under “Centerline from”.")
                return None, None
            return chosen, feat.geometry()

        layer = QgsProject.instance().mapLayer(c["len_layer_id"])
        if layer is None:
            self._append_log("The outline to spine is no longer in the project.")
            return None, None
        feat = layer.getFeature(c["len_fid"])
        geom = feat.geometry() if feat is not None else None
        if geom is None or geom.isEmpty():
            self._append_log("Could not re-read the outline's geometry.")
            return None, None
        if c.get("len_from") != "total":
            self._append_log(
                "No total outline assigned — spining the "
                f"{c.get('len_from', 'source')} outline instead. Assign the "
                "total landslide outline (or pick one under “Centerline from”) "
                "for a whole-slide runout length.")
        return layer, geom

    def _remeasure_centerline(self):
        c = self._current
        cl_layer = self._existing_centerline_layer()
        if c is None or cl_layer is None or self._cl_fid is None:
            self._append_log("Nothing to re-measure — draw a centerline first.")
            return
        feat = cl_layer.getFeature(self._cl_fid)
        geom = feat.geometry() if feat is not None else None
        if geom is None or geom.isEmpty():
            self._append_log(
                "The centerline feature is gone (deleted, or edits rolled "
                "back). Draw it again.")
            return
        length = self._measure_length(geom, cl_layer.crs())
        c["length"] = length
        c["length_method"] = (c["length_method"] or "medial axis") + ", edited"
        if cl_layer.isEditable():
            idx = cl_layer.fields().indexOf("length_m")
            if idx >= 0:
                cl_layer.changeAttributeValue(
                    self._cl_fid, idx, float(length))
        self._show_current()
        self._append_log(f"Centerline re-measured: {_fmt(length)} m.")
        # the edit moved the ends and changed L, so the drop and travel angle
        # go stale — re-read them when a DEM is assigned
        if self._dem_layer() is not None:
            self._sample_elevation_current(auto=True)

    # ---------- elevation drop ----------
    def _dem_layer(self):
        """The raster assigned to "Elevation (DEM)", or None."""
        if not hasattr(self, "_dem_combo"):
            return None
        lyr = QgsProject.instance().mapLayer(self._dem_combo.currentData() or "")
        return lyr if isinstance(lyr, QgsRasterLayer) else None

    def _on_dem_changed(self, *_args):
        """Sample as soon as a DEM is picked, if a centerline already exists —
        so choosing the raster after drawing shows the drop without another
        click. A genuine user change only; the repopulate blocks signals."""
        if (self._current is not None and self._cl_fid is not None
                and self._dem_layer() is not None):
            self._sample_elevation_current(auto=True)

    def _is_hillshade(self, dem):
        """True when a raster is shading, not elevation.

        A hillshade / shaded-relief layer is a Byte raster of 0-255 illumination
        values; sampling it and calling the result a fall height would be a
        confident wrong answer in the wrong units. The data type is the reliable
        tell (an elevation DEM is Int16/Float32); the name is a backstop."""
        try:
            if dem.dataProvider().dataType(1) == Qgis.DataType.Byte:
                return True
        except Exception:
            pass
        return "hillshade" in _name_tokens(dem.name())

    def _sample_elevation_current(self, auto=False):
        """Read the slide's fall height from the assigned DEM along the current
        centerline: crown (highest), toe (lowest), drop H and travel angle.

        Auto=True is the call made straight after drawing/editing/picking, so it
        stays quiet when there is simply nothing to do; a button press (auto=
        False) explains what is missing instead."""
        c = self._current
        if c is None:
            if not auto:
                self._append_log("Measure a slide first.")
            return
        dem = self._dem_layer()
        if dem is None:
            if not auto:
                self._append_log(
                    "Assign a single-band elevation raster to “Elevation "
                    "(DEM)” first.")
            return
        cl_layer = self._existing_centerline_layer()
        if cl_layer is None or self._cl_fid is None:
            if not auto:
                self._append_log(
                    "Draw a centerline first — the fall height is read along "
                    "it.")
            return
        feat = cl_layer.getFeature(self._cl_fid)
        geom = feat.geometry() if feat is not None else None
        if geom is None or geom.isEmpty():
            self._append_log("The centerline feature is gone — draw it again.")
            return
        if self._is_hillshade(dem):
            self._append_log(
                f"“{dem.name()}” looks like a hillshade (a byte, 0-255 shading "
                "raster), not an elevation model — its values are not metres, "
                "so no fall height was read. Load a DEM/DSM, or let the 3D "
                "viewer tab fetch ArcticDEM, then pick that under “Elevation "
                "(DEM)”.")
            return
        if not hasattr(dem.dataProvider(), "sample"):
            self._append_log(
                "This QGIS build can't sample raster values (needs 3.4+).")
            return

        result = self._elevation_drop(geom, cl_layer.crs(), dem)
        if result is None:
            self._append_log(
                f"Could not read elevations from “{dem.name()}” under the "
                "centerline — the DEM may not cover this slide, or every sample "
                "fell on nodata.")
            return
        z_top, z_bottom, n = result
        drop = z_top - z_bottom
        length = c.get("length")
        hl = reach = None
        if length and length > 0:
            hl = drop / length
            reach = math.degrees(math.atan2(drop, length))
        c.update(z_top=z_top, z_bottom=z_bottom, drop=drop,
                 hl_ratio=hl, reach_angle=reach, dem_name=dem.name())
        self._show_current()
        self._write_centerline_relief(cl_layer, z_top, z_bottom, drop, reach)

        msg = (f"Elevation from “{dem.name()}”: crown {_fmt(z_top)} m → toe "
               f"{_fmt(z_bottom)} m = {_fmt(drop)} m fall ({n} samples).")
        if reach is not None:
            msg += (f" Over {_fmt(length)} m of centerline that is H/L "
                    f"{hl:.3f} — a {reach:.1f}° travel angle.")
        self._append_log(msg)

    def _elevation_drop(self, line_geom, line_crs, dem):
        """(z_top, z_bottom, n_samples) sampled along a line from a DEM, or None.

        The line is reprojected to local UTM and densified so the crown and toe
        aren't skipped between sparse medial-axis vertices; every densified
        vertex is read from the DEM (transformed into the DEM's own CRS), and
        the highest and lowest finite readings are the crown and toe."""
        work, work_crs = self._to_utm(line_geom, line_crs)
        if work is None:
            return None
        length = work.length()
        step = max(length / 300.0, 1.0) if length > 0 else 1.0
        dense = work.densifyByDistance(step) or work
        provider = dem.dataProvider()
        dem_crs = dem.crs()
        xform = None
        if work_crs != dem_crs:
            xform = QgsCoordinateTransform(
                work_crs, dem_crs, QgsProject.instance())
        z_top = z_bottom = None
        n = 0
        for v in dense.vertices():
            p = QgsPointXY(v.x(), v.y())
            if xform is not None:
                try:
                    p = xform.transform(p)
                except Exception:
                    continue
            try:
                val, ok = provider.sample(p, 1)
            except Exception:
                return None
            if not ok or val is None or math.isnan(val):
                continue
            n += 1
            if z_top is None or val > z_top:
                z_top = val
            if z_bottom is None or val < z_bottom:
                z_bottom = val
        if n == 0 or z_top is None:
            return None
        return z_top, z_bottom, n

    def _write_centerline_relief(self, cl_layer, z_top, z_bottom, drop, reach):
        """Stamp the fall height onto the centerline feature, so the derived
        layer carries the relief alongside its length. Silently skips fields a
        pre-existing centerline layer (older schema) doesn't have."""
        started = not cl_layer.isEditable()
        if started and not cl_layer.startEditing():
            return
        for name, value in (("z_top_m", z_top), ("z_bot_m", z_bottom),
                            ("drop_m", drop), ("reach_deg", reach)):
            idx = cl_layer.fields().indexOf(name)
            if idx >= 0 and value is not None:
                cl_layer.changeAttributeValue(self._cl_fid, idx, float(value))
        if started:
            cl_layer.commitChanges()

    def _to_utm(self, geom, layer_crs):
        """(geometry in local UTM metres, that UTM CRS) — or (None, None).

        The zone comes from the geometry's own centroid, so the projection is
        always the one with least distortion over the slide itself.

        Both CRSs are checked for validity up front, because a transform
        between two CRSs the install can't resolve does NOT fail — it quietly
        succeeds as a no-op, handing back the original degrees as if they were
        metres. Refusing is the only safe answer there."""
        try:
            if not layer_crs.isValid():
                self._append_log(
                    "The layer's CRS could not be resolved, so its geometry "
                    "cannot be measured in metres. Set a valid CRS on the "
                    "layer and measure again.")
                return None, None
            centroid = geom.centroid().asPoint()
            wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            if layer_crs != wgs:
                centroid = QgsCoordinateTransform(
                    layer_crs, wgs, QgsProject.instance()).transform(centroid)
            work = QgsCoordinateReferenceSystem(
                f"EPSG:{_utm_epsg(centroid.y(), centroid.x())}")
            if not work.isValid():
                self._append_log(
                    f"This QGIS could not build UTM zone "
                    f"EPSG:{_utm_epsg(centroid.y(), centroid.x())} — its PROJ "
                    "database looks incomplete, so no metric measurement is "
                    "possible.")
                return None, None
            out = QgsGeometry(geom)
            if layer_crs != work:
                transform = QgsCoordinateTransform(
                    layer_crs, work, QgsProject.instance())
                if out.transform(transform) != 0:
                    self._append_log(
                        f"Could not reproject the geometry from "
                        f"{layer_crs.authid() or 'an unknown CRS'} to "
                        f"{work.authid()} — check the layer's CRS.")
                    return None, None
            return out, work
        except Exception as e:
            self._append_log(f"Could not project the geometry to UTM: {e}")
            return None, None

    def _target_crs(self):
        """CRS the centerline layer lives in: the project's, unless it has no
        authid (a custom CRS a memory-layer URI can't name), in which case
        WGS84 stands in — lengths are ellipsoidal either way."""
        crs = QgsProject.instance().crs()
        if crs.isValid() and crs.authid():
            return crs
        return QgsCoordinateReferenceSystem("EPSG:4326")

    def _existing_centerline_layer(self):
        project = QgsProject.instance()
        layer = project.mapLayer(self._cl_layer_id) if self._cl_layer_id else None
        if layer is not None:
            return layer
        for lyr in project.mapLayersByName(CENTERLINE_LAYER):
            if isinstance(lyr, QgsVectorLayer):
                self._cl_layer_id = lyr.id()
                return lyr
        return None

    def _centerline_layer(self, crs):
        """The scratch centerline layer, created on first use."""
        layer = self._existing_centerline_layer()
        if layer is not None:
            return layer
        layer = QgsVectorLayer(
            f"LineString?crs={crs.authid()}", CENTERLINE_LAYER, "memory")
        if not layer.isValid():
            self._append_log("Could not create the centerline layer.")
            return None
        layer.dataProvider().addAttributes([
            QgsField("slide", QMetaType.Type.QString),
            QgsField("length_m", QMetaType.Type.Double),
            QgsField("method", QMetaType.Type.QString),
            QgsField("z_top_m", QMetaType.Type.Double),
            QgsField("z_bot_m", QMetaType.Type.Double),
            QgsField("drop_m", QMetaType.Type.Double),
            QgsField("reach_deg", QMetaType.Type.Double),
        ])
        layer.updateFields()
        symbol = QgsLineSymbol.createSimple({"color": "255,32,32", "width": "0.7"})
        if symbol is not None and layer.renderer() is not None:
            layer.renderer().setSymbol(symbol)
        lg.add_to_group(layer, "Volume")
        self._cl_layer_id = layer.id()
        self._append_log(f"Created scratch layer “{CENTERLINE_LAYER}”.")
        return layer

    # ---------- results table ----------
    def _record(self, row):
        """Make `row` this slide's result: replace the row of the same slide
        name, else append — one row per slide however often it is re-measured,
        with no separate "add" step to forget before Write to layer can see it.

        A centerline already drawn for the slide survives a re-measure (changing
        the material, say): its length and drop carry over to the new row, and
        True is returned so _measure can say so. Re-measure it if the outline
        itself moved."""
        kept = False
        idx = next((i for i, r in enumerate(self._rows)
                    if r.get("name") == row["name"]), None)
        if idx is None:
            self._rows.append(row)
        else:
            old = self._rows[idx]
            if old.get("cl_fid") is not None:
                for k in ("length", "length_method", "z_top", "z_bottom", "drop",
                          "reach_angle", "hl_ratio", "dem_name", "cl_fid"):
                    row[k] = old.get(k)
                kept = True
            self._rows[idx] = row
        self._current = row
        self._cl_fid = row.get("cl_fid")
        self.centerline_btn.setEnabled(True)
        self.remeasure_btn.setEnabled(self._cl_fid is not None)
        self.sample_btn.setEnabled(self._cl_fid is not None)
        self._show_current()
        return kept

    def _refresh_table(self):
        self.table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            others = [n for n in (row.get("total_layer", ""), row.get("best_layer"),
                                  row.get("low_layer"), row.get("high_layer"))
                      if n and n != row["layer_name"]]
            layers = row["layer_name"] + (f"  + {', '.join(others)}" if others else "")
            values = [
                row["name"], row.get("material_label", ""),
                _fmt(row.get("v_larsen")), _fmt(row.get("v_larsen_lo")),
                _fmt(row.get("v_larsen_hi")),
                _fmt(row.get("v_larsen_srclo")), _fmt(row.get("v_larsen_srchi")),
                _fmt(row.get("a_total")),
                _fmt(row.get("src_best")), _fmt(row.get("src_low")),
                _fmt(row.get("src_high")),
                _fmt(row.get("length")), _fmt(row.get("drop")),
                layers,
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c in TABLE_NUMERIC:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)

    def _selected_rows(self):
        """Indices of the selected rows, or every row when none are selected —
        so the buttons do the obvious thing on a table you haven't clicked."""
        idx = sorted({i.row() for i in self.table.selectedIndexes()})
        return idx or list(range(len(self._rows)))

    def _remove_rows(self):
        idx = sorted({i.row() for i in self.table.selectedIndexes()})
        if not idx:
            self._append_log("Select the row(s) to remove first.")
            return
        for r in reversed(idx):
            if 0 <= r < len(self._rows):
                if self._rows[r] is self._current:
                    # the slide on screen went with it: clear the readouts
                    # rather than keep editing a row the table no longer holds
                    self._current = None
                    self._cl_fid = None
                    for b in (self.centerline_btn, self.remeasure_btn,
                              self.sample_btn):
                        b.setEnabled(False)
                del self._rows[r]
        self._show_current()

    def _copy_table(self):
        if not self._rows:
            self._append_log("Nothing to copy — the results table is empty.")
            return
        lines = ["\t".join(TABLE_COLS)]
        for r in range(self.table.rowCount()):
            cells = []
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))
        self._append_log(f"Copied {len(self._rows)} row(s) to the clipboard.")

    # ---------- event columns for the CSV ----------
    def _estimates(self):
        """(larsen, row) — the slide's Larsen et al. volume and its row.

        From the slide on screen, else the last row. Read from v_larsen, never
        from a shared "best": under the old one-fit-per-Measure rows that field
        meant whatever the row's fit made it mean."""
        row = getattr(self, "_current", None)
        if not row:
            rows = list(getattr(self, "_rows", None) or [])
            row = rows[-1] if rows else {}
        return _num(row.get("v_larsen")), row

    def _scar_centroid(self):
        """(lat, lon) of the digitized scar, in EPSG:4326, or (None, None).

        Compared against the detection's epicentre, this is the number that says
        whether the seismic location was any good. The SOURCE scar if one is
        assigned, else the total outline — the same polygons the volume was
        measured from, not the draw target."""
        lat, lon, _role = self._outline_centroid(("best", "total"))
        return lat, lon

    def landslide_centre(self):
        """(lat, lon, role) of the whole landslide's centre in EPSG:4326: the
        TOTAL outline's centroid, else the source scar's when no total is
        drawn; (None, None, None) without either. For the 3D viewer's
        seismic-offset row."""
        return self._outline_centroid(("total", "best"))

    def _outline_centroid(self, roles):
        """(lat, lon, role) of the first assigned role layer's centroid."""
        try:
            from qgis.core import (QgsCoordinateReferenceSystem,
                                   QgsCoordinateTransform, QgsProject)
            lyr = role = None
            for r in roles:
                lyr = self._role_layer(r)
                if lyr is not None:
                    role = r
                    break
            if lyr is None:
                return None, None, None
            feats = list(lyr.getSelectedFeatures()) or list(lyr.getFeatures())
            if not feats:
                return None, None, None
            geom = None
            for f in feats:
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                geom = g if geom is None else geom.combine(g)
            if geom is None:
                return None, None, None
            c = geom.centroid().asPoint()
            src = lyr.crs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            if src.isValid() and src != dst:
                c = QgsCoordinateTransform(
                    src, dst, QgsProject.instance()).transform(c)
            return c.y(), c.x(), role
        except Exception:
            return None, None, None

    def _event_row(self):
        """The event-level columns stamped onto every exported row: the
        detection, where the digitized scar sits, and the implied depth."""
        from . import verdict as V
        det = getattr(self.dock, "detection", None)
        row = det.as_row() if det is not None else {}
        _larsen, src = self._estimates()
        depth, _area = _implied_depth(src)
        row["implied_depth"] = "" if depth is None else f"{depth:.2f}"
        lat, lon = self._scar_centroid()
        row["scar_lat"] = "" if lat is None else f"{lat:.6f}"
        row["scar_lon"] = "" if lon is None else f"{lon:.6f}"
        off = (V.haversine_km(det.lat, det.lon, lat, lon)
               if det is not None and det.is_locatable() and lat is not None else None)
        row["offset_km"] = "" if off is None else f"{off:.2f}"
        return row

    def _export_csv(self):
        if not self._rows:
            self._append_log("Nothing to export — the results table is empty.")
            return
        start = self.dock.results_dir()        # start in this project's folder
        path, _ = QFileDialog.getSaveFileName(
            self, "Export volume results",
            os.path.join(start, "landslide_volumes.csv"), "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([h for h, _k in CSV_FIELDS])
                stamp = self._event_row()
                for row in self._rows:
                    merged = dict(row)
                    # the detection identity belongs to the EVENT, so every
                    # measured slide carries it; a row's own value always wins
                    # if it has one.
                    for k, val in stamp.items():
                        if merged.get(k) in (None, ""):
                            merged[k] = val
                    w.writerow(["" if merged.get(k) is None else merged.get(k)
                                for _h, k in CSV_FIELDS])
        except OSError as e:
            self._append_log(f"Could not write {path}: {e}")
            return
        self._append_log(f"Wrote {len(self._rows)} row(s) to {path}")

    # ---------- write-back ----------
    def _write_to_layer(self):
        if not self._rows:
            self._notify("Nothing to write — measure a slide first.")
            return
        # _selected_rows() falls back to EVERY row when nothing is clicked, which
        # is the right default for Copy and Export CSV — they only read. This one
        # writes attributes onto the outline features and COMMITS the edit, so
        # doing that to a whole table because the user had not clicked a row is a
        # data-loss trap. Ask first, and say how many.
        explicit = sorted({i.row() for i in self.table.selectedIndexes()})
        if not explicit and len(self._rows) > 1:
            from qgis.PyQt.QtWidgets import QMessageBox
            names = ", ".join(str(r.get("name") or "?") for r in self._rows[:4])
            if len(self._rows) > 4:
                names += f", … ({len(self._rows)} in total)"
            ok = QMessageBox.question(
                self, "Write to layer",
                f"No rows are selected, so this will stamp attributes onto ALL "
                f"{len(self._rows)} measured slides and commit the edit:\n\n"
                f"{names}\n\nSelect rows first to write just those. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if ok != QMessageBox.StandardButton.Yes:
                self._append_log("Write to layer cancelled.")
                return
        project = QgsProject.instance()
        written = 0
        for r in self._selected_rows():
            row = self._rows[r]
            layer = project.mapLayer(row["layer_id"])
            if layer is None:
                self._append_log(
                    f"“{row['name']}”: layer {row['layer_name']} is no longer "
                    "in the project.")
                continue
            if self._write_one(layer, row):
                written += 1
        if written:
            self._notify(
                f"Wrote attributes for {written} slide(s). The layer's edits "
                "are committed — save the project/layer as usual.")
        else:
            self._notify("Nothing was written — see the log for why.",
                         level=Qgis.MessageLevel.Warning)

    def _write_one(self, layer, row):
        caps = layer.dataProvider().capabilities()
        if not caps & Qgis.VectorProviderCapability.ChangeAttributeValues:
            self._append_log(
                f"“{row['name']}”: {layer.name()} does not accept attribute "
                "edits (read-only source).")
            return False

        if not layer.getFeature(row["fid"]).isValid():
            self._append_log(
                f"“{row['name']}”: feature {row['fid']} is no longer in "
                f"{layer.name()} — saving a layer renumbers features that were "
                "still unsaved when they were measured. Measure it again, then "
                "write.")
            return False

        missing = [(n, t) for n, t, _k in WRITEBACK_FIELDS
                   if layer.fields().indexOf(n) < 0]
        if missing:
            if not caps & Qgis.VectorProviderCapability.AddAttributes:
                self._append_log(
                    f"“{row['name']}”: {layer.name()} cannot take new fields "
                    f"({', '.join(n for n, _t in missing)} are missing).")
                return False
            # Adding fields needs a clean edit buffer. Committing one that holds
            # unsaved digitizing would save the user's work behind their back
            # AND renumber the very feature we are about to write to, so ask
            # rather than do it.
            if layer.isEditable() and layer.isModified():
                self._append_log(
                    f"“{row['name']}”: {layer.name()} has unsaved edits and "
                    "needs new fields. Save the layer (toggle editing off), "
                    "measure again, then Write to layer.")
                return False
            if layer.isEditable():
                layer.commitChanges()
            if not layer.dataProvider().addAttributes(
                    [QgsField(n, t) for n, t in missing]):
                self._append_log(
                    f"“{row['name']}”: could not add fields to {layer.name()}.")
                return False
            layer.updateFields()

        started = not layer.isEditable()
        if started and not layer.startEditing():
            self._append_log(
                f"“{row['name']}”: could not open {layer.name()} for editing.")
            return False
        ok = True
        for name, _t, key in WRITEBACK_FIELDS:
            idx = layer.fields().indexOf(name)
            if idx < 0:
                continue
            value = row.get(key)
            if not layer.changeAttributeValue(
                    row["fid"], idx,
                    QVariant() if value is None else value):
                ok = False
        if started and not layer.commitChanges():
            self._append_log(
                f"“{row['name']}”: commit failed — "
                f"{'; '.join(layer.commitErrors())}")
            return False
        if not ok:
            self._append_log(
                f"“{row['name']}”: some attributes could not be set on "
                f"feature {row['fid']}.")
        legacy = [n for n in LEGACY_WRITEBACK if layer.fields().indexOf(n) >= 0]
        if legacy:
            self._append_log(
                f"“{layer.name()}” also has {', '.join(legacy)} from an earlier "
                "write-back (an older fit, or area × thickness) — left as they "
                "were. The current volumes are vsrc_* (Larsen et al. 2010, best "
                "source outline), vlowsrc_m3 and vhisrc_m3 (source low / high).")
        return ok


# ---------- name matching ----------
def _name_tokens(name):
    """Lower-case word tokens of a layer name, punctuation flattened.

    "Source Area (low)", "source_area_LOW" and "Source Area - Low" all reduce to
    {"source", "area", "low"}, which is what lets the role drop-downs recognise
    an existing project's layers whatever the capitalisation or bracketing."""
    return frozenset(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


# ---------- formatting ----------
def _num(v):
    """A row value as a float, or None. Row values are usually already numeric
    but can arrive as formatted strings from a restored project, so parse
    defensively rather than letting a str reach the volume arithmetic."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v):
    """Numbers for display: thousands-separated, and never more precision than
    the measurement carries. Areas and volumes span many orders of magnitude,
    so the decimal count follows the value."""
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.3g}"


def _implied_depth(row):
    """(depth_m, area_m2): the Larsen et al. volume spread over the TOTAL outline.

    Depth is None when either is missing. Over the total rather than the scar,
    so it is a mean thickness for the whole slide — comparable with measured
    deposit thicknesses — instead of a scar depth fixed by the area alone (V/A of the
    Larsen fit on its own outline is just αA^(γ−1))."""
    from . import verdict as V
    area = _num(row.get("a_total"))
    return V.implied_depth_m(_num(row.get("v_larsen")), area), area


def _km2(a):
    """An area for the readouts, in km² to 3 significant figures."""
    return "—" if a is None else f"{a / 1e6:,.3g} km²"


def _vol_range(v, lo, hi):
    """'4.31 Mm³ (2.59–6.03)' — a volume at the precision either fit supports."""
    txt = f"{v / 1e6:,.3g} Mm³"
    if lo is not None and hi is not None:
        txt += f"  ({lo / 1e6:,.3g}–{hi / 1e6:,.3g})"
    return txt


def _truthy(v):
    """A QgsSettings bool, which comes back as a bool, 'true'/'false' or 1/0."""
    return v is True or str(v).strip().lower() in ("true", "1", "yes")
