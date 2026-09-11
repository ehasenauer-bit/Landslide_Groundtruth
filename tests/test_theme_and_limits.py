"""Every colour's contrast in both QGIS themes, and the grid-size guard.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_theme_and_limits.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

from qgis.core import QgsApplication
app=QgsApplication([],False)
from landslide_groundtruth import theme, limits, dock as D

print("=== THEME ===")
print("1. dock still re-exports everything the other tabs import")
from landslide_groundtruth.dock import (PRE_BG,POST_BG,ROW_FG,MUTED_FG,
    CLOUD_CLEAR,CLOUD_SOME,CLOUD_HEAVY,CLOUD_UNKNOWN,
    CLOUD_GREEN_MAX,CLOUD_AMBER_MAX,STATUS_COLORS)
print("   sar_tab's set + planet_tab's set: importable from .dock")

print("2. structural values unchanged (row tints, thresholds)")
assert (PRE_BG.red(),PRE_BG.green(),PRE_BG.blue())==(220,235,252)
assert (POST_BG.red(),POST_BG.green(),POST_BG.blue())==(224,244,226)
assert (ROW_FG.red(),ROW_FG.green(),ROW_FG.blue())==(20,20,20)
assert (MUTED_FG.red(),MUTED_FG.green(),MUTED_FG.blue())==(120,120,120)
assert (CLOUD_GREEN_MAX,CLOUD_AMBER_MAX)==(10.0,40.0)
print("   row tints and thresholds identical")

print("3. EVERY token clears 4.5:1 where it is actually drawn")
def lum(hexs):
    hexs=hexs.lstrip("#"); r,g,b=(int(hexs[i:i+2],16)/255 for i in (0,2,4))
    f=lambda c: c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4
    return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b)
def ratio(a,b):
    la,lb=lum(a),lum(b); hi,lo=max(la,lb),min(la,lb); return (hi+0.05)/(lo+0.05)
def qc(c): return "#%02x%02x%02x"%(c.red(),c.green(),c.blue())
LIGHT,DARK="#F0F0F0","#333333"
bad=0
print("   status, light theme (drawn on #F0F0F0):")
for k in ("success","error","warn"):
    c=theme.STATUS_COLORS_LIGHT[k]; r=ratio(c,LIGHT); ok=r>=4.5
    print(f"     {k:8s} {c}  {r:.2f}:1  {'ok' if ok else 'FAIL'}"); bad+=(not ok)
print("   status, dark theme (drawn on #333333):")
for k in ("success","error","warn"):
    c=theme.STATUS_COLORS_DARK[k]; r=ratio(c,DARK); ok=r>=4.5
    print(f"     {k:8s} {c}  {r:.2f}:1  {'ok' if ok else 'FAIL'}"); bad+=(not ok)
print("   cloud, on the plugin's own row tints:")
for nm,c in (("clear",CLOUD_CLEAR),("some",CLOUD_SOME),
             ("heavy",CLOUD_HEAVY),("unknown",CLOUD_UNKNOWN)):
    rp,rq=ratio(qc(c),qc(PRE_BG)),ratio(qc(c),qc(POST_BG)); ok=min(rp,rq)>=4.5
    print(f"     {nm:8s} {qc(c)}  PRE {rp:.2f}:1  POST {rq:.2f}:1  {'ok' if ok else 'FAIL'}"); bad+=(not ok)
for nm,c in (("ROW_FG/PRE",(qc(ROW_FG),qc(PRE_BG))),("ROW_FG/POST",(qc(ROW_FG),qc(POST_BG)))):
    r=ratio(*c); ok=r>=4.5
    print(f"     {nm:11s} {r:.2f}:1  {'ok' if ok else 'FAIL'}"); bad+=(not ok)
assert bad==0, f"{bad} contrast failures"

print("3b. one colour genuinely cannot serve both themes")
need_light=(lum(LIGHT)+0.05)/4.5-0.05
need_dark=4.5*(lum(DARK)+0.05)-0.05
print(f"   light needs L<={need_light:.3f}, dark needs L>={need_dark:.3f} -> disjoint")
assert need_light < need_dark
assert theme.STATUS_COLORS_LIGHT["success"]!=theme.STATUS_COLORS_DARK["success"]

print("3c. status_color() follows the running palette")
from qgis.PyQt.QtGui import QPalette, QColor as QC
from qgis.PyQt.QtWidgets import QApplication
qa=QApplication.instance()
pal=qa.palette(); pal.setColor(QPalette.Window, QC("#333333")); qa.setPalette(pal)
assert theme.is_dark_theme() is True
assert theme.status_color("success")==theme.STATUS_COLORS_DARK["success"]
print("   dark palette  ->", theme.status_color("success"))
pal.setColor(QPalette.Window, QC("#F0F0F0")); qa.setPalette(pal)
assert theme.is_dark_theme() is False
assert theme.status_color("success")==theme.STATUS_COLORS_LIGHT["success"]
print("   light palette ->", theme.status_color("success"))

print("4. colour is never the only signal")
for k in ("success","error","warn"):
    assert theme.STATUS_GLYPH[k], k
    assert theme.status_text(k,"x").split()[0]==theme.STATUS_GLYPH[k]
print("   every status colour has a glyph:", {k:theme.STATUS_GLYPH[k] for k in ("success","error","warn")})

print("\n=== LIMITS ===")
print("5. the real combinations")
CASES=[
 # (name, w_m, h_m, res_m, allowed, must_caution)
 # 17 km error -> 34 km box at ArcticDEM's 2 m is 289 Mpx: genuinely large, but
 # it IS the real workflow, so it must be ALLOWED with a caution, not refused.
 ("your 17 km detection @ 2 m", 34000,34000,2, True,  True),
 ("17 km @ 10 m",               34000,34000,10, True, False),
 ("the 50 km / 2 m worst case", 100000,100000,2, False, True),
 ("5 km default @ 2 m",         10000,10000,2, True,  False),
 ("typical scar 2 km @ 1 m",     2000,2000,1, True,   False),
 ("30 km @ 3 m (just over warn)",30000,30000,3, True, True),
 ("34 km @ 4 m (comfortable)",   34000,34000,4, True, False),
 ("50 km @ 1 m (worse still)", 100000,100000,1, False, True),
]
for name,w,h,r,should_pass,must_caution in CASES:
    ok,msg = limits.check_grid(w,h,r,"terrain grid")
    cells = limits.grid_cells(w,h,r)
    good = (ok==should_pass) and (bool(msg)==must_caution)
    print(f"   [{'ok ' if good else 'FAIL'}] {name:28s} {cells/1e6:9.0f} Mpx  allowed={ok}")
    if msg: print(f"          {msg[:104]}")
    assert good,(name,ok,msg)

print("\n6. the refusal names BOTH ways out, with usable numbers")
ok,msg = limits.check_grid(100000,100000,2,"terrain grid")
assert not ok
import re
m=re.search(r"about (\d+(?:\.\d+)?) m", msg); assert m, msg
coarse=float(m.group(1))
assert limits.check_grid(100000,100000,coarse)[0], "the suggested resolution must actually fit"
m2=re.search(r"about (\d+(?:\.\d+)?) km across", msg); assert m2, msg
side=float(m2.group(1))*1000
assert limits.check_grid(side,side,2)[0], "the suggested area must actually fit"
print(f"   suggested {coarse:g} m -> fits; suggested {side/1000:g} km @ 2 m -> fits")

print("\n7. degenerate inputs never crash")
for args in ((0,0,1),(1000,1000,0),(-5,10,1),(None,10,1),("x",10,1),(10,10,None)):
    ok,msg = limits.check_grid(*args)
    assert ok is True and msg=="", (args,ok,msg)
print("   zero/negative/None/str all pass through silently")
print("   describe(0,0,0) =", repr(limits.describe(0,0,0)))

print("\n8. guards are actually wired in")
for f,needle in (("viewer3d_tab.py","limits.check_grid"),("volume_tab.py","limits.check_grid")):
    src=open(os.path.join(PLUG, f)).read()
    assert needle in src, f
    print(f"   {f}: guarded")
print("\nTHEME + LIMITS VERIFIED")
