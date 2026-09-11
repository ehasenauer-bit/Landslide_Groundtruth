"""The Fusion tab: optical + SAR change, terrain- and glacier-weighted, into one
landslide score raster.

What it does and why it is a separate tab
-----------------------------------------
Over snow-covered Alaska terrain a landslide leaves two independent signatures —
dark debris where bright snow was (optical DROP) and rough rubble where specular
snow was (SAR backscatter RISE). Individually neither is reliable: snowfall, melt,
cloud shadow and the seasonal illumination swing at 60-63N each move the optical
channel more than a slide does, and speckle, wet snow and layover each move the
SAR channel. What none of them do is move BOTH channels with the right signs at
once. So this tab is a soft AND across the two, and the AND is the whole method.

It CONSUMES the other tabs' outputs rather than re-searching: the S2/Landsat Run's
dBright/dNDSI GeoTIFF and the SAR tab's float32 change raster. That keeps one
scene-selection decision (made where the scene tables and footprints are) instead
of two, and it means the fusion can be re-run with different weightings in seconds
without touching the network.

The compute runs synchronously on the GUI thread, as the SAR tab's change
detection does — QGIS will be unresponsive for a few seconds on a large AOI.
"""
import os
import re

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsColorRampShader, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsProject, QgsRasterLayer, QgsRasterShader, QgsRectangle,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtGui import QColor

from . import fusion_cloud
from . import fusion_core
from . import fusion_glacier
from . import fusion_grid
from . import layer_group as lg
from . import layover_dim
from . import sar_change
from .flow_layout import FlowRow

# combo entries: (label, fusion_core kind key, default absolute floor)
# First entry is the default measure and supplies the default floor. dBright
# leads on measured reliability, not on theory — see the combo's tooltip.
OPTICAL_KINDS = [
    ("dBright — broadband brightness change (recommended)", "dbright", 0.05),
    ("dNDSI — snow index change", "dndsi", 0.10),
]
SAR_KINDS = [
    ("Log-ratio, increase only (recommended)", "logratio", 3.0),
    ("Brightness — intensity z-score (equal best)", "tsint", 3.0),
    ("Int-corr — correlation loss (best on snow/ice)", "intcorr", 0.3),
    ("MT int-corr — multi-temporal texture", "mtcorr", 0.8),
    ("|log-ratio| — change magnitude", "logratio_mag", 1.0),
]

# Name fragments used to auto-preselect the layer combos, best match first.
# NOTE the bare fragment "bright" is deliberately absent from OPTICAL_HINTS: the
# SAR tab names its z-score layer "S1 change brightness z …", which contains it,
# so a bare "bright" would preselect a SAR raster as the OPTICAL input while
# SAR_HINTS picked the same raster as the SAR input — fusing a layer with itself.
OPTICAL_HINTS = ("dbright", "dndsi", "ndsi")
SAR_HINTS = ("log-ratio", "logratio", "log_ratio", "brightness z", "tsint")

# markers that positively identify a raster as belonging to the OTHER sensor;
# a candidate carrying one is never auto-preselected for this side
OPTICAL_EXCLUDE = ("s1 ", "log-ratio", "logratio", "int-corr", "mt-corr",
                   "brightness z")
SAR_EXCLUDE = ("dndsi", "dbright", "dndvi", "ndvi")

# How much of each other two change rasters must cover before the tab will treat
# them as describing the same event. Measured as min(a-in-b, b-in-a), so it is
# the SMALLER of the two containments — see _footprint_match for why the
# one-directional version of this test was worse than no test at all.
FOOTPRINT_MATCH_MIN = 0.60

# score ramp: transparent below 0.2 so a quiet AOI renders as nothing at all
SCORE_RAMP = [(0.0, "#ffffff", 0), (0.20, "#ffffcc", 0), (0.40, "#fed976", 140),
              (0.60, "#fd8d3c", 200), (0.80, "#e31a1c", 230),
              (1.00, "#800026", 255)]

# A small, deliberately restrained palette. Mid-tone hues so they stay legible on
# both the light and dark QGIS themes — QGIS does not tell a widget which theme is
# active, so anything near black or near white would vanish on one of them.
CLR_OK = "#2e9e5b"       # a step is satisfied
CLR_WARN = "#d98324"     # usable but worth reading
CLR_BAD = "#c0392b"      # blocks the run, or a failed step
CLR_ACCENT = "#2c7fb8"   # step numbers, headline figures
CLR_MUTED = "#8a8a8a"    # not reached yet
CLR_OPTICAL = "#e08a2e"  # the optical input, everywhere it appears
CLR_SAR = "#3d8fd1"      # the SAR input, everywhere it appears

ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
EVENT_ID = re.compile(r"(event_\d{6}_\d{4})")

# Score above which a pixel counts as "anomalous" for the area sieve. Matches the
# value at which SCORE_RAMP stops being transparent, so the sieve removes exactly
# the specks that would otherwise be drawn.
SIEVE_THRESHOLD = 0.20

# spelled out rather than built from an EPSG lookup so the DEM round-trip
# cannot fail on a QGIS build with a thin PROJ database
WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
    '298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",'
    '0.0174532925199433],AUTHORITY["EPSG","4326"]]')


class FusionTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock                 # shared Environment fields + helpers
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings
        self._last_layers = []
        self._dem_cache = {}
        self._prev_kind = {}             # side -> last kind, for floor defaults
        self._build_ui()
        self._prev_kind = {"optical": self.optical_kind_combo.currentData(),
                           "sar": self.sar_kind_combo.currentData()}

        # Watch the project so a raster produced in another tab shows up here
        # without a button press. Debounced: loading a project adds layers one
        # at a time and would otherwise re-scan (and re-log) once per layer.
        self._bbox_cache = {}
        self._browsed = {"optical": {}, "sar": {}}   # path -> name, per side
        self._sar_user_choice = False  # True once the user picks SAR by hand
        self._refreshing = False
        self._applying_preset = False
        self._pair_overlap = None      # last auto-pair's footprint overlap
        self._rescan = QTimer(self)
        self._rescan.setSingleShot(True)
        self._rescan.setInterval(400)
        self._rescan.timeout.connect(lambda: self._refresh_layers(quiet=True))
        project = QgsProject.instance()
        self._project_signals = [(project.layersAdded, self._queue_rescan),
                                 (project.layersRemoved, self._queue_rescan)]
        for sig, slot in self._project_signals:
            sig.connect(slot)

        self._watch_for_custom()
        self._refresh_layers()
        self._update_steps()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addWidget(self._intro())
        root.addWidget(self._steps_panel())
        root.addWidget(self._preset_row())

        self.pages = QTabWidget()
        self.pages.addTab(self._run_page(), "Run")
        self.pages.addTab(self._advanced_page(), "Advanced")
        self.pages.setTabToolTip(0, "The two inputs and what to produce.")
        self.pages.setTabToolTip(
            1, "Thresholds, terrain, glacier and cloud options. Every default in "
               "here was measured against three truthed events — you should not "
               "need to open this tab for a normal run.")
        root.addWidget(self.pages)

        root.addWidget(self._action_row())
        root.addWidget(self._candidates_box())
        root.addWidget(self._log_pane(), 1)

    # ---------- header ----------
    def _intro(self):
        lbl = QLabel(
            "Combines an <b>optical</b> change raster with a <b>SAR</b> change "
            "raster into one landslide score. A pixel scores when the sensors "
            "agree — dark debris where snow was, rougher surface where smooth "
            "snow was — which is what separates a slide from a snowfall, a cloud "
            "shadow or speckle.")
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet("QLabel { color: palette(mid); }")
        return lbl

    def _steps_panel(self):
        """Three numbered steps that report the project's ACTUAL state.

        A newcomer should be able to tell what to do next without reading a
        manual, and an expert should be able to see at a glance that the tab is
        about to fuse the pair they think it is — which is why step 2 shows the
        footprint match and not just a tick."""
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self._step_status = []
        for i, title in enumerate(("Optical change raster",
                                   "SAR change raster",
                                   "Fuse")):
            num = QLabel(f"{i + 1}")
            num.setAlignment(Qt.AlignCenter)
            num.setFixedWidth(20)
            num.setStyleSheet(
                f"QLabel {{ color: white; background: {CLR_ACCENT};"
                " border-radius: 9px; font-weight: bold; }")
            name = QLabel(title)
            name.setStyleSheet("QLabel { font-weight: bold; }")
            status = QLabel("…")
            status.setWordWrap(True)
            status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            grid.addWidget(num, i, 0)
            grid.addWidget(name, i, 1)
            grid.addWidget(status, i, 2)
            grid.setColumnStretch(2, 1)
            self._step_status.append(status)
        return box

    def _set_step(self, i, text, colour, detail=""):
        """Headline in `colour`, supporting `detail` smaller and quieter.

        Each step answers two things — is it satisfied, and on what — and those
        deserve different weight. The headline is what you scan; the detail is
        what you check when something looks wrong."""
        lbl = self._step_status[i]
        html = f'<span style="color:{colour};">{text}</span>'
        if detail:
            html += (f'<span style="color:palette(mid); font-size:10px;">'
                     f'&nbsp; {detail}</span>')
        lbl.setText(html)
        lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet("")

    def _update_steps(self):
        """Refresh the three status lines from what is actually selected."""
        opt = self._optical_combo.currentData()
        sar = self._sar_combo.currentData()
        if opt:
            date = self._event_date(opt)
            kind = self.optical_kind_combo.currentText().split(" —")[0]
            extra = ""
            if self.pair_optical_check.isChecked():
                sib, _k = self._optical_sibling(opt, self.optical_kind_combo.currentData())
                if sib:
                    extra = " + dBright/dNDSI pair"
            self._set_step(0, f"✓ {date or os.path.basename(opt)}", CLR_OK,
                           f"{kind}{extra}")
        else:
            self._set_step(0, "Choose an optical raster", CLR_BAD,
                           "a dNDSI or dBright from the Sentinel-2 / Landsat Run")
        if sar:
            n = 1 + (len(self._sar_siblings(sar))
                     if self.pair_sar_check.isChecked() else 0)
            ov = self._pair_overlap
            detail = (f"{ov:.0%} footprint match with the optical raster"
                      if ov is not None else "")
            self._set_step(1, f"✓ {n} detector{'s' if n != 1 else ''}",
                           CLR_OK if (ov is None or ov >= 0.80) else CLR_WARN,
                           detail)
        elif not self.out_fused_check.isChecked():
            self._set_step(1, "Not needed", CLR_MUTED,
                           "'Fused score' is unticked — this is an optical-only run")
        else:
            ov = self._pair_overlap
            if ov is not None and ov < FOOTPRINT_MATCH_MIN:
                self._set_step(1, "No SAR raster covers this area", CLR_BAD,
                               f"best match {ov:.0%} — pick one, or run SAR "
                               "change detection for this event")
            else:
                self._set_step(1, "Choose a SAR raster", CLR_BAD,
                               "a change raster from the SAR tab")
        outs = [n for n, c in (("fused score", self.out_fused_check),
                               ("optical-only", self.out_optical_check))
                if c.isChecked()]
        ready = bool(opt) and (bool(sar) or not self.out_fused_check.isChecked())
        if not outs:
            self._set_step(2, "Nothing to produce", CLR_BAD,
                           "tick an output below")
        elif ready:
            self._set_step(2, "Ready", CLR_OK,
                           "will produce " + " and ".join(outs))
        else:
            self._set_step(2, "Waiting", CLR_MUTED, "on the inputs above")
        self.run_btn.setEnabled(ready and bool(outs))

    # ---------- presets ----------
    def _preset_row(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("Settings")
        lab.setStyleSheet("QLabel { font-weight: bold; }")
        h.addWidget(lab)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Validated default", "default")
        self.preset_combo.addItem("Optical only (ignore SAR)", "optical")
        self.preset_combo.addItem("Custom", "custom")
        self.preset_combo.setToolTip(
            "<b>Validated default</b> — the configuration measured against the "
            "Iliamna, Hubbard and Valdez truth polygons: average the channels, "
            "5×5 smoothing, no terrain or glacier weighting.<br><br>"
            "<b>Optical only</b> — drops SAR entirely. Better on events where the "
            "deposit is quieter in radar than its surroundings (Hubbard).<br><br>"
            "<b>Custom</b> — selected automatically as soon as you change any "
            "control, so you always know when you have left the validated setup.")
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        h.addWidget(self.preset_combo, 1)
        return w

    def _preset_changed(self, _i):
        key = self.preset_combo.currentData()
        if key == "custom" or self._applying_preset:
            return
        self._applying_preset = True
        try:
            self.smooth_combo.setCurrentIndex(self.smooth_combo.findData(5))
            self.mode_combo.setCurrentIndex(self.mode_combo.findData("mean"))
            self.detrend_check.setChecked(True)
            self.pair_optical_check.setChecked(True)
            self.pair_sar_check.setChecked(True)
            self.terrain_check.setChecked(False)
            self.glacier_check.setChecked(False)
            self.lowland_check.setChecked(True)
            self.cloud_check.setChecked(True)
            self.min_area_spin.setValue(0.05)
            self.saronly_cap_spin.setValue(fusion_core.SAR_ONLY_WEIGHT)
            self.out_optical_check.setChecked(True)
            self.out_fused_check.setChecked(key == "default")
            self._append_log(f"preset: {self.preset_combo.currentText()}")
        finally:
            self._applying_preset = False
        self._update_steps()

    def _mark_custom(self, *_a):
        """Any hand edit moves the preset to Custom, so the label never lies."""
        if self._applying_preset or self._refreshing:
            return
        idx = self.preset_combo.findData("custom")
        if idx >= 0 and self.preset_combo.currentIndex() != idx:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
        self._update_steps()

    def _watch_for_custom(self):
        """Connect every tunable control to the Custom switch, by reflection so a
        control added later is covered without extra bookkeeping."""
        for name, obj in vars(self).items():
            if name.startswith("_") or name == "preset_combo":
                continue
            sig = (getattr(obj, "toggled", None) if isinstance(obj, QCheckBox)
                   else getattr(obj, "valueChanged", None)
                   if isinstance(obj, (QSpinBox, QDoubleSpinBox))
                   else getattr(obj, "currentIndexChanged", None)
                   if isinstance(obj, QComboBox) else None)
            if sig is not None:
                sig.connect(self._mark_custom)

    # ---------- pages ----------
    def _run_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)
        v.addWidget(self._inputs_box())
        v.addWidget(self._outputs_box())
        v.addStretch(1)
        return w

    def _advanced_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)
        note = QLabel(
            "Every default here was measured against three truthed events. "
            "Changing them switches the preset to Custom.")
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(note)
        # Collapsed on open. Four expanded panels of spinboxes is the choice
        # paralysis this split exists to remove; the titles say what is inside,
        # and a normal run never needs any of it.
        for build in (self._detect_box, self._terrain_box,
                      self._glacier_box, self._cloud_box):
            gb = build()
            gb.setCollapsed(True)
            v.addWidget(gb)
        v.addStretch(1)
        return w

    def _action_row(self):
        btn_row = FlowRow()
        self.run_btn = QPushButton("Fuse")
        self.run_btn.setDefault(True)
        f = self.run_btn.font()
        f.setBold(True)
        self.run_btn.setFont(f)
        self.run_btn.setStyleSheet(
            f"QPushButton {{ background: {CLR_ACCENT}; color: white;"
            " padding: 6px 18px; border-radius: 3px; }"
            f"QPushButton:disabled {{ background: {CLR_MUTED}; color: #eeeeee; }}"
            "QPushButton:hover:!disabled { background: #24699b; }")
        self.run_btn.setToolTip(
            "Warp both inputs onto the coarser of the two grids, rank each above "
            "its floor, average the channels, and write a multiband GeoTIFF plus "
            "styled layers.")
        self.run_btn.clicked.connect(self._run)
        btn_row.addWidget(self.run_btn)
        return btn_row

    def _candidates_box(self):
        """Ranked shortlist of the scoring blobs. Click a row to zoom to it.

        The score raster alone is hard to read: measured over six truthed
        events it puts the real scar in the top 2% of pixels, but a handful of
        isolated background pixels still score higher, so the scar is rarely the
        brightest thing on screen. As BLOBS it is: ranked by peak score the scar
        came 1st on four of six events and never below 15th of ~120-190, so a
        short list finds it where the heatmap buries it."""
        box = QgsCollapsibleGroupBox("Candidates")
        box.setToolTip(
            "The scoring blobs that survived the area sieve, strongest first.\n\n"
            "Ranked by PEAK score, not area: the largest blob is usually a broad "
            "terrain or illumination artefact, while the slide is the one with "
            "the strongest core. Area is shown so a one-pixel spike is obvious.\n\n"
            "Click a row to zoom the map to that blob.")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 4, 8, 8)
        self.cand_table = QTableWidget(0, 3)
        self.cand_table.setHorizontalHeaderLabels(["#", "Area km\u00b2", "Score"])
        self.cand_table.verticalHeader().setVisible(False)
        self.cand_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cand_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cand_table.setMaximumHeight(180)
        hh = self.cand_table.horizontalHeader()
        hh.setStretchLastSection(True)
        self.cand_table.setColumnWidth(0, 40)
        self.cand_table.setColumnWidth(1, 90)
        self.cand_table.itemSelectionChanged.connect(self._zoom_to_candidate)
        v.addWidget(self.cand_table)
        self.cand_note = QLabel("Run the fusion to list candidates.")
        self.cand_note.setStyleSheet(f"color:{CLR_MUTED}; font-size:10px;")
        v.addWidget(self.cand_note)
        self._cand_rows = []
        self._cand_geo = None
        return box

    def _fill_candidates(self, score, gt, shape, proj, min_px):
        """Populate the shortlist from the FINAL score — the same pixels the map
        shows, so a row always corresponds to something visible."""
        import numpy as np
        self.cand_table.blockSignals(True)
        self.cand_table.setRowCount(0)
        self._cand_rows, self._cand_geo = [], (gt, shape, proj)
        try:
            mask = np.isfinite(score) & (score >= SIEVE_THRESHOLD)
            ys, xs, roots = sar_change.label_blobs(mask)
            dxm, dym = layover_dim.metric_pixel_size(gt, shape[0])
            rows = fusion_core.candidates(score, ys, xs, roots, dxm, dym,
                                          min_px=max(1, int(min_px)), limit=20)
        except Exception as e:                       # noqa: BLE001 — never block a run
            self.cand_table.blockSignals(False)
            self.cand_note.setText(f"candidates unavailable: {type(e).__name__}: {e}")
            return
        for i, c in enumerate(rows, 1):
            r = self.cand_table.rowCount()
            self.cand_table.insertRow(r)
            for col, txt in enumerate((str(i), f"{c['area_km2']:.2f}",
                                       f"{c['peak']:.3f}")):
                self.cand_table.setItem(r, col, QTableWidgetItem(txt))
        self._cand_rows = rows
        self.cand_table.blockSignals(False)
        if rows:
            self.cand_note.setText(
                f"{len(rows)} strongest of the blobs above {SIEVE_THRESHOLD:g} "
                f"\u2014 click a row to zoom. The scar is usually in the top few.")
        else:
            self.cand_note.setText(
                f"nothing scored above {SIEVE_THRESHOLD:g} over the minimum area "
                f"\u2014 no candidate to list.")

    def _zoom_to_candidate(self):
        """Zoom the canvas to the selected blob, padded so it has context."""
        rows = self.cand_table.selectionModel().selectedRows() \
            if self.cand_table.selectionModel() else []
        if not rows or self._cand_geo is None:
            return
        i = rows[0].row()
        if i >= len(self._cand_rows):
            return
        c = self._cand_rows[i]
        gt, _shape, proj = self._cand_geo
        x0 = gt[0] + gt[1] * c["col0"]
        x1 = gt[0] + gt[1] * (c["col1"] + 1)
        y0 = gt[3] + gt[5] * c["row0"]
        y1 = gt[3] + gt[5] * (c["row1"] + 1)
        rect = QgsRectangle(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        pad = max(rect.width(), rect.height()) * 0.6 or 1e-4
        rect.grow(pad)
        try:
            canvas = self.iface.mapCanvas()
            dst = canvas.mapSettings().destinationCrs()
            src = QgsCoordinateReferenceSystem()
            src.createFromWkt(proj)
            if src.isValid() and dst.isValid() and src.authid() != dst.authid():
                rect = QgsCoordinateTransform(
                    src, dst, QgsProject.instance()).transformBoundingBox(rect)
            canvas.setExtent(rect)
            canvas.refresh()
        except Exception as e:                       # noqa: BLE001
            self._append_log(f"  could not zoom to candidate {i + 1}: "
                             f"{type(e).__name__}: {e}")

    def _log_pane(self):
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setLineWrapMode(QTextEdit.WidgetWidth)
        self.log.setStyleSheet(
            "QTextEdit { font-family: Menlo, Consolas, monospace; font-size: 11px; }")
        return self.log

    def _inputs_box(self):
        box = QgsCollapsibleGroupBox("Inputs")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        # the dock can be as narrow as 360 px; without this the label column is
        # squeezed until "Optical change raster" elides to "Optica"
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self._optical_combo = QComboBox()
        self._optical_combo.setToolTip(
            "A dNDSI or dBright GeoTIFF from the Sentinel-2 / Landsat Run. The "
            "cloud mask needs the <event>_metadata.json the Run writes beside it, "
            "so prefer the file in its package folder over a copy.")
        self._optical_browse = QPushButton("…")
        self._optical_browse.setMaximumWidth(32)
        self._optical_browse.clicked.connect(
            lambda: self._browse_into("optical", "optical change raster"))
        self._optical_combo.currentIndexChanged.connect(self._optical_chosen)
        form.addRow(self._tag("Optical change raster", CLR_OPTICAL),
                    self._row(self._optical_combo, self._optical_browse))
        self.optical_kind_combo = QComboBox()
        for label, key, _floor in OPTICAL_KINDS:
            self.optical_kind_combo.addItem(label, key)
        self.optical_kind_combo.setToolTip(
            "Detected from the layer name — only touch it if the guess is wrong.\n\n"
            "dBright (mean of red/green/blue/NIR) is the default because it is "
            "the channel that keeps working. Measured over five truthed events, "
            "background flagged at 50% recall with no SAR: dBright alone never "
            "exceeded 7%, while dNDSI alone went blind (~100%) at Valdez and "
            "Knik-Barry, where the deposit landed on rock and moraine rather "
            "than snow.\n\n"
            "dNDSI ((green - SWIR1)/(green + SWIR1)) is a band RATIO, so the "
            "illumination and BRDF drift that dominates high-latitude albedo "
            "differencing largely cancels — which matters here, because nothing "
            "in this plugin does a topographic illumination correction. That "
            "makes it the better channel WHERE THERE IS SNOW to contrast "
            "against, and it stays worth pairing: the two together beat either "
            "alone once SAR joins. Leave the pairing tickbox on and this choice "
            "only decides which one leads.")
        self.optical_kind_combo.currentIndexChanged.connect(self._kind_changed)
        form.addRow(self._tag("is a", CLR_OPTICAL, bold=False),
                    self.optical_kind_combo)

        self._sar_combo = QComboBox()
        self._sar_combo.setToolTip(
            "The change map from the SAR tab — chosen automatically as the one "
            "whose footprint MATCHES the optical raster above. A raster covering "
            "noticeably different ground is listed greyed out with how far off "
            "it is, rather than offered as a pair; the … button forces any file "
            "you like past that.\n\nThe SAR tab "
            "saves a float32 copy under the layer's own name in "
            "<output>/sar/change. Older runs left only a temporary file, and a "
            "temp file that has since been cleaned up shows here greyed out as "
            "'no longer on disk'.")
        self._sar_browse = QPushButton("…")
        self._sar_browse.setMaximumWidth(32)
        self._sar_browse.clicked.connect(
            lambda: self._browse_into("sar", "SAR change raster"))
        self._sar_combo.currentIndexChanged.connect(self._sar_chosen)
        form.addRow(self._tag("SAR change raster", CLR_SAR),
                    self._row(self._sar_combo, self._sar_browse))
        self.sar_kind_combo = QComboBox()
        for label, key, _floor in SAR_KINDS:
            self.sar_kind_combo.addItem(label, key)
        self.sar_kind_combo.setToolTip(
            "Detected from the layer name — touch it only if the guess is wrong.\n\n"
            "It fixes the SIGN for the amplitude detectors: log-ratio is "
            "10·log10(pre/post), so a brighter-after deposit reads NEGATIVE, while "
            "the brightness z-score is post-minus-pre and reads POSITIVE.\n\n"
            "The correlation detectors (int-corr, MT int-corr) have no polarity to "
            "get wrong — they measure how much the scattering pattern was "
            "rearranged.")
        self.sar_kind_combo.currentIndexChanged.connect(self._kind_changed)
        form.addRow(self._tag("is a", CLR_SAR, bold=False), self.sar_kind_combo)

        # --- everything below is "use more of what you already have" ---
        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Sunken)
        form.addRow(rule)
        more = QLabel("Use every companion raster the same run produced")
        more.setStyleSheet("QLabel { color: palette(mid); }")
        more.setWordWrap(True)
        form.addRow(more)

        self.pair_optical_check = QCheckBox(
            "the matching dBright/dNDSI  (optical)")
        self.pair_optical_check.setChecked(True)
        self.pair_optical_check.setToolTip(
            "The Run writes dNDSI and dBright side by side. They come from "
            "different bands — NDSI is a green/SWIR ratio, brightness a 4-band "
            "mean — so their errors are only partly shared, and averaging them "
            "suppresses artefacts that move only one.\n\nThey are AVERAGED, not "
            "multiplied: at Valdez 2026-08-04 the deposit landed on rock and "
            "moraine, so NDSI never moved (AUC 0.491) while brightness saw it "
            "plainly — a product let the blind channel erase the event "
            "(background at 50% recall 98.8% vs 8.1% averaged).\n\nFound "
            "automatically; nothing to pick.")
        form.addRow(self.pair_optical_check)

        self.pair_sar_check = QCheckBox(
            "the other SAR detectors  (int-corr, brightness-z, MT int-corr)")
        self.pair_sar_check.setChecked(True)
        self.pair_sar_check.setToolTip(
            "Takes the MAXIMUM across every detector the SAR tab produced for this "
            "scene pair.\n\nThey measure different physics: log-ratio a change in "
            "backscattered POWER, int-corr loss of the scattering PATTERN. Each is "
            "blind to different events — int-corr is the best channel measured on "
            "Iliamna (2.76% background at 50% recall, 24.0% precision) and the "
            "worst at Valdez (8.09%); log-ratio is the reverse. Max means either "
            "firing counts.\n\nMeasured worst-case / mean background: log-ratio "
            "alone 4.57% / 3.02%, max over detectors 3.38% / 2.46%. Generate the "
            "others by ticking them in the SAR tab; found automatically here.")
        form.addRow(self.pair_sar_check)

        self.refresh_btn = QPushButton("⟳ Refresh layer list")
        self.refresh_btn.setToolTip(
            "Re-scan the project for raster layers. Use it after running the "
            "Sentinel-2 / Landsat or SAR tab so their new output shows up here — "
            "no need to reload the plugin.")
        self.refresh_btn.clicked.connect(self._refresh_layers)
        form.addRow(self.refresh_btn)
        return box

    def _detect_box(self):
        box = QgsCollapsibleGroupBox("Detection")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)

        self.opt_floor_spin = QDoubleSpinBox()
        self.opt_floor_spin.setRange(0.0, 2.0)
        self.opt_floor_spin.setDecimals(3)
        self.opt_floor_spin.setSingleStep(0.01)
        self.opt_floor_spin.setValue(OPTICAL_KINDS[0][2])
        self.opt_floor_spin.setToolTip(
            "Absolute admission floor, in the optical measure's own units. A pixel "
            "must DROP by at least this much to score at all. Without a floor, "
            "percentile ranking makes a quiet AOI look exactly like a busy one — "
            "the 99th percentile of noise ranks 1.0 just as a real deposit does.")
        form.addRow("Optical floor (decrease)", self.opt_floor_spin)

        self.sar_floor_spin = QDoubleSpinBox()
        self.sar_floor_spin.setRange(0.0, 20.0)
        self.sar_floor_spin.setDecimals(2)
        self.sar_floor_spin.setSingleStep(0.5)
        self.sar_floor_spin.setValue(SAR_KINDS[0][2])
        self.sar_floor_spin.setToolTip(
            "Absolute admission floor in dB (or sigma for the z-score). Default 3 "
            "is the SAR tab's own significance threshold for the multilooked "
            "ratio, so the fusion agrees with what that tab already calls real.")
        form.addRow("SAR floor (dB)", self.sar_floor_spin)

        self.detrend_check = QCheckBox("Remove the AOI-wide median from each layer")
        self.detrend_check.setChecked(True)
        self.detrend_check.setToolTip(
            "Cancels a basin-wide shift from fresh snowfall, melt onset or "
            "illumination change, which moves the whole AOI while a slide moves a "
            "few hundred pixels.\n\nCaveat: this assumes most of the AOI did NOT "
            "change. Over a small AOI dominated by the slide, or a glacier that "
            "changed wholesale, the median IS the signal — turn it off there.")
        form.addRow(self.detrend_check)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Mean of all channels — recommended", "mean")
        self.mode_combo.addItem("Geometric mean √(o·s) — soft AND", "geometric")
        self.mode_combo.addItem("Product o·s — hard AND", "product")
        self.mode_combo.setToolTip(
            "MEAN averages every channel; the other two multiply them.\n\n"
            "Multiplying is a veto: a channel blind to this particular event "
            "zeroes the rest. Across three truthed events every channel was blind "
            "to at least one (dNDSI at Valdez AUC 0.491, SAR at Hubbard 0.368), "
            "and the AND collapsed — background at 50% recall was 6.9% / 100% / "
            "8.6% versus 4.6% / 1.0% / 6.0% for the mean.\n\nThe two "
            "multiplicative modes rank identically to each other; they are kept "
            "for comparison.")
        form.addRow("Combine", self.mode_combo)

        self.saronly_check = QCheckBox(
            "Fall back to SAR alone where optical is missing")
        self.saronly_check.setChecked(True)
        self.saronly_check.setToolTip(
            "Under cloud the optical channel is gone. These pixels score from SAR "
            "alone and are flagged in the confidence band and drawn as a separate "
            "layer, because single-sensor evidence is genuinely weaker and must "
            "not be mistaken for a corroborated detection.")
        form.addRow(self.saronly_check)

        self.saronly_cap_spin = QDoubleSpinBox()
        self.saronly_cap_spin.setRange(0.0, 1.0)
        self.saronly_cap_spin.setDecimals(2)
        self.saronly_cap_spin.setSingleStep(0.05)
        self.saronly_cap_spin.setValue(fusion_core.SAR_ONLY_WEIGHT)
        self.saronly_cap_spin.setToolTip(
            "Scales a SAR-only pixel's score so single-sensor evidence cannot "
            "outrank corroborated evidence.\n\nAt 1.0 it can and does: a SAR-only "
            "pixel at rank 1.0 scores 1.0 while a corroborated pixel at 0.9/0.9 "
            "scores 0.9, and the top of the colour ramp fills with single-sensor "
            "noise — over water and tidal flats above all. 0.5 puts the whole "
            "single-sensor range below a moderately corroborated detection.")
        form.addRow("Single-sensor score cap", self.saronly_cap_spin)

        self.min_area_spin = QDoubleSpinBox()
        self.min_area_spin.setRange(0.0, 10.0)
        self.min_area_spin.setDecimals(3)
        self.min_area_spin.setSingleStep(0.01)
        self.min_area_spin.setValue(0.05)
        self.min_area_spin.setSuffix(" km²")
        self.min_area_spin.setToolTip(
            "Clear scoring blobs smaller than this. A slide is a CONNECTED patch; "
            "isolated specks are speckle and residual noise.\n\nMeasured on "
            "Iliamna 2026-08-08: at score ≥ 0.5 the raw map holds 219 blobs, of "
            "which the slide is the largest at 12x the runner-up. A 0.05 km² "
            "minimum leaves 20; 0.2 km² leaves exactly one — the slide.\n\n"
            "0 disables it.")
        form.addRow("Minimum blob area", self.min_area_spin)

        self.smooth_combo = QComboBox()
        for lbl, k in (("5×5 (recommended)", 5), ("3×3", 3), ("7×7", 7), ("Off", 1)):
            self.smooth_combo.addItem(lbl, k)
        self.smooth_combo.setToolTip(
            "Average the score over a small window before thresholding. A slide "
            "is a CONNECTED patch; speckle and single-pixel index noise are "
            "not.\n\nMeasured on both validated events it improves every "
            "variant — Iliamna 3.9% → 2.7% background at 50% recall, Hubbard "
            "1.6% → 0.9%. Larger windows keep helping on big slides and start "
            "smearing small ones.")
        form.addRow("Spatial smoothing", self.smooth_combo)
        return box

    def _terrain_box(self):
        box = QgsCollapsibleGroupBox("Terrain weighting")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        self.terrain_check = QCheckBox("Weight by nearby steep terrain")
        self.terrain_check.setToolTip(
            "Downloads a Copernicus GLO-30 DEM on the fusion grid and weights the "
            "score by the MAXIMUM slope within the search radius — not by the "
            "pixel's own slope.\n\nThat distinction is the point: an Alaska rock "
            "avalanche detaches at 40-60° and runs out onto a glacier tongue at "
            "2-8°, and the flat deposit is the detectable half. A per-pixel slope "
            "threshold deletes exactly the thing you are looking for.\n\nOFF by "
            "default: measured on three truthed events it HURT, taking mean "
            "background at 50% recall from 3.0% to 6.7% and AUC on Iliamna from "
            "0.901 to 0.711. Alaska landslides run onto flat ground often enough "
            "that gating on terrain costs more than it saves. Turn it on only for "
            "an AOI where you know the target is confined to steep slopes.")
        form.addRow(self.terrain_check)

        self.radius_spin = QSpinBox()
        self.radius_spin.setRange(0, 5000)
        self.radius_spin.setSingleStep(50)
        self.radius_spin.setValue(250)
        self.radius_spin.setSuffix(" m")
        self.radius_spin.setToolTip(
            "How far to look for a steep source. Roughly the runout length you "
            "want to keep below a headwall.")
        form.addRow("Source search radius", self.radius_spin)

        self.slope_lo_spin = QDoubleSpinBox()
        self.slope_lo_spin.setRange(0.0, 90.0)
        self.slope_lo_spin.setValue(10.0)
        self.slope_lo_spin.setSuffix(" °")
        form.addRow("Weight 0 below", self.slope_lo_spin)
        self.slope_hi_spin = QDoubleSpinBox()
        self.slope_hi_spin.setRange(0.0, 90.0)
        self.slope_hi_spin.setValue(30.0)
        self.slope_hi_spin.setSuffix(" °")
        self.slope_hi_spin.setToolTip(
            "Smooth ramp between the two angles rather than a hard cutoff, so the "
            "weighting fades instead of stamping step edges into the score.")
        form.addRow("Weight 1 above", self.slope_hi_spin)
        # an inverted ramp would otherwise collapse the terrain weight to a hard
        # step with no indication that the two boxes disagree
        self.slope_lo_spin.valueChanged.connect(
            lambda v: self.slope_hi_spin.setMinimum(float(v)))
        self.slope_hi_spin.setMinimum(float(self.slope_lo_spin.value()))

        self.lowland_check = QCheckBox("Ignore low-lying ground (water, tidal flats)")
        self.lowland_check.setChecked(True)
        self.lowland_check.setToolTip(
            "The steepest-ground-within-reach rule is what preserves a flat runout "
            "below a headwall — but at a coastline it lets an intertidal mudflat "
            "borrow the slope of the hill next to it. Water and wet tidal mud swing "
            "C-band backscatter by many dB between passes, so in a coastal AOI they "
            "are the loudest false positives, and where optical is missing there is "
            "nothing to veto them.\n\nCost, stated plainly: a real runout that "
            "reached tidal level is suppressed too. Untick it for a fjord-wall "
            "event.")
        form.addRow(self.lowland_check)

        self.min_elev_spin = QDoubleSpinBox()
        self.min_elev_spin.setRange(0.0, 500.0)
        self.min_elev_spin.setSingleStep(5.0)
        self.min_elev_spin.setValue(15.0)
        self.min_elev_spin.setSuffix(" m")
        self.min_elev_spin.setToolTip(
            "Weight is 0 below this elevation and ramps to 1 over the next 10 m.")
        form.addRow("Ignore below", self.min_elev_spin)
        return box

    def _glacier_box(self):
        box = QgsCollapsibleGroupBox("Glacier weighting")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        self.glacier_check = QCheckBox("Downweight pixels inside glacier outlines")
        self.glacier_check.setToolTip(
            "Downweight, never delete. Debris-covered ice changes appearance "
            "wholesale between any two dates and generates the loudest false "
            "positives — but a rock avalanche running out ONTO a glacier is a "
            "common Alaska event, and a hard mask erases those.\n\nOFF by "
            "default because that is not hypothetical: the Hubbard 2026-07-28 "
            "deposit sits INSIDE the RGI outline and was downweighted to 0.30, "
            "taking background flagged at 50% recall from 1.0% to 11.0%. Turn it "
            "on only when ice is the nuisance, not the substrate.")
        form.addRow(self.glacier_check)

        self.glacier_factor_spin = QDoubleSpinBox()
        self.glacier_factor_spin.setRange(0.0, 1.0)
        self.glacier_factor_spin.setDecimals(2)
        self.glacier_factor_spin.setSingleStep(0.05)
        self.glacier_factor_spin.setValue(0.30)
        self.glacier_factor_spin.setToolTip(
            "Multiplier applied inside the outlines. 1.0 = no effect, 0 = erase.")
        form.addRow("Weight inside ice", self.glacier_factor_spin)

        self.glacier_path_edit = QLineEdit()
        self.glacier_path_edit.setPlaceholderText(
            "glacier outlines (shapefile / GeoPackage / /vsizip/…)")
        self._glacier_browse = QPushButton("…")
        self._glacier_browse.setMaximumWidth(32)
        self._glacier_browse.clicked.connect(self._browse_glacier)
        form.addRow("Outlines", self._row(self.glacier_path_edit,
                                          self._glacier_browse))
        self.rgi_btn = QPushButton("Download RGI 7.0 glacier complexes (Alaska)")
        self.rgi_btn.setToolTip(
            "Fetches RGI 7.0 region 01 (Alaska), the 'C' glacier-complex product, "
            "from NSIDC and caches it — about 40-80 MB, once.\n\nNSIDC requires a "
            "NASA Earthdata Login: fill in the 'NASA Earthdata Login' box in the "
            "Environment header first (it writes ~/.netrc, which this reads).\n\n"
            "Leave the Outlines field empty and this cached copy is used "
            "automatically.")
        self.rgi_btn.clicked.connect(self._download_rgi)
        form.addRow(self.rgi_btn)

        hint = QLabel(
            "Outlines are a snapshot: RGI 7.0 region 01 derives from 1999-2010 "
            "imagery, so Alaska termini have retreated well inside the mask and it "
            "over-covers at the snout — the edge is feathered for that reason. "
            "CC BY 4.0; cite the RGI 7.0 Consortium (2023), doi:10.5067/f6jmovy5navz.")
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(hint)
        return box

    def _cloud_box(self):
        box = QgsCollapsibleGroupBox("Cloud masking")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        self.cloud_check = QCheckBox("Mask cloud from the scene classification band")
        self.cloud_check.setChecked(True)
        self.cloud_check.setToolTip(
            "Nothing upstream masks cloud per pixel. A cloud in the post scene is "
            "a large positive dBright and its SHADOW is a large negative dBright — "
            "the same sign and size as fresh debris on snow.\n\nThis fetches "
            "Sentinel-2 SCL (or Landsat qa_pixel) for the scenes the Run used, "
            "read from the <event>_metadata.json beside the optical raster.")
        form.addRow(self.cloud_check)

        self.cloud_frac_spin = QDoubleSpinBox()
        self.cloud_frac_spin.setRange(0.05, 1.0)   # 0.0 would mask every pixel
        self.cloud_frac_spin.setDecimals(2)
        self.cloud_frac_spin.setSingleStep(0.1)
        self.cloud_frac_spin.setValue(0.5)
        self.cloud_frac_spin.setToolTip(
            "A pixel is masked when at least this fraction of a side's scenes flag "
            "it. The Run takes a per-pixel MEDIAN, so one cloudy scene out of five "
            "does not corrupt the composite but three out of five does.")
        form.addRow("Contaminated when ≥", self.cloud_frac_spin)

        self.cloud_dark_check = QCheckBox(
            "Also mask SCL 'dark area' (class 2)")
        self.cloud_dark_check.setToolTip(
            "Off by default: in mountains that class is mostly TERRAIN shadow, and "
            "masking it removes a large share of every steep AOI.")
        form.addRow(self.cloud_dark_check)
        return box

    def _outputs_box(self):
        box = QgsCollapsibleGroupBox("Outputs")
        box.setSaveCollapsedState(False)
        form = QFormLayout(box)
        self.out_fused_check = QCheckBox("Fused score  (optical + SAR)")
        self.out_fused_check.setChecked(True)
        self.out_fused_check.setToolTip(
            "The soft AND across both sensors. Worth having when SAR corroborates "
            "the optical evidence — Iliamna 2026-08-08 gained precision 12% → 24% "
            "from it.\n\nUntick it when SAR is not helping and you do not want a "
            "SAR raster involved at all: the SAR input then becomes optional.")
        form.addRow(self.out_fused_check)

        self.out_optical_check = QCheckBox("Optical only  (no SAR)")
        self.out_optical_check.setChecked(True)
        self.out_optical_check.setToolTip(
            "The optical channel with the same terrain and glacier weighting but "
            "no AND.\n\nOn Hubbard 2026-07-28 the deposit is QUIETER in SAR than "
            "the crevassed ice around it, so the AND destroys the detection and "
            "this is the only usable layer: fused AUC 0.663 vs optical-only 0.889.")
        form.addRow(self.out_optical_check)

        note = QLabel(
            "Both bands are always written to the GeoTIFF; these choose which "
            "become map layers. Whether SAR helps is event-specific, so keeping "
            "both is the safe default.")
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(note)
        return box

    @staticmethod
    def _tag(text, colour=None, bold=True):
        """A form label with an optional colour bar.

        Rich text on a QLabel rather than a stylesheet on the QComboBox: styling
        a combo partially makes Qt drop its native rendering, including the
        drop-down arrow, so the colour goes beside the control instead of on it."""
        bar = f'<span style="color:{colour};">&#9612;</span> ' if colour else ""
        weight = "font-weight:bold;" if bold else "color:palette(mid);"
        lbl = QLabel(f'{bar}<span style="{weight}">{text}</span>')
        lbl.setTextFormat(Qt.RichText)
        return lbl

    @staticmethod
    def _row(*widgets):
        """Box a couple of widgets into one QFormLayout row."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for i, x in enumerate(widgets):
            h.addWidget(x, 1 if i == 0 else 0)
        return w

    # ---------- layer discovery ----------
    def _sar_change_dir(self):
        """<output>/sar/change — where the SAR tab keeps its float32 exports."""
        base = self._out_dir()
        return os.path.join(os.path.dirname(base), "sar", "change") if base else ""

    def _durable_twin(self, layer_name):
        """The float32 export the SAR tab wrote for this layer, if it exists.

        When the layover fade fires, the layer QGIS shows is a baked RGBA whose
        dB values are gone, but _cd_export_float has already written the real
        float32 raster under the layer's own sanitised name (plus a settings
        tag). Rather than telling the user to go hunting for it, find it."""
        import glob
        d = self._sar_change_dir()
        if not d or not os.path.isdir(d):
            return None
        safe = layer_name.replace("\u2192", "_to_").replace("\u00d7", "x")
        safe = re.sub(r"[^\w.+-]+", "_", safe).strip("_")
        if not safe:
            return None
        hits = sorted(glob.glob(os.path.join(d, safe + "*.tif")))
        return hits[-1] if hits else None

    def _survey_layers(self):
        """Every raster layer in the project as (name, path_or_None, reason).

        A usable layer carries its path and no reason; an unusable one carries
        None and the reason it cannot be fused. Unusable layers are NOT dropped:
        a layer that silently fails to appear in a dropdown is unexplainable from
        the UI, and the user is left assuming the tab is broken."""
        out = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsRasterLayer):
                continue
            name = lyr.name()
            if not lyr.isValid():
                out.append((name, None, "layer is not valid in QGIS"))
                continue
            # basemaps and web services are rasters to QGIS but carry a URI, not
            # a path; reporting "file no longer on disk: crs=EPSG:3857&format=..."
            # for every Google/Esri layer buries the messages that matter
            provider = (lyr.providerType() or "").lower()
            if provider != "gdal":
                out.append((name, None,
                            f"{provider.upper() or 'non-file'} web layer, not a "
                            "data raster"))
                continue
            src = (lyr.source() or "").split("|")[0]
            if not src:
                out.append((name, None, "not a file-based raster"))
                continue
            if not os.path.exists(src):
                out.append((name, None,
                            f"the file is no longer on disk: {src}"))
                continue
            ok, why = fusion_grid.describe_raster(src)
            if ok:
                out.append((name, src, None))
                continue
            twin = self._durable_twin(name)
            if twin:
                ok2, _why2 = fusion_grid.describe_raster(twin)
                if ok2:
                    out.append((f"{name}  [float32 copy]", twin, None))
                    continue
            if "colour image" in why:
                why += (". The SAR tab saves a float32 copy in <output>/sar/change "
                        "— none found for this layer, so either re-run change "
                        "detection (it writes one now) or untick 'fade layover "
                        "slopes' and run again")
            out.append((name, None, why))
        return out

    def _queue_rescan(self, *_args):
        """Coalesce a burst of layersAdded/Removed into one rescan."""
        try:
            self._rescan.start()
        except RuntimeError:            # tab torn down mid-signal
            pass

    def _refresh_layers(self, quiet=False):
        """Repopulate both combos from the project's raster layers.

        Usable layers come first and are preselected by name. Unusable ones are
        still LISTED, greyed out and annotated with the reason, so a missing
        layer is a visible explanation rather than a silent absence. Rasters
        explicitly browsed to are preserved across a refresh — clearing the combo
        would otherwise drop the user's own choice and let _best_match quietly
        substitute a different layer."""
        self._refreshing = True
        survey = self._survey_layers()
        rasters = sorted([(n, p) for n, p, r in survey if p],
                         key=lambda t: t[0].lower())
        # One row per FILE, not per layer. Adding a package to the project twice
        # gives two QgsRasterLayers over one raster, and they render as two rows
        # identical in every visible respect — same name, same footprint, same
        # folder — which reads as "there are two different rasters here" and
        # sends you looking for a difference that does not exist.
        unique, seen_paths = [], set()
        for nm, src in rasters:
            if src not in seen_paths:
                seen_paths.add(src)
                unique.append((nm, src))
        rasters = unique
        rejected = sorted([(n, r) for n, p, r in survey if not p],
                          key=lambda t: t[0].lower())

        # Optical first: it is the ANCHOR. Every SAR candidate is measured
        # against the optical footprint, so the SAR list cannot be built until
        # the optical choice has settled.
        self._fill_combo("optical", rasters, rejected, OPTICAL_HINTS,
                         OPTICAL_EXCLUDE)
        opt_path = self._optical_combo.currentData()
        anchor = self._bbox4326(opt_path) if opt_path else None
        lost = self._fill_combo("sar", rasters, rejected, SAR_HINTS,
                                SAR_EXCLUDE, anchor=anchor)
        if lost and self._sar_user_choice:
            # the hand-picked SAR raster does not cover the optical raster now
            # selected; hand the pairing back to geography rather than leave a
            # dead choice sitting in the box looking authoritative
            self._sar_user_choice = False
            if not quiet:
                self._warn(
                    "The SAR raster you had picked does not cover the same "
                    "ground as the optical raster now selected, so it was "
                    "released. It is still listed, greyed out, with how far off "
                    "it is.")

        # Pair the SAR input to the OPTICAL one by geography, not by name order.
        # Alphabetical order pairs whichever event happens to spell earliest: with
        # three events loaded it put Hubbard optical against Iliamna SAR, 0%
        # overlap. Only re-pick when the user has not chosen the SAR layer
        # themselves.
        self._pair_overlap = None
        if opt_path and not self._sar_user_choice:
            idx, ov = self._best_footprint_match(self._sar_combo, SAR_HINTS,
                                                 anchor, SAR_EXCLUDE)
            self._sar_combo.blockSignals(True)
            if idx > 0 and ov >= FOOTPRINT_MATCH_MIN:
                self._sar_combo.setCurrentIndex(idx)
            else:
                self._sar_combo.setCurrentIndex(0)   # no honest pair — ask
            self._sar_combo.blockSignals(False)
            self._autodetect_measure("sar")
            if idx > 0:
                self._pair_overlap = ov
            else:
                # The search above only sees rows the filter left selectable, so
                # when it finds nothing it returns 0.0 — and reporting "best 0%"
                # for a raster that missed by a whisker would send the user
                # looking for the wrong problem. Score every SAR-looking raster,
                # greyed or not, and report the real near-miss.
                near = [self._footprint_match(anchor, self._bbox4326(src))
                        for nm, src in rasters
                        if any(h in nm.lower() for h in SAR_HINTS)
                        and not any(x in nm.lower() for x in SAR_EXCLUDE)]
                self._pair_overlap = max(near) if near else None
                if near and not quiet:
                    self._warn(
                        "No SAR change raster covers the same ground as the "
                        f"selected optical raster (a pair needs a "
                        f"{FOOTPRINT_MATCH_MIN:.0%} footprint match; the "
                        f"closest of {len(near)} manages {max(near):.0%}). "
                        "Every one is listed greyed out with how far off it is "
                        "— the usual cause is an optical raster left over from "
                        "a run at a different radius. Run SAR change detection "
                        "over the same AOI, or use the … button to force a file.")
        elif opt_path:
            # user-chosen SAR: still report the match, so the step panel says
            # what this pair actually is instead of going silent
            sar_path = self._sar_combo.currentData()
            if sar_path:
                self._pair_overlap = self._footprint_match(
                    anchor, self._bbox4326(sar_path))

        # blockSignals above suppressed currentIndexChanged, so the measure
        # auto-detect never ran for a selection made BY the refresh — which is
        # every selection except a manual one. Run it explicitly.
        self._autodetect_measure("optical")
        self._autodetect_measure("sar")

        self._refreshing = False
        self._update_steps()
        if quiet:
            return
        self._append_log(f"{len(rasters)} usable raster layer(s), "
                         f"{len(rejected)} unusable.")
        for nm, why in rejected:
            self._append_log(f"  unusable — {nm}: {why}")
        if not rasters:
            self._warn("No usable raster layers found. Every raster in the "
                       "project is listed greyed-out in the dropdowns with the "
                       "reason; the log pane has the same list.")

    def _fill_combo(self, role, rasters, rejected, hints, exclude, anchor=None):
        """Repopulate one side's combo. Returns True if a selection was dropped.

        Every row is labelled with its ground footprint, because a layer NAME
        does not identify a raster here: re-running an event writes a second
        raster under the same event id, and two dbright layers whose names differ
        only in an end date are indistinguishable in a dropdown 160 px wide. The
        size is the thing that tells them apart, so the size is on every row.

        `anchor` is the optical footprint each SAR candidate is measured against.
        A raster covering different ground is LISTED, with how far off it is, and
        disabled — the tab's standing rule is that a layer which silently fails
        to appear is unexplainable from the UI, so nothing is ever dropped."""
        combo = self._optical_combo if role == "optical" else self._sar_combo
        keep = combo.currentData()
        known = {p for _n, p in rasters}
        # browsed-in files survive a rescan, but only while they still exist: a
        # stale entry would otherwise stay selected after its file was gone
        rows = list(rasters) + sorted(
            (nm, src) for src, nm in self._browsed[role].items()
            if src not in known and os.path.exists(src))
        seen = {}
        for nm, _src in rows:
            seen[nm] = seen.get(nm, 0) + 1
        dup = {nm for nm, c in seen.items() if c > 1}

        good, mismatched = [], []
        for nm, src in rows:
            why = self._footprint_reason(src, anchor)
            label = self._row_label(nm, src, dup)
            if why:
                mismatched.append((f"{label}  —  {why}", None))
            else:
                good.append((label, src))

        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— choose a layer —", None)
        for label, src in good:
            combo.addItem(label, src)
        first_bad = combo.count()
        for label, _none in mismatched:              # wrong ground for this pair
            combo.addItem(label, None)
        for nm, why in rejected:                     # not fusable at all
            combo.addItem(f"{nm}  —  {why}", None)
        model = combo.model()
        for i in range(first_bad, combo.count()):
            item = model.item(i) if hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(False)
        idx = combo.findData(keep) if keep else -1
        lost = bool(keep) and idx < 0
        if idx < 0:
            idx = self._best_match(combo, hints, exclude)
        if idx > 0 and not combo.itemData(idx):
            idx = 0                                  # never land on a greyed row
        combo.setCurrentIndex(max(0, idx))
        combo.blockSignals(False)
        return lost

    def _bbox4326(self, path):
        """Lon/lat bounding box of a raster, cached by (path, mtime)."""
        return self._geom(path).get("bbox")

    def _footprint_km(self, path):
        """(width, height) of a raster's ground footprint in km, or None."""
        return self._geom(path).get("km")

    def _geom(self, path):
        """Footprint of a raster — lon/lat bbox and ground size — by (path, mtime).

        Only the geotransform is read — no pixel access — so this stays cheap
        enough to run for every candidate layer on every refresh."""
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            return {}
        hit = self._bbox_cache.get(key)
        if hit is not None:
            return hit
        out = {}
        try:
            gt, shape, proj = fusion_grid.reference_grid(path)
            out["bbox"] = fusion_cloud.bbox_4326(gt, shape, proj)
            out["km"] = fusion_grid.extent_km(gt, shape)
        except Exception:                            # noqa: BLE001
            pass
        self._bbox_cache[key] = out
        return out

    def _row_label(self, name, path, dup_names):
        """A combo row that identifies its raster: name, footprint, and — only
        when another layer shares the name — the folder that separates them."""
        bits = []
        km = self._footprint_km(path)
        if km:
            bits.append(f"{km[0]:.0f}×{km[1]:.0f} km")
        if name in dup_names:
            folder = os.path.basename(os.path.dirname(path))
            if folder:
                bits.append(folder)
        return f"{name}  ·  {'  ·  '.join(bits)}" if bits else name

    @staticmethod
    def _footprint_match(a, b):
        """How far two lon/lat boxes agree about WHICH GROUND they cover, 0-1.

        min(intersection/area(a), intersection/area(b)) — symmetric, so a box
        swallowed whole by a much larger one scores LOW rather than perfectly.
        That asymmetry was the bug this replaces: intersection/area(optical)
        alone returns 1.00 for a 20 km optical tile sitting inside a 90 km SAR
        scene. A flawless score, for a pair that shares 5% of its ground — so the
        tab auto-paired them, said "100% footprint overlap" in the step panel,
        and fused a 90 km raster carrying optical evidence over a twentieth of
        itself."""
        if not a or not b:
            return 0.0
        return min(FusionTab._overlap_fraction(a, b),
                   FusionTab._overlap_fraction(b, a))

    def _footprint_reason(self, path, anchor):
        """Why this raster cannot pair with the anchor footprint, or None.

        None whenever the footprint is unknown or there is no anchor yet: this
        filter exists to stop an accident, and it must never block on ignorance."""
        if not anchor or not path:
            return None
        bb = self._bbox4326(path)
        if not bb:
            return None
        m = self._footprint_match(anchor, bb)
        if m >= FOOTPRINT_MATCH_MIN:
            return None
        # Kept short on purpose: the dock can be 360 px wide and a combo elides
        # from the right, so the number has to arrive before the explanation does
        if m <= 0.0:
            return "no overlap with the optical raster"
        return f"{m:.0%} of the optical footprint, needs {FOOTPRINT_MATCH_MIN:.0%}"

    @staticmethod
    def _overlap_fraction(a, b):
        """Area of intersection / area of `a`, for two lon/lat boxes."""
        if not a or not b:
            return 0.0
        ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        area = (a[2] - a[0]) * (a[3] - a[1])
        return (ox * oy / area) if area > 0 else 0.0

    def _best_footprint_match(self, combo, hints, anchor_bbox, exclude=()):
        """Index of the hint-matching entry whose footprint best matches `anchor_bbox`.

        Name order is NOT a safe way to pair the two inputs. Sorting is
        alphabetical, so which event lands first depends on how its dates happen
        to spell — with three events loaded the tab paired Hubbard optical with
        Iliamna SAR (0% overlap) purely because one layer name began with an
        earlier character. Geography is the only thing that actually says two
        rasters describe the same place, so pair on it.

        Rows the footprint filter greyed out carry no path and are skipped here
        too, so this only ever ranks candidates that already cover the same
        ground; the score it returns is what the step panel reports.

        Returns (index, match) with index -1 when nothing matches."""
        best, best_m = -1, 0.0
        for i in range(1, combo.count()):
            path = combo.itemData(i)
            if not path:
                continue
            text = combo.itemText(i).lower()
            if not any(h in text for h in hints):
                continue
            if any(x in text for x in exclude):
                continue
            m = self._footprint_match(anchor_bbox, self._bbox4326(path))
            if m > best_m:
                best, best_m = i, m
        return best, best_m

    @staticmethod
    def _best_match(combo, hints, exclude=()):
        """Index of the first SELECTABLE entry matching a hint, or -1.

        Entries with no data are the greyed-out unusable ones; matching a hint
        against those would preselect a layer the user cannot actually fuse (the
        SAR hint 'log-ratio' matches a dead temp-file layer perfectly)."""
        for hint in hints:
            for i in range(1, combo.count()):
                if not combo.itemData(i):
                    continue
                text = combo.itemText(i).lower()
                if hint in text and not any(x in text for x in exclude):
                    return i
        return -1

    def _browse_into(self, role, what):
        """Browse to a raster by hand — the escape hatch past every filter.

        A file chosen here is remembered for that side and re-offered on each
        rescan even when the footprint filter would have greyed it out: the
        filter is there to stop an accident, not to overrule a decision."""
        combo = self._optical_combo if role == "optical" else self._sar_combo
        start = self._out_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select the {what}", start if os.path.isdir(start) else "",
            "GeoTIFF (*.tif *.tiff);;All files (*)")
        if not path:
            return
        self._browsed[role][path] = os.path.basename(path)
        combo.addItem(os.path.basename(path), path)
        combo.setCurrentIndex(combo.count() - 1)

    def _browse_glacier(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select glacier outlines", "",
            "Vector (*.shp *.gpkg *.geojson *.json *.zip);;All files (*)")
        if not path:
            return
        if path.lower().endswith(".zip"):
            path = "/vsizip/" + path
        self.glacier_path_edit.setText(path)

    # layer-name fragment -> measure key, longest/most specific first
    MEASURE_HINTS = {
        "optical": (("dndsi", "dndsi"), ("ndsi", "dndsi"),
                    ("dbright", "dbright"), ("bright", "dbright")),
        "sar": (("mt int-corr", "mtcorr"), ("mt_int-corr", "mtcorr"),
                ("mtcorr", "mtcorr"), ("int-corr", "intcorr"),
                ("int_corr", "intcorr"), ("intcorr", "intcorr"),
                ("brightness z", "tsint"), ("brightness_z", "tsint"),
                ("tsint", "tsint"), ("log-ratio", "logratio"),
                ("logratio", "logratio"), ("log_ratio", "logratio")),
    }

    def _sar_chosen(self, _idx):
        """A SAR pick made through the UI wins over automatic re-pairing."""
        if not self._refreshing:
            self._sar_user_choice = True
        self._autodetect_measure("sar")

    def _optical_chosen(self, _idx):
        """A new optical raster re-anchors everything downstream.

        The SAR list is filtered and paired against the OPTICAL footprint, so
        leaving it alone when the anchor moves is what let a 45 km-radius dBright
        sit next to a SAR tile from an earlier, much smaller run — the selection
        changed and nothing else did. Re-running the scan is cheap: the
        footprints are cached by (path, mtime)."""
        self._autodetect_measure("optical")
        if not self._refreshing:
            self._refresh_layers()

    def _autodetect_measure(self, side):
        """Set the measure combo from the chosen layer's name.

        The layer name already states which product it is, so asking the user to
        restate it is just an opportunity to get the SIGN wrong. Detected values
        are logged, and the combo stays editable for the cases the name does not
        cover (a renamed layer, a hand-made raster)."""
        combo, kind_combo = ((self._optical_combo, self.optical_kind_combo)
                             if side == "optical"
                             else (self._sar_combo, self.sar_kind_combo))
        path = combo.currentData()
        if not path:
            return
        text = (combo.currentText() + " " + os.path.basename(path)).lower()
        for frag, key in self.MEASURE_HINTS[side]:
            if frag in text:
                idx = kind_combo.findData(key)
                if idx >= 0 and idx != kind_combo.currentIndex():
                    kind_combo.setCurrentIndex(idx)
                    self._append_log(
                        f"  detected {side} measure: {kind_combo.currentText()}")
                return

    def _kind_changed(self):
        """Move a floor to its measure's default only when it still sits on the
        PREVIOUS measure's default — never clobber a hand-tuned value.

        The previous kind is tracked explicitly. Testing "does the value equal
        ANY known default" instead would reset a floor the user had deliberately
        set to the other measure's number, and would fire on the wrong combo:
        this slot is connected to both, so changing the SAR measure must not
        touch the optical floor."""
        for name, combo, spin, table in (
                ("optical", self.optical_kind_combo, self.opt_floor_spin, OPTICAL_KINDS),
                ("sar", self.sar_kind_combo, self.sar_floor_spin, SAR_KINDS)):
            key = combo.currentData()
            prev = self._prev_kind.get(name)
            self._prev_kind[name] = key
            if prev is None or prev == key:
                continue
            defaults = {k: f for _l, k, f in table}
            if prev in defaults and abs(spin.value() - defaults[prev]) < 1e-9:
                spin.setValue(defaults.get(key, spin.value()))

    # ---------- paths ----------
    def _out_dir(self):
        """Output folder, mirroring the other tabs:
        <output or project/out/interactive>/fusion.

        Returns "" when neither Environment path is set. Both default to empty,
        and os.path.join("", "out", "interactive") is the RELATIVE string
        "out/interactive" — which would scatter output under whatever directory
        QGIS happens to have been launched from."""
        out = self.dock.out_edit.text().strip()
        proj = self.dock.project_edit.text().strip()
        if not out and not proj:
            return ""
        base = out or os.path.join(proj, "out", "interactive")
        return os.path.join(base, "fusion")

    def _glacier_cache_dir(self):
        """Where the downloaded RGI archive is kept: <output>/glacier_outlines."""
        base = self._out_dir()
        return os.path.join(os.path.dirname(base), "glacier_outlines") if base else ""

    def _download_rgi(self):
        """Fetch and cache the RGI 7.0 Alaska complexes, then fill in the path."""
        cache = self._glacier_cache_dir()
        if not cache:
            self._warn("Set the Output (or Project) folder in the Environment box "
                       "first — that is where the outlines are cached.")
            return
        self.rgi_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            path = fusion_glacier.ensure_rgi_c01(cache, log=self._step)
            self.glacier_path_edit.setText(path)
            # deliberately NOT auto-enabled: downweighting cost Hubbard 1.0% ->
            # 11.0% background because the deposit sits inside the outline
            self._step("  outlines ready — tick 'Downweight pixels inside glacier "
                       "outlines' to use them")
            self._append_log(f"  {fusion_glacier.RGI_VINTAGE_NOTE}")
        except PermissionError as e:
            self._append_log(f"  {e}")
            self._warn(str(e))
        except Exception as e:                       # noqa: BLE001
            self._append_log(f"  RGI download failed: {type(e).__name__}: {e}")
            self._warn(f"Could not download the RGI outlines: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            self.rgi_btn.setEnabled(True)

    # ---------- run ----------
    def _run(self):
        want_fused = self.out_fused_check.isChecked()
        want_optical = self.out_optical_check.isChecked()
        if not (want_fused or want_optical):
            self._warn("Tick at least one output in the Outputs box — there is "
                       "nothing to produce otherwise.")
            return

        opt_path = self._optical_combo.currentData()
        sar_path = self._sar_combo.currentData()
        if not opt_path:
            self._warn("Pick an optical change raster first.")
            return
        if not sar_path:
            if want_fused:
                self._warn("Pick a SAR change raster, or untick 'Fused score' in "
                           "the Outputs box to run optical-only.")
                return
            sar_path = None            # optical-only run; SAR is not needed
        for p in [opt_path] + ([sar_path] if sar_path else []):
            if not os.path.exists(p):
                self._warn(f"Missing file: {p}")
                return
        if sar_path and os.path.abspath(opt_path) == os.path.abspath(sar_path):
            self._warn("Both inputs point at the same raster. Fusing a layer "
                       "with itself just squares its own rank — pick the optical "
                       "change raster for one and the SAR change raster for the "
                       "other.")
            return
        for label, p in ([("Optical", opt_path)]
                         + ([("SAR", sar_path)] if sar_path else [])):
            ok, why = fusion_grid.describe_raster(p)
            if not ok:
                self._warn(f"{label} input is not usable: {why}")
                return
        if not self._out_dir():
            self._warn("Set the Output (or Project) folder in the Environment "
                       "box first — otherwise the fused raster would be written "
                       "relative to whatever folder QGIS was started from.")
            return
        self.run_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._fuse(opt_path, sar_path)
        except Exception as e:                       # noqa: BLE001
            self._append_log(f"FAILED: {type(e).__name__}: {e}")
            self._warn(f"Fusion failed: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            # not setEnabled(True): the button's state is derived from whether the
            # inputs are actually runnable, so ask that rather than assert it
            self._update_steps()

    def _step(self, msg):
        self._append_log(msg)
        QApplication.processEvents()     # the compute is synchronous; keep the
                                         # log readable as it runs

    def _fuse(self, opt_path, sar_path):
        import numpy as np

        self.log.clear()
        self._step("Fusing…")

        ev_o, ev_s = self._event_id(opt_path), self._event_id(sar_path)
        if ev_o:
            self._step(f"  EVENT: {ev_o}")

        # 1. reference grid = the COARSER input; a fused score is only as good as
        # its worst input, and upsampling SAR would manufacture detail from speckle
        inputs = [opt_path] + ([sar_path] if sar_path else [])
        ref_path, gt, shape, proj, ginfo = fusion_grid.pick_reference(inputs)
        for g in ginfo:
            self._step(f"  {os.path.basename(g['path'])}: {g['shape'][1]}×"
                       f"{g['shape'][0]} px, ~{g['ground_m']:.1f} m ground"
                       + ("  ← sets the resolution" if g["path"] == ref_path
                          else ""))

        # 1b. …but NOT the extent. The reference is whichever input is coarser,
        # and letting its footprint through as the output footprint is how a
        # 20 km optical tile against a 90 km SAR scene produced a 90 km raster
        # that was blind over 95% of itself. Reach and detail are separate
        # questions; the honest reach is the ground both inputs describe.
        gt, shape, xinfo = fusion_grid.crop_to_common(gt, shape, proj, inputs)
        w_km, h_km = fusion_grid.extent_km(gt, shape)
        if xinfo["cropped"]:
            self._step(f"  extent: cropped to the ground BOTH inputs cover — "
                       f"{shape[1]}×{shape[0]} px, {w_km:.1f}×{h_km:.1f} km "
                       f"({xinfo['kept']:.0%} of the reference raster)")
        else:
            self._step(f"  extent: {w_km:.1f}×{h_km:.1f} km — both inputs cover "
                       f"all of it")

        # 2. warp both onto it. 'average' is a genuine aggregation when going
        # fine→coarse; nearest/bilinear would throw away most of the measurements
        opt_raw = fusion_grid.warp_to_reference(opt_path, gt, shape, proj,
                                                resample="average")
        if sar_path:
            sar_raw = fusion_grid.warp_to_reference(sar_path, gt, shape, proj,
                                                    resample="average")
        else:
            sar_raw = np.full(shape, np.nan, dtype=np.float32)

        # 3. sign: convert both to evidence-positive exactly once
        okind = self.optical_kind_combo.currentData()
        skind = self.sar_kind_combo.currentData()
        opt = fusion_core.orient_evidence(opt_raw, okind)
        if sar_path:
            sar = fusion_core.orient_evidence(sar_raw, skind)
            self._step(f"  oriented: {okind} (decrease ⇒ evidence), "
                       f"{skind} (increase ⇒ evidence)")
        else:
            sar = sar_raw
            self._step(f"  oriented: {okind} (decrease ⇒ evidence); "
                       "OPTICAL-ONLY run, no SAR input")

        # 4. cloud: a masked pixel must become MISSING, never 'no change'
        cloud = None
        if self.cloud_check.isChecked():
            meta, mpath = fusion_cloud.find_metadata(opt_path)
            if not meta:
                self._step("  cloud mask: no <event>_metadata.json beside the "
                           "optical raster — cannot tell which scenes to fetch")
                self._warn("No scene metadata beside the optical raster, so cloud "
                           "is NOT masked. Point at the file in its Run package "
                           "folder, or accept that clouds may score.")
            else:
                self._step(f"  cloud mask: reading {os.path.basename(mpath)}")
                cloud, note = fusion_cloud.cloud_mask_on_grid(
                    meta, gt, shape, proj,
                    frac_thresh=float(self.cloud_frac_spin.value()),
                    mask_dark=self.cloud_dark_check.isChecked(),
                    log=self._step)
                self._step("  " + note)
                if cloud is not None:
                    # A mask that removes nearly everything is far more likely to
                    # be a broken mask than a cloudy scene, and it is invisible in
                    # the output: the optical band just quietly turns to NaN and
                    # the score falls back to SAR. It must shout. (Mt Logan,
                    # 45 km: 99.5% masked, optical surviving on a 10 km square.)
                    masked = float(np.mean(cloud))
                    if masked > 0.80:
                        self._warn(
                            f"The cloud mask removed {masked:.0%} of the AOI, so "
                            "the optical channel is blank over almost all of it "
                            "and the score is effectively SAR-only. Check the "
                            "scene list in the <event>_metadata.json beside the "
                            "optical raster, or untick 'mask clouds' to see the "
                            "optical evidence unmasked.")
                    opt = np.where(cloud, np.nan, opt).astype(np.float32)

        # 5. detrend, then rank above an ABSOLUTE floor.
        # The floor is tested against the RAW evidence and the ranking is done on
        # the detrended evidence. Testing the floor on detrended values would
        # quietly redefine it as "deviates from the AOI median by more than the
        # floor" — a pixel with zero physical change could clear a 3 dB bar just
        # by sitting on the bright side of a basin-wide snowfall. A detection has
        # to be physically significant AND an outlier, not either alone.
        opt_admit, sar_admit = opt, sar
        if self.detrend_check.isChecked():
            opt, o_off = fusion_core.detrend_median(opt)
            sar, s_off = fusion_core.detrend_median(sar)
            self._step(f"  detrended for ranking: optical {o_off:+.4g}, "
                       f"SAR {s_off:+.4g} removed (floors still test raw values)")
        o_rank, o_info = fusion_core.robust_rank(
            opt, okind, floor=float(self.opt_floor_spin.value()),
            admit_values=opt_admit)

        # second optical channel from the same Run, when available
        n_optical = 1
        if self.pair_optical_check.isChecked():
            sib, sib_kind = self._optical_sibling(opt_path, okind)
            if sib:
                try:
                    sib_raw = fusion_grid.warp_to_reference(
                        sib, gt, shape, proj, resample="average")
                    sib_ev = fusion_core.orient_evidence(sib_raw, sib_kind)
                    if cloud is not None:
                        sib_ev = np.where(cloud, np.nan, sib_ev).astype(np.float32)
                    sib_admit = sib_ev
                    if self.detrend_check.isChecked():
                        sib_ev, _off = fusion_core.detrend_median(sib_ev)
                    sib_rank, sib_info = fusion_core.robust_rank(
                        sib_ev, sib_kind,
                        floor=fusion_core.DEFAULT_FLOORS.get(sib_kind),
                        admit_values=sib_admit)
                    o_rank = fusion_core.combine_optical(o_rank, sib_rank)
                    n_optical = 2
                    self._step(f"  paired optical: + {os.path.basename(sib)}")
                    self._step(f"  {sib_kind}: {sib_info['n_admitted']} of "
                               f"{sib_info['n_valid']} px cleared the "
                               f"{sib_info['floor']:g} floor")
                except Exception as e:               # noqa: BLE001
                    self._step(f"  paired optical skipped: {type(e).__name__}: {e}")
            else:
                self._step("  paired optical: no matching sibling raster found")
        s_rank, s_info = fusion_core.robust_rank(
            sar, skind, floor=float(self.sar_floor_spin.value()),
            admit_values=sar_admit)

        # pool the other detectors the SAR tab wrote for this same pair
        if sar_path and self.pair_sar_check.isChecked():
            sibs = self._sar_siblings(sar_path)
            if not sibs:
                self._step("  SAR pooling: no other detectors found for this pair")
            for spath, skind2 in sibs:
                try:
                    raw2 = fusion_grid.warp_to_reference(
                        spath, gt, shape, proj, resample="average")
                    ev2 = fusion_core.orient_evidence(raw2, skind2)
                    admit2 = ev2
                    if self.detrend_check.isChecked():
                        ev2, _o = fusion_core.detrend_median(ev2)
                    r2, i2 = fusion_core.robust_rank(
                        ev2, skind2,
                        floor=fusion_core.DEFAULT_FLOORS.get(skind2),
                        admit_values=admit2)
                    s_rank = fusion_core.combine_sar(s_rank, r2)
                    self._step(f"  + SAR {skind2}: {i2['n_admitted']} of "
                               f"{i2['n_valid']} px cleared the "
                               f"{i2['floor']:g} floor")
                except Exception as e:                # noqa: BLE001
                    self._step(f"  SAR {skind2} skipped: {type(e).__name__}: {e}")

        # 6. terrain weight from the focal-MAX slope
        slope_w = None
        if self.terrain_check.isChecked():
            dem = self._dem_on_grid(gt, shape, proj)
            if dem is None:
                self._step("  terrain: no DEM available — weighting skipped")
                self._warn("No DEM could be fetched, so terrain weighting was "
                           "skipped. Glacier and flat-ground signal will NOT be "
                           "suppressed.")
            else:
                slope_w, slope, tmeta = fusion_core.slope_weight(
                    dem, gt, float(self.radius_spin.value()),
                    lo_deg=float(self.slope_lo_spin.value()),
                    hi_deg=float(self.slope_hi_spin.value()))
                self._step(f"  terrain: focal-max slope over "
                           f"{tmeta['k_row']}×{tmeta['k_col']} px "
                           f"(~{tmeta['radius_m']:.0f} m), ramp "
                           f"{tmeta['lo_deg']:.0f}–{tmeta['hi_deg']:.0f}°")
                if self.lowland_check.isChecked():
                    lw = fusion_core.lowland_weight(
                        dem, float(self.min_elev_spin.value()))
                    slope_w = (slope_w * lw).astype(slope_w.dtype)
                    vetoed = float((lw < 0.5).mean()) * 100.0
                    self._step(f"  terrain: low-lying veto below "
                               f"{self.min_elev_spin.value():.0f} m removed "
                               f"{vetoed:.1f}% of the AOI (water / tidal flats)")
                if tmeta["n_void"]:
                    self._step(f"  terrain: {tmeta['void_pct']:.1f}% of the AOI "
                               "has no DEM — those pixels get NO terrain "
                               "weighting (treated as weight 1, not suppressed)")
                    if tmeta["void_pct"] > 20.0:
                        self._warn(
                            f"{tmeta['void_pct']:.0f}% of the AOI has no DEM "
                            "coverage, so terrain weighting is inert over most "
                            "of it and flat-ground signal will not be suppressed "
                            "there.")

        # 7. glacier weight
        glacier_w = None
        gpath = self.glacier_path_edit.text().strip()
        if self.glacier_check.isChecked():
            if not gpath:
                # fall back to the cached RGI archive, fetching it once if needed
                cache = self._glacier_cache_dir()
                cached = (os.path.join(cache, fusion_glacier.RGI_C01_NAME)
                          if cache else "")
                if cached and os.path.exists(cached):
                    gpath = cached
                    self._step(f"  glacier: using cached {os.path.basename(cached)}")
                else:
                    try:
                        self._step("  glacier: no outlines set — fetching RGI 7.0 "
                                   "Alaska (once, ~40-80 MB)…")
                        gpath = fusion_glacier.ensure_rgi_c01(cache, log=self._step)
                        self.glacier_path_edit.setText(gpath)
                    except Exception as e:           # noqa: BLE001
                        self._step(f"  glacier: {type(e).__name__}: {e}")
                        self._warn(f"Glacier downweighting was skipped: {e}")
                        gpath = ""
            if gpath:
                try:
                    gmask, gmeta = fusion_glacier.rasterize_outlines(
                        gpath, gt, shape, proj)
                    glacier_w = fusion_core.glacier_weight(
                        gmask, float(self.glacier_factor_spin.value()))
                    self._step(f"  glacier: {gmeta['note']}")
                except Exception as e:               # noqa: BLE001
                    self._step(f"  glacier: {type(e).__name__}: {e}")
                    self._warn(f"Glacier outlines could not be used: {e}")

        # 8. fuse
        bands, meta = fusion_core.fuse(
            o_rank, s_rank, slope_w=slope_w, glacier_w=glacier_w,
            mode=self.mode_combo.currentData(),
            allow_sar_only=self.saronly_check.isChecked(),
            sar_only_weight=float(self.saronly_cap_spin.value()),
            smooth_k=int(self.smooth_combo.currentData() or 1),
            optical_n=n_optical)
        # Area sieve: a slide is a connected patch, a speck is noise. Applied to
        # the final score only — the ingredient bands stay untouched so you can
        # still see WHY something scored before it was cleared.
        min_km2 = float(self.min_area_spin.value())
        cand_min_px = 1
        if min_km2 > 0:
            dxm, dym = layover_dim.metric_pixel_size(gt, shape[0])
            min_px = max(1, int(round(min_km2 * 1e6 / (dxm * dym))))
            cand_min_px = min_px
            sig = np.isfinite(bands["score"]) & (bands["score"] >= SIEVE_THRESHOLD)
            before = int(sig.sum())
            bands["score"] = sar_change.sieve_small_blobs(
                bands["score"], sig, min_px, fill=0.0)
            after = int((np.isfinite(bands["score"])
                         & (bands["score"] >= SIEVE_THRESHOLD)).sum())
            self._step(f"  area sieve ≥{min_km2:g} km² ({min_px} px): cleared "
                       f"{before - after} of {before} scoring pixel(s) in blobs "
                       "too small to be a slide")

        # shortlist the surviving blobs: the scar is rarely the brightest PIXEL
        # but is almost always one of the strongest BLOBS (see _candidates_box)
        self._fill_candidates(bands["score"], gt, shape, proj, cand_min_px)

        for line in fusion_core.summarize(bands, meta, [o_info, s_info]):
            self._step(line)

        # The AND across two sensors IS the method. If the two rasters barely
        # overlap, almost every pixel falls back to single-sensor evidence and
        # the map shows SAR noise rather than corroborated detections — which is
        # invisible unless you happen to turn on the coverage layer.
        n_cov = meta["n_both"] + meta["n_sar_only"] + meta.get("n_optical_only", 0)
        pct_both = (100.0 * meta["n_both"] / n_cov) if n_cov else 0.0
        if sar_path:
            self._step(f"  CORROBORATED COVERAGE: {pct_both:.1f}% of measured "
                       "pixels saw BOTH sensors")
        if sar_path and pct_both < 50.0:
            self._warn(
                f"Only {pct_both:.0f}% of the AOI was seen by both sensors, so most "
                "of this map is single-sensor SAR and the cross-check the method "
                "depends on never happened there. Check the '— single-sensor "
                "coverage' layer: the optical raster probably does not cover the "
                "whole AOI.")

        bracket = self._report_dates(opt_path, sar_path) or (None, None)
        self._check_in_view(gt, shape, proj, ev_o)

        # 9. write + load
        name = self._out_name(opt_path, sar_path, okind, skind)
        d = self._out_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name + ".tif")
        fusion_grid.write_multiband(path, bands, list(fusion_core.BAND_NAMES),
                                    gt, proj)
        self._step(f"  saved: {path}")
        self._load(path, name, bands, meta, bracket,
                   self._grid_radius_km(gt, shape),
                   want_fused=self.out_fused_check.isChecked(),
                   want_optical=self.out_optical_check.isChecked())

    def _dem_on_grid(self, gt, shape, proj):
        """Copernicus GLO-30 on the fusion grid, whatever CRS that grid is in.

        layover_dim.fetch_dem_on_grid warps to EPSG:4326, which is right when the
        fusion grid is the SAR product (it always is, in lon/lat) but wrong if a
        30 m Landsat raster turned out to be the coarser input and the grid is
        UTM. So the DEM is always fetched in lon/lat and then warped onto the
        reference grid, which collapses both cases into one path."""
        import numpy as np
        from osgeo import gdal

        key = (tuple(round(float(v), 6) for v in gt), tuple(shape))
        if key in self._dem_cache:
            return self._dem_cache[key]
        h, w = shape
        lon0, lat0, lon1, lat1 = fusion_cloud.bbox_4326(gt, shape, proj)
        self._step("  terrain: fetching Copernicus GLO-30 …")
        try:
            arr = layover_dim.fetch_dem_on_grid(lon0, lat0, lon1, lat1, w, h)
        except Exception as e:                       # noqa: BLE001
            self._step(f"  terrain: DEM fetch failed: {type(e).__name__}: {e}")
            arr = None
        if arr is None:
            self._dem_cache[key] = None
            return None

        # A geographic reference grid IS the grid we just fetched on — same bbox,
        # same width/height, and fetch_dem_on_grid warps to EPSG:4326 — so the
        # array already lines up and a second warp would only add interpolation.
        if abs(gt[1]) < 0.5:                         # degrees, per metric_pixel_size
            dem = np.asarray(arr, dtype=np.float32)
            self._dem_cache[key] = dem
            return dem

        # Projected reference grid (a 30 m Landsat raster turned out to be the
        # coarser input): round-trip the lon/lat DEM through a /vsimem raster so
        # it can be warped onto the reference grid properly.
        vp = "/vsimem/_fusion_dem.tif"
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(vp, w, h, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((lon0, (lon1 - lon0) / float(w), 0.0,
                            lat1, 0.0, -(lat1 - lat0) / float(h)))
        ds.SetProjection(WGS84_WKT)
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(fusion_grid.NODATA)   # before the write; see
        b.WriteArray(np.asarray(arr, dtype=np.float32))  # fusion_grid._as_nodata_source
        b.FlushCache()
        ds = None
        try:
            dem = fusion_grid.warp_to_reference(vp, gt, shape, proj,
                                                resample="bilinear")
        finally:
            try:
                gdal.Unlink(vp)
            except Exception:                        # noqa: BLE001
                pass
        self._dem_cache[key] = dem
        return dem

    # ---------- reporting ----------
    def _report_dates(self, opt_path, sar_path):
        """Print all four acquisition dates and the interval both sensors bracket.

        Returns that bracket as (pre, post) ISO strings for the layer-group name,
        or (None, None) when it cannot be determined.

        Never blocks: a deliberately wide SAR pair is a legitimate choice. But if
        the two intervals do not overlap, the two sensors are describing different
        time spans and any 'agreement' between them is coincidence."""
        meta, _ = fusion_cloud.find_metadata(opt_path)
        o_pre = o_post = None
        if meta:
            pre = [d[:10] for d in (meta.get("pre_dates") or []) if d]
            post = [d[:10] for d in (meta.get("post_dates") or []) if d]
            o_pre, o_post = (max(pre) if pre else None,
                             min(post) if post else None)
        if not (o_pre and o_post):
            # no metadata sidecar (a copied or hand-made raster): the Run puts the
            # acquisition dates in the filename too, so read them rather than
            # giving up on the bracket entirely
            od = ISO_DATE.findall(os.path.basename(opt_path))
            if len(od) >= 2:
                o_pre, o_post = od[0], od[-1]
        s_dates = ISO_DATE.findall(os.path.basename(sar_path)) if sar_path else []
        s_pre, s_post = (s_dates[0], s_dates[-1]) if len(s_dates) >= 2 else (None, None)
        if not sar_path:
            self._step(f"  optical window: {o_pre or '?'} → {o_post or '?'} "
                       "(optical-only run, no SAR window)")
            return o_pre, o_post
        self._step(f"  optical window: {o_pre or '?'} → {o_post or '?'}")
        self._step(f"  SAR window:     {s_pre or '?'} → {s_post or '?'}")
        if not (o_pre and o_post and s_pre and s_post):
            self._step("  effective bracket: not determinable from the file names")
            # still name the group after whichever pair we do know
            return (s_pre or o_pre), (s_post or o_post)
        lo, hi = max(o_pre, s_pre), min(o_post, s_post)
        if lo > hi:
            self._step(f"  NO OVERLAP: optical {o_pre}→{o_post} and SAR "
                       f"{s_pre}→{s_post} bracket different intervals")
            self._warn("The optical and SAR pairs do not bracket a common "
                       "interval — they describe different time spans, so any "
                       "agreement between them is coincidental.")
            return s_pre, s_post
        self._step(f"  effective event bracket: {lo} → {hi}")
        return lo, hi

    def _check_in_view(self, gt, shape, proj, event_id):
        """Warn when the fused area is nowhere near what the map is showing.

        The layer pickers deliberately keep your last choice across a refresh, so
        opening a second event's project and pressing Fuse can silently re-fuse
        the FIRST event's rasters — the run succeeds, the numbers look healthy,
        and the output lands hundreds of km away. Comparing the fusion grid with
        the canvas extent catches that in the one place it shows."""
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        try:
            from qgis.core import (QgsCoordinateReferenceSystem,
                                   QgsCoordinateTransform, QgsProject,
                                   QgsRectangle)
            ext = canvas.extent()
            if ext is None or ext.isEmpty():
                return
            src = canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem()
            dst.createFromWkt(proj) if proj else dst.createFromString("EPSG:4326")
            if not dst.isValid():
                return
            tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
            view = tr.transformBoundingBox(ext)
            minx, miny, maxx, maxy = fusion_grid.grid_bbox(gt, shape)
            grid = QgsRectangle(minx, miny, maxx, maxy)
            if not grid.intersects(view):
                self._warn(
                    "The rasters you are fusing"
                    + (f" ({event_id})" if event_id else "")
                    + " cover an area OUTSIDE the current map view. The layer "
                      "pickers keep your previous choice, so this is usually a "
                      "different event's rasters left selected — press Refresh "
                      "and check the two Inputs.")
                self._step("  WARNING: the fusion grid does not intersect the "
                           "current map view")
        except Exception:                            # noqa: BLE001
            return                                   # a guard must never break Run

    def _grid_radius_km(self, gt, shape):
        """Half the fusion grid's ground width in km — the AOI 'radius' the other
        tabs put in their group names (layer_group's
        '<source> <pre>/<post> [<radius>] <product>').

        Measured off the raster rather than read from a search field: this tab
        has no search of its own, and the honest radius is whatever the two
        inputs actually covered once warped onto a common grid."""
        dx_m, _dy = layover_dim.metric_pixel_size(gt, shape[0])
        return (dx_m * shape[1]) / 2000.0

    OPTICAL_SIBLING = {"dndsi": "dbright", "dbright": "dndsi"}

    # filename detector token -> measure key. Longest first: "MT_int-corr"
    # contains "int-corr".
    SAR_FILE_KIND = (("MT_int-corr", "mtcorr"), ("brightness_z", "tsint"),
                     ("int-corr", "intcorr"), ("log-ratio", "logratio"))

    def _sar_siblings(self, path):
        """Other detectors the SAR tab wrote for the SAME pair and settings.

        Names differ only in the detector token and its span:
          S1_change_log-ratio_<pre>_to_<post>_t36_VV_7x7_20m_lee5_rn_a8.tif
          S1_change_int-corr_3xpre_to_<post>_t36_VV_7x7_20m_lee5_rn_a8.tif
        so globbing on everything from '_t<track>_' onward plus the post date
        finds the set. Returns [(path, kind)] excluding `path` itself."""
        import glob
        base = os.path.basename(path)
        m = re.search(r"_to_(\d{4}-\d{2}-\d{2})(_t\d+_[A-Z]{2}_.*\.tif)$", base)
        if not m:
            return []
        post, suffix = m.group(1), m.group(2)
        # One entry PER KIND: the same detector can exist several times for one
        # pair (a 3xpre and a 5xpre MT int-corr, say). Taking both would pool a
        # detector with itself and make the result depend on what happens to be
        # left in the folder. Newest wins — it is the one that matches the run
        # you just did.
        best = {}
        for cand in sorted(glob.glob(os.path.join(os.path.dirname(path),
                                                  f"S1_change_*_to_{post}{suffix}"))):
            if os.path.abspath(cand) == os.path.abspath(path):
                continue
            cb = os.path.basename(cand)
            for token, kind in self.SAR_FILE_KIND:
                if f"S1_change_{token}_" in cb:
                    ok, _why = fusion_grid.describe_raster(cand)
                    if not ok:
                        break
                    prev = best.get(kind)
                    if prev is None or os.path.getmtime(cand) > os.path.getmtime(prev):
                        best[kind] = cand
                    break
        return [(v, k) for k, v in sorted(best.items())]

    def _optical_sibling(self, path, kind):
        """The Run's other optical change raster beside this one, or (None, None).

        review_package writes `<event>_dndsi_<dates>.tif` and
        `<event>_dbright_<dates>.tif` into the same package folder, so the twin
        is a filename substitution away."""
        other = self.OPTICAL_SIBLING.get(kind)
        if not other:
            return None, None
        base = os.path.basename(path)
        if f"_{kind}_" not in base:
            return None, None
        cand = os.path.join(os.path.dirname(path),
                            base.replace(f"_{kind}_", f"_{other}_", 1))
        if not os.path.exists(cand):
            return None, None
        ok, _why = fusion_grid.describe_raster(cand)
        return (cand, other) if ok else (None, None)

    def _event_date(self, path):
        """The event's calendar date, for the step panel.

        Read from the Run's <event>_metadata.json when it is beside the raster,
        because that is the date the raster was actually built for. The event id
        encodes it too (YYMMDD_HHMM) and is the fallback — deriving it from the
        raster either way means the panel cannot drift from the file the way
        reading the Sentinel-2 tab's live date field could."""
        meta, _p = fusion_cloud.find_metadata(path)
        if meta:
            dt = str(meta.get("datetime_utc") or "")[:10]
            if ISO_DATE.fullmatch(dt):
                return dt
        m = re.search(r"event_(\d{2})(\d{2})(\d{2})_", os.path.basename(path or ""))
        if m:
            yy, mm, dd = m.groups()
            return f"20{yy}-{mm}-{dd}"
        return ""

    @staticmethod
    def _event_id(path):
        """'event_260728_1526' from a Run product's filename, or ''."""
        m = EVENT_ID.search(os.path.basename(path or ""))
        return m.group(1) if m else ""

    def _out_name(self, opt_path, sar_path, okind, skind):
        """Output basename, carrying the EVENT id.

        Without it two events whose SAR spans happen to match write to the same
        file and the second silently overwrites the first — which is exactly what
        happened when a Hubbard session re-ran with Iliamna's rasters still
        selected and clobbered the Iliamna result."""
        src = sar_path or opt_path
        dates = ISO_DATE.findall(os.path.basename(src))
        span = f"{dates[0]}_to_{dates[-1]}" if len(dates) >= 2 else "fused"
        ev = self._event_id(opt_path)
        kinds = f"{okind}_{skind}" if sar_path else f"{okind}_opticalonly"
        return lg.name("fusion", ev, kinds, span).replace(" ", "_")

    def _load(self, path, name, bands, meta, bracket=(None, None),
              radius_km=0.0, want_fused=True, want_optical=True):
        names = list(fusion_core.BAND_NAMES)
        score_band = names.index("score") + 1
        # same convention as the Sentinel-2 / Landsat and SAR tabs:
        #   <source> <pre>/<post> [<radius>]   e.g. "Fusion 8-7/8-19 15km"
        # Mark only the non-default measure, so a group name stays short unless
        # it needs to say something. dBright is the default and is left
        # unmarked. e.g. "Fusion 8-7/8-19 10km (ndsi)"
        tag = "(ndsi)" if self.optical_kind_combo.currentData() == "dndsi" else ""
        group_name = lg.name("Fusion", lg.date_pair(*bracket),
                             lg.radius_tag(radius_km), tag)
        group = lg.new_group(group_name)
        self._append_log(f"  layer group: {group_name}")
        added = []
        if want_fused:
            lyr = QgsRasterLayer(path, name)
            if lyr.isValid():
                self._style_score(lyr, score_band)
                lg.add_to(lyr, group)
                added.append(lyr)
            else:
                self._warn("The fused raster failed to load.")

        # single-sensor pixels as their own layer: the confidence band says these
        # are SAR-only, and they must not read as corroborated detections
        if want_fused and (meta["n_sar_only"] or meta.get("n_optical_only")):
            conf_band = names.index("confidence") + 1
            c = QgsRasterLayer(path, f"{name} — single-sensor coverage")
            if c.isValid():
                self._style_single_sensor(c, conf_band)
                lg.add_to(c, group)
                added.append(c)
        # The optical channel WITHOUT the AND. Which of the two is better is
        # event-specific (see fusion_core.fuse) and not predictable, so both are
        # on by default; the Outputs box lets you drop either.
        if want_optical:
            oo = QgsRasterLayer(path, f"{name} — optical only (no AND)")
            if oo.isValid():
                self._style_score(oo, names.index("optical_only") + 1)
                lg.add_to(oo, group)
                added.append(oo)
        self._last_layers = added
        self._append_log(f"  {len(added)} layer(s) added; bands: "
                         + ", ".join(names))

    def _style_score(self, layer, band):
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Interpolated)
        items = []
        for value, hexcolor, alpha in SCORE_RAMP:
            c = QColor(hexcolor)
            c.setAlpha(alpha)
            items.append(QgsColorRampShader.ColorRampItem(value, c, f"{value:.2f}"))
        ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(ramp)
        r = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
        r.setClassificationMin(0.0)
        r.setClassificationMax(1.0)
        layer.setRenderer(r)
        layer.triggerRepaint()

    def _style_single_sensor(self, layer, band):
        """Shade ONLY the SAR-only pixels.

        A QGIS Discrete ramp treats each item's value as the class UPPER bound,
        and anything below the first item falls INTO that first class. With
        CONF_SAR_ONLY (1) as the first item, CONF_NONE (0) rendered as "SAR only"
        too — telling the analyst there was single-sensor evidence exactly where
        nothing was measured. Every class is therefore bounded explicitly,
        starting at CONF_NONE."""
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)

        def _c(hexcolor, alpha):
            c = QColor(hexcolor)
            c.setAlpha(alpha)
            return c

        ramp.setColorRampItemList([
            QgsColorRampShader.ColorRampItem(
                fusion_core.CONF_NONE, _c("#ffffff", 0), "not measured"),
            QgsColorRampShader.ColorRampItem(
                fusion_core.CONF_SAR_ONLY, _c("#3182bd", 90), "SAR only"),
            QgsColorRampShader.ColorRampItem(
                fusion_core.CONF_BOTH, _c("#ffffff", 0), "both sensors"),
            QgsColorRampShader.ColorRampItem(
                fusion_core.CONF_OPTICAL_ONLY, _c("#bdbdbd", 70),
                "optical only (not scored)"),
        ])
        shader.setRasterShaderFunction(ramp)
        r = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
        r.setClassificationMin(float(fusion_core.CONF_NONE))
        r.setClassificationMax(float(fusion_core.CONF_OPTICAL_ONLY))
        layer.setRenderer(r)
        layer.triggerRepaint()

    # ---------- plumbing ----------
    # log line -> colour. Ordered: the first match wins, so the loud categories
    # are tested before the quiet ones.
    LOG_RULES = (
        ("FAILED", CLR_BAD, True), ("WARNING", CLR_WARN, True),
        ("NO OVERLAP", CLR_BAD, True), ("NOTHING cleared", CLR_WARN, True),
        ("could not", CLR_WARN, False), ("unusable —", CLR_MUTED, False),
        ("skipped", CLR_MUTED, False),
        ("CORROBORATED COVERAGE", CLR_ACCENT, True),
        ("SENSOR", CLR_ACCENT, True), ("EVENT:", CLR_ACCENT, True),
        ("peak fused score", CLR_ACCENT, True),
        ("effective event bracket", CLR_ACCENT, False),
        ("saved:", CLR_OK, False), ("layer group:", CLR_OK, False),
        ("preset:", CLR_ACCENT, False),
        ("← fusion grid", CLR_ACCENT, False),
    )

    def _append_log(self, line):
        """Append one line, coloured by what it says.

        The log is the tab's diagnostic surface and it is long; a wall of
        identical grey makes the two lines that matter — a warning, or the
        corroborated-coverage figure — as invisible as the thirty that do not.

        Every line is wrapped in a span, even an uncoloured one: QTextEdit.append
        only parses a string as rich text when it contains markup, so a bare
        string would show its own HTML entities as literal text. `white-space:pre`
        keeps the leading indentation that gives the log its structure."""
        from qgis.PyQt.QtGui import QTextCursor
        text = str(line)
        colour, bold = None, False
        for needle, c, b in self.LOG_RULES:
            if needle in text:
                colour, bold = c, b
                break
        esc = (text.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        style = "white-space:pre;"
        if colour:
            style += f"color:{colour};"
        if bold:
            style += "font-weight:bold;"
        self.log.append(f'<span style="{style}">{esc}</span>')
        self.log.moveCursor(QTextCursor.End)

    def _warn(self, text):
        self.iface.messageBar().pushWarning("Fusion", text)
        self._append_log("WARNING: " + str(text).replace("\n", " "))

    def teardown(self):
        for sig, slot in getattr(self, "_project_signals", []):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._project_signals = []
        timer = getattr(self, "_rescan", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        self._dem_cache = {}
        self._last_layers = []
