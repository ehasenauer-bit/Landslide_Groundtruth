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
