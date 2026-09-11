# MOSART — parked

MOSART (SAR elevation change from Sentinel-1 amplitude) was removed from the
Volume tab. It is **not ready for research use** and the plugin should not
advertise it. Nothing in `qgis_plugin/` imports anything in this folder.

## Why it came out

MOSART's coregistration is ISCE2 / hyp3-isce2 — Linux-only, so it cannot run
inside QGIS on a Mac. What shipped in the dock was only the QGIS-side half: a
box that wrote a job spec (`<name>_job.json`) into `<project dir>/mosart/`,
opened a Colab notebook, and then watched that folder so a Δh GeoTIFF written
back by hand would auto-import into a "MOSART" layer group. The notebook itself
still needed the user to confirm the bursts `asf_search` returned and to tune
speckle and water masking. A groupbox labelled "Run MOSART" in front of that is
a promise the plugin cannot keep.

The **∫Δh fit is untouched and still works.** It takes any Δh raster in metres —
a lidar/photogrammetry dDEM, an externally produced SAR product — and it can
still build one itself from a pre/post DEM pair via "Difference DEMs → Δh
layer". The bias correction and the σ_V bounds in `dem_diff.py` (the parts that
exist *because* an inverted Δh carries a DC offset) are all still there. Only the
job-writer and the folder watcher are gone.

## What is here

| File | What it is |
|---|---|
| `volume_tab_mosart.py` | Every line removed from `volume_tab.py`, verbatim, re-homed onto a `MosartMixin` so it stays syntactically whole. Not imported by anything. |
| `Run_MOSART.ipynb` | The Colab notebook, moved from `mosart_colab/`. |

## Reinstating it

The code came out of `qgis_plugin/landslide_groundtruth/volume_tab.py` at five
points. `git log --diff-filter=D -- mosart_colab` finds the removal commit if you
would rather take the diff than re-apply by hand.

1. **Imports.** Restore what came out of the header — these had no other consumer
   in the module:
   ```python
   import json
   import webbrowser
   from datetime import datetime, timezone
   from qgis.PyQt.QtCore import Qt, QVariant, QTimer, QFileSystemWatcher
   ```
2. **`__init__`**, after `self._on_fit_changed()` — call `self._init_mosart()`
   (or paste its body back).
3. **`_build_ui`**, after `root.addWidget(self._build_ddem_box())` — add
   `root.addWidget(self._build_mosart_box())`.
4. **The ↻ Refresh button** in `_build_ui` — repoint it from `_refresh_layers`
   to `_refresh_and_import`, and restore the tooltip that mentions `mosart/`.
5. **The methods** — either add `MosartMixin` to `VolumeTab`'s bases, or paste
   the two blocks (`_build_mosart_box` / `_prepare_mosart_job`, and the
   `# MOSART folder: organise + auto-import` section) back in. Note that
   `_project_dir()` is in the second block: it is currently MOSART-only, so it
   came out too, and anything else that grows a need for it will want it back
   in the tab proper rather than in the mixin.

Also update the default notebook URL in `_build_mosart_box` — it still points at
`mosart_colab/Run_MOSART.ipynb`, which is now `parked/mosart/Run_MOSART.ipynb`.

## Before it goes back in

The thing that made it not-ready was never the plumbing; it was that a Δh the
plugin cannot produce, validate, or bound was being fed to a volume integral
that is *linear* in its DC offset. Whatever replaces this needs the inversion to
run somewhere reproducible, and needs its output checked against
`dem_diff.stable_ground_stats` before anyone reports a number from it.
