"""The Maxar VHR (GEGD) tab: very-high-resolution before/after strips for one event.

GEGD Pro is NGA's front end to Vantor's (formerly Maxar's) streaming OGC services.
With an API key it gives this plugin three things the other tabs cannot: a
catalog of every WorldView / GeoEye strip over Alaska, searchable in one request
at any map scale; each strip streamed straight onto the canvas at 0.3–0.6 m; and
fresh collects online about 2.3 h after the satellite passes. What it does NOT
give is radiometry: every strip arrives as 8-bit, DRA-stretched RGB (or pan for
WorldView-1), with no NIR or SWIR. It is for looking and tracing; Fusion refuses
it, and no layer here is named like a change index.

Workflow (M1): **Use it in every tab** fills the Event box from the seismic
detection → **Search** lists the strips that actually see the search circle,
split before/after on the full UTC origin time, graded and ranked → the ★
before/after pair streams onto the map by itself → **Flip** toggles the after
strip. Three clicks from a detection to a live comparison.

**Origin time.** The split is ``acquired < origin`` = before, on aware UTC
datetimes. The origin is ``dock.detection.origin_utc`` when the detection matches
the tab's fields (it keeps seconds), otherwise the Event time field read as UTC
wall-clock: ``dt_edit.dateTime().toPyDateTime().replace(tzinfo=utc)``. Never
``.toUTC()``: the dock stores the UTC wall clock in a LocalTime-spec QDateTime,
so ``.toUTC()`` would shift the boundary by the Mac's own 8–9 h offset and
silently move a bracketing strip to the wrong side (Iliamna's 2026-08-01 00:18Z
WorldView-1 strip is BEFORE an 08-08 event even though Alaska calls it 07-31).

**Threading.** ``GegdTab.__init__`` never touches ``QgsApplication.authManager()``:
opening the dock (or a test) must never prompt for the master password. Every
action that needs the key calls ``gegd_auth.ensure_ready()`` first, on the GUI
thread; all network work runs in ``gegd_tasks`` QgsTasks, which never touch a
widget; a generation counter drops the answer of a search that was superseded.

**Re-ranking is free.** The Scene filters only re-rank the strips already in
hand (gegd_rank is pure), so changing one costs zero requests.

**Layers.** Streams are XYZ layers carrying ``authcfg=gegdpro`` only (see
gegd_layers); **Stream picks** replaces only the previous stream set — never a
clip, a scar, or anything another tab made. Footprints and the search circle are
memory layers, rebuilt on each search. On reopen, ``gegd_layers.adopt_layers``
finds them again by custom property, and ``<project>/gegd/<event_id>/search.json``
(no key, no pixels) restores the table without a request.

**Key hygiene.** The key lives in the QGIS password store (``gegd_auth``); the
field is cleared after Save key and is never persisted (password echo, and
``key_edit`` is in project_state.SECRET_ATTRS). Every string that reaches the log
or the message bar passes through ``gegd_api.redact``. One leak the plugin cannot
close: QGIS's Network Logger (F12) shows request headers, the key among them.
"""
import html
import json
import os
import re
import time
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QDateTime, QItemSelectionModel, Qt, QTimer
from qgis.PyQt.QtGui import QBrush
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject, QgsRectangle, QgsSettings,
)
from qgis.gui import QgsCollapsibleGroupBox

from . import gegd_api as api
from . import gegd_auth
from . import gegd_layers as gl
from . import gegd_rank as R
from . import layer_group as lg
from .flow_layout import FlowRow
from .gegd_tasks import KeyTestTask, SearchTask
from .theme import (GRADE_VISUAL_FG, MISSES_EPICENTRE_FG, MUTED_FG, POST_BG, PRE_BG,
                    ROW_FG, status_css, status_html, status_text)

# search.json: the cached result of one search (no key, no pixels)
SEARCH_FORMAT = "landslide-gegd-search"
SEARCH_VERSION = 1
# remembered per profile: which Vivid-exclusion form the server accepted. Only
# 'not_in' or 'neq' is ever stored: 'none' (exclusion left to the client) is a
# last resort a search falls to, never a setting it starts from.
S_PRODUCT_CLAUSE = "landslide/gegd_product_clause"

# Sensor filter: the four Vantor platforms seen over Alaska, plus everything else.
SENSORS = (
    ("WV01", "WorldView-1"), ("WV02", "WorldView-2"), ("WV03", "WorldView-3"),
    ("GE01", "GeoEye-1"), ("other", "other"),
)

COLUMNS = ("Pick", "Date UTC (Δd)", "Sensor", "Off-nadir°", "Sun°", "AOI cover",
           "Strip cloud %")
COLUMN_TIPS = (
    "Tick the strips to stream with 'Stream picks'. ★ marks the recommended "
    "before/after pair: mapping-grade, close in time, a low view angle, and a sun "
    "and relief shift similar enough between the two that Flip compares like with "
    "like.",
    "Acquisition time (UTC) and days from the event, negative = before. Before/after "
    "is decided on the full UTC time, so an Alaska-evening strip can carry the next "
    "day's date here; the row tooltip gives the Alaska time.",
    "WorldView-1 is panchromatic only (grey, ~0.5 m); WorldView-2/3 and GeoEye-1 "
    "stream as pansharpened colour. 'stereo' = an in-track mate seconds apart (one "
    "look from two angles, not two dates). 'ingested' = a strip someone had GEGD "
    "ingest on request (PRIVATE_FEATURE) rather than one from the Daily Take "
    "archive. '3rd-party' = another vendor's imagery under a separate licence.",
    "How far from straight down the satellite looked, and the processing level. "
    "LV3D = orthorectified on a fine DEM (off-nadir ≤30°, CE90 ≈ 8 m): safe to trace "
    "on. LV2A = coarse DEM (>30°): tens of metres off in steep relief, so it is "
    "greyed — look, don't trace. Every strip is orthorectified on a PRE-event DEM, so "
    "where the surface dropped 50 m the image puts the new surface 50·tan(incidence) "
    "sideways: ~21 m at 20° off-nadir, ~34 m at 30°.",
    "Sun elevation. Below ~25° the 8-bit DRA stretch starts to swallow shadowed "
    "slopes; below the Scene-filter minimum a strip is hidden.",
    "Share of the search circle the footprint covers. Red '(misses epicentre)' when "
    "the epicentre itself lies outside the strip.",
    "GEGD's cloud estimate for the WHOLE strip (often 15+ km long), not for your "
    "circle: weak evidence either way. A strip 30% cloudy can be clear over the scar, "
    "and snow is not cloud. Look at the stream to judge.",
)

# The catalog's productName for a strip ingested on request (gegd_api.OPTICAL_PRODUCTS)
INGESTED_PRODUCT = "PRIVATE_FEATURE"

# A detection "matches" the tab's fields within these (the fields are filled from
# it with 6 decimals and whole minutes shown; the record keeps seconds).
MATCH_DEG = 0.01
MATCH_S = 60.0
RERANK_MS = 250          # filter spin boxes re-rank once the user stops typing
USAGE_MS = 30_000        # usage line refresh while the tab is on screen
# Save key / Forget key wait this long at most for cancelled requests to wind down
# before they touch the stored key (see _when_idle).
IDLE_WAIT_S = 10.0
IDLE_POLL_MS = 50


def _aware(d):
    """Aware UTC; naive is UTC (never local: see the module docstring)."""
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _wall(qdt):
    """The same wall clock in a LocalTime-spec QDateTime. Every event-time field in
    the dock shows the UTC wall clock; copying the wall clock (not the instant)
    keeps it, whatever time spec the source edit happens to carry."""
    return QDateTime(qdt.date(), qdt.time())


def _safe_id(text):
    """An event id fit for a folder name ('AK2026-0808', 'event_260808_1300')."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "")).strip("._-")
    return s or "event"


def _iso(d):
    return _aware(d).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if d is not None else None


def _esc(text):
    return html.escape(str(text or ""), quote=False)


def strip_feature(s):
    """A Strip back as a GeoJSON feature that gegd_api.parse_features reads into an
    equal Strip. For search.json when the raw catalog answer is not at hand."""
    props = {
        "featureId": s.feature_id, "legacyIdentifier": s.catalog_id,
        "acquisitionDate": _iso(s.acq_utc), "createdDate": _iso(s.created_utc),
        "source": s.source or s.sensor, "productName": s.product_name,
        "sensorType": s.sensor_type, "isThirdParty": bool(s.is_third_party),
        "vendorName": s.vendor, "offNadirAngle": s.ona, "sunElevation": s.sun_el,
        "sunAzimuth": s.sun_az, "cloudCover": s.cloud, "groundSampleDistance": s.gsd_m,
        "bandConfiguration": s.band_config, "processingLevel": s.processing_level,
        "demCorrection": s.dem_correction, "ce90Accuracy": s.ce90,
        "earliestAcquisitionTime": _iso(s.earliest_utc),
        "latestAcquisitionTime": _iso(s.latest_utc),
        "copyright": s.copyright, "licenseType": s.license_type,
    }
    if s.multi_date:
        props["outputMosaic"] = True
    geom = None
    if s.rings:
        geom = {"type": "MultiPolygon",
                "coordinates": [[[[float(x), float(y)] for x, y in ring]] for ring in s.rings]}
    return {"type": "Feature", "properties": props, "geometry": geom}


def _features_from_raw(raw):
    """The feature list of a raw WFS answer (bytes, text or parsed), or None."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        doc = json.loads(raw.lstrip("﻿")) if isinstance(raw, str) else raw
    except ValueError:
        return None
    feats = doc.get("features") if isinstance(doc, dict) else doc
    if not isinstance(feats, list):
        return None
    return [f for f in feats if isinstance(f, dict)]


def _parse_feature_list(feats):
    """[feature dict] -> [Strip] through the one real parser (raises SchemaDrift)."""
    body = json.dumps({"type": "FeatureCollection", "features": list(feats or [])})
    return api.parse_features(body.encode("utf-8"))


def _nearest_to_json(n):
    if n is None or n == R.NOT_FOUND:
        return n
    return {"feature": strip_feature(n)}


def _nearest_from_json(v):
    if v is None or v == R.NOT_FOUND:
        return v
    try:
        found = _parse_feature_list([v["feature"]])
        return found[0] if found else None
    except (api.GegdError, KeyError, TypeError):
        return None


class GegdTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock                 # results_dir(), the detection, the S2 fields
        self.iface = dock.iface
        self.canvas = dock.canvas
        # The server. Only https://pro.gegd.com may ever receive the key (the
        # HostGuard enforces that); tests aim this at a local mock server.
        self._host = api.HOST_DEFAULT
        self._gen = 0                    # search generation (drops superseded answers)
        self._key_gen = 0                # key-test generation (Save/Test/Forget bump it)
        self._idle_gen = 0               # the newest Save/Forget waiting in _when_idle
        self._key_task = None            # the key test in flight, if any
        self._tasks = set()              # running tasks, referenced until they finish
        self._busy_on = False
        self._torn_down = False
        self._search = None              # the last search: inputs + raw evidence (dict)
        self._ranked = None              # gegd_rank.Ranked for the current filters
        self._row_strips = []            # RankedStrip per table row
        self._picks = set()              # featureIds ticked in the Pick column
        self._show_hidden = False        # list the rows the Scene filters hide
        self._filling = False            # the table is being rebuilt (ignore its signals)
        self._syncing = False            # table <-> footprint selection in progress
        self._stream_layers = []         # the current stream set (Stream picks replaces it)
        self._compare = (None, None)     # (before, after) layers Flip works on
        self._stream_group = ""          # layer-tree folder of the current stream set
        self._footprint_layer = None
        self._aoi_layer = None
        self._fp_ids = {}                # featureId -> QGIS feature id in the footprints
        self._fp_hooked = None           # the footprint layer whose selection we follow
        self._signals = []               # (signal, slot) to disconnect in teardown
        self._rerank_timer = QTimer(self)
        self._rerank_timer.setSingleShot(True)
        self._rerank_timer.setInterval(RERANK_MS)
        self._rerank_timer.timeout.connect(self._rerank_now)
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(USAGE_MS)
        self._usage_timer.timeout.connect(self._refresh_usage)
        self._build_ui()
        self._connect_external()
        self._refresh_event()
        self._refresh_key_status()
        self._refresh_usage()
        self._update_buttons()
        # Hand back this project's layers and last search once project_state has
        # restored the fields. Its restore is deferred by one tick and registered
        # AFTER this one (the dock builds the tabs first), so this waits two.
        QTimer.singleShot(0, self._defer_restore)

    # ================================================================ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        intro = QLabel(
            "Maxar/Vantor strips at 0.3–0.6 m from NGA's GEGD Pro, streamed straight "
            "onto the map for a before/after look at the event. Needs your own GEGD Pro "
            "API key. Display imagery (8-bit, no NIR/SWIR): for looking and tracing, "
            "never for Fusion's change indices.")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        root.addWidget(self._build_key_box())
        root.addWidget(self._build_event_box())
        root.addWidget(self._build_filter_box())

        row = FlowRow()
        self.search_btn = QPushButton("Search")
        f = self.search_btn.font(); f.setBold(True); self.search_btn.setFont(f)
        self.search_btn.setDefault(True)
        self.search_btn.setToolTip(
            "One catalog request (WFS, about 0.3–1.6 s) for every strip whose footprint "
            "touches the search circle in the window, at any map scale. The strips are "
            "split before/after on the full UTC origin time, graded (mapping / visual "
            "only / context), ranked on time gap, view angle, sun, coverage and strip "
            "cloud, and the recommended ★ pair is streamed if 'Stream the ★ pair' is "
            "ticked. A side that comes back empty costs one more small request, to say "
            "where the nearest strip outside the window is. Nothing is downloaded.")
        self.search_btn.clicked.connect(self._search_clicked)
        self.stream_btn = QPushButton("Stream picks")
        self.stream_btn.setToolTip(
            "Stream the ticked strips onto the map (the ★ pair if nothing is ticked), "
            "replacing the previous Maxar STREAMS only — clips, scars and every other "
            "tab's layers are never touched. The top before/after pair is shown, after "
            "on top; any extra picks are added switched off, and a switched-off stream "
            "fetches nothing. Double-click a row to make that one strip the shown "
            "before- or after-strip without removing anything: the strip it takes over "
            "from is switched off, and the after-strip stays on top so Flip still works.")
        self.stream_btn.clicked.connect(self._stream_picks)
        self.flip_btn = QPushButton("Flip before/after")
        self.flip_btn.setToolTip(
            "Toggle the after-strip on and off over the before-strip. QGIS keeps both "
            "rendered, so the flip is instant and costs no requests: the fastest way to "
            "see what moved. Both strips are orthorectified on a pre-event DEM, so "
            "terrain that dropped shifts sideways by roughly 50·tan(incidence) per 50 m "
            "(see Off-nadir°); the ★ pair is chosen so that shift is similar in both.")
        self.flip_btn.clicked.connect(self._flip)
        self.zoom_btn = QPushButton("Zoom to search area")
        self.zoom_btn.setToolTip(
            "Frame the search circle on the canvas. Use this rather than QGIS's own "
            "'Zoom to Layer', which zooms out to the whole world for streamed (XYZ) "
            "layers. Strips render at any scale; the deposit reads best near 1:10,000.")
        self.zoom_btn.clicked.connect(self._zoom)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop the search or key test in flight. The table "
                                   "keeps the previous results.")
        self.cancel_btn.clicked.connect(self._cancel)
        for b in (self.search_btn, self.stream_btn, self.flip_btn, self.zoom_btn,
                  self.cancel_btn):
            row.addWidget(b)
        root.addWidget(row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.hide()
        root.addWidget(self._progress)
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self._summary_label)
        self._show_hidden_btn = QPushButton("Show hidden strips")
        self._show_hidden_btn.setCheckable(True)
        self._show_hidden_btn.setToolTip(
            "List (greyed) the strips the Scene filters hide, with the reason in each "
            "row's tooltip. Re-ranks what is already in hand: no request.")
        self._show_hidden_btn.toggled.connect(self._toggle_hidden)
        self._show_hidden_btn.hide()
        hid = FlowRow()
        hid.addWidget(self._show_hidden_btn)
        root.addWidget(hid)

        root.addWidget(self._build_results_box())

        logbox = QgsCollapsibleGroupBox("Log")
        logbox.setSaveCollapsedState(False)
        logbox.setCollapsed(True)
        lv = QVBoxLayout(logbox)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(1000)
        self._log.setMinimumHeight(90)
        lv.addWidget(self._log)
        root.addWidget(logbox)
        root.addStretch(1)

    def _group(self, title, collapsed):
        """A collapsible group ("drop-down") and its form, opened the same way every
        session (setSaveCollapsedState(False), as dock.py's and Planet's boxes)."""
        box = QgsCollapsibleGroupBox(title)
        box.setSaveCollapsedState(False)
        box.setCollapsed(collapsed)
        form = QFormLayout(box)
        form.setContentsMargins(6, 4, 6, 4)
        return box, form

    def _build_key_box(self):
        box, form = self._group("Maxar/Vantor GEGD Pro connection", collapsed=False)
        self._key_box = box
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText("paste your GEGD Pro API key, then Save key")
        self.key_edit.setToolTip(
            "Your GEGD Pro API key (GEGD Pro web app > API keys). Save key puts it in "
            "the QGIS password store as the auth config 'gegdpro' — encrypted, unlocked "
            "by your QGIS master password or the macOS Keychain — and clears this field. "
            "Saved projects only ever hold 'authcfg=gegdpro', never the key, and while "
            "this plugin is loaded (and for the rest of that QGIS session after you "
            "switch it off) it strips the key from any request that is not "
            "https://pro.gegd.com, so a project edited to point elsewhere cannot "
            "collect it. Press Forget key before disabling or uninstalling the plugin.\n"
            "The one place it still shows: QGIS's Network Logger (F12) lists request "
            "headers, key included. Never share a logger export, 'Copy as cURL' or "
            "'Save Network Log' while Maxar layers are loaded.")
        self.key_edit.returnPressed.connect(self._save_key)
        form.addRow("API key", self.key_edit)
        row = FlowRow()
        self.save_key_btn = QPushButton("Save key")
        self.save_key_btn.setToolTip(
            "Store the pasted key in the QGIS password store (auth config 'gegdpro'), "
            "clear the field, and Test it. Saving again replaces the stored key, so a "
            "rotated key takes effect on the next request. A search or key test still "
            "running is cancelled first: it was made with the old key.")
        self.save_key_btn.clicked.connect(self._save_key)
        self.test_key_btn = QPushButton("Test")
        self.test_key_btn.setToolTip(
            "One small catalog request (it names the newest Vantor optical strip over "
            "Alaska) to prove the stored key reaches GEGD Pro and the catalog answers.")
        self.test_key_btn.clicked.connect(self._test_key)
        self.forget_key_btn = QPushButton("Forget key")
        self.forget_key_btn.setToolTip(
            "Remove the key from this QGIS's password store. Maxar streams already on "
            "the map stop loading new tiles, and Search needs a key again. The key "
            "itself stays valid at GEGD until it expires or you revoke it there.\n"
            "Press this BEFORE you disable or uninstall the plugin: the stored key "
            "outlives it, and in a later QGIS session without the plugin nothing "
            "stops a project that names authcfg=gegdpro from sending the key to "
            "whatever server it points at.")
        self.forget_key_btn.clicked.connect(self._forget_key)
        for b in (self.save_key_btn, self.test_key_btn, self.forget_key_btn):
            row.addWidget(b)
        form.addRow(row)
        self._key_status = QLabel()
        self._key_status.setWordWrap(True)
        self._key_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self._key_status)
        self._usage_label = QLabel()
        self._usage_label.setWordWrap(True)
        self._usage_label.setStyleSheet("QLabel { color: palette(mid); }")
        self._usage_label.setToolTip(
            "Counted by this QGIS on every request to the GEGD host (tiles included). "
            "GEGD's own usage meter needs a full account login, which API keys do not "
            "have, so this is the only count available here.")
        form.addRow(self._usage_label)
        # Settings only (no password store): open until a key has been saved here.
        box.setCollapsed(gegd_auth.key_set_utc() is not None)
        return box

    def _build_event_box(self):
        box, form = self._group("Event location and time", collapsed=False)
        self._event_label = QLabel()
        self._event_label.setWordWrap(True)
        self._event_label.setTextFormat(Qt.RichText)
        self._event_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self._event_label)

        self.lat_edit = QLineEdit()
        self.lat_edit.setPlaceholderText("e.g. 60.0265   (or paste '60.0265, -153.0559')")
        self.lon_edit = QLineEdit()
        self.lon_edit.setPlaceholderText("e.g. -153.0559   (negative in Alaska/Yukon)")
        self.lat_edit.textEdited.connect(self._split_pair)
        form.addRow("Latitude", self.lat_edit)
        form.addRow("Longitude", self.lon_edit)

        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.2, 50.0)
        self.radius_spin.setSingleStep(0.5)
        self.radius_spin.setValue(5.0)
        self.radius_spin.setSuffix(" km")
        self.radius_spin.setToolTip(
            "Radius of the search circle around the epicentre. A strip is listed only if "
            "its footprint touches this circle (checked here, not just by the server's "
            "box). 'Use it in every tab' sets it from the detection's location error: a "
            "5 km circle inside a 17 km error looks at about a twelfth of the ground the "
            "scar could be on.")
        form.addRow("Search radius", self.radius_spin)

        self.dt_edit = QDateTimeEdit()
        self.dt_edit.setCalendarPopup(True)
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        main_dt = getattr(self.dock, "dt_edit", None)
        if main_dt is not None:
            self.dt_edit.setDateTime(_wall(main_dt.dateTime()))
        else:
            self.dt_edit.setDateTime(_wall(QDateTime.currentDateTimeUtc()))
        self.dt_edit.setToolTip(
            "The origin time, in UTC — the line above echoes it in Alaska time. Strips "
            "acquired before this instant are 'before', the rest 'after'. When the "
            "Detection box matches these fields its time (with seconds) is used.")
        form.addRow("Event time (UTC)", self.dt_edit)

        self.pre_days_spin = QSpinBox()
        self.pre_days_spin.setRange(1, R.MAX_PRE_DAYS)
        self.pre_days_spin.setValue(365)
        self.pre_days_spin.setSuffix(" d")
        self.pre_days_spin.setToolTip(
            "How far back to look for before-strips. VHR is sparse — a given slope may "
            "be imaged a few times a year — so the default is a full year, nearest-first "
            "in the ranking. GEGD's online archive starts around 2024-07-20; nothing "
            "older comes back.")
        self.post_days_spin = QSpinBox()
        self.post_days_spin.setRange(1, R.MAX_POST_DAYS)
        self.post_days_spin.setValue(90)
        self.post_days_spin.setSuffix(" d")
        self.post_days_spin.setToolTip(
            "How far after the event to look (never past now). The ranking prefers the "
            "FIRST good after-strip, because early snow can bury a deposit. No Daily "
            "Take is collected Dec–Jan at these latitudes.")
        days = QHBoxLayout()
        days.setContentsMargins(0, 0, 0, 0)
        days.addWidget(QLabel("before"))
        days.addWidget(self.pre_days_spin, 1)
        days.addWidget(QLabel("after"))
        days.addWidget(self.post_days_spin, 1)
        w = QWidget()
        w.setLayout(days)
        form.addRow("Window", w)

        self._archive_note = QLabel()
        self._archive_note.setWordWrap(True)
        self._archive_note.hide()
        form.addRow(self._archive_note)

        # '&&' because a single & is a Qt mnemonic marker
        self.copy_btn = QPushButton("⟵ Copy location && date from Sentinel-2 / Landsat tab")
        self.copy_btn.setToolTip(
            "Pull latitude, longitude, radius and event time from the Sentinel-2 / "
            "Landsat tab. The before/after windows are NOT copied: VHR needs a much "
            "wider window than Sentinel-2's.")
        self.copy_btn.clicked.connect(self._copy_from_main)
        form.addRow(self.copy_btn)

        for wdg in (self.lat_edit, self.lon_edit):
            wdg.textChanged.connect(self._refresh_event)
        self.radius_spin.valueChanged.connect(self._refresh_event)
        self.dt_edit.dateTimeChanged.connect(self._refresh_event)
        self.pre_days_spin.valueChanged.connect(self._refresh_event)
        self.post_days_spin.valueChanged.connect(self._refresh_event)
        return box

    def _build_filter_box(self):
        box, form = self._group("Scene filters", collapsed=True)
        self.ona_max_spin = QDoubleSpinBox()
        self.ona_max_spin.setRange(0.0, 45.0)
        self.ona_max_spin.setDecimals(1)
        self.ona_max_spin.setSingleStep(1.0)
        self.ona_max_spin.setValue(30.0)
        self.ona_max_spin.setSuffix("°")
        self.ona_max_spin.setToolTip(
            "Largest off-nadir angle that still counts as MAPPING grade (safe to trace "
            "on). GEGD orthorectifies strips up to 30° on a fine DEM (LV3D, CE90 ≈ 8 m) "
            "and steeper ones on a coarse DEM (LV2A), which in steep relief puts the "
            "ground tens of metres off. Steeper strips are 'visual only' and never ★. "
            "Re-ranks with no request.")
        form.addRow("Max off-nadir", self.ona_max_spin)
        self.show_visual_check = QCheckBox("Also list steeper strips (up to 45°) as visual only")
        self.show_visual_check.setChecked(True)
        self.show_visual_check.setToolTip(
            "List strips beyond the off-nadir limit, or on a coarse DEM, greyed and "
            "marked visual only: often the only after-image for weeks, and fine for "
            "seeing THAT something moved — just not for tracing where. Never ★. Strips "
            "past 45° are always hidden.")
        form.addRow(self.show_visual_check)
        self.sun_min_spin = QDoubleSpinBox()
        self.sun_min_spin.setRange(0.0, 60.0)
        self.sun_min_spin.setDecimals(0)
        self.sun_min_spin.setValue(15.0)
        self.sun_min_spin.setSuffix("°")
        self.sun_min_spin.setToolTip(
            "Hide strips taken with the sun lower than this. The 8-bit DRA stretch "
            "loses shadowed slopes first; below ~15° most north-facing terrain is black. "
            "Strips below 25° are still listed but rank lower.")
        form.addRow("Min sun elevation", self.sun_min_spin)
        self.cloud_max_spin = QSpinBox()
        self.cloud_max_spin.setRange(0, 100)
        self.cloud_max_spin.setValue(100)
        self.cloud_max_spin.setSuffix(" % of strip")
        self.cloud_max_spin.setToolTip(
            "Hide strips whose WHOLE-STRIP cloud estimate is above this. 100 = no cap, "
            "the default: GEGD has no cloud figure for your circle, and a strip 15+ km "
            "long can be 30% cloudy and clear over the scar (the PlanetScope tab had to "
            "drop its scene-cloud cap after it emptied whole windows of usable scenes). "
            "Strip cloud already lowers a strip's rank a little; lower this only to "
            "thin a long list, and look at the stream to judge the scar itself.")
        form.addRow("Max strip cloud", self.cloud_max_spin)
        self.sensor_checks = {}
        srow = FlowRow()
        for key, label in SENSORS:
            cb = QCheckBox(label if key == "other" else key)
            cb.setChecked(True)
            cb.setToolTip(label if key != "other" else
                          "Any sensor that is not WorldView-1/2/3 or GeoEye-1.")
            self.sensor_checks[key] = cb
            srow.addWidget(cb)
        form.addRow("Sensors", srow)
        self.third_party_check = QCheckBox("Include third-party imagery (separate licence)")
        self.third_party_check.setChecked(False)
        self.third_party_check.setToolTip(
            "GEGD also serves other vendors' collects (Umbra, ICEYE and Capella X-band "
            "SAR, sometimes SkySat or BlackSky). They come under their own licences, "
            "are graded 'context' and are never ★ — but in Dec–Jan, when no Maxar "
            "Daily Take is collected this far north, X-band SAR may be the only VHR "
            "after-look.")
        form.addRow(self.third_party_check)
        self.auto_stream_check = QCheckBox("Stream the ★ pair as soon as Search finishes")
        self.auto_stream_check.setChecked(True)
        self.auto_stream_check.setToolTip(
            "After a search, put the recommended before/after pair on the map straight "
            "away (after on top), replacing the previous Maxar streams. Off: nothing is "
            "streamed until you press Stream picks.")
        form.addRow(self.auto_stream_check)

        self.ona_max_spin.valueChanged.connect(self._schedule_rerank)
        self.sun_min_spin.valueChanged.connect(self._schedule_rerank)
        self.cloud_max_spin.valueChanged.connect(self._schedule_rerank)
        self.show_visual_check.toggled.connect(self._schedule_rerank)
        self.third_party_check.toggled.connect(self._schedule_rerank)
        for cb in self.sensor_checks.values():
            cb.toggled.connect(self._schedule_rerank)
        return box

    def _build_results_box(self):
        box = QgsCollapsibleGroupBox("Results")
        box.setSaveCollapsedState(False)
        box.setCollapsed(False)
        v = QVBoxLayout(box)
        row = FlowRow()
        self._clear_sel_btn = QPushButton("Clear selection")
        self._clear_sel_btn.setToolTip(
            "Deselect every row and its footprint on the map. Selecting a row "
            "highlights that strip's footprint in yellow so you can see where it "
            "lies; this takes the highlight off again. The map's own 'Deselect "
            "Features from All Layers' does the same.")
        self._clear_sel_btn.clicked.connect(self._clear_selection)
        row.addWidget(self._clear_sel_btn)
        self.show_outlines_check = QCheckBox("Show footprints and search circle")
        self.show_outlines_check.setChecked(True)
        self.show_outlines_check.setToolTip(
            "The blue (before) and green (after) strip outlines and the red search "
            "circle with its epicentre cross. Untick for a clean look at the imagery; "
            "a new Search keeps them hidden until this is ticked again. Hiding them "
            "also clears the selection, so no yellow highlight is left behind.")
        self.show_outlines_check.toggled.connect(self._apply_outlines)
        row.addWidget(self.show_outlines_check)
        v.addWidget(row)
        t = QTableWidget(0, len(COLUMNS))
        t.setHorizontalHeaderLabels(list(COLUMNS))
        for i, tip in enumerate(COLUMN_TIPS):
            t.horizontalHeaderItem(i).setToolTip(tip)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.horizontalHeader().setStretchLastSection(True)
        t.setMinimumHeight(200)
        t.setWordWrap(False)
        t.itemSelectionChanged.connect(self._on_table_selection)
        t.itemDoubleClicked.connect(self._on_double_click)
        t.itemChanged.connect(self._on_item_changed)
        self._table = t
        v.addWidget(t)
        self._detail_label = QLabel()
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextFormat(Qt.RichText)
        self._detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self._detail_label)
        return box

    # ================================================================ wiring
    def _connect_external(self):
        """The detection box and the project: connected here, undone in teardown."""
        d = self.dock
        for name, sig in (("det_lat_edit", "textChanged"), ("det_lon_edit", "textChanged"),
                          ("det_time_edit", "dateTimeChanged")):
            w = getattr(d, name, None)
            if w is not None:
                self._connect(getattr(w, sig), self._refresh_event)
        project = QgsProject.instance()
        self._connect(project.readProject, self._on_project_read)
        self._connect(project.cleared, self._on_project_cleared)
        self._connect(project.layersWillBeRemoved, self._on_layers_removed)

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._signals.append((signal, slot))

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_usage()
        self._usage_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._usage_timer.stop()

    # ================================================================ key
    def _set_key_status(self, kind, text):
        self._key_status.setText(status_text(kind, api.redact(text)))
        self._key_status.setStyleSheet(status_css(kind))

    def _refresh_key_status(self):
        """From QgsSettings only: when a key was saved here (never the key, and never
        the password store — this runs while the dock is being built)."""
        t = gegd_auth.key_set_utc()
        if t is None:
            self._set_key_status(
                "info", "No GEGD Pro key saved in this QGIS profile yet. Paste yours "
                "above and press Save key.")
            return
        days = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
        self._set_key_status(
            "info", f"Key saved here {t:%Y-%m-%d} ({days:.0f} d ago) in the QGIS password "
            f"store ({gegd_auth.AUTHCFG_ID}). Press Test to check it still works.")

    def _refresh_usage(self, *_):
        try:
            n, b = gegd_auth.usage_today()
        except Exception:
            return
        self._usage_label.setText(
            f"Today (UTC): {n} requests to GEGD, {b / 1e6:.1f} MB received, counted by "
            "this QGIS — GEGD's own meter is not visible to API keys.")

    def _password_store_refused(self, where):
        msg = ("The QGIS password store is locked or disabled, so the GEGD Pro key cannot "
               f"be {where}. Unlock it when QGIS asks for the master password, or check "
               "Settings > Options > Authentication.")
        self._set_key_status("error", msg)
        self._key_box.setCollapsed(False)
        return msg

    def _when_idle(self, action):
        """Cancel this tab's GEGD requests, then run ``action`` once none is in flight.

        Saving or removing the 'gegdpro' config while a worker thread is loading it
        can freeze QGIS: the auth manager and the auth method take their locks in
        opposite orders (measured in 3.44: GUI thread stuck in
        removeAuthenticationConfig, worker stuck in its request). Cancelling aborts
        the transfer at once, so the wait is normally a few event-loop turns; the
        event loop keeps running meanwhile, and after IDLE_WAIT_S it goes ahead
        anyway. With nothing in flight ``action`` runs straight away.

        Only the newest Save/Forget runs: one pressed while another is still
        waiting replaces it. Run in turn, Save-then-Forget had the Save start a key
        test that the Forget did not cancel, so the Forget waited the full
        IDLE_WAIT_S and then removed the key with that test still loading it; and
        Forget-then-Save had the Forget clear the field the Save then read, so the
        new key was silently dropped (both measured). A task started while waiting
        (Test stays clickable) is cancelled as well."""
        self._key_gen += 1                  # a key test still running may not report
        self._idle_gen += 1
        gen = self._idle_gen
        self._stop_key_test()
        self._cancel()
        self._update_buttons()
        deadline = time.monotonic() + IDLE_WAIT_S

        def poll():
            if self._torn_down or gen != self._idle_gen:
                return                      # torn down, or a newer Save/Forget took over
            if self._tasks and time.monotonic() < deadline:
                self._cancel()              # incl. anything started since (a Test click)
                QTimer.singleShot(IDLE_POLL_MS, poll)
                return
            action()
        poll()

    def _save_key(self):
        key = self.key_edit.text().strip()
        if not key:
            self._set_key_status("warn", "Paste the key into the field first.")
            return
        if not gegd_auth.ensure_ready(self):
            self._password_store_refused("saved")
            return
        self._when_idle(self._store_pasted_key)

    def _store_pasted_key(self):
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            ok = gegd_auth.store_key(key)
        except ValueError as e:
            self._set_key_status("error", api.redact(str(e), key))
            return
        finally:
            key = None
        if not ok:
            self._set_key_status("error", "QGIS refused to store the key in its password "
                                 "store. Nothing was saved; try again after unlocking it.")
            return
        self.key_edit.clear()
        self._log_line(f"GEGD Pro key saved in the QGIS password store "
                       f"(auth config {gegd_auth.AUTHCFG_ID}).")
        self._set_key_status("info", "Saved in the QGIS password store. Testing it…")
        self._test_key()

    def _test_key(self):
        if not gegd_auth.ensure_ready(self):
            self._password_store_refused("read")
            return
        if not gegd_auth.has_key():
            self._set_key_status("warn", "No GEGD Pro key is stored in this QGIS yet. Paste "
                                 "yours above and press Save key.")
            self._key_box.setCollapsed(False)
            return
        # One key test at a time, and only the newest may write the status: a test of
        # the key just replaced (or forgotten) must never print "Key works".
        self._stop_key_test()
        self._key_gen += 1
        gen = self._key_gen
        self._set_key_status("info", "Testing the key against the GEGD catalog…")
        task = KeyTestTask(self._host, product_clause=self._product_clause())
        self._key_task = task
        self._launch(task, lambda t: self._on_key_test_done(t, gen))
        self._update_buttons()

    def _stop_key_test(self):
        task, self._key_task = self._key_task, None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass

    def _on_key_test_done(self, task, gen=None):
        if gen is not None and gen != self._key_gen:
            return                          # a newer Save/Test/Forget superseded it
        self._key_task = None
        self._update_buttons()
        self._refresh_usage()
        if task.error is not None or task.isCanceled():
            if task.error is None or getattr(task.error, "cancelled", False):
                self._set_key_status("info", "Key test cancelled.")
                return
            self._set_key_status("error", task.error.user_message)
            self._log_line("Key test failed: " + task.error.user_message)
            self._key_box.setCollapsed(False)
            return
        where = f"stored in the QGIS password store ({gegd_auth.AUTHCFG_ID})"
        s = task.newest
        if s is not None and s.acq_utc is not None:
            text = (f"Key works · newest Vantor optical strip over Alaska "
                    f"{s.acq_utc:%Y-%m-%d %H:%M}Z ({s.sensor or '?'}) · {where}")
        elif getattr(task, "n_records", 0):
            text = (f"Key works (the catalog answered) · no Vantor optical strip among "
                    f"the {task.n_records} newest Alaska records of the last {task.days} d "
                    f"· {where}")
        else:
            text = (f"Key works (the catalog answered) · no Alaska strip in the last "
                    f"{task.days} d · {where}")
        self._set_key_status("success", text)
        self._log_line(text)

    def _forget_key(self):
        if not gegd_auth.ensure_ready(self):
            self._password_store_refused("removed")
            return
        self._when_idle(self._remove_stored_key)

    def _remove_stored_key(self):
        ok = gegd_auth.forget_key()
        self.key_edit.clear()
        if ok:
            self._set_key_status(
                "info", "Key removed from this QGIS. Maxar streams on the map stop loading "
                "new tiles, and Search needs a key again.")
            self._log_line("GEGD Pro key removed from the QGIS password store.")
        else:
            self._set_key_status("error", "QGIS could not remove the stored key. Check "
                                 "Settings > Options > Authentication for 'gegdpro'.")

    # ================================================================ event
    def _latlon(self):
        try:
            lat = float(self.lat_edit.text().strip())
            lon = float(self.lon_edit.text().strip())
        except ValueError:
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        return lat, lon

    def _field_time(self):
        """The Event time field as aware UTC. Its wall clock IS UTC; the QDateTime's
        own time spec is ignored (never .toUTC(): see the module docstring)."""
        return self.dt_edit.dateTime().toPyDateTime().replace(tzinfo=timezone.utc)

    def _matching_detection(self):
        """dock.detection when it describes the event in these fields, else None."""
        det = getattr(self.dock, "detection", None)
        if det is None or not det.is_locatable() or det.origin_utc is None:
            return None
        ll = self._latlon()
        if ll is None:
            return None
        if abs(det.lat - ll[0]) > MATCH_DEG or abs(det.lon - ll[1]) > MATCH_DEG:
            return None
        if abs((_aware(det.origin_utc) - self._field_time()).total_seconds()) > MATCH_S:
            return None
        return det

    def _origin_utc(self):
        """(origin as aware UTC, the matching detection or None)."""
        det = self._matching_detection()
        if det is not None:
            return _aware(det.origin_utc), det
        return self._field_time(), None

    def _event_id(self):
        """det.event_id when the detection matches, else 'event_%y%m%d_%H%M' (the
        run_single convention), made safe for a folder name."""
        origin, det = self._origin_utc()
        if det is not None and det.event_id:
            return _safe_id(det.event_id)
        return _safe_id(f"event_{origin:%y%m%d_%H%M}")

    def _window(self, origin=None):
        o = origin if origin is not None else self._origin_utc()[0]
        return R.search_window(o, self.pre_days_spin.value(), self.post_days_spin.value())

    def _refresh_event(self, *_):
        """The event line (with the Alaska time spelled out) and the archive note."""
        if self._torn_down:
            return
        try:
            origin, det = self._origin_utc()
        except (RuntimeError, AttributeError):
            return
        lines = []
        if det is not None:
            bits = [f"<b>{_esc(det.event_id or 'Detection')}</b>",
                    f"{origin:%Y-%m-%d %H:%M:%S} UTC ({_esc(R.alaska_str(origin))})"]
            if det.loc_error_km:
                bits.append(f"±{det.loc_error_km:g} km")
            vol = det.vol_str()
            if vol:
                bits.append(_esc(vol))
            lines.append(" · ".join(bits))
        else:
            lines.append("<i>No detection record matches these fields.</i>")
            lines.append(f"Event time {origin:%Y-%m-%d %H:%M} UTC = "
                         f"{_esc(R.alaska_str(origin))}")
        t0, t1 = self._window(origin)
        if t1 > t0:
            lines.append(f"Searches {t0:%Y-%m-%d} → {t1:%Y-%m-%d} UTC; 'before' = acquired "
                         f"before {origin:%Y-%m-%d %H:%M:%S} UTC.")
        self._event_label.setText("<br>".join(lines))

        notes = []
        floor = f"{api.ARCHIVE_FLOOR_UTC:%Y-%m-%d}"
        if t1 <= api.ARCHIVE_FLOOR_UTC:
            notes.append(f"This event predates GEGD's online archive (it starts around "
                         f"{floor}), so expect no strips. Use the Sentinel-2 / SAR tabs.")
        elif t0 < api.ARCHIVE_FLOOR_UTC:
            notes.append(f"The before-window reaches past the start of GEGD's online "
                         f"archive (around {floor}); nothing older can come back.")
        ll = self._latlon()
        if (origin.month in R.WINTER_MONTHS and ll is not None
                and abs(ll[0]) >= R.WINTER_LAT):
            notes.append("No Maxar Daily Take is collected Dec–Jan at this latitude: "
                         "after-strips usually start about February. The SAR tab sees "
                         "through the dark.")
        if origin > datetime.now(timezone.utc):
            notes.append("The event time is in the future — check the date, and that it "
                         "is UTC.")
        self._archive_note.setText("\n".join(status_text("warn", n) for n in notes))
        self._archive_note.setStyleSheet(status_css("warn"))
        self._archive_note.setVisible(bool(notes))
        self._update_buttons()

    def _split_pair(self, text):
        """'60.0265, -153.0559' pasted into Latitude fills both fields."""
        parts = [p for p in re.split(r"[,;\s]+", (text or "").strip()) if p]
        if len(parts) != 2:
            return
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return
        self.lat_edit.setText(f"{lat:.6f}")
        self.lon_edit.setText(f"{lon:.6f}")

    def _copy_from_main(self):
        """Location, radius and time from the Sentinel-2 / Landsat tab (not the
        windows: VHR needs a far wider one)."""
        d = self.dock
        self.lat_edit.setText(d.lat_edit.text())
        self.lon_edit.setText(d.lon_edit.text())
        self.radius_spin.setValue(d.radius_spin.value())
        self.dt_edit.setDateTime(_wall(d.dt_edit.dateTime()))

    def _fill_from_detection(self):
        """Search pressed on empty fields: take the event from the detection."""
        det = getattr(self.dock, "detection", None)
        if det is None or not det.is_locatable():
            return False
        self.lat_edit.setText(f"{det.lat:.6f}")
        self.lon_edit.setText(f"{det.lon:.6f}")
        self.radius_spin.setValue(det.suggested_radius_km())
        if det.origin_utc is not None:
            when = QDateTime.fromString(det.origin_utc.strftime("%Y-%m-%d %H:%M:%S"),
                                        "yyyy-MM-dd HH:mm:ss")
            if when.isValid():
                self.dt_edit.setDateTime(when)
            else:
                # The field holds the UTC wall clock in a LocalTime QDateTime, so a
                # time inside this computer's spring-forward gap cannot be shown.
                # (The dock's Detection time field cannot hold one either, so this
                # is defensive.) Say so rather than keep a stale time silently.
                self._warn(f"The detection time {det.origin_utc:%Y-%m-%d %H:%M:%S} UTC falls "
                           "in this computer's daylight-saving gap and cannot be shown in "
                           "the Event time field; set the field by hand.")
        self._log_line(f"Filled the event from the detection {det.event_id or ''}".rstrip()
                       + ".")
        return True

    # ================================================================ search
    def _filters(self):
        ticked = frozenset(k for k, cb in self.sensor_checks.items() if cb.isChecked())
        return R.Filters(
            ona_max=float(self.ona_max_spin.value()),
            show_visual=self.show_visual_check.isChecked(),
            sun_min=float(self.sun_min_spin.value()),
            cloud_max=float(self.cloud_max_spin.value()),     # PERCENT, as gegd_rank expects
            sensors=None if len(ticked) == len(self.sensor_checks) else ticked,
            third_party=self.third_party_check.isChecked())

    def _search_clicked(self):
        if not self.lat_edit.text().strip():
            self._fill_from_detection()
        ll = self._latlon()
        if ll is None:
            self._warn("Enter the event latitude and longitude first (or fill the Detection "
                       "box at the top and press 'Use it in every tab').")
            return
        lat, lon = ll
        r_km = float(self.radius_spin.value())
        origin, _det = self._origin_utc()
        t0, t1 = self._window(origin)
        if t1 <= t0:
            self._warn("The search window is empty: the event time is in the future, or "
                       "the window ends before it starts. Check the date (UTC).")
            return
        if not gegd_auth.ensure_ready(self):
            self._warn(self._password_store_refused("read"))
            return
        if not gegd_auth.has_key():
            msg = ("No Maxar/Vantor GEGD Pro key is stored in this QGIS. Paste your key in "
                   "the connection box and press Save key. (The previous results are kept.)")
            self._set_key_status("warn", msg)
            self._key_box.setCollapsed(False)
            self._set_summary("warn", msg)
            self._warn(msg)
            return
        self._gen += 1
        gen = self._gen
        clause = self._product_clause()
        request = dict(event_id=self._event_id(), lat=lat, lon=lon, r_km=r_km,
                       origin=origin, t0=t0, t1=t1,
                       pre_days=int(self.pre_days_spin.value()),
                       post_days=int(self.post_days_spin.value()), host=self._host)
        task = SearchTask(self._host, lat, lon, r_km, origin, t0, t1, self._filters(),
                          clause)
        self._log_line(f"Searching GEGD Pro: {r_km:g} km around {lat:.4f}, {lon:.4f}; "
                       f"{t0:%Y-%m-%d} → {t1:%Y-%m-%d} UTC; event "
                       f"{origin:%Y-%m-%d %H:%M:%S} UTC ({request['event_id']})…")
        self._set_summary("info", "Searching the GEGD catalog…")
        self._busy(True)
        self._launch(task, lambda t: self._on_search_done(t, gen, request))

    @staticmethod
    def _product_clause():
        """The Vivid-exclusion form to start a request with: the one the server last
        accepted, never 'none' (a remembered 'none' would download Vivid's huge
        footprints on every search for good)."""
        try:
            c = QgsSettings().value(S_PRODUCT_CLAUSE, "not_in", type=str) or "not_in"
        except Exception:
            c = "not_in"
        return c if c in ("not_in", "neq") else "not_in"

    @staticmethod
    def _remember_clause(clause):
        """Keep the form a request got through with. 'none' is not kept: the next
        search tries NOT IN again (two cheap failing requests at worst)."""
        s = QgsSettings()
        if clause in ("not_in", "neq"):
            s.setValue(S_PRODUCT_CLAUSE, clause)
        else:
            s.remove(S_PRODUCT_CLAUSE)

    def _launch(self, task, done):
        """Start `task`, keep it referenced until it finishes, then call done(task)
        on the main thread — unless the tab was torn down in the meantime."""
        self._tasks.add(task)

        def finish(*_):
            if task not in self._tasks:
                return                      # already handled
            self._tasks.discard(task)
            if self._torn_down:
                return
            try:
                done(task)
            except RuntimeError:            # widgets deleted under us
                pass
        task.taskCompleted.connect(finish)
        task.taskTerminated.connect(finish)
        QgsApplication.taskManager().addTask(task)
        return task

    def _cancel(self):
        for t in list(self._tasks):
            try:
                t.cancel()
            except RuntimeError:
                pass

    def _on_search_done(self, task, gen, request):
        if gen != self._gen:
            return                          # a newer search (or a new project) superseded it
        self._busy(False)
        self._refresh_usage()
        err = task.error
        if err is not None or task.ranked is None:
            if err is None or getattr(err, "cancelled", False):
                self._set_summary("info", "Search cancelled; the previous results are kept.")
                self._log_line("Search cancelled.")
                return
            msg = err.user_message
            detail = f" [{err.detail}]" if err.detail and err.detail not in msg else ""
            self._log_line("Search failed: " + msg + detail)
            self._set_summary("error", msg + " (The previous results are kept.)")
            self._warn(msg)
            if err.kind in ("key_not_sent", "key_rejected", "wrong_auth_scheme"):
                self._key_box.setCollapsed(False)
                self._set_key_status("error", msg)
            return
        clause = getattr(task, "product_clause", None)
        if clause in api.PRODUCT_CLAUSES:
            self._remember_clause(clause)
            if clause == "none":
                self._log_line("GEGD could not read either Vivid exclusion form; Vivid and "
                               "base-mosaic records were dropped here instead.")
        for e in getattr(task, "hint_errors", None) or []:
            self._log_line("A follow-up request for the empty-state hint failed (the "
                           "results are complete without it): " + e.user_message)
        strips = list(getattr(task, "strips", None) or [])
        feats = _features_from_raw(getattr(task, "raw_features_json", None))
        if not strips and feats:
            try:
                strips = _parse_feature_list(feats)
            except api.GegdError:
                strips = []
        if not strips:
            strips = [r.strip for r in task.ranked.pre + task.ranked.post]
        if feats is None:
            feats = [strip_feature(s) for s in strips]
        self._search = dict(
            request, strips=strips, features=feats,
            nearest_pre=getattr(task, "nearest_pre", None),
            nearest_post=getattr(task, "nearest_post", None),
            canary_hits=getattr(task, "canary_hits", None),
            searched_utc=datetime.now(timezone.utc), product_clause=clause or "not_in",
            restored=False)
        self._reset_hidden()
        ranked = self._rerank_now(reset_picks=True, vectors=False)
        n_req = getattr(task, "n_requests", None)
        self._log_line(
            f"GEGD returned {ranked.n_found} record(s)"
            + (f" in {n_req} request(s)" if n_req else "") + f": "
            f"{len(ranked.pre)} before, {len(ranked.post)} after touch the circle"
            + (f"; {ranked.n_outside} outside it" if ranked.n_outside else "")
            + (f"; {ranked.n_excluded} Vivid/base mosaic dropped" if ranked.n_excluded else "")
            + ".")
        for m in ranked.messages:
            self._log_line("  " + m)
        star = [r for r in (ranked.star_pre, ranked.star_post) if r is not None]
        if star and self.auto_stream_check.isChecked():
            self._stream_strips(star, replace=True)
        self._rebuild_vectors(aoi=True)
        path = self._save_search()
        if path:
            self._log_line(f"Search cached in {path} (no key, no pixels).")

    # ================================================================ ranking
    def _schedule_rerank(self, *_):
        if self._search is not None and not self._torn_down:
            self._rerank_timer.start()

    def _rerank_now(self, reset_picks=False, vectors=True):
        """Re-rank the strips in hand for the current filters. No request: gegd_rank
        is pure, so a filter change is free."""
        s = self._search
        if s is None or self._torn_down:
            return None
        ranked = R.rank(s["strips"], s["origin"], s["lat"], s["lon"], s["r_km"],
                        self._filters(), t0=s["t0"], t1=s["t1"],
                        nearest_pre=s["nearest_pre"], nearest_post=s["nearest_post"],
                        canary_hits=s["canary_hits"])
        self._ranked = ranked
        listed = {r.strip.feature_id for r in ranked.pre + ranked.post}
        if reset_picks:
            self._picks = {r.strip.feature_id for r in (ranked.star_pre, ranked.star_post)
                           if r is not None}
        else:
            self._picks &= listed
        self._fill_table()
        self._refresh_summary()
        if vectors:
            self._rebuild_vectors(aoi=False)
        self._update_buttons()
        return ranked

    def _reset_hidden(self):
        """Back to listing only what the filters pass, without re-entering
        _toggle_hidden (a new search re-ranks anyway)."""
        self._show_hidden = False
        self._show_hidden_btn.blockSignals(True)
        self._show_hidden_btn.setChecked(False)
        self._show_hidden_btn.blockSignals(False)

    def _toggle_hidden(self, on):
        self._show_hidden = bool(on)
        if self._ranked is not None:
            self._fill_table()
            self._refresh_summary()
            self._rebuild_vectors(aoi=False)

    # ================================================================ table
    def _rows(self):
        r = self._ranked
        if r is None:
            return []
        return [x for x in list(r.pre) + list(r.post)
                if x.hidden_reason is None or self._show_hidden]

    def _fill_table(self):
        self._filling = True
        try:
            t = self._table
            t.clearSelection()
            t.setRowCount(0)
            rows = self._rows()
            self._row_strips = rows
            t.setRowCount(len(rows))
            for i, r in enumerate(rows):
                for c, item in enumerate(self._row_items(r)):
                    t.setItem(i, c, item)
            t.resizeColumnsToContents()
        finally:
            self._filling = False
        self._detail_label.clear()

    def _row_items(self, r):
        s = r.strip
        pre = r.side == "pre"
        word = "before" if pre else "after"
        pick = QTableWidgetItem(("★ " if r.star else "") + word)
        pick.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        pick.setCheckState(Qt.Checked if s.feature_id in self._picks else Qt.Unchecked)
        pick.setData(Qt.UserRole, s.feature_id)
        gap = -r.gap_days if pre else r.gap_days
        date = f"{s.acq_utc:%Y-%m-%d %H:%M} ({gap:+.1f} d)" if s.acq_utc else "?"
        band = (s.band_config or "").upper()
        sensor = s.sensor or "?"
        if band in ("P", "PAN"):
            sensor += " Pan"
        elif band:
            sensor += " RGB" if band.startswith("RGB") or band in ("PS", "MS") else f" {band}"
        badges = []
        if r.stereo:
            badges.append("stereo")
        if (s.product_name or "").upper() == INGESTED_PRODUCT:
            badges.append("ingested")
        if s.is_third_party:
            badges.append("3rd-party")
        if s.multi_date:
            badges.append("mosaic")
        if badges:
            sensor += " · " + ", ".join(badges)
        ona = "—" if s.ona is None else f"{s.ona:.1f}"
        if s.processing_level:
            ona += f" ({s.processing_level})"
        sun = "—" if s.sun_el is None else f"{s.sun_el:.0f}"
        cover = f"{100.0 * (r.cover or 0.0):.0f}%"
        if not r.covers_epicentre:
            cover += " (misses epicentre)"
        cloud = "—" if s.cloud is None else f"{100.0 * s.cloud:.1f}"
        items = [pick] + [QTableWidgetItem(x) for x in (date, sensor, ona, sun, cover, cloud)]
        tip = self._row_tip(r)
        muted = r.hidden_reason is not None or r.grade != "mapping"
        for it in items:
            if it is not pick:
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            it.setBackground(QBrush(PRE_BG if pre else POST_BG))
            it.setForeground(QBrush(MUTED_FG if muted else ROW_FG))
            it.setToolTip(tip)
            if muted:
                f = it.font(); f.setItalic(True); it.setFont(f)
        if r.grade == "visual":
            items[3].setForeground(QBrush(GRADE_VISUAL_FG))
        if not r.covers_epicentre:
            items[5].setForeground(QBrush(MISSES_EPICENTRE_FG))
        if r.star:
            f = pick.font(); f.setBold(True); pick.setFont(f)
        return items

    def _row_tip(self, r):
        """Everything the seven columns leave out, for the row tooltip."""
        s = r.strip
        word = "before" if r.side == "pre" else "after"
        lines = []
        if s.acq_utc:
            lines.append(f"{s.acq_utc:%Y-%m-%d %H:%M:%S} UTC = {R.alaska_str(s.acq_utc)}")
        what = [s.sensor or "?"]
        if s.band_config:
            what.append(s.band_config)
        if s.gsd_m:
            what.append(f"GSD {s.gsd_m:.2f} m")
        what.append(f"{word}, {r.gap_days:.1f} d from the event")
        lines.append(" · ".join(what))
        geo = []
        if s.ona is not None:
            geo.append(f"off-nadir {s.ona:.1f}°")
        if r.incidence is not None:
            geo.append(f"incidence {r.incidence:.1f}°")
        if s.processing_level:
            lvl = {"LV3D": "fine DEM", "LV2A": "coarse DEM"}.get(s.processing_level.upper(), "")
            geo.append(f"{s.processing_level}" + (f" ({lvl})" if lvl else ""))
        if s.dem_correction:
            geo.append(f"DEM {s.dem_correction}")
        if s.ce90 is not None and (s.processing_level or "").upper() == "LV3D":
            geo.append(f"CE90 {s.ce90:.1f} m")
        if geo:
            lines.append(" · ".join(geo))
        if r.shift50 is not None:
            lines.append(f"If the source dropped 50 m, this image puts the new surface "
                         f"~{r.shift50:.0f} m sideways (pre-event DEM).")
        sun = []
        if s.sun_el is not None:
            sun.append(f"sun {s.sun_el:.0f}° high")
        if s.sun_az is not None:
            sun.append(f"azimuth {s.sun_az:.0f}°")
        if s.cloud is not None:
            sun.append(f"strip cloud {100.0 * s.cloud:.1f}% (whole strip)")
        if sun:
            lines.append(" · ".join(sun))
        lines.append(f"Covers {100.0 * (r.cover or 0.0):.0f}% of the search circle, "
                     + ("including the epicentre." if r.covers_epicentre
                        else "but NOT the epicentre."))
        lines.append({"mapping": "Mapping grade: safe to trace on.",
                      "visual": "Visual only: look, don't trace (tens of metres off in relief).",
                      "context": "Context only: not a before/after image "
                                 "(SAR, third-party or multi-date mosaic)."}.get(r.grade, ""))
        if r.hidden_reason:
            lines.append(f"Hidden by Scene filters: {r.hidden_reason}.")
        if r.stereo:
            lines.append(f"In-track stereo with {len(r.stereo)} other strip(s) seconds "
                         "apart: one look from two angles, not two dates.")
        if (s.product_name or "").upper() == INGESTED_PRODUCT:
            lines.append("Ingested on request (PRIVATE_FEATURE), not from the Daily Take "
                         "archive.")
        if s.is_third_party:
            lines.append(f"Third-party ({s.vendor or s.sensor or 'other vendor'}): separate "
                         "licence.")
        ids = [f"featureId {s.feature_id}"]
        if s.catalog_id:
            ids.insert(0, f"catalog id {s.catalog_id}")
        lines.append(" · ".join(ids))
        lag = s.ingest_lag_h
        if lag is not None:
            lines.append(f"Online {lag:.1f} h after collection.")
        return "\n".join(x for x in lines if x)

    def _detail_html(self, r):
        return "<br>".join(_esc(x) for x in self._row_tip(r).split("\n"))

    def _selected_rows(self):
        rows = sorted({i.row() for i in self._table.selectionModel().selectedRows()})
        return [self._row_strips[i] for i in rows if 0 <= i < len(self._row_strips)]

    def _on_table_selection(self):
        if self._filling:
            return
        picked = self._selected_rows()
        if len(picked) == 1:
            self._detail_label.setText(self._detail_html(picked[0]))
        elif picked:
            self._detail_label.setText(f"{len(picked)} strips selected (their footprints are "
                                       "selected on the map).")
        else:
            self._detail_label.clear()
        if not self._syncing:
            self._select_footprints({r.strip.feature_id for r in picked})

    def _on_item_changed(self, item):
        if self._filling or item is None or item.column() != 0:
            return
        fid = item.data(Qt.UserRole)
        if not fid:
            return
        if item.checkState() == Qt.Checked:
            self._picks.add(fid)
        else:
            self._picks.discard(fid)

    def _on_double_click(self, item):
        row = item.row() if item is not None else -1
        if 0 <= row < len(self._row_strips):
            self._stream_strips([self._row_strips[row]], replace=False)

    # ================================================================ footprints
    def _vector_group(self):
        s = self._search
        return lg.name("Maxar", lg.date_pair(f"{s['origin']:%Y-%m-%d}", ""),
                       lg.radius_tag(s["r_km"]), "search area")

    def _rebuild_vectors(self, aoi=False):
        """Footprints (and with `aoi`, the search circle) for the current ranking.
        Memory layers, replaced each time: free, and no scale gate."""
        s = self._search
        if s is None or self._ranked is None or self._torn_down:
            return
        group = self._vector_group()
        if aoi or self._aoi_layer is None:
            old, self._aoi_layer = self._aoi_layer, None
            if old is not None:
                lg.remove_layer(old)
            lyr = gl.aoi_layer(s["lat"], s["lon"], s["r_km"], s["event_id"])
            lg.add_to_group(lyr, group)
            self._aoi_layer = lyr
        old, self._footprint_layer = self._footprint_layer, None
        self._fp_ids = {}
        if old is not None:
            lg.remove_layer(old)
        try:
            lyr = gl.footprint_layer(self._ranked, s["event_id"],
                                     include_hidden=self._show_hidden)
        except TypeError:                   # a footprint_layer without include_hidden
            lyr = gl.footprint_layer(self._ranked, s["event_id"])
        lg.add_to_group(lyr, group)
        self._footprint_layer = lyr
        self._hook_footprints()
        self._apply_outlines(only_hide=True)

    def _apply_outlines(self, *_, only_hide=False):
        """Show or hide the footprints and the search circle (layer-tree visibility,
        so a hidden layer costs nothing to draw). Hiding also clears the selection.

        `only_hide` is for new or adopted layers: they arrive visible, so only an
        unticked box needs applying, and a layer the user switched off by hand in the
        Layers panel is not switched back on behind their back."""
        on = self.show_outlines_check.isChecked()
        if on and only_hide:
            return
        for lyr in (self._footprint_layer, self._aoi_layer):
            node = self._node(lyr)
            if node is not None:
                node.setItemVisibilityChecked(on)
        if not on:
            self._clear_selection()

    def _clear_selection(self):
        """Deselect every row and every footprint on the map."""
        self._table.clearSelection()        # -> _on_table_selection -> empty map selection
        lyr = self._footprint_layer
        if lyr is None:
            return
        try:                                # rows a filter hides can still be selected on the map
            if lyr.selectedFeatureCount():
                lyr.removeSelection()
        except RuntimeError:
            self._footprint_layer = None

    def _hook_footprints(self):
        lyr = self._footprint_layer
        self._fp_ids = {}
        if lyr is None:
            return
        gl.style_selection(lyr)             # also restyles footprints adopted from a project
        try:
            idx = lyr.fields().indexOf("feature_id")
            if idx >= 0:
                self._fp_ids = {f.attribute(idx): f.id() for f in lyr.getFeatures()}
            if self._fp_hooked is not lyr:
                lyr.selectionChanged.connect(self._on_fp_selection)
                self._fp_hooked = lyr
        except RuntimeError:
            self._footprint_layer = None

    def _select_footprints(self, fids):
        lyr = self._footprint_layer
        if lyr is None:
            return
        self._syncing = True
        try:
            lyr.selectByIds([self._fp_ids[f] for f in fids if f in self._fp_ids])
        except RuntimeError:
            self._footprint_layer = None
        finally:
            self._syncing = False

    def _on_fp_selection(self, *_):
        """Map selection -> table selection (the other half of the sync)."""
        lyr = self._footprint_layer
        if self._syncing or lyr is None or self._torn_down:
            return
        try:
            idx = lyr.fields().indexOf("feature_id")
            fids = {f.attribute(idx) for f in lyr.selectedFeatures()} if idx >= 0 else set()
        except RuntimeError:
            return
        self._syncing = True
        try:
            self._table.clearSelection()
            sm = self._table.selectionModel()
            for i, r in enumerate(self._row_strips):
                if r.strip.feature_id in fids:
                    sm.select(self._table.model().index(i, 0),
                              QItemSelectionModel.Select | QItemSelectionModel.Rows)
        except RuntimeError:                # the table went with the dock
            pass
        finally:
            self._syncing = False

    # ================================================================ streams
    def _node(self, lyr):
        if lyr is None:
            return None
        try:
            return QgsProject.instance().layerTreeRoot().findLayer(lyr.id())
        except (RuntimeError, AttributeError):
            return None

    @staticmethod
    def _top(rows):
        """The row of a side that is shown: its ★ if picked, else the best ranked."""
        if not rows:
            return None
        star = [r for r in rows if r.star]
        return star[0] if star else rows[0]

    def _stream_picks(self):
        r = self._ranked
        if r is None:
            self._warn("Search first: there is nothing to stream yet.")
            return
        picks = [x for x in list(r.pre) + list(r.post) if x.strip.feature_id in self._picks]
        if not picks:
            picks = [x for x in (r.star_pre, r.star_post) if x is not None]
        if not picks:
            self._warn("Tick at least one strip in the Pick column (no ★ pair was found).")
            return
        self._stream_strips(picks, replace=True)

    def _drop_streams(self):
        """Remove the previous stream set — and nothing else."""
        for lyr in self._stream_layers:
            lg.remove_layer(lyr)
        self._stream_layers = []
        self._compare = (None, None)

    def _stream_strips(self, rows, replace):
        """Stream `rows` (RankedStrips). replace=True swaps out the previous stream set;
        False (double-click) adds beside it (see _add_streams). The top before/after
        pair is visible, after on top; extra picks are added switched off (they
        fetch nothing)."""
        if not rows:
            return []
        if not gegd_auth.ensure_ready(self):
            self._warn(self._password_store_refused("read"))
            return []
        if not replace and self._compare != (None, None):
            return self._add_streams(rows)
        s = self._search or {}
        if replace:
            self._drop_streams()
        pre = [x for x in rows if x.side == "pre"]
        post = [x for x in rows if x.side == "post"]
        top_pre, top_post = self._top(pre), self._top(post)
        pre_d = f"{top_pre.strip.acq_utc:%Y-%m-%d}" if top_pre else ""
        post_d = f"{top_post.strip.acq_utc:%Y-%m-%d}" if top_post else ""
        name = lg.name("Maxar", lg.date_pair(pre_d, post_d),
                       lg.radius_tag(s.get("r_km")), "stream")
        self._stream_group = name
        group = self._stream_group_node(name)
        # lg.add_to puts each new layer on top: hidden extras first, the shown
        # before next, the shown after last, so after ends up above before.
        order = ([x for x in pre if x is not top_pre] + ([top_pre] if top_pre else [])
                 + [x for x in post if x is not top_post] + ([top_post] if top_post else []))
        made = {}
        for x in order:
            lyr = self._make_stream(x)
            if lyr is None:
                continue
            lg.add_to(lyr, group)
            node = self._node(lyr)
            shown = (x is top_pre or x is top_post)
            if node is not None:
                node.setItemVisibilityChecked(shown)
            self._stream_layers.append(lyr)
            made[id(x)] = lyr
        if not made and not group.children():           # nothing built: no empty folder
            (group.parent() or QgsProject.instance().layerTreeRoot()).removeChildNode(group)
        before, after = self._compare
        if top_pre is not None and id(top_pre) in made:
            before = made[id(top_pre)]
        if top_post is not None and id(top_post) in made:
            after = made[id(top_post)]
        self._compare = (before, after)
        if made:
            names = ", ".join(("★ " if x.star else "") + x.strip.label
                              for x in order if id(x) in made)
            self._log_line(f"Streaming {names} (after on top; Flip toggles it).")
        self._update_buttons()
        return [made[id(x)] for x in order if id(x) in made]

    def _stream_group_node(self, name):
        """The stream set's folder, created directly BELOW the search-area folder
        when that exists: footprints, search circle and the selection highlight are
        outlines only, and must stay drawn over the opaque strips whatever was
        clicked first (layer_group would put a new folder at the very top)."""
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(name)
        if group is not None:
            return group
        idx = 0
        if self._search is not None:
            vname = self._vector_group()
            for i, child in enumerate(root.children()):
                if isinstance(child, QgsLayerTreeGroup) and child.name() == vname:
                    idx = i + 1
                    break
        return root.insertGroup(idx, name)

    def _stream_for(self, fid):
        """The stream layer already showing featureId `fid`, or None."""
        for lyr in self._stream_layers:
            if gl.get_prop(lyr, "fid") == fid and self._node(lyr) is not None:
                return lyr
        return None

    def _add_streams(self, rows):
        """Double-click: add strips beside the current stream set, removing nothing.

        Each one becomes the SHOWN strip of its side of the Flip pair and lands
        where Flip needs it: a new after-strip directly above the current after, a
        new before-strip directly below it, so the after stays on top and Flip
        still reveals the before. The strip it takes over from is switched off
        (kept, fetching nothing). A strip already streamed is moved and switched
        on, never added twice."""
        made = []
        for x in rows:
            lyr = self._stream_for(x.strip.feature_id)
            fresh = lyr is None
            if fresh:
                lyr = self._make_stream(x)
                if lyr is None:
                    continue
            before, after = self._compare
            old = before if x.side == "pre" else after
            if lyr is not old:
                self._place_stream(lyr, x.side, fresh)
                if fresh:
                    self._stream_layers.append(lyr)
                node_old = self._node(old)
                if node_old is not None:
                    node_old.setItemVisibilityChecked(False)
                self._compare = (lyr, after) if x.side == "pre" else (before, lyr)
            node = self._node(lyr)
            if node is not None:
                node.setItemVisibilityChecked(True)
            made.append((x, lyr))
        for x, _l in made:
            self._log_line(f"Streaming {x.strip.label} as the shown "
                           f"{'before' if x.side == 'pre' else 'after'}-strip; the one it "
                           "replaces stays in the list, switched off (Flip toggles the "
                           "after-strip).")
        self._update_buttons()
        return [lyr for _x, lyr in made]

    @staticmethod
    def _child_index(parent, node):
        lid = node.layerId()
        for i, child in enumerate(parent.children()):
            if isinstance(child, QgsLayerTreeLayer) and child.layerId() == lid:
                return i
        return 0

    def _place_stream(self, lyr, side, fresh):
        """Put `lyr` where Flip needs it (see _add_streams): an after-strip directly
        above the pair's after-strip, a before-strip directly below it. With no
        after-strip on the map, beside the before-strip; with neither, at the top of
        the stream folder. An existing layer is moved (clone, then remove: the layer
        itself never leaves the project)."""
        before, after = self._compare
        node_a, node_b = self._node(after), self._node(before)
        anchor = node_a if node_a is not None else node_b
        if anchor is not None and anchor.parent() is not None:
            parent = anchor.parent()
            idx = self._child_index(parent, anchor)
            if side == "pre" and node_a is not None:
                idx += 1
        else:
            name = self._stream_group or lg.name(
                "Maxar", lg.radius_tag((self._search or {}).get("r_km")), "stream")
            self._stream_group = name
            parent, idx = self._stream_group_node(name), 0
        node = None if fresh else self._node(lyr)
        if node is None:
            if fresh:
                QgsProject.instance().addMapLayer(lyr, False)
            parent.insertLayer(idx, lyr)
            return
        clone = node.clone()
        parent.insertChildNode(idx, clone)
        old_parent = node.parent()
        if old_parent is not None:
            old_parent.removeChildNode(node)

    def _make_stream(self, x):
        try:
            if self._host == api.HOST_DEFAULT:
                lyr = gl.make_stream_layer(x, x.side)
            else:
                lyr = gl.make_stream_layer(x, x.side, host=self._host)
        except (ValueError, api.GegdError) as e:
            self._log_line(f"Could not build a stream for {x.strip.label}: "
                           f"{api.redact(str(e))}")
            return None
        if not lyr.isValid():
            self._log_line(f"QGIS rejected the stream layer for {x.strip.label}.")
            return None
        if self._search:
            gl.set_props(lyr, event_id=self._search.get("event_id", ""))
        return lyr

    def _flip(self):
        """Toggle the after-strip of the compare pair (no request: both are rendered)."""
        before, after = self._compare
        node_a = self._node(after)
        if node_a is None:
            self._warn("Stream a before/after pair first (Search, or Stream picks).")
            self._update_buttons()
            return
        node_b = self._node(before)
        if node_b is not None and not node_b.itemVisibilityChecked():
            node_b.setItemVisibilityChecked(True)
        node_a.setItemVisibilityChecked(not node_a.itemVisibilityChecked())

    # ================================================================ map
    def _zoom(self):
        s = self._search
        if s is not None:
            lat, lon, r = s["lat"], s["lon"], s["r_km"]
        else:
            ll = self._latlon()
            if ll is None:
                self._warn("Enter the event latitude and longitude first.")
                return
            (lat, lon), r = ll, self.radius_spin.value()
        lonmin, latmin, lonmax, latmax = R.event_bbox(lat, lon, r)
        rect = QgsRectangle(lonmin, latmin, lonmax, latmax)
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

    # ================================================================ project
    def _out_dir(self):
        """<project folder>/gegd (dock.results_dir(): the saved project's folder, or
        the shared fallback for an unsaved one). '' when neither exists."""
        base = self.dock.results_dir()
        return os.path.join(base, "gegd") if base else ""

    def _search_doc(self):
        s = self._search
        return {
            "format": SEARCH_FORMAT, "version": SEARCH_VERSION,
            "searched_utc": f"{s['searched_utc']:%Y-%m-%dT%H:%M:%SZ}",
            "host": s.get("host") or api.HOST_DEFAULT,
            "event_id": s["event_id"], "lat": s["lat"], "lon": s["lon"],
            "radius_km": s["r_km"], "origin_utc": _iso(s["origin"]),
            "pre_days": s.get("pre_days"), "post_days": s.get("post_days"),
            "t0": _iso(s["t0"]), "t1": _iso(s["t1"]),
            "product_clause": s.get("product_clause") or "not_in",
            "nearest_pre": _nearest_to_json(s.get("nearest_pre")),
            "nearest_post": _nearest_to_json(s.get("nearest_post")),
            "canary_hits": s.get("canary_hits"),
            "picks": sorted(self._picks),
            "features": s["features"],
        }

    def _save_search(self):
        """Write search.json beside the project (no key, no pixels); its path or ''."""
        if self._search is None:
            return ""
        base = self._out_dir()
        if not base:
            self._log_line("Search not cached: save the project first (or set the "
                           "Environment results folder).")
            return ""
        folder = os.path.join(base, self._search["event_id"])
        path = os.path.join(folder, "search.json")
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = path + ".partial"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._search_doc(), fh, indent=1)
            os.replace(tmp, path)             # never a half-written cache (Drive sync)
            with open(path, encoding="utf-8") as fh:
                ok = len(json.load(fh).get("features", [])) == len(self._search["features"])
        except (OSError, ValueError, TypeError) as e:
            self._log_line(f"Could not cache the search in {path}: {e}")
            return ""
        if not ok:
            self._log_line(f"The cached search in {path} did not read back intact.")
            return ""
        return path

    def _doc_head(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and doc.get("format") == SEARCH_FORMAT else None

    def _doc_matches(self, doc):
        """True when a cached search is for the event in the fields now."""
        ll = self._latlon()
        if doc is None or ll is None:
            return False
        try:
            lat, lon = float(doc["lat"]), float(doc["lon"])
            origin = api.parse_time(doc["origin_utc"])
        except (KeyError, TypeError, ValueError):
            return False
        if origin is None or abs(lat - ll[0]) > MATCH_DEG or abs(lon - ll[1]) > MATCH_DEG:
            return False
        return abs((origin - self._origin_utc()[0]).total_seconds()) <= MATCH_S

    def _find_search_doc(self):
        """(path, doc) of the cached search for the event in the fields, or (None, None):
        <gegd>/<event_id>/search.json first, else the newest matching one."""
        base = self._out_dir()
        if not base or not os.path.isdir(base):
            return None, None
        exact = os.path.join(base, self._event_id(), "search.json")
        doc = self._doc_head(exact) if os.path.isfile(exact) else None
        if self._doc_matches(doc):
            return exact, doc
        best = (None, None, -1.0)
        try:
            names = os.listdir(base)
        except OSError:
            return None, None
        for name in names:
            p = os.path.join(base, name, "search.json")
            if p == exact or not os.path.isfile(p):
                continue
            d = self._doc_head(p)
            if self._doc_matches(d):
                mt = os.path.getmtime(p)
                if mt > best[2]:
                    best = (p, d, mt)
        return best[0], best[1]

    def _restore_search(self):
        """Put the cached search for this event back in the table: no request."""
        path, doc = self._find_search_doc()
        if doc is None:
            return False
        try:
            strips = _parse_feature_list(doc.get("features") or [])
            search = dict(
                event_id=_safe_id(doc.get("event_id")), lat=float(doc["lat"]),
                lon=float(doc["lon"]), r_km=float(doc["radius_km"]),
                origin=api.parse_time(doc["origin_utc"]), t0=api.parse_time(doc["t0"]),
                t1=api.parse_time(doc["t1"]), pre_days=doc.get("pre_days"),
                post_days=doc.get("post_days"), host=doc.get("host") or api.HOST_DEFAULT,
                strips=strips, features=list(doc.get("features") or []),
                nearest_pre=_nearest_from_json(doc.get("nearest_pre")),
                nearest_post=_nearest_from_json(doc.get("nearest_post")),
                canary_hits=doc.get("canary_hits"),
                searched_utc=api.parse_time(doc.get("searched_utc")) or datetime.now(timezone.utc),
                product_clause=doc.get("product_clause") or "not_in", restored=True)
        except (api.GegdError, KeyError, TypeError, ValueError) as e:
            self._log_line(f"Could not read the cached search {path}: {api.redact(str(e))}")
            return False
        if None in (search["origin"], search["t0"], search["t1"]):
            self._log_line(f"The cached search {path} has unreadable times; ignored.")
            return False
        self._search = search
        self._picks = set(doc.get("picks") or [])
        self._rerank_now(reset_picks=not self._picks, vectors=False)
        self._rebuild_vectors(aoi=True)
        self._log_line(f"Restored the last search from {path} (no request).")
        return True

    def _defer_restore(self):
        QTimer.singleShot(0, self._restore_from_project)

    def _restore_from_project(self):
        """Adopt this project's Maxar layers and restore its last search."""
        if self._torn_down:
            return
        try:
            self._adopt()
            if self._search is None:
                self._restore_search()
        except RuntimeError:
            pass

    def _adopt(self):
        found = gl.adopt_layers(QgsProject.instance())
        self._stream_layers = list(found.get("stream") or [])
        self._footprint_layer = found.get("footprints")
        self._aoi_layer = found.get("aoi")
        self._compare = self._pair_from(self._stream_layers)
        # The folder the pair sits in, so a double-click after a reopen lands beside
        # it instead of opening a stray 'Maxar stream' folder.
        self._stream_group = ""
        for lyr in (self._compare[1], self._compare[0]):
            node = self._node(lyr)
            parent = node.parent() if node is not None else None
            if isinstance(parent, QgsLayerTreeGroup) and parent.parent() is not None:
                self._stream_group = parent.name()
                break
        self._hook_footprints()
        self._apply_outlines(only_hide=True)
        self._update_buttons()

    def _pair_from(self, layers):
        """(before, after) among stream layers: visible ones first, top first."""
        def visible(lyr):
            node = self._node(lyr)
            return node is not None and node.itemVisibilityChecked()
        before = after = None
        for lyr in sorted(layers, key=lambda l: not visible(l)):
            side = gl.get_prop(lyr, "side")
            if side == "post" and after is None:
                after = lyr
            elif side == "pre" and before is None:
                before = lyr
        return before, after

    def _on_project_read(self, *_):
        # After project_state has put the fields back (it defers one tick too).
        self._defer_restore()

    def _on_project_cleared(self, *_):
        """File > New, or the first step of opening another project: forget this
        project's search and layers (QGIS has already dropped the layers)."""
        if self._torn_down:
            return
        self._gen += 1                       # an in-flight search belongs to the old project
        self._busy(False)
        self._search = None
        self._ranked = None
        self._picks = set()
        self._stream_layers = []
        self._compare = (None, None)
        self._footprint_layer = None
        self._aoi_layer = None
        self._fp_ids = {}
        try:
            self._reset_hidden()
            self._fill_table()
            self._summary_label.clear()
            self._update_buttons()
        except RuntimeError:
            pass

    def _on_layers_removed(self, removed):
        """Forget layers the user (or another tab) removed."""
        ids = set()
        for x in (removed if isinstance(removed, (list, tuple)) else [removed]):
            try:
                ids.add(x if isinstance(x, str) else x.id())
            except (RuntimeError, AttributeError):
                pass

        def gone(lyr):
            if lyr is None:
                return True
            try:
                return lyr.id() in ids
            except RuntimeError:
                return True
        self._stream_layers = [l for l in self._stream_layers if not gone(l)]
        b, a = self._compare
        self._compare = (None if gone(b) else b, None if gone(a) else a)
        if self._footprint_layer is not None and gone(self._footprint_layer):
            self._footprint_layer = None
            self._fp_ids = {}
        if self._aoi_layer is not None and gone(self._aoi_layer):
            self._aoi_layer = None
        if not self._torn_down:
            try:
                self._update_buttons()
            except RuntimeError:
                pass

    # ================================================================ status
    def _refresh_summary(self):
        r = self._ranked
        s = self._search
        if r is None or s is None:
            self._summary_label.clear()
            self._show_hidden_btn.hide()
            return
        vis_pre = sum(1 for x in r.pre if x.hidden_reason is None)
        vis_post = sum(1 for x in r.post if x.hidden_reason is None)
        lines = []
        if s.get("restored"):
            lines.append(f"<i>From the last search, {s['searched_utc']:%Y-%m-%d %H:%M} UTC; "
                         "Search to refresh.</i>")
        head = f"{vis_pre + vis_post} strip(s) listed ({vis_pre} before, {vis_post} after)"
        if r.n_hidden:
            head += f", {r.n_hidden} hidden by Scene filters"
        lines.append(_esc(head) + ".")
        stars = []
        for word, x in (("before", r.star_pre), ("after", r.star_post)):
            if x is not None:
                ona = f", ONA {x.strip.ona:.0f}°" if x.strip.ona is not None else ""
                stars.append(f"★ {word} {_esc(x.strip.label)}{ona}")
        if stars:
            lines.append("<b>" + " → ".join(stars) + "</b>")
        warn = vis_pre == 0 or vis_post == 0
        for m in r.messages:
            if m:
                lines.append(status_html("warn", _esc(m)) if warn else _esc(m))
        self._summary_label.setTextFormat(Qt.RichText)
        self._summary_label.setText("<br>".join(x for x in lines if x))
        self._summary_label.setStyleSheet("")
        self._show_hidden_btn.setVisible(bool(r.n_hidden) or self._show_hidden)
        self._show_hidden_btn.setText("Hide filtered strips" if self._show_hidden
                                      else f"Show hidden strips ({r.n_hidden})")

    def _set_summary(self, kind, text):
        self._summary_label.setTextFormat(Qt.PlainText)
        self._summary_label.setText(status_text(kind, api.redact(text)))
        self._summary_label.setStyleSheet(status_css(kind))

    def _busy(self, on):
        self._busy_on = bool(on)
        try:
            self._progress.setVisible(self._busy_on)
        except RuntimeError:
            return
        self._update_buttons()

    def _update_buttons(self):
        if self._torn_down:
            return
        busy = self._busy_on
        r = self._ranked
        have = r is not None and any(x.hidden_reason is None for x in list(r.pre) + list(r.post))
        self.search_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy or self._key_task is not None)
        self.test_key_btn.setEnabled(self._key_task is None)
        self.stream_btn.setEnabled(have)
        self.flip_btn.setEnabled(self._node(self._compare[1]) is not None)
        self.zoom_btn.setEnabled(self._search is not None or self._latlon() is not None)

    def _log_line(self, line):
        try:
            self._log.appendPlainText(api.redact(str(line)))
        except RuntimeError:
            pass

    def _warn(self, text):
        text = api.redact(str(text))
        self.iface.messageBar().pushWarning("Maxar VHR", text.replace("\n", " "))
        self._log_line(text)

    # ================================================================ teardown
    def teardown(self):
        """Cancel tasks, stop timers, disconnect the detection and project signals.
        The HostGuard stays: it belongs to the plugin, not the dock."""
        self._torn_down = True
        for t in list(self._tasks):
            try:
                t.cancel()
            except RuntimeError:
                pass
        self._tasks.clear()
        for timer in (self._rerank_timer, self._usage_timer):
            try:
                timer.stop()
            except RuntimeError:
                pass
        for signal, slot in self._signals:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._signals = []
        lyr = self._fp_hooked
        self._fp_hooked = None
        if lyr is not None:
            try:
                lyr.selectionChanged.disconnect(self._on_fp_selection)
            except (TypeError, RuntimeError):
                pass
