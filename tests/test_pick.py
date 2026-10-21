"""Picking the epicentre off the map, and accepting a pasted coordinate pair.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_pick.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsProject, QgsPointXY)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
app=QgsApplication([],False); app.initQgis()
from qgis.PyQt.QtWidgets import QLineEdit, QPlainTextEdit, QPushButton
from landslide_groundtruth import dock as D

class FakeBar:
    def __init__(s): s.msgs=[]
    def pushInfo(s,a,b): s.msgs.append(b)
    def pushWarning(s,a,b): s.msgs.append("WARN "+b)
class FakeIface:
    def __init__(s): s.bar=FakeBar()
    def messageBar(s): return s.bar
class Stub:
    def __init__(s):
        s.iface=FakeIface(); s.log=QPlainTextEdit()
        s.lat_edit=QLineEdit(); s.lon_edit=QLineEdit()
        from collections import deque; s._log_tail=deque(maxlen=80)
    def _warn(s,t): s.iface.messageBar().pushWarning("L",t)
for m in ("_append_log","_log_tail_text","_wgs84","_set_lat_lon","_log_pasted_pair"):
    setattr(Stub,m,getattr(D.LandslideDock,m))
s=Stub()
# _build_ui makes this connection; the stub must too, or the splitter never runs
D.coord_paste.link_pair(s.lat_edit, s.lon_edit, on_split=s._log_pasted_pair)

print("=== 1. transform from a projected CRS (UTM 7N, Saint Elias) ===")
utm=QgsCoordinateReferenceSystem("EPSG:32607")
wgs=QgsCoordinateReferenceSystem("EPSG:4326")
# forward-project the known epicentre so we can check the round trip
fwd=QgsCoordinateTransform(wgs,utm,QgsProject.instance())
pt=fwd.transform(QgsPointXY(-140.60,60.50))
print(f"   60.500,-140.600 -> UTM7N {pt.x():.1f}, {pt.y():.1f}")
back=s._wgs84(pt, utm)
print(f"   back            -> {back.y():.6f}, {back.x():.6f}")
assert abs(back.y()-60.50)<1e-6 and abs(back.x()+140.60)<1e-6
s._set_lat_lon(back.y(), back.x())
print("   fields:", s.lat_edit.text(), s.lon_edit.text())
assert s.lat_edit.text()=="60.500000" and s.lon_edit.text()=="-140.600000"

print("\n=== 2. already-WGS84 canvas is a no-op passthrough ===")
p2=s._wgs84(QgsPointXY(-140.6,60.5), wgs)
assert abs(p2.x()+140.6)<1e-12
print("   ok, identity")

print("\n=== 3. an invalid CRS returns None (and _on_map_pick warns) ===")
assert s._wgs84(QgsPointXY(1,2), QgsCoordinateReferenceSystem()) is None
print("   ok, None")

def paste(box, text):
    """What Cmd+V does: replace the selection, emitting textEdited (setText
    never does, so it would not exercise the splitter at all)."""
    box.selectAll(); box.insert(text)

print("\n=== 4. pasted pair splitting (the case the validator used to eat) ===")
CASES=[("60.5, -140.6",("60.500000","-140.600000")),
       ("60.5,-140.6",("60.500000","-140.600000")),
       ("59.906992, -149.823317",("59.906992","-149.823317")),
       ("60.5;-140.6",("60.500000","-140.600000")),
       ("60.5\t-140.6",("60.500000","-140.600000")),
       ("60.5 -140.6",("60.500000","-140.600000")),
       ("60.5 −140.6",("60.500000","-140.600000")),     # typographic minus
       ("60.0265° N, 153.0559° W",("60.026500","-153.055900")),
       ("153.0559 W, 60.0265 N",("60.026500","-153.055900")),  # letters say which
       ("POINT(-149.82 59.91)",("59.910000","-149.820000")),   # lon first: swapped
       ("Latitude = 60.50°, Longitude = -140.60°",("60.500000","-140.600000"))]
for box in ("lat_edit","lon_edit"):
    for text,(wl,wo) in CASES:
        s.lat_edit.clear(); s.lon_edit.clear()
        paste(getattr(s,box), text)
        got=(s.lat_edit.text(), s.lon_edit.text())
        ok = got==(wl,wo)
        print(("   ok   " if ok else "   FAIL ")+f"into {box[:3]}: {text!r:42s} -> {got}")
        assert ok, (box,text,got)
assert "Pasted coordinate pair split: 60.500000, -140.600000" in s._log_tail_text(), \
    "the S2 tab logs the split"

print("\n=== 5. must NOT split things that are not pairs ===")
for text in ["60.5","-140.6","","60.5, -140.6, 12","abc, def","999, -140.6","60.5, -999",
             "60° 30' N","60 30 N","60 N, 61 N"]:
    s.lat_edit.clear(); s.lon_edit.clear()
    paste(s.lat_edit, text)
    s.lat_edit.editingFinished.emit()
    got=(s.lat_edit.text(), s.lon_edit.text())
    assert got==(text,""), (text, got)
    print(f"   ok   {text!r:22s} left alone")

print("\n=== 6. a pair being TYPED is split on Enter, not mid-word ===")
# Splitting "60.5, -1" the moment it parses would put -1 in Longitude and send
# the rest of the typing into Latitude.
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtCore import Qt
s.lat_edit.clear(); s.lon_edit.clear()
QTest.keyClicks(s.lat_edit, "60.5, -140.6")
print("   while typing:", repr(s.lat_edit.text()), repr(s.lon_edit.text()))
assert (s.lat_edit.text(), s.lon_edit.text())==("60.5, -140.6","")
QTest.keyClick(s.lat_edit, Qt.Key_Return)
print("   after Enter: ", s.lat_edit.text(), s.lon_edit.text())
assert (s.lat_edit.text(), s.lon_edit.text())==("60.500000","-140.600000")

print("\n=== 7. no re-entry, and a filled box is pasted over ===")
s.lat_edit.clear(); s.lon_edit.clear()
paste(s.lat_edit, "60.5, -140.6")
paste(s.lat_edit, "61.5, -141.6")     # twice in a row, over the first
print("   second paste:", s.lat_edit.text(), s.lon_edit.text())
assert (s.lat_edit.text(), s.lon_edit.text())==("61.500000","-141.600000")
s.lat_edit.setText("1, 2")            # a program setting text is not a paste
assert (s.lat_edit.text(), s.lon_edit.text())==("1, 2","-141.600000")
print("\nMAP-PICK + PAIR-PASTE VERIFIED")
