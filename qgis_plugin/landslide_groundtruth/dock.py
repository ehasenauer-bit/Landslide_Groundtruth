"""The dock panel: location pick, date, pre/post sliders, source preference, run."""
import base64
import json
import math
import os
import platform
import tempfile
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QDateTime, QUrl, QUrlQuery, QSize, QVariant
from qgis.PyQt.QtGui import QDoubleValidator, QColor, QBrush, QPixmap, QIcon
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QComboBox, QSlider, QDoubleSpinBox, QDateTimeEdit, QProgressBar,
    QPlainTextEdit, QFileDialog, QCheckBox, QTableWidget,
    QTableWidgetItem, QSplitter, QToolButton, QScrollArea, QGridLayout,
    QTabWidget,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsSettings,
    QgsRectangle, QgsNetworkAccessManager,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsField, QgsFeature, QgsGeometry, QgsPointXY, QgsFillSymbol,
    QgsMarkerSymbol,
)
from qgis.gui import QgsDockWidget, QgsCollapsibleGroupBox

from .task import PipelineTask
from . import layer_group as lg
from .flow_layout import FlowRow

# label -> --prefer value. PlanetScope lives in its own tab now (separate
# Data/Orders/Tiles system); this tab covers only the Planetary Computer STAC
# sources — Sentinel-2 and Landsat.
SOURCES = [
    ("Auto (Sentinel-2 → Landsat)", "auto"),
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
    ("swir_falsecolor", "SWIR false colour (12-11-4)",
     "SWIR2-SWIR1-Red (Sentinel-2 B12-B11-B4 / Landsat swir2-swir1-red): snow and "
     "clean ice read dark blue, fresh rock/ice-avalanche debris reads bright "
     "orange/brown — the highest-contrast combo for spotting debris on a glacier. "
     "PlanetScope has no SWIR, so this is Sentinel-2/Landsat only."),
    ("ndvi", "NDVI (pre & post)",
     "Raw NDVI before and after — the inputs behind dNDVI, for thresholding by eye."),
    ("dndvi", "NDVI change (dNDVI)",
     "pre→post NDVI change; vegetation loss is a strong negative."),
    ("dndsi", "NDSI change (dNDSI)",
     "pre→post snow-index change; new dark debris on snow/ice reads a strong "
     "negative — the debris-on-glacier signal where there's no vegetation to lose. "
     "Sentinel-2/Landsat only (needs SWIR)."),
    ("dbright", "Brightness change (dBrightness)",
     "pre→post broadband brightness/albedo change; bare rock/soil reads positive."),
]

# sensor code (from result.json) -> human-readable name shown in the run banner
SENSOR_LABEL = {
    "planet": "PlanetScope (~3 m)",
    "s2": "Sentinel-2 (~10 m)",
    "landsat": "Landsat (~30 m)",
}

# Short sensor tag for the layer-tree folder name (SENSOR_LABEL is too long there).
SENSOR_TAG = {"planet": "PlanetScope", "s2": "S2", "landsat": "Landsat"}

# Which product each run-output file is, keyed off the `kind` token review_package
# bakes into the filename. Ordered most-specific first so "dndvi" wins over "ndvi".
# The value becomes the folder's product suffix, e.g. "S2 7-20/7-21 NDVI".
CORE_PRODUCTS = [
    ("dndvi", "dNDVI"), ("dndsi", "dNDSI"), ("dbright", "dBright"),
    ("highlight", "HONC"), ("falsecolor", "False-color"),
    ("swir", "SWIR"), ("ndvi", "NDVI"), ("rgb", "TC"),
]


def _core_product(basename):
    """Product tag for a run-output filename, or '' if none matches (e.g. the
    predicted-epicentre point.gpkg, which then sits at the top level of the run's
    own folder rather than in a product subfolder)."""
    low = basename.lower()
    for token, tag in CORE_PRODUCTS:
        if f"_{token}_" in low or low.endswith("_" + token):
            return tag
    return ""


def _first_date(dates):
    """Earliest acquisition date in a side's list, for the folder name; '' if none."""
    uniq = sorted({d for d in (dates or []) if d})
    return uniq[0] if uniq else ""

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
# the raw surface-reflectance bands, so we control the stretch instead of
# inheriting a pre-baked 'visual' TCI, which clips bright snow/ice to flat white.
#
# The render reproduces the project's "Highlight Optimized Natural Color" look
# (see review_package): a cube-root tone curve cbrt(0.6 * reflectance). In data-
# API terms that's gamma 3 (output = input**(1/3)) applied to reflectance scaled
# by 0.6. Same tone curve for every source; only the band names and the rescale
# (because each collection stores reflectance differently) change per source.
PC_DATA_URL = "https://planetarycomputer.microsoft.com/api/data/v1"
_HIGHLIGHT_FORMULA = "gamma RGB 3.0, saturation 1.2"

# Per-source highlight render config for the data API. `query` is the assets +
# colour_formula + rescale (+ unscale for Landsat), percent-encoded ONCE here so
# the call site appends it verbatim. PlanetScope is absent: it has no single
# streamable COG, so it can't be rendered on the map / from the data API.
#   Sentinel-2 L2A: SR is uint16 reflectance×10000 (no offset), so the rescale is
#     in raw DN — 16667 = 10000 / 0.6 bakes in the 0.6 scale with white headroom.
#   Landsat C2 L2: SR carries scale/offset (r = DN×2.75e-5 − 0.2); unscale=true so
#     the rescale below is in reflectance units (0..1/0.6), mirroring S2. Tune the
#     rescale here if Landsat scenes read too dark/bright.
HIGHLIGHT_RENDER = {
    "Sentinel-2": {
        "collection": "sentinel-2-l2a",
        "query": ("assets=B04&assets=B03&assets=B02"
                  f"&color_formula={quote(_HIGHLIGHT_FORMULA)}"
                  "&rescale=0,16667&nodata=0"),
    },
    "Landsat": {
        "collection": "landsat-c2-l2",
        "query": ("assets=red&assets=green&assets=blue&unscale=true"
                  f"&color_formula={quote(_HIGHLIGHT_FORMULA)}"
                  "&rescale=0,1.6667&nodata=0"),
    },
}
# sources renderable on the map / in the gallery via the data API (preview order)
STREAMABLE = ("Sentinel-2", "Landsat")

# muted text for table rows the run will NOT composite (ranked below the cutoff)
MUTED_FG = QColor(120, 120, 120)

# QGIS network-request timeout (ms) while previewing PC tiler layers: the on-
# demand tiler can be slow to render the first tiles of a fresh scene, and the
# 60 s default aborts them ("Network request … timed out"). 3 minutes.
NETWORK_TIMEOUT_MS = 180000

# NASA Earthdata (URS) login, modelled on the qgis-nasa-earthdata-plugin settings
# tab. We validate a username/password with a Basic-auth GET to the URS token API
# (200 = valid, 401/403 = bad) and, on success, persist them to ~/.netrc + the
# EARTHDATA_* env vars — the exact form earthaccess / rasterio / the run subprocess
# read automatically. URS host is the machine name used in the .netrc entry.
EARTHDATA_TOKENS_URL = "https://urs.earthdata.nasa.gov/api/users/tokens"
EARTHDATA_HOST = "urs.earthdata.nasa.gov"
EARTHDATA_REGISTER_URL = "https://urs.earthdata.nasa.gov/"

# status-line colours shared by the login panel (green ok / red fail / amber hint)
STATUS_COLORS = {
    "success": "#2e7d32", "error": "#c62828",
    "warn": "#e65100", "info": "palette(mid)",
}

# Responsive text: the dock rescales its base font with its own size so the panel
# stays readable whether it's a cramped side dock or a large floating window. The
# scale is the geometric mean of the width/height ratios vs this baseline (so both
# dimensions contribute — a change in the aspect ratio still moves it), clamped to
# [MIN, MAX] so text never becomes unreadably small or cartoonishly large.
FONT_BASE_W, FONT_BASE_H = 380, 720
FONT_SCALE_MIN, FONT_SCALE_MAX = 0.8, 1.5


class LandslideDock(QgsDockWidget):
    def __init__(self, iface):
        super().__init__("Landslide Ground-Truthing")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.task = None
        self.settings = QgsSettings()
        self._ed_reply = None        # in-flight Earthdata credential-check request
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
        self._gallery_replies = []   # in-flight quicklook-thumbnail requests
        self._footprint_layers = []  # scene-footprint vector layers on the map
        # baseline font the responsive rescaling is measured from (point size if
        # the theme uses one, else pixel size); _last_font_scale guards against
        # re-applying an unchanged size on every resize tick.
        base_font = self.font()
        self._base_font_pt = base_font.pointSizeF()
        self._base_font_px = base_font.pixelSize()
        self._last_font_scale = None
        self.setWidget(self._wrap_scrollable(self._build_dock()))
        self._init_project_state()

    # ---------- per-project persistence ----------
    def _init_project_state(self):
        """Bind the dock's inputs to the CURRENT QGIS project, so every project
        keeps its own AOI/date/options (see project_state.py). Restores straight
        away because the dock is usually opened after the project, then follows
        the project signals: written into the .qgz as it saves, read back when
        another project is opened, reset to defaults on File > New."""
        from .project_state import ProjectState
        self.project_state = ProjectState(self)
        project = QgsProject.instance()
        project.writeProject.connect(self.project_state.save)
        project.readProject.connect(self.project_state.restore)
        project.cleared.connect(self.project_state.restore)
        self.project_state.restore()

    def _wrap_scrollable(self, inner):
        """Put the whole dock inside a scroll area so the panel can always be read
        top-to-bottom and left-to-right, even when the docked area is smaller than
        the content (narrow side dock, small screen, many stacked controls).

        setWidgetResizable(True) lets the content fill the viewport when there's
        room; a minimum width forces a HORIZONTAL scrollbar (rather than crushing
        the form/table) once the dock is narrower than that, and the VERTICAL
        scrollbar appears whenever the stacked controls are taller than the dock."""
        inner.setMinimumWidth(360)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(inner)
        return scroll

    # ---------- top-level layout: shared Environment + tabbed sources ----------
    def _build_dock(self):
        """Shared Environment header over a QTabWidget: a Sentinel-2/Landsat tab
        (the Planetary Computer STAC pipeline) and a PlanetScope tab (Planet's own
        Data/Orders/Tiles system). Environment (venv/project/out) is shared because
        both tabs launch the SAME venv subprocess."""
        from .planet_tab import PlanetTab
        from .sar_tab import SarTab
        from .viewer3d_tab import Viewer3DTab
        from .volume_tab import VolumeTab
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._build_env_box())

        tabs = QTabWidget()
        tabs.addTab(self._build_ui(), "Sentinel-2 / Landsat")
        self.planet_tab = PlanetTab(self)
        tabs.addTab(self.planet_tab, "PlanetScope")
        self.sar_tab = SarTab(self)
        tabs.addTab(self.sar_tab, "SAR (Sentinel-1)")
        self.viewer3d_tab = Viewer3DTab(self)
        tabs.addTab(self.viewer3d_tab, "3D viewer")
        # The one tab that consumes the review step's output rather than
        # producing imagery: it measures the polygon you digitized and converts
        # scar area to volume. Runs entirely in-process — no venv subprocess.
        self.volume_tab = VolumeTab(self)
        tabs.addTab(self.volume_tab, "Volume from area")
        outer.addWidget(tabs, 1)
        return container

    def _build_env_box(self):
        """Shared environment settings (paths to the venv + project + output).
        Collapsible + collapsed by default: these are set once, then forgotten.
        Read by BOTH tabs (see _collect and PlanetTab)."""
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
        return env

    # ---------- Sentinel-2 / Landsat tab ----------
    def _build_ui(self):
        w = QWidget()
        root = QVBoxLayout(w)

        # --- NASA Earthdata login (drop-down) ---
        root.addWidget(self._build_earthdata_box())

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
        self.radius_spin.setValue(5.0)
        self.radius_spin.setSuffix(" km")
        form.addRow("Search radius", self.radius_spin)

        self.dt_edit = QDateTimeEdit(QDateTime.currentDateTimeUtc())
        self.dt_edit.setCalendarPopup(True)
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("Event time (UTC)", self.dt_edit)

        # --- add the entered location to the map (drop-down + button) ---
        # "Add point" drops a marker at the lat/lon; "Add search area" draws a
        # translucent red circle of the search radius. Both are memory layers, so
        # nothing is written to disk. Placed right under the event time so the
        # location can be sanity-checked on the canvas before searching.
        self.point_area_combo = QComboBox()
        self.point_area_combo.addItem("Add point", "point")
        self.point_area_combo.addItem("Add search area", "area")
        self.point_area_combo.setToolTip(
            "Add a QGIS layer for the entered location: a marker at the "
            "latitude/longitude, or a translucent red circle of the search "
            "radius.")
        add_pa_btn = QPushButton("Add")
        add_pa_btn.setFixedWidth(56)
        add_pa_btn.clicked.connect(self._add_point_or_area)
        pa_row = QHBoxLayout()
        pa_row.addWidget(self.point_area_combo, 1)
        pa_row.addWidget(add_pa_btn)
        form.addRow("Point and area", pa_row)

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

        # Cloud filtering is ON by default at 50%: eo:cloud_cover is a whole-scene
        # metric over a 110 km granule, so it says nothing about your few-km AOI,
        # but a 50% cap trims the mostly-clouded acquisitions that swamp a long
        # list while still keeping most of the scenes worth eyeballing. Untick the
        # box to see every scene, clouds and all, and pick purely by thumbnail.
        self.cloud_filter_check = QCheckBox("Hide scenes cloudier than")
        self.cloud_filter_check.setChecked(True)
        self.cloud_filter_check.setToolTip(
            "On (default): drop scenes whose WHOLE-SCENE cloud cover exceeds the "
            "value on the right. That is a scene-wide metric, not your AOI — a "
            "scene can be 60% cloudy overall and still clear over your point, so "
            "raise the cap or untick this if the list looks too thin.\n"
            "Off: NO cloud filtering — every scene in the window is listed, clouds "
            "and all, so you can see the clouds and pick by eye.\n"
            "Either way this only chooses WHICH scenes are listed: a Run downloads "
            "the scenes as acquired, with the cloud left in. No pixels are masked "
            "out for cloud, so you never get holes in the imagery.")
        self.cloud_spin = QDoubleSpinBox()
        self.cloud_spin.setRange(0.0, 100.0)
        self.cloud_spin.setDecimals(0)
        self.cloud_spin.setSingleStep(5.0)
        self.cloud_spin.setValue(50.0)
        self.cloud_spin.setSuffix(" %")
        self.cloud_spin.setEnabled(True)
        self.cloud_spin.setToolTip(self.cloud_filter_check.toolTip())
        self.cloud_filter_check.toggled.connect(self.cloud_spin.setEnabled)
        cloud_row = QHBoxLayout()
        cloud_row.addWidget(self.cloud_filter_check)
        cloud_row.addWidget(self.cloud_spin)
        cloud_row.addStretch(1)
        form.addRow("Cloud filter", cloud_row)
        root.addLayout(form)

        # --- which review layers to export ---
        # These are OUTPUT products (renderings), not satellite acquisitions — the
        # "scene" word is reserved for the Candidate scenes table below. Each checked
        # product is written by the run; uncheck what you won't use to download less.
        # The predicted-epicentre point layer is always included. Expanded by default
        # so the choice is visible (it's a primary control).
        scenes_box = QgsCollapsibleGroupBox("Layers to export")
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

        # (PlanetScope coverage/quality toggles moved to the PlanetScope tab.)

        # --- search (free dry-run) / run / cancel ---
        btn_row = FlowRow()
        self.search_btn = QPushButton("Search / Preview")
        self.search_btn.setToolTip(
            "Free dry-run: list the candidate before/after scenes per source, "
            "nearest the event date first, and show them below with their browse "
            "images. Nothing is downloaded and no order is placed — use it to check "
            "coverage and pick the scenes before a full Run. Scenes over the cloud "
            "cap are hidden by default; untick the cloud filter to list them too.")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map")
        self.map_preview_btn.setToolTip(
            "Render the TICKED scene(s) in Highlight Optimized Natural Color "
            "straight onto the QGIS canvas (clipped to the AOI) — no download, no "
            "order. Tick the scenes you want in the table, or double-click a row to "
            "preview just that one; with nothing ticked the ★ best before & after "
            "scenes are used. Works for Sentinel-2 and Landsat; run Search / "
            "Preview first to find the scenes. Toggle the layers' visibility to "
            "compare before vs after. (PlanetScope has its own tab.)")
        self.map_preview_btn.setEnabled(False)
        self.map_preview_btn.clicked.connect(self._preview_on_map)
        self.run_btn = QPushButton("Run")
        self.run_btn.setToolTip(
            "Download and export the TICKED scenes as the review layers checked "
            "above. Exactly the scenes you ticked are used — the ★ suggestion is "
            "not added, and ticking only one side (post alone, say) runs that side "
            "on its own, minus the change rasters that need both. With the table "
            "empty (no Search yet) the run falls back to picking scenes itself.")
        self.run_btn.clicked.connect(self._run)
        f = self.run_btn.font(); f.setBold(True); self.run_btn.setFont(f); self.run_btn.setDefault(True)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.search_btn)
        btn_row.addWidget(self.map_preview_btn)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        root.addWidget(btn_row)

        # draw each candidate scene's footprint on the map (off by default)
        self.footprint_check = QCheckBox("Show scene footprints on map")
        self.footprint_check.setToolTip(
            "Draw scene footprint outlines on the canvas (before = blue, after = "
            "green) plus the search AOI box, so you can see whether a scene "
            "actually covers the AOI or leaves the epicentre in a diagonal nodata "
            "gap. With table rows selected, only THOSE scenes' footprints are "
            "drawn — click a row to isolate its granule, Ctrl/Shift-click for "
            "several, click in empty table space to show all candidates again. Off "
            "by default; refreshes after each Search / Preview.")
        self.footprint_check.toggled.connect(self._on_footprint_toggle)
        root.addWidget(self.footprint_check)

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
        scenes_lbl = QLabel(
            "Candidate scenes  (tick the scenes to download; ★ = best-ranked, only "
            "a suggestion)")
        scenes_lbl.setToolTip(
            "Ticks choose the scenes: 'Preview on map' renders the TICKED rows and "
            "a Run downloads EXACTLY them — nothing starts ticked and nothing is "
            "added for you. Tick one scene per side for a plain before/after pair, "
            "several on a side to median-composite them, or only one side (e.g. "
            "post alone, when nothing usable was acquired before the event) to get "
            "just that side's imagery without the change rasters.\n\n"
            "Selecting a row (click; Ctrl/Shift-click for several) is separate: it "
            "drives the browse-image preview below and isolates that scene's "
            "footprint on the map. Double-click a row to preview just that scene.\n\n"
            "Hand-picking works for Sentinel-2 / Landsat; PlanetScope has its own "
            "tab.")
        scenes_box.addWidget(scenes_lbl)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Side", "Date (UTC)", "Gap (d)", "Cloud %", "AOI %", "Source", "Scene ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Extended, like the PlanetScope tab: select one scene to isolate its
        # footprint and browse image, Ctrl/Shift-click to compare several.
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.itemDoubleClicked.connect(self._preview_row_on_map)
        scenes_box.addWidget(self.table)
        out_split.addWidget(scenes)

        # quicklook gallery: every candidate's browse thumbnail at once (before +
        # after) so you can scan for the cloud-free scene over the AOI in one
        # glance, instead of clicking the table row by row. Click a tile to select
        # its scene (drives the big preview + Preview on map).
        gallerybox = QWidget()
        g_layout = QVBoxLayout(gallerybox)
        g_layout.setContentsMargins(0, 0, 0, 0)
        g_layout.addWidget(QLabel("Quicklook gallery (click a thumbnail to select its scene)"))
        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_inner = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery_inner)
        self.gallery_layout.setAlignment(Qt.AlignTop)
        self.gallery_scroll.setWidget(self.gallery_inner)
        g_layout.addWidget(self.gallery_scroll, 1)
        out_split.addWidget(gallerybox)

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
        out_split.setStretchFactor(1, 3)   # gallery
        out_split.setStretchFactor(2, 2)   # preview
        out_split.setStretchFactor(3, 2)   # log
        scenes.setMinimumHeight(120)
        gallerybox.setMinimumHeight(0)     # drag closed when you don't need it
        previewbox.setMinimumHeight(0)     # drag closed when you don't need it
        logbox.setMinimumHeight(80)
        root.addWidget(out_split, 1)
        return w

    # ---------- NASA Earthdata login (drop-down) ----------
    def _build_earthdata_box(self):
        """A collapsible 'NASA Earthdata Login' panel modelled on the
        qgis-nasa-earthdata-plugin settings tab. The Sentinel-2 / Landsat pipeline
        here streams from Microsoft's Planetary Computer (no login needed), so these
        credentials are for NASA Earthdata-hosted products: enter a username +
        password, we validate them against NASA URS and, on success, persist them to
        ~/.netrc and the EARTHDATA_* env vars so any Earthdata download (earthaccess,
        rasterio, the run subprocess) can authenticate. Collapsed by default."""
        box = QgsCollapsibleGroupBox("NASA Earthdata Login")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        self.earthdata_box = box
        form = QFormLayout(box)

        info = QLabel(
            'Optional: store NASA Earthdata credentials for NASA-hosted imagery '
            'downloads. The Sentinel-2 / Landsat search above uses the Microsoft '
            'Planetary Computer and needs no login. Register at '
            f'<a href="{EARTHDATA_REGISTER_URL}">urs.earthdata.nasa.gov</a>.')
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(info)

        self.ed_user_edit = QLineEdit(
            self.settings.value("landslide/earthdata_user", "", type=str))
        self.ed_user_edit.setPlaceholderText("NASA Earthdata username")
        form.addRow("Username", self.ed_user_edit)

        self.ed_pass_edit = QLineEdit()
        self.ed_pass_edit.setEchoMode(QLineEdit.Password)
        self.ed_pass_edit.setPlaceholderText("NASA Earthdata password")
        self.ed_pass_edit.returnPressed.connect(self._earthdata_login)
        form.addRow("Password", self.ed_pass_edit)

        row = FlowRow()
        self.ed_login_btn = QPushButton("Test && save credentials")
        self.ed_login_btn.setToolTip(
            "Check the username/password against NASA URS. If valid, save them to "
            "~/.netrc and the EARTHDATA_* environment variables for downloads.")
        self.ed_login_btn.clicked.connect(self._earthdata_login)
        self.ed_check_btn = QPushButton("Check .netrc")
        self.ed_check_btn.setToolTip(
            "Report whether ~/.netrc already holds a NASA Earthdata entry.")
        self.ed_check_btn.clicked.connect(self._earthdata_check_netrc)
        row.addWidget(self.ed_login_btn)
        row.addWidget(self.ed_check_btn)
        form.addRow(row)

        self.ed_status = QLabel()
        self.ed_status.setWordWrap(True)
        form.addRow("Status", self.ed_status)
        return box

    def _set_ed_status(self, text, tone="info"):
        self.ed_status.setText(text)
        self.ed_status.setStyleSheet(
            f"QLabel {{ color: {STATUS_COLORS.get(tone, 'palette(mid)')}; }}")

    def _earthdata_login(self):
        if self._ed_reply is not None:
            return                       # a check is already in flight
        user = self.ed_user_edit.text().strip()
        pw = self.ed_pass_edit.text()
        if not user or not pw:
            self._set_ed_status(
                "Enter your NASA Earthdata username and password.", "warn")
            return
        self._set_ed_status("Testing credentials against NASA URS…", "info")
        self.ed_login_btn.setEnabled(False)
        req = QNetworkRequest(QUrl(EARTHDATA_TOKENS_URL))
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.setRawHeader(b"Authorization", ("Basic " + token).encode())
        reply = QgsNetworkAccessManager.instance().get(req)
        self._ed_reply = reply
        reply.finished.connect(
            lambda r=reply, u=user, p=pw: self._earthdata_login_done(r, u, p))

    def _earthdata_login_done(self, reply, user, password):
        if reply is not self._ed_reply:
            reply.deleteLater()
            return
        self._ed_reply = None
        self.ed_login_btn.setEnabled(True)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        err = reply.error()
        reply.deleteLater()
        if err == QNetworkReply.NoError and status == 200:
            try:
                self._earthdata_save_netrc(user, password)
            except OSError as e:
                self._set_ed_status(
                    f"Credentials valid, but writing ~/.netrc failed: {e}", "error")
                return
            self.settings.setValue("landslide/earthdata_user", user)
            os.environ["EARTHDATA_USERNAME"] = user
            os.environ["EARTHDATA_PASSWORD"] = password
            self.ed_pass_edit.clear()
            self._set_ed_status(
                f"✓ Credentials valid — saved for {user} (~/.netrc + env vars).",
                "success")
        elif status in (401, 403):
            self._set_ed_status(
                "✗ Invalid NASA Earthdata username or password.", "error")
        else:
            self._set_ed_status(
                f"Could not verify (HTTP {status or '—'}); nothing saved. "
                "Check your connection and try again.", "error")

    def _earthdata_save_netrc(self, username, password):
        """Write the URS entry to ~/.netrc, replacing any existing one and keeping
        other machines' entries (mirrors the reference plugin). chmod 600 on POSIX
        so the stored password isn't world-readable."""
        from pathlib import Path
        netrc_path = Path.home() / ".netrc"
        existing = ""
        if netrc_path.exists():
            try:
                existing = netrc_path.read_text()
            except OSError:
                existing = ""
        # keep every line except the old urs.earthdata.nasa.gov machine block
        kept = []
        skip = False
        for line in (existing.splitlines() if existing.strip() else []):
            if line.strip().startswith("machine"):
                skip = EARTHDATA_HOST in line
            if not skip:
                kept.append(line)
        entry = (f"machine {EARTHDATA_HOST}\n"
                 f"    login {username}\n    password {password}\n")
        head = "\n".join(kept).strip()
        netrc_path.write_text((head + "\n\n" if head else "") + entry)
        if platform.system() != "Windows":
            import stat
            os.chmod(netrc_path, stat.S_IRUSR | stat.S_IWUSR)

    def _earthdata_check_netrc(self):
        from pathlib import Path
        netrc_path = Path.home() / ".netrc"
        if not netrc_path.exists():
            self._set_ed_status("No ~/.netrc file found yet.", "warn")
            return
        try:
            import netrc as netrc_mod
            auth = netrc_mod.netrc(str(netrc_path)).authenticators(EARTHDATA_HOST)
        except Exception as e:  # netrc raises on malformed/permission issues
            self._set_ed_status(f"Could not read ~/.netrc: {e}", "error")
            return
        if auth and auth[0]:
            self._set_ed_status(
                f"✓ ~/.netrc has a NASA Earthdata entry for {auth[0]}.", "success")
        else:
            self._set_ed_status(
                "~/.netrc exists but has no NASA Earthdata entry.", "warn")

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
        # cloud filter off -> 100, which run_single/imagery read as "no cap" and
        # drop the eo:cloud_cover predicate entirely (not the same as 'lt 100',
        # which would still discard overcast and metadata-less scenes).
        max_cloud = (self.cloud_spin.value() if self.cloud_filter_check.isChecked()
                     else 100.0)
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{self.radius_spin.value():.2f}",
            "--pre-days", str(self.pre_slider.value()),
            "--post-days", str(self.post_slider.value()),
            "--prefer", self.source_combo.currentData(),
            "--max-cloud", f"{max_cloud:.0f}",
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
        # enabled only when idle AND a streamable scene (S2 or Landsat) is in hand
        self.map_preview_btn.setEnabled(
            (not on) and bool(self._best_streamable("pre") or self._best_streamable("post")))

    def _checked_scene_selection(self):
        """Hand-picked scenes to download, from the table's ticked checkboxes.

        Returns one of:
          dict(source, pre=[ids], post=[ids]) — the Sentinel-2 / Landsat scenes to
            run. EITHER side may be empty: a post-only (or pre-only) run is valid
            and produces that side's imagery, just no change rasters, which need
            both sides to subtract. That matters for a fresh event where nothing
            usable was acquired before it yet.
          None — nothing ticked at all, so the caller decides what to do;
          str  — an error message (mixed sources) to show and abort, so a
            half-made selection isn't silently ignored.
        Only Sentinel-2 / Landsat scenes are hand-pickable; PlanetScope is left to
        the automatic path (and ignored here even if ticked)."""
        picked = {"pre": [], "post": []}
        sources = set()
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is None or cell.checkState() != Qt.Checked:
                continue
            source = cell.data(Qt.UserRole + 1)
            cid = cell.data(Qt.UserRole + 2)
            side = cell.data(Qt.UserRole + 3)
            if source not in STREAMABLE or not cid or side not in picked:
                continue   # PlanetScope / id-less rows aren't hand-pickable
            picked[side].append(cid)
            sources.add(source)
        if not picked["pre"] and not picked["post"]:
            return None   # nothing streamable ticked
        if len(sources) > 1:
            return ("Tick scenes from a single source — all Sentinel-2 OR all "
                    "Landsat. They can't be composited together.")
        return dict(source=next(iter(sources)), pre=picked["pre"], post=picked["post"])

    def _run(self):
        if not any(cb.isChecked() for cb in self.scene_checks.values()):
            self._warn("Select at least one layer to export.")
            return
        sel = self._checked_scene_selection()
        if isinstance(sel, str):
            self._warn(sel)
            return
        # Rows on the table but none ticked: don't quietly fall back to the
        # automatic ranking and download scenes that weren't chosen. Only an empty
        # table (no Search yet) leaves the run nothing to go on, and there the
        # automatic pick is the whole point.
        if sel is None and self.table.rowCount() > 0:
            self._warn("Tick the scene(s) you want to download in the table "
                       "(either side alone is fine), or run Search / Preview "
                       "again to let the Run pick automatically.")
            return
        c = self._collect()
        if c is None:
            return
        python, script, project, out, args = c
        if sel:
            # only pass the sides that were actually ticked — a missing side means
            # "don't fetch that side", not "fall back to the automatic search"
            for side in ("pre", "post"):
                if sel[side]:
                    args = args + [f"--{side}-scene-ids", ",".join(sel[side])]
        self.log.clear()
        if sel:
            parts = [f"{len(sel[s])} {s}" for s in ("pre", "post") if sel[s]]
            self._append_log(
                f"Manual scene selection: downloading {' + '.join(parts)} "
                f"{sel['source']} scene(s) you ticked — nothing else "
                f"(automatic ranking and PlanetScope skipped).")
            if not sel["pre"] or not sel["post"]:
                missing = "pre" if not sel["pre"] else "post"
                self._append_log(
                    f"  one-sided run: no {missing} scene ticked, so the {missing} "
                    f"imagery and the change rasters (dNDVI / dNDSI / dBrightness, "
                    f"which subtract one side from the other) are skipped.")
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
        # drop the old result BEFORE emptying the table: clearing rows fires a
        # selection change, and the footprint refresh that hangs off it would
        # otherwise redraw every stale candidate on its way out.
        self._search_result = None    # invalidate map-preview until new results land
        self.table.setRowCount(0)
        self._preview_pix = None
        self.preview.setText("Select a scene to preview its browse image.")
        self._clear_gallery()
        self._clear_footprints()      # stale footprints go until the new search lands
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
            self._draw_footprints(log=True)
        for note in result.get("notes", []):
            self._append_log("note: " + note)
        # the map preview renders via the data API (Sentinel-2 or Landsat); enable
        # it only when there's a streamable scene on at least one side.
        has_streamable = bool(self._best_streamable("pre") or self._best_streamable("post"))
        self.map_preview_btn.setEnabled(has_streamable)
        npre, npost = len(result.get("pre", [])), len(result.get("post", []))
        self.iface.messageBar().pushInfo(
            "Landslide", f"Found {npre} pre / {npost} post candidate scenes "
                         f"(no orders placed).")

    def _fill_table(self, result):
        pre = result.get("pre", [])
        post = result.get("post", [])
        # The dry-run lists every acquisition near the event (clouds included — see
        # imagery.search_event). We mark the best review scene per side by what a
        # usable before/after actually needs (see _rank_like_run): ★ = covers the
        # event point, fills the most of the AOI box, and is least cloudy; ✓ = also
        # among the best-covering low-cloud scenes; plain/greyed = less coverage,
        # cloudier, or off-point entirely. The marks are a SUGGESTION — the ticks
        # decide, and a 'cloudy' (whole-scene) row is often clear over the point.
        sel = self._run_selection(pre, post, result.get("params", {}))
        rows = [("pre", c) for c in pre] + [("post", c) for c in post]
        self.table.setRowCount(len(rows))
        off_point = 0                    # scenes whose footprint misses the epicentre
        for r, (side, c) in enumerate(rows):
            info = sel[side]
            cid = c.get("id")
            covers_pt = self._covers_event(c)
            if not covers_pt:
                off_point += 1
            cover_frac = self._aoi_coverage(c)          # 0..1 of the AOI box filled
            is_top = cid is not None and cid == info["top"]
            in_comp = cid in info["used"]
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else str(c["gap_days"])
            cloud = "" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}"
            cover = f"{cover_frac*100:.0f}"
            marker = "★ " if is_top else ("✓ " if in_comp else "  ")
            cells = [marker + side, date, gap, cloud, cover,
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
            # stash the browse URL + source + scene id + side on the row, for the
            # preview and the manual run-selection checkbox.
            side_item = self.table.item(r, 0)
            side_item.setData(Qt.UserRole, c.get("thumb_url"))
            side_item.setData(Qt.UserRole + 1, c.get("source", ""))
            side_item.setData(Qt.UserRole + 2, c.get("id"))
            side_item.setData(Qt.UserRole + 3, side)
            # checkbox = use this scene — 'Preview on map' renders the ticked rows
            # and a Run downloads exactly them. NOTHING starts ticked: pre-ticking
            # the ★ meant a Run silently composited it alongside whatever you had
            # picked, so the only way to run one chosen scene was to notice the ★
            # and untick it. Hand-pickable for Sentinel-2 / Landsat.
            side_item.setFlags(side_item.flags() | Qt.ItemIsUserCheckable)
            side_item.setCheckState(Qt.Unchecked)
            if c.get("thumb_url"):
                self.table.item(r, 6).setToolTip(c["thumb_url"])
            self.table.item(r, 4).setToolTip(
                f"Covers {cover}% of the search-AOI box"
                + ("" if covers_pt else " — but NOT the event point itself"))
            if is_top:
                side_item.setToolTip(
                    f"★ Best scene on this side: covers the event point and the most "
                    f"of the AOI ({cover}%) with the least cloud. A suggestion, not a "
                    f"selection — it is NOT ticked for you.")
            elif in_comp:
                side_item.setToolTip(
                    "✓ Also among the best-covering, low-cloud scenes on this side, "
                    "so it's another reasonable pick.")
            else:
                side_item.setToolTip(
                    "Ranked below the suggestion (less AOI coverage, or cloudier). "
                    "Cloud % is a WHOLE-scene metric, so check the thumbnail — a "
                    "'cloudy' scene is often clear over the point and the right pick.")
            side_item.setToolTip(
                side_item.toolTip() + "\n\nTick the checkbox to use this scene: a "
                "Run downloads EXACTLY the ticked rows (and 'Preview on map' "
                "renders them). Nothing else is added. Sentinel-2 / Landsat only — "
                "PlanetScope has its own tab.")
            if not covers_pt:
                # muted text flags it visually; the tooltip says why it's demoted
                for col in range(self.table.columnCount()):
                    it = self.table.item(r, col)
                    if it is not None:
                        it.setForeground(QBrush(MUTED_FG))
                side_item.setToolTip(
                    side_item.toolTip() + "\n\n⚠ This scene's footprint does NOT "
                    "cover the epicentre — its acquisition leaves the event point "
                    "in a nodata gap, so it is ranked last and won't be the "
                    "Preview-on-map default. It still lists in case you want the "
                    "surrounding area.")
        if off_point:
            self._append_log(
                f"note: {off_point} of {len(rows)} candidate scene(s) do not cover "
                f"the epicentre (footprint clips the AOI but misses the point); "
                f"they are ranked last so the ★ and Preview-on-map favour scenes "
                f"that actually cover the event point.")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ---------- replicate fetch_event's scene selection (for the ★/preview) ----------
    def _rank_like_run(self, cands, cloud_weight, auto_window):
        """Order candidates for the ★ / Preview-on-map: the scene that best covers
        the point AND the AOI, with the least cloud.

        Keys, in order:
          1. covers the epicentre (a scene that leaves the point in a nodata gap is
             useless for a before/after there, so it sinks below every one that
             covers it, whatever else it has going for it);
          2. AOI coverage, bucketed to 5% (the scene filling the most of the search
             box — fewest nodata gaps over the area);
          3. cloud cover, least first (whole-scene metric, so it only breaks ties
             between similarly-covering scenes — which is why coverage is bucketed);
          4. gap_days, nearest the event last, as a final tiebreaker.
        cloud_weight / auto_window no longer reshuffle this: coverage of the point
        and the area is what makes a review scene usable, so it leads regardless of
        which Run mode produced the candidates."""
        def covers(c):
            return 0 if self._covers_event(c) else 1        # point-covering first

        def cov_bucket(c):
            return -round(self._aoi_coverage(c) * 20)       # 5% bins, most first

        def cloud(c):
            v = c.get("cloud_pct")
            return 100.0 if v is None else v

        def gap(c):
            g = c.get("gap_days")
            return 1e9 if g is None else g

        return sorted(cands, key=lambda c: (covers(c), cov_bucket(c), cloud(c), gap(c)))

    def _run_selection(self, pre, post, params):
        """Which scenes to mark per side: {'pre'/'post': {top, used}}.

        Picks the source a Run would use (explicit --prefer, else the first of
        Planet→Sentinel-2→Landsat with scenes on both sides), then ranks that
        source's scenes by point+AOI coverage and cloud (see `_rank_like_run`).
        `top` (the ★) is the scene the preview should show — best coverage of the
        point and the box, least cloud; `used` (the ✓ set) is the top 1
        (auto-window) or top 6 of that ranking."""
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
        # keep the footprint overlay in sync with the selection (selected rows
        # only; all candidates when nothing is selected)
        if self.footprint_check.isChecked():
            self._draw_footprints()
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

    def _preview_url_for(self, source, item_id, thumb_url, max_size=1024):
        """Browse-image URL for a scene, shared by the table preview and the gallery.

        Sentinel-2 / Landsat: a data-API Highlight Optimized Natural Color render
        from the raw SR bands (so ice keeps its texture instead of a 'visual' TCI's
        blown-out white, and no SAS signing is needed). PlanetScope (and anything
        with no data-API render): its baked rendered_preview/thumbnail, signed for
        Planet. The colour_formula is percent-encoded once in HIGHLIGHT_RENDER, so
        the query is appended verbatim here."""
        cfg = HIGHLIGHT_RENDER.get(source)
        if cfg and item_id:
            return (f"{PC_DATA_URL}/item/preview.png?collection={cfg['collection']}"
                    f"&item={item_id}&{cfg['query']}&max_size={max_size}")
        return self._auth_thumb_url(thumb_url, source) if thumb_url else None

    def _scene_preview_url(self, cell):
        """Browse-image URL for the selected table row's scene (large preview)."""
        return self._preview_url_for(cell.data(Qt.UserRole + 1),
                                     cell.data(Qt.UserRole + 2),
                                     cell.data(Qt.UserRole), max_size=1024)

    def _auth_thumb_url(self, url, source):
        # Planet browse PNGs need the API key; STAC rendered previews are public.
        # Prefer the stored (account/pasted) key over the ambient PL_API_KEY env var,
        # matching the PlanetScope tab so a signed-in account outranks a stray env key.
        if source == "PlanetScope" and "api_key=" not in url:
            key = (self.settings.value("landslide/planet_api_key", "", type=str)
                   or os.environ.get("PL_API_KEY"))
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
        self._rescale_fonts()    # grow/shrink text with the dock size
        self._render_preview()   # keep the preview fit to the pane as it resizes

    def _rescale_fonts(self):
        """Scale the base font by how far the dock's size departs from the baseline.

        Driven off the dock's OWN width/height (a stable size set by the user
        dragging the dock), not the scrolled content — so changing the font can't
        feed back into another resize and oscillate. The new font is set on the
        dock, which Qt propagates to every child widget that hasn't overridden its
        own font."""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        scale = math.sqrt((w / FONT_BASE_W) * (h / FONT_BASE_H))
        scale = max(FONT_SCALE_MIN, min(FONT_SCALE_MAX, scale))
        if self._last_font_scale is not None and abs(scale - self._last_font_scale) < 0.02:
            return                       # negligible change -> skip the relayout
        self._last_font_scale = scale
        f = self.font()
        if self._base_font_pt > 0:
            f.setPointSizeF(round(self._base_font_pt * scale, 1))
        elif self._base_font_px > 0:
            f.setPixelSize(max(1, round(self._base_font_px * scale)))
        else:                            # neither reported -> assume 9 pt default
            f.setPointSizeF(round(9.0 * scale, 1))
        self.setFont(f)

    # ---------- quicklook gallery (all candidates' thumbnails at once) ----------
    def _clear_gallery(self):
        """Abort any in-flight thumbnail fetches and tear down the tile grid, so a
        fresh search rebuilds from scratch instead of stacking onto the old one."""
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
        """Fill the gallery with every pre and post candidate's browse thumbnail.

        Grouped Before/After; each tile is clickable and selects the matching table
        row (which drives the big preview and Preview on map). Thumbnails load
        asynchronously so the UI stays responsive."""
        self._clear_gallery()
        for side, title in (("pre", "Before"), ("post", "After")):
            cands = result.get(side, [])
            if not cands:
                continue
            self.gallery_layout.addWidget(QLabel(f"<b>{title}</b> ({len(cands)} scenes)"))
            host = QWidget()
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0)
            cols = 3
            for i, c in enumerate(cands):
                grid.addWidget(self._make_gallery_tile(c), i // cols, i % cols)
            self.gallery_layout.addWidget(host)

    def _make_gallery_tile(self, c):
        date = (c.get("date") or "")[:10]
        cloud = "—" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}%"
        gap = "" if c.get("gap_days") is None else f"{c['gap_days']}d"
        src = c.get("source", "")
        cid = c.get("id")
        tile = QToolButton()
        tile.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tile.setIconSize(QSize(128, 128))
        tile.setFixedWidth(150)
        tile.setAutoRaise(True)
        tile.setText(f"{date}\n{src}\ncloud {cloud} · {gap}")
        tile.setToolTip(f"{src}\n{cid}\n{date}   cloud {cloud}   gap {gap}")
        tile.clicked.connect(lambda _=False, x=cid: self._select_row_by_id(x))
        url = self._preview_url_for(src, cid, c.get("thumb_url"), max_size=512)
        if url:
            baked = (self._auth_thumb_url(c.get("thumb_url"), src)
                     if c.get("thumb_url") else None)
            fb = baked if (baked and baked != url) else None
            self._fetch_gallery_thumb(tile, url, fallback=fb)
        else:
            tile.setText(tile.text() + "\n(no preview)")
        return tile

    def _fetch_gallery_thumb(self, tile, url, fallback=None):
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._gallery_replies.append(reply)
        reply.finished.connect(
            lambda r=reply, t=tile, fb=fallback: self._gallery_thumb_loaded(r, t, fb))

    def _gallery_thumb_loaded(self, reply, tile, fallback):
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
                pass   # tile was torn down by a newer search
            return
        if fallback:   # render URL failed — try the baked thumbnail once
            self._fetch_gallery_thumb(tile, fallback)
            return
        try:
            tile.setText(tile.text() + "\n(no preview)")
        except RuntimeError:
            pass

    def _select_row_by_id(self, cid):
        """Select the table row for scene `cid` (called when a gallery tile is
        clicked), so the big preview and Preview on map follow the gallery pick."""
        if not cid:
            return
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell and cell.data(Qt.UserRole + 2) == cid:
                self.table.selectRow(r)
                self.table.scrollToItem(cell)
                break

    # ---------- scene footprints on the map ----------
    def _on_footprint_toggle(self, checked):
        if checked:
            self._draw_footprints(log=True)
        else:
            self._clear_footprints()

    def _clear_footprints(self):
        for lyr in self._footprint_layers:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except (RuntimeError, AttributeError):
                pass
        self._footprint_layers = []

    def _qgs_geom(self, geom):
        """A GeoJSON Polygon/MultiPolygon dict -> QgsGeometry (lon/lat), or None."""
        if not geom:
            return None
        coords = geom.get("coordinates")
        gtype = geom.get("type")

        def ring(r):
            return [QgsPointXY(p[0], p[1]) for p in r]

        try:
            if gtype == "Polygon":
                return QgsGeometry.fromPolygonXY([ring(r) for r in coords])
            if gtype == "MultiPolygon":
                return QgsGeometry.fromMultiPolygonXY(
                    [[ring(r) for r in poly] for poly in coords])
        except (TypeError, IndexError):
            return None
        return None

    def _event_point(self):
        """QgsPointXY of the event epicentre from the last search, or None.

        The same lon/lat the search box is centred on (see `_aoi_bbox`)."""
        result = self._search_result or {}
        try:
            return QgsPointXY(float(result.get("lon")), float(result.get("lat")))
        except (TypeError, ValueError):
            return None

    def _covers_event(self, candidate):
        """True if the scene's footprint actually contains the event point.

        The STAC search returns every scene whose TILE footprint intersects the
        AOI box, so a scene can clip a corner of the box yet leave the epicentre
        in that acquisition's diagonal nodata gap — a Preview-on-map then paints
        imagery off to one side of the point (covering the AOI, not the point).
        This tests real coverage of the point so the ranking can sink those
        scenes. Absent/unparseable geometry -> True (never demote a scene we
        cannot test)."""
        pt = self._event_point()
        if pt is None:
            return True
        g = self._qgs_geom(candidate.get("geometry"))
        if g is None or g.isEmpty():
            return True
        return g.contains(pt)

    def _aoi_coverage(self, candidate):
        """Fraction (0..1) of the search-AOI box the scene footprint fills.

        area(footprint ∩ AOI) / area(AOI). Drives the ★/preview toward the scene
        that fills the MOST of the box (fewest nodata gaps over the area), which
        is the other half of 'covers the point AND the AOI'. The ratio is taken in
        the AOI's own lon/lat space, so the box's degree anisotropy cancels top
        and bottom. 0.0 when geometry or AOI is missing, so an untestable scene
        never wins on coverage."""
        bbox = self._aoi_bbox()
        g = self._qgs_geom(candidate.get("geometry"))
        if bbox is None or g is None or g.isEmpty():
            return 0.0
        minx, miny, maxx, maxy, _ = bbox
        aoi = QgsGeometry.fromRect(QgsRectangle(minx, miny, maxx, maxy))
        aoi_area = aoi.area()
        if aoi_area <= 0:
            return 0.0
        try:
            inter = g.intersection(aoi)
        except Exception:
            return 0.0
        if inter is None or inter.isEmpty():
            return 0.0
        return max(0.0, min(1.0, inter.area() / aoi_area))

    def _selected_ids(self):
        """Scene ids of the currently selected table rows."""
        ids = set()
        for idx in self.table.selectionModel().selectedRows():
            cell = self.table.item(idx.row(), 0)
            if cell is not None and cell.data(Qt.UserRole + 2):
                ids.add(cell.data(Qt.UserRole + 2))
        return ids

    def _draw_footprints(self, log=False):
        """Draw footprints for the SELECTED table rows (before = blue, after =
        green) — or for every candidate when nothing is selected — plus the search
        AOI box, so you can see whether a scene covers the AOI or leaves the
        epicentre in a nodata gap. Redrawn on each selection change, so clicking a
        row isolates its granule instead of the full overlapping pile. Memory
        layers, tracked for clean removal."""
        self._clear_footprints()
        result = self._search_result
        if not result:
            return
        sel = self._selected_ids()
        added = 0
        for side, outline in (("pre", "0,90,200"), ("post", "0,150,60")):
            cands = [c for c in result.get(side, []) if c.get("geometry")
                     and (not sel or c.get("id") in sel)]
            if not cands:
                continue
            lyr = QgsVectorLayer("Polygon?crs=EPSG:4326",
                                 f"Scene footprints — {side}", "memory")
            pr = lyr.dataProvider()
            pr.addAttributes([
                QgsField("source", QVariant.String),
                QgsField("scene_id", QVariant.String),
                QgsField("date", QVariant.String),
                QgsField("gap_days", QVariant.Int),
                QgsField("cloud_pct", QVariant.Double),
            ])
            lyr.updateFields()
            feats = []
            for c in cands:
                g = self._qgs_geom(c.get("geometry"))
                if g is None:
                    continue
                f = QgsFeature(lyr.fields())
                f.setGeometry(g)
                f.setAttributes([
                    c.get("source"), c.get("id"), c.get("date"),
                    c.get("gap_days"),
                    c.get("cloud_pct") if c.get("cloud_pct") is not None else None,
                ])
                feats.append(f)
            if not feats:
                continue
            pr.addFeatures(feats)
            lyr.updateExtents()
            sym = QgsFillSymbol.createSimple({
                "style": "no",                 # no fill, outline only
                "outline_color": outline,
                "outline_width": "0.6",
            })
            lyr.renderer().setSymbol(sym)
            QgsProject.instance().addMapLayer(lyr)
            self._footprint_layers.append(lyr)
            added += len(feats)

        # the search AOI box, so coverage gaps read against the actual search area
        bbox = self._aoi_bbox()
        if bbox is not None:
            minx, miny, maxx, maxy, _ = bbox
            aoi = QgsVectorLayer("Polygon?crs=EPSG:4326", "Search AOI", "memory")
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromRect(
                QgsRectangle(minx, miny, maxx, maxy)))
            aoi.dataProvider().addFeatures([f])
            aoi.updateExtents()
            sym = QgsFillSymbol.createSimple({
                "style": "no",
                "outline_color": "220,30,30",
                "outline_width": "0.8",
                "outline_style": "dash",
            })
            aoi.renderer().setSymbol(sym)
            QgsProject.instance().addMapLayer(aoi)
            self._footprint_layers.append(aoi)
        # Only announce the first draw after a search: this now also runs on every
        # selection change, and logging each one would bury the run output.
        if added and log:
            self._append_log(
                f"Drew {added} scene footprint(s) + AOI on the map. Select rows in "
                f"the table to show only those scenes' footprints.")

    # ---------- preview on map (Highlight Optimized Natural Color AOI render) ----------
    def _best_streamable(self, side):
        """The scene Preview-on-map would lead with on `side` ('pre'/'post').

        Prefers the best Sentinel-2 candidate, then Landsat (the two sources the
        data API can render). Within a source, ranks by the SAME blend the run uses
        (not gap-sorted display order), so the preview shows the scene the run
        actually prioritises. PlanetScope is excluded (no single streamable COG)."""
        if not self._search_result:
            return None
        params = self._search_result.get("params", {})
        cw = params.get("cloud_weight", 0.5) or 0.5
        auto = bool(params.get("auto_window"))
        rows = self._search_result.get(side, [])
        for src in STREAMABLE:
            cands = [c for c in rows if c.get("source") == src and c.get("id")]
            ranked = self._rank_like_run(cands, cw, auto)
            if ranked:
                return ranked[0]
        return None

    def _candidate_by_id(self, cid):
        """(side, candidate) for scene `cid` in the last search result, or None."""
        if not cid or not self._search_result:
            return None
        for side in ("pre", "post"):
            for c in self._search_result.get(side, []):
                if c.get("id") == cid:
                    return side, c
        return None

    def _preview_picks(self):
        """[(side, candidate, why), …] for Preview on map, from the TICKED rows.

        Same rule as the PlanetScope tab: ticks choose the scenes — tick one to
        preview it, tick several to compare. With nothing streamable ticked, fall
        back to the ★ best-ranked scene on each side so the button still gives a
        sensible before/after pair. Row SELECTION only drives the browse-image
        pane and the footprint overlay. PlanetScope rows are skipped: the data API
        can't render them."""
        picks = []
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is None or cell.checkState() != Qt.Checked:
                continue
            if cell.data(Qt.UserRole + 1) not in STREAMABLE:
                continue
            info = self._candidate_by_id(cell.data(Qt.UserRole + 2))
            if info:
                picks.append((info[0], info[1], "ticked"))
        if not picks:                    # nothing ticked -> the run's pick per side
            for side in ("pre", "post"):
                c = self._best_streamable(side)
                if c and c.get("id"):
                    picks.append((side, c, "★ best"))
        return picks

    def _clear_preview_layers(self):
        """Remove the rasters a previous Preview-on-map added, so each preview
        shows just the current pick instead of piling up."""
        for lyr in self._preview_added:
            lg.remove_layer(lyr)
        self._preview_added = []

    def _preview_row_on_map(self, item):
        """Double-click a row -> preview exactly that one scene (ignores ticks)."""
        cell = self.table.item(item.row(), 0)
        if cell is None:
            return
        if cell.data(Qt.UserRole + 1) not in STREAMABLE:
            self._warn("Only Sentinel-2 / Landsat scenes can be rendered on the map.")
            return
        info = self._candidate_by_id(cell.data(Qt.UserRole + 2))
        if info:
            self._render_picks([(info[0], info[1], "double-clicked")])

    def _preview_on_map(self):
        """Render the ticked (or ★ best) scenes over the AOI."""
        self._render_picks(self._preview_picks())

    def _render_picks(self, picks):
        """Render [(side, candidate, why), …] onto the canvas, clipped to the AOI.

        Downloads a Highlight Optimized Natural Color GeoTIFF clipped to the search
        box from the Planetary Computer data API (raw SR bands + our stretch — no
        'visual' TCI white-out, no SAS signing) and loads each as a georeferenced
        raster, then zooms to the AOI. Covers the AOI box only; Sentinel-2 falls
        back to the signed visual COG if the render fails (Landsat has none)."""
        bbox = self._aoi_bbox()
        if bbox is None:
            self._warn("Run Search / Preview first (need the AOI location).")
            return
        if not picks:
            self._warn("No streamable (Sentinel-2 / Landsat) scene to preview — "
                       "tick the scene(s) you want in the table.")
            return
        scenes = []
        off_point = 0
        for side, c, why in picks:
            date = (c.get("date") or "")[:10]
            short = "S2" if c.get("source") == "Sentinel-2" else "Landsat"
            if not self._covers_event(c):
                off_point += 1
            scenes.append((f"{short} {side} {date}".strip(), c["id"],
                           c.get("cog_url"), c.get("source"), why))
        self._append_log(
            f"Preview on map: rendering Highlight Optimized Natural Color over the "
            f"AOI for {len(scenes)} scene(s)…")
        if off_point:
            # the render still covers the AOI box, but this scene has no pixels at
            # the epicentre — say so rather than leave the user reading a nodata gap
            if off_point == len(scenes):
                msg = (f"None of the {len(scenes)} scene(s) being previewed cover the "
                       f"event point — their imagery sits to one side of the epicentre, "
                       f"which falls in the scene's nodata gap. No Sentinel-2 / Landsat "
                       f"scene in this window covers the point on that side.")
            else:
                msg = (f"{off_point} of {len(scenes)} previewed scene(s) do not cover the "
                       f"event point (epicentre in a nodata gap); the others do.")
            self._warn(msg)
        self._ensure_network_timeout()
        self.map_preview_btn.setEnabled(False)
        self._clear_preview_layers()   # replace the previous preview, don't pile up
        self._preview_failed = []
        self._tif_fallbacks = []
        self._tif_pending = len(scenes)
        for label, item_id, cog_url, source, why in scenes:
            self._append_log(f"  {label} ({why})")
            self._download_aoi_tif(label, item_id, cog_url, bbox, source)

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

    def _download_aoi_tif(self, label, item_id, cog_url, bbox, source):
        """Fetch a Highlight Optimized Natural Color GeoTIFF clipped to the AOI.

        Single async GET to the data API's bbox endpoint (same mechanism as the
        working scene-preview pane), using the per-source render config (collection
        + bands + stretch), sized to ~10 m/px and capped so a wide AOI stays a sane
        download. The reply lands in `_tif_loaded`."""
        cfg = HIGHLIGHT_RENDER.get(source)
        if cfg is None:
            self._preview_failed.append(label)
            self._tif_pending -= 1
            if self._tif_pending <= 0:
                self._after_tif_downloads()
            return
        minx, miny, maxx, maxy, radius = bbox
        px = int(min(2048, max(256, round(radius * 2 * 100))))   # ~10 m/px, capped
        url = (f"{PC_DATA_URL}/item/bbox/{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}.tif"
               f"?collection={cfg['collection']}&item={item_id}&{cfg['query']}"
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
                    lg.add_to_group(lyr, "Imagery preview")
                    self._preview_added.append(lyr)
                    self._append_log(
                        f"  loaded {label} (AOI render, Highlight Optimized Natural Color)")
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
        lg.add_to_group(lyr, "Imagery preview")
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
            msg = (f"Loaded {n} scene(s) over the AOI (Highlight Optimized Natural "
                   f"Color). Toggle the layers to compare before vs after.")
            if self._preview_failed:
                msg += f" {len(self._preview_failed)} scene(s) failed to load."
            self.iface.messageBar().pushInfo("Landslide", msg)
        else:
            self._warn("Preview on map: no scene could be loaded.")

    # ---------- add point / search-area to the map ----------
    def _add_point_or_area(self):
        """Drop-down action: add the entered location to the map as either a point
        marker or a translucent search-radius circle. Memory layers only."""
        try:
            lat = float(self.lat_edit.text().strip())
            lon = float(self.lon_edit.text().strip())
        except ValueError:
            self._warn("Enter valid numeric latitude and longitude first.")
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            self._warn("Latitude must be -90..90 and longitude -180..180.")
            return
        if self.point_area_combo.currentData() == "point":
            self._add_point_layer(lat, lon)
        else:
            self._add_area_layer(lat, lon, self.radius_spin.value())

    def _add_point_layer(self, lat, lon):
        """A red marker at the entered lat/lon (memory point layer)."""
        lyr = QgsVectorLayer("Point?crs=EPSG:4326", "Event point", "memory")
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        lyr.dataProvider().addFeatures([f])
        lyr.updateExtents()
        sym = QgsMarkerSymbol.createSimple({
            "name": "circle",
            "color": "255,0,0",
            "outline_color": "255,255,255",
            "outline_width": "0.4",
            "size": "3.5",
        })
        lyr.renderer().setSymbol(sym)
        QgsProject.instance().addMapLayer(lyr)
        self._append_log(f"Added event point at {lat:.6f}, {lon:.6f}.")

    def _add_area_layer(self, lat, lon, radius_km):
        """A search-radius circle: red outline, 25%-opacity red fill (memory
        polygon layer). Built as an N-sided ring using the same degree scaling as
        the AOI box, so it stays round on the map at any latitude."""
        dlat = radius_km / 111.32
        dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
        n = 72
        ring = [QgsPointXY(lon + dlon * math.cos(2 * math.pi * i / n),
                           lat + dlat * math.sin(2 * math.pi * i / n))
                for i in range(n + 1)]
        lyr = QgsVectorLayer("Polygon?crs=EPSG:4326",
                             f"Search area — {radius_km:g} km", "memory")
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        lyr.dataProvider().addFeatures([f])
        lyr.updateExtents()
        sym = QgsFillSymbol.createSimple({
            "color": "255,0,0,64",         # red fill @ ~25% opacity
            "outline_color": "255,0,0",
            "outline_width": "0.6",
        })
        lyr.renderer().setSymbol(sym)
        QgsProject.instance().addMapLayer(lyr)
        self._append_log(f"Added {radius_km:g} km search-area circle.")

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

    def _zoom_to_layers(self, layers):
        """Frame the canvas on the combined extent of the just-created layers.

        Reprojects each layer's extent into the canvas CRS and unions them, so a
        Run lands on the imagery it produced — centred on the event AOI — instead
        of on the first layer's raw extent (which, for the lone epicentre point,
        collapses the zoom to a single coordinate). Zero-area layers are skipped;
        falls back to the search AOI if nothing usable remains."""
        dst = self.canvas.mapSettings().destinationCrs()
        union = None
        for lyr in layers:
            try:
                ext = lyr.extent()
            except (RuntimeError, AttributeError):
                continue
            if ext is None or ext.isEmpty() or ext.width() <= 0 or ext.height() <= 0:
                continue                       # skip the epicentre point (no area)
            src = lyr.crs()
            if dst.isValid() and src.isValid() and src != dst:
                try:
                    ext = QgsCoordinateTransform(
                        src, dst, QgsProject.instance()).transformBoundingBox(ext)
                except Exception:
                    continue
            if union is None:
                union = QgsRectangle(ext)
            else:
                union.combineExtentWith(ext)
        if union is None or union.isEmpty():
            self._zoom_to_aoi()                # nothing framable -> the search box
            return
        union.scale(1.05)                      # a little breathing room around it
        self.canvas.setExtent(union)
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
        # Each Run opens its OWN folder — "S2 7-20/7-21", or "… (2)" if that event's
        # folder already exists — so a new run never merges into a previous one.
        # Inside it: one subfolder per product (NDVI, dNDVI, HONC…), with the
        # predicted-epicentre point sitting at the run folder's top level.
        prefix = SENSOR_TAG.get(result.get("sensor"), "Imagery")
        dates = lg.date_pair(_first_date(result.get("pre_dates")),
                             _first_date(result.get("post_dates")))
        run_group = None                 # opened on the first valid layer (never if empty)
        subs = {}                        # product tag -> its subfolder within this run
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
                if run_group is None:
                    run_group = lg.new_group(lg.name(prefix, dates))
                product = _core_product(name)
                if product:
                    if product not in subs:
                        subs[product] = lg.subgroup(run_group, product)
                    lg.add_to(lyr, subs[product])
                else:
                    lg.add_to(lyr, run_group)   # the predicted-epicentre point
                added.append(lyr)
        if added:
            self._zoom_to_layers(added)   # frame the new outputs, not a stale extent

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
        # acquisition dates — also baked into each layer's name, so the log and the
        # legend agree on when the imagery is from
        for side in ("pre", "post"):
            dates = result.get(f"{side}_dates") or []
            if dates:
                self._append_log(f"  {side} date(s): " + ", ".join(sorted(set(dates))))
        self._append_log("  clouds kept as acquired (no cloud masking)")
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
        # drop the project-signal connections first: they'd otherwise fire into
        # deleted widgets when the plugin is unloaded/reloaded with QGIS open
        state = getattr(self, "project_state", None)
        if state is not None:
            project = QgsProject.instance()
            for signal, slot in ((project.writeProject, state.save),
                                 (project.readProject, state.restore),
                                 (project.cleared, state.restore)):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
            self.project_state = None
        for attr in ("_preview_reply", "_ed_reply"):
            reply = getattr(self, attr, None)
            if reply is not None:
                try:
                    reply.abort()
                except RuntimeError:
                    pass
                setattr(self, attr, None)
        for reply in self._sign_replies + self._tif_replies + self._gallery_replies:
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._sign_replies = []
        self._tif_replies = []
        self._gallery_replies = []
        if self.task is not None:
            self.task.cancel()
        if getattr(self, "planet_tab", None) is not None:
            self.planet_tab.teardown()
        if getattr(self, "sar_tab", None) is not None:
            self.sar_tab.teardown()
        if getattr(self, "viewer3d_tab", None) is not None:
            self.viewer3d_tab.teardown()
