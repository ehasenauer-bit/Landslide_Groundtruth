"""Install imagery tools: the dock's button, the task, and what each outcome leaves.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_setup_task.py

By default SetupTask.run is replaced by a fast fake, so this checks the WIRING —
button -> QgsTaskManager -> log/progress -> done — and the three outcomes
(success, failure, cancel) without the network. With LANDSLIDE_REAL_INSTALL=1 it
also does the real thing into a throwaway folder (about 260 MB, 2-5 minutes):
uv, Python 3.12, requirements.lock, setup_check.py, then the status it reports.
Run that after changing requirements.lock or environment.py.
"""
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TOOLS = tempfile.mkdtemp(prefix="landslide_tools_")
os.environ["LANDSLIDE_TOOLS_DIR"] = TOOLS
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication, QgsSettings
QgsApplication.setPrefixPath(os.environ.get(
    "QGIS_PREFIX", "/Applications/QGIS-LTR.app/Contents/MacOS"), True)
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


class FakeBar(QObject):
    def __init__(s): super().__init__(); s.msgs = []
    def pushMessage(s, *a, **k): pass
    def pushInfo(s, t, m): s.msgs.append(("info", m))
    def pushWarning(s, t, m): s.msgs.append(("warn", m))
    def pushCritical(s, t, m): s.msgs.append(("crit", m))
    def pushSuccess(s, t, m): s.msgs.append(("ok", m))
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s, t): pass
    def unsetMapTool(s, t): pass
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar = FakeBar(); s._c = FakeCanvas(); s._w = QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


saved = {k: QgsSettings().value(f"landslide/{k}", "") for k in ("python", "project", "out")}
for k in saved:
    QgsSettings().setValue(f"landslide/{k}", "")
from landslide_groundtruth import environment as E
from landslide_groundtruth.dock import LandslideDock

iface = FakeIface()
dock = LandslideDock(iface)


def wait_done(timeout=600):
    t0 = time.time()
    while dock._setup_task is not None and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        app.processEvents()


real_run = E.SetupTask.run


def fake_run(outcome):
    def run(self):
        self.logLine.emit("[1/4] Downloading uv… (fake)")
        self.setProgress(50)
        if outcome == "cancel":
            while not self.isCanceled():
                time.sleep(0.01)
            self.error = "Cancelled."
            return False
        if outcome == "fail":
            self.error = "uv stopped with exit code 2 — the lines above say why."
            self.logLine.emit(self.error)
            return False
        py = E.env_python()
        os.makedirs(os.path.dirname(py), exist_ok=True)
        open(py, "w").close()
        with open(E.marker_path(), "w") as f:
            json.dump({"lock": E.lock_digest(), "installed": "2026-11-02 12:00"}, f)
        return True
    return run


print("=== a new user: the box is open and offers Install ===")
check("Setup box starts open", not dock.env_box.isCollapsed())
check("Install is offered", dock.setup_btn.isVisibleTo(dock.env_box)
      and dock.setup_btn.text() == "Install imagery tools")

print("\n=== a failed install says so and stays installable ===")
E.SetupTask.run = fake_run("fail")
dock._start_setup()
check("while running: Cancel shown, Install disabled",
      dock.setup_cancel_btn.isVisibleTo(dock.env_box) and not dock.setup_btn.isEnabled())
wait_done()
check("a warning names the failure",
      any(k == "warn" and "Installing the imagery tools failed" in m
          for k, m in iface._bar.msgs))
check("the log is kept on screen with the reason",
      "exit code 2" in dock.setup_log.toPlainText()
      and dock.setup_log.isVisibleTo(dock.env_box))
check("still not installed, Install enabled again",
      E.status()[0] == E.MISSING and dock.setup_btn.isEnabled())

print("\n=== Cancel stops it quietly ===")
E.SetupTask.run = fake_run("cancel")
iface._bar.msgs.clear()
dock._start_setup()
for _ in range(20):
    app.processEvents(); time.sleep(0.01)
dock._cancel_setup()
wait_done()
check("cancel reports as info, not as a failure",
      any(k == "info" and "cancelled" in m.lower() for k, m in iface._bar.msgs)
      and not any(k == "warn" for k, _ in iface._bar.msgs))
check("nothing marked ready", not E.usable(E.status()[0]))

print("\n=== success: ready, box folds away, searches pass the gate ===")
E.SetupTask.run = fake_run("ok")
iface._bar.msgs.clear()
dock._start_setup()
wait_done()
check("success message", any(k == "ok" for k, _ in iface._bar.msgs))
check("status ready", E.status()[0] == E.READY)
check("box collapses", dock.env_box.isCollapsed())
check("Install hidden, Remove offered",
      not dock.setup_btn.isVisibleTo(dock.env_box)
      and dock.setup_remove_btn.isVisibleTo(dock.env_box))
launch = dock.pipeline_launch()
check("a search launches the installed python",
      launch is not None and launch[0] == E.env_python())

if os.environ.get("LANDSLIDE_REAL_INSTALL") == "1":
    print("\n=== the real install (network) ===")
    E.SetupTask.run = real_run
    E.remove()
    t0 = time.time()
    dock._start_setup()
    wait_done(timeout=1800)
    print("   log tail:", dock.setup_log.toPlainText().splitlines()[-3:])
    check(f"real install ready in {time.time() - t0:.0f} s", E.status()[0] == E.READY)
    check("the installed python runs the pipeline's smoke test",
          "The imagery tools are ready." in dock.setup_log.toPlainText())
    print(f"   size: {E.installed_size_bytes() / 1e9:.2f} GB")

E.SetupTask.run = real_run
E.remove()
for k, v in saved.items():
    QgsSettings().setValue(f"landslide/{k}", v)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
