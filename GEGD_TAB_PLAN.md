# Maxar VHR (GEGD Pro) tab: implementation plan

*2026-10-13. Built from the official Vantor Hub QGIS plugin source (v1.6.2) and the `maxar-platform` SDK (1.0.7), about 130 read-only requests against your GEGD Pro account, offline QGIS 3.44.11 experiments, the plugin codebase, and a domain and licensing review. Three independent draft plans were judged and merged, and an adversarial review fixed six blocking problems before this version.*

*Published copy: account-specific access details, key-handling history and correspondence have been left out.*

**Evidence tags used below**

| Tag | Meaning |
|---|---|
| **[live]** | Verified against `pro.gegd.com` on 2026-10-12/13. |
| **[qgis]** | Verified in headless QGIS-LTR 3.44.11 against a local mock server with a dummy key. Proves QGIS behaviour, not GEGD behaviour. |
| **[code]** | Read in this repo. Line numbers are as of today; your WIP may shift them. |
| **[assumed → Mn]** | Not yet tested. Milestone Mn settles it. |

The full research reports, the pre-review detailed spec and the review findings are in `out/gegd_planning/` (gitignored).

---

## 0. One-page summary

### The services the tab uses

| Service on `https://pro.gegd.com` | What the tab uses it for |
|---|---|
| WFS catalog `Maxar:FinishedFeature`: 94 attributes, CQL, sort, paging, JSON [live] | Event search and triage |
| WMS / WMTS streaming of **one strip** (`CQL_FILTER=featureId='…'`) [live] | Pre/post streaming, thumbnails |
| WMS `image/geotiff` and WCS 2.0.1 GetCoverage: native resolution, server-side UTM [live] | Downloaded clips |
| Vivid Standard basemap and its seamline (patch-date) WFS [live] | Pre-event context fallback |

- **Auth.** The key goes in a request header `maxar-api-key: <KEY>` [live]. The `?maxar_api_key=` query form also works, but the server copies it into every capabilities link and WFS "next" link, so the tab never uses it. (My first test said the header was rejected; that was a quoting bug in my probe.)
- **Pixels.** Every optical strip is **8-bit, DRA-stretched RGB (pansharpened) or pan (WV01)**, with no NIR and no SWIR [live]. That is excellent for seeing, tracing and draping, but **it can never feed Fusion's radiometric channels** (dNDSI, dBright).
- **Archive and latency.** The online Daily Take archive starts around **2024-07-20**, and new strips appear about **2.3 h after collection** (median), with no access delay [live]. The 2023 events (Peters Dome, Bagley) have nothing. No Daily Take is collected Dec–Jan at these latitudes.

### What the tab does for your four needs

| Your need | How the tab delivers it |
|---|---|
| **Tied to the event** | **Use it in every tab** fills it from the seismic detection. The search area comes from the location error. Pre and post are split on the full UTC origin time. Empty results say why (archive floor, winter, no coverage, everything filtered). |
| **Faster triage** | One WFS call (0.3–1.6 s) returns every candidate at any map scale. They are ranked on geometry, sun, recency, AOI coverage and strip cloud, and a recommended ★ before/after pair is streamed automatically. Thumbnails are cut to the scar. |
| **Map the scar on VHR** | One button refines or traces **the layer the Volume tab actually uses** (found at run time, shapefile or GeoPackage). Every edit gets a backup and a provenance record (which strip, off-nadir, analyst, confidence). |
| **Downloads for analysis** | Native-class 0.5 m clips on one UTM grid per event, beside the project. They drape and pair in the 3D tab with no 3D code change. They are not Fusion inputs (8-bit, see above). |

Total effort: **3 clicks from a detection to a live pre/post comparison**, and 5 clicks plus digitizing to a refined scar and downloaded clips. The official plugin needs a password login (which creates and deletes API keys), a type choice, a filter window, zooming below 1:50,000 and toggling the 5 newest strips. It has no pre/post concept and no download. It also officially supports only QGIS ≤3.40, while you run 3.44.11.

### Milestones

| | Ships | Size | Depends on |
|---|---|---|---|
| **M0** | Pre-flight checks in headless QGIS, a synthetic test-fixture generator, the licensing question sent to GEGD Pro support, key hygiene | S | nothing |
| **M1** | Key box, event search, ranked table, footprints, auto-streamed ★ pair, flip | M–L | M0 |
| **M2** | Thumbnail gallery, blink/opacity/side-by-side, find by ID, same-day S2 cloud proxy | M | M1 |
| **M3** | Scar tracing and refining in your real ground-truth layers, with backup and provenance | S–M | M1 |
| **M4** | Clip downloads (tiled, cancellable, cached, licence-tagged) | L | M1 + **GEGD's answer on downloads** |
| **M5** | Vivid pre-event fallback, access-check matrix, API-drift handling, key-age warning | S–M | M1 |
| **M6** | Optional pre/post co-registration | M | M4 |

### Decisions I need from you

Everything else proceeds on the recommendation; the full list is in §15.

**Answered 2026-10-13:** 1 yes · 2 A (save into your existing scar files) · 3 yes · 5 yes (the shared drive is AEC-only, so clips go beside the project). **4:** not technically required, so M4 is not gated; asking support is optional (§15).

1. **Fusion can't use Maxar pixels.** VHR helps Fusion only by giving you better truth polygons. *Accept?*
2. **Scar edits go into your real ground-truth layers**, the ones Volume uses, after a dry run on a copy of Iliamna. *Recommended*, versus a separate VHR GeoPackage you merge by hand.
3. **Volume doesn't recognise the name "Landslide scars".** Your projects' total layer is named that, but Volume's automatic name matching only accepts {total, area}, {total, landslide}, {landslide, area} and {total, extent}. *Recommend* a one-line addition to `volume_tab.py` (not WIP), which also fixes the gap when you use Volume on its own.
4. **Downloads and licensing.** Whether GeoTIFF export of streamed strips is allowed for internal analysis, what limits apply and what credit line is required is a question for GEGD Pro support or the sponsor. M1–M3 don't wait for the answer; M4 does.
5. **Where licensed clips live.** Beside the project on the shared research drive is fine **only if everyone with access to that drive is on the AEC team**. Is that true?

---

## 1. Facts the design rests on

### 1.1 GEGD server [live unless noted]
- **Header auth.** `maxar-api-key: <KEY>` works in any case on WFS, WMS, WMTS, WCS, seamlines and `/streaming/v1/projects`.
  - `x-api-key`, `apikey`, `maxar_api_key` (as a header) and `Authorization: ApiKey …` all give 401. `Authorization: Bearer <KEY>` gives 403 "Malformed token".
  - With header auth, the capabilities documents contain no key.
- **WFS `Maxar:FinishedFeature` (version 2.0.0).**
  - JSON output, CQL on numbers and dates, `sortBy`, `count`/`startIndex`, `resultType=hits` (returns `numberMatched`). CountDefault is 5000.
  - **Spatial filter:** use `BBOX(featureGeometry,lonmin,latmin,lonmax,latmax,'CRS:84')`. A BBOX without the CRS argument is lat,lon order, and lon-first returns **0 with no error**. `INTERSECTS(POINT)` returns 0, and `DWITHIN` ignores its distance.
  - **Fields:** `featureId`, `legacyIdentifier`, `acquisitionDate`, `createdDate`, `source`, `productName`, `sensorType`, `cloudCover` (**strip-level, fraction 0–1**, at most about 0.35 observed), `offNadirAngle`, `sunElevation`, `sunAzimuth`, `groundSampleDistance`, `processingLevel`, `demCorrection`, `ce90Accuracy`, `bandConfiguration`, `bitsPerPixel`, `isThirdParty`, `vendorName`, `featureGeometry`, `resolutionX/Y`.
  - **Processing levels:** `LV3D` ("Fine DEM", off-nadir ≤30°, CE90 8.4 m) or `LV2A` ("Coarse DEM", off-nadir >30°; its `ce90Accuracy=0.0014` is junk). The field is null on records older than about April 2026.
  - **IDs:** new `legacyIdentifier`s start `B0…` (since about Aug 2026). Key everything on `featureId` and never decode the sensor from an ID.
  - **No AOI cloud geometry exists:** `hasCloudlessGeometry` is never true in Alaska.
- **WMS `Maxar:Imagery`.**
  - `cql_filter=featureId='…'` renders one strip, transparent outside its footprint. A bogus id gives a fully transparent tile.
  - `image/geotiff` gives **RGBA Byte, with nodata=0 declared on ALL 4 bands (alpha included)**, so the mask must come from alpha, never from 0.
    - It is pixel-identical to WCS.
    - **WV01 pan arrives as 2-band Gray+Alpha** (WCS gives 1 band).
  - **A GeoTIFF requested in UTM returns a geotransform exactly equal to the request** [M0]. 2048×2048 works (16.8 MB, about 7 s); nothing larger was tried.
  - Responses are deterministic (identical md5 for identical requests) and never gzip-encoded.
  - **Bytes per format** for the same 1024² tile [M0]:

    | Format | Bytes | Fidelity |
    |---|---|---|
    | GeoTIFF | 4.2 MB | lossless |
    | **PNG** | **1.47 MB** (35%) | **bit-identical RGB + alpha** |
    | JPEG | 0.17 MB (4%) | PSNR 34 dB, no alpha |

  - **Tone and geometry across requests** [M0, adversarially re-verified]:
    - There is no per-request DRA re-stretch; tone agrees to 0.02 DN, so tiles have no tone seams.
    - **EPSG:4326 renders jump by about 1 px (0.5 m) in x depending on where each request starts.**
    - UTM renders are geometrically consistent to about 0.1 px and line up with the WMTS streams to about 0.05 px. Overlap pixels still differ through sub-pixel resampling.
  - A date-filtered mosaic honours `sortBy`.
- **WMTS (GeoWebCache).**
  - Tile matrix sets EPSG:3857/4326/900913, z0–21.
  - `GetTile` honours `CQL_FILTER` and is not cached server-side (`no-store`).
  - **Both** `/streaming/v1/ogc/wmts` and the advertised `/streaming/v1/ogc/gwc/service/wmts` honour the filter and return byte-identical tiles [M0]. The advertised GWC path is primary; the other is the drift fallback.
- **WCS 2.0.1 `Maxar__Imagery`.**
  - Per-strip `CQL_FILTER`; native grid when no `scalesize` is given (WV02 is about 0.25 × 0.5 m in degrees at 60°N); `outputCrs` to UTM works (WV01 pan at 0.596 m).
  - Output is 3-band (1 for pan) with **no mask**.
  - Maxar retired its *commercial* Hub WCS on 2026-03-01, so treat GEGD WCS as at risk and keep it off the critical path.
- **Size and metering.** WMS is verified up to **2048×2048** [M0] and WCS up to 1001×1001; nothing larger was tried, so the clip tile stays at 2048. No rate-limit or usage headers were seen on about 190 responses. The usage endpoints need a full login (JWT), so the **tab cannot see quota**.
- **Product mix in Alaska, Sep 2026** (467 strips): WV02 159, WV01 129, WV03 82, GE01 28, third-party SAR 67 (Capella, ICEYE, Umbra).
  - `RDOG_BASE` records older than the archive floor exist (Iliamna 2021 WV01, Bagley 2019 GE01). **They are legacy mosaic tiles, not single-date** [M0]: `outputMosaic=true`, about 1,570 km², and Iliamna's spans 2021-03-27 → 04-01. Any record whose `latestAcquisitionTime − earliestAcquisitionTime` exceeds a few minutes is flagged multi-date and put in the context class. Rights text is read from the record's `copyright` and `licenseType` fields when present.
- **CQL details** [M0]: `productName NOT IN (…)` works, as does the `<>` chain. `featureId='X' OR legacyIdentifier='Y'` works. `sortBy=acquisitionDate D,featureId A` is accepted. **Sorting on `productName` gives HTTP 400 "Bad data query."**, which `classify_http` maps to a query-construction error, not an auth error.
- **Latency:** WFS 0.3–1.6 s; WMS 1 Mpx 1.2–2.7 s; WMTS tile 0.8–2.4 s; WCS 1 Mpx 2.0–2.4 s.

### 1.2 Benchmark events [live]

| Event | Nearest pre | Post |
|---|---|---|
| **Iliamna 2026-08-08** (60.0265, −153.0559) | 08-01 00:18Z WV01 pan, off-nadir 29.6°, LV3D *(that is 07-31 in AKDT)* | **08-26 WV02** (off-nadir 20.2°, cloud 0, 0.5 m; deposit clearly visible); 08-31 WV02 (14.1°); 09-22 WV02 ×2 (an in-track stereo pair) |
| Hubbard 2026-07-28 | 06-01 GE01 | 08-05 WV02 |
| Azumi 2026-08-24 | 06-25 | 09-03 WV03 |
| Knik 2026-08-09 / Valdez 2026-08-04 | none within 60 d | 09-02 / 09-03 WV03 |
| Logan 2026-08-05 | 06-20 WV02 | none within 60 d |
| Peters Dome 2023-09-13, Bagley 2023-08-07 | before the archive floor | none |

Vivid at Iliamna: all four seamline patches **predate** the event (2023-08-18 to 2026-05-20). The 2026-09-02 date on the Vivid record is a mosaic date, not a pixel date.

### 1.3 QGIS 3.44.11 [qgis]
- The **APIHeader auth method** exists (`libauthmethod_apiheader`). An authcfg with a fixed id adds the header on:
  - `QgsBlockingNetworkRequest`, both on the GUI thread and inside a QgsTask;
  - XYZ tiles, WMS GetCapabilities and GetMap, and WMTS tiles.
- A saved `.qgz` holds only `authcfg=<id>`, never the key.
- **The authcfg sends the header to *any* host a layer names.** A crafted or edited `.qgz` pointing `authcfg=gegdpro` at another server would receive your key. A request preprocessor that strips the header for every host except `pro.gegd.com` fixes this, verified offline. **The fix is mandatory** (§3).
- **The WMTS provider silently drops `CQL_FILTER`**, so you would see the newest-on-top mosaic instead of your strip. An **XYZ layer** whose template is set with `QgsDataSourceUri.setParam("url", …)` keeps it.
  - The WMS provider keeps `cql_filter` but makes 1 GetCapabilities call per layer.
- `QgsNetworkAccessManager.setRequestPreprocessor` runs on every thread and can add or strip headers. **On the XYZ path it runs twice per tile**, once before the auth header is added, so it must not be used to count requests [M0].
- **Redirects:** a 302 hop to another host goes back through the preprocessors (blocking, fetcher and XYZ requests). The exception is `QgsFileDownloader`, where Qt follows the redirect internally and the key reached the redirect host in a local test [M0 verify].
- A missing authcfg makes QGIS fail the request before sending anything.
- **`QgsBlockingNetworkRequest` can hang its thread for good on cancel** [measured 2026-10-14]. `doRequest` connects the feedback to its own `abort()` before the reply exists, and to the reply's `abort()` only after it does. Both connections are queued from the GUI thread. A cancel landing between the two delivers only `abort()`, which `deleteLater()`s the reply without it ever emitting `finished`, so the inner event loop waits forever. `test_gegd_tab`'s superseded key test stalled in about 4 of 7 runs, and Cancel, Save key, Forget key or a plugin reload could leave a task running for good. So `gegd_net` does not use it (§2.1).
- **Network Logger (F12) exposes the key** [M0]. `requestAboutToBeCreated`, the logger's feed, carried the full header on 30/30 GEGD requests. It fires after the preprocessors, so nothing in the plugin can hide it. Mitigation: a README and tooltip warning never to share logger exports, "Copy as cURL" or "Save Network Log" while GEGD layers are loaded.
- **Keychain:** in 3.44 the password helper is on by default even in an isolated profile. Tests must call `setPasswordHelperEnabled(False)` *before* `setMasterPassword`, or they touch the global Keychain entry.

### 1.4 Codebase [code]
- **Tab wiring:**
  - `dock._build_dock` (`dock.py` ~328–363) builds the QTabWidget, with the Planet `addTab` at ~347.
  - **Use it in every tab**: the target tuple is at ~766 and its tooltip at ~638–640.
  - `teardown` is at ~3119–3170.
  - None of these overlaps your current WIP hunks in `dock.py`.
- `plugin.py`, `project_state.py` and `volume_tab.py` are **not** in your WIP.
- **Persistence:** `project_state.OWNERS` (~77–84) reflects every *public* widget of each owner. `SECRET_ATTRS` already excludes `key_edit`. Changing a shipped default later needs `RETIRED_DEFAULTS` plus a `VERSION` bump.
- **Event time:** `dock._apply_detection` (~762–775) puts the **UTC wall-clock time in a LocalTime-spec QDateTime**. Calling `.toUTC()` on it would shift the pre/post boundary by 8–9 h on your Mac.
- **Volume** picks role layers by name tokens, and each role contributes its **LARGEST polygon** (`volume_tab.py` ~132–141, 645–680). "Landslide scars" matches no total subject (decision 3).
- **3D:** `viewer3d_tab._find_pairs` pairs `<base>_pre_<kind>` with `<base>_post_<kind>` after stripping a date tag, zipping by name order (so 1 pre plus 2 posts pairs with the earliest post). The editable combo `det_imagery_sat` (~459–466) feeds the figure's "Imagery" row, the HTML details and the web index.
- **Fusion:** `fusion_grid.describe_raster` already refuses Byte and ≥3-band rasters, so clips can't sneak in.
- **Your real ground truth** (Iliamna, Hubbard, Azumi):
  - total = `Total Area.gpkg|layername=landslide_scars`, shown as "Landslide scars";
  - source = **`Source Area (Best).shp`** and `Source Area (Low).shp`;
  - Knik uses `Source (Best).shp`;
  - Iliamna's `Total Area.gpkg` also holds a misfiled `total_area` layer.
- **Tests:**
  - `tests/test_plugin_loads.py:77` asserts 6 tabs.
  - `tests/test_results_follow_project.py` fails if any new file contains the string `out_edit`.
  - **This repo is PUBLIC on GitHub** (`ehasenauer-bit/Landslide_Groundtruth`).
- **Git:** the index holds your staged deletion of `tests/test_hdr_bracket.py`, so every commit of mine goes through a temporary index (Appendix B).

---

## 2. Architecture

### 2.1 New files

None of these overlaps your WIP. Paths are under `qgis_plugin/landslide_groundtruth/`.

| File | Imports | Contents |
|---|---|---|
| `gegd_api.py` | stdlib | `HOST_DEFAULT`, `ENDPOINTS` (every path is a constant, including the M0-chosen WMTS path), `LAYER`, `FEATURE_TYPE`, `EXCLUDED_PRODUCTS`, `REQUIRED_FIELDS`.<br>URL builders, which **never include auth**: `wfs_getfeature_url`, `wfs_hits_url`, `wfs_by_id_url`, `wms_thumb_url`, `wms_clip_url`, `wcs_clip_url`, `wmts_xyz_template`, `seamlines_url`.<br>`Strip` (frozen dataclass), `parse_features()` (raises `SchemaDrift`), `classify_http(service, status, body, ctype)`, `parse_ogc_exception()`, `redact()`. |
| `gegd_rank.py` | stdlib, pure functions | `event_bbox`, `aoi_disc`, `aoi_cover`, `split_sides`, `incidence_deg`, `relief_shift_m`, `grade`, `strip_score`, `best_pair`, `stereo_mates`, `empty_state_message`, `rank`.<br>Module-level functions only, so they can never collide with the tabs' existing `_aoi_bbox` helpers. |
| `gegd_auth.py` | qgis.core | `AUTHCFG_ID="gegdpro"`, `KEY_HEADER="maxar-api-key"`, `has_apiheader()`, `ensure_ready()` (GUI thread only), `store_key()`, `has_key()`, `forget_key()`, `key_age_days()`, `mode()`, `HostGuard` (strip and count, always on), `HeaderInjector` (fallback only). |
| `gegd_net.py` | qgis.core | `fetch(url, service, timeout_s, retries, feedback, force_refresh)`. Each try is a **non-blocking** `get()` on the calling thread's `QgsNetworkAccessManager.instance()`, so the preprocessors and HostGuard run, the Network Logger initiator is kept, and the guard counts the request once. The key is added by `authManager().updateNetworkRequest(req, "gegdpro")` and `updateNetworkReply`; if the config can't be applied, the result is "no key stored" and nothing is sent. A local `QEventLoop` ends on the reply's `finished`, on a 100 ms timer poll that finds the feedback cancelled, or on a stall backstop. Any reply still running is aborted, and every reply is deleted on the GUI thread. Every request uses `ManualRedirectPolicy`: a same-origin 3xx is followed by hand (at most 5 hops), and a 3xx to any other origin is refused with nothing sent there. Retries, `Retry-After`, `setTransferTimeout`, cache bypass and `classify_http` are as in §11. It does not use `QgsBlockingNetworkRequest`, because that hangs on cancel (§1.3). Cache paths are under `~/.cache/landslide_gegd/`. |
| `gegd_tasks.py` | qgis.core, osgeo | `SearchTask`, `ThumbTask`, `ClipTask`; later `AccessCheckTask` and `CoregTask`. They return data or paths and never touch widgets or the project. |
| `gegd_layers.py` | qgis.core | `strip_xyz_uri`, `strip_wms_uri` (fallback), `footprint_layer`, `aoi_layer`, `load_clip`, `set_props`, `set_rights(vendor-aware)`, `adopt_layers`. |
| `gegd_products.py` | stdlib, osgeo | `event_id`, `event_epsg`, `plan_clip`, `cliptag`, `clip_path`, `write_sidecar`, `manifest_update`, `ensure_license_file`, `find_existing_clips` (stale-clip rule). |
| `gegd_scars.py` | qgis.core, sqlite3 | `discover_role_target(role)`, `backup_once(layer)` (GeoPackage via sqlite3 backup; shapefile by copying every sidecar), `record_edit`, `arm_trace`, `arm_refine`, `centroid_checks`, `xcheck`. |
| `gegd_tab.py` | qgis.gui | `class GegdTab(QWidget)`: UI and wiring only. |
| `gegd_coreg.py` (M6) | numpy | `phase_correlate`, `estimate_shift`, `apply_shift`, `revert_shift`. |

**Not used, and why:**
- The vendor SDK needs oauthlib and pystac (neither ships with QGIS), sends the header form wrongly for GEGD, and has no download helper.
- `requests` bypasses QGIS's proxy, SSL and auth handling.
- Subprocesses or the venv: the key would have to travel in a command line or environment, and the venv has no GDAL anyway.

### 2.2 Edits to existing files

| File | WIP? | Edit |
|---|---|---|
| `dock.py` | **yes** | 5 small hunks: (a) import; (b) `self.gegd_tab = GegdTab(self)` plus `tabs.addTab(…, "Maxar VHR (GEGD)")` after PlanetScope; (c) `"gegd_tab"` added to the **Use it in every tab** tuple; (d) the same button's tooltip now lists Maxar; (e) `self.gegd_tab.teardown()` in `teardown`. I re-diff against your WIP right before staging. |
| `plugin.py` | no | `HostGuard.install()` in `initGui` and `.remove()` in `unload`. It reads no key, so it never prompts for a password, and it protects projects opened before the dock exists. |
| `project_state.py` | no | One row `("gegd_tab", "gegd")` in `OWNERS`. |
| `volume_tab.py` | no | *(Decision 3)* One extra `TOTAL_SUBJECTS` entry so "Landslide scars" is recognised. Check `_name_tokens` for plural handling first. |
| `tests/test_plugin_loads.py`, `tests/test_results_follow_project.py` | no | 6→7 tabs; one row in `outs()`. |
| `planet_tab.py`, `task.py`, `viewer3d_tab.py`, `web3d_export.py`, `review_package.py`, `run_single.py`, `fusion_*` | — | **No change.** The 3D credit is set at run time through `det_imagery_sat` (§9). |

### 2.3 Threading rules (enforced by tests)
1. `GegdTab.__init__` never touches `QgsApplication.authManager()`, so opening the dock or running tests never prompts for the master password. A test monkeypatches it to raise during construction.
2. `gegd_auth.ensure_ready()` runs on the GUI thread before every task launch and every layer creation. It unlocks the auth DB (or the Keychain); any password prompt happens here.
3. All network work runs in `QgsTask` subclasses with a `QgsFeedback`, so cancel aborts the HTTP request. `gegd_net` polls the feedback every 100 ms, so a cancel ends the request within about that long whenever it lands; `test_gegd_net` checks 85 cancels, each ending within 2 s. Tasks are kept referenced until they finish, and a generation counter drops stale results (the `viewer3d_tab` pattern).
4. Layers are created only on the main thread, in `finished()`.
5. GDAL options are set with `gdal.config_options({...})` (thread-local), **never** with process-global `SetConfigOption`.
6. `teardown()` cancels tasks, stops timers, closes the side-by-side view, and disconnects the `det_*` and `readProject` signals. The HostGuard stays installed (it belongs to the plugin).

---

## 3. Key handling and security

**Storage.** The key lives in the QGIS auth DB as an APIHeader config: id `gegdpro`, header `maxar-api-key`, written with `storeAuthenticationConfig(cfg, True)`. It is encrypted, unlocked by the master password or the macOS Keychain, and **layer sources carry only `authcfg=gegdpro`**. QgsSettings holds non-secrets only: `gegd_key_set_utc`, `gegd_host`, `gegd_tile_px`, `gegd_usage/<date>`, `gegd_analyst`, `gegd_clip_mode`, `gegd_product_clause`.

**HostGuard** (always installed from `plugin.initGui`; verified in M0 against the real authcfg):
- A request preprocessor that **removes** the `maxar-api-key` header (`setRawHeader(name, QByteArray())`, a null value, not an empty string) unless:
  - the scheme is `https`;
  - the host is exactly `pro.gegd.com`;
  - the port is 443 or unset.

  A changed `gegd_host` is allowed only after a confirmation dialog. Without the guard, a local test server received the key from 15 different QGIS request paths. With it, 0.
- On allowed requests it sets `RedirectPolicyAttribute = ManualRedirectPolicy`, so Qt can never carry the key across a redirect. (`SameOriginRedirectPolicy` breaks `QgsBlockingNetworkRequest`.) The tab's own requests (`gegd_net`) set Manual policy on every request. They follow a same-origin 3xx by hand, back through the auth config and the guard, and refuse any other 3xx as `endpoint_moved`, sending nothing to the new address. **The tab never uses `QgsFileDownloader` for GEGD.**
- **Usage counter:** counted once per request on `QgsNetworkAccessManager.requestAboutToBeCreated`/`finished` for the GEGD host, never in the preprocessor (XYZ runs it twice per tile). It is flushed to QgsSettings by a GUI-thread timer and drives "Today: N requests, X MB sent by this QGIS". GEGD's own meter isn't visible to API keys.

**Fallback** (no APIHeader method, or M0 shows the authcfg failing):
- `HeaderInjector` adds the header for the allowed host only.
- Layer URIs are then built **without** `authcfg` (a missing authcfg would kill the request before the injector runs).
- The injector reads the key, so it is installed when the tab first needs it, not at startup, unless you opt in.

**Key box**
1. Paste the key, press **Save key**: `ensure_ready` → `store_key` → clear the field → **Test**. Save key and Forget key first cancel the tab's requests and write the `gegdpro` config only once none is in flight (`GegdTab._when_idle`; writing it while a worker loads it can deadlock 3.44's auth locks). If both are pressed before that wait ends, only the newer one runs [measured 2026-10-19: run in turn, Save-then-Forget waited the full 10 s on the key test the Save started and then removed the key while that test was still running, and Forget-then-Save cleared the field before the Save read it, so the new key was dropped].
2. **Test** is one WFS request: the 25 newest Alaska records of the last 120 days, newest first, with the Vivid exclusion in the form Search last got through. Of these, the status names the newest **Vantor optical** strip. That strip is picked client-side by `gegd_tasks.vantor_optical`, which skips THIRD_PARTY records (flagged or not), radar, records whose `sensorType` is not `Optical` or is missing, Vivid/BASE_MOSAIC and multi-date mosaic tiles. It is not picked with a CQL term, because the server might refuse one and Test would then blame the key. (The newest record is often a third-party SAR collect.)
3. On success the status reads: "Key works · newest Vantor optical strip over Alaska 2026-10-12 21:32Z (WV02) · stored in the QGIS password store (gegdpro)". If the page has no such strip, it reads "Key works (the catalog answered) · no Vantor optical strip among the N newest Alaska records …". If the page is empty, it reads "… no Alaska strip in the last 120 d …".
4. The key-age warning uses the real expiry date you give me (commercial keys default to 180 days).

**Where the key must never appear, and the guard for each**

| Place | Guard |
|---|---|
| Layer sources and `.qgz` files | `authcfg` only. Test: save a project, unzip it, grep for the dummy key. |
| URLs, capabilities, paging links | Header auth only. Server-provided links are never followed; paging uses our own `startIndex`. |
| Log, message bar, exceptions | Every string goes through `redact()` (header form, query form and the literal key). Task exceptions become `GegdError(redact(...))`. |
| Command lines, environment, `GDAL_HTTP_HEADERS` | Not used. All bytes come through QGIS's network stack. |
| Project state | `key_edit` is secret; the ID search box is the private `_id_edit`. |
| Test fixtures | **Synthetic only** (§13). A scanner test fails on anything key-shaped. |
| Network Logger (F12) | **It does show the key** (M0), and the plugin can't prevent that. The tooltip and README warn against sharing logger exports, "Copy as cURL" or "Save Network Log" while GEGD layers are loaded. |

**Hygiene.** Headless development checks read the key from a private scratch file outside the repo and Drive; the repo holds no key.

---

## 4. Event integration
- **Widgets.** `lat_edit`, `lon_edit`, `radius_spin` and `dt_edit` follow the Planet/SAR naming, so the **Use it in every tab** tuple fills them, with the radius from the location error (`suggested_radius_km()`).
  - A **Copy from Sentinel-2 tab** button copies location, radius and time only.
  - If Search is pressed with empty fields while a locatable detection exists, the tab fills itself from the detection.
- **Event line.** Example: `AK2026-0808 · 2026-08-08 hh:mm UTC (… AKDT, previous day) · ±N km · V Mm³`. When nothing matches: "No detection record matches these fields."
- **Origin time.** `detection.origin_utc` when the detection matches (it keeps seconds). Otherwise `dt_edit.dateTime().toPyDateTime().replace(tzinfo=timezone.utc)`, **never `.toUTC()`**. A test runs under `TZ=America/Anchorage`.
- **Window.** `[origin − pre_days, min(origin + post_days, now))`. The split is `acq < origin` = pre, on aware UTC datetimes. So Iliamna's 08-01 00:18Z WV01 is pre, and its tooltip says "previous AKDT day".
- **AOI.** A disc of `radius_spin` around the epicentre. The WFS BBOX is the disc's bbox plus 10%, and a client-side intersection check guards against axis-order bugs.
- **Event id.** `det.event_id`, otherwise `event_%y%m%d_%H%M` (the `run_single` convention), sanitised.

## 5. Search and triage (M1, gallery in M2)

**Request** (SearchTask, header via authcfg):
```
GET https://pro.gegd.com/streaming/v1/ogc/wfs?service=WFS&version=2.0.0&request=GetFeature
  &typeNames=Maxar:FinishedFeature&outputFormat=application/json&count=5000&sortBy=acquisitionDate%20D
  &cql_filter=BBOX(featureGeometry,{lonmin},{latmin},{lonmax},{latmax},'CRS:84')
    AND acquisitionDate>='{t0}Z' AND acquisitionDate<'{t1}Z' AND productName NOT IN (VIVID_*, BASE_MOSAIC)
```
- A **single page** (event windows never come close to 5000), so there is no paging-order risk.
- The `NOT IN` form, versus a chain of `<>`, is decided in M0 and remembered. If neither works, those products are dropped client-side.
- **Client-side safety net:** drop anything outside the AOI disc, drop excluded products again, dedupe on `featureId`, raise `SchemaDrift` if a required field is missing.
- `search.json` (no key, no pixels) restores the table when the project is reopened, with a "from last search …" badge.

**Classes**
- `mapping` = LV3D, or null level with off-nadir ≤ limit.
- `visual` = LV2A, or null level with off-nadir > limit. Badge: "coarse DEM, tens of m shift in relief, don't trace".
- `context` = SAR, third-party, `RDOG_BASE` (legacy base layer whose date may not be per-pixel).
- **None of these is ever ★.**

**Ranking** (lower is better; mapping-grade always above visual):
```
s = gap_days/τ_side + 2.0·tan(incidence) + 0.5·max(0, 25−sun)/10 + 1.0·(1−AOI cover) + 3.0·[misses epicentre] + w_cloud·cloud
```
- `incidence = asin((R+h)/R · sin(off-nadir))`.
- **Relief shift.** The tooltip states "if the source dropped 50 m, this image puts the new surface ~X m off" (`50·tan(incidence)`): about 21 m for Iliamna 08-26, about 34 m at 30°.
- **★ pair.** Over the top 5 per side, minimise `s_pre + s_post + 0.1·|Δsun_el| + 0.0167·|Δsun_az| + 0.5·|Δtan inc|`. The last term keeps the relief shift common to both images.
- The golden test (Iliamna) gives ★ pre = 08-01 WV01 and ★ post = **08-26 WV02**, the first good post-event strip, chosen over the lower off-nadir 08-31 because early snow can bury a deposit (decision 8).
- **Stereo mates** (same sensor, under 5 min apart) get a badge.

**Cloud**
- GEGD has no AOI cloud geometry, and its 8-bit RGB can't run OmniCloudMask.
- The table therefore shows **"Strip cloud %"** explicitly labelled as strip-wide.
- In M2, it adds **"S2 AOI cloud (proxy)"**: when the S2 tab's OmniCloudMask result exists for a scene within ±2 h of the strip, its AOI cloud % is shown and used in ranking **[assumed → M2: how the S2 run exposes per-scene AOI cloud]**.
- Thumbnails cut to the scar are the *confirmation*, not the metric (your stated preference).

**Table**

The dock is about 360 px wide, so seven compact columns:

| Column | Content |
|---|---|
| Pick ★ | Checkbox, plus "★ before" / "★ after" |
| Date UTC (Δd) | Pre/post tint |
| Sensor | Badges: stereo, ingested, 3rd-party (separate licence) |
| Off-nadir° (level) | Visual-grade values greyed |
| Sun° | |
| AOI cover | Warning colour when it misses the epicentre |
| Strip cloud % | |

- Tooltips carry everything else: UTC and AKDT time, GSD, DEM correction, CE90 (LV3D only), catalog ID and featureId, ingest lag, the shift line.
- Selection syncs with the footprint layer in both directions; double-click streams that strip.

**Footprints and search area.** Memory layers, outline-only (blue pre, green post, dashed visual), at any scale. They are replaced on each search.

**Honest empty and failure states**

| Situation | Behaviour |
|---|---|
| **Search failed** | Mapped message (§11); the previous table is kept. |
| **Zero results, window before 2024-07** | Archive-floor message ("… use the S2/SAR tabs; ask GEGD for an ingest"). Shown only when nothing at all came back. |
| **Zero results, otherwise** | A canary `resultType=hits` query (50 km, 1 year) tells "no coverage here" apart from "query broken". |
| **No pre or no post** | One query outside the window, `count=10` (`NEAREST_COUNT`): the nearest record in the padded box can be SAR, third-party or a mosaic, or can miss the circle. The first usable strip that touches the circle is named: "nearest earlier strip 2025-06-14 (WV02, ONA 22°)". |
| **No post, event Nov–Feb** | "No Daily Take Dec–Jan at this latitude; first possible post collect is about February. Try SAR." |
| **Everything filtered** | "12 strips found, all hidden by Scene filters", with **Show them**. |

**Thumbnails (M2).** WMS PNG per strip, 320 px wide with square ground pixels, at most 4 in flight. The extent can be "Whole search area", "3 km around epicentre" or "Current map view". Cached in `~/.cache/landslide_gegd/thumbs/` (LRU, 500 MB). Two columns, Before | After, 12 per side, then More.

**Find by ID (M2).** Matches `featureId` or `legacyIdentifier` (whether `OR` is accepted is checked in M0).

## 6. Streaming and compare (flip in M1, the rest in M2)
- **Stream layer** = an XYZ layer over the WMTS GetTile template with `CQL_FILTER=featureId='…'`, set through `QgsDataSourceUri.setParam("url", …)` with `setAuthConfigId("gegdpro")`. **Never the WMTS provider.** Fallback: a WMS layer with `cql_filter`.
  - Names look like `Maxar before · 2026-08-01 WV01 · ONA 30° (stream)`. There is no `_pre_`/`_post_` token, so 3D never pairs a stream.
  - Group: `lg.name("Maxar", date_pair, radius, "stream")`, with the after-layer on top.
  - Custom properties `landslide/gegd_*`, plus vendor-aware rights metadata.
- **Replace rule.** **Stream picks** replaces only the previous stream set. Clips and scars are never touched (your keep-paid-downloads rule). Hidden streams fetch nothing.
- **Compare controls:**
  - **Flip**: toggles the after-layer; the render cache makes it instant.
  - **Blink**: `_blink_check` is private and always starts off, so a saved project never opens blinking. It toggles with signals blocked so the project isn't dirtied every tick.
  - **Opacity**, and **gamma** (display only; lifts DRA shadows at 15–25° sun).
  - **Side-by-side**: a second map view bound to a "Maxar before" **map theme**, using the view's own extent and scale sync **[assumed → M2]**.
- **Transparency guard.** If a picked strip is fully transparent over the view: "strip doesn't cover this view (or not entitled)".
- **Vivid (M5).** Offered only as **pre-event context**, and only when *every* seamline patch intersecting the AOI or scar predates the origin and no mapping-grade pre exists (Knik, Valdez, Azumi). It is labelled with the patch dates, never offered as post, and kept out of tracing provenance unless the patch date is recorded.
  - **M0 verified** that `VividStandard30Seamlines` describes the `Maxar:VividStandard` layer: texture matches the source strip inside its patch and drops at the seamline. Seams are invisible in the pixels, so dates must come from the seamlines.
  - Patch text is built dynamically. At Iliamna today: 5 patches near the epicentre, all pre-event, latest 2026-07-10; the epicentre patch is 2026-05-20.

## 7. Scar mapping (M3)

**Finding the target at run time** (`discover_role_target`). Nothing is hard-coded.
1. The layer the Volume tab would use for that role. This calls `dock.volume_tab._match_role_layers` on the project's polygon layers, and requires decision 3 so that "Landslide scars" counts as total.
2. Otherwise, a loaded layer whose *source basename* matches Volume's tokens (`Source Area (Best).shp`, `Source (Best).shp`, `Total Area.gpkg|landslide_scars`).
3. Otherwise, a case-insensitive search of the project folder for `.gpkg` and `.shp` by those tokens.
4. Only then, **ask** before creating `Total Area.gpkg|landslide_scars` with the exact schema (fid, POLYGON, `slide` TEXT, EPSG:3857).

**Guards**
- Never read the `total_area` layer.
- A per-feature centroid check warns when a polygon sits beyond max(2·loc_err, radius) from the epicentre ("misfiled?").
- The schema is checked before `slide` is written; `slide` is filled only if null.
- **Gated on a saved project.** With an unsaved project, Trace, Refine and Save are disabled: "Save the project in the event folder first". Nothing ever lands in the shared folder.

**Actions**
- **Refine selected** (the default when the slide already has a polygon): back up once per file per session, record the pre-edit geometry, `startEditing`, Vertex tool.
  - The backup goes to `scar_backups/`: a GeoPackage via the sqlite3 backup API; a shapefile by copying `.shp/.shx/.dbf/.prj/.cpg`.
- **Trace new**: add-feature tool, then **Supersede** (record, then delete the old polygon) or **Keep both** (warns that Volume measures the LARGEST polygon).
- **Save edits**: `commitChanges`, then re-open read-only and verify the count and geometry hash (Drive-sync hazard), then write provenance.
- **Never auto-commit.** On a role switch or project save the tab asks "Save scar edits / Keep editing / Discard". It never commits from inside `writeProject`.

**Provenance.** `Scar edits (VHR).gpkg|scar_edits`, beside the project and never loaded (its name matches no Volume role). One row per committed edit:
- target file, layer and fid; action; geometry before and after; area before and after;
- **the topmost visible Maxar strip** (fid, catalog ID, acquisition UTC, sensor, GSD, off-nadir, incidence, processing level, 50 m shift);
- the pre strip, co-registration state, confidence, analyst, UTC time.

Your ground-truth schema stays untouched.

**Cross-check** shown after save: ellipsoidal area (as Volume computes it), Δarea and IoU against the previous polygon, "Volume will use the LARGEST polygon: fid N", centroid distance against location error, and a ±(50 m·tan inc) source-zone band. Tracing on a visual-grade strip needs a confirmation and is recorded as LV2A.

## 8. Clip downloads (M4, after GEGD answers decision 4)

**Plan**
- **Box:** "Auto" = the discovered total polygon(s) that pass the centroid check, plus 500 m, else the map view. Alternatives: a box around the epicentre, the current view, or draw a box.
- **Grid:** the event's UTM zone (Iliamna is 32605), recorded once in the event manifest. **0.5 m** with target-aligned pixels, so every pre and post clip lands on an identical grid.
- **Size:** `limits.check_grid` (warn above 100 Mpx, refuse above 400). Above 64 Mpx a confirmation shows tiles, MB and "counts against GEGD usage (metering not visible to API keys)". The button reads "(N tiles, ~X MB)".

**ClipTask**
1. **Reuse first:** same request hash (fid, epsg, res, box, mode, version), so zero requests.
2. **Fetch 2048 px WMS PNG tiles requested directly in the event's UTM zone**, on a fixed global grid (tile origins at whole multiples of 2048 × res), into `~/.cache/landslide_gegd/tiles/`. Settled by M0:
   - UTM renders are geometrically consistent to about 0.1 px, while 4326 renders jump about 1 px depending on where each request starts.
   - PNG is bit-identical to GeoTIFF at 35% of the bytes.
   - Each tile is georeferenced locally from the bbox we requested (the server's geotransform matched requests exactly).
   - Tile size and format are part of the cache hash.
   - A 3×3 km clip at 0.5 m is about 9 tiles and about 50 MB on the wire.
3. **Validate each tile:** PNG signature and content type (not an OGC exception), requested size, alpha present.
   - A single **GeoTIFF control tile** per clip checks that the server's geotransform still equals the request, which catches a server that silently degrades resolution.
   - Never compare overlap pixels: sub-pixel resampling differs between requests even though the geometry agrees.
4. **Mask from alpha, never from 0.** The server declares nodata=0 on all bands, alpha included, so VRTs and warps use `srcNodata=None` plus the binary alpha. Otherwise true-black shadow pixels become holes. **WV01 arrives as Gray+Alpha.**
5. Mosaic the tiles in a VRT on the event grid (already in the target CRS, so there is no reprojection, only a 0.5 m snap).
6. Write `<final>.partial`, then `os.replace` (Drive safety). JPEG q90 YCbCr with an internal mask (default, about 8× smaller) or DEFLATE RGBA; overviews 2–32. GDAL options are thread-local.
7. **WV01 pan** is written as 3-band grey plus mask (or Gray+Alpha), never 1-band Byte, which the 3D tab would mistake for a DEM.
8. **Tags:** a vendor-aware `TIFFTAG_COPYRIGHT` ("Imagery © {year} {Vantor | vendor}, via NGA GEGD Pro. AEC internal use only." — wording confirmed in M0), the acquisition time, and `GEGD_FEATURE_ID/CATALOG_ID/SIDE/EVENT_ID/LICENSE`.
9. Cancel or failure leaves no final file.

**Layout**
```
<project folder>/gegd/LICENSE-AEC-INTERNAL.txt          (sponsor, purpose, credit line, no-public-release)
<project folder>/gegd/<event_id>/search.json
<project folder>/gegd/<event_id>/<event_id>_gegd.json   (manifest)
<project folder>/gegd/<event_id>/<featureId>_<cliptag>/<event_id>_<pre|post>_<YYYY-MM-DD>_maxar.tif (+ _gegd.json sidecar)
```
- The names pair in 3D with no 3D change.
- The sidecar is never `*_metadata.json`, so Fusion's cloud lookup can't attach it.
- Nothing is named `dbright`, `dndsi` or `ndsi`.
- **Keep paid downloads:** new clips go beside existing ones; re-downloading replaces only that exact file; a different box gets a new folder.
- **Stale filter** on reopen: a clip is adopted only if its box still contains the epicentre or intersects the AOI, *and* its side recomputed against the current origin matches its name. Rejects are listed as "made for a different location or time" (your stale-raster rule).

**Licensing.** Licensed pixels live only under `gegd/`. Scars and provenance are derived products and live outside it.

## 9. Downstream

| Consumer | Effect | Change |
|---|---|---|
| **3D viewer** | Clips drape and auto-pair. | When the tab adds a pair, it sets `viewer3d_tab.det_imagery_sat` to "Maxar WorldView: Imagery © {year} Vantor, via NGA GEGD Pro; AEC internal" (only if the combo is empty or holds a default). The credit then reaches the PNG figure, web export details and index with **no edit to your WIP files**. A message-bar warning appears when a 3D export runs with a GEGD drape. A proper banner comes later, after your web3d WIP lands. |
| **Fusion** | Refuses clips; lists streams as web layers. | None. A test pins it. VHR helps Fusion only through better truth polygons for the 6-event benchmark. |
| **Volume** | Uses the scar layers. Byte rasters are never offered as DEMs. | Decision 3, one line. |
| **review_package / verdict** | Unchanged. | None. Provenance lives in the side GeoPackage. |

## 10. Persistence
- `OWNERS` row `("gegd_tab", "gegd")`.
- **Persisted public widgets** (pinned by a test): `lat_edit`, `lon_edit`, `radius_spin`, `dt_edit`, `pre_days_spin` (365), `post_days_spin` (90), `ona_max_spin` (30), `show_visual_check`, `sun_min_spin` (15), `cloud_max_spin` (100 = no cap; changed from 35 in the M1 review: a whole-strip cloud cap hides strips clear over the scar, the PlanetScope lesson, and strip cloud is already priced in the score), `sensor_checks`, `third_party_check` (off), `auto_stream_check` (on), `thumb_extent_combo`, `blink_spin`, `scar_role_combo`, `confidence_combo`, `clip_mode_combo`, `clip_km_spin`, `res_combo`, `compress_combo`.
- Combo **labels are final from day one**, because combos are stored by label.
- Everything dynamic is `_private`.
- On `readProject`, `adopt_layers()` rebuilds stream, clip and compare state from layer custom properties.
- Any later default change goes through `RETIRED_DEFAULTS` plus a `VERSION` bump.

## 11. Errors, usage, API drift
- **Classified on (service, status, body)**, so a 401 means "key not sent, mistyped or expired" only on streaming services. On Discovery, usage, API-key or analytics endpoints the same 401 means "not available to API keys".
- 403 "Malformed token" → wrong auth scheme. Other 403 → expired, revoked or outside entitlement. 404/410 → service moved or retired (names the service).
- 429/5xx/timeouts → 3 retries with 5/10/20 s backoff, honouring `Retry-After`.
- An OGC exception XML inside a 200 is parsed. A `NOT IN` parse error triggers one retry with the other form. A size error halves the tile size once. Usage, limit or quota text stops downloads.
- **Drift:** every path is a constant; the host is editable (behind the HostGuard confirmation); required-field checks; namespace discovery and an **AccessCheckTask** matrix in M5 (WFS, WMS, WMTS, WCS, GeoTIFF, plus expected 401/410s, each with status and latency).
- **Frugality:** 12 thumbnails per side, all cached; hidden streams fetch nothing; clips reused by hash; raw tiles cached; size confirmation.

## 12. Tests (offline, under `tests/run_all.sh`, temp auth DB and config dir, no live key)

**Fixtures are synthetic because the repo is public.**
- `tests/fixtures/gegd/make_fixtures.py` builds:
  - FeatureCollections that follow the real schema, with fake UUIDs and plausible coordinates, and no third-party, PRIVATE_FEATURE or order-number fields;
  - GDAL-made RGBA and grey tiles with a marker tag;
  - error bodies.
- Real-response fixtures, if wanted, live in gitignored `out/gegd_fixtures_local/`, and tests that need them skip when they are absent.
- `test_gegd_fixtures_clean.py` fails on:
  - any base64 run of 40+ characters that decodes to a UUID (the key's shape);
  - emails;
  - `PRIVATE_FEATURE`, `factoryOrderNumber`, `THIRD_PARTY`;
  - any TIFF without the generator's marker.

| Test | Asserts |
|---|---|
| `test_gegd_api` | BBOX is always `'CRS:84'` lon-first; quoting, encoding, Z times; no auth in any URL; parser (lon/lat rings, cloud as a fraction, `B0…` IDs not decoded, `SchemaDrift`); `classify_http` per service; `redact` (all forms). |
| `test_gegd_rank` | Strict split (−1 s is pre, exactly origin is post); the previous-AKDT-day case under `TZ=America/Anchorage`; relief-shift values; coverage geometry; golden Iliamna-shaped ★ pair; visual and context classes never ★; empty-state texts. |
| `test_gegd_auth` | Header arrives on blocking, QgsTask, XYZ and WMS requests. A missing authcfg sends nothing. **HostGuard strips the header off-host (XYZ and blocking)**, keeps it on `pro.gegd.com`, and survives dock teardown. Fallback-mode URIs carry no `authcfg`. |
| `test_gegd_net` | A cancel at every point of a request ends its QgsTask within 2 s. That covers: before it starts; on the worker inside `get()` (request preprocessor and reply preprocessor, the old race window itself); from the GUI thread 0–15 ms in; mid-transfer from a trickling server; during a retry wait; and a 12-task burst. That is 85 cancels, and every reply is deleted. The guard sees a `finished` for every request it counted, and usage is one per request, initiator kept. 6 threads × 60 quick fetches: none fails. The key reaches the allowed host from a worker and is stripped off-host. A missing authcfg creates no request. A silent server and headers-then-silence are timeouts, retried, never a truncated 200. A 3xx to another host or port is not followed. A same-origin 3xx (absolute or relative) is followed; a loop stops at 5 hops. KeyTestTask names the newest Vantor optical strip. |
| `test_gegd_project_has_no_key` | Save a `.qgz` with stream layers, unzip it: no dummy key, `authcfg=gegdpro` present; `key_edit` and `_id_edit` are not persisted. |
| `test_gegd_layers` | Tile requests keep `CQL_FILTER` with z/x/y filled; never a WMTS-provider URI; properties and rights set. |
| `test_gegd_tab` | 7 tabs. Construction never calls `authManager()`. Persisted widget set is pinned. **Use it in every tab** fills the tab. `search.json` restore. `_out_dir()` under `results_dir()`. No `out_edit` string. Unsaved project → scar actions refused, nothing written. Teardown cancels tasks and stops the blink. |
| `test_gegd_scars` | Role discovery on a mock project shaped like Iliamna (shapefile source, "Landslide scars" gpkg layer, misfiled `total_area`). Shapefile and GeoPackage backups. Supersede/keep. Provenance rows. Volume matches the targets and never "Scar edits (VHR)". No commit from `writeProject`. |
| `test_gegd_clip` | Tiles abut; target-aligned grid; `check_grid` refuses 17 km at 0.5 m. Output in UTM, mask from alpha, **a black pixel with alpha 255 stays valid**, tags, overviews. WV01 never 1-band. Same hash = zero fetches. A second strip keeps the first. Cancel leaves no file. A degraded tile aborts. Thread-local GDAL options. |
| `test_gegd_downstream` | Fusion rejects all clip layouts; Fusion's cloud lookup ignores `*_gegd.json`; 3D pairs and dates the stems; stale filter (epicentre moved 50 km, or origin moved so the side flips). |

A **live smoke test** (`tests/live_gegd_smoke.py`, gated on `GEGD_LIVE=1`, not part of `run_all.sh`) uses your real profile's auth DB via an env-var path. It prints counts only, never values.

## 13. Milestones and acceptance (each accepted in QGIS 3.44.11 on real events)

### M0: Pre-flight (S)

Runs headless with a temp auth DB, the key read from a mode-600 scratch file, at most about 20 read-only requests, results recorded in memory as pass/fail and counts only.

| # | Check |
|---|---|
| 1 | APIHeader authcfg → WFS `resultType=hits` returns `numberMatched`. |
| 2 | XYZ stream for Iliamna 08-26 renders, and a different featureId renders different pixels. Tried on **both** WMTS paths, plus the WMS fallback once. |
| 3–5 | WMS GeoTIFF at 2048² in EPSG:4326; GeoTIFF in EPSG:32605; WV01 band layout. |
| 6 | `NOT IN` vs `<>`; `featureId … OR legacyIdentifier …`. |
| 7 | Two overlapping tiles are identical in the overlap (no per-request DRA seams). |
| 8 | Capabilities fetched via the authcfg contain no key. |
| 9 | **HostGuard** strips the header for another host against the real authcfg. |
| 10 | Network Logger: is the header shown or exported? |
| 11 | `VividStandard30Seamlines` ↔ `Maxar:VividStandard`; what `RDOG_BASE` is. |

Also in M0: fixture generator and scanner test; the decision-4 question asked.

**Accept when** every check has a recorded result, and the authcfg/XYZ path is confirmed or the fallbacks are switched in **before any UI work**.

**M0 RESULT (2026-10-13, 59 read-only requests across two independent agents, 0 key copies in any output). Done.**

| Checks | Result |
|---|---|
| 1, 2, 3, 4, 5, 6, 8, 9, 11, 12 | Pass. **The authcfg + XYZ streaming path works as designed, so no fallback is needed.** |
| 7 | No tone seams, but 4326 requests jitter about 1 px. Clips therefore use UTM-grid PNG tiles (§8). |
| 10 | Network Logger exposes the key; mitigated by the warning (§3). |

Folded into the plan: HostGuard https + port + manual-redirect rules, counting outside the preprocessor, RDOG_BASE as multi-date context, PNG transfer, WV01 Gray+Alpha, and alpha masks.

**Still to do from M0:** the synthetic fixture generator and its scanner test, which land with M1's first commit.

### M1: Triage and stream (M–L)

**Scope:** `gegd_api`, `gegd_rank`, `gegd_auth` (with HostGuard), `gegd_net`, SearchTask, `gegd_layers`; the tab's key, event, filter, table, footprint, log and flip parts; the `dock.py`, `plugin.py` and `project_state.py` edits; the tests above for these parts.

**Accept on Iliamna**
1. Save the key → "Key works…". The field clears, and QGIS3.ini plus a saved `.qgz` contain 0 key hits.
2. **Use it in every tab** fills the tab, with the radius from the location error.
3. Search returns in under 5 s without freezing QGIS. ★ = 08-01 WV01 → 08-26 WV02. 08-31 and the 09-22 stereo pair are listed; no Vivid.
4. The pair auto-streams. The deposit shows at 1:10,000 and renders at 1:100,000. Flip is instant.
5. Peters Dome 2023 gives the archive-floor message. Knik shows the 09-02 post and an honest pre-side message.
6. Forget key → Search gives a clear 401 message and the table is kept.
7. Reopen in a fresh QGIS: (a) stream layers render **without opening the dock**; (b) filters and the table restore **when the dock is opened**.
8. A layer edited to point `authcfg=gegdpro` at another host gets no key.
9. `tests/run_all.sh` is green.

### M2: Visual triage and compare (M)

**Scope:** gallery with 3 extents, table↔footprint sync, double-click add, blink/opacity/gamma/side-by-side, find by ID, third-party toggle, S2 cloud proxy, re-rank with no requests.

**Accept when:**
- **Azumi:** 12 thumbnails per side within 15 s, and "Current map view" re-renders them at the scar.
- **Hubbard:** ★ = 06-01 GE01 → 08-05 WV02, or a tooltip explaining why not.
- An off-nadir 30→25 change re-ranks with 0 requests.
- Blink makes no requests.
- Side-by-side stays in sync.

### M3: Scar mapping (S–M)

**Accept on a copy of the real Iliamna event folder and project**, with the `.shp` source layer and the "Landslide scars" gpkg layer:
- Refine the total on 08-26, then flip and trace the source on 08-01: one button each, plus vertices.
- Backups exist (gpkg and shp sidecars). Provenance rows name 08-26 for total and 08-01 for source. `total_area` is never read.
- Volume assigns both roles with no drop-down change (decision 3), measures the refined polygon, and its verdict is not "wrong place".
- An unsaved project refuses scar actions.
- Saving the project mid-trace prompts and does not commit.
- Then, with your go-ahead, run it once on the real folder.

### M4: Clip downloads (L), gated on decision 4

**Accept on Iliamna**
- Auto box at 0.5 m. Two UTM 5N files on the same grid, which overlay the streams within 1 px.
- The 3D tab lists "maxar (2026-08-01 → 2026-08-26)" with no 3D code change, and its credit line reads "Imagery © 2026 Vantor…".
- Fusion ignores both clips.
- Repeating the download makes 0 requests. Downloading 08-31 keeps 08-26. Cancelling at 50% leaves no file.
- Moving the epicentre 50 km marks the old clips stale. A 17 km box is refused.
- **Hubbard** pairs GE01 → WV02 the same way.

### M5: Hardening and context (S–M)
- Vivid pre-event fallback (seamline-verified): Iliamna shows "all Vivid patches predate the event (latest …)" with the dates read live, and a mocked post-origin patch triggers the warning.
- AccessCheckTask; a mocked 410 and a mocked namespace change give clear messages.
- Key-age warning; README and metadata; `tests/README.md` rows staged at hunk level.

### M6: Co-registration, optional (M)

Phase correlation on luma at 2 m, masking:
- the scar plus 300 m;
- saturated and dark pixels;
- RGI glacier outlines (reusing Fusion's loader).

It is applied by changing the geotransform only, so it reverts exactly. **Accept when:** Iliamna post→pre gives ≥5 windows and MAD ≤2 m, and a low-texture case is refused.

### Later
- 3D export banner (after your web3d WIP lands).
- Swipe tool.
- Parallel tile fetches.
- WCS backend.
- HD / P3DR (`EnhancedImagery`, `EnhancedAccuracy`) layers.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Key leaks via the `.qgz`, logs, fixtures or the public repo | authcfg-only URIs, HostGuard, `redact()`, no subprocess, synthetic fixtures plus scanner, no-key-in-project test, rotation. |
| Key sent to a foreign host via a crafted project | HostGuard installed at plugin load; test. |
| WMTS provider shows the wrong strip | Never used; test. |
| Axis order silently gives 0 results | CRS:84 form only; client-side intersect; canary query. |
| Downloads not allowed, or metered and degraded | Decision 4 before M4; tile geotransform check; caching and reuse; confirmation dialog. |
| WCS end of life | Not on the critical path. |
| 8-bit DRA used as radiometry | Fusion refuses (test); naming avoids index hints; "visual only" tooltips. |
| LV2A / high off-nadir / pre-event-DEM shift in steep relief (about 34 m per 50 m drop at 30°; scars 84–100% on ice) | Mapping/visual tiers, off-nadir ≤30° default, tan(incidence) ranking, common-mode pair term, shift line and band, LV2A confirmation, M6. |
| Damage to your ground truth | Run-time role discovery, centroid checks, backups (gpkg and shp), provenance, no auto-commit, verify after commit, M3 on a copy first. |
| Cross-event mixing in unsaved projects | Scar actions require a saved project; clips are event-scoped. |
| Stale clips under a reused event id | `cliptag` folders; geography plus strict-side adopt rule. |
| Licensed pixels leave AEC | `gegd/` rule, LICENSE file, TIFF and layer rights, 3D credit via `det_imagery_sat`, export warning, decision 5. |
| Sparse VHR or winter gap read as a bug | 365/90 windows, nearest-outside hints, winter and archive messages pointing to SAR, Vivid pre fallback. |
| Collision with your WIP | 5 small `dock.py` hunks re-diffed before staging; temp-index commits; edit the main checkout, never a worktree. |
| Drive sync serves stale reads | Caches in `~/.cache`; `.partial` + `os.replace`; post-write verification; git checks before and after edits. |

## 15. All decisions, with recommendations

| # | Decision | Recommendation | If changed later |
|---|---|---|---|
| 1 | Fusion can't use Maxar pixels (8-bit DRA RGB, no SWIR) | **ANSWERED: yes.** VHR feeds Fusion only as truth polygons | — |
| 2 | Scar edits target | **ANSWERED: A**, your existing scar files (the layers Volume measures), with a backup each session and provenance; M3 is accepted on a copy of Iliamna first | — |
| 3 | "Landslide scars" not matched by Volume | **ANSWERED: yes.** One-line `TOTAL_SUBJECTS` addition | — |
| 4 | Downloads permitted? limits? credit wording? | *Open.* Not a technical requirement: the server serves GeoTIFF. Proposed: **don't gate M4**; keep the size confirmation, the degraded-tile check and caching; asking support is optional | — |
| 5 | Clip storage | **ANSWERED: the shared drive is AEC-only**, so clips go beside the project (`<project>/gegd/`) | Moving clips later needs a re-adopt |
| 6 | Key storage | **ANSWERED: yes.** QGIS auth DB | — |
| 7 | Search windows | 365 d pre / 90 d post, nearest-first | RETIRED_DEFAULTS + VERSION |
| 8 | Ranking taste | First good post (Iliamna 08-26) over lowest off-nadir (08-31) | Weights and golden test change together |
| 9 | Cloud | Strip % column + same-day S2 AOI-cloud proxy + thumbnails to confirm | — |
| 10 | Auto-stream ★ pair after Search | On | RETIRED_DEFAULTS |
| 11 | Clip defaults | 0.5 m UTM grid, scar + 500 m box, confirm above 64 Mpx, JPEG q90 + mask | RETIRED_DEFAULTS |
| 12 | Third-party imagery (ICEYE, Umbra, Capella, SkySat, BlackSky) | Hidden by default, tagged "separate licence" | — |
| 13 | Provenance | Side GeoPackage; ground-truth schema untouched | Adding columns later needs a per-file confirmation |
| 14 | Low/high source areas and centerlines | Stay with the Volume tab in v1 | — |
| 15 | Milestone order | M1 → M2 → M3 → M4 (tracing on streams gives value before downloads) | Swap M3/M4 if you need 3D drapes first |
| 16 | Tab name and position | "Maxar VHR (GEGD)", after PlanetScope | Label change is cosmetic |

---

## Appendix A: Request templates (header `maxar-api-key` added by `authcfg=gegdpro`)

**Search:** see §5. For a count only, use `resultType=hits` without `outputFormat` and read the `numberMatched` attribute.

**Thumbnail / clip tile (WMS):**
```
https://pro.gegd.com/streaming/v1/ogc/wms?service=WMS&version=1.1.1&request=GetMap&layers=Maxar:Imagery&styles=
  &srs=EPSG:4326&bbox={lon0},{lat0},{lon1},{lat1}&width={w}&height={h}&format=image/png&transparent=true
  &exceptions=application/vnd.ogc.se_xml&cql_filter=featureId='{fid}'
```
For a clip tile use `format=image/geotiff` (RGBA, alpha = footprint).

**Stream (XYZ over WMTS GetTile; path settled in M0):**
```
https://pro.gegd.com/streaming/v1/ogc/wmts?service=WMTS&request=GetTile&version=1.0.0&layer=Maxar:Imagery&style=raster
  &tilematrixset=EPSG:3857&tilematrix=EPSG:3857:{z}&tilerow={y}&tilecol={x}&format=image/png&CQL_FILTER=featureId='{fid}'
```

**WCS (backend B, not on the critical path):**
```
https://pro.gegd.com/streaming/v1/ogc/wcs?service=WCS&version=2.0.1&request=GetCoverage&coverageId=Maxar__Imagery
  &subset=Lat({lat0},{lat1})&subset=Long({lon0},{lon1})&format=image/tiff&CQL_FILTER=featureId='{fid}'
  [&outputCrs=http://www.opengis.net/def/crs/EPSG/0/{326zz}]
```

**Vivid seamlines:**
```
https://pro.gegd.com/basemaps/v1/seamlines/ows?service=WFS&version=2.0.0&request=GetFeature
  &typeNames=Maxar:VividStandard30Seamlines&outputFormat=application/json
  &bbox={lon0},{lat0},{lon1},{lat1},urn:ogc:def:crs:OGC:1.3:CRS84
```

## Appendix B: Commit recipe (your WIP and your staged deletion stay untouched)
1. Re-diff `dock.py`; build `mine.patch` with only my hunks plus whole new files.
2. `GIT_INDEX_FILE=$tmp git read-tree HEAD`
3. `GIT_INDEX_FILE=$tmp git apply --cached --recount mine.patch`
4. `GIT_INDEX_FILE=$tmp git commit`
5. `git reset -q -- <my paths>`
6. Check `git show --stat HEAD`, and confirm `git status` still shows your staged deletion and your WIP.

Nothing licensed and nothing key-shaped is ever committed: the repo is public.
