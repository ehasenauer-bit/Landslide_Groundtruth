"""Fade radar-layover slopes in the SAR display.

With single-geometry coverage (common in interior Alaska — see the descending-
only Denali case), the slopes facing the radar illumination are foreshortened /
laid over and pile up energy, reading as artificially HIGH amplitude that isn't
ground signal. This module builds a per-pixel opacity (alpha) that dims those
pixels so they stop dominating the map.

Geometry-aware, not hard-coded to "east": Sentinel-1 is right-looking, so the
illumination arrives from —
  descending pass → from the EAST  → east-facing slopes lay over (bright)
  ascending  pass → from the WEST  → west-facing slopes lay over (bright)
so the layover-facing aspect is picked from the scene's orbit_state. "East" and
"west" are only the latitude-free approximation: at 60°N the real look at the
target is ~10° off it (the tilted ground track, minus the meridian convergence
between nadir and the target), and every function here that is given a latitude
uses the true geometry (look_azimuth_deg) instead.

A pixel is dimmed only when it is (a) on a slope FACING the illumination, (b)
STEEP enough to actually foreshorten/lay over (flat ground of any aspect is
left alone), and (c) HIGH-valued (the actual layover pile-up / strong change),
per the user's choice. Pure numpy so it unit-tests without QGIS/GDAL.
"""
import numpy as np

__all__ = ["ground_heading_deg", "look_azimuth_deg", "illumination_aspect_deg",
           "range_azimuth_deg", "iw_incidence_deg", "slope_aspect_deg",
           "metric_pixel_size", "layover_alpha", "radar_shadow",
           "IW_INCIDENCE_DEG", "IW_NEAR_DEG", "IW_FAR_DEG"]

# Sentinel-1 IW incidence angle, mid-swath — now only the FALLBACK. The real
# value runs 29.1° at near range to 46.0° at far range and `sentinel-1-rtc`
# carries neither a per-pixel incidence band nor a scene-level angle, but the
# scene footprint says where a point sits across the swath, and iw_incidence_deg
# turns that into an angle. Use this constant only when there is no footprint.
# It matters: shadow starts at (90° - incidence), i.e. 61° slopes at near range
# but 44° at far range, and the shadowed fraction of an AOI swings ~70x across
# the swath (Iliamna 0.03% at 29°, 2.1% at 46°).
IW_INCIDENCE_DEG = 39.0
IW_NEAR_DEG, IW_FAR_DEG = 29.1, 46.0

# Sentinel-1's orbit, for the look DIRECTION. Sun-synchronous at 98.18° and
# ~693 km, so the ground track is not north-south: it leans west of north on an
# ascending pass, by an amount that grows with latitude, and the Earth turning
# underneath leans it further. Net of the convergence back at the target (see
# look_azimuth_deg), treating the look as due east/west put it ~10° wrong at
# 60°N — on real Iliamna terrain that alone moved ~22% of the shadow mask.
S1_INCLINATION_DEG = 98.18
_S1_ALTITUDE_KM = 693.0
_EARTH_RADIUS_KM = 6371.0
# speed of the SUB-SATELLITE point in the inertial frame: 7.5 km/s orbital,
# scaled down to the ground by R / (R + h)
_S1_GROUND_SPEED_KMS = 7.5 * _EARTH_RADIUS_KM / (_EARTH_RADIUS_KM + _S1_ALTITUDE_KM)
_EARTH_SURFACE_KMS = 0.4651          # equatorial rotation speed


def _direction(orbit_state):
    """'asc' / 'desc' from whatever the STAC item carried, else None."""
    s = str(orbit_state or "").strip().lower()
    return "asc" if s.startswith("asc") else "desc" if s.startswith("desc") else None


def ground_heading_deg(orbit_state, lat):
    """Compass heading of the Sentinel-1 ground track at latitude `lat`, or None.

    Spherical orbit geometry, not a lookup: the inertial heading of a circular
    orbit of inclination i at latitude φ satisfies sin ψ = cos i / cos φ (the
    track is most tilted near the orbit's turning latitude, 81.8° for S1), and
    the ground heading is the direction of the satellite's velocity RELATIVE to
    the ground, i.e. minus the eastward speed of the Earth's surface at φ.

    ~348° / ~192° at the equator, ~342° / ~198° at 60°N. This is the
    heading OF THE NADIR TRACK, and the target is not on it: see
    look_azimuth_deg for the correction that matters at the target. Checked
    against two real Iliamna footprints, whose track-parallel edges extrapolate
    back to nadir at ~343° for this model's 341.6°.
    """
    d = _direction(orbit_state)
    if d is None or lat is None:
        return None
    phi = np.radians(float(lat))
    ratio = np.cos(np.radians(S1_INCLINATION_DEG)) / max(np.cos(phi), 1e-9)
    psi = float(np.arcsin(np.clip(ratio, -1.0, 1.0)))     # ascending: west of north
    h = psi if d == "asc" else np.pi - psi
    east = _S1_GROUND_SPEED_KMS * np.sin(h) - _EARTH_SURFACE_KMS * np.cos(phi)
    north = _S1_GROUND_SPEED_KMS * np.cos(h)
    return float(np.degrees(np.arctan2(east, north)) % 360.0)


def look_azimuth_deg(orbit_state, lat=None, incidence_deg=None):
    """Compass azimuth the radar LOOKS along AT THE TARGET — from the sensor out
    across the ground, the direction of increasing ground range. None if unknown.

    Right-looking, so it is 90° clockwise of the along-track direction — but the
    along-track direction AT THE TARGET, not at nadir. The target sits 340-620 km
    across-track from the nadir point, and over that many degrees of longitude
    local north itself rotates (meridian convergence, Δλ·sin φ): the target
    lies to the right of the track, i.e. east of it ascending and west of it
    descending, so the track-parallel direction there is turned by
    ± x·tan φ / R for a cross-track ground range x. At 60°N mid-swath that is
    ~7.5°, and leaving it out over-corrects the old due-E/W assumption by about
    as much as the tilt it was fixing. With it the model matches the track-
    parallel edges of real Iliamna footprints to within ~0.5°, where due-E/W
    is ~10° out and the nadir heading alone ~8° out the other way.

    `incidence_deg` places the target across the swath (the same information:
    see iw_incidence_deg); without it the target is assumed at IW_INCIDENCE_DEG.
    ~79° (ENE) ascending and ~281° (WNW) descending at 60°N.

    Without a latitude it falls back to the old due-east / due-west (90° / 270°)
    approximation — what a caller with only a projected grid and no lat_hint
    gets. Second-order terms left out: the nadir point is ~1° of latitude south
    of the target (~0.5° of heading), and the ellipsoid.
    """
    d = _direction(orbit_state)
    if d is None:
        return None
    head = ground_heading_deg(d, lat)
    if head is None:
        return 90.0 if d == "asc" else 270.0
    theta = IW_INCIDENCE_DEG if incidence_deg is None else float(incidence_deg)
    x_km = _ground_range_at_incidence(theta)
    turn = np.degrees(x_km * np.tan(np.radians(float(lat))) / _EARTH_RADIUS_KM)
    along = head + (turn if d == "asc" else -turn)
    return float((along + 90.0) % 360.0)


def illumination_aspect_deg(orbit_state, lat=None, incidence_deg=None):
    """Compass aspect (deg, 0=N 90=E 180=S 270=W) of the slopes that FACE the
    radar and therefore lay over — the look azimuth turned around. None if
    unknown, and the caller then does not dim anything (no reliable geometry).

    Descending is lit from the east and ascending from the west; with a latitude
    that becomes ~101° / ~259° at 60°N rather than exactly 90° / 270°."""
    look = look_azimuth_deg(orbit_state, lat, incidence_deg)
    return None if look is None else (look + 180.0) % 360.0


def _incidence_at_ground_range(x_km):
    """Incidence angle (deg) at ground distance `x_km` from nadir, on a sphere.
    The look angle α off nadir and the Earth-central angle γ = x/R sum to it."""
    g = float(x_km) / _EARTH_RADIUS_KM
    rs = _EARTH_RADIUS_KM + _S1_ALTITUDE_KM
    alpha = np.arctan2(_EARTH_RADIUS_KM * np.sin(g), rs - _EARTH_RADIUS_KM * np.cos(g))
    return float(np.degrees(alpha + g))


def _ground_range_at_incidence(theta_deg):
    """Inverse of _incidence_at_ground_range, by bisection (it is monotonic)."""
    lo, hi = 0.0, 1500.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _incidence_at_ground_range(mid) < theta_deg:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# where the IW swath's two edges sit on the ground, from nadir: ~343 and ~617
# km on this sphere, a 274 km swath. Real footprints measure ~252 km (two
# Iliamna scenes, geodesic), essentially ESA's published 250 km, so the model is
# ~8% wide: the footprint edges probably sit a little inside 29.1/46.0, or the
# altitude/sphere simplification shows. iw_incidence_deg only uses the target's
# FRACTIONAL position across the footprint, so this costs up to ~1 deg right at
# the swath edges and nothing at mid-swath.
_IW_NEAR_KM = _ground_range_at_incidence(IW_NEAR_DEG)
_IW_FAR_KM = _ground_range_at_incidence(IW_FAR_DEG)


def _outer_ring(geometry):
    """Outer ring [(lon, lat), ...] of a GeoJSON Polygon / MultiPolygon, or []."""
    try:
        coords = geometry["coordinates"]
        ring = coords[0][0] if geometry["type"] == "MultiPolygon" else coords[0]
        return [(float(x), float(y)) for x, y, *_ in ring]
    except (KeyError, TypeError, IndexError, ValueError):
        return []


def _bearing_km(lon0, lat0, lon1, lat1):
    """(initial bearing deg, great-circle distance km) from point 0 to point 1.

    Footprint corners are up to ~300 km from the target, far enough that a flat
    lon/lat projection using the TARGET's local north puts them a few degrees
    off in bearing — the same meridian convergence look_azimuth_deg corrects
    for, at a smaller scale."""
    p0, p1 = np.radians(lat0), np.radians(lat1)
    dl = np.radians(((lon1 - lon0 + 180.0) % 360.0) - 180.0)   # antimeridian-safe
    y = np.sin(dl) * np.cos(p1)
    x = np.cos(p0) * np.sin(p1) - np.sin(p0) * np.cos(p1) * np.cos(dl)
    a = np.sin((p1 - p0) / 2) ** 2 + np.cos(p0) * np.cos(p1) * np.sin(dl / 2) ** 2
    dist = 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(min(1.0, a)))
    return float(np.degrees(np.arctan2(y, x)) % 360.0), float(dist)


def iw_incidence_deg(footprint, lon, lat, orbit_state):
    """Incidence angle (deg) at (lon, lat) inside an IW scene, or None.

    `footprint` is the scene's GeoJSON geometry (the STAC item's `geometry`,
    which the SAR tab's candidates already carry). Its extremes along the look
    direction are the swath's near and far edges; where the point falls between
    them fixes its ground range, and the spherical model turns that into an
    angle. Only the EXTREMES are used, so an irregular or truncated footprint —
    the last slice of a datatake — still measures the full swath correctly.

    None when the orbit direction or footprint is missing, or when the footprint
    does not span something swath-shaped along the look direction (then the
    caller should fall back to IW_INCIDENCE_DEG rather than trust a number).
    Longitudes are unwrapped around `lon`, so a scene over the Aleutians that
    straddles the antimeridian measures the same as any other.
    """
    ring = _outer_ring(footprint) if footprint else []
    if _direction(orbit_state) is None or len(ring) < 3 or lat is None:
        return None
    swath = _IW_FAR_KM - _IW_NEAR_KM
    # every corner as (bearing, distance) from the target, measured in the
    # target's own frame — an azimuthal-equidistant projection centred on it
    polar = [_bearing_km(lon, lat, x, y) for x, y in ring]
    # The look direction at the target depends on where the target sits across
    # the swath (look_azimuth_deg), which is what this is measuring. Two passes
    # settle it: a corner ~85 km along-track moves by 85 km x sin(error), so the
    # first pass's ~0.1 deg residual shifts the answer by ~0.01 deg.
    inc = None
    for _ in range(2):
        look = look_azimuth_deg(orbit_state, lat, inc)
        proj = [d * np.cos(np.radians(b - look)) for b, d in polar]
        near, far = min(proj), max(proj)
        if not 0.6 * swath <= far - near <= 1.5 * swath:
            return None
        frac = float(np.clip(-near / (far - near), 0.0, 1.0))
        inc = _incidence_at_ground_range(_IW_NEAR_KM + frac * swath)
    return inc


def _grid_lat(gt, n_rows, lat_hint=None):
    """Latitude for the look geometry: `lat_hint` when given, else the centre of
    a geographic (degrees) grid, else None — a projected grid carries no
    latitude of its own, and guessing one would be worse than the E/W fallback."""
    if lat_hint is not None:
        return float(lat_hint)
    if abs(gt[1]) < 0.5:
        return float(gt[3] + gt[5] * (n_rows / 2.0))
    return None


def slope_aspect_deg(dem, dx_m, dy_m):
    """(slope_deg, aspect_deg) for a north-up DEM (row 0 = north). Aspect is the
    compass direction the surface FACES downhill (0=N, 90=E, 180=S, 270=W)."""
    dem = np.asarray(dem, dtype=np.float64)
    gy_row = np.gradient(dem, float(dy_m), axis=0)   # dz/d(row); row increases south
    gx_col = np.gradient(dem, float(dx_m), axis=1)   # dz/d(col); col increases east
    # downhill vector in map coords: east = -dz/deast = -gx_col; north = -dz/dnorth
    # and dz/dnorth = -gy_row, so downhill_north = gy_row
    aspect = np.degrees(np.arctan2(-gx_col, gy_row)) % 360.0
    slope = np.degrees(np.arctan(np.hypot(gx_col, gy_row)))
    return slope.astype(np.float32), aspect.astype(np.float32)


def metric_pixel_size(gt, n_rows, lat_hint=None):
    """(dx_m, dy_m) ground pixel size from a GDAL geotransform. Handles a
    geographic (degrees) grid by scaling with latitude, or a projected (metres)
    grid directly."""
    px_w, px_h = abs(gt[1]), abs(gt[5])
    if px_w < 0.5:                       # degrees (geographic)
        lat = lat_hint
        if lat is None:
            lat = gt[3] + gt[5] * (n_rows / 2.0)   # centre latitude
        dx = px_w * 111320.0 * max(0.05, np.cos(np.radians(lat)))
        dy = px_h * 110540.0
        return float(dx), float(dy)
    return float(px_w), float(px_h)      # already metres


def _angular_gap(a, b):
    """Smallest absolute compass angle between arrays a and scalar b (degrees)."""
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


def _box_mean(a, k):
    """Fast k×k edge-padded box mean via an integral image (no scipy). Used to
    (1) smooth the DEM before slope/aspect — bilinear-upsampled tiles come out as
    flat facets whose derivatives terrace into blocky aspect — and (2) feather the
    dim mask so its opacity ramps over a few pixels instead of a hard 25%/100%
    edge (the 'blocky artifact')."""
    a = np.asarray(a, dtype=np.float64)
    if k <= 1:
        return a
    pad = k // 2
    ap = np.pad(a, ((pad, k - 1 - pad), (pad, k - 1 - pad)), mode="edge")
    ii = np.zeros((ap.shape[0] + 1, ap.shape[1] + 1))
    ii[1:, 1:] = ap.cumsum(0).cumsum(1)
    return (ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]) / (k * k)


def layover_alpha(value, valid, dem, gt, orbit_state, *, lat_hint=None,
                  mode="amplitude", thr=None, high_percentile=75.0,
                  dim=0.25, slope_min_deg=12.0, aspect_tol_deg=55.0,
                  dem_smooth=3, feather=5):
    """Per-pixel opacity in [dim, 1] (float32): `dim` where a pixel is layover-
    facing AND steep AND high-valued, else 1.0.

    value/valid/dem must share the same grid. `orbit_state` picks the layover
    aspect; if it is unknown the whole array is opaque (no dimming). `mode`:
      'amplitude' → high = value ≥ its `high_percentile` among valid pixels
      'change'    → high = |value| ≥ `thr` (the detector's significance cut)
    Returns (alpha, meta). meta reports the layover aspect and dimmed-pixel count.
    """
    value = np.asarray(value, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    alpha = np.ones(value.shape, dtype=np.float32)

    lay_aspect = illumination_aspect_deg(
        orbit_state, _grid_lat(gt, value.shape[0], lat_hint))
    if lay_aspect is None:
        return alpha, {"orbit_state": orbit_state, "layover_aspect": None,
                       "n_dimmed": 0, "note": "unknown orbit — not dimmed"}

    dx, dy = metric_pixel_size(gt, value.shape[0], lat_hint)
    # smooth the DEM first: a bilinear-upsampled tile is a lattice of flat facets
    # whose slope/aspect derivatives terrace into blocky patches
    dem_s = _box_mean(dem, dem_smooth) if dem_smooth and dem_smooth > 1 else dem
    slope, aspect = slope_aspect_deg(dem_s, dx, dy)
    facing = _angular_gap(aspect, lay_aspect) <= aspect_tol_deg
    steep = slope >= slope_min_deg
    if mode == "change":
        t = abs(float(thr)) if thr is not None else 0.0
        high = np.abs(value) >= t
    else:
        finite = value[valid & np.isfinite(value)]
        cut = np.percentile(finite, high_percentile) if finite.size else np.inf
        high = value >= cut
    dim_mask = valid & facing & steep & high & np.isfinite(slope)
    # feather the hard dim/keep edge so opacity ramps over a few pixels instead of
    # a blocky 25%/100% step; strength in [0,1] → alpha in [dim, 1]
    if feather and feather > 1:
        strength = np.clip(_box_mean(dim_mask.astype(np.float64), feather), 0.0, 1.0)
    else:
        strength = dim_mask.astype(np.float64)
    alpha = (1.0 - strength * (1.0 - float(dim))).astype(np.float32)
    return alpha, {"orbit_state": str(orbit_state), "layover_aspect": lay_aspect,
                   "n_dimmed": int(dim_mask.sum()),
                   "note": f"dimmed layover-facing (aspect~{lay_aspect:.0f}°) "
                           f"steep high-value pixels to {dim:.0%}"}


def range_azimuth_deg(orbit_state, lat=None, incidence_deg=None):
    """Compass azimuth (deg) the beam TRAVELS across the ground — the direction of
    increasing ground range, pointing AWAY from the sensor. None if unknown.

    The same thing as look_azimuth_deg, named for what radar_shadow marches
    along: ~79° ascending and ~281° descending at 60°N, or exactly 90° / 270°
    without a latitude."""
    return look_azimuth_deg(orbit_state, lat, incidence_deg)


def _shift(a, drow, dcol, fill):
    """`a` translated by (drow, dcol) with `fill` in the vacated border, i.e.
    out[r, c] = a[r - drow, c - dcol]. No wraparound (np.roll would wrap a ridge
    on one edge of the AOI into a shadow on the other)."""
    out = np.full(a.shape, fill, dtype=a.dtype)
    h, w = a.shape
    if abs(drow) >= h or abs(dcol) >= w:
        return out
    out[max(0, drow):h - max(0, -drow), max(0, dcol):w - max(0, -dcol)] = \
        a[max(0, -drow):h - max(0, drow), max(0, -dcol):w - max(0, dcol)]
    return out


def radar_shadow(dem, gt, orbit_state, *, incidence_deg=IW_INCIDENCE_DEG,
                 lat_hint=None, dem_smooth=3, max_steps=400):
    """Boolean mask: True where terrain hides the pixel from the radar.

    What shadow does to a change map is NOT what you might expect, and this was
    built on the wrong expectation first. Measured on a quiet Iliamna pair
    (2026-08-31 -> 09-12, same tracks, no slide), shadowed pixels are ~10x
    QUIETER than lit ground, not noisier: after a 5x5 speckle filter 0.8-0.9% of
    them read as a >=3 dB change against 5.7-14.5% of lit ground, and their
    before-vs-after correlation (0.86-0.91) matches lit ground's. The noise
    floor is steady from pass to pass on one track, so noise over noise is ~1,
    not a heavy-tailed outlier. A shadowed sample therefore rarely wins the
    merge's "strongest anomaly wins", and masking it does not reduce noise (the
    merged background moved 17.91% -> 17.89%).

    What the mask IS for is the merge's confidence label — see
    sar_change.merge_geometries(masks=...). A shadowed pass has not "seen no
    change"; it has not seen the ground at all, and the mask says so.

    Shadow is a deterministic function of terrain and look geometry, so it is
    PREDICTED from the DEM rather than inferred from the pixels — which is the
    only way to find it at all, since `sentinel-1-rtc` leaves shadowed pixels
    finite and ordinary-looking rather than NoData.

    Method (Pairman & McNeill's horizon test, the standard one): the ray from the
    sensor descends toward far range at (90° - incidence) above the horizontal,
    so a pixel at height h is shadowed when some point d metres back TOWARD the
    radar rises above h + d·cot(incidence). Marching outward in one-pixel steps
    and OR-ing that test catches both self-shadow (a slope tilted away by more
    than 90° - incidence) and cast shadow behind a ridge, in one pass. The march
    stops once the ray has climbed past the AOI's total relief, beyond which
    nothing can block it.

    `dem` and `gt` must describe the same grid; `orbit_state` picks the look
    direction, and an unknown one masks nothing (no reliable geometry) rather
    than guessing. Returns (mask, meta).

    Known limit: only terrain INSIDE the DEM can cast a shadow, so a ridge just
    outside the AOI is not seen. Pure numpy, like the rest of this module.
    """
    dem = np.asarray(dem, dtype=np.float32)
    mask = np.zeros(dem.shape, dtype=bool)
    az = range_azimuth_deg(orbit_state, _grid_lat(gt, dem.shape[0], lat_hint),
                           incidence_deg)
    finite = np.isfinite(dem)
    if az is None or not finite.any():
        return mask, {"orbit_state": str(orbit_state), "range_azimuth": az,
                      "n_shadow": 0, "n_steps": 0,
                      "note": ("unknown orbit direction — nothing masked"
                               if az is None else "no usable DEM — nothing masked")}
    theta = float(incidence_deg)
    if not 1.0 <= theta <= 89.0:
        raise ValueError("incidence_deg must be between 1 and 89")
    cot = 1.0 / np.tan(np.radians(theta))

    # same DEM smoothing as layover_alpha: a bilinear-upsampled tile is a lattice
    # of flat facets, and its noise would speckle the mask with one-pixel shadows
    dem_s = _box_mean(dem, dem_smooth) if dem_smooth and dem_smooth > 1 else dem
    lo = float(np.nanmin(dem_s))
    ground = np.where(np.isfinite(dem_s), dem_s, lo).astype(np.float64)

    dx, dy = metric_pixel_size(gt, dem.shape[0], lat_hint)
    step = float(min(dx, dy))
    # unit vector pointing TOWARD the radar = opposite the ground-range direction
    a = np.radians(az)
    east, north = -np.sin(a), -np.cos(a)
    relief = float(ground.max() - ground.min())
    # once the ray has risen by more than the AOI's total relief, no ground left
    # in the scene can reach it — every further step is wasted work
    n_steps = int(min(max_steps, max(1, np.ceil(relief / (cot * step)))))

    for k in range(1, n_steps + 1):
        d = k * step
        # out[r, c] must hold the ground d metres back toward the radar, so the
        # shift is the NEGATIVE of that pixel offset (see _shift)
        drow = int(round(north * d / dy))
        dcol = int(round(-east * d / dx))
        blocker = _shift(ground, drow, dcol, fill=-np.inf)
        mask |= blocker > (ground + d * cot)

    return mask, {
        "orbit_state": str(orbit_state), "range_azimuth": az,
        "incidence_deg": theta, "n_steps": n_steps,
        "n_shadow": int(mask.sum()),
        "frac_shadow": float(mask.mean()),
        "note": (f"look azimuth {az:.0f}°, {theta:.1f}° incidence (slopes "
                 f"tilted >{90.0 - theta:.0f}° away, plus cast shadow): "
                 f"{mask.mean():.1%} of the AOI"),
    }


def as_alpha_band(alpha):
    """float alpha in [0,1] → uint8 0..255 for a GeoTIFF alpha band."""
    return np.clip(np.rint(np.asarray(alpha) * 255.0), 0, 255).astype(np.uint8)


# Public, no-login Copernicus GLO-30 DEM bucket. Tiles are 1°×1°, named by their
# SW-corner integer lat/lon. GDAL reads the COGs directly over /vsicurl — no
# planetary_computer / pystac / requests (none of which QGIS's Python has), only
# osgeo + stdlib urllib, both always present in QGIS.
_COP_DEM_BASE = "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com"


def _cop_dem_tile_url(sw_lat, sw_lon):
    ns = f"N{sw_lat:02d}" if sw_lat >= 0 else f"S{abs(sw_lat):02d}"
    ew = f"E{sw_lon:03d}" if sw_lon >= 0 else f"W{abs(sw_lon):03d}"
    name = f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"
    return f"{_COP_DEM_BASE}/{name}/{name}.tif"


def _url_exists(url, timeout=30):
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _warp_sources_to_grid(gdal, sources, minx, miny, maxx, maxy, width, height):
    """VRT over `sources` (GDAL-openable paths) → warp to the EXACT AOI grid."""
    vrt = gdal.BuildVRT("/vsimem/_layover_dem.vrt", sources)
    if vrt is None:
        return None
    ds = gdal.Warp("", vrt, format="MEM", outputBounds=(minx, miny, maxx, maxy),
                   width=int(width), height=int(height), dstSRS="EPSG:4326",
                   resampleAlg="bilinear")
    try:
        gdal.Unlink("/vsimem/_layover_dem.vrt")
    except Exception:
        pass
    if ds is None:
        return None
    return np.asarray(ds.GetRasterBand(1).ReadAsArray(), dtype=np.float32)


def fetch_dem_on_grid(minx, miny, maxx, maxy, width, height):
    """Copernicus GLO-30 DEM warped to the EXACT AOI grid (EPSG:4326 bbox at the
    given width/height), so it lines up pixel-for-pixel with the SAR renders. Uses
    only osgeo + stdlib (importable inside QGIS, unlike planetary_computer).

    Two strategies over the open, no-login Copernicus DEM bucket:
      1. FAST — GDAL /vsicurl range reads. GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
         stops the sidecar-probing that is the usual cause of a generic /vsicurl
         failure.
      2. ROBUST FALLBACK — download whole tiles with stdlib urllib (which works
         where GDAL's own curl does not: proxy / SSL / offline-curl builds) into
         GDAL's in-memory filesystem, then warp locally.
    A VRT over every intersecting 1°×1° tile handles an AOI that straddles the grid.
    Returns a float32 ndarray, or None if no DEM tile covers the AOI.
    """
    import math
    import urllib.request
    from osgeo import gdal

    tiles = [(la, lo)
             for la in range(math.floor(miny), math.floor(maxy) + 1)
             for lo in range(math.floor(minx), math.floor(maxx) + 1)]
    urls = [u for u in (_cop_dem_tile_url(la, lo) for la, lo in tiles)
            if _url_exists(u)]                        # skip ocean/missing tiles
    if not urls:
        return None

    for k, v in (("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"),
                 ("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif"),
                 ("GDAL_HTTP_MAX_RETRY", "3"), ("GDAL_HTTP_RETRY_DELAY", "1")):
        gdal.SetConfigOption(k, v)

    # 1. fast path
    try:
        arr = _warp_sources_to_grid(gdal, ["/vsicurl/" + u for u in urls],
                                    minx, miny, maxx, maxy, width, height)
        if arr is not None:
            return arr
    except Exception:
        pass                                          # fall through to download

    # 2. robust fallback: stdlib download → GDAL in-memory FS → warp
    mem = []
    try:
        for i, u in enumerate(urls):
            with urllib.request.urlopen(u, timeout=180) as r:
                if getattr(r, "status", 200) != 200:
                    continue
                data = r.read()
            p = f"/vsimem/_layover_dem_{i}.tif"
            gdal.FileFromMemBuffer(p, data)
            mem.append(p)
        if not mem:
            return None
        return _warp_sources_to_grid(gdal, mem, minx, miny, maxx, maxy,
                                     width, height)
    finally:
        for p in mem:
            try:
                gdal.Unlink(p)
            except Exception:
                pass
