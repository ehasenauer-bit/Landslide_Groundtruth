# NISAR in the landslide ground-truthing plugin — assessment and implementation plan

**Written 2026-09-16.** Every availability number below was measured live against NASA CMR
and ASF on that date, not taken from documentation — because the documentation and the
archive disagree in ways that matter (see §3). Re-measure before trusting the specifics;
the *method* is what should outlive this file.

---

## 1. Is NISAR L-band?

Yes — and it is the only civil SAR mission that is L-band **and** something else at the
same time.

| | L-SAR | S-SAR |
|---|---|---|
| Built by | NASA/JPL | ISRO |
| Centre frequency | 1.257 GHz | 3.2 GHz |
| Wavelength | ~24 cm | ~9.4 cm |
| Distribution | ASF DAAC / NASA Earthdata (free, open) | ISRO Bhoonidhi |

Both radars share one 12 m deployable mesh reflector using SweepSAR, giving a 242 km swath
at 12-day exact repeat. Launched 2025-07-30 on GSLV-F16.

For comparison, the plugin's current SAR source — Sentinel-1 — is **C-band, 5.405 GHz,
~5.5 cm**. NISAR's wavelength is roughly four times longer. That is the whole point and
also the whole risk; see §6.

### What about TanDEM-L?

Not a thing you can get data from, and probably never will be on this project's timescale.

TanDEM-L is a **proposed** DLR mission — two identical L-band satellites flying in
formation, conceived as the L-band successor to TanDEM-X, aimed at weekly global
deformation and 3D vegetation structure. It completed Phase A and a Phase B1 study years
ago and has been sitting at "awaiting final approval and funding" ever since. CEOS still
lists its status as *considered* with a notional December 2028 launch, which in practice
means unfunded. No hardware, no launch date anyone is holding to, no archive.

Don't confuse it with **TanDEM-X**, which is real, flying since 2010, X-band, and the
source of the global 12 m DEM — a different mission with a confusingly similar name.

The L-band missions that actually matter for planning:

| Mission | Band | Status | Relevance here |
|---|---|---|---|
| **NISAR** | L (+S) | Flying, open archive | The subject of this document |
| ALOS-2 / PALSAR-2 | L | Flying since 2014 | Restricted data, JAXA licensing — not viable |
| ALOS-4 / PALSAR-3 | L | Launched 2024 | Same licensing problem |
| SAOCOM 1A/1B | L | Flying (CONAE, Argentina) | Limited Alaska tasking, awkward access |
| **ROSE-L** | L | ESA Copernicus, ~2028 | The one to watch — will be free and open like Sentinel-1 |
| TanDEM-L | L | Proposed, unfunded | Ignore |

If the goal is "a free and open L-band archive for Alaska landslides," NISAR is the only
option today and ROSE-L is the only other one coming.

---

## 2. Verdict

**Yes — add NISAR GCOV, before/after amplitude only. It is measured working on Iliamna. No
interferometric products. The guardrail is the hard part, not the plumbing.**

> **Update, same day (§11):** GCOV amplitude and GUNW coherence were both run end-to-end on
> the Iliamna pair after this section was first written. GCOV **detects the slide**
> (13.2 dB at 0.1 km from the seismic point, ~3× scene background). GUNW coherence
> **does not** (no signal at the event; the terrain was already decorrelated before it
> happened). That retires the "L-band HH is unvalidated" risk and reverses this document's
> original speculation that GUNW was the more promising product. §11 has the numbers.

Four findings drive everything else:

1. **The pixels are reachable without downloading 6 GB per scene.** A GCOV granule is a
   6.0 GB HDF5 file. Measured today: a georeferenced 16 km × 16 km HH+HV window over
   Iliamna came back in **16.5 seconds** over `/vsicurl` with HTTP range reads, using the
   Earthdata credentials already in `~/.netrc`. This was the make-or-break question and
   the answer is favourable.

2. **The date guardrail cannot be a floor date.** NISAR availability is a set of intervals
   with holes in it, one of which is a 13-day period where the instrument acquired nothing
   anywhere on Earth. A single "earliest valid date" constant gives wrong answers in both
   directions.

3. **CMR's own collection metadata lies about coverage.** Both GCOV collections advertise
   `2025-10-01 → ongoing`. The actual granule holdings are nothing like that. Any guardrail
   built by reading collection temporal extents will confidently tell the analyst that data
   exists for months where there is none.

4. **L-band HH amplitude detects the Iliamna slide; L-band coherence does not.** Measured,
   not assumed — §11. This is the finding that makes the feature worth building, and the
   one that says to build only half of what looked attractive.

---

## 3. What NISAR actually holds right now

Measured 2026-09-16 by counting CMR granule hits per month, globally, for both GCOV
collections.

```
NISAR_L2_GCOV_BETA_V1                    NISAR_L2_GCOV_PROVISIONAL_V1
  2025-09        0                         2025-09        0
  2025-10    1,675                         2025-10       44
  2025-11    7,141                         2025-11      556
  2025-12    8,818                         2025-12      719
  2026-01    5,893                         2026-01      745
  2026-02        0   <-- beta stops         2026-02      727
  2026-03        0                          2026-03      664
  2026-04        0                          2026-04      788
  2026-05        0                          2026-05      853
  2026-06        0                          2026-06   20,465   <-- provisional ramps
  2026-07        0                          2026-07   36,898
  2026-08        0                          2026-08   29,831
  2026-09        0                          2026-09   20,227
```

Read together, the archive has four regimes:

| Period | State | What the analyst should be told |
|---|---|---|
| before 2025-10-17 | **Nothing exists** | NISAR had not begun science acquisition |
| 2025-10-17 → 2026-01-20 | Beta, dense | Usable; beta calibration caveats apply |
| 2026-01-21 → 2026-06-16 | **Sparse hole** | ~700–850 granules/month globally — an order of magnitude below the beta period and ~3% of the current rate. This is targeted acquisition only, not systematic coverage. ASF is reprocessing this span; full record expected end of 2026 |
| 2026-06-17 → present | Provisional, dense | Full systematic coverage, 36–72 h latency |

### The instrument gap

Daily counts across the period ASF documents as a permanent gap:

```
2026-07-27   1,204
2026-07-28       0   <-- gap starts
   ...          0     (13 consecutive days, globally zero)
2026-08-09       0
2026-08-10   1,352   <-- resumes
```

The documented gap is "July 27 – August 10"; the archive says the last acquisition is
**2026-07-27** and the first after is **2026-08-10**. This is not a coverage hole over
Alaska — it is global. Nothing was acquired.

**The Iliamna event (2026-08-08 11:37) sits inside this gap.** More on that below.

### Why collection metadata must not be used

```
CMR collection temporal extents:
  NISAR_L2_GCOV_BETA_V1          2025-10-01 -> ongoing      (reality: stops 2026-01-20)
  NISAR_L2_GCOV_PROVISIONAL_V1   2025-10-28 -> ongoing      (reality: sparse until 2026-06-17)
```

Both claim "ongoing" coverage from October 2025. A guardrail that reads
`TemporalExtents.RangeDateTimes` — the obvious implementation — would pass an event dated
2026-03-15 as fully covered, and the analyst would get an empty result with no explanation.
**Availability must be established by counting granules, never by reading collection
extents.**

---

## 4. What the Iliamna test case actually shows

This is the only event in the existing benchmark that falls in the NISAR era at all —
Peters Dome (2023), Valdez, Hubbard and Azumi all predate launch. A live search over the
Iliamna AOI (60.0272, −153.061, 8 km radius, ±60 days) returned **37 GCOV granules**:

```
nearest pre-event   2026-07-25 14:30   gap -13.9 d   track 078 ascending
nearest post-event  2026-08-11 14:22   gap  +3.1 d   track 150 ascending
                    ^^^^ the 13-day instrument gap sits between them
```

Same-track pairs available (viewing geometry preserved, which the tab already requires):

| Track | Pass | Pre | Post | Separation |
|---|---|---|---|---|
| 150 | asc | 2026-07-18 | 2026-08-11 | 24 d |
| 006 | asc | 2026-07-20 | 2026-08-13 | 24 d |
| 078 | asc | 2026-07-25 | 2026-08-18 | 24 d |
| 015 | desc | 2026-07-21 | 2026-08-14 | 24 d |

So change detection **is** possible for Iliamna — pre/post never required an image on the
event day. But be clear about what is lost: NISAR can constrain the Iliamna scar's
*existence and extent*, and can contribute nothing whatsoever to its *timing*. The 13-day
blind window is wider than the seismic uncertainty by orders of magnitude. For an event
whose whole provenance is an AEC seismic detection, NISAR here is a geometry witness, not a
chronology witness.

Also note the 24-day pre→post separation on every track. The tab already warns above 36
days that "snow-state or seasonal drift can read as change" (`sar_tab.py:2020`). 24 days in
August at Iliamna is inside that threshold but not comfortably.

---

## 5. Access mechanics — measured, not assumed

### Auth already works

NISAR sits on the same stack the plugin already talks to: NASA CMR for discovery, ASF DAAC
behind Earthdata Login for pixels. `opera_s1.py` already implements exactly this:
`edl_session()` prefers `EARTHDATA_TOKEN`, falls back to `~/.netrc`, and `download()`
detects the EDL redirect bounce and raises something actionable.

Confirmed on this machine:

```
~/.netrc has an earthdata entry            yes
EARTHDATA_TOKEN                            unset (netrc path is the live one)
.h5 anonymous                              401 -> urs.earthdata.nasa.gov/oauth/authorize
.h5 with netrc + cookie jar                206, correct HDF5 magic 894844460d0a1a0a
arbitrary byte range (1000000-1000099)     206, 100 bytes  -> range reads work
browse PNG, fully anonymous                200, 1.5 MB     -> no auth needed for previews
```

Two things follow. **The cookie jar is mandatory** — EDL's redirect chain needs it, which
is why `requests.Session` works and a bare `HTTPBasicAuthHandler` silently fails (already
recorded in project memory as an Earthdata gotcha; it applies identically here). And
**previews are free**: the per-polarization browse PNGs are anonymous, so the tab's gallery
and thumbnail path costs nothing and needs no credentials.

### The 6 GB problem, and why it isn't one

A single GCOV granule:

```
NISAR_L2_PR_GCOV_027_150_A_032_4005_DHDH_A_20260811T142220_..._001.h5   6,442,450,944 bytes
```

Downloading that to render an 8 km AOI would be absurd. It is not necessary. Measured, in
QGIS-LTR's GDAL 3.12.3:

```
open container over /vsicurl                    7.8 s   (46 subdatasets enumerated)
rebuild georeferencing from coordinate arrays   3.6 s
read 1600x1600 HH window (16 km at 10 m)        5.3 s   99% valid
read 1600x1600 HV window                        7.2 s   99% valid
                                        TOTAL  16.5 s   vs 6.0 GB download
gamma-0 dB, p5/median/p95    HH  -16.3 / -9.6 / -1.5
                             HV  -23.9 / -17.3 / -9.4
```

Those values are physically sensible — an ~8 dB HH−HV ratio is what natural terrain gives —
so this is real calibrated data, not a misread buffer.

### The georeferencing trap

This is the one genuinely awkward mechanic, and it is not in the ASF documentation.

GDAL gained a native NISAR driver in **3.13.0**. QGIS-LTR here ships **GDAL 3.12.3**, so
that driver does not exist:

```
GDAL 3.12.3   HDF5 YES   netCDF YES   NISAR NO
```

ASF's documented workaround is to rename `.h5` → `.nc` (or prefix `NETCDF:`) so the netCDF
driver picks up the CF geolocation. **That workaround does not work for remote files on
macOS:**

```
NETCDF:"/vsicurl/...":/science/...  ->  RuntimeError: Opening a /vsi file with the netCDF
                                        driver requires Linux userfaultfd to be available.
```

It is Linux-only for `/vsi` paths. On macOS it works only after the whole 6 GB file is on
local disk — which defeats the entire approach.

Using the `HDF5:` prefix instead reads pixels correctly but returns an identity
geotransform and no CRS. The fix is to rebuild the georeferencing from the file's own
coordinate arrays, which are tiny and cheap to read remotely:

```python
mds = gdal.OpenEx(url, gdal.OF_MULTIDIM_RASTER)
g   = mds.GetRootGroup().OpenGroupFromFullname("/science/LSAR/GCOV/grids/frequencyA")
x   = g.OpenMDArray("xCoordinates").ReadAsArray()
y   = g.OpenMDArray("yCoordinates").ReadAsArray()
epsg = int(next(a for a in g.OpenMDArray("projection").GetAttributes()
                if a.GetName() == "epsg_code").Read())
px, py = float(x[1] - x[0]), float(y[1] - y[0])
gt = (x[0] - px / 2.0, px, 0.0, y[0] - py / 2.0, 0.0, py)   # centres -> corner
```

Verified against the Iliamna granule: EPSG 3413, 10 m pixels, 35280 × 35280 grid,
geotransform `(-3383280.0, 10, 0, 1095840.0, 0, -10)`, and (60.0272, −153.061) lands at
col 22685, row 6653 — inside the grid, where the window read then returned valid data.

**Do not assume UTM.** ASF's GCOV documentation says products are "projected to the
appropriate UTM zone for their location." This Alaska granule is **EPSG 3413, NSIDC Polar
Stereographic North** — at 60°N, well outside where anyone would expect polar stereo. Read
`projection@epsg_code` from every granule; never infer the CRS from latitude. Two granules
over the same AOI on different tracks are not guaranteed to share a CRS, so reproject to a
common AOI grid before differencing rather than assuming alignment.

One Python gotcha worth writing into the code: `gdal.Open(...).GetRasterBand(1)` on one line
lets the dataset be garbage-collected and invalidates the band
(`TypeError: ... argument 1 of type 'GDALRasterBandShadow *'`). Hold the dataset reference.

### Where the code can live

The venv can do the pixels but not the georeferencing:

```
./venv:  rasterio 1.5.0 (GDAL 3.12.1, HDF5 driver yes)  numpy 2.5.0  fsspec  requests
         h5py MISSING    osgeo MISSING
```

rasterio read the same AOI window in 3.8 s with an identical −9.6 dB median — but exposes
only 6 metadata tags, no CRS, no coordinate arrays, and no multidimensional API. The
georeferencing rebuild above needs `osgeo`'s multidim API or `h5py`, and the venv has
neither. The granule's ISO XML sidecar carries `EPSG::3413` but no grid origin or spacing,
so it can't close the gap either.

This is less limiting than it looks, because it matches how the tab already works: the
subprocess does **search**, and the tab itself fetches **pixels** (`_render_scenes`,
`_cd_compute` and the `_cd_tif_loaded` chain all run in QGIS Python, where `osgeo` is
present and proven). So NISAR search goes in the venv; NISAR pixel reads go in the tab, on
a background task. No new dependency in either place.

---

## 6. The science caveat, stated plainly

The plugin's SAR detector is validated on **Sentinel-1 C-band VV** — Peters Dome 2023, and
the MAX-pool-of-four-detectors design recorded in the project notes. NISAR over Alaska land
acquires **dual-pol HH/HV** (the `DHDH` field in every granule name above). So a NISAR
change map differs from the validated product in two independent ways at once: C→L band and
VV→HH polarization.

What that means concretely:

- **L-band penetrates.** At ~24 cm, vegetation canopy, dry snow and glacier firn are far
  more transparent than at C-band. Given that the benchmark scars are 84–100% *on ice*,
  this could be a genuine advantage — L-band should see the ice surface and shallow
  subsurface where C-band sees the snowpack — or it could change the scattering regime
  enough that the existing thresholds mean nothing.
  **Measured on Iliamna (§11): it is the advantage.** The existing `|log-ratio|` gives a
  13.2 dB peak 0.1 km from the seismic point against a 3.2 dB scene background. The
  detector transfers to L-band HH without retuning on this event. One event is not a
  validation set, but it is no longer an open question whether the approach works at all.
- **HH ≠ VV.** The project's own measurement that polarity is unreliable (53% of Iliamna
  slide pixels brighten, 47% darken → use `|log-ratio|`) was made at C-band VV. It should
  be re-measured at L-band HH before being assumed to carry over.
- **Gamma-0 is gamma-0.** The one thing that *does* transfer cleanly: GCOV delivers
  terrain-corrected gamma-naught in linear power, exactly like the Sentinel-1 RTC the tab
  already consumes. The log-ratio arithmetic applies unchanged. It is the *thresholds* and
  the *interpretation*, not the math, that need revalidation.

**Do not ship a NISAR change map as equivalent to the Sentinel-1 one.** Label it as
L-band HH, unvalidated, and treat the first Iliamna run as an experiment whose result is
itself the finding.

---

## 7. Where it belongs: a source option in the SAR tab

Recommended: **a source selector inside the existing SAR tab**, not a new tab.

The reason is that every NISAR candidate maps onto the existing candidate contract with no
gaps. `sar_imagery._candidate` documents that contract as
`id / date / gap_days / cloud_pct / source / thumb_url / geometry / bbox` plus
`orbit_state / relative_orbit / polarizations`, and promises that anything honouring it
gets the table, gallery and footprint code for free. NISAR honours all of it:

| Field | NISAR source |
|---|---|
| `id` | CMR `GranuleUR` |
| `date` | `_20260811T142220_` in the granule name |
| `gap_days` | computed, as now |
| `cloud_pct` | `None` — radar, same as Sentinel-1 |
| `source` | `"NISAR"` |
| `thumb_url` | per-pol browse PNG, **anonymous** |
| `geometry` / `bbox` | CMR `SpatialExtent` |
| `orbit_state` | the `A`/`D` field in the granule name |
| `relative_orbit` | the `REL` field (078/150/006 asc, 116/015 desc over Iliamna) |
| `polarizations` | decoded from `DHDH` → `['HH','HV']` |

Decoding the granule name — this is the parsing contract, and it mirrors `opera_s1.py`'s
existing `_ACQ_RE` / `_TRACK_RE` approach:

```
NISAR_L2_PR_GCOV_027_150_A_032_4005_DHDH_A_20260811T142220_20260811T142259_P05023_N_F_J_001
         │  │    │   │   │  │   │    │      │
         │  │    │   │   │  │   │    │      └─ acquisition start
         │  │    │   │   │  │   │    └──────── polarization mode (DH = HH+HV)
         │  │    │   │   │  │   └───────────── radar mode
         │  │    │   │   │  └───────────────── frame
         │  │    │   │   └──────────────────── pass direction  A = ascending, D = descending
         │  │    │   └──────────────────────── relative orbit  <- pair pre/post on this
         │  │    └──────────────────────────── cycle
         │  └───────────────────────────────── product
         └──────────────────────────────────── PR = provisional, BE = beta
```

Against that, a separate tab would duplicate the pairing logic, the ~600-line change
detection block, the styling, the export path and the Fusion hand-off — all to avoid
branching in a handful of methods. The reuse argument wins decisively.

What genuinely differs and must be branched:

- **The Planetary Computer key box** (`_build_key_box`, `_pc_key`, `_test_key`,
  `_with_key`) is meaningless for NISAR. Hide it when the source is NISAR; NISAR's
  credential story is Earthdata, which belongs in its own small status line ("Earthdata:
  ~/.netrc ✓" / "not configured").
- **The render controls** (`_render_query`, `_preview_url`) build Planetary Computer data-API
  URLs with server-side `rescale`/`expression`. NISAR has no such service. Previews come
  from the static browse PNGs, so the dB-range and stretch controls have no effect on a
  NISAR preview and should be disabled with a tooltip saying why, rather than silently
  doing nothing.
- **The pixel fetch** (`_render_scenes`, and the `_cd_*` chain) uses
  `QgsNetworkAccessManager` to GET a rendered `.tif`. NISAR needs a GDAL windowed read
  instead. This is the only substantial new code.

The seam is cleaner than it sounds. `_cd_tif_loaded` (`sar_tab.py:2040`) does one thing:
write bytes to a temp file and record `self._cd_paths[role] = path`. Everything downstream —
`_cd_compute`, the detectors, styling, export, Fusion — consumes *local GeoTIFF paths on a
common grid*. So the NISAR path has to produce exactly that and nothing more.

**Threading matters.** A `/vsicurl` read is 5–16 s of blocking I/O. Running it on the UI
thread will freeze QGIS in a way that looks identical to a crash — precisely the failure
mode `limits.py` was written to prevent ("QGIS does not warn, it just stops responding, and
the analyst has no way to tell a wedged run from a slow one"). It must go through a
`QgsTask`, reusing the pattern in `task.py`.

---

## 8. The guardrail

The requirement was: NISAR only works within the dates data actually exists. The evidence
in §3 says what a correct guardrail has to be.

### The key realisation

**The guardrail and the search are the same CMR call.** A granule query is already scoped to
the exact AOI and the exact window; it either returns granules or it doesn't. There is no
need for a separate availability service that could disagree with the search.

So the guardrail's real job is not *deciding whether to search* — it is **explaining an
empty result correctly**. Four different situations produce zero granules, and conflating
them is the actual failure mode:

| Situation | What must be said |
|---|---|
| Event predates the mission | "NISAR launched 2025-07-30; first science acquisitions 2025-10-17. This event predates NISAR entirely — no amount of widening the window will help." |
| Event in the 2026-01-21 → 2026-06-16 reprocessing hole | "NISAR was flying and observing, but these data are still being reprocessed. ASF expects the full science record by end of 2026 — re-run this search later." |
| Event in the 2026-07-28 → 2026-08-09 instrument gap | "NISAR acquired nothing anywhere on Earth in this window. Widen the pre/post window to bracket the gap — pre ≤ 07-27, post ≥ 08-10." |
| Covered period, AOI simply not imaged | "NISAR has coverage for this period but no granule images this AOI within ±N days. Widen the window." |

Same empty table, four different honest answers, three of which tell the analyst what to do
next. A single floor date can produce only one of them.

### Two layers, with clearly different jobs

**Layer 1 — hardcoded floor. Job: be permissive and never wrong offline.**

The floor must be the one date that can *only* move later in real terms, never earlier:
first science acquisition, 2025-10-17. Because ASF is actively backfilling, any tighter
constant goes stale in the wrong direction — a floor of 2026-06-17 (today's dense-coverage
front) would refuse a March 2026 event that becomes searchable in November. So the constant
refuses only the genuinely impossible, and the live layer supplies precision.

**Layer 2 — live granule count. Job: be precise, and fail soft.**

One CMR call, reading the `CMR-Hits` response header with `page_size=1`. It is ~0.5 s and
needs no auth. Critically, **query both collections** — beta and provisional are disjoint in
time and a single-collection check silently loses the entire Oct 2025 – Jan 2026 record.

When the network is down, fall back to the hardcoded floor and *say so*, rather than
blocking the feature or silently passing a bad date. Earthdata endpoints are known to lie
on failure — the project already has the scar tissue for this (NSIDC paths 302 even when
absent). A guardrail that treats a redirect as success is worse than no guardrail.

### Proposed module

```python
# nisar_availability.py — what NISAR can possibly have, and what it actually has.
#
# Two layers with deliberately different jobs. The constants are a FLOOR, chosen to be
# permissive: they refuse only what is physically impossible, because ASF is backfilling
# the archive and any tighter constant rots in the direction that wrongly blocks the user.
# The live check is the precise answer and it is the same granule query the search runs.
#
# Do NOT replace the live check with CMR collection temporal extents. Both GCOV
# collections advertise "2025-10-01 -> ongoing"; measured holdings on 2026-09-16 were
# beta 2025-10-17..2026-01-20 and provisional sparse until 2026-06-17. Reading the
# advertised extent tells the analyst that March 2026 is covered. It is not.

import datetime as dt

LAUNCH          = dt.datetime(2025, 7, 30)
FIRST_SCIENCE   = dt.datetime(2025, 10, 17)   # hard floor: nothing exists before this
COLLECTIONS     = ("NISAR_L2_GCOV_BETA_V1", "NISAR_L2_GCOV_PROVISIONAL_V1")

# Known holes, as of 2026-09-16. Advisory only — they shape the MESSAGE, never the
# decision, so that a stale table degrades into a slightly unhelpful note rather than a
# wrongly refused search. The reprocessing hole is expected to close by end of 2026;
# the instrument gap never will.
INSTRUMENT_GAP  = (dt.datetime(2026, 7, 28), dt.datetime(2026, 8, 10))
REPROCESSING    = (dt.datetime(2026, 1, 21), dt.datetime(2026, 6, 17))


def explain_absence(event_dt, pre_days, post_days, checked_live):
    """Why a NISAR search came back empty — the actionable half of the guardrail.

    Returns a sentence that tells the analyst what to DO, or None if the emptiness is
    not explained by a known archive-wide condition (i.e. the AOI simply wasn't imaged).
    """
    lo = event_dt - dt.timedelta(days=pre_days)
    hi = event_dt + dt.timedelta(days=post_days)
    if hi < FIRST_SCIENCE:
        return (f"NISAR launched {LAUNCH:%Y-%m-%d} and began science acquisition "
                f"{FIRST_SCIENCE:%Y-%m-%d}. This event predates the mission — widening "
                f"the window cannot help.")
    if INSTRUMENT_GAP[0] <= event_dt < INSTRUMENT_GAP[1]:
        return (f"NISAR acquired nothing anywhere on Earth between "
                f"{INSTRUMENT_GAP[0]:%Y-%m-%d} and {INSTRUMENT_GAP[1]:%Y-%m-%d} "
                f"(instrument gap). Widen the window to bracket it: pre on or before "
                f"{INSTRUMENT_GAP[0] - dt.timedelta(days=1):%Y-%m-%d}, post on or after "
                f"{INSTRUMENT_GAP[1]:%Y-%m-%d}.")
    if REPROCESSING[0] <= event_dt < REPROCESSING[1]:
        return ("NISAR was observing, but this period is still being reprocessed and only "
                "a sparse targeted subset is public. ASF expects the full science record "
                "by end of 2026 — re-run this search later.")
    if not checked_live:
        return ("Could not reach NASA CMR, so coverage could not be confirmed. The event "
                "date is after NISAR's first science acquisition, so data may exist.")
    return None
```

`available(lat, lon, event_dt, pre_days, post_days)` then performs the live count over both
collections and returns `(n_granules, note)`, where `note` comes from `explain_absence`
when `n_granules == 0`. The existing `notes` list in `_search_candidates` carries it
straight to the tab's log — no new UI channel needed.

### What the UI should do

Don't disable the tab or grey out the date field. The SAR tab's date and AOI are shared
with every other source; gating them on NISAR's archive would break Sentinel-1 for
pre-2025 events, which is most of the benchmark.

Instead, mirror the precedent already in `run_single.py:_search_candidates` — where an
event before 2016 silently drops Sentinel-2 in favour of Landsat. Applied here: **when the
event is outside NISAR's range, don't offer NISAR as the source**, and when the analyst
selects it anyway, run the search and return the explained empty result. Honest empty beats
wrongly blocked, especially while the archive is still growing backwards.

---

## 9. Implementation plan

Ordered so that the riskiest thing is proven first and each step is independently useful.

**Scope: before/after amplitude only.** GCOV `HHHH` (and `HVHV` where wanted) pre and post,
differenced with the detector that already exists. No interferometry — see §11.

### ~~Step 0 — prototype before committing~~ — DONE 2026-09-17

Ran the track-150 pre/post pair (2026-07-18 → 2026-08-11) through the existing
`|log-ratio|` over a 16 km AOI at 10 m:

```
valid pairs 99.5%       |log-ratio| dB: median 2.48   p95 8.57   p99 12.18
strongest change  13.2 dB  at 0.1 km from the seismic point   (300 m smoothing)
within 0.5 km  9.21 dB    |    whole 16 km box  3.18 dB
```

The detector transfers to L-band HH unmodified. This was the step that could have killed the
feature; it didn't. Proceed to Step 1.

### Step 1 — `nisar_availability.py` (new, ~120 lines)

As sketched in §8. Pure functions, no QGIS imports, no network in the constants path — so
it is directly unit-testable in the venv. Tests that matter:

- an event before `FIRST_SCIENCE` is explained as pre-mission, offline
- an event inside `INSTRUMENT_GAP` gets the bracket-the-gap advice with correct dates
- an event inside `REPROCESSING` gets the come-back-later advice
- a CMR failure produces the fall-back note and never a false "covered"
- the live check queries **both** collections (regression guard: a single-collection
  check loses Oct 2025 – Jan 2026 entirely)

`tests/run_all.sh` auto-discovers `tests/test_*.py`, so `tests/test_nisar_availability.py`
needs no registration.

### Step 2 — `nisar_imagery.py` (new, ~250 lines)

Implements the same `search_event(lat, lon, radius_km, event_time, pre_days, post_days,
seasonal, auto_window, **_ignored)` contract as `sar_imagery.py`, returning
`dict(source="NISAR", pre=[...], post=[...], notes=[...])`.

Reuses `opera_s1.py`'s CMR helpers (`bbox_from_point`, `_search`) — this is the same API
and the same auth story, so lift them rather than re-deriving. Add the granule-name regex
from §7 to fill `orbit_state`, `relative_orbit` and `polarizations`.

Windowing must go through `imagery.windows()` so the tab's day sliders keep meaning the
same thing across sources — same reasoning as `sar_imagery.search_event`.

Watch for: several dates return two granules at the same timestamp in different radar modes
(e.g. `..._058_2005_DHDH_M` and `..._058_4005_DHDH_A` on 2026-06-22 06:01). The tab's
`_one_per_day` / `_one_frame_per_pass` dedup handles this if `relative_orbit` and
`orbit_state` are populated correctly — which is another reason to get the name parsing
right rather than approximating it.

### Step 3 — wire into the dispatcher (~10 lines)

In `run_single.py`: add `"nisar": ["nisar"]` to the `sensors` dict in
`_search_candidates`, an `elif s == "nisar":` branch in `_search_one_source`, and `nisar`
to `--prefer`'s choices. Follow the existing `s1`/`dem` precedent — both are already
search/preview-only, which NISAR is too at this stage (`run_single.py:513`).

### Step 4 — `nisar_read.py` (new, ~200 lines) — the georeferenced windowed read

Lives in the plugin package (needs `osgeo`). One entry point:

```python
def read_window(granule_url, group, pol, bbox_wgs84, out_tif, out_epsg, out_res):
    """One NISAR GCOV polarization over an AOI -> a local GeoTIFF on the caller's grid."""
```

Does: rebuild the geotransform per §5; transform the AOI bbox into the granule's *own*
CRS (read from `projection@epsg_code`, do not assume UTM); compute the pixel window;
windowed `ReadAsArray`; warp to the caller's output grid; write.

Two rules from project memory apply directly here and are easy to get wrong:
set nodata **before** `WriteArray` (reversing that order silently destroys all-zero blocks
on GDAL 3.12), and hold the dataset reference when grabbing a band.

Needs `GDAL_HTTP_NETRC=YES` plus a cookie file, and
`CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.h5`. Fail loudly and specifically when EDL bounces —
reuse `opera_s1.download`'s error text, which already says the useful thing.

### Step 5 — SAR tab integration (~300 lines changed)

1. A source combo — `Sentinel-1 (Planetary Computer)` / `NISAR L-band (ASF)`. Keep the
   full names distinct; the project already has a convention about not blurring
   "Microsoft Planetary Computer" against other providers.
2. Route `_collect` to `--prefer nisar` when NISAR is selected.
3. Hide the PC key box for NISAR; show an Earthdata credential status line instead.
4. Disable the render/stretch controls for NISAR with a tooltip explaining that previews
   are static browse images.
5. Branch the pixel fetch: a `QgsTask` calling `nisar_read.read_window` per role, writing
   to the same temp-path contract `_cd_paths[role]` already expects. `_cd_compute` and
   everything after it stays untouched.
6. Label NISAR change maps as L-band HH in the layer name, the log and the export metadata
   tag — so a NISAR result can never be mistaken for a validated C-band VV one later.

### Step 6 — optional: the pre-event control pair (~2 days)

Still pure amplitude, no interferometry — this fell out of the GUNW test but does not depend
on it.

Alongside the event-spanning pre/post pair, fetch **one extra pre-event scene on the same
track** and difference *that* pair too. Two amplitude change maps come back:

```
event    |log-ratio|( post , pre  )   what changed across the event
control  |log-ratio|( pre  , pre-1 )  what changes on this ground anyway
```

Subtracting, or gating the event map on where the control map is quiet, separates "this
surface changed because of the event" from "this surface always changes" — glaciers, snow,
braided rivers. That is exactly the false-positive class the Fusion work keeps fighting, and
the evidence it is needed is right here: over Iliamna the *largest* coherence change in the
scene was a glacier 6 km from the slide (§11). Amplitude will have the same population of
always-moving surfaces.

Cost is one extra scene per track. **Applies to the Sentinel-1 path identically** — nothing
about it is NISAR-specific, so it may be worth building there first, where there is a
six-event benchmark to measure it against.

### Step 7 — validate on Iliamna

Run the track-150 pair through the finished tab path. Compare against the existing
Sentinel-1 result and the ground-truth polygon. Record what L-band HH does that C-band VV
doesn't — particularly on the on-ice portion of the scar, where the penetration difference
should be largest. **This result is the deliverable**, more than the code is.

Explicitly out of scope for a first pass: Fusion integration. Fusion's thresholds are tuned
on C-band VV and the design notes already record how badly footprint and scene-mismatch
issues bite there. Prove the NISAR change map alone first.

---

## 10. Risks and open questions

| Risk | Assessment |
|---|---|
| ~~L-band HH detector performance is unknown~~ | **Retired for Iliamna (§11):** 13.2 dB peak, 0.1 km from the seismic point, ~3× background. Still unvalidated on a *second* event — but the remaining benchmark events all predate NISAR, so a second test means waiting for a new Alaska slide. |
| **Generalisation from one event** | Now the real science risk. Iliamna is on ice; a vegetated or bedrock scar may behave differently at L-band, and there is no way to check against the existing benchmark. |
| **The archive moves under you** | Certain, and by design — ASF is backfilling to end of 2026. This is exactly why the guardrail is a live count with an advisory hole table, not a date constant. |
| `/vsicurl` HDF5 read latency | Measured 16.5 s for two polarizations. Acceptable on a `QgsTask`; unacceptable on the UI thread. |
| CRS varies between granules | Confirmed real (3413 where UTM was documented). Always reproject to a common AOI grid; never assume two granules align. |
| QGIS 4.0 / GDAL 3.13 upgrade | Would make the native NISAR driver available and let most of `nisar_read.py` collapse into a plain `gdal.Open`. Worth revisiting after upgrading; don't build against it now. |
| Beta vs provisional mixing | Different calibration. Don't pair a beta pre-scene with a provisional post-scene without flagging it — the radiometric offset would read as change. Not yet measured; add a check. |
| Earthdata token vs netrc | netrc works here today. `EARTHDATA_TOKEN` is the better path for portability, and `opera_s1.edl_session()` already prefers it. |

### Not verified

- Whether beta and provisional products over the same AOI are radiometrically consistent
  enough to pair. Flagged above; needs measurement.
- S-band. ISRO Bhoonidhi only, limited sample products, separate auth. Not pursued — the
  L-band path is the useful one and S-band adds a whole second access story for little gain.
- ~~Whether GUNW would beat GCOV amplitude.~~ **Measured — it does not. See §11.** The
  original speculation here (that L-band coherence loss would be the stronger landslide
  signal) was wrong for this terrain, and wrong for a reason that generalises to most of
  the Alaska catalogue.
- Whether the browse PNGs are georeferenced usefully enough for map-preview overlay (the
  `_LATLON.kml` sidecars suggest yes, untested).

---

## 11. GUNW — what it is, and why it is out of scope

> **Closed 2026-09-17.** Scope is before/after amplitude only. This section is the record of
> what GUNW is and why it was tested and rejected — read it if someone proposes
> interferometry again, otherwise skip to §12.

### What it is

**GUNW = Geocoded Unwrapped Interferogram.** Where GCOV gives you *brightness* from a single
pass, GUNW gives you the *interferometric relationship between two passes*. Its layers, all
confirmed present by opening a granule remotely:

```
science/LSAR/GUNW/grids/frequencyA/
  wrappedInterferogram/HH/coherenceMagnitude     20 m  <- how correlated the two passes are
  unwrappedInterferogram/HH/coherenceMagnitude   80 m
  unwrappedInterferogram/HH/unwrappedPhase       80 m  <- line-of-sight displacement
  unwrappedInterferogram/HH/connectedComponents  80 m  <- unwrapping reliability regions
  unwrappedInterferogram/HH/ionospherePhaseScreen      <- NOT applied by default
  pixelOffsets/...                                     <- amplitude cross-correlation offsets
```

Co-pol only (HH or VV), frequency A only, 2.5–2.8 GB per granule — less than half a GCOV.
Pairs are 12-day nearest-neighbour.

The appeal for landslides is **coherence loss**: a slide destroys the surface, so the two
passes stop correlating and coherence collapses. That is a physically direct signal, and
L-band holds coherence far better than C-band, so NISAR should in principle be the best
instrument ever flown for it.

### GUNW bridges the instrument gap — a real advantage

Unexpected and genuinely useful: the interferometric processor **paired across the
2026-07-28 → 2026-08-09 outage automatically**, forming 24-day pairs instead of the nominal
12-day. Five event-spanning interferograms exist over Iliamna:

| Track | Reference | Secondary | Span | Spans 8 Aug? |
|---|---|---|---|---|
| 150 A | 2026-07-18 | 2026-08-11 | 24 d | yes |
| 006 A | 2026-07-20 | 2026-08-13 | 24 d | yes |
| 015 D | 2026-07-21 | 2026-08-14 | 24 d | yes |
| 078 A | 2026-07-25 | 2026-08-18 | 24 d | yes |
| 116 D | 2026-07-16 | 2026-08-21 | 36 d | yes |

So the gap that blinds GCOV on the event date does *not* blind GUNW — the interferogram
straddles it by construction. If coherence worked, this would be the better product.

### It does not work. Measured.

Track 150A, the same AOI, comparing a **pre-event control pair** (07-06 → 07-18, 12 d) with
the **event-spanning pair** (07-18 → 08-11, 24 d). Same geometry, same track, so terrain and
glacier decorrelation appear in both and cancel:

```
                              valid   coherence p10/median/p90   frac < 0.3
control (12 d, pre-event)     100%      0.13 / 0.25 / 0.60          64.3%
event   (24 d, spans slide)   100%      0.13 / 0.24 / 0.60          65.3%

coherence change (event - control), 640,000 paired pixels at 20 m:
    median  -0.005      p5  -0.205      p95  +0.204
```

The change distribution is **symmetric noise centred on zero**. And spatially:

```
strongest smoothed coherence LOSS   -0.33   6.4 km from the event point
mean coherence change within 0.5 km of the event   -0.018
mean coherence change over the whole 16 km box     -0.004
```

The biggest coherence drop in the scene is **6 km away from the landslide**. At the slide
itself there is no signal — it changes by 0.018 where the whole box changes by 0.004.

The 80 m multilooked coherence is worse, not better (median 0.13, 71.7% below 0.3, change
median −0.006), so the obvious objection — "multilook it and the coherence will come up" —
is retired too.

### Why it fails, and why that generalises

**You cannot lose coherence you never had.** Pre-event coherence at the slide location was
already 0.21. Iliamna is a glaciated stratovolcano: ice flow, seasonal snow and steep
terrain decorrelate even L-band across 12 days. The interferometric signal was gone before
the landslide happened.

This is not bad luck specific to one event. The project's own benchmark finding is that
these scars are **84–100% on ice**. Glaciated terrain is exactly where repeat-pass coherence
is least available. The Alaska catalogue is largely made of the terrain GUNW is worst at.

### Verdict — closed

**The interferometric products are out of scope. Amplitude only.**

Two independent reasons, and either alone is sufficient:

1. **Measured failure.** Coherence returns nothing on the terrain this plugin targets, for a
   reason that generalises across the catalogue.
2. **Scope decision (2026-09-17).** The requirement is before/after amplitude. That rules
   out GUNW, GOFF/`pixelOffsets`, and the phase products generally — not as a close call
   but as a category.

This section is kept as the *record of why*, so the idea does not get re-proposed on the
strength of "L-band holds coherence better than C-band," which is true and still irrelevant
here. Nothing downstream depends on it.

One thing did come out of this test that belongs in the amplitude build, and it has moved to
**Step 6 of §9**: the pre-event control pair.

---

## 12. ROSE-L — right mission, wrong decade

**ROSE-L (Radar Observing System for Europe in L-band), also designated Sentinel-12**, is
ESA's L-band Copernicus Expansion mission: two satellites, 693 km sun-synchronous, the
largest planar SAR antenna ever built at 11 m × 3.6 m, 7.5-year design life. Dual-pol
(HH+HV or VV+VH) or quad-pol, >260 km swath in its wide mode, 5–10 m resolution in the
geohazard mode, 3–6 day revisit per satellite depending on mode, and explicitly designed for
repeat-pass interferometry with ionospheric phase characterisation.

**Will it work? Nothing to answer yet — it does not fly.** Launch is 2028 at the earliest
(ESA's own pages say 2028; one industry source says mid-2027; CDR was completed 2025-06-04
and the second flight model has no confirmed launcher). The second satellite follows "a few
years after" the first. There is no archive, no API, no product spec to build against.

Two things make it worth *planning around* even so:

- **It will be free and open.** Copernicus operates under the EU Delegated Act: no
  restrictions on use or user, commercial or not, European or not, a free version always
  available, no time limit. That is the same policy that makes Sentinel-1 usable here, and
  it is a materially better deal than NISAR's Earthdata-login-gated access — no credentials,
  no cookie jar, no 6 GB HDF5.
- **The revisit is 3–6 days against NISAR's 12.** For event timing — the thing NISAR could
  not do for Iliamna because of the instrument gap — that is the difference that matters.

**Implementation implication: none now, one design note.** Don't write ROSE-L code. But when
building the NISAR source, keep the *source abstraction* at the level of
`search_event() → candidates` + `read_window() → local GeoTIFF`, rather than letting
NISAR-specific assumptions (HDF5 subdatasets, Earthdata auth, granule-name parsing) leak into
the tab. That is already the shape `sar_imagery.py` and `opera_s1.py` follow, so it costs
nothing extra — and it means ROSE-L in 2028 is a new module, not a refactor. The same
abstraction is what would let a second L-band source be compared against NISAR rather than
replacing it.

---

## 13. What to build

**Scope, fixed 2026-09-17: before/after amplitude. No interferograms.** That means NISAR
GCOV `HHHH`/`HVHV` pre and post, through the detector that already exists.

| # | Option | Effort | Verdict |
|---|---|---|---|
| **1** | **Availability guardrail** — `nisar_availability.py` (§8) | ~1 day | **First.** No dependency on anything else, unit-testable offline with no network, and the piece most likely to quietly mislead an analyst if skipped. |
| **2** | **NISAR GCOV as a source in the SAR tab** (§7, §9 Steps 1–5) | ~1 week | **The feature.** Measured working; Step 0 is already done. |
| **3** | **Pre-event control pair** (§9 Step 6) | ~2 days | **Optional, and not NISAR-specific.** Pure amplitude. Consider building it on the Sentinel-1 path first, where there is a six-event benchmark to measure against. |
| 4 | NISAR into Fusion | ~1 week | Not yet. Fusion's thresholds are C-band VV; prove the standalone L-band map on more than one event first. |
| — | ~~GUNW coherence, GOFF/pixel offsets, unwrapped phase~~ | — | **Out of scope** (§11). Measured failing *and* ruled out by scope. |
| — | ROSE-L | — | Nothing to build until 2028+. Keep the `search_event()` / `read_window()` seam clean and it is a new module later, not a refactor (§12). |

The one-line version: **guardrail, then GCOV amplitude into the SAR tab. Nothing
interferometric.**

---

## Sources

- [NISAR L-Band Data Now Publicly Available — ASF](https://asf.alaska.edu/notices/nisar-l-band-data-now-publicly-available/)
- [Available NISAR Data — NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/availability-overview/)
- [Geocoded Polarimetric Covariance (GCOV) — NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/gcov/)
- [Using NISAR Data in QGIS — NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/using-qgis/)
- [NISAR L-Band Data Released — NASA Earthdata](https://www.earthdata.nasa.gov/news/nisar-l-band-data-released-expanding-record-surface-changes)
- [NISAR — NASA Science](https://science.nasa.gov/mission/nisar/)
- [Tandem-L — eoPortal](https://www.eoportal.org/satellite-missions/tandem-l)
- [TanDEM-L Mission Summary — CEOS Database](https://database.eohandbook.com/database/missionsummary.aspx?missionID=835)
- [ROSE-L — eoPortal](https://www.eoportal.org/satellite-missions/rose-l)
- [Contract signed for new Copernicus ROSE-L mission — ESA](https://www.esa.int/Applications/Observing_the_Earth/Copernicus/Contract_signed_for_new_Copernicus_ROSE-L_mission)
- [Geocoded Unwrapped Interferogram (GUNW) — NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/gunw/)
- [ROSE-L — SentiWiki, Copernicus](https://sentiwiki.copernicus.eu/web/rose-l)
- [ROSE-L SAR mission — Sentinel Online](https://sentinels.copernicus.eu/copernicus/rose-l)
- [ROSE-L A, B (Sentinel 12A, 12B) — Gunter's Space Page](https://space.skyrocket.de/doc_sdat/rose-l.htm)
- [ESA Member States approve full and open Sentinel data policy](https://www.esa.int/Applications/Observing_the_Earth/Copernicus/ESA_Member_States_approve_full_and_open_Sentinel_data_policy_principles)
- Live measurements against `cmr.earthdata.nasa.gov` and `nisar.asf.earthdatacloud.nasa.gov`, 2026-09-16.
