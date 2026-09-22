# Merge-rule benchmark: ascending + descending SAR

Scores the SAR tab's ways of combining an ascending and a descending change map
by what they do to the **Fusion** result, against the hand-digitised scars of six
truthed Alaska events (Azumi, Hubbard, Iliamna, Knik–Barry, Mt. Logan, Valdez).

It exists because an earlier harness lived only in a session scratchpad and was
lost. It replicates the plugin's own code paths rather than re-deriving them:

1. **`make_sar.py`** — `SarTab._cd_compute`, headless: the tab's scene rules (the
   after-scene must image the event point and have ≥ 3 same-track before-dates;
   up to 6 before-scenes, one per day), VV at 20 m, all four detectors, Lee 5×5,
   7×7 window, radiometric normalisation, 8 px sieve — once per orbit direction.
2. **`score.py`** — builds each merge variant, then `FusionTab._fuse` with the
   *default* preset (dBright + dNDSI averaged, four SAR detectors MAX-pooled,
   detrend, cloud mask, mean fusion, 5×5 smoothing, 0.05 km² sieve at 0.20) and
   scores it.

| Variant | What it is |
|---|---|
| `none` | optical only |
| `asc`, `desc` | one pass |
| `merged` | stronger pass per pixel, no terrain masks |
| `merged+masks` | stronger pass per pixel with shadow + layover masks (**shipped default**) |
| `fill-auto` / `fill-asc` / `fill-desc` | fill-only merge: one pass leads, the other fills its blind spots |

Metrics (a pixel Fusion leaves unscored counts as *not flagged*):

- **bg50** — background flagged at 50% recall of the scar (the 6-event benchmark's metric)
- **rank** — where the first candidate blob touching the scar lands, by peak score
- **area** — share of the scar at or above the 0.20 display threshold
- **false area** — share of *non-scar* ground at or above 0.20: the noise the map shows

## Running

```bash
./benchmarks/merge_rules/run.sh
```

Needs QGIS's Python (for GDAL) and the network. The first run downloads ~80
Sentinel-1 scenes (cached afterwards); `SKIP_SAR=1` re-scores cached maps.
Inputs and outputs are set in `config.py` and can be overridden:
`LANDSLIDE_BENCH_DIR` (work dir, default `out/benchmarks/merge_rules`, gitignored),
`LANDSLIDE_OPTICAL_DIR` (Run packages, default `out/interactive/qgis_packages`),
`LANDSLIDE_SCAR_DIR` (the shared drive's per-event `Total Area.gpkg` folders;
always read layer `landslide_scars` — see the stray-layer trap in the notes).

## Result, 2026-09-22

10 km AOI, 60 days before / 30 after, cloud mask on. Knik has no usable ascending
pass, so every SAR rule is descending-only there.

| Variant | bg50 worst | bg50 mean | false area mean | worst rank |
|---|---|---|---|---|
| no SAR | 6.35% | 2.56% | 27.3% | 24 |
| stronger pass, no masks | 5.78% | 1.29% | 31.0% | 12 |
| **stronger pass + masks (default)** | 4.82% | **1.08%** | 29.6% | 10 |
| **fill-only, auto primary** | **3.67%** | 1.21% | **22.8%** | 10 |

- **Neither rule dominates.** The stronger-pass rule wins where both passes saw
  the slide (Iliamna bg50 0.50% vs 2.49%; Hubbard rank 5 vs 8). Fill-only wins
  the worst case (Valdez 3.67% vs 4.82%) and shows less false change on all five
  two-pass events.
- **The masks leave ground blind to both passes unscored in Fusion.** At Mt. Logan
  6.8% of the scar goes unscored and the detected area falls from 83.8% to 73.9%;
  the masks still improve the worst case (5.78% → 4.82%). Untested fix: fall back
  to the unmasked value where both passes are blind.
- **Harness check:** the optical-only baseline reproduces the earlier recorded
  numbers within 0.3 points on five of six events. Ranks are worse than the
  2026-09-17 run's because a 10 km AOI holds more competing blobs than an 8 km one.

Six events, five with both passes: differences of a few tenths of a percent are
within noise.
