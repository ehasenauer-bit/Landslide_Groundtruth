"""Mark a spot: Pick on map fills the mark fields, Show notes opens the table.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_mark_pick.py
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsProject, QgsPointXY)
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
app = QgsApplication([], False); app.initQgis()
from qgis.PyQt.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QPushButton
from qgis.PyQt.QtCore import Qt
from qgis.gui import QgsMapCanvas, QgsMapToolPan
from landslide_groundtruth import dock as D, map_marks as M


class FakeBar:
    def __init__(s): s.msgs = []
    def pushInfo(s, a, b): s.msgs.append(b)
    def pushWarning(s, a, b): s.msgs.append("WARN " + b)


class FakeIface:
    def __init__(s): s.bar = FakeBar(); s.tables = []
    def messageBar(s): return s.bar
    def showAttributeTable(s, layer): s.tables.append(layer)


class Stub:
    def __init__(s, d):
        s.iface = FakeIface(); s.log = QPlainTextEdit(); s.canvas = QgsMapCanvas()
        from collections import deque; s._log_tail = deque(maxlen=80)
        s._mark_lat_edit = QLineEdit(); s._mark_lon_edit = QLineEdit()
        s._mark_note_edit = QLineEdit(); s._mark_status = QLabel()
        s.pick_btn = QPushButton(); s.pick_btn.setCheckable(True)
        s.pick_btn.toggled.connect(s._toggle_pick)
        s._mark_pick_btn = QPushButton(); s._mark_pick_btn.setCheckable(True)
        s._mark_pick_btn.toggled.connect(s._toggle_mark_pick)
        s._d = d
    def results_dir(s): return s._d
    def _warn(s, t): s.iface.messageBar().pushWarning("L", t)
    def _set_lat_lon(s, lat, lon): s.event = (lat, lon)


for m in ("_append_log", "_log_tail_text", "_wgs84", "_toggle_pick", "_end_pick",
          "_on_map_pick", "_toggle_mark_pick", "_end_mark_pick", "_on_mark_pick",
          "_on_mark_pick_deactivated", "_show_mark_notes", "_add_mark"):
    setattr(Stub, m, getattr(D.LandslideDock, m))

d = tempfile.mkdtemp()
s = Stub(d)
pan = QgsMapToolPan(s.canvas)
s.canvas.setMapTool(pan)
utm = QgsCoordinateReferenceSystem("EPSG:32605")
s.canvas.setDestinationCrs(utm)

print("=== 1. arm, click (projected canvas), fields filled, tool handed back ===")
s._mark_pick_btn.setChecked(True)
assert s.canvas.mapTool() is s._mark_pick_tool
pt = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), utm,
                            QgsProject.instance()).transform(QgsPointXY(-153.09, 60.03))
s._mark_pick_tool.canvasClicked.emit(pt, Qt.MouseButton.LeftButton)   # Qt 6 rejects a bare 1
print("   fields:", s._mark_lat_edit.text(), s._mark_lon_edit.text())
assert (s._mark_lat_edit.text(), s._mark_lon_edit.text()) == ("60.030000", "-153.090000")
assert not s._mark_pick_btn.isChecked() and s.canvas.mapTool() is pan
assert not hasattr(s, "event"), "the event location must not move"

print("=== 2. then mark it with a note ===")
s._mark_note_edit.setText("dark streak")
s._add_mark(M.COME_BACK)
layer = M.find_layer()
f = next(layer.getFeatures())
assert (f["kind"], f["note"]) == (M.COME_BACK, "dark streak")
print("   saved:", f["kind"], f["note"], f["lat"], f["lon"])

print("=== 3. only one picker armed at a time ===")
s._mark_pick_btn.setChecked(True)
s.pick_btn.setChecked(True)
assert not s._mark_pick_btn.isChecked() and s.canvas.mapTool() is s._pick_tool
s._mark_pick_btn.setChecked(True)
assert not s.pick_btn.isChecked() and s.canvas.mapTool() is s._mark_pick_tool
s._mark_pick_btn.setChecked(False)
assert s.canvas.mapTool() is pan, s.canvas.mapTool()
print("   ok")

print("=== 4. choosing another QGIS tool un-arms the button ===")
s._mark_pick_btn.setChecked(True)
s.canvas.setMapTool(pan)
assert not s._mark_pick_btn.isChecked() and s._mark_pick_tool is None
assert s.canvas.mapTool() is pan
print("   ok")

print("=== 5. Show notes opens the marks table; reloads it if not in project ===")
s._show_mark_notes()
assert s.iface.tables[-1] is layer
QgsProject.instance().removeMapLayer(layer.id())
s._show_mark_notes()
assert s.iface.tables[-1].name() == M.LAYER_NAME and s.iface.tables[-1].featureCount() == 1
s2 = Stub(tempfile.mkdtemp())
QgsProject.instance().removeAllMapLayers()
s2._show_mark_notes()
assert not s2.iface.tables and "No marks yet" in s2._mark_status.text()
print("   ok")
print("\nMARK PICK + NOTES VERIFIED")
# Leave without Python tearing QGIS down: on QGIS 4 that teardown segfaults (exit
# 139) after every check has passed, and the runner reads it as a failure. Same
# exit as test_map_marks.py.
sys.stdout.flush()
os._exit(0)
