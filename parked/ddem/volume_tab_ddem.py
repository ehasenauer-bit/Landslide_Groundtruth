"""The Volume tab's elevation-change (∫Δh) fit and DEM differencing, parked.

Removed from volume_tab.py on 2026-10-23, when the tab was rebuilt around the two
fits actually run on every slide (deposit area × thickness and the Source-scar
fit, computed together by one Measure). Neither ∫Δh path was being used: these
events rarely have two DEM epochs bracketing them.

Everything below is the removed UI code VERBATIM, as a mixin, so nothing has to
be rewritten to bring it back. It is outside qgis_plugin/ so nothing imports or
packages it, and outside tests/ so the runner does not sweep it. dem_diff.py
itself stays in the plugin: the 3D viewer tab uses its warp/write helpers, and
its integrate_dh / stable_ground_stats / difference_dems are untouched.

See README.md beside this file for how to put it back.
"""
import os
import re

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QApplication,
)
from qgis.core import Qgis, QgsProject, QgsRasterLayer
from qgis.gui import QgsCollapsibleGroupBox


class DdemMixin:
    # ---------- was inline in _refresh_relief_combos ----------
    def _refresh_ddem_pickers(self):

        # The elevation-change fit's three raster pickers — the Δh input and the
        # pre/post pair — share the single-band-raster filter with the DEM combo.
        # Guarded with getattr because they're built after this may first run.
        for combo in (getattr(self, "_ddem_combo", None),
                      getattr(self, "_pre_dem_combo", None),
                      getattr(self, "_post_dem_combo", None)):
            if combo is None:
                continue
            keep = combo.currentData() if combo.count() else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— none —", None)
            for lyr in self._project_rasters():
                if lyr.bandCount() == 1:
                    combo.addItem(lyr.name(), lyr.id())
            idx = combo.findData(keep)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    # ---------- the inputs box ----------
    def _build_ddem_box(self):
        """Elevation-change (∫Δh) inputs — used only by the "Elevation change"
        fit. Collapsed by default; that fit's outline instruction points here.

        Two ways to feed it. Assign a Δh raster you already have (a
        lidar/photogrammetry dDEM, a SAR elevation-change product — anything in
        metres of surface change), OR build one here by differencing a pre/post
        DEM pair. Either
        way the volume is Δh summed over the TOTAL outline, split into erosion
        (loss) and deposition (gain). The differencing math lives in dem_diff so
        this tab stays free of numpy/GDAL until the fit is actually used."""
        box = QgsCollapsibleGroupBox("Elevation change → volume (∫Δh)")
        box.setSaveCollapsedState(False)
        box.setCollapsed(True)
        v = QVBoxLayout(box)

        note = QLabel(
            "For the “Elevation change (∫Δh over outline)” fit. Assign an "
            "elevation-change raster (Δh, in metres) and press Measure: the "
            "volume is Δh summed over the TOTAL outline (or the source outline "
            "if no total is assigned), reported as net, erosion and deposition. "
            "No Δh raster yet? Difference a pre/post DEM pair below to make one.")
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        v.addWidget(note)

        form = QFormLayout()
        # underscore-prefixed like the other layer pickers: rebuilt from the
        # project each refresh, so persisting them would only mark it dirty.
        self._ddem_combo = QComboBox()
        self._ddem_combo.setToolTip(
            "The elevation-change raster (Δh) to integrate — single-band, in "
            "METRES of surface change. Positive = the surface rose (deposition), "
            "negative = it dropped (erosion), unless you flip the sign below. A "
            "geographic (lon/lat) raster is reprojected to a metric grid before "
            "summing, so equal-area pixels are used.")
        form.addRow("Elevation change (Δh)", self._ddem_combo)

        self.ddem_grid_edit = QLineEdit("2")
        self.ddem_grid_edit.setToolTip(
            "Grid resolution in metres for the integration (and for the "
            "differencing below). The raster is warped to this square grid, so a "
            "pixel is exactly this on a side. Finer captures more detail but "
            "costs memory; coarser than the input's own resolution just smooths "
            "it. Defaults to 2 m.")
        form.addRow("Grid (m)", self.ddem_grid_edit)

        self.ddem_deposit_positive = QCheckBox(
            "Positive Δh is deposition (post − pre)")
        self.ddem_deposit_positive.setChecked(True)
        self.ddem_deposit_positive.setToolTip(
            "Which way the Δh raster is signed. Checked (post − pre): a positive "
            "value means the surface ROSE — deposition. Uncheck for a pre − post "
            "product, where positive means loss; the sign is flipped so erosion "
            "and deposition still come out labelled correctly.")
        form.addRow("Sign", self.ddem_deposit_positive)
        v.addLayout(form)

        sub = QLabel("Make a Δh raster by differencing two DEMs:")
        sub.setStyleSheet("QLabel { color: palette(mid); font-style: italic; }")
        v.addWidget(sub)

        dform = QFormLayout()
        self._pre_dem_combo = QComboBox()
        self._pre_dem_combo.setToolTip(
            "The BEFORE (pre-event) elevation surface — e.g. a pre-event lidar "
            "DSM. A tiled DEM must be loaded as a single layer (a VRT) to be "
            "picked here.")
        dform.addRow("Pre-event DEM", self._pre_dem_combo)
        self._post_dem_combo = QComboBox()
        self._post_dem_combo.setToolTip(
            "The AFTER (post-event) elevation surface. Δh = post − pre.")
        dform.addRow("Post-event DEM", self._post_dem_combo)
        v.addLayout(dform)

        self.ddem_coregister = QCheckBox("Vertical co-register on stable ground")
        self.ddem_coregister.setChecked(True)
        self.ddem_coregister.setToolTip(
            "Remove the DC vertical bias between the two DEMs — a sigma-clipped "
            "median of the difference, so the slide and other real change fall "
            "out and only quasi-stable ground sets the offset. Leave on unless "
            "the DEMs already share a vertical datum with no geolocation bias.\n\n"
            "Caveat: where the surrounding ground is itself changing (a glacier "
            "between epochs), the offset is drawn from moving ground — check the "
            "stable-pixel count reported in the log.")
        v.addWidget(self.ddem_coregister)

        row = QHBoxLayout()
        self.diff_btn = QPushButton("Difference DEMs → Δh layer")
        self.diff_btn.setToolTip(
            "Warp the pre and post DEMs to a shared metric grid over the "
            "outline, co-register, subtract, and load the result as a Δh raster "
            "— then selected above, ready to Measure. Runs in this window; a "
            "large AOI at a fine grid can take a few seconds.")
        self.diff_btn.clicked.connect(self._difference_dems)
        row.addWidget(self.diff_btn)
        row.addStretch(1)
        v.addLayout(row)
        return box

    # ---------- elevation-change (∫Δh) fit ----------
    def _ddem_outline(self):
        """(feature, layer, role) for the outline the ∫Δh fit integrates over.

        The TOTAL outline when assigned — both erosion and deposition live inside
        it — else the best source outline. (None, None, None) with a logged
        reason when neither is available."""
        _a_total, total_feat, total_layer = self._role_area("total", "Total area")
        _a_best, best_feat, best_layer = self._role_area("best", "Source best")
        if total_feat is not None:
            return total_feat, total_layer, "total"
        if best_feat is not None:
            return best_feat, best_layer, "source"
        return None, None, None

    def _outline_utm(self, feat, layer):
        """(geom_in_utm, epsg_int, bounds_tuple) for a feature, or (None, ...).

        The metric frame the differencing and integration share — a UTM zone
        chosen from the outline's own centroid, and its bounding box (both in
        metres). Logging of any CRS failure is done by _to_utm."""
        geom_utm, utm_crs = self._to_utm(feat.geometry(), layer.crs())
        if geom_utm is None:
            return None, None, None
        try:
            epsg = int(utm_crs.authid().split(":")[1])
        except (AttributeError, IndexError, ValueError):
            self._append_log("Could not read the UTM zone's EPSG code.")
            return None, None, None
        return geom_utm, epsg, geom_utm.boundingBox()

    # ---------- the other estimate, alongside the primary fit ----------
    # The Fit combo picks ONE calibration per Measure, and it used to end there:
    # a row was Larsen or ∫Δh, never both, and measuring the same slide twice
    # auto-renamed it, so the two independent volumes landed in the table as
    # "slide 1" and "slide 2" with nothing recording they were one event. The act
    # of recording the cross-check destroyed it. These two helpers run the OTHER
    # path opportunistically when its inputs happen to be assigned and merge the
    # result into the SAME row under its own field names, so one row can carry
    # the seismic, the area-scaling and the elevation-change volumes at once.
    # The Fit still decides which one is the headline (v_best).

    def _secondary_dh(self):
        """{v_dh_*} from integrating the assigned Δh over the outline, or {}.

        Silent by design: this runs beside an area-scaling Measure the user did
        not ask to be a Δh measurement, so a missing layer or a failed warp
        should add nothing rather than raise an error about a fit they did not
        select."""
        try:
            from . import dem_diff
            dh_layer = self._ddem_layer()
            if dh_layer is None:
                return {}
            feat, layer, role = self._ddem_outline()
            if feat is None:
                return {}
            geom_utm, epsg, bb = self._outline_utm(feat, layer)
            if geom_utm is None:
                return {}
            res = self._ddem_grid_res()
            margin = 2.0 * res
            bounds = (bb.xMinimum() - margin, bb.yMinimum() - margin,
                      bb.xMaximum() + margin, bb.yMaximum() + margin)
            pad = max(4.0 * res, 0.5 * max(bb.width(), bb.height()))
            cal_bounds = (bb.xMinimum() - pad, bb.yMinimum() - pad,
                          bb.xMaximum() + pad, bb.yMaximum() + pad)
            cal = dem_diff.stable_ground_stats(
                dh_layer.source(), geom_utm.asWkt(), epsg, cal_bounds, res)
            area = self._measure_area(feat.geometry(), layer.crs())
            r = dem_diff.integrate_dh(
                dh_layer.source(), geom_utm.asWkt(), epsg, bounds, res,
                sign_deposit_positive=self.ddem_deposit_positive.isChecked(),
                offset=(cal["offset_m"] if cal.get("ok") else 0.0),
                sigma_dh_m=cal.get("sigma_m", 0.0), outline_area_m2=area)
            if not r.get("pixel_count"):
                return {}
            return {
                "v_dh_erosion": abs(r["v_erosion"]),
                "v_dh_deposit": r["v_deposit"],
                "v_dh_net": r["v_net"],
                "v_dh_sigma": r.get("v_sigma_m3"),
                "sigma_dh": r.get("sigma_dh_m"),
                "dh_offset": r.get("offset_applied_m"),
                "dh_coverage": r.get("coverage_frac"),
                "ddem_name": dh_layer.name(), "ddem_res": res,
                "covered_area": r["covered_area_m2"],
                "mean_dh": r["mean_dh_m"], "max_rise": r["max_rise_m"],
                "max_drop": r["max_drop_m"],
            }
        except Exception as e:
            self._append_log(f"(Δh cross-check skipped: {e})")
            return {}

    def _secondary_larsen(self):
        """{v_larsen*} from area-scaling the source scar, or {} — the mirror of
        _secondary_dh, run beside a ∫Δh Measure so the two can be compared."""
        try:
            a_best, best_feat, best_layer = self._role_area("best", "Source best")
            if best_feat is None or not a_best:
                return {}
            a_low, _lf, _ll = self._role_area("low", "Source low")
            a_high, _hf, _hl = self._role_area("high", "Source high")
            v, vlo, vhi, calc = volume_calc.volume_source(
                a_best, A_low=a_low, A_high=a_high, material=self._material(),
                fit="scar", project_dir=self.dock.project_edit.text().strip())
            return {"v_larsen": v, "v_larsen_lo": vlo, "v_larsen_hi": vhi,
                    "larsen_area_m2": a_best, "larsen_calc": calc}
        except Exception as e:
            self._append_log(f"(area-scaling cross-check skipped: {e})")
            return {}

    def _measure_ddem(self):
        """Volume from integrating an elevation-change raster over the outline.

        The Δh raster (a lidar/photogrammetry dDEM, an externally produced SAR
        elevation-change product, or one built by the differencing tool) is summed over the TOTAL outline — the
        whole affected area, since both erosion and deposition count — or the
        source outline when no total is assigned. dem_diff does the numpy/GDAL
        work; this resolves the outline to metres, hands it over, and drops the
        result into the same _current/results spine the area-scaling fits use."""
        try:
            from . import dem_diff
        except Exception as e:
            self._append_log(
                "The elevation-change fit needs the DEM tools (numpy + GDAL), "
                f"which failed to load: {e}. They ship with QGIS — if this "
                "persists the install's Python is incomplete.")
            return

        outline_feat, outline_layer, conv_role = self._ddem_outline()
        if outline_feat is None:
            self._append_log(
                "The elevation-change fit sums Δh over the TOTAL landslide "
                "outline — assign that layer (or a source outline) first.")
            return
        dh_layer = self._ddem_layer()
        if dh_layer is None:
            self._append_log(
                "No elevation-change raster assigned. Pick one under “Elevation "
                "change (Δh)” — a Δh product in metres — or build one with "
                "“Difference DEMs → Δh layer”.")
            return

        geom_utm, epsg, bb = self._outline_utm(outline_feat, outline_layer)
        if geom_utm is None:
            return
        res = self._ddem_grid_res()
        margin = 2.0 * res
        bounds = (bb.xMinimum() - margin, bb.yMinimum() - margin,
                  bb.xMaximum() + margin, bb.yMaximum() + margin)
        sign_pos = self.ddem_deposit_positive.isChecked()
        area = self._measure_area(outline_feat.geometry(), outline_layer.crs())
        # Calibrate against the ground AROUND the slide before integrating.
        # An inverted Δh (shape-from-shading and friends) constrains the shape
        # of the change field far better than its absolute datum, and volume is
        # LINEAR in the residual offset — half a metre over a 1 km² outline is
        # 500,000 m³ of pure artefact reported as signal. The same pass gives the
        # per-pixel noise, which is the only reason this estimate can carry an
        # error bar at all. A wider box than the outline, so there IS surrounding
        # ground to measure.
        cal_pad = max(4.0 * res, 0.5 * max(bb.width(), bb.height()))
        cal_bounds = (bb.xMinimum() - cal_pad, bb.yMinimum() - cal_pad,
                      bb.xMaximum() + cal_pad, bb.yMaximum() + cal_pad)
        try:
            cal = dem_diff.stable_ground_stats(
                dh_layer.source(), geom_utm.asWkt(), epsg, cal_bounds, res)
        except Exception as e:
            self._append_log(f"Stable-ground check failed ({e}); "
                             "integrating without a bias correction.")
            cal = {"offset_m": 0.0, "sigma_m": 0.0, "stable_px": 0, "ok": False}
        dh_offset = float(cal["offset_m"]) if cal.get("ok") else 0.0
        if cal.get("ok"):
            self._append_log(
                f"Stable ground around the slide: bias {cal['offset_m']:+.3f} m, "
                f"noise {cal['sigma_m']:.3f} m ({cal['stable_px']:,} px). "
                f"The bias is removed before integrating.")
        else:
            self._append_log(
                "Not enough ground outside the outline to check the Δh for a "
                "residual bias — the volume assumes it is unbiased, and carries "
                "no uncertainty.")
        try:
            r = dem_diff.integrate_dh(
                dh_layer.source(), geom_utm.asWkt(), epsg, bounds, res,
                sign_deposit_positive=sign_pos,
                offset=dh_offset, sigma_dh_m=cal.get("sigma_m", 0.0),
                outline_area_m2=area)
        except Exception as e:
            self._append_log(
                f"Could not integrate “{dh_layer.name()}” over the outline: {e}")
            return
        cover = r.get("coverage_frac")
        if cover is not None and cover < 0.90 and r["pixel_count"]:
            self._notify(
                f"The elevation-change layer covers only {cover:.0%} of your "
                f"outline, so this volume is roughly {cover:.0%} of the real "
                f"one. Check the Δh layer's extent and nodata before using it.",
                level=Qgis.Warning)
        if r["pixel_count"] == 0:
            self._append_log(
                f"“{dh_layer.name()}” has no valid Δh pixels inside the outline "
                "— does it cover this slide? Check its extent and nodata.")
            return

        material, material_label = self._material(), self.material_combo.currentText()
        fit_label = self.fit_combo.currentText()
        calc = (f"∫Δh · {res:g} m grid · "
                f"{'post−pre' if sign_pos else 'pre−post'} (dem_diff)")
        self._current = {
            "name": self.name_edit.text().strip() or "slide",
            "material": material, "material_label": material_label,
            "fit": "ddem", "fit_label": fit_label,
            "conv_role": "ddem",
            "a_conv": r["covered_area_m2"], "a_conv_low": None, "a_conv_high": None,
            # a_total is the TOTAL outline's plan area — recorded only when a
            # total outline actually drove the integration, mirroring the
            # area-scaling path (which leaves it None otherwise). The integrated
            # (covered) area is carried separately in covered_area.
            "a_total": area if conv_role == "total" else None,
            "src_best": None, "src_low": None, "src_high": None,
            # |erosion|, NOT the net. For an outline that correctly spans scar
            # AND deposit — which is what this fit asks for — the net is a small
            # residual of two large opposing numbers and tends to zero as the
            # delineation improves: 2.00 Mm³ eroded against 1.99 Mm³ deposited
            # reports 12,000 m³. A seismic inversion estimates the MOBILIZED
            # volume, which is the erosion side.
            "v_best": abs(r["v_erosion"]),
            "v_low": (abs(r["v_erosion"]) - r.get("v_sigma_m3", 0.0)
                      if r.get("v_sigma_m3") else None),
            "v_high": (abs(r["v_erosion"]) + r.get("v_sigma_m3", 0.0)
                       if r.get("v_sigma_m3") else None),
            "v_net": r["v_net"],
            "v_erosion": abs(r["v_erosion"]), "v_deposit": r["v_deposit"],
            "sigma_dh": r.get("sigma_dh_m"), "v_sigma": r.get("v_sigma_m3"),
            "dh_offset": r.get("offset_applied_m"),
            "dh_coverage": cover,
            "covered_area": r["covered_area_m2"],
            "mean_dh": r["mean_dh_m"], "max_rise": r["max_rise_m"],
            "max_drop": r["max_drop_m"],
            "ddem_name": dh_layer.name(), "ddem_res": res,
            "length": None, "length_method": "",
            "z_top": None, "z_bottom": None, "drop": None,
            "reach_angle": None, "hl_ratio": None, "dem_name": "",
            "layer_id": outline_layer.id(), "layer_name": outline_layer.name(),
            "conv_fid": outline_feat.id(),
            "len_layer_id": outline_layer.id(), "len_fid": outline_feat.id(),
            "len_from": conv_role,
            "total_layer": outline_layer.name() if conv_role == "total" else "",
            # the outline is already named by layer_name/used_text; don't also
            # claim it as the source-best layer (which has no area under ddem).
            "best_layer": "", "low_layer": "", "high_layer": "",
            "fids_text": str(outline_feat.id()),
            "used_text": (f"∫Δh “{dh_layer.name()}” over {conv_role} "
                          f"“{outline_layer.name()}”  (feature {outline_feat.id()})"),
            "calc": calc,
        }
        self._current["v_dh_erosion"] = abs(r["v_erosion"])
        self._current["v_dh_deposit"] = r["v_deposit"]
        self._current["v_dh_net"] = r["v_net"]
        self._current["v_dh_sigma"] = r.get("v_sigma_m3")
        self._current.update(self._secondary_larsen())
        self._cl_fid = None
        self._show_current()
        self.add_btn.setEnabled(True)
        self.centerline_btn.setEnabled(True)
        self.remeasure_btn.setEnabled(False)
        self.sample_btn.setEnabled(False)

        self._append_log(
            f"∫Δh over {conv_role} “{outline_layer.name()}” "
            f"({r['covered_area_m2'] / 1e6:.3f} km² covered, "
            f"{r['pixel_count']:,} px @ {res:g} m): "
            f"eroded {_fmt(abs(r['v_erosion']))} m³, deposited "
            f"{_fmt(r['v_deposit'])} m³, net {_fmt(r['v_net'])} m³. "
            f"Mean Δh {r['mean_dh_m']:+.2f} m (drop {r['max_drop_m']:+.1f}, "
            f"rise {r['max_rise_m']:+.1f}). Δh from “{dh_layer.name()}”; {calc}.")
        if conv_role == "source":
            self._append_log(
                "No total outline assigned — integrated over the SOURCE outline, "
                "so the runout deposit is excluded. Assign a total outline to "
                "capture the whole affected area.")

    def _difference_dems(self):
        """Re-entry guard around the actual run: the impl calls processEvents()
        (to flush the 'differencing…' log), which can dispatch a SECOND queued
        click of this same button into a nested, concurrent run. Disable the
        button and gate on a flag so the second click is ignored."""
        if getattr(self, "_diffing", False):
            return
        self._diffing = True
        self.diff_btn.setEnabled(False)
        try:
            self._difference_dems_impl()
        finally:
            self._diffing = False
            self.diff_btn.setEnabled(True)

    def _difference_dems_impl(self):
        """Warp a pre/post DEM pair to one metric grid, co-register, subtract,
        write the Δh as a GeoTIFF and load it — selected for the ∫Δh fit.

        Runs synchronously: a manual button on a bounded AOI, so a few seconds of
        blocking is acceptable and simpler than a background task."""
        try:
            from . import dem_diff
        except Exception as e:
            self._append_log(f"DEM tools (numpy + GDAL) failed to load: {e}.")
            return
        pre = self._raster_from(self._pre_dem_combo)
        post = self._raster_from(self._post_dem_combo)
        if pre is None or post is None:
            self._append_log(
                "Pick both a pre-event and a post-event DEM to difference.")
            return
        if pre.id() == post.id():
            self._append_log("Pre and post DEM are the same layer — pick two.")
            return

        outline_feat, outline_layer, _role = self._ddem_outline()
        if outline_feat is None:
            self._append_log(
                "Assign a total (or source) outline first — it sets the area to "
                "difference over.")
            return
        geom_utm, epsg, bb = self._outline_utm(outline_feat, outline_layer)
        if geom_utm is None:
            return
        res = self._ddem_grid_res()
        # a generous margin so stable ground surrounds the slide for co-registration
        margin = max(300.0, 0.25 * max(bb.width(), bb.height()))
        bounds = (bb.xMinimum() - margin, bb.yMinimum() - margin,
                  bb.xMaximum() + margin, bb.yMaximum() + margin)
        coreg = self.ddem_coregister.isChecked()

        # The outline size and the grid resolution are set independently, and
        # this path holds several arrays of the same shape at once (pre, post,
        # difference, mask). Refuse a grid that cannot finish rather than warping
        # into a hang the user cannot tell from a slow run.
        from . import limits
        ok, lmsg = limits.check_grid(bounds[2] - bounds[0], bounds[3] - bounds[1],
                                     res, what="difference grid")
        if not ok:
            self._notify(lmsg, level=Qgis.Warning)
            return
        if lmsg:
            self._append_log(lmsg)

        self._append_log(
            f"Differencing “{post.name()}” − “{pre.name()}” over "
            f"{bb.width() + 2 * margin:.0f}×{bb.height() + 2 * margin:.0f} m @ "
            f"{res:g} m … runs here, may take a few seconds.")
        QApplication.processEvents()
        try:
            dh, gt, proj, stats = dem_diff.difference_dems(
                pre.source(), post.source(), bounds, epsg, res, coregister=coreg)
        except Exception as e:
            self._append_log(f"Differencing failed: {e}")
            return

        out_dir = self.dock.results_dir()      # this project's folder once it's saved
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)   # <tools>/out/interactive may not exist yet
            except OSError:
                pass
        if not out_dir or not os.path.isdir(out_dir):
            import tempfile
            out_dir = tempfile.gettempdir()
        name = self.name_edit.text().strip() or "slide"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "slide"
        path = os.path.join(out_dir, f"{safe}_dh.tif")
        # Drop any layer already reading this exact file so a re-difference
        # updates ONE Δh layer instead of stacking duplicates — and so the file
        # handle is released first (on Windows an open layer locks the GeoTIFF
        # and the rewrite below would fail with a confusing "could not write").
        for lyr in list(self._project_rasters()):
            try:
                if (os.path.normpath(lyr.source().split("|", 1)[0])
                        == os.path.normpath(path)):
                    lg.remove_layer(lyr)
            except (RuntimeError, AttributeError):
                pass
        try:
            dem_diff.write_gtiff(path, dh, gt, proj)
        except Exception as e:
            self._append_log(f"Could not write {path}: {e}")
            return
        layer = QgsRasterLayer(path, f"Δh {name} (post−pre)")
        if not layer.isValid():
            self._append_log(f"Wrote {path} but QGIS could not load it back.")
            return
        lg.add_to_group(layer, "Volume")   # registers the layer AND folders it
        self._refresh_relief_combos()
        idx = self._ddem_combo.findData(layer.id())
        if idx >= 0:
            self._ddem_combo.setCurrentIndex(idx)

        warn = ""
        if stats["valid_px"] and stats["stable_px"] < 0.05 * stats["valid_px"]:
            warn = (f" ⚠ the offset rested on a thin slice of ground "
                    f"({stats['stable_px']:,} px) — treat it as unreliable.")
        self._append_log(
            f"Δh written to {path} and loaded as “{layer.name()}”, selected "
            f"above. Vertical offset removed: {stats['offset_m']:+.2f} m (from "
            f"{stats['stable_px']:,} stable of {stats['valid_px']:,} valid px)."
            f"{warn} Now press Measure with the elevation-change fit selected.")

    # ---------- was the first branch of _show_current ----------
    def _show_current_ddem(self, c):
        if c.get("conv_role") == "ddem":
            # The ∫Δh fit reports a covered area (not a converted one), the
            # change-field statistics, and an erosion/deposition split in place
            # of the area fit's ±1σ range.
            self.source_out.setText(c["used_text"])
            self.area_best_out.setText(
                f"{_fmt(c['covered_area'])} m² covered  "
                f"({c['covered_area'] / 1e6:,.4f} km² @ "
                f"{_fmt(c.get('ddem_res'))} m grid)")
            self.area_range_out.setText(
                f"mean Δh {c['mean_dh']:+.2f} m   ·   max drop "
                f"{c['max_drop']:+.1f} m   ·   max rise {c['max_rise']:+.1f} m")
            # The headline is the MOBILIZED volume (|erosion|) — the quantity a
            # seismic inversion estimates — not the net, which tends to zero as
            # the delineation improves and once printed 12,000 m³ for a 2 Mm³
            # slide. 3 significant figures: the ∫Δh error bar is decimetres of
            # vertical times a whole scar, so 4 decimal places of Mm³ was
            # precision theatre.
            sig = c.get("v_sigma")
            head = f"{_fmt(c['v_best'])} m³   ({c['v_best'] / 1e6:,.3g} Mm³) eroded"
            if sig:
                head += f"   ± {_fmt(sig)} m³"
            self.vol_best_out.setText(head)
            bits = [f"deposited {_fmt(c['v_deposit'])} m³",
                    f"net {_fmt(c.get('v_net'))} m³"]
            if c.get("sigma_dh") is not None:
                bits.append(f"Δh noise {c['sigma_dh']:.2f} m")
            if c.get("dh_offset"):
                bits.append(f"bias removed {c['dh_offset']:+.2f} m")
            if c.get("dh_coverage") is not None:
                bits.append(f"{c['dh_coverage']:.0%} of the outline covered")
            self.vol_range_out.setText("   ·   ".join(bits))
            self.calc_out.setText(c["calc"])
            self._show_depth(c)

    def _raster_from(self, combo):
        """The raster layer a combo points at, or None."""
        lyr = QgsProject.instance().mapLayer(combo.currentData() or "")
        return lyr if isinstance(lyr, QgsRasterLayer) else None

    def _ddem_layer(self):
        """The raster assigned to "Elevation change (Δh)", or None."""
        if not hasattr(self, "_ddem_combo"):
            return None
        return self._raster_from(self._ddem_combo)

    def _ddem_grid_res(self):
        """Integration/differencing grid resolution (m); 2 m on empty/bad input."""
        try:
            r = float(self.ddem_grid_edit.text().strip())
            if r > 0:
                return r
        except (ValueError, AttributeError):
            pass
        return 2.0
