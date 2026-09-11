"""Guards on the two operations that could destroy work.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_dataloss.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)
import tempfile

from qgis.core import QgsApplication
app=QgsApplication([],False)
from landslide_groundtruth import viewer3d_tab as V3, detection as DET

print("=== 1. filename safety ===")
for raw,want in [("AK2026-0204","AK2026-0204"),("AK 2026/02-04","AK_2026_02-04"),
                 ("../../etc/passwd","etc_passwd"),("",""),(None,""),
                 ("a:b*c?d","a_b_c_d"),("....","landslide")]:
    got=V3._safe_name(raw)
    exp = want or "landslide"
    ok = got==exp
    print(("   ok   " if ok else "   FAIL ")+f"{raw!r:20s} -> {got!r}")
    assert ok,(raw,got,exp)
assert "/" not in V3._safe_name("../../etc/passwd")
print("   path traversal neutralised")

print("\n=== 2. never overwrites ===")
d=tempfile.mkdtemp()
paths=[]
for i in range(4):
    p=V3._unique_path(d,"AK2026-0204_3d",".html")
    open(p,"w").write(f"figure {i}")
    paths.append(os.path.basename(p))
print("   four exports ->", paths)
assert paths==["AK2026-0204_3d.html","AK2026-0204_3d (2).html",
               "AK2026-0204_3d (3).html","AK2026-0204_3d (4).html"], paths
# every earlier figure must survive with its own content
for i,b in enumerate(paths):
    assert open(os.path.join(d,b)).read()==f"figure {i}"
print("   all four still on disk with their original content")

print("\n=== 3. stem names the event ===")
class FakeDock: detection=None
class Stub:
    def __init__(s): s.dock=FakeDock()
Stub._viewer_stem=V3.Viewer3DTab._viewer_stem
s=Stub()
print("   no detection, no dt_edit ->", s._viewer_stem())
assert s._viewer_stem()=="instant_flip_3d"
det=DET.Detection(event_id="AK2026-0204", lat=60.5, lon=-140.6,
                  origin_utc=__import__("datetime").datetime(2026,2,4,8,48,5))
s.dock.detection=det
print("   with detection          ->", s._viewer_stem())
assert s._viewer_stem()=="AK2026-0204_3d"
from qgis.PyQt.QtWidgets import QDateTimeEdit
from qgis.PyQt.QtCore import QDateTime
s2=Stub(); s2.dt_edit=QDateTimeEdit(); s2.dt_edit.setDateTime(QDateTime.fromString("2026-02-04","yyyy-MM-dd"))
print("   date only               ->", s2._viewer_stem())
assert s2._viewer_stem()=="landslide_2026-02-04_3d"

print("\n=== 4. write-to-layer guard is present and gated on an EXPLICIT selection ===")
src=open(os.path.join(PLUG, "volume_tab.py")).read()
i=src.index("def _write_to_layer")
body=src[i:i+2200]
assert "explicit = sorted({i.row() for i in self.table.selectedIndexes()})" in body
assert "if not explicit and len(self._rows) > 1:" in body
assert "QMessageBox.Cancel)" in body and "QMessageBox.Yes" in body
print("   confirms only when nothing is selected AND >1 row")
# read-only consumers must NOT have been made harder
for fn in ("_copy_table","_export_csv"):
    j=src.index(f"def {fn}")
    assert "QMessageBox" not in src[j:j+1200], fn
print("   Copy and Export CSV still fall through to all rows (read-only)")
print("\nDATA-LOSS GUARDS VERIFIED")
