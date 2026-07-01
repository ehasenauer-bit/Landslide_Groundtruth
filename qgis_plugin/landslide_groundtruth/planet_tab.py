"""The PlanetScope tab: Planet's own Data / Tiles system, separate from the
Sentinel-2 / Landsat (Planetary Computer STAC) pipeline in dock.py.

Why a separate tab: PlanetScope uses different APIs and a different preview
mechanism than the STAC sources. Search runs through the SAME venv subprocess as
the rest of the plugin (`run_single.py --search-only --prefer planet`, which uses
the `planet` SDK), but the marquee feature — a FULL-RESOLUTION preview of a scene
straight on the QGIS canvas with no order and zero quota — is done in-process via
Planet's Data API tile service (the same one Planet Explorer uses):

  1. POST the scene ids to https://tiles.planet.com/data/v1/layers  -> a tile hash
  2. add https://tiles{0-3}.planet.com/data/v1/layers/<hash>/{z}/{x}/{y}?api_key=…
     as an XYZ raster layer.

v1 scope is BROWSE + MAP PREVIEW only (no ordering/download yet); ordering the
AOI-clipped SR+UDM2 bundle into the review package comes in a later pass.

Auth: the tile POST + browse thumbnails need a Planet API key (field below, saved
to QgsSettings 'landslide/planet_api_key', or the PL_API_KEY env var). The search
subprocess authenticates the same way (or via `planet auth login`).
"""
import base64
import json
import math
import os
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QUrl, QByteArray, QSize
from qgis.PyQt.QtGui import QPixmap, QIcon
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDoubleSpinBox, QDateTimeEdit, QCheckBox,
    QProgressBar, QPlainTextEdit, QTableWidget, QTableWidgetItem, QSplitter,
    QScrollArea, QGridLayout, QToolButton,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsRectangle,
    QgsNetworkAccessManager, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsField, QgsFeature, QgsFillSymbol,
)
from qgis.PyQt.QtCore import QVariant

from .task import PipelineTask

# Planet's Data API tile service (undocumented, but stable — it's what Planet
# Explorer's "Add preview to map" relies on). POST scene ids to get a tile hash,
# then stream XYZ tiles from a per-hash layer. {0} is a subdomain shard 0-3.
TILE_HASH_URL = "https://tiles.planet.com/data/v1/layers"
TILE_XYZ_URL = "https://tiles{shard}.planet.com/data/v1/layers/{hash}/{{z}}/{{x}}/{{y}}"
ITEM_TYPE = "PSScene"

# table row tints, matching the Sentinel tab (pre = blue, post = green)
from .dock import PRE_BG, POST_BG, ROW_FG, MUTED_FG  # noqa: E402


class PlanetTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock                 # shared Environment fields + helpers live here
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings
        self.task = None
        self._search_result = None       # last search.json (candidates + params)
        self._preview_reply = None       # in-flight browse-thumbnail request
        self._preview_pix = None         # last loaded thumbnail, kept for rescaling
        self._tile_replies = []          # in-flight tile-hash POSTs
        self._preview_layers = []        # XYZ preview layers added to the map
        self._footprint_layers = []      # scene-footprint vector layers on the map
        self._gallery_replies = []       # in-flight quicklook-thumbnail requests
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)

        intro = QLabel(
            "Search PlanetScope (~3 m) and preview scenes at full resolution on the "
            "map — no order placed, no quota used. Ordering into the review package "
            "comes later; for now this is browse + preview.")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        # --- event inputs (own copy; convenience button pulls from the other tab) ---
        form = QFormLayout()
        self.lat_edit = QLineEdit()
        self.lat_edit.setPlaceholderText("e.g. 59.906992")
        self.lon_edit = QLineEdit()
        self.lon_edit.setPlaceholderText("e.g. -149.823317")
        form.addRow("Latitude", self.lat_edit)
        form.addRow("Longitude", self.lon_edit)

        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.2, 50.0)
        self.radius_spin.setSingleStep(0.5)
        self.radius_spin.setValue(3.0)
        self.radius_spin.setSuffix(" km")
        form.addRow("Search radius", self.radius_spin)

        self.dt_edit = QDateTimeEdit()
        self.dt_edit.setCalendarPopup(True)
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_edit.setDateTime(self.dock.dt_edit.dateTime())
        form.addRow("Event time (UTC)", self.dt_edit)

        copy_btn = QPushButton("⟵ Copy location & date from Sentinel-2 / Landsat tab")
        copy_btn.setToolTip("Pull latitude, longitude, radius, event time and the "
                            "before/after windows from the other tab so you don't "
                            "re-enter the same event.")
        copy_btn.clicked.connect(self._copy_from_main)
        form.addRow(copy_btn)

        self.auto_check = QCheckBox("Auto: tightest window (nearest clear scene each side)")
        self.auto_check.setToolTip(
            "Use only the clear scene nearest the event date on each side. The day "
            "spinners below then set the MAXIMUM days to search each side.")
        form.addRow(self.auto_check)

        self.pre_spin = QDoubleSpinBox()
        self.pre_spin.setRange(1, 365)
        self.pre_spin.setDecimals(0)
        self.pre_spin.setValue(60)
        self.pre_spin.setSuffix(" d")
        self.post_spin = QDoubleSpinBox()
        self.post_spin.setRange(1, 365)
        self.post_spin.setDecimals(0)
        self.post_spin.setValue(90)
        self.post_spin.setSuffix(" d")
        days = QHBoxLayout()
        days.addWidget(QLabel("before"))
        days.addWidget(self.pre_spin)
        days.addWidget(QLabel("after"))
        days.addWidget(self.post_spin)
        form.addRow("Window", self._wrap(days))

        # --- Planet-specific filters (moved here from the old Advanced options) ---
        self.cloud_spin = QDoubleSpinBox()
        self.cloud_spin.setRange(0.0, 100.0)
        self.cloud_spin.setDecimals(0)
        self.cloud_spin.setSingleStep(5.0)
        self.cloud_spin.setValue(80.0)
        self.cloud_spin.setSuffix(" %")
        self.cloud_spin.setToolTip(
            "Maximum WHOLE-SCENE cloud cover to consider. Scene-wide metric, not "
            "your AOI — per-pixel UDM2 masking still applies, so a high value "
            "surfaces scenes clear over your point but cloudy elsewhere (what Planet "
            "Explorer shows).")
        form.addRow("Max cloud %", self.cloud_spin)

        self.coverage_combo = QComboBox()
        self.coverage_combo.addItem("AOI overlap (match Planet Explorer)", "aoi")
        self.coverage_combo.addItem("Cover the exact epicentre (stricter)", "point")
        self.coverage_combo.setToolTip(
            "AOI overlap: accept any scene overlapping the search box — recovers "
            "partial-coverage scenes near the event date. Epicentre: require the "
            "footprint to contain the point (can miss the nearest scenes).")
        form.addRow("Coverage", self.coverage_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Standard quality only", "standard")
        self.quality_combo.addItem("Include test-quality (match Planet Explorer)", "any")
        self.quality_combo.setToolTip(
            "Near a fresh event the nearest/clearest scenes are often published as "
            "'test' quality (looser geo/radiometric calibration). Fine for a visual "
            "review; eyeball before trusting reflectance/NDVI.")
        form.addRow("Quality", self.quality_combo)

        self.key_edit = QLineEdit(self._api_key())
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText("Planet API key (or set PL_API_KEY)")
        self.key_edit.setToolTip(
            "Needed for the full-res map preview and browse thumbnails, and passed "
            "to the search subprocess. Saved to QGIS settings; leave blank to use "
            "the PL_API_KEY environment variable or a `planet auth login` session.")
        form.addRow("Planet API key", self.key_edit)
        root.addLayout(form)

        # --- buttons ---
        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("Search (free)")
        self.search_btn.setToolTip(
            "Free Data API search for candidate before/after PlanetScope scenes. No "
            "orders placed, no quota used.")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map (full-res)")
        self.map_preview_btn.setToolTip(
            "Stream the selected scene (or the nearest before & after scenes) onto "
            "the canvas at full resolution via Planet's tile service — no order, no "
            "download. Toggle the before/after layers to compare.")
        self.map_preview_btn.setEnabled(False)
        self.map_preview_btn.clicked.connect(self._preview_on_map)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        for b in (self.search_btn, self.map_preview_btn, self.cancel_btn):
            btn_row.addWidget(b)
        root.addLayout(btn_row)

        self.footprint_check = QCheckBox("Show scene footprints on map")
        self.footprint_check.setToolTip(
            "Draw each candidate scene's footprint (before = blue, after = green) "
            "plus the AOI box, so you can see whether a strip actually covers the "
            "epicentre. A single PlanetScope strip is only a few km wide.")
        self.footprint_check.toggled.connect(self._on_footprint_toggle)
        root.addWidget(self.footprint_check)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

        # --- outputs: candidate table / gallery / thumbnail / log ---
        split = QSplitter(Qt.Vertical)

        tablebox = QWidget()
        tl = QVBoxLayout(tablebox)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Candidate scenes (★ = nearest on each side)"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Side", "Date (UTC)", "Gap (d)", "Cloud %", "Scene ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        tl.addWidget(self.table)
        split.addWidget(tablebox)

        gallerybox = QWidget()
        gl = QVBoxLayout(gallerybox)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(QLabel("Quicklook gallery (click a thumbnail to select its scene)"))
        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_inner = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery_inner)
        self.gallery_layout.setAlignment(Qt.AlignTop)
        self.gallery_scroll.setWidget(self.gallery_inner)
        gl.addWidget(self.gallery_scroll, 1)
        split.addWidget(gallerybox)

        previewbox = QWidget()
        pl = QVBoxLayout(previewbox)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(QLabel("Scene preview (browse image)"))
        self.preview = QLabel("Search, then select a scene to preview its browse image.")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("QLabel { background: palette(base); }")
        pl.addWidget(self.preview, 1)
        split.addWidget(previewbox)

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
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        split.setStretchFactor(3, 2)
        root.addWidget(split, 1)

    def _wrap(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    # ---------- inputs ----------
    def _copy_from_main(self):
        """Pull the event from the Sentinel-2 / Landsat tab so the same landslide
        isn't typed twice."""
        d = self.dock
        self.lat_edit.setText(d.lat_edit.text())
        self.lon_edit.setText(d.lon_edit.text())
        self.radius_spin.setValue(d.radius_spin.value())
        self.dt_edit.setDateTime(d.dt_edit.dateTime())
        self.auto_check.setChecked(d.auto_check.isChecked())
        self.pre_spin.setValue(d.pre_slider.value())
        self.post_spin.setValue(d.post_slider.value())

    def _api_key(self):
        """Planet API key from the field (if built yet), else settings, else env."""
        field = getattr(self, "key_edit", None)
        if field is not None and field.text().strip():
            return field.text().strip()
        return (self.settings.value("landslide/planet_api_key", "", type=str)
                or os.environ.get("PL_API_KEY", ""))

    def _collect(self):
        """Validate inputs and build the run_single.py CLI args for a Planet-only
        search-only dry-run, or return None. Reuses the shared Environment fields
        (venv python / project / output) from the dock."""
        try:
            lat = float(self.lat_edit.text().strip())
            lon = float(self.lon_edit.text().strip())
        except ValueError:
            self._warn("Enter valid numeric latitude and longitude.")
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            self._warn("Latitude must be -90..90 and longitude -180..180.")
            return None
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        # own output subdir so a Planet search.json never clashes with the
        # Sentinel tab's search.json in the shared output directory
        out = os.path.join(base_out, "planet")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return None
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return None
        # persist the API key and feed it to the child so `planet.Planet()` can
        # authenticate via PL_API_KEY without a separate `planet auth login`
        key = self.key_edit.text().strip()
        if key:
            self.settings.setValue("landslide/planet_api_key", key)
            os.environ["PL_API_KEY"] = key
        os.makedirs(out, exist_ok=True)

        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{self.radius_spin.value():.2f}",
            "--pre-days", str(int(self.pre_spin.value())),
            "--post-days", str(int(self.post_spin.value())),
            "--prefer", "planet",
            "--max-cloud", f"{self.cloud_spin.value():.0f}",
            "--coverage", self.coverage_combo.currentData(),
            "--quality", self.quality_combo.currentData(),
            "--search-only", "--out", out,
        ]
        if self.auto_check.isChecked():
            args.append("--auto-window")
        return python, script, project, out, args

    # ---------- search ----------
    def _search(self):
        c = self._collect()
        if c is None:
            return
        python, script, project, out, args = c
        self.log.clear()
        self.table.setRowCount(0)
        self._search_result = None
        self._preview_pix = None
        self.preview.setText("Search, then select a scene to preview its browse image.")
        self._clear_gallery()
        self._clear_footprints()
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
        self._load_gallery(result)
        if self.footprint_check.isChecked():
            self._draw_footprints()
        for note in result.get("notes", []):
            self._append_log("note: " + note)
        npre, npost = len(result.get("pre", [])), len(result.get("post", []))
        self.map_preview_btn.setEnabled(bool(npre or npost))
        self.iface.messageBar().pushInfo(
            "PlanetScope", f"Found {npre} pre / {npost} post candidate scene(s) "
                           f"(no orders placed).")

    def _rank(self, cands):
        """Candidates ordered as a run would compose them: nearest day first under
        auto-window, else the gap + cloud_weight*cloud blend planet_imagery uses."""
        params = (self._search_result or {}).get("params", {})
        auto = bool(params.get("auto_window"))
        cw = params.get("cloud_weight", 0.5)
        cw = 0.5 if cw is None else cw

        def gap(c):
            g = c.get("gap_days")
            return 1e9 if g is None else g

        def cloud(c):
            v = c.get("cloud_pct")
            return 100.0 if v is None else v

        if auto:
            return sorted(cands, key=lambda c: (round(gap(c)), cloud(c)))
        return sorted(cands, key=lambda c: gap(c) + cw * cloud(c))

    def _fill_table(self, result):
        pre = result.get("pre", [])
        post = result.get("post", [])
        top = {"pre": None, "post": None}
        for side, rows in (("pre", pre), ("post", post)):
            ranked = self._rank(rows)
            top[side] = ranked[0].get("id") if ranked else None
        rows = [("pre", c) for c in pre] + [("post", c) for c in post]
        self.table.setRowCount(len(rows))
        for r, (side, c) in enumerate(rows):
            cid = c.get("id")
            is_top = cid is not None and cid == top[side]
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else str(c["gap_days"])
            cloud = "" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}"
            marker = "★ " if is_top else "  "
            cells = [marker + side, date, gap, cloud, cid or ""]
            bg = (PRE_BG if side == "pre" else POST_BG)
            if is_top:
                bg = bg.darker(112)
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                item.setForeground(ROW_FG if is_top else MUTED_FG)
                if is_top:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, col, item)
            head = self.table.item(r, 0)
            head.setData(Qt.UserRole, c.get("thumb_url"))
            head.setData(Qt.UserRole + 1, cid)
            head.setData(Qt.UserRole + 2, side)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ---------- quicklook gallery ----------
    def _clear_gallery(self):
        for reply in self._gallery_replies:
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._gallery_replies = []
        while self.gallery_layout.count():
            item = self.gallery_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _load_gallery(self, result):
        self._clear_gallery()
        for side in ("pre", "post"):
            cands = result.get(side, [])
            if not cands:
                continue
            self.gallery_layout.addWidget(QLabel(
                f"{'Before' if side == 'pre' else 'After'} ({len(cands)})"))
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            cols = 3
            for i, c in enumerate(cands):
                grid.addWidget(self._make_tile(c), i // cols, i % cols)
            self.gallery_layout.addWidget(grid_host)

    def _make_tile(self, c):
        date = (c.get("date") or "")[:10]
        cloud = "?" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}%"
        gap = "" if c.get("gap_days") is None else f"gap {c['gap_days']}d"
        cid = c.get("id")
        tile = QToolButton()
        tile.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tile.setIconSize(QSize(128, 128))
        tile.setFixedWidth(150)
        tile.setAutoRaise(True)
        tile.setText(f"{date}\ncloud {cloud} · {gap}")
        tile.setToolTip(f"{cid}\n{date}  cloud {cloud}  {gap}")
        tile.clicked.connect(lambda _=False, x=cid: self._select_row_by_id(x))
        url = self._auth_thumb(c.get("thumb_url"))
        if url:
            self._fetch_tile_thumb(tile, url)
        else:
            tile.setText(tile.text() + "\n(no preview)")
        return tile

    def _fetch_tile_thumb(self, tile, url):
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._gallery_replies.append(reply)
        reply.finished.connect(lambda r=reply, t=tile: self._tile_thumb_loaded(r, t))

    def _tile_thumb_loaded(self, reply, tile):
        if reply in self._gallery_replies:
            self._gallery_replies.remove(reply)
        ok = reply.error() == QNetworkReply.NoError
        data = reply.readAll()
        reply.deleteLater()
        pix = QPixmap()
        if ok and not data.isEmpty() and pix.loadFromData(data):
            try:
                tile.setIcon(QIcon(pix))
            except RuntimeError:
                pass  # tile torn down by a newer search
        else:
            try:
                tile.setText(tile.text() + "\n(no preview)")
            except RuntimeError:
                pass

    def _select_row_by_id(self, cid):
        for r in range(self.table.rowCount()):
            head = self.table.item(r, 0)
            if head is not None and head.data(Qt.UserRole + 1) == cid:
                self.table.selectRow(r)
                return

    # ---------- browse-thumbnail preview pane ----------
    def _auth_thumb(self, url):
        """Planet browse PNGs need the API key appended."""
        if not url:
            return None
        key = self._api_key()
        if key and "api_key=" not in url:
            url += ("&" if "?" in url else "?") + "api_key=" + key
        return url

    def _preview_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        head = self.table.item(rows[0].row(), 0)
        url = self._auth_thumb(head.data(Qt.UserRole)) if head else None
        if not url:
            self._preview_pix = None
            self.preview.setText("No browse image available for this scene.")
            return
        self.preview.setText("Loading preview…")
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._preview_reply = reply
        reply.finished.connect(lambda r=reply: self._preview_loaded(r))

    def _preview_loaded(self, reply):
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
        if self._preview_pix is None:
            return
        self.preview.setPixmap(self._preview_pix.scaled(
            max(self.preview.width(), 1), max(self.preview.height(), 1),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_preview()

    # ---------- full-res preview on map (Planet tile service) ----------
    def _preview_picks(self):
        """[(layer_name, [scene ids]), …] to stream onto the map.

        The selected table row (if any) overrides its own side; each other side
        falls back to its ★ nearest scene, so you always get a before/after pair
        unless a window is empty."""
        result = self._search_result or {}
        picks = {"pre": None, "post": None}
        for idx in self.table.selectionModel().selectedRows():
            head = self.table.item(idx.row(), 0)
            if head is None:
                continue
            side = head.data(Qt.UserRole + 2)
            cid = head.data(Qt.UserRole + 1)
            if side in picks and cid:
                picks[side] = cid
        for side in ("pre", "post"):
            if picks[side] is None:
                ranked = self._rank(result.get(side, []))
                picks[side] = ranked[0].get("id") if ranked else None
        out = []
        for side in ("pre", "post"):
            cid = picks[side]
            if not cid:
                continue
            date = self._date_for(side, cid)
            out.append((f"PlanetScope {'before' if side == 'pre' else 'after'} "
                        f"{date}".strip(), [cid]))
        return out

    def _date_for(self, side, cid):
        for c in (self._search_result or {}).get(side, []):
            if c.get("id") == cid:
                return (c.get("date") or "")[:10]
        return ""

    def _preview_on_map(self):
        key = self._api_key()
        if not key:
            self._warn("Set a Planet API key (above) to stream tiles onto the map.")
            return
        picks = self._preview_picks()
        if not picks:
            self._warn("Run Search first — no PlanetScope scene to preview.")
            return
        self._clear_preview_layers()
        self._append_log(f"Preview on map: requesting tiles for {len(picks)} scene(s)…")
        self.map_preview_btn.setEnabled(False)
        self._tile_pending = len(picks)
        for name, ids in picks:
            self._append_log(f"  {name}")
            self._request_tile_layer(name, ids, key)

    def _request_tile_layer(self, name, ids, key):
        """POST scene ids -> tile hash, then add the XYZ layer (async, off the GUI)."""
        item_type_ids = [f"{ITEM_TYPE}:{i}" for i in ids]
        body = QByteArray(("ids=" + quote(",".join(item_type_ids))).encode())
        req = QNetworkRequest(QUrl(TILE_HASH_URL))
        req.setHeader(QNetworkRequest.ContentTypeHeader,
                      "application/x-www-form-urlencoded")
        token = base64.b64encode(f"{key}:".encode()).decode()
        req.setRawHeader(b"Authorization", ("Basic " + token).encode())
        reply = QgsNetworkAccessManager.instance().post(req, body)
        self._tile_replies.append(reply)
        reply.finished.connect(
            lambda r=reply, n=name, k=key: self._tile_hash_ready(r, n, k))

    def _tile_hash_ready(self, reply, name, key):
        if reply in self._tile_replies:
            self._tile_replies.remove(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        ok = reply.error() == QNetworkReply.NoError and status in (200, 201)
        data = bytes(reply.readAll())
        reply.deleteLater()
        tile_hash = None
        if ok and data:
            try:
                tile_hash = json.loads(data).get("name")
            except (ValueError, AttributeError):
                tile_hash = None
        if not tile_hash:
            self._append_log(f"    tile request failed for {name} "
                             f"(HTTP {status}) — check the API key / scene access")
        else:
            self._add_tile_layer(name, tile_hash, key)
        self._tile_pending -= 1
        if self._tile_pending <= 0:
            self.map_preview_btn.setEnabled(True)
            if self._preview_layers:
                self._zoom_to_aoi()

    def _add_tile_layer(self, name, tile_hash, key):
        # {z}/{x}/{y} must survive as literal placeholders: they're braces in the
        # template, and quoting the whole URL percent-encodes them so QGIS decodes
        # them back and substitutes per tile.
        tile_url = (TILE_XYZ_URL.format(shard=0, hash=tile_hash)
                    + f"?api_key={key}")
        uri = ("type=xyz&crs=EPSG:3857&zmin=0&zmax=20&url="
               + quote(tile_url, safe=""))
        lyr = QgsRasterLayer(uri, name, "wms")
        if not lyr.isValid():
            self._append_log(f"    could not build the tile layer for {name}")
            return
        QgsProject.instance().addMapLayer(lyr)
        self._preview_layers.append(lyr)
        self._append_log(f"    added: {name}")

    def _clear_preview_layers(self):
        for lyr in self._preview_layers:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except (RuntimeError, AttributeError):
                pass
        self._preview_layers = []

    # ---------- scene footprints ----------
    def _on_footprint_toggle(self, checked):
        if checked:
            self._draw_footprints()
        else:
            self._clear_footprints()

    def _clear_footprints(self):
        for lyr in self._footprint_layers:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except (RuntimeError, AttributeError):
                pass
        self._footprint_layers = []

    def _draw_footprints(self):
        self._clear_footprints()
        result = self._search_result
        if not result:
            return
        for side, outline in (("pre", "0,90,200"), ("post", "0,150,60")):
            cands = [c for c in result.get(side, []) if c.get("geometry")]
            if not cands:
                continue
            lyr = QgsVectorLayer("Polygon?crs=EPSG:4326",
                                 f"PlanetScope footprints — {side}", "memory")
            pr = lyr.dataProvider()
            pr.addAttributes([
                QgsField("scene_id", QVariant.String),
                QgsField("date", QVariant.String),
                QgsField("gap_days", QVariant.Int),
                QgsField("cloud_pct", QVariant.Double),
            ])
            lyr.updateFields()
            feats = []
            for c in cands:
                g = self.dock._qgs_geom(c.get("geometry"))
                if g is None:
                    continue
                f = QgsFeature(lyr.fields())
                f.setGeometry(g)
                f.setAttributes([c.get("id"), c.get("date"), c.get("gap_days"),
                                 c.get("cloud_pct")])
                feats.append(f)
            if not feats:
                continue
            pr.addFeatures(feats)
            lyr.updateExtents()
            sym = QgsFillSymbol.createSimple({
                "style": "no", "outline_color": outline, "outline_width": "0.6"})
            lyr.renderer().setSymbol(sym)
            QgsProject.instance().addMapLayer(lyr)
            self._footprint_layers.append(lyr)

    # ---------- misc ----------
    def _aoi(self):
        """(lat, lon, radius_km) from the last search result, or None."""
        result = self._search_result or {}
        try:
            return (float(result.get("lat")), float(result.get("lon")),
                    float(result.get("params", {}).get("radius_km")))
        except (TypeError, ValueError):
            return None

    def _zoom_to_aoi(self):
        aoi = self._aoi()
        if aoi is None:
            return
        lat, lon, radius = aoi
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        rect = QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        dst = self.canvas.mapSettings().destinationCrs()
        if dst.isValid() and dst.authid() != "EPSG:4326":
            try:
                rect = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem("EPSG:4326"), dst,
                    QgsProject.instance()).transformBoundingBox(rect)
            except Exception:
                pass
        self.canvas.setExtent(rect)
        self.canvas.refresh()

    def _busy(self, on):
        self.progress.setVisible(on)
        self.search_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        self.map_preview_btn.setEnabled(
            (not on) and bool(self._search_result and
                              (self._search_result.get("pre") or
                               self._search_result.get("post"))))

    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _warn(self, text):
        self.iface.messageBar().pushWarning("PlanetScope", text)

    def teardown(self):
        if self._preview_reply is not None:
            try:
                self._preview_reply.abort()
            except RuntimeError:
                pass
            self._preview_reply = None
        for reply in self._tile_replies + self._gallery_replies:
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._tile_replies = []
        self._gallery_replies = []
        if self.task is not None:
            self.task.cancel()
