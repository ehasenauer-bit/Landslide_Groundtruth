"""Download defaults, no seasonal box in the dock, and the future/winter window guard.

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
# Chosen 2026-10-21: HDR as the picture, dBright as the change evidence. A
# default run must still carry change evidence, or it cannot answer the
# question the plugin exists to answer.
assert set(D.DEFAULT_SCENES)=={"highlight_hdr","dbright"}, D.DEFAULT_SCENES
assert "swir_falsecolor" not in D.DEFAULT_SCENES, \
    "SWIR false colour is a single-date composite, not a change detector, and PlanetScope has none"
off=[k for k in keys if k not in D.DEFAULT_SCENES]
print("   on :", list(D.DEFAULT_SCENES))
print("   off:", off)
assert set(off)=={"true_color","highlight_natural","false_color","ndvi","swir_falsecolor",
                  "dndvi","dndsi"}, off

print("\n=== 1b. every dock checkbox is a real review_package scene ===")
# The dock can't import the project (different interpreter), so the two lists are
# duplicated by hand and have to be checked at the source level — the same trick
# section 5 uses for run_single.py. A checkbox whose token review_package doesn't
# know is silently ignored by the export: the user ticks it and no layer appears.
rp_src=open(os.path.join(ROOT,"review_package.py")).read()
block=rp_src.split("SCENE_KEYS = [",1)[1].split("]",1)[0]
rp_keys={t.strip().strip('"\'') for t in block.split(",") if t.strip()}
print("   review_package.SCENE_KEYS =", sorted(rp_keys))
assert set(keys)==rp_keys, (sorted(set(keys)^rp_keys),)
print("   dock SCENES match, both ways:", len(keys), "keys")

print("\n=== 1c. a failing HDR cannot take the change rasters with it ===")
# highlight_hdr holds the whole AOI in float through one scene-fitted curve —
# several GB on a 20 km AOI, so MemoryError is a realistic outcome on a machine
# that is merely busy. Now that it is ON by default, every large-AOI run is
# exposed to that; dBright/dNDSI are the layers the run exists to produce, and
# losing the whole package to the picture would be the worst possible trade, so
# the HDR write is the one product allowed to fail softly. Source-level because
# review_package needs rioxarray, which QGIS's own python does not carry (the
# venv runs it end-to-end instead).
assert "tolerate=(MemoryError,)" in rp_src, \
    "the HDR write must tolerate MemoryError so the default picture can't lose the run"
assert 'def write_sides(kind, render, source="", when=None, tolerate=()):' in rp_src, \
    "write_sides must default to tolerating NOTHING — every other product fails loudly"
hdr_branch=rp_src.split('if "highlight_hdr" in want:',1)[1].split("\n\n",1)[0]
assert "tolerate=(MemoryError,)" in hdr_branch, hdr_branch
print("   HDR tolerates MemoryError; every other product still fails loudly")

print("\n=== 2. the window guard ===")
class Stub:
    def __init__(s, when, post=90, lat="60.5"):
        s.dt_edit=QDateTimeEdit(); s.dt_edit.setDateTime(QDateTime(when))
        s.post_slider=QSlider(Qt.Horizontal); s.post_slider.setRange(1,365); s.post_slider.setValue(post)
        s.lat_edit=QLineEdit(lat)
        s.window_warn=QLabel()
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

print("\n=== 3. the 'compare against last summer' box is gone ===")
dsrc=open(os.path.join(PLUG, "dock.py")).read()
assert "seasonal_check" not in dsrc and '"--seasonal"' not in dsrc, \
    "the winter box was removed on request: the dock must never shift the pre window"
print("   dock.py has no seasonal box and never passes --seasonal")
assert '"--seasonal"' not in open(os.path.join(ROOT, "run_single.py")).read(), \
    "--seasonal was removed from the CLI too"
print("   run_single.py no longer accepts --seasonal")
print("\nDEFAULTS + GUARD VERIFIED")
