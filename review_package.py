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
SCENE_KEYS = ["true_color", "highlight_natural", "false_color", "swir_falsecolor",
              "dndsi", "ndvi", "dndvi", "dbright"]

# Selectable tone curves for the PlanetScope SR detail render — see _highlight_rolloff
# and _highlight_natural. The plugin's PlanetScope tab builds its tone combo box from
# this dict and passes the key through run_single.py --planet-tone; the value is the
# label shown. Ordered rolloff-first: it is the DEFAULT, because the review task is
# reading scars on terrain, where full-brightness midtones matter more than texture
# inside bright ice.
TONE_MODES = {
    "knee": "Highlight rolloff — untouched midtones, tamed highlights",
    "natural": "Highlight Optimized Natural Color — even detail, softer contrast",
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


def _rgb(comp, bands, path, src_crs):
    """Write a 3-band uint8 GeoTIFF from reflectance bands, stretched 0-0.3 -> 0-255."""
    rgb = comp.sel(band=bands)
    rgb = (rgb.clip(0, 0.3) / 0.3 * 255).fillna(0).astype("uint8")
    # Drop source attrs that no longer match a 3-band uint8 image: masked-read
    # sources (e.g. Planet) carry a float NaN nodata that can't cast to uint8 and
    # a 4-band 'long_name' that trips rioxarray's band-name check.
    rgb.attrs = {}
    rgb.rio.write_crs(src_crs).rio.write_nodata(0).rio.to_raster(path, driver="GTiff")


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


def _highlight_rolloff(comp, path, src_crs, white=WHITE, knee=KNEE, contrast=1.0,
                       desat=DESAT, black=0.0, onset=DESAT_ONSET):
    """Write the "highlight rolloff" rendering of the true-colour bands.

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
    """
    refl = comp.sel(band=["red", "green", "blue"]).clip(0, None)
    rgb = (refl - black) / max(white - black, 1e-6)
    rmax = refl.max(dim="band")        # reflectance, for the physical desat gate
    m = rgb.max(dim="band")            # normalised, drives the shoulder gain
    # m.where(m != 0, 1.0) only guards the divide; _shoulder(0)==0 keeps those pixels 0.
    # Below a nonzero black point m goes negative, where _shoulder is the identity, so
    # sh/m is exactly 1 and those pixels pass through to be clipped to black below.
    sh = _shoulder(m, knee)
    rgb = rgb * (sh / m.where(m != 0, 1.0))
    # Fade toward `sh` — the max channel's own post-gain value — so a compressed
    # highlight walks to WHITE at its own brightness rather than to grey.
    rgb = rgb + (sh - rgb) * _desat_weight(rmax, onset, desat)
    rgb = _contrast(rgb.clip(0, 1), contrast) * 255
    if black > 0:
        # nodata is 0, so a valid pixel driven to DN 0 by the black point would punch a
        # transparent HOLE in the layer exactly where a dark scar is. Floor the valid
        # pixels at 1; NaN passes through np.clip untouched and still becomes nodata.
        rgb = rgb.clip(1.0, 255.0)
    rgb = rgb.fillna(0).astype("uint8")
    rgb.attrs = {}    # see _rgb: stale float-nodata / 4-band long_name would break the write
    rgb.rio.write_crs(src_crs).rio.write_nodata(0).rio.to_raster(path, driver="GTiff")


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
    rgb.rio.write_crs(src_crs).rio.write_nodata(0).rio.to_raster(path, driver="GTiff")


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

    def pair(kind):
        """(pre_path, post_path) for a two-sided product, dated per side."""
        return _tagged(base, "pre", pre_tag, kind), _tagged(base, "post", post_tag, kind)

    # true-colour pre/post — context and false-positive checks (clearcut, burn, cloud)
    if "true_color" in want:
        pre_p, post_p = pair("rgb")
        _rgb(img["pre"], ["red", "green", "blue"], pre_p, src_crs)
        _rgb(img["post"], ["red", "green", "blue"], post_p, src_crs)
        layers += [pre_p, post_p]

    # Highlight Optimized Natural Color pre/post — same bands, cube-root tone curve
    if "highlight_natural" in want:
        pre_p, post_p = pair("highlight")
        _highlight_natural(img["pre"], pre_p, src_crs)
        _highlight_natural(img["post"], post_p, src_crs)
        layers += [pre_p, post_p]

    # false-colour (NIR-red-green) — vegetation pops bright red, fresh bare scar reads dark
    if "false_color" in want:
        pre_p, post_p = pair("falsecolor")
        _rgb(img["pre"], ["nir", "red", "green"], pre_p, src_crs)
        _rgb(img["post"], ["nir", "red", "green"], post_p, src_crs)
        layers += [pre_p, post_p]

    # SWIR false colour (S2 12-11-4 / Landsat swir2-swir1-red) — fresh rock/ice-
    # avalanche debris on snow reads bright orange/brown against dark-blue snow, the
    # highest-contrast band combo for spotting debris on a glacier. Guarded on the
    # SWIR bands so a SWIR-less composite (older cache/manual path) just skips it.
    if "swir_falsecolor" in want and "swir1" in list(img["pre"].coords["band"].values):
        pre_p, post_p = pair("swir")
        _rgb(img["pre"], ["swir2", "swir1", "red"], pre_p, src_crs)
        _rgb(img["post"], ["swir2", "swir1", "red"], post_p, src_crs)
        layers += [pre_p, post_p]

    # raw NDVI pre/post — the inputs behind dNDVI, handy for thresholding by eye
    if "ndvi" in want:
        pre_p, post_p = pair("ndvi")
        img["ndvi_pre"].rename("ndvi").rio.write_crs(src_crs).rio.to_raster(
            pre_p, driver="GTiff")
        img["ndvi_post"].rename("ndvi").rio.write_crs(src_crs).rio.to_raster(
            post_p, driver="GTiff")
        layers += [pre_p, post_p]

    # change rasters — where a scar lights up
    if "dndvi" in want:
        path = _change_path(base, "dndvi", pre_tag, post_tag)
        img["dndvi"].rio.write_crs(src_crs).rio.to_raster(path, driver="GTiff")
        layers.append(path)
    # NDSI change — new dark debris on snow/ice drives NDSI down, so a strong
    # NEGATIVE dNDSI is the debris-on-glacier signal (works where there is no
    # vegetation for dNDVI to catch). None when the composite carried no SWIR.
    if "dndsi" in want and img.get("dndsi") is not None:
        path = _change_path(base, "dndsi", pre_tag, post_tag)
        img["dndsi"].rio.write_crs(src_crs).rio.to_raster(path, driver="GTiff")
        layers.append(path)
    if "dbright" in want and img.get("dbright") is not None:
        path = _change_path(base, "dbright", pre_tag, post_tag)
        img["dbright"].rio.write_crs(src_crs).rio.to_raster(path, driver="GTiff")
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
