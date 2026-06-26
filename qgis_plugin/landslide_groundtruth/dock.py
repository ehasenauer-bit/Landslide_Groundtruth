"""The dock panel: location pick, date, pre/post sliders, source preference, run."""
import json
import math
import os

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


class LandslideDock(QgsDockWidget):
    def __init__(self, iface):
        super().__init__("Landslide Ground-Truthing")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.task = None
        self.settings = QgsSettings()
        self._preview_reply = None   # in-flight thumbnail request (if any)
        self._preview_pix = None     # last loaded preview, kept for rescaling
        self._search_result = None   # last Search/Preview result (for map preview)
        self._sign_replies = []      # in-flight COG-signing requests
        self._sign_pending = 0       # signs still outstanding this preview
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
        rows = [("pre", c) for c in pre] + [("post", c) for c in post]
        self.table.setRowCount(len(rows))
        # the search lists are ranked best-first, so the first scene on each side
        # is the one a real Run would actually composite — flag those rows.
        chosen_rows = set()
        if pre:
            chosen_rows.add(0)
        if post:
            chosen_rows.add(len(pre))
        for r, (side, c) in enumerate(rows):
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else str(c["gap_days"])
            cloud = "" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}"
            chosen = r in chosen_rows
            label = ("★ " + side) if chosen else side
            cells = [label, date, gap, cloud, c.get("source", ""), c.get("id", "")]
            base = PRE_BG if side == "pre" else POST_BG
            bg = base.darker(112) if chosen else base   # chosen rows a touch darker
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setBackground(QBrush(bg))
                item.setForeground(QBrush(ROW_FG))
                if chosen:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, col, item)
            # stash the free browse-image URL + source on the row for the preview
            side_item = self.table.item(r, 0)
            side_item.setData(Qt.UserRole, c.get("thumb_url"))
            side_item.setData(Qt.UserRole + 1, c.get("source", ""))
            if c.get("thumb_url"):
                self.table.item(r, 5).setToolTip(c["thumb_url"])
            if chosen:
                side_item.setToolTip(
                    "Nearest clear scene on this side — the one a Run would composite.")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ---------- scene preview ----------
    def _preview_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        cell = self.table.item(rows[0].row(), 0)
        url = cell.data(Qt.UserRole) if cell else None
        if not url:
            self._preview_pix = None
            self.preview.setText("No browse image available for this scene.")
            return
        self._fetch_preview(self._auth_thumb_url(url, cell.data(Qt.UserRole + 1)))

    def _auth_thumb_url(self, url, source):
        # Planet browse PNGs need the API key; STAC rendered previews are public.
        if source == "PlanetScope" and "api_key=" not in url:
            key = (os.environ.get("PL_API_KEY")
                   or self.settings.value("landslide/planet_api_key", "", type=str))
            if key:
                url += ("&" if "?" in url else "?") + "api_key=" + key
        return url

    def _fetch_preview(self, url):
        self._preview_pix = None
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
            self.preview.setText("Preview unavailable for this scene.")
            return
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

    # ---------- preview on map (streamed Sentinel-2 COGs) ----------
    def _best_s2(self, side):
        """Nearest streamable Sentinel-2 candidate on `side` ('pre'/'post').

        The candidate lists are gap-sorted across all sources, so the first one
        whose source is Sentinel-2 AND that carries a true-colour COG href is the
        scene a Run would composite there — the one worth previewing on the map."""
        if not self._search_result:
            return None
        for c in self._search_result.get(side, []):
            if c.get("source") == "Sentinel-2" and c.get("cog_url"):
                return c
        return None

    def _preview_on_map(self):
        """Stream the chosen pre & post Sentinel-2 scenes onto the canvas.

        Signs each scene's true-colour COG fresh (PC SAS tokens are short-lived),
        adds it as a /vsicurl raster layer, then zooms to the search AOI so the
        landslide area is in view. Independent of the table row selection."""
        scenes = []
        for side in ("pre", "post"):
            c = self._best_s2(side)
            if c:
                date = (c.get("date") or "")[:10]
                scenes.append((f"S2 {side} {date}".strip(), c["cog_url"]))
        if not scenes:
            self._warn("No Sentinel-2 scene with a streamable COG to preview.")
            return
        self._append_log(
            f"Preview on map: signing + streaming {len(scenes)} Sentinel-2 "
            f"scene(s)…")
        self.map_preview_btn.setEnabled(False)
        self._preview_added = []
        self._preview_failed = []
        self._sign_pending = len(scenes)
        for label, cog_url in scenes:
            self._sign_cog(label, cog_url)

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
            msg = (f"Loaded {n} Sentinel-2 scene(s) on the map (streamed, no "
                   f"download). Toggle the layers to compare before vs after.")
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
