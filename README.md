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
`--seasonal` (winter event — use the prior-year summer as the pre window so snow
doesn't swamp the signal), `--auto-window` (tightest before/after gap: use only
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
  The metric is scene-wide over the full satellite strip/tile, **not** your AOI, and
  per-pixel cloud masking (UDM2 for PlanetScope, SCL/QA for Sentinel-2/Landsat) still
  runs afterwards — so a cap only ever throws away scenes that might have been clear
  over your point, which is why the interactive default is not to cap.
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
   polygon over the post imagery. `$area` in the field calculator gives the area;
   the plugin's **Volume from area** tab gives area *and* volume (below).

Useful plugins: **STAC API Browser** (load the same imagery directly), **Planet
Explorer** (stream PlanetScope, order clips), and **Profile Tool** (terrain).

## Volume from a digitized scar

The plugin's **Volume from area** tab turns the polygons you digitize into a
volume, using the Larsen et al. (2010) area–volume scaling `V = αA^γ`
(`larsen_BR_volume.py`). You draw each outline into its own layer and assign
those layers to roles; nothing depends on what happens to be selected on the map.

**The volume is computed from the TOTAL landslide outline** — the whole affected
area, source through runout to deposit. A mapped slide usually carries several
outlines: one total extent, plus two or three interpretations of the source scar
inside it. Only the total is converted. The source outlines are measured and
reported beside it, never run through the relation — it answers a question about
whatever area it is handed, and the source scar is a fraction of what failed, so
feeding it a source outline silently answers a different question and reads low.

1. Under **Scar outlines (draw)**, press **New scar layer** — it creates a
   polygon layer, makes it active, switches it into edit mode and arms QGIS's Add
   Polygon tool, so you can start drawing immediately. Click each vertex,
   right-click to finish. Press it once per outline: the first layer is wired to
   **Total area layer** automatically, since that is the one the volume needs. The
   **Draw into** drop-down only chooses where the drawing buttons digitize — it
   does not decide what gets measured.
2. In **Current measurement**, assign the layers:

   | Drop-down | Holds | Used for |
   |-----------|-------|----------|
   | **Total area layer** | the whole landslide outline | **the volume** |
   | **Source area (best)** | best source-scar interpretation | reported only |
   | **Source area (low)** | conservative source interpretation | reported only |
   | **Source area (high)** | generous source interpretation | reported only |

   Only the total is required. The line under the drop-downs shows every assigned
   area live, so a mis-assignment is visible before you press **Measure**. Swapped
   source roles are refused by name, and so is a total that comes out *smaller*
   than one of the source areas — the total should contain the source, so that
   almost always means the Total area drop-down is pointing at a source outline.

   **Layers named for their role are picked up automatically**, so a project that
   already carries the outlines comes up ready to measure:

   | Layer name | Fills |
   |------------|-------|
   | `Total Area`, `Total Landslide Area`, `Landslide Area`, `Total Extent` | **Total area layer** |
   | `Source Area (low)` | Source area (low) |
   | `Source Area (high)` | Source area (high) |
   | `Source Area (best)`, or a plain `Source Area` | Source area (best) |

   Capitalisation, punctuation and bracketing don't matter — `SOURCE AREA (LOW)`,
   `source_area_low` and `Barry Arm Source Area - low` all match, as do the
   synonyms *min/minimum/lower/conservative* and *max/maximum/higher/generous*.
   Matching is on whole words, so a layer called `lowland` is not mistaken for a
   low estimate, and one layer can never fill two roles. Anything you set (or
   clear) yourself is left alone from then on; a match the tab made for you stays
   open to revision, so loading an explicit `Source Area (best)` later still
   claims that role from a bare `Source Area`. Every automatic assignment is named
   in the log.
   - If a role layer holds **more than one polygon**, its largest is used and the
     others are listed in the log — each role is one interpretation of one
     outline, so several polygons are alternative attempts at it rather than parts
     of it, and adding or dissolving them together would inflate the area.
   - A source outline that isn't inside the total gets a note; the usual cause is
     layers from two different slides assigned together.
   - Because only the total is converted, the volume's range is the **published
     fit uncertainty** — a single outline carries no area uncertainty of its own.
     The source low/high no longer widen it.
3. Pick the **Fit** and **Hillslope material**. Material moves the answer a long
   way — a 60 000 m² bedrock area comes out ~1.3 × 10⁶ m³ against ~2.4 × 10⁵ m³
   for soil — so choose it from what actually failed, not the surrounding cover.
   Fit decides both the calibration and what the outline must be; the orange line
   at the top of the tab always states which:

   | Fit | Outline must be | Status |
   |-----|-----------------|--------|
   | **Source scar** | the evacuated source scar alone | ready — this is the fit `larsen_BR_volume.py` carries |
   | **Total landslide area** | source + runout track + deposit | needs coefficients (see below) |

   Note the tension worth being deliberate about: the tab measures the **total**
   outline, but the only calibration that ships is the **source-scar** one. Running
   the total area through the scar fit reads high. Fill in `LARSEN_TOTAL` below to
   pair the total outline with a total-area calibration.

4. Optionally open **Centerline** and press **Draw centerline** for a slide
   length. It derives the medial-axis spine of the **total** outline — following
   the slide's bends rather than cutting across them, which a bounding box or
   longest chord does not — into a scratch layer left in edit mode, so you can
   trim the ends with the Vertex Tool and **Re-measure**. Length is reported for
   reference; the volume comes from area alone.
5. **Add to results ↓** appends a row and moves on to the next slide. The table
   accumulates across a session; **Export CSV** writes raw unrounded values, and
   **Write to layer** stamps `area_m2` (the total), `src_m2` / `src_lo_m2` /
   `src_hi_m2` (the source areas), `vol_m3`, `vol_lo_m3`, `vol_hi_m3` and the rest
   onto the *total* outline's own feature, so the numbers travel with the
   geometry. **Save the scar layer before writing back** — adding fields needs a
   clean edit buffer, and QGIS renumbers features when a layer is saved, so the
   tab refuses rather than committing your digitizing behind your back or
   stamping the numbers onto the wrong feature.

Both scratch layers the tab creates (`Landslide scars`, `Landslide centerlines`)
are memory-backed and vanish when QGIS closes — use *Export ▸ Save Features As…*
to keep them.

Areas are ellipsoidal plan-view area — the same quantity `$area` reports and the
same one Larsen et al. measured from imagery, so the published coefficients stay
valid. Slope-corrected true surface area would be larger and is deliberately not
what is fed to the relation.

The volume arithmetic itself is read from the project's own
`larsen_BR_volume.py`, so editing the coefficients there changes what the tab
reports; the tab says which implementation and which fit produced each number
("Calculated by"), and falls back to a built-in copy when the project dir isn't
configured.

### Enabling the total-area fit

`larsen_BR_volume.py` carries only the **source-scar** fit — its function is
`volume_source` and its constants are labelled *soil scar* / *bedrock scar*.
Running those on a total-area outline applies a source-area calibration to a
larger polygon and reads high, so the tab does **not** quietly reuse them: pick
*Total landslide area* without coefficients and it refuses, naming what to add.

Larsen et al. publish separate constants for total landslide area. Those values
are not reproduced here — guessing published constants would be worse than
refusing — so paste them in yourself, either in `larsen_BR_volume.py`:

```python
LARSEN_TOTAL = {
    "bedrock": (log10_alpha, std_log10_alpha, gamma, std_gamma),
    "soil":    (log10_alpha, std_log10_alpha, gamma, std_gamma),
}
```

or in `LARSEN_TOTAL` in `qgis_plugin/landslide_groundtruth/volume_calc.py`. The
project file wins, so the constants can live with the rest of the science. Either
way the error propagation is the same code path as the scar fit — only the four
coefficients change.

## Module map

| file | role |
|------|------|
| `planet_imagery.py` | **primary source.** Planet Data API search + Orders API clip/download of PlanetScope (~3 m) surface reflectance; UDM2 cloud-masked median composite; same return contract as `imagery.py`. Checks the order cache before ordering, and `recall_preview` loads earlier orders back for free |
| `planet_cache.py` | shared cache + ledger of Planet orders already paid for, so a scene is ordered once. Pure stdlib (the QGIS plugin imports it directly) — see [Ordering a scene once](#ordering-a-scene-once-quota) |
| `imagery.py` | tries Planet first (via `planet_imagery`), then STAC: cloud/snow-masked median composites, dNDVI, dBrightness (albedo change); falls back S2→Landsat; `--seasonal` for winter |
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
- **Snow and clouds.** Alaska coastal/winter events are the hard case. Use
  `--seasonal`, widen `--post-days` to reach the next clear/snow-free window,
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
bundle and renders it here instead. Two curves, `--planet-tone`:

- **`knee`** (default) — highlight rolloff. A plain linear stretch up to
  `KNEE × WHITE` = 0.165 reflectance, so midtones and shadows are arithmetically
  identical to an unstretched render; brighter pixels are compressed along an
  asymptote that never clips. Right when the scar is on terrain and ice is context.
- **`natural`** — `cbrt(0.6 × reflectance)`, the Copernicus Browser look. Even
  detail across the whole range, at the cost of global contrast.

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
