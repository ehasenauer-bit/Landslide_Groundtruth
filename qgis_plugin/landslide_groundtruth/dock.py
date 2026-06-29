"""The dock panel: location pick, date, pre/post sliders, source preference, run."""
import json
import math
import os
import tempfile
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QDateTime, QUrl, QUrlQuery
from qgis.PyQt.QtGui import QDoubleValidator, QColor, QBrush, QPixmap
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QComboBox, QSlider, QDoubleSpinBox, QDateTimeEdit, QProgressBar,
    QPlainTextEdit, QFileDialog, QCheckBox, QTableWidget,
    QTableWidgetItem, QSplitter,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsSettings,
    QgsRectangle, QgsNetworkAccessManager,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)
from qgis.gui import QgsDockWidget, QgsCollapsibleGroupBox

from .task import PipelineTask

# label -> --prefer value
SOURCES = [
    ("Auto (Planet → Sentinel-2 → Landsat)", "auto"),
    ("PlanetScope (~3 m)", "planet"),
    ("Sentinel-2 (~10 m)", "s2"),
    ("Landsat (~30 m)", "landsat"),
]

# Downloadable review "scenes": (--scenes token, checkbox label, tooltip).
# Keys must match review_package.SCENE_KEYS. All checked by default = the full
# review package (the historical behaviour); uncheck to download fewer products.
SCENES = [
    ("true_color", "True colour (RGB)",
     "Natural-colour red/green/blue, linear 0–0.3 stretch. The context layer and "
     "false-positive check (clearcut, burn, cloud)."),
    ("highlight_natural", "Highlight Optimized Natural Color",
     "Copernicus Browser's 'Highlight Optimized Natural Color' look: a cube-root "
     "tone curve, cbrt(0.6 × reflectance), on the true-colour bands. Lifts shadow "
     "detail and tames blown-out snow/cloud so one stretch reads across the whole "
     "scene. Sentinel Hub custom script by Marko Repše, CC BY-SA 4.0 — not a "
     "separate download, just a second rendering of the same scene."),
    ("false_color", "False colour (NIR-R-G)",
     "NIR-red-green: vegetation pops bright red, fresh bare scar reads dark."),
    ("ndvi", "NDVI (pre & post)",
     "Raw NDVI before and after — the inputs behind dNDVI, for thresholding by eye."),
    ("dndvi", "NDVI change (dNDVI)",
     "pre→post NDVI change; vegetation loss is a strong negative."),
    ("dbright", "Brightness change (dBrightness)",
     "pre→post broadband brightness/albedo change; bare rock/soil reads positive."),
]

# sensor code (from result.json) -> human-readable name shown in the run banner
SENSOR_LABEL = {
    "planet": "PlanetScope (~3 m)",
    "s2": "Sentinel-2 (~10 m)",
    "landsat": "Landsat (~30 m)",
}

# table row tints: pre = blue, post = green; explicit dark text so the pastel
# backgrounds stay readable under both the light and dark QGIS themes.
PRE_BG = QColor(220, 235, 252)
POST_BG = QColor(224, 244, 226)
ROW_FG = QColor(20, 20, 20)

# Planetary Computer's public asset-signing endpoint. Given a blob href it
# returns {"href": "<href>?<SAS>", "msft:expiry": ...}; the SAS token is short-
# lived, so we sign on demand at preview time rather than caching signed URLs.
PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

# Planetary Computer data API (public titiler; no SAS token needed — it signs
# blob reads server-side, same service the rendered_preview thumbnails come from).
# We use it to render BOTH the scene-preview PNG and the on-map AOI GeoTIFF from
# the raw 10 m bands (B04/B03/B02), so we control the stretch instead of inheriting
# the pre-baked 'visual' TCI, which clips bright snow/ice to flat white.
#
# The render reproduces the project's "Highlight Optimized Natural Color" look
# (see review_package): a cube-root tone curve cbrt(0.6 * reflectance). In data-
# API terms that's gamma 3 (output = input**(1/3)) applied to reflectance scaled
# by 0.6 — i.e. rescale 0..16667 of the 0..10000 reflectance DN range. It lifts
# shadow detail and tames blown-out snow so one stretch reads across the scene.
# Tune the formula/rescale constants here if a scene needs it.
PC_DATA_URL = "https://planetarycomputer.microsoft.com/api/data/v1"
SOURCE_COLLECTION = {"Sentinel-2": "sentinel-2-l2a", "Landsat": "landsat-c2-l2"}
_S2_TC_BANDS = "assets=B04&assets=B03&assets=B02"
_S2_TC_FORMULA = "gamma RGB 3.0, saturation 1.2"
_S2_TC_RESCALE = "0,16667"

# muted text for table rows the run will NOT composite (ranked below the cutoff)
MUTED_FG = QColor(120, 120, 120)

# QGIS network-request timeout (ms) while previewing PC tiler layers: the on-
# demand tiler can be slow to render the first tiles of a fresh scene, and the
# 60 s default aborts them ("Network request … timed out"). 3 minutes.
NETWORK_TIMEOUT_MS = 180000


class LandslideDock(QgsDockWidget):
    def __init__(self, iface):
        super().__init__("Landslide Ground-Truthing")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.task = None
        self.settings = QgsSettings()
        self._preview_reply = None   # in-flight thumbnail request (if any)
        self._preview_pix = None     # last loaded preview, kept for rescaling
        self._preview_fallback = None  # baked thumb to retry if a render URL fails
        self._search_result = None   # last Search/Preview result (for map preview)
        self._sign_replies = []      # in-flight COG-signing requests
        self._sign_pending = 0       # signs still outstanding this preview
        self._tif_replies = []       # in-flight AOI-GeoTIFF downloads
        self._tif_pending = 0        # AOI downloads still outstanding this preview
        self._tif_fallbacks = []     # (label, cog_url) whose AOI render failed
        self._preview_added = []     # raster layers added by the current preview
        self._preview_failed = []    # labels that failed to sign/load
        self._gdal_tuned = False     # GDAL /vsicurl options set once
        self.setWidget(self._build_ui())

    # ---------- UI ----------
    def _build_ui(self):
        w = QWidget()
        root = QVBoxLayout(w)

        # --- environment settings (paths to the venv + project) ---
        # Collapsible + collapsed by default: these are set once, then forgotten.
        env = QgsCollapsibleGroupBox("Environment")
        env.setSaveCollapsedState(False)
        env.setCollapsed(True)
        ef = QFormLayout(env)
        self.python_edit = QLineEdit(self.settings.value(
            "landslide/python", "", type=str))
        self.project_edit = QLineEdit(self.settings.value(
            "landslide/project", "", type=str))
        self.out_edit = QLineEdit(self.settings.value(
            "landslide/out", "", type=str))
        for label, edit, picker in (
            ("venv python", self.python_edit, self._pick_python),
            ("project dir", self.project_edit, self._pick_project),
            ("output dir", self.out_edit, self._pick_out),
        ):
            row = QHBoxLayout()
            row.addWidget(edit)
            btn = QPushButton("…")
            btn.setFixedWidth(28)
            btn.clicked.connect(picker)
            row.addWidget(btn)
            ef.addRow(label, row)
        root.addWidget(env)

        # --- event inputs ---
        form = QFormLayout()
        self.lat_edit = QLineEdit()
        self.lat_edit.setPlaceholderText("e.g. 59.906992")
        self.lat_edit.setValidator(QDoubleValidator(-90.0, 90.0, 8))
        form.addRow("Latitude", self.lat_edit)

        self.lon_edit = QLineEdit()
        self.lon_edit.setPlaceholderText("e.g. -149.823317")
        self.lon_edit.setValidator(QDoubleValidator(-180.0, 180.0, 8))
        form.addRow("Longitude", self.lon_edit)

        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.2, 50.0)
        self.radius_spin.setSingleStep(0.5)
        self.radius_spin.setValue(3.0)
        self.radius_spin.setSuffix(" km")
        form.addRow("Search radius", self.radius_spin)

        self.dt_edit = QDateTimeEdit(QDateTime.currentDateTimeUtc())
        self.dt_edit.setCalendarPopup(True)
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("Event time (UTC)", self.dt_edit)

        self.auto_check = QCheckBox("Auto: tightest window (nearest clear scene each side)")
        self.auto_check.setToolTip(
            "Use only the clear scene nearest the event date on each side, for the "
            "smallest before/after gap. The sliders below then set the MAXIMUM days "
            "to search each side.")
        self.auto_check.toggled.connect(self._update_day_labels)

        self.pre_slider, self.pre_lbl = self._day_slider(60)
        self.post_slider, self.post_lbl = self._day_slider(90)
        for s in (self.pre_slider, self.post_slider):
            s.valueChanged.connect(self._update_day_labels)
        form.addRow(self.auto_check)
        form.addRow("Days before", self._slider_row(self.pre_slider, self.pre_lbl))
        form.addRow("Days after", self._slider_row(self.post_slider, self.post_lbl))
        self._update_day_labels()

        self.source_combo = QComboBox()
        for label, value in SOURCES:
            self.source_combo.addItem(label, value)
        form.addRow("Imagery source", self.source_combo)

        # max whole-scene cloud cover to consider. Scene-wide metric: per-pixel
        # cloud masking still applies, so a high value surfaces scenes that are
        # clear over the AOI but cloudy elsewhere — i.e. what Planet Explorer shows.
        self.cloud_spin = QDoubleSpinBox()
        self.cloud_spin.setRange(0.0, 100.0)
        self.cloud_spin.setDecimals(0)
        self.cloud_spin.setSingleStep(5.0)
        self.cloud_spin.setValue(80.0)
        self.cloud_spin.setSuffix(" %")
        self.cloud_spin.setToolTip(
            "Maximum WHOLE-SCENE cloud cover to consider. This is a scene-wide "
            "metric, not your AOI — per-pixel cloud masking still applies later, "
            "so a high value surfaces scenes that are clear over your point but "
            "cloudy elsewhere (matching Planet Explorer). Lower it to only consider "
            "mostly-clear scenes.")
        form.addRow("Max cloud %", self.cloud_spin)
        root.addLayout(form)

        # --- which review "scenes" to download ---
        # Each checked product is written by the run; uncheck what you won't use to
        # download less. The predicted-epicentre point layer is always included.
        # Expanded by default so the choice is visible (it's a primary control).
        scenes_box = QgsCollapsibleGroupBox("Scenes to download")
        scenes_box.setSaveCollapsedState(False)
        scenes_box.setCollapsed(False)
        sbox = QVBoxLayout(scenes_box)
        self.scene_checks = {}
        for key, label, tip in SCENES:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setToolTip(tip)
            self.scene_checks[key] = cb
            sbox.addWidget(cb)
        root.addWidget(scenes_box)

        # --- advanced PlanetScope coverage/quality toggles ---
        # Collapsible + collapsed by default: most runs leave these at the defaults
        # (both checked = match Planet Explorer). Tucked away to declutter the panel.
        adv = QgsCollapsibleGroupBox("Advanced options")
        adv.setSaveCollapsedState(False)
        adv.setCollapsed(True)
        adv_layout = QVBoxLayout(adv)

        # PlanetScope coverage: any AOI overlap (Planet Explorer-like, default) vs.
        # require the scene footprint to contain the exact epicentre.
        self.aoi_overlap_check = QCheckBox("AOI overlap (match Planet Explorer)")
        self.aoi_overlap_check.setChecked(True)
        self.aoi_overlap_check.setToolTip(
            "Checked: accept any PlanetScope scene overlapping the search box, like "
            "Planet Explorer — recovers partial-coverage scenes close to the event "
            "date. Unchecked: require the scene footprint to contain the exact "
            "epicentre point (stricter; can miss the nearest scenes). Sentinel-2 / "
            "Landsat ignore this.")
        adv_layout.addWidget(self.aoi_overlap_check)

        # include 'test'-quality PlanetScope (not just 'standard'). Near a fresh
        # event the nearest/clearest scenes are frequently published as 'test'
        # quality; they're fine for the visual review (looser calibration).
        self.test_quality_check = QCheckBox("Include test-quality PlanetScope (match Planet Explorer)")
        self.test_quality_check.setChecked(True)
        self.test_quality_check.setToolTip(
            "Checked: also consider PlanetScope scenes Planet publishes as 'test' "
            "quality, not just 'standard'. Near a fresh event the nearest and "
            "clearest scenes are often test-only (Planet Explorer shows them). Test "
            "scenes have looser geo/radiometric calibration — fine for spotting and "
            "digitizing a scar by eye, but eyeball them before trusting NDVI/"
            "reflectance values. Sentinel-2 / Landsat ignore this.")
        adv_layout.addWidget(self.test_quality_check)
        root.addWidget(adv)

        # --- search (free dry-run) / run / cancel ---
        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("Search / Preview")
        self.search_btn.setToolTip(
            "Free dry-run: search candidate before/after scenes per source and "
            "show them below. No Planet orders are placed and nothing is "
            "downloaded — use it to check coverage before a full Run.")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map")
        self.map_preview_btn.setToolTip(
            "Stream the nearest Sentinel-2 before & after scenes (true colour) "
            "straight onto the QGIS canvas as Cloud-Optimized GeoTIFF layers — "
            "no download, no order. Run Search / Preview first to find the "
            "scenes. Toggle the two layers' visibility to compare before vs "
            "after. (PlanetScope / Landsat support comes later.)")
        self.map_preview_btn.setEnabled(False)
        self.map_preview_btn.clicked.connect(self._preview_on_map)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.search_btn)
        btn_row.addWidget(self.map_preview_btn)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        root.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)         # indeterminate
        self.progress.hide()
        root.addWidget(self.progress)

        # --- outputs: candidate scenes (top) + run log (bottom) ---
        # A draggable splitter so the user can grow whichever pane matters now,
        # instead of the old fixed 50/50 split that left both cramped.
        out_split = QSplitter(Qt.Vertical)

        scenes = QWidget()
        scenes_box = QVBoxLayout(scenes)
        scenes_box.setContentsMargins(0, 0, 0, 0)
        scenes_box.addWidget(QLabel("Candidate scenes"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Side", "Date (UTC)", "Gap (d)", "Cloud %", "Source", "Scene ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        scenes_box.addWidget(self.table)
        out_split.addWidget(scenes)

        # preview pane: free browse image of the selected scene (no order placed)
        previewbox = QWidget()
        pv_layout = QVBoxLayout(previewbox)
        pv_layout.setContentsMargins(0, 0, 0, 0)
        pv_layout.addWidget(QLabel("Scene preview"))
        self.preview = QLabel("Select a scene to preview its browse image.")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("QLabel { background: palette(base); }")
        pv_layout.addWidget(self.preview, 1)
        out_split.addWidget(previewbox)

        logbox = QWidget()
        log_layout = QVBoxLayout(logbox)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Run log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log)
        out_split.addWidget(logbox)

        out_split.setStretchFactor(0, 3)   # table
        out_split.setStretchFactor(1, 3)   # preview
        out_split.setStretchFactor(2, 2)   # log
        scenes.setMinimumHeight(120)
        previewbox.setMinimumHeight(0)     # drag closed when you don't need it
        logbox.setMinimumHeight(80)
        root.addWidget(out_split, 1)
        return w

    def _day_slider(self, value):
        s = QSlider(Qt.Horizontal)
        s.setRange(1, 365)
        s.setValue(value)
        return s, QLabel()

    def _update_day_labels(self, *_):
        suffix = " (max)" if self.auto_check.isChecked() else ""
        self.pre_lbl.setText(f"{self.pre_slider.value()} d{suffix}")
        self.post_lbl.setText(f"{self.post_slider.value()} d{suffix}")

    def _slider_row(self, slider, lbl):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(slider, 1)
        h.addWidget(lbl)
        return row

    # ---------- path pickers ----------
    def _pick_python(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select venv python")
        if p:
            self.python_edit.setText(p)

    def _pick_project(self):
        p = QFileDialog.getExistingDirectory(self, "Select project dir")
        if p:
            self.project_edit.setText(p)

    def _pick_out(self):
        p = QFileDialog.getExistingDirectory(self, "Select output dir")
        if p:
            self.out_edit.setText(p)

    # ---------- run / search ----------
    def _collect(self):
        """Validate inputs and build the shared CLI args, or return None.

        Returns (python, script, project, out, args) where `args` is everything
        common to a full Run and a Search/Preview dry-run."""
        try:
            lat = float(self.lat_edit.text().strip())
            lon = float(self.lon_edit.text().strip())
        except ValueError:
            self._warn("Enter valid numeric latitude and longitude.")
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            self._warn("Latitude must be -90..90 and longitude -180..180.")
            return None
        python = self.python_edit.text().strip()
        project = self.project_edit.text().strip()
        out = self.out_edit.text().strip() or os.path.join(project, "out", "interactive")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path.")
            return None
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return None
        self._save_settings()
        os.makedirs(out, exist_ok=True)

        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{self.radius_spin.value():.2f}",
            "--pre-days", str(self.pre_slider.value()),
            "--post-days", str(self.post_slider.value()),
            "--prefer", self.source_combo.currentData(),
            "--max-cloud", f"{self.cloud_spin.value():.0f}",
            "--coverage", "aoi" if self.aoi_overlap_check.isChecked() else "point",
            "--quality", "any" if self.test_quality_check.isChecked() else "standard",
            "--out", out,
        ]
        if self.auto_check.isChecked():
            args.append("--auto-window")
        # which review layers to download (ignored by the Search dry-run). Always
        # pass the explicit list so "all checked" and "none checked" stay distinct.
        scenes = [k for k, cb in self.scene_checks.items() if cb.isChecked()]
        if scenes:
            args += ["--scenes", ",".join(scenes)]
        return python, script, project, out, args

    def _busy(self, on):
        self.progress.setVisible(on)
        self.run_btn.setEnabled(not on)
        self.search_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        # enabled only when idle AND a streamable Sentinel-2 scene is in hand
        self.map_preview_btn.setEnabled(
            (not on) and bool(self._best_s2("pre") or self._best_s2("post")))

    def _run(self):
        if not any(cb.isChecked() for cb in self.scene_checks.values()):
            self._warn("Select at least one scene to download.")
            return
        c = self._collect()
        if c is None:
            return
        python, script, project, out, args = c
        self.log.clear()
        self._busy(True)
        self.task = PipelineTask(python, script, project, args, out)
        self.task.logLine.connect(self._append_log)   # queued: worker -> GUI thread
        self.task.taskCompleted.connect(self._on_done)
        self.task.taskTerminated.connect(self._on_done)
        QgsApplication.taskManager().addTask(self.task)

    def _search(self):
        c = self._collect()
        if c is None:
            return
        python, script, project, out, args = c
        args = args + ["--search-only"]
        self.log.clear()
        self.table.setRowCount(0)
        self._preview_pix = None
        self._search_result = None    # invalidate map-preview until new results land
        self.preview.setText("Select a scene to preview its browse image.")
        self._busy(True)
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="search.json")
        self.task.logLine.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_search_done)
        self.task.taskTerminated.connect(self._on_search_done)
        QgsApplication.taskManager().addTask(self.task)

    def _cancel(self):
        if self.task is not None:
            self.task.cancel()

    def _on_search_done(self):
        self._busy(False)
        result = getattr(self.task, "result", None)
        self.task = None
        if not result:
            self._append_log("Search finished with no result.")
            return
        self._search_result = result
        self._fill_table(result)
        for note in result.get("notes", []):
            self._append_log("note: " + note)
        # the map preview streams Sentinel-2 'visual' COGs; enable it only when
        # there's a streamable S2 scene on at least one side.
        has_s2 = bool(self._best_s2("pre") or self._best_s2("post"))
        self.map_preview_btn.setEnabled(has_s2)
        npre, npost = len(result.get("pre", [])), len(result.get("post", []))
        self.iface.messageBar().pushInfo(
            "Landslide", f"Found {npre} pre / {npost} post candidate scenes "
                         f"(no orders placed).")

    def _fill_table(self, result):
        pre = result.get("pre", [])
        post = result.get("post", [])
        # The dry-run lists are gap-sorted for display, but a Run does NOT pick the
        # nearest scene — it ranks by the SAME cloud-weighted blend fetch_event uses
        # (gap_days + cloud_weight*cloud_pct) and median-composites the top N. So
        # replicate that selection here: ★ = the run's top-ranked scene, ✓ = also in
        # the composite, plain/greyed = ranked below the cutoff (not used).
        sel = self._run_selection(pre, post, result.get("params", {}))
        rows = [("pre", c) for c in pre] + [("post", c) for c in post]
        self.table.setRowCount(len(rows))
        for r, (side, c) in enumerate(rows):
            info = sel[side]
            cid = c.get("id")
            is_top = cid is not None and cid == info["top"]
            in_comp = cid in info["used"]
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else str(c["gap_days"])
            cloud = "" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}"
            marker = "★ " if is_top else ("✓ " if in_comp else "  ")
            cells = [marker + side, date, gap, cloud,
                     c.get("source", ""), c.get("id", "")]
            base = PRE_BG if side == "pre" else POST_BG
            bg = base.darker(112) if is_top else base   # the run's pick a touch darker
            fg = ROW_FG if in_comp else MUTED_FG        # grey the rows a Run won't use
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setBackground(QBrush(bg))
                item.setForeground(QBrush(fg))
                if is_top:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, col, item)
            # stash the browse URL + source + scene id on the row for the preview
            side_item = self.table.item(r, 0)
            side_item.setData(Qt.UserRole, c.get("thumb_url"))
            side_item.setData(Qt.UserRole + 1, c.get("source", ""))
            side_item.setData(Qt.UserRole + 2, c.get("id"))
            if c.get("thumb_url"):
                self.table.item(r, 5).setToolTip(c["thumb_url"])
            if is_top:
                side_item.setToolTip(
                    "★ The run's top-ranked scene on this side (gap_days + cloud "
                    "weighting). With --auto-window it's the single scene used; "
                    "otherwise the run median-composites this plus the ✓ scenes.")
            elif in_comp:
                side_item.setToolTip(
                    "✓ Also in the run's composite — a Run medians the top-ranked "
                    "clear scenes on this side, not just one.")
            else:
                side_item.setToolTip(
                    "Not used by the run: ranked below the composite cutoff "
                    "(gap_days + cloud weighting).")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ---------- replicate fetch_event's scene selection (for the ★/preview) ----------
    def _rank_like_run(self, cands, cloud_weight, auto_window):
        """Order candidates exactly as imagery.search_scenes would for a Run.

        auto_window: nearest day first, then clearest among same-day (cloud_weight
        ignored). Otherwise the blend cost gap_days + cloud_weight*cloud_pct, so a
        clearer scene a little further from the event can outrank a cloudy near one."""
        def gap(c):
            g = c.get("gap_days")
            return 1e9 if g is None else g

        def cloud(c):
            v = c.get("cloud_pct")
            return 100.0 if v is None else v

        if auto_window:
            return sorted(cands, key=lambda c: (round(gap(c)), cloud(c)))
        return sorted(cands, key=lambda c: gap(c) + cloud_weight * cloud(c))

    def _run_selection(self, pre, post, params):
        """Which scenes a Run would composite per side: {'pre'/'post': {top, used}}.

        Mirrors fetch_event: pick the source it would use (explicit --prefer, else
        the first of Planet→Sentinel-2→Landsat with scenes on both sides), then take
        the top 1 (auto-window) or top 6 (default, median-composited) by the run's
        ranking. `top` is the scene the preview should show; `used` is the full
        composite set."""
        cw = params.get("cloud_weight", 0.5)
        cw = 0.5 if cw is None else cw
        auto = bool(params.get("auto_window"))
        prefer = params.get("prefer", "auto")
        label_for = {"planet": "PlanetScope", "s2": "Sentinel-2", "landsat": "Landsat"}

        def of(rows, src):
            return [c for c in rows if c.get("source") == src]

        if prefer in label_for:
            source = label_for[prefer]
        else:  # 'auto': fetch_event's source priority, first with both sides covered
            source = next((s for s in ("PlanetScope", "Sentinel-2", "Landsat")
                           if of(pre, s) and of(post, s)), None)
        out = {}
        for side, rows in (("pre", pre), ("post", post)):
            ranked = self._rank_like_run(of(rows, source), cw, auto) if source else []
            used = ranked[:1] if auto else ranked[:6]
            out[side] = dict(top=ranked[0].get("id") if ranked else None,
                             used={c.get("id") for c in used})
        return out

    # ---------- scene preview ----------
    def _preview_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        cell = self.table.item(rows[0].row(), 0)
        if not cell:
            return
        primary = self._scene_preview_url(cell)
        baked = cell.data(Qt.UserRole)
        baked = self._auth_thumb_url(baked, cell.data(Qt.UserRole + 1)) if baked else None
        url = primary or baked
        if not url:
            self._preview_pix = None
            self.preview.setText("No browse image available for this scene.")
            return
        # If we built a snow-safe render URL, keep the baked thumbnail as a fallback
        # so a render hiccup degrades to the old preview rather than to nothing.
        fb = baked if (primary and baked and primary != baked) else None
        self._fetch_preview(url, fallback=fb)

    def _s2_render_query(self):
        """Snow-safe true-colour render params for the PC data API.

        Used by both the scene-preview PNG and the on-map AOI GeoTIFF, each fetched
        with a single GET, so the colour_formula is percent-encoded exactly once
        here (encoding it again at the call site is the bug that blanked the map)."""
        return (f"{_S2_TC_BANDS}&color_formula={quote(_S2_TC_FORMULA)}"
                f"&rescale={_S2_TC_RESCALE}&nodata=0")

    def _scene_preview_url(self, cell):
        """Browse-PNG URL for the selected row's scene.

        Sentinel-2: a snow-safe data-API render from raw bands (so ice keeps its
        texture instead of the 'visual' TCI's blown-out white). Other sources:
        their baked rendered_preview/thumbnail (signed for Planet)."""
        source = cell.data(Qt.UserRole + 1)
        item_id = cell.data(Qt.UserRole + 2)
        coll = SOURCE_COLLECTION.get(source)
        if source == "Sentinel-2" and coll and item_id:
            return (f"{PC_DATA_URL}/item/preview.png?collection={coll}"
                    f"&item={item_id}&{self._s2_render_query()}&max_size=1024")
        baked = cell.data(Qt.UserRole)
        return self._auth_thumb_url(baked, source) if baked else None

    def _auth_thumb_url(self, url, source):
        # Planet browse PNGs need the API key; STAC rendered previews are public.
        if source == "PlanetScope" and "api_key=" not in url:
            key = (os.environ.get("PL_API_KEY")
                   or self.settings.value("landslide/planet_api_key", "", type=str))
            if key:
                url += ("&" if "?" in url else "?") + "api_key=" + key
        return url

    def _fetch_preview(self, url, fallback=None):
        self._preview_pix = None
        self._preview_fallback = fallback
        self.preview.setText("Loading preview…")
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._preview_reply = reply
        reply.finished.connect(lambda r=reply: self._preview_loaded(r))

    def _preview_loaded(self, reply):
        # ignore stale replies (user clicked another row before this one returned)
        if reply is not self._preview_reply:
            reply.deleteLater()
            return
        self._preview_reply = None
        ok = reply.error() == QNetworkReply.NoError
        data = reply.readAll()
        reply.deleteLater()
        pix = QPixmap()
        if not ok or data.isEmpty() or not pix.loadFromData(data):
            fb = self._preview_fallback
            if fb:  # render URL failed — fall back to the baked thumbnail once
                self._preview_fallback = None
                self._fetch_preview(fb)
                return
            self.preview.setText("Preview unavailable for this scene.")
            return
        self._preview_fallback = None
        self._preview_pix = pix
        self._render_preview()

    def _render_preview(self):
        if not getattr(self, "preview", None) or self._preview_pix is None:
            return
        self.preview.setPixmap(self._preview_pix.scaled(
            max(self.preview.width(), 1), max(self.preview.height(), 1),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_preview()   # keep the preview fit to the pane as it resizes

    # ---------- preview on map (snow-safe AOI GeoTIFF) ----------
    def _best_s2(self, side):
        """The Sentinel-2 candidate a Run would lead with on `side` ('pre'/'post').

        Ranks the S2 candidates by the SAME blend the run uses (not gap-sorted
        display order), so the on-map preview shows the scene the run actually
        prioritises — the fix for the ★ previewing a near-but-cloudy scene the run
        would down-rank. Rendered from the scene id via the data API, so a COG href
        is no longer required."""
        if not self._search_result:
            return None
        params = self._search_result.get("params", {})
        s2 = [c for c in self._search_result.get(side, [])
              if c.get("source") == "Sentinel-2" and c.get("id")]
        ranked = self._rank_like_run(s2, params.get("cloud_weight", 0.5) or 0.5,
                                     bool(params.get("auto_window")))
        return ranked[0] if ranked else None

    def _selected_s2_by_side(self):
        """side -> the Sentinel-2 candidate selected in the table (or None).

        Lets 'Preview on map' honour a row you picked instead of always using the
        run's ★ scene. The table is single-select, so at most one side is set."""
        out = {"pre": None, "post": None}
        if not self._search_result:
            return out
        by_id = {}
        for side in ("pre", "post"):
            for c in self._search_result.get(side, []):
                if c.get("id"):
                    by_id[c["id"]] = (side, c)
        for idx in self.table.selectionModel().selectedRows():
            cell = self.table.item(idx.row(), 0)
            if cell and cell.data(Qt.UserRole + 1) == "Sentinel-2":
                info = by_id.get(cell.data(Qt.UserRole + 2))
                if info:
                    out[info[0]] = info[1]
        return out

    def _clear_preview_layers(self):
        """Remove the rasters a previous Preview-on-map added, so each preview
        shows just the current selection/★ pair instead of piling up."""
        for lyr in self._preview_added:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except (RuntimeError, AttributeError):
                pass
        self._preview_added = []

    def _preview_on_map(self):
        """Render the selected (or run's ★) pre & post Sentinel-2 scenes over the AOI.

        Per side, previews the Sentinel-2 row you selected in the table, or the
        run's top-ranked scene if you didn't select one on that side. Downloads a
        snow-safe true-colour GeoTIFF clipped to the search box from the Planetary
        Computer data API (raw bands + our stretch — no 'visual' TCI white-out, no
        SAS signing) and loads each as a georeferenced raster, then zooms to the
        AOI. Covers the AOI box only; falls back to the signed visual COG if a
        download fails."""
        bbox = self._aoi_bbox()
        if bbox is None:
            self._warn("Run Search / Preview first (need the AOI location).")
            return
        chosen = self._selected_s2_by_side()
        scenes = []
        for side in ("pre", "post"):
            c = chosen[side] or self._best_s2(side)
            if c and c.get("id"):
                date = (c.get("date") or "")[:10]
                pick = "selected" if chosen[side] else "★ best"
                scenes.append((f"S2 {side} {date}".strip(), c["id"],
                               c.get("cog_url"), pick))
        if not scenes:
            self._warn("No Sentinel-2 scene to preview.")
            return
        self._append_log(
            f"Preview on map: downloading snow-safe true colour over the AOI for "
            f"{len(scenes)} Sentinel-2 scene(s)…")
        self._ensure_network_timeout()
        self.map_preview_btn.setEnabled(False)
        self._clear_preview_layers()   # replace the previous preview, don't pile up
        self._preview_failed = []
        self._tif_fallbacks = []
        self._tif_pending = len(scenes)
        for label, item_id, cog_url, pick in scenes:
            self._append_log(f"  {label} ({pick})")
            self._download_aoi_tif(label, item_id, cog_url, bbox)

    def _ensure_network_timeout(self, ms=NETWORK_TIMEOUT_MS):
        """Raise QGIS's network-request timeout so a slow AOI render survives.

        The data API renders the clipped GeoTIFF on demand, which can exceed the
        60 s default on a cold scene. Bump 'qgis/networkAndProxy/networkTimeout'
        (only ever upward, so a user's higher value is kept) and update the live
        network manager. Global QGIS setting — also helps other slow layers."""
        try:
            s = QgsSettings()
            cur = s.value("qgis/networkAndProxy/networkTimeout", 60000, type=int)
            if cur < ms:
                s.setValue("qgis/networkAndProxy/networkTimeout", ms)
                self._append_log(f"  raised network timeout {cur} → {ms} ms (3 min)")
            try:
                QgsNetworkAccessManager.instance().setTimeout(ms)
            except (AttributeError, TypeError):
                pass   # older QGIS: the setting above still applies
        except Exception:
            pass

    def _aoi_bbox(self):
        """(minx, miny, maxx, maxy, radius_km) for the search AOI in lon/lat, or None.

        Same box the run searches and `_zoom_to_aoi` frames; radius is returned so
        the render can be sized near Sentinel-2's native 10 m."""
        result = self._search_result or {}
        try:
            lat = float(result.get("lat"))
            lon = float(result.get("lon"))
            radius = float(result.get("params", {}).get("radius_km"))
        except (TypeError, ValueError):
            return None
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        return (lon - dlon, lat - dlat, lon + dlon, lat + dlat, radius)

    def _download_aoi_tif(self, label, item_id, cog_url, bbox):
        """Fetch a snow-safe true-colour GeoTIFF clipped to the AOI (async GET).

        Single GET to the data API's bbox endpoint (same mechanism as the working
        scene-preview pane), sized to ~10 m/px and capped so a wide AOI stays a
        sane download. The reply lands in `_tif_loaded`."""
        minx, miny, maxx, maxy, radius = bbox
        px = int(min(2048, max(256, round(radius * 2 * 100))))   # ~10 m/px, capped
        url = (f"{PC_DATA_URL}/item/bbox/{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}.tif"
               f"?collection=sentinel-2-l2a&item={item_id}&{self._s2_render_query()}"
               f"&width={px}&height={px}")
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._tif_replies.append(reply)
        reply.finished.connect(
            lambda r=reply, l=label, cu=cog_url: self._tif_loaded(r, l, cu))

    def _tif_loaded(self, reply, label, cog_url):
        if reply in self._tif_replies:
            self._tif_replies.remove(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        ok = reply.error() == QNetworkReply.NoError and status == 200
        data = bytes(reply.readAll())
        reply.deleteLater()
        added = False
        if ok and data:
            try:
                fd, path = tempfile.mkstemp(suffix=".tif", prefix="landslide_preview_")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                lyr = QgsRasterLayer(path, label)
                if lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)
                    self._preview_added.append(lyr)
                    self._append_log(
                        f"  loaded {label} (AOI render, snow-safe true colour)")
                    added = True
            except OSError:
                pass
        if not added:
            if cog_url:   # degrade to the previous signed-COG behaviour
                self._tif_fallbacks.append((label, cog_url))
            else:
                self._preview_failed.append(label)
                self._append_log(f"  could not render {label}")
        self._tif_pending -= 1
        if self._tif_pending <= 0:
            self._after_tif_downloads()

    def _after_tif_downloads(self):
        if self._tif_fallbacks:
            self._append_log(f"  AOI render failed for {len(self._tif_fallbacks)} "
                             f"scene(s); falling back to signed COG stream")
            self._sign_pending = len(self._tif_fallbacks)
            for label, cog_url in self._tif_fallbacks:
                self._sign_cog(label, cog_url)
        else:
            self._finish_map_preview()

    def _sign_cog(self, label, cog_url):
        url = QUrl(PC_SIGN_URL)
        q = QUrlQuery()
        q.addQueryItem("href", cog_url)
        url.setQuery(q)
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(url))
        self._sign_replies.append(reply)
        reply.finished.connect(lambda r=reply, l=label: self._cog_signed(r, l))

    def _cog_signed(self, reply, label):
        if reply in self._sign_replies:
            self._sign_replies.remove(reply)
        ok = reply.error() == QNetworkReply.NoError
        data = bytes(reply.readAll())
        reply.deleteLater()
        href = None
        if ok:
            try:
                href = json.loads(data.decode("utf-8")).get("href")
            except (ValueError, UnicodeDecodeError):
                href = None
        if href:
            self._add_cog_layer(label, href)
        else:
            self._preview_failed.append(label)
            self._append_log(f"  could not sign {label} (asset-signing failed)")
        self._sign_pending -= 1
        if self._sign_pending <= 0:
            self._finish_map_preview()

    def _add_cog_layer(self, label, signed_href):
        self._tune_gdal_for_cog()
        lyr = QgsRasterLayer("/vsicurl/" + signed_href, label)
        if not lyr.isValid():
            self._preview_failed.append(label)
            self._append_log(f"  layer would not open for {label}")
            return
        QgsProject.instance().addMapLayer(lyr)
        self._preview_added.append(lyr)
        self._append_log(f"  loaded {label} (streamed)")

    def _tune_gdal_for_cog(self):
        """One-time GDAL tweaks so /vsicurl COG reads stay fast (skip directory
        listing, cache range reads). Best-effort — QGIS bundles osgeo, but don't
        fail the preview if it isn't importable."""
        if self._gdal_tuned:
            return
        self._gdal_tuned = True
        try:
            from osgeo import gdal
            gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
            gdal.SetConfigOption("VSI_CACHE", "TRUE")
            gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "3")
            gdal.SetConfigOption("GDAL_HTTP_RETRY_DELAY", "1")
        except Exception:
            pass

    def _finish_map_preview(self):
        self.map_preview_btn.setEnabled(bool(self._search_result))
        if self._preview_added:
            self._zoom_to_aoi()
            n = len(self._preview_added)
            msg = (f"Loaded {n} Sentinel-2 scene(s) over the AOI (snow-safe true "
                   f"colour). Toggle the layers to compare before vs after.")
            if self._preview_failed:
                msg += f" {len(self._preview_failed)} scene(s) failed to load."
            self.iface.messageBar().pushInfo("Landslide", msg)
        else:
            self._warn("Preview on map: no Sentinel-2 scene could be loaded.")

    def _zoom_to_aoi(self):
        """Frame the canvas on the search AOI (lat/lon + radius box) so the
        landslide point is centred rather than lost in the full S2 granule."""
        result = self._search_result or {}
        try:
            lat = float(result.get("lat"))
            lon = float(result.get("lon"))
            radius = float(result.get("params", {}).get("radius_km"))
        except (TypeError, ValueError):
            return
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        rect = QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        dst = self.canvas.mapSettings().destinationCrs()
        if dst.isValid() and dst.authid() != "EPSG:4326":
            try:
                xform = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem("EPSG:4326"), dst,
                    QgsProject.instance())
                rect = xform.transformBoundingBox(rect)
            except Exception:
                pass
        self.canvas.setExtent(rect)
        self.canvas.refresh()

    def _on_done(self):
        self._busy(False)
        result = getattr(self.task, "result", None)
        self.task = None
        if not result:
            self._append_log("Run finished with no result.")
            return
        status = result.get("status")
        if status == "error":
            self._warn(f"Pipeline error: {result.get('error')}")
            return
        if status == "no_imagery":
            self._warn("No usable imagery found for that location/date/window.")
            return
        self._load_layers(result.get("layers", []), result)

    def _load_layers(self, layers, result):
        added = []
        for path in layers:
            name = os.path.splitext(os.path.basename(path))[0]
            if path.endswith(".tif"):
                lyr = QgsRasterLayer(path, name)
            elif path.endswith(".gpkg"):
                lyr = QgsVectorLayer(path, name, "ogr")
            else:
                continue
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                added.append(lyr)
        if added:
            extent = QgsRectangle(added[0].extent())
            self.canvas.setExtent(extent)
            self.canvas.refresh()

        # End-of-run banner: spell out WHICH satellite was actually used. In 'auto'
        # mode the pipeline falls back PlanetScope -> Sentinel-2 -> Landsat silently,
        # so make the source (and any drop in resolution) impossible to miss.
        sensor = result.get("sensor")
        label = SENSOR_LABEL.get(sensor, sensor or "unknown")
        npre, npost = result.get("n_pre_scenes"), result.get("n_post_scenes")
        self._append_log("")
        self._append_log("=" * 52)
        self._append_log(f"  SATELLITE USED:  {label}")
        self._append_log(f"  scenes composited: {npre} pre / {npost} post")
        self._append_log("=" * 52)
        if sensor and sensor != "planet":
            warn = (f"Imagery source was {label} — NOT PlanetScope (~3 m). "
                    f"Small scars (smaller than ~50 m) may not resolve.")
            self._append_log("  ⚠ " + warn)
            # if Auto fell back off PlanetScope, spell out WHY so it's not a mystery
            note = result.get("fallback_note")
            if note:
                self._append_log(f"  why not PlanetScope: {note}")
                warn += f"  Reason: {note}"
            self.iface.messageBar().pushWarning("Landslide", warn)
        else:
            self.iface.messageBar().pushInfo(
                "Landslide", f"Done — satellite used: {label} "
                             f"({npre} pre / {npost} post scenes).")

    # ---------- helpers ----------
    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _warn(self, text):
        self.iface.messageBar().pushWarning("Landslide", text.replace("\n", " "))
        self._append_log(text)

    def _save_settings(self):
        self.settings.setValue("landslide/python", self.python_edit.text().strip())
        self.settings.setValue("landslide/project", self.project_edit.text().strip())
        self.settings.setValue("landslide/out", self.out_edit.text().strip())

    def teardown(self):
        if self._preview_reply is not None:
            self._preview_reply.abort()
            self._preview_reply = None
        for reply in self._sign_replies:
            reply.abort()
        self._sign_replies = []
        if self.task is not None:
            self.task.cancel()
