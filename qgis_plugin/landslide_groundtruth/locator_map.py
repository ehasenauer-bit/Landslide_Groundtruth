"""The "where on Earth is this" inset for the exported map figure.

The map slide's fourth cell used to repeat the Details block the 3D slide
already carries. A reader who has never heard of the valley in the other three
panels needs the other thing entirely: where this is. This module builds that
panel's geometry — coastlines/borders and a graticule around the event, in a
Lambert azimuthal equal-area projection centred ON the event — so the viewer can
draw the locator with plain canvas 2D and no network. That matters: the exported
HTML is self-contained by design and has to keep opening offline, which rules
out fetching basemap tiles (and would taint the canvas `toDataURL` exports).

Land comes from QGIS's own bundled Natural Earth world map
(`resources/data/world_map.gpkg`), so there is nothing to download and nothing
extra to ship with the plugin.

Pure GDAL/OGR apart from locating that file, so it runs (and is tested) head-less.
"""
import math
import os

from osgeo import ogr, osr

__all__ = ["build_locator", "default_world_map", "HALF_M_DEFAULT"]

HALF_M_DEFAULT = 400000.0          # 800 km across: an Alaskan event in its region
_POINT_BUDGET = 20000              # a shredded coast (fjords, archipelagos) must
                                   # not quietly add 500 KB to every export
_GPKG_REL = ("resources", "data", "world_map.gpkg")
_FALLBACK_BASES = (                # only for head-less runs; inside QGIS we ask QGIS
    "/Applications/QGIS-LTR.app/Contents/Resources/qgis",
    "/Applications/QGIS.app/Contents/Resources/qgis",
    "/usr/share/qgis",
    "/usr/local/share/qgis",
)


def default_world_map():
    """Path to QGIS's bundled world_map.gpkg, or "" if this install has none."""
    bases = []
    try:                                        # the only PyQGIS touch in here
        from qgis.core import QgsApplication
        bases.append(QgsApplication.pkgDataPath())
    except Exception:
        pass
    bases.extend(_FALLBACK_BASES)
    for base in bases:
        if not base:
            continue
        p = os.path.join(base, *_GPKG_REL)
        if os.path.exists(p):
            return p
    return ""


def _wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def _crs_pair(lat, lon):
    """(wgs84, laea-centred-on-the-event, forward, inverse)."""
    laea = osr.SpatialReference()
    laea.ImportFromProj4(
        "+proj=laea +lat_0=%.8f +lon_0=%.8f +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs" % (lat, lon))
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    try:                                        # GDAL 3: ask for lon,lat order
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        laea.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:                      # GDAL 2 already used lon,lat
        pass
    return (wgs, laea,
            osr.CoordinateTransformation(wgs, laea),
            osr.CoordinateTransformation(laea, wgs))


def _lonlat_window(fwd, inv, half_m, lat, lon, pad=0.10, n=48):
    """lon/lat box that certainly contains the projected square window.

    Returned lon bounds are relative to `lon` and may fall outside ±180; the
    caller splits them at the antimeridian (Aleutian events straddle it). If a
    pole falls inside the window every meridian is in view, so the box opens up
    to the full 360°."""
    dlons, lats = [], []
    for i in range(n + 1):
        t = -half_m + 2 * half_m * i / n
        for x, y in ((t, -half_m), (t, half_m), (-half_m, t), (half_m, t)):
            px, py, _ = inv.TransformPoint(x, y)
            if not (px == px and py == py):     # NaN from a failed transform
                continue
            dlons.append(_wrap180(px - lon))
            lats.append(py)
    if not lats:
        return -180.0, 180.0, -90.0, 90.0
    lat_lo, lat_hi = min(lats), max(lats)
    dlo, dhi = min(dlons), max(dlons)
    for pole in (90.0, -90.0):                  # pole inside the frame?
        x, y, _ = fwd.TransformPoint(lon, pole)
        if abs(x) <= half_m and abs(y) <= half_m:
            dlo, dhi = -180.0, 180.0
            lat_lo, lat_hi = (lat_lo, 90.0) if pole > 0 else (-90.0, lat_hi)
    dpad, lpad = (dhi - dlo) * pad, (lat_hi - lat_lo) * pad
    return (lon + dlo - dpad, lon + dhi + dpad,
            max(-90.0, lat_lo - lpad), min(90.0, lat_hi + lpad))


def _lon_rects(lon_lo, lon_hi, lat_lo, lat_hi):
    """Split an unwrapped lon range into 1-2 rects inside ±180 (antimeridian)."""
    if lon_hi - lon_lo >= 360.0:
        return [(-180.0, lat_lo, 180.0, lat_hi)]
    out, a, b = [], lon_lo, lon_hi
    if a < -180.0:
        out.append((a + 360.0, lat_lo, 180.0, lat_hi))
        a = -180.0
    if b > 180.0:
        out.append((-180.0, lat_lo, b - 360.0, lat_hi))
        b = 180.0
    out.append((a, lat_lo, b, lat_hi))
    return out


def _rect_geom(x0, y0, x1, y1):
    r = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        r.AddPoint_2D(x, y)
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(r)
    return g


def _emit(geom, out):
    """Append geom's polygons to `out` as [[ring, hole, ...], ...] int metres."""
    name = geom.GetGeometryName()
    if name == "POLYGON":
        rings = []
        for i in range(geom.GetGeometryCount()):
            r = geom.GetGeometryRef(i)
            pts = [[int(round(r.GetX(k))), int(round(r.GetY(k)))]
                   for k in range(r.GetPointCount())]
            if len(pts) >= 4:
                rings.append(pts)
        if rings:
            out.append(rings)
    elif geom.GetGeometryCount():
        for i in range(geom.GetGeometryCount()):
            _emit(geom.GetGeometryRef(i), out)


def _npoints(polys):
    return sum(len(r) for p in polys for r in p)


def _land(ds, fwd, half_m, rects, tol):
    """Country polygons clipped to the lon/lat rects, projected and simplified.

    Clipped in lon/lat BEFORE the transform on purpose: a spatial filter returns
    whole features, and projecting the far side of the United States into an
    Alaska-centred LAEA is both wasted work and numerically hostile (the
    antipode maps to infinity)."""
    lyr = ds.GetLayerByName("countries")
    if lyr is None:
        return []
    polys = []
    for x0, y0, x1, y1 in rects:
        box = _rect_geom(x0, y0, x1, y1)
        lyr.SetSpatialFilterRect(x0, y0, x1, y1)
        lyr.ResetReading()
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None:
                continue
            try:
                g = g.Clone().Intersection(box)
                if g is None or g.IsEmpty():
                    continue
                g.Transform(fwd)
                if tol > 0:
                    g = g.SimplifyPreserveTopology(tol) or g
            except RuntimeError:                # a broken ring; skip that country
                continue
            _emit(g, polys)
    lyr.SetSpatialFilter(None)
    return polys


def _place(ds, lon, lat):
    """'Alaska, United States' for the event point — blank if nothing covers it."""
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint_2D(lon, lat)
    names = []
    for layer, field in (("states_provinces", "name"), ("countries", "NAME")):
        lyr = ds.GetLayerByName(layer)
        if lyr is None:
            continue
        lyr.SetSpatialFilterRect(lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01)
        lyr.ResetReading()
        for f in lyr:
            g = f.GetGeometryRef()
            if g is not None and g.Contains(pt):
                v = (f.GetField(field) or "").strip()
                if v and v not in names:
                    names.append(v)
                break
        lyr.SetSpatialFilter(None)
    return ", ".join(n.replace("United States of America", "United States")
                     for n in names)


def _grat_step(span_deg):
    for s in (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0):
        if span_deg / s <= 6.0:
            return s
    return 30.0


def _deg_label(v, pos, neg, step):
    dec = 0 if step >= 1.0 else (1 if step >= 0.25 else 2)
    if abs(v) < 1e-9 or abs(abs(v) - 180.0) < 1e-9:
        return "%.*f°" % (dec, abs(v))          # the prime and anti meridians,
    return "%.*f° %s" % (dec, abs(v), pos if v >= 0 else neg)   # and the equator


def _graticule(fwd, half_m, lon_lo, lon_hi, lat_lo, lat_hi):
    """Parallels and meridians as projected polylines, each with its label.

    Drawn as polylines rather than straight rules because they are not straight:
    in an equal-area azimuthal projection centred on the event both families
    curve, and that curvature is most of what tells a reader they are looking at
    a map of somewhere near 60° N rather than a rectangle of pixels."""
    out = []
    dlat = _grat_step(lat_hi - lat_lo)
    dlon = _grat_step(min(360.0, lon_hi - lon_lo))
    lim = half_m * 1.45                         # a margin past the frame; JS clips
    def line(pts, lab, axis):
        keep = [p for p in pts if abs(p[0]) < lim and abs(p[1]) < lim]
        if len(keep) >= 2:
            out.append({"pts": keep, "lab": lab, "axis": axis})
    n = 72                                     # enough to draw a smooth curve
    v = math.ceil(lat_lo / dlat) * dlat
    while v <= lat_hi + 1e-9:
        if -90.0 < v < 90.0:
            pts = []
            for i in range(n + 1):
                x, y, _ = fwd.TransformPoint(lon_lo + (lon_hi - lon_lo) * i / n, v)
                pts.append([int(round(x)), int(round(y))])
            line(pts, _deg_label(v, "N", "S", dlat), "lat")
        v += dlat
    v = math.ceil(lon_lo / dlon) * dlon
    while v <= lon_hi + 1e-9:
        pts = []
        for i in range(n + 1):
            x, y, _ = fwd.TransformPoint(v, lat_lo + (lat_hi - lat_lo) * i / n)
            pts.append([int(round(x)), int(round(y))])
        line(pts, _deg_label(_wrap180(v), "E", "W", dlon), "lon")
        v += dlon
    return out


def build_locator(lat, lon, half_m=HALF_M_DEFAULT, gpkg=None, label=""):
    """Geometry for the location panel, or None if no world map is available.

    All coordinates are metres in a Lambert azimuthal equal-area projection
    centred on (lat, lon) — so the event itself is always (0, 0) and the panel
    is the square ±half_m around it, drawn with y north."""
    path = gpkg or default_world_map()
    if not path or not os.path.exists(path):
        return None
    ds = ogr.Open(path)
    if ds is None:
        return None
    half_m = float(half_m)
    _, _, fwd, inv = _crs_pair(lat, lon)
    lon_lo, lon_hi, lat_lo, lat_hi = _lonlat_window(fwd, inv, half_m, lat, lon)
    rects = _lon_rects(lon_lo, lon_hi, lat_lo, lat_hi)
    tol = half_m / 500.0                        # ~1.6 px on the exported panel
    land = _land(ds, fwd, half_m, rects, tol)
    while _npoints(land) > _POINT_BUDGET and tol < half_m / 20.0:
        tol *= 2.5                              # a dense coast (fjords, islands)
        land = _land(ds, fwd, half_m, rects, tol)
    return {
        "lat": float(lat), "lon": float(lon), "half_m": half_m,
        "place": _place(ds, lon, lat),
        "label": str(label or ""),
        "land": land,
        "grat": _graticule(fwd, half_m, lon_lo, lon_hi, lat_lo, lat_hi),
    }
