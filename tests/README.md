# Plugin checks

Headless checks for the QGIS plugin. They build real widgets, apply real
renderers to real GeoTIFFs and run a real subprocess — no mocking of the parts
that matter — so a green run means the plugin actually loads and behaves, not
that a stub agreed with itself.

```bash
./tests/run_all.sh              # everything, ~40 s
./tests/run_all.sh detection    # only files matching "detection"
```

## Which Python

These `import qgis.core`, so they need the interpreter **inside the QGIS app
bundle** — not the project `venv`, which has the imagery stack but no PyQGIS.
`run_all.sh` finds it; override if yours is elsewhere:

```bash
QGIS_PYTHON=/path/to/QGIS.app/Contents/Frameworks/bin/python3 ./tests/run_all.sh
```

The runner also exports `PROJ_LIB` / `GDAL_DATA` from the bundle and clears
`PYTHONHOME`. Both matter:

* without `proj.db`, a coordinate transform silently returns its input
  unchanged, so the CRS check would pass for the wrong reason;
* an inherited `PYTHONHOME` stops the child interpreter booting at all
  (`init_fs_encoding`), which is the same failure `task.py::_clean_env` exists
  to prevent.

## What each file pins down

| File | Guards against |
|---|---|
| `test_plugin_loads.py` | The plugin failing to import or build. Imports all 26 modules, builds the dock, checks all six tabs, tears down. Run this first — it catches a syntax error anywhere in the package. |
| `test_detection.py` | Losing the seismic record's meaning: the UTC→Alaska calendar-day rollover, a dropped minus sign on longitude, out-of-range values half-accepted, and the four tabs drifting apart. |
| `test_env.py` | The Environment box going back to being a silent gate — it must open itself when unconfigured, validate progressively, and mark the field that is actually wrong. |
| `test_fail.py` | Error messages that cry wolf. 14 real failure signatures must be recognised **and 7 ordinary log lines must not be** — a bare `401` matches every PlanetScope scene from 1 April (`20240401_…`). |
| `test_pick.py` | The map picker's CRS transform (round-trips through UTM 7N to six decimals) and the pasted `lat, lon` pair, which a validator used to swallow silently. |
| `test_defaults.py` | Shipping defaults that cannot answer the question: the change rasters must be on, `--seasonal` must reach the CLI, and a future or mid-winter window must warn. |
| `test_style.py` | Change rasters loading as grey. Filename→product mapping (including `dndsi` vs `dndvi`), ascending ramp stops, a fully transparent zero, and the **dBright ramp staying exactly `-0.30 / -0.15 / -0.05`** — it is validated, do not drift it. |
| `test_verdict.py` | The volume cross-check losing its meaning, and the CSV letting the three volumes share a column. |
| `test_fit.py` | Reading `v_best` blindly. Under the `ddem` fit it holds the **net** volume, so treating it as the Larsen estimate compares a seismic inversion against a near-zero number. |
| `test_dataloss.py` | The two operations that can destroy work: `Write to layer` applying to every row, and an export overwriting a figure that was already shared. |
| `test_theme_and_limits.py` | Colours that vanish and grids that hang. **Recomputes every contrast ratio** against both QGIS themes (no single colour can clear 4.5:1 on both — the constraints are disjoint), and checks the grid guard admits a real 17 km AOI while refusing 50 km at 2 m. |
| `test_footprint_pairing.py` | Fusing two rasters that describe different ground. The overlap test used to be one-directional, so a 20 km optical tile **inside** a 90 km SAR scene scored a perfect 100% and was auto-paired — pins the symmetric `min()` match that scores it 5%, and the extent fix that stops the coarser input from setting the output footprint. Real Mt Logan bboxes. |
| `test_retired_defaults.py` | An old default frozen into a project forever. Every widget is saved, untouched ones included, so the St. Elias project kept restoring Max cloud 80 / "Cover the exact epicentre" / "Standard quality only" a day after those defaults changed — together they emptied its post-event list. Pins the one-time swap to AOI overlap + test quality + no cap in a pre-v2 project, **written into the project at once**, and that a choice made after it (or any value that was never a default) is kept. Seeded with the strings that project file holds. |
| `test_planet_keeps_renders.py` | A Planet preview taking downloaded scenes off the map. The free tile preview and the paid SR render shared one layer list and each cleared it, so Preview on map removed renders you had ordered — and every render of an event wrote the same `<event>_<side>_planet_<tone>.tif`, so a kept layer would have shown the next scene's pixels anyway. Drives the real `_render_picks` / `_on_detail_done` on real GeoTIFFs: a preview replaces only tiles, a render of other scenes adds beside the last, a re-render of the same file replaces that one layer, a failed render removes nothing. Also pins `run_single`'s per-scene, per-product file names. |
| `test_planet_spec_fallback.py` | A few seconds of Planet lag failing the whole search. The SDK fetches its bundle spec once per process with one bare 5 s request tried twice; on 2026-09-25 13:31 that failed and the tab listed nothing, while the same search minutes later found 60 pre / 13 post. Pins the 15 s tries, the saved copy in the order cache and the fallback to it, the SDK's own error when there is no copy, and `failed` in `search.json`. Offline (httpx faked); re-execs in the venv for the Planet SDK. |
| `test_results_follow_project.py` | A project's results scattered in a folder shared by every other project. `dock.results_dir()` is the saved project's own folder, beside its `.qgz`; checks the real `--out` of the Sentinel-2 run, the PlanetScope and SAR searches, and the SAR, Fusion and 3D output folders all land under it, that Save As and opening another project move it, that an unsaved project falls back to the Environment folder, and that the RGI glacier archive stays shared. Also fails if any tab reads `out_edit` itself again instead of asking the dock. |
| `test_planet_search_failed.py` | A failed search reading as an empty archive. "Found 0 pre / 0 post" after a search that never ran sends you off loosening filters that were never applied. Drives the real `_on_search_done`: a failed search must warn with the reason, and "Found 0" is kept for a search that actually ran. |
| `test_toa_dehaze.py` | Planet TOA renders going grey at low sun. The TOA path left path radiance under every band (blue p0.1 0.04-0.16), `_balance_cast` multiplied it by up to 1.74x, and the darkest shadow in 2024-09-11 Denali rendered at DN ~90 — the HDR lift's steep part fell below every pixel. Pins the per-band floor coming off BEFORE the balance, the shadow toe, snow spread untouched, an all-snow frame (no dark object) left bit-identical, and `_open_scene` actually running it; checks the real 2024 scene (0.27 -> <0.12) when it is in the order cache. Re-execs in the venv. |

## Adding one

Copy any existing file's header — it computes `ROOT`, `PKG` and `PLUG` from
`__file__`, so there are no absolute paths. Print a summary line and `assert`;
`run_all.sh` treats a non-zero exit as failure and shows the last dozen lines.

Two habits worth keeping, because both caught real bugs here:

* **Assert the negative.** Every check that something is detected should be
  paired with strings that must *not* trigger it.
* **Verify advice.** If the code suggests a number to the user, feed that number
  back through the same function. Two suggestions here were off by one cell and
  said so only when tested.
