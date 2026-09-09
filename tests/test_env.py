"""The Environment box: it is a hard gate, so it must open itself, validate live and point at the wrong field.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_env.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)
import tempfile

from qgis.core import QgsApplication, QgsSettings
app = QgsApplication([], False)
from landslide_groundtruth import dock as D

class FakeBar:
    def __init__(s): s.msgs=[]
    def pushWarning(s,a,b): s.msgs.append(b); print("   [messageBar WARN]", b[:110])
    def pushInfo(s,a,b): pass
class FakeIface:
    def __init__(s): s.bar=FakeBar()
    def messageBar(s): return s.bar

class Stub:
    def __init__(self):
        self.settings = QgsSettings()
        self.iface = FakeIface()
        self.detection = None
    def _warn(self, t): self.iface.messageBar().pushWarning("Landslide", t)
    def _append_log(self, t): pass
    def _pick_python(self): pass
    def _pick_project(self): pass
    def _pick_out(self): pass
for m in ("_build_env_box","_env_ok","_refresh_env_status","env_gate"):
    setattr(Stub, m, getattr(D.LandslideDock, m))

# blank settings so this is a genuine first-run
QgsSettings().setValue("landslide/python",""); QgsSettings().setValue("landslide/project","")
QgsSettings().setValue("landslide/out","")

s = Stub()
box = s._build_env_box()
print("1. first run:", repr(box.title()))
print("   expanded =", not box.isCollapsed(), " (must be True — it is a hard gate)")
assert not box.isCollapsed()
print("   status   =", s.env_status.text())
assert "⚠" in s.env_status.text()
print("   placeholder(python) =", s.python_edit.placeholderText())
assert s.python_edit.placeholderText() and s.python_edit.toolTip()
print("   tooltip set =", bool(s.python_edit.toolTip()))

print("2. gate refuses and points at the box")
assert s.env_gate() is False
assert "Environment box" in s.iface.bar.msgs[-1]
assert not box.isCollapsed() and s.python_edit.styleSheet(), "must mark the offending field"
print("   offending field marked:", s.python_edit.styleSheet())

print("3. progressive validation")
d = tempfile.mkdtemp()
py = os.path.join(d, "python3"); open(py, "w").close()
s.python_edit.setText("/nope/python3"); print("   bad python  ->", s.env_status.text())
assert "no file at that Python path" in s.env_status.text()
s.python_edit.setText(py);            print("   good python ->", s.env_status.text())
assert "folder you downloaded" in s.env_status.text()
s.project_edit.setText(d);            print("   folder, no run_single.py ->", s.env_status.text())
assert "no run_single.py" in s.env_status.text()
open(os.path.join(d, "run_single.py"), "w").close()
s.project_edit.setText(""); s.project_edit.setText(d)
print("   complete    ->", s.env_status.text())
assert "✓" in s.env_status.text()

print("4. gate now passes, and the red border is cleared")
assert s.env_gate() is True
assert s.python_edit.styleSheet() == "", repr(s.python_edit.styleSheet())
print("   env_gate() ->", True, "; border cleared")

print("5. a configured user gets it collapsed next session")
s2 = Stub(); s2.settings = QgsSettings()
QgsSettings().setValue("landslide/python", py); QgsSettings().setValue("landslide/project", d)
box2 = s2._build_env_box()
print("   collapsed =", box2.isCollapsed())
assert box2.isCollapsed() is True
print("\nENV SMOKE TEST PASSED")
