# Landslide Ground-Truthing — QGIS plugin

Interactive front-end for the landslide imagery pipeline. Pick an event location
on the map, set its date and the pre/post imagery windows, choose an imagery
source (PlanetScope ~3 m, Sentinel-2 ~10 m, Landsat ~30 m, or Auto), and the
pipeline runs and loads the results — true/false-colour pre+post, dNDVI,
brightness change, and the predicted epicentre point — straight into your project
for review. You confirm the slide and digitize the scar by eye in QGIS.

Users install it from the QGIS plugin repository. See the repository's main
README. This file is for working on the plugin.

## How it works (subprocess → separate Python)

The plugin runs inside QGIS's own Python (PyQt/PyQGIS only) and does **not**
import the heavy imagery stack. When you click **Search** or **Run**, it launches
a separate Python as a background process running `run_single.py`, streams its
output to the log, then reads the `result.json` it writes and loads the listed
layer files.

```
QGIS Python (plugin UI)  --subprocess-->  imagery Python (run_single.py)
        ^                                          |
        |  load layers  <----- result.json + *.tif/*.gpkg files
```

That Python is one of two (`dock.pipeline_python` / `pipeline_dir`):

- **Installed by the plugin** (`environment.py`, the Setup box's *Install
  imagery tools*). uv, a standalone Python 3.12 and `requirements.lock` go into
  the per-user data folder (`environment.data_dir()`). The scripts are the
  plugin's own `pipeline/` copy in a release zip, or this repository's root in a
  development checkout.
- **Your own** (*Use my own Python (advanced)*): any interpreter with
  `requirements.txt` installed, plus optionally a folder holding
  `run_single.py`. This is the development setup below.

## Development setup

1. Build the venv at the repository root:
   `python -m venv venv && venv/bin/pip install -r requirements.txt`.
2. Symlink the inner `landslide_groundtruth/` folder into your QGIS profile's
   plugins directory. QGIS 3 shown; QGIS 4 profiles live under `QGIS4/`:

   - **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`

   ```bash
   ln -s "$PWD/qgis_plugin/landslide_groundtruth" \
     "$HOME/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/landslide_groundtruth"
   ```
   (run from the repository root; edits are picked up on the next QGIS restart /
   *Plugin Reloader*.)
3. Enable **Landslide Ground-Truthing** under **Plugins ▸ Manage and Install
   Plugins ▸ Installed**, open the dock, and under **Setup ▸ Use my own Python
   (advanced)** set *Python for the imagery tools* to `venv/bin/python`. These
   paths live in QgsSettings, never in a project file.

Tests: `tests/run_all.sh` (QGIS 3.44 LTR), or
`QGIS_APP=~/Applications/QGIS-final-4_2_3.app tests/run_all.sh` for QGIS 4.

## Releasing

1. Bump `version` and `changelog` in `metadata.txt`.
2. If `requirements.txt` changed: `python3 scripts/build_release.py --relock --uv
   /path/to/uv`. This recompiles `requirements.lock`, pinned to what `./venv`
   has installed. Then check that a fresh *Install imagery tools* still works.
3. `python3 scripts/build_release.py` writes `dist/landslide_groundtruth-<version>.zip`,
   with the pipeline scripts copied into `pipeline/`.
4. Upload it at <https://plugins.qgis.org/plugins/add/> (OSGeo ID login). The site
   scans it with Bandit and detect-secrets; critical findings block a version.

## Use

1. **Pick location on map** → click the event location on the canvas.
2. Set **Search radius**, **Event time (UTC)**, and the **Days before / Days
   after** sliders.
3. Choose an **Imagery source** (Auto recommended; pick PlanetScope for small
   slides once Planet is authenticated).
4. Tune what the search considers:
   - **Cloud filter** (*off by default*) — with the box unticked there is **no**
     cloud filtering: every scene in the window is listed, clouds and all, so you
     can see the clouds and pick by eye. `eo:cloud_cover` is a scene-wide number
     over a ~110 km granule, not your AOI, and per-pixel cloud masking still runs
     afterwards — so a "90% cloudy" scene is regularly the clearest thing you have
     over the epicentre. Tick the box (and set a %) only to thin a very long list.
   - **AOI overlap (match Planet Explorer)** (default on) — accept any PlanetScope
     scene overlapping the search box. Uncheck to require the scene to cover the
     exact epicentre (stricter; can miss the nearest scenes).
   - **Include test-quality PlanetScope (match Planet Explorer)** (default on) —
     also consider scenes Planet flags as `test` quality, not just `standard`. Near
     a fresh event the nearest/clearest scenes are often test-only, so this is
     usually what recovers a 1-day-after scene. Test scenes have looser
     geo/radiometric calibration — fine for spotting/digitizing a scar by eye, but
     eyeball them before trusting NDVI/reflectance values.
5. **Search / Preview** (free) → lists the candidate before/after scenes per
   source in the table, nearest the event date first, with date, day-gap, cloud %,
   and source. **No orders are placed and nothing downloads** — use it to dial the
   event in before paying.

   Scene picking works the same way as the PlanetScope tab:
   - **Ticks choose the scenes, and nothing else does.** Nothing starts ticked; a
     **Run** downloads *exactly* the rows you tick and adds nothing to them
     (*Preview on map* renders the same rows). Tick one per side for a plain
     before/after pair, or several on a side to median-composite them. The ★/✓
     marks are only a suggestion — ★ is the scene the automatic ranking would lead
     with, greyed rows are ranked below its cutoff but are not unusable, and with
     the cloud filter off that's often where the near-date scene you actually want
     is sitting. With rows listed and none ticked, Run stops and asks you to pick;
     only an empty table (no Search yet) falls back to the automatic ranking.
   - **One side alone is a valid run.** Tick post scenes and no pre scene (a fresh
     event with nothing usable acquired before it, say) and the Run exports just
     that side's imagery — true colour, highlight, false colour, SWIR, NDVI. The
     three change rasters are skipped, since dNDVI/dNDSI/dBrightness subtract one
     side from the other. Pre-only works the same way.
   - **Selection is separate.** Click a row (Ctrl/Shift-click for several) to load
     its browse image in the preview pane and isolate its footprint on the map;
     double-click a row to preview just that one scene on the canvas.
   - **Quicklook gallery** — every pre and post candidate's browse thumbnail at
     once, grouped Before / After, so you can scan for the scene that's cloud-free
     over the AOI in one glance. Click a thumbnail to select that scene.
   - **Show scene footprints on map** (checkbox, off by default) — draws footprint
     outlines (before = blue, after = green) plus the search AOI box, so you can
     see whether a scene covers the AOI or leaves the epicentre in a diagonal
     nodata gap. With rows selected only *those* scenes' footprints are drawn;
     with nothing selected, all candidates are.
   - **Preview on map** — renders the ticked (or ★ best) before & after scenes
     over the AOI in *Highlight Optimized Natural Color* — the same look as the
     Run's `*_highlight.tif`, streamed from the data API with no download or
     order. Works for **Sentinel-2 and Landsat** (PlanetScope has its own tab).
6. **Run** → composites the imagery and loads the layers. Watch the log/progress;
   **Cancel** stops the subprocess.
7. When a Run finishes, the log shows a **SATELLITE USED** banner naming the source
   actually used, and the message bar warns (yellow) if Auto fell back off
   PlanetScope to coarser Sentinel-2/Landsat. The warning now also states **why**
   PlanetScope was skipped (e.g. "no orderable scene in the post window" or "ordered
   scenes clipped to no clear pixels over the AOI on the post side"), so a fallback
   isn't a mystery — note that PlanetScope is always *tried first* in Auto, so a
   fallback means its fetch failed, not that another source out-ranked it.

## Dialing in an event vs. billing

- **Search / Preview uses the Data API, which is free** and consumes no Planet
  quota — search and re-search as many times as you like (move the point, change
  the dates/cloud%/overlap) at no cost. This is how you "dial in" the event.
- **Only Run places Orders** (PlanetScope clip + download), and *each* Run places
  **2 Orders** (pre + post) that consume your Planet account quota. There is no
  "already-downloaded" skip, so re-running the *same* event orders — and bills —
  again. Settle the inputs with Search / Preview first, then Run once.
- **Sentinel-2 and Landsat are always free** (Microsoft Planetary Computer, no
  quota), so a Run that uses or falls back to them costs nothing.

## Notes / current limitations

- PlanetScope renders place Orders (pre+post) that consume Planet quota, once per
  scene (see the main README's *Ordering a scene once*).
- Progress is stage-level (parsed from the script's stdout), not a true percent.
- This is imagery-gathering only — scar delineation/area are done by hand in QGIS
  against the loaded layers.
- One run at a time.
