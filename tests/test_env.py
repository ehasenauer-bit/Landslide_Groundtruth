"""The Setup box: a hard gate, so it must open itself, say what to do, and track the install.

Covers both ways the pipeline can run — the imagery tools the plugin installs
itself (environment.py, faked here with a marker file: tests/test_setup_task.py
does a real install) and "Use my own Python" — and the states in between.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_env.py
"""
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TOOLS = tempfile.mkdtemp(prefix="landslide_tools_")
os.environ["LANDSLIDE_TOOLS_DIR"] = TOOLS          # never the user's real install
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsSettings
app = QgsApplication([], False)
from landslide_groundtruth import dock as D
from landslide_groundtruth import environment as E

class FakeBar:
    def __init__(s): s.msgs=[]
    def pushWarning(s,a,b): s.msgs.append(b); print("   [messageBar WARN]", b[:110])
    def pushInfo(s,a,b): pass
    def pushSuccess(s,a,b): pass
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
    # the 'Results now go to' line reads QgsProject, which this bare test never sets
    # up (first touching it here drops a symbology-style.db in the working folder);
    # tests/test_results_follow_project.py covers it on a real dock
    def _refresh_results_dir(self, *_): pass
for m in ("_build_env_box", "_env_ok", "_refresh_env_status", "env_gate",
          "pipeline_python", "pipeline_dir", "pipeline_launch", "_save_settings",
          "_start_setup", "_cancel_setup", "_remove_setup", "_setup_line",
          "_setup_done", "_pipeline_running"):
    setattr(Stub, m, getattr(D.LandslideDock, m))

def blank_settings():
    for k in ("python", "project", "out"):
        QgsSettings().setValue(f"landslide/{k}", "")

def fake_install(lock=None):
    """What a finished SetupTask leaves behind: the env's python and env.json."""
    py = E.env_python()
    os.makedirs(os.path.dirname(py), exist_ok=True)
    open(py, "w").close()
    with open(E.marker_path(), "w") as f:
        json.dump({"lock": E.lock_digest() if lock is None else lock,
                   "installed": "2026-11-02 12:00"}, f)

blank_settings()
s = Stub()
box = s._build_env_box()
print("1. first run, nothing installed:", repr(box.title()))
assert not box.isCollapsed(), "it is a hard gate — must open itself"
print("   status   =", s.env_status.text())
assert "⚠" in s.env_status.text() and "aren't installed" in s.env_status.text()
assert s.setup_btn.isVisibleTo(box) and s.setup_btn.text() == "Install imagery tools"
assert s.setup_btn.font().bold(), "Install is the primary action"
assert not s.setup_remove_btn.isVisibleTo(box), "nothing to remove yet"
assert s.env_adv_box.isCollapsed(), "own-Python fields stay out of a new user's way"
assert s.python_edit.placeholderText() and s.python_edit.toolTip()
assert f"{E.DOWNLOAD_MB} MB" in box.findChildren(D.QLabel)[0].text(), "says the cost"

print("2. gate refuses and points at Install")
assert s.env_gate() is False
assert "Install imagery tools" in s.iface.bar.msgs[-1], s.iface.bar.msgs[-1]
assert s.pipeline_launch() is None
assert not box.isCollapsed()

print("3. installed -> ready, the gate passes and the launch uses the installed python")
fake_install()
s._refresh_env_status()
print("   status   =", s.env_status.text())
assert "✓" in s.env_status.text()
assert not s.setup_btn.isVisibleTo(box) and s.setup_remove_btn.isVisibleTo(box)
py, script, cwd = s.pipeline_launch()
assert py == E.env_python(), py
assert script == os.path.join(E.bundled_pipeline_dir(), "run_single.py")
assert os.path.isfile(script) and cwd == E.bundled_pipeline_dir()

print("4. a plugin update with a new lock -> stale: still runs, offers Update")
fake_install(lock="older")
s._refresh_env_status()
print("   status   =", s.env_status.text())
assert E.status()[0] == E.STALE and s.env_gate() is True
assert s.setup_btn.isVisibleTo(box) and s.setup_btn.text() == "Update imagery tools"
assert "⚠" in s.env_status.text()

print("5. env folder damaged -> broken: refuses, offers Repair")
os.remove(E.env_python())
s._refresh_env_status()
assert E.status()[0] == E.BROKEN and s.env_gate() is False
assert s.setup_btn.text() == "Repair imagery tools"
os.remove(E.marker_path())
assert E.status()[0] == E.BROKEN, "an unfinished install (folder, no marker) is broken"
E.remove()
assert not os.path.exists(TOOLS) and E.status()[0] == E.MISSING
print("   removed  ->", E.status())

print("6. own Python (advanced): progressive validation, field marked, then cleared")
d = tempfile.mkdtemp()
py = os.path.join(d, "python3"); open(py, "w").close()
s.python_edit.setText("/nope/python3"); print("   bad python  ->", s.env_status.text())
assert "no file at the Python path" in s.env_status.text()
assert s.env_gate() is False and not s.env_adv_box.isCollapsed()
assert s.python_edit.styleSheet(), "must mark the offending field"
s.python_edit.setText(py)
s.project_edit.setText(d);            print("   folder, no run_single.py ->", s.env_status.text())
assert "no run_single.py" in s.env_status.text()
open(os.path.join(d, "run_single.py"), "w").close()
s.project_edit.setText(""); s.project_edit.setText(d)
print("   complete    ->", s.env_status.text())
assert "✓" in s.env_status.text() and "own Python" in s.env_status.text()
assert not s.setup_btn.isVisibleTo(box), "an own Python needs no install"
assert s.env_gate() is True
assert s.python_edit.styleSheet() == "", repr(s.python_edit.styleSheet())
assert s.pipeline_launch() == (py, os.path.join(d, "run_single.py"), d)
print("   saved to QgsSettings as typed:", QgsSettings().value("landslide/python"))
assert QgsSettings().value("landslide/python") == py

print("7. a configured user gets it collapsed next session")
box2 = Stub()._build_env_box()
assert box2.isCollapsed() is True
blank_settings()
print("\nENV SMOKE TEST PASSED")
