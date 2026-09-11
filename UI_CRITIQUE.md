# Landslide Ground-Truthing — consolidated UI critique

**Scope:** the QGIS plugin package at `qgis_plugin/landslide_groundtruth/` — six tabs
(`Sentinel-2 / Landsat` built inline in `dock.py`, `PlanetScope` = `planet_tab.py`,
`SAR (Sentinel-1)` = `sar_tab.py`, `Fusion` = `fusion_tab.py`, `3D viewer` = `viewer3d_tab.py`,
`Volume from area` = `volume_tab.py`) plus the shared collapsible `Environment` header in `dock.py`.

**The user this is written against:** an analyst who arrives holding a seismic detection record —

```
Detection = Y   Coherency = 0.61   HF/LF = 11.8   Org time = 08:48:37
2026-02-04 08:48:05 UTC  (= 2026-02-03 23:48:05 AKT — PREVIOUS CALENDAR DAY)
Lat 60.50   Lon -140.60   Loc error 17 km
Vol 1.3 M m³ (range 0.9 – 1.7)
```

— whose job is to find the real scar inside that uncertainty disc, delineate it, and check the
area-derived volume against the seismic volume.

---

## 1. Verdict

**No — someone who knows nothing about satellite imagery or QGIS cannot operate this today.**
They can be stopped in the first thirty seconds by a collapsed box labelled `Environment` asking
for a "venv python" (`dock.py:324`), and if they get past it they will be stopped again by the
absence of any way to click a location on the map — a feature `metadata.txt:5`, `README.md:305`,
`README.md:334` and `qgis_plugin/README.md:61` all say exists and which appears in **zero** files
(`QgsMapToolEmitPoint`, `setMapTool` and `canvasClicked` return no hits across the package).
**For a competent remote-sensing analyst who already knows what a dNDSI is, this is a strong,
unusually honest instrument** — the Fusion tab's step panel, the measured-negative-results in the
Advanced tooltips, and `project_state.py` are better than most shipped QGIS plugins.
**The single biggest structural gap is that the plugin has no model of the thing the analyst is
holding, and no model of the thing they are producing:** the detection record has no home in the
code (no `seismic`, `coherenc`, `loc.?error` or `hf.?lf` anywhere in the package) and neither does
the conclusion (grepping for `verdict|confirmed|inconclusive` returns one unrelated string at
`sar_tab.py:582`). It is six excellent imagery-acquisition tools sharing a dock, and
ground-truthing is the one thing it does not represent.

### Verification provenance — read this before you act on any single item

156 findings were filed by six tab reviewers, one per tab. Each was then adversarially
fact-checked against its cited `file:line`: **127 were confirmed exactly as written, 29 were
downgraded or corrected, and none were fully refuted.** Where a finding was corrected, this
document uses the corrected version, not the original claim — several of the corrections matter
(for example, "the failure produces nine words in the log" is wrong: `task.py:49-61` merges
`stderr` into `stdout` and streams it, so the real traceback usually *is* in the log; the defect
is that nothing tells you to look there).

**The absence of any outright refutation means the check was somewhat generous.** "Confirmed"
means *the cited code says what the reviewer claimed*, not that it necessarily matters much. Treat
the severity labels as the reviewers' opinion and the code citations as fact. A separate,
unverified pass on the volume cross-check contributed 17 further findings; those are marked in
§4 and should be re-checked before you act on them.

Eleven additional findings in this document were verified by me directly against the source and
are certain. They are marked **[verified]**.

---

## 2. What is genuinely good

This is capable software written by someone who understands both the physics and the failure
modes. Specifically:

- **`project_state.py`** — per-project persistence of every input into the `.qgz` itself, with the
  reasoning written out (`QgsSettings` is global, so the last-opened AOI would clobber the
  previous one), widget discovery by reflection so a control added later persists for free, and
  two deliberate exclusions: `SECRET_ATTRS` (credentials stay out of a shareable project file) and
  `GLOBAL_PATH_ATTRS`. Almost nobody gets the credential exclusion right.

- **The Fusion step panel** (`fusion_tab.py:177-183, 227-277`). A live three-row readiness strip
  that says what is satisfied, what is missing, and what pressing the button will produce. Its
  docstring states the goal — *"A newcomer should be able to tell what to do next without reading
  a manual"* — and it is the only place in the plugin that achieves it. **This is the template the
  other five tabs should copy** (see §8).

- **Fusion lists unusable layers greyed-out with the reason instead of hiding them**
  (`fusion_tab.py:868-872`). Hiding them would make "why isn't my layer in the dropdown?" an
  unanswerable question. The reasons are too jargon-heavy (`fusion_tab.py:911`), but the decision
  is right.

- **Negative results shipped in the tooltips.** `fusion_tab.py:643-649` records that terrain
  weighting moved mean background at 50 % recall from 3.0 % to 6.7 % and Iliamna AUC from 0.901 to
  0.711 — i.e. the control is off *because it was measured to hurt*. Shipping your own failed
  experiment in the UI is rare and correct.

- **`_style_dbright`** (`dock.py:2206-2224`). The one-sided, transparent-at-zero, darkening-only
  ramp is exactly right for debris-on-snow and is the model the other change rasters should
  follow. This is deliberate and this critique does not touch it.

- **`sar_pairing.py`** — dependency-free (stdlib `datetime` only), self-testing via
  `python sar_pairing.py`, and it produces the human-readable pair bracket rather than a number.
  The cross-orbit tag (`sar_pairing.py:79`) is a real safety rail.

- **The Planet cache ledger and Recall / Re-tone / Resume.** Recognising that quota is spent at
  order creation and building recall-without-re-order around that fact is the correct architecture
  for a paid API.

- **`dem_diff.py:9-26`** — the geodesy notes. Writing down *why* differencing two SETSM strips
  cancels the geoid is what lets a reader spot that the Volume tab's arbitrary DEM pickers break
  the assumption (see §4).

- **`LARSEN_TOTAL = {}`** (`volume_calc.py:55`). Refusing to guess coefficients rather than
  shipping a plausible-looking number is the right instinct. (The UI handling of that refusal is
  wrong — §3, Theme 6 — but the instinct is right.)

- **`flow_layout.py`** — the docstring correctly diagnoses that `setMinimumWidth(360)` on the
  scroll inner widget overrides what a `QHBoxLayout` says it needs, so button labels clip
  mid-word, and solves it properly with a `heightForWidth` flow layout.

---

## 3. The themes, ordered by cost to the user

Each theme merges every site across all six tabs. Fix the theme, not the site.

---

### Theme 1 — The detection record has nowhere to go, and the verdict has nowhere to come from

**Blocker. Cost: the plugin cannot do the job it is named for.**

The analyst arrives with a lat/lon, a location error, an origin time, and a seismic volume with a
range. The plugin can receive the first two of five, in four separate places, in a text box:

| What the analyst holds | Where it goes | What it drives today |
|---|---|---|
| Lat 60.50 / Lon −140.60 | 4 separate `QLineEdit` pairs (`dock.py:358-365`, `planet_tab.py:199-201`, `sar_tab.py:~243`, `viewer3d_tab.py:199-200`) | search centre only |
| Loc error 17 km | **nowhere** | — |
| 2026-02-04 08:48:05 UTC | 4 separate `QDateTimeEdit`s | pre/post split |
| Vol 1.3 M m³ (0.9–1.7) | **nowhere** — no `seismic` field, no CSV column (`volume_tab.py:188-212`) | — |
| Detection / Coherency / HF-LF / event id | **nowhere** | — |
| **The verdict the analyst reaches** | **nowhere** | — |

Consequences that follow directly:

- `run_single.py:319` accepts `--event-id` and `dock.py:855-868` passes nine flags, none of them
  that one. Outputs are keyed on `event_id = a.event_id or f"event_{a.when:%y%m%d_%H%M}"`
  (`run_single.py:512`) — derived, not the catalogue identifier. **[verified]**
- `volume_tab.py`'s 38-column CSV (`CSV_FIELDS`, `volume_tab.py:188`) carries areas, volumes,
  centerline geometry and layer provenance, and not one field that joins a row back to the
  detection. A season of ground-truthing exports as rows keyed by a free-text `"slide 1"`.
- Nothing catches the failure mode that actually happens: assigning a 2 km² *total* outline to the
  `Source area (best)` slot (nothing prevents it — the role is name-matched, and a layer called
  "Landslide scar" matches source-best) yields **V = 179.7 Mm³, 138× the seismic estimate**, and
  the tab reports it with a confident ±1σ and no complaint whatsoever.

Full design in **§4**. This is the one item that changes what the plugin *is*.

---

### Theme 2 — The `Environment` box is a hard gate that ships collapsed, unlabelled and unvalidated, and eleven code paths dead-end into it

**Blocker.**

`dock.py:319-346`:

```python
env = QgsCollapsibleGroupBox("Environment")
env.setSaveCollapsedState(False)
env.setCollapsed(True)
...
for label, edit, picker in (
    ("venv python",  self.python_edit,  self._pick_python),
    ("project dir",  self.project_edit, self._pick_project),
    ("output dir",   self.out_edit,     self._pick_out),
):
```

Three bare labels, no tooltips, no placeholders, no validation feedback, no indication of whether
what you typed works. `setSaveCollapsedState(False)` means it re-collapses on every QGIS restart,
including for a user who has never configured it. And **every tab dead-ends into it**:

| Site | Message |
|---|---|
| `dock.py:841` | `"Set a valid venv python path."` |
| `dock.py:844` | `"run_single.py not found in project dir: …"` |
| `planet_tab.py:1023, 1668, 2019, 2129, 2229` | `"Set a valid venv python path in Environment (top of the panel)."` |
| `sar_tab.py:899, 902` | same |
| `viewer3d_tab.py:659` | same — and `"Auto-fetch a DEM over the AOI"` is the *pre-selected* Source (`viewer3d_tab.py:234`), so the very first click a new user makes hits it |
| `fusion_tab.py:1257-1261` | `"Set the Output (or Project) folder in the Environment box first…"` — while the step panel says **"Ready"** and enables the button (`fusion_tab.py:273`) |

"venv" is a word this audience does not have. The box it points at is collapsed. The fields are
empty. There is no example of what a correct value looks like.

**Fix, once, in `dock._build_env_box`:**

```python
env = QgsCollapsibleGroupBox("Environment — set these three up once")
env.setSaveCollapsedState(False)
self.env_box = env                       # tabs need to be able to open it
ROWS = (
    ("Python for the imagery tools", self.python_edit, self._pick_python,
     "…/landslide_groundtruth/venv/bin/python3",
     "The python program inside the project's venv folder — 'python3' in "
     "venv/bin (macOS/Linux) or python.exe in venv\\Scripts (Windows). "
     "The plugin runs the imagery tools with it."),
    ("Folder with the imagery tools", self.project_edit, self._pick_project,
     "…/landslide_groundtruth   (the folder containing run_single.py)",
     "The folder you downloaded the pipeline into. It must contain run_single.py."),
    ("Where to save results", self.out_edit, self._pick_out,
     "defaults to <project>/out/interactive",
     "Imagery, change rasters and the SAR change files the Fusion tab reads."),
)
```

then `env.setCollapsed(all(v.strip() for v in (python, project, out)))` so it **opens itself when
unconfigured**, a live status row (`✓ ready` / `⚠ Python not found at that path` /
`⚠ no run_single.py in that folder`) recomputed on `textChanged`, and one shared refusal string:

> `"Before searching, open the Environment box at the top of this panel and choose the Python program that has the imagery tools installed. Ask whoever installed the plugin if you are not sure."`

plus `self.env_box.setCollapsed(False)`, a red 1px border on the offending field, and `setFocus()`.

**Also add a no-setup escape hatch.** `viewer3d_tab.py` is the worst case because its default path
requires the venv, but the alternative (`"Use a DEM layer loaded in the project"`) requires the
user to already have a DEM. A third source item that warps the public Copernicus GLO-30 COG
through `dem_diff.warp` needs neither, and makes the tab work out of the box.

---

### Theme 3 — There is no map-pick tool, three documents promise one, and every radius default ignores the location error

**Blocker. [verified]**

`QgsMapToolEmitPoint`, `setMapTool` and `canvasClicked` appear in **zero files** in the package.
The only geographic input anywhere is two decimal-degree text boxes. Meanwhile:

- `metadata.txt:5` — *"Pick an event location on the map…"*
- `README.md:305` — *"(pick location on the map, set date + pre/post-day sliders…)"*
- `README.md:334` — *"a dropped minus sign on longitude is the classic error … the plugin's **Pick location on map** avoids this by reading the click directly."*
- `qgis_plugin/README.md:61` — *"1. **Pick location on map** → click the event location on the canvas."*

The documentation names this feature as **the defence against the exact error it cannot prevent**.
`viewer3d_tab.py:199-200` doesn't even set a placeholder or a validator on its lat/lon, unlike
`dock.py:358-365`.

Add to the `dock.py` location form, and mirror it in the other three tabs (or better, delete the
other three — Theme 4b):

```python
self.pick_btn = QPushButton("Pick on map")
self.pick_btn.setCheckable(True)
self.pick_btn.setToolTip("Click the spot on the QGIS map and the latitude and "
                         "longitude fill in. West longitude is negative.")
# on toggle: QgsMapToolEmitPoint(self.canvas), canvasClicked -> _on_map_pick,
# transform canvas CRS -> EPSG:4326, write f"{p.y():.6f}" / f"{p.x():.6f}",
# untoggle, canvas.unsetMapTool(...); also unset in teardown().
self.centre_btn = QPushButton("Use map centre")
```

and accept a pasted pair in `_collect`: if `","` is in `lat_edit.text()`, split it and fill both.

**And the radius.** All four search-radius spinboxes default to **5.0 km**
(`dock.py:371`, `planet_tab.py:208`, `sar_tab.py:251`, `viewer3d_tab.py:204`). **[verified]**
Against a 17 km location error that is a 10 × 10 km box inside a 34 × 34 km uncertainty square —
**about 1/12 of the ground the slide could be on.** The scar is more likely outside the search
area than inside it, and nothing in the UI says so. The naive correction is worse: 5 → 17 km is
**11.6× the area**, 11.6× the download, and 11.6× the Planet quota. The staged remedy is in §4.

At minimum, add a live caption under the radius row driven by a new "Location error (km)" field:

```python
self.radius_hint.setText(
    f"Your search box is {2*r:.0f} × {2*r:.0f} km = {(2*r)**2:.0f} km². "
    f"The reported location error is {err:.0f} km, so the slide could be "
    f"anywhere in a {2*err:.0f} × {2*err:.0f} km square "
    f"({100*(2*r)**2/(2*err)**2:.0f}% of it is covered by this search).")
```

---

### Theme 4 — Failure is silent, and sometimes it is *cheerful*

**Blocker. Recurs on all six tabs.**

Two distinct defects that look the same from the user's chair.

**4a — A crashed subprocess produces one grey log line and nothing else.**
`PipelineTask` only sets `.result` on a clean exit (`task.py:70-71`). Wrong venv python, missing
dependency, no network, child crash — all land in a bare `_append_log`:

| Site | Text |
|---|---|
| `dock.py:1027` | `"Search finished with no result."` |
| `dock.py:2195` | (same pattern, on `_on_done`) |
| `sar_tab.py:955` | `"Search finished with no result."` |
| `viewer3d_tab.py:679` | `"Search finished with no result."` — the only failure path in that file that logs without also warning |
| `planet_tab.py:1729-1735` | `"Render detail finished with no result."` — **after the quota is already spent** |

The corrected finding matters here: `task.py:49-61` merges `stderr` into `stdout` and emits every
line, so **the real traceback usually is already in the log**. What is missing is (a) any signal
that something failed, (b) any pointer to the log, and (c) the exit code. The progress bar simply
disappears.

Fix in three places:

```python
# task.py — before `return code == 0`
self.exit_code = code
self.error_tail = "\n".join(self._tail)       # collections.deque(maxlen=40) fed in the stdout loop
self.logLine.emit(f"process exited with code {code}")
```

```python
# every `if not result:` branch
tail = getattr(task_ref, "error_tail", "")
hint = (" The Python you chose does not have the imagery tools installed."
        if ("ModuleNotFoundError" in tail or "Failed to import encodings" in tail) else "")
self._warn("The imagery tools could not be run." + hint +
           " Check the Environment section at the top of this panel; the "
           "Run log below has the details.")
self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
```

**4b — Total failure reported as a friendly blue "Found 0".**

- `run_single.py:663-665` catches *any* exception from `_search_candidates` and returns
  `dict(pre=[], post=[], notes=[f"search failed: {e}"])` **with exit code 0**. `dock.py:1050` then
  pushes `pushInfo("Landslide", f"Found {npre} pre / {npost} post candidate scenes (no orders placed).")`.
  A network outage looks identical to "there is genuinely nothing here".
- `planet_tab.py:1098` does the same, and worse: `_collect` (`planet_tab.py:1000-1046`) **never
  checks `_api_key()`** — unlike `_render_picks` (`:1494`), `_render_detail` (`:1657`) and
  `_resume_detail`, which all do. So the first button a new user presses runs with no credentials,
  fails inside the Planet SDK, and reports `Found 0 pre / 0 post` in a blue bar.
- `sar_tab.py:968` pushes `pushInfo("SAR", "Found 0 pre / 0 post Sentinel-1 scene(s) (no cloud filter — radar sees through cloud).")`
  while `map_preview_btn`, `run_btn` and `cd_btn` all stay disabled with no stated reason.

**Rule to adopt:** `pushInfo` is for success. Zero results is `pushWarning` with the three
remedies named. A caught exception is `pushWarning` that says the word "failed". And in
`volume_tab.py`, `_notify` (`volume_tab.py:1359-1367`) already exists *for exactly this purpose* —
its docstring says *"so an action button never looks like it did nothing when the Log panel is
scrolled out of view"* — and it is called **only** from `_prepare_mosart_job`. Every other failure
in that tab (`_measure` at 1431/1451/1470/1476/1491/1495, `_measure_ddem` at 1654/1662/1668/1687,
`_difference_dems_impl` at 1784/1788/1793/1816/1840, `_draw_centerline` at 2113/2131/2139,
`_write_one` at 2625/2631/2651) goes to the log only. Route all of them through `_notify`.

---

### Theme 5 — The one action that spends money says the tab is free, and has no price, no confirmation, and no refund path

**Blocker. PlanetScope tab.**

`planet_tab.py:186` is the first and most prominent text on the tab:

> *"Preview scenes at full resolution straight on the map — no order placed, no quota used.
> (Ordering into the review package comes later.)"*

This was true when the module docstring said *"v1 scope is BROWSE + MAP PREVIEW only"*
(`planet_tab.py:15-16`). The tab has since gained `Render detail (quota)` (`planet_tab.py:522`),
which places a real Planet order. A novice reads "no quota used" as a property of the tab.

It compounds:

1. **The tick-boxes that say they drive the free preview also decide what the paid order buys.**
   `planet_tab.py:631` — *"Candidate scenes (★ = nearest each side; tick the scenes to preview on
   the map)"*. `_detail_picks_by_side` (`planet_tab.py:1617-1639`) reads the same checkboxes, and
   `_render_detail` orders every ticked scene. Tick six candidates to compare them — as the label
   invites — press the bold button, buy six scenes.
2. **No cost anywhere.** `planet_tab.py:1640` places the order with no confirmation dialog. The
   word "quota" appears in the button label and six tooltips and is never defined. Planet charges
   in km²; the AOI comes from `Search radius` (default 5 km → ~100 km²); nothing connects the two.
3. **Cancel burns the quota and destroys the handle.** `planet_tab.py:573` — a bare `Cancel` with
   no tooltip and no confirmation. Planet charges at order *creation* (`planet_imagery.py:20-21`).
   `PipelineTask.cancel()` kills the child, so `render.json` is never read, and the plugin loses
   track of the order — a retry buys the same scenes again.
4. **Then it tells you to resume with the Resume button disabled.** `planet_tab.py:1864` fires
   *"A PlanetScope order (pre & post) is still processing … resume it (no re-order) to finish"*
   unconditionally, but `_refresh_resume_btn` (`planet_tab.py:2152-2159`) only enables the button
   when the `Timed-out orders` combo is not `"off"` — and its stored default **is** `"off"`
   (`planet_tab.py:494-495`).

**Fixes, in order of value:**

```python
# planet_tab.py:186-188 — replace the intro outright
"Two ways to look at PlanetScope.  FREE: search, browse thumbnails, and stream a "
"scene onto the map at full resolution.  USES QUOTA: 'Render detail (quota)' places "
"a Planet order for the raw imagery so bright snow keeps its texture. Only the "
"button marked (quota) ever spends anything — Recall, Re-tone and Resume are free."
```

```python
# planet_tab.py:631 — the checkbox column label
"Candidate scenes  (★ = nearest each side; tick scenes to preview on the map — the "
"same ticks decide what 'Render detail' orders, one scene = one charge)"
```

```python
# a live price line under the button row, from radius_spin.valueChanged + table itemChanged
self.quota_lbl.setText(
    f"Render detail will order {n} scene(s) covering about {(2*r)**2:.0f} km² "
    f"of your Planet quota.")
```

```python
# first-run gate, backed by QgsSettings("landslide/planet_quota_ack")
"Render detail places a real Planet order. Planet charges quota in km² of imagery at "
"the moment the order is placed — cancelling the download does not refund it.\n\n"
f"This order: {n} scene(s), about {km2:.0f} km².        [ ] Don't ask again"
```

```python
# _cancel, when self._task_kind == "render"
"The Planet order has already been placed and your quota is already spent. "
"Cancelling stops the download but does NOT refund it — and the plugin loses "
"track of the order, so a retry would buy the same scenes again."
```

and change `planet_tab.py:494`'s stored default from `"off"` to `"manual"` so the button the
message bar tells you to press is actually live.

**Two credential leaks in the same tab, worth fixing while you are here:**

- `planet_tab.py:2274` writes `?api_key={key}` into the XYZ layer's datasource URI. QGIS persists
  that URI into the `.qgs`/`.qgz` and shows it verbatim in Layer Properties ▸ Information — so
  every `Preview on map` click plants a quota-spending credential into a file the user will email
  to a colleague. Register it once via `QgsApplication.authManager()` and use `authcfg=<id>`.
- `planet_tab.py:894` writes the account **password** in plaintext to `QgsSettings` and prefills
  it back into the field next session. A masked field plus a `Log in` button carries an implied
  contract that the password is exchanged for a token and not kept. Delete the write; if prefill
  must stay, gate it on an explicit `"Remember my password on this computer (stored unencrypted)"`
  checkbox.

---

### Theme 6 — Defaults are tuned for a summer event in the lower 48

**Blocker in aggregate.** For the February detection at 60.5 °N in the header of this document,
the out-of-the-box configuration is wrong in six independent ways at once.

| Control | Ships as | Why it is wrong for this event |
|---|---|---|
| Search radius (×4) | 5.0 km | covers ~1/12 of a 17 km error disc (Theme 3) |
| Days before / after | 60 / 90 (`dock.py:404-405`) | 90 days after 4 Feb is **early May** — still snow at 60.5 °N. 60 days before is **6 Dec**: solar noon altitude ≈ 7°, ~5.5 h of light. The pre-window is physically unusable. |
| `--seasonal` | **not passed at all** | `run_single.py:377` defines it as *"winter event: use prior-year pre window"* — the single most relevant control in the pipeline for this event — and `dock.py:855-868` passes nine flags and not this one **[verified]** |
| Event time | `QDateTime.currentDateTimeUtc()` (`dock.py:375`) | the "after" window is entirely in the future; guaranteed zero post-scenes, reported as a blue "Found 0" |
| `Layers to export` | only `highlight_natural` (`dock.py:47, 460`) | the three change layers that reveal a slide — dNDVI, dNDSI, dBright — are all **unticked**. A first Run returns two photographs and a point. |
| Planet window / coverage | 30 / 30 days (`planet_tab.py:231-234`); `coverage = "point"` (`planet_tab.py:279`) | different from tab 1 for the same event, and `point` is the option whose own tooltip says it *"can miss the nearest scenes"*, against `run_single.py:351-354` which documents the default as `aoi` |

Two more that are less severe but same family: `viewer3d_tab.py:129` searches terrain over
**±3650 days** and ranks without regard to date, so a 2014 glacier surface can be draped under a
2026 event with no warning; `fusion_tab.py:633, 704` title the two Advanced panels that were
*measured to hurt* with neutral names (`"Terrain weighting"`, `"Glacier weighting"`), so opening
Advanced invites the two worst possible changes.

**Fixes:**

```python
# dock.py:47
DEFAULT_SCENES = {"highlight_natural", "dndvi", "dndsi", "dbright"}
# dock.py:460
cb.setChecked(key in DEFAULT_SCENES)
```

```python
# dock.py:375
self.dt_edit = QDateTimeEdit(QDateTime.currentDateTimeUtc().addDays(-30))
self.dt_edit.setToolTip(
    "Roughly when the slide happened. The date is what matters — the time only "
    "breaks ties between two passes on the same day. UTC is the world clock: "
    "Alaska is UTC−8/−9, so a late-evening Alaska event is already the next day "
    "in UTC.")
```

```python
# new, next to the sliders (dock.py, after :405)
self.seasonal_check = QCheckBox(
    "Winter event — look for a 'before' picture in the same season last year")
self.seasonal_check.setToolTip(
    "For an event between roughly October and March, when the weeks before it "
    "were dark or snow-covered. Instead of the days just before the event, the "
    "search looks at the same window one year earlier, when the ground was "
    "visible.")
# in _collect:
if self.seasonal_check.isChecked():
    args += ["--seasonal"]
```

```python
# fusion_tab.py:633 / :704 — name the panels by their measured effect
QgsCollapsibleGroupBox("Terrain weighting — off; measured to make results worse")
QgsCollapsibleGroupBox("Glacier weighting — off; hides slides that ran onto ice")
```

Add the seasonal auto-nudge: in `_update_day_labels`, if the event month is 10–3 and the box is
unticked, set a caption reading *"That date is in the dark half of the year — tick 'Winter event'
above."*; and a future-window guard: *"⚠ The 'after' window runs past today — those pictures do
not exist yet."*

---

### Theme 7 — Long jobs run on the GUI thread, with no progress, no estimate and no size cap

**Blocker on two tabs.**

| Site | What blocks | Symptom |
|---|---|---|
| `viewer3d_tab.py:1755` | `gdal.Warp` inside `_make_flip_cache_layer`, called synchronously from `_flip_to`, with the flip-cache checkbox **ticked by default** (`:399`) | first before/after flip freezes QGIS for tens of seconds, `_flip_to` never calls `_busy()` — not even a progress bar |
| `viewer3d_tab.py` `_export_web_viewer` | full render + DEM read | same, worse artefact |
| `volume_tab.py:1802` | `_difference_dems_impl` warps both DEMs to (outline bbox + 25 %) at the `Grid (m)` default of 2 m | a 4 km outline = 9 M px; a 10 km outline ≈ 56 M px across three float32 arrays ≈ 670 MB, in the GUI thread, with one `processEvents()` before the warp |
| `fusion_tab.py:1265` | whole compute under `setOverrideCursor(Qt.WaitCursor)` — the docstring admits *"QGIS will be unresponsive for a few seconds on a large AOI"* | plus `fusion_tab.py:1467`, where ticking glacier weighting starts a **40–80 MB NSIDC download inside the frozen run** with no cancel |
| `sar_tab.py` `_cd_compute` (2033+) | numpy detectors on the GUI thread from the last reply's slot | (downloads are correctly async — `QgsNetworkAccessManager` at 1528-1530 and 2003-2007 — only the maths blocks) |
| `sar_tab.py:1516` | `self.progress` is shown/hidden only by `_busy()`, which only `_search()` calls | Preview, Run and Compute change map have **no** progress indicator at all |
| `dock.py:521` | indeterminate `QProgressBar`, and the first log line is a raw `$ /Users/…/venv/bin/python /Users/…/run_single.py --lat …` (`task.py:36`) | to a novice, the first thing a Run prints looks like an error |

And there is no upper bound on the work: `viewer3d_tab.py:201` allows a 50 km radius, the
resolution combo offers `2 m`, and `dem_diff.warp` uses `format="MEM"` (`dem_diff.py:88`) — that
combination is a 50 000 × 50 000 float32 array, **about 10 GB**, with no estimate, no confirmation
and no cap.

**Fixes:**

- **Estimate before the click, everywhere a grid is sized.** Connect radius/resolution to a
  palette(mid) caption: `f"About {n:,} × {n:,} cells (~{n*n*4/1e9:.1f} GB in memory)."` and, past
  a threshold, `" Too big — reduce the Search radius or pick a coarser resolution."`
- **Refuse past a hard cap** rather than crashing: in `_warp_selected_strip` (before
  `viewer3d_tab.py:1021`) refuse over `1.5e8` px; in `_difference_dems_impl` (between
  `volume_tab.py:1804` and `1807`) refuse over `12e6` cells and *name the grid size that would
  work*: `f"Raise Grid (m) to {suggest:g} for this outline, or difference a smaller area."`
- **Never fetch from the network inside a frozen run.** In `fusion_tab._run`, before
  `setOverrideCursor`, block on missing data with a `QMessageBox`: *"Glacier weighting needs the
  glacier outlines (about 60 MB) and a free NASA Earthdata account. Download them now?"* and run
  the fetch under a `QProgressDialog` with a Cancel button. Put the fetch in both checkbox labels
  too — `fusion_tab.py:638` already does this for terrain; `fusion_tab.py:709-716` does not.
- **Show the bar for every long action**, not just Search: `self.progress.setVisible(True)` at the
  top of `_render_scenes` and `_run_change_detection`, determinate over the download count
  (`setFormat("Downloading radar scene %v of %m…")`), switching to indeterminate with
  `setFormat("Computing the change map — this can take a few seconds…")` for the numpy block.
- **Say what is happening in words**, driven off the existing log stream: a `status_lbl` above the
  progress bar set to `"Searching for scenes…"` / `"Compositing scenes…"` / `"Downloading
  imagery…"`, plus elapsed seconds from a `QTimer` started in `_busy(True)`. And gate the raw
  command line at `task.py:36-37` behind a `verbose` flag.

---

### Theme 8 — The results have no stated meaning, and a null result is indistinguishable from a crash

**Blocker on the Fusion tab; major on tabs 1 and 3.**

- **A null result draws literally nothing.** `SCORE_RAMP` (`fusion_tab.py:76-78`) is fully
  transparent below 0.20, and the area sieve (`fusion_tab.py:1508-1522`) zeroes every blob under
  `SIEVE_THRESHOLD = 0.20`. An AOI with no landslide — **the correct and most common answer** —
  produces a layer that draws nothing at all. The user cannot tell "ran fine, found nothing" from
  "silently failed" from "the layer is behind the basemap".
- **The legend is six bare numbers.** `fusion_tab.py:1860` labels the stops
  `f"{value:.2f}"`, so the Layers panel shows `0.00 / 0.20 / … / 1.00` beside a yellow→red ramp.
  And the number is not a confidence: `robust_rank` (`fusion_core.py:226-280`) converts each
  channel to a **percentile among pixels that cleared the floor**, so the strongest admitted pixel
  always scores exactly 1.00 — in an AOI with no landslide in it.
- **Three near-identical layers, no guidance on which to believe.** A default run adds the fused
  score, `"… — single-sensor coverage"`, and `"… — optical only (no AND)"` (`fusion_tab.py:1843`).
  Two use the identical ramp. "AND" is a logic term the user has never met.
- **After a successful run, step 3 reverts to "Ready"** (`fusion_tab.py:1274-1276` →
  `_update_steps`), so the state after a run looks identical to the state before it. Nothing moves
  the canvas either — a grep for `zoom`/`setExtent` in `fusion_tab.py` returns nothing, even
  though `_check_in_view` (`:1655-1691`) already computes the footprint.
- **dNDVI and dNDSI load as unstyled grey rasters.** `dock.py:2260` applies `_style_dbright` to
  one product out of three. The two layers the tooltips call the primary landslide signal arrive
  in QGIS's default grey min/max stretch, where a −0.4 scar on a snowfield is a slightly darker
  grey smudge. **[verified: `_style_dbright` at `dock.py:2206` is the only ramp the plugin applies
  to its own optical output.]** The one-sided darkening-only design of `_style_dbright` is correct
  — generalise it, do not replace it:

```python
# dock.py — replace the single `if "dbright" in name.lower():` test
RAMPS = (("dndvi", -0.30), ("dndsi", -0.30), ("dbright", -0.30))
for token, lo in RAMPS:                       # "dndvi" before "ndvi"
    if token in name.lower():
        self._style_change(lyr, lo, token)    # _style_dbright, generalised
        lyr.setOpacity(0.75)
        break
```

- **Nothing says what a slide looks like in radar.** The real heuristics exist and are buried in
  log output nobody reads (`sar_tab.py:2173-2175, 2181`). Put them on screen under the button row:

```python
self.cd_read_lbl = QLabel(
    "Reading the change map: a fresh slide usually shows as a NEW bright patch on a "
    "slope that was smooth before (snow, ice, bare rock) — rough debris scatters more "
    "radar back, so red = brighter after. Two things that are NOT slides: a whole "
    "slope reddening after new snow or a thaw, and bright stripes on every slope "
    "facing the same direction (that is the radar's viewing angle — try 'Dim steep "
    "slopes that face the radar').")
```

**Fusion fixes:**

```python
# fusion_tab.py:1860 — label the stops with words, not numbers
(0.40, '#fed976', 140, '0.40  possible'),
(0.60, '#fd8d3c', 200, '0.60  likely'),
(0.80, '#e31a1c', 230, '0.80  strong'),
(1.00, '#800026', 255, '1.00  strongest in this scene'),
```

```python
# a persistent result panel between the step panel and self.pages, filled at the end of _fuse
"Colour = ranking within THIS area, not a probability. 1.00 only means 'strongest "
f"pixel here'. {n_admitted:,} pixels passed the optical floor and {n_sar:,} the SAR "
"floor. Look at the largest connected red patch first; isolated red specks are noise."
# and, when meta['max_score'] is not finite or < SIEVE_THRESHOLD:
"Nothing found — no pixels in this area changed enough in both sensors to be a "
"candidate. That is a normal result."
```

```python
# fusion_tab.py:1822/1834/1843 — name the layers by meaning
f"Landslide score — both sensors agree ({name})"
f"Where only radar could see — weaker evidence ({name})"
f"Landslide score — optical evidence only ({name})"
```

plus a `"Zoom to result"` button beside Fuse (enabled only while `_last_layers` is non-empty) and
a `"✓ Done — {n} layers added"` step-3 state driven by a new `self._last_result`.

---

### Theme 9 — Undefined vocabulary in the labels the user must navigate by

**Major, ~20 findings, all six tabs.** These are not tooltips; they are the words on screen.

| Term | Sites (user-visible) | Replacement |
|---|---|---|
| **AOI** | ~20 strings in `dock.py` (incl. the layer name `"Search AOI"` at `:1713`), 15 in `sar_tab.py` (incl. the table header `"AOI %"` at `:372`), several in `planet_tab.py`, `fusion_tab.py:555` | **"search area"** everywhere; keep `_aoi_bbox` / `_aoi_coverage` in code |
| **venv** | `dock.py:337, 841`; `planet_tab.py:1023, 1668, 2019, 2129, 2229`; `sar_tab.py:899`; `viewer3d_tab.py:659` | "Python for the imagery tools" (Theme 2) |
| **DEM / DSM / DTM** | `viewer3d_tab.py:227, 234-235, 268` | "Ground shape (elevation)"; "Download the ground shape for me (recommended)" / "Use an elevation file I already loaded in QGIS" |
| **SR / TOA / browse / quicklook / Data API** | `planet_tab.py:345, 401, 650, 663` | "Image look"; "Skip Planet's atmospheric correction (better over snow and ice)"; "Thumbnails (click one to select its scene)"; "Scene preview (low-resolution thumbnail from Planet)" |
| **layover** | `sar_tab.py:710` — *"Fade layover slopes (dim high signal on the layover-facing side)"* | **"Dim steep slopes that face the radar (they look falsely bright)"** |
| **Radiometrically normalize** | `sar_tab.py:753` | **"Cancel whole-scene brightness shifts (snow, rain, thaw)"** |
| **STAC / RTC** | `sar_tab.py:301` tooltip on the first button pressed | "Look up which Sentinel-1 radar pictures exist before and after your event. This is free and downloads nothing." |
| **SCL class 2** | `fusion_tab.py:783` — *"Also mask SCL 'dark area' (class 2)"* | "Also ignore dark pixels — off, because in mountains most dark pixels are hillside shadow, not cloud (Sentinel-2 scene-classification class 2.)" |
| **AND / blob** | `fusion_tab.py:1843, 617` | "both sensors agree"; "Ignore patches smaller than" |
| **asce** | `sar_tab.py:1130` `(c.get("orbit_state") or "")[:4]` → the literal non-word `"asce"`, propagated into the Orbit column, gallery captions (`:1268`), **every map layer name** (`:1394-1398`) and saved GeoTIFF filenames (`:1548`) | `_orbit_tag()` → `"ASC"` / `"DESC"` |
| **pre / post** | `dock.py:1100` and `planet_tab.py:1241` fill the `Side` column with literal `"pre"`/`"post"`, while the gallery two inches below says `Before (6)` / `After (4)` and the layers say `"PlanetScope before 2024-07-20"` | `"Before"` / `"After"` in the table too |

**Also fix the three orphan superlatives.** `fusion_tab.py:53-59` offers five SAR measures of which
one is *"(recommended)"*, another *"(equal best)"* and a third *"(best on snow/ice)"* — over
snow-covered Alaska, which is the whole point of the plugin. Three contradictory endorsements in
one dropdown is not a choice a novice can make. Keep one endorsement, move both measure combos to
Advanced, and put a read-only `"Detected: snow-index change (dNDSI), from the layer name — change
this in Advanced if it is wrong."` on the Run page.

---

### Theme 10 — Two invisible, different selection mechanisms on the same table

**Blocker on the SAR tab; major on tabs 1 and 2.**

The scene table on three tabs responds three different ways to the same row:

| Gesture | Effect | Stated where |
|---|---|---|
| click the ~13 px checkbox inside the cell | selects what gets downloaded / analysed / **ordered** | one clause of a section header |
| click anywhere else in the row | highlights it; drives the preview pane and footprint isolation | nowhere |
| double-click | renders that one scene | nowhere (`sar_tab.py:379`) |

`dock.py:548-558` sets `ExtendedSelection` + `SelectRows` and puts a user-checkable box inside the
same cell as the `"★ pre"` text (`dock.py:1126-1127`). A novice clicks a row, watches it turn
blue, presses the bold button, and gets refused (`dock.py:954`):

> *"Tick the scene(s) you want to download in the table (either side alone is fine), or run Search / Preview again to let the Run pick automatically."*

On the Planet tab the same ambiguity **spends money** (Theme 5).

**Fix: give the checkbox its own column and say what each gesture does.**

```python
self.table = QTableWidget(0, 8)
self.table.setHorizontalHeaderLabels(
    ["Use", "When", "Date (UTC)", "Days from event",
     "Cloud over search area", "Area covered", "Satellite", "Scene ID"])
# move setFlags / setCheckState / the UserRole payload onto column 0
self.table.setColumnWidth(0, 34)
```

```python
legend = QLabel("☐ tick the 'Use' box = download this scene · a highlighted row is "
                "only previewed · double-click shows just that scene on the map")
legend.setStyleSheet("QLabel { color: palette(mid); }")
```

and make the refusal constructive: when nothing is ticked but rows *are* highlighted, offer
`QMessageBox.question(self, "Landslide", f"Use the {n} highlighted scene(s)?")` and tick them.

While you are in that table: **no column header carries a tooltip anywhere in the plugin**, so
`"Gap (d)"` has zero explanation in the UI, and `"Coverage"` shows a bare `83` with no `%` sign
(`dock.py:1097`, `planet_tab.py:1239`). Add `%`, and add the eight header tooltips.

---

### Theme 11 — Data-loss traps

**Major, `Volume from area` tab — the tab where the user's own irreplaceable work lives.**

- **`New scar layer` creates a memory-provider layer** (`volume_tab.py:834`) and `_draw_outline`
  calls `layer.startEditing()` (`:872`) and **never commits**. The plugin says so in a log line —
  *"It lives in memory only — use Export > Save Features As… to keep it."* — and then leaves the
  user in edit mode. Every subsequent QGIS prompt ("Stop editing? Save changes?") is a chance to
  lose twenty minutes of digitizing. Add a `Save outlines…` button next to `New scar layer` /
  `Draw outline` that commits the buffer and runs `QgsVectorFileWriter` into
  `<project dir>/outlines.gpkg`, and make the risk persistent rather than log-only:

  ```python
  sel_lbl.setText(sel_lbl.text() +
      f"  —  “{name}” is temporary and not yet saved. Press Save outlines before closing QGIS.")
  ```

- **`Write to layer` with nothing selected writes and commits to *every* row.** `_selected_rows`
  (`volume_tab.py:2546`) falls back to `list(range(len(self._rows)))`, and `_write_to_layer` calls
  `commitChanges` (`:2680`) — a real, committed edit to the user's data. Two buttons down,
  `Remove row` uses the opposite rule and refuses with *"Select the row(s) to remove first."*
  The novice learns "nothing selected = nothing happens" from the safe button and applies it to
  the destructive one. Make the destructive button the stricter one:

  ```python
  idx = sorted({i.row() for i in self.table.selectedIndexes()})
  if not idx:
      self._notify("Select the row(s) to write first — this commits attribute "
                   "edits to your layers, so it never guesses.", Qgis.Warning)
      return
  ```

- **The web export always writes the same filename** (`viewer3d_tab.py:1983`,
  `"instant_flip_3d.html"`) with no uniquifier and no overwrite prompt, silently destroying a
  multi-megabyte artefact the user may already have shared a path to.

- **`New scar layer` also permanently pins the Total role** (`volume_tab.py:857-861`) because that
  one combo fill is not wrapped in `blockSignals` — unlike every other programmatic fill in the
  file (`:494-506`, `:531-535`) — so `_pin_role` fires and name-matching never touches the Total
  role again for the life of the session. And the role it pins is the one the **default Fit does
  not use** (`volume_tab.py:1460-1463` converts `a_best`), so the exact out-of-the-box sequence —
  *New scar layer → draw → Measure* — fails.

---

### Theme 12 — Layer-tree and on-disk naming is machine vocabulary, and drops the year

**Major. [verified]**

`layer_group.py` builds group names as `<source> <pre>/<post> [<radius>] <product>` — e.g.
**`"Planet 7-20/7-21 20km HONC"`**. Two problems in one string:

1. `HONC` is an unexpanded acronym for *Highlight Optimized Natural Color*. It appears in **no
   visible label, tooltip or message** anywhere in the UI. The user ticks a box saying "Highlight
   Optimized Natural Color" and gets a folder called `HONC`. Likewise `TC` for "True colour (RGB)"
   and `dBright` for "Brightness change (dBrightness)" (`dock.py:90-97`).
2. `_md()` (`layer_group.py:31-40`) renders `2024-07-20` as `7-20`. **The year is gone.** Two
   events a year apart produce identical group names and collide visually in the Layers panel —
   which is precisely the situation an analyst working a detection catalogue is in.

Inside those folders the layer names are the raw output filenames
(`dock.py:2249`: `name = os.path.splitext(...)`), and the SAR tab is worse:
`"S1 change MT int-corr 6×pre→2023-09-21 (t131 VV, 7×7)"` (`sar_tab.py:2065-2066`) in a folder
called `MT-corr`, from a checkbox that said *"Texture — multi-temporal intensity correlation"*.

Full before → after tables in **§5**.

---

### Theme 13 — Precision theatre

**Major, mostly `Volume from area`.**

`_fmt` (`volume_tab.py:2703-2713`) renders anything ≥ 1000 with thousand separators and zero
decimals, and `_show_current` (`:2016`) adds a 4-decimal `Mm³` restatement. So the panel reads:

> `Volume (best):  1,300,000 m³   (1.3000 Mm³)`

Seven digits, then five significant figures — 100 m³ resolution — from a relation whose real
predictive spread for a single landslide is **a factor of 2–3**. The same 4-decimal treatment is
applied to areas (`:2001`) and to the ∫Δh covered area (`:1986`). This is the exact formatting
that makes a cross-check look decisive when it is not: *1.3000 vs 1.3 seismic* reads as agreement
to four decimals rather than "both land in the same order of magnitude, which is all either can
claim".

```python
def _fmt_vol(v):
    """Volumes at the precision the Larsen relation actually supports."""
    if v is None:
        return "—"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2g} × 10⁶ m³"
    return f"{float(f'{v:.2g}'):,.0f} m³"
```

and a standing caption under the volume rows:

```python
caveat = QLabel("This method is accurate to roughly a factor of two for any single "
                "landslide — quote it as an order of magnitude, not an exact figure.")
```

Leave `CSV_FIELDS` writing raw values as it already does.

Same family: **no sanity bound on the digitized area.** `_coefficient_volume`
(`volume_calc.py:106`) accepts any positive area. A 3 m² mis-click returns `1.10 m³`; the whole
valley at 1e8 m² returns **45 km³**, larger than any landslide in recorded history, with the same
confident formatting and the same ±1σ. Add a band check against the Larsen calibration range.

---

### Theme 14 — The documentation describes a plugin that no longer exists

**Major. [verified]** This costs you directly, because it is what a new user reads first.

| Document | What it says | Reality |
|---|---|---|
| `qgis_plugin/README.md` | documents **2 of the 6 tabs** | SAR, Fusion, 3D viewer and Volume are entirely absent |
| `qgis_plugin/README.md:36` | symlink example `/Users/ethanhasenauer/Documents/AEC Work/...` | wrong username **and** wrong location — the repo now lives in Google Drive |
| `qgis_plugin/README.md:144` | *"This is imagery-gathering only — scar delineation/area are done by hand in QGIS"* | false since the Fusion and Volume tabs shipped |
| `qgis_plugin/README.md:61`, `README.md:305`, `README.md:334`, `metadata.txt:5` | promise "Pick location on map" | does not exist (Theme 3) |
| `README.md:165-171`, `:184-187`, `:220-222` | *"The volume is computed from the TOTAL landslide outline … Only the total is converted"* and *"The source low/high no longer widen it"* | the code does the **opposite** under the default fit: `volume_tab.py:1460-1463` converts `a_best` and passes `conv_low, conv_high` |
| `metadata.txt:5` `description=` | never mentions SAR change detection, Fusion, or the ∫Δh volume path | four of the six tabs are invisible to anyone reading the plugin manager |

Also `planet_tab.py:15-16`'s module docstring still says *"v1 scope is BROWSE + MAP PREVIEW only
(no ordering/download yet)"* — the stale sentence that produced the false "no quota used" intro in
Theme 5, and `sar_tab.py:769-772`'s docstring says a Planetary Computer key **is required** ten
lines above the label that says *"No login needed"*.

---

## 4. The seismic-detection gap

This is not a missing feature. It is a missing *object*. Everything below follows from adding it.

### 4.1 The `Detection` — one record, one paste box, one source of truth

Add `detection.py` to the package and a **`Detection` group box at the top of the dock, above the
tab bar**, beside `Environment`. It holds one thing:

```python
@dataclass
class Detection:
    event_id:      str            # "AK2026-0204a"  — the catalogue key
    origin_utc:    datetime       # 2026-02-04 08:48:05Z
    lat:           float          # 60.500
    lon:           float          # -140.600
    loc_error_km:  float          # 17.0
    vol_best_m3:   float | None   # 1.3e6
    vol_low_m3:    float | None   # 0.9e6
    vol_high_m3:   float | None   # 1.7e6
    coherency:     float | None   # 0.61       provenance only
    hf_lf:         float | None   # 11.8       provenance only
    detection:     str | None     # "Y"        provenance only
    notes:         str = ""
```

**Paste-parsing is the whole point.** The analyst has the record on screen in some other window.
A `QPlainTextEdit` with `"Paste your detection record here"` and a `Read it` button, parsed with a
handful of tolerant regexes, removes six chances to mistype:

```python
_PAT = {
    "lat":       r"lat(?:itude)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)",
    "lon":       r"lon(?:gitude)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)",
    "loc_error": r"loc\.?\s*error\s*[=:]?\s*(\d+(?:\.\d+)?)\s*km",
    "vol":       r"vol(?:ume)?\s*[=:]?\s*(\d+(?:\.\d+)?)\s*M?\s*m\^?3",
    "vol_range": r"range\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)",
    "origin":    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*UTC",
    "coherency": r"coherency\s*[=:]?\s*(\d*\.?\d+)",
    "hf_lf":     r"hf\s*/?\s*lf\s*[=:]?\s*(\d*\.?\d+)",
}
```

Then **echo the timezone back, loudly**, because this is a real trap and not a hypothetical one:

```
Read:  AK2026-0204a
       2026-02-04 08:48:05 UTC   =   2026-02-03 23:48 Alaska time (previous day)
       60.500, -140.600  ± 17 km
       Seismic volume 1.3 × 10⁶ m³  (0.9 – 1.7)
```

Why it matters concretely: Sentinel-1 crosses Alaska on an ascending pass around **03:00 UTC**.
For this event that pass is 5.8 hours **before** the failure — a genuine "before" scene. An
analyst who types the local date/time they were handed (`2026-02-03 23:48`) into a field labelled
`Event time (UTC)` moves the pre/post boundary back nine hours and that scene is silently
reclassified as the **after** image. The change map then compares two pre-event scenes and finds
nothing, and there is no way to discover why.

**One `Detection`, four tabs.** Today the four tabs own separate lat/lon/time widgets
(`dock.py`, `planet_tab.py`, `sar_tab.py`, `viewer3d_tab.py`), and there is **no live sync
anywhere** — zero `textChanged` / `dateTimeChanged` connections between them. **[verified]** The
only bridge is a manual `Copy location & date from Sentinel-2 / Landsat tab` button in
`planet_tab.py:219`, `sar_tab.py:261` and `viewer3d_tab.py:222`, with no staleness indicator, so
the tabs silently diverge and the user finds out when the Fusion tab reports that the two change
rasters do not overlap.

> **Real bug, and a good illustration of the cost:** `planet_tab.py:218` carries the comment
> *"'&&' because a single & is a Qt mnemonic marker — it renders as 'location _date'"* and
> correctly writes `&&`. **`sar_tab.py:261` and `viewer3d_tab.py:222` both use a single `&`**, so
> two of the three buttons render as *"Copy location _date from Sentinel-2 / Landsat tab"*.
> The trap is documented in one file and hit in the other two. **[verified — the original review
> found only the SAR one; the 3D viewer has it too.]**

Once `Detection` exists, delete the three duplicate location forms and the three copy buttons.
The per-tab fields become read-only echoes of the detection, with a per-tab **Search radius** the
only thing that still varies.

### 4.2 Radius from location error — staged, wide then narrow

17 km of location error against a 5 km default radius is the single most consequential default in
the plugin (Theme 3). But widening to 17 km blindly is 11.6× the area, 11.6× the download, and on
the PlanetScope tab 11.6× the money.

**Stage it, and say so.** Add a `Coverage plan` control beside the radius that computes from
`Detection.loc_error_km`:

| Stage | Radius | Sensor | Cost | Purpose |
|---|---|---|---|---|
| **1. Wide sweep** | `ceil(loc_error)` = **17 km** | SAR (Sentinel-1) or Sentinel-2 — both free | none | find *where* the scar is anywhere in the disc |
| **2. Confirm** | **5 km** re-centred on the candidate | Sentinel-2 / Landsat | none | change rasters at full quality on a small box |
| **3. Detail** | **2–3 km** on the confirmed scar | PlanetScope | **quota** | delineation-grade imagery, ordered once |

with a caption that does the arithmetic out loud:

```python
self.plan_lbl.setText(
    f"Stage 1 covers the whole {2*err:.0f} × {2*err:.0f} km uncertainty square "
    f"({(2*err)**2:.0f} km²) with free radar/optical imagery.\n"
    f"Once you can see the scar, press 'Re-centre here' and Stage 2 drops to "
    f"{(2*r2)**2:.0f} km² — {(2*err)**2/(2*r2)**2:.0f}× less to download.\n"
    f"⚠ Ordering PlanetScope at the Stage 1 radius would spend about "
    f"{2*(2*err)**2:.0f} km² of quota, against {2*(2*r3)**2:.0f} km² at Stage 3.")
```

and a **`Re-centre here`** button that takes a map click (the picker from Theme 3) and moves the
search centre to it *while keeping the original detection coordinates in the `Detection`* — so the
export can record both, and the offset between them, which is itself a validation of the seismic
location.

Hard-gate the expensive mistake: if `Render detail (quota)` is pressed while
`radius_km > 1.5 * <the radius that would cover the confirmed scar>`, or simply while
`radius_km >= loc_error_km`, put it behind:

> *"You are about to order PlanetScope over the whole 34 × 34 km uncertainty square (about 2,300 km² of quota). Find the scar first with the free Sentinel-2 or SAR tabs, press 'Re-centre here', then order a 3 km box (about 72 km²)."*

### 4.3 The three-way volume reconciliation

The plugin can produce **three** independent volume estimates and can currently display **one at a
time, with no arithmetic between them**.

| Estimate | Source | Status today |
|---|---|---|
| **V_seismic** | force-history inversion — 1.3 Mm³ (0.9–1.7) | **cannot be entered anywhere** |
| **V_Larsen** | `A^γ` scaling from the digitized scar (`volume_calc.py:106-123`) | works, over-precise, mislabelled σ |
| **V_∫Δh** | DEM differencing over the outline (`dem_diff.integrate_dh`) | works, reports the **wrong quantity**, carries **no uncertainty** |

Five defects block the comparison:

**(a) Larsen and ∫Δh are mutually exclusive per Measure.** `volume_tab.py:1439-1440`:

```python
if fit == "ddem":
    return self._measure_ddem()
```

`_measure` early-returns before any area scaling happens. A ∫Δh row sets
`v_low/v_high/src_best/src_low/src_high = None` (`:1712-1713`); a Larsen row never carries
`v_erosion/v_deposit`. **The plugin owns two independent volumes for the same slide and refuses to
put them side by side.** Worse, measuring the same slide twice auto-renames it —
`self.name_edit.setText(_next_name(row["name"]))` (`:2509`) — so the two estimates land in the
table and the CSV as `"slide 1"` and `"slide 2"` with nothing recording that they are one event.
*The act of recording the cross-check destroys it.*

**Fix:** make Fit non-exclusive per *measurement*. Keep the combo as the **primary** fit, but have
`_measure` opportunistically also run the other path when its inputs are assigned, and store both
in one row: `v_larsen`, `v_larsen_lo`, `v_larsen_hi`, `v_dh_erosion`, `v_dh_deposit`, `v_dh_net`,
`sigma_dh`. One row per slide, up to three volumes in it.

**(b) The ∫Δh path reports NET Δh as "Volume (best)".** `volume_tab.py:1713` sets
`"v_best": r["v_net"]`, and `:1991-1992` renders it. For an outline that correctly captures both
scar and deposit — exactly what `FIT_OUTLINE["ddem"]` instructs — **the net is a small residual of
two large opposing numbers and approaches zero as the delineation gets better.** A slide with
2.0 Mm³ of erosion and 1.99 Mm³ of deposition reports `V best = 12,000 m³`. The seismic inversion
estimates the *mobilized* volume, which corresponds to `|V_erosion|`. And `viewer3d_tab.py:1893,
1906` copies `v_best` straight into the exported figure's `Volume` field, so a published relief
figure would be captioned with the near-zero net.

```python
# volume_tab.py:1715 — put the comparable quantity in the headline
"v_best":    abs(r["v_erosion"]),
"v_net":     r["v_net"],
"v_erosion": abs(r["v_erosion"]),
"v_deposit": r["v_deposit"],
```

and flip the sign for display (`dem_diff.py:372` sums `vals[vals < 0]`, so the panel currently
reads `"deposition 900,000 + erosion -850,000"` — a `+` between a positive and a negative).

**(c) "Volume (±1σ)" is the wrong σ.** `volume_calc.py:117-123` propagates only `std(log10 α)`,
`std(γ)` and the analyst's low/high area spread — the uncertainty in **where the regression line
sits**. It contains no term for the scatter of individual landslides about that line, which is the
dominant term (roughly a factor of 2–3 in Larsen's own data). The tab prints ±23 %/+30 % for this
event. Your own canonical script already flags the deeper ambiguity and the plugin never surfaces
it — `larsen_BR_volume.py:11-16`: the published σ *"[is] called standard deviation in their main
text but standard error in the supplement so their exact meaning is unclear."*

Relabel the row `"Likely range"` and put the truth in the tooltip; then use a **separate,
honest predictive σ** for the verdict test (below).

There is a second, quieter reinterpretation: `volume_calc.py:116`,
`stdlogA = ((d1 + d2) / 2.0) / 2.0`. The extra `/2` encodes *"the analyst's low-to-high span is
±2σ"*. Nothing on screen says that. An analyst who digitizes a conservative and a generous outline
is expressing a plausible range, i.e. roughly ±1σ. For best 60,665 m², low 45,000, high 80,000 the
code gives 0.935–1.808 Mm³ where the ±1σ reading gives 0.803–2.105 Mm³ — **a ~30 % narrowing that
directly flips the verdict against 0.9–1.7.** Either document it in the tooltip or add a
`Low/high outlines represent: [ ±1σ | 95 % range ]` control.

**(d) The ∫Δh path carries no uncertainty at all, though it is already computed and thrown away.**
`dem_diff.py:132` computes `sd = keep.std()` — the standard deviation of the difference over
quasi-stable ground, i.e. the per-pixel vertical error — uses it for clipping, and drops it.
`dem_diff.py:298-300` returns `stats` without it. Add it, and propagate:
`σ_V ≈ sd × A_covered / sqrt(N_eff)` with an autocorrelation-inflated `N_eff`, or conservatively
`σ_V ≈ sd × A_covered` for a fully-correlated bias. Without a σ, the more direct estimate cannot
participate in any agreement test at all.

**(e) An imported Δh (MOSART) is integrated with no bias check and no coverage check.**
`integrate_dh` accepts an `offset` argument (`dem_diff.py:336`) precisely so residual stable-ground
bias can be removed — and `volume_tab.py:1683` never passes it. **Volume from ∫Δh is linear in the
bias:** a DC offset of 0.5 m over a 1 km² outline integrates to 500,000 m³, **38 % of this event's
entire seismic volume**, reported as signal with a 4-decimal Mm³ readout. And only *zero* coverage
is caught (`volume_tab.py:1690`); 40 % coverage silently returns 40 % of the volume. The outline's
plan area is computed one line later (`:1696`) — the check is one subtraction away:

```python
cover = (r["covered_area_m2"] / area) if area else None
if cover is not None and cover < 0.90:
    self._notify(f"The elevation-change layer covers only {cover:.0%} of your outline, "
                 f"so this volume is roughly {cover:.0%} of the real one. Check the Δh "
                 "layer's extent and nodata before using it.", Qgis.Warning)
```

**The verdict rule.** Work in log₁₀. For each estimate *x*:

```
D_x  = log10(V_x / V_seismic)
σ_x  = (log10(V_x_high) − log10(V_x_low)) / 2
σ_s  = (log10(V_s_high) − log10(V_s_low)) / 2
```

For this detection, `σ_s = (log10 1.7 − log10 0.9)/2 = 0.138` (a factor of 1.37). Use a **realistic
predictive** `σ_Larsen ≈ 0.40` (factor ~2.5), **not** the ±1σ the tab prints.

```
|D| ≤ sqrt(σ_x² + σ_s²)                 →  AGREE
|D| ≤ 2 × sqrt(σ_x² + σ_s²), or ≤ log10(2)  →  MARGINAL
otherwise                               →  DISAGREE
```

**Do not use plain interval overlap.** Two wide log-normal intervals overlap almost
unconditionally, and — worse — overlap *rewards imprecision*: an analyst can force "agree" simply
by drawing a more generous low/high pair. The log-ratio test cannot be gamed that way. Against the
138× blunder from §3 Theme 1: `|D| = log10(179.7/1.3) = 2.14` versus
`sqrt(0.40² + 0.138²) = 0.42` → **5.1σ, flagged hard**.

**Add the cheapest sanity check in the whole tab: implied mean depth.** The tab already holds both
numbers in `_current` (`a_conv`, `v_best`) and never divides them. `V/A` for this event under the
bedrock fit is `1.30e6 / 60,665 = 21.4 m` — plausible for a deep bedrock detachment and checkable
against the DEM and the visible headscarp. Under soil it is 4.0 m. With a 2 km² outline in the
source-best slot it is **90 m**, which no analyst would accept for a second. Print it:

```python
f.addRow("Implied mean depth", self.depth_out)   # f"{v_best / a_conv:.1f} m"
```

**And name the material choice for what it is.** `Hillslope material` defaults to Bedrock
(`volume_calc.py:58-61`, no `setCurrentIndex`), and at this event's area the two options give
1.30 Mm³ vs 0.244 Mm³ — **a factor of 5.3, about twenty times wider than the ±1σ printed beside
it**. Bedrock is the right default for the Saint Elias, so keep it, but say the size of the
consequence in the tooltip and relabel the row `"What failed?"` with user-answerable options
(`"Rock — bare rock face, cliff, exposed bedrock scar"` / `"Soil / loose ground — vegetated slope,
till, colluvium"`).

There is a case neither option covers and it is the case this plugin exists for: **a rock-and-ice
avalanche detaching from a glacierized headwall.** Larsen calibrated on soil and bedrock hillslope
landslides; there is no ice class, ice is ~1/3 the density of rock, and the seismic "volume" is a
mass divided by an assumed density. Add a standing note under the material row:

> *"Neither option covers a rock-and-ice avalanche. If a large part of what moved was ice or entrained snow, this relation is outside its calibration, and the seismic volume depends on an assumed density — check that both numbers assume the same thing before comparing them."*

Finally, a geometry case that is common in exactly this terrain: **a multi-lobe source scar
contributes only its largest polygon** (`volume_tab.py:717, 1414-1416`). The reasoning is sound
for the intended case (three sketches of one outline) and wrong for a headwall with two or three
separate detachment zones — which are parts, not alternatives. And the obvious fixes are also
wrong: because γ = 1.41 the relation is superlinear, so `V(A₁+A₂)` overestimates and a merged
outline spanning the gap overestimates further. **The correct treatment — convert each lobe
separately and sum the volumes — is offered nowhere.** Offer it as a third option in the
multi-polygon handling.

### 4.4 Season and darkness triage for a February event at 60.5 °N

The plugin has everything needed to triage this automatically and does none of it.

At 60.5 °N on **4 February**, solar noon altitude is ≈ **13°** and the day is ~7.5 h — optical is
marginal, snow-covered and shadow-dominated. Sixty days earlier (**6 December**, the default
pre-window) the sun reaches ≈ **7°** for ~5.5 h: the default "before" window is *physically
unusable*. Ninety days later (**5 May**) is still snow-covered at elevation.

Add a **`Season check`** line to the `Detection` box, computed the moment a detection is read:

```python
"⚠ 4 Feb at 60.5°N: the sun is about 13° above the horizon at midday and the ground "
"is snow-covered.\n"
"   • Optical 'before' window (60 days back = 6 Dec) is in near-darkness — tick "
"'Winter event' so the search uses last summer instead (run_single --seasonal).\n"
"   • Optical 'after': the first reliably snow-free pass is usually mid-June. "
"Widen 'Days after' to ~140 days, or accept a snow-on comparison.\n"
"   • Start on the SAR (Sentinel-1) tab. Radar works in darkness and through cloud, "
"and a new pass arrives every 6–12 days."
```

and make the tab bar reflect it: when the detection month is 10–3, **open the plugin on the SAR
tab, not tab 1**, with a one-line banner saying why. That single behaviour would do more for a
winter-event novice than any wording change in this document.

`planet_tab.py:615` currently says *"A single PlanetScope strip is only a few km wide"* — a
PSScene footprint is roughly 25 × 16 km to 32 × 20 km, an order of magnitude larger, and a user
reading that beside a 5 km radius will shrink their search when the opposite reasoning applies.
Fix the sentence, and add the winter caveat to the zero-result branch: mid-winter Alaska/Yukon
PlanetScope coverage is sparse to absent, and nothing in the tab says so.

### 4.5 The verdict, and the export schema

**Nothing in the plugin records a conclusion.** `verdict`, `confirmed` and `inconclusive` appear
nowhere in the package except one unrelated string at `sar_tab.py:582`. **[verified]** The plugin
gathers evidence and cannot capture the finding — which *is* the deliverable of ground-truthing.

Add a **`Verdict`** box at the bottom of the `Volume from area` tab (or as a seventh tab, if it
grows):

```python
VERDICTS = [
    ("confirmed",     "Confirmed — a scar is visible and I have delineated it"),
    ("probable",      "Probable — something changed here but the scar is not clean"),
    ("not_found",     "Not found — I searched the area and see no scar"),
    ("obscured",      "Cannot tell — cloud, snow or darkness obscured the area"),
    ("wrong_place",   "Scar found, but well outside the reported location"),
    ("not_landslide", "Change is real but is not a landslide (e.g. glacier surge, avalanche, flood)"),
]
```

with, beside it: the evidence actually used (which tabs produced layers), the delineated area, the
three volumes, the computed verdict against §4.3's rule, the offset between the detection
coordinates and the digitized scar centroid, and a free-text note.

**Export schema.** Extend `CSV_FIELDS` (`volume_tab.py:188-212`) so a season of work is
machine-joinable to the detection catalogue:

```python
# identity — from the Detection, not typed
("event_id",            "det.event_id"),
("origin_utc",          "det.origin_utc"),
("det_lat",             "det.lat"),
("det_lon",             "det.lon"),
("det_loc_error_km",    "det.loc_error_km"),
("det_coherency",       "det.coherency"),
("det_hf_lf",           "det.hf_lf"),
# what the analyst found
("verdict",             "verdict"),
("scar_lat",            "scar_centroid_lat"),
("scar_lon",            "scar_centroid_lon"),
("offset_from_det_km",  "offset_km"),
# the three volumes, never sharing a column
("vol_seismic_m3",      "det.vol_best_m3"),
("vol_seismic_lo_m3",   "det.vol_low_m3"),
("vol_seismic_hi_m3",   "det.vol_high_m3"),
("vol_larsen_m3",       "v_larsen"),
("vol_larsen_lo_m3",    "v_larsen_lo"),
("vol_larsen_hi_m3",    "v_larsen_hi"),
("vol_dh_erosion_m3",   "v_dh_erosion"),
("vol_dh_deposit_m3",   "v_dh_deposit"),
("vol_dh_net_m3",       "v_dh_net"),
("vol_dh_sigma_m3",     "sigma_dh"),
("implied_depth_m",     "implied_depth"),
# the comparison
("D_larsen_log10",      "d_larsen"),
("D_dh_log10",          "d_dh"),
("agreement",           "agreement"),      # agree / marginal / disagree
("analyst_note",        "note"),
```

and pass `--event-id` from `dock._collect` (`dock.py:855-868`) so the output filenames carry the
catalogue key rather than `event_260204_0848` derived from the datetime.

Finally, **the exported figure is where the cross-check would be most visible and it is absent.**
`viewer3d_tab.py:460` offers one free-text `"Volume"` field with the placeholder *"paste the
estimate from the Volume tab"*, and `_pull_volume_details` (`:1893-1906`) reads `v_best` blindly —
so under the ∫Δh fit it prints the near-zero net, with no range, on a published figure. Replace
that single field with the reconciliation block:

```
Volume    Seismic     1.3 × 10⁶ m³  (0.9 – 1.7)
          Area scaling 1.3 × 10⁶ m³  (bedrock, source scar; ×2 typical spread)
          ∫Δh erosion  1.1 × 10⁶ m³  (± 0.2, 96 % of outline covered)
          → agree      (|D| = 0.07 ≤ 0.42)
```

---

## 5. Naming

### 5.a User-facing names

**Layer-tree group names** — `layer_group.py` builds `<source> <pre>/<post> [<radius>] <product>`.
Restructure to two levels using the existing `new_group()` / `subgroup()` API, and restore the year:

| Before | After |
|---|---|
| `Planet 7-20/7-21 20km HONC` | `AK2026-0204a — 60.500, −140.600` ▸ `PlanetScope · 2025-07-20 → 2026-07-21 · 20 km` ▸ `Natural colour (shadow-lifted)` |
| `S2 7-20/7-21 20km NDVI` | `AK2026-0204a — …` ▸ `Sentinel-2 · 2025-07-20 → 2026-07-21 · 20 km` ▸ `Vegetation index (raw)` |
| `SAR 7-20/7-21 Log-ratio` | `AK2026-0204a — …` ▸ `Sentinel-1 · 2026-01-30 → 2026-02-11 · ASC t94` ▸ `Brightness change` |
| `MT-corr` | `Texture change` |
| `Int-corr` | `Texture change` |
| `Brightness-z` | `Brightness change` |
| `HONC` | `Natural colour (shadow-lifted)` |
| `TC` | `True colour` |
| `dBright` | `Brightness change` |
| `dNDVI` | `Vegetation change` |
| `dNDSI` | `Snow/debris change` |
| `SWIR` | `SWIR debris view` |
| `False-color` | `False colour` |
| `Search AOI` (`dock.py:1713`) / `Search area — 5 km` (`dock.py:2116`) — two names, one thing | `Search area (5 km)`, one name, deduped by name before adding |
| `S1 change MT int-corr 6×pre→2023-09-21 (t131 VV, 7×7)` | `Radar change (texture) 2023-09-09 → 2023-09-21` — push `t131, VV, 7×7 window, 6 before-scenes` into `lyr.setAbstract()` |
| `S1 before 2023-09-09 (asce t94, VV)` | `S1 before 2023-09-09 (ASC t94, VV)` |
| `…_dndvi_2025-08-14_vs_2026-05-02` (raw filename as layer name, `dock.py:2249`) | `Vegetation change · 2025-08-14 → 2026-05-02` |
| `Event point` / unnamed point layer | `Reported epicentre (± 17 km)` |
| `instant_flip_3d.html` (always, overwrites) | `AK2026-0204a_3d_2026-02-04.html`, `_2` on collision |

**Output filenames** — currently `<event_id>_<side>_<date>_<kind>.tif` with
`event_id = "event_260204_0848"` derived from the datetime (`run_single.py:512`).

| Before | After |
|---|---|
| `event_260204_0848_post_2026-05-02_swir.tif` | `AK2026-0204a_after_2026-05-02_swir.tif` |
| `event_260204_0848_dndvi_2025-08-14_vs_2026-05-02.tif` | `AK2026-0204a_dndvi_2025-08-14_to_2026-05-02.tif` |
| `event_260204_0848_metadata.json` | `AK2026-0204a_metadata.json` |
| (no verdict output) | `AK2026-0204a_verdict.json`, `AK2026-0204a_volumes.csv` |

**Form labels** — the ones that block a decision:

| Before | After |
|---|---|
| `venv python` / `project dir` / `output dir` | `Python for the imagery tools` / `Folder with the imagery tools` / `Where to save results` |
| `Environment` | `Environment — set these three up once` |
| `Event time (UTC)` (×4, minute precision) | `Event date and time (UTC)`, with the AKT echo beside it |
| `Search radius` | `Search area radius` |
| `Source` (3D viewer, `viewer3d_tab.py:237`) | `Ground shape` |
| `Auto-fetch a DEM over the AOI` | `Download the ground shape for me (recommended)` |
| `Use a DEM layer loaded in the project` | `Use an elevation file I already loaded in QGIS` |
| `Terrain (DEM)` | `Ground shape (elevation)` |
| `SR tone curve` | `Image look` |
| `Coverage` (bare number `83`) | `Covers area` (`83%`) |
| `AOI %` (`sar_tab.py:372`) | `Coverage` |
| `Side` with cells `pre` / `post` | `Before / after` with cells `Before` / `After` |
| `Gap (d)` | `Days from event` |
| `Hillslope material` (default Bedrock) | `What failed?` — `Rock — bare rock face, cliff, exposed bedrock scar` / `Soil / loose ground — vegetated slope, till, colluvium` |
| `Volume (±1σ)` | `Likely range` |
| `Area → volume` / `Other areas (reported)` (wrong under ∫Δh) | retitle per fit: `Area covered by Δh` / `Elevation change` / `Erosion / deposition` |
| `A_best: matched "Source Area" by name. Change the A_best drop-down…` (`volume_tab.py:529` — no widget is called `A_best`) | `"Source area (best)": matched the layer "Source Area" by name. Change the "Source area (best)" drop-down to override.` |
| `Search / Preview` · `Preview on map` · `Run` (two contain "Preview") | `1. Find scenes` · `2. Show on map` · `3. Download && make layers` |
| `Run` (page) vs `Fuse` (button) vs `Run` (tab 1) vs `Run (full detail)` (SAR) | inner pages → `Setup` / `Advanced`; the Fusion button → `Run cross-check` |
| `Fade layover slopes (dim high signal on the layover-facing side)` | `Dim steep slopes that face the radar (they look falsely bright)` |
| `Radiometrically normalize brightness maps` | `Cancel whole-scene brightness shifts (snow, rain, thaw)` |
| `Remove the AOI-wide median from each layer` | `Remove the area-wide average change from each map (cancels basin-wide snowfall or melt)` |
| `Minimum blob area` | `Ignore patches smaller than` |
| `Also mask SCL 'dark area' (class 2)` | `Also ignore dark pixels — off, because in mountains most dark pixels are hillside shadow, not cloud` |
| `Download RGI 7.0 glacier complexes (Alaska)` | `Download glacier outlines (Alaska & Yukon, ~60 MB, needs a free NASA Earthdata account)` |
| `Quicklook gallery` | `Scene thumbnails (click one to select that scene)` |
| `Scene preview (amplitude render)` | `Selected scene (radar brightness picture)` |
| `Cached orders:` | `Imagery you already paid for:` |
| `Add epicentre point to 3D` | `Mark the slide location in 3D` |
| `Highlight Optimized Natural Color` (US spelling, beside `True colour` / `False colour`) | `Natural colour, shadow-lifted` |

### 5.b Source-module names

The package currently mixes three conventions:

- **`<domain>_<role>`** — `fusion_core`, `fusion_cloud`, `fusion_glacier`, `fusion_grid`, `fusion_tab`
- **`<domain>_<thing>` inconsistently** — `sar_change`, `sar_pairing`, but then `opera_s1`, `layover_dim`
- **bare topic names** — `dem_diff`, `volume_calc`, `centerline`, `web3d_export`, `layer_group`, `flow_layout`, `task`

**One scheme: `<domain>_<role>.py`, domains = `optical | sar | fusion | terrain | volume | ui`.**
Modules with no domain (shared infrastructure) take the `ui_` prefix. Tabs are always `*_tab.py`.

| Current | Proposed | Risk |
|---|---|---|
| `dock.py` | `optical_tab.py` (tab) + `dock.py` (shell only) | **HIGH** — `plugin.py:8` does `from .dock import LandslideDock`; `planet_tab.py:132` and `sar_tab.py:77` import colour constants from it. Do the `theme.py` split first (below), then move the tab class. |
| `planet_tab.py` | `planet_tab.py` — unchanged | none |
| `sar_tab.py` | `sar_tab.py` — unchanged | none |
| `sar_change.py` | `sar_core.py` | **MED** — imported by `sar_tab.py:71`, `fusion_tab.py:45`, `fusion_core.py:291/387/588/625` |
| `sar_pairing.py` | `sar_pairing.py` — unchanged (also has a `__main__` self-test; keep it working) | none |
| `layover_dim.py` | `sar_layover.py` | **MED** — `sar_tab.py:73`, `fusion_tab.py:44`, `fusion_core.py:386/479`, `fusion_grid.py:118` |
| `opera_s1.py` | **`research/opera_s1.py`** — move out of the installed package | **LOW** — nothing imports it; a repo-wide grep finds only its own docstring. It also `import requests` at module scope, which `layover_dim.py:146-147` records QGIS's Python does not have, so it would fail to import if anything ever did reach it. |
| `fusion_*.py` | unchanged — this family is already right and is the model | none |
| `dem_diff.py` | `terrain_diff.py` | **MED** — `viewer3d_tab.py:54`, `volume_tab.py:1652/1777` (both function-local) |
| `viewer3d_tab.py` | `terrain3d_tab.py` | **LOW** — only `dock.py:292` |
| `web3d_export.py` | `terrain3d_export.py` | **LOW** — only `viewer3d_tab.py:1942/2217`, both function-local |
| `volume_tab.py` | unchanged | none |
| `volume_calc.py` | `volume_core.py` (matches `fusion_core`) | **LOW** — `volume_tab.py:88` |
| `centerline.py` | `volume_centerline.py` | **LOW** — `volume_tab.py:87` |
| `layer_group.py` | `ui_layertree.py` | **MED** — imported as `lg` by `dock.py:31`, `fusion_tab.py:43`, `planet_tab.py:51`, `sar_tab.py:74`, `viewer3d_tab.py:55`, `volume_tab.py:89` — but always aliased, so it is six one-line edits |
| `flow_layout.py` | `ui_flow.py` | **LOW** — five `from .flow_layout import FlowRow` |
| `task.py` | `ui_task.py` | **LOW** — four `from .task import PipelineTask` |
| `project_state.py` | `ui_project_state.py` | **LOW** |
| — (new) | **`theme.py`** | see below |
| — (new) | **`detection.py`** | §4 |
| — (new) | **`symbol_color.py`** | ~200 lines of QGIS-symbol-to-RGB logic currently at `viewer3d_tab.py:2300-2470`, inside a 2,524-line file — extract so the wording and default fixes above are navigable |

**`theme.py` is the one that must happen first.** Today `sar_tab.py:77` and `planet_tab.py:132`
import `PRE_BG`, `POST_BG`, `ROW_FG`, `MUTED_FG`, `STATUS_COLORS`, `CLOUD_*` **from `dock.py`** —
so the shared design tokens live inside the largest tab file and every other tab depends on it.
Move `dock.py:115-129`, `:184` and `:201-204` into `theme.py`, re-export from `dock` for one
release, then drop the re-export. This is what makes §6 implementable and unblocks the `dock.py`
split.

**What is safe and what is not:**

- **Safe:** the plugin is installed via a **directory-level** symlink
  (`qgis_plugin/README.md:36`), so file renames *inside* the package need no reinstall.
- **Safe:** `metadata.txt` names no Python module (only `icon=icon.png`).
- **Safe:** `project_state.py` discovers widgets by reflection over *attribute* names on the dock
  and its tabs, not module names.
- **Risky:** `plugin.py:8` `from .dock import LandslideDock` — the only external entry point.
  Keep `dock.py` as the file that defines `LandslideDock` even after the tab code moves out.
- **Risky:** the six `import layer_group as lg` sites and the function-local imports inside
  `fusion_core.py` and `volume_tab.py` — grep for `from . import` before each rename, not after.
- **Not a module rename, but adjacent:** `sar_tab.py` writes change rasters to
  `<output>/sar/change` and `fusion_tab.py:845-858` discovers them by scanning that exact path.
  That directory name is a contract between two tabs. Do not "tidy" it without changing both.

---

## 6. Colour

There is currently no colour system. There are four independent ones: `dock.py:201-204`
(`STATUS_COLORS`), `dock.py:115-129` (table tints and cloud severity), `fusion_tab.py:83-89`
(`CLR_*`), and per-file one-offs (`viewer3d_tab.py:194`, `volume_tab.py:267, 772`). Two tabs
import the first from `dock.py`; the Fusion tab reinvents it with different hex values for the
same three meanings.

### 6.1 One token set, in `theme.py`, light and dark

```python
# theme.py — semantic tokens. Every value below was contrast-checked against the
# QGIS default panel (#F0F0F0) and Night Mapping (#333333); all are ≥ 4.5:1.
_LIGHT = {
    "ok":       "#1B7F3B",   # 4.6:1
    "warn":     "#A85B00",   # 4.5:1
    "error":    "#B3261E",   # 5.9:1
    "running":  "#1B5FA8",   # 5.8:1
    "accent":   "#2C6FA8",   # 4.7:1   primary action
    "idle":     "palette(mid)",
    "required": "#B3261E",
}
_DARK = {
    "ok":       "#4CC77C",   # 5.8:1
    "warn":     "#FFB454",   # 7.1:1
    "error":    "#FF8A80",   # 5.5:1
    "running":  "#6FB4F0",   # 5.6:1
    "accent":   "#7FB6E8",   # 5.8:1
    "idle":     "palette(mid)",
    "required": "#FF8A80",
}

def c(name, widget):
    """Theme-aware token lookup. Never hardcode a hex outside this module."""
    dark = widget.palette().color(QPalette.Window).lightness() < 128
    return (_DARK if dark else _LIGHT)[name]
```

### 6.2 Status: idle / running / ok / warn / error

| State | Where it must appear | Token | Non-colour cue (required) |
|---|---|---|---|
| **idle** | step rows not yet reached; disabled buttons | `idle` | `…` prefix |
| **running** | progress bar, status label, the running tab's own label | `running` | animated bar + `"— 12 s elapsed"` |
| **ok** | satisfied steps, successful completion | `ok` | `✓` prefix |
| **warn** | usable but worth reading (cross-orbit pair, partial coverage, stale terrain) | `warn` | `⚠` prefix |
| **error** | blocks the run, or a failure | `error` | `✗` prefix |

`fusion_tab.py` already gets half of this right — `'✓'` is prefixed at `:239` and `:250`. Finish
it: `:242 → "✗ Choose an optical raster"`, `:259 → "✗ No SAR raster covers this area"`,
`:263 → "✗ Choose a SAR raster"`, `:270 → "✗ Nothing to produce"`, `:276 → "… Waiting"`,
`:254 → "— Not needed"`, `:273 → "▶ Ready"`. Then replicate the whole strip on the other five tabs.

### 6.3 Required vs optional fields

Do not colour optional fields at all — colour is a poor "absence" signal. Instead:

- **Required and empty**, once the user has tried to act: `1px solid {c("required")}` on the
  widget, cleared on the field's own `textChanged`. Never on first paint — an untouched form
  covered in red borders reads as broken.
- **Optional**: append a plain-text ` (optional)` to the label. `dock.py:644` already tries this
  with grey `palette(mid)` text reading `"Optional: …"` inside a collapsed box — the word is right,
  the placement is not.
- **Blocked-because-of-something-else**: never grey a button without saying why. Make the tooltip
  state-dependent (`fusion_tab.py:397-400` is the model to fix first — its disabled tooltip is the
  most jargon-dense string on the tab and never says why the button is off):

  ```python
  self.run_btn.setToolTip(
      "Cross-check the two change maps and write a landslide score map."
      if self.run_btn.isEnabled() else
      "Can't run yet — see the numbered steps above: " + first_unmet)
  ```

### 6.4 The primary action in each button row

One bold `setDefault(True)` button per row, and it must be **the action the user should take
next**, not the most expensive one. Two are currently wrong:

- `sar_tab.py:326-329` bolds `Run (full detail)` — which downloads a higher-resolution grey
  picture. The action that finds landslides, `Compute change map` (`sar_tab.py:654`), is unstyled
  **and hidden inside a collapsed panel positioned above the Search button it requires**.
- `planet_tab.py:525` bolds `Render detail (quota)` — **the button that spends money** — in a row
  of eight, four of which are disabled with no stated reason.

```python
def _emphasize(self, btn):
    """Exactly one bold/default button: the next step, not the biggest one."""
    for b in (self.search_btn, self.map_preview_btn, self.run_btn, self.cd_btn):
        f = b.font(); f.setBold(b is btn); b.setFont(f)
        b.setDefault(b is btn)
```

Call `_emphasize(self.search_btn)` at build time and
`_emphasize(self.cd_btn if (npre and npost) else self.map_preview_btn)` after a search.
Give the primary action `background: {c("accent")}` and white text; leave every other button
default-styled. Split the Planet row into two labelled `FlowRow`s — `Find scenes — free` and
`Uses your Planet quota` — and *hide* rather than grey the follow-ups until they apply.

### 6.5 Severity in result tables

Three columns carry severity today and each does it differently. Unify on **one scale, applied to
the cell text colour, with the word in the cell**:

| Column | Thresholds | Token | Cell text |
|---|---|---|---|
| **Cloud over search area** | ≤ 10 % / ≤ 40 % / above | `ok` / `warn` / `error` | `"6% clear"` / `"28% some"` / `"71% heavy"` |
| **Covers area** (AOI %) | ≥ 90 % / ≥ 50 % / below | `ok` / `warn` / `error` | `"96%"` / `"63% partial"` / `"12% edge only"` |
| **Fusion score** | ≥ 0.80 / ≥ 0.40 / below | `error` (= strongest candidate) / `warn` / `idle` | `"0.86 strong"` / `"0.51 possible"` |

Note the deliberate inversion on the third: on a *hazard* map, red means "look here", not "bad".
Say so in the legend so the two scales are not confused.

Current unmeasured-state encodings must survive greyscale. `dock.py:1093` can produce
`"12%"`, `"~45%"`, `"❄ 8%"` or nothing, in four colours — five encodings whose meanings exist only
in a per-cell tooltip. Make the prefixes explicit suffixes: `f"~{scene_cloud:.0f}% (estimate)"`,
`+ " (check picture)"` for the snow-swamped case, and add a visible legend under the table.

### 6.6 Ramps for the plugin's own output rasters

| Raster | Ramp | Status |
|---|---|---|
| **dBright** | one-sided, transparent at 0, `#6baed6 → #2171b5 → #08306b` to −0.30 (`dock.py:2217-2220`) | **correct — keep exactly as is** |
| **dNDVI** | the same one-sided ramp at `lo = −0.30` | **missing** — loads as grey (Theme 8) |
| **dNDSI** | the same one-sided ramp at `lo = −0.30` | **missing** — loads as grey |
| **SAR log-ratio / brightness-z** | diverging red↔blue, transparent at 0 (`sar_tab.py:135-140`) | correct, and the transparent centre is right |
| **SAR int-corr / mt-corr** | one-sided white→`#a50026` (`sar_tab.py:141-145`) | correct |
| **Fusion score** | `SCORE_RAMP` (`fusion_tab.py:76-78`) | correct hues; **labels must become words** (Theme 8), and the fully-transparent `< 0.20` band needs the null-result message beside it |
| **Δh (elevation change)** | **missing** — add a symmetric diverging ramp about 0, `#01665e ← 0 → #8c510a`, transparent within ±(2 × stable-ground σ) from `dem_diff.py:132` | this is the one place where a *symmetric* ramp is correct, because erosion and deposition are both signal |

One consistency note: the SAR diverging ramps encode **polarity** as a hue (red = brighter after,
blue = darker after), and the measured behaviour on Iliamna was that 53 % of slide pixels brighten
and 47 % darken. The ramp is not wrong — the polarity is real information — but the legend should
say so, because a user who has learned "red = slide" from one event will misread the next.

### 6.7 Dark-theme safety — the current offenders

Every value below is a hardcoded hex on a widget whose background follows the QGIS theme. Ratios
computed against Night Mapping's `#333333` panel:

| Site | Hex | Contrast on dark | Verdict |
|---|---|---|---|
| `viewer3d_tab.py:194` | `#b2182b` | **1.80 : 1** | the one message explaining why the entire tab is dead is the least legible text on it |
| `volume_tab.py:772` | `#c62828` | **2.21 : 1** | the role-mismatch warning |
| `fusion_tab.py:85` `CLR_BAD` | `#c0392b` | **2.29 : 1** | every blocked step |
| `dock.py:202` `error` | `#c62828` | **2.21 : 1** | login failure |
| `dock.py:202` `success` | `#2e7d32` | **2.40 : 1** | login success |
| `volume_tab.py:267` | `#e65100` | **3.32 : 1** | the tab's most important instruction |
| `dock.py:203` `warn` | `#e65100` | **3.32 : 1** | |
| `fusion_tab.py:83` `CLR_OK` | `#2e9e5b` | **3.62 : 1** | |

And a second family that is theme-independent but still fails, because the cells set **both**
background and foreground — pale row tints `PRE_BG` `#DCEBFC` / `POST_BG` `#E0F4E2` with:

| Foreground | On `PRE_BG` | Verdict |
|---|---|---|
| `MUTED_FG` `QColor(120,120,120)` (`dock.py:184`, greys every row a Run won't use) | **3.60 : 1** | fails AA |
| `CLOUD_UNKNOWN` `QColor(130,130,130)` | **3.09 : 1** | fails AA |
| `CLOUD_SOME` `QColor(176,122,0)` | **3.04 : 1** | fails AA |
| `CLOUD_CLEAR` `QColor(30,138,54)` | **3.61 : 1** | fails AA |
| `CLOUD_HEAVY` `QColor(197,30,42)` | 4.78 : 1 | passes |

So three of the four cloud severity colours and the "this row will not be used" grey are all below
AA on the very tints that were chosen to make them readable. The comment at `dock.py:120-123`
says they were *"darkened a touch from pure web hues so the text stays legible on the pale
pre/post row backgrounds"* — the intent was right, the amount was not enough. Use the §6.1 tokens
and re-check against the actual tint, not against white.

`viewer3d_tab.py` already does the right thing in four places (`:191`, `:333`, `:344`, `:363` use
`palette(mid)`) — the file knows the correct idiom and departs from it exactly where it matters
most. Replace `:194` with a theme-driven treatment:

```python
warn.setStyleSheet("QLabel { color: palette(text); background: palette(alternate-base); "
                   "border: 1px solid palette(mid); border-radius: 4px; padding: 6px; }")
```

### 6.8 Where colour is currently the sole carrier of meaning

1. **Fusion input tags** (`fusion_tab.py:813-823`): an orange `▌` prefixes the optical input and a
   blue `▌` the SAR input, with **no key anywhere** — they read as decoration. Either give them a
   key or drop them.
2. **Fusion step panel**: satisfied vs blocked is `CLR_OK` green vs `CLR_BAD` red — the classic
   red/green pair — mitigated only by the `✓` that appears on *some* rows. Finish the glyphs (§6.2).
3. **The Cloud column** (`dock.py:1093, 1143`): four colours, no legend, meaning only in a tooltip.
4. **Muted rows** (`dock.py:1104, 1106`): "this row will not be used by a Run" is carried entirely
   by `MUTED_FG` at 3.6:1. Add a word to the `Use` column, or a `—` marker.
5. **SAR pair summary** (`sar_tab.py:1119-1121`): `sar_pairing.py:79` tags a cross-orbit pair
   `"⚠ cross-orbit — geometry differs"` — the glyph is there, but the label is italic grey and
   reads as a footnote. Key the style off the text so an alarming state looks alarming.
6. **SAR change overlays**: `"red = likely surface change"` is stated once in the change-detection
   info label (`sar_tab.py:566-582`) and nowhere near the layer it describes. Put it in the layer's
   legend labels, which the QGIS Layers panel will show.

---

## 7. Accessibility

**The package contains zero accessibility metadata. [verified]** `setAccessibleName`,
`setAccessibleDescription` and `setWhatsThis` appear in **no file**. A screen-reader user gets the
widget class and the visible text and nothing else — which for the three file pickers means
hearing `"button …"` three times in a row (`dock.py:342`, `QPushButton("…")`).

**Contrast.** Eight hardcoded colours below AA on the dark theme, four table foregrounds below AA
on the pale row tints. Numbers in §6.7. Fix via the `theme.py` tokens; do not fix them individually.

**Keyboard and mnemonics.**

- **No `setShortcut`, no `QKeySequence`, no `setTabOrder`, no `setBuddy` anywhere in the package.
  [verified]** Tab order is construction order, which is mostly correct by luck; there is no
  keyboard route to the primary action of any tab, and no `QLabel.setBuddy` means `Alt+`-jumping
  to a form field is impossible.
- **The Qt mnemonic bug, in two files.** `planet_tab.py:218` documents the trap and gets it right
  with `&&`; **`sar_tab.py:261` and `viewer3d_tab.py:222` both use a single `&`**, so both buttons
  render as *"Copy location _date from Sentinel-2 / Landsat tab"* and silently claim `Alt+D`.
  `dock.py:659` and `sar_tab.py:797` get it right. **[verified]** Grep for
  `QPushButton("[^"]*&[^&]` before every release.
- Add the four that matter: `Ctrl+Return` on each tab's primary action, `Esc` to cancel a running
  task, `F1` → the tab's own help, and `setBuddy` on every form label.

**Touch and pointer targets.** `dock.py:342` sets `btn.setFixedWidth(28)` on the three file
pickers. 28 px wide × ~24 px tall clears WCAG 2.2 SC 2.5.8 (24 × 24 minimum) by a hair, but it is
well under the 44 px comfortable target, it is the control a first-time user must hit three times
during setup, and its label is a single ellipsis with no accessible name. Widen to a labelled
`Browse…` button (~72 px) — there is room, because the row is a `QHBoxLayout` inside a form.
`dock.py:393`'s `setFixedWidth(56)` on `add_pa_btn` has the same problem in milder form.

**Font scaling.** `dock.py:204-210` rescales the dock's base font by the geometric mean of its
width/height against a 380 × 720 baseline, clamped to `[0.8, 1.5]`. The intent is good and the
implementation is careful. Two hazards: (a) at 0.8× it *shrinks below* whatever font size the user
chose in QGIS or their OS, which is a direct accessibility regression for anyone who raised their
system font on purpose — clamp the lower bound to 1.0 and let the panel scroll instead; (b) the
monospace log panes hardcode `font-size: 11px` (`fusion_tab.py:411`), which the rescale does not
touch, so the smallest text in the plugin is also the only text immune to the scaling.

**The 360 px minimum dock width** (`dock.py:274`) is the constraint the rest of the layout fights:

- The scene table is seven columns sized with `resizeColumnsToContents()` and
  `setStretchLastSection(True)` (`dock.py:1221-1222`), with a ~55-character Sentinel-2 scene ID in
  the stretched last column — producing a horizontal scrollbar *inside* an already
  horizontally-scrolling dock. Fix: `setStretchLastSection(False)`, `setColumnWidth(<scene id>, 90)`
  with the full id in the cell tooltip, `QHeaderView.Stretch` on Date.
- **No table in the plugin is sortable.** `setSortingEnabled(True)` plus numeric sort keys via
  `item.setData(Qt.EditRole, float(...))` on the Gap / Cloud / Coverage cells is a two-line change
  that removes most of the horizontal-scrolling problem by letting the user sort instead of scan.
- The Planet tab splits the same 360 px across **four always-open panes**
  (`planet_tab.py:681-684`, stretch 3/3/2/2), so the candidate table — the control the whole tab
  exists to serve — gets about a third of the panel. Tab 1 does the opposite deliberately
  (`dock.py:560-590, 613-618`, gallery and preview collapsed at zero stretch, with the reasoning
  written out: *"The table is the primary signal, so it dominates."*). Mirror tab 1.
- The Fusion tab nests a second `QTabWidget` (`fusion_tab.py:151`) inside the plugin's own
  six-tab bar. There is ~180–200 px of separation, so it is not as confusing as it sounds, but a
  `QgsCollapsibleGroupBox("Advanced settings — you should not need these")` matches the idiom used
  everywhere else in the plugin and removes the second tab bar entirely.

---

## 8. Phased plan

Effort is developer-days for someone who knows this codebase (i.e. you).

### First — make it possible to finish a task at all  ·  ~8–11 days

| # | Item | Effort | Files |
|---|---|---|---|
| 1 | **`theme.py`** — extract `PRE_BG`/`POST_BG`/`ROW_FG`/`MUTED_FG`/`CLOUD_*`/`STATUS_COLORS` out of `dock.py`, add the §6.1 token function, re-export from `dock` for one release | 0.5 | `theme.py`(new), `dock.py`, `planet_tab.py`, `sar_tab.py` |
| 2 | **Environment box** — self-opening when unconfigured, labelled, placeholdered, tooltipped, live-validated; one shared refusal string; `self.env_box` stored so tabs can open it (Theme 2) | 1.5 | `dock.py`, + the 11 refusal sites in `planet_tab.py`, `sar_tab.py`, `viewer3d_tab.py`, `fusion_tab.py` |
| 3 | **Map-pick tool + `Use map centre` + pasted-pair parsing** (Theme 3) | 1 | `dock.py`; delete the duplicate forms in the other three tabs after item 8 |
| 4 | **Failure escalation** — `task.py` `exit_code`/`error_tail`; every `if not result:` becomes a `_warn` + log-scroll; `pushInfo` → `pushWarning` on zero results and caught exceptions; `planet_tab._collect` gains the missing `_api_key()` check; `volume_tab` routes all 20+ log-only failures through the existing `_notify` (Theme 4) | 2 | `task.py`, `dock.py`, `sar_tab.py`, `planet_tab.py`, `viewer3d_tab.py`, `volume_tab.py`, `run_single.py` |
| 5 | **The mnemonic bug** — `sar_tab.py:261` and `viewer3d_tab.py:222` `&` → `&&` | 0.05 | 2 files |
| 6 | **Defaults for the actual events** — `DEFAULT_SCENES` incl. the three change layers; event time −30 d; `--seasonal` checkbox wired into `_collect`; Planet window 60/90 and `coverage="aoi"`; future-window guard (Theme 6) | 1 | `dock.py`, `planet_tab.py` |
| 7 | **Style dNDVI and dNDSI** with the generalised `_style_dbright` (Theme 8) | 0.5 | `dock.py` |
| 8 | **`detection.py` + the `Detection` box**, paste-parsing, AKT echo, one source of truth feeding all four tabs; delete the three copy buttons (§4.1) | 2 | `detection.py`(new), `dock.py`, `planet_tab.py`, `sar_tab.py`, `viewer3d_tab.py` |
| 9 | **Size guards** — refuse-with-a-suggestion in `viewer3d._warp_selected_strip` and `volume._difference_dems_impl`; live cell-count captions (Theme 7) | 0.75 | `viewer3d_tab.py`, `volume_tab.py` |
| 10 | **Data-loss** — `Save outlines…`; `Write to layer` requires an explicit selection; `blockSignals` on the `New scar layer` role fill; uniquify `instant_flip_3d.html` (Theme 11) | 1 | `volume_tab.py`, `viewer3d_tab.py` |

### Second — make it interpretable, and make it record a conclusion  ·  ~10–14 days

| # | Item | Effort | Files |
|---|---|---|---|
| 11 | **Step panels on all six tabs**, modelled on `fusion_tab.py:227-277`, with the §6.2 glyphs and state-dependent disabled-button tooltips | 2.5 | all six tab files |
| 12 | **Result panels** — plain-language outcome after every run; Fusion's null-result message; `"Zoom to result"`; `"✓ Done"` step state; layers renamed by meaning; score legend in words (Theme 8) | 2 | `fusion_tab.py`, `dock.py`, `sar_tab.py` |
| 13 | **Volume cross-check, part 1** — one row holds all three volumes; ∫Δh reports `\|erosion\|` not net; implied mean depth; `_fmt_vol`; area band check; relabel `Volume (±1σ)`; per-fit row labels (§4.3) | 2.5 | `volume_tab.py`, `volume_calc.py` |
| 14 | **Volume cross-check, part 2** — `dem_diff` returns the stable-ground σ; Δh coverage ratio check; `offset=` passed to `integrate_dh`; the log10 agreement rule and the verdict readout (§4.3) | 2 | `dem_diff.py`, `volume_tab.py` |
| 15 | **Verdict box + export schema** — `VERDICTS`, evidence summary, offset from the reported epicentre, the extended `CSV_FIELDS`; `--event-id` passed from `_collect`; the reconciliation block on the exported figure (§4.5) | 2 | `volume_tab.py`, `dock.py`, `viewer3d_tab.py` |
| 16 | **Staged radius plan + Planet cost gate + quota confirmation + cancel warning + `authcfg` for the API key + delete the plaintext password** (§4.2, Theme 5) | 2 | `dock.py`, `planet_tab.py` |
| 17 | **Season/darkness triage** — the `Season check` line, and opening on the SAR tab for an Oct–Mar detection (§4.4) | 0.75 | `detection.py`, `dock.py` |
| 18 | **Jargon pass** — the Theme 9 table applied verbatim; `_orbit_tag()`; `%` on coverage; `Before`/`After` in the tables; header tooltips everywhere | 1.5 | all six tab files |
| 19 | **Checkbox gets its own `Use` column** + legend + constructive refusal, on all three scene tables (Theme 10) | 1 | `dock.py`, `sar_tab.py`, `planet_tab.py` |

### Third — polish, structure, and the documentation  ·  ~7–9 days

| # | Item | Effort | Files |
|---|---|---|---|
| 20 | **Progress and status** — bar visible for every long action; determinate download counts; `status_lbl` driven off the log stream; elapsed timer; gate the raw command line behind `verbose` (Theme 7) | 1.5 | `task.py`, all six tab files |
| 21 | **Move the blocking work off the GUI thread** — `_flip_to`'s `gdal.Warp` and `_build_web_viewer` onto `QgsTask.fromFunction` (the pattern already exists at `viewer3d_tab.py:1029`); `_difference_dems_impl` likewise; the glacier/terrain fetch out of `fusion._run` and behind a `QProgressDialog` | 2 | `viewer3d_tab.py`, `volume_tab.py`, `fusion_tab.py` |
| 22 | **Layer-tree restructure** — two-level per-event grouping, year restored in `_md()`, `PRODUCT_FOLDER` map, SAR folder names matching the checkboxes, `setAbstract()` for the diagnostics (§5.a) | 1.5 | `layer_group.py`, `dock.py`, `sar_tab.py`, `planet_tab.py` |
| 23 | **Accessibility** — `setAccessibleName`/`setAccessibleDescription` on every non-obvious control; `setBuddy` on form labels; `Ctrl+Return` / `Esc` / `F1`; `Browse…` instead of `…`; font-scale lower bound → 1.0; sortable tables; Planet splitter mirrors tab 1 (§7) | 2 | all files |
| 24 | **Module renames**, in the risk order from §5.b; `opera_s1.py` → `research/`; extract `symbol_color.py` from `viewer3d_tab.py` | 1 | package-wide |
| 25 | **Documentation** — `qgis_plugin/README.md` covers all six tabs, correct symlink path, honest limitations; `README.md:165-171` rewritten to match what the code actually converts; `metadata.txt` `description=`/`about=` rewritten (and the map-pick promise becomes true once item 3 lands); the three stale docstrings (`planet_tab.py:15-16`, `sar_tab.py:769-772`, `planet_tab.py:501`) (Theme 14) | 1.5 | `README.md`, `qgis_plugin/README.md`, `metadata.txt`, 3 modules |

**If you only do three things:** items **2** (Environment), **8** (`Detection`), and **15**
(Verdict + export schema). Those three convert the plugin from *an imagery-fetching toolkit that a
remote-sensing specialist can drive* into *a ground-truthing instrument that takes a detection in
and produces a defensible, joinable verdict out* — which is what it is named for, and the only
part of the gap that no amount of wording changes can close.
