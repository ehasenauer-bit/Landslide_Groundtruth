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
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
)
from qgis.gui import QgsCollapsibleGroupBox, QgsCheckableComboBox

from . import dem_diff
from .task import PipelineTask

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

        self._dem_layer = None           # QgsRasterLayer used as terrain
        self._dem_mean_z = None          # mean AOI elevation, for the camera
        self._hillshade_layer = None     # optional draped multidirectional hillshade
        self._canvas3d = None            # the native 3D canvas we created
        self._scene_extent = None        # last scene extent (scene CRS), for Reset
        self._flip_cache = {}            # imagery layer id -> aligned cache layer id
        self._extra_scene_layers = []    # non-drape layer ids kept in the scene (point)

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
        self.loaded_dem_row_label = QLabel("Loaded DEM")
        tform.addRow(self.loaded_dem_row_label, self.loaded_combo)

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
        self.zfactor_spin.setPrefix("z ")
        hs_row.addWidget(self.hillshade_check, 1)
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

        # --- actions -------------------------------------------------------
        btn_row = QHBoxLayout()
        self.open_btn = QPushButton("Open / update 3D view")
        self.open_btn.clicked.connect(self._open_view)
        self.open_btn.setEnabled(False)
        self.reset_btn = QPushButton("Reset camera")
        self.reset_btn.clicked.connect(self._reset_camera)
        self.close_btn = QPushButton("Close 3D view")
        self.close_btn.clicked.connect(self._close_view)
        btn_row.addWidget(self.open_btn, 2)
        btn_row.addWidget(self.reset_btn, 1)
        btn_row.addWidget(self.close_btn, 1)
        root.addLayout(btn_row)

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
        for w in (self.loaded_combo, self.loaded_dem_row_label):
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
        self._refresh_before_after()

    def _checked_drape_ids(self):
        ids = []
        for i in range(self.drape_list.count()):
            it = self.drape_list.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        return ids

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
        # merge pre + post candidates; prefer strips that actually cover the event
        cands = list(result.get("pre", [])) + list(result.get("post", []))
        cands = [c for c in cands if c.get("dem_url")]
        if not cands:
            self._log("No ArcticDEM strips found over this AOI. Try a larger radius, "
                      "or load a DEM manually and use 'Use a DEM layer'.")
            self._warn("No ArcticDEM strips found over the AOI.")
            return
        cands = self._rank_strips(cands)
        self.strip_combo.blockSignals(True)
        self.strip_combo.clear()
        for c in cands:
            date = (c.get("date") or "?")[:10]
            cov = "covers event" if c.get("_covers") else "overlaps AOI"
            self.strip_combo.addItem(f"{date}  ·  {cov}", c)
        self.strip_combo.setEnabled(True)
        self.strip_combo.blockSignals(False)
        self._log(f"Found {len(cands)} ArcticDEM strip(s). Warping the top one; "
                  f"switch strips with the dropdown if it has gaps over the AOI.")
        self._warp_selected_strip()

    def _rank_strips(self, cands):
        """Best-first: strips that cover the event point, then newest acquisition
        first (the most current terrain surface)."""
        result = self._search_result or {}
        try:
            lat, lon = float(result.get("lat")), float(result.get("lon"))
        except (TypeError, ValueError):
            lat = lon = None
        for c in cands:
            c["_covers"] = self._covers_point(c, lat, lon)
        # stable sort: newest date first, then bring covering strips to the front
        cands.sort(key=lambda c: (c.get("date") or ""), reverse=True)
        cands.sort(key=lambda c: 0 if c["_covers"] else 1)
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

    def _on_strip_changed(self):
        if self.strip_combo.isEnabled() and self.strip_combo.currentData():
            self._warp_selected_strip()

    def _warp_selected_strip(self):
        cand = self.strip_combo.currentData()
        if not cand:
            return
        aoi = self._aoi()
        if aoi is None:
            return
        lat, lon, radius = aoi
        res = self.res_combo.currentData()
        epsg = dem_diff.utm_epsg(lat, lon)
        dlat = radius / 111.32
        dlon = radius / (111.32 * math.cos(math.radians(lat)))
        bounds = dem_diff.utm_bounds(lon - dlon, lat - dlat, lon + dlon, lat + dlat,
                                     epsg, res)
        base_out = self.dock.out_edit.text().strip() or os.path.join(
            self.dock.project_edit.text().strip(), "out", "interactive")
        out_dir = os.path.join(base_out, "viewer3d")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"terrain_{epsg}_{res}m.tif")
        url = cand["dem_url"]
        self._gen += 1
        gen = self._gen
        self._busy(True)
        self._log(f"Warping strip to {res} m over the AOI (EPSG:{epsg})…")
        self._warp_task = QgsTask.fromFunction(
            "Warp ArcticDEM strip", self._warp_worker,
            on_finished=lambda exc, res_: self._on_warp_done(exc, res_, gen, out_path),
            url=url, bounds=bounds, epsg=epsg, res=res, out_path=out_path)
        QgsApplication.taskManager().addTask(self._warp_task)

    @staticmethod
    def _warp_worker(task, url, bounds, epsg, res, out_path):
        """Runs off the GUI thread: /vsicurl ranged read + warp to the AOI grid.
        Touches only GDAL/numpy (dem_diff), never Qt."""
        arr, gt, proj = dem_diff.warp(url, bounds, epsg, res)
        valid = dem_diff.valid_heights(arr)
        cover = float(valid.mean()) if valid.size else 0.0
        if cover <= 0.0:
            return {"error": "no valid elevation pixels over the AOI"}
        zmean = float(np.nanmean(arr[valid]))
        dem_diff.write_gtiff(out_path, np.where(valid, arr, np.nan), gt, proj)
        return {"path": out_path, "zmean": zmean, "cover": cover}

    def _on_warp_done(self, exc, result, gen, out_path):
        if gen != self._gen:
            return                       # a newer fetch superseded this one
        self._busy(False)
        self._warp_task = None
        if exc is not None:
            self._log(f"Warp failed: {exc}")
            self._warn("ArcticDEM warp failed — see the log.")
            return
        if not result or result.get("error"):
            msg = (result or {}).get("error", "unknown error")
            self._log(f"Warp produced no terrain: {msg}. Try another strip or a "
                      f"larger radius.")
            return
        cover = result["cover"]
        self._dem_mean_z = result["zmean"]
        self._set_terrain_from_file(result["path"],
                                    f"ArcticDEM terrain ({cover*100:.0f}% AOI cover)")
        if cover < 0.6:
            self._log(f"Note: this strip covers only {cover*100:.0f}% of the AOI — "
                      f"pick another strip in the dropdown if the terrain has holes.")

    def _set_terrain_from_file(self, path, name):
        lyr = QgsRasterLayer(path, name)
        if not lyr.isValid():
            self._warn(f"Could not load terrain raster:\n{path}")
            return
        QgsProject.instance().addMapLayer(lyr)
        self._install_terrain_layer(lyr)

    def _use_loaded_dem(self):
        lid = self.loaded_combo.currentData()
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is None or not isinstance(lyr, QgsRasterLayer):
            self._warn("Pick a single-band DEM raster loaded in the project.")
            return
        self._dem_mean_z = self._sample_center_z(lyr)
        self._install_terrain_layer(lyr)

    def _install_terrain_layer(self, lyr):
        """Adopt `lyr` as the terrain DEM, (re)build the hillshade, refresh lists."""
        self._dem_layer = lyr
        self.terrain_label.setText(f"Terrain: {lyr.name()}")
        self.open_btn.setEnabled(True)
        self.web_btn.setEnabled(True)
        self._rebuild_hillshade()
        self._refresh_drape_list()
        self._log(f"Terrain ready: {lyr.name()}. Tick drape layers, then "
                  f"'Open / update 3D view'.")

    def _rebuild_hillshade(self):
        """Add/refresh a multidirectional hillshade of the terrain DEM as a drapeable
        layer. Uses a second QgsRasterLayer on the same file with a hillshade
        renderer — no extra file written."""
        # drop a stale hillshade
        if self._hillshade_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(self._hillshade_layer.id())
            except Exception:
                pass
            self._hillshade_layer = None
        if not self.hillshade_check.isChecked() or self._dem_layer is None:
            return
        src = self._dem_layer.source()
        hs = QgsRasterLayer(src, "Hillshade (multidirectional)")
        if not hs.isValid():
            return
        renderer = QgsHillshadeRenderer(hs.dataProvider(), 1, 315.0, 45.0)
        renderer.setMultiDirectional(True)
        renderer.setZFactor(self.zfactor_spin.value())
        hs.setRenderer(renderer)
        QgsProject.instance().addMapLayer(hs)
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

    def _candidate_rasters(self):
        """Project rasters selectable as before/after images.

        Excludes our own '(3D cache)' copies and the current terrain DEM /
        hillshade, so the pickers list imagery, not the surface it drapes on."""
        skip = set()
        if self._dem_layer is not None:
            skip.add(self._dem_layer.id())
        if self._hillshade_layer is not None:
            skip.add(self._hillshade_layer.id())
        return [l for l in self._project_rasters()
                if not l.name().endswith("(3D cache)") and l.id() not in skip]

    def _refresh_before_after(self):
        """(Re)populate the Before/After pickers, preserving the user's ticks.

        On the first populate, default the ticks to an auto-detected pre/post
        pair (…_pre_/…_post_ or PlanetScope before/after) so the common case
        needs no picking; the user can tick any other layer(s), one or more."""
        rasters = self._candidate_rasters()
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
        }
        return w3d.build_viewer_html(cfg)

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
