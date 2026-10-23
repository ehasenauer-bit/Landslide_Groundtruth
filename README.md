# Landslide imagery-gathering pipeline

Takes a predicted location + time (a seismic detection — lat/lon + UTC time) and
pulls **before/after satellite imagery** for the event —
**PlanetScope (~3 m) first**, falling back to Sentinel-2 (~10 m) then Landsat
(~30 m) — then exports a QGIS review package with everything needed to spot and
digitize the scar by eye.

Python does the **imagery gathering**; it does not auto-delineate the scar. You
confirm the slide and digitize the polygon in QGIS. Once you have, the plugin's
**Volume from area** tab measures that polygon and converts its area to a volume
via the Larsen et al. (2010) scaling — see [Volume from a digitized
scar](#volume-from-a-digitized-scar).

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
consume your account quota — but only once per scene: every order is cached and
recallable for free afterwards (see [Ordering a scene once](#ordering-a-scene-once-quota)).

## Run

One event at a time, from its location + time. Use the **QGIS plugin** (click the
point on the map — see *Interactive use / QGIS plugin* below) or `run_single.py`
on the command line:

```bash
# free coverage preview only (no Planet orders, no downloads):
python run_single.py --lat 60.465 --lon -142.10 --datetime "2023-08-07 11:19" \
    --radius-km 3 --prefer auto --out out/interactive --search-only

# fetch the imagery and write the review package:
python run_single.py --lat 60.465 --lon -142.10 --datetime "2023-08-07 11:19" \
    --radius-km 3 --pre-days 60 --post-days 30 --prefer auto --out out/interactive
```

Key flags: `--prefer {auto,planet,s2,landsat}` (source priority, default `auto`),
`--pre-days`/`--post-days` (imagery windows), `--radius-km` (imagery
search/footprint radius around the point, default 3),
`--auto-window` (tightest before/after gap: use only
the single clear scene nearest the event on each side), `--search-only` (free
dry-run: list candidate scenes to `<out>/search.json`, no orders/downloads).

Two flags control which scenes the search will even consider (raise these if a
manual search in **Planet Explorer** turns up closer/clearer scenes than the tool
finds):

- `--max-cloud PCT` — maximum **whole-scene** cloud cover to consider. `100` means
  **no cap at all**: every acquisition in the window is listed, clouds and all, so
  you judge the scenes by eye instead of by metadata. That is what `--search-only`
  does when the flag is omitted; a *Run* without the flag still caps (PlanetScope 80,
  Sentinel-2/Landsat 60, either one 20 with `--auto-window`), since it picks scenes
  for you rather than showing them to you.
  The metric is scene-wide over the full satellite strip/tile, **not** your AOI — so a
  cap only ever throws away scenes that might have been clear over your point, which is
  why the interactive default is not to cap. It also only decides *which* scenes are
  used: for Sentinel-2/Landsat the chosen scenes are composited **as acquired**, with no
  per-pixel cloud masking, so the downloaded layers show the cloud that was there
  instead of holes where a mask fired (PlanetScope still UDM2-masks in its own tab).
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

Every imagery layer's filename carries the **acquisition date** of the scene behind
it (`<pre>` / `<post>` below — e.g. `_pre_2023-07-28_rgb.tif`; a span like
`2023-07-18_to_2023-07-28` when several scenes were composited into that side).
QGIS names a loaded layer after its file, so the legend always says when the
imagery is from.

- `qgis_packages/<event>_pre_<pre>_rgb.tif`, `_post_<post>_rgb.tif` — true-colour
  before/after (`true_color`).
- `qgis_packages/<event>_pre_<pre>_highlight.tif`, `_post_<post>_highlight.tif` —
  "Highlight Optimized Natural Color" before/after (`highlight_natural`): a
  cube-root tone curve, `cbrt(0.6 × reflectance)`, on the true-colour bands — the
  same look the Copernicus Browser offers. Lifts shadow detail and tames blown-out
  snow/cloud so one stretch reads across the whole scene. (Sentinel Hub custom
  script by Marko Repše, CC BY-SA 4.0; it's a rendering of the same bands, not an
  extra download.)
- `qgis_packages/<event>_pre_<pre>_falsecolor.tif`, `_post_<post>_falsecolor.tif` —
  NIR-red-green before/after (vegetation = bright red; fresh bare scar reads dark)
  (`false_color`).
- `qgis_packages/<event>_pre_<pre>_ndvi.tif`, `_post_<post>_ndvi.tif` — raw NDVI
  before/after (`ndvi`).
- `qgis_packages/<event>_dndvi_<pre>_vs_<post>.tif`,
  `_dbright_<pre>_vs_<post>.tif` — the two change rasters (`dndvi`, `dbright`);
  both dates are in the name, since a change raster is only meaningful as a pair.
- `qgis_packages/<event>_point.gpkg` — the predicted (seismic) epicentre.
- `qgis_packages/<event>_metadata.json` — sensor + scene ids/dates used, layer list.

## QGIS review step (the manual hinge)

For each event, open in QGIS (filenames carry the acquisition date, as above):
1. Load the `_pre_…_rgb.tif` / `_post_…_rgb.tif` pair; use the **Swipe** tool
   (or flicker) to confirm it's a landslide and not a clearcut, burn, flood scar,
   cloud shadow, or new snow — these are the common false positives.
2. Add the `_pre_…_falsecolor.tif` / `_post_…_falsecolor.tif` pair to read
   vegetation change directly (healthy veg is bright red; a fresh scar goes dark).
3. Load `_dndvi_….tif` (vegetation loss) and `_dbright_….tif`
   (brightness/albedo rise) — agreement between the two confirms a scar.
4. Load `<event>_point.gpkg` (the predicted epicentre), then digitize the scar
   polygon over the post imagery. `$area` in the field calculator gives the area;
   the plugin's **Volume from area** tab gives area *and* volume (below).

Useful plugins: **STAC API Browser** (load the same imagery directly), **Planet
Explorer** (stream PlanetScope, order clips), and **Profile Tool** (terrain).

## Volume from a digitized scar

The plugin's **Volume from area** tab turns the outlines you digitize into two
independent volumes, side by side, from one **Measure**:

| Fit | Converts | Needs | Range from |
|-----|----------|-------|------------|
| **Area × thickness** (Toney et al. 2021) | the **total** outline (source + track + deposit) | a mean deposit thickness | the low/high thickness |
| **Source scar** (Larsen et al. 2010, Table S1, `larsen_BR_volume.py`) | the **best source** outline — the evacuated scar alone | what failed: bedrock or soil | the published fit spread, widened by source low/high |

Neither is the headline: each keeps its own fields in the row, the CSV and the
written attributes, and the verdict sets each against the seismic volume. The tab
reads top to bottom in the order the work is done:

1. **Outlines.** Press **New** beside *Total area*, draw the whole affected area
   (click each vertex, right-click to finish, toggle editing off to save), then
   **New** beside *Source (best)* for the scar. Each New writes a GeoPackage beside
   the project's `.qgz`, named for its role (`<slide>_total_area.gpkg`, …), assigns
   it and arms QGIS's Add Polygon tool; **Draw** adds another polygon to the
   assigned layer. Total is a red outline, source blue. A conservative/generous
   pair of source outlines goes under the collapsed *Source low / high* box — the
   source-scar fit reads them as a ±2σ area span. The line under the pickers shows
   every assigned area live; swapped roles, or a total smaller than a source, are
   refused before Measure rather than measured.

   **Layers named for their role are picked up automatically**, so a project that
   already carries the outlines comes up ready to measure:

   | Layer name | Fills |
   |------------|-------|
   | `Total Area`, `Total Landslide Area`, `Landslide Area`, `Total Extent` | Total area |
   | `Source Area (best)`, or a plain `Source Area` | Source (best) |
   | `Source Area (low)` | Source (low) |
   | `Source Area (high)` | Source (high) |

   Capitalisation, punctuation and bracketing don't matter, and the synonyms
   *min/minimum/lower/conservative* and *max/maximum/higher/generous* match. Anything
   you set yourself is left alone. A role layer holding several polygons is measured
   by its largest — they are alternative attempts at one outline, not parts of it.
2. **Settings.** The *Deposit thickness* setting fills mean/low/high from published
   values (near date = imagery soon after the event, snow and ice still in the
   deposit; far date = after a melt season); edit any number and it reads Custom.
   *What failed?* (bedrock/soil) is read by the source-scar fit only and moves it a
   long way — choose it from what actually failed, not the surrounding cover.
3. **Measure** runs both fits and records the slide's row — no separate add step.
   Measuring the same slide name again replaces its row (and keeps its centerline);
   a new name adds a row. If one fit cannot run — no source outline, say — the other
   still does and the Volumes box says why.
4. **Volumes** shows both, at three significant figures, plus the implied mean
   depth: the source-scar volume ÷ the **total** outline area, i.e. the mean
   thickness over the whole footprint that the Larsen volume implies. It compares
   directly with the deposit thickness under Settings — several times it means the
   two fits disagree, tens of metres means the outline or the material is wrong. Warnings about this measurement show here.
5. **Centerline & elevation drop.** **Draw centerline** derives the medial-axis
   spine of the total outline into `Landslide centerlines`, left in edit mode to
   trim and **Re-measure**; with a DEM assigned it also reads the crown-to-toe fall
   height and travel angle. Reference only.
6. **Verdict.** Record the call and your initials; the summary lists the seismic
   volume and each measured one with its own ×ratio and agree/marginal/disagree.
   **Write to layer** stamps everything — `vthk_m3` / `vthk_lo_m3` / `vthk_hi_m3`
   (area × thickness), `vsrc_m3` / `vsrc_lo_m3` / `vsrc_hi_m3` (source scar), the
   thickness, `total_m2`, `src_m2` …, length and drop — onto the **total** outline's
   own feature. Save the outline layer before writing back: adding fields needs a
   clean edit buffer, and QGIS renumbers features on save. **Copy results** puts the
   table on the clipboard as TSV.

The results table (with **Export CSV**) and the log sit collapsed at the bottom.
The explanations are behind the **? Help** toggle in the tab's header, remembered
per user; tooltips carry the detail either way.

Areas are ellipsoidal plan-view area — the same quantity `$area` reports and the
same one Larsen et al. measured from imagery, so the published coefficients stay
valid. The Larsen arithmetic is read from the project's own `larsen_BR_volume.py`,
so editing the coefficients there changes what the tab reports; the log names the
implementation that ran, and the tab falls back to a built-in copy when the
project dir isn't configured.

**No longer in the tab (2026-10-23):** the elevation-change (`∫Δh`) fit and DEM
differencing, parked verbatim in `parked/ddem/` with the recipe for putting them
back; and the total-area Larsen fit, whose coefficients ship empty
(`LARSEN_TOTAL` in `volume_calc.py` / `larsen_BR_volume.py` is still read by
`volume_calc.volume_source(fit="total")`).

## Module map

| file | role |
|------|------|
| `planet_imagery.py` | **primary source.** Planet Data API search + Orders API clip/download of PlanetScope (~3 m) surface reflectance; UDM2 cloud-masked median composite; same return contract as `imagery.py`. Checks the order cache before ordering, and `recall_preview` loads earlier orders back for free |
| `planet_cache.py` | shared cache + ledger of Planet orders already paid for, so a scene is ordered once. Pure stdlib (the QGIS plugin imports it directly) — see [Ordering a scene once](#ordering-a-scene-once-quota) |
| `imagery.py` | the STAC sources (Sentinel-2, Landsat): median composites of the chosen scenes **as acquired** — only fill/nodata is dropped, never cloud — plus dNDVI, dNDSI, dBrightness (albedo change); falls back S2→Landsat |
| `review_package.py` | export the per-event QGIS review package (true/false-colour pre+post, dNDVI, dBrightness, predicted point) + `metadata.json` |
| `run_groundtruth.py` | library module: `process_one(ev, args)`, the per-event pipeline (fetch imagery → export review package). Imported by `run_single.py`; no CLI of its own |
| `run_single.py` | single-event entry point/CLI: takes `--lat/--lon/--datetime` directly, calls `run_groundtruth.process_one`, writes a `result.json` (or `search.json` with `--search-only`); used by the QGIS plugin |
| `larsen_BR_volume.py` | `volume_source(A_best, A_low, A_high, type)` — landslide volume from source-scar area, Larsen et al. (2010) `V = αA^γ` for bedrock or soil, with the uncertainty propagated in log₁₀ space. Loaded directly by the plugin's Volume tab (numpy only, no I/O) — see [Volume from a digitized scar](#volume-from-a-digitized-scar) |
| `qgis_plugin/` | QGIS dock-widget plugin (pick location on the map, set date + pre/post-day sliders, choose source) that runs `run_single.py` in the venv as a background process and loads the result layers — see `qgis_plugin/README.md` |

## Interactive use / QGIS plugin

The **QGIS plugin** in `qgis_plugin/` gives a dock panel: click the
event location on the map, set the date and the *days before/after* sliders, pick
an imagery source, and the results load straight into your project. The plugin
keeps the heavy dependencies in this venv (it shells out to `run_single.py`),
so nothing extra needs installing into QGIS. Install/usage: `qgis_plugin/README.md`.

## Important caveats

- **Resolution vs. slide size.** With Planet authenticated, PlanetScope (~3 m)
  is used first and resolves most small slides. When Planet has no coverage and
  it falls back to Sentinel-2 (~10 m) / Landsat (~30 m), slides smaller than
  roughly 50 m across can only be confirmed present/absent. The `sensor` field
  in each `metadata.json` (and the plugin's **SATELLITE USED** banner) tells you
  which source was used.
- **Snow and clouds.** Alaska coastal/winter events are the hard case. Widen
  `--post-days` to reach the next clear/snow-free window,
  and always do the QGIS visual check.
- **`--auto-window` and scene coverage.** Auto-window uses the single scene
  nearest the event date on each side. A Sentinel-2 scene can be returned by the
  search (its tile footprint intersects the AOI) yet leave the AOI in that
  acquisition's diagonal nodata gap. The pipeline detects an empty composite and
  falls back (S2→Landsat) or reports `no_imagery` rather than emitting a blank
  package — if that happens, drop `--auto-window` so it composites several scenes.
- **Coordinate sanity.** Double-check the input lat/lon (a dropped minus sign on
  longitude is the classic error) before trusting the imagery footprint — the
  plugin's *Pick location on map* avoids this by reading the click directly.

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
  order is ready); the pre and post orders are submitted together and processed
  concurrently, so the wait is ~one order's, not two.
- The `analytic_sr_udm2` bundle requires surface-reflectance access on your
  plan; the code sets a non-SR (`analytic_udm2`) fallback bundle automatically.

### Rendering the SR detail preview (tone + stretch)

Planet's free tiles are pre-rendered 8-bit RGB that has already clipped bright
terrain to flat white, so **Render detail** orders the raw surface-reflectance
bundle and renders it here instead. Three curves, `--planet-tone`:

- **`knee`** (default) — highlight rolloff. A plain linear stretch up to
  `KNEE × WHITE` = 0.165 reflectance, so midtones and shadows are arithmetically
  identical to an unstretched render; brighter pixels are compressed along an
  asymptote that never clips. Right when the scar is on terrain and ice is context.
- **`natural`** — `cbrt(0.6 × reflectance)`, the Copernicus Browser look. Even
  detail across the whole range, at the cost of global contrast.
- **`linear`** (**None** in the plugin) — every shaping switched off: a plain
  black/white stretch of the reflectance itself, no rolloff, cube root,
  desaturation, or S-curve. Bright ice clips to flat white exactly as a naive
  stretch would — the un-toned reference. Honours the manual `--planet-white`/
  `--planet-black`, but never auto-fits.

**S-curve contrast.** `knee` and `natural` get a gentle S-curve nudge
(`--planet-contrast`, default `CONTRAST` = 1.15) that pins both ends and so cannot
clip. The plugin's **Contrast** checkbox (on by default) turns it off by passing
`--planet-contrast 1.0`; `linear` never adds contrast.

**Auto-stretch.** The knee curve's fixed 0–0.165 linear zone assumes the frame
contains terrain. On an AOI that is *entirely* snow/ice it contains nothing: a
measured 2026-06-26 clip ran p1 0.45 / median 0.84 / p90 0.93 reflectance with **no
pixels at all** below 0.165, so the whole histogram landed in the shoulder, 88% of
the image came out within 5 DN of white, and the highlight desaturation (onset 0.5
reflectance) stripped the hue off 97% of what was left. So the knee path now fits
its black and white points to the scene — but only when the scene has no dark end to
lose (`review_package.auto_stretch`):

| fraction of AOI below 0.165 reflectance | what happens |
|---|---|
| ≥ 5% (terrain, with or without ice in frame) | **left alone, byte for byte** |
| 0.5%–5% | blended between the two |
| ≤ 0.5% (an all-ice frame) | the scene's own p0.1–p99 becomes the stretch |

On the measured clip that is 15× the standard deviation and 0% of pixels within
5 DN of white. Real scenes separate cleanly either side of the threshold — icefield
clips here measure 0–3.5%, terrain clips 21–65% — so the gate rarely has to
interpolate. Safety properties, all deliberate:

- Pre and post are measured **together**, so a swipe comparison can't read a
  per-side stretch difference as change on the ground.
- The black point comes from p0.1 (then 10% lower), so it clips well under 0.1% of
  the frame. A dark scar larger than that keeps its interior; a smaller one
  flattens toward black, still the most conspicuous thing in a bright frame.
- A derived white point is capped at 1.0 reflectance — above that Planet's
  atmospheric correction is overshooting, not measuring ground (a low-sun
  2026-01-31 clip here hit p99 = 3.5), and letting it set white would crush the
  real scene into the bottom of the ramp.
- Valid pixels are floored at DN 1 whenever a black point is in play, so a clipped
  scar can't collide with `nodata = 0` and punch a transparent hole in the layer.
- The desaturation onset rises with the derived white point, so the artefact
  suppression still only touches physically impossible reflectance.

Overrides: `--planet-no-auto-stretch` forces the fixed 0–0.30 stretch back (the
reason to want it is reading *inside* a small clipped scar), and
`--planet-black` / `--planet-white` set the stretch by hand, which disables auto on
its own. In the plugin these are the **Auto-stretch** checkbox and the **Manual
stretch** black/white boxes next to the tone combo; all of them are free to change
after a render — hit **Re-tone**, which re-renders the clips already on disk. The
stretch actually applied is recorded in `render.json` and appears in the layer name.

### Ordering a scene once (quota)

Quota is charged when an order is **created**, not when its files are fetched.
`planet_cache.py` exploits that: every order is downloaded into a shared cache and
recorded in a ledger, and every entry point checks the ledger before ordering. So
looking at an event a second time is free — in this project or any other.

```
~/.cache/landslide_planet/       # or $LANDSLIDE_PLANET_CACHE
    ledger.json                  # event / AOI / scene-ids -> order id
    orders/<order_id>/           # the order tree as Planet delivered it
```

The cache deliberately lives outside the project: a different `--out`, a second
project on the same event, or a fresh clone all share one pool, and the GeoTIFFs
stay out of Drive sync.

- **`--planet-recall`** — put imagery already ordered for this event back on the
  map. No search, no order, no quota; no network at all when the clips are still on
  disk. If they were deleted, the order is downloaded again, which is also free.
  This is the plugin's **Recall order (free)** button.
- **`--planet-list-orders`** — what has this account already paid for near here?
  Writes `<out>/planet_orders.json` and backs the plugin's **Cached orders** picker.
  Pure ledger read. The PlanetScope tab refreshes it after every search, so you can
  see what you already own *before* spending quota.
- **`--planet-render`** now reuses a cached order whenever it already contains the
  requested scene ids over a wide enough AOI, and only orders the rest.
  `--planet-force-order` overrides that.
- **`--planet-retone`** reads clips from the cache as well as from the older
  per-project `<out>/planet_render/<event>/` trees.

Orders downloaded before the cache existed (under `out/planet_cache/<event>/` or
`out/planet_render/<event>/`) are adopted into the ledger automatically, **in
place** — nothing is moved or deleted — so imagery you already paid for is
recallable without re-downloading it.

Matching is geometric, not by name: each order's delivered clip footprint is read
from its own metadata, and a cached order counts as "this event" when that footprint
overlaps the AOI on screen. The default event id comes from the event *timestamp*
and isn't location-unique, so it's only a fallback for orders whose delivery carried
no footprint.
