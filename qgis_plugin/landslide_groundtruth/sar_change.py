"""Incoherent SAR change detection — the amplitude half of Jung & Yun (2020).

Implements the *incoherent* landslide detectors from "Evaluation of Coherent
and Incoherent Landslide Detection Methods Based on Synthetic Aperture Radar
for Rapid Response" (Remote Sens. 2020, 12, 265; doi:10.3390/rs12020265),
adapted from their ALOS-2 SLC stack to this plugin's Sentinel-1 RTC AOI
GeoTIFFs:

  log-ratio (their Eq. 6)         10·log10(<pre>/<post>) of k×k-multilooked
                                  LINEAR gamma-naught. 2 scenes. Positive =
                                  darker after the event; negative = brighter
                                  (fresh debris is usually rougher → brighter).
  intensity correlation (Eq. 14)  windowed Pearson correlation between two
                                  scenes — sensitive to changes in image
                                  TEXTURE rather than absolute brightness.
  normalized difference (Eq. 16)  (ρ_ref − ρ_co)/(ρ_ref + ρ_co) with a
                                  pre-event reference pair vs. a co-event
                                  pair. 3 scenes. High where the pre-event
                                  texture correlation vanished at the event —
                                  the paper's best quick-product detector
                                  (AUC 0.74–0.84 across their test cases).
  multi-temporal possibility      per-pixel position of each co-event
  (their §3.2.2 + Fig. 4)         correlation on the distribution of ALL
                                  pre-event pair correlations, averaged over
                                  the co-event group. N pre + 1 post scenes.
                                  The paper's most reliable detector overall
                                  (AUC 0.77–0.93): the reference distribution
                                  IS that pixel's normal seasonal/moisture
                                  variability, so recurring natural change
                                  stops looking like an anomaly.
  intensity z-score               post-event multilooked brightness vs the
  (their §3.2.1 in spirit)        pre-stack's own per-pixel mean/σ, in dB.
                                  N pre + 1 post scenes. The BRIGHTNESS
                                  complement of the correlation detectors:
                                  correlation subtracts window means, so a
                                  debris sheet that uniformly brightens a
                                  smooth dark slope (snow/ice — no pre-event
                                  texture to lose) is invisible to it but
                                  obvious here.

The coherent (interferometric-phase) half of the paper is intentionally out
of scope: RTC carries no phase, and the paper's own result is that coherence
methods fail over low-coherence natural terrain anyway.

Co-registration comes free here: every input is the SAME bbox/width/height
data-API render of DEM-aligned RTC scenes from the same relative orbit, so
the arrays line up pixel-for-pixel without any resampling in this module.

Pure numpy + GDAL (both ship inside QGIS; no scipy). All window statistics
run on integral images, so cost is independent of the window size. GDAL is
imported lazily inside the I/O helpers only, so the pure-numpy detectors and
tail-split helpers import (and unit-test) without osgeo present.
"""
import numpy as np

NODATA = -9999.0


# ---------- GeoTIFF I/O ----------
def read_band(path):
    """(arr float32, valid bool, geotransform, projection) of band 1.

    Valid excludes NaN/±inf, the band's nodata value, the render's mask band,
    and non-positive pixels — gamma-naught is positive over real ground, and
    0 is what masked/empty areas of a data-API render come back as."""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise IOError(f"GDAL could not open {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    if arr is None:
        raise IOError(f"no raster data in {path}")
    arr = arr.astype(np.float32)
    valid = np.isfinite(arr) & (arr > 0)
    nd = band.GetNoDataValue()
    if nd is not None:
        valid &= arr != nd
    mask = band.GetMaskBand()
    if mask is not None:
        m = mask.ReadAsArray()
        if m is not None:
            valid &= m > 0
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    ds = None
    return arr, valid, gt, proj


def write_gtiff(path, arr, gt, proj):
    """Write float32 `arr` (NaN → NODATA) as a single-band deflate GeoTIFF."""
    from osgeo import gdal
    h, w = arr.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    band = ds.GetRasterBand(1)
    band.WriteArray(np.where(np.isfinite(arr), arr, NODATA).astype(np.float32))
    band.SetNoDataValue(NODATA)
    band.FlushCache()
    ds = None


def read_raster(path, band=1):
    """(arr float32 with NODATA→NaN, geotransform, projection) of `band`. Unlike
    read_band this applies NO γ⁰>0 validity test, so it is safe for DEMs and for
    already-computed change rasters that can legitimately be zero or negative."""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise IOError(f"GDAL could not open {path}")
    b = ds.GetRasterBand(band)
    arr = b.ReadAsArray().astype(np.float32)
    nd = b.GetNoDataValue()
    if nd is not None:
        arr = np.where(arr == nd, np.nan, arr)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    ds = None
    return arr, gt, proj


def write_gtiff_2band(path, value, alpha_u8, gt, proj):
    """Band 1 = float value (NaN→NODATA), band 2 = 0..255 alpha. A single-band
    renderer keeps its styling on band 1 while QGIS modulates opacity by band 2
    (renderer.setAlphaBand(2)) — used to fade radar-layover pixels."""
    from osgeo import gdal
    h, w = value.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 2, gdal.GDT_Float32,
                                              options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    b1 = ds.GetRasterBand(1)
    b1.WriteArray(np.where(np.isfinite(value), value, NODATA).astype(np.float32))
    b1.SetNoDataValue(NODATA)
    ds.GetRasterBand(2).WriteArray(np.asarray(alpha_u8).astype(np.float32))
    ds.FlushCache()
    ds = None


def write_gray_rgba(path, gray_u8, alpha_u8, gt, proj):
    """4-band Byte RGBA (R=G=B=gray, A=alpha) — QGIS auto-renders it with per-pixel
    opacity, revealing the basemap beneath dimmed pixels. For the grayscale
    amplitude preview, whose auto grayscale styling can't take an external alpha."""
    from osgeo import gdal
    g = np.asarray(gray_u8).astype(np.uint8)
    a = np.asarray(alpha_u8).astype(np.uint8)
    h, w = g.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 4, gdal.GDT_Byte,
                                              options=["COMPRESS=DEFLATE", "ALPHA=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    for i in (1, 2, 3):
        ds.GetRasterBand(i).WriteArray(g)
    ab = ds.GetRasterBand(4)
    ab.WriteArray(a)
    ab.SetColorInterpretation(gdal.GCI_AlphaBand)
    ds.FlushCache()
    ds = None


def colorize(value, lo, hi, stops):
    """Map a float array to RGBA (float32, shape H×W×4, 0..255) by linear
    interpolation over `stops` = [(value, '#rrggbb', alpha0_255), …] ascending.
    NaN → fully transparent. Bakes a pseudocolor ramp into pixels so the result
    can carry a per-pixel alpha (the layover fade) that QGIS renders reliably —
    which a single-band renderer's alpha band does not."""
    v = np.asarray(value, dtype=np.float32)
    xs = np.array([s[0] for s in stops], dtype=np.float64)
    cols = np.array([[int(s[1][1:3], 16), int(s[1][3:5], 16),
                      int(s[1][5:7], 16), s[2]] for s in stops], dtype=np.float64)
    vc = np.clip(v, lo, hi).astype(np.float64)
    out = np.empty(v.shape + (4,), dtype=np.float32)
    for ch in range(4):
        out[..., ch] = np.interp(vc, xs, cols[:, ch])
    out[~np.isfinite(v)] = 0.0
    return out


def write_rgba(path, rgba_u8, gt, proj):
    """H×W×4 uint8 RGBA → GeoTIFF that QGIS auto-renders with per-pixel opacity."""
    from osgeo import gdal
    a = np.asarray(rgba_u8)
    h, w = a.shape[:2]
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 4, gdal.GDT_Byte,
                                              options=["COMPRESS=DEFLATE", "ALPHA=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    for i in range(4):
        ds.GetRasterBand(i + 1).WriteArray(a[..., i].astype(np.uint8))
    ds.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    ds.FlushCache()
    ds = None


# ---------- window statistics (integral images) ----------
def _win_sum(a, k):
    """k×k moving-window sum, zero-padded at the edges, via an integral image."""
    pad = (k - 1) // 2
    q = np.pad(np.asarray(a, dtype=np.float64),
               ((pad, k - 1 - pad), (pad, k - 1 - pad)))
    ii = np.zeros((q.shape[0] + 1, q.shape[1] + 1))
    ii[1:, 1:] = q.cumsum(0).cumsum(1)
    return ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]


def _win_mean(a, valid, k, min_frac=0.5):
    """Valid-only k×k mean; NaN where under min_frac of the window is valid."""
    n = _win_sum(valid, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = _win_sum(np.where(valid, a, 0.0), k) / n
    m[n < k * k * min_frac] = np.nan
    return m


# ---------- speckle filtering (input pre-processing) ----------
# Speckle is SAR's inherent multiplicative salt-and-pepper. Filtering EACH scene
# before the ratio/correlation detectors is the standard incoherent-CD chain and
# the single biggest noise lever — it matters most for the correlation detectors,
# where speckle artificially decorrelates windows and inflates false "change".
# ENL ≈ 4.4 is the nominal equivalent-number-of-looks of Sentinel-1 IW GRD (and
# thus of the RTC product derived from it); it sets the speckle model's strength.
S1_ENL = 4.4


def lee_filter(arr, valid, k, enl=S1_ENL):
    """Classic Lee (1980) adaptive speckle filter, valid-aware.

    Per pixel: out = mean + W·(pixel − mean), with the local k×k mean and the
    Lee weight W = 1 − Cu²/Ci² clamped to [0, 1], where Cu² = 1/ENL is the
    speckle coefficient-of-variation² and Ci² = var/mean² is the local one. In a
    homogeneous window (Ci² ≈ Cu²) W→0 and the pixel is replaced by the window
    mean — full speckle smoothing; over an edge or bright target (Ci² ≫ Cu²)
    W→1 and the pixel is kept — so edges and slide scars survive. This is the
    edge-preserving win over a plain box blur. Pixels whose window is data-
    starved (mean is NaN) or invalid keep their original value; the caller's
    validity mask carries through unchanged."""
    a = arr.astype(np.float64)
    mean = _win_mean(a, valid, k)
    mean_sq = _win_mean(a * a, valid, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = np.maximum(mean_sq - mean * mean, 0.0)
        ci2 = var / (mean * mean)
        w = np.clip(1.0 - (1.0 / float(enl)) / ci2, 0.0, 1.0)
        out = mean + w * (a - mean)
    out = np.where(np.isfinite(out) & valid, out, arr)
    return out.astype(np.float32)


def median_filter(arr, valid, k):
    """Valid-aware k×k median filter — a non-adaptive speckle option.

    Kills isolated bright/dark speckle pixels while preserving edges (a mean
    blur smears them). Invalid neighbours are excluded from each window's
    median; pixels with no valid neighbourhood keep their original value."""
    a = np.where(valid, arr, np.nan).astype(np.float64)
    pad = k // 2
    padded = np.pad(a, pad, mode="edge")
    stack = np.stack([padded[dy:dy + a.shape[0], dx:dx + a.shape[1]]
                      for dy in range(k) for dx in range(k)])
    with np.errstate(invalid="ignore"):
        med = np.nanmedian(stack, axis=0)
    out = np.where(valid & np.isfinite(med), med, arr)
    return out.astype(np.float32)


# ---------- blob sieve (output post-processing) ----------
def sieve_small_blobs(values, mask, min_area, fill=0.0, connectivity=8):
    """Zero out anomalous pixels sitting in change blobs smaller than min_area.

    A real slide is a connected patch of pixels; scattered single anomalous
    pixels are residual speckle. `mask` is the boolean 'anomalous' set (the
    caller thresholds each detector at its own significance level); connected
    components of it smaller than `min_area` pixels are set back to `fill`
    (0 = the no-change value every ramp renders transparent, so the coverage
    metric is preserved — unlike NaN, which would read as missing data).

    Pure numpy + a small union-find (QGIS has no scipy). Only mask pixels are
    unioned, so cost scales with the anomalous-pixel count, not the image."""
    out = np.asarray(values).copy()
    m = np.asarray(mask) & np.isfinite(out)
    if min_area <= 1 or not m.any():
        return out
    h, w = m.shape
    ys, xs = np.nonzero(m)
    n = ys.size
    idx = -np.ones((h, w), dtype=np.int64)
    idx[ys, xs] = np.arange(n)
    parent = np.arange(n, dtype=np.int64)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]   # path compression
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    offs = [(0, 1), (1, 0)] + ([(1, 1), (1, -1)] if connectivity == 8 else [])
    for dy, dx in offs:
        ny, nx = ys + dy, xs + dx
        ok = (ny >= 0) & (ny < h) & (nx >= 0) & (nx < w)
        nb = np.full(n, -1, dtype=np.int64)
        nb[ok] = idx[ny[ok], nx[ok]]
        for a, b in zip(np.nonzero(nb >= 0)[0].tolist(), nb[nb >= 0].tolist()):
            union(a, b)
    roots = np.array([find(i) for i in range(n)], dtype=np.int64)
    counts = np.bincount(roots, minlength=n)
    small = counts[roots] < min_area
    out[ys[small], xs[small]] = fill
    return out


# ---------- detectors ----------
def log_ratio(pre, post, valid, k):
    """Eq. 6: 10·log10(pre/post) after k×k multilooking in LINEAR power.

    Averaging before the log is the physically right order (multilooking
    averages power, not dB) and is what tames speckle: single-look ratios
    swing several dB over unchanged ground. ±3 dB is a common significance
    rule of thumb for the multilooked ratio."""
    mp = _win_mean(pre, valid, k)
    mq = _win_mean(post, valid, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = 10.0 * np.log10(mp / mq)
    r[~np.isfinite(r)] = np.nan
    return r.astype(np.float32)


def intensity_correlation(a, b, valid, k):
    """Eq. 14: windowed Pearson correlation of two intensity images.

    Measures whether the local image TEXTURE stayed the same, independent of
    absolute brightness — which is what makes it robust where backscatter
    level drifts (soil moisture, snow state) and is the paper's key finding.
    NaN where the window is data-starved or has no variance (a perfectly
    flat window has undefined correlation)."""
    n = _win_sum(valid, k)
    av = np.where(valid, a, 0.0).astype(np.float64)
    bv = np.where(valid, b, 0.0).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        ma, mb = _win_sum(av, k) / n, _win_sum(bv, k) / n
        cov = _win_sum(av * bv, k) / n - ma * mb
        va = _win_sum(av * av, k) / n - ma * ma
        vb = _win_sum(bv * bv, k) / n - mb * mb
        rho = cov / np.sqrt(va * vb)
    bad = (n < k * k * 0.5) | ~np.isfinite(rho)
    rho = np.clip(rho, -1.0, 1.0)
    rho[bad] = np.nan
    return rho.astype(np.float32)


def corr_norm_diff(rho_ref, rho_co):
    """Eq. 16: (ρ_ref − ρ_co)/(ρ_ref + ρ_co) — normalized loss of correlation.

    ρ is floored at a small positive value first: negative correlation means
    'no correlation' for change purposes, and letting it through would blow
    up the denominator. Output ≈0 = texture as stable across the event as it
    was before; → 1 = the pre-event correlation vanished at the event."""
    ref = np.clip(rho_ref, 1e-3, 1.0)
    co = np.clip(rho_co, 1e-3, 1.0)
    out = (ref - co) / (ref + co)
    out[~(np.isfinite(rho_ref) & np.isfinite(rho_co))] = np.nan
    return out.astype(np.float32)


def multi_temporal_possibility(refs, cos, h_floor=0.03):
    """§3.2.2: per-pixel possibility that the co-event correlations are
    anomalously LOW against the pre-event reference distribution.

    refs — reference-group correlation maps (every pre×pre pair, from
           intensity_correlation); cos — co-event maps (each pre×post).

    Per pixel and per co-event value x: possibility = P(reference ≥ x), the
    survival function of the reference samples, smoothed with a Gaussian
    kernel (bandwidth by Silverman's rule, floored at h_floor — with a
    handful of samples the smoothing is what keeps the output from
    quantizing to steps of 1/M). This is the paper's KDE→CDF lookup
    (Fig. 4b) in closed form; the normal CDF is evaluated through the
    logistic approximation Φ(z) ≈ σ(1.702·z) (max error < 0.01 — far below
    the sampling noise of ≤15 reference values) because QGIS's numpy has no
    erf. Possibilities are averaged over the co-event group, which is the
    paper's mitigation for natural decorrelation inside single co-event
    pairs.

    Output: ~0.5 where the co-event correlation is mid-distribution (normal),
    → 1 where it sits below everything the reference group ever did (change),
    → 0 where it is anomalously high. NaN where fewer than 3 reference
    samples are valid or no co-event value is."""
    ref = np.stack([np.asarray(r, dtype=np.float32) for r in refs])
    rvalid = np.isfinite(ref)
    n = rvalid.sum(0).astype(np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        mu = np.where(rvalid, ref, 0.0).sum(0) / n
        var = np.where(rvalid, (ref - mu) ** 2, 0.0).sum(0) / n
        h = 1.06 * np.sqrt(var) * n ** -0.2          # Silverman's rule
    h = np.where(np.isfinite(h), np.maximum(h, h_floor), h_floor)
    acc = np.zeros(h.shape, np.float32)
    cnt = np.zeros(h.shape, np.float32)
    for co in cos:
        cvalid = np.isfinite(co)
        cz = np.where(cvalid, co, 0.0)
        s = np.zeros(h.shape, np.float32)
        for i in range(ref.shape[0]):
            z = np.clip((np.where(rvalid[i], ref[i], 0.0) - cz) / h, -40.0, 40.0)
            s += np.where(rvalid[i], 1.0 / (1.0 + np.exp(-1.702 * z)), 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            p = s / n
        acc += np.where(cvalid, p, 0.0)
        cnt += cvalid
    with np.errstate(invalid="ignore", divide="ignore"):
        out = acc / cnt
    out[(n < 3) | (cnt == 0)] = np.nan
    return out.astype(np.float32)


def intensity_zscore(pres, post, valids, vpost, k, sigma_floor_db=0.75):
    """Time-series intensity anomaly (the paper's §3.2.1 in spirit): how far
    the post-event multilooked backscatter sits outside the pre-event stack's
    own per-pixel brightness distribution, in σ units, dB domain.

    This is the BRIGHTNESS complement of the correlation detectors, which
    subtract window means and therefore cannot see a spatially uniform
    brightness change at all — and over smooth dark surfaces (snow, ice,
    radar shadow) have no pre-event texture to lose in the first place.
    A fresh debris sheet brightening such a slope by several dB is invisible
    to correlation but scores a large positive z here.

    Positive z = brighter after the event (rougher/wetter debris), negative
    = darker. |z| ≥ 2 is suggestive, ≥ 3 strong. σ is floored (default
    0.75 dB) because a handful of pre-event scenes underestimates natural
    variability — without the floor, a pixel whose pre-stack happened to be
    eerily stable turns any noise into a huge z. NaN where fewer than two
    pre-event samples (no spread to compare against) or the post pixel is
    invalid."""
    mls = []
    for a, v in zip(pres, valids):
        m = _win_mean(a, v, k)             # multilook in LINEAR power…
        with np.errstate(invalid="ignore", divide="ignore"):
            mls.append(10.0 * np.log10(m))  # …then compare in dB
    stack = np.stack(mls)
    good = np.isfinite(stack)
    n = good.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mu = np.where(good, stack, 0.0).sum(0) / n
        var = np.where(good, (stack - mu) ** 2, 0.0).sum(0) / n
    sd = np.maximum(np.sqrt(var), sigma_floor_db)
    mp = _win_mean(post, vpost, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (10.0 * np.log10(mp) - mu) / sd
    z[(n < 2) | ~np.isfinite(z)] = np.nan
    return z.astype(np.float32)


# ---------- deposit / scar tail split (report rec #2: "sign the change map") ----------
# Deposit polarity of each SIGNED brightness detector: the sign of the change
# VALUE where a fresh, rougher deposit (a backscatter INCREASE) shows up. The
# correlation-family detectors are one-sided change-MAGNITUDE measures (→ high =
# change, either direction) with no deposit/scar polarity, so they map to None
# and must NOT be split into tails. Keys match sar_tab's CD_PRODUCTS.
DEPOSIT_SIGN = {
    "logratio": -1,   # 10·log10(pre/post): brighter-after ⇒ post>pre ⇒ NEGATIVE
    "tsint":    +1,   # post brighter than the pre-stack ⇒ POSITIVE z-score
    "intcorr":  None,  # normalized correlation loss: 0→1 magnitude, no direction
    "mtcorr":   None,  # multi-temporal possibility: ~0.5 normal → 1 change
}


def deposit_sign(kind):
    """+1 / −1 for a signed brightness detector (the value-sign of a fresh
    deposit), or None for an unsigned change-magnitude detector."""
    return DEPOSIT_SIGN.get(kind)


def deposit_oriented(arr, kind):
    """Re-orient a change map so DEPOSIT (backscatter increase) reads POSITIVE and
    SCAR (decrease) reads NEGATIVE, whatever the detector's native sign. Returns a
    float32 array, or None for an unsigned (correlation-family) detector."""
    s = DEPOSIT_SIGN.get(kind)
    if s is None:
        return None
    return (np.asarray(arr, dtype=np.float32) * s).astype(np.float32)


def split_tails(arr, kind, threshold, fill=np.nan):
    """Split a SIGNED change map into its deposit and scar tails (report rec #2).

    ``threshold`` ≥ 0 is a magnitude cut in the detector's own units — dB for
    log-ratio, σ for the z-score. Returns ``(deposit, scar, meta)``:

      deposit  the deposit-oriented value where it is ≥ +threshold: the fresh
               backscatter-INCREASE signal (rough debris on a smoother substrate —
               the primary co-event deposit indicator over snow/ice/bedrock);
               ``fill`` elsewhere.
      scar     the POSITIVE magnitude where the oriented value is ≤ −threshold: a
               backscatter DECREASE (scar / smoothing / de-vegetation); ``fill``
               elsewhere.
      meta     {'signed', 'deposit_sign', 'threshold', 'n_deposit', 'n_scar'}.

    Caveat carried from the Alaska report: the deposit=increase rule holds over
    SMOOTH substrate (snow, ice, stripped bedrock); over talus/moraine/vegetated
    runout the sign can flip, so treat both tails as candidates there.

    For an unsigned detector (correlation family) a deposit/scar split is not
    physically meaningful: ``deposit`` and ``scar`` come back all-``fill`` and
    ``meta['signed']`` is False — keep the single one-sided magnitude map instead.
    """
    a = np.asarray(arr, dtype=np.float32)
    thr = abs(float(threshold))
    oriented = deposit_oriented(a, kind)
    if oriented is None:
        empty = np.full(a.shape, fill, dtype=np.float32)
        return empty, empty.copy(), {"signed": False, "deposit_sign": None,
                                     "threshold": thr, "n_deposit": 0, "n_scar": 0}
    finite = np.isfinite(oriented)
    dep_mask = finite & (oriented >= thr)
    scar_mask = finite & (oriented <= -thr)
    deposit = np.where(dep_mask, oriented, fill).astype(np.float32)
    scar = np.where(scar_mask, -oriented, fill).astype(np.float32)
    return deposit, scar, {"signed": True, "deposit_sign": DEPOSIT_SIGN[kind],
                           "threshold": thr, "n_deposit": int(dep_mask.sum()),
                           "n_scar": int(scar_mask.sum())}


# ---------- dual-geometry merge (report rec #5) ----------
def merge_geometries(maps, kind, threshold, agree_min=2):
    """Combine per-geometry change maps (ascending + descending) into one, so a
    scar pixel lost to layover in one viewing geometry is recovered from the other.

    ``maps`` — one change array per geometry ACTUALLY available (same shape, same
    detector ``kind``, each in the detector's NATIVE units with NaN where that
    geometry has no valid / layover-free data). That count is often **1** in
    Alaska's steep terrain, where a given AOI/track has only ascending OR only
    descending coverage — this function degrades to that case honestly rather
    than faking a merge (see ``meta['single_geometry']`` / ``meta['note']``).

    ``threshold`` — significance magnitude: dB or σ for the signed detectors, the
    ``>`` cut for the unsigned correlation family.

    Cross-geometry backscatter is NOT directly comparable, so values are never
    averaged across geometries. Per pixel the geometry with the STRONGEST anomaly
    wins (its native value, sign preserved), and a companion CONFIDENCE map records
    how many geometries saw the pixel and whether they agree:

        NaN  no geometry has valid data here
        0    valid, but not anomalous in any geometry (background)
        1    anomalous in one orbit while every other orbit was BLIND (NaN) here —
             a true layover recovery (the whole point of the merge)
        2    anomalous in ≥agree_min geometries, CONSISTENT sign (high confidence)
        3    orbits DISAGREE — either anomalous with conflicting sign, or one orbit
             flagged it while another orbit had valid data and saw no change;
             suspect / possible geometry artifact, inspect before trusting

    Returns ``(merged, confidence, meta)`` — merged in native units (so the tab's
    existing styling applies unchanged), plus a confidence raster and a meta dict.
    """
    arrs = [np.asarray(m, dtype=np.float32) for m in maps]
    if not arrs:
        raise ValueError("merge_geometries needs at least one map")
    shape = arrs[0].shape
    for a in arrs:
        if a.shape != shape:
            raise ValueError("geometry maps differ in shape")
    thr = abs(float(threshold))
    signed = DEPOSIT_SIGN.get(kind) is not None

    agree_min = max(2, int(agree_min))
    stack = np.stack(arrs)                       # (G, H, W)
    valid = np.isfinite(stack)
    if signed:
        strength = np.where(valid, np.abs(stack), -np.inf)
        anom = valid & (np.abs(stack) >= thr)
    else:
        strength = np.where(valid, stack, -np.inf)
        anom = valid & (stack >= thr)

    valid_count = valid.sum(0)
    any_valid = valid_count > 0
    n_geom = int(sum(int(v.any()) for v in valid))      # geometries with ANY data

    # merged value: the geometry with the strongest anomaly at each pixel wins;
    # sign is preserved so deposit/scar polarity survives the merge
    idx = np.argmax(strength, axis=0)
    merged = np.take_along_axis(stack, idx[None], axis=0)[0].astype(np.float32)
    merged[~any_valid] = np.nan

    ncount = anom.sum(0)                                 # geometries flagging anomaly
    multi = ncount >= agree_min
    # sub-quorum: anomalous in fewer than agree_min geometries (normally exactly 1
    # for an asc+desc pair). Split by whether the NON-anomalous geometries were
    # blind (NaN → true layover recovery) or valid-but-saw-no-change (disagreement,
    # a lower-confidence / possible-artifact pixel — NOT a recovery).
    sub = any_valid & (ncount >= 1) & ~multi
    others_valid = valid_count > ncount                  # a valid orbit saw no anomaly
    conf = np.zeros(shape, dtype=np.float32)
    conf[~any_valid] = np.nan
    conf[sub & ~others_valid] = 1.0                      # recovered (others blind)
    conf[sub & others_valid] = 3.0                       # single-orbit, contradicted
    if signed:
        # agreement = every anomalous orbit shares sign; conflict = a deposit and a
        # scar are claimed at one pixel by different orbits
        has_pos = (anom & (stack > 0)).any(0)
        has_neg = (anom & (stack < 0)).any(0)
        sign_conflict = multi & has_pos & has_neg
        conf[multi & ~sign_conflict] = 2.0
        conf[sign_conflict] = 3.0
    else:
        conf[multi] = 2.0
    n_conflict = int((conf == 3).sum())

    if n_geom < 2:
        note = ("single-geometry only — layover on the opposite-facing slopes "
                "(possibly the source headscarp) is UNRECOVERED; add the other "
                "orbit direction if this terrain has coverage")
    else:
        note = (f"merged {n_geom} geometries — conf 2 (both orbits agree) is "
                f"highest confidence, conf 1 is recovered from a single orbit "
                f"where the other was blind to layover, conf 3 pixels disagree "
                f"(opposite sign, or one orbit saw no change) and are suspect")
    meta = dict(
        n_geometries=n_geom,
        single_geometry=(n_geom < 2),
        n_single=int((conf == 1).sum()),
        n_agree=int((conf == 2).sum()),
        n_conflict=n_conflict,
        note=note,
    )
    return merged, conf.astype(np.float32), meta
