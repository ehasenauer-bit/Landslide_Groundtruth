"""Optical + SAR change fusion for landslide detection over snow-covered terrain.

The detection problem this module solves
----------------------------------------
A rock/ice avalanche onto Alaska snow leaves two independent signatures:

  optical   dark debris replaces bright snow → brightness (or NDSI) DROPS
  SAR       rough rubble replaces specular snow → C-band backscatter RISES

Neither alone is trustworthy here. A snowfall, a melt onset, a cloud shadow or a
seasonal illumination shift each produce an optical change far larger than any
slide; layover, wet snow and speckle each produce a SAR change of the same size.
What they do NOT do is produce BOTH signatures at once with the right signs —
dry snow is largely transparent at C-band, and a radar artefact leaves the
albedo alone. So the fusion is a soft AND, and the AND is the whole point.

Sign bookkeeping (the thing that is easiest to get backwards)
-------------------------------------------------------------
Every quantity here is converted to "evidence-positive" form exactly once, in
`orient_evidence`, and everything downstream assumes bigger = more landslide-like:

  dBright / dNDSI   landslide DECREASES them          → multiply by −1
  log-ratio         10·log10(pre/post), so a brighter-after deposit is
                    NEGATIVE                          → multiply by −1
                    (this is sar_change.DEPOSIT_SIGN["logratio"], reused rather
                    than re-derived — see sar_change.deposit_oriented)
  intensity z-score post-minus-pre, deposit already POSITIVE → multiply by +1

Get this wrong and the detector confidently finds the opposite of a landslide,
so `orient_evidence` is the single place it is written down.

Why rank normalisation needs absolute floors
--------------------------------------------
The two inputs are not comparable in raw units (albedo difference vs decibels)
and are not even similarly distributed — dBright over snow is bounded and
bimodal, log-ratio after multilooking is roughly Gaussian with σ ≈ 1–1.5 dB. So
each is converted to a percentile rank. But pure ranking has a nasty failure:
in a quiet AOI the 99th percentile of dBright might be −0.03, pure noise, and
it would rank 1.0 exactly like a −0.7 deposit in a real one. Ranking alone makes
every AOI look like it contains a detection.

The fix is an absolute admission floor per layer, in that layer's own physical
units, applied BEFORE ranking: a pixel that does not clear the floor scores a
hard 0 and is excluded from the ranking population. Ranks then describe only
pixels that already cleared a physically meaningful bar. If nothing clears it,
the output is legitimately empty — which is the answer we want in a quiet AOI.

Terrain weighting: source zone, not per-pixel slope
---------------------------------------------------
Glacier suppression by "drop low-slope pixels" deletes the thing being looked
for: an Alaska rock avalanche detaches at 40–60° and runs out onto a glacier
tongue at 2–8°, and the deposit — flat, large, high-contrast — is the detectable
half. `slope_weight` therefore ramps on the MAXIMUM slope within a radius, not
on the pixel's own slope, so a flat deposit below a steep headwall keeps its
score while a flat glacier interior with no steep contributing terrain loses it.

Pure numpy + lazily-imported GDAL, because QGIS ships no scipy: the focal
maximum is a separable two-pass running max (scipy.ndimage.grey_dilation is not
available), mirroring how sar_change implements its window statistics.
"""
import numpy as np

# score bands, in the order fusion_core writes them
BAND_NAMES = ("optical_rank", "sar_rank", "slope_weight", "glacier_weight",
              "score_preglacier", "score", "confidence", "optical_only")

# Confidence codes written into the confidence band. NaN means neither sensor
# measured the pixel at all; every finite code below means something WAS measured.
CONF_NONE = 0          # reserved; fuse() writes NaN for the nothing-measured case
CONF_SAR_ONLY = 1      # optical masked (cloud/nodata); score from SAR alone
CONF_BOTH = 2          # both sensors contributed — the only corroborated class
CONF_OPTICAL_ONLY = 3  # optical measured, SAR missing. Deliberately NOT scored:
                       # over snow the optical channel alone cannot separate a
                       # slide from snowfall, melt or an illumination change, so
                       # scoring it would manufacture single-sensor detections
                       # from the least trustworthy half of the pair.
CONF_LABELS = {CONF_NONE: "not measured", CONF_SAR_ONLY: "SAR only",
               CONF_BOTH: "both sensors", CONF_OPTICAL_ONLY: "optical only"}

# evidence sign per input kind: multiply the raw array by this so that
# "more landslide-like" is always MORE POSITIVE. See the module docstring.
EVIDENCE_SIGN = {
    "dbright": -1,    # dark debris on bright snow ⇒ brightness drops
    "dndsi": -1,      # debris is not snow ⇒ NDSI drops
    "logratio": -1,   # 10·log10(pre/post): rougher/brighter after ⇒ negative
    "tsint": +1,      # post-minus-pre z-score: brighter after ⇒ positive
    # The correlation family is already evidence-positive and has NO polarity at
    # all (sar_change.DEPOSIT_SIGN maps both to None): they measure how much the
    # scattering pattern was REARRANGED, not whether it got brighter or darker.
    # That makes them the natural SAR partner for a fusion, because the polarity
    # of an amplitude detector is the one thing measurement showed to be
    # unreliable — 53% of the Iliamna slide brightened and 47% darkened.
    "intcorr": +1,    # normalized correlation loss, 0→1
    "mtcorr": +1,     # multi-temporal "possibility", ~0.5 normal → 1 change
}

# Kinds whose evidence is the MAGNITUDE of change, with no reliable polarity.
#
# The textbook signature over snow is "rougher debris ⇒ backscatter increases",
# and that is what "logratio" assumes. Measured against the Iliamna 2026-08-08
# truth polygon it does not hold: inside the slide 53% of pixels brighten and 47%
# darken, so the signed term separates slide from background at AUC 0.556 — a
# coin flip — while |log-ratio| reaches 0.726. Fused with dNDSI at a threshold
# capturing half the slide, the signed term flagged 100% of the background and
# the magnitude term 3.9%.
#
# Physically this is unsurprising on a glacier: the deposit both roughens smooth
# firn (brighter) and mantles crevassed ice with fines (darker), and wet debris
# absorbs. Which one wins varies within a single slide, so polarity is not
# something to bet the detector on.
MAGNITUDE_KINDS = {"logratio_mag"}

# default absolute admission floors, in each layer's own units. The SAR value is
# sar_tab.SIG["logratio"] — the plugin's existing "±3 dB is the significance
# rule of thumb for the multilooked ratio" — reused so the fusion agrees with
# what the SAR tab already calls significant.
# The 1 dB magnitude floor is measured, not inherited: at the SAR tab's 3 dB
# significance value only 27% of the Iliamna slide clears, at 1 dB 64% does
# (against 29% of background).
# intcorr/mtcorr floors are sar_tab's own SIG values (0.3 and 0.8). On Iliamna a
# looser mtcorr floor of 0.6 measured better as a fusion channel (background at
# 50% recall 3.06% vs 3.90%), but that is one event against the plugin's
# documented significance level, so the documented value stands as the default.
DEFAULT_FLOORS = {"dbright": 0.05, "dndsi": 0.10, "logratio": 3.0,
                  "tsint": 3.0, "logratio_mag": 1.0,
                  "intcorr": 0.3, "mtcorr": 0.8}


def orient_evidence(arr, kind):
    """Raw change array → evidence-positive float32 (bigger = more slide-like).

    `kind` is one of EVIDENCE_SIGN's or MAGNITUDE_KINDS' keys. Raises on an
    unknown kind rather than silently guessing a sign, because a wrong sign here
    inverts the detector."""
    if kind in MAGNITUDE_KINDS:
        return np.abs(np.asarray(arr, dtype=np.float32)).astype(np.float32)
    try:
        sign = EVIDENCE_SIGN[kind]
    except KeyError:
        raise ValueError(
            f"unknown change kind {kind!r}; expected one of "
            f"{sorted(set(EVIDENCE_SIGN) | MAGNITUDE_KINDS)} — an unsigned "
            "correlation-family detector (intcorr/mtcorr) has no deposit "
            "polarity and cannot be oriented")
    return (np.asarray(arr, dtype=np.float32) * sign).astype(np.float32)


def detrend_median(arr, valid=None):
    """Subtract the AOI-wide finite median. Returns (detrended, offset).

    A basin-wide snowfall, melt onset or illumination shift moves the WHOLE AOI
    in one direction; a landslide moves a few hundred pixels. Removing the median
    cancels the former and leaves the latter essentially untouched.

    Caveat, and it is a real one: this assumes most of the AOI did not change. In
    a small AOI dominated by the slide, or over a glacier that changed wholesale
    between the two dates, the median IS the signal and subtracting it removes
    real evidence. The SAR tab carries the same warning for its own radiometric
    normalize (sar_tab.py), and both are exposed as a checkbox for that reason."""
    a = np.asarray(arr, dtype=np.float32)
    m = np.isfinite(a)
    if valid is not None:
        m &= np.asarray(valid, dtype=bool)
    if not m.any():
        return a.copy(), 0.0
    off = float(np.median(a[m]))
    return (a - off).astype(np.float32), off


def _axis_max(x, k, axis):
    """1-D running maximum of width k along `axis`, edge-padded, NaN-tolerant."""
    k = int(k)
    if k <= 1:
        return x
    pad = k // 2
    p = [(0, 0), (0, 0)]
    p[axis] = (pad, k - 1 - pad)
    xp = np.pad(x, p, mode="edge")
    n = x.shape[axis]
    out = None
    for i in range(k):
        sl = [slice(None), slice(None)]
        sl[axis] = slice(i, i + n)
        v = xp[tuple(sl)]
        out = v.copy() if out is None else np.fmax(out, v)
    return out


def max_filter_hw(a, k_row, k_col):
    """Rectangular focal maximum, k_row×k_col, edge-padded, NaN-tolerant.

    Separable: two 1-D running maxima, so cost is O(k) passes rather than O(k²),
    and no scipy.ndimage.grey_dilation — QGIS has no scipy. NaN never wins a
    maximum (np.fmax), so a void does not eat its neighbourhood; a window that is
    entirely NaN stays NaN."""
    a = np.asarray(a, dtype=np.float32)
    if int(k_row) <= 1 and int(k_col) <= 1:
        return a.copy()
    return _axis_max(_axis_max(a, k_row, 0), k_col, 1).astype(np.float32)


def max_filter(a, k):
    """Square k×k focal maximum. Thin wrapper over max_filter_hw."""
    return max_filter_hw(a, k, k)


def smoothstep(x, lo, hi):
    """Hermite 0→1 ramp, clamped outside [lo, hi]. A smooth ramp rather than a
    hard cutoff so terrain and glacier weighting fade instead of stamping visible
    step edges into the score (the 'blocky artifact' layover_dim._box_mean feathers
    against).

    A degenerate ramp (hi <= lo) collapses to a hard STEP at lo rather than to
    all-zeros: zeros would multiply the entire score raster by 0 and produce a
    blank map with no error at all, which is the worst possible failure mode for
    a detector. A step is what someone setting lo == hi actually meant."""
    x = np.asarray(x, dtype=np.float32)
    if not np.isfinite(lo) or not np.isfinite(hi):
        return np.ones_like(x, dtype=np.float32)
    if hi <= lo:
        return (x >= float(lo)).astype(np.float32)
    t = np.clip((x - float(lo)) / (float(hi) - float(lo)), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def robust_rank(values, kind, floor=None, valid=None, admit_values=None):
    """Evidence array → 0–1 percentile rank among pixels that clear an absolute
    floor. Returns (rank float32 with NaN where invalid, info dict).

    Pixels that are valid but below the floor score a hard 0.0 — "measured, no
    evidence" — which is different from NaN, "not measured". Only admitted pixels
    enter the ranking population, so a rank of 0.9 always means "in the top decile
    of pixels that already cleared a physically significant change", never "the
    least quiet pixel in a quiet scene".

    `values` must already be evidence-positive (see orient_evidence); `kind`
    selects the default floor from DEFAULT_FLOORS when `floor` is None.

    `admit_values` is the array the FLOOR is tested against, when that differs
    from the array being ranked. This matters: median-detrending is applied
    before ranking, and testing an "absolute" floor against a detrended array
    would quietly turn it into "deviates from the AOI median by more than the
    floor" — a pixel with literally zero physical change could clear a 3 dB bar.
    So the tab passes the RAW evidence here and the detrended evidence as
    `values`: a pixel must be both physically significant AND an outlier."""
    v = np.asarray(values, dtype=np.float32)
    a = v if admit_values is None else np.asarray(admit_values, dtype=np.float32)
    if floor is None:
        floor = DEFAULT_FLOORS.get(kind, 0.0)
    floor = float(floor)

    ok = np.isfinite(v) & np.isfinite(a)
    if valid is not None:
        ok &= np.asarray(valid, dtype=bool)

    rank = np.full(v.shape, np.nan, dtype=np.float32)
    rank[ok] = 0.0                                  # measured, below the floor
    admitted = ok & (a >= floor)
    n = int(admitted.sum())
    info = {"floor": floor, "n_admitted": n,
            "n_valid": int(ok.sum()), "kind": kind}
    if n == 0:
        info["max"] = float("nan")
        return rank, info

    vals = v[admitted]
    order = np.sort(vals)
    # searchsorted 'right' → the count of admitted values <= this one; divide by n
    # so no admitted pixel can score 0 (which would be indistinguishable from
    # "below the floor") and the strongest scores exactly 1. Every member of a tie
    # takes that tie's HIGHEST rank, so a large constant admitted region ranks
    # near 1.0 — correct for a uniform real deposit, and the reason n_admitted is
    # reported: with only a handful of admitted pixels a rank of 1.0 means
    # "strongest of 3", not "strong".
    rank[admitted] = (np.searchsorted(order, vals, side="right")
                      .astype(np.float32) / float(n))
    info["max"] = float(vals.max())
    info["p50"] = float(np.median(vals))
    return rank, info


def fill_voids(a, valid, max_k=65):
    """Replace invalid cells with a LOCAL mean so derivatives do not see a cliff.

    Filling a DEM void with a global median is worse than not filling it: over a
    ramp the global median can sit a thousand metres below the hole's
    neighbourhood, and the resulting fake escarpment produces a ~89 degree slope
    that the focal maximum then smears a full window across, inventing a steep
    "source" where there is only missing data. So holes are grown shut from their
    own surroundings, doubling the window until they close."""
    from . import sar_change
    a = np.asarray(a, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    out = np.where(valid, a, np.nan).astype(np.float32)
    k = 3
    with np.errstate(invalid="ignore", divide="ignore"):
        while not np.isfinite(out).all() and k <= max_k:
            m = sar_change._win_mean(out, np.isfinite(out), k, min_frac=0.0)
            hole = (~np.isfinite(out)) & np.isfinite(m)
            if hole.any():
                out = np.where(hole, m, out)
            k = k * 2 + 1
    if not np.isfinite(out).all():          # nothing valid anywhere nearby
        fill = float(np.median(a[valid])) if valid.any() else 0.0
        out = np.where(np.isfinite(out), out, fill)
    return out.astype(np.float32)


def combine_optical(rank_a, rank_b):
    """MEAN of two optical ranks (dNDSI and dBright) — deliberately not a product.

    They come from different bands — NDSI is a green/SWIR ratio, brightness a
    4-band mean — so their errors are only partly shared and averaging them
    suppresses artefacts that move only one.

    The mean is arithmetic because a GEOMETRIC mean is a veto: one channel at
    rank 0 zeroes the pair however strong the other is. That is not hypothetical.
    Measured background flagged at 50% recall, over three truthed events:

                      geometric        arithmetic
      Iliamna            6.32%            6.21%
      Hubbard            0.92%            0.92%
      Valdez            98.81%            8.08%     <-- the veto firing

    At Valdez the deposit landed on bare rock and moraine, so NDSI barely moved
    (AUC 0.491, a coin flip) while brightness saw it plainly (0.836). Under a
    product, dNDSI's zero erased dBright's 0.99 and the event vanished from the
    map. A channel with no signal must abstain, not overrule — and averaging is
    the cheapest form of abstention that still rewards agreement.

    A NaN on either side falls back to the other: the pair is an enhancement,
    never a second requirement to satisfy."""
    a = np.asarray(rank_a, dtype=np.float32)
    b = np.asarray(rank_b, dtype=np.float32)
    both = np.isfinite(a) & np.isfinite(b)
    out = np.where(np.isfinite(a), a, b).astype(np.float32)
    out[both] = 0.5 * (a[both] + b[both])
    return out


def combine_sar(rank_a, rank_b):
    """MAX of two SAR detector ranks — deliberately not a mean.

    The optical channels are averaged because dNDSI and dBright measure nearly
    the same physical thing, so averaging denoises them. The SAR detectors are
    NOT the same thing: log-ratio measures a change in backscattered POWER,
    int-corr measures loss of the scattering PATTERN. Each is blind to different
    events — int-corr is superb on Iliamna (2.76% background at 50% recall, 24.0%
    precision) and collapses at Valdez (8.09%); log-ratio is the reverse. Taking
    the max means "either detector firing counts as evidence", which recovers the
    event in both cases.

    Measured as the SAR channel, worst-case / mean background at 50% recall over
    three truthed events:

      log-ratio alone           4.57% / 3.02%
      mean(log-ratio, int-corr) 4.41% / 2.98%
      MAX(log-ratio, int-corr)  3.35% / 2.40%   <- and max won for every subset

    A NaN on either side falls back to the other."""
    a = np.asarray(rank_a, dtype=np.float32)
    b = np.asarray(rank_b, dtype=np.float32)
    both = np.isfinite(a) & np.isfinite(b)
    out = np.where(np.isfinite(a), a, b).astype(np.float32)
    out[both] = np.maximum(a[both], b[both])
    return out


def slope_weight(dem, gt, radius_m, lo_deg=10.0, hi_deg=30.0, smooth_k=3,
                 lat_hint=None):
    """Terrain weight 0–1 from the MAXIMUM slope within `radius_m` of each pixel.

    Returns (weight float32, slope_deg float32, meta dict). `lo_deg`/`hi_deg`
    bracket a smoothstep ramp: 0 where nothing within the radius is steeper than
    lo_deg, 1 once something reaches hi_deg.

    Using the focal maximum rather than the pixel's own slope is deliberate — it
    is what keeps a flat runout deposit below a steep headwall, which a per-pixel
    slope threshold deletes. See the module docstring.

    The DEM is box-smoothed first (layover_dim._box_mean, default 3×3) because
    bilinear-upsampled DEM tiles come out as flat facets whose derivatives
    terrace into blocky slope, exactly as the layover mask does it. Slope itself
    is layover_dim.slope_aspect_deg — np.gradient central differences, NOT a Horn
    3×3, so it will not match a QGIS 'Slope' layer pixel-for-pixel."""
    from . import layover_dim
    from . import sar_change

    dem = np.asarray(dem, dtype=np.float32)
    h, w = dem.shape
    dx_m, dy_m = layover_dim.metric_pixel_size(gt, h, lat_hint=lat_hint)

    # DEM voids are NOT hypothetical — GLO-30 has them over steep Alaska terrain
    # and open water. layover_dim._box_mean is an integral image (cumsum), so a
    # SINGLE NaN poisons every cell after it: measured here, one NaN turned 87%
    # of a 60x60 box mean into NaN and left the terrain gate inert (fuse() maps a
    # NaN weight back to 1.0) over most of the AOI, silently. So smooth with the
    # validity-aware window instead, and fill what remains before differentiating
    # so np.gradient cannot spread the holes any further.
    finite = np.isfinite(dem)
    n_void = int((~finite).sum())
    filled = fill_voids(dem, finite) if n_void else dem
    if smooth_k > 1:
        dem_s = sar_change._win_mean(filled, np.isfinite(filled), int(smooth_k))
        # _win_mean nulls any window with under half its samples, which at a
        # CORNER of a fully valid DEM is 4 of 9 — so a clean grid comes back with
        # NaN corners. Falling back to the unsmoothed value there is right;
        # filling them from a global statistic invents a cliff (measured: a
        # spurious 42-89 degree slope that the focal max spread 19 px inland,
        # weighting a flat corner 1.0).
        gap = (~np.isfinite(dem_s)) & np.isfinite(filled)
        if gap.any():
            dem_s = np.where(gap, filled, dem_s)
    else:
        dem_s = np.asarray(filled, dtype=np.float32)
    slope, _aspect = layover_dim.slope_aspect_deg(dem_s, dx_m, dy_m)

    # One window per axis, so the search radius is honoured in METRES both ways.
    # A single window sized off the coarser axis would under-reach on the finer
    # one; the plugin's SAR grids happen to be near-square in metres (the AOI box
    # is square in metres, so the DEGREE pixels are anisotropic to compensate),
    # but a Landsat UTM reference grid or a hand-made raster need not be.
    def _k(px_m):
        if radius_m <= 0 or px_m <= 0:
            return 1
        kk = int(round(2.0 * float(radius_m) / float(px_m))) | 1
        return max(1, min(kk, 2 * max(h, w) + 1))

    k_col, k_row = _k(dx_m), _k(dy_m)          # cols span x, rows span y
    smax = max_filter_hw(slope, k_row, k_col)
    weight = smoothstep(smax, lo_deg, hi_deg)
    # a pixel whose DEM was void gets no terrain opinion at all
    weight[~finite] = np.nan
    weight[~np.isfinite(smax)] = np.nan
    meta = {"window_px": max(k_row, k_col), "k_row": k_row, "k_col": k_col,
            "dx_m": dx_m, "dy_m": dy_m, "n_void": n_void,
            "void_pct": (100.0 * n_void / dem.size) if dem.size else 0.0,
            "radius_m": float(radius_m), "lo_deg": float(lo_deg),
            "hi_deg": float(hi_deg)}
    return weight.astype(np.float32), slope, meta


def lowland_weight(dem, min_elev_m=15.0, ramp_m=10.0):
    """0 below `min_elev_m`, ramping to 1 by `min_elev_m + ramp_m`.

    The source-zone rule — weight a pixel by the STEEPEST ground within reach —
    is what preserves a flat runout deposit below a headwall. At a coastline it
    does the opposite: an intertidal mudflat sitting beside a steep coastal hill
    inherits that hill's slope and sails through terrain weighting. Water and wet
    tidal mud swing C-band backscatter by many dB between passes, so those pixels
    are the loudest false positives in any coastal AOI — and in a SAR-only region
    there is no optical channel to veto them.

    Elevation separates them cleanly, because sea level is sea level. The cost is
    real and must be stated: a genuine runout that reached tidal level (a
    Barry-Arm-style slide into a fjord) is suppressed too, which is why this is a
    ramp rather than a cliff, and why the tab exposes it as a switch."""
    d = np.asarray(dem, dtype=np.float32)
    w = smoothstep(d, float(min_elev_m), float(min_elev_m) + max(1e-3, float(ramp_m)))
    w[~np.isfinite(d)] = 1.0          # no DEM -> no opinion, never a veto
    return w.astype(np.float32)


def glacier_weight(mask, factor=0.3, feather_k=3):
    """Multiplicative weight: `factor` inside the glacier mask, 1.0 outside.

    A downweight rather than a delete. Glacier ice generates the loudest false
    positives here — debris-covered tongues change appearance wholesale between
    any two dates through pond drainage, ice-cliff backwasting and moraine
    migration — but a hard mask also erases every rock avalanche that ran out
    onto ice, which in Alaska is a large fraction of the real events. Downweighting
    stops glaciers dominating the top of the score range while leaving an
    onto-glacier deposit visible and rankable.

    The mask edge is feathered so a retreating terminus (RGI outlines predate
    current termini by design) does not stamp a hard step into the score."""
    m = np.asarray(mask, dtype=np.float32)
    if feather_k and feather_k > 1:
        from . import layover_dim
        m = layover_dim._box_mean(m, int(feather_k)).astype(np.float32)
    m = np.clip(m, 0.0, 1.0)
    return (1.0 - m * (1.0 - float(factor))).astype(np.float32)


# A SAR-only pixel carries strictly weaker evidence than a corroborated one, so
# its score is scaled to sit below the corroborated range. Without this the
# fallback branch assigns the raw SAR rank, and a SAR-only pixel at rank 1.0
# scores 1.0 while a fully corroborated pixel at 0.9/0.9 scores 0.9 — measured:
# 56% of SAR-only pixels beat the MEDIAN corroborated pixel, so the top of the
# colour ramp fills with single-sensor noise (over water and tidal flats, where
# backscatter swings hardest between passes and there is no optical to veto it).
SAR_ONLY_WEIGHT = 0.5


def fuse(optical_rank, sar_rank, slope_w=None, glacier_w=None,
         mode="mean", allow_sar_only=True,
         sar_only_weight=SAR_ONLY_WEIGHT, smooth_k=5, optical_n=1):
    """Combine the two evidence ranks into a score raster plus a confidence band.

    Returns (bands dict keyed by BAND_NAMES, meta dict).

    `mode` is 'mean' (the default), 'geometric' (√(o·s)) or 'product' (o·s).

    MEAN IS THE DEFAULT BECAUSE THE AND LOSES. Measured over three truthed events,
    background flagged at 50% recall — the multiplicative modes do not merely
    underperform, they collapse:

                                        Iliamna  Hubbard   Valdez
      geometric AND (was the default)     6.94%   100.00%    8.56%
      mean of dNDSI, dBright and SAR      4.57%     0.97%    6.04%

    A product is a veto: any channel that is blind to a particular event zeroes
    the others, and across three events every channel was blind to at least one
    (dNDSI at Valdez, AUC 0.491; SAR at Hubbard, 0.368). Averaging lets a blind
    channel abstain while still rewarding agreement, and it never collapses.

    'geometric' and 'product' are kept for comparison and rank
    pixels IDENTICALLY — the product is the geometric mean squared, a monotone
    transform — so the choice only affects how values spread across a colour
    ramp; geometric keeps the useful part of the range off the floor.

    That equivalence is why the score is always BUILT on the geometric scale and
    only squared at the end for 'product' mode. Computing the two branches
    separately would break it: the SAR-only fallback assigns the raw rank s, and
    under an un-squared product a SAR-only pixel at s=0.5 would outrank a fully
    corroborated pixel at o=s=0.7 (0.49). Uncorroborated evidence must never
    outrank corroborated evidence because of a display setting.

    Both modes are a soft AND: either rank at 0 kills the score. That is the
    intent — a change only one sensor can see is, in this terrain, far more
    likely to be weather, illumination or speckle than a landslide.

    Where optical is NaN (cloud, shadow, nodata) but SAR is valid and
    `allow_sar_only`, the score falls back to the SAR rank SCALED BY
    `sar_only_weight` and the pixel is marked CONF_SAR_ONLY. The scaling is not
    cosmetic: unscaled, single-sensor evidence outranks corroborated evidence
    (see SAR_ONLY_WEIGHT) and the strongest-looking detections on the map end up
    being the ones no second sensor ever confirmed. The reverse case — optical valid, SAR missing — is
    marked CONF_OPTICAL_ONLY and deliberately left UNSCORED: over snow the
    optical channel on its own cannot tell a slide from snowfall or an
    illumination change."""
    o = np.asarray(optical_rank, dtype=np.float32)
    s = np.asarray(sar_rank, dtype=np.float32)
    if o.shape != s.shape:
        raise ValueError(f"grids differ: optical {o.shape} vs SAR {s.shape}")
    if mode not in ("mean", "geometric", "product"):
        raise ValueError(f"unknown fusion mode {mode!r}: "
                         "use 'mean', 'geometric' or 'product'")

    o_ok, s_ok = np.isfinite(o), np.isfinite(s)
    both = o_ok & s_ok
    sar_only = s_ok & ~o_ok
    optical_only = o_ok & ~s_ok

    # always build on the geometric scale, then apply one monotone transform
    g = np.full(o.shape, np.nan, dtype=np.float32)
    if mode == "mean":
        # `o` is already the MEAN of `optical_n` optical channels, so o*optical_n
        # is their sum and this is the true unweighted mean over every channel —
        # not mean(mean(optical), sar), which would give SAR half the vote on its
        # own.
        n = max(1, int(optical_n))
        g[both] = (o[both] * n + s[both]) / float(n + 1)
    else:
        g[both] = np.sqrt(np.clip(o[both] * s[both], 0.0, None))

    conf = np.full(o.shape, CONF_NONE, dtype=np.float32)
    conf[both] = CONF_BOTH
    conf[optical_only] = CONF_OPTICAL_ONLY
    if allow_sar_only and sar_only.any():
        g[sar_only] = s[sar_only] * float(sar_only_weight)
        conf[sar_only] = CONF_SAR_ONLY
    elif sar_only.any():
        conf[sar_only] = CONF_SAR_ONLY          # measured, just not scored
    conf[~(o_ok | s_ok)] = np.nan

    # Spatial smoothing before the weights. A slide is a CONNECTED patch; speckle,
    # residual cloud edges and single-pixel index noise are not, so averaging over
    # a small window raises the coherent signal relative to the incoherent
    # background. Measured on both validated events, it improves EVERY variant:
    #
    #   Iliamna  dNDSI×|lr|   3.87% -> 2.68% background at 50% recall (5x5)
    #   Hubbard  dNDSI×dBright 1.56% -> 0.92%                          (3x3)
    #
    # Applied on the geometric scale, before the 'product' squaring, so the two
    # modes stay monotone transforms of one another.
    if smooth_k and int(smooth_k) > 1:
        from . import sar_change
        # min_frac=0 so a window that is only partly inside the raster still
        # averages what it has. The 0.5 default would null the outer ring of the
        # grid, and on a small AOI that ring is a real fraction of the map.
        with np.errstate(invalid="ignore", divide="ignore"):
            sm = sar_change._win_mean(g, np.isfinite(g), int(smooth_k), min_frac=0.0)
        g = np.where(np.isfinite(sm), sm, g).astype(np.float32)

    score = (g * g).astype(np.float32) if mode == "product" else g

    sw = (np.ones_like(score) if slope_w is None
          else np.asarray(slope_w, dtype=np.float32))
    gw = (np.ones_like(score) if glacier_w is None
          else np.asarray(glacier_w, dtype=np.float32))
    # a NaN terrain weight (no DEM coverage) must not silently delete the score
    sw = np.where(np.isfinite(sw), sw, 1.0).astype(np.float32)
    gw = np.where(np.isfinite(gw), gw, 1.0).astype(np.float32)

    pre_glacier = (score * sw).astype(np.float32)
    final = (pre_glacier * gw).astype(np.float32)

    # The optical channel put through the SAME terrain and glacier weighting but
    # WITHOUT the AND, shipped on every run.
    #
    # Whether the SAR channel helps turns out to be event-specific and not
    # predictable from the rasters alone: on Iliamna 2026-08-08 the AND improved
    # separation (optical AUC 0.822 -> fused 0.838), on Hubbard 2026-07-28 it
    # destroyed it (0.892 -> 0.661, because the slide is QUIETER in SAR than the
    # crevassed ice around it). Two truth-free heuristics for predicting which
    # case you are in — SAR lift over the strongest optical pixels, and pixel
    # retention through the AND — were both tested against those two events and
    # both pointed the wrong way. So rather than guess, emit both answers and let
    # the analyst compare them against the imagery.
    # NB: distinct from the `optical_only` boolean mask above — that is "SAR is
    # missing here", this is "the score you would get from optical alone".
    oo = np.where(np.isfinite(o), o, np.nan).astype(np.float32)
    if smooth_k and int(smooth_k) > 1:                # same treatment as `score`
        from . import sar_change as _sc
        with np.errstate(invalid="ignore", divide="ignore"):
            _sm = _sc._win_mean(oo, np.isfinite(oo), int(smooth_k), min_frac=0.0)
        oo = np.where(np.isfinite(_sm), _sm, oo).astype(np.float32)
    optical_only_score = (oo * sw * gw).astype(np.float32)

    bands = {"optical_rank": o, "sar_rank": s, "slope_weight": sw,
             "glacier_weight": gw, "score_preglacier": pre_glacier,
             "score": final, "confidence": conf,
             "optical_only": optical_only_score}
    meta = {"mode": mode, "sar_only_weight": float(sar_only_weight),
            "smooth_k": int(smooth_k or 1), "optical_n": max(1, int(optical_n)),
            "n_both": int(both.sum()), "n_sar_only": int(sar_only.sum()),
            "n_optical_only": int(optical_only.sum()),
            "n_scored": int(np.isfinite(final).sum()),
            "max_score": (float(np.nanmax(final))
                          if np.isfinite(final).any() else float("nan"))}
    return bands, meta


def candidates(score, ys, xs, roots, dx_m, dy_m, min_px=1, limit=20):
    """Ranked scoring blobs of the fused score. Pure numpy; no GDAL, no scipy.

    `ys`/`xs`/`roots` come from sar_change.label_blobs on the mask of pixels at
    or above the display threshold — the same components the area sieve already
    works in, so what the table lists is exactly what survived on the map.

    Ranked by PEAK score, not area. The biggest blob is usually a broad terrain
    or illumination artefact; the slide is the one with the strongest core. Area
    is reported so a one-pixel spike is obvious, not used to sort.

    Returns at most `limit` dicts: area_km2, peak, mean, n_px, the peak pixel
    (peak_row/peak_col) to centre a zoom on, and the blob's pixel bounding box
    (row0/row1/col0/col1) to frame it."""
    out = []
    if ys.size == 0:
        return out
    sc = np.asarray(score, dtype=np.float32)
    vals = sc[ys, xs]
    px_km2 = (float(dx_m) * float(dy_m)) / 1e6
    order = np.argsort(roots, kind="mergesort")
    r_sorted = roots[order]
    # one pass over runs of equal root, so cost is the blob pixels, not the image
    starts = np.flatnonzero(np.r_[True, r_sorted[1:] != r_sorted[:-1]])
    ends = np.r_[starts[1:], r_sorted.size]
    for a, b in zip(starts.tolist(), ends.tolist()):
        sel = order[a:b]
        n = sel.size
        if n < min_px:
            continue
        v = vals[sel]
        k = int(np.argmax(v))
        yy, xx = ys[sel], xs[sel]
        out.append({"n_px": int(n), "area_km2": float(n * px_km2),
                    "peak": float(v[k]), "mean": float(v.mean()),
                    "peak_row": int(yy[k]), "peak_col": int(xx[k]),
                    "row0": int(yy.min()), "row1": int(yy.max()),
                    "col0": int(xx.min()), "col1": int(xx.max())})
    out.sort(key=lambda c: (-c["peak"], -c["area_km2"]))
    return out[:limit] if limit else out


def summarize(bands, meta, floors_info=None):
    """Human-readable lines for the tab's log pane. Says plainly when nothing
    cleared the floors, rather than letting a noise map imply a detection."""
    lines = []
    for info in (floors_info or []):
        if info.get("n_admitted", 0) == 0:
            lines.append(
                f"  {info['kind']}: NOTHING cleared the {info['floor']:g} floor "
                f"({info['n_valid']} valid px) — no evidence from this sensor")
        else:
            lines.append(
                f"  {info['kind']}: {info['n_admitted']} of {info['n_valid']} px "
                f"cleared the {info['floor']:g} floor "
                f"(max {info.get('max', float('nan')):.3g})")
    lines.append(f"  corroborated (both sensors): {meta['n_both']} px; "
                 f"SAR-only: {meta['n_sar_only']} px; "
                 f"optical-only (not scored): {meta.get('n_optical_only', 0)} px")
    if not meta["n_scored"] or not np.isfinite(meta["max_score"]):
        lines.append("  no pixels scored — the AOI shows no fused evidence")
    else:
        lines.append(f"  peak fused score {meta['max_score']:.3f}")
    return lines
