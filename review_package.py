"""Export a per-event QGIS review package from pre/post imagery.

This is purely the visual-review hinge — there is NO automatic delineation. The
package gives a human everything needed to spot a landslide scar by eye and
digitize it in QGIS. Which of these "scenes" get written is selectable (see
SCENE_KEYS / the `scenes` arg); by default all are produced.

Every imagery layer carries the ACQUISITION DATE of the scene(s) behind it in its
filename — and therefore in its QGIS layer name (`<pre>` / `<post>` below, see
`_date_tag`), so a loaded layer always says when it was imaged:

  <event>_pre_<pre>_rgb.tif        true-colour BEFORE (context, false-positive check)
  <event>_post_<post>_rgb.tif      true-colour AFTER
  <event>_pre_<pre>_highlight.tif  Highlight Optimized Natural Color BEFORE (see _highlight_natural)
  <event>_post_<post>_highlight.tif Highlight Optimized Natural Color AFTER
  <event>_pre_<pre>_hdr.tif        HDR, one scene-fitted curve, BEFORE (see _highlight_hdr)
  <event>_post_<post>_hdr.tif      HDR, one scene-fitted curve, AFTER
  <event>_pre_<pre>_falsecolor.tif NIR-red-green BEFORE  (vegetation = bright red)
  <event>_post_<post>_falsecolor.tif NIR-red-green AFTER  (fresh scar = dark/bare)
  <event>_pre_<pre>_swir.tif       SWIR false colour BEFORE (12-11-4: snow blue, rock/debris orange)
  <event>_post_<post>_swir.tif     SWIR false colour AFTER  (fresh debris on snow = orange/brown)
  <event>_pre_<pre>_ndvi.tif       NDVI BEFORE
  <event>_post_<post>_ndvi.tif     NDVI AFTER
  <event>_dndvi_<pre>_vs_<post>.tif   pre->post NDVI change  (vegetation loss = strong negative)
  <event>_dndsi_<pre>_vs_<post>.tif   pre->post NDSI change  (new debris on snow/ice = strong negative)
  <event>_dbright_<pre>_vs_<post>.tif pre->post brightness/albedo change (bare rock/soil = positive)
  <event>_point.gpkg         the predicted (seismic) epicentre to search around (always written)
  <event>_metadata.json      sensor + scene ids/dates used, and the layer file list

Compare the pre/post pairs with the QGIS Swipe tool and read the two change
rasters (dNDVI + dBrightness) — where they agree is a strong scar signal.
"""
from __future__ import annotations
import json
import os

import numpy as np
import geopandas as gpd
from shapely.geometry import Point
import rioxarray  # noqa: F401  (registers the .rio accessor)

# Canonical, ordered list of selectable download "scenes". Each key is a token
# accepted by run_single.py --scenes and produced by export_review_package; the
# QGIS dock builds its checkboxes from the same keys. The predicted-point layer
# is NOT in here — it is always written as the digitizing anchor.
SCENE_KEYS = ["true_color", "highlight_natural", "highlight_hdr", "false_color",
              "swir_falsecolor", "dndsi", "ndvi", "dndvi", "dbright"]

# Selectable tone curves for the PlanetScope SR detail render — see _highlight_rolloff
# and _highlight_natural. The plugin's PlanetScope tab builds its tone combo box from
# this dict and passes the key through run_single.py --planet-tone; the value is the
# label shown. 'natural' and 'hdr' are ALSO review-package products in their own right
# on Sentinel-2/Landsat (SCENE_KEYS 'highlight_natural' and 'highlight_hdr') — this
# dict is only the PlanetScope tab's combo box, not the list of who may use a curve.
# Ordered rolloff-first: it is the DEFAULT, because the review task is reading scars
# on terrain, where full-brightness midtones matter more than texture inside bright
# ice.
TONE_MODES = {
    "knee": "Highlight rolloff — untouched midtones, tamed highlights",
    "natural": "Highlight Optimized Natural Color — even detail, softer contrast",
    "linear": "None — plain linear stretch, no tone shaping",
    "hdr": "HDR (one global curve) — shadow AND snow detail, no clipping at either end",
}

# Gentle S-curve strength applied to BOTH tone modes by the PlanetScope render path
# (see _contrast). 1.0 is an exact identity; 1.15 is roughly a "+10" contrast nudge in
# photo-editor terms. NOTE the render functions themselves default to 1.0 so the
# Sentinel-2/Landsat review-package export stays byte-identical to before — only the
# Planet path opts in, via run_single._write_render_json.
CONTRAST = 1.15

# Reflectance mapped to full white by the LINEAR part of the knee curve, and where
# on that 0-1 ramp the highlight rolloff takes over. knee=0.55 -> reflectance 0.165;
# everything below that is a plain linear stretch (see _highlight_rolloff). Tuned for
# reading scars on rock/vegetation with bright ice as context: it deliberately spends
# the ice's internal texture (0.45-1.0 reflectance lands within ~10 DN of white) to
# keep every midtone and shadow at full linear brightness. For features ON the ice,
# knee=0.40 with white=0.45 trades ~37% midtone brightness for ~45 DN of ice texture.
WHITE = 0.30
KNEE = 0.55

# Highlight desaturation ("path to white") for the knee curve — see _highlight_rolloff.
# DESAT is the strength (0 = off, i.e. the old purely ratio-preserving output; 1 = full),
# DESAT_ONSET the REFLECTANCE at which the fade starts.
#
# Why this is needed: the knee curve is ratio-preserving by design (one gain per pixel,
# driven by the channel maximum), so it renders the source hue at FULL saturation right
# up to 255 — there is no drift to white. That is correct only while the source hue means
# something, and over bright snow it stops meaning anything. Planet's atmospheric
# correction over-corrects snow badly at low sun, per band and by different amounts: on a
# 17.4° solar-elevation scene here, surface reflectance reached 3.26 (326% — impossible),
# a third of pixels exceeded 1.0, and which band came out on top flipped pixel to pixel
# (red>blue in 58%, blue>red in 42%). Reproducing that faithfully paints the highlights in
# alternating orange and cyan.
#
# The fade is keyed to reflectance, not to how far the shoulder has compressed: the
# shoulder's `1-exp(-t)` is already at 0.92 by 0.5 reflectance and 0.998 by 1.0, so it
# cannot distinguish bright terrain from a failed retrieval, and using it washed out real
# ground. Reflectance can. Above 1.0 a value is physically impossible, so its hue is an
# artefact by definition; DESAT_ONSET=0.5 starts the fade well below that but still above
# every real surface here — sunlit snow and ice only. Everything that carries diagnostic
# colour (vegetation ~0.05, fresh debris ~0.2, moraine ~0.3, skylit blue shadow ~0.4)
# sits below the onset and comes out bit-identical. That matters: scars are read partly
# BY colour, fresh debris against vegetation.
DESAT = 1.0
DESAT_ONSET = 0.5

# Cyan-selective de-blue: a SECOND desaturation pass keyed to HUE, not brightness.
# The DESAT pass above only fires above DESAT_ONSET (0.5 reflectance), so the skylit
# blue cast on shadowed snow and glacier ice — ~0.2-0.4 reflectance, only ~8% more
# blue than red at the source but stretched loud as the knee brightens it toward white
# — passes straight through. (The DESAT block above notes that skylit blue shadow was
# deliberately left bit-identical; this pass is where we walk that back on purpose.)
# It fades a pixel toward its own neutral value (`sh`) in proportion to how far BLUE
# sits above the warm red/green channels, so warm scar/debris colour (red >= blue) is
# bit-identical BY CONSTRUCTION — only a blue/cyan excess is pulled to grey. Neutralises
# to GREY, unlike cutting the blue channel (which leaves green on top and greens the
# snow). It trades the blue of skylit shadow/ice — real, but not the diagnostic signal —
# for cleaner snow while leaving untouched the warm colour scars are actually read by.
# BLUE_DESAT=0 restores the old output exactly.
BLUE_DESAT = 0.6          # strength: 0 = off, 1.0 ~ fully neutral blue shadows
BLUE_DESAT_K = 0.18       # post-gain (blue - max(red,green)) that earns full weight
BLUE_DESAT_LO = 0.08      # reflectance gate: below this (deep-noise shadow) no fade ...
BLUE_DESAT_HI = 0.22      # ... rising to full weight by here (shadowed-snow reflectance)

# Cast-strip: a final pass on the RENDERED rgb that fades a colour cast toward each
# pixel's own luminance. Because the fade target IS the luminance, it removes a cast
# WITHOUT changing brightness — the whole point, vs the 'natural' cube-root mode which
# de-fringes by darkening the scene. Two casts are handled:
#   CYAN_STRIP fades a COOL cast (blue > red — the cyan AND blue that Planet SR's
#     atmospheric over-correction paints on bright snow/ice) at every brightness. It
#     catches true cyan the BLUE_DESAT pass above misses (that one keys on blue over the
#     MAX of red/green, so a cyan pixel with high green slips through).
#   PINK_STRIP fades a WARM cast (red > blue — the pink/salmon on bright snow) but ONLY
#     through a luminance gate, so the warm colour of scars/debris on darker terrain —
#     the diagnostic signal — is left untouched.
# CAST_STRIP_K is the channel-difference (0-255 DN) that earns full strength. Either
# strength at 0 disables that strip.
CYAN_STRIP = 1.0          # cool (cyan/blue) cast removal strength; 0 = off
PINK_STRIP = 1.0          # warm (pink/salmon) cast removal strength on snow; 0 = off
CAST_STRIP_K = 30.0       # DN of |blue-red| that earns full-strength fade
PINK_GATE_LO = 175.0      # luminance (0-255) below which PINK_STRIP does nothing ...
PINK_GATE_HI = 210.0      # ... rising to full strength here (snow-bright pixels only)

# --- auto-stretch: fitting the knee curve to a scene that has no dark end ----------
# WHITE/KNEE above assume the frame CONTAINS terrain: the linear, arithmetically-untouched
# zone is reflectance 0 -> KNEE*WHITE (0.165 by default) and everything above it is
# compressed into the shoulder's last few DN. On an AOI that is entirely snow/ice that
# assumption inverts. A real 2026-06-26 PlanetScope clip here ran p1 0.45 / median 0.84 /
# p90 0.93 reflectance with NOTHING below 0.165: all of it landed in the shoulder, 91% of
# the image came out within 5 DN of white, and the desaturation gate stripped the hue off
# what was left (97% of the frame sat above DESAT_ONSET). The texture was in the data the
# whole time — a percentile stretch over the same pixels gives ~6x the standard deviation.
#
# So derive a black point and a white point from the scene, but ONLY when the scene has no
# dark end to protect. The trigger is the fraction of the AOI inside the untouched linear
# zone (see auto_stretch): at AUTO_DARK_FULL or more the defaults are left exactly alone
# and the render is unchanged, because a scene with that much dark ground is the mixed
# terrain-with-ice case the fixed curve is deliberately tuned for — raising WHITE there
# dims every midtone to buy texture in ice that is only context. Below AUTO_DARK_MIN the
# full scene-derived stretch applies, and in between the two are blended, so two renders
# of the same AOI can't flip between looks on a hair's-breadth difference.
AUTO_DARK_FULL = 0.05     # >= this fraction below the linear ceiling -> defaults, untouched
AUTO_DARK_MIN = 0.005     # <= this -> the scene-derived stretch, at full strength
# The black point comes from a percentile FAR out in the dark tail, not from p1: it is the
# one number here that can destroy information, and the thing it would destroy is a small
# dark subject on ice — a fresh scar. p0.1 of a 20 km AOI is ~0.1% of the frame, so any
# scar bigger than about 600 m x 600 m contains the black point instead of being clipped by
# it, and keeps its internal texture. The cost is small: on the measured all-ice clip p0.1
# is 0.405 against p1's 0.464, i.e. ~11% of the usable span given up as insurance.
AUTO_BLACK_PCT = 0.1
AUTO_WHITE_PCT = 99.0     # white point percentile; the tail above it is the shoulder's job
AUTO_BLACK_MARGIN = 0.10  # ... then drop the black point this FRACTION lower (never to 0)
# Hard ceiling on a derived white point. Reflectance above 1.0 is physically impossible, so
# a percentile up there is measuring Planet's atmospheric-correction overshoot rather than
# the ground (a low-sun 2026-01-31 clip here hit p99 = 3.5 with 32% of pixels over 1.0).
# Letting that set white would crush the real scene into the bottom eighth of the ramp; the
# shoulder already handles arbitrarily bright pixels without clipping them.
AUTO_WHITE_MAX = 1.0
AUTO_ONSET_MAX = 0.95     # ... and keep the desat onset off 1.0 (see _desat_weight)
AUTO_MIN_SPAN = 0.05      # refuse to stretch a histogram flatter than this (noise blowup)
AUTO_MIN_PIXELS = 50000   # ... or one too thinly sampled to resolve AUTO_BLACK_PCT
AUTO_SAMPLE = 500000      # target sample size for the percentile scan (strided read)


# ---------------------------------------------------------------------------
# How every raster in this package is written.
#
# A bare .rio.to_raster(path, driver="GTiff") takes GDAL's defaults: ONE-ROW
# strips, no compression, no overviews. That layout is what makes a stitched
# multi-tile output unpannable in QGIS. The block is a whole raster-width row,
# so filling a ~1900 px screen window drags in the FULL width, once per row --
# a ~5x over-read and ~1100 separate block reads per repaint, which also blows
# GDAL's block cache so the next pan step re-reads everything it just threw
# away. 512x512 tiles turn the same repaint into ~12 block reads of pixels the
# screen actually wants, and adjacent pan steps reuse them.
#
# Measured on a 9344x9329 dBright (Bering Glacier), GDAL cache 64 MB,
# 24 pan repaints at 1900x1100: 0.36 s -> 0.13 s, file 697 -> 315 MB.
#
# ZSTD level 1 is LOSSLESS -- pixels read back bit-identical -- and is chosen
# over DEFLATE because DEFLATE's decompression cost cancels most of the tiling
# win (0.29 s on the same test, barely better than no tiling at all).
#
# float64 change rasters are written as float32. These are bounded indices
# (dBright / dNDSI / dNDVI all sit inside about +-2), so float32's ~7 significant
# digits are orders of magnitude more than the data carries -- and it halves
# every byte the renderer has to move.
#
# Measured before switching, against the hand-digitized "Total Area" scar
# outlines in the shared-drive QGIS projects: 51 polygon x raster combinations,
# 559,515 scar pixels. 41 combinations were unchanged outright; the other 10
# moved exactly ONE 10 m pixel across a display/floor threshold. Worst area
# disagreement anywhere: 100 m². For scale, one pixel of wobble in where the
# outline was drawn is worth +-123,718 m² on the Iliamna scar -- the digitizing
# hand is ~1,200x the larger error term.
#
# Pass keep_float64=True for anything that is NOT a bounded index (absolute
# coordinates, timestamps) -- nothing in this module currently needs it.
# ---------------------------------------------------------------------------
TILE = 512                # internal block size, px
OVERVIEW_FLOOR = 512      # stop halving once the smallest level is under this


def _tif_opts(dtype):
    """GTiff creation options for `dtype`. PREDICTOR is dtype-specific: 3 is the
    floating-point predictor, 2 the integer one; using the wrong one just costs
    compression ratio, but there is no reason to."""
    pred = 3 if np.issubdtype(np.dtype(dtype), np.floating) else 2
    return dict(driver="GTiff", tiled=True, blockxsize=TILE, blockysize=TILE,
                compress="ZSTD", zstd_level=1, predictor=pred,
                bigtiff="IF_SAFER", num_threads="ALL_CPUS")


def _add_overviews(path):
    """Append internal pyramid levels, halving until the top level is small.

    Overviews are DISPLAY ONLY -- every read that does not ask for a decimated
    view (Identify, the fusion/volume tabs, gdal.Warp) still gets full
    resolution. They exist so a zoomed-out repaint reads a 292 px thumbnail
    instead of decimating 9344 px of source. Internal (inside the .tif), not a
    .ovr sidecar, so the pyramid cannot get separated from the raster when a
    package folder is moved or shared.

    Best-effort: a package whose overviews failed to build is still a valid,
    correct package, so never fail the export over it."""
    try:
        import rasterio
        from rasterio.enums import Resampling
        with rasterio.open(path, "r+") as ds:
            n, f, factors = max(ds.width, ds.height), 2, []
            while n // f >= OVERVIEW_FLOOR:
                factors.append(f)
                f *= 2
            if factors:
                ds.build_overviews(factors, Resampling.average)
    except Exception as e:
        print(f"note: could not build overviews for {os.path.basename(path)}: {e}")


def _to_tif(da, path, src_crs, nodata=None, keep_float64=False):
    """Write `da` as a tiled, overviewed GeoTIFF. The single write path for this
    module -- see the block comment above for why the options matter.

    float64 is narrowed to float32 unless `keep_float64`; the cast happens BEFORE
    the CRS/nodata tags go on, because astype() drops rioxarray's encoding."""
    if not keep_float64 and np.dtype(da.dtype) == np.float64:
        da = da.astype("float32")
    da = da.rio.write_crs(src_crs)
    if nodata is not None:
        da = da.rio.write_nodata(nodata)
    da.rio.to_raster(path, **_tif_opts(da.dtype))
    _add_overviews(path)


def _rgb(comp, bands, path, src_crs):
    """Write a 3-band uint8 GeoTIFF from reflectance bands, stretched 0-0.3 -> 0-255."""
    rgb = comp.sel(band=bands)
    rgb = (rgb.clip(0, 0.3) / 0.3 * 255).fillna(0).astype("uint8")
    # Drop source attrs that no longer match a 3-band uint8 image: masked-read
    # sources (e.g. Planet) carry a float NaN nodata that can't cast to uint8 and
    # a 4-band 'long_name' that trips rioxarray's band-name check.
    rgb.attrs = {}
    _to_tif(rgb, path, src_crs, nodata=0)


def _contrast(d, k=1.0):
    """Gentle S-curve contrast on a 0-1 DataArray. k=1.0 is an exact identity.

    This is the classic "gain" sigmoid: it pivots at 0.5 and pins BOTH endpoints,
    so unlike a linear contrast stretch around a mid-grey pivot it can never clip
    shadows to solid black or highlights to flat white — it only steepens the
    middle and eases off at both extremes. That matters here because clipping is
    the exact failure mode both tone curves exist to avoid.
    """
    if k == 1.0:
        return d
    d = d.clip(0, 1)   # a negative base with a fractional exponent would go NaN
    lo = 0.5 * (2.0 * d) ** k
    hi = 1.0 - 0.5 * (2.0 - 2.0 * d) ** k
    return lo.where(d < 0.5, hi)


def _shoulder(d, knee=KNEE):
    """Highlight rolloff: identity for d <= knee, smooth asymptote to 1.0 above it.

    Slope is exactly 1 at the knee, so the curve leaves the straight line
    tangentially and there is no tone break along a snowline. Asymptotic rather
    than clamped, so arbitrarily bright pixels stay distinct instead of piling up
    at 255 (snow/ice surface reflectance runs well past 1.0).

    The lower clamp on `t` is numerically a no-op — it only bounds the branch that
    `.where` discards — but it keeps np.exp from overflowing to inf (and warning about
    it) on the far-below-black values a narrow manual black/white pair can produce."""
    t = ((d - knee) / (1.0 - knee)).clip(-50.0, None)
    return d.where(d <= knee, knee + (1.0 - knee) * (1.0 - np.exp(-t)))


def _desat_weight(refl, onset=DESAT_ONSET, strength=DESAT):
    """How far a pixel should be faded toward neutral: 0-1, keyed to REFLECTANCE.

    `refl` is the pixel's channel-maximum surface reflectance (NOT normalised — the
    black/white points must not move this gate, which is about what a value means
    physically). Exactly 0 at and below `onset`, rising to `strength` at reflectance
    1.0 — the threshold past which a surface-reflectance value is impossible and its
    hue is an artefact by definition. Smoothstep rather than a linear ramp so the
    derivative is zero at both ends and no edge appears in the render where the fade
    begins. See DESAT for why reflectance and not the shoulder's compression drives
    this, and auto_stretch for why `onset` rises with a scene-derived white point."""
    u = ((refl - onset) / max(1.0 - onset, 1e-6)).clip(0.0, 1.0)
    return strength * u * u * (3.0 - 2.0 * u)


def auto_stretch(comps, white=WHITE, knee=KNEE, onset=DESAT_ONSET):
    """(black, white, onset, note) for _highlight_rolloff, fitted to the scene itself.

    Rescues an AOI that is entirely snow/ice, where the fixed WHITE/KNEE put the whole
    histogram in the shoulder and the render comes out flat white (see AUTO_DARK_FULL
    for the measured case). Returns the arguments to render WITH, so the caller can
    stay ignorant of how the decision was made.

    `comps` is the list of composites the render covers — normally [pre, post], Nones
    allowed. They are pooled into ONE stretch on purpose: rendering each side against
    its own percentiles would put a brightness step between the before and after
    layers that no real change on the ground produced, which is exactly what a swipe
    comparison would then read as a scar.

    Leaves the defaults strictly alone unless the scene has no dark end to protect —
    a partially-iced AOI keeps the render it has today, because the fixed curve is
    tuned for precisely that case. Guarantees:

      * >= AUTO_DARK_FULL of valid pixels below the linear ceiling (knee*white) -> the
        inputs are returned verbatim, so the render is unchanged.
      * a flat (< AUTO_MIN_SPAN) or barely-sampled histogram is refused likewise,
        rather than amplifying sensor noise across the full ramp.
      * the black point comes from AUTO_BLACK_PCT and is then dropped a further
        AUTO_BLACK_MARGIN, so it clips well under 0.1% of the frame. A dark subject
        larger than that keeps its internal texture; one smaller flattens toward black
        (still the most conspicuous thing in a bright frame, and switching auto-stretch
        off recovers its interior).
      * a derived white point is capped at AUTO_WHITE_MAX, so an atmospheric-correction
        overshoot can't set it.

    Percentiles are taken on the per-pixel channel MAXIMUM, the same quantity that
    drives the shoulder gain and the desaturation gate. The black point is then
    subtracted per channel, so in the darkest ~0.1% the weakest channel clips first and
    those pixels lean toward the hue of their strongest band.

    `onset` is raised to the white point when one is derived: the desaturation exists
    to kill hue on physically impossible reflectance (see DESAT), and on a scene whose
    real ground sits at 0.8-0.9 an onset of 0.5 would strip the colour off the terrain
    it is supposed to be showing.
    """
    vals = []
    for c in comps:
        if c is None:
            continue
        rgb = c.sel(band=["red", "green", "blue"])
        ydim, xdim = rgb.dims[-2], rgb.dims[-1]
        npix = rgb.sizes[ydim] * rgb.sizes[xdim]
        # Strided so the scan costs the same on any AOI size. A PlanetScope strip can
        # leave most of the AOI grid as NaN, though, so if the sample comes back thin
        # (partial coverage, not a small scene) fall back to reading every pixel —
        # sparse coverage is a reason to look harder, not to give up on the stretch.
        for step in (max(1, int(np.sqrt(npix / float(AUTO_SAMPLE)))), 1):
            sub = rgb.isel({ydim: slice(None, None, step), xdim: slice(None, None, step)})
            v = np.asarray(sub.max(dim="band").values, dtype="float32").ravel()
            v = v[np.isfinite(v)]
            if v.size >= AUTO_MIN_PIXELS or step == 1:
                break
        vals.append(v)
    v = np.concatenate(vals) if vals else np.empty(0, dtype="float32")
    if v.size < AUTO_MIN_PIXELS:
        return 0.0, white, onset, (
            f"auto-stretch: off — only {v.size} valid pixels sampled, too few to take "
            f"percentiles from (need {AUTO_MIN_PIXELS})")

    ceiling = knee * white                      # top of the untouched linear zone
    frac_dark = float(np.mean(v < ceiling))
    # smoothstep 1 -> 0 across [AUTO_DARK_MIN, AUTO_DARK_FULL]; 1 = fully scene-derived
    u = (frac_dark - AUTO_DARK_MIN) / (AUTO_DARK_FULL - AUTO_DARK_MIN)
    u = min(max(u, 0.0), 1.0)
    w = 1.0 - u * u * (3.0 - 2.0 * u)
    if w <= 0.0:
        return 0.0, white, onset, (
            f"auto-stretch: off — {frac_dark * 100:.1f}% of the AOI is below "
            f"{ceiling:.3f} reflectance, so the scene has terrain the fixed curve "
            f"already renders untouched")

    lo, hi = (float(x) for x in np.percentile(v, [AUTO_BLACK_PCT, AUTO_WHITE_PCT]))
    # A FRACTION below p0.1, never a fraction of the span: on a scene with an impossible
    # bright tail the span is huge and subtracting a slice of it would drive the black
    # point to 0, silently turning the stretch back into today's flat-white render.
    lo = max(0.0, lo * (1.0 - AUTO_BLACK_MARGIN))
    hi = min(hi, AUTO_WHITE_MAX)
    if hi - lo < AUTO_MIN_SPAN:
        return 0.0, white, onset, (
            f"auto-stretch: off — the scene spans only {hi - lo:.3f} reflectance "
            f"({lo:.3f}-{hi:.3f}), too flat to stretch without amplifying noise")

    black_out = w * lo                          # w<1 blends toward no shift at all
    white_out = (1.0 - w) * white + w * hi
    onset_out = min(max(onset, white_out), AUTO_ONSET_MAX)
    return black_out, white_out, onset_out, (
        f"auto-stretch: {frac_dark * 100:.2f}% of the AOI below {ceiling:.3f} "
        f"reflectance -> black {black_out:.3f}, white {white_out:.3f} (was 0.000/"
        f"{white:.3f}), desat onset {onset_out:.2f}"
        + (f", blended at {w:.2f} strength" if w < 1.0 else "")
        + f"; scene p{AUTO_BLACK_PCT:g}-p{AUTO_WHITE_PCT:g} = {lo:.3f}-{hi:.3f}")


def _strip_cast(rgb, strength, cool, gate_lo=None, gate_hi=None):
    """Fade a colour cast toward luminance on the FINAL 0-255 rgb (band red/green/blue).

    The fade target is each pixel's own luminance, so the cast is neutralised with NO
    change in brightness (see CYAN_STRIP/PINK_STRIP). cool=True targets a COOL cast
    (blue>red: cyan and blue); cool=False a WARM cast (red>blue: pink/salmon). Weight
    scales with the blue/red difference over CAST_STRIP_K. A gate_lo/gate_hi luminance
    window, when given, limits the fade to bright pixels (snow), so warm scar/debris
    colour on darker terrain is untouched. strength<=0 is an exact no-op."""
    if strength <= 0:
        return rgb
    r = rgb.sel(band="red").drop_vars("band")
    g = rgb.sel(band="green").drop_vars("band")
    b = rgb.sel(band="blue").drop_vars("band")
    y = 0.299 * r + 0.587 * g + 0.114 * b
    excess = (b - r) if cool else (r - b)
    w = (excess / CAST_STRIP_K).clip(0.0, 1.0) * strength
    if gate_lo is not None:
        u = ((y - gate_lo) / max(gate_hi - gate_lo, 1e-6)).clip(0.0, 1.0)
        w = w * (u * u * (3.0 - 2.0 * u))
    return rgb + (y - rgb) * w


# HDR tone mode: ONE global tone curve, fitted to the scene, that holds detail at BOTH ends
# of a frame carrying deep shadow and sunlit snow at once. It replaced a Mertens exposure
# fusion (three bracketed renders blended through a Laplacian pyramid), which was dropped
# because it could not make the "no crushed blacks, no blown highlights" promise on every
# scene: the pyramid collapse oversteps the input range — the documented main limitation of
# exposure fusion (Mertens-Kautz-Van Reeth 2007; see the IPOL reference implementation) —
# and measured 10.6% of the frame above 1.0 on a mixed rock-and-glacier scene, every one of
# those pixels then flattened to paper white by the clip that followed. A second clip in the
# contrast-normalising finish could crush both ends at once (0.81% at DN>=254 AND 1.45% at
# DN<=1 on the 2026-09-08 Wrangell frame). Stacking two tone curves in sequence is also a
# known way to get unpredictable tone, which is why photo pipelines forbid it.
#
# So the curve is a single pass with no clip in it anywhere:
#
#     u = _shoulder(refl / white)          asymptotic -> u < 1 always, highlights CANNOT blow
#     y = u + (log1p(k*u)/log1p(k) - u) * (1 - u)**HDR_LIFT_TAPER        k = HDR_LIFT
#
# _shoulder never reaches 1.0 for finite input, so the output is strictly inside (0, 1) and
# the DN 1/254 clamp in _highlight_hdr is a guarantee, not a rescue.
#
# The TAPER is the part that took measuring. A plain log lift (the second line without the
# (1-u) weight) opens the shadows beautifully and then flattens the snow, because log1p
# compresses hardest exactly where the shoulder has already compressed: on an icefield
# frame it drove the median to DN 232 and left the whole snow field inside 6 DN. Weighting
# the lift by (1-u)**HDR_LIFT_TAPER applies it at the bottom and fades it to nothing at the
# top, so snow keeps the slope the shoulder gave it. Both ends measured on two real S2
# frames (their reflectance recovered from the HONC render, which is an exactly invertible
# cube root), against the HONC that this render sits beside — darkest quartile and
# brightest decile, luminance std in DN:
#
#                          Kenai icefield (refl p50 0.88)   Wrangells (refl p50 0.10)
#     render                 shadow      snow                shadow      snow
#     HONC cube root          13.9        4.1                 10.0        7.7
#     plain log lift          17.6        2.0                 14.1        4.0
#     this curve              14.3        5.4                 13.4        9.9
#
# i.e. better than HONC at BOTH ends on BOTH scenes, where the untapered lift bought
# shadows by wrecking snow. The exposure fusion this replaced still wins the brightest
# decile on the icefield (9.9 vs 5.4) — that is local tone mapping doing something a
# single global curve provably cannot, and it is the price of never clipping.
HDR_LIFT = 60.0                    # shadow lift strength; 0 = no lift (the plain rolloff curve)
HDR_LIFT_TAPER = 2.0               # ... faded out by (1-u)**this, so the highlights keep their slope
# The white point is the scene's own bright tail, pooled over pre AND post (see hdr_white) —
# a percentile, not the maximum, because everything above it is compressed by the asymptote
# rather than clipped, so the anchor only has to be about right. Clamped at both ends: below
# HDR_WHITE_MIN a dark frame would be stretched until its noise showed, above HDR_WHITE_MAX
# an atmospheric-correction overshoot could set it and drag the whole scene dark.
HDR_WHITE_PCT = 99.5               # percentile of channel-max reflectance -> the white anchor
HDR_WHITE_MIN = 0.30               # ... never below this (a dark scene keeps the fixed curve's reach)
HDR_WHITE_MAX = 1.30               # ... nor above it (an atco overshoot must not set the white)
HDR_WHITE_FALLBACK = 0.90          # used when the frame is too small to take a percentile from
HDR_MIN_PIXELS = 5000              # ... below this many valid pixels, fall back
# HDR keeps a BIT of colour by easing the chroma neutralizers (highlight desat, cyan de-blue,
# cyan/pink cast-strips) by this factor — otherwise a mostly-bright scene (snow/cloud) is
# neutralised almost to greyscale, which reads as lifeless. The neutralizers only touch chroma,
# never luminance, so this cannot darken the image or flatten the snow's contrast. HDR-ONLY: the
# knee mode still neutralises fully (its casts are cleaned as before). 1.0 = fully neutral (old
# HDR look); lower keeps more colour (but lets more of the atmospheric cast/edge fringe through).
HDR_SATURATION = 0.7

def _rolloff_rgb(comp, white=WHITE, knee=KNEE, contrast=1.0,
                 desat=DESAT, black=0.0, onset=DESAT_ONSET,
                 blue_desat=BLUE_DESAT, cyan_strip=CYAN_STRIP,
                 pink_strip=PINK_STRIP, lift=0.0, lift_taper=HDR_LIFT_TAPER):
    """The "highlight rolloff" render as a 0-255 float rgb DataArray (NaN = nodata);
    _highlight_rolloff writes it and _highlight_hdr fuses several exposures of it.

    The alternative to _highlight_natural's global power law: below the knee this
    is the SAME linear 0..white stretch _rgb uses, so midtones and shadows come
    out arithmetically untouched (at knee=0.55, white=0.30 that's every pixel
    under 0.165 reflectance — vegetation, wet rock, shadowed slope, moraine).
    Only brighter pixels get compressed. Use it when the subject sits on terrain
    and bright ice/snow is context you just don't want blown out; use
    _highlight_natural when you need even detail across the whole dynamic range
    and can spend global contrast to get it.

    `black` shifts the bottom of that linear ramp off zero, for the one case the
    paragraph above does NOT cover: an AOI with no dark end at all, where the whole
    histogram sits above the knee and renders flat white. It defaults to 0, which is
    an exact no-op — the "midtones arithmetically untouched" guarantee holds whenever
    it is left there, and only there. auto_stretch derives it (with a matching `white`
    and `onset`) from the scene, and only for scenes that have nothing dark to lose.

    `lift` (HDR mode only; 0 is an exact no-op and every other caller leaves it there)
    opens the shadows by running the shoulder's own output through log1p before the gain
    is taken, weighted by (1 - u)**`lift_taper` so the lift fades to nothing at the top —
    see HDR_LIFT / HDR_LIFT_TAPER. It is what lets a single curve hold a shadowed valley
    and sunlit snow at once. Without the taper the lift buys the shadows by flattening the
    snow, which is measured and asserted in tests/test_hdr_curve.py.

    The rolloff is driven by the per-pixel channel MAXIMUM and applied as a single
    gain to R/G/B, so a pixel's hue and saturation survive the compression instead of
    drifting grey the way independent per-channel compression makes them. (The
    contrast step afterwards is per-channel, as in any photo editor, which lifts
    saturation very slightly — the same reason the Sentinel Hub script pairs its cube
    root with a saturation boost.)

    Preserving the ratio all the way to 255 is right for terrain and wrong for blown
    snow, where the source hue is an atmospheric-correction artefact rather than a
    colour — so pixels brighter than `onset` reflectance are additionally faded
    toward neutral (`desat`, see DESAT/_desat_weight). The fade is keyed to reflectance,
    which leaves every surface that carries diagnostic colour untouched; it is well
    above the knee, so the "midtones arithmetically untouched" guarantee above holds a
    fortiori. desat=0 restores the old purely ratio-preserving output.

    Finally, two cast-strips run on the rendered rgb (see _strip_cast, CYAN_STRIP/
    PINK_STRIP): they fade Planet SR's cyan/blue and pink casts on snow toward luminance,
    removing the colour without darkening — the reason to reach for this over the
    cube-root mode when the goal is to read detail under a fringe. cyan_strip/pink_strip
    at 0 skip them and restore the pre-cast-strip output.
    """
    refl = comp.sel(band=["red", "green", "blue"]).clip(0, None)
    rgb = (refl - black) / max(white - black, 1e-6)
    rmax = refl.max(dim="band")        # reflectance, for the physical desat gate
    m = rgb.max(dim="band")            # normalised, drives the shoulder gain
    # m.where(m != 0, 1.0) only guards the divide; _shoulder(0)==0 keeps those pixels 0.
    # Below a nonzero black point m goes negative, where _shoulder is the identity, so
    # sh/m is exactly 1 and those pixels pass through to be clipped to black below.
    sh = _shoulder(m, knee)
    if lift > 0:
        # HDR mode's shadow lift (see HDR_LIFT). Applied to the SHOULDER's output, so it
        # rides on top of the rolloff instead of replacing it, and to the channel maximum
        # only — the gain below is still one number per pixel, so hue survives it exactly
        # as it survives the rolloff. Faded out by (1 - u)**lift_taper so it opens the
        # shadows without flattening the snow (see HDR_LIFT_TAPER); the fade is 0 at the
        # top, so the curve still ends where the shoulder left it and cannot reach 1.
        # Negative `m` (possible only under a manual black point, which HDR never uses) is
        # floored first so log1p stays real.
        u = sh.clip(0.0, None)
        lifted = np.log1p(lift * u) / float(np.log1p(lift))
        sh = u + (lifted - u) * (1.0 - u).clip(0.0, None) ** lift_taper
    rgb = rgb * (sh / m.where(m != 0, 1.0))
    # Fade toward `sh` — the max channel's own post-gain value — so a compressed
    # highlight walks to WHITE at its own brightness rather than to grey.
    rgb = rgb + (sh - rgb) * _desat_weight(rmax, onset, desat)
    # Cyan-selective de-blue (see BLUE_DESAT): a second fade toward `sh`, but weighted
    # by how far BLUE sits above the warm channels rather than by brightness — so it
    # neutralises the skylit blue of snow-shadow/ice (which the reflectance-gated pass
    # above leaves alone below its onset) while warm scar/debris colour, where blue is
    # not the excess channel, is bit-identical.
    if blue_desat > 0:
        warm = rgb.sel(band=["red", "green"]).max(dim="band")
        cyan = ((rgb.sel(band="blue") - warm) / BLUE_DESAT_K).clip(0.0, 1.0)
        u = ((rmax - BLUE_DESAT_LO)
             / max(BLUE_DESAT_HI - BLUE_DESAT_LO, 1e-6)).clip(0.0, 1.0)
        # A manual black point over-stretches a blue shadow: subtracting it drives the
        # shadow's blue far past the warm channels, so a fixed-strength fade leaves a
        # larger blue tint than the fixed/auto curve does (measurably ~2.4x at black
        # 0.15). Raise the EFFECTIVE strength by that same amplification, rmax/(rmax -
        # black), so the leftover tint is the fraction a black=0 stretch would leave —
        # i.e. invariant to where the black point sits. At black=0 (ratio 1) this is
        # exactly blue_desat, so the fixed curve and every auto-stretch (which use a
        # near-zero black) render bit-for-bit as before.
        ratio = ((rmax - black) / rmax.where(rmax > 1e-6, 1e-6)).clip(0.0, 1.0)
        s_eff = 1.0 - (1.0 - blue_desat) * ratio
        rgb = rgb + (sh - rgb) * (s_eff * cyan
                                  * (u * u * (3.0 - 2.0 * u))).clip(0.0, 1.0)
    rgb = _contrast(rgb.clip(0, 1), contrast) * 255
    # Cast-strip on the final rgb, fading toward luminance so it CANNOT darken (unlike
    # the cube-root mode): clear the cyan/blue cast Planet SR paints on snow/ice, then
    # the pink/salmon warm cast on bright snow (luminance-gated, so warm scar/debris
    # colour on darker terrain is untouched). See CYAN_STRIP/PINK_STRIP.
    rgb = _strip_cast(rgb, cyan_strip, cool=True)
    rgb = _strip_cast(rgb, pink_strip, cool=False,
                      gate_lo=PINK_GATE_LO, gate_hi=PINK_GATE_HI)
    if black > 0:
        # nodata is 0, so a valid pixel driven to DN 0 by the black point would punch a
        # transparent HOLE in the layer exactly where a dark scar is. Floor the valid
        # pixels at 1; NaN passes through np.clip untouched and still becomes nodata.
        rgb = rgb.clip(1.0, 255.0)
    return rgb


def _write_rgb(rgb, path, src_crs):
    """Write a 0-255 float rgb DataArray (NaN = nodata) as the 3-band uint8 GeoTIFF."""
    rgb = rgb.fillna(0).astype("uint8")
    rgb.attrs = {}    # see _rgb: stale float-nodata / 4-band long_name would break the write
    _to_tif(rgb, path, src_crs, nodata=0)


def _highlight_rolloff(comp, path, src_crs, **kw):
    """Write the highlight-rolloff render (the curve itself is _rolloff_rgb)."""
    _write_rgb(_rolloff_rgb(comp, **kw), path, src_crs)


def hdr_white(comps):
    """The HDR curve's white anchor, fitted to the scene — (white, note).

    `comps` is the list of composites the render covers, normally [pre, post], Nones
    allowed. They are pooled into ONE white point for the same reason auto_stretch pools
    its stretch: rendering each side against its own percentiles would put a brightness
    step between the before and after layers that no change on the ground produced, and
    a swipe comparison would read that step as a scar.

    HDR_WHITE_PCT of the per-pixel channel MAXIMUM — the same quantity that drives the
    shoulder gain and the desaturation gate. A percentile rather than the maximum is safe
    here in a way it would not be under a clipping curve: everything above the anchor is
    compressed by _shoulder's asymptote, not clipped, so the brightest half-percent of the
    frame stays ordered and visible instead of piling up at white.
    """
    vals = []
    for c in comps:
        if c is None:
            continue
        rgb = c.sel(band=["red", "green", "blue"])
        ydim, xdim = rgb.dims[-2], rgb.dims[-1]
        npix = rgb.sizes[ydim] * rgb.sizes[xdim]
        # Strided so the scan costs the same on any AOI size; fall back to every pixel when
        # the sample comes back thin (partial coverage), exactly as auto_stretch does.
        for step in (max(1, int(np.sqrt(npix / float(AUTO_SAMPLE)))), 1):
            sub = rgb.isel({ydim: slice(None, None, step), xdim: slice(None, None, step)})
            v = np.asarray(sub.max(dim="band").values, dtype="float32").ravel()
            v = v[np.isfinite(v)]
            if v.size >= HDR_MIN_PIXELS or step == 1:
                break
        vals.append(v)
    v = np.concatenate(vals) if vals else np.empty(0, dtype="float32")
    if v.size < HDR_MIN_PIXELS:
        return HDR_WHITE_FALLBACK, (
            f"hdr: white {HDR_WHITE_FALLBACK:.2f} (fallback — only {v.size} valid pixels "
            f"sampled, too few for a percentile; need {HDR_MIN_PIXELS})")
    p = float(np.percentile(v, HDR_WHITE_PCT))
    w = min(max(p, HDR_WHITE_MIN), HDR_WHITE_MAX)
    why = "" if w == p else (
        f", clamped from {p:.3f} by "
        + ("HDR_WHITE_MIN" if w == HDR_WHITE_MIN else "HDR_WHITE_MAX"))
    return w, f"hdr: white {w:.3f} = p{HDR_WHITE_PCT} of {v.size} sampled pixels{why}"


def _highlight_hdr(comp, path, src_crs, knee=KNEE, contrast=1.0, desat=DESAT,
                   black=0.0, onset=DESAT_ONSET, blue_desat=BLUE_DESAT,
                   cyan_strip=CYAN_STRIP, pink_strip=PINK_STRIP, white=None):
    """HDR render: ONE global tone curve — the rolloff shoulder with a log shadow lift on
    top (see HDR_LIFT) — so a frame holding both deep shadow and sunlit snow keeps detail at
    BOTH ends without a clip anywhere in the chain. `white` is the scene's anchor from
    hdr_white; passing None fits it from this composite alone, which is right for a
    one-sided render and wrong for a pre/post pair (see hdr_white)."""
    # Keep a bit of colour: ease the chroma neutralizers by HDR_SATURATION. They touch only
    # chroma (fade toward the pixel's own luminance/neutral), never brightness, so this cannot
    # darken the image or reduce the snow's contrast — it only lets some colour survive.
    s = HDR_SATURATION
    if white is None:
        white = hdr_white([comp])[0]
    rgb = _rolloff_rgb(comp, white=white, knee=knee, contrast=contrast, desat=desat * s,
                       black=black, onset=onset, blue_desat=blue_desat * s,
                       cyan_strip=cyan_strip * s, pink_strip=pink_strip * s, lift=HDR_LIFT)
    # The curve is asymptotic, so across the physical range it lands at DN 254.99 at worst
    # and the uint8 truncation alone would hold — but only across the physical range: past
    # about 10x the white point the shoulder's exp() underflows and it pins at exactly 1.0.
    # This clamp is what turns "no DN 0, no DN 255" into a guarantee that does not depend on
    # how absurd an atmospheric-correction overshoot gets. DN 0 matters twice over: nodata
    # is 0, so a valid pixel written as 0 would punch a transparent HOLE exactly where the
    # deepest shadow is. NaN passes through clip untouched and still becomes nodata.
    _write_rgb(rgb.clip(1.0, 254.0), path, src_crs)


def _highlight_natural(comp, path, src_crs, contrast=1.0):
    """Write the "Highlight Optimized Natural Color" rendering of the true-colour bands.

    This reproduces the Sentinel Hub custom script of that name (author Marko
    Repše, CC BY-SA 4.0) — the same look the Copernicus Browser offers. Instead
    of our linear 0-0.3 stretch it applies a cube-root tone curve per band,
        value = cbrt(0.6 * reflectance)  clipped to 0-1,
    which lifts shadow detail and compresses highlights so a single stretch reads
    well across the scene's whole dynamic range (less blown-out snow/cloud, more
    texture in dark forest/water). It is NOT a separate data download — just a
    second rendering of the same composite bands. This is the S2-L2A form; our
    bands are already 0-1 surface reflectance, so it applies as-is to Sentinel-2
    and approximately to Landsat too.

    Because the power law curves everywhere, it costs global contrast to buy that
    even detail: it crosses over the plain linear stretch at ~0.127 reflectance,
    brightening everything below and darkening everything above, which is what
    reads as a slightly milky midtone. `contrast` puts a little of that back.
    """
    rgb = comp.sel(band=["red", "green", "blue"]).clip(0, None)
    rgb = np.cbrt(0.6 * rgb).clip(0, 1)
    rgb = _contrast(rgb, contrast)
    rgb = (rgb * 255).fillna(0).astype("uint8")
    rgb.attrs = {}    # see _rgb: stale float-nodata / 4-band long_name would break the write
    _to_tif(rgb, path, src_crs, nodata=0)


def _linear(comp, path, src_crs, white=WHITE, black=0.0):
    """Write a plain linear black/white stretch of the true-colour bands — the "None"
    tone mode, with every shaping the other two curves add switched OFF.

    No highlight rolloff, no cube-root, no highlight desaturation, no S-curve contrast:
    just (black..white) reflectance mapped to (0..255) per channel and hard-clipped, the
    same maths as _rgb but with the endpoints exposed. Bright ice therefore CLIPS to flat
    white exactly as a naive stretch would — that is the point. It is the un-toned
    reference for reading what the surface reflectance itself looks like, without the
    render making a highlight/contrast decision on your behalf.

    `white`/`black` default to WHITE (0.30) and 0.0 — the same window _rgb uses — and are
    the only knobs: the plugin's Manual-stretch spin boxes drive them. The auto-stretch is
    deliberately NOT wired in here (a scene-fitted stretch is itself a processing choice
    this mode exists to turn off), and `contrast` is not a parameter at all.
    """
    refl = comp.sel(band=["red", "green", "blue"]).clip(0, None)
    rgb = ((refl - black) / max(white - black, 1e-6)).clip(0, 1) * 255
    if black > 0:
        # nodata is 0, so floor valid pixels at 1 lest a black-clipped scar punch a
        # transparent hole in the layer (see _highlight_rolloff). NaN stays nodata.
        rgb = rgb.clip(1.0, 255.0)
    rgb = rgb.fillna(0).astype("uint8")
    rgb.attrs = {}    # see _rgb: stale float-nodata / 4-band long_name would break the write
    _to_tif(rgb, path, src_crs, nodata=0)


def _date_tag(dates):
    """Filename tag for the acquisition date(s) behind one side's composite.

    One scene (the usual case, and always with --auto-window) -> '2023-08-09'.
    Several scenes composited together -> '2023-08-05_to_2023-08-09', the span
    they cover, so the layer name can't imply a single acquisition it isn't.
    Empty/absent dates -> '' and the caller falls back to an undated filename
    (a composite from a source that doesn't report dates, e.g. an older cache).
    """
    uniq = sorted({d for d in (dates or []) if d})
    if not uniq:
        return ""
    return uniq[0] if len(uniq) == 1 else f"{uniq[0]}_to_{uniq[-1]}"


def _tagged(base, side, tag, kind):
    """'<base>_<side>_<date>_<kind>.tif' — the date dropped when there isn't one."""
    return f"{base}_{side}_{tag}_{kind}.tif" if tag else f"{base}_{side}_{kind}.tif"


def _change_path(base, kind, pre_tag, post_tag):
    """'<base>_<kind>_<pre>_vs_<post>.tif' for a pre->post change raster.

    Both dates go in the name: a change layer is only meaningful as a pair, and
    the pair is what you need to know when reading it (a 3-day dNDVI and a
    3-month one are different measurements)."""
    if pre_tag and post_tag:
        return f"{base}_{kind}_{pre_tag}_vs_{post_tag}.tif"
    return f"{base}_{kind}.tif"


def export_review_package(out_dir, event_id, img, src_crs, near_pt, scenes=None):
    """Write the per-event review layers QGIS opens. Returns the list of layer paths.

    img: the dict returned by imagery.fetch_event / planet_imagery.fetch_event
         (pre, post, ndvi_pre/post, dndvi, dbright composites on a shared grid;
         Sentinel-2/Landsat also carry swir1/swir2 bands and a dndsi composite —
         PlanetScope has no SWIR, so those keys are absent/None and skip cleanly).
         `pre_dates`/`post_dates` (acquisition dates of the scenes composited per
         side) go into the filenames; a source that omits them still exports, just
         with undated names.
         A ONE-SIDED run (only `pre` or only `post`, from hand-picking scenes on a
         single side) exports that side's products and skips the other side plus
         the three change rasters, which are differences.
    near_pt: (x, y) predicted epicentre in src_crs, written as the search point.
    scenes: which products to write — a subset of SCENE_KEYS. None/empty -> all.
            The predicted-point layer is always written regardless.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, event_id)
    want = set(scenes) if scenes else set(SCENE_KEYS)
    # acquisition dates of the composited scenes -> into every layer's filename,
    # which is what QGIS shows as the layer name (see dock._load_layers)
    pre_tag = _date_tag(img.get("pre_dates"))
    post_tag = _date_tag(img.get("post_dates"))
    layers = []
    tags = {"pre": pre_tag, "post": post_tag}

    def write_sides(kind, render, source="", when=None, tolerate=()):
        """Write a two-sided product for whichever sides this run actually has.

        `source` names the img key holding the array ('' = the side's composite,
        e.g. 'ndvi_' for ndvi_pre/ndvi_post). A one-sided run (see
        imagery.fetch_event) simply exports one file instead of two; `when` gates a
        side further (the SWIR products need SWIR bands in that composite).

        `tolerate` names exception types that skip just that side, with a line in
        the run log, instead of failing the whole package. Default () catches
        nothing, so every other product still fails loudly. Only the HDR render
        passes anything: it is the one product heavy enough to run a machine out of
        memory on a large AOI, and an opt-in picture must never take the change
        rasters — the layers the run exists to produce — down with it."""
        for side in ("pre", "post"):
            arr = img.get(f"{source}{side}")
            if arr is None or (when is not None and not when(arr)):
                continue
            path = _tagged(base, side, tags[side], kind)
            try:
                render(arr, path)
            except tolerate as e:
                print(f"    [{kind}] {side}: skipped — {type(e).__name__}: {e}")
                continue
            layers.append(path)

    def has_swir(comp):
        return "swir1" in list(comp.coords["band"].values)

    # true-colour pre/post — context and false-positive checks (clearcut, burn, cloud)
    if "true_color" in want:
        write_sides("rgb", lambda c, p: _rgb(c, ["red", "green", "blue"], p, src_crs))

    # Highlight Optimized Natural Color pre/post — same bands, cube-root tone curve
    if "highlight_natural" in want:
        write_sides("highlight", lambda c, p: _highlight_natural(c, p, src_crs))

    # HDR pre/post — the same true-colour bands as HONC, run through ONE scene-fitted
    # tone curve (shoulder + log shadow lift, see HDR_LIFT) instead of the cube root, so a
    # frame holding both deep shadow and sunlit snow keeps detail at BOTH ends. Nothing in
    # the chain clips: the curve cannot reach either endpoint, so no valid pixel comes out
    # at DN 0 or DN 255 on any scene. The white anchor is fitted ONCE across both sides
    # (hdr_white) so the before/after pair stays photometrically comparable under a swipe —
    # a per-side fit would put a brightness step between them that no change on the ground
    # produced. Measured against HONC on the surface table: ~92% of its shadow separation
    # kept, and the snow range goes from a 4 DN white smear to 29 DN of readable texture.
    if "highlight_hdr" in want:
        hdr_w, hdr_note = hdr_white([img.get("pre"), img.get("post")])
        print(f"    [hdr] {hdr_note}")
        write_sides("hdr", lambda c, p: _highlight_hdr(c, p, src_crs, white=hdr_w),
                    tolerate=(MemoryError,))

    # false-colour (NIR-red-green) — vegetation pops bright red, fresh bare scar reads dark
    if "false_color" in want:
        write_sides("falsecolor",
                    lambda c, p: _rgb(c, ["nir", "red", "green"], p, src_crs))

    # SWIR false colour (S2 12-11-4 / Landsat swir2-swir1-red) — fresh rock/ice-
    # avalanche debris on snow reads bright orange/brown against dark-blue snow, the
    # highest-contrast band combo for spotting debris on a glacier. Guarded on the
    # SWIR bands so a SWIR-less composite (older cache/manual path) just skips it.
    if "swir_falsecolor" in want:
        write_sides("swir", lambda c, p: _rgb(c, ["swir2", "swir1", "red"], p, src_crs),
                    when=has_swir)

    # raw NDVI pre/post — the inputs behind dNDVI, handy for thresholding by eye
    if "ndvi" in want:
        write_sides("ndvi", source="ndvi_",
                    render=lambda a, p: _to_tif(a.rename("ndvi"), p, src_crs))

    # change rasters — where a scar lights up. Each is a pre->post DIFFERENCE, so
    # all three are None (and skipped) on a one-sided run.
    if "dndvi" in want and img.get("dndvi") is not None:
        path = _change_path(base, "dndvi", pre_tag, post_tag)
        _to_tif(img["dndvi"], path, src_crs)
        layers.append(path)
    # NDSI change — new dark debris on snow/ice drives NDSI down, so a strong
    # NEGATIVE dNDSI is the debris-on-glacier signal (works where there is no
    # vegetation for dNDVI to catch). None when the composite carried no SWIR.
    if "dndsi" in want and img.get("dndsi") is not None:
        path = _change_path(base, "dndsi", pre_tag, post_tag)
        _to_tif(img["dndsi"], path, src_crs)
        layers.append(path)
    if "dbright" in want and img.get("dbright") is not None:
        path = _change_path(base, "dbright", pre_tag, post_tag)
        _to_tif(img["dbright"], path, src_crs)
        layers.append(path)

    # the predicted epicentre to search around (digitize the scar against it)
    pt = gpd.GeoDataFrame([{"geometry": Point(near_pt), "kind": "predicted"}], crs=src_crs)
    pt.to_file(f"{base}_point.gpkg", driver="GPKG")
    layers.append(f"{base}_point.gpkg")

    return layers


def write_metadata(out_dir, ev, img, layers):
    """Write <event>_metadata.json describing the scenes used. Returns its path."""
    os.makedirs(out_dir, exist_ok=True)
    meta = dict(
        event_id=ev["event_id"],
        datetime_utc=ev.get("datetime_utc"),
        lat=ev.get("lat"), lon=ev.get("lon"),
        loc_source=ev.get("loc_source"),
        search_radius_km=ev.get("search_radius_km"),
        sensor=img["sensor"],
        n_pre_scenes=len(img["pre_scenes"]),
        n_post_scenes=len(img["post_scenes"]),
        pre_scenes=img["pre_scenes"],
        post_scenes=img["post_scenes"],
        # acquisition dates of those scenes — the same dates the layer filenames
        # carry, listed per scene here rather than collapsed to a span
        pre_dates=img.get("pre_dates"),
        post_dates=img.get("post_dates"),
        layers=layers,
    )
    path = os.path.join(out_dir, f"{ev['event_id']}_metadata.json")
    with open(path, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return path
