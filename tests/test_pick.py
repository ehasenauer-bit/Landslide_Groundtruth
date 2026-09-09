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
for m in ("_append_log","_log_tail_text","_wgs84","_set_lat_lon","_split_pasted_pair"):
    setattr(Stub,m,getattr(D.LandslideDock,m))
s=Stub()
# _build_ui makes this connection; the stub must too, or the splitter never runs
s.lat_edit.textChanged.connect(s._split_pasted_pair)

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

print("\n=== 4. pasted pair splitting (the case the validator used to eat) ===")
CASES=[("60.5, -140.6",("60.500000","-140.600000")),
       ("60.5,-140.6",("60.500000","-140.600000")),
       ("59.906992, -149.823317",("59.906992","-149.823317")),
       ("60.5;-140.6",("60.500000","-140.600000")),
       ("60.5\t-140.6",("60.500000","-140.600000"))]
for text,(wl,wo) in CASES:
    s.lat_edit.clear(); s.lon_edit.clear()
    s.lat_edit.setText(text)          # textChanged fires the splitter
    got=(s.lat_edit.text(), s.lon_edit.text())
    ok = got==(wl,wo)
    print(("   ok   " if ok else "   FAIL ")+f"{text!r:26s} -> {got}")
    assert ok, (text,got)

print("\n=== 5. must NOT split things that are not pairs ===")
for text in ["60.5","-140.6","","60.5, -140.6, 12","abc, def","999, -140.6","60.5, -999"]:
    s.lat_edit.clear(); s.lon_edit.clear()
    s.lat_edit.setText(text)
    got=(s.lat_edit.text(), s.lon_edit.text())
    assert got[1]=="", (text, got)
    print(f"   ok   {text!r:22s} left alone (lon empty)")

print("\n=== 6. no infinite recursion from the setText inside textChanged ===")
s.lat_edit.clear(); s.lon_edit.clear()
s.lat_edit.setText("60.5, -140.6")
s.lat_edit.setText("61.5, -141.6")     # twice in a row
print("   second paste:", s.lat_edit.text(), s.lon_edit.text())
assert (s.lat_edit.text(), s.lon_edit.text())==("61.500000","-141.600000")
print("\nMAP-PICK + PAIR-PASTE VERIFIED")
