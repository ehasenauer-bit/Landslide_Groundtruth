"""Refuse a raster grid that cannot finish, before it is allocated.

Two paths in the plugin build a metric grid from a user-chosen extent and a
user-chosen resolution, and neither checked the product:

  * the 3D tab warps terrain over the search radius (up to 50 km) at the chosen
    resolution (down to 2 m);
  * the Volume tab differences two DEMs over the outline plus a margin.

The controls are independent, so nothing stops a combination that is thousands
of times larger than anything sensible. A 50 km radius at 2 m is a 50 000 ×
50 000 grid — 2.5 billion cells, about 10 GB as float32, before the second array
needed to subtract one from the other. QGIS does not warn, it just stops
responding, and the analyst has no way to tell a wedged run from a slow one.

The numbers below are cell counts, not byte counts, because the caller usually
needs several arrays of the same shape (source, destination, mask, difference)
and the peak is a multiple nobody can predict from here. Cells are the thing the
user can actually act on: halving the resolution quarters them.
"""

# Above this, say it will be slow. ~100 megapixels — a 10 km box at 1 m, or a
# 34 km box at 4 m. (A 34 km box at ArcticDEM's native 2 m is 289 Mpx, so the
# real workflow for a 17 km location error lands in the cautioned band, not the
# refused one — which is the intent: warn, do not block.)
CELLS_WARN = 100_000_000
# Beyond this the operation is refused. ~400 megapixels is already ~1.6 GB per
# float32 array; the real ceiling is the several arrays the callers hold at once.
CELLS_MAX = 400_000_000


def grid_cells(width_m, height_m, res_m):
    """Cell count for an extent at a resolution. 0 if the inputs make no sense."""
    try:
        w, h, r = float(width_m), float(height_m), float(res_m)
        if r <= 0 or w <= 0 or h <= 0:
            return 0
        return int((w / r) + 1) * int((h / r) + 1)
    except (TypeError, ValueError):
        return 0


def describe(width_m, height_m, res_m):
    """'12.5 km × 12.5 km at 2 m — 39 megapixels' for a live caption."""
    cells = grid_cells(width_m, height_m, res_m)
    if not cells:
        return ""
    return (f"{float(width_m) / 1000:.3g} km × {float(height_m) / 1000:.3g} km "
            f"at {float(res_m):g} m — {cells / 1e6:.0f} megapixels")


def _suggest(width_m, height_m, res_m):
    """The two ways out, phrased as the actions the user can actually take.

    Both numbers are CHECKED against grid_cells() before being offered rather
    than solved for algebraically: grid_cells adds one cell per side, so the
    closed-form answer lands a few thousand cells over the cap and the advice
    would be wrong by exactly the amount that makes it useless."""
    import math
    try:
        w, h, r = float(width_m), float(height_m), float(res_m)
        if w <= 0 or h <= 0 or r <= 0:
            return ""
    except (TypeError, ValueError):
        return ""
    # coarsest resolution that fits, starting from the algebraic estimate and
    # stepping until it genuinely does
    coarser = max(r, math.ceil(math.sqrt((w * h) / CELLS_MAX)))
    for _ in range(64):
        if grid_cells(w, h, coarser) <= CELLS_MAX:
            break
        coarser += 1
    # widest square that fits at the CURRENT resolution, FLOORED to 0.1 km before
    # it is printed: rounding the number up (39.996 -> "40") hands back a figure
    # that does not fit, which is the one thing this sentence must not do.
    side = math.sqrt(CELLS_MAX) * r
    for _ in range(64):
        if grid_cells(side, side, r) <= CELLS_MAX:
            break
        side -= r * 2
    side_km = math.floor(side / 100.0) / 10.0
    while side_km > 0.1 and grid_cells(side_km * 1000, side_km * 1000, r) > CELLS_MAX:
        side_km = round(side_km - 0.1, 1)
    return (f"Either coarsen the resolution to about {coarser:g} m, or reduce "
            f"the area to about {side_km:g} km across at {r:g} m.")


def check_grid(width_m, height_m, res_m, what="grid"):
    """(ok, message). ok is False only when the grid is refused.

    A message with ok=True is a caution worth showing but not worth blocking on —
    the caller decides whether to surface it."""
    cells = grid_cells(width_m, height_m, res_m)
    if not cells:
        return True, ""
    if cells > CELLS_MAX:
        return False, (
            f"That {what} would be {cells / 1e6:.0f} megapixels "
            f"({describe(width_m, height_m, res_m)}), which will exhaust memory "
            f"and lock up QGIS. " + _suggest(width_m, height_m, res_m))
    if cells > CELLS_WARN:
        return True, (
            f"Large {what}: {describe(width_m, height_m, res_m)}. This may take "
            f"several minutes and a lot of memory.")
    return True, ""
