"""Map marks: the analyst's own notes on where to look, kept with the project.

Hunting a slide inside a detection's error disc means ruling places out and
flagging others for a second look. Two kinds of mark, typed in as coordinates:

  not_here   a red X          — checked, nothing there, do not look again
  come_back  a blue circle    — a possible landslide area to return to

Both go into ONE point layer, "Map marks", stored as map_marks.gpkg in the
project's folder (dock.results_dir()) — a file rather than a memory layer for
the same reason the Volume outlines are: a scratch layer is gone at the next
QGIS restart, and these are the record of what was already searched.

Each mark is written to the file as it is added (the OGR provider commits
straight away), so there is no edit session to remember to save.
"""

import os
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFeature, QgsField, QgsGeometry,
    QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRendererCategory,
    QgsVectorFileWriter, QgsVectorLayer,
)

LAYER_NAME = "Map marks"
FILE_NAME = "map_marks.gpkg"
TABLE = "map_marks"
# Marks the layer as ours in the project, so it is found again after a rename.
PROPERTY = "landslide_groundtruth/map_marks"

NOT_HERE = "not_here"
COME_BACK = "come_back"
# kind -> (legend label, symbol properties). Shape AND colour differ, so the two
# stay apart for someone who cannot separate the red from the blue.
KINDS = {
    NOT_HERE: ("Don't look here", {
        "name": "cross2", "color": "214,32,32", "outline_color": "214,32,32",
        "outline_width": "0.9", "size": "5"}),
    COME_BACK: ("Come back — possible landslide", {
        "name": "circle", "color": "0,0,0,0", "outline_color": "30,110,230",
        "outline_width": "0.9", "size": "6.5"}),
}


class MarkError(Exception):
    """A mark that could not be added; str(e) is what to tell the user."""


def parse_coords(lat_text, lon_text):
    """(lat, lon) as floats, or MarkError saying what is wrong."""
    try:
        lat = float(str(lat_text).strip())
        lon = float(str(lon_text).strip())
    except ValueError:
        raise MarkError("Enter a numeric latitude and longitude first.")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise MarkError("Latitude must be -90..90 and longitude -180..180.")
    return lat, lon


def _fields():
    return [QgsField("kind", QMetaType.Type.QString),
            QgsField("note", QMetaType.Type.QString),
            QgsField("lat", QMetaType.Type.Double),
            QgsField("lon", QMetaType.Type.Double),
            QgsField("added_utc", QMetaType.Type.QString)]


def style(layer):
    """Red X / blue circle, by the `kind` field."""
    cats = [QgsRendererCategory(kind, QgsMarkerSymbol.createSimple(props), label)
            for kind, (label, props) in KINDS.items()]
    layer.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
    layer.setCustomProperty(PROPERTY, True)


def _create_file(path):
    """An empty marks GeoPackage at `path`."""
    scratch = QgsVectorLayer("Point?crs=EPSG:4326", LAYER_NAME, "memory")
    scratch.dataProvider().addAttributes(_fields())
    scratch.updateFields()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = TABLE
    opts.fileEncoding = "UTF-8"
    try:
        ctx = QgsProject.instance().transformContext()
        err, msg = QgsVectorFileWriter.writeAsVectorFormatV3(
            scratch, path, ctx, opts)[:2]
    except Exception as e:                           # noqa: BLE001
        raise MarkError(f"Could not write {path}: {e}")
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise MarkError(f"Could not write {path}: {msg}")


def find_layer(project=None):
    """The marks layer already in the project, or None."""
    project = project or QgsProject.instance()
    for layer in project.mapLayers().values():
        try:
            if layer.customProperty(PROPERTY) and layer.isValid():
                return layer
        except RuntimeError:
            continue
    return None


def ensure_layer(results_dir, project=None):
    """The project's marks layer, loading or creating map_marks.gpkg as needed.

    An existing file is reused, never overwritten: it is the record of what has
    been searched."""
    project = project or QgsProject.instance()
    layer = find_layer(project)
    if layer is not None:
        return layer
    if not results_dir:
        raise MarkError(
            "Nowhere to save the marks. Save this QGIS project first, or set "
            "“Results (unsaved projects)” in Setup.")
    try:
        os.makedirs(results_dir, exist_ok=True)
    except OSError as e:
        raise MarkError(f"Could not create {results_dir}: {e}")
    path = os.path.join(results_dir, FILE_NAME)
    if not os.path.exists(path):
        _create_file(path)
    layer = QgsVectorLayer(f"{path}|layername={TABLE}", LAYER_NAME, "ogr")
    if not layer.isValid():
        raise MarkError(f"{path} exists but QGIS could not load it as map marks.")
    style(layer)
    try:        # so the file opens styled in any project it is dropped into
        save = (getattr(layer, "saveStyleToDatabaseV2", None)
                or layer.saveStyleToDatabase)
        save(TABLE, "Map marks", True, "")
    except Exception:                                # noqa: BLE001 — a nicety
        pass
    project.addMapLayer(layer)
    return layer


def add_mark(results_dir, kind, lat, lon, note="", project=None):
    """Add one mark and write it to the file. Returns the layer."""
    if kind not in KINDS:
        raise MarkError(f"Unknown mark kind: {kind}")
    layer = ensure_layer(results_dir, project)
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
    feat["kind"] = kind
    feat["note"] = note or ""
    feat["lat"] = lat
    feat["lon"] = lon
    feat["added_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ok, _added = layer.dataProvider().addFeatures([feat])
    if not ok:
        raise MarkError(
            f"Could not write the mark to {layer.source().split('|')[0]} — is "
            "the file read-only, or the layer in an edit session?")
    layer.updateExtents()
    layer.triggerRepaint()
    return layer
