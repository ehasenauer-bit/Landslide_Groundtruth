"""MOSART (SAR elevation change) — PARKED, not wired into the plugin.

Removed from `qgis_plugin/landslide_groundtruth/volume_tab.py` because the
method is not ready for research use: MOSART's coregistration is ISCE2 /
hyp3-isce2, which is Linux-only and cannot run inside QGIS on this machine, so
the plugin's half of it was never more than a job-spec writer plus a folder
watcher pointed at a Colab notebook that a user still had to babysit. Shipping
it in the dock advertised a capability the plugin does not have.

This file is DELIBERATELY not imported by anything. It lives outside
`qgis_plugin/` so the plugin never loads it and `tests/run_all.sh` never sweeps
it — park it here, commit it once, and it stops moving. Reinstating it is
described in README.md next to this file.

Everything below is the code as it was removed, verbatim, re-homed onto a mixin
so it stays syntactically whole and can be diffed against a future rewrite. The
imports at the top are the ones `volume_tab.py` was carrying on its behalf; they
were dropped from that module when this came out.
"""

import json
import os
import re
import webbrowser
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QTimer, QFileSystemWatcher
from qgis.PyQt.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsProject, QgsRasterLayer,
)
from qgis.gui import QgsCollapsibleGroupBox

from qgis_plugin.landslide_groundtruth import layer_group as lg  # noqa: F401


class MosartMixin:
    """Mix into `VolumeTab` to restore the feature (see README.md).

    Expects from the host tab: `self.dock`, `self.canvas`, `self.name_edit`,
    `self._ddem_outline()`, `self._notify()`, `self._append_log()`,
    `self._refresh_layers()`, `self._project_rasters()`.
    """

    def _init_mosart(self):
        """Called from `VolumeTab.__init__`, after `_build_ui()`.

        Auto-import of MOSART Δh products dropped into <project dir>/mosart/.
        The dock builds project_edit AFTER this tab, so defer the wiring that
        reads it to the next event-loop turn (the pattern project_state uses).
        """
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_mosart_dir_changed)
        self._mosart_debounce = QTimer(self)
        self._mosart_debounce.setSingleShot(True)
        self._mosart_debounce.timeout.connect(self._do_mosart_autoimport)
        QTimer.singleShot(0, self._wire_mosart_watch)

    def _build_mosart_box(self):
        """Prepare an external MOSART (SAR elevation-change) run.

        MOSART's coregistration is ISCE2/hyp3-isce2 — Linux-only, so it cannot
        run in QGIS on this machine. This box does the QGIS-side half: it writes
        a job spec (the AOI from your outline, the event date and search window,
        the orbit and reference DEM) into <project dir>/mosart/, and opens the
        Colab notebook that reads it, runs MOSART on a Linux VM, and writes a Δh
        GeoTIFF back into mosart/ — which then auto-imports here for the ∫Δh fit.

        NOT turnkey: the notebook still needs you to confirm the bursts asf_search
        finds and to tune the speckle/water masking (its markdown cells say how)."""
        box = QgsCollapsibleGroupBox(
            "Run MOSART (SAR elevation change · external)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        v = QVBoxLayout(box)

        note = QLabel(
            "MOSART reconstructs elevation change from Sentinel-1 amplitude. Its "
            "coregistration (ISCE2) is Linux-only, so it runs in Google Colab, "
            "not QGIS. This writes a job (AOI + dates + DEM) into mosart/ and "
            "opens the notebook; you run it (needs a free NASA Earthdata login "
            "for the SLC download — the GLO-30 DEM is fetched automatically); the "
            "Δh it writes back to mosart/ auto-imports here. Research method — "
            "validated on volcanoes, not yet on landslides.")
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(note)

        form = QFormLayout()
        self.mosart_event_edit = QLineEdit()
        self.mosart_event_edit.setPlaceholderText("YYYY-MM-DD")
        self.mosart_event_edit.setToolTip(
            "The event date. MOSART searches Sentinel-1 acquisitions in a window "
            "before and after it (below) to build the reference and see the "
            "change.")
        form.addRow("Event date", self.mosart_event_edit)

        self.mosart_pre_days = QLineEdit("60")
        self.mosart_pre_days.setToolTip(
            "How many days BEFORE the event to search for Sentinel-1 scenes. "
            "MOSART needs several pre-event dates to establish the reference "
            "surface, so give it room (≥ ~36 d spans a few 12-day repeats).")
        form.addRow("Pre-event window (days)", self.mosart_pre_days)

        self.mosart_post_days = QLineEdit("30")
        self.mosart_post_days.setToolTip(
            "How many days AFTER the event to search. A few post-event dates let "
            "the change settle out of speckle.")
        form.addRow("Post-event window (days)", self.mosart_post_days)

        self.mosart_orbit_combo = QComboBox()
        for label in ("Either", "Ascending", "Descending"):
            self.mosart_orbit_combo.addItem(label)
        self.mosart_orbit_combo.setToolTip(
            "Restrict to one orbit direction, or leave Either and pick the track "
            "with best coverage in the notebook. All dates in one MOSART run must "
            "share a track/burst, so you'll settle on one there anyway.")
        form.addRow("Orbit direction", self.mosart_orbit_combo)

        self.mosart_dem_combo = QComboBox()
        self.mosart_dem_combo.addItem("Copernicus GLO-30 (auto, free)", "glo_30")
        self.mosart_dem_combo.setToolTip(
            "Reference DEM MOSART anchors on. GLO-30 (30 m) is downloaded "
            "automatically and needs no account — start here. Pushing your "
            "high-res lidar DSM as the anchor is a later refinement (a "
            "datum-sensitive conversion), not wired in yet.")
        form.addRow("Reference DEM", self.mosart_dem_combo)

        self.mosart_notebook_edit = QLineEdit(
            "https://colab.research.google.com/github/ehasenauer-bit/"
            "Landslide_Groundtruth/blob/main/mosart_colab/Run_MOSART.ipynb")
        self.mosart_notebook_edit.setToolTip(
            "The Colab notebook the button opens. Defaults to the copy in your "
            "GitHub repo — change it to wherever you actually push the notebook "
            "(it must be pushed for the link to resolve).")
        form.addRow("Colab notebook", self.mosart_notebook_edit)
        v.addLayout(form)

        row = QHBoxLayout()
        self.mosart_job_btn = QPushButton("Prepare MOSART job + open Colab")
        self.mosart_job_btn.setToolTip(
            "Write <name>_job.json (AOI from the total/source outline, the dates, "
            "orbit and DEM) into this project's mosart/ folder, and open the "
            "Colab notebook. The notebook reads the job from your Drive, runs "
            "MOSART, and writes the Δh back to mosart/ — which auto-imports here.")
        self.mosart_job_btn.clicked.connect(self._prepare_mosart_job)
        row.addWidget(self.mosart_job_btn)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _prepare_mosart_job(self):
        """Write the MOSART job spec into mosart/ and open the Colab notebook."""
        event = self.mosart_event_edit.text().strip()
        if not event:
            self._notify(
                "Set the event date (YYYY-MM-DD) so MOSART knows which "
                "Sentinel-1 acquisitions to search around.", Qgis.Warning)
            return
        proj = self._project_dir()
        if not proj or not os.path.isdir(proj):
            self._notify(
                "Set a valid project dir in the dock first — the job and the "
                "returned Δh live in <project dir>/mosart/.", Qgis.Warning)
            return

        # AOI in lon/lat (EPSG:4326): prefer the assigned outline; fall back to
        # the current map view, so MOSART can be run BEFORE an outline exists
        # (you often run it to find where the change is, then digitise).
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        outline_feat, outline_layer, conv_role = self._ddem_outline()
        aoi_wkt = None
        try:
            if outline_feat is not None:
                geom = QgsGeometry(outline_feat.geometry())
                if outline_layer.crs() != wgs:
                    xform = QgsCoordinateTransform(
                        outline_layer.crs(), wgs, QgsProject.instance())
                    if geom.transform(xform) != 0:
                        self._notify(
                            "Could not reproject the outline to lon/lat.",
                            Qgis.Warning)
                        return
                bb = geom.boundingBox()
                aoi_wkt = geom.asWkt()
                aoi_src = f"outline “{outline_layer.name()}” ({conv_role})"
            else:
                bb = self.canvas.extent()
                canvas_crs = self.canvas.mapSettings().destinationCrs()
                if canvas_crs != wgs:
                    bb = QgsCoordinateTransform(
                        canvas_crs, wgs, QgsProject.instance()
                    ).transformBoundingBox(bb)
                aoi_src = "the current map view (no outline assigned)"
            aoi = [round(bb.xMinimum(), 6), round(bb.yMinimum(), 6),
                   round(bb.xMaximum(), 6), round(bb.yMaximum(), 6)]
            if aoi[0] == aoi[2] or aoi[1] == aoi[3]:
                self._notify(
                    "The AOI is empty — assign an outline, or zoom the map to the "
                    "event area, then try again.", Qgis.Warning)
                return
        except Exception as e:
            self._notify(f"Could not build the AOI: {e}", Qgis.Warning)
            return
        if aoi_wkt is None:
            aoi_wkt = (f"POLYGON (({aoi[0]} {aoi[1]}, {aoi[0]} {aoi[3]}, "
                       f"{aoi[2]} {aoi[3]}, {aoi[2]} {aoi[1]}, "
                       f"{aoi[0]} {aoi[1]}))")

        def _days(edit, default):
            try:
                return max(0, int(float(edit.text().strip())))
            except (ValueError, AttributeError):
                return default
        name = self.name_edit.text().strip() or "slide"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "slide"
        job = {
            "name": name,
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_date": event,
            "pre_window_days": _days(self.mosart_pre_days, 60),
            "post_window_days": _days(self.mosart_post_days, 30),
            "orbit_direction": self.mosart_orbit_combo.currentText().lower(),
            "polarization": "VV",
            "aoi_lonlat_bbox": aoi,
            "aoi_wkt_4326": aoi_wkt,
            "aoi_source": aoi_src,
            "reference_dem": self.mosart_dem_combo.currentData() or "glo_30",
            "output_dh_geotiff": f"{safe}_mosart_dh.tif",
            "notes": ("Output must be a georeferenced Δh GeoTIFF in METRES "
                      "(post − pre), written back into this mosart/ folder."),
        }
        mdir = os.path.join(proj, "mosart")
        try:
            os.makedirs(mdir, exist_ok=True)
        except OSError as e:
            self._notify(f"Could not create {mdir}: {e}", Qgis.Critical)
            return
        job_path = os.path.join(mdir, f"{safe}_job.json")
        try:
            with open(job_path, "w", encoding="utf-8") as fh:
                json.dump(job, fh, indent=2)
        except OSError as e:
            self._notify(f"Could not write {job_path}: {e}", Qgis.Critical)
            return
        self._setup_mosart_watch()

        self._notify(
            f"Wrote MOSART job → {job_path}", Qgis.Success)
        self._append_log(
            f"  AOI lon/lat: {aoi[0]},{aoi[1]} .. {aoi[2]},{aoi[3]}  from {aoi_src}\n"
            f"  event {event}, window −{job['pre_window_days']}/"
            f"+{job['post_window_days']} d, orbit {job['orbit_direction']}, "
            f"DEM {job['reference_dem']}.\n"
            "In Colab: mount Drive, open this project's mosart/ folder, run the "
            "notebook (a free NASA Earthdata login is needed for the SLC "
            f"download). It writes “{job['output_dh_geotiff']}” back to mosart/, "
            "which auto-imports here for the ∫Δh fit.")

        url = self.mosart_notebook_edit.text().strip()
        if url:
            try:
                webbrowser.open(url)
                self._append_log(f"Opened the Colab notebook: {url}")
            except Exception as e:
                self._append_log(
                    f"Could not open a browser ({e}); open it manually: {url}")

    # ---------- MOSART folder: organise + auto-import ----------
    def _project_dir(self):
        """The dock's project dir, or '' — read defensively because the dock
        builds project_edit AFTER this tab (see __init__)."""
        pe = getattr(self.dock, "project_edit", None)
        return pe.text().strip() if pe is not None else ""

    def _refresh_and_import(self):
        """The ↻ Refresh button: import any new mosart/ rasters, then re-sync the
        layer pickers — so a file that appeared on disk (a MOSART product, or
        anything you added) shows up without reloading the plugin."""
        self._import_mosart_layers(announce=True)
        self._refresh_layers()

    def _wire_mosart_watch(self):
        """Deferred one-shot (project_edit exists by now): follow the project-dir
        field and start watching its mosart/ folder, importing what's already
        there."""
        pe = getattr(self.dock, "project_edit", None)
        if pe is not None:
            pe.textChanged.connect(self._on_project_dir_changed)
        self._setup_mosart_watch()
        self._import_mosart_layers(announce=False)

    def _on_project_dir_changed(self, *_args):
        self._setup_mosart_watch()

    def _setup_mosart_watch(self):
        """Point the watcher at the project dir AND its mosart/ subfolder — the
        project dir too, so a mosart/ created later is noticed and picked up."""
        watcher = getattr(self, "_fs_watcher", None)
        if watcher is None:
            return
        old = watcher.directories()
        if old:
            watcher.removePaths(old)
        proj = self._project_dir()
        if proj and os.path.isdir(proj):
            mdir = os.path.join(proj, "mosart")
            # Watch mosart/ once it exists (quiet — only MOSART writes there);
            # until then watch the project dir only to catch mosart/ appearing.
            watcher.addPath(mdir if os.path.isdir(mdir) else proj)

    def _on_mosart_dir_changed(self, _path):
        """Something changed under a watched dir. Debounce, because Google Drive
        writes a downloading file incrementally and we want the finished one."""
        self._setup_mosart_watch()          # a mosart/ may have just appeared
        self._mosart_debounce.start(1500)

    def _do_mosart_autoimport(self):
        self._import_mosart_layers(announce=False)

    def _import_mosart_layers(self, announce=True):
        """Load rasters in <project dir>/mosart/ that aren't in QGIS yet, into a
        “MOSART” layer group. Returns the count added. `announce` (a button
        press) creates the folder if absent and logs the no-op cases; the
        watcher passes False to stay quiet until something actually lands."""
        proj = self._project_dir()
        if not proj:
            if announce:
                self._append_log(
                    "No project dir set — set one in the dock so MOSART products "
                    "have a home (they go in <project dir>/mosart/).")
            return 0
        mdir = os.path.join(proj, "mosart")
        if not os.path.isdir(mdir):
            if not announce:
                return 0                    # watcher: nothing there yet
            try:
                os.makedirs(mdir, exist_ok=True)
            except OSError as e:
                self._append_log(f"Could not create {mdir}: {e}")
                return 0
            self._setup_mosart_watch()
            self._append_log(
                f"Created {mdir} — drop MOSART Δh GeoTIFFs here (or point your "
                "external MOSART run at it) and they'll load automatically.")
            return 0

        loaded = set()
        for lyr in self._project_rasters():
            try:
                loaded.add(os.path.normpath(lyr.source().split("|", 1)[0]))
            except (RuntimeError, AttributeError):
                pass
        exts = (".tif", ".tiff", ".vrt")
        added = skipped = 0
        for fn in sorted(os.listdir(mdir)):
            if not fn.lower().endswith(exts):
                continue
            path = os.path.join(mdir, fn)
            if os.path.normpath(path) in loaded:
                continue
            layer = QgsRasterLayer(path, os.path.splitext(fn)[0])
            if not layer.isValid():
                skipped += 1               # e.g. a file still syncing — retried next change
                continue
            lg.add_to_group(layer, "MOSART")   # registers it; fires layersAdded
            added += 1
        if added:
            self._refresh_layers()
            self._setup_mosart_watch()
            self._append_log(
                f"Imported {added} raster{'s' if added != 1 else ''} from "
                f"mosart/ into the “MOSART” group"
                + (f"; skipped {skipped} unreadable file(s)." if skipped else "."))
        elif announce:
            self._append_log(
                "No new rasters in mosart/ — everything there is already loaded"
                + (f" ({skipped} unreadable)." if skipped else "."))
        return added
