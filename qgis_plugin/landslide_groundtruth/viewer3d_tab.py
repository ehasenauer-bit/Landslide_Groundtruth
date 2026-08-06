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

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDoubleSpinBox, QDateTimeEdit, QCheckBox,
    QProgressBar, QPlainTextEdit, QListWidget, QListWidgetItem, QSpinBox,
)
from qgis.core import (
    QgsProject, QgsApplication, QgsRasterLayer, QgsTask, QgsVector3D,
    QgsHillshadeRenderer, QgsGeometry, QgsPointXY, Qgis,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
)
from qgis.gui import QgsCollapsibleGroupBox

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
            for w in (self.fetch_btn, self.open_btn, self.reset_btn, self.close_btn):
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
        ms.setLayers(drape)

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

    # -------------------------------------------------------- teardown ----
    def teardown(self):
        for t in (self.task, self._warp_task):
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
        self.task = self._warp_task = None
