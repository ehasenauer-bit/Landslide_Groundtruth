# Parked: the Volume tab's deposit area × thickness fit

Removed from `qgis_plugin/landslide_groundtruth/volume_tab.py` and `volume_calc.py`
on 2026-11-04, on Ethan's call. Area × thickness (Toney et al. 2021: the total
outline's plan area × a mean deposit thickness) had run beside Larsen et al.
(2010) on every Measure, with neither one the headline. Earlier publications on
these landslides used Larsen et al., while the deposit thicknesses here had not
been checked against measured volumes. So Larsen became the tab's one volume. The
source low and high outlines moved out of their collapsed box into the Outlines
panel, and each now gets its own Larsen volume (the low and high estimates).

Nothing here is imported, packaged or run. It sits outside `qgis_plugin/`, so the
release build leaves it out, and outside `tests/`, so the test runner skips it. The
last commit with the fit in place is **`cae8034`**:
`git show cae8034:qgis_plugin/landslide_groundtruth/volume_tab.py` gives the whole
tab as it was.

## What is here

| File | What it holds |
|---|---|
| `volume_calc_thickness.py` | `THICKNESS_SETTINGS` (the seven deposit settings, with sources), `thickness_setting()` and `volume_thickness()`, verbatim from `volume_calc.py` |
| `volume_tab_thickness.py` | `ThicknessMixin`, holding verbatim from `volume_tab.py`: `_build_settings_box` as it was (thickness and material), `_apply_thickness_setting`, `_sync_thickness_setting`, `_show_thickness_note`, `_thickness_values`, `_thickness_volume`; plus three snippets that were inline in methods the tab kept (`_build_thickness_readout`, `_log_thickness`, `_show_thickness`), and the fit's write-back, table and CSV columns as constants |
| `tests/test_thickness_setting.py` | the Setting combo / numbers test, moved unchanged from `tests/` |

The research behind the settings is in `docs/volume_methods_references.md` §3.

## What stayed in the plugin

- **The implied mean depth** (Larsen volume ÷ total outline area) and the
  >5 m warning (`volume_calc.LARSEN_DEPTH_FLAG_M`, `implied_depth_note`). These
  check Larsen against Deline et al. 2015's measured deposits and do not use
  this fit.
- **`project_state.RETIRED_DEFAULTS["3"]`**, the 1.5 / 1.0 / 2.5 m thickness
  swap. No widget has those keys now, so it does nothing. It is kept so that old
  projects still migrate if the fit comes back. Projects saved while the fit was
  live still hold `volume/thick_*_edit` entries, which go unread, so if you put
  the fields back those projects reopen on their own thicknesses.
- **`LEGACY_WRITEBACK`** now names `thick_*` and `vthk_*`. A layer written before
  the park keeps those fields, and the log explains them rather than overwriting
  them.

## Putting it back

The tab has no Fit combo, so the fit would come back as a second volume beside
Larsen (that is how it ran until 2026-11-04):

1. Paste `volume_calc_thickness.py`'s three names back into `volume_calc.py`.
   They were just above `LARSEN_DEPTH_FLAG_M`, with `volume_thickness` at the end.
   Re-add the `"thickness"` entries to `FITS` and `FIT_OUTLINE` if you want them
   (`git show cae8034:…/volume_calc.py`).
2. Mix `ThicknessMixin` into `VolumeTab` (or paste the methods in). The
   `_build_settings_box` here replaces the tab's current one.
3. In `_build_volumes_box`, call `self._build_thickness_readout(f)` before the
   Larsen row. In `_show_current`, call `self._show_thickness(c)` in the `else:`
   branch and `self.vol_thick_out.setText("press Measure")` in the `None` one.
4. In `_measure`, run `thick = self._thickness_volume(a_total, a_best, warnings)`
   beside `_scar_volume`, and add `ROW_KEYS_THICKNESS` to the row before
   `row.update(thick)`. Then call `self._log_thickness(row)` when `thick` is set.
   The cae8034 `_measure` also returned early when neither fit ran. Today's
   version records a total-only slide with no volume, and that is still right
   with thickness back.
5. Add `WRITEBACK_FIELDS_THICKNESS`, `TABLE_COLS_THICKNESS` (with their
   `_refresh_table` values, and widen `TABLE_NUMERIC`) and `CSV_FIELDS_THICKNESS`
   to the tab's lists, and take `thick_*` / `vthk_*` back out of
   `LEGACY_WRITEBACK`.
6. Add `("v_thick", "area × thickness")` back to `viewer3d_tab._pull_volume_details`,
   and restore `_estimates()` to return `(thickness, larsen, row)` if anything
   needs it.
7. Tests: move `tests/test_thickness_setting.py` back into `tests/`. Restore the
   thickness sections of `tests/test_volume_flow.py`, `test_fit.py`,
   `test_one_row_crosscheck.py` and `test_retired_defaults.py` from
   `git show cae8034:tests/<file>`.
