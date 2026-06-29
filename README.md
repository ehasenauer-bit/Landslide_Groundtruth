# Landslide imagery-gathering pipeline

Reads a predicted location + time (from `LandslideInventory.xlsx` or the command
line) and pulls **before/after satellite imagery** for each detected landslide —
**PlanetScope (~3 m) first**, falling back to Sentinel-2 (~10 m) then Landsat
(~30 m) — then exports a QGIS review package with everything needed to spot and
digitize the scar by eye.

This is the **imagery-gathering stage only**. It does not auto-delineate the
scar or estimate volume: Python finds and prepares the right before/after
imagery; you confirm the slide and digitize the polygon in QGIS.

## Install

Python 3.11+ (developed and tested on 3.14).

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` documents every dependency (with the known-good version) and
the one optional credential (`PL_API_KEY`). No API keys are needed for
Sentinel-2/Landsat — Microsoft Planetary Computer signs requests anonymously via
`planetary_computer.sign_inplace`.

**PlanetScope** is the primary source and needs a one-time login:

```bash
planet auth login          # OAuth browser flow; stores a token under ~/.planet/
```

(Or set `PL_API_KEY` from your Planet account settings.) The password is never
read by this code. If you skip this, the pipeline logs `[planet] unavailable …`
and automatically falls back to Sentinel-2/Landsat, so it still runs. Planet
clips are ordered through the Orders API (server-side clip to the event AOI) and
consume your account quota; downloads cache under `out/planet_cache/<event>/`.

## Run

```bash
# see what would be processed, no network:
python run_groundtruth.py --inventory LandslideInventory.xlsx --out out --dry-run

# one event:
python run_groundtruth.py --inventory LandslideInventory.xlsx --out out \
    --event 230914_Denali --post-days 30

# everything flagged for ground-truthing (not yet confirmed in ESEC):
python run_groundtruth.py --inventory LandslideInventory.xlsx --out out --limit 0
```

Key flags: `--prefer {auto,planet,s2,landsat}` (source priority, default `auto`),
`--pre-days`/`--post-days` (imagery windows), `--radius-scale` (widen the
downloaded imagery footprint relative to the search radius, default 1.5),
`--seasonal` (winter event — use the prior-year summer as the pre window so snow
doesn't swamp the signal), `--auto-window` (tightest before/after gap: use only
the single clear scene nearest the event on each side).

Two flags control which scenes the search will even consider (raise these if a
manual search in **Planet Explorer** turns up closer/clearer scenes than the tool
finds):

- `--max-cloud PCT` — maximum **whole-scene** cloud cover to consider (default 80,
  or 20 with `--auto-window`). This is a scene-wide metric over the full satellite
  strip/tile, **not** your AOI; per-pixel cloud masking (UDM2 for PlanetScope, SCL/QA
  for Sentinel-2/Landsat) still runs afterwards. A high cap therefore surfaces scenes
  that are clear over your point but cloudy elsewhere — exactly the scenes Planet
  Explorer shows you. Lower it to only consider mostly-clear scenes.
- `--coverage {aoi,point}` — PlanetScope coverage requirement (default `aoi`). `aoi`
  accepts any scene overlapping the search box, like Planet Explorer, so a strip that
  covers only part of the AOI near the event date still counts; `point` requires the
  scene footprint to contain the exact epicentre (stricter — can miss the nearest
  scenes). Sentinel-2/Landsat ignore this.
- `--quality {standard,any}` — PlanetScope quality (default `standard`). Planet
  publishes some acquisitions as `test` quality (looser geo/radiometric calibration);
  `standard` skips them, `any` includes them. Near a *fresh* event the nearest and
  clearest PlanetScope scenes are frequently test-only — Planet Explorer shows them —
  so `any` is often what recovers a 1-day-after scene for the visual review. Eyeball
  test scenes before trusting NDVI/reflectance quantitatively. Sentinel-2/Landsat
  ignore this.

## Two change signals: dNDVI and brightness (reflectivity)

Each event ships with **two** independent change rasters so you can spot the scar
from more than just one signal:

- **dNDVI** — vegetation loss. Scars show a strong negative NDVI change.
- **dBrightness** — pre→post change in broadband surface reflectance (albedo, the
  mean of red/green/blue/NIR). Fresh scars expose bright bare soil and rock, so
  they brighten. This catches sparsely-vegetated rock/scree margins where the
  NDVI drop alone is weak.

Where the two agree is a strong scar signal; brightening without NDVI loss is
often water/sandbar.

## Outputs (`out/`)

Which of these "scenes" get written is selectable — `--scenes true_color,dndvi,…`
on the CLI, or the **Scenes to download** checkboxes in the plugin (default: all).
The predicted-point layer is always written.

- `qgis_packages/<event>_pre_rgb.tif`, `_post_rgb.tif` — true-colour before/after
  (`true_color`).
- `qgis_packages/<event>_pre_highlight.tif`, `_post_highlight.tif` — "Highlight
  Optimized Natural Color" before/after (`highlight_natural`): a cube-root tone
  curve, `cbrt(0.6 × reflectance)`, on the true-colour bands — the same look the
  Copernicus Browser offers. Lifts shadow detail and tames blown-out snow/cloud so
  one stretch reads across the whole scene. (Sentinel Hub custom script by Marko
  Repše, CC BY-SA 4.0; it's a rendering of the same bands, not an extra download.)
- `qgis_packages/<event>_pre_falsecolor.tif`, `_post_falsecolor.tif` — NIR-red-green
  before/after (vegetation = bright red; fresh bare scar reads dark) (`false_color`).
- `qgis_packages/<event>_pre_ndvi.tif`, `_post_ndvi.tif` — raw NDVI before/after (`ndvi`).
- `qgis_packages/<event>_dndvi.tif`, `_dbright.tif` — the two change rasters
  (`dndvi`, `dbright`).
- `qgis_packages/<event>_point.gpkg` — the predicted (seismic) epicentre.
- `qgis_packages/<event>_metadata.json` — sensor + scene ids/dates used, layer list.
- `summary.csv` — sensor + pre/post scene counts for every processed event.

## QGIS review step (the manual hinge)

For each event, open in QGIS:
1. Load `<event>_pre_rgb.tif` and `<event>_post_rgb.tif`; use the **Swipe** tool
   (or flicker) to confirm it's a landslide and not a clearcut, burn, flood scar,
   cloud shadow, or new snow — these are the common false positives.
2. Add `<event>_pre_falsecolor.tif` / `<event>_post_falsecolor.tif` to read
   vegetation change directly (healthy veg is bright red; a fresh scar goes dark).
3. Load `<event>_dndvi.tif` (vegetation loss) and `<event>_dbright.tif`
   (brightness/albedo rise) — agreement between the two confirms a scar.
4. Load `<event>_point.gpkg` (the predicted epicentre), then digitize the scar
   polygon over the post imagery. `$area` in the field calculator gives the area.

Useful plugins: **STAC API Browser** (load the same imagery directly), **Planet
Explorer** (stream PlanetScope, order clips), and **Profile Tool** (terrain).

## Module map

| file | role |
|------|------|
| `inventory.py` | parse the `landslides` sheet; fix Excel serial dates & fractional times; flag coordinate typos; pick best location (ground truth > grid center > grid search) and a search radius from the location errors |
| `planet_imagery.py` | **primary source.** Planet Data API search + Orders API clip/download of PlanetScope (~3 m) surface reflectance; UDM2 cloud-masked median composite; same return contract as `imagery.py` |
| `imagery.py` | tries Planet first (via `planet_imagery`), then STAC: cloud/snow-masked median composites, dNDVI, dBrightness (albedo change); falls back S2→Landsat; `--seasonal` for winter |
| `review_package.py` | export the per-event QGIS review package (true/false-colour pre+post, dNDVI, dBrightness, predicted point) + `metadata.json` |
| `run_groundtruth.py` | batch orchestrator + CLI (reads the inventory, loops events) |
| `run_single.py` | single-event entry point: takes `--lat/--lon/--datetime` directly, reuses `run_groundtruth.process_one`, writes a `result.json`; used by the QGIS plugin |
| `qgis_plugin/` | QGIS dock-widget plugin (pick location on the map, set date + pre/post-day sliders, choose source) that runs `run_single.py` in the venv as a background process and loads the result layers — see `qgis_plugin/README.md` |

## Interactive use / QGIS plugin

Beyond the batch CLI, you can pull imagery for a single ad-hoc location two ways:

```bash
# command line — same pipeline, manual location/time
python run_single.py --lat 60.465 --lon -142.10 --datetime "2023-08-07 11:19" \
    --radius-km 3 --pre-days 60 --post-days 30 --prefer s2 --out out/interactive

# free coverage preview only (no Planet orders, no downloads):
python run_single.py --lat 60.465 --lon -142.10 --datetime "2023-08-07 11:19" \
    --radius-km 3 --prefer auto --out out/interactive --search-only
```

…or via the **QGIS plugin** in `qgis_plugin/`, which gives a dock panel: click the
event location on the map, set the date and the *days before/after* sliders, pick
an imagery source, and the results load straight into your project. The plugin
keeps the heavy dependencies in this venv (it shells out to `run_single.py`),
so nothing extra needs installing into QGIS. Install/usage: `qgis_plugin/README.md`.

## Important caveats

- **Resolution vs. slide size.** With Planet authenticated, PlanetScope (~3 m)
  is used first and resolves most small slides. When Planet has no coverage and
  it falls back to Sentinel-2 (~10 m) / Landsat (~30 m), slides smaller than
  roughly 50 m across can only be confirmed present/absent. The `sensor` column
  in `summary.csv` (and each `metadata.json`) tells you which source was used.
- **Snow and clouds.** Alaska coastal/winter events are the hard case. Use
  `--seasonal`, widen `--post-days` to reach the next clear/snow-free window,
  and always do the QGIS visual check.
- **`--auto-window` and scene coverage.** Auto-window uses the single scene
  nearest the event date on each side. A Sentinel-2 scene can be returned by the
  search (its tile footprint intersects the AOI) yet leave the AOI in that
  acquisition's diagonal nodata gap. The pipeline detects an empty composite and
  falls back (S2→Landsat) or reports `no_imagery` rather than emitting a blank
  package — if that happens, drop `--auto-window` so it composites several scenes.
- **Coordinate QC.** The parser flags suspect coordinates (e.g. a positive
  longitude with a missing minus sign, or a value outside Alaska). Fix these in
  the source sheet before trusting the imagery footprint.

## Planet integration

Implemented in `planet_imagery.py` and wired in as the primary source. Per
window it searches PSScene via the Data API (AOI + date + cloud cover, filtered
to items you can download), orders the N least-cloudy scenes as the
`analytic_sr_udm2` bundle clipped to the event AOI, UDM2 cloud-masks them, and
median-composites onto the event UTM grid — producing the same composite contract
as the STAC sources.

- Authenticate once with `planet auth login` (or `PL_API_KEY`); without it the
  pipeline falls back to Sentinel-2/Landsat automatically.
- Force the source with `--prefer {auto,planet,s2,landsat}` (default `auto`).
  `--prefer planet` skips the STAC fallback (returns no imagery if Planet has
  no coverage), useful when you only want ~3 m results.
- Orders consume account quota and take ~1–5 min each (the SDK polls until the
  order is ready); downloads cache under `out/planet_cache/<event>/`.
- The `analytic_sr_udm2` bundle requires surface-reflectance access on your
  plan; the code sets a non-SR (`analytic_udm2`) fallback bundle automatically.
