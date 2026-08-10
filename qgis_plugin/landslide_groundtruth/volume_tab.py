"""The Volume tab: landslide area -> volume via Larsen et al. (2010).

Where this sits in the workflow: the other tabs get you imagery and elevation
change; this one turns the polygons you digitize over that imagery into numbers.

WHICH OUTLINE THE VOLUME COMES FROM: it follows the Fit, because the Larsen
coefficients are calibrated per outline definition. The source-scar fit (Larsen
Table S1, the default) converts the BEST SOURCE outline and widens the volume
range with the low/high source outlines when they are assigned. The total-area
fit converts the TOTAL outline instead, once its coefficients are filled in (see
LARSEN_TOTAL in volume_calc). Whichever outline is NOT the fit's calibrated input
is still measured and reported alongside, never run through the relation, because
the relation answers a question about whatever area it is handed — feeding the
scar fit a total outline reads high, and feeding a total fit a source outline
reads low.

How the pieces fit:

  Roles     Four layer pickers: one TOTAL and three SOURCE roles (best/low/high).
            Under the source-scar fit the SOURCE best drives the volume and
            source low/high set its range; the total is reported alongside. Under
            the total-area fit the TOTAL drives it and the source roles are
            reported. Assigning by layer rather than by map selection means the
            input is explicit and re-measurable, and layers named for their role
            — "Total Area", "Source Area (low)" — are recognised automatically,
            so a project that already holds the outlines comes up ready to
            measure. A role layer holding several polygons contributes its
            largest; the rest are reported, not combined, since they are
            alternative attempts at one outline rather than parts of it.

  Area      Ellipsoidal plan-view area (QgsDistanceArea + the project
            ellipsoid) — the same thing $area gives, and the same quantity
            Larsen et al. measured from imagery, so the published coefficients
            stay valid. Slope-corrected true surface area would be larger and
            would NOT be what the regression was fitted to.

  Fit       Which calibration to run: the supplied source-scar fit, or a
            total-area fit whose coefficients ship EMPTY (see volume_calc). The
            outline instruction on the tab follows this choice, so the polygon
            and the calibration can't silently disagree.

  Length    A medial-axis centerline of the total landslide outline (see
            centerline.py) — a whole-slide runout length, taken from the total
            even under the scar fit; with no total assigned it spines the
            converted outline instead. Dropped into an editable scratch layer so
            you can trim the ends or nudge it and re-measure. Reported for
            reference only — the volume comes from area alone.

  Results   One row per slide, accumulating across slides so a session's work
            exports as one CSV, plus a write-back that stamps the numbers onto
            the converted outline's own feature so they travel with the geometry.

The volume arithmetic itself lives in volume_calc, which prefers the project's
canonical larsen_BR_volume.py and names whichever implementation ran.
"""
import csv
import os
import re
from functools import partial

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QSplitter, QFileDialog, QApplication, QHeaderView, QToolButton,
)
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsDistanceArea, QgsUnitTypes,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsField, QgsFeature, QgsLineSymbol, QgsFillSymbol, QgsVectorDataProvider,
)
from qgis.gui import QgsCollapsibleGroupBox

from . import centerline as centerline_mod
from . import volume_calc


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

# Scratch polygon layer "New scar layer" creates to digitize outlines into, when
# the project doesn't already have one. Memory-backed, like the centerlines —
# the tab says so, and Save Features As… is how you keep it.
SCAR_LAYER = "Landslide scars"

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

# Attribute fields the write-back adds to the scar layer. (name, type, source
# key in a results row.) Kept short and lower-case so they survive a Shapefile
# round-trip (10-character field-name limit) as well as GeoPackage. area_m2 is
# the area that PRODUCED the volume (source best under the scar fit, total under
# the total fit); area_lo/hi_m2 are the low/high areas that set the range;
# total_m2 records the total outline for context when it wasn't the converted one.
WRITEBACK_FIELDS = [
    ("slide", QVariant.String, "name"),
    ("fit", QVariant.String, "fit_label"),
    ("material", QVariant.String, "material_label"),
    ("area_m2", QVariant.Double, "a_conv"),
    ("area_lo_m2", QVariant.Double, "a_conv_low"),
    ("area_hi_m2", QVariant.Double, "a_conv_high"),
    ("total_m2", QVariant.Double, "a_total"),
    ("length_m", QVariant.Double, "length"),
    ("vol_m3", QVariant.Double, "v_best"),
    ("vol_lo_m3", QVariant.Double, "v_low"),
    ("vol_hi_m3", QVariant.Double, "v_high"),
]

# Measurement units we refuse to convert from. Square degrees because QGIS only
# has one crude global factor for them, and "unknown" because that is what a CRS
# the install cannot resolve reports — in both cases the number is not an area in
# any usable sense, and feeding it to the volume relation would produce a
# confident answer that is wrong by orders of magnitude. See _measure_area.
UNUSABLE_AREA_UNITS = (QgsUnitTypes.AreaSquareDegrees, QgsUnitTypes.AreaUnknownUnit)
UNUSABLE_LENGTH_UNITS = (QgsUnitTypes.DistanceDegrees,
                         QgsUnitTypes.DistanceUnknownUnit)

TABLE_COLS = ["Slide", "Fit", "Material", "Total area (m²)", "V best (m³)",
              "V low (m³)", "V high (m³)", "Src best (m²)", "Src low (m²)",
              "Src high (m²)", "Length (m)", "Layers"]

# CSV header + the row keys behind it, so the export carries raw numbers rather
# than the table's thousands-separated display strings. converted_area_m2 is the
# one the volume came from (source best under the scar fit, total under the total
# fit); converted_from names which role that was. The other areas are recorded
# but were not converted.
CSV_FIELDS = [
    ("slide", "name"), ("fit", "fit"), ("material", "material"),
    ("converted_from", "conv_role"),
    ("converted_area_m2", "a_conv"),
    ("converted_area_low_m2", "a_conv_low"),
    ("converted_area_high_m2", "a_conv_high"),
    ("volume_best_m3", "v_best"), ("volume_low_m3", "v_low"),
    ("volume_high_m3", "v_high"),
    ("total_area_m2", "a_total"),
    ("source_best_m2", "src_best"), ("source_low_m2", "src_low"),
    ("source_high_m2", "src_high"),
    ("centerline_m", "length"), ("centerline_method", "length_method"),
    ("converted_layer", "layer_name"), ("total_layer", "total_layer"),
    ("source_best_layer", "best_layer"),
    ("source_low_layer", "low_layer"), ("source_high_layer", "high_layer"),
    ("converted_feature_id", "conv_fid"), ("calculator", "calc"),
]


class VolumeTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings
        self._current = None        # last measurement (dict), not yet in the table
        self._rows = []             # accumulated result rows (raw values)
        self._cl_layer_id = None    # scratch centerline layer
        self._cl_fid = None         # centerline feature for the current slide
        self._planar_noted = False  # measurement-mode note already logged
        self._watched = []          # layer ids whose edit signals we follow
        # role -> set by hand (stop name-matching it) / last name-matched layer
        self._role_pinned = {"total": False, "best": False,
                             "low": False, "high": False}
        self._role_auto = {}
        self._build_ui()
        # after every combo exists: fill them and sync the fit-dependent text
        self._refresh_layers()
        self._on_fit_changed()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        intro = QLabel(
            "Volume from landslide area — Larsen et al. (2010) area–volume "
            "scaling. The area converted to a volume follows the Fit below: the "
            "source-scar fit converts the BEST SOURCE outline (and widens the ± "
            "range with the low/high source outlines); the total-area fit "
            "converts the TOTAL outline. The outline not used is measured and "
            "reported alongside. Assign the layers below and press Measure.")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        # What the outline must BE depends on which calibration is selected, so
        # this text follows the Fit choice rather than stating one of them as if
        # it were always true (see _on_fit_changed).
        self.outline_lbl = QLabel()
        self.outline_lbl.setWordWrap(True)
        self.outline_lbl.setStyleSheet("QLabel { color: #e65100; }")
        root.addWidget(self.outline_lbl)

        root.addWidget(self._build_scar_box())

        form = QFormLayout()
        self.name_edit = QLineEdit("slide 1")
        self.name_edit.setToolTip(
            "Label for this slide in the results table, the CSV export and the "
            "centerline layer. Auto-increments after each Add to results.")
        form.addRow("Slide name", self.name_edit)

        self.fit_combo = QComboBox()
        for label, _key in volume_calc.FITS:
            self.fit_combo.addItem(label)
        self.fit_combo.setToolTip(
            "Which calibration to run the area through.\n\n"
            "Source scar — the fit carried by larsen_BR_volume.py; the outline "
            "must be the evacuated source scar alone.\n\n"
            "Total landslide area — source + runout + deposit. Needs its own "
            "coefficients: see LARSEN_TOTAL in volume_calc.py. Until those are "
            "filled in the tab reports it as not configured rather than reusing "
            "the scar fit on a larger polygon, which would read high.")
        self.fit_combo.currentIndexChanged.connect(self._on_fit_changed)
        form.addRow("Fit", self.fit_combo)

        self.material_combo = QComboBox()
        for label, _key in volume_calc.MATERIALS:
            self.material_combo.addItem(label)
        self.material_combo.setToolTip(
            "Which material's coefficients to use. Bedrock failures are deeper "
            "for a given area than soil failures, so this choice moves the "
            "volume substantially — pick it from what actually failed, not the "
            "surrounding cover.")
        form.addRow("Hillslope material", self.material_combo)
        root.addLayout(form)

        # --- actions ---
        btns = QHBoxLayout()
        self.measure_btn = QPushButton("Measure")
        self.measure_btn.setToolTip(
            "Measure the assigned layers and convert the area the selected Fit "
            "is calibrated on — the best SOURCE area for the scar fit (source "
            "low/high widen the range), the TOTAL area for the total fit. The "
            "outline not converted is measured and recorded alongside.")
        self.measure_btn.clicked.connect(self._measure)
        self.add_btn = QPushButton("Add to results ↓")
        self.add_btn.setToolTip(
            "Append the measurement below to the results table and move on to "
            "the next slide.")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self._add_row)
        for b in (self.measure_btn, self.add_btn):
            btns.addWidget(b)
        root.addLayout(btns)

        root.addWidget(self._build_current_box())
        root.addWidget(self._build_centerline_box())

        # --- results ---
        split = QSplitter(Qt.Vertical)

        tablebox = QWidget()
        tl = QVBoxLayout(tablebox)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Results (one row per slide)"))
        self.table = QTableWidget(0, len(TABLE_COLS))
        self.table.setHorizontalHeaderLabels(TABLE_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        tl.addWidget(self.table)

        rbtns = QHBoxLayout()
        self.write_btn = QPushButton("Write to layer")
        self.write_btn.setToolTip(
            "Stamp the numbers onto the CONVERTED outline's own feature as "
            "attributes (fields are added if missing), so they travel with the "
            "geometry. Applies to the selected rows, or all rows if none are "
            "selected. Needs a layer that accepts attribute edits.")
        self.write_btn.clicked.connect(self._write_to_layer)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the table to the clipboard as TSV.")
        self.copy_btn.clicked.connect(self._copy_table)
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.setToolTip(
            "Write every row to CSV with raw unrounded values.")
        self.csv_btn.clicked.connect(self._export_csv)
        self.remove_btn = QPushButton("Remove row")
        self.remove_btn.setToolTip("Drop the selected row(s) from the table.")
        self.remove_btn.clicked.connect(self._remove_rows)
        for b in (self.write_btn, self.copy_btn, self.csv_btn, self.remove_btn):
            rbtns.addWidget(b)
        tl.addLayout(rbtns)
        split.addWidget(tablebox)

        logbox = QWidget()
        lo = QVBoxLayout(logbox)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.addWidget(QLabel("Log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        lo.addWidget(self.log)
        split.addWidget(logbox)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

    def _build_scar_box(self):
        """Where the outlines come from.

        This panel is only about DRAWING. Nothing here is measured — the layer
        chosen here is just where "New scar layer" and "Draw outline" digitize,
        so a session's outlines land somewhere deliberate. Which layers get
        measured is decided by the role drop-downs in Current measurement, so
        drawing and measuring stay separate concerns.

        Both buttons hand off to QGIS's own digitizing tools rather than
        reimplementing them."""
        box = QgsCollapsibleGroupBox("Scar outlines (draw)")
        box.setSaveCollapsedState(False)
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Draw into"))
        # underscore-prefixed so project_state skips it: the layer list is
        # rebuilt from the project anyway, and persisting it would write a
        # project entry (marking the project dirty) on every repopulate.
        self._layer_combo = QComboBox()
        self._layer_combo.setToolTip(
            "Layer the drawing buttons digitize into. This does NOT decide what "
            "gets measured — the Total / Source area drop-downs below do.")
        row.addWidget(self._layer_combo, 1)
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Re-read the polygon layers in this project.")
        refresh.clicked.connect(self._refresh_layers)
        row.addWidget(refresh)
        v.addLayout(row)

        btns = QHBoxLayout()
        self.new_layer_btn = QPushButton("New scar layer")
        self.new_layer_btn.setToolTip(
            f"Create an empty polygon layer (“{SCAR_LAYER}”), make it active, "
            "switch it into edit mode and arm QGIS's Add Polygon tool — so you "
            "can start drawing straight away. Press it once per outline: one "
            "layer for the best estimate, and, for an uncertainty range, one "
            "each for the conservative and generous interpretations. Scratch "
            "layers: use Export > Save Features As… to keep them.")
        self.new_layer_btn.clicked.connect(self._new_scar_layer)
        self.draw_btn = QPushButton("Draw outline")
        self.draw_btn.setToolTip(
            "Put the layer above into edit mode and arm QGIS's Add Polygon "
            "tool, ready to digitize another outline into it.")
        self.draw_btn.clicked.connect(self._draw_outline)
        for b in (self.new_layer_btn, self.draw_btn):
            btns.addWidget(b)
        v.addLayout(btns)

        self.sel_lbl = QLabel()
        self.sel_lbl.setWordWrap(True)
        self.sel_lbl.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(self.sel_lbl)

        project = QgsProject.instance()
        project.layersAdded.connect(self._refresh_layers)
        project.layersRemoved.connect(self._refresh_layers)
        self._layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        return box

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
                and l.geometryType() == QgsWkbTypes.PolygonGeometry
                and l.name() != CENTERLINE_LAYER]

    def _refresh_layers(self, *_args):
        """Repopulate the draw-target and the three role drop-downs.

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
        # state the tab can be in. The draw target is different — it needs
        # somewhere to draw, so it does default to the first layer.
        roles = (("total", self._total_combo), ("best", self._best_combo),
                 ("low", self._low_combo), ("high", self._high_combo))
        for combo, has_none in [(self._layer_combo, False)] + \
                               [(c, True) for _r, c in roles]:
            keep = combo.currentData() if combo.count() else None
            combo.blockSignals(True)
            combo.clear()
            if has_none:
                combo.addItem("— none —", None)
            for layer in layers:
                combo.addItem(layer.name(), layer.id())
            if not combo.count():
                combo.addItem("— no polygon layer yet —", None)
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
                    f"A_{role}: matched “{layer.name()}” by name. Change the "
                    f"A_{role} drop-down to override.")

        self._on_layer_changed()
        self._update_role_label()

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

    def _chosen_layer(self):
        """The DRAW target — not what gets measured (see _role_layer)."""
        return self._layer_from(self._layer_combo)

    def _layer_from(self, combo):
        layer = QgsProject.instance().mapLayer(combo.currentData() or "")
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _on_layer_changed(self, *_args):
        """Keep the draw-target's polygon count on screen, following the layer's
        own edit signals so it stays true while you digitize."""
        layer = self._chosen_layer()
        for lid in list(self._watched):
            watched = QgsProject.instance().mapLayer(lid)
            if watched is None:
                continue
            for signal in ("featureAdded", "featuresDeleted", "geometryChanged"):
                try:
                    getattr(watched, signal).disconnect(self._update_draw_label)
                except (TypeError, RuntimeError, AttributeError):
                    pass
        self._watched = []
        if layer is not None:
            for signal in ("featureAdded", "featuresDeleted", "geometryChanged"):
                try:
                    getattr(layer, signal).connect(self._update_draw_label)
                except (TypeError, RuntimeError, AttributeError):
                    pass
            self._watched = [layer.id()]
        self._update_draw_label()

    def _update_draw_label(self, *_args):
        layer = self._chosen_layer()
        if layer is None:
            self.sel_lbl.setText(
                "No polygon layer in this project — press New scar layer to "
                "make one and start drawing.")
            return
        n = layer.featureCount()
        if n <= 0:
            self.sel_lbl.setText(
                f"“{layer.name()}” is empty — press Draw outline and click the "
                "outline's vertices, right-click to finish.")
        else:
            self.sel_lbl.setText(
                f"“{layer.name()}” holds {n} polygon{'s' if n != 1 else ''}. "
                "Assign it to a role below, or Draw outline to add another.")
        self._update_role_label()

    # ---------- the measured layers (roles) ----------
    def _role_layer(self, role):
        """The layer assigned to 'total' / 'best' / 'low' / 'high', or None."""
        return self._layer_from({"total": self._total_combo,
                                 "best": self._best_combo,
                                 "low": self._low_combo,
                                 "high": self._high_combo}[role])

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
        easy mistake to make once the roles are picked by hand."""
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
            extra = f" of {len(measured)}, largest" if len(measured) > 1 else ""
            parts.append(f"{label} {_fmt(measured[0][0])} m²{extra}")
        if not parts:
            if self._fit() == "total":
                self.role_lbl.setText(
                    "Assign the total landslide outline to “Total area layer” — "
                    "the total-area fit computes the volume from it. The "
                    "source-area layers are optional and reported alongside.")
            else:
                self.role_lbl.setText(
                    "Assign the best source outline to “Source area (best)” — "
                    "the source-scar fit computes the volume from it. Source "
                    "low/high widen the range; the total is reported alongside.")
            self.role_lbl.setStyleSheet("QLabel { color: palette(mid); }")
            return
        problem = self._role_order_problem(areas)
        if not problem:
            if self._fit() == "total" and "total" not in areas:
                problem = ("No total area layer assigned — the total-area fit "
                           "converts the total outline.")
            elif self._fit() != "total" and "best" not in areas:
                problem = ("No source-best layer assigned — the source-scar fit "
                           "converts the best source outline.")
        self.role_lbl.setText("  ·  ".join(parts) + (f"\n⚠ {problem}" if problem else ""))
        self.role_lbl.setStyleSheet(
            "QLabel { color: #c62828; }" if problem
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

    def _new_scar_layer(self):
        crs = self._target_crs()
        name, n = SCAR_LAYER, 1
        while QgsProject.instance().mapLayersByName(name):
            n += 1
            name = f"{SCAR_LAYER} {n}"
        layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", name, "memory")
        if not layer.isValid():
            self._append_log("Could not create the scar layer.")
            return
        layer.dataProvider().addAttributes([QgsField("slide", QVariant.String)])
        layer.updateFields()
        symbol = QgsFillSymbol.createSimple({
            "color": "255,80,80,50", "outline_color": "220,30,30",
            "outline_width": "0.6"})
        if symbol is not None and layer.renderer() is not None:
            layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        self._refresh_layers()
        idx = self._layer_combo.findData(layer.id())
        if idx >= 0:
            self._layer_combo.setCurrentIndex(idx)
        self._append_log(
            f"Created scratch layer “{name}”. It lives in memory only — use "
            "Export > Save Features As… to keep it.")
        # The FIRST layer is wired to the TOTAL role, because that is the one the
        # volume needs and the one you can't measure without. Later layers are
        # left for you to assign — only you know whether the next outline is the
        # source best, the conservative one or the generous one.
        if self._role_layer("total") is None:
            role_idx = self._total_combo.findData(layer.id())
            if role_idx >= 0:
                self._total_combo.setCurrentIndex(role_idx)
                self._append_log(f"Assigned “{name}” to Total area layer.")
        self._draw_outline()

    def _draw_outline(self):
        layer = self._chosen_layer()
        if layer is None:
            self._append_log(
                "No polygon layer to draw on — press New scar layer first.")
            return
        self.iface.setActiveLayer(layer)
        if not layer.isEditable():
            layer.startEditing()
        if self._trigger("actionAddFeature"):
            self._append_log(
                f"Digitizing on “{layer.name()}”: click each vertex of the "
                "outline, right-click to finish. Then assign this layer to "
                "the Total area layer below (or to a source-area role if this "
                "is a source outline) and Measure.")
        else:
            self._append_log(
                f"“{layer.name()}” is in edit mode — use QGIS's Add Polygon "
                "Feature tool to draw the outline.")

    def _build_current_box(self):
        """Which layers are measured, and what came out.

        The drop-downs are the input. Which one is converted to a volume follows
        the Fit: the source-scar fit converts "Source area (best)" and widens the
        range with the low/high source outlines; the total-area fit converts
        "Total area layer". The outline the fit doesn't use is measured and
        reported beside the result, never fed to the relation, because the
        relation answers a question about the area you give it. Everything below
        the drop-downs is read-only computed output, which is also why the
        per-project state save skips it (see project_state._persistable)."""
        box = QgsCollapsibleGroupBox("Current measurement")
        box.setSaveCollapsedState(False)
        f = QFormLayout(box)

        # underscore-prefixed: these hold layer references, rebuilt from the
        # project on every refresh, so persisting them would only write project
        # entries (marking it dirty) for values that are rediscovered anyway.
        self._total_combo = QComboBox()
        self._total_combo.setToolTip(
            "Layer holding the TOTAL landslide outline — the whole affected "
            "area, source through runout to deposit. Converted to a volume only "
            "under the total-area fit; under the source-scar fit it is measured "
            "and reported alongside, not converted.\n\n"
            "Filled in automatically from a layer named “Total Area”, “Total "
            "Landslide Area” or “Landslide Area”. Capitalisation and punctuation "
            "don't matter.")
        f.addRow("Total area layer", self._total_combo)

        self._best_combo = QComboBox()
        self._best_combo.setToolTip(
            "Layer holding the BEST source-scar interpretation. Under the "
            "source-scar fit THIS is the area converted to a volume.\n\n"
            "Filled in automatically from “Source Area (best)”, or a plain "
            "“Source Area”.")
        f.addRow("Source area (best)", self._best_combo)
        self._low_combo = QComboBox()
        self._low_combo.setToolTip(
            "Optional. Layer holding the CONSERVATIVE (smaller) source-scar "
            "interpretation, from “Source Area (low)”. Under the source-scar fit "
            "it lowers the volume range (paired with source high).")
        f.addRow("Source area (low)", self._low_combo)
        self._high_combo = QComboBox()
        self._high_combo.setToolTip(
            "Optional. Layer holding the GENEROUS (larger) source-scar "
            "interpretation, from “Source Area (high)”. Under the source-scar fit "
            "it raises the volume range (paired with source low).")
        f.addRow("Source area (high)", self._high_combo)
        for role, combo in (("total", self._total_combo),
                            ("best", self._best_combo), ("low", self._low_combo),
                            ("high", self._high_combo)):
            combo.currentIndexChanged.connect(self._update_role_label)
            # a genuine user change pins the role; programmatic fills are done
            # with signals blocked, so they don't
            combo.currentIndexChanged.connect(partial(self._pin_role, role))

        self.role_lbl = QLabel()
        self.role_lbl.setWordWrap(True)
        self.role_lbl.setStyleSheet("QLabel { color: palette(mid); }")
        f.addRow(self.role_lbl)

        self.source_out = self._ro("assign the layer the fit needs, then Measure")
        f.addRow("Measured", self.source_out)
        self.area_best_out = self._ro()
        f.addRow("Area → volume", self.area_best_out)
        self.area_range_out = self._ro()
        f.addRow("Other areas (reported)", self.area_range_out)
        self.length_out = self._ro()
        f.addRow("Centerline length", self.length_out)
        self.vol_best_out = self._ro()
        f.addRow("Volume (best)", self.vol_best_out)
        self.vol_range_out = self._ro()
        f.addRow("Volume (±1σ)", self.vol_range_out)
        self.calc_out = self._ro()
        f.addRow("Calculated by", self.calc_out)
        return box

    def _build_centerline_box(self):
        """Centerline tools. Collapsed by default — length is a reference
        number, not an input to the volume, so it stays out of the way until
        it's wanted."""
        box = QgsCollapsibleGroupBox("Centerline (length, optional)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        v = QVBoxLayout(box)

        note = QLabel(
            "Derives the medial-axis spine of the total landslide outline (or "
            "the converted outline if no total is assigned) — it follows the "
            f"slide's bends instead of cutting across them — into “{CENTERLINE_LAYER}”, "
            "left in edit mode so you can trim the ends with the Vertex Tool. "
            "Re-measure afterwards to pick up your edits. Reported for "
            "reference; the volume comes from area alone.")
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(note)

        row = QHBoxLayout()
        self.centerline_btn = QPushButton("Draw centerline")
        self.centerline_btn.setEnabled(False)
        self.centerline_btn.clicked.connect(self._draw_centerline)
        self.remeasure_btn = QPushButton("Re-measure (after editing)")
        self.remeasure_btn.setEnabled(False)
        self.remeasure_btn.clicked.connect(self._remeasure_centerline)
        for b in (self.centerline_btn, self.remeasure_btn):
            row.addWidget(b)
        v.addLayout(row)
        return box

    def _ro(self, text=""):
        e = QLineEdit(text)
        e.setReadOnly(True)
        return e

    def _append_log(self, msg):
        self.log.appendPlainText(msg)

    def _on_fit_changed(self, *_args):
        """Keep the outline instruction honest about the selected calibration,
        and say up front when a fit has no coefficients yet — better than
        letting Measure be the first place that mentions it."""
        fit = self._fit()
        text = volume_calc.FIT_OUTLINE.get(fit, "")
        if fit == "total" and not self._total_ready():
            text += ("  Not configured yet: add LARSEN_TOTAL to "
                     "larsen_BR_volume.py (or volume_calc.py) — see the log.")
        self.outline_lbl.setText("⚠ " + text if text else "")
        # The role readout names which layer the fit needs, so keep it in step.
        if hasattr(self, "role_lbl"):
            self._update_role_label()

    def _fit(self):
        return dict(volume_calc.FITS).get(self.fit_combo.currentText(), "scar")

    def _material(self):
        return dict(volume_calc.MATERIALS).get(
            self.material_combo.currentText(), "bedrock")

    def _total_ready(self):
        return volume_calc.total_coefficients(
            self._material(), self.dock.project_edit.text().strip()) is not None

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
        """Volume from the outline the selected Fit is calibrated on.

        The Larsen coefficients are per outline definition, so which outline is
        converted follows the Fit: the source-scar fit converts the BEST SOURCE
        outline and widens the range with the source low/high outlines when they
        are assigned; the total-area fit converts the TOTAL outline. Whichever
        outline is not the fit's input is measured and reported alongside but
        never fed to the relation — running it on the wrong outline silently
        answers a different question (a total outline through the scar fit reads
        high; a source outline through a total fit reads low)."""
        if not self._polygon_layers():
            self._append_log(
                "This project has no polygon layer. Press New scar layer to "
                "make one and start drawing the outline.")
            return

        fit = self._fit()
        # Every role is measured up front; the Fit decides which one is converted
        # to a volume and which are reported alongside.
        a_total, total_feat, total_layer = self._role_area("total", "Total area")
        a_best, best_feat, best_layer = self._role_area("best", "Source best")
        a_low, _lf, low_layer = self._role_area("low", "Source low")
        a_high, _hf, high_layer = self._role_area("high", "Source high")

        problem = self._role_order_problem(
            {"total": a_total, "best": a_best, "low": a_low, "high": a_high})
        if problem:
            self._append_log(
                f"{problem} Reassign the drop-downs — measuring was skipped "
                "rather than reporting a number from the wrong outline.")
            return

        if fit == "total":
            conv_role, conv_area = "total", a_total
            conv_feat, conv_layer = total_feat, total_layer
            conv_low = conv_high = None
        else:  # scar: the calibrated input is the source scar
            conv_role, conv_area = "source", a_best
            conv_feat, conv_layer = best_feat, best_layer
            conv_low, conv_high = a_low, a_high

        if conv_area is None:
            # A layer that is assigned but empty/unmeasurable already produced a
            # specific message in _role_area; only the truly-unassigned case
            # needs the "pick a layer" guidance here.
            if conv_layer is None and fit == "total":
                self._append_log(
                    "No layer is assigned to “Total area layer”. The total-area "
                    "fit converts the TOTAL landslide outline, so assign that "
                    "layer — or switch Fit to “Source scar” to convert the "
                    "source outline instead.")
            elif conv_layer is None:
                self._append_log(
                    "No layer is assigned to “Source area (best)”. The "
                    "source-scar fit converts the BEST SOURCE outline, so assign "
                    "that layer. The total and low/high layers are optional — "
                    "low/high widen the range, the total is reported alongside.")
            return

        material, material_label = self._material(), self.material_combo.currentText()
        fit_label = self.fit_combo.currentText()
        try:
            v_best, v_low, v_high, calc = volume_calc.volume_source(
                conv_area, A_low=conv_low, A_high=conv_high,
                material=material, fit=fit,
                project_dir=self.dock.project_edit.text().strip())
        except volume_calc.NotConfigured as e:
            self._append_log(str(e))
            self._on_fit_changed()
            return
        except Exception as e:
            self._append_log(f"Volume calculation failed: {e}")
            return

        if conv_role == "source" and total_feat is not None:
            self._warn_if_total_excludes_source(total_feat, best_layer)

        used = [f"{conv_role} “{conv_layer.name()}” → volume"]
        for label, area, layer in (("total", a_total, total_layer),
                                   ("src best", a_best, best_layer),
                                   ("src low", a_low, low_layer),
                                   ("src high", a_high, high_layer)):
            if area is not None and layer is not conv_layer:
                used.append(f"{label} “{layer.name()}”")
        # The centerline is a whole-slide runout length, so it is taken from the
        # TOTAL outline whenever one is assigned — even under the scar fit, where
        # the volume itself comes from the source scar. With no total outline
        # there is nothing else to spine but the converted one.
        if total_feat is not None:
            len_layer_id, len_fid = total_layer.id(), total_feat.id()
            len_from = "total"
        else:
            len_layer_id, len_fid = conv_layer.id(), conv_feat.id()
            len_from = conv_role
        self._current = {
            "name": self.name_edit.text().strip() or "slide",
            "material": material, "material_label": material_label,
            "fit": fit, "fit_label": fit_label,
            "conv_role": conv_role,
            "a_conv": conv_area, "a_conv_low": conv_low, "a_conv_high": conv_high,
            "a_total": a_total,
            "src_best": a_best, "src_low": a_low, "src_high": a_high,
            "v_best": v_best, "v_low": v_low, "v_high": v_high,
            "length": None, "length_method": "",
            "layer_id": conv_layer.id(), "layer_name": conv_layer.name(),
            "conv_fid": conv_feat.id(),
            "len_layer_id": len_layer_id, "len_fid": len_fid, "len_from": len_from,
            "total_layer": total_layer.name() if a_total is not None else "",
            "best_layer": best_layer.name() if a_best is not None else "",
            "low_layer": low_layer.name() if a_low is not None else "",
            "high_layer": high_layer.name() if a_high is not None else "",
            "fids_text": str(conv_feat.id()),
            "used_text": ", ".join(used),
            "calc": calc,
        }
        self._cl_fid = None
        self._show_current()
        self.add_btn.setEnabled(True)
        self.centerline_btn.setEnabled(True)
        self.remeasure_btn.setEnabled(False)

        conv_name = "Source best" if conv_role == "source" else "Total"
        self._append_log(
            f"{conv_name} area = {_fmt(conv_area)} m² from "
            f"“{conv_layer.name()}” — V = {_fmt(v_best)} m³ "
            f"({_fmt(v_low)} – {_fmt(v_high)}). {fit_label}, "
            f"{material_label.lower()}; via {calc}.")
        # Report the areas that were measured but not converted.
        if conv_role == "source":
            others = [f"total {_fmt(a_total)} m²"] if a_total is not None else []
        else:
            others = [f"{lbl} {_fmt(a)} m²" for lbl, a in
                      (("src low", a_low), ("src best", a_best),
                       ("src high", a_high)) if a is not None]
        if others:
            self._append_log(
                "Also measured (not converted): " + ", ".join(others) + ".")
        # Say where the ± range came from.
        if conv_role == "source" and conv_low is not None and conv_high is not None:
            self._append_log(
                "Range combines the published fit uncertainty with the source "
                "low/high area spread.")
        elif conv_role == "source":
            missing = ("both source low and high outlines"
                       if conv_low is None and conv_high is None
                       else "the other source outline")
            self._append_log(
                f"Range is the published fit uncertainty — assign {missing} to "
                "add area uncertainty to it.")
        else:
            self._append_log(
                "Range is the published fit uncertainty — one total outline "
                "carries no area uncertainty of its own.")

    def _warn_if_total_excludes_source(self, total_feat, best_layer):
        """The total outline should contain the source scar. If it doesn't, the
        two are probably from different slides, or the Total area layer is
        pointing at the wrong outline. Flagged, not blocked: a source mapped from
        different imagery can legitimately poke outside the total."""
        try:
            if best_layer is None:
                return
            measured = self._layer_areas(best_layer)
            if not measured:
                return
            total = total_feat.geometry()
            if not total.contains(measured[0][1].geometry()):
                self._append_log(
                    f"Note: the source outline in “{best_layer.name()}” is not "
                    "fully inside the total outline. Check both belong to the "
                    "same slide.")
        except Exception:
            pass

    def _show_current(self):
        c = self._current
        if c is None:
            return
        self.source_out.setText(
            f"{c['used_text']}  (feature {c['conv_fid']})")
        # The area that produced the volume, with its low/high range if any.
        conv_txt = f"{_fmt(c['a_conv'])} m²   ({c['a_conv'] / 1e6:,.4f} km²)"
        if c.get("a_conv_low") is not None and c.get("a_conv_high") is not None:
            conv_txt += (f"   [range {_fmt(c['a_conv_low'])} – "
                         f"{_fmt(c['a_conv_high'])} m²]")
        self.area_best_out.setText(conv_txt)
        # The areas measured but not converted, for context.
        if c["conv_role"] == "source":
            others = ([f"total {_fmt(c['a_total'])}"]
                      if c["a_total"] is not None else [])
        else:
            others = [f"{lbl} {_fmt(a)}" for lbl, a in
                      (("src low", c["src_low"]), ("src best", c["src_best"]),
                       ("src high", c["src_high"])) if a is not None]
        self.area_range_out.setText(
            ("  ·  ".join(others) + " m²") if others else "— (none assigned)")
        self.vol_best_out.setText(
            f"{_fmt(c['v_best'])} m³   ({c['v_best'] / 1e6:,.4f} Mm³)")
        self.vol_range_out.setText(
            f"{_fmt(c['v_low'])} – {_fmt(c['v_high'])} m³")
        self.calc_out.setText(c["calc"])
        if c["length"] is None:
            self.length_out.setText("— (not measured)")
        else:
            self.length_out.setText(
                f"{_fmt(c['length'])} m   ({c['length_method']})")

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
                da.measureArea(geom), QgsUnitTypes.AreaSquareMeters)
        self._note_planar_fallback()
        g, _work = self._to_utm(geom, crs)
        return None if g is None else g.area()

    def _measure_length(self, line, crs):
        """Length in m, or None — same reasoning as _measure_area."""
        da = self._distance_area(crs)
        if da.lengthUnits() not in UNUSABLE_LENGTH_UNITS:
            return da.convertLengthMeasurement(
                da.measureLength(line), QgsUnitTypes.DistanceMeters)
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
        # The length spines the TOTAL outline when one was assigned (see _measure),
        # so a scar-fit volume still gets a whole-slide runout length.
        layer = QgsProject.instance().mapLayer(c["len_layer_id"])
        if layer is None:
            self._append_log("The outline to spine is no longer in the project.")
            return
        feat = layer.getFeature(c["len_fid"])
        geom = feat.geometry() if feat is not None else None
        if geom is None or geom.isEmpty():
            self._append_log("Could not re-read the outline's geometry.")
            return
        if c.get("len_from") != "total":
            self._append_log(
                "No total outline assigned — spining the "
                f"{c.get('len_from', 'converted')} outline instead. Assign the "
                "total landslide outline for a whole-slide runout length.")

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

        c["length"] = length
        c["length_method"] = method
        self._show_current()
        self.remeasure_btn.setEnabled(True)

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
            QgsField("slide", QVariant.String),
            QgsField("length_m", QVariant.Double),
            QgsField("method", QVariant.String),
        ])
        layer.updateFields()
        symbol = QgsLineSymbol.createSimple({"color": "255,32,32", "width": "0.7"})
        if symbol is not None and layer.renderer() is not None:
            layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        self._cl_layer_id = layer.id()
        self._append_log(f"Created scratch layer “{CENTERLINE_LAYER}”.")
        return layer

    # ---------- results table ----------
    def _add_row(self):
        if self._current is None:
            return
        row = dict(self._current)
        row["name"] = self.name_edit.text().strip() or row["name"]
        self._rows.append(row)
        self._refresh_table()
        self.name_edit.setText(_next_name(row["name"]))
        self._current = None
        self._cl_fid = None
        self.add_btn.setEnabled(False)
        self.centerline_btn.setEnabled(False)
        self.remeasure_btn.setEnabled(False)
        self.source_out.setText("assign the layer the fit needs, then Measure")
        for e in (self.area_best_out, self.area_range_out, self.length_out,
                  self.vol_best_out, self.vol_range_out, self.calc_out):
            e.clear()
        self._append_log(f"Added “{row['name']}” to the results table.")

    def _refresh_table(self):
        self.table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            others = [n for n in (row.get("total_layer", ""), row["best_layer"],
                                  row["low_layer"], row["high_layer"])
                      if n and n != row["layer_name"]]
            layers = row["layer_name"] + (f"  + {', '.join(others)}" if others else "")
            values = [
                row["name"], row["fit_label"], row["material_label"],
                _fmt(row["a_total"]),
                _fmt(row["v_best"]), _fmt(row["v_low"]), _fmt(row["v_high"]),
                _fmt(row["src_best"]), _fmt(row["src_low"]), _fmt(row["src_high"]),
                _fmt(row["length"]),
                layers,
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if 3 <= c <= 10:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
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
                del self._rows[r]
        self._refresh_table()

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

    def _export_csv(self):
        if not self._rows:
            self._append_log("Nothing to export — the results table is empty.")
            return
        start = self.dock.out_edit.text().strip() or ""
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
                for row in self._rows:
                    w.writerow(["" if row.get(k) is None else row.get(k)
                                for _h, k in CSV_FIELDS])
        except OSError as e:
            self._append_log(f"Could not write {path}: {e}")
            return
        self._append_log(f"Wrote {len(self._rows)} row(s) to {path}")

    # ---------- write-back ----------
    def _write_to_layer(self):
        if not self._rows:
            self._append_log("Nothing to write — add a measurement first.")
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
            self._append_log(
                f"Wrote attributes for {written} slide(s). The layer's edits "
                "are committed — save the project/layer as usual.")

    def _write_one(self, layer, row):
        caps = layer.dataProvider().capabilities()
        if not caps & QgsVectorDataProvider.ChangeAttributeValues:
            self._append_log(
                f"“{row['name']}”: {layer.name()} does not accept attribute "
                "edits (read-only source).")
            return False

        if not layer.getFeature(row["conv_fid"]).isValid():
            self._append_log(
                f"“{row['name']}”: feature {row['conv_fid']} is no longer in "
                f"{layer.name()} — saving a layer renumbers features that were "
                "still unsaved when they were measured. Measure it again, then "
                "write.")
            return False

        missing = [(n, t) for n, t, _k in WRITEBACK_FIELDS
                   if layer.fields().indexOf(n) < 0]
        if missing:
            if not caps & QgsVectorDataProvider.AddAttributes:
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
                    row["conv_fid"], idx,
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
                f"feature {row['conv_fid']}.")
        return ok


# ---------- name matching ----------
def _name_tokens(name):
    """Lower-case word tokens of a layer name, punctuation flattened.

    "Source Area (low)", "source_area_LOW" and "Source Area - Low" all reduce to
    {"source", "area", "low"}, which is what lets the role drop-downs recognise
    an existing project's layers whatever the capitalisation or bracketing."""
    return frozenset(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


# ---------- formatting ----------
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


def _next_name(name):
    """"slide 1" -> "slide 2"; a name without a trailing number gets " 2"."""
    m = re.search(r"^(.*?)(\d+)(\D*)$", name)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}"
    return f"{name} 2"
