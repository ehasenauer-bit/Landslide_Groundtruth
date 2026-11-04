"""HDR true-colour render for the dock's Sentinel-2 preview, in plain numpy.

The same curve as review_package._highlight_hdr (the PlanetScope tab's "HDR" tone
mode): a highlight rolloff shoulder with a tapered log shadow lift, anchored at the
scene's own bright tail, so a frame holding both deep shadow and sunlit snow keeps
detail at BOTH ends and no valid pixel comes out pure black or pure white. The plugin
can't import the project (review_package needs xarray/rioxarray/geopandas, which the
QGIS interpreter doesn't ship), so this is a numpy port — constants copied from
review_package and kept in step by tests/test_hdr_render.py, which checks the two
agree pixel for pixel whenever xarray is importable.

It replaces the data API's cube-root "HONC" colour_formula for Sentinel-2 because a
global power law can't hold both ends: measured on a same-day Denali pair it kept 30%
less snow texture than this curve, and it rendered MPC's un-removed +1000 BOA offset
as a 0.10-reflectance haze over every shadow. The data API is still what fetches the
bands; it just returns raw DN now and the tone curve runs here.

Arrays are (3, H, W) float reflectance in red, green, blue order, NaN = nodata.
"""
import numpy as np

# --- constants: copies of review_package's (see test_hdr_render.py) -------------
KNEE = 0.55
CONTRAST = 1.15            # the PlanetScope tab's default S-curve (its Contrast box)
DESAT = 1.0
DESAT_ONSET = 0.5
BLUE_DESAT = 0.6
BLUE_DESAT_K = 0.18
BLUE_DESAT_LO = 0.08
BLUE_DESAT_HI = 0.22
CYAN_STRIP = 1.0
PINK_STRIP = 1.0
CAST_STRIP_K = 30.0
PINK_GATE_LO = 175.0
PINK_GATE_HI = 210.0
HDR_LIFT = 60.0
HDR_LIFT_TAPER = 2.0
HDR_WHITE_PCT = 99.5
HDR_WHITE_MIN = 0.30
HDR_WHITE_MAX = 1.30
HDR_WHITE_FALLBACK = 0.90
HDR_MIN_PIXELS = 5000
HDR_SATURATION = 0.7
AUTO_SAMPLE = 500000

# Sentinel-2 L2A processing baseline 04.00 (rolled out 2022-01-25) stores
# DN = 10000 * reflectance + 1000; MPC serves it un-harmonized. Older scenes have no
# offset. Same rule as imagery._s2_boa_offset.
S2_OFFSET_DATE = "2022-01-25"


def s2_boa_offset(candidate):
    """DN to ADD before dividing by 10000: -1000 for baseline >= 04.00, else 0.

    Uses the search record's `boa_offset` when the run wrote one (it reads the
    scene's own s2:processing_baseline), else falls back to the acquisition date."""
    c = candidate or {}
    off = c.get("boa_offset")
    if off is not None:
        try:
            return float(off)
        except (TypeError, ValueError):
            pass
    date = (c.get("date") or "")[:10]
    return -1000.0 if date and date >= S2_OFFSET_DATE else 0.0


def s2_reflectance(dn, offset, nodata=0):
    """uint16 Sentinel-2 L2A DN (3, H, W) -> float32 reflectance, NaN where nodata.

    A pixel is nodata when ALL three bands are `nodata` (a real dark pixel can hit 0
    in one band after the offset, and must not punch a hole)."""
    dn = np.asarray(dn)
    hole = np.all(dn == nodata, axis=0)
    refl = (dn.astype("float32") + np.float32(offset)) / np.float32(10000.0)
    refl[:, hole] = np.nan
    return refl


def hdr_white(arrays):
    """The white anchor, pooled over every array given — (white, note).

    One anchor for a whole batch (pre AND post) so a swipe between them doesn't show
    a brightness step no change on the ground produced. HDR_WHITE_PCT of the
    per-pixel channel maximum, clamped to [HDR_WHITE_MIN, HDR_WHITE_MAX]."""
    vals = []
    for a in arrays:
        if a is None:
            continue
        npix = a.shape[-2] * a.shape[-1]
        for step in (max(1, int(np.sqrt(npix / float(AUTO_SAMPLE)))), 1):
            sub = a[:, ::step, ::step]
            with np.errstate(invalid="ignore"):
                v = np.max(sub, axis=0).astype("float32").ravel()
            v = v[np.isfinite(v)]
            if v.size >= HDR_MIN_PIXELS or step == 1:
                break
        vals.append(v)
    v = np.concatenate(vals) if vals else np.empty(0, dtype="float32")
    if v.size < HDR_MIN_PIXELS:
        return HDR_WHITE_FALLBACK, (
            f"hdr: white {HDR_WHITE_FALLBACK:.2f} (fallback — only {v.size} valid "
            f"pixels, need {HDR_MIN_PIXELS})")
    p = float(np.percentile(v, HDR_WHITE_PCT))
    w = min(max(p, HDR_WHITE_MIN), HDR_WHITE_MAX)
    return w, f"hdr: white {w:.3f} = p{HDR_WHITE_PCT} of {v.size} pixels"


def _smooth(u):
    return u * u * (3.0 - 2.0 * u)


def _shoulder(d, knee):
    t = np.clip((d - knee) / (1.0 - knee), -50.0, None)
    return np.where(d <= knee, d, knee + (1.0 - knee) * (1.0 - np.exp(-t)))


def _contrast(d, k):
    if k == 1.0:
        return d
    d = np.clip(d, 0.0, 1.0)
    lo = 0.5 * (2.0 * d) ** k
    hi = 1.0 - 0.5 * (2.0 - 2.0 * d) ** k
    return np.where(d < 0.5, lo, hi)


def _strip_cast(rgb, strength, cool, gate_lo=None, gate_hi=None):
    if strength <= 0:
        return rgb
    r, g, b = rgb[0], rgb[1], rgb[2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    excess = (b - r) if cool else (r - b)
    w = np.clip(excess / CAST_STRIP_K, 0.0, 1.0) * strength
    if gate_lo is not None:
        w = w * _smooth(np.clip((y - gate_lo) / max(gate_hi - gate_lo, 1e-6), 0.0, 1.0))
    return rgb + (y - rgb) * w


def hdr_rgb(refl, white, knee=KNEE, contrast=CONTRAST):
    """(3, H, W) reflectance -> (3, H, W) uint8, 0 = nodata, valid pixels in 1..254.

    review_package._highlight_hdr's curve (rolloff + tapered log lift, chroma
    neutralisers eased by HDR_SATURATION). Brightness is untouched by the
    neutralisers, so they can't cost snow contrast."""
    s = HDR_SATURATION
    desat, blue_desat = DESAT * s, BLUE_DESAT * s
    cyan_strip, pink_strip = CYAN_STRIP * s, PINK_STRIP * s
    refl = np.asarray(refl, dtype="float64")
    valid = np.all(np.isfinite(refl), axis=0)
    refl = np.clip(np.nan_to_num(refl, nan=0.0), 0.0, None)
    rgb = refl / max(white, 1e-6)
    rmax = refl.max(axis=0)
    m = rgb.max(axis=0)
    sh = _shoulder(m, knee)
    u = np.clip(sh, 0.0, None)
    lifted = np.log1p(HDR_LIFT * u) / float(np.log1p(HDR_LIFT))
    sh = u + (lifted - u) * np.clip(1.0 - u, 0.0, None) ** HDR_LIFT_TAPER
    rgb = rgb * (sh / np.where(m != 0, m, 1.0))
    # highlight fade toward white at the pixel's own brightness (reflectance-gated)
    wd = desat * _smooth(np.clip((rmax - DESAT_ONSET) / max(1.0 - DESAT_ONSET, 1e-6),
                                 0.0, 1.0))
    rgb = rgb + (sh - rgb) * wd
    # cyan-selective de-blue of skylit snow shadow
    if blue_desat > 0:
        warm = np.maximum(rgb[0], rgb[1])
        cyan = np.clip((rgb[2] - warm) / BLUE_DESAT_K, 0.0, 1.0)
        ub = np.clip((rmax - BLUE_DESAT_LO) / max(BLUE_DESAT_HI - BLUE_DESAT_LO, 1e-6),
                     0.0, 1.0)
        rgb = rgb + (sh - rgb) * np.clip(blue_desat * cyan * _smooth(ub), 0.0, 1.0)
    rgb = _contrast(np.clip(rgb, 0.0, 1.0), contrast) * 255.0
    rgb = _strip_cast(rgb, cyan_strip, cool=True)
    rgb = _strip_cast(rgb, pink_strip, cool=False,
                      gate_lo=PINK_GATE_LO, gate_hi=PINK_GATE_HI)
    out = np.clip(rgb, 1.0, 254.0).astype("uint8")
    out[:, ~valid] = 0
    return out
