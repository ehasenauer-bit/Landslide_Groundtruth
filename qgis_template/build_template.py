#!/usr/bin/env python3
"""Build the QGIS template project: basemaps + styled reference layers.

Writes into the QGIS user profile's template folder:

    Landslide.qgz              the template itself
    landslide_reference.gpkg   built first by build_reference_gpkg.py
    styles/*.qml               each layer's style, for reuse elsewhere

After running, QGIS offers it under Project > New from Template, and
Settings > Options > General can make it the default for File > New.

Run it with plain system python — the script re-execs itself under the QGIS
bundle's interpreter (PyQGIS does not start standalone on macOS without a
prefix path, a plugin path, and PROJ_LIB all set):

    python3 build_template.py

Layer paths are written ABSOLUTE. The GeoPackage lives inside the QGIS user
profile, which is a stable per-user location QGIS owns, so a relative path from
there to wherever a landslide project happens to be saved buys nothing and only
adds a way to break. Pass --relative-paths to switch.
"""
import argparse
import os
import sys

BUNDLES = ("/Applications/QGIS-LTR.app", "/Applications/QGIS.app")
GUARD = "_LSGT_QGIS_REEXEC"


def reexec_under_qgis():
    """Re-run this script under the QGIS bundle's python with PyQGIS on the path.

    The bundle ships python3.12 as the binary but its stdlib and site-packages
    under Resources/python3.11, so PYTHONHOME has to point at Resources rather
    than at the usual MacOS dir — hence the explicit env rather than a venv.
    """
    try:
        import qgis.core  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get(GUARD):
        sys.exit("PyQGIS still not importable after re-exec — is QGIS installed?")

    bundle = next((b for b in BUNDLES if os.path.isdir(b)), None)
    if bundle is None:
        sys.exit(f"No QGIS bundle found in {' or '.join(BUNDLES)}")
    res = os.path.join(bundle, "Contents/Resources")
    python = os.path.join(bundle, "Contents/Frameworks/bin/python3")
    stdlib = next((os.path.join(res, d) for d in sorted(os.listdir(res))
                   if d.startswith("python3.")), None)
    if not (os.path.exists(python) and stdlib):
        sys.exit(f"Unexpected QGIS bundle layout under {bundle}")

    env = dict(os.environ)
    env[GUARD] = "1"
    env["PYTHONHOME"] = res
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(stdlib, "site-packages"), os.path.join(res, "python")])
    env["PROJ_LIB"] = os.path.join(res, "qgis/proj")
    env["GDAL_DATA"] = os.path.join(res, "qgis/gdal")
    env["QGIS_BUNDLE"] = bundle
    os.execve(python, [python, os.path.abspath(__file__)] + sys.argv[1:], env)


reexec_under_qgis()

import urllib.parse  # noqa: E402

from qgis.core import (  # noqa: E402
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsFillSymbol,
    QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsProject,
    QgsProperty, QgsRasterLayer, QgsRectangle, QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont  # noqa: E402

PROFILE = os.path.expanduser(
    "~/Library/Application Support/QGIS/QGIS3/profiles/default")
TEMPLATE_DIR = os.path.join(PROFILE, "project_templates")

# XYZ basemaps. (label, url, zmax, visible)
#
# Google's tile endpoints are reached directly here rather than through the
# Maps Platform API. That is how every "QGIS Google Satellite" recipe does it,
# and it is against Google's Terms of Service, which require Maps imagery to be
# served through their APIs. Esri World Imagery is included alongside as a
# licensed layer of comparable resolution — swap the visible one if the terms
# matter for a published figure.
BASEMAPS = (
    ("Google Satellite",
     "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", 20, True),
    ("Google Hybrid (labels)",
     "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", 20, False),
    ("Google Terrain",
     "https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}", 20, False),
    ("Esri World Imagery",
     "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery"
     "/MapServer/tile/{z}/{y}/{x}", 19, False),
)

# Web Mercator so the XYZ tiles draw at native resolution instead of being
# resampled every pan. Area/distance readouts stay honest because the project
# ellipsoid is set to WGS84 below, which makes QGIS measure geodetically rather
# than in projected units — Mercator area error at 60 N is a factor of ~4.
PROJECT_CRS = "EPSG:3857"

# A small patch of the Logan massif in EPSG:3857, used only to ask each basemap
# for real pixels at build time (see tiles_arrive). Any land extent would do —
# every basemap here is global — so retargeting the template at another region
# does not require changing it.
PROBE_EXTENT_3857 = (-15679000.0, 8560000.0, -15580000.0, 8625000.0)


def xyz_layer(name, url, zmax):
    """Build an XYZ raster layer.

    The encoding of `url` is load-bearing and fails silently if wrong. The URI
    is itself an &-and-=-delimited parameter string, so any & or = *inside* the
    tile URL — Google's "lyrs=s&x={x}&y={y}&z={z}" is all of them — has to be
    percent-encoded or the parser truncates the URL at the first &. But ':' and
    '/' must stay literal: encoding those as %3A/%2F yields a layer that reports
    isValid() == True, raises no provider error, and renders nothing at all.
    safe=':/' is the combination that satisfies both.
    """
    uri = (f"type=xyz&url={urllib.parse.quote(url, safe=':/')}"
           f"&zmax={zmax}&zmin=0")
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        raise RuntimeError(f"XYZ layer failed to build: {name}")
    if not tiles_arrive(layer):
        raise RuntimeError(
            f"{name}: layer is valid but served no tile data — check the URL "
            f"encoding in xyz_layer(), or the endpoint may be unreachable.\n"
            f"  {uri}")
    return layer


def tiles_arrive(layer, probe=PROBE_EXTENT_3857):
    """Fetch one real block and report whether any imagery came back.

    isValid() only means the URI parsed, so it cannot catch a mangled tile URL —
    that failure mode is completely silent. Nothing short of asking the provider
    for pixels distinguishes a working basemap from a broken one, so the build
    pays for one small request per basemap rather than shipping a blank
    template. A uniform block is the signature of failure; real imagery has
    thousands of distinct pixel values.
    """
    block = layer.dataProvider().block(1, QgsRectangle(*probe), 200, 120)
    if block is None:
        return False
    raw = bytes(block.data())
    return len(set(raw[i:i + 4] for i in range(0, len(raw), 4))) > 5


def gpkg_layer(gpkg, table, name):
    layer = QgsVectorLayer(f"{gpkg}|layername={table}", name, "ogr")
    if not layer.isValid():
        raise RuntimeError(f"Layer {table} missing from {gpkg} — "
                           "run build_reference_gpkg.py first")
    return layer


def text_format(size_pt, bold=True, color="#000000", buffer_mm=1.0):
    fmt = QgsTextFormat()
    font = QFont("Helvetica Neue")
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(size_pt)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(color))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(buffer_mm)
    buf.setSizeUnit(Qgis.RenderUnit.Millimeters)
    buf.setColor(QColor("white"))
    buf.setOpacity(0.9)
    fmt.setBuffer(buf)
    return fmt


def style_peaks(layer):
    """Hollow triangle + haloed bold label — the standard summit convention."""
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({
        "name": "triangle",
        "size": "3.2", "size_unit": "MM",
        "color": "255,255,255,0",              # hollow, so imagery shows through
        "outline_color": "0,0,0,255",
        "outline_width": "0.4", "outline_width_unit": "MM",
    })))

    settings = QgsPalLayerSettings()
    settings.fieldName = "name"
    settings.setFormat(text_format(9))
    settings.placement = Qgis.LabelPlacement.AroundPoint
    settings.dist = 1.5

    # Elevation drives label priority so that when summits crowd together the
    # big ones survive collision resolution. scale_linear clamps at the ends,
    # and coalesce covers the ~7% of OSM peaks with no ele tag.
    props = settings.dataDefinedProperties()
    props.setProperty(
        QgsPalLayerSettings.Property.Priority,
        QgsProperty.fromExpression(
            'coalesce(scale_linear("ele_m", 500, 6000, 2, 10), 4)'))
    settings.setDataDefinedProperties(props)

    # Labels come in later than the markers: 10k triangles at regional zoom is
    # texture, 10k labels is a solid grey block.
    settings.scaleVisibility = True
    settings.minimumScale = 600000
    settings.maximumScale = 0

    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.setScaleBasedVisibility(True)
    layer.setMinimumScale(2000000)             # hidden above ~1:2M
    layer.setMaximumScale(0)                   # no zoomed-in limit


def style_border(layer, color, width, style):
    """Unlabelled by design: Natural Earth boundary lines carry no usable name
    field, and on a landslide map the line itself is the whole point."""
    layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({
        "line_color": color,
        "line_width": str(width), "line_width_unit": "MM",
        "line_style": style,
        "capstyle": "round",
    })))


def style_parks(layer):
    """Outline only — a fill would fight the imagery this template exists for."""
    layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "outline_color": "26,120,60,220",
        "outline_width": "0.5", "outline_width_unit": "MM",
        "outline_style": "dash",
    })))
    settings = QgsPalLayerSettings()
    settings.fieldName = "name"
    settings.setFormat(text_format(9, bold=False, color="#1a783c"))
    settings.placement = Qgis.LabelPlacement.PerimeterCurved
    settings.scaleVisibility = True
    settings.minimumScale = 3000000
    settings.maximumScale = 0
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(False)              # on-demand; parks clutter fast


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=TEMPLATE_DIR,
                    help="template folder (default: the QGIS default profile's)")
    ap.add_argument("--gpkg", default=None,
                    help="reference GeoPackage (default: <dir>/landslide_reference.gpkg)")
    ap.add_argument("--name", default="Landslide.qgz")
    ap.add_argument("--relative-paths", action="store_true",
                    help="store layer sources relative to the template file")
    ap.add_argument("--no-default", action="store_true",
                    help="skip writing the profile's project_default.qgs")
    args = ap.parse_args()

    out_dir = os.path.abspath(os.path.expanduser(args.dir))
    gpkg = os.path.abspath(os.path.expanduser(
        args.gpkg or os.path.join(out_dir, "landslide_reference.gpkg")))
    if not os.path.exists(gpkg):
        sys.exit(f"Reference GeoPackage not found: {gpkg}\n"
                 "Run build_reference_gpkg.py first.")
    styles_dir = os.path.join(out_dir, "styles")
    os.makedirs(styles_dir, exist_ok=True)

    QgsApplication.setPrefixPath(
        os.path.join(os.environ.get("QGIS_BUNDLE", BUNDLES[0]), "Contents/MacOS"),
        True)
    QgsApplication.setPluginPath(
        os.path.join(os.environ.get("QGIS_BUNDLE", BUNDLES[0]), "Contents/PlugIns/qgis"))
    app = QgsApplication([], False)
    app.initQgis()

    project = QgsProject.instance()
    project.setTitle("Landslide ground-truthing")
    project.setCrs(QgsCoordinateReferenceSystem(PROJECT_CRS))
    project.setEllipsoid("WGS84")              # geodetic measurement — see PROJECT_CRS
    project.setDistanceUnits(Qgis.DistanceUnit.Meters)
    project.setAreaUnits(Qgis.AreaUnit.SquareKilometers)
    project.setFilePathStorage(
        Qgis.FilePathType.Relative if args.relative_paths
        else Qgis.FilePathType.Absolute)

    root = project.layerTreeRoot()
    ref_group = root.addGroup("Reference")
    base_group = root.addGroup("Basemaps")

    # Reference layers, top of the tree first so peaks draw over the borders.
    vectors = (
        ("peaks", "Peaks", style_peaks),
        ("borders_international", "International border",
         lambda lyr: style_border(lyr, "90,25,90,255", 0.8, "dash dot")),
        ("borders_state_province", "State / province border",
         lambda lyr: style_border(lyr, "90,25,90,180", 0.45, "dash")),
        ("protected_areas", "Parks & protected areas", style_parks),
    )
    for table, label, styler in vectors:
        layer = gpkg_layer(gpkg, table, label)
        styler(layer)
        project.addMapLayer(layer, False)
        ref_group.addLayer(layer)
        layer.saveNamedStyle(os.path.join(styles_dir, f"{table}.qml"))
        print(f"  reference  {label}  ({layer.featureCount()} features)")

    for label, url, zmax, visible in BASEMAPS:
        layer = xyz_layer(label, url, zmax)
        project.addMapLayer(layer, False)
        node = base_group.addLayer(layer)
        node.setItemVisibilityChecked(visible)
        print(f"  basemap    {label}{'' if visible else '  (off)'}")

    out = os.path.join(out_dir, args.name)
    if not project.write(out):
        sys.exit(f"Failed to write {out}")

    # The same project doubles as the profile's default. QGIS looks for exactly
    # this filename and only consults it when Options > General > "Create new
    # project from default project" is ticked, so writing it is inert until then
    # — but it saves clicking "Set current project as default" by hand.
    default_project = None
    if not args.no_default:
        default_project = os.path.join(PROFILE, "project_default.qgs")
        if not project.write(default_project):
            sys.exit(f"Failed to write {default_project}")

    app.exitQgis()
    print(f"\nTemplate: {out} ({os.path.getsize(out) / 1024:.0f} kB)")
    print(f"Styles:   {styles_dir}")
    if default_project:
        print(f"Default:  {default_project}")
        print("\nTick Settings > Options > General > Project files >")
        print("'Create new project from default project' to arm it.")


if __name__ == "__main__":
    main()
