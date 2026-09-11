"""Download defaults, the --seasonal flag, and the future/winter window guard.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_defaults.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
app=QgsApplication([],False)
from qgis.PyQt.QtWidgets import QLineEdit, QLabel, QCheckBox, QSlider, QDateTimeEdit, QPlainTextEdit
from qgis.PyQt.QtCore import Qt, QDateTime
from landslide_groundtruth import dock as D
from datetime import datetime, timedelta

print("=== 1. default download set ===")
print("   DEFAULT_SCENES =", D.DEFAULT_SCENES)
keys=[k for k,_l,_t in D.SCENES]
for k in D.DEFAULT_SCENES: assert k in keys, k
change={"dndvi","dndsi","dbright"}
assert change <= set(D.DEFAULT_SCENES), "the change layers must be on by default"
assert "swir_falsecolor" not in D.DEFAULT_SCENES, \
    "SWIR false colour is a single-date composite, not a change detector, and PlanetScope has none"
off=[k for k in keys if k not in D.DEFAULT_SCENES]
print("   on :", list(D.DEFAULT_SCENES))
print("   off:", off)
assert set(off)=={"true_color","false_color","ndvi","swir_falsecolor"}, off

print("\n=== 2. the window guard ===")
class Stub:
    def __init__(s, when, post=90, lat="60.5"):
        s.dt_edit=QDateTimeEdit(); s.dt_edit.setDateTime(QDateTime(when))
        s.post_slider=QSlider(Qt.Horizontal); s.post_slider.setRange(1,365); s.post_slider.setValue(post)
        s.lat_edit=QLineEdit(lat)
        s.seasonal_check=QCheckBox(); s.window_warn=QLabel()
setattr(Stub,"_check_event_window",D.LandslideDock._check_event_window)
now=datetime.utcnow()
CASES=[
 ("future event",      now+timedelta(days=10), 90, "60.5", "future"),
 ("today, 90d after",  now,                    90, "60.5", "past today"),
 ("2 yrs ago summer",  datetime(now.year-2,7,15), 90,"60.5", None),
 ("2 yrs ago FEBRUARY",datetime(now.year-2,2,4),  90,"60.5", "winter event"),
 ("Feb but low lat",   datetime(now.year-2,2,4),  90,"34.0", None),
 ("Feb, no lat typed", datetime(now.year-2,2,4),  90,"",     None),
]
for name,when,post,lat,want in CASES:
    s=Stub(when,post,lat); s._check_event_window()
    txt=s.window_warn.text()
    vis=s.window_warn.isVisible() or bool(txt)
    ok = (want is None and not txt) or (want and want in txt)
    print(("   ok   " if ok else "   FAIL ")+f"{name:20s} -> {txt[:74] or '(no warning)'}")
    assert ok,(name,txt)

print("\n=== 3. winter auto-ticks the seasonal box, once ===")
s=Stub(datetime(now.year-2,2,4),90,"60.5")
assert not s.seasonal_check.isChecked()
s._check_event_window()
assert s.seasonal_check.isChecked(), "winter must auto-tick"
print("   auto-ticked:", s.seasonal_check.isChecked())
s.seasonal_check.setChecked(False)          # user overrides
s._check_event_window()
assert not s.seasonal_check.isChecked(), "must NOT re-tick after the user clears it"
print("   user override respected on re-check:", not s.seasonal_check.isChecked())

print("\n=== 4. summer never ticks it ===")
s2=Stub(datetime(now.year-2,7,15),90,"60.5"); s2._check_event_window()
assert not s2.seasonal_check.isChecked()
print("   summer -> unticked")

print("\n=== 5. --seasonal reaches the CLI, and run_single accepts it ===")
import subprocess
rs=os.path.join(ROOT, "run_single.py")
src=open(rs).read()
assert '"--seasonal"' in src and '"--event-id"' in src
print("   run_single.py declares --seasonal and --event-id")
dsrc=open(os.path.join(PLUG, "dock.py")).read()
assert 'args += ["--seasonal"]' in dsrc
print("   dock.py now passes it")
print("\nDEFAULTS + SEASONAL + GUARD VERIFIED")
