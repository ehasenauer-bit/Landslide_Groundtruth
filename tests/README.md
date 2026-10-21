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
| `test_pick.py` | The map picker's CRS transform (round-trips through UTM 7N to six decimals) and the pasted `lat, lon` pair, which a validator used to swallow silently — pasted into **either** box, with hemisphere letters or lon-first (WKT/GeoJSON) order, while three numbers or degrees-minutes are left alone, and a pair being **typed** waits for Enter instead of splitting at "60.5, -1". `test_plugin_loads.py` pastes into every tab's pair. |
| `test_defaults.py` | Shipping defaults that cannot answer the question: the change rasters must be on, the dock must not shift the pre window to last summer (that box was removed), and a future or mid-winter window must warn. |
| `test_style.py` | Change rasters loading as grey. Filename→product mapping (including `dndsi` vs `dndvi`), ascending ramp stops, a fully transparent zero, and the **dBright ramp staying exactly `-0.30 / -0.15 / -0.05`** — it is validated, do not drift it. |
| `test_verdict.py` | The volume cross-check losing its meaning, and the CSV letting the three volumes share a column. |
| `test_fit.py` | Reading `v_best` blindly. Under the `ddem` fit it holds the **net** volume, so treating it as the Larsen estimate compares a seismic inversion against a near-zero number. |
| `test_dataloss.py` | The two operations that can destroy work: `Write to layer` applying to every row, and an export overwriting a figure that was already shared. |
| `test_theme_and_limits.py` | Colours that vanish and grids that hang. **Recomputes every contrast ratio** against both QGIS themes (no single colour can clear 4.5:1 on both — the constraints are disjoint), and checks the grid guard admits a real 17 km AOI while refusing 50 km at 2 m. |
| `test_footprint_pairing.py` | Fusing two rasters that describe different ground. The overlap test used to be one-directional, so a 20 km optical tile **inside** a 90 km SAR scene scored a perfect 100% and was auto-paired — pins the symmetric `min()` match that scores it 5%, and the extent fix that stops the coarser input from setting the output footprint. Real Mt Logan bboxes. |
| `test_retired_defaults.py` | An old default frozen into a project forever. Every widget is saved, untouched ones included, so the St. Elias project kept restoring Max cloud 80 / "Cover the exact epicentre" / "Standard quality only" a day after those defaults changed — together they emptied its post-event list. Pins the one-time swap to test quality + no cap in a pre-v2 project (the epicentre coverage is kept: it is the default again since 2026-10-21), **written into the project at once**, and that a choice made after it (or any value that was never a default) is kept. Seeded with the strings that project file holds. |
| `test_planet_keeps_renders.py` | A Planet preview taking downloaded scenes off the map. The free tile preview and the paid SR render shared one layer list and each cleared it, so Preview on map removed renders you had ordered — and every render of an event wrote the same `<event>_<side>_planet_<tone>.tif`, so a kept layer would have shown the next scene's pixels anyway. Drives the real `_render_picks` / `_on_detail_done` on real GeoTIFFs: a preview replaces only tiles, a render of other scenes adds beside the last, a re-render of the same file replaces that one layer, a failed render removes nothing. Also pins `run_single`'s per-scene, per-product file names. |
| `test_planet_spec_fallback.py` | A few seconds of Planet lag failing the whole search. The SDK fetches its bundle spec once per process with one bare 5 s request tried twice; on 2026-09-25 13:31 that failed and the tab listed nothing, while the same search minutes later found 60 pre / 13 post. Pins the 15 s tries, the saved copy in the order cache and the fallback to it, the SDK's own error when there is no copy, and `failed` in `search.json`. Offline (httpx faked); re-execs in the venv for the Planet SDK. |
| `test_results_follow_project.py` | A project's results scattered in a folder shared by every other project. `dock.results_dir()` is the saved project's own folder, beside its `.qgz`; checks the real `--out` of the Sentinel-2 run, the PlanetScope and SAR searches, and the SAR, Fusion and 3D output folders all land under it, that Save As and opening another project move it, that an unsaved project falls back to the Environment folder, and that the RGI glacier archive stays shared. Also fails if any tab reads `out_edit` itself again instead of asking the dock. |
| `test_planet_search_failed.py` | A failed search reading as an empty archive. "Found 0 pre / 0 post" after a search that never ran sends you off loosening filters that were never applied. Drives the real `_on_search_done`: a failed search must warn with the reason, and "Found 0" is kept for a search that actually ran. |
| `test_toa_dehaze.py` | Planet TOA renders going grey at low sun. The TOA path left path radiance under every band (blue p0.1 0.04-0.16), `_balance_cast` multiplied it by up to 1.74x, and the darkest shadow in 2024-09-11 Denali rendered at DN ~90 — the HDR lift's steep part fell below every pixel. Pins the per-band floor coming off BEFORE the balance, the shadow toe, snow spread untouched, an all-snow frame (no dark object) left bit-identical, and `_open_scene` actually running it; checks the real 2024 scene (0.27 -> <0.12) when it is in the order cache. Re-execs in the venv. |
| `test_planet_key_not_saved.py` | The Planet API key landing in a saved project. The tile preview's url used to end in `?api_key=…`, and QGIS saves every layer source, so 22 of the 61 projects on the shared drive held the key in plain text. Planet never needed it there (the tile hash is the credential; tiles came back byte-identical with no key, a bogus key and the real one). Drives the real Preview on map chain with only the network faked, checks the hash POST still sends the key, then searches every member of the saved `.qgz` for it, raw and as a Basic token — and the same search must flag an old-style keyed layer. Also runs `strip_planet_keys.py` on one: dry run changes nothing, the backup is byte-identical and private, the key is gone, another vendor's key stays (its basemap needs it), QGIS opens the result in the plugin's new form, a second run is a no-op, and nothing it prints holds the key. |
| `test_planet_password_not_saved.py` | The Planet account password sitting in plain text in QGIS3.ini. A successful login used to save `landslide/planet_pass` beside the API key, though the key it recovers is all a later session needs. Runs in a throwaway QGIS profile (`QGIS_CUSTOM_CONFIG_PATH`, checked before anything is written) and drives the real login chain with only the network faked: a password an older build left is removed on load, the field starts empty and is cleared after use, no file in the profile holds the password after a good or a failed sign-in, and the next session still comes back signed in with the account key outranking `PL_API_KEY`. |
| `test_planet_key_in_password_store.py` | The Planet API key in plain text in QGIS3.ini, and the master-password prompt that moving it costs. With a QGIS master password the key is an auth config in the encrypted auth DB (`planet_keystore.py`); without one it stays in QgsSettings, and no master password is ever created for it. A fake `QgsCredentials` counts every prompt: none at startup, one at first use, one between five gallery thumbnails after a cancel, none for Log out, and Log in asks again. Also: an old plain key moves into the store, a locked store at sign-in keeps the key for the session only (never plain text) and drops the older saved one, a crafted `authcfg=planetk` request gets nothing, the key survives a master-password reset, and an erased store under a stale marker creates no master password. Runs in a throwaway profile with QGIS's password helper off, so the real Keychain entry is never read. |
| `test_nisar_search.py` | NISAR showing up where it cannot, or vanishing where it can. The date gate refuses only a window ending before first science (2025-10-17) — the archive is backfilling, so any tighter date would wrongly block a search. An empty side must say *why* (pre-mission, the 2026-07-28 instrument gap, the reprocessing hole, archive latency), granule names must decode to the track and pass the tab pairs on, one row per pass (the frame over the event, the finer 40 MHz mode), and the gallery must get the 10 KB thumbnail rather than the 2.6 MB browse. Offline: CMR stubbed with synthetic records in the real UMM-G layout. |
| `test_nisar_read.py` | A NISAR scene landing in the wrong place. GDAL 3.12 has no NISAR driver, so the geotransform is rebuilt from the file's pixel-centre coordinates and EPSG code; pins the half-pixel, the AOI grid matching the Planetary Computer render's exactly, the product's subswath mask (0, 255) becoming NaN rather than dark ground, the cached window being reused at a new Pixel size, strided reads for big AOIs, the display stretch, Earthdata credentials scoped to the granule's host, and the look geometry from the granule's `radarGrid` cube — the LOS read as local east/north, not along the EPSG:3413 grid's axes (~108° apart at 153°W: 241° vs 133°), bilinear at the AOI centre on the 2 km height layer (one HDF5 chunk per layer: all 21 cost 24 s a scene), refused when it contradicts `lookDirection`, and cached with the window under a new key. Writes a synthetic GCOV-shaped HDF5 (netCDF-4) and reads it with the HDF5 driver, the same path as the remote 6 GB file. |
| `test_sar_two_radars.py` | Sentinel-1 and NISAR mixed into one pair. Both number their tracks from 1 and Iliamna is imaged by both missions' "track 150" — pairing on track alone stars a NISAR after-scene against a Sentinel-1 before-scene as "same track". Lifts `_default_picks`, `_cd_pick` and friends out of `sar_tab.py`: one ★ pair per radar in every Pair-by mode, change detection drawing from the Radar combo's sensor only (ticks of the other ignored, and logged), and pre-NISAR events unchanged. Also the event-bracket summary. |
| `test_nisar_terrain.py` | The layover fade and the merge's shadow/layover masks put on the wrong slopes for NISAR. It looks LEFT — an ascending pass at Iliamna looks 241° and lays over EAST-facing slopes, where Sentinel-1 ascending lays over west-facing ones — so the Sentinel-1 orbit model cannot be used. Lifts the tab's `_nisar_*_done`, `_terrain_masks`, `_write_change_with_fade` and `_apply_layover_amplitude` out of `sar_tab.py`: the read task's measured geometry reaching the candidate / post role without mutating the search result, the fade and masks then matching `layover_dim` for that look (east face, not west), a scene without geometry skipped with the reason logged, Sentinel-1 calls unchanged, and the mask cache keyed on the look. |
| `test_outline_saved.py` | An afternoon's digitizing living in a memory layer. "New scar layer" makes a scratch layer, so closing QGIS without Export > Save Features As… loses the outline and the project reopens with nothing to measure. Drives the real **New total area** / **New source area (best)** buttons: each writes a GeoPackage in the project's own folder, names it for its role so a reopened project re-matches the drop-down, pins that role, and a second press writes a new file rather than over the first. Also pins the refusal when there is nowhere to save — an outline quietly dropped in a temp dir is the scratch layer again. And the colour convention: total a **red** outline, best source **blue**, no fill, stored in the GeoPackage itself; a tab-made scratch layer is amber until it gets one of those roles and then follows it, while a layer the tab did not make is never restyled. Every created outline carries an `area_km2` field QGIS recomputes on each reshape, checked against `_measure_area`. |

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
