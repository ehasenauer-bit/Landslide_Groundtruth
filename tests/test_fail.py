"""A failed run must say what broke and what to do — and must NOT cry wolf on ordinary log lines.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_fail.py
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
from qgis.PyQt.QtWidgets import QPlainTextEdit
from landslide_groundtruth import dock as D
from landslide_groundtruth.task import PipelineTask, failure_hint

# ---- 1. a REAL failing subprocess, end to end -----------------------------
print("=== 1. real subprocess that dies the way a bad venv python does ===")
d=tempfile.mkdtemp()
script=os.path.join(d,"run_single.py")
open(script,"w").write("import sys\n"
    "print('[planet] searching 20240401_182512_31_2451')\n"
    "sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "sys.stderr.write(\"ModuleNotFoundError: No module named 'rasterio'\\n\")\n"
    "sys.exit(1)\n")
t=PipelineTask("/usr/bin/python3", script, d, [], d)  # a python that CAN run standalone
lines=[]
t.logLine.connect(lines.append)
ok=t.run()
print("   run() ->", ok, "| result:", t.result)
print("   log:")
for l in lines: print("     ", l)
assert ok is False and t.result is None
tail="\n".join(lines)
h=failure_hint(tail)
print("   hint ->", h[:78])
assert "missing the imagery tools" in h
assert "credentials" not in h, "the 20240401 scene id must not trigger the 401 hint"
print("   (note the log contains 20240401 — no false 401 hint)")

# ---- 2. the dock turns that into a real message ---------------------------
print("\n=== 2. dock.report_failure ===")
class FakeBar:
    def __init__(s): s.msgs=[]
    def pushWarning(s,a,b): s.msgs.append(b)
    def pushInfo(s,a,b): pass
class FakeIface:
    def __init__(s): s.bar=FakeBar()
    def messageBar(s): return s.bar
class Stub:
    def __init__(s):
        s.iface=FakeIface(); s.log=QPlainTextEdit()
        from collections import deque; s._log_tail=deque(maxlen=80)
    def _warn(s,t): s.iface.messageBar().pushWarning("Landslide", t.replace("\n"," "))
for m in ("_append_log","_log_tail_text","report_failure"):
    setattr(Stub,m,getattr(D.LandslideDock,m))
s=Stub()
for l in lines: s._append_log(l)
msg=s.report_failure("The imagery run did not finish.")
print("   ->", msg)
assert "did not finish" in msg and "pip install" in msg

print("\n=== 3. an UNRECOGNISED failure still says where to look ===")
s2=Stub(); s2._append_log("[s2] something odd"); s2._append_log("giving up")
m2=s2.report_failure("The scene search did not finish.")
print("   ->", m2)
assert "See the Run log below" in m2

print("\n=== 4. tail is bounded (no unbounded growth on a long run) ===")
s3=Stub()
for i in range(5000): s3._append_log(f"line {i}")
assert len(s3._log_tail)==80, len(s3._log_tail)
assert s3._log_tail_text(25).count("\n")==24
print("   5000 lines logged -> tail holds", len(s3._log_tail))

print("\n=== 5. report_failure before any logging (no AttributeError) ===")
class Bare(Stub):
    def __init__(s):
        s.iface=FakeIface(); s.log=QPlainTextEdit()   # NO _log_tail
for m in ("_append_log","_log_tail_text","report_failure"):
    setattr(Bare,m,getattr(D.LandslideDock,m))
b=Bare()
print("   ->", b.report_failure("The imagery run did not finish."))
b._append_log("late line")   # must self-heal the deque
assert len(b._log_tail)==1
print("   survived with no deque, and self-healed on next log")
print("\nFAILURE-ESCALATION VERIFIED")
