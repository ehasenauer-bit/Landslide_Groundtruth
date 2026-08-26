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

Auth: the tile POST + browse thumbnails need a Planet API key. Priority is an
explicit account login (email/password -> the account's api_key) or a key pasted in
the field (both saved to QgsSettings 'landslide/planet_api_key'), and only then the
PL_API_KEY env var as a fallback -- so signing in always overrides a stray ambient
key. The search subprocess authenticates the same way (the effective key is exported
as PL_API_KEY before it runs).
"""
import base64
import json
import math
import os
import random
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QUrl, QByteArray, QSize, QTimer
from qgis.PyQt.QtGui import QPixmap, QIcon
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
    QgsField, QgsFeature, QgsFillSymbol,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QVariant

from .task import PipelineTask
from .flow_layout import FlowRow
from . import layer_group as lg

# Planet's Data API tile service (undocumented, but stable — it's what Planet
# Explorer's "Add preview to map" relies on). POST scene ids to get a tile hash,
# then stream XYZ tiles from a per-hash layer. {0} is a subdomain shard 0-3.
TILE_HASH_URL = "https://tiles.planet.com/data/v1/layers"
TILE_XYZ_URL = "https://tiles{shard}.planet.com/data/v1/layers/{hash}/{{z}}/{{x}}/{{y}}"
ITEM_TYPE = "PSScene"

# Planet account login (mirrors the `planet` SDK's PlanetLegacyAuthClient.login):
# POST {email, password} as JSON and get back a JSON body {"token": "<jwt>"}, whose
# base64url payload carries the user's api_key. We do it in QGIS's Python via Qt
# networking (no `planet` SDK import needed here), then feed the recovered key into
# the same PL_API_KEY path the rest of the tab already uses.
PLANET_LOGIN_URL = "https://api.planet.com/v0/auth/login"
# The SDK tags the calling app in an X-Planet-App header; mirror it so the request
# looks like a normal SDK login.
PLANET_APP_HEADER = "landslide-groundtruth-qgis"

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
NAVY = "#1e3967"        # table header background
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
/* Styling a QComboBox makes Qt drop the native popup for one that grows to fit
   EVERY item — with a ledger of orders that runs off the bottom of the screen and
   can't be scrolled. combobox-popup: 0 restores the list-view popup, which honours
   setMaxVisibleItems() and gives it a scrollbar. */
QComboBox {{ combobox-popup: 0; }}
QComboBox QAbstractItemView {{
    border: 1px solid palette(mid); selection-background-color: {TEAL};
    selection-color: white;
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

# auto-resume wait after a Render-detail order times out, and a cap on how many
# times we'll auto-retry before falling back to the manual button (so a genuinely
# stuck order can't re-poll forever unattended).
_AUTO_RESUME_MS = 15 * 60 * 1000
_MAX_AUTO_RESUME = 3


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
        self._detail_labels = None       # side -> layer label for the pending SR render
        self._detail_dates = None        # side -> acquisition date for the pending SR render (folder name)
        self._preview_group = None       # layer-tree folder the tile preview loads into
        self._preview_layers = []        # preview layers (XYZ tiles / SR GeoTIFFs) on the map
        self._preview_extent = None      # union of previewed scene footprints (EPSG:4326)
        self._footprint_layers = []      # scene-footprint vector layers on the map
        self._gallery_replies = []       # in-flight quicklook-thumbnail requests
        self._pending = None             # last render.json 'pending' block (resumable order)
        self._autoresume_tries = 0       # auto-resume retries used for the current pending
        # AOI/date the last SR detail render used, so Re-tone can find its downloaded
        # clips even after the form has changed. None until a render has been launched.
        self._last_render = None
        # single-shot timer that fires an auto-resume once the wait elapses (armed
        # only in "auto15" mode); created before _build_ui so the combo's restore can
        # arm/disarm it safely.
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._auto_resume_fire)
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("planetTab")
        self.setStyleSheet(THEME_QSS)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        intro = QLabel(
            "Preview scenes at full resolution straight on the map — no order "
            "placed, no quota used. (Ordering into the review package comes later.)")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        # --- Planet account login (drop-down) ---
        root.addWidget(self._build_login_box())

        # --- Event (drop-down) — own copy; the button pulls from the other tab ---
        # "and", not "&": a group-box title takes & as a mnemonic marker too
        form = self._options_group(root, "Event location and time", collapsed=False)
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

        # '&&' because a single & is a Qt mnemonic marker — it renders as "location _date"
        copy_btn = QPushButton("⟵ Copy location && date from Sentinel-2 / Landsat tab")
        copy_btn.setToolTip("Pull latitude, longitude, radius, event time and the "
                            "before/after windows from the other tab so you don't "
                            "re-enter the same event.")
        copy_btn.clicked.connect(self._copy_from_main)
        form.addRow(copy_btn)

        # --- Search window (drop-down) — what dates to look at, per event ---
        form = self._options_group(root, "Search window", collapsed=False)

        self.auto_check = QCheckBox("Auto: tightest window (nearest clear scene each side)")
        self.auto_check.setToolTip(
            "Use only the clear scene nearest the event date on each side. The day "
            "sliders below then set the MAXIMUM days to search each side.")
        self.auto_check.toggled.connect(self._update_day_labels)
        form.addRow(self.auto_check)

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

        # --- Scene filters (drop-down) — which acquisitions the search will consider ---
        # Planet-specific; these were the old Advanced options.
        form = self._options_group(root, "Scene filters")

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
        self.coverage_combo.setCurrentIndex(self.coverage_combo.findData("point"))  # default
        self.coverage_combo.setToolTip(
            "AOI overlap: accept any scene overlapping the search box — recovers "
            "partial-coverage scenes near the event date. Epicentre: require the "
            "footprint to contain the point (can miss the nearest scenes).")
        form.addRow("Coverage", self.coverage_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Standard quality only", "standard")
        self.quality_combo.addItem("Include test-quality (match Planet Explorer)", "any")
        self.quality_combo.setCurrentIndex(self.quality_combo.findData("any"))  # default
        self.quality_combo.setToolTip(
            "Near a fresh event the nearest/clearest scenes are often published as "
            "'test' quality (looser geo/radiometric calibration). Fine for a visual "
            "review; eyeball before trusting reflectance/NDVI.")
        form.addRow("Quality", self.quality_combo)

        # --- SR detail rendering (drop-down) — how the ordered reflectance is drawn ---
        # Everything in here is free to change after a render: the clips stay on disk,
        # so Re-tone re-draws them without another order (see _retone_detail).
        form = self._options_group(root, "SR detail rendering")

        # Tone curve for the SR detail render. Both presets are detail-preserving; they
        # differ in WHAT they preserve, so this is a per-scene judgement call rather
        # than a better/worse setting. Keys must match run_single.py --planet-tone /
        # review_package.TONE_MODES (the plugin can't import the project, so the labels
        # are duplicated here — keep them in step).
        self.tone_combo = QComboBox()
        self.tone_combo.addItem("Highlight rolloff — untouched midtones, tamed "
                                "highlights", "knee")
        self.tone_combo.addItem("Highlight Optimized Natural Color — even detail, "
                                "softer contrast", "natural")
        self.tone_combo.addItem("None — plain linear stretch, no tone shaping", "linear")
        self.tone_combo.addItem("HDR (exposure fusion) — shadow AND snow detail at once",
                                "hdr")
        self.tone_combo.setToolTip(
            "Tone curve applied to the raw surface reflectance by 'Render detail'.\n"
            "• Highlight rolloff (knee) — THE DEFAULT. A plain linear stretch below "
            "0.165 reflectance, so midtones and shadows (vegetation, wet rock, shadowed "
            "slope, moraine) are arithmetically IDENTICAL to an unstretched render, and "
            "only brighter pixels get compressed — nothing clips to flat white. The cost "
            "is texture inside bright ice, which lands within ~10 DN of white; on a frame "
            "that is ALL ice, Auto-stretch below refits the curve so that stops mattering."
            "\n"
            "• Natural Color (cube root) — curves everywhere, so detail is even across "
            "the whole range: snow lands near DN 215 with real texture. The cost is "
            "global contrast — it brightens everything below 0.127 reflectance and "
            "darkens everything above, which reads as milky midtones.\n"
            "• None (linear) — turns OFF all of the above: a plain black/white stretch of "
            "the reflectance itself, no rolloff, cube root, desaturation, or S-curve. "
            "Bright ice clips to flat white, exactly as a naive stretch would — the point "
            "is to see the un-toned pixels. Honours the Manual stretch below, but not "
            "Auto-stretch or Contrast (neither applies).\n"
            "• HDR (exposure fusion) — renders the rolloff curve at several exposures and "
            "fuses them (Mertens exposure fusion), so a scene with deep shadow AND blown "
            "snow keeps detail at BOTH ends in one image. It fits its own dynamic range, so "
            "the white point / Auto-stretch / Manual stretch don't apply. Slower to render "
            "(a few exposures + the fusion), the local-tone-map answer when a single white "
            "point can't hold the scene.\n"
            "Stay on rolloff to read a scar on terrain with ice as context; switch to "
            "Natural Color when the feature is ON the ice, or to read the whole scene at "
            "once; pick None to check what the raw reflectance looks like unshaped. rolloff "
            "and Natural Color get a gentle S-curve contrast nudge (see 'Contrast' below) "
            "that cannot clip either end.\n"
            "Switching this after a render is FREE — see 'Re-tone'.")
        tone_mode = self.settings.value("landslide/planet_tone", "knee", type=str)
        idx = self.tone_combo.findData(tone_mode)
        self.tone_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.tone_combo.currentIndexChanged.connect(self._on_tone_changed)
        form.addRow("SR tone curve", self.tone_combo)

        # The gentle S-curve nudge both shaped curves get (run_single --planet-contrast,
        # review_package.CONTRAST = 1.15). ON by default keeps every existing render
        # byte-identical; OFF passes contrast 1.0 (identity). Greyed out for the None
        # curve, which never adds contrast.
        self.contrast_check = QCheckBox("Add S-curve contrast")
        self.contrast_check.setChecked(self.settings.value(
            "landslide/planet_contrast_scurve", True, type=bool))
        self.contrast_check.setToolTip(
            "ON (default): apply a gentle S-curve contrast nudge (strength 1.15, roughly a "
            "'+10' in photo-editor terms) to the Highlight rolloff and Natural Color "
            "renders. It pivots at mid-grey and pins BOTH ends, so it can never clip "
            "shadows to black or highlights to white.\n"
            "Turn it OFF to render the tone curve with nothing added on top — contrast "
            "1.0, an exact identity.\n"
            "No effect on the None tone curve, which never adds contrast. Switching this "
            "is FREE — see 'Re-tone'.")
        self.contrast_check.toggled.connect(self._on_tone_changed)
        form.addRow("Contrast", self.contrast_check)

        # The rolloff curve's fixed stretch assumes the frame contains terrain. On an AOI
        # that is ALL snow/ice there is nothing in its untouched linear zone, the whole
        # histogram lands in the shoulder, and the render comes out flat white. This lets
        # run_single fit the black/white points to the scene in that case — and only in
        # that case (see review_package.auto_stretch; a scene with dark ground keeps the
        # render it has today, byte for byte).
        self.autostretch_check = QCheckBox("Fit black/white points to an all-ice scene")
        self.autostretch_check.setChecked(self.settings.value(
            "landslide/planet_autostretch", True, type=bool))
        self.autostretch_check.setToolTip(
            "ON (default): before rendering, measure the scene and derive the rolloff "
            "curve's black and white points from it — but ONLY when the scene has no dark "
            "end to protect.\n"
            "• Under 0.5% of the AOI below 0.165 reflectance (an all-ice frame): the "
            "scene's own p0.1-p99 becomes the stretch. On a measured all-ice clip this "
            "took the image from 88% of pixels within 5 DN of white to 15x the contrast.\n"
            "• 5% or more below 0.165 (terrain, with or without ice in frame): left "
            "completely alone — the fixed curve is deliberately tuned for that case, and "
            "raising the white point there would dim every midtone to buy texture in ice "
            "that is only context. In between the two are blended.\n"
            "• Both sides are measured together, so before/after stay comparable under "
            "the Swipe tool.\n"
            "Turn it OFF to force the fixed 0.0-0.30 stretch back — mainly to read INSIDE "
            "a dark scar smaller than ~0.1% of the frame, which the fitted black point "
            "clips to near-black. Switching this is FREE — see 'Re-tone'.")
        self.autostretch_check.toggled.connect(self._on_tone_changed)
        form.addRow("Auto-stretch", self.autostretch_check)

        # Order the DN ('analytic') product and do our own TOA-reflectance + haze
        # conversion, instead of Planet's Surface Reflectance. SR over-corrects bright
        # snow/ice (impossible >1.0 reflectance, the cyan/pink cast); TOA applies no
        # atmospheric model so it can't over-correct, and dark-object subtraction removes
        # the residual blue haze (see run_single --planet-toa / planet_imagery). Unlike the
        # tone controls above this changes what is ORDERED, so it needs a fresh Render
        # detail (a Re-tone/Recall of an existing order can't apply it) and it USES QUOTA.
        self.toa_check = QCheckBox("Raw TOA + haze (skip Planet's SR correction)")
        self.toa_check.setChecked(self.settings.value(
            "landslide/planet_toa", False, type=bool))
        self.toa_check.setToolTip(
            "OFF (default): order Surface Reflectance (analytic_sr_udm2) — Planet's own "
            "atmospheric correction.\n"
            "ON: order the DN 'analytic' product (analytic_udm2) and convert it to "
            "top-of-atmosphere reflectance with a dark-object haze removal here, skipping "
            "Planet's SR correction. SR over-corrects bright snow/ice — impossible >1.0 "
            "reflectance and a cyan/pink cast — because atmospheric correction over bright "
            "targets is error-prone; TOA can't over-correct because it applies no model.\n"
            "This changes what is ORDERED, so it takes a fresh 'Render detail' and USES "
            "QUOTA — a Re-tone or Recall of an existing SR order can't switch to it.")
        self.toa_check.toggled.connect(
            lambda v: self.settings.setValue("landslide/planet_toa", v))
        form.addRow("Product", self.toa_check)

        # Hard override for the stretch, for when neither the fixed curve nor the fitted
        # one is what you want. 0 = leave it to the auto-stretch above.
        self.white_spin = QDoubleSpinBox()
        self.white_spin.setRange(0.0, 5.0)
        self.white_spin.setSingleStep(0.05)
        self.white_spin.setDecimals(3)
        self.white_spin.setSpecialValueText("auto")
        self.black_spin = QDoubleSpinBox()
        self.black_spin.setRange(0.0, 5.0)
        self.black_spin.setSingleStep(0.05)
        self.black_spin.setDecimals(3)
        self.black_spin.setSpecialValueText("auto")
        for sp, key in ((self.white_spin, "planet_white"),
                        (self.black_spin, "planet_black")):
            sp.setValue(self.settings.value(f"landslide/{key}", 0.0, type=float))
            sp.setToolTip(
                "Surface reflectance mapped to black and to white by the rolloff curve, "
                "overriding both the 0.0-0.30 default and the auto-stretch. 'auto' (0) "
                "leaves that end to the checkbox above; setting EITHER one takes the "
                "whole stretch under manual control. The knee stays where it is, so "
                "white is where the linear ramp ends and the highlight rolloff begins — "
                "pixels above it are compressed, never clipped. Free to change (Re-tone).")
            sp.valueChanged.connect(self._on_tone_changed)
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(QLabel("black"))
        srow.addWidget(self.black_spin, 1)
        srow.addWidget(QLabel("white"))
        srow.addWidget(self.white_spin, 1)
        form.addRow("Manual stretch", self._wrap(srow))

        # --- Order handling (drop-down) — a set-once policy, hence collapsed ---
        form = self._options_group(root, "Order handling")

        # What to do when a Render-detail order is still processing at the wait budget.
        # The order is already placed and paid for, so "resuming" only downloads it —
        # it never re-orders or spends more quota.
        self.autoresume_combo = QComboBox()
        self.autoresume_combo.addItem("Don't resume", "off")
        self.autoresume_combo.addItem("Resume manually (enable the button)", "manual")
        self.autoresume_combo.addItem("Auto-resume after 15 min", "auto15")
        self.autoresume_combo.setToolTip(
            "If a 'Render detail' order is still processing when the render times out "
            "(~30 min), the order is already placed and paid for. This picks how to "
            "finish it WITHOUT re-ordering:\n"
            "• Don't resume — leave it; recover it yourself in Planet Explorer.\n"
            "• Resume manually — enable the 'Resume order' button to download "
            "it when you're ready.\n"
            "• Auto-resume after 15 min — download it automatically 15 minutes later.\n"
            "Resuming never places a new order or uses extra quota.")
        # restore the saved choice WITHOUT firing the handler yet (the Resume button it
        # touches isn't built until the button row below) — connect at the end of _build_ui.
        mode = self.settings.value("landslide/planet_autoresume", "off", type=str)
        idx = self.autoresume_combo.findData(mode)
        self.autoresume_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Timed-out orders", self.autoresume_combo)

        # --- buttons ---
        # A wrapping row, not a QHBoxLayout: seven labelled buttons never fit on one
        # line in a narrow dock, and the font here is rescaled with the panel size,
        # so any fixed arrangement clips the labels at some scale. FlowRow gives each
        # button its natural width and spills onto further lines instead.
        btn_row = FlowRow()
        self.search_btn = QPushButton("Search (free)")
        self.search_btn.setObjectName("primary")
        self.search_btn.setToolTip(
            "Free Data API search for candidate before/after PlanetScope scenes. No "
            "orders placed, no quota used.")
        self.search_btn.clicked.connect(self._search)
        self.map_preview_btn = QPushButton("Preview on map")
        self.map_preview_btn.setObjectName("primary")
        self.map_preview_btn.setToolTip(
            "Stream the TICKED scene(s) onto the canvas at full resolution via "
            "Planet's tile service — no order, no download. Tick the scenes you "
            "want in the table, or double-click a row to preview just that scene. "
            "With nothing ticked, the nearest before & after scenes are used. "
            "Toggle the layers to compare.")
        self.map_preview_btn.setEnabled(False)
        self.map_preview_btn.clicked.connect(self._preview_on_map)
        self.detail_btn = QPushButton("Render detail (quota)")
        self.detail_btn.setToolTip(
            "Planet's free tiles are pre-rendered 8-bit RGB that clips bright terrain "
            "(snow/ice) to flat white — no brightness slider can recover detail that "
            "was already thrown away. This instead ORDERS the raw surface-reflectance "
            "bundle for the ticked scene(s), clips it to the AOI, and renders it from "
            "the raw pixels with the tone curve chosen in 'SR tone curve' above. "
            "With nothing ticked, the nearest before & after scenes are used. Places a "
            "Planet order (USES QUOTA) and can take a few minutes. Changing the tone "
            "curve afterwards does NOT need another order — use 'Re-tone'.")
        self.detail_btn.setEnabled(False)
        self.detail_btn.clicked.connect(self._render_detail)
        self.recall_btn = QPushButton("Recall order (free)")
        self.recall_btn.setObjectName("primary")
        self.recall_btn.setToolTip(
            "Load PlanetScope imagery you have ALREADY ORDERED for this event back onto "
            "the map — no new order, no quota. Every 'Render detail' order is kept in a "
            "shared cache, so a second look at an event is free forever, across projects "
            "and output folders.\n"
            "Uses the newest cached order for each side, or the one picked in 'Cached "
            "orders' below. Works offline when the clips are still on disk; if they were "
            "deleted, the order is downloaded again — also free.")
        self.recall_btn.clicked.connect(self._recall_detail)
        self.retone_btn = QPushButton("Re-tone (free)")
        self.retone_btn.setToolTip(
            "Re-render the LAST 'Render detail' with the tone curve currently selected "
            "above, reading the surface-reflectance clips already on disk. No new order, "
            "no quota, no network — seconds, not minutes. Use it to flip between the two "
            "curves on the same scene and keep whichever reads better.\n"
            "Re-tones the scene and AOI the render actually used, so it stays correct "
            "even if you've since changed the form fields.")
        self.retone_btn.setEnabled(False)
        self.retone_btn.clicked.connect(self._retone_detail)
        self.resume_btn = QPushButton("Resume order")
        self.resume_btn.setToolTip(
            "Finish a 'Render detail' order that was still processing when the render "
            "timed out. Downloads the already-placed order and loads it — no new "
            "order, no extra quota. Enabled once an order is pending, unless "
            "'Timed-out orders' (above) is set to 'Don't resume'.")
        self.resume_btn.setEnabled(False)
        self.resume_btn.clicked.connect(self._resume_detail)
        self.zoom_btn = QPushButton("Zoom to scene")
        self.zoom_btn.setToolTip(
            "Frame the previewed scene(s) on the canvas. Use this instead of QGIS's "
            "own 'Zoom to Layer', which zooms out to the whole world for XYZ tile "
            "layers like these previews.")
        self.zoom_btn.setEnabled(False)
        self.zoom_btn.clicked.connect(self._zoom_to_preview)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        for b in (self.search_btn, self.map_preview_btn, self.detail_btn,
                  self.recall_btn, self.retone_btn, self.resume_btn, self.zoom_btn,
                  self.cancel_btn):
            btn_row.addWidget(b)
        root.addWidget(btn_row)

        # --- cached orders (already paid for) ---
        # A picker rather than only "newest": an event often has several orders (a
        # first look, a re-order after widening the AOI, both sides separately), and
        # any of them can be put back on the canvas for free. Populated from the
        # ledger by Search/Recall, so it also answers "have I already ordered here?"
        # BEFORE spending quota on Render detail.
        recall_row = QHBoxLayout()
        recall_row.addWidget(QLabel("Cached orders:"))
        self.recall_combo = QComboBox()
        self.recall_combo.addItem("Newest cached order per side", None)
        self.recall_combo.setToolTip(
            "PlanetScope orders already paid for near this location. Pick one and hit "
            "'Recall order (free)' to load exactly it, or leave it on 'Newest' to load "
            "the most recent before & after. Empty means nothing has been ordered here "
            "yet — the first 'Render detail' is the only one that costs quota.")
        self.recall_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        # The ledger is cumulative — every order this account has ever paid for near
        # the AOI shows up here — so cap the drop-down and let it scroll instead of
        # growing past the bottom of the screen. Needs 'combobox-popup: 0' in the
        # theme, or Qt ignores this and renders one unscrollable list.
        self.recall_combo.setMaxVisibleItems(12)
        self.recall_combo.setEnabled(False)
        self.recall_btn.setEnabled(False)
        recall_row.addWidget(self.recall_combo, 1)
        root.addLayout(recall_row)

        self.footprint_check = QCheckBox("Show scene footprints on map")
        self.footprint_check.setToolTip(
            "Draw scene footprints (before = blue, after = green) plus the AOI "
            "box, so you can see whether a strip actually covers the epicentre. "
            "With table rows selected, only THOSE scenes' footprints are drawn — "
            "click a row to isolate its strip, Ctrl/Shift-click for several, "
            "click in empty table space to show all candidates again. A single "
            "PlanetScope strip is only a few km wide.")
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
        tl.addWidget(self._section(
            "Candidate scenes  (★ = nearest each side; tick the scenes to preview on the map)"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Side", "Date (UTC)", "Gap (d)", "Cloud %", "Scene ID"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Extended selection: click a scene to preview exactly it, Ctrl/Shift-click to
        # preview several (e.g. a specific before + after, or two candidates to compare).
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.itemDoubleClicked.connect(self._preview_row_on_map)
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

        # Wire the auto-resume combo now that the Resume button it toggles exists.
        self.autoresume_combo.currentIndexChanged.connect(self._on_autoresume_changed)
        self._refresh_resume_btn()
        # Match the greying of Contrast/Auto-stretch to the restored tone curve (None
        # disables both) — the combo's setCurrentIndex above ran before the handler was
        # connected, so it didn't fire.
        self._sync_tone_controls()
        # Show what this account already owns from the moment the panel opens, so a
        # previously-ordered event can be recalled without searching first.
        self._refresh_recall_combo()

    def _wrap(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _options_group(self, root, title, collapsed=True):
        """Add a collapsible options group ("drop-down") to `root`; return its form.

        The options on this tab split into four kinds that are set on completely
        different schedules — the search window changes per event, the scene filters
        per site, the render settings once you've found a look you like, and the
        order-timeout policy essentially never. Flat in one form they were fifteen
        rows deep and the per-event fields were buried among knobs nobody touches
        twice, so each kind gets its own drop-down and only the per-event ones start
        open.

        setSaveCollapsedState(False) plus an explicit default matches
        _build_login_box and dock.py's boxes: the panel opens the same shape every
        time rather than inheriting a state set weeks ago on a different event."""
        box = QgsCollapsibleGroupBox(title)
        box.setSaveCollapsedState(False)
        box.setCollapsed(collapsed)
        root.addWidget(box)
        form = QFormLayout(box)
        form.setContentsMargins(6, 4, 6, 4)
        return form

    def _update_day_labels(self, *_):
        suffix = " (max)" if self.auto_check.isChecked() else ""
        self.pre_lbl.setText(f"{self.pre_slider.value()} d{suffix}")
        self.post_lbl.setText(f"{self.post_slider.value()} d{suffix}")

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
        # start collapsed only when actually signed in (an explicit account/pasted
        # key); a bare PL_API_KEY env var leaves the box open to prompt sign-in
        box.setCollapsed(bool(self._stored_key()))
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

        # Prefill from the last successful login so credentials survive between
        # sessions (stored in QgsSettings, same plaintext store as the API key).
        self.pass_edit = QLineEdit(
            self.settings.value("landslide/planet_pass", "", type=str))
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
        # Seed from the *stored* key only (not the env var) so an account login takes
        # priority over any ambient PL_API_KEY.
        self.key_edit = QLineEdit(
            self.settings.value("landslide/planet_api_key", "", type=str))
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText("Planet API key (or set PL_API_KEY)")
        self.key_edit.setToolTip(
            "Needed for the full-res map preview and browse thumbnails, and passed "
            "to the search subprocess. Filled in automatically when you log in; "
            "saved to QGIS settings. An account login or a key typed here takes "
            "priority over the PL_API_KEY environment variable, which is only used "
            "as a fallback when you haven't signed in.")
        self.key_edit.textEdited.connect(self._mark_manual_key)
        form.addRow("API key", self.key_edit)

        # If we came back signed in, immediately override any ambient PL_API_KEY in
        # this process so every consumer (previews, thumbnails, the search subprocess)
        # uses the signed-in account's key rather than a stray env key inherited at
        # launch — without this, an early consumer could read the env key first.
        stored = self._stored_key()
        if stored:
            os.environ["PL_API_KEY"] = stored

        self.login_status = QLabel()
        self.login_status.setWordWrap(True)
        form.addRow("Status", self.login_status)
        self._refresh_login_state()
        if self._auth_source() == "env":
            self._set_login_status(
                "A PL_API_KEY environment variable is set and will be used as a "
                "fallback. Log in to use your Planet account instead.", "warn")
        return box

    def _set_login_status(self, text, tone="info"):
        self.login_status.setText(text)
        self.login_status.setStyleSheet(
            f"QLabel {{ color: {STATUS_COLORS.get(tone, 'palette(mid)')}; }}")

    def _refresh_login_state(self):
        """Reflect the auth source honestly in the box title + Log out button: an
        account login and a pasted key read as 'signed in'; a bare PL_API_KEY env var
        reads as a fallback an account login would override, not as being signed in."""
        src = self._auth_source()
        user = self.settings.value("landslide/planet_user", "", type=str)
        # Log out only makes sense for an explicit stored key, not the env fallback.
        self.logout_btn.setEnabled(bool(self._stored_key()))
        if src == "account":
            self.login_box.setTitle(
                f"Planet account — signed in{f' ({user})' if user else ''}")
        elif src == "manual":
            self.login_box.setTitle("Planet account — using a pasted API key")
        elif src == "env":
            self.login_box.setTitle(
                "Planet account — using PL_API_KEY (env) · log in to use your account")
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
        req.setRawHeader(b"X-Planet-App", PLANET_APP_HEADER.encode())
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
        # Persist the whole login (email + password + recovered key) so the next
        # session comes back signed in without re-entering anything. Tag the source as
        # 'account' so this key outranks any ambient PL_API_KEY env var.
        self.settings.setValue("landslide/planet_api_key", api_key)
        self.settings.setValue("landslide/planet_auth_source", "account")
        self.settings.setValue("landslide/planet_user", user)
        self.settings.setValue("landslide/planet_pass", self.pass_edit.text())
        os.environ["PL_API_KEY"] = api_key
        self._set_login_status(f"✓ Signed in as {user}.", "success")
        self._refresh_login_state()
        self.login_box.setCollapsed(True)

    @staticmethod
    def _api_key_from_jwt(data):
        """Pull `api_key` out of the response Planet's /v0/auth/login returns.

        The endpoint returns JSON {"token": "<jwt>"} (matching the `planet` SDK's
        PlanetLegacyAuthClient); the JWT is header.payload.signature and its
        base64url-encoded middle part is a JSON payload carrying the account's
        api_key. Some responses may be a bare JWT string, so fall back to that."""
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        try:
            obj = json.loads(text)
            jwt = obj.get("token") if isinstance(obj, dict) else obj
        except ValueError:
            jwt = text.strip('"')          # bare JWT string, not a JSON envelope
        if not isinstance(jwt, str) or not jwt:
            return None
        try:
            payload = jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)   # restore base64 padding
            claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
            return claims.get("api_key")
        except (ValueError, IndexError):
            return None

    def _planet_logout(self):
        self.key_edit.clear()
        self.pass_edit.clear()
        self.settings.remove("landslide/planet_api_key")
        self.settings.remove("landslide/planet_auth_source")
        self.settings.remove("landslide/planet_pass")
        os.environ.pop("PL_API_KEY", None)
        # If a PL_API_KEY env var is set outside this process (e.g. via launchctl /
        # the shell), it reappears on the next QGIS launch and becomes the fallback
        # again — the panel will say so rather than pretending you're signed out.
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
        self.pre_slider.setValue(d.pre_slider.value())
        self.post_slider.setValue(d.post_slider.value())

    def _stored_key(self):
        """An *explicit* Planet API key the user provided — recovered from an account
        login or pasted by hand, and persisted to QgsSettings. Deliberately excludes
        the ambient PL_API_KEY env var so an account login takes priority over a stray
        environment key."""
        field = getattr(self, "key_edit", None)
        if field is not None and field.text().strip():
            return field.text().strip()
        return self.settings.value("landslide/planet_api_key", "", type=str)

    def _env_key(self):
        """The ambient PL_API_KEY environment variable — used only as a fallback."""
        return os.environ.get("PL_API_KEY", "").strip()

    def _api_key(self):
        """Effective Planet API key for requests: an explicit account/pasted key wins
        over the ambient PL_API_KEY env var (which is a fallback only)."""
        return self._stored_key() or self._env_key()

    def _auth_source(self):
        """How the effective key was obtained: 'account' (email/password login),
        'manual' (pasted key), 'env' (PL_API_KEY fallback), or '' (nothing yet)."""
        src = self.settings.value("landslide/planet_auth_source", "", type=str)
        if self._stored_key():
            # a stored key with no recorded source is a pre-existing paste
            return src if src in ("account", "manual") else "manual"
        if self._env_key():
            return "env"
        return ""

    def _apply_key_to_env(self, key):
        """Publish `key` to the child subprocess + in-process SDK via PL_API_KEY, and
        persist it to QgsSettings — but only when it's an explicit account/pasted key,
        never promoting the ambient env var into the stored (account) slot."""
        if not key:
            return
        os.environ["PL_API_KEY"] = key
        if self._auth_source() != "env":
            self.settings.setValue("landslide/planet_api_key", key)

    def _mark_manual_key(self, text):
        """User is typing an API key by hand → record the source as a manual paste so
        the account/manual/env distinction in the login panel stays honest. (Fires on
        user edits only, not the setText() a successful login does, so it never
        clobbers the 'account' source.)"""
        if text.strip():
            self.settings.setValue("landslide/planet_auth_source", "manual")
        self._refresh_login_state()

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
        # feed the effective key (an account/pasted key wins over the ambient
        # PL_API_KEY) to the child so `planet.Planet()` authenticates as that account
        # without a separate `planet auth login`
        self._apply_key_to_env(self._api_key())
        os.makedirs(out, exist_ok=True)

        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{self.radius_spin.value():.2f}",
            "--pre-days", str(self.pre_slider.value()),
            "--post-days", str(self.post_slider.value()),
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
        self.detail_btn.setEnabled(bool(npre or npost))
        # Surface what's already been ordered here BEFORE offering to spend quota:
        # if this event has cached orders, Recall is the free way to see it again.
        cached = self._refresh_recall_combo() or 0
        if cached:
            self._append_log(
                f"{cached} PlanetScope order(s) here are already paid for — "
                f"'Recall order (free)' loads them back onto the map without touching "
                f"your quota.")
        self.iface.messageBar().pushInfo(
            "PlanetScope", f"Found {npre} pre / {npost} post candidate scene(s) "
                           f"(no orders placed)."
            + (f" {cached} order(s) here are already paid for — use Recall."
               if cached else ""))

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
            # checkbox = include this scene in Preview on map. The ★ nearest
            # scene each side starts ticked; tick others to preview several at once.
            head.setFlags(head.flags() | Qt.ItemIsUserCheckable)
            head.setCheckState(Qt.Checked if is_top else Qt.Unchecked)
            if is_top:
                head.setToolTip(
                    "★ Nearest clear scene on this side — ticked by default for "
                    "the before/after preview. Tick more rows to preview several.")
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
        # keep the footprint overlay in sync with the selection (selected rows
        # only; all candidates when nothing is selected)
        if self.footprint_check.isChecked():
            self._draw_footprints()
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
        """[(layer_name, [scene id]), …] to stream onto the map.

        Preview the TICKED table rows — tick a scene's checkbox to include it, tick
        several to compare. With nothing ticked, fall back to the ★ nearest scene on
        each side so the button still gives a sensible before/after pair. Row
        selection only drives the browse-image preview pane."""
        result = self._search_result or {}
        picks = []                          # [(side, cid), …] in table order
        for r in range(self.table.rowCount()):
            head = self.table.item(r, 0)
            if head is None or head.checkState() != Qt.Checked:
                continue
            side = head.data(Qt.UserRole + 2)
            cid = head.data(Qt.UserRole + 1)
            if cid:
                picks.append((side, cid))
        if not picks:                       # nothing ticked -> nearest each side
            for side in ("pre", "post"):
                ranked = self._rank(result.get(side, []))
                if ranked and ranked[0].get("id"):
                    picks.append((side, ranked[0]["id"]))
        return [(self._label_for(side, cid), [cid]) for side, cid in picks]

    def _label_for(self, side, cid):
        date = self._date_for(side, cid)
        return f"PlanetScope {'before' if side == 'pre' else 'after'} {date}".strip()

    def _preview_row_on_map(self, item):
        """Double-click a row -> preview exactly that one scene (ignores ticks)."""
        head = self.table.item(item.row(), 0)
        if head is None:
            return
        side = head.data(Qt.UserRole + 2)
        cid = head.data(Qt.UserRole + 1)
        if cid:
            self._render_picks([(self._label_for(side, cid), [cid])])

    def _date_for(self, side, cid):
        for c in (self._search_result or {}).get(side, []):
            if c.get("id") == cid:
                return (c.get("date") or "")[:10]
        return self._date_from_id(cid)      # fall back to the id itself (no search needed)

    @staticmethod
    def _date_from_id(cid):
        """PlanetScope scene id -> acquisition date, e.g. '20240131_210809_72_2479' ->
        '2024-01-31'. '' if the id isn't date-prefixed. Lets a layer be dated straight from
        render.json's scene ids, with no dependency on the current search result."""
        p = (cid or "")[:8]
        return f"{p[:4]}-{p[4:6]}-{p[6:8]}" if len(p) == 8 and p.isdigit() else ""

    def _preview_on_map(self):
        self._render_picks(self._preview_picks())

    def _render_picks(self, picks):
        key = self._api_key()
        if not key:
            self._warn("Set a Planet API key (above) to stream tiles onto the map.")
            return
        if not picks:
            self._warn("Run Search first — no PlanetScope scene to preview.")
            return
        # Folder for the tile preview layers: "Planet <pre>/<post> preview" when the
        # picks give a clean before/after pair, else just "Planet preview". The side
        # comes from the label _label_for() built; the date from the search result.
        pre = post = ""
        for label, ids in picks:
            side = "pre" if "before" in label else "post" if "after" in label else None
            if side and ids:
                if side == "pre":
                    pre = self._date_for("pre", ids[0])
                else:
                    post = self._date_for("post", ids[0])
        self._preview_group = lg.name("Planet", lg.date_pair(pre, post), "preview")
        self._clear_preview_layers()
        self._preview_extent = None
        self._append_log(f"Preview on map: requesting tiles for {len(picks)} scene(s)…")
        self.map_preview_btn.setEnabled(False)
        self._tile_pending = len(picks)
        for name, ids in picks:
            self._append_log(f"  {name}")
            self._request_tile_layer(name, ids, key)

    def _request_tile_layer(self, name, ids, key):
        """POST scene ids -> tile hash, then add the XYZ layer (async, off the GUI).

        Authenticate with the api_key as a QUERY PARAM, the way the rest of the tile
        service does (the XYZ tile GETs, the browse thumbnails) and Planet's own
        qgis-planet-plugin does: tiles.planet.com/data/v1/layers rejects HTTP Basic
        auth with a 401. The Basic header is kept as a harmless belt-and-suspenders
        fallback (the Data API accepts it)."""
        item_type_ids = [f"{ITEM_TYPE}:{i}" for i in ids]
        body = QByteArray(("ids=" + quote(",".join(item_type_ids))).encode())
        req = QNetworkRequest(QUrl(TILE_HASH_URL + "?api_key=" + quote(key)))
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
        bounds = None
        if ok and data:
            try:
                obj = json.loads(data)
                tile_hash = obj.get("name")
                bounds = obj.get("bounds")   # [w, s, e, n] scene footprint
            except (ValueError, AttributeError):
                tile_hash = None
        if not tile_hash:
            # Surface Planet's own error text so a 401/403 tells us *why* (bad key vs
            # the account lacking tile/streaming rights to the scene) instead of a
            # generic guess.
            detail = ""
            if data:
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict):
                        detail = (msg.get("message") or msg.get("error") or "")
                except (ValueError, AttributeError):
                    detail = data[:200].decode("utf-8", "replace").strip()
            hint = ""
            if status in (401, 403):
                hint = (" — the key was rejected: either it's invalid or your Planet "
                        "account isn't licensed to stream these PlanetScope scenes "
                        "(being able to search a scene doesn't grant tile access)")
            self._append_log(f"    tile request failed for {name} (HTTP {status})"
                             f"{f': {detail}' if detail else ''}{hint}")
        else:
            self._add_tile_layer(name, tile_hash, key)
            self._grow_preview_extent(bounds)
        self._tile_pending -= 1
        if self._tile_pending <= 0:
            self.map_preview_btn.setEnabled(True)
            if self._preview_layers:
                # Frame the actual scene footprint, not the AOI: a PlanetScope strip
                # is only a few km wide and can sit well off the epicentre, so zooming
                # to the AOI can leave the imagery off-screen (blank canvas).
                self.zoom_btn.setEnabled(True)
                if self._preview_extent is not None:
                    self._zoom_to_rect(self._preview_extent)
                else:
                    self._zoom_to_aoi()

    # ---------- SR detail render (order + Highlight Optimized Natural Color) ----------
    def _detail_picks_by_side(self):
        """{'pre': [ids], 'post': [ids]} to order & render, from the TICKED rows.

        Grouped by side because the SR render composites per side (one before layer,
        one after layer), unlike the tile preview which streams each scene alone.
        With nothing ticked, fall back to the ★ nearest scene each side — same
        default as the free tile preview."""
        result = self._search_result or {}
        picks = {"pre": [], "post": []}
        for r in range(self.table.rowCount()):
            head = self.table.item(r, 0)
            if head is None or head.checkState() != Qt.Checked:
                continue
            side = head.data(Qt.UserRole + 2)
            cid = head.data(Qt.UserRole + 1)
            if cid and side in picks:
                picks[side].append(cid)
        if not picks["pre"] and not picks["post"]:
            for side in ("pre", "post"):
                ranked = self._rank(result.get(side, []))
                if ranked and ranked[0].get("id"):
                    picks[side].append(ranked[0]["id"])
        return picks

    def _render_detail(self):
        """Order the ticked (or ★) scene(s), clip to the AOI, and load the Highlight
        Optimized Natural Color render from raw surface reflectance onto the map.

        This is the detail-preserving alternative to the free tile preview: it shells
        out to run_single.py --planet-render, which places a Planet order (uses quota)
        and renders the cube-root tone curve from the raw SR bands so bright terrain
        keeps its texture instead of clipping to white."""
        picks = self._detail_picks_by_side()
        if not picks["pre"] and not picks["post"]:
            self._warn("Run Search first, then tick the scene(s) to render.")
            return
        aoi = self._aoi()
        if aoi is None:
            self._warn("Run Search first (need the AOI location).")
            return
        key = self._api_key()
        if not key:
            self._warn("Set a Planet API key (above) to order & render SR detail.")
            return
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        out = os.path.join(base_out, "planet")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return
        self._apply_key_to_env(key)
        os.makedirs(out, exist_ok=True)

        lat, lon, radius = aoi
        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{radius:.2f}",
            "--prefer", "planet", "--planet-render", "--out", out,
        ] + self._tone_args()
        if self.toa_check.isChecked():
            args.append("--planet-toa")   # order DN + our TOA/haze instead of Planet SR
        if picks["pre"]:
            args += ["--pre-scene-ids", ",".join(picks["pre"])]
        if picks["post"]:
            args += ["--post-scene-ids", ",".join(picks["post"])]
        # Remember exactly what this render was for, so a later Re-tone reads the same
        # workdir even if the form has moved on since (event_id derives from --datetime).
        self._last_render = dict(lat=lat, lon=lon, radius=radius, when=when,
                                 event_id=None)
        # remember a label per side (built from our own search result) so the loaded
        # GeoTIFF layers get the same "PlanetScope before <date>" naming as the tiles
        self._detail_labels = {
            side: (self._label_for(side, ids[0]) if ids else None)
            for side, ids in picks.items()
        }
        # …and the acquisition date per side, so the loaded layers land in a folder
        # named for the pre/post dates (e.g. "Planet 7-20/7-21 HONC"). Kept across a
        # Re-tone (same scenes) so the tone switch only changes the product suffix.
        self._detail_dates = {
            side: (self._date_for(side, ids[0]) if ids else "")
            for side, ids in picks.items()
        }
        self._append_log(
            f"Render detail: ordering {len(picks['pre'])} pre + {len(picks['post'])} "
            f"post SR scene(s), clipping to the AOI, and rendering Highlight Optimized "
            f"Natural Color from the raw reflectance. This places a Planet order (uses "
            f"quota) and can take a few minutes…")
        # a fresh order supersedes any previous pending one (and its auto-resume state)
        self._pending = None
        self._autoresume_tries = 0
        if self._resume_timer.isActive():
            self._resume_timer.stop()
        self._busy(True)
        self.detail_btn.setEnabled(False)
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="render.json")
        self.task.logLine.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_detail_done)
        self.task.taskTerminated.connect(self._on_detail_done)
        QgsApplication.taskManager().addTask(self.task)

    def _on_detail_done(self):
        self._busy(False)
        result = getattr(self.task, "result", None)
        self.task = None
        if not result:
            self._append_log("Render detail finished with no result.")
            # keep any prior pending order resumable (don't strand the button disabled)
            self._refresh_resume_btn()
            self._refresh_retone_btn()
            self._refresh_recall_combo()
            return
        for note in result.get("notes", []):
            self._append_log("note: " + note)
        # Say plainly which sides cost nothing, so it's obvious when a render was served
        # from the cache rather than bought again.
        reused = result.get("reused") or {}
        if reused:
            self._append_log(
                "  no quota spent — served from order(s) already paid for: "
                + ", ".join(f"{s} {o[:12]}" for s, o in reused.items()))
        # 'available' is present on recall runs; otherwise re-read the ledger so a
        # brand-new order joins the picker immediately.
        self._refresh_recall_combo(result.get("available"))
        # Which curve actually produced these GeoTIFFs. render.json records it, so a
        # stale combo selection can't mislabel the layers. The "natural" fallback is for
        # a render.json written before tone modes existed — those were always cube-root,
        # so it is NOT the current default leaking in here.
        tone = result.get("tone") or "natural"
        tone_word = {"knee": "rolloff", "natural": "natural",
                     "linear": "linear", "hdr": "HDR"}.get(tone, "natural")
        # Two rolloff renders of the same scene can now differ in their stretch as well
        # as their curve, so say which one this is in the layer name — otherwise a fitted
        # and an unfitted layer sit on the canvas under identical labels. A nonzero black
        # is scene-fitted on knee but a manual choice on linear (which never auto-fits).
        stretch = result.get("stretch") or {}
        if stretch.get("black"):
            kind = "stretch" if tone == "linear" else "fitted"
            tone_word += f", {kind} {stretch['black']:.2f}-{stretch.get('white', 0):.2f}"
        # DN/TOA render (--planet-toa) vs Planet SR: stamp it so a TOA layer is obvious
        # next to an SR one on the canvas. render.json carries the flag (set from the
        # actual product, so recall/re-tone of a TOA order are labelled too).
        toa = bool(result.get("toa"))
        if toa:
            tone_word += ", TOA"
        # Date the layers (and their folder) from the scenes render.json says were actually
        # composited, so a recall or re-tone still gets "PlanetScope before <date>" naming
        # even when we didn't launch it here (e.g. after a plugin reload cleared the labels
        # we cache at render time). The scene id carries the date, so no search is needed.
        scenes = result.get("scenes") or {}
        labels = dict(self._detail_labels or {})
        dates = dict(self._detail_dates or {})
        for s in ("pre", "post"):
            ids = scenes.get(s) or []
            if ids and not dates.get(s):
                d = self._date_from_id(ids[0])
                dates[s] = d
                labels.setdefault(
                    s, f"PlanetScope {'before' if s == 'pre' else 'after'} {d}".strip())
        product = {"knee": "Roll off", "natural": "HONC",
                   "linear": "None", "hdr": "HDR"}.get(tone, "HONC") + (" TOA" if toa else "")
        group = lg.name("Planet", lg.date_pair(dates.get("pre"), dates.get("post")), product)
        # replace whatever the previous preview (tiles or SR) put on the map
        self._clear_preview_layers()
        self._preview_extent = None
        loaded = 0
        for side in ("pre", "post"):
            path = result.get(side)
            if not path or not os.path.exists(path):
                continue
            label = labels.get(side) \
                or f"PlanetScope {'before' if side == 'pre' else 'after'}"
            label += f" · SR detail ({tone_word})"
            lyr = QgsRasterLayer(path, label)
            if lyr.isValid():
                lg.add_to_group(lyr, group)
                self._preview_layers.append(lyr)
                loaded += 1
                self._append_log(f"  loaded {label}")
            else:
                self._append_log(f"  could not open the rendered {side} layer")
        if loaded:
            # The GeoTIFFs are clipped to the AOI box, so framing the AOI frames the
            # render exactly (no per-layer extent bookkeeping needed).
            self.zoom_btn.setEnabled(True)
            self._zoom_to_aoi()
            shown = {"knee": "Highlight rolloff",
                     "natural": "Highlight Optimized Natural Color",
                     "linear": "None (plain linear stretch)"}.get(
                         tone, "Highlight Optimized Natural Color")
            other = "Natural Color" if tone == "knee" else "Highlight rolloff"
            self.iface.messageBar().pushInfo(
                "PlanetScope", f"Loaded {loaded} SR detail layer(s) — {shown}, "
                f"rendered from raw surface reflectance. Toggle before/after to "
                f"compare, or switch the tone curve and hit Re-tone to see {other} "
                f"on the same pixels (free).")
        elif not (result.get("pending") or {}).get("orders"):
            # nothing loaded AND nothing left processing -> a real failure
            self._warn("Render detail: no layer could be loaded (see the log).")
        # Capture any order still processing so the Resume button (or auto-resume) can
        # finish it later without re-ordering. render.json's 'pending' is {} when clear.
        pending = result.get("pending") or {}
        self._pending = pending if pending.get("orders") else None
        self._refresh_resume_btn()
        # clips are on disk now, so the free tone switch is available
        self._refresh_retone_btn()
        if self._has_pending():
            side_word = " & ".join(self._pending["orders"])
            self.iface.messageBar().pushInfo(
                "PlanetScope",
                f"A PlanetScope order ({side_word}) is still processing. It's saved "
                f"in your Planet account — resume it (no re-order) to finish.")
            if self._autoresume_mode() == "auto15" and not self._resume_timer.isActive():
                self._arm_auto_resume()

    # ---------- recall orders already paid for (no order, no quota) ----------
    def _recall_aoi(self):
        """(lat, lon, radius_km) for a recall: the last search's AOI, else the form.

        Falling back to the form matters here — recalling imagery you already own
        shouldn't require running a search first, since the coordinates alone identify
        which event's orders to load."""
        aoi = self._aoi()
        if aoi is not None:
            return aoi
        try:
            return (float(self.lat_edit.text().strip()),
                    float(self.lon_edit.text().strip()),
                    float(self.radius_spin.value()))
        except (TypeError, ValueError):
            return None

    def _ledger(self):
        """The project's planet_cache module, or None.

        Imported straight into QGIS's Python — safe, unlike the imagery stack, because
        planet_cache is pure stdlib path/JSON bookkeeping. Loaded by explicit file path
        rather than via sys.path so it always tracks the configured project dir instead
        of a stale copy from a previous one."""
        project = self.dock.project_edit.text().strip()
        path = os.path.join(project, "planet_cache.py")
        if not path or not os.path.exists(path):
            return None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_landslide_planet_cache", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception as e:
            self._append_log(f"could not read the Planet order cache: {e}")
            return None

    def _refresh_recall_combo(self, entries=None):
        """Fill 'Cached orders' with the orders already paid for, this AOI's first.

        Refreshed after a search — so you can see what this account already owns here
        BEFORE spending quota on Render detail — and after any render/recall, so a new
        order shows up straight away. `entries` comes from render.json's 'available'
        block when a run just produced one; otherwise the ledger is read directly.

        Only orders whose delivered footprint COVERS the epicentre are listed (the
        strict pc.entries(require_point=True) filter), so the picker answers "which
        paid-for orders actually image THIS event?" instead of the whole cumulative
        ledger. When no AOI is known yet (no lat/lon in the form, no prior search) there
        is nothing to filter on, so the entire ledger is shown. Trade-off, chosen
        deliberately: an order for a different location no longer appears here — set the
        form to that location to bring its orders back into range."""
        combo = getattr(self, "recall_combo", None)
        if combo is None:
            return
        near = entries
        pc = self._ledger()
        if near is None:
            if pc is None:
                return
            aoi = self._recall_aoi()
            project = self.dock.project_edit.text().strip()
            base_out = self.dock.out_edit.text().strip() or os.path.join(
                project, "out", "interactive")
            try:
                # Pick up orders downloaded before the shared cache existed so they
                # appear in the picker too. Scoped to the output dirs, NOT the whole
                # project — this runs on the GUI thread and walking venv/ would stall it.
                pc.adopt([base_out, os.path.join(project, "out"),
                          os.path.join(project, "Output")], log=lambda *_: None)
                near = (pc.entries(lat=aoi[0], lon=aoi[1], radius_km=aoi[2],
                                   require_point=True)
                        if aoi else pc.entries())
            except Exception as e:
                self._append_log(f"could not list cached Planet orders: {e}")
                return
        keep = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Newest cached order per side", None)
        n = 0

        def _add(e):
            oid, side = e.get("order_id"), e.get("side")
            if not oid or side not in ("pre", "post"):
                return 0
            combo.addItem(e.get("label") or oid, (side, oid))
            return 1

        for e in near or []:
            n += _add(e)
        if keep is not None:
            idx = combo.findData(keep)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        combo.setEnabled(n > 0)
        self.recall_btn.setEnabled(n > 0 and self.task is None)
        return n

    def _recall_detail(self):
        """Load imagery ALREADY ORDERED for this event back onto the map via
        run_single.py --planet-recall. No search, no new order, no quota.

        Targets the AOI/date in the form (that is what identifies the event) and the
        order picked in 'Cached orders', if any — pinning one side still fills the
        other from the newest cached order, so you never lose a before/after pair by
        picking. Reuses _on_detail_done so a recalled render lands on the canvas
        exactly like a fresh one."""
        aoi = self._recall_aoi()
        if aoi is None:
            self._warn("Enter a valid latitude/longitude (or run Search) first — "
                       "Recall needs the location to know which orders to load.")
            return
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        out = os.path.join(base_out, "planet")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return
        # No key is required when the clips are still on disk, but pass one through if
        # we have it so a cached order whose files were deleted can be re-downloaded
        # (also free) instead of failing.
        self._apply_key_to_env(self._api_key())
        os.makedirs(out, exist_ok=True)

        lat, lon, radius = aoi
        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{radius:.2f}",
            "--prefer", "planet", "--planet-recall", "--out", out,
        ] + self._tone_args()
        picked = self.recall_combo.currentData()
        if picked:
            side, oid = picked
            args += [f"--recall-{side}-order", oid]
        # a recalled order is what Re-tone should now target, same as a fresh render
        self._last_render = dict(lat=lat, lon=lon, radius=radius, when=when,
                                 event_id=None)
        # the recalled scenes need not be in the current search result, so let
        # _on_detail_done fall back to generic before/after labels
        self._detail_labels = None
        self._append_log(
            "Recall: loading PlanetScope scenes already ordered for this event"
            + (f" (order {picked[1][:12]})" if picked else "")
            + " — no search, no order, no quota…")
        self._busy(True)
        self.detail_btn.setEnabled(False)
        self.recall_btn.setEnabled(False)
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="render.json")
        self.task.logLine.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_detail_done)
        self.task.taskTerminated.connect(self._on_detail_done)
        QgsApplication.taskManager().addTask(self.task)

    # ---------- resume a timed-out SR order (no re-order, no extra quota) ----------
    def _autoresume_mode(self):
        return self.settings.value("landslide/planet_autoresume", "off", type=str)

    # ---------- tone curve (free re-render of an SR detail already on disk) ----------
    def _tone_mode(self):
        return self.tone_combo.currentData() or "knee"

    def _tone_label(self):
        return self.tone_combo.currentText()

    def _tone_args(self):
        """The render-appearance arguments for run_single.py, as a list.

        One helper for all four paths that render (Render detail, Re-tone, Recall,
        Resume) because they must agree: a tone or stretch setting that reached only
        some of them would leave the layer on the canvas silently disagreeing with the
        controls that claim to describe it. Either spin box set above 0 is passed
        through and takes the stretch under manual control (run_single then skips the
        auto-stretch on its own, so the flags don't need to be mutually exclusive here)."""
        args = ["--planet-tone", self._tone_mode()]
        if not self.autostretch_check.isChecked():
            args.append("--planet-no-auto-stretch")
        # Off -> render with contrast 1.0 (identity); on -> omit so run_single keeps the
        # default 1.15 and the output stays byte-identical. Harmless for the None curve,
        # which ignores contrast either way.
        if not self.contrast_check.isChecked():
            args += ["--planet-contrast", "1.0"]
        if self.white_spin.value() > 0:
            args += ["--planet-white", f"{self.white_spin.value():.4f}"]
        if self.black_spin.value() > 0:
            args += ["--planet-black", f"{self.black_spin.value():.4f}"]
        return args

    def _sync_tone_controls(self):
        """Grey out the controls that do nothing for the selected curve. The None (linear)
        mode adds no contrast and never auto-fits, so its Contrast and Auto-stretch boxes
        would be misleading if left live; Manual stretch stays enabled, since linear
        honours it."""
        shaped = self._tone_mode() != "linear"
        self.contrast_check.setEnabled(shaped)
        self.autostretch_check.setEnabled(shaped)

    def _on_tone_changed(self, *_):
        """Remember the choice, and nudge toward the free re-render rather than letting
        the selection silently disagree with what's on the canvas."""
        self.settings.setValue("landslide/planet_tone", self._tone_mode())
        self.settings.setValue("landslide/planet_autostretch",
                               self.autostretch_check.isChecked())
        self.settings.setValue("landslide/planet_contrast_scurve",
                               self.contrast_check.isChecked())
        self.settings.setValue("landslide/planet_white", self.white_spin.value())
        self.settings.setValue("landslide/planet_black", self.black_spin.value())
        self._sync_tone_controls()
        if self._last_render and self.task is None:
            self._append_log(
                f"Tone curve set to '{self._tone_label()}' — click Re-tone to re-render "
                f"the loaded scene with it (free), or Render detail for a new scene.")

    def _refresh_retone_btn(self):
        """Re-tone needs a previous render to read clips from, and no running task."""
        if not hasattr(self, "retone_btn"):
            return
        self.retone_btn.setEnabled(bool(self._last_render) and self.task is None)

    def _retone_detail(self):
        """Re-render the last SR detail with the selected tone curve via run_single.py
        --planet-retone: reads the clips already downloaded under
        <out>/planet_render/<event_id>. No order, no quota, no network.

        Targets the AOI/date the render actually used (self._last_render) rather than
        the current form, for the same reason _resume_detail does — the user may have
        moved the form on since. Reuses _on_detail_done so the result loads exactly like
        a fresh render."""
        last = self._last_render
        if not last:
            self._warn("Run 'Render detail' once first — Re-tone re-renders the "
                       "surface-reflectance clips that render leaves on disk.")
            return
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        out = os.path.join(base_out, "planet")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return
        args = [
            "--lat", f"{last['lat']:.6f}", "--lon", f"{last['lon']:.6f}",
            "--datetime", last["when"], "--radius-km", f"{last['radius']:.2f}",
            "--prefer", "planet", "--planet-retone", "--out", out,
        ] + self._tone_args()
        if last.get("event_id"):
            args += ["--event-id", str(last["event_id"])]
        self._append_log(
            f"Re-tone: re-rendering the downloaded SR clips as '{self._tone_label()}' "
            f"— no order, no quota…")
        self._busy(True)
        self.detail_btn.setEnabled(False)
        self.retone_btn.setEnabled(False)
        # No API key needed: --planet-retone never constructs a Planet client.
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="render.json")
        self.task.logLine.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_detail_done)
        self.task.taskTerminated.connect(self._on_detail_done)
        QgsApplication.taskManager().addTask(self.task)

    def _has_pending(self):
        return bool((self._pending or {}).get("orders"))

    def _refresh_resume_btn(self):
        """Enable Resume only when an order is pending, resume isn't switched off, and
        no task is already running."""
        if not hasattr(self, "resume_btn"):
            return
        self.resume_btn.setEnabled(
            self._has_pending() and self._autoresume_mode() != "off"
            and self.task is None)

    def _on_autoresume_changed(self, *_):
        mode = self.autoresume_combo.currentData() or "off"
        self.settings.setValue("landslide/planet_autoresume", mode)
        # switching away from auto disarms a waiting timer; switching to auto with an
        # order already pending arms one
        if mode != "auto15" and self._resume_timer.isActive():
            self._resume_timer.stop()
        elif (mode == "auto15" and self._has_pending() and self.task is None
              and not self._resume_timer.isActive()):
            self._arm_auto_resume()
        self._refresh_resume_btn()

    def _arm_auto_resume(self):
        self._append_log(
            "Auto-resume: the pending PlanetScope order will be downloaded in 15 min "
            "(no re-order). Click 'Resume order' to do it sooner.")
        self._resume_timer.start(_AUTO_RESUME_MS)

    def _auto_resume_fire(self):
        if (self._autoresume_mode() != "auto15" or not self._has_pending()
                or self.task is not None):
            return
        if self._autoresume_tries >= _MAX_AUTO_RESUME:
            self._append_log(
                "Auto-resume: still not ready after several tries — click 'Resume "
                "pending order' to keep waiting on it.")
            return
        self._autoresume_tries += 1
        self._append_log(f"Auto-resume: downloading the pending order "
                         f"(attempt {self._autoresume_tries})…")
        self._resume_detail()

    def _resume_detail(self):
        """Finish the pending order via run_single.py --planet-resume: download the
        already-placed order, composite, and load it — no new order, no extra quota.

        Resumes against the AOI the order was PLACED for (persisted in render.json's
        pending block), not the current form, since the user may have changed the
        form since. Reuses _on_detail_done to load the result exactly like a render."""
        pending = self._pending or {}
        orders = pending.get("orders") or {}
        if not orders:
            self._warn("No pending PlanetScope order to resume.")
            return
        lat, lon = pending.get("lat"), pending.get("lon")
        radius, event_id = pending.get("radius_km"), pending.get("event_id")
        if lat is None or lon is None or radius is None:
            self._warn("Pending order is missing its AOI — re-run Render detail.")
            return
        key = self._api_key()
        if not key:
            self._warn("Set a Planet API key (above) to resume the order.")
            return
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        out = os.path.join(base_out, "planet")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return
        self._apply_key_to_env(key)
        os.makedirs(out, exist_ok=True)

        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{float(lat):.6f}", "--lon", f"{float(lon):.6f}",
            "--datetime", when, "--radius-km", f"{float(radius):.2f}",
            "--prefer", "planet", "--planet-resume", "--out", out,
        ]
        args += self._tone_args()
        if event_id:
            args += ["--event-id", str(event_id)]
        if orders.get("pre"):
            args += ["--resume-pre-order", orders["pre"]]
        if orders.get("post"):
            args += ["--resume-post-order", orders["post"]]
        # a resumed order is now the thing Re-tone should target (see _render_detail)
        self._last_render = dict(lat=float(lat), lon=float(lon), radius=float(radius),
                                 when=when, event_id=event_id)
        if self._resume_timer.isActive():
            self._resume_timer.stop()
        self._append_log(
            f"Resume pending order: downloading the already-placed SR order(s) "
            f"({' & '.join(orders)}) and rendering — no new order, no extra quota…")
        self._busy(True)
        self.detail_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="render.json")
        self.task.logLine.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_detail_done)
        self.task.taskTerminated.connect(self._on_detail_done)
        QgsApplication.taskManager().addTask(self.task)

    def _add_tile_layer(self, name, tile_hash, key):
        # Build the XYZ datasource URI exactly like Planet's own qgis-planet-plugin
        # (planet_api/p_client.tile_service_url + pe_utils.tile_service_data_src_uri):
        # a RAW, unencoded tile URL placed last, with {z}/{x}/{y} kept as literal
        # braces. Percent-encoding the whole URL (quote(url)) yields a layer that is
        # 'valid' but renders NOTHING — verified against the QGIS 3.44 wms/xyz provider
        # (raw url -> tiles draw, encoded url -> blank). A random tiles{0-3} shard
        # spreads the tile requests the way Planet Explorer does.
        #
        # NB: we deliberately do NOT setExtent() this layer to the scene footprint.
        # An XYZ layer's provider reports a whole-world tile pyramid, so clamping only
        # layer.extent() desyncs it from that grid and the tiles stop rendering. That
        # is why QGIS's own 'Zoom to Layer' zooms to the whole Earth for these layers
        # — use the tab's "Zoom to scene" button to frame the scene instead.
        tile_url = (TILE_XYZ_URL.format(shard=random.randint(0, 3), hash=tile_hash)
                    + f"?api_key={key}")
        uri = f"type=xyz&crs=EPSG:3857&format=&url={tile_url}"
        lyr = QgsRasterLayer(uri, name, "wms")
        if not lyr.isValid():
            self._append_log(f"    could not build the tile layer for {name}")
            return
        lg.add_to_group(lyr, self._preview_group)
        self._preview_layers.append(lyr)
        self._append_log(f"    added: {name}")

    def _grow_preview_extent(self, bounds):
        """Union a scene's [w, s, e, n] footprint into the preview zoom extent."""
        if not bounds or len(bounds) != 4:
            return
        try:
            rect = QgsRectangle(float(bounds[0]), float(bounds[1]),
                                float(bounds[2]), float(bounds[3]))
        except (TypeError, ValueError):
            return
        if self._preview_extent is None:
            self._preview_extent = rect
        else:
            self._preview_extent.combineExtentWith(rect)

    def _clear_preview_layers(self):
        for lyr in self._preview_layers:
            lg.remove_layer(lyr)
        self._preview_layers = []
        if hasattr(self, "zoom_btn"):
            self.zoom_btn.setEnabled(False)

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
        clicking a row isolates its strip instead of the full overlapping pile."""
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

    def _zoom_to_preview(self):
        """Frame the previewed scene footprint(s); fall back to the AOI box."""
        if self._preview_extent is not None:
            self._zoom_to_rect(self._preview_extent)
        else:
            self._zoom_to_aoi()

    def _zoom_to_aoi(self):
        aoi = self._aoi()
        if aoi is None:
            return
        lat, lon, radius = aoi
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        self._zoom_to_rect(QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat))

    def _zoom_to_rect(self, rect):
        """Zoom the canvas to an EPSG:4326 rectangle, reprojected to the canvas CRS."""
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
        have = bool(self._search_result and
                    (self._search_result.get("pre") or
                     self._search_result.get("post")))
        self.map_preview_btn.setEnabled((not on) and have)
        self.detail_btn.setEnabled((not on) and have)
        if on:
            self.resume_btn.setEnabled(False)
            if hasattr(self, "retone_btn"):
                self.retone_btn.setEnabled(False)
            if hasattr(self, "recall_btn"):
                self.recall_btn.setEnabled(False)
        else:
            self._refresh_resume_btn()
            self._refresh_retone_btn()
            # Recall depends only on the ledger, not on a search — enable it whenever
            # this location has a cached order to load.
            if hasattr(self, "recall_btn"):
                self.recall_btn.setEnabled(bool(getattr(self, "recall_combo", None))
                                           and self.recall_combo.count() > 1)

    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _warn(self, text):
        self.iface.messageBar().pushWarning("PlanetScope", text)

    def teardown(self):
        if getattr(self, "_resume_timer", None) is not None:
            self._resume_timer.stop()
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
