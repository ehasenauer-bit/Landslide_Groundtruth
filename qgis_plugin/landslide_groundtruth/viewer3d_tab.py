"""The 3D viewer tab: a Google-Earth-style native QGIS 3D scene, driven from here.

Replaces the old DEM-differencing tab. The goal is the thing Google Earth is good
at and the flat 2D canvas is not — draping the pre/post imagery, the SWIR / dNDVI /
dNDSI change rasters and a hillshade over real terrain so a rock/ice-avalanche scar
and its runout can be read in 3D against the cirque/headwall geometry that produced
it.

Why it opens a separate QGIS 3D dock rather than living inside this tab: QGIS 3.44's
Python API does not expose `Qgs3DMapCanvas.setMapSettings`, so a plugin cannot embed
a *configured* interactive 3D canvas in its own widget. The one supported entry point
is `iface.createNewMapCanvas3D()`, which returns a fully wired native 3D canvas in its
own dock. So this tab is a control panel: it resolves a terrain DEM (auto-fetched
ArcticDEM 2 m, or a DEM already in the project), picks which layers to drape, then
creates/updates that native 3D view and points the camera at the AOI. The 3D dock is
a normal QGIS dock — drag it onto this panel's tab to sit them side by side.

Terrain sources:
  * Auto-fetch ArcticDEM 2 m — reuses the project's DEM-strip search
    (`run_single.py --prefer dem --search-only`, PGC's anonymous AWS Open Data) to
    find strips over the AOI, then warps the chosen strip to a local UTM GeoTIFF via
    dem_diff.warp (/vsicurl ranged COG reads — only the AOI's bytes are fetched).
  * A DEM raster already loaded in the project (e.g. a local ArcticDEM / Copernicus
    GLO-30 mosaic) — used directly, nothing fetched.

The elevation-differencing that used to live here is gone by request; the geodesy
helpers it shared (dem_diff.warp / utm_bounds / write_gtiff) are reused above.
"""
import os
import math
import re

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDoubleSpinBox, QDateTimeEdit, QCheckBox,
    QProgressBar, QPlainTextEdit, QListWidget, QListWidgetItem, QSpinBox,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsVectorLayer, QgsTask,
    QgsVector3D, QgsHillshadeRenderer, QgsGeometry, QgsPointXY, Qgis,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRectangle,
)
from qgis.gui import QgsCollapsibleGroupBox, QgsCheckableComboBox

from . import dem_diff
from . import layer_group as lg
from .task import PipelineTask
from .flow_layout import FlowRow

# The terrain settings class we build ourselves (the canvas owns its
# Qgs3DMapSettings; we mutate that live and only construct the terrain settings).
# Guarded so the tab still loads and reports cleanly if a build lacks the 3D module
# rather than failing to import the whole plugin.
try:
    from qgis._3d import QgsDemTerrainSettings
    HAS_3D = True
    _IMPORT_3D_ERR = None
except Exception as e:                       # pragma: no cover - depends on build
    HAS_3D = False
    _IMPORT_3D_ERR = str(e)

# the native 3D view's title (used to create / find / close it)
VIEW_NAME = "Landslide 3D"

# Planetary Computer's public asset-signing endpoint. 3DEP 'data' COGs live in a
# private Azure container; their SAS tokens expire (~1 h), so the pipeline stores
# the UNSIGNED blob href in search.json and we re-sign fresh just before the warp.
PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

# machine 'dem_source' tags that mean a PGC SETSM strip (vs a fallback DEM)
PGC_SOURCES = ("arcticdem", "earthdem", "rema")

# Fallback DEM tiers, in the order they're tried when an ArcticDEM strip warps to
# <50% valid pixels (or fails outright): USGS 3DEP first (finer where it exists,
# but US-only), then NRCan MRDEM (30 m, seamless over Canada — the one that
# actually delivers terrain just north of the border). Matched on dem_source
# prefix; see _tier_of.
FALLBACK_TIERS = ("3dep", "mrdem")

# When the chosen terrain is an ArcticDEM strip, warp it together with the other
# overlapping strips so one strip's gaps get filled by another's (a single
# opportunistic stereo pass can leave the event point in a hole even when its
# footprint clips the AOI). Cap the mosaic so a decade of strips over a well-
# imaged geocell can't fan out into dozens of /vsicurl reads — the greedy
# footprint pick in _pgc_gapfill_cands stops as soon as the AOI is blanketed.
MAX_MOSAIC_STRIPS = 8


def _pc_sign(href):
    """SAS-sign a Planetary Computer blob href. Returns the signed URL, or the
    original href on any failure (the warp then fails cleanly and is reported).
    Runs off the GUI thread inside the warp worker, so it uses stdlib urllib."""
    if not href or "windows.net" not in href:
        return href                      # already local / non-PC → leave as-is
    try:
        import json
        from urllib.request import urlopen
        from urllib.parse import quote
        url = PC_SIGN_URL + "?href=" + quote(href, safe="")
        with urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("href") or href
    except Exception:
        return href

# warp resolution for the auto-fetched ArcticDEM terrain. 2 m is native but the
# ranged download and the terrain tessellation both grow quadratically as the
# pixel shrinks; 10 m is plenty to read cirque/headwall/scar relief in 3D and
# keeps a 5 km-radius AOI to a ~1000² grid (a few seconds).
TERRAIN_RES = [
    ("10 m — balanced (recommended)", 10),
    ("5 m — sharp", 5),
    ("2 m — native (heaviest)", 2),
    ("20 m — fast", 20),
]

# DEM strips are opportunistic stereo tasking, not a revisit schedule, so cast a
# wide net when auto-fetching a terrain base — any good ArcticDEM strip over the
# AOI will do (this is context terrain, not a dated pre/post pair).
SEARCH_PRE_DAYS = 3650
SEARCH_POST_DAYS = 3650

# An acquisition-date tag in a run's layer name: '_2023-08-09', or the
# '_2023-08-05_to_2023-08-09' span of a multi-scene composite (see
# review_package._date_tag). Stripped before pairing pre with post, since the two
# sides carry DIFFERENT dates by definition and would otherwise never match.
_DATE_TAG_RE = re.compile(r"_\d{4}-\d{2}-\d{2}(?:_to_\d{4}-\d{2}-\d{2})?(?=_|$)")


class Viewer3DTab(QWidget):
    def __init__(self, dock):
        super().__init__()
        self.dock = dock                 # shared Environment fields + helpers
        self.iface = dock.iface
        self.canvas = dock.canvas
        self.settings = dock.settings

        self.task = None                 # in-flight strip search (subprocess)
        self._warp_task = None           # in-flight ArcticDEM warp (in-process)
        self._search_result = None       # last search.json (strip candidates)
        self._gen = 0                    # terrain-build generation (drops stale warps)
        self._pgc_fallback = None        # best ArcticDEM warp set aside while trying
                                         # 3DEP, to install if 3DEP comes up empty

        self._dem_layer = None           # QgsRasterLayer used as terrain
        self._dem_mean_z = None          # mean AOI elevation, for the camera
        self._dem_date = None            # terrain source acquisition date (display)
        self._hillshade_layer = None     # optional draped multidirectional hillshade
        self._canvas3d = None            # the native 3D canvas we created
        self._scene_extent = None        # last scene extent (scene CRS), for Reset
        self._flip_cache = {}            # imagery layer id -> aligned cache layer id
        self._extra_scene_layers = []    # non-drape layer ids kept in the scene (point)

        # A terrain/hillshade layer removed from the project leaves a dangling C++
        # handle; drop our reference before the object is deleted so later access
        # (e.g. the before/after refresh) can't hit "C/C++ object has been deleted".
        try:
            QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_removed)
        except Exception:
            pass

        self._build_ui()
        self._on_source_changed()
        self._refresh_drape_list()

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        intro = QLabel(
            "Drape the pre/post imagery, the SWIR / dNDVI / dNDSI change rasters "
            "and a hillshade over real terrain in a native QGIS 3D scene — read the "
            "scar and its runout against the cirque/headwall that produced it. "
            "Opens as a QGIS 3D dock (drag it onto this tab to dock them together).")
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: palette(mid); }")
        root.addWidget(intro)

        if not HAS_3D:
            warn = QLabel(
                "⚠ This QGIS build has no 3D support (qgis._3d failed to import): "
                f"{_IMPORT_3D_ERR}. Install/enable QGIS 3D to use this tab.")
            warn.setWordWrap(True)
            warn.setStyleSheet("QLabel { color: #b2182b; }")
            root.addWidget(warn)

        # --- event AOI (own copy; button pulls from the Sentinel tab) ---
        form = QFormLayout()
        self.lat_edit = QLineEdit()
        self.lon_edit = QLineEdit()
        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.2, 50.0)
        self.radius_spin.setSingleStep(0.5)
        self.radius_spin.setValue(5.0)
        self.radius_spin.setSuffix(" km")
        self.dt_edit = QDateTimeEdit()
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_edit.setCalendarPopup(True)
        try:
            self.dt_edit.setDateTime(self.dock.dt_edit.dateTime())
            self.lat_edit.setText(self.dock.lat_edit.text())
            self.lon_edit.setText(self.dock.lon_edit.text())
            self.radius_spin.setValue(self.dock.radius_spin.value())
        except Exception:
            pass
        form.addRow("Latitude", self.lat_edit)
        form.addRow("Longitude", self.lon_edit)
        form.addRow("Search radius", self.radius_spin)
        form.addRow("Event time (UTC)", self.dt_edit)
        root.addLayout(form)

        copy_btn = QPushButton("Copy location & date from Sentinel-2 / Landsat tab")
        copy_btn.clicked.connect(self._copy_from_main)
        root.addWidget(copy_btn)

        # --- terrain group -------------------------------------------------
        terr_box = QgsCollapsibleGroupBox("Terrain (DEM)")
        tform = QFormLayout(terr_box)

        self.source_combo = QComboBox()
        self.source_combo.addItem("Auto-fetch ArcticDEM 2 m (PGC)", "arcticdem")
        self.source_combo.addItem("Use a DEM layer loaded in the project", "loaded")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        tform.addRow("Source", self.source_combo)

        # loaded-DEM picker (raster layers in the project)
        self.loaded_combo = QComboBox()
        self.loaded_dem_row_label = QLabel("Loaded DEM / DSM")
        self.loaded_combo.setToolTip(
            "Any single-band elevation raster already loaded in the project — a "
            "DSM (surface, canopy/buildings in) or a DTM (bare earth). Pick it, "
            "tag its model below, then Fetch / build terrain.")
        tform.addRow(self.loaded_dem_row_label, self.loaded_combo)

        # tag the loaded layer's terrain model so the layer name + the volume-tab
        # DSM-vs-DTM guardrail know what it is (the search path tags this itself).
        self.loaded_model_combo = QComboBox()
        self.loaded_model_combo.addItem("Surface model (DSM — canopy/buildings in)", "DSM")
        self.loaded_model_combo.addItem("Bare-earth model (DTM)", "DTM")
        self.loaded_model_combo.addItem("Unknown / don't tag", "")
        self.loaded_model_row_label = QLabel("This layer is a")
        tform.addRow(self.loaded_model_row_label, self.loaded_model_combo)

        # auto-fetch controls
        self.res_combo = QComboBox()
        for label, val in TERRAIN_RES:
            self.res_combo.addItem(label, val)
        self.res_row_label = QLabel("Fetch resolution")
        tform.addRow(self.res_row_label, self.res_combo)

        self.strip_combo = QComboBox()
        self.strip_combo.setEnabled(False)
        self.strip_row_label = QLabel("ArcticDEM strip")
        self.strip_combo.currentIndexChanged.connect(self._on_strip_changed)
        tform.addRow(self.strip_row_label, self.strip_combo)

        self.vscale_spin = QDoubleSpinBox()
        self.vscale_spin.setRange(0.5, 5.0)
        self.vscale_spin.setSingleStep(0.5)
        self.vscale_spin.setValue(2.0)
        self.vscale_spin.setToolTip(
            "Vertical exaggeration of the terrain in the 3D scene. 1.5–2× reads "
            "cirque/headwall relief without cartoonish spikes.")
        tform.addRow("Vertical exaggeration", self.vscale_spin)

        hs_row = QHBoxLayout()
        self.hillshade_check = QCheckBox("Add multidirectional hillshade")
        self.hillshade_check.setChecked(True)
        self.hillshade_check.setToolTip(
            "Add a multidirectional shaded-relief layer of the DEM to the drape "
            "list, so terrain morphology reads even where imagery is transparent.")
        self.zfactor_spin = QDoubleSpinBox()
        self.zfactor_spin.setRange(0.5, 5.0)
        self.zfactor_spin.setSingleStep(0.5)
        self.zfactor_spin.setValue(1.0)
        # Was an unlabelled "z 1.00" spinbox; give it a visible label + tooltip so
        # it doesn't read as a mystery control next to the hillshade checkbox.
        zfactor_lbl = QLabel("z-factor")
        zfactor_tip = (
            "Hillshade z-factor: vertical exaggeration applied only to the "
            "shaded-relief calculation (steepens the shading). Separate from the "
            "scene's Vertical exaggeration above; 1.0 = true slope.")
        zfactor_lbl.setToolTip(zfactor_tip)
        self.zfactor_spin.setToolTip(zfactor_tip)
        hs_row.addWidget(self.hillshade_check, 1)
        hs_row.addWidget(zfactor_lbl)
        hs_row.addWidget(self.zfactor_spin)
        tform.addRow("Hillshade", self._wrap(hs_row))

        self.fetch_btn = QPushButton("Fetch / build terrain")
        self.fetch_btn.clicked.connect(self._fetch_terrain)
        tform.addRow(self.fetch_btn)

        self.terrain_label = QLabel("No terrain set.")
        self.terrain_label.setWordWrap(True)
        self.terrain_label.setStyleSheet("QLabel { color: palette(mid); }")
        tform.addRow(self.terrain_label)
        root.addWidget(terr_box)

        # --- draped layers group ------------------------------------------
        drape_box = QgsCollapsibleGroupBox("Draped layers")
        dlay = QVBoxLayout(drape_box)
        hint = QLabel(
            "Tick the layers to drape over the terrain. Top of the list draws on "
            "top — set a layer's opacity in the Layers panel to blend (e.g. imagery "
            "over hillshade).")
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: palette(mid); }")
        dlay.addWidget(hint)
        self.drape_list = QListWidget()
        self.drape_list.setMinimumHeight(120)
        # (un)ticking a drape layer changes what the Before/After pickers may offer
        self.drape_list.itemChanged.connect(self._on_drape_checks_changed)
        dlay.addWidget(self.drape_list)
        refresh_btn = QPushButton("Refresh layer list")
        refresh_btn.clicked.connect(self._refresh_drape_list)
        dlay.addWidget(refresh_btn)
        root.addWidget(drape_box)

        # --- before / after flip (live 3D) --------------------------------
        flip_box = QgsCollapsibleGroupBox("Before / after (3D flip)")
        fbl = QVBoxLayout(flip_box)
        fhint = QLabel(
            "Pick the Before and After image(s), then flip between them in the "
            "open 3D scene — no rebuild, no camera move. Open the 3D view first.")
        fhint.setWordWrap(True)
        fhint.setStyleSheet("QLabel { color: palette(mid); }")
        fbl.addWidget(fhint)

        sel = QFormLayout()
        self.before_combo = QgsCheckableComboBox()
        self.before_combo.setToolTip(
            "Tick one or more layers to drape as the BEFORE image. Tick several "
            "to stack them (first ticked draws on top — set layer opacity in the "
            "Layers panel to blend).")
        self.before_combo.checkedItemsChanged.connect(self._on_ba_changed)
        self.after_combo = QgsCheckableComboBox()
        self.after_combo.setToolTip(
            "Tick one or more layers to drape as the AFTER image. Tick several "
            "to stack them (first ticked draws on top — set layer opacity in the "
            "Layers panel to blend).")
        self.after_combo.checkedItemsChanged.connect(self._on_ba_changed)
        sel.addRow("Before image(s)", self.before_combo)
        sel.addRow("After image(s)", self.after_combo)
        fbl.addLayout(sel)

        brow = QHBoxLayout()
        self.before_btn = QPushButton("◀ Before")
        self.before_btn.setCheckable(True)
        self.before_btn.setToolTip("Drape the PRE (before) image over the terrain.")
        self.before_btn.clicked.connect(lambda: self._flip_to("pre"))
        self.after_btn = QPushButton("After ▶")
        self.after_btn.setCheckable(True)
        self.after_btn.setToolTip("Drape the POST (after) image over the terrain.")
        self.after_btn.clicked.connect(lambda: self._flip_to("post"))
        brow.addWidget(self.before_btn)
        brow.addWidget(self.after_btn)
        fbl.addLayout(brow)

        self.flip_cache_check = QCheckBox(
            "Cache imagery aligned to the terrain for fast flipping")
        self.flip_cache_check.setChecked(True)
        self.flip_cache_check.setToolTip(
            "First flip reprojects each image to the 3D scene's CRS (the terrain "
            "UTM), clips streamed scenes to the AOI, builds overviews, and caches "
            "it under <output dir>/viewer3d/flip_cache. Later flips just swap "
            "those aligned copies, so the scene re-textures the DEM near-"
            "instantly. Uncheck to flip the original layers directly.")
        fbl.addWidget(self.flip_cache_check)

        self.add_point_btn = QPushButton("Add epicentre point to 3D")
        self.add_point_btn.setToolTip(
            "Add the predicted-epicentre point (…_point) to the 3D scene as a "
            "terrain-clamped marker.")
        self.add_point_btn.clicked.connect(self._add_point_to_3d)
        fbl.addWidget(self.add_point_btn)

        orow = QFormLayout()
        self.overlay_combo = QgsCheckableComboBox()
        self.overlay_combo.setToolTip(
            "Vector layers to drape on the exported 3D web viewer's terrain: "
            "polygons (translucent fill + outline), lines, and points (peaks — a "
            "marker on a short pole). Tick any you want baked into the viewer.")
        orow.addRow("Overlays (web)", self.overlay_combo)
        fbl.addLayout(orow)

        self.web_btn = QPushButton("Export instant-flip 3D web viewer")
        self.web_btn.setToolTip(
            "Bake the DEM plus the ticked Before/After image(s) into a single "
            "standalone HTML 3D viewer and open it in your browser. Both images "
            "preload onto the GPU, so you orbit freely and flip before/after "
            "instantly with zero loading — unlike QGIS's 3D view, which re-"
            "textures the terrain on every flip. Fully offline / self-contained.")
        self.web_btn.setEnabled(False)   # needs a terrain DEM first
        self.web_btn.clicked.connect(self._export_web_viewer)
        fbl.addWidget(self.web_btn)
        root.addWidget(flip_box)

        # --- figure Details (captions for the exported PNG) ---------------
        det_box = QgsCollapsibleGroupBox("Figure details (exported PNG)")
        det_box.setSaveCollapsedState(False)
        det_box.setCollapsed(True)
        dform = QFormLayout(det_box)
        # imagery source is a dropdown of the plugin's sources (editable for others)
        self.det_imagery_sat = QComboBox()
        self.det_imagery_sat.setEditable(True)
        self.det_imagery_sat.addItems([
            "", "PlanetScope (~3 m)", "Sentinel-2 (~10 m)", "Landsat (~30 m)",
            "Sentinel-1 (SAR)", "Maxar / WorldView"])
        self.det_imagery_sat.setToolTip(
            "Satellite/source of the before & after imagery — pick one or type your own.")
        dform.addRow("Imagery satellite", self.det_imagery_sat)
        self.det_terrain_sat = QLineEdit()
        self.det_area = QLineEdit()
        self.det_cl_length = QLineEdit()
        self.det_cl_drop = QLineEdit()
        self.det_volume = QLineEdit()
        for lbl, w, tip in (
            ("Terrain satellite", self.det_terrain_sat, "auto-filled from the DEM source; editable"),
            ("Area", self.det_area, "landslide area (Pull from the Volume tab, or type)"),
            ("Centerline length", self.det_cl_length, "runout horizontal length"),
            ("Vertical drop", self.det_cl_drop, "centerline elevation drop"),
            ("Volume", self.det_volume, "paste the estimate from the Volume tab"),
        ):
            # the placeholder already shows `tip` in the empty field; an
            # identical tooltip would only repeat it (rule b), so don't set one.
            w.setPlaceholderText(tip)
            dform.addRow(lbl, w)
        self.pull_vol_btn = QPushButton("↻ Pull area / centerline / volume from Volume tab")
        self.pull_vol_btn.setToolTip(
            "Copy the latest area, centerline length + vertical drop, and volume "
            "estimate from the 'Volume from area' tab into the fields above. Run a "
            "measurement (and the centerline) there first.")
        self.pull_vol_btn.clicked.connect(self._pull_volume_details)
        dform.addRow(self.pull_vol_btn)
        root.addWidget(det_box)

        # --- actions -------------------------------------------------------
        # FlowRow (not a fixed QHBoxLayout) so the three buttons wrap onto a
        # second line instead of clipping their labels in a narrow dock.
        btn_row = FlowRow()
        self.open_btn = QPushButton("Open / update 3D view")
        self.open_btn.clicked.connect(self._open_view)
        self.open_btn.setEnabled(False)
        self.open_btn.setToolTip(
            "Fetch / build terrain first — the 3D scene needs a terrain DEM. "
            "Once terrain is built this opens (or updates) the QGIS 3D view.")
        # open_btn was the primary action (added with stretch 2); FlowRow has no
        # stretch, so carry that emphasis with a bold font + default button.
        f = self.open_btn.font(); f.setBold(True); self.open_btn.setFont(f)
        self.open_btn.setDefault(True)
        self.reset_btn = QPushButton("Reset camera")
        self.reset_btn.clicked.connect(self._reset_camera)
        self.close_btn = QPushButton("Close 3D view")
        self.close_btn.clicked.connect(self._close_view)
        btn_row.addWidget(self.open_btn)
        btn_row.addWidget(self.reset_btn)
        btn_row.addWidget(self.close_btn)
        root.addWidget(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(90)
        root.addWidget(self.log, 1)

        if not HAS_3D:
            for w in (self.fetch_btn, self.open_btn, self.reset_btn, self.close_btn,
                      self.before_btn, self.after_btn, self.add_point_btn):
                w.setEnabled(False)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    # ---------------------------------------------------------- helpers ----
    def _log(self, msg):
        self.log.appendPlainText(msg)

    def _warn(self, msg):
        self.iface.messageBar().pushWarning("3D viewer", msg)

    def _busy(self, on):
        self.progress.setVisible(on)
        self.fetch_btn.setEnabled(not on and HAS_3D)

    def _copy_from_main(self):
        d = self.dock
        self.lat_edit.setText(d.lat_edit.text())
        self.lon_edit.setText(d.lon_edit.text())
        self.radius_spin.setValue(d.radius_spin.value())
        self.dt_edit.setDateTime(d.dt_edit.dateTime())

    def _on_source_changed(self):
        auto = self.source_combo.currentData() == "arcticdem"
        for w in (self.res_combo, self.res_row_label, self.strip_combo,
                  self.strip_row_label):
            w.setVisible(auto)
        for w in (self.loaded_combo, self.loaded_dem_row_label,
                  self.loaded_model_combo, self.loaded_model_row_label):
            w.setVisible(not auto)
        if not auto:
            self._populate_loaded_dems()

    def _project_rasters(self):
        """Project raster layers in top-to-bottom drawing order."""
        order = QgsProject.instance().layerTreeRoot().layerOrder()
        return [lyr for lyr in order if isinstance(lyr, QgsRasterLayer)]

    def _populate_loaded_dems(self):
        self.loaded_combo.clear()
        for lyr in self._project_rasters():
            # single-band rasters are the DEM candidates; skip our own 3-band tifs
            if lyr.bandCount() == 1:
                self.loaded_combo.addItem(lyr.name(), lyr.id())
        if self.loaded_combo.count() == 0:
            self.loaded_combo.addItem("— no single-band raster in project —", None)

    def _refresh_drape_list(self):
        """List project rasters as checkable drape candidates.

        Preserve the user's ticks across refreshes; on the first populate (nothing
        ticked yet) default to the layers currently visible in the tree."""
        prev = set(self._checked_drape_ids())
        self.drape_list.blockSignals(True)      # rebuild silently; refresh once below
        self.drape_list.clear()
        root = QgsProject.instance().layerTreeRoot()
        for lyr in self._project_rasters():
            if lyr.name().endswith("(3D cache)"):
                continue          # our own aligned flip copies — not a drape choice
            node = root.findLayer(lyr.id())
            item = QListWidgetItem(lyr.name())
            item.setData(Qt.UserRole, lyr.id())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            if prev:
                check = lyr.id() in prev
            else:
                check = bool(node and node.isVisible())
            item.setCheckState(Qt.Checked if check else Qt.Unchecked)
            self.drape_list.addItem(item)
        self.drape_list.blockSignals(False)
        self._refresh_before_after()

    def _checked_drape_ids(self):
        ids = []
        for i in range(self.drape_list.count()):
            it = self.drape_list.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        return ids

    def _on_drape_checks_changed(self, *_):
        """A drape layer was (un)ticked. The Before/After pickers only offer
        layers that are being draped, so re-populate them to match."""
        self._refresh_before_after()

    def _aoi(self):
        """(lat, lon, radius_km) or None with a warning."""
        try:
            lat = float(self.lat_edit.text().strip())
            lon = float(self.lon_edit.text().strip())
        except ValueError:
            self._warn("Enter valid numeric latitude and longitude.")
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            self._warn("Latitude must be -90..90 and longitude -180..180.")
            return None
        return lat, lon, self.radius_spin.value()

    # ----------------------------------------------------- terrain build ----
    def _fetch_terrain(self):
        if not HAS_3D:
            return
        if self.source_combo.currentData() == "loaded":
            self._use_loaded_dem()
            return
        # auto-fetch: run the DEM-strip search, then warp the chosen strip
        aoi = self._aoi()
        if aoi is None:
            return
        c = self._collect_search()
        if c is None:
            return
        python, script, project, out, args = c
        self.log.clear()
        self.strip_combo.blockSignals(True)
        self.strip_combo.clear()
        self.strip_combo.setEnabled(False)
        self.strip_combo.blockSignals(False)
        self._search_result = None
        self._pgc_fallback = None
        self._busy(True)
        self._log("Searching PGC for ArcticDEM strips over the AOI (free, no download)…")
        self.task = PipelineTask(python, script, project, args, out,
                                 result_name="search.json")
        self.task.logLine.connect(self._log)
        self.task.taskCompleted.connect(self._on_search_done)
        self.task.taskTerminated.connect(self._on_search_done)
        QgsApplication.taskManager().addTask(self.task)

    def _collect_search(self):
        """Build the run_single.py CLI for a DEM-strip search, reusing the dock's
        Environment fields. Mirrors the old DEM tab's search dry-run."""
        aoi = self._aoi()
        if aoi is None:
            return None
        lat, lon, radius = aoi
        python = self.dock.python_edit.text().strip()
        project = self.dock.project_edit.text().strip()
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            project, "out", "interactive")
        out = os.path.join(base_out, "viewer3d")
        script = os.path.join(project, "run_single.py")
        if not (python and os.path.exists(python)):
            self._warn("Set a valid venv python path in Environment (top of the panel).")
            return None
        if not os.path.exists(script):
            self._warn(f"run_single.py not found in project dir:\n{script}")
            return None
        os.makedirs(out, exist_ok=True)
        when = self.dt_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        args = [
            "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
            "--datetime", when, "--radius-km", f"{radius:.2f}",
            "--pre-days", str(SEARCH_PRE_DAYS), "--post-days", str(SEARCH_POST_DAYS),
            "--prefer", "dem", "--search-only", "--out", out,
        ]
        return python, script, project, out, args

    def _on_search_done(self):
        self._busy(False)
        result = getattr(self.task, "result", None)
        self.task = None
        if not result:
            self._log("Search finished with no result.")
            return
        self._search_result = result
        # merge pre + post candidates (ArcticDEM strips + the 3DEP fallbacks the
        # pipeline appends); keep only those carrying a data URL.
        cands = list(result.get("pre", [])) + list(result.get("post", []))
        cands = [c for c in cands if c.get("dem_url") or c.get("dem_urls")]
        if not cands:
            self._log("No DEM (ArcticDEM or 3DEP) found over this AOI. Try a larger "
                      "radius, or load a DEM manually and use 'Use a DEM layer'.")
            self._warn("No DEM found over the AOI.")
            return
        cands = self._rank_strips(cands)
        self.strip_combo.blockSignals(True)
        self.strip_combo.clear()
        for c in cands:
            date = (c.get("date") or "seamless")[:10]
            src = c.get("source", "DEM")
            model = c.get("terrain_model", "")
            cov = "covers event" if c.get("_covers") else "overlaps AOI"
            label = " ".join(x for x in (date, "·", src, model, "·", cov) if x)
            self.strip_combo.addItem(label, c)
        self.strip_combo.setEnabled(True)
        # default selection: ArcticDEM if its footprints blanket >=50% of the AOI,
        # otherwise the best 3DEP fallback (the <50% trigger).
        self.strip_combo.setCurrentIndex(self._pick_default_strip(cands))
        self.strip_combo.blockSignals(False)
        n_pgc = sum(1 for c in cands if c.get("dem_source") in PGC_SOURCES)
        n_3dep = sum(1 for c in cands if str(c.get("dem_source", "")).startswith("3dep"))
        self._log(f"Found {n_pgc} ArcticDEM + {n_3dep} 3DEP DEM option(s). Warping "
                  f"the selected one; switch with the dropdown if it has gaps.")
        for note in result.get("notes", []):
            self._log("note: " + note)
        self._warp_selected_strip()

    def _rank_strips(self, cands):
        """Best-first, in source tiers so a fallback never outranks usable
        ArcticDEM.

        Tier 0 = PGC SETSM strips (ArcticDEM/EarthDEM/REMA), tier 1 = 3DEP,
        tier 2 = MRDEM (Canada). Within a tier: entries covering the event point
        first, then ArcticDEM by newest acquisition and the fallbacks by finest
        resolution (DSM 1 m → 10 m → 30 m)."""
        result = self._search_result or {}
        try:
            lat, lon = float(result.get("lat")), float(result.get("lon"))
        except (TypeError, ValueError):
            lat = lon = None
        for c in cands:
            c["_covers"] = self._covers_point(c, lat, lon)

        def key(c):
            covers = 0 if c["_covers"] else 1
            if c.get("dem_source") in PGC_SOURCES:
                dstr = (c.get("date") or "").replace("-", "")
                dnum = int(dstr) if dstr.isdigit() else 0
                return (0, covers, -dnum)          # newest ArcticDEM first
            ds = str(c.get("dem_source", ""))
            tier = 1 if ds.startswith("3dep") else 2   # 3DEP before MRDEM
            return (tier, covers, c.get("resolution_m") or 999)   # finest first

        cands.sort(key=key)
        return cands

    def _covers_point(self, cand, lat, lon):
        """True if the strip footprint contains the event point. Strips without
        footprint metadata are not excluded (treated as covering), matching the
        old DEM tab — a long thin strip can overlap the AOI box yet miss the point."""
        if lat is None:
            return True
        geom = self.dock._qgs_geom(cand.get("geometry"))
        if geom is None or geom.isEmpty():
            return True
        return geom.contains(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))

    def _arcticdem_footprint_cover(self, cands, lat, lon, radius_km):
        """Fraction of the AOI box the ArcticDEM footprints (union) blanket.

        Cheap, geometry-only, zero byte reads — answers 'can ArcticDEM cover this
        AOI at all' (the <50% trigger) better than any single strip's post-warp
        valid-pixel fraction. Uses unaryUnion so overlapping strips aren't double
        counted. Interior holes the footprint polygon can't see are caught later
        by the post-warp check in _on_warp_done."""
        dlat = radius_km / 111.32
        dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
        aoi = QgsGeometry.fromRect(
            QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat))
        geoms = []
        for c in cands:
            if c.get("dem_source") not in PGC_SOURCES:
                continue
            g = self.dock._qgs_geom(c.get("geometry"))
            if g is not None and not g.isEmpty():
                geoms.append(g)
        if not geoms:
            return 0.0
        inter = QgsGeometry.unaryUnion(geoms).intersection(aoi)
        area = aoi.area()
        if inter is None or inter.isEmpty() or area <= 0:
            return 0.0
        return min(1.0, inter.area() / area)

    def _point_in_pgc_footprint(self, cands, lat, lon):
        """True if the event point lies inside the union of ArcticDEM strip
        footprints — i.e. at least one strip actually has data AT the point, not
        just somewhere in the AOI box. This is the coverage that matters: a strip
        can blanket >=50% of the box off to one side and still leave the centred
        point in a hole (the '56% covered but the point is bare' case). Strips
        without footprint geometry are ignored here; if none carry geometry this
        returns False and the box-coverage test below still gets a say."""
        pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
        for c in cands:
            if c.get("dem_source") not in PGC_SOURCES:
                continue
            g = self.dock._qgs_geom(c.get("geometry"))
            if g is not None and not g.isEmpty() and g.contains(pt):
                return True
        return False

    def _pick_default_strip(self, cands):
        """Combo index to auto-warp: the best ArcticDEM strip if any strip covers
        the event point OR the strip footprints blanket >=50% of the AOI, else the
        first available fallback (3DEP, then MRDEM). `cands` is ranked, so index 0
        is the best ArcticDEM strip when any exist. The gaps around the chosen
        strip get mosaicked shut at warp time (see _pgc_gapfill_cands)."""
        result = self._search_result or {}
        try:
            lat, lon = float(result.get("lat")), float(result.get("lon"))
            radius = float(result.get("params", {}).get("radius_km"))
        except (TypeError, ValueError):
            return 0
        cover = self._arcticdem_footprint_cover(cands, lat, lon, radius)
        point_ok = self._point_in_pgc_footprint(cands, lat, lon)
        if point_ok or cover >= 0.5:
            why = ("covers the event point" if point_ok
                   else f"blankets ~{cover*100:.0f}% of the AOI (>=50%)")
            self._log(f"ArcticDEM {why} — using ArcticDEM (overlapping strips "
                      f"mosaicked to fill gaps).")
            return 0
        nxt = self._fallback_index("pgc")
        if nxt >= 0:
            src = (self.strip_combo.itemData(nxt) or {}).get("source", "a fallback DEM")
            self._log(f"No ArcticDEM strip covers the event point and its footprints "
                      f"blanket only ~{cover*100:.0f}% of the AOI — defaulting to "
                      f"{src}.")
            return nxt
        self._log(f"No ArcticDEM strip covers the event point and its footprints "
                  f"cover only ~{cover*100:.0f}% of the AOI, and no fallback DEM was "
                  f"found here — using ArcticDEM (may have gaps).")
        return 0

    def _tier_of(self, cand):
        """Source tier of a candidate: 'pgc' (ArcticDEM/EarthDEM/REMA), a fallback
        tier from FALLBACK_TIERS ('3dep' / 'mrdem'), or 'other'."""
        cand = cand or {}
        if cand.get("dem_source") in PGC_SOURCES:
            return "pgc"
        ds = str(cand.get("dem_source", ""))
        for tier in FALLBACK_TIERS:
            if ds.startswith(tier):
                return tier
        return "other"

    def _fallback_index(self, after_tier):
        """Combo index of the next fallback DEM to try after `after_tier`, or -1.

        'pgc' starts at the first fallback tier; a fallback tier starts at the one
        after it — so the chain runs ArcticDEM → 3DEP → MRDEM and stops."""
        if after_tier == "pgc":
            start = 0
        elif after_tier in FALLBACK_TIERS:
            start = FALLBACK_TIERS.index(after_tier) + 1
        else:
            start = 0
        for tier in FALLBACK_TIERS[start:]:
            for i in range(self.strip_combo.count()):
                if self._tier_of(self.strip_combo.itemData(i)) == tier:
                    return i
        return -1

    def _select_and_warp_index(self, idx):
        """Switch the dropdown to a specific candidate and warp it WITHOUT firing
        the manual-pick handler — that would clear the ArcticDEM safety net we
        need if this fallback also comes up empty."""
        self.strip_combo.blockSignals(True)
        self.strip_combo.setCurrentIndex(idx)
        self.strip_combo.blockSignals(False)
        self._warp_selected_strip()

    def _on_strip_changed(self):
        if self.strip_combo.isEnabled() and self.strip_combo.currentData():
            # a manual pick starts fresh — drop any ArcticDEM held from an earlier
            # auto <50% → 3DEP switch, so hand-selecting 3DEP can't install it
            self._pgc_fallback = None
            self._warp_selected_strip()

    @staticmethod
    def _cand_urls(cand):
        """A candidate's data URLs (dem_urls list, or the single dem_url)."""
        return list(cand.get("dem_urls") or
                    ([cand["dem_url"]] if cand.get("dem_url") else []))

    def _mosaic_urls_for(self, primary, lat, lon, radius):
        """URLs to warp for `primary`. For an ArcticDEM strip, greedily fold in
        the other overlapping strips so their footprints, unioned with the
        primary's, blanket the AOI box — one strip's gaps get filled by another's.
        Non-PGC sources (3DEP/MRDEM) are already AOI-wide mosaics, so they warp
        from their own tiles unchanged.

        Ordering matters: gdal.Warp paints sources in list order and the LAST
        valid pixel wins, so `primary` (the strip the dropdown label names, and
        whose date the layer carries) goes last to stay on top, better-ranked
        fillers just under it, and the rest below — the others only ever show
        through where the strips above them have no data."""
        if self._tier_of(primary) != "pgc":
            return self._cand_urls(primary)

        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        box = QgsGeometry.fromRect(
            QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat))
        box_area = box.area()

        def cover_in_box(cand):
            g = self.dock._qgs_geom(cand.get("geometry"))
            if g is None or g.isEmpty():
                return None
            g = g.intersection(box)
            return None if (g is None or g.isEmpty()) else g

        # candidates in ranked (best-first) order, primary excluded
        others = []
        for i in range(self.strip_combo.count()):
            c = self.strip_combo.itemData(i)
            if c is not primary and self._tier_of(c) == "pgc" and self._cand_urls(c):
                others.append(c)

        covered = cover_in_box(primary)
        chosen = []                          # fillers, best-ranked first
        for c in others:
            if len(chosen) + 1 >= MAX_MOSAIC_STRIPS:
                break
            g = cover_in_box(c)
            if g is None:
                continue
            if covered is None:
                covered, chosen = g, chosen + [c]
                continue
            gain = g.difference(covered)
            if gain is not None and not gain.isEmpty() and gain.area() > 0.02 * box_area:
                covered = covered.combine(g)
                chosen.append(c)
            if covered is not None and covered.area() >= 0.98 * box_area:
                break

        # painter's order: worst filler first … best filler … primary last (top).
        # Primary's url(s) are appended last unconditionally, so even if a filler
        # repeats one it can't pull the primary off the top of the stack.
        primary_urls = self._cand_urls(primary)
        pset = set(primary_urls)
        urls, seen = [], set()
        for c in reversed(chosen):
            for u in self._cand_urls(c):
                if u and u not in seen and u not in pset:
                    seen.add(u)
                    urls.append(u)
        urls += [u for u in primary_urls if u]
        return urls

    def _warp_selected_strip(self):
        cand = self.strip_combo.currentData()
        if not cand:
            return
        aoi = self._aoi()
        if aoi is None:
            return
        lat, lon, radius = aoi
        # don't oversample a coarse source onto a finer grid than it carries: warp
        # at max(user resolution, the candidate's native resolution).
        res = max(self.res_combo.currentData(), int(cand.get("resolution_m") or 0))
        epsg = dem_diff.utm_epsg(lat, lon)
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        bounds = dem_diff.utm_bounds(lon - dlon, lat - dlat, lon + dlon, lat + dlat,
                                     epsg, res)
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            self.dock.project_edit.text().strip(), "out", "interactive")
        out_dir = os.path.join(base_out, "viewer3d")
        os.makedirs(out_dir, exist_ok=True)
        # source in the cache name so ArcticDEM and 3DEP at the same epsg/res don't
        # clobber each other.
        src_tag = cand.get("dem_source", "src")
        out_path = os.path.join(out_dir, f"terrain_{epsg}_{res}m_{src_tag}.tif")
        urls = self._mosaic_urls_for(cand, lat, lon, radius)
        if not urls:
            self._warn("Selected DEM candidate has no data URL.")
            return
        src_label = cand.get("source", "DEM")
        self._gen += 1
        gen = self._gen
        self._busy(True)
        if self._tier_of(cand) == "pgc" and len(urls) > 1:
            extra = f" (mosaicking {len(urls)} overlapping strips to fill gaps)"
        elif len(urls) > 1:
            extra = f" ({len(urls)} tiles)"
        else:
            extra = ""
        self._log(f"Warping {src_label}{extra} to {res} m over the AOI (EPSG:{epsg})…")
        self._warp_task = QgsTask.fromFunction(
            f"Warp {src_label}", self._warp_worker,
            on_finished=lambda exc, res_: self._on_warp_done(exc, res_, gen, out_path),
            url=urls, bounds=bounds, epsg=epsg, res=res, out_path=out_path,
            sign=bool(cand.get("needs_signing")))
        QgsApplication.taskManager().addTask(self._warp_task)

    @staticmethod
    def _warp_worker(task, url, bounds, epsg, res, out_path, sign=False):
        """Runs off the GUI thread: /vsicurl ranged read + warp to the AOI grid.
        Touches only GDAL/numpy/urllib (dem_diff + PC signing), never Qt."""
        urls = [url] if isinstance(url, str) else list(url)
        if sign:                          # 3DEP: SAS-sign each blob href fresh
            urls = [_pc_sign(u) for u in urls]
        arr, gt, proj = dem_diff.warp(urls, bounds, epsg, res)
        valid = dem_diff.valid_heights(arr)
        cover = float(valid.mean()) if valid.size else 0.0
        if cover <= 0.0:
            return {"error": "no valid elevation pixels over the AOI"}
        # whether the AOI centre — the event point, since bounds are centred on
        # it — actually resolved to a valid pixel. A strip can cover most of the
        # box yet leave the point itself in a hole, which is the coverage the
        # user cares about; drives the point-aware fallback in _on_warp_done.
        h, w = valid.shape
        point_valid = bool(valid[h // 2, w // 2])
        zmean = float(np.nanmean(arr[valid]))
        dem_diff.write_gtiff(out_path, np.where(valid, arr, np.nan), gt, proj)
        return {"path": out_path, "zmean": zmean, "cover": cover,
                "point_valid": point_valid, "n_sources": len(urls)}

    def _on_warp_done(self, exc, result, gen, out_path):
        if gen != self._gen:
            return                       # a newer fetch superseded this one
        self._busy(False)
        self._warp_task = None

        cand = self.strip_combo.currentData() or {}
        tier = self._tier_of(cand)

        if exc is not None or not result or result.get("error"):
            reason = (str(exc) if exc is not None
                      else (result or {}).get("error", "unknown error"))
            # Walk the fallback chain: the source that just failed hands off to
            # the next tier (ArcticDEM <50% → 3DEP → MRDEM). When the chain is
            # exhausted, install the partial ArcticDEM we set aside rather than
            # leave the user with nothing — it was already warped to disk.
            nxt = self._fallback_index(tier)
            if nxt >= 0:
                src = (self.strip_combo.itemData(nxt) or {}).get("source", "the next DEM")
                self._log(f"{cand.get('source', 'That source')} produced no "
                          f"terrain ({reason}); trying {src}…")
                self._select_and_warp_index(nxt)
                return
            if self._pgc_fallback is not None:
                saved = self._pgc_fallback
                self._pgc_fallback = None
                pct = saved["result"]["cover"] * 100
                self._log(f"No fallback DEM resolved terrain here ({reason}); "
                          f"installing the ArcticDEM strip despite covering just "
                          f"{pct:.0f}% of the AOI. Expect holes — pick another "
                          f"strip or shrink the radius if they matter.")
                self._warn("Fallback DEMs empty here — using the partial "
                           "ArcticDEM strip (see log).")
                self._install_warp_result(saved["result"], saved["cand"])
                return
            if exc is not None:
                self._log(f"Warp failed: {reason}")
                self._warn("DEM warp failed — see the log.")
            else:
                self._log(f"Warp produced no terrain: {reason}. Try another strip "
                          f"or a larger radius.")
            return

        cover = result["cover"]
        point_bare = not result.get("point_valid", True)
        # post-warp safety net: the ArcticDEM mosaic warped to <50% valid pixels
        # over the AOI, or — even at decent box coverage — left the event point
        # itself in a hole (no overlapping strip had data there). Either way try
        # the fallback chain rather than install terrain that's holey where it
        # matters, but hold on to THIS result so that if every fallback is empty
        # here we still fall back to it (a partial surface beats no terrain).
        if tier == "pgc" and (cover < 0.5 or point_bare):
            nxt = self._fallback_index("pgc")
            if nxt >= 0:
                self._pgc_fallback = {"result": result, "cand": cand}
                src = (self.strip_combo.itemData(nxt) or {}).get("source", "a fallback DEM")
                if point_bare:
                    why = ("left the event point in a hole"
                           + (f" and resolved only {cover*100:.0f}% of the AOI"
                              if cover < 0.5 else
                              f" ({cover*100:.0f}% of the AOI covered elsewhere)"))
                else:
                    why = f"resolved only {cover*100:.0f}% valid pixels over the AOI (<50%)"
                self._log(f"The ArcticDEM mosaic {why}; trying {src}…")
                self._select_and_warp_index(nxt)
                return
        self._pgc_fallback = None
        self._install_warp_result(result, cand)

    def _install_warp_result(self, result, cand):
        """Install a completed warp as the terrain layer: mean Z for the camera, a
        descriptive name, the DSM/DTM guardrail note, and a low-coverage warning.

        Shared by the normal success path and the "3DEP was empty, keep the
        partial ArcticDEM" fallback, so both name and warn about the terrain the
        same way — the caller has already decided this result is the one to use."""
        cover = result["cover"]
        self._dem_mean_z = result["zmean"]
        src = cand.get("source", "DEM")
        model = cand.get("terrain_model", "")
        d = cand.get("date")
        if d:
            date_str = d[:10]                       # ArcticDEM strip acquisition day
        elif str(cand.get("dem_source", "")).startswith("3dep-seamless"):
            date_str = "seamless mosaic"            # 3DEP seamless is timeless
        else:
            date_str = "undated"
        n_src = result.get("n_sources", 1)
        is_pgc_mosaic = cand.get("dem_source") in PGC_SOURCES and n_src > 1
        mosaic_tag = f"+{n_src - 1} strip mosaic" if is_pgc_mosaic else ""
        name = " ".join(x for x in (src, model, date_str, mosaic_tag, "terrain",
                                    f"({cover*100:.0f}% AOI cover)") if x)
        self._set_terrain_from_file(result["path"], name, date_str)
        if not self.det_terrain_sat.text().strip():
            self.det_terrain_sat.setText(self._terrain_sensor(cand))
        # A gap-filling mosaic mixes strips of different dates and a few metres of
        # per-strip vertical bias — fine as context terrain, wrong for differencing.
        if is_pgc_mosaic:
            self._log(f"Terrain is a mosaic of {n_src} ArcticDEM strips — the gaps in "
                      f"the {date_str} strip are filled from others. Fine for the 3D "
                      f"view, but the patches carry different dates and small vertical "
                      f"offsets, so don't feed THIS layer into the volume/differencing "
                      f"tab; pick a single dated strip there.")
        # DSM vs DTM guardrail (this terrain may feed the volume/differencing tab).
        if model:
            kind = ("surface model — canopy/buildings INCLUDED" if cand.get("is_dsm")
                    else "bare-earth model — canopy/buildings removed")
            self._log(f"Terrain is a {model} ({kind}). Do NOT difference a DSM "
                      f"against a DTM in the volume tab — the canopy/building height "
                      f"offset reads as fake elevation change.")
        if cover < 0.6:
            self._log(f"Note: this DEM covers only {cover*100:.0f}% of the AOI — pick "
                      f"another entry in the dropdown if the terrain has holes.")

    def _set_terrain_from_file(self, path, name, date=None):
        lyr = QgsRasterLayer(path, name)
        if not lyr.isValid():
            self._warn(f"Could not load terrain raster:\n{path}")
            return
        lg.add_to_group(lyr, "3D terrain")
        self._install_terrain_layer(lyr, date=date)

    def _use_loaded_dem(self):
        lid = self.loaded_combo.currentData()
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is None or not isinstance(lyr, QgsRasterLayer):
            self._warn("Pick a single-band DEM/DSM raster loaded in the project.")
            return
        self._dem_mean_z = self._sample_center_z(lyr)
        model = self.loaded_model_combo.currentData()
        name = f"{lyr.name()} ({model})" if model else lyr.name()
        # the plugin can't know a loaded raster's acquisition date, but DEM files
        # usually carry it in the name/path (e.g. SETSM ..._20150803_...) — sniff it.
        sniff = self._sniff_date(lyr.name(), lyr.source())
        date = f"{sniff} (from filename)" if sniff else "unknown (loaded layer)"
        self._install_terrain_layer(lyr, name, date)
        if not self.det_terrain_sat.text().strip():
            self.det_terrain_sat.setText(lyr.name())
        if model:
            kind = ("surface model — canopy/buildings INCLUDED" if model == "DSM"
                    else "bare-earth model — canopy/buildings removed")
            self._log(f"Loaded terrain tagged as {model} ({kind}). Do NOT difference "
                      f"a DSM against a DTM in the volume tab.")

    def _install_terrain_layer(self, lyr, name=None, date=None):
        """Adopt `lyr` as the terrain DEM, (re)build the hillshade, refresh lists.

        `name` overrides the display label (e.g. a source/model-tagged name);
        `date` is the source's acquisition date (or a note like 'seamless
        mosaic' / 'unknown (loaded layer)'), shown so the terrain's provenance
        is visible."""
        self._dem_layer = lyr
        self._dem_date = date
        label = name or lyr.name()
        datetxt = f"   ·   acquired {date}" if date else ""
        self.terrain_label.setText(f"Terrain: {label}{datetxt}")
        self.open_btn.setEnabled(True)
        self.web_btn.setEnabled(True)
        self._rebuild_hillshade()
        self._refresh_drape_list()
        self._log(f"Terrain ready: {label}"
                  + (f" — acquired {date}" if date else "")
                  + ". Tick drape layers, then 'Open / update 3D view'.")

    @staticmethod
    def _sniff_date(*texts):
        """First YYYY-MM-DD / YYYYMMDD date found in the given strings, or None.

        DEM filenames commonly embed the acquisition date (ArcticDEM/SETSM strips
        as ..._YYYYMMDD_..., Copernicus/USGS tiles as YYYY-MM-DD), so this recovers
        it for a hand-loaded DEM the plugin has no metadata for."""
        import re
        for t in texts:
            if not t:
                continue
            m = re.search(r"(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}", str(t))
            if m:
                s = re.sub(r"[-_]", "", m.group(0))
                mo, dy = int(s[4:6]), int(s[6:8])
                if 1 <= mo <= 12 and 1 <= dy <= 31:      # guard against a random 8-digit run
                    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
        return None

    def _rebuild_hillshade(self):
        """Add/refresh a multidirectional hillshade of the terrain DEM as a drapeable
        layer. Uses a second QgsRasterLayer on the same file with a hillshade
        renderer — no extra file written."""
        # drop a stale hillshade
        if self._hillshade_layer is not None:
            lg.remove_layer(self._hillshade_layer)
            self._hillshade_layer = None
        dem = self._alive(self._dem_layer)
        if not self.hillshade_check.isChecked() or dem is None:
            return
        src = dem.source()
        hs = QgsRasterLayer(src, "Hillshade (multidirectional)")
        if not hs.isValid():
            return
        renderer = QgsHillshadeRenderer(hs.dataProvider(), 1, 315.0, 45.0)
        renderer.setMultiDirectional(True)
        renderer.setZFactor(self.zfactor_spin.value())
        hs.setRenderer(renderer)
        lg.add_to_group(hs, "3D terrain")
        self._hillshade_layer = hs

    @staticmethod
    def _sample_center_z(lyr):
        try:
            c = lyr.extent().center()
            val, ok = lyr.dataProvider().sample(QgsPointXY(c.x(), c.y()), 1)
            return float(val) if ok and val == val else None
        except Exception:
            return None

    # -------------------------------------------------------- 3D view ----
    def _valid_canvas(self):
        c = self._canvas3d
        if c is None:
            return None
        try:
            if c in self.iface.mapCanvases3D():
                return c
        except (RuntimeError, AttributeError):
            pass
        return None

    def _drape_layers(self):
        ids = self._checked_drape_ids()
        proj = QgsProject.instance()
        return [proj.mapLayer(i) for i in ids if proj.mapLayer(i) is not None]

    def _scene_crs_and_extent(self, dem):
        """(scene_crs, extent_in_scene_crs). Keep a projected DEM's own CRS; for a
        geographic DEM build a UTM CRS from its centre and reproject the extent, so
        the Local 3D scene is always in metres."""
        crs = dem.crs()
        ext = dem.extent()
        if crs.isValid() and not crs.isGeographic():
            return crs, ext
        proj = QgsProject.instance()
        wgs84 = QgsCoordinateReferenceSystem.fromEpsgId(4326)
        try:
            c = QgsCoordinateTransform(crs, wgs84, proj).transform(ext.center())
            utm = QgsCoordinateReferenceSystem.fromEpsgId(
                dem_diff.utm_epsg(c.y(), c.x()))
            ext_utm = QgsCoordinateTransform(crs, utm, proj).transformBoundingBox(ext)
            return utm, ext_utm
        except Exception:
            return crs, ext

    def _open_view(self):
        if not HAS_3D:
            return
        if self._dem_layer is None:
            self._warn("Fetch or select a terrain DEM first.")
            return
        dem = self._dem_layer
        if dem.extent().isEmpty():
            self._warn("The terrain DEM has an empty extent.")
            return
        # A "Local" 3D scene needs a projected (metre) CRS or the terrain is
        # degenerate. ArcticDEM auto-fetch is already UTM; a loaded DEM might be
        # geographic (Copernicus GLO-30 ships as EPSG:4326), so give the scene a
        # UTM CRS and reproject the extent into it. The DEM layer stays in its own
        # CRS — QGIS's terrain generator resamples it into the scene grid.
        scene_crs, extent = self._scene_crs_and_extent(dem)
        if not scene_crs.isValid() or extent.isEmpty():
            self._warn("Could not derive a projected scene CRS from the DEM.")
            return
        # Clip the scene to where the ticked before/after imagery actually
        # covers (their combined bbox ∩ DEM); fall back to the full DEM when
        # nothing is ticked yet.
        proj = QgsProject.instance()
        ba_ids = self._checked_ids(self.before_combo) + \
            self._checked_ids(self.after_combo)
        extent = self._drape_crop_extent(
            scene_crs, [proj.mapLayer(i) for i in ba_ids], extent)
        center = extent.center()
        zc = self._dem_mean_z if self._dem_mean_z is not None else 0.0

        canvas = self._valid_canvas()
        if canvas is None:
            canvas = self.iface.createNewMapCanvas3D(VIEW_NAME, Qgis.SceneMode.Local)
            self._canvas3d = canvas
        if canvas is None:
            self._warn("QGIS could not create a 3D map view.")
            return

        ms = canvas.mapSettings()
        if ms is None:
            self._warn("The 3D view has no map settings to configure.")
            return
        ms.setCrs(scene_crs)
        try:
            ms.setOrigin(QgsVector3D(center.x(), center.y(), 0.0))
        except Exception:
            pass
        drape = self._drape_layers()
        proj = QgsProject.instance()
        # keep any explicitly-added extras (e.g. the epicentre point) in the scene.
        extra = [proj.mapLayer(i) for i in self._extra_scene_layers]
        extra = [l for l in extra if l is not None and l not in drape]
        # open the scene showing the ticked BEFORE image(s), so before/after is
        # ready to flip the moment the view appears.
        lead = []
        for lid in self._checked_ids(self.before_combo):
            l = proj.mapLayer(lid)
            if l is not None and l not in drape and l not in extra and l not in lead:
                lead.append(l)
        ms.setLayers(extra + lead + drape)

        terr = QgsDemTerrainSettings()
        terr.setLayer(dem)
        try:
            terr.setVerticalScale(self.vscale_spin.value())
        except Exception:
            pass
        ms.setTerrainSettings(terr)
        ms.setExtent(extent)

        self._scene_extent = extent          # for Reset camera
        self._point_camera(canvas, center, zc, extent)

        n = len(drape)
        self._log(f"3D view updated: terrain '{dem.name()}', {n} draped layer(s), "
                  f"{self.vscale_spin.value():g}× vertical exaggeration.")
        self.iface.messageBar().pushInfo(
            "3D viewer",
            "Opened the native QGIS 3D view. Drag its dock onto this panel's tab to "
            "sit them side by side; orbit with left-drag, tilt with Shift+drag.")
        self._refresh_before_after()   # scene open — enable Before/After flipping
        if self._checked_ids(self.before_combo):
            self.before_btn.setChecked(True)   # scene opened on the before image(s)

    def _point_camera(self, canvas, center, zc, extent):
        cc = canvas.cameraController()
        if cc is None:
            return
        dist = max(extent.width(), extent.height()) * 1.4
        try:
            # oblique "Google Earth" framing; pitch is a tilt in degrees, yaw = north-up
            cc.setLookingAtMapPoint(QgsVector3D(center.x(), center.y(), zc),
                                    dist, 45.0, 0.0)
        except Exception:
            try:
                cc.setViewFromTop(center.x(), center.y(), dist)
            except Exception:
                pass

    def _reset_camera(self):
        canvas = self._valid_canvas()
        if canvas is None or self._scene_extent is None:
            self._warn("Open the 3D view first.")
            return
        extent = self._scene_extent
        center = extent.center()
        zc = self._dem_mean_z if self._dem_mean_z is not None else 0.0
        self._point_camera(canvas, center, zc, extent)

    def _close_view(self):
        try:
            self.iface.closeMapCanvas3D(VIEW_NAME)
        except Exception:
            pass
        self._canvas3d = None

    # ------------------------------------------ before/after flip + point ----
    def _find_pairs(self):
        """Loaded (pre, post) raster pairs as (label, pre_id, post_id).

        Matched by name so it works on whatever's in the project: the run's
        …_pre_<date>_<kind> / …_post_<date>_<kind> outputs (rgb, swir, highlight,
        ndvi, …) and the PlanetScope before/after previews. The acquisition-date
        tag is stripped before matching — it differs between the two sides, which
        is the whole point of it — and put back in the label, so the picker says
        which dates the flip compares. Our own '(3D cache)' copies are excluded so
        we never try to cache a cache."""
        rasters = [l for l in self._project_rasters()
                   if not l.name().endswith("(3D cache)")]
        # Grouped by the undated name, so '…_pre_2023-07-28_rgb' finds
        # '…_post_2023-08-09_rgb' under the shared key '…_post_rgb'. A list per key,
        # not one layer: two runs of the same event (different dates, or an older
        # undated run) can be loaded at once and both deserve a flip entry.
        by_key = {}
        for l in rasters:
            by_key.setdefault(_DATE_TAG_RE.sub("", l.name()), []).append(l)
        raw = []   # (kind_label, base, pre_lyr, post_lyr)
        for key, pres in by_key.items():
            idx = key.find("_pre_")
            if idx == -1:
                continue
            base, kind = key[:idx], key[idx + len("_pre_"):]
            posts = by_key.get(f"{base}_post_{kind}") or []
            # name order = date order, so each pre meets the post of its own run
            for pre_l, post_l in zip(sorted(pres, key=lambda l: l.name()),
                                     sorted(posts, key=lambda l: l.name())):
                raw.append((kind.replace("_", " ") + self._date_span(pre_l, post_l),
                            base, pre_l, post_l))
        multi = len({b for _, b, _, _ in raw}) > 1
        out = []
        for kind, base, pre, post in raw:
            out.append((f"{base} · {kind}" if multi else kind, pre.id(), post.id()))
        # PlanetScope before/after previews (first of each, if both present).
        # Matched on the layer names as-is — that tab names its own layers.
        ps_pre = next((l for l in rasters
                       if l.name().startswith("PlanetScope before")), None)
        ps_post = next((l for l in rasters
                        if l.name().startswith("PlanetScope after")), None)
        if ps_pre is not None and ps_post is not None:
            out.append(("PlanetScope before/after", ps_pre.id(), ps_post.id()))
        return out

    @staticmethod
    def _date_span(pre_lyr, post_lyr):
        """' (2023-07-28 → 2023-08-09)' from the two layer names, or '' if undated.

        The dates are already in the names (review_package puts them there); this
        just lifts them into the flip label so the picker reads as a comparison of
        two dates rather than of two anonymous layers."""
        def tag(lyr):
            m = _DATE_TAG_RE.search(lyr.name())
            return m.group(0).lstrip("_").replace("_to_", "–") if m else None
        pre, post = tag(pre_lyr), tag(post_lyr)
        return f"  ({pre} → {post})" if pre and post else ""

    def _find_point_layer(self):
        for l in QgsProject.instance().mapLayers().values():
            if isinstance(l, QgsVectorLayer) and l.name().endswith("_point"):
                return l
        return None

    @staticmethod
    def _alive(obj):
        """Return obj if its underlying C++ object still exists, else None.

        Guards against 'wrapped C/C++ object … has been deleted' when a stored
        layer is removed from the project but our Python reference lingers."""
        if obj is None:
            return None
        try:
            from qgis.PyQt import sip
        except ImportError:
            try:
                import sip
            except ImportError:
                return obj                     # can't check -> assume alive
        try:
            return None if sip.isdeleted(obj) else obj
        except Exception:
            return None

    def _on_layers_removed(self, layer_ids):
        """Drop terrain/hillshade references when their layers leave the project."""
        ids = set(layer_ids)
        for attr in ("_dem_layer", "_hillshade_layer"):
            lyr = self._alive(getattr(self, attr, None))
            if lyr is not None and lyr.id() in ids:
                setattr(self, attr, None)

    def _candidate_rasters(self):
        """Project rasters selectable as before/after images.

        Excludes our own '(3D cache)' copies and the current terrain DEM /
        hillshade, so the pickers list imagery, not the surface it drapes on."""
        skip = set()
        dem = self._alive(self._dem_layer)
        if dem is not None:
            skip.add(dem.id())
        hs = self._alive(self._hillshade_layer)
        if hs is not None:
            skip.add(hs.id())
        return [l for l in self._project_rasters()
                if not l.name().endswith("(3D cache)") and l.id() not in skip]

    def _refresh_before_after(self):
        """(Re)populate the Before/After pickers, preserving the user's ticks.

        On the first populate, default the ticks to an auto-detected pre/post
        pair (…_pre_/…_post_ or PlanetScope before/after) so the common case
        needs no picking; the user can tick any other layer(s), one or more."""
        # The Before/After pickers only offer layers ticked for draping — you
        # can't flip to imagery that isn't on the terrain. Keep drape-list order;
        # if nothing is ticked yet, fall back to all candidates so the UI isn't
        # dead on first open.
        cand = self._candidate_rasters()
        checked = self._checked_drape_ids()
        if checked:
            rank = {lid: n for n, lid in enumerate(checked)}
            rasters = sorted((l for l in cand if l.id() in rank),
                             key=lambda l: rank[l.id()])
        else:
            rasters = cand
        first = self.before_combo.count() == 0 and self.after_combo.count() == 0
        for combo in (self.before_combo, self.after_combo):
            prev = set(self._checked_ids(combo))
            combo.blockSignals(True)
            combo.clear()
            for l in rasters:
                combo.addItem(l.name(), l.id())
            self._set_checked(combo, prev)
            combo.blockSignals(False)
        if first:
            pairs = self._find_pairs()
            if pairs:
                _, pre_id, post_id = pairs[0]
                for combo, lid in ((self.before_combo, pre_id),
                                   (self.after_combo, post_id)):
                    combo.blockSignals(True)
                    self._set_checked(combo, {lid})
                    combo.blockSignals(False)
        have = (HAS_3D and self.before_combo.count() > 0
                and self.after_combo.count() > 0)
        self.before_btn.setEnabled(have)
        self.after_btn.setEnabled(have)
        if not have:
            self.before_btn.setChecked(False)
            self.after_btn.setChecked(False)
        self.add_point_btn.setEnabled(HAS_3D and self._find_point_layer() is not None)
        # web-viewer overlays: any vector layer (polygon / line / point), stable
        # order so ticks don't jump around on refresh.
        prevv = set(self._checked_ids(self.overlay_combo))
        self.overlay_combo.blockSignals(True)
        self.overlay_combo.clear()
        vlayers = sorted((l for l in QgsProject.instance().mapLayers().values()
                          if isinstance(l, QgsVectorLayer)), key=lambda l: l.name())
        for l in vlayers:
            self.overlay_combo.addItem(l.name(), l.id())
        self._set_checked(self.overlay_combo, prevv)
        self.overlay_combo.blockSignals(False)

    def _checked_ids(self, combo):
        """Layer ids ticked in a QgsCheckableComboBox, in list order."""
        model = combo.model()
        out = []
        if hasattr(model, "item"):
            for i in range(combo.count()):
                it = model.item(i)
                if it is not None and it.checkState() == Qt.Checked:
                    d = combo.itemData(i)
                    if d:
                        out.append(d)
            return out
        try:                                   # fallback: match checked texts
            texts = set(combo.checkedItems())
        except Exception:
            return out
        for i in range(combo.count()):
            if combo.itemText(i) in texts:
                d = combo.itemData(i)
                if d:
                    out.append(d)
        return out

    def _set_checked(self, combo, ids):
        """Tick exactly `ids` in a QgsCheckableComboBox (untick the rest)."""
        want = set(ids)
        model = combo.model()
        if not hasattr(model, "item"):
            return
        for i in range(combo.count()):
            it = model.item(i)
            if it is not None:
                it.setCheckState(
                    Qt.Checked if combo.itemData(i) in want else Qt.Unchecked)

    def _on_ba_changed(self, *_):
        # the before/after choice changed; which side is showing is now unknown
        self.before_btn.setChecked(False)
        self.after_btn.setChecked(False)

    def _scene_ids_with_caches(self, ids):
        """`ids` plus any cache copies built for them (to exclude on a flip)."""
        out = set(ids)
        for oid in ids:
            cid = self._flip_cache.get(oid)
            if cid:
                out.add(cid)
        return out

    def _flip_to(self, side):
        """Drape the ticked Before/After image(s) in the open scene, no rebuild."""
        if not HAS_3D:
            return
        canvas = self._valid_canvas()
        if canvas is None:
            self._warn("Open the 3D view first (Open / update 3D view), then flip.")
            return
        before_ids = self._checked_ids(self.before_combo)
        after_ids = self._checked_ids(self.after_combo)
        if not before_ids or not after_ids:
            self._warn("Tick at least one Before image and one After image.")
            return
        show_ids = before_ids if side == "pre" else after_ids
        # route through the aligned on-disk cache when enabled, so the flip only
        # swaps already-projected layers instead of re-warping onto the DEM.
        if self.flip_cache_check.isChecked() and self._ensure_flip_cache(show_ids):
            show_ids = [self._flip_cache.get(i, i) for i in show_ids]
        proj = QgsProject.instance()
        shown = [l for l in (proj.mapLayer(i) for i in show_ids) if l is not None]
        if not shown:
            self._warn("The selected image layer(s) are no longer in the project.")
            return
        ms = canvas.mapSettings()
        exclude = self._scene_ids_with_caches(before_ids + after_ids)
        base = [l for l in ms.layers() if l.id() not in exclude]
        ms.setLayers(shown + base)             # ticked imagery on top of the rest
        self.before_btn.setChecked(side == "pre")
        self.after_btn.setChecked(side == "post")
        names = ", ".join(l.name() for l in shown)
        self._log(f"3D flip: showing {'before' if side == 'pre' else 'after'} — "
                  f"{names}.")

    # ----- aligned on-disk cache (fast DEM drape) -----
    def _scene_crs(self):
        if self._dem_layer is None:
            return None
        try:
            crs, _ = self._scene_crs_and_extent(self._dem_layer)
        except Exception:
            return None
        return crs if crs.isValid() else None

    def _flip_cache_dir(self):
        base = self.dock.out_edit.text().strip() or os.path.join(
            self.dock.project_edit.text().strip(), "out", "interactive")
        d = os.path.join(base, "viewer3d", "flip_cache")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as e:
            self._log(f"flip cache: cannot create cache dir ({e}).")
            return None

    def _ensure_flip_cache(self, ids):
        """Ensure every id in `ids` has an aligned cache layer; True iff all do."""
        scene_crs = self._scene_crs()
        if scene_crs is None:
            return False
        dst = scene_crs.authid() or scene_crs.toWkt()
        for lid in ids:
            cid = self._flip_cache.get(lid)
            if cid and QgsProject.instance().mapLayer(cid) is not None:
                continue
            src = QgsProject.instance().mapLayer(lid)
            if src is None:
                continue
            made = self._make_flip_cache_layer(src, dst)
            if made is not None:
                self._flip_cache[lid] = made.id()
        return all(
            self._flip_cache.get(i)
            and QgsProject.instance().mapLayer(self._flip_cache[i]) is not None
            for i in ids)

    def _make_flip_cache_layer(self, src, dst_srs):
        """Reproject+clip `src` to a local overview-built GeoTIFF in the scene CRS.

        The heavy work (remote read, warp to the terrain UTM, overview build) runs
        once and is memoised on disk; later flips just toggle the returned layer.
        Returns the cache QgsRasterLayer, or None on any failure (caller then
        flips the original)."""
        try:
            from osgeo import gdal
        except Exception as e:            # QGIS bundles GDAL, but never break a flip
            self._log(f"flip cache: GDAL unavailable ({e}); using originals.")
            return None
        d = self._flip_cache_dir()
        if d is None:
            return None
        src_uri = src.source()
        safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in src.name())
        crs = self._scene_crs()
        dtag = ((crs.authid() if crs else "") or "utm").replace(":", "_")
        out_path = os.path.join(d, f"{safe}__{dtag}.3dcache.tif")
        if not os.path.exists(out_path):
            self._log(f"flip cache: building {os.path.basename(out_path)} …")
            opts = dict(
                format="GTiff",
                creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
                dstSRS=dst_srs, resampleAlg="bilinear",
                multithread=True, warpMemoryLimit=256,
            )
            # clip streamed COGs to the scene footprint to keep the cache small;
            # local run outputs are already AOI-sized, so leave their extent alone.
            if "vsicurl" in src_uri and self._scene_extent is not None:
                e = self._scene_extent
                opts["outputBounds"] = (e.xMinimum(), e.yMinimum(),
                                        e.xMaximum(), e.yMaximum())
            try:
                ds = gdal.Warp(out_path, src_uri, **opts)
            except Exception as ex:
                self._log(f"flip cache: warp failed for {src.name()} ({ex}).")
                return None
            if ds is None:
                self._log(f"flip cache: warp produced nothing for {src.name()}.")
                return None
            try:
                ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
            except Exception:
                pass
            ds = None                     # flush to disk
        lyr = QgsRasterLayer(out_path, f"{src.name()} (3D cache)")
        if not lyr.isValid():
            self._log(f"flip cache: cached layer invalid for {src.name()}.")
            return None
        QgsProject.instance().addMapLayer(lyr, False)   # keep the legend tidy
        self._log(f"flip cache: ready — {lyr.name()}")
        return lyr

    # ----- terrain-clamped epicentre point -----
    def _add_point_to_3d(self):
        if not HAS_3D:
            return
        pt = self._find_point_layer()
        if pt is None:
            self._warn("No epicentre point layer (…_point) in the project.")
            return
        ok, err = self._apply_3d_point_symbol(pt)
        if pt.id() not in self._extra_scene_layers:
            self._extra_scene_layers.append(pt.id())   # so _open_view keeps it too
        canvas = self._valid_canvas()
        if canvas is not None:
            ms = canvas.mapSettings()
            cur = list(ms.layers())
            if pt.id() not in [l.id() for l in cur]:
                ms.setLayers([pt] + cur)
            where = "Added the epicentre point to the 3D scene"
        else:
            where = "Styled the epicentre point for 3D — open the 3D view to see it"
        if ok:
            self._log(where + " (terrain-clamped marker).")
        else:
            self._log(where + f", but the raised 3D marker style couldn't be applied "
                      f"({err}); the point still renders draped on the terrain.")

    def _apply_3d_point_symbol(self, lyr):
        """Give `lyr` a terrain-clamped sphere 3D symbol. Best-effort: the 3D
        symbol classes moved modules across QGIS versions, so every step is
        guarded and we return (ok, error) rather than raising."""
        try:
            try:
                from qgis._3d import QgsPoint3DSymbol, QgsPhongMaterialSettings
            except ImportError:
                from qgis.core import QgsPoint3DSymbol, QgsPhongMaterialSettings
            try:
                from qgis._3d import QgsVectorLayer3DRenderer
            except ImportError:
                from qgis.core import QgsVectorLayer3DRenderer
        except Exception as e:
            return False, f"3D API unavailable: {e}"
        try:
            from qgis.PyQt.QtGui import QColor
            sym = QgsPoint3DSymbol()
            self._set_altitude_terrain(sym)
            shape = getattr(QgsPoint3DSymbol, "Sphere", None)
            if shape is None:
                shape_enum = getattr(QgsPoint3DSymbol, "Shape", None)
                shape = getattr(shape_enum, "Sphere", None) if shape_enum else None
            if shape is not None:
                self._call_first(sym, ("setShape",), shape)
            props = dict(sym.shapeProperties()) if hasattr(sym, "shapeProperties") else {}
            props["radius"] = 60.0
            self._call_first(sym, ("setShapeProperties",), props)
            mat = QgsPhongMaterialSettings()
            try:
                mat.setDiffuse(QColor(230, 40, 40))
                mat.setAmbient(QColor(90, 10, 10))
            except Exception:
                pass
            self._call_first(sym, ("setMaterialSettings", "setMaterial"), mat)
            renderer = QgsVectorLayer3DRenderer(sym)
            lyr.setRenderer3D(renderer)
            return True, ""
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _set_altitude_terrain(sym):
        """Clamp a 3D symbol to the terrain surface across QGIS versions."""
        try:
            sym.setAltitudeClamping(Qgis.AltitudeClamping.Terrain)
            return
        except Exception:
            pass
        for modname in ("qgis.core", "qgis._3d"):
            try:
                mod = __import__(modname, fromlist=["Qgs3DTypes"])
                sym.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
                return
            except Exception:
                continue

    @staticmethod
    def _call_first(obj, method_names, *a):
        """Call the first existing method in `method_names`; True if one ran."""
        for name in method_names:
            fn = getattr(obj, name, None)
            if fn is not None:
                try:
                    fn(*a)
                    return True
                except Exception:
                    continue
        return False

    # ------------------------------------------- figure details ----
    def _pull_volume_details(self):
        """Copy area / centerline / volume from the Volume tab's last result.

        Reads only the volume tab's public-ish result dict (self._current);
        defensive so it degrades to a warning if the tab hasn't run or its
        shape changed. Every field stays user-editable afterwards."""
        vt = getattr(self.dock, "volume_tab", None)
        cur = getattr(vt, "_current", None) if vt is not None else None
        if not cur:
            self._warn("No Volume-tab result yet — run a measurement in the "
                       "'Volume from area' tab (and compute the centerline) first.")
            return

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        a = num(cur.get("a_conv")) or num(cur.get("a_total")) or num(cur.get("src_best"))
        vb, vl, vh = num(cur.get("v_best")), num(cur.get("v_low")), num(cur.get("v_high"))
        ln, dr = num(cur.get("length")), num(cur.get("drop"))
        if a is not None:
            self.det_area.setText(f"{a/1e6:.2f} km²" if a >= 1e5 else f"{a:.0f} m²")
        if ln is not None:
            self.det_cl_length.setText(f"{ln/1000:.2f} km" if ln >= 1000 else f"{ln:.0f} m")
        if dr is not None:
            self.det_cl_drop.setText(f"{dr:.0f} m")
        if vb is not None:
            s = self._fmt_vol(vb)
            if vl is not None and vh is not None:
                if abs(vb) >= 1e6 and abs(vl) >= 1e6 and abs(vh) >= 1e6:
                    s += f"  ({vl/1e6:.2f}–{vh/1e6:.2f} Mm³)"   # unit once, for legibility
                else:
                    s += f"  ({self._fmt_vol(vl)}–{self._fmt_vol(vh)})"
            self.det_volume.setText(s)
        got = [k for k, v in (("area", a), ("length", ln), ("drop", dr),
                              ("volume", vb)) if v is not None]
        if got:
            self._log("Pulled from Volume tab: " + ", ".join(got) + ".")
        else:
            self._warn("Volume tab has a result but no area/centerline/volume "
                       "numbers yet — measure + compute the centerline there.")

    @staticmethod
    def _fmt_vol(v):
        if v is None:
            return "?"
        return f"{v/1e6:.2f} Mm³" if abs(v) >= 1e6 else f"{v:,.0f} m³"

    @staticmethod
    def _terrain_sensor(cand):
        """A sensor label for the terrain source, for the figure Details."""
        s = str(cand.get("dem_source", ""))
        if s in ("arcticdem", "earthdem", "rema"):
            return "Maxar WorldView (stereo photogrammetry)"
        if s.startswith("3dep"):
            return "USGS 3DEP"
        return cand.get("source", "DEM")

    # -------------------------------------- instant-flip web viewer ----
    def _export_web_viewer(self):
        """Bake DEM + before/after imagery into a standalone WebGL viewer.

        Unlike QGIS's 3D view (which re-textures the terrain on every flip),
        the exported page uploads BOTH images to the GPU up front, so flipping
        before/after is a zero-load texture swap while you orbit freely."""
        from . import web3d_export
        if self._dem_layer is None:
            self._warn("Build or select a terrain DEM first (Terrain section).")
            return
        proj = QgsProject.instance()
        before = [proj.mapLayer(i) for i in self._checked_ids(self.before_combo)]
        after = [proj.mapLayer(i) for i in self._checked_ids(self.after_combo)]
        before = [l for l in before if l is not None]
        after = [l for l in after if l is not None]
        if not before or not after:
            self._warn("Tick at least one Before image and one After image.")
            return
        scene_crs, extent = self._scene_crs_and_extent(self._dem_layer)
        if not scene_crs.isValid() or extent.isEmpty():
            self._warn("Could not derive a projected scene CRS/extent from the DEM.")
            return
        # Clip the terrain to where the before/after imagery actually HAS pixels —
        # the intersection of their valid-data footprints (two scenes can share a
        # bounding box yet cover very different ground via nodata fill), clamped to
        # the DEM. Keeps a partial scene from padding the figure with a black void.
        extent = self._drape_crop_extent(scene_crs, before + after, extent)
        self._busy(True)
        self._log("Exporting instant-flip 3D web viewer (rendering imagery, "
                  "reading DEM)…")
        try:
            html = self._build_web_viewer(web3d_export, scene_crs, extent,
                                          before, after)
        except Exception as e:
            self._busy(False)
            self._log(f"Web viewer export failed: {e}")
            self._warn(f"Web viewer export failed: {e}")
            return
        self._busy(False)
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            self.dock.project_edit.text().strip(), "out", "interactive")
        out_dir = os.path.join(base_out, "viewer3d", "web")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self._warn(f"Cannot create output dir: {e}")
            return
        path = os.path.join(out_dir, "instant_flip_3d.html")
        try:
            with open(path, "w") as f:
                f.write(html)
        except OSError as e:
            self._warn(f"Could not write the viewer: {e}")
            return
        self._log(f"Wrote {path} ({round(len(html)/1024)} KB). Opening in browser…")
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.iface.messageBar().pushInfo(
            "3D viewer", "Opened the instant-flip 3D viewer in your browser — "
            "orbit freely; Space or the buttons flip before/after with no loading.")

    def _build_web_viewer(self, w3d, scene_crs, extent, before, after):
        """Render the two image sets + DEM to embedded assets; return the HTML."""
        from qgis.PyQt.QtCore import QSize, QByteArray, QBuffer, QIODevice
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsMapSettings, QgsMapRendererParallelJob

        ew, eh = extent.width(), extent.height()
        tex_max = 2048
        if ew >= eh:
            tw, th = tex_max, max(1, int(round(tex_max * eh / ew)))
        else:
            th, tw = tex_max, max(1, int(round(tex_max * ew / eh)))

        def render(layers):
            ms = QgsMapSettings()
            ms.setDestinationCrs(scene_crs)
            ms.setExtent(extent)
            ms.setOutputSize(QSize(tw, th))
            ms.setBackgroundColor(QColor(10, 12, 16))
            ms.setLayers(layers)          # index 0 draws on top
            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "PNG")
            buf.close()
            return "data:image/png;base64," + bytes(ba.toBase64()).decode("ascii")

        before_uri = render(before)
        after_uri = render(after)
        # 255×255 mesh keeps vertex count under 65536 → 16-bit indices, no
        # OES_element_index_uint dependency (the verified path).
        rows = self._dem_grid(scene_crs, extent, 255, 255)
        elev_b64, ncols, nrows, zmin, zmax = w3d.encode_heightfield(rows)
        label = lambda ls: " + ".join(l.name() for l in ls)
        cfg = {
            "title": "Landslide 3D — before / after",
            "before_label": label(before),
            "after_label": label(after),
            "ncols": ncols, "nrows": nrows,
            "width_m": float(ew), "height_m": float(eh),
            "zmin": zmin, "zmax": zmax,
            "exaggeration": float(self.vscale_spin.value()),
            "elev_b64": elev_b64,
            "before_uri": before_uri,
            "after_uri": after_uri,
            # provenance + georeferencing for the exported figure's axes/caption
            "crs": scene_crs.authid() or "",
            "utm": [extent.xMinimum(), extent.yMinimum(),
                    extent.xMaximum(), extent.yMaximum()],
            "before_date": self._sniff_date(label(before)) or "",
            "after_date": self._sniff_date(label(after)) or "",
        }
        polys, lines, points = self._collect_overlays(scene_crs, extent)
        cfg["polys"], cfg["lines"], cfg["points"] = polys, lines, points
        # extra Details rows for the figure (satellites, area, centerline, volume)
        det = []
        img = self.det_imagery_sat.currentText().strip()
        if img:
            det.append(["Imagery", img])
        for lbl, w in (("Terrain", self.det_terrain_sat), ("Area", self.det_area),
                       ("Centerline length", self.det_cl_length),
                       ("Vertical drop", self.det_cl_drop), ("Volume", self.det_volume)):
            v = w.text().strip()
            if lbl == "Volume" and v:      # normalize any older ×10⁶ m³ text to Mm³
                v = v.replace(" ×10⁶ m³", " Mm³").replace("×10⁶ m³", "Mm³")
            if v:
                det.append([lbl, v])
        cfg["extra_details"] = det
        if polys or lines or points:
            self._log(f"Overlays draped: {len(polys)} polygon, {len(lines)} line, "
                      f"{len(points)} point layer(s).")
        return w3d.build_viewer_html(cfg)

    def _overlay_extent(self, scene_crs):
        """Combined bounding box (scene CRS) of the ticked polygon/line overlays.

        Points (peaks) are skipped — they can be scattered across the whole AOI
        and would defeat the crop. Returns None if nothing usable is ticked, in
        which case the export falls back to the full DEM extent."""
        from qgis.core import QgsCoordinateTransform, QgsWkbTypes
        proj = QgsProject.instance()
        rect = None
        for lid in self._checked_ids(self.overlay_combo):
            lyr = proj.mapLayer(lid)
            if not isinstance(lyr, QgsVectorLayer):
                continue
            if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PointGeometry:
                continue
            ext = lyr.extent()
            if ext is None or ext.isEmpty():
                continue
            try:
                ext = QgsCoordinateTransform(
                    lyr.crs(), scene_crs, proj).transformBoundingBox(ext)
            except Exception:
                continue
            if rect is None:
                rect = QgsRectangle(ext)
            else:
                rect.combineExtentWith(ext)
        return rect

    def _imagery_extent(self, scene_crs, layers):
        """Combined bounding box (scene CRS) of the given raster layers.

        Used to clip the 3D terrain to the smallest area the before/after
        imagery actually covers, so the scene isn't padded out with DEM that
        has no drape on it. Returns None if no layer has a usable extent."""
        proj = QgsProject.instance()
        rect = None
        for lyr in layers:
            if lyr is None:
                continue
            ext = lyr.extent()
            if ext is None or ext.isEmpty():
                continue
            try:
                ext = QgsCoordinateTransform(
                    lyr.crs(), scene_crs, proj).transformBoundingBox(ext)
            except Exception:
                continue
            if rect is None:
                rect = QgsRectangle(ext)
            else:
                rect.combineExtentWith(ext)
        return rect

    def _valid_data_bbox(self, layer, scene_crs, max_dim=1024):
        """Bounding box (scene CRS) of a raster layer's VALID (non-nodata) pixels.

        Two drape images can share a file bounding box yet cover very different
        ground — a partial satellite scene is padded with nodata to the AOI grid —
        so layer.extent() over-reports coverage. Read a decimated validity mask
        (any band off its nodata, or != 0 when none is set), take the tight box of
        valid pixels, return it in the scene CRS. None if unreadable or all-nodata."""
        from osgeo import gdal
        try:
            ds = gdal.Open(layer.source())
        except Exception:
            ds = None
        if ds is None:
            return None
        W, H = ds.RasterXSize, ds.RasterYSize
        if W <= 0 or H <= 0:
            return None
        step = max(1, int(max(W, H) / float(max_dim)))
        ow, oh = max(1, W // step), max(1, H // step)
        valid = np.zeros((oh, ow), dtype=bool)
        for b in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(b)
            try:
                a = band.ReadAsArray(0, 0, W, H, ow, oh)   # decimated read
            except Exception:
                a = None
            if a is None:
                continue
            nod = band.GetNoDataValue()
            valid |= (a != nod) if nod is not None else (a != 0)
        gt = ds.GetGeoTransform()
        ds = None
        ys, xs = np.where(valid)
        if xs.size == 0:
            return None
        px0, px1 = int(xs.min()) * step, (int(xs.max()) + 1) * step
        py0, py1 = int(ys.min()) * step, (int(ys.max()) + 1) * step
        gxs, gys = [], []
        for px, py in ((px0, py0), (px1, py1)):
            gxs.append(gt[0] + px * gt[1] + py * gt[2])
            gys.append(gt[3] + px * gt[4] + py * gt[5])
        rect = QgsRectangle(min(gxs), min(gys), max(gxs), max(gys))
        if layer.crs() != scene_crs:
            try:
                rect = QgsCoordinateTransform(
                    layer.crs(), scene_crs,
                    QgsProject.instance()).transformBoundingBox(rect)
            except Exception:
                return None
        return rect

    def _drape_crop_extent(self, scene_crs, layers, dem_extent):
        """Extent to clip the 3D scene to: the INTERSECTION of the drape layers'
        valid-data footprints, clamped to the DEM — so the scene is cropped to the
        SMALLER area a partial before/after scene actually covers, not padded out
        with the black void of its nodata fill. Falls back to the union of the full
        layer extents, then the DEM, when nodata can't be read."""
        layers = [l for l in layers if l is not None]
        inter = None
        for lyr in layers:
            vb = self._valid_data_bbox(lyr, scene_crs)
            if vb is None or vb.isEmpty():
                continue
            inter = QgsRectangle(vb) if inter is None else inter.intersect(vb)
            if inter is None or inter.isEmpty():
                break
        if inter is None or inter.isEmpty():
            inter = self._imagery_extent(scene_crs, layers)     # union of full extents
        if inter is None or inter.isEmpty():
            return dem_extent
        r = inter.intersect(dem_extent)
        return r if (r is not None and not r.isEmpty()) else dem_extent

    def _collect_overlays(self, scene_crs, extent):
        """Ticked vector layers → (polys, lines, points) in mesh-local metres.

        Each feature is transformed to the scene CRS, offset to the mesh centre
        (so it lines up with the exported terrain), and polygon rings are
        triangulated for the draped fill. Points become peak markers, lines
        become draped polylines."""
        from . import web3d_export
        from qgis.core import (QgsCoordinateTransform, QgsWkbTypes, QgsExpression,
                               QgsExpressionContext, QgsExpressionContextUtils)
        proj = QgsProject.instance()
        cx, cy = extent.center().x(), extent.center().y()
        # densify/subdivide target (scene metres) so draped polygons CONFORM to
        # the terrain instead of spanning flat sheets between boundary vertices.
        step = max(extent.width(), extent.height()) / 200.0
        polys, lines, points = [], [], []
        for i, lid in enumerate(self._checked_ids(self.overlay_combo)):
            lyr = proj.mapLayer(lid)
            if not isinstance(lyr, QgsVectorLayer):
                continue
            color = self._overlay_color(lyr, i)
            name = lyr.name()
            try:
                xform = QgsCoordinateTransform(lyr.crs(), scene_crs, proj)
            except Exception:
                xform = None
            gtype = QgsWkbTypes.geometryType(lyr.wkbType())
            # label expression for points (peaks): the layer's display field/expr
            lexpr = lctx = None
            if gtype == QgsWkbTypes.PointGeometry and lyr.displayExpression():
                lexpr = QgsExpression(lyr.displayExpression())
                lctx = QgsExpressionContext(
                    QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
            rings, paths, coords, labels = [], [], [], []
            for feat in lyr.getFeatures():
                g = feat.geometry()
                if g is None or g.isEmpty():
                    continue
                if xform is not None:
                    g = QgsGeometry(g)
                    try:
                        g.transform(xform)
                    except Exception:
                        continue
                if gtype == QgsWkbTypes.PolygonGeometry:
                    mps = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
                    for poly in mps:
                        if not poly:
                            continue
                        ring = [[p.x() - cx, p.y() - cy] for p in poly[0]]
                        if len(ring) >= 3:
                            rings.append({
                                "outline": web3d_export.densify_ring(ring, step),
                                "tris": web3d_export.subdivide_tris(
                                    web3d_export.triangulate_ring(ring), step)})
                elif gtype == QgsWkbTypes.LineGeometry:
                    mls = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
                    for ln in mls:
                        path = [[p.x() - cx, p.y() - cy] for p in ln]
                        if len(path) >= 2:
                            paths.append(path)
                elif gtype == QgsWkbTypes.PointGeometry:
                    pts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
                    lbl = self._feature_label(lexpr, lctx, feat)
                    for p in pts:
                        # only keep peaks that fall within the terrain (hillshade)
                        if not extent.contains(QgsPointXY(p.x(), p.y())):
                            continue
                        coords.append([p.x() - cx, p.y() - cy])
                        labels.append(lbl)
            try:
                rtype = type(lyr.renderer()).__name__
            except Exception:
                rtype = "?"
            if rings:
                has_fill, frgb, lrgb, falpha = self._poly_style(lyr, color)
                polys.append({"name": name, "color": frgb, "line_color": lrgb,
                              "fill": has_fill, "fill_alpha": falpha, "rings": rings})
                self._log(f"overlay '{name}' [{rtype}] polygon: outline rgb={lrgb} "
                          f"fill={'on '+str(frgb)+f' α{falpha:.2f}' if has_fill else 'off'}")
                self._log(f"    symbol: {self._describe_symbol(lyr)}")
            if paths:
                lines.append({"name": name, "color": color, "paths": paths})
                self._log(f"overlay '{name}' [{rtype}] line: rgb={color}")
                self._log(f"    symbol: {self._describe_symbol(lyr)}")
            if coords:
                points.append({"name": name, "color": color,
                               "coords": coords, "labels": labels})
                self._log(f"overlay '{name}' [{rtype}] point×{len(coords)}: rgb={color}")
        return polys, lines, points

    @staticmethod
    def _feature_label(expr, ctx, feat):
        """Evaluate a layer's display expression for one feature; '' on failure."""
        if expr is None:
            return ""
        try:
            ctx.setFeature(feat)
            v = expr.evaluate(ctx)
            return "" if v is None else str(v)
        except Exception:
            return ""

    @staticmethod
    def _layer_symbol(lyr):
        """A representative QgsSymbol for the layer across renderer types — single,
        categorized, graduated, rule-based — so colour extraction isn't limited to
        single-symbol renderers (which silently fell back to the palette before).
        None if it can't be resolved."""
        try:
            r = lyr.renderer()
        except Exception:
            return None
        if r is None:
            return None
        try:
            s = r.symbol()                       # single-symbol renderer
            if s is not None:
                return s
        except Exception:
            pass
        try:                                     # categorized / graduated / rule-based
            from qgis.core import QgsRenderContext
            syms = r.symbols(QgsRenderContext())
            if syms:
                return syms[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _rgb_of(color):
        return [color.red(), color.green(), color.blue()]

    @staticmethod
    def _sat(rgb):
        """HSV-ish saturation 0..1 of an [r,g,b] — 0 for grey/black/white."""
        mx = max(rgb)
        return 0.0 if mx == 0 else (mx - min(rgb)) / float(mx)

    @staticmethod
    def _symbol_colors(sym):
        """Every meaningful colour a symbol's layers draw, skipping transparent ones.

        Type-aware: a line layer's only real colour is color() — its fillColor()/
        strokeColor() are bogus (0,0,0) — while a fill layer's are stroke/fill, and a
        marker's are color/fill/stroke. Trusting the wrong accessor is what made line-
        styled overlays read as black."""
        cols = []
        try:
            cols.append(Viewer3DTab._rgb_of(sym.color()))
        except Exception:
            pass
        try:
            for k in range(sym.symbolLayerCount()):
                sl = sym.symbolLayer(k)
                try:
                    lt = sl.layerType()
                except Exception:
                    lt = ""
                if "Line" in lt:
                    attrs = ("color",)
                elif "Fill" in lt:
                    attrs = ("strokeColor", "fillColor")
                else:
                    attrs = ("color", "fillColor", "strokeColor")
                for attr in attrs:
                    try:
                        c = getattr(sl, attr)()
                        if c.alpha() > 0:
                            cols.append([c.red(), c.green(), c.blue()])
                    except Exception:
                        pass
        except Exception:
            pass
        return cols

    @staticmethod
    def _overlay_color(lyr, i):
        """The layer's VISIBLE colour, else a palette colour.

        symbol.color() alone is unreliable — a multi-layer line symbol reports its
        first layer (often a dark casing), so a yellow centreline came back dark.
        Instead pick the most-saturated colour the symbol actually draws (a vivid
        line/marker beats a grey casing; brightness breaks ties so white markers
        still resolve to white)."""
        pal = [[255, 70, 70], [255, 220, 60], [90, 200, 255],
               [130, 230, 130], [220, 130, 230], [255, 150, 60]]
        s = Viewer3DTab._layer_symbol(lyr)
        if s is not None:
            cols = Viewer3DTab._symbol_colors(s)
            if cols:
                cols.sort(key=lambda c: (Viewer3DTab._sat(c), sum(c)))
                return cols[-1]
        return pal[i % len(pal)]

    @staticmethod
    def _describe_symbol(lyr):
        """Dump a layer's symbol structure for the diagnostic log — symbol colour
        plus each symbol layer's class and its color/fill/stroke/brush, so a wrong
        overlay colour can be traced to the exact symbol without QGIS access here."""
        sym = Viewer3DTab._layer_symbol(lyr)
        if sym is None:
            return "no symbol"
        parts = []
        try:
            parts.append("sym.color=" + str(Viewer3DTab._rgb_of(sym.color())))
        except Exception:
            pass
        try:
            for k in range(sym.symbolLayerCount()):
                sl = sym.symbolLayer(k)
                d = type(sl).__name__
                for attr in ("fillColor", "strokeColor", "color"):
                    try:
                        d += " %s=%s" % (attr, Viewer3DTab._rgb_of(getattr(sl, attr)()))
                    except Exception:
                        pass
                for attr in ("brushStyle", "strokeStyle"):
                    try:
                        d += " %s=%d" % (attr, int(getattr(sl, attr)()))
                    except Exception:
                        pass
                parts.append(d)
        except Exception:
            pass
        return " | ".join(parts)

    @staticmethod
    def _poly_style(lyr, fallback_rgb):
        """Outline colour of a polygon layer's symbol (drawn OUTLINE-ONLY).

        Routing on the symbol-layer TYPE is essential: a landslide 'polygon' is often
        styled as a plain LINE symbol used as the ring (QgsSimpleLineSymbolLayer),
        whose visible colour is color() — but which ALSO answers fillColor()/
        strokeColor() with a bogus (0,0,0). Reading those made the rings export black.
        So: for a fill-type layer the ring is its strokeColor(); for a line/marker
        layer the ring is its own color(). Returns (False, fill_rgb, line_rgb, 0.0) —
        outline-only per preference, so no translucent fill is drawn."""
        from qgis.PyQt.QtCore import Qt
        line_rgb = list(fallback_rgb)
        sym = Viewer3DTab._layer_symbol(lyr)
        if sym is None:
            return False, list(fallback_rgb), line_rgb, 0.0
        try:
            sls = [sym.symbolLayer(k) for k in range(sym.symbolLayerCount())]
        except Exception:
            sls = []
        outline = []                                    # candidate ring colours
        for sl in sls:
            try:
                lt = sl.layerType()
            except Exception:
                lt = ""
            if "Fill" in lt:                            # fill symbol -> the ring is its STROKE
                try:
                    pen_ok = int(sl.strokeStyle()) != int(Qt.NoPen)
                except Exception:
                    pen_ok = True
                try:
                    sc = sl.strokeColor()
                    if pen_ok and sc.alpha() > 0:
                        outline.append([sc.red(), sc.green(), sc.blue()])
                except Exception:
                    pass
            else:                                       # line/marker symbol -> its own colour is the ring
                try:
                    c = sl.color()
                    if c.alpha() > 0:
                        outline.append([c.red(), c.green(), c.blue()])
                except Exception:
                    pass
        if outline:
            outline.sort(key=lambda c: (Viewer3DTab._sat(c), sum(c)))
            line_rgb = outline[-1]                       # most-saturated visible ring colour
        else:
            try:
                line_rgb = Viewer3DTab._rgb_of(sym.color())
            except Exception:
                pass
        return False, line_rgb, line_rgb, 0.0            # outline-only: no fill

    def _dem_grid(self, scene_crs, extent, ncols, nrows):
        """Warp the DEM to an ncols×nrows grid over `extent` (north-first rows).

        Returns a list of rows with nodata cells as None."""
        from osgeo import gdal
        dst = scene_crs.authid() or scene_crs.toWkt()
        ds = gdal.Warp(
            "", self._dem_layer.source(), format="MEM", dstSRS=dst,
            outputBounds=(extent.xMinimum(), extent.yMinimum(),
                          extent.xMaximum(), extent.yMaximum()),
            width=ncols, height=nrows, resampleAlg="bilinear")
        if ds is None:
            raise RuntimeError("DEM warp for the web viewer produced nothing.")
        band = ds.GetRasterBand(1)
        nod = band.GetNoDataValue()
        arr = np.array(band.ReadAsArray(), dtype="float64")
        ds = None
        if nod is not None:
            arr = np.where(arr == nod, np.nan, arr)
        rows = []
        for j in range(arr.shape[0]):
            rows.append([None if v != v else float(v) for v in arr[j]])
        return rows

    # -------------------------------------------------------- teardown ----
    def teardown(self):
        for t in (self.task, self._warp_task):
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
        self.task = self._warp_task = None
