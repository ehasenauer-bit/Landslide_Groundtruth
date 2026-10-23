# Parked: the Volume tab's ∫Δh fit and DEM differencing

Removed from `qgis_plugin/landslide_groundtruth/volume_tab.py` on 2026-10-23,
when the tab was rebuilt around the way it is actually used: total + source-best
outlines drawn in the tab, one slide per project, and **both** area × thickness
and the source-scar fit wanted on every slide — so one Measure now runs both and
the Fit combo is gone. Neither ∫Δh path was being used: these events rarely have
two DEM epochs bracketing them.

`volume_tab_ddem.py` here is the removed UI code **verbatim**, as a `DdemMixin`.
It is outside `qgis_plugin/` so nothing imports or packages it, and outside
`tests/` so the runner does not sweep it.

What is **not** parked: `dem_diff.py` stays in the plugin, unchanged — the 3D
viewer uses its warp / write helpers, and `integrate_dh`, `stable_ground_stats`
and `difference_dems` (with `tests/test_dh_uncertainty.py`) still pass. Only the
tab's wiring to them is here.

## What is in the mixin

| Method | Was |
|---|---|
| `_build_ddem_box` | the "Elevation change → volume (∫Δh)" group: Δh picker, grid, sign, pre/post DEM pickers, co-register, *Difference DEMs → Δh layer* |
| `_refresh_ddem_pickers` | the block inside `_refresh_relief_combos` that filled those three raster pickers |
| `_measure_ddem` | Measure under the ∫Δh fit: stable-ground bias + σ, integrate over the total outline, erosion / deposition / net |
| `_difference_dems`, `_difference_dems_impl` | warp, co-register, subtract, write and load a Δh GeoTIFF (with the `limits.check_grid` guard) |
| `_secondary_dh`, `_secondary_larsen` | the opportunistic other estimate merged into one row |
| `_ddem_outline`, `_outline_utm`, `_raster_from`, `_ddem_layer`, `_ddem_grid_res` | helpers only these used |
| `_show_current_ddem` | the ∫Δh branch of `_show_current` |

## Putting it back

The tab no longer has a Fit combo, so ∫Δh would come back as a **third volume**
beside the two, not as a mode:

1. Mix `DdemMixin` into `VolumeTab` (or paste the methods in), and add the box to
   `_build_ui` — below Centerline is where it read best.
2. Call `_refresh_ddem_pickers()` at the end of `_refresh_relief_combos`.
3. In `_measure`, after the two fits, `row.update(self._secondary_dh())` — it is
   silent when no Δh raster is assigned. Store `v_dh_erosion` (always `abs()`: a
   negative erosion makes `verdict.d_log10` return None and the estimate vanish
   from the cross-check without a word).
4. Pass `dh_erosion=` to `verdict.reconcile` in `_verdict_row` / `_refresh_verdict`
   (the parameter is still there) and add a line for it; add `vol_dh_*` columns to
   `CSV_FIELDS` and `vdh_*` fields to `WRITEBACK_FIELDS`.
5. Restore the tests from the last commit that had them:
   `git show a698d80:tests/test_fit.py`, `…:tests/test_one_row_crosscheck.py`, and
   the `limits.check_grid` entry for `volume_tab.py` in `tests/test_theme_and_limits.py`.

The Fit-combo plumbing itself (`_on_fit_changed`, `_fit`, `_total_ready`, the
total-area fit) is not parked — `git show a698d80:qgis_plugin/landslide_groundtruth/volume_tab.py`
has the whole pre-rebuild tab.
