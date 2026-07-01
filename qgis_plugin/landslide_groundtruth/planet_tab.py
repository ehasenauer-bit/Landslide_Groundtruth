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
    QScrollArea, QGridLayout, QToolButton, QFrame, QSlider,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsRectangle,
    QgsNetworkAccessManager, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsField, QgsFeature, QgsFillSymbol,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QVariant

from .task import PipelineTask

# Planet's Data API tile service (undocumented, but stable — it's what Planet
# Explorer's "Add preview to map" relies on). POST scene ids to get a tile hash,
# then stream XYZ tiles from a per-hash layer. {0} is a subdomain shard 0-3.
TILE_HASH_URL = "https://tiles.planet.com/data/v1/layers"
TILE_XYZ_URL = "https://tiles{shard}.planet.com/data/v1/layers/{hash}/{{z}}/{{x}}/{{y}}"
ITEM_TYPE = "PSScene"

# Planet account login (mirrors the official qgis-planet-plugin login tab): POST
# {email, password} and get back a JWT whose base64url payload carries the user's
# api_key. Same endpoint the `planet` SDK's ClientV1.login() calls. We do it in
# QGIS's Python via Qt networking (no `planet` SDK import needed here), then feed
# the recovered key into the same PL_API_KEY path the rest of the tab already uses.
PLANET_LOGIN_URL = "https://api.planet.com/v0/auth/login"

# status-line colours shared by the login panel (green ok / red fail / amber hint)
STATUS_COLORS = {
    "success": "#2e7d32", "error": "#c62828",
    "warn": "#e65100", "info": "palette(mid)",
}

# Planet's brand palette, pulled from the official qgis-planet-plugin (its logo
# SVGs + .ui stylesheets): teal accent, navy header, near-black ink. Used to give
# this tab the Planet Explorer look instead of the plain QGIS widget theme.
TEAL = "#009da5"        # primary accent / call-to-action buttons (rgb 0,157,165)
TEAL_DARK = "#0b7c82"   # button hover / pressed
NAVY = "#1e3967"        # header banner + table header background
INK = "#27282a"         # near-black (rgb 39,40,42)

# One stylesheet applied to the whole PlanetTab (and only it — the Sentinel-2 /
# Landsat tab keeps the native QGIS look). Accent colours are explicit so they
# read on both light and dark QGIS themes; structural colours use the palette().
THEME_QSS = f"""
QLabel#section {{ color: {TEAL}; font-weight: 600; padding-top: 2px; }}
QPushButton#primary {{
    background-color: {TEAL}; color: white; border: none;
    border-radius: 4px; padding: 6px 14px; font-weight: 600;
}}
QPushButton#primary:hover {{ background-color: {TEAL_DARK}; }}
QPushButton#primary:disabled {{ background-color: #9cc7ca; color: #eef4f4; }}
QPushButton {{ border-radius: 4px; padding: 5px 10px; }}
QLineEdit, QComboBox, QDoubleSpinBox, QDateTimeEdit {{
    border: 1px solid palette(mid); border-radius: 4px; padding: 3px 6px;
}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus {{
    border: 1px solid {TEAL};
}}
QToolButton {{
    border: 1px solid palette(mid); border-radius: 6px;
    padding: 4px; background: palette(base);
}}
QToolButton:hover {{ border: 1px solid {TEAL}; }}
QHeaderView::section {{
    background: {NAVY}; color: white; padding: 4px 6px;
    border: none; font-weight: 600;
}}
QProgressBar {{ border: 1px solid palette(mid); border-radius: 4px; text-align: center; }}
QProgressBar::chunk {{ background-color: {TEAL}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: palette(mid); border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {TEAL}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEAL}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
"""

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
        self._login_reply = None         # in-flight Planet login POST
        self._preview_reply = None       # in-flight browse-thumbnail request
        self._preview_pix = None         # last loaded thumbnail, kept for rescaling
        self._tile_replies = []          # in-flight tile-hash POSTs
        self._preview_layers = []        # XYZ preview layers added to the map
        self._footprint_layers = []      # scene-footprint vector layers on the map
        self._gallery_replies = []       # in-flight quicklook-thumbnail requests
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("planetTab")
        self.setStyleSheet(THEME_QSS)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(self._build_header())

        intro = QLabel(
            "Preview scenes at full resolution straight on the map — no order "
            "placed, no quota used. (Ordering into the review package comes later.)")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        # --- Planet account login (drop-down) ---
        root.addWidget(self._build_login_box())

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
        # Cloud cover as a teal slider, echoing Planet Explorer's slider filters.
        self.cloud_slider = QSlider(Qt.Horizontal)
        self.cloud_slider.setRange(0, 100)
        self.cloud_slider.setValue(80)
        self.cloud_slider.setToolTip(
            "Maximum WHOLE-SCENE cloud cover to consider. Scene-wide metric, not "
            "your AOI — per-pixel UDM2 masking still applies, so a high value "
            "surfaces scenes clear over your point but cloudy elsewhere (what Planet "
            "Explorer shows).")
        self.cloud_lbl = QLabel("80%")
        self.cloud_lbl.setFixedWidth(38)
        self.cloud_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.cloud_slider.valueChanged.connect(
            lambda v: self.cloud_lbl.setText(f"{v}%"))
        crow = QHBoxLayout()
        crow.addWidget(self.cloud_slider, 1)
        crow.addWidget(self.cloud_lbl)
        form.addRow("Max cloud", self._wrap(crow))

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

        root.addLayout(form)

        # --- buttons ---
        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("Search (free)")
        self.search_btn.setObjectName("primary")
        self.search_btn.setToolTip(
            "Free Data API search for candidate before/after PlanetScope scenes. No "
            "orders placed, no quota used.")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map (full-res)")
        self.map_preview_btn.setObjectName("primary")
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
        tl.addWidget(self._section("Candidate scenes  (★ = nearest on each side)"))
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
        gl.addWidget(self._section("Quicklook gallery  (click a thumbnail to select its scene)"))
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
        pl.addWidget(self._section("Scene preview  (browse image)"))
        self.preview = QLabel("Search, then select a scene to preview its browse image.")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("QLabel { background: palette(base); }")
        pl.addWidget(self.preview, 1)
        split.addWidget(previewbox)

        logbox = QWidget()
        lo = QVBoxLayout(logbox)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.addWidget(self._section("Log"))
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

    def _build_header(self):
        """A Planet-branded banner: navy bar with the lowercase 'planet' wordmark
        (teal dot) + a subtitle, mirroring the Planet Explorer header."""
        bar = QFrame()
        bar.setObjectName("planetHeader")
        bar.setStyleSheet(
            f"QFrame#planetHeader {{ background: {NAVY}; border-radius: 6px; }}"
            "QFrame#planetHeader QLabel { background: transparent; color: white; }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 10, 14, 10)
        col = QVBoxLayout()
        col.setSpacing(0)
        word = QLabel(
            f'<span style="font-size:20px; font-weight:700; letter-spacing:1px;">'
            f'planet<span style="color:{TEAL};">.</span></span>')
        sub = QLabel("PlanetScope · browse & full-resolution preview")
        sub.setStyleSheet("color:#b9c4da;")
        col.addWidget(word)
        col.addWidget(sub)
        lay.addLayout(col)
        lay.addStretch(1)
        return bar

    def _section(self, text):
        """A section heading styled in Planet teal (see THEME_QSS QLabel#section)."""
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    # ---------- Planet account login (drop-down) ----------
    def _build_login_box(self):
        """A collapsible 'Planet account' panel modelled on the official
        qgis-planet-plugin login tab: sign in with your Planet email + password
        (which exchanges them for your API key), or paste an API key directly. The
        recovered key flows into the SAME PL_API_KEY / QgsSettings path everything
        else on this tab already uses, so login is just a friendlier front door to
        the existing key field."""
        box = QgsCollapsibleGroupBox("Planet account")
        box.setSaveCollapsedState(False)
        # start collapsed if a key is already in hand, else open to prompt sign-in
        box.setCollapsed(bool(self._api_key()))
        self.login_box = box
        form = QFormLayout(box)

        info = QLabel(
            'Sign in with your Planet account to search PlanetScope and stream '
            'full-res previews. No account? '
            '<a href="https://www.planet.com/explorer/">planet.com</a>. You can also '
            'paste an API key directly instead of signing in.')
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(info)

        self.user_edit = QLineEdit(
            self.settings.value("landslide/planet_user", "", type=str))
        self.user_edit.setPlaceholderText("email")
        self.user_edit.returnPressed.connect(self._planet_login)
        form.addRow("Email", self.user_edit)

        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("password")
        self.pass_edit.returnPressed.connect(self._planet_login)
        form.addRow("Password", self.pass_edit)

        btns = QHBoxLayout()
        self.login_btn = QPushButton("Log in")
        self.login_btn.setObjectName("primary")
        self.login_btn.clicked.connect(self._planet_login)
        self.logout_btn = QPushButton("Log out")
        self.logout_btn.clicked.connect(self._planet_logout)
        btns.addWidget(self.login_btn)
        btns.addWidget(self.logout_btn)
        form.addRow(self._wrap(btns))

        # API key: the manual alternative to email/password, and where a successful
        # login drops the recovered key. This is the field _api_key()/_collect() read.
        self.key_edit = QLineEdit(self._api_key())
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText("Planet API key (or set PL_API_KEY)")
        self.key_edit.setToolTip(
            "Needed for the full-res map preview and browse thumbnails, and passed "
            "to the search subprocess. Filled in automatically when you log in; "
            "saved to QGIS settings. Leave blank to use the PL_API_KEY environment "
            "variable or a `planet auth login` session.")
        form.addRow("API key", self.key_edit)

        self.login_status = QLabel()
        self.login_status.setWordWrap(True)
        form.addRow("Status", self.login_status)
        self._refresh_login_state()
        return box

    def _set_login_status(self, text, tone="info"):
        self.login_status.setText(text)
        self.login_status.setStyleSheet(
            f"QLabel {{ color: {STATUS_COLORS.get(tone, 'palette(mid)')}; }}")

    def _refresh_login_state(self):
        """Reflect whether a key is in hand in the box title + Log out button."""
        have = bool(self._api_key())
        self.logout_btn.setEnabled(have)
        user = self.settings.value("landslide/planet_user", "", type=str)
        if have:
            self.login_box.setTitle(
                f"Planet account — signed in{f' ({user})' if user else ''}")
        else:
            self.login_box.setTitle("Planet account — sign in")

    def _planet_login(self):
        if self._login_reply is not None:
            return                       # a login is already in flight
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text()
        if not user or not pw:
            self._set_login_status(
                "Enter your Planet email and password (or paste an API key).", "warn")
            return
        self._set_login_status("Signing in…", "info")
        self.login_btn.setEnabled(False)
        body = QByteArray(json.dumps({"email": user, "password": pw}).encode())
        req = QNetworkRequest(QUrl(PLANET_LOGIN_URL))
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        reply = QgsNetworkAccessManager.instance().post(req, body)
        self._login_reply = reply
        reply.finished.connect(lambda r=reply, u=user: self._planet_login_done(r, u))

    def _planet_login_done(self, reply, user):
        if reply is not self._login_reply:
            reply.deleteLater()
            return
        self._login_reply = None
        self.login_btn.setEnabled(True)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        ok = reply.error() == QNetworkReply.NoError and status == 200
        data = bytes(reply.readAll())
        reply.deleteLater()
        if not ok:
            if status in (401, 403):
                self._set_login_status(
                    "Sign-in failed — check your email and password.", "error")
            else:
                self._set_login_status(
                    f"Sign-in failed (HTTP {status or '—'}).", "error")
            return
        api_key = self._api_key_from_jwt(data)
        if not api_key:
            self._set_login_status(
                "Signed in, but no API key came back — try pasting a key.", "error")
            return
        self.key_edit.setText(api_key)
        self.settings.setValue("landslide/planet_api_key", api_key)
        self.settings.setValue("landslide/planet_user", user)
        os.environ["PL_API_KEY"] = api_key
        self.pass_edit.clear()
        self._set_login_status(f"✓ Signed in as {user}.", "success")
        self._refresh_login_state()
        self.login_box.setCollapsed(True)

    @staticmethod
    def _api_key_from_jwt(data):
        """Pull `api_key` out of the JWT Planet's /v0/auth/login returns.

        The body is a bare JWT string (header.payload.signature); the middle part
        is a base64url-encoded JSON payload carrying the account's api_key. Mirrors
        the parsing in the `planet` SDK's ClientV1.login()."""
        try:
            jwt = data.decode("utf-8").strip().strip('"')
            payload = jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)   # restore base64 padding
            obj = json.loads(base64.urlsafe_b64decode(payload.encode()))
            return obj.get("api_key")
        except (ValueError, IndexError, UnicodeDecodeError):
            return None

    def _planet_logout(self):
        self.key_edit.clear()
        self.pass_edit.clear()
        self.settings.remove("landslide/planet_api_key")
        os.environ.pop("PL_API_KEY", None)
        self._set_login_status("Signed out.", "info")
        self._refresh_login_state()

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
            "--max-cloud", str(self.cloud_slider.value()),
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
        for attr in ("_preview_reply", "_login_reply"):
            reply = getattr(self, attr, None)
            if reply is not None:
                try:
                    reply.abort()
                except RuntimeError:
                    pass
                setattr(self, attr, None)
        for reply in self._tile_replies + self._gallery_replies:
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._tile_replies = []
        self._gallery_replies = []
        if self.task is not None:
            self.task.cancel()
