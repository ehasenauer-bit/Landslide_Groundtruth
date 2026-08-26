"""The SAR (Sentinel-1) tab: cloud-free amplitude previews for pre/post visual
change inspection, from Planetary Computer's `sentinel-1-rtc` collection.

Why SAR: radar sees through cloud and darkness, so where the optical tabs wait
weeks for a clear scene, Sentinel-1 delivers a usable image every pass (~6-12
days per track over Alaska). A fresh landslide changes surface roughness, which
reads as an amplitude (brightness/texture) change between the pre and post
grayscale images.

Why RTC: the collection is ALREADY radiometrically terrain corrected (gamma-
naught, terrain-flattened against a DEM), so brightness differences between
dates mean the ground changed — not the viewing geometry. That also means one
fixed brightness stretch works for every scene; there is no per-scene
adjustment (the stretch spinner here applies to all previews equally, keeping
pre and post directly comparable).

Pipeline shape mirrors the Sentinel-2/Landsat tab: search runs through the same
venv subprocess (`run_single.py --search-only --prefer s1`), previews render
in-process through the Planetary Computer data API (`/item/preview.png` for
thumbnails, `/item/bbox/....tif` for the on-canvas AOI render — grayscale,
10 m pixels). No Run/composite path; instead the Change-detection panel turns
same-track amplitude scenes into per-pixel change-possibility maps in-process
(multi-temporal intensity correlation when 3+ before-scenes share the track,
falling back to quick-product intensity-correlation / log-ratio — the
incoherent half of Jung & Yun 2020, doi:10.3390/rs12020265; the math lives in
sar_change.py). Those analysis fetches re-use the AOI-GeoTIFF endpoint WITHOUT
the display rescale, so they get raw float32 gamma-naught rather than the
byte-stretched render.

Auth: NONE REQUIRED. sentinel-1-rtc historically needed a Planetary Computer
account key (the collection description still says so), but Microsoft retired
the account-registration system and anonymous SAS tokens / data-API renders now
work (verified 2026-07; see github.com/microsoft/PlanetaryComputer issue #464).
A key, if you have one from the old registration, only grants higher rate
limits and longer-lived tokens — the optional field below stores it in
QgsSettings ('landslide/pc_subscription_key') and exports it as
PC_SDK_SUBSCRIPTION_KEY for the search subprocess. NOTE: this is Microsoft's
Planetary Computer — unrelated to the Planet account on the PlanetScope tab,
despite the similar name.

Orbit geometry: meaningful pre/post SAR comparison requires the same viewing
geometry. Scenes from the same RELATIVE ORBIT (track) look at the slope from
the same direction; comparing ascending vs descending scenes shows geometry
differences, not ground change. The default pairing therefore picks the post
scene nearest the event, then the nearest pre scene from the SAME track (the
'Pair by' control can relax this).
"""
import math
import os
import tempfile
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QUrl, QSize, QVariant
from qgis.PyQt.QtGui import QPixmap, QIcon, QColor
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDoubleSpinBox, QDateTimeEdit, QCheckBox,
    QProgressBar, QPlainTextEdit, QTableWidget, QTableWidgetItem, QSplitter,
    QScrollArea, QGridLayout, QToolButton, QSlider,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsRectangle,
    QgsNetworkAccessManager, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsField, QgsFeature, QgsFillSymbol, QgsGeometry, QgsPointXY,
    QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader,
)
from qgis.gui import QgsCollapsibleGroupBox

from . import sar_change
from . import sar_pairing
from . import layer_group as lg
from .flow_layout import FlowRow
from .task import PipelineTask
from .dock import PRE_BG, POST_BG, ROW_FG, MUTED_FG, STATUS_COLORS

COLLECTION = "sentinel-1-rtc"
PC_DATA_URL = "https://planetarycomputer.microsoft.com/api/data/v1"
# Requesting a SAS token for the collection is the cheapest authenticated call —
# 200 = the subscription key works, 401/403 = it doesn't. Used by 'Test & save'.
PC_TOKEN_URL = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{COLLECTION}"

# Default linear gamma-naught stretch, black -> white. 0..0.2 is what the
# Planetary Computer Explorer itself uses for the VV grayscale render; RTC
# normalization is why one fixed stretch reads well across scenes and dates.
DEFAULT_STRETCH = 0.20

# "Preview on map (quick)" renders a small fixed-size image for speed — coarse
# pixels, no client-side smoothing, thrown away after — so it appears almost
# immediately at any AOI size. "Run (full detail)" instead honors the Pixel size
# combo and saves a GeoTIFF. This is the quick preview's square dimension in px:
# small enough to render/download fast, big enough to read the scene at a glance.
PREVIEW_PX = 384

# selectable polarizations (VV is the standard choice for land/landslides; VH
# is more sensitive to volume scattering/vegetation structure)
POLARIZATIONS = [
    ("VV (recommended)", "vv"),
    ("VH", "vh"),
]

# how the default preview pairs the pre scene to the post scene
ORBIT_MODES = [
    ("Same track (recommended)", "track"),
    ("Both geometries (asc + desc pairs)", "both"),
    ("Same direction (asc/desc)", "direction"),
    ("Any orbit (not comparable!)", "any"),
]

# incoherent change-detection products (Jung & Yun 2020, doi:10.3390/rs12020265).
# The paper's coherent (phase) methods need SLC/InSAR data RTC doesn't carry —
# and its own finding is they fail over low-coherence natural terrain anyway.
# Each is a checkbox: tick which change maps to compute. The two recommended
# defaults (texture + brightness) fail in opposite ways and are meant to be read
# together — see the tooltip in _build_change_box. Tuple is
# (key, checkbox label, before-scenes needed, checked by default).
CD_PRODUCTS = [
    ("mtcorr", "Texture — multi-temporal intensity correlation", 3, True),
    ("tsint", "Brightness — intensity z-score", 2, True),
    ("intcorr", "Norm. diff — intensity correlation", 2, False),
    ("logratio", "Log-ratio of intensity", 1, False),
]
# before-scenes each product needs, and human names, keyed for reuse in _cd_pick
CD_NEED = {k: n for k, _l, n, _d in CD_PRODUCTS}
CD_NAMES = {"mtcorr": "multi-temporal intensity correlation",
            "tsint": "multi-temporal intensity (brightness z)",
            "intcorr": "intensity correlation", "logratio": "log-ratio"}

# Speckle filter applied to EACH change-detection input scene before the
# detectors run (Gap 1). Distinct from the display-only median filter in the
# Display panel: this one cleans the raw float γ⁰ that feeds the math. Value is
# None (off) or (kind, window). Lee is adaptive/edge-preserving; median is a
# simpler fallback. See sar_change.lee_filter / median_filter.
SPECKLE_FILTERS = [
    ("None (off)", None),
    ("Lee 5×5 (adaptive, recommended)", ("lee", 5)),
    ("Lee 7×7 (adaptive, stronger)", ("lee", 7)),
    ("Median 5×5", ("median", 5)),
]

# Minimum change-blob area (Gap 2): connected clusters of anomalous pixels
# smaller than this are treated as speckle and cleared. A slide is a connected
# patch; scattered single red pixels are noise. Value = pixel count (0 = off).
BLOB_MIN_AREAS = [
    ("Off", 0),
    ("4 px", 4),
    ("8 px (recommended)", 8),
    ("16 px", 16),
]

# multi-temporal cap: more before-scenes sharpen the reference distribution,
# but every scene adds a download and the reference-pair count grows as
# C(n,2); 6 scenes → 15 reference pairs is a good accuracy/cost balance
# (the paper used 16 scenes, offline)
MT_MAX_PRE = 6

# window for the local statistics (multilook mean / texture correlation), in
# pixels of the chosen render resolution. The paper multilooked 16×16 on 3 m
# data ≈ 48 m; 7×7 at 10 m px ≈ 70 m is the closest practical equivalent and
# gives the correlation estimate 49 samples.
CD_WINDOWS = [
    ("5×5 (sharper, noisier)", 5),
    ("7×7 (recommended)", 7),
    ("9×9 (smoother)", 9),
    ("11×11 (smoothest)", 11),
]


class SarTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock                 # shared Environment fields + helpers
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings
        self.task = None
        self._search_result = None       # last search.json (candidates + params)
        self._key_reply = None           # in-flight subscription-key check
        self._preview_reply = None       # in-flight preview-pane request
        self._preview_pix = None         # last loaded preview, kept for rescaling
        self._preview_fallback = None    # baked thumb to retry if a render fails
        self._tif_replies = []           # in-flight AOI-GeoTIFF downloads
        self._tif_pending = 0
        self._preview_added = []         # raster layers added by the current preview
        self._amp_group = None           # layer-tree folder the amplitude preview loads into
        self._preview_failed = []
        self._preview_mode = "preview"   # 'preview' (quick) | 'run' (full detail + save)
        self._preview_res = None         # effective render resolution of the last render
        self._preview_smooth = 0         # median window applied to the last render (0=off)
        self._preview_saved = []         # GeoTIFF paths saved by the current Run
        self._gallery_replies = []       # in-flight quicklook-thumbnail requests
        self._footprint_layers = []      # scene-footprint vector layers on the map
        self._cd_replies = []            # in-flight change-detection downloads
        self._cd_pending = 0
        self._cd_paths = {}              # role ('pre0'…/'post') -> tif path
        self._cd_meta = None             # products/pol/window of the running compute
        self._cd_last_layers = []        # newest change maps (kept above amplitude previews)
        # recent per-geometry computed change arrays, for the asc+desc merge
        # (rec #5): each = {mkey, track, direction, out, gt, proj, thr, pre_d, post_d}
        self._cd_results = []
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        intro = QLabel(
            "Sentinel-1 radar amplitude (10 m grayscale, terrain-corrected). "
            "Sees through cloud — compare pre/post brightness and texture to "
            "spot a slide when optical scenes are clouded out.")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        root.addWidget(self._build_key_box())

        # --- event inputs (own copy; button pulls from the Sentinel tab) ---
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
        self.radius_spin.setValue(5.0)
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

        # Window: how far before / after the event to search, as day sliders.
        self.pre_slider = QSlider(Qt.Horizontal)
        self.pre_slider.setRange(1, 365)
        self.pre_slider.setValue(30)
        self.post_slider = QSlider(Qt.Horizontal)
        self.post_slider.setRange(1, 365)
        self.post_slider.setValue(30)
        self.pre_lbl = QLabel()
        self.post_lbl = QLabel()
        for s in (self.pre_slider, self.post_slider):
            s.valueChanged.connect(self._update_day_labels)
        self._update_day_labels()
        days = QHBoxLayout()
        days.addWidget(QLabel("before"))
        days.addWidget(self.pre_slider, 1)
        days.addWidget(self.pre_lbl)
        days.addWidget(QLabel("after"))
        days.addWidget(self.post_slider, 1)
        days.addWidget(self.post_lbl)
        form.addRow("Window", self._wrap(days))
        root.addLayout(form)

        # --- collapsible option drop-downs (keep the tab compact) ---
        root.addWidget(self._build_display_box())
        root.addWidget(self._build_change_box())
        root.addWidget(self._build_filter_box())

        # --- buttons ---
        # FlowRow (not a fixed QHBoxLayout) so the four buttons wrap onto a
        # second line instead of clipping their labels in a narrow dock.
        btn_row = FlowRow()
        self.search_btn = QPushButton("Search (free)")
        self.search_btn.setToolTip(
            "Free STAC search for candidate before/after Sentinel-1 RTC scenes. "
            "Nothing is downloaded; no cloud filter is needed (radar sees through "
            "cloud).")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map (quick)")
        self.map_preview_btn.setToolTip(
            "FAST, coarse look: render the TICKED scene(s) — or the best "
            "same-track before/after pair if nothing is ticked — as grayscale "
            "amplitude clipped to the AOI, at a small fixed resolution and with "
            "no smoothing, so it appears almost immediately. Double-click a row "
            "to preview just that scene. Use 'Run (full detail)' for the full-"
            "resolution render. Toggle the layers to compare before vs after.")
        self.map_preview_btn.setEnabled(False)
        self.map_preview_btn.clicked.connect(self._preview_on_map)
        self.run_btn = QPushButton("Run (full detail)")
        self.run_btn.setToolTip(
            "Full-resolution render of the same scene(s) at the Pixel size and "
            "speckle filter set in 'Display && pairing options', and SAVES each "
            "as a GeoTIFF under the tab's output folder "
            "(out/interactive/sar/amplitude) so it persists after the session. "
            "Lands in the same 'SAR …/… amplitude' layer folder, replacing the "
            "quick preview. Slower than Preview — a full-size AOI render can take "
            "a few seconds when the endpoint is cold.")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._run_full)
        f = self.run_btn.font()
        f.setBold(True)
        self.run_btn.setFont(f)
        self.run_btn.setDefault(True)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        for b in (self.search_btn, self.map_preview_btn, self.run_btn,
                  self.cancel_btn):
            btn_row.addWidget(b)
        root.addWidget(btn_row)

        self.footprint_check = QCheckBox("Show scene footprints on map")
        self.footprint_check.setToolTip(
            "Draw scene footprints (before = blue, after = green). With table "
            "rows selected, only THOSE scenes' footprints are drawn — click a row "
            "to isolate its outline, Ctrl/Shift-click for several, click in empty "
            "table space to clear the selection and show all candidates again. "
            "Sentinel-1 IW scenes are ~250 km wide, so your whole search area "
            "almost always sits inside one scene.")
        self.footprint_check.toggled.connect(self._on_footprint_toggle)
        root.addWidget(self.footprint_check)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

        # --- outputs: candidate table / gallery / preview / log ---
        split = QSplitter(Qt.Vertical)

        tablebox = QWidget()
        tl = QVBoxLayout(tablebox)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel(
            "Candidate scenes  (★ = default same-track pair; tick the scenes to "
            "preview on the map)"))
        # seismic-time bracket summary for the starred pair (see sar_pairing)
        self.pair_summary = QLabel("")
        self.pair_summary.setWordWrap(True)
        self.pair_summary.setTextFormat(Qt.PlainText)
        self.pair_summary.setVisible(False)
        self.pair_summary.setStyleSheet("QLabel { font-style: italic; padding: 2px 0; }")
        tl.addWidget(self.pair_summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Side", "Date (UTC)", "Gap (d)", "Orbit", "Track", "Scene ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.itemDoubleClicked.connect(self._preview_row_on_map)
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
        pl.addWidget(QLabel("Scene preview (amplitude render)"))
        self.preview = QLabel("Search, then select a scene to preview it.")
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

    def _update_day_labels(self, *_):
        self.pre_lbl.setText(f"{self.pre_slider.value()} d")
        self.post_lbl.setText(f"{self.post_slider.value()} d")

    # ---------- display & pairing options (drop-down) ----------
    def _build_display_box(self):
        """Collapsible panel of the manual render/pairing controls. The defaults
        are right for most sessions, so these live folded away: polarization,
        pre/post pairing rule, display scale and stretch, render pixel size and
        speckle filter. Polarization + Pair by also steer the change-detection
        panel's scene picks."""
        box = QgsCollapsibleGroupBox("Display && pairing options")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        form = QFormLayout(box)

        self.pol_combo = QComboBox()
        for label, value in POLARIZATIONS:
            self.pol_combo.addItem(label, value)
        self.pol_combo.setToolTip(
            "Which radar polarization to render. VV (vertical transmit/receive) "
            "is the standard for land surfaces and landslide scars; VH is more "
            "sensitive to vegetation structure. Both are grayscale. Change "
            "detection also compares this polarization.")
        self.pol_combo.currentIndexChanged.connect(self._on_render_changed)
        form.addRow("Polarization", self.pol_combo)

        self.orbit_combo = QComboBox()
        for label, value in ORBIT_MODES:
            self.orbit_combo.addItem(label, value)
        self.orbit_combo.setToolTip(
            "How the default preview pairs the pre scene with the post scene.\n\n"
            "Same track: both scenes from the same relative orbit — identical "
            "viewing geometry, so brightness change means GROUND change. Best.\n"
            "Both geometries: the best same-track pair on EACH orbit direction — "
            "four layers. Slopes smeared by layover in one geometry read cleanly "
            "in the other, and real change should appear in both pairs.\n"
            "Same direction: both ascending or both descending — similar but not "
            "identical geometry.\n"
            "Any orbit: no constraint — an ascending-vs-descending pair will show "
            "geometry differences that can masquerade as change. Avoid unless "
            "nothing else pairs.")
        self.orbit_combo.currentIndexChanged.connect(self._on_pairing_changed)
        form.addRow("Pair by", self.orbit_combo)

        self.scale_combo = QComboBox()
        self.scale_combo.addItem("dB (log — recommended)", "db")
        self.scale_combo.addItem("Linear γ⁰", "linear")
        self.scale_combo.setToolTip(
            "dB (logarithmic) compresses SAR's huge dynamic range into gray "
            "midtones — the standard way to display amplitude, and much less "
            "harsh than a linear stretch (which crushes water/ice to black and "
            "saturates bright slopes to white). Pre and post always share the "
            "same scale, so they stay directly comparable.")
        self.scale_combo.currentIndexChanged.connect(self._on_render_changed)
        form.addRow("Display scale", self.scale_combo)

        # dB display range: black point / white point. -25..0 dB covers typical
        # VV land backscatter (water/smooth ice ≈ -25, bright rock faces ≈ 0).
        self.db_min = QDoubleSpinBox()
        self.db_min.setRange(-40.0, -2.0)
        self.db_min.setDecimals(0)
        self.db_min.setValue(-25.0)
        self.db_min.setSuffix(" dB")
        self.db_max = QDoubleSpinBox()
        self.db_max.setRange(-30.0, 10.0)
        self.db_max.setDecimals(0)
        self.db_max.setValue(0.0)
        self.db_max.setSuffix(" dB")
        dbrow = QHBoxLayout()
        dbrow.addWidget(QLabel("black"))
        dbrow.addWidget(self.db_min, 1)
        dbrow.addWidget(QLabel("white"))
        dbrow.addWidget(self.db_max, 1)
        db_wrap = self._wrap(dbrow)
        db_wrap.setToolTip(
            "dB values mapped to black and white. Narrow the range for more "
            "contrast; widen it to tame saturation. -25 to 0 dB suits VV over "
            "land; try -30 to -5 dB for VH.")
        form.addRow("dB range", db_wrap)

        self.stretch_spin = QDoubleSpinBox()
        self.stretch_spin.setRange(0.02, 1.00)
        self.stretch_spin.setDecimals(2)
        self.stretch_spin.setSingleStep(0.02)
        self.stretch_spin.setValue(DEFAULT_STRETCH)
        self.stretch_spin.setToolTip(
            "LINEAR mode only: gamma-naught from 0 (black) to this value (white). "
            "0.20 matches the Microsoft Planetary Computer Explorer. Ignored in "
            "dB mode.")
        form.addRow("Stretch (max γ⁰)", self.stretch_spin)

        # Speckle control: SAR's salt-and-pepper noise averages out when pixels
        # are aggregated, at the cost of detail. 20 m halves the speckle while
        # staying sharper than Landsat; landslide scars >50 m stay obvious.
        self.detail_combo = QComboBox()
        self.detail_combo.addItem("10 m — full detail (most speckle)", 10)
        self.detail_combo.addItem("20 m — balanced (recommended)", 20)
        self.detail_combo.addItem("30 m — smoothest (least speckle)", 30)
        self.detail_combo.setCurrentIndex(1)
        self.detail_combo.setToolTip(
            "Pixel size of the on-map AOI render. Coarser pixels average away "
            "speckle (radar's inherent salt-and-pepper noise) but soften small "
            "features. Applies to the next Preview on map, and to change "
            "detection (10 m recommended there — its stat window already "
            "averages speckle).")
        form.addRow("Pixel size", self.detail_combo)

        # Median speckle filter, applied client-side to the downloaded AOI render
        # before it's loaded. Kills isolated bright/dark speckle pixels while
        # preserving edges (unlike a mean blur), and keeps the chosen pixel size.
        self.smooth_combo = QComboBox()
        self.smooth_combo.addItem("None", 0)
        self.smooth_combo.addItem("Median 3×3 (recommended)", 3)
        self.smooth_combo.addItem("Median 5×5 (strongest)", 5)
        self.smooth_combo.setCurrentIndex(1)
        self.smooth_combo.setToolTip(
            "Median speckle filter applied to the on-map render. 3×3 removes "
            "single-pixel speckle and keeps edges sharp; 5×5 smooths harder but "
            "can dim features smaller than ~3 pixels across (at 10 m px, ~30 m). "
            "Combines well with Pixel size: median at 10 m often reads better "
            "than unfiltered 20 m. Applies to the next Preview on map.")
        # Named "Render smoothing" (not "Speckle filter") to distinguish it from the
        # Noise reduction panel's analysis speckle filter, which feeds the detectors.
        form.addRow("Render smoothing", self.smooth_combo)
        return box

    # ---------- change detection (drop-down) ----------
    def _build_change_box(self):
        """Collapsible 'Change detection' panel: the incoherent (amplitude)
        detectors of Jung & Yun 2020 run on the same-track scenes of the last
        search — see sar_change.py for the math and _cd_pick for how scenes
        are chosen (ticked rows steer the pick; the reference stack is topped
        up from the same track when they are too few)."""
        box = QgsCollapsibleGroupBox("Change detection (find likely slide pixels)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        form = QFormLayout(box)

        info = QLabel(
            "Computes per-pixel change maps from same-track scenes and adds "
            "them as colored overlays (red = likely surface change). 'Same "
            "track' means the same relative orbit number (t123), not merely "
            "ascending or descending — two ascending tracks see a slope from "
            "different angles, so only one track's scenes can feed a run. Run "
            "Search first, then tick which products to compute. The two "
            "recommended defaults are complementary: texture (multi-temporal "
            "intensity correlation — the paper's most reliable detector) and "
            "brightness (z-score — catches debris that uniformly brightens "
            "smooth snow/ice, which texture methods cannot see). Speckle and "
            "false-positive filtering live in the Noise reduction panel below. "
            "Caution: wet snow, melt onset and freeze–thaw between the "
            "compared scenes also change radar brightness — compare scenes in "
            "the same snow state, and treat red areas as candidates to verify, "
            "not confirmed slides.")
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(info)

        # one checkbox per detector — tick which change maps to compute. Each
        # ticked product adds its own layer; the two defaults fail in opposite
        # ways and are meant to be read together.
        cd_tips = {
            "mtcorr":
                "Texture: compares each before×after correlation against the "
                "distribution of ALL before×before pairs, pixel by pixel — "
                "natural variability is IN the reference distribution. The "
                "paper's most reliable detector (AUC 0.77–0.93). Needs 3+ "
                "before-scenes (uses up to 6). Blind to a debris sheet that "
                "uniformly brightens a smooth slope (no texture to lose) — "
                "that's what Brightness is for.",
            "tsint":
                "Brightness: how many σ the after-scene's backscatter sits "
                "outside the before-stack's own per-pixel distribution. Red = "
                "brighter after (fresh debris). Needs 2+ before-scenes. Sees "
                "the uniform brightening the mean-invariant correlations "
                "cannot; in turn fooled by scene-wide brightness drift the "
                "correlations shrug off — read the two together.",
            "intcorr":
                "Norm. diff: one reference before×before pair vs one "
                "before×after co-event pair (paper Eqs. 14–16). Texture-based "
                "2-scene fallback when there aren't enough scenes for the "
                "multi-temporal texture map.",
            "logratio":
                "Log-ratio: 10·log10(before/after) of window-averaged "
                "backscatter. Simplest 1-before-scene brightness fallback; "
                "red = brighter after, blue = darker after.",
        }
        self.cd_product_checks = {}
        checks_row = QVBoxLayout()
        checks_row.setContentsMargins(0, 0, 0, 0)
        for key, label, need, default in CD_PRODUCTS:
            # Surface the before-scene requirement in the label so it's visible
            # before ticking (the "before" count is what blocks a run). "N+" for
            # the detectors that also use extra before-scenes; a plain count for
            # log-ratio, which uses exactly one.
            req = "1 before" if need == 1 else f"{need}+ before"
            cb = QCheckBox(f"{label}  (needs {req})")
            cb.setChecked(default)
            cb.setToolTip(cd_tips[key])
            self.cd_product_checks[key] = cb
            checks_row.addWidget(cb)
        form.addRow("Compute", self._wrap(checks_row))

        self.cd_window_combo = QComboBox()
        for label, value in CD_WINDOWS:
            self.cd_window_combo.addItem(label, value)
        self.cd_window_combo.setCurrentIndex(1)
        self.cd_window_combo.setToolTip(
            "Window for the local statistics (multilook average / texture "
            "correlation), in pixels of the render resolution. Bigger = less "
            "speckle noise but change features smaller than the window fade. "
            "7×7 at 10 m px ≈ 70 m — close to the paper's 16×16 at 3 m.")
        form.addRow("Stat window", self.cd_window_combo)

        # compute the maps and log their statistics without adding any layer to
        # the canvas — for checking whether a method/window/filter combination
        # is worth rendering before you clutter the map with it
        self.cd_stats_only_check = QCheckBox("Compute stats only (no map layer)")
        self.cd_stats_only_check.setToolTip(
            "Run the detectors and print each product's coverage and anomaly "
            "spread to the Log, but don't add any raster layer to the map. "
            "Useful for comparing windows and filters quickly without piling "
            "up layers.")
        form.addRow(self.cd_stats_only_check)

        self.cd_btn = QPushButton("Compute change map")
        self.cd_btn.setEnabled(False)
        self.cd_btn.setToolTip(
            "Download raw (float) gamma-naught for the same-track scene pair — "
            "or stack — over the AOI and compute the ticked change products. "
            "Ticked table rows are honored when they fit the same-track rule "
            "(too few ticked before-scenes are topped up from the same track, "
            "logged); otherwise the ★ defaults are used. Speckle / blob / "
            "normalization "
            "filtering is taken from the Noise reduction panel. Each run adds "
            "new layers, so you can compare products, windows and filters.")
        self.cd_btn.clicked.connect(self._run_change_detection)
        form.addRow(self.cd_btn)

        # rec #5: merge the ascending + descending change maps so a scar lost to
        # layover in one geometry is recovered from the other. Workflow: compute
        # with an ascending after-scene, compute again with a descending one, then
        # Merge. Degrades honestly to one geometry where the terrain has only one.
        self.cd_merge_btn = QPushButton("Merge geometries (asc + desc)")
        self.cd_merge_btn.setEnabled(False)
        self.cd_merge_btn.setToolTip(
            "Combine the most recent ascending and descending change maps of each "
            "product into one, recovering scar pixels that layover blanked in a "
            "single orbit. Compute once with an ascending after-scene and once "
            "with a descending one first. If only one geometry has been computed "
            "(common in this terrain — many areas lack both passes), it still runs "
            "but flags that the opposite-facing slopes, possibly the source "
            "headscarp, are unrecovered.")
        self.cd_merge_btn.clicked.connect(self._merge_geometries_action)
        form.addRow(self.cd_merge_btn)
        return box

    # ---------- noise reduction (drop-down) ----------
    def _build_filter_box(self):
        """Collapsible 'Noise reduction' panel: the three SAR-specific noise
        levers that feed change detection, each switchable off. Speckle filter
        and radiometric normalization clean the analysis inputs/outputs; the
        blob sieve drops isolated single-pixel change flags. All three apply to
        the next Compute change map — see _cd_compute for where each hooks in."""
        box = QgsCollapsibleGroupBox("Noise reduction (speckle && false positives)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        form = QFormLayout(box)

        info = QLabel(
            "Reduces the salt-and-pepper and whole-scene drift that make raw "
            "SAR change maps noisy. All three apply to Compute change map and "
            "can be turned off here. Read together with a bigger Stat window "
            "and coarser Pixel size (Display panel), which also cut speckle.")
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(info)

        self.speckle_cd_combo = QComboBox()
        for label, value in SPECKLE_FILTERS:
            self.speckle_cd_combo.addItem(label, value)
        self.speckle_cd_combo.setCurrentIndex(1)     # Lee 5×5
        self.speckle_cd_combo.setToolTip(
            "Speckle filter applied to EACH scene before the detectors run — "
            "the single biggest noise lever, and the standard incoherent "
            "change-detection step. Lee is adaptive: it smooths flat areas "
            "fully but keeps edges and bright scars, and it matters most for "
            "the correlation detectors, where speckle artificially "
            "decorrelates windows and invents change. Median is a simpler "
            "fallback. Set to None to feed the raw γ⁰ (noisiest). Distinct "
            "from the Display panel's Render smoothing, which only cleans the "
            "on-screen amplitude preview.")
        form.addRow("Analysis speckle filter", self.speckle_cd_combo)

        self.blob_combo = QComboBox()
        for label, value in BLOB_MIN_AREAS:
            self.blob_combo.addItem(label, value)
        self.blob_combo.setCurrentIndex(2)           # 8 px
        self.blob_combo.setToolTip(
            "Minimum change-blob size. A slide is a connected patch of "
            "anomalous pixels; scattered single red pixels are residual "
            "speckle. Connected clusters smaller than this are cleared to "
            "no-change. 8 px at 10 m ≈ 800 m² — well below a mapped slide, "
            "well above single-pixel speckle. Off keeps every flagged pixel.")
        form.addRow("Min change area", self.blob_combo)

        self.radionorm_check = QCheckBox(
            "Radiometrically normalize brightness maps")
        self.radionorm_check.setChecked(True)
        self.radionorm_check.setToolTip(
            "Log-ratio and brightness-z only: subtract the scene-wide median "
            "so the unchanged background centers at zero. This removes the "
            "global brightness offset a whole-scene snow-state / soil-moisture "
            "shift introduces between the two acquisitions — the large red or "
            "blue washes that aren't real ground change. Assumes most of the "
            "scene is unchanged (robust to the minority that isn't). The "
            "correlation detectors are already mean-invariant, so this leaves "
            "them untouched.")
        form.addRow(self.radionorm_check)
        return box

    # ---------- Planetary Computer key (drop-down) ----------
    def _build_key_box(self):
        """A collapsible 'Planetary Computer account' panel. Sentinel-1 RTC is the
        one Planetary Computer collection this plugin uses that requires a (free)
        account subscription key to read pixels — the Sentinel-2/Landsat tab stays
        anonymous. NOT the same thing as the Planet account on the PlanetScope tab."""
        box = QgsCollapsibleGroupBox("Microsoft Planetary Computer key (optional)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        self.key_box = box
        form = QFormLayout(box)

        info = QLabel(
            'No login needed — Sentinel-1 RTC works anonymously. If you have an '
            'old Microsoft Planetary Computer subscription key, storing it here '
            'raises API rate limits; otherwise leave this blank. (Unrelated to '
            'your Planet Labs account on the PlanetScope tab.)')
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: palette(mid); }")
        form.addRow(info)

        self.key_edit = QLineEdit(
            self.settings.value("landslide/pc_subscription_key", "", type=str))
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText(
            "Subscription key (or set PC_SDK_SUBSCRIPTION_KEY)")
        self.key_edit.returnPressed.connect(self._test_key)
        form.addRow("Key", self.key_edit)

        self.key_btn = QPushButton("Test && save key")
        self.key_btn.setToolTip(
            "Request a Sentinel-1 RTC access token with this key. If it works, "
            "the key is saved to QGIS settings and exported as "
            "PC_SDK_SUBSCRIPTION_KEY for the search subprocess.")
        self.key_btn.clicked.connect(self._test_key)
        form.addRow(self.key_btn)

        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        form.addRow("Status", self.key_status)
        if self._pc_key():
            self._set_key_status("Key on file (not re-tested this session).", "info")
        return box

    def _set_key_status(self, text, tone="info"):
        self.key_status.setText(text)
        self.key_status.setStyleSheet(
            f"QLabel {{ color: {STATUS_COLORS.get(tone, 'palette(mid)')}; }}")

    def _pc_key(self):
        """Subscription key from the field (if built yet), else settings, else env."""
        field = getattr(self, "key_edit", None)
        if field is not None and field.text().strip():
            return field.text().strip()
        return (self.settings.value("landslide/pc_subscription_key", "", type=str)
                or os.environ.get("PC_SDK_SUBSCRIPTION_KEY", ""))

    def _test_key(self):
        if self._key_reply is not None:
            return                       # a check is already in flight
        key = self.key_edit.text().strip()
        if not key:
            self._set_key_status("Paste your Microsoft Planetary Computer key first.", "warn")
            return
        self._set_key_status("Testing key against the token endpoint…", "info")
        self.key_btn.setEnabled(False)
        req = QNetworkRequest(QUrl(PC_TOKEN_URL))
        req.setRawHeader(b"Ocp-Apim-Subscription-Key", key.encode())
        reply = QgsNetworkAccessManager.instance().get(req)
        self._key_reply = reply
        reply.finished.connect(lambda r=reply, k=key: self._key_tested(r, k))

    def _key_tested(self, reply, key):
        if reply is not self._key_reply:
            reply.deleteLater()
            return
        self._key_reply = None
        self.key_btn.setEnabled(True)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        err = reply.error()
        reply.deleteLater()
        if err == QNetworkReply.NoError and status == 200:
            self._save_key(key)
            self._set_key_status("✓ Key works — saved for SAR previews.", "success")
            self.key_box.setCollapsed(True)
        elif status in (401, 403):
            self._set_key_status(
                "✗ Key rejected — check it against your Microsoft Planetary "
                "Computer account page.", "error")
        else:
            self._set_key_status(
                f"Could not verify (HTTP {status or '—'}); nothing saved. "
                "Check your connection and try again.", "error")

    def _save_key(self, key):
        self.settings.setValue("landslide/pc_subscription_key", key)
        # the planetary_computer SDK in the search subprocess reads this env var
        # to sign restricted collections (task._clean_env copies os.environ)
        os.environ["PC_SDK_SUBSCRIPTION_KEY"] = key

    # ---------- inputs ----------
    def _copy_from_main(self):
        d = self.dock
        self.lat_edit.setText(d.lat_edit.text())
        self.lon_edit.setText(d.lon_edit.text())
        self.radius_spin.setValue(d.radius_spin.value())
        self.dt_edit.setDateTime(d.dt_edit.dateTime())
        self.pre_slider.setValue(d.pre_slider.value())
        self.post_slider.setValue(d.post_slider.value())

    def _collect(self):
        """Validate inputs and build the run_single.py CLI args for a Sentinel-1
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
        # own output subdir so this search.json never clashes with the other tabs'
        out = os.path.join(base_out, "sar")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return None
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return None
        # persist the subscription key and feed it to the child so the
        # planetary_computer SDK can sign sentinel-1-rtc items
        key = self.key_edit.text().strip()
        if key:
            self._save_key(key)
        os.makedirs(out, exist_ok=True)

        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{self.radius_spin.value():.2f}",
            "--pre-days", str(self.pre_slider.value()),
            "--post-days", str(self.post_slider.value()),
            "--prefer", "s1", "--search-only", "--out", out,
        ]
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
        # a new search = new AOI/event: drop any recorded per-geometry change maps
        # so the asc+desc merge can never combine rasters from different ground
        self._cd_results = []
        self.cd_merge_btn.setEnabled(False)
        self._preview_pix = None
        self.preview.setText("Search, then select a scene to preview it.")
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
        self.run_btn.setEnabled(bool(npre or npost))
        self.cd_btn.setEnabled(bool(npre and npost))
        self.iface.messageBar().pushInfo(
            "SAR", f"Found {npre} pre / {npost} post Sentinel-1 scene(s) "
                   f"(no cloud filter — radar sees through cloud).")

    # ---------- default pre/post pairing (same viewing geometry) ----------
    @staticmethod
    def _gap(c):
        g = c.get("gap_days")
        return 1e9 if g is None else g

    def _covers_event(self, c):
        """Does this scene's footprint actually image the event point?

        Adjacent frames of ONE pass share track number and date but cover
        different along-track ground segments — pairing by track alone can
        star a frame whose coverage misses the event entirely and shares zero
        pixels with the other side's frame (observed: 0% overlap on a real
        pair). Scenes without footprint metadata are not excluded."""
        result = self._search_result or {}
        try:
            lat, lon = float(result.get("lat")), float(result.get("lon"))
        except (TypeError, ValueError):
            return True
        geom = self.dock._qgs_geom(c.get("geometry"))
        if geom is None or geom.isEmpty():
            return True
        return geom.contains(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))

    def _pair_for(self, pre_all, post_all, mode, direction=None):
        """(pre, post, note) — best matching-geometry pair, optionally restricted
        to one orbit direction ('ascending'/'descending').

        Post: the scene nearest the event (freshest look at the slide). Pre: the
        nearest pre-event scene with MATCHING geometry per `mode` — same relative
        orbit (track) preferred, since only same-geometry pairs make brightness
        change mean ground change. Falls back (with a note) when nothing on the
        pre side matches."""
        if direction is not None:
            pre_all = [c for c in pre_all if c.get("orbit_state") == direction]
            post_all = [c for c in post_all if c.get("orbit_state") == direction]
        post = min(post_all, key=self._gap) if post_all else None
        if post is None:
            return (min(pre_all, key=self._gap) if pre_all else None), None, None
        note = None
        if mode == "track":
            match = [c for c in pre_all
                     if c.get("relative_orbit") == post.get("relative_orbit")]
            if not match:  # degrade to same direction before giving up
                match = [c for c in pre_all
                         if c.get("orbit_state") == post.get("orbit_state")]
        elif mode == "direction":
            match = [c for c in pre_all
                     if c.get("orbit_state") == post.get("orbit_state")]
        else:
            match = pre_all
        if not match and pre_all and direction is None:
            match = pre_all
            note = ("no pre scene matches the post scene's orbit "
                    f"({post.get('orbit_state')} track {post.get('relative_orbit')}) "
                    "— pairing across orbits; expect geometry differences, widen "
                    "the before window for a same-track pair")
        pre = min(match, key=self._gap) if match else None
        return pre, post, note

    def _default_picks(self):
        """([(side, candidate), …], [notes]) — what a bare Preview on map renders
        and what the table stars.

        'Both geometries' previews the best same-track pair on EACH orbit
        direction (four layers): slopes lost to layover in one geometry read
        cleanly in the other, and real change should show up in both pairs. The
        other modes give a single pre/post pair (see _pair_for)."""
        result = self._search_result or {}
        pre_all = result.get("pre", [])
        post_all = result.get("post", [])
        notes = []
        # only frames that image the event point are comparable candidates —
        # adjacent same-track frames cover different ground (see _covers_event)
        pre_cov = [c for c in pre_all if self._covers_event(c)]
        post_cov = [c for c in post_all if self._covers_event(c)]
        if (pre_all and not pre_cov) or (post_all and not post_cov):
            notes.append("no scene footprint contains the event point — "
                         "pairing by time only (check the footprints overlay)")
        pre_all = pre_cov or pre_all
        post_all = post_cov or post_all
        mode = self.orbit_combo.currentData()
        picks = []
        if mode == "both":
            for direction in ("ascending", "descending"):
                pre, post, _ = self._pair_for(pre_all, post_all, "track", direction)
                if pre and post:
                    picks += [("pre", pre), ("post", post)]
                elif pre or post:
                    notes.append(f"no complete same-track {direction} pair in the "
                                 f"window — geometry skipped (widen the sliders "
                                 f"to pick it up)")
            if picks:
                return picks, notes
            mode = "track"   # neither direction pairs -> plain same-track pair
        pre, post, note = self._pair_for(pre_all, post_all, mode)
        if note:
            notes.append(note)
        picks = [(s, c) for s, c in (("pre", pre), ("post", post)) if c]
        return picks, notes

    def _on_pairing_changed(self, *_):
        if self._search_result:
            self._fill_table(self._search_result)   # re-star the default pair

    def _on_render_changed(self, *_):
        # polarization changed: refresh the gallery + preview renders
        if self._search_result:
            self._load_gallery(self._search_result)
            self._preview_selected()

    # ---------- candidate table ----------
    def _fill_table(self, result):
        pre = result.get("pre", [])
        post = result.get("post", [])
        default_picks, _notes = self._default_picks()
        # surface how the starred pair brackets the seismic event time (rec #1)
        try:
            ev = self.dt_edit.dateTime().toString("yyyy-MM-ddTHH:mm:ss")
            summary = sar_pairing.summarize(default_picks, ev)
        except Exception:
            summary = ""
        self.pair_summary.setText(("Event bracket —  " + summary.replace("\n", "\n                 "))
                                  if summary else "")
        self.pair_summary.setVisible(bool(summary))
        star = {(side, c.get("id")) for side, c in default_picks}
        rows = [("pre", c) for c in pre] + [("post", c) for c in post]
        self.table.setRowCount(len(rows))
        for r, (side, c) in enumerate(rows):
            cid = c.get("id")
            is_star = (side, cid) in star
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else str(c["gap_days"])
            orbit = (c.get("orbit_state") or "")[:4]
            track = "" if c.get("relative_orbit") is None else str(c["relative_orbit"])
            marker = "★ " if is_star else "  "
            cells = [marker + side, date, gap, orbit, track, cid or ""]
            bg = (PRE_BG if side == "pre" else POST_BG)
            if is_star:
                bg = bg.darker(112)
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                item.setForeground(ROW_FG if is_star else MUTED_FG)
                if is_star:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(r, col, item)
            head = self.table.item(r, 0)
            head.setData(Qt.UserRole, c.get("thumb_url"))
            head.setData(Qt.UserRole + 1, cid)
            head.setData(Qt.UserRole + 2, side)
            head.setData(Qt.UserRole + 3, c.get("polarizations"))
            # checkbox = include this scene in Preview on map. The ★ same-track
            # pair starts ticked; tick others to compare more scenes at once.
            head.setFlags(head.flags() | Qt.ItemIsUserCheckable)
            head.setCheckState(Qt.Checked if is_star else Qt.Unchecked)
            if is_star:
                head.setToolTip(
                    "★ The default preview pair: post = nearest scene after the "
                    "event, pre = nearest before-scene from the same track, so "
                    "both look at the slope with identical geometry.")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ---------- render URLs ----------
    def _pol_for(self, cand_pols):
        """The polarization asset to render for a scene: the combo's pick when the
        scene carries it, else the scene's first available (e.g. HH at high
        latitudes)."""
        want = self.pol_combo.currentData()
        pols = [p.lower() for p in (cand_pols or [])]
        if not pols or want in pols:
            return want
        return pols[0]

    def _render_query(self, cand_pols, tif=False):
        """assets/expression + stretch query for the data API. Grayscale colormap
        for PNGs; the AOI GeoTIFF stays a single rescaled byte band (QGIS renders
        that as grayscale natively).

        dB mode maps 10*log10(γ⁰) — via natural log, the function PC's own
        Sentinel-1 render configs use (10/ln10 = 4.342944819) — onto the user's
        black/white dB range. Log display is the SAR standard: it hands the huge
        linear dynamic range back as midtones instead of crushed black/white."""
        pol = self._pol_for(cand_pols)
        if self.scale_combo.currentData() == "db":
            lo = self.db_min.value()
            hi = max(self.db_max.value(), lo + 1.0)
            expr = quote(f"4.342944819*log({pol})", safe="")
            q = (f"expression={expr}&asset_as_band=true"
                 f"&rescale={lo:g},{hi:g}&nodata=-32768")
        else:
            q = f"assets={pol}&rescale=0,{self.stretch_spin.value():g}&nodata=-32768"
        if not tif:
            q += "&colormap_name=gray"
        return q

    def _render_desc(self):
        """Human-readable render summary for labels/logs, e.g. 'dB -25–0'."""
        if self.scale_combo.currentData() == "db":
            return f"dB {self.db_min.value():g}–{self.db_max.value():g}"
        return f"γ⁰ 0–{self.stretch_spin.value():g}"

    def _with_key(self, url):
        key = self._pc_key()
        if key:
            url += ("&" if "?" in url else "?") + "subscription-key=" + key
        return url

    def _preview_url(self, cid, cand_pols, max_size=1024):
        return self._with_key(
            f"{PC_DATA_URL}/item/preview.png?collection={COLLECTION}&item={cid}"
            f"&{self._render_query(cand_pols)}&max_size={max_size}")

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
                f"<b>{'Before' if side == 'pre' else 'After'}</b> ({len(cands)})"))
            host = QWidget()
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0)
            cols = 3
            for i, c in enumerate(cands):
                grid.addWidget(self._make_tile(c), i // cols, i % cols)
            self.gallery_layout.addWidget(host)

    def _make_tile(self, c):
        date = (c.get("date") or "")[:10]
        gap = "" if c.get("gap_days") is None else f"gap {c['gap_days']}d"
        orbit = (c.get("orbit_state") or "")[:4]
        track = "" if c.get("relative_orbit") is None else f"t{c['relative_orbit']}"
        cid = c.get("id")
        tile = QToolButton()
        tile.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tile.setIconSize(QSize(128, 128))
        tile.setFixedWidth(150)
        tile.setAutoRaise(True)
        tile.setText(f"{date}\n{orbit} {track} · {gap}")
        tile.setToolTip(f"{cid}\n{date}  {orbit} track {track}  {gap}")
        tile.clicked.connect(lambda _=False, x=cid: self._select_row_by_id(x))
        if cid:
            url = self._preview_url(cid, c.get("polarizations"), max_size=512)
            baked = self._with_key(c["thumb_url"]) if c.get("thumb_url") else None
            fb = baked if (baked and baked != url) else None
            self._fetch_tile_thumb(tile, url, fallback=fb)
        else:
            tile.setText(tile.text() + "\n(no preview)")
        return tile

    def _fetch_tile_thumb(self, tile, url, fallback=None):
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
        self._gallery_replies.append(reply)
        reply.finished.connect(
            lambda r=reply, t=tile, fb=fallback: self._tile_thumb_loaded(r, t, fb))

    def _tile_thumb_loaded(self, reply, tile, fallback):
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
            return
        if fallback:   # render URL failed — try the baked thumbnail once
            self._fetch_tile_thumb(tile, fallback)
            return
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

    # ---------- preview pane ----------
    def _preview_selected(self):
        # keep the footprint overlay in sync with the selection (selected rows
        # only; all candidates when nothing is selected)
        if self.footprint_check.isChecked():
            self._draw_footprints()
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        head = self.table.item(rows[0].row(), 0)
        if head is None or not head.data(Qt.UserRole + 1):
            return
        url = self._preview_url(head.data(Qt.UserRole + 1),
                                head.data(Qt.UserRole + 3), max_size=1024)
        baked = self._with_key(head.data(Qt.UserRole)) if head.data(Qt.UserRole) else None
        self._preview_fallback = baked if (baked and baked != url) else None
        self._fetch_preview(url)

    def _fetch_preview(self, url):
        self._preview_pix = None
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
            fb = self._preview_fallback
            if fb:   # render failed — degrade to the baked thumbnail once
                self._preview_fallback = None
                self._fetch_preview(fb)
                return
            self.preview.setText("Preview unavailable for this scene.")
            return
        self._preview_fallback = None
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

    # ---------- preview on map (grayscale AOI GeoTIFF) ----------
    def _candidate_by_id(self, cid):
        for side in ("pre", "post"):
            for c in (self._search_result or {}).get(side, []):
                if c.get("id") == cid:
                    return side, c
        return None, None

    def _labeled(self, picks):
        """[(side, candidate), …] -> [(layer label, candidate), …]."""
        out = []
        for side, c in picks:
            date = (c.get("date") or "")[:10]
            orbit = (c.get("orbit_state") or "")[:4]
            track = c.get("relative_orbit")
            pol = self._pol_for(c.get("polarizations")).upper()
            label = (f"S1 {'before' if side == 'pre' else 'after'} {date} "
                     f"({orbit} t{track}, {pol})")
            out.append((label, c))
        return out

    def _preview_picks(self):
        """[(label, candidate), …] to render: the TICKED table rows, falling back
        to the default same-track before/after pair when nothing is ticked (see
        _default_pair). Row selection only drives the preview pane."""
        picks = []
        for r in range(self.table.rowCount()):
            head = self.table.item(r, 0)
            if head is None or head.checkState() != Qt.Checked:
                continue
            side, c = self._candidate_by_id(head.data(Qt.UserRole + 1))
            if c:
                picks.append((side, c))
        if not picks:
            picks, notes = self._default_picks()
            for note in notes:
                self._append_log("note: " + note)
        return self._labeled(picks)

    def _preview_row_on_map(self, item):
        """Double-click a row -> quick-preview exactly that one scene (ignores ticks)."""
        head = self.table.item(item.row(), 0)
        if head is None:
            return
        side, c = self._candidate_by_id(head.data(Qt.UserRole + 1))
        if c:
            self._render_scenes(self._labeled([(side, c)]), mode="preview")

    def _aoi_bbox(self):
        """(minx, miny, maxx, maxy, radius_km) of the search AOI in lon/lat."""
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

    def _preview_on_map(self):
        picks = self._preview_picks()
        if not picks:
            self._warn("Run Search first — no Sentinel-1 scene to preview.")
            return
        self._render_scenes(picks, mode="preview")

    def _run_full(self):
        picks = self._preview_picks()
        if not picks:
            self._warn("Run Search first — no Sentinel-1 scene to render.")
            return
        self._render_scenes(picks, mode="run")

    def _render_scenes(self, picks, mode="preview"):
        """Download + load the AOI amplitude render for each (label, candidate).

        mode='preview' is the FAST coarse look: a small fixed image (PREVIEW_PX),
        no client-side smoothing, dropped in from a throwaway temp file.
        mode='run' is the full-detail render at the chosen Pixel size + median
        filter, and SAVES each GeoTIFF under out/interactive/sar/amplitude so it
        survives the session. Both land in the same "SAR <pre>/<post> amplitude"
        folder, so a Run replaces the quick preview (and vice versa)."""
        bbox = self._aoi_bbox()
        if bbox is None:
            self._warn("Run Search first — no Sentinel-1 scene to preview.")
            return
        self.dock._ensure_network_timeout()   # AOI renders can be slow when cold
        # Folder for the amplitude layers: "SAR <pre>/<post> amplitude" from the
        # before/after dates in the picks (side read from the label _labeled() built).
        pre = post = ""
        for label, c in picks:
            d = (c.get("date") or "")[:10]
            if "before" in label:
                pre = d
            elif "after" in label:
                post = d
        self._amp_group = lg.name("SAR", lg.date_pair(pre, post), "amplitude")
        self._clear_preview_layers()
        minx, miny, maxx, maxy, radius = bbox
        if mode == "run":
            res = self.detail_combo.currentData() or 10
            k = self.smooth_combo.currentData()
            px = int(min(2048, max(128, round(radius * 2 * 1000 / res))))
            res_txt = f"{res} m px"
        else:                                    # quick preview: fixed small image
            px = PREVIEW_PX
            res = max(1, round(radius * 2 * 1000 / px))   # effective, for the log
            k = 0                                         # skip smoothing for speed
            res_txt = f"~{res} m px"
        self._preview_mode = mode
        self._preview_res = res
        self._preview_smooth = k
        self._preview_saved = []
        smooth = f", median {k}×{k}" if k else ""
        head = "Run (full detail)" if mode == "run" else "Preview (quick)"
        self._append_log(
            f"{head}: rendering {len(picks)} amplitude scene(s) over the AOI "
            f"({self._render_desc()} grayscale, {res_txt}{smooth})…")
        self.map_preview_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self._preview_failed = []
        self._tif_pending = len(picks)
        for label, c in picks:
            self._append_log(f"  {label}")
            url = self._with_key(
                f"{PC_DATA_URL}/item/bbox/{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}.tif"
                f"?collection={COLLECTION}&item={c['id']}"
                f"&{self._render_query(c.get('polarizations'), tif=True)}"
                f"&width={px}&height={px}")
            reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
            self._tif_replies.append(reply)
            reply.finished.connect(
                lambda r=reply, l=label, cand=c: self._tif_loaded(r, l, cand))

    def _amp_out_dir(self):
        """Output folder for saved Run amplitude GeoTIFFs, mirroring _collect's
        search dir: <output or project/out/interactive>/sar/amplitude."""
        base = self.dock.out_edit.text().strip() or os.path.join(
            self.dock.project_edit.text().strip(), "out", "interactive")
        return os.path.join(base, "sar", "amplitude")

    def _amp_save_path(self, c, res):
        """Persistent path for a Run amplitude GeoTIFF. The filename encodes
        date/orbit/track/polarization/resolution, so re-running a scene at the
        same settings overwrites its file instead of piling up copies."""
        out = self._amp_out_dir()
        os.makedirs(out, exist_ok=True)
        date = (c.get("date") or "")[:10] or "undated"
        orbit = (c.get("orbit_state") or "")[:4] or "orb"
        track = c.get("relative_orbit")
        pol = self._pol_for(c.get("polarizations")).upper()
        return os.path.join(out, f"S1_{date}_{orbit}_t{track}_{pol}_{res}m.tif")

    def _tif_loaded(self, reply, label, cand):
        if reply in self._tif_replies:
            self._tif_replies.remove(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        ok = reply.error() == QNetworkReply.NoError and status == 200
        data = bytes(reply.readAll())
        reply.deleteLater()
        added = False
        if ok and data:
            try:
                # Run saves a persistent GeoTIFF in the output folder; Preview
                # uses a throwaway temp file (fast, coarse look).
                if self._preview_mode == "run":
                    path = self._amp_save_path(cand, self._preview_res)
                    with open(path, "wb") as f:
                        f.write(data)
                else:
                    fd, path = tempfile.mkstemp(suffix=".tif",
                                                prefix="landslide_sar_")
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                k = self._preview_smooth
                if k:
                    self._apply_median(path, k)
                lyr = QgsRasterLayer(path, label)
                if lyr.isValid():
                    lg.add_to_group(lyr, self._amp_group)
                    self._preview_added.append(lyr)
                    if self._preview_mode == "run":
                        self._preview_saved.append(path)
                        self._append_log(
                            f"  loaded {label} → saved {os.path.basename(path)}")
                    else:
                        self._append_log(f"  loaded {label}")
                    added = True
            except OSError as e:
                self._append_log(f"  write failed for {label}: {e}")
        if not added:
            self._preview_failed.append(label)
            hint = " (HTTP 401/403 — check the key)" if status in (401, 403) else ""
            self._append_log(f"  could not render {label} (HTTP {status}){hint}")
        self._tif_pending -= 1
        if self._tif_pending <= 0:
            self._finish_map_preview()

    def _apply_median(self, path, k):
        """k×k median-filter band 1 of the downloaded AOI GeoTIFF, in place.

        Classic speckle suppression: the median kills isolated bright/dark
        pixels while preserving edges (a mean blur would smear them). Pure
        numpy — the window is gathered as k² shifted views and the median taken
        across them — since QGIS bundles numpy but not scipy. Only the data
        band is rewritten; the render's alpha/mask band (which carries the
        scene-footprint transparency) is left untouched. Best-effort: any
        failure just logs and leaves the unfiltered image."""
        try:
            import numpy as np
            from osgeo import gdal
            ds = gdal.Open(path, gdal.GA_Update)
            if ds is None:
                return
            band = ds.GetRasterBand(1)
            arr = band.ReadAsArray()
            if arr is None or arr.ndim != 2:
                return
            pad = k // 2
            padded = np.pad(arr, pad, mode="edge")
            stack = np.stack([padded[dy:dy + arr.shape[0], dx:dx + arr.shape[1]]
                              for dy in range(k) for dx in range(k)])
            med = np.median(stack, axis=0).astype(arr.dtype)
            band.WriteArray(med)
            band.FlushCache()
            ds = None
        except Exception as e:
            self._append_log(
                f"    median filter skipped ({type(e).__name__}: {e})")

    def _raise_cd_layer(self):
        """Keep the newest change maps above any amplitude preview layers —
        addMapLayer stacks new layers on top, so a Preview/Run render loaded
        after a change map would otherwise bury the overlays."""
        root = QgsProject.instance().layerTreeRoot()
        for lyr in reversed(self._cd_last_layers):
            try:
                node = root.findLayer(lyr.id())
            except (RuntimeError, AttributeError):
                continue                  # layer was removed/deleted
            if node is None:
                continue
            parent = node.parent() or root
            clone = node.clone()
            parent.insertChildNode(0, clone)
            parent.removeChildNode(node)

    def _finish_map_preview(self):
        self.map_preview_btn.setEnabled(bool(self._search_result))
        self.run_btn.setEnabled(bool(self._search_result))
        self._raise_cd_layer()
        if self._preview_added:
            self._zoom_to_aoi()
            kind = "full-detail" if self._preview_mode == "run" else "quick"
            msg = (f"Loaded {len(self._preview_added)} {kind} SAR amplitude "
                   f"scene(s) over the AOI. Toggle the layers to compare before "
                   f"vs after.")
            if self._preview_saved:
                msg += (f" Saved {len(self._preview_saved)} GeoTIFF(s) to "
                        f"{self._amp_out_dir()}.")
            if self._preview_failed:
                msg += f" {len(self._preview_failed)} scene(s) failed to load."
            self.iface.messageBar().pushInfo("SAR", msg)
        else:
            self._warn("Preview on map: no scene could be loaded.")

    def _clear_preview_layers(self):
        for lyr in self._preview_added:
            lg.remove_layer(lyr)
        self._preview_added = []

    def _zoom_to_aoi(self):
        bbox = self._aoi_bbox()
        if bbox is None:
            return
        minx, miny, maxx, maxy, _ = bbox
        rect = QgsRectangle(minx, miny, maxx, maxy)
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

    # ---------- change detection (incoherent, Jung & Yun 2020) ----------
    def _cd_pick(self, requested):
        """(products, post scene, [pre scenes nearest-first]) or None (warns).

        `requested` is the list of ticked product keys (Texture/Brightness/…).

        Post = nearest after-scene (ticked table rows win); pre = the
        before-scenes on the post scene's track, nearest first. Same track =
        same RELATIVE ORBIT (t123), which is stricter than same direction: two
        ascending tracks see the same slope from different angles. It is a hard
        requirement here (unlike visual preview) because these detectors compare
        pixels, so the viewing geometry must be identical.

        Ticked before-rows are honored, but only as a preference: if the ticked
        subset is too thin for the requested products the reference stack is
        topped up from the rest of the same-track scenes (with a note) rather
        than failing — ticking rows is how you drive the *preview*, and it
        should not silently starve the multi-temporal reference.

        Each requested product is kept only if enough same-track before-scenes
        exist for it (texture needs 3, brightness/norm-diff 2, log-ratio 1);
        the rest are skipped with a note naming a lower-requirement product.
        Returns None only if NONE of the ticked products can run."""
        result = self._search_result or {}
        pre_all, post_all = result.get("pre", []), result.get("post", [])
        ticked_pre, ticked_post = [], []
        for r in range(self.table.rowCount()):
            head = self.table.item(r, 0)
            if head is None or head.checkState() != Qt.Checked:
                continue
            side, c = self._candidate_by_id(head.data(Qt.UserRole + 1))
            if c:
                (ticked_pre if side == "pre" else ticked_post).append(c)
        post_pool = ticked_post or post_all
        if not post_pool:
            self._warn("Change detection needs a scene AFTER the event — none "
                       "in the search window.")
            return None
        # frames must image the event point: adjacent frames of one pass share
        # the track number but cover different ground — an off-event frame can
        # share ZERO pixels with the other side's frame (see _covers_event)
        cov = [c for c in post_pool if self._covers_event(c)]
        if cov:
            post_pool = cov
        else:
            self._append_log("note: no after-scene footprint contains the "
                             "event point — using the nearest anyway")
        post = min(post_pool, key=self._gap)
        track = post.get("relative_orbit")
        direction = post.get("orbit_state") or "?"
        same_track = [c for c in pre_all if c.get("relative_orbit") == track]
        cov = [c for c in same_track if self._covers_event(c)]
        off_frame = len(same_track) - len(cov)
        if cov:
            same_track = cov
        elif same_track:
            self._append_log("note: no same-track before-scene footprint "
                             "contains the event point — using nearest anyway")
            off_frame = 0
        pool = self._one_per_day(same_track, "same-track before-scene(s)")
        if not pool:
            self._warn(
                f"No before-scene on the post scene's relative orbit "
                f"(track t{track}, {direction}) — 'same track' means the same "
                f"repeat orbit, not just {direction}. Widen the before window "
                f"and search again, or tick an after-scene on a track that does "
                f"have before-scenes.")
            return None
        # ticks steer the preview; honor them here but never let a thin tick set
        # block a product the search actually has scenes for (see docstring)
        in_pool = {c.get("id") for c in pool}
        chosen = self._one_per_day([c for c in ticked_pre
                                    if c.get("id") in in_pool])
        if ticked_pre and not chosen:
            self._append_log(
                f"note: no ticked before-scene is a usable t{track} frame — "
                f"using the same-track scenes instead")
        need = max(CD_NEED[m] for m in requested)
        if chosen and len(chosen) < need:
            days = {(c.get("date") or "")[:10] for c in chosen}
            extra = [c for c in pool if (c.get("date") or "")[:10] not in days]
            if extra:
                self._append_log(
                    f"note: only {len(chosen)} ticked before-scene(s) on t{track}"
                    f" — added {len(extra)} more same-track scene(s) to the "
                    f"reference stack (the products need up to {need})")
                chosen = sorted(chosen + extra, key=self._gap)
        pre_pool = chosen or pool
        n = len(pre_pool)
        # keep each ticked product the same-track scene count can actually feed;
        # skip the rest with a note pointing at a lower-requirement detector so
        # the choice stays transparent (no silent auto-substitution)
        alt = {"mtcorr": "Norm. diff or Log-ratio", "tsint": "Log-ratio",
               "intcorr": "Log-ratio", "logratio": "more before-scenes"}
        products = []
        for m in requested:
            if n >= CD_NEED[m]:
                products.append(m)
            else:
                self._append_log(
                    f"note: {CD_NAMES[m]} needs {CD_NEED[m]} before-date(s) on "
                    f"track t{track} — only {n} available; skipped (try "
                    f"{alt[m]}, or widen the before window).")
        if not products:
            dates = ", ".join((c.get("date") or "")[:10] for c in pre_pool)
            self._append_log(
                f"Change detection stopped: the after-scene is on relative orbit "
                f"t{track} ({direction}), and only {n} before-date(s) on that "
                f"same orbit are available ({dates}). Scenes on the other tracks "
                f"in the table — even other {direction} ones — image these slopes "
                f"from a different angle, so they cannot be differenced pixel by "
                f"pixel." + (f" {off_frame} t{track} frame(s) were excluded: their "
                             f"footprint does not reach the event point."
                             if off_frame else ""))
            self._warn(
                f"Not enough before-scenes on the after-scene's own repeat orbit "
                f"(t{track}, {direction}): {n} date(s), and the ticked products "
                f"need {need}. Widen the before window and search again, tick a "
                f"lower-requirement product (Log-ratio needs 1), or tick an "
                f"after-scene on a better-covered track. See the log for detail.")
            return None
        count = (MT_MAX_PRE if any(m in ("mtcorr", "tsint") for m in products)
                 else max(CD_NEED[m] for m in products))
        return products, post, pre_pool[:count]

    def _one_per_day(self, cands, what=None):
        """`cands` nearest-first, one scene per acquisition day.

        Two frames from ONE acquisition (same day, same track — adjacent
        along-track scenes) are the same look at the ground, not extra temporal
        samples. A same-day reference pair would correlate near-perfectly and
        skew the multi-temporal reference distribution toward 'nothing ever
        changes', inflating false alarms. `what` names the scenes in the log
        note; pass None to collapse silently."""
        seen, out = set(), []
        for c in sorted(cands, key=self._gap):
            day = (c.get("date") or "")[:10]
            if day and day in seen:
                continue
            seen.add(day)
            out.append(c)
        if what and len(out) < len(cands):
            self._append_log(f"note: collapsed {len(cands) - len(out)} same-pass "
                             f"duplicate {what} — one scene per acquisition day")
        return out

    def _cd_pol(self, cands):
        """One polarization present in EVERY scene — ratio/correlation must
        compare like with like. The display combo's choice when possible."""
        sets = [set(p.lower() for p in (c.get("polarizations") or []))
                for c in cands]
        common = set.intersection(*sets) if sets else set()
        if not common:            # metadata absent — trust the combo choice
            return self.pol_combo.currentData()
        want = self.pol_combo.currentData()
        return want if want in common else sorted(common)[0]

    def _checked_products(self):
        """Ticked change-product keys, in CD_PRODUCTS order (texture first)."""
        return [k for k, cb in self.cd_product_checks.items() if cb.isChecked()]

    def _run_change_detection(self):
        if not self._search_result:
            self._warn("Run Search first — change detection uses the search "
                       "results.")
            return
        requested = self._checked_products()
        if not requested:
            self._warn("Tick at least one change product to compute "
                       "(Texture and Brightness are the recommended pair).")
            return
        picked = self._cd_pick(requested)
        if picked is None:
            return
        products, post, pres = picked
        bbox = self._aoi_bbox()
        if bbox is None:
            self._warn("Run Search first — no AOI to analyze.")
            return
        # roles: 'post' + 'pre0' (nearest before) … 'pre{n}'; pre_roles keeps
        # the nearest-first order the compute step relies on
        roles = {"post": post}
        pre_roles = []
        for i, c in enumerate(pres):
            roles[f"pre{i}"] = c
            pre_roles.append(f"pre{i}")
        pol = self._cd_pol(list(roles.values()))
        self.dock._ensure_network_timeout()
        res = self.detail_combo.currentData() or 10
        k = self.cd_window_combo.currentData()
        minx, miny, maxx, maxy, radius = bbox
        px = int(min(2048, max(128, round(radius * 2 * 1000 / res))))
        # snapshot the Noise-reduction panel now, so tweaking it mid-download
        # can't change what this run computes
        self._cd_meta = dict(
            products=products, pol=pol, k=k, res=res, roles=roles,
            pre_roles=pre_roles,
            speckle=self.speckle_cd_combo.currentData(),
            min_area=self.blob_combo.currentData(),
            radionorm=self.radionorm_check.isChecked(),
            stats_only=self.cd_stats_only_check.isChecked())
        self._cd_paths = {}
        self._cd_pending = len(roles)
        self._cd_last_layers = []
        self.cd_btn.setEnabled(False)
        self._append_log(
            "Change detection (" + " + ".join(CD_NAMES[m] for m in products) +
            f", {pol.upper()}, {k}×{k} window at {res} m px):")
        spk = self._cd_meta["speckle"]
        self._append_log(
            "  filters: speckle " +
            (f"{spk[0]} {spk[1]}×{spk[1]}" if spk else "off") +
            f"; min change area {self._cd_meta['min_area'] or 'off'}"
            f"{' px' if self._cd_meta['min_area'] else ''}"
            f"; radiometric normalize "
            f"{'on' if self._cd_meta['radionorm'] else 'off'}" +
            ("; STATS ONLY (no layers)" if self._cd_meta["stats_only"] else ""))
        for role in pre_roles + ["post"]:
            c = roles[role]
            self._append_log(f"  {role}: {(c.get('date') or '')[:10]} "
                             f"t{c.get('relative_orbit')}  {c.get('id')}")
        base = (roles["pre0"].get("gap_days") or 0) + (roles["post"].get("gap_days") or 0)
        if base > 36:
            self._append_log(
                f"  note: {base} days between the compared scenes — snow-state "
                "or seasonal drift over that span can read as change")
        # raw float32 gamma-naught: same bbox endpoint as the visual preview but
        # WITHOUT rescale/colormap, so pixel values are data, not display bytes
        for role, c in roles.items():
            url = self._with_key(
                f"{PC_DATA_URL}/item/bbox/"
                f"{minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f}.tif"
                f"?collection={COLLECTION}&item={c['id']}"
                f"&assets={pol}&nodata=-32768&width={px}&height={px}")
            reply = QgsNetworkAccessManager.instance().get(
                QNetworkRequest(QUrl(url)))
            self._cd_replies.append(reply)
            reply.finished.connect(
                lambda r=reply, ro=role: self._cd_tif_loaded(r, ro))

    def _cd_tif_loaded(self, reply, role):
        if reply in self._cd_replies:
            self._cd_replies.remove(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        ok = reply.error() == QNetworkReply.NoError and status == 200
        data = bytes(reply.readAll())
        reply.deleteLater()
        if ok and data:
            try:
                fd, path = tempfile.mkstemp(suffix=".tif",
                                            prefix=f"landslide_cd_{role}_")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                self._cd_paths[role] = path
            except OSError:
                pass
        if role not in self._cd_paths:
            hint = " (HTTP 401/403 — check the key)" if status in (401, 403) else ""
            self._append_log(
                f"  could not fetch the {role} scene (HTTP {status or '—'}){hint}")
        self._cd_pending -= 1
        if self._cd_pending <= 0:
            self._cd_compute()

    def _cd_compute(self):
        """All downloads landed: run the detector and add the styled result."""
        import numpy as np
        meta = self._cd_meta or {}
        roles = meta.get("roles", {})
        self.cd_btn.setEnabled(True)
        if len(self._cd_paths) < len(roles):
            self._warn("Change detection: could not download every scene — "
                       "see the log.")
            return
        k = meta["k"]
        pre_roles = meta.get("pre_roles") or []
        products = meta.get("products") or []
        speckle = meta.get("speckle")
        try:
            arrs, valids, shape, gt, proj = {}, {}, None, None, None
            for role, path in self._cd_paths.items():
                arr, valid, g, p = sar_change.read_band(path)
                if shape is None:
                    shape, gt, proj = arr.shape, g, p
                elif arr.shape != shape:
                    raise ValueError(
                        f"scene grids differ ({role}: {arr.shape} vs {shape})")
                # Gap 1: speckle-filter EACH input scene before the detectors —
                # cleans the raw γ⁰ that feeds the ratio/correlation math
                if speckle:
                    kind, sk = speckle
                    arr = (sar_change.lee_filter(arr, valid, sk) if kind == "lee"
                           else sar_change.median_filter(arr, valid, sk))
                arrs[role], valids[role] = arr, valid
            self._append_log(
                "  scenes downloaded" +
                (f", speckle-filtered ({speckle[0]} {speckle[1]}×{speckle[1]})"
                 if speckle else "") + " — computing…")
            pres = [arrs[r] for r in pre_roles]
            pvs = [valids[r] for r in pre_roles]
            outs = []
            for mkey in products:
                if mkey == "logratio":
                    out = sar_change.log_ratio(
                        pres[0], arrs["post"], pvs[0] & valids["post"], k)
                elif mkey == "intcorr":
                    # reference pair = the two before-scenes; co-event pair =
                    # the nearest before + the after scene (paper Eqs. 14-16)
                    rho_ref = sar_change.intensity_correlation(
                        pres[1], pres[0], pvs[1] & pvs[0], k)
                    rho_co = sar_change.intensity_correlation(
                        pres[0], arrs["post"], pvs[0] & valids["post"], k)
                    out = sar_change.corr_norm_diff(rho_ref, rho_co)
                elif mkey == "tsint":
                    # brightness anomaly vs the pre-stack's own distribution —
                    # the detector that SEES a uniform debris brightening,
                    # which the mean-invariant correlations are blind to
                    out = sar_change.intensity_zscore(
                        pres, arrs["post"], pvs, valids["post"], k)
                else:
                    # multi-temporal (paper §3.2.2): reference group = every
                    # before×before pair, co-event group = each before×after
                    refs, cos = [], []
                    for i in range(len(pres)):
                        for j in range(i + 1, len(pres)):
                            refs.append(sar_change.intensity_correlation(
                                pres[i], pres[j], pvs[i] & pvs[j], k))
                        cos.append(sar_change.intensity_correlation(
                            pres[i], arrs["post"], pvs[i] & valids["post"], k))
                    self._append_log(
                        f"  reference group: {len(refs)} before×before pairs; "
                        f"co-event group: {len(cos)} before×after pairs")
                    out = sar_change.multi_temporal_possibility(refs, cos)
                outs.append((mkey, out))
        except Exception as e:                       # noqa: BLE001 — surface, don't crash QGIS
            self._warn(f"Change detection failed: {type(e).__name__}: {e}")
            return
        # per-product 'anomalous' cut — the same threshold the stats below report
        # as candidates; the blob sieve keys off exactly these pixels
        SIG = {"logratio": ("abs", 3.0), "tsint": ("abs", 3.0),
               "intcorr": ("gt", 0.3), "mtcorr": ("gt", 0.8)}
        min_area = meta.get("min_area") or 0
        radionorm = meta.get("radionorm")
        stats_only = meta.get("stats_only")
        pol, track = meta["pol"].upper(), roles["post"].get("relative_orbit")
        pre_d = (roles[pre_roles[0]].get("date") or "")[:10]
        post_d = (roles["post"].get("date") or "")[:10]
        tail = f"(t{track} {pol}, {k}×{k})"
        # short product tag per change metric, for the layer-tree subfolder name
        CD_PRODUCT = {"logratio": "Log-ratio", "intcorr": "Int-corr",
                      "tsint": "Brightness-z", "mtcorr": "MT-corr"}
        cd_group = None                  # this compute's own folder (opened lazily)
        added = computed = 0
        for mkey, out in outs:
            if mkey == "logratio":
                label = f"S1 change log-ratio {pre_d}→{post_d} {tail}"
            elif mkey == "intcorr":
                pre2_d = (roles[pre_roles[1]].get("date") or "")[:10]
                label = f"S1 change int-corr {pre2_d}+{pre_d}→{post_d} {tail}"
            elif mkey == "tsint":
                label = (f"S1 change brightness z {len(pre_roles)}×pre→"
                         f"{post_d} {tail}")
            else:
                label = (f"S1 change MT int-corr {len(pre_roles)}×pre→"
                         f"{post_d} {tail}")

            # Gap 3: radiometric normalization — recenter the unchanged
            # background of the BRIGHTNESS maps to zero, cancelling the global
            # snow-state / soil-moisture brightness offset between the two
            # acquisitions (the correlation maps are already mean-invariant)
            if radionorm and mkey in ("logratio", "tsint"):
                f0 = out[np.isfinite(out)]
                if f0.size:
                    off = float(np.median(f0))
                    out = out - off
                    self._append_log(
                        f"  radiometric normalize: removed {off:+.2f}"
                        f"{' dB' if mkey == 'logratio' else 'σ'} global offset")

            # Gap 2: drop anomalous blobs smaller than the minimum area —
            # scattered single-pixel flags are residual speckle, not slides
            kind, thr = SIG[mkey]
            if min_area:
                sig = ((np.abs(out) > thr) if kind == "abs" else (out > thr))
                sig &= np.isfinite(out)
                before = int(sig.sum())
                # fill with each map's own 'normal' value (mtcorr centers on
                # ~0.5, the rest on 0) so cleared pixels read as no-change
                fill = 0.5 if mkey == "mtcorr" else 0.0
                out = sar_change.sieve_small_blobs(out, sig, min_area, fill=fill)
                kept = (((np.abs(out) > thr) if kind == "abs" else (out > thr))
                        & np.isfinite(out))
                self._append_log(
                    f"  min change area {min_area}px: removed "
                    f"{before - int(kept.sum())} of {before} anomalous pixel(s) "
                    f"in blobs smaller than {min_area}px")

            finite = out[np.isfinite(out)]
            cov = 100.0 * np.isfinite(out).mean() if out.size else 0.0

            if not stats_only:
                try:
                    fd, opath = tempfile.mkstemp(suffix=".tif",
                                                 prefix="landslide_change_")
                    os.close(fd)
                    sar_change.write_gtiff(opath, out, gt, proj)
                except Exception as e:               # noqa: BLE001
                    self._warn(f"Could not write {label}: {e}")
                    continue
                lyr = QgsRasterLayer(opath, label)
                if not lyr.isValid():
                    self._warn(f"{label}: result raster failed to load.")
                    continue
                self._style_cd_layer(lyr, mkey)
                if cd_group is None:
                    cd_group = lg.new_group(
                        lg.name("SAR", lg.date_pair(pre_d, post_d), "change"))
                sub = lg.subgroup(cd_group, CD_PRODUCT.get(mkey, mkey))
                lg.add_to(lyr, sub)
                self._cd_last_layers.append(lyr)
                added += 1
            computed += 1
            # record this geometry's map for a later asc+desc merge (rec #5) — only
            # for real computes: stats-only asked for NO layers, and merge writes
            # layers. Keep the in-memory array (true signed values, not read_band's
            # γ⁰>0 validity) plus the keys the merge must check for commensurability:
            # shape/gt (same AOI grid) and k/pol/res (same detector settings).
            if not stats_only:
                self._cd_results.append(dict(
                    mkey=mkey, track=track,
                    direction=(roles["post"].get("orbit_state") or ""),
                    out=out, gt=gt, proj=proj, shape=out.shape,
                    thr=float(SIG[mkey][1]), k=k, pol=pol, res=meta.get("res"),
                    pre_d=pre_d, post_d=post_d))
                self._cd_results = self._cd_results[-12:]
                self.cd_merge_btn.setEnabled(True)
            self._append_log(
                ("  layer added: " if not stats_only else "  computed: ") +
                f"{label} ({cov:.0f}% of AOI valid)")
            if cov < 5.0:
                self._warn(f"Change map: only {cov:.0f}% of the AOI has common "
                           "scene coverage — the compared frames barely "
                           "overlap. Turn on 'Show scene footprints' and tick "
                           "frames that cover your area of interest.")
            if not finite.size:
                continue
            # rec #2: for the signed brightness detectors, report the deposit
            # (backscatter↑) vs scar (↓) tail split so the analyst can key on the
            # fresh-debris signal (see sar_change.split_tails)
            _dep, _scar, _sm = sar_change.split_tails(out, mkey, SIG[mkey][1])
            if _sm["signed"]:
                self._append_log(
                    f"  sign split (|·|>{_sm['threshold']:.0f}): "
                    f"{_sm['n_deposit']} deposit px (backscatter↑, fresh debris) "
                    f"vs {_sm['n_scar']} scar px (↓) — favor deposit over smooth "
                    "snow/ice/bedrock; treat both as candidates over talus/vegetation")
            if mkey == "logratio":
                p2, p98 = np.percentile(finite, [2, 98])
                frac = 100.0 * (np.abs(finite) > 3.0).mean()
                self._append_log(
                    f"  spread: 2–98% = {p2:+.1f}…{p98:+.1f} dB; {frac:.1f}% "
                    "beyond ±3 dB (red = brighter after — fresh debris)")
            elif mkey == "tsint":
                p2, p98 = np.percentile(finite, [2, 98])
                frac = 100.0 * (np.abs(finite) > 3.0).mean()
                self._append_log(
                    f"  z spread: 2–98% = {p2:+.1f}σ…{p98:+.1f}σ; {frac:.1f}% "
                    "beyond ±3σ (red = brighter after — fresh debris)")
            elif mkey == "intcorr":
                p90, p99 = np.percentile(finite, [90, 99])
                frac = 100.0 * (finite > 0.3).mean()
                self._append_log(
                    f"  spread: 90% = {p90:.2f}, 99% = {p99:.2f}; {frac:.1f}% "
                    "above 0.3 (the colored candidates)")
            else:
                p90, p99 = np.percentile(finite, [90, 99])
                frac = 100.0 * (finite > 0.8).mean()
                self._append_log(
                    f"  possibility: 90% = {p90:.2f}, 99% = {p99:.2f}; "
                    f"{frac:.1f}% above 0.8 (the colored candidates — ~0.5 "
                    "is normal)")
        if stats_only:
            if computed:
                self.iface.messageBar().pushInfo(
                    "SAR", f"Computed {computed} change product(s) — stats only, "
                           "no layers added (see the Log).")
            return
        if not added:
            return
        self._zoom_to_aoi()
        self.iface.messageBar().pushInfo(
            "SAR", f"{added} change layer(s) added — colored = anomalous, "
                   "transparent = normal (per-layer stats in the Log). A "
                   "blank-looking map means 'no anomalies' — use Preview on "
                   "map for grayscale amplitude beneath. Wet snow / melt "
                   "between scenes also reads as change — verify candidates "
                   "against imagery.")

    def _style_cd_layer(self, lyr, method):
        """Pseudocolor with alpha: no-change fades out so the map shows through.

        Both brightness products (log-ratio, brightness z) share one
        convention: RED = brighter after the event (fresh debris is rougher →
        brighter), blue = darker after, transparent at no-change. The
        correlation methods get a transparent→red 'possibility' ramp — for
        the multi-temporal map anything below ~0.6 is the normal range of
        that pixel's own history, so it fades out entirely."""
        if method == "logratio":
            # log-ratio = 10·log10(pre/post): NEGATIVE means brighter after
            lo, hi = -6.0, 6.0
            stops = [(-6.0, "#b2182b", 255, "-6 dB (brighter after)"),
                     (-1.5, "#f4a582", 120, "-1.5"),
                     (0.0, "#f7f7f7", 0, "0 (no change)"),
                     (1.5, "#92c5de", 120, "+1.5"),
                     (6.0, "#2166ac", 255, "+6 dB (darker after)")]
        elif method == "tsint":
            lo, hi = -5.0, 5.0
            stops = [(-5.0, "#2166ac", 255, "-5σ (darker after)"),
                     (-2.0, "#92c5de", 120, "-2σ"),
                     (0.0, "#f7f7f7", 0, "0 (normal)"),
                     (2.0, "#f4a582", 120, "+2σ"),
                     (5.0, "#b2182b", 255, "+5σ (brighter after)")]
        elif method == "intcorr":
            lo, hi = 0.0, 0.6
            stops = [(0.0, "#ffffff", 0, "0 (unchanged)"),
                     (0.15, "#fdae61", 90, "0.15"),
                     (0.30, "#f46d43", 180, "0.30"),
                     (0.60, "#a50026", 255, "≥0.6 (strong change)")]
        else:
            lo, hi = 0.0, 1.0
            stops = [(0.0, "#ffffff", 0, "0"),
                     (0.60, "#ffffff", 0, "0.6 (normal range)"),
                     (0.80, "#fdae61", 120, "0.8"),
                     (0.90, "#f46d43", 200, "0.9"),
                     (1.00, "#a50026", 255, "1.0 (strong anomaly)")]
        items = []
        for value, color, alpha, text in stops:
            c = QColor(color)
            c.setAlpha(alpha)
            items.append(QgsColorRampShader.ColorRampItem(value, c, text))
        fn = QgsColorRampShader(lo, hi, None, QgsColorRampShader.Interpolated)
        fn.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(fn)
        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
        renderer.setClassificationMin(lo)
        renderer.setClassificationMax(hi)
        lyr.setRenderer(renderer)

    def _style_cd_confidence(self, lyr):
        """Discrete style for the asc+desc merge confidence raster: 1 recovered
        from a single orbit where the other was blind, 2 both orbits agree (high
        confidence), 3 orbits disagree (suspect); 0 (no change) fades out."""
        stops = [(0.0, "#f7f7f7", 0, "0  no change"),
                 (1.0, "#fdae61", 160, "1  recovered (single orbit, other blind)"),
                 (2.0, "#b2182b", 255, "2  agreement (both orbits)"),
                 (3.0, "#762a83", 220, "3  disagree (suspect)")]
        items = []
        for value, color, alpha, text in stops:
            c = QColor(color)
            c.setAlpha(alpha)
            items.append(QgsColorRampShader.ColorRampItem(value, c, text))
        fn = QgsColorRampShader(0.0, 3.0, None, QgsColorRampShader.Discrete)
        fn.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(fn)
        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
        renderer.setClassificationMin(0.0)
        renderer.setClassificationMax(3.0)
        lyr.setRenderer(renderer)

    def _merge_geometries_action(self):
        """Merge the most recent ascending + descending change maps of each product
        so a scar lost to layover in one viewing geometry is recovered from the
        other (report rec #5). Reads the in-memory computed arrays (true signed
        values, not read_band's γ⁰>0 validity). Degrades honestly to one geometry
        where only one pass was computed — common in this steep terrain."""
        if not self._cd_results:
            self._warn("Compute a change map first — ideally once with an "
                       "ascending after-scene and once with a descending one — "
                       "then Merge.")
            return
        NAME = {"logratio": "log-ratio", "intcorr": "int-corr",
                "tsint": "brightness-z", "mtcorr": "MT-corr"}
        # group by product, newest first; keep the latest result per orbit direction
        by_prod = {}
        for r in reversed(self._cd_results):
            sel = by_prod.setdefault(r["mkey"], {})
            # group by orbit direction; if the scene lacked orbit_state, fall back
            # to the track so two real geometries aren't collapsed under one key
            gk = r["direction"] or f"t{r['track']}"
            sel.setdefault(gk, r)                  # first (newest) per geometry wins
        merge_group = None
        added = 0
        for mkey, sel in by_prod.items():
            results = list(sel.values())
            # commensurability: 'strongest anomaly wins' only makes sense across
            # rasters on the SAME grid computed with the SAME detector settings
            def _grid(rr):
                return (rr["shape"], tuple(round(float(v), 6) for v in rr["gt"]),
                        rr["k"], rr["pol"], rr["res"])
            if any(_grid(rr) != _grid(results[0]) for rr in results):
                self._warn(
                    f"Merge {NAME.get(mkey, mkey)}: geometries were computed with "
                    "different AOI / window / polarization / resolution — recompute "
                    "them with identical settings, then merge. Skipped.")
                continue
            try:
                merged, conf, meta = sar_change.merge_geometries(
                    [r["out"] for r in results], mkey, results[0]["thr"])
            except Exception as e:               # noqa: BLE001 — surface, don't crash
                self._warn(f"Merge ({NAME.get(mkey, mkey)}) failed: "
                           f"{type(e).__name__}: {e}")
                continue
            dirs = "+".join(sorted({(r["direction"] or "?")[:4] for r in results}))
            r0 = results[0]
            gt, proj, pre_d, post_d = r0["gt"], r0["proj"], r0["pre_d"], r0["post_d"]
            self._append_log(
                f"Merge {NAME.get(mkey, mkey)} [{dirs}, "
                f"{meta['n_geometries']} geometry(ies)]: {meta['note']}")
            if meta["single_geometry"]:
                self._warn(
                    f"Merge {NAME.get(mkey, mkey)}: only one geometry available — "
                    "the opposite-facing slopes (possibly the source headscarp) "
                    "are unrecovered. Compute the other orbit direction if this "
                    "terrain has coverage.")
            else:
                self._append_log(
                    f"  recovered (single-orbit, other blind)={meta['n_single']} px · "
                    f"agree (both orbits)={meta['n_agree']} px · "
                    f"disagree/suspect={meta['n_conflict']} px")
            try:
                fd, mpath = tempfile.mkstemp(suffix=".tif", prefix="landslide_merge_")
                os.close(fd)
                sar_change.write_gtiff(mpath, merged, gt, proj)
                fd, cpath = tempfile.mkstemp(suffix=".tif", prefix="landslide_conf_")
                os.close(fd)
                sar_change.write_gtiff(cpath, conf, gt, proj)
            except Exception as e:               # noqa: BLE001
                self._warn(f"Could not write merged {NAME.get(mkey, mkey)}: {e}")
                continue
            mlyr = QgsRasterLayer(
                mpath, f"S1 change {NAME.get(mkey, mkey)} MERGED {dirs} "
                       f"{pre_d}→{post_d}")
            clyr = QgsRasterLayer(
                cpath, f"S1 MERGED confidence {dirs} {pre_d}→{post_d}")
            if not mlyr.isValid() or not clyr.isValid():
                self._warn(f"Merged {NAME.get(mkey, mkey)}: raster failed to load.")
                continue
            self._style_cd_layer(mlyr, mkey)
            self._style_cd_confidence(clyr)
            if merge_group is None:
                merge_group = lg.new_group(
                    lg.name("SAR", lg.date_pair(pre_d, post_d), "change merged"))
            sub = lg.subgroup(merge_group, NAME.get(mkey, mkey))
            lg.add_to(clyr, sub)          # confidence underneath
            lg.add_to(mlyr, sub)          # merged change on top
            self._cd_last_layers += [clyr, mlyr]
            added += 1
        if added:
            self.iface.messageBar().pushInfo(
                "SAR", f"Merged {added} product(s) across geometries — read the "
                "confidence layer: 2 = both orbits agree (strong), 1 = recovered "
                "from one orbit (other blind), 3 = orbits disagree (suspect).")

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

    def _selected_ids(self):
        """Scene ids of the currently selected table rows."""
        ids = set()
        for idx in self.table.selectionModel().selectedRows():
            head = self.table.item(idx.row(), 0)
            if head is not None and head.data(Qt.UserRole + 1):
                ids.add(head.data(Qt.UserRole + 1))
        return ids

    def _draw_footprints(self):
        """Draw footprints for the SELECTED table rows only — or for every
        candidate when nothing is selected. Redrawn on each selection change, so
        clicking a row isolates its outline instead of the full overlapping pile."""
        self._clear_footprints()
        result = self._search_result
        if not result:
            return
        sel = self._selected_ids()
        for side, outline in (("pre", "0,90,200"), ("post", "0,150,60")):
            cands = [c for c in result.get(side, []) if c.get("geometry")
                     and (not sel or c.get("id") in sel)]
            if not cands:
                continue
            lyr = QgsVectorLayer("Polygon?crs=EPSG:4326",
                                 f"Sentinel-1 footprints — {side}", "memory")
            pr = lyr.dataProvider()
            pr.addAttributes([
                QgsField("scene_id", QVariant.String),
                QgsField("date", QVariant.String),
                QgsField("gap_days", QVariant.Int),
                QgsField("orbit", QVariant.String),
                QgsField("track", QVariant.Int),
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
                                 c.get("orbit_state"), c.get("relative_orbit")])
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
    def _busy(self, on):
        self.progress.setVisible(on)
        self.search_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        have = bool(self._search_result and
                    (self._search_result.get("pre") or
                     self._search_result.get("post")))
        self.map_preview_btn.setEnabled((not on) and have)
        self.run_btn.setEnabled((not on) and have)
        self.cd_btn.setEnabled(
            (not on) and bool(self._search_result and
                              self._search_result.get("pre") and
                              self._search_result.get("post")))
        # merge is available only when idle and at least one geometry is recorded
        self.cd_merge_btn.setEnabled((not on) and bool(self._cd_results))

    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _warn(self, text):
        self.iface.messageBar().pushWarning("SAR", text)

    def teardown(self):
        for attr in ("_preview_reply", "_key_reply"):
            reply = getattr(self, attr, None)
            if reply is not None:
                try:
                    reply.abort()
                except RuntimeError:
                    pass
                setattr(self, attr, None)
        for reply in self._tif_replies + self._gallery_replies + self._cd_replies:
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._tif_replies = []
        self._gallery_replies = []
        self._cd_replies = []
        if self.task is not None:
            self.task.cancel()
