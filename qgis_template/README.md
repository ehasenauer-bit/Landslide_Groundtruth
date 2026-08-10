# QGIS template project

A starting project for each new landslide: satellite basemap already loaded,
named peaks, international and state/province borders, and park boundaries —
all styled, so a new `.qgz` opens ready to work instead of empty.

Two scripts build it. Run them once; after that every **File → New** starts from
the result.

```bash
cd qgis_template && python3 build_reference_gpkg.py && python3 build_template.py
```

Then tick one checkbox in QGIS — see [Arming it](#arming-it) below.

---

## What gets built

Everything lands in the QGIS default profile:

```
~/Library/Application Support/QGIS/QGIS3/profiles/default/
├── project_default.qgs              used by File > New once armed
└── project_templates/
    ├── Landslide.qgz                used by Project > New from Template
    ├── landslide_reference.gpkg     the data
    └── styles/*.qml                 each layer's style, reusable elsewhere
```

| Layer | Source | Notes |
|---|---|---|
| Peaks | three gazetteers merged — see below | triangle + haloed label; hidden above ~1:2M, labelled below ~1:600k |
| International border | Natural Earth 10m admin-0 | the 141°W Alaska/Yukon line |
| State / province border | Natural Earth 10m admin-1 | AK/YT, AK/BC |
| Parks & protected areas | OSM `boundary=protected_area` / `national_park` | outline only, labels off by default |
| Google Satellite | `mt1.google.com` XYZ | visible |
| Google Hybrid / Terrain | `mt1.google.com` XYZ | off — tick for place labels or shaded relief |
| Esri World Imagery | ArcGIS Online XYZ | off — licensed alternative, see [Licensing](#licensing) |

Default region is Alaska + Yukon + the northern BC panhandle
(54–72°N, 173–123°W): 6,269 peaks, 399 protected areas, 13.6 MB.

### Where the peaks come from

No single gazetteer covers this region, so three are merged on a name + 600 m
proximity match:

| Source | Coverage | Elevation? | Kept |
|---|---|---|---|
| OSM `natural=peak` / `volcano` | both sides of the border | **yes** — the only source that has it | 4,789 |
| NRCan CGNDB (`CONCISE=MTN`) | authoritative for Yukon and BC | no | 1,058 |
| USGS GNIS Domestic Names (`Summit`) | authoritative for Alaska | no | 422 |

6,269 peaks for the default region, after 3,709 cross-source duplicates were
dropped.

OSM is merged first so its elevations survive the dedup. Elevation drives label
priority, so where summits crowd together the big ones win — peaks with no
elevation fall back to a mid-range priority rather than disappearing.

**The union is still not exhaustive.** Unofficial climbing names on the Logan
massif — *Catenary Peak* and *Hubsew Peak*, both labelled on the reference map
this template was modelled on — are in none of the three. Names like that have
to be added by hand; the layer is editable, or keep a small companion
GeoPackage of your own summits.

## Arming it

`build_template.py` writes `project_default.qgs`, but QGIS ignores it until you
say so. One time:

1. **Settings → Options → General** (on macOS this may be **QGIS → Preferences**)
2. Under **Project files**, tick **Create new project from default project**
3. Close with **OK**

**File → New** now opens the template. `Project → New from Template → Landslide`
also works, armed or not.

To stop using it, untick the box — or click **Reset default** next to it.

## Retargeting the region

Working outside Alaska? Rebuild with a different bounding box (south, west,
north, east), pointing the gazetteers at the right jurisdictions, then re-run
the template build:

```bash
python3 build_reference_gpkg.py --bbox 46.0 -125.0 49.5 -120.0 --gnis-states WA --cgndb-provs bc && python3 build_template.py
```

Useful flags:

| Flag | Effect |
|---|---|
| `--skip-parks` | peaks and borders only — parks are the slow half |
| `--keep-cache` | keep `_staging/` so the next run can skip the network |
| `--reuse-cache` | reuse whatever is staged instead of re-querying |
| `--park-simplify 0` | keep every park vertex (and a ~100 MB layer) |

A full sweep is about 50 Overpass queries and roughly twelve minutes; the
gazetteer downloads are seconds. `--keep-cache` then `--reuse-cache` makes
re-running to change styling or simplification nearly instant. `_staging/` is
~115 MB, so delete it when you're done iterating.

## Licensing

The Google basemaps hit `mt1.google.com` directly. This is the recipe every
"Google Satellite in QGIS" guide uses, and it **breaches Google's Terms of
Service**, which require Maps imagery to be served through the Maps Platform
APIs. It works, and nothing will stop you — but it is worth knowing before an
image ends up in a published figure.

**Esri World Imagery** is in the project as a licensed layer of comparable
resolution; tick it and untick Google to swap. Over the St. Elias it is
noticeably the *better* layer as well as the safer one — Google's coverage there
is a patchwork of scenes from different dates with visible seams and colour
mismatches, while Esri's is a consistent mosaic. Worth comparing the two before
committing to Google out of habit.

For actual Google imagery under licence, the **Google Maps Platform Map Tiles
API** needs an API key and a session-token request — say the word and I'll wire
it in.

OSM data is ODbL (attribute "© OpenStreetMap contributors"). Natural Earth is
public domain.

## Notes on the choices

**CRS is EPSG:3857.** Web Mercator matches the XYZ tiles, so they draw at native
resolution instead of being resampled on every pan. Mercator *area* at 60°N is
off by about 4×, so the project ellipsoid is set to WGS 84 — that makes QGIS
measure geodetically and the distortion never reaches your numbers. Change the
CRS in **Project → Properties → CRS** if you'd rather work in EPSG:3338 (Alaska
Albers); leave the ellipsoid alone.

**Layer paths are absolute.** The GeoPackage lives inside the QGIS profile, a
stable per-user location, so a relative path from there to wherever a project
gets saved buys nothing. Pass `--relative-paths` to `build_template.py` if you
need the folder to be portable between machines.

**Boundary monuments are filtered out.** The Alaska/Yukon boundary survey put
numbered markers into OSM tagged `natural=peak` ("Monument 144", "Boundary Point
157"). They are survey markers, not summits, so `build_reference_gpkg.py` drops
them. See `JUNK_NAME` if you want them back.

**Park outlines are simplified to ~22 m.** Full OSM detail is about 100 MB and
invisible at any scale this template gets used at. They are context, not
measurement targets — use `--park-simplify 0` if you need exact boundaries.

**One park is missing.** Yukon Delta National Wildlife Refuge has a
self-intersecting polygon in OSM that GDAL rejects when clipping (298 of 299
areas survive). It is nowhere near the St. Elias, so it has been left alone
rather than patched.

**XYZ URL encoding is load-bearing and fails silently.** If you ever hand-edit
`BASEMAPS` or `xyz_layer`, know that the layer URI is itself an `&`/`=`
delimited parameter string. Any `&` or `=` *inside* the tile URL — Google's
`lyrs=s&x={x}&y={y}&z={z}` is nothing but those — must be percent-encoded, or
the URL gets truncated at the first `&`. But `:` and `/` must stay literal:
encoding them as `%3A`/`%2F` produces a layer that reports `isValid() == True`,
raises no provider error, logs nothing, and renders a completely blank basemap.
`urllib.parse.quote(url, safe=':/')` is the combination that satisfies both.
Because none of the obvious health checks catch this, `build_template.py` now
fetches one real tile per basemap (`tiles_arrive`) and refuses to write a
template containing a basemap that returned no pixels.

**Only global Overpass mirrors, and empty tiles get double-checked.** Worth
knowing if you ever edit `OVERPASS`: an early version of this script listed
`overpass.osm.ch`, which serves a Switzerland-only extract. Asked about Alaska
it returns HTTP 200, valid JSON, no error remark, and zero elements — so
whenever the global mirrors were rate-limited and the code fell through to it,
whole tiles were recorded as empty. Two Brooks Range tiles holding ~400 named
peaks vanished that way, and three identical runs returned 3639, 2932, and 3817
peaks before anyone noticed. The script now queries only whole-planet mirrors
and requires two of them to agree before believing a tile is empty. Add a
mirror only after confirming it serves the planet.

---

## Building it by hand

The scripts do all of this. Here it is as clicks, for when you want to change
something or rebuild on another machine.

### 1. Add the basemap

Browser panel → right-click **XYZ Tiles** → **New Connection…**

| Field | Value |
|---|---|
| Name | `Google Satellite` |
| URL | `https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}` |
| Max. Zoom Level | `20` |

**OK**, then double-click the new entry to add it to the map. The connection is
stored in your profile, so it stays available to every project — worth doing
even if you never build the template.

Other `lyrs` values: `y` hybrid, `p` terrain, `m` roads. For Esri, use
`https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`
with max zoom 19 — note the `{z}/{y}/{x}` order.

### 2. Add the reference layers

**Layer → Add Layer → Add Vector Layer…** (`Ctrl+Shift+V`), browse to
`landslide_reference.gpkg`, **Add**, then select all four layers. Drag them
above the basemap in the Layers panel.

### 3. Style them

Per layer: **Layer Properties → Symbology → Style ▾ → Load Style… → From file**,
pick the matching `.qml` from `project_templates/styles/`.

To do it from scratch instead, for peaks:

- **Symbology** → Simple Marker → Shape `Triangle`, Size `3.2 mm`, Fill
  `transparent`, Stroke `black` `0.4 mm`
- **Labels** → Single Labels → Value `name` → **Text**: bold, 9 pt →
  **Buffer**: tick *Draw text buffer*, `1 mm`, white → **Placement**: Around
  point, Distance `1.5 mm`
- **Labels → Rendering** → tick *Scale dependent visibility*, Minimum
  `1:600000`. Set **Priority** to the data-defined expression
  `coalesce(scale_linear("ele_m", 500, 6000, 2, 10), 4)`
- **Layer Properties → Rendering** → tick *Scale dependent visibility*,
  Maximum (exclusive) `1:2000000`

### 4. Set the project properties

**Project → Properties → General**, under *Measurements*: Ellipsoid `WGS 84`,
distance `Meters`, area `Square kilometers`.
**→ CRS** tab: `EPSG:3857`.

### 5. Save it as the template

**Project → Save As…** into
`~/Library/Application Support/QGIS/QGIS3/profiles/default/project_templates/`
as `Landslide.qgz`. (The folder path is shown, and changeable, at
**Settings → Options → General → Template folder**.)

Then **Settings → Options → General → Project files** → **Set current project as
default**, and tick **Create new project from default project**.
