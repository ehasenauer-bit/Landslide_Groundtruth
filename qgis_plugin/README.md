# Landslide Ground-Truthing — QGIS plugin

Interactive front-end for the landslide imagery pipeline. Pick an event location
on the map, set its date and the pre/post imagery windows, choose an imagery
source (PlanetScope ~3 m, Sentinel-2 ~10 m, Landsat ~30 m, or Auto), and the
pipeline runs and loads the results — true/false-colour pre+post, dNDVI,
brightness change, and the predicted epicentre point — straight into your project
for review. You confirm the slide and digitize the scar by eye in QGIS.

## How it works (Option B: subprocess → venv)

The plugin runs inside QGIS's own Python (PyQt/PyQGIS only) and does **not**
import the heavy imagery stack. When you click **Run**, it launches the
project's `venv` Python as a background process running `run_single.py`, streams
its output to the log, then reads the `result.json` it writes and loads the
listed layer files. This keeps all the science dependencies in the venv and out
of QGIS — nothing extra to install into QGIS itself.

```
QGIS Python (plugin UI)  --subprocess-->  venv Python (run_single.py)
        ^                                          |
        |  load layers  <----- result.json + *.tif/*.gpkg files
```

## Install

1. Copy (or symlink) the inner `landslide_groundtruth/` folder into your QGIS
   plugins directory:

   - **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`

   ```bash
   ln -s "/Users/ethanhasenauer/Documents/AEC Work/SatClaude/landslide_groundtruth/qgis_plugin/landslide_groundtruth" \
     "$HOME/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/landslide_groundtruth"
   ```
   (A symlink means edits to the code are picked up on the next QGIS restart /
   *Plugin Reloader*.)

2. Restart QGIS → **Plugins ▸ Manage and Install Plugins ▸ Installed** → enable
   **Landslide Ground-Truthing**. (It's marked experimental; tick *Show also
   experimental plugins* if you don't see it.)

3. Click the toolbar button to open the dock.

## First-run setup (the Environment box)

| Field | Set to |
|-------|--------|
| **venv python** | `…/landslide_groundtruth/venv/bin/python` |
| **project dir** | `…/landslide_groundtruth` (folder containing `run_single.py`) |
| **output dir**  | where results/clips should be written (default: `<project>/out/interactive`) |

These persist between sessions. Planet auth is read automatically from
`~/.planet/` (run `…/venv/bin/planet auth login` once if needed); without it the
pipeline falls back to Sentinel-2/Landsat.

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
   - **Ticks choose the scenes.** The ★ row on each side (what the automatic
     ranking would lead with) starts ticked; *Preview on map* renders the ticked
     rows and a **Run** composites them. Tick several on a side to
     median-composite them, or untick the ★ and tick another to swap scenes. The
     ★/✓ marks are a suggestion — greyed rows are ranked below the automatic
     cutoff, not unusable, and with the cloud filter off that's often where the
     near-date scene you actually want is sitting.
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

## Notes / current limitations (v0.1, experimental)

- PlanetScope runs place **2 Orders** (pre+post) and consume Planet quota; see
  *Dialing in an event vs. billing* above.
- Progress is stage-level (parsed from the script's stdout), not a true percent.
- This is imagery-gathering only — scar delineation/area are done by hand in QGIS
  against the loaded layers.
- One run at a time.
