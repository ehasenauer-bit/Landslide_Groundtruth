"""One change raster per grid, and a name that says what the pixels are.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_sar_naming.py

The change-detection render asks the data API for a px x px image of a
2*radius km box:

    px = min(2048, max(128, round(radius * 2 * 1000 / res)))

so the 2048 ceiling means the requested `res` stops having any effect past about
20 km radius at 20 m (10 km at 10 m) — the grid coarsens instead. Two things
followed, and both were silent:

  * the durable float32 export is named from the layer label plus
    _cd_settings_tag, and NEITHER carried the radius. With `res` no longer
    distinguishing runs above the cap, 20 km / 28 km / 45 km runs of one scene
    pair all wrote S1_change_log-ratio_..._20m_lee5_rn_a8.tif — three different
    rasters, one path, no warning, last one wins. The Fusion tab reads geometry
    from the file, so it would pair on the NEW extent while the layer tree still
    described the old run.

  * every pixel-denominated setting quietly changed meaning with it. "8 px" is
    0.32 ha at 20 m and 1.54 ha at a 45 km radius.

Mt Logan 2026-08-29 came within one changed dropdown of hitting the first: the
09:59 (45 km) and 11:07 (28 km) runs differed only because the blob sieve was
moved between them.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from landslide_groundtruth.sar_tab import SarTab

LABEL = "S1 change log-ratio 2026-08-18→2026-08-30 (t116 VV, 7×7)"
RADII = (20, 28, 45)


def px_for(radius, res):
    return int(min(2048, max(128, round(radius * 2 * 1000 / res))))


def meta_for(radius, res=20, **kw):
    px = px_for(radius, res)
    m = dict(res=res, radius=radius, px=px, eff_res=2000.0 * radius / px,
             speckle=("lee", 5), radionorm=True, min_area=8)
    m.update(kw)
    return m


def fname(meta):
    return f"{SarTab._safe_name(LABEL)}_{SarTab._cd_settings_tag(meta)}.tif"


print("=== 1. the cap really does ignore the Pixel size control ===")
for radius in RADII:
    e10 = 2000.0 * radius / px_for(radius, 10)
    e20 = 2000.0 * radius / px_for(radius, 20)
    print(f"   {radius:>2} km radius: asked 10 m -> {e10:5.2f} m, "
          f"asked 20 m -> {e20:5.2f} m")
assert abs(2000.0 * 28 / px_for(28, 10) - 2000.0 * 28 / px_for(28, 20)) < 1e-9, \
    "past the cap the two Pixel size choices must produce the SAME grid"
assert abs(2000.0 * 20 / px_for(20, 20) - 20.0) < 1e-9, "under it, 20 m is 20 m"

print("=== 2. the OLD tag collapsed three grids onto one filename ===")
def old_tag(meta):                       # the shipped tag before this fix
    sp = meta.get("speckle")
    parts = [f"{int(meta['res'])}m", f"{sp[0]}{sp[1]}", "rn", "a8"]
    return "_".join(parts)
old_names = {old_tag(meta_for(r)) for r in RADII}
print(f"   {len(RADII)} radii -> {len(old_names)} filename(s): {old_names.pop()}")
assert len(old_names) == 0, "the point of this test is that they were identical"

print("=== 3. the new tag gives each grid its own file ===")
names = {}
for radius in RADII:
    names[radius] = fname(meta_for(radius))
    print(f"   {radius:>2} km -> {names[radius]}")
assert len(set(names.values())) == len(RADII), \
    "three different grids must never share a path"

print("=== 4. the name states the EFFECTIVE resolution, not the request ===")
assert "_28km_27m_" in names[28], names[28]
assert "_45km_44m_" in names[45], names[45]
assert "_20km_20m_" in names[20], names[20]
print("   28 km reads 27m (not the 20m asked for); 45 km reads 44m")

print("=== 5. a rounded resolution alone would NOT have been enough ===")
# 27.8 and 28.0 km both round to 27 m but cover different ground, so the radius
# has to be in the key too — this is why the fix is not just 'use eff_res'.
a, b = meta_for(27.8), meta_for(28.0)
assert int(round(a["eff_res"])) == int(round(b["eff_res"])) == 27
assert fname(a) != fname(b), "radius must separate them when resolution cannot"
print(f"   27.8 km -> ...{SarTab._cd_settings_tag(a)}")
print(f"   28.0 km -> ...{SarTab._cd_settings_tag(b)}")

print("=== 6. the other pixel-changing settings still separate runs ===")
base = meta_for(28)
for field, val in (("speckle", ("med", 3)), ("min_area", 16),
                   ("radionorm", False)):
    other = dict(base); other[field] = val
    assert fname(other) != fname(base), field
print("   speckle, min_area and radiometric normalization each still key the file")

print("=== 7. older results with no radius recorded still produce a name ===")
legacy = dict(res=20, speckle=("lee", 5), radionorm=True, min_area=8)
print(f"   legacy meta -> {SarTab._cd_settings_tag(legacy)}")
assert SarTab._cd_settings_tag(legacy) == "20m_lee5_rn_a8", \
    "a result recorded before this fix must keep its old, readable name"
assert SarTab._cd_settings_tag({}) , "an empty meta must still yield something"

print("=== 8. what 'min change area' actually means at each radius ===")
for radius in RADII:
    e = meta_for(radius)["eff_res"]
    print(f"   {radius:>2} km radius: 8 px = {8 * e * e / 1e4:5.2f} ha, "
          f"7x7 window spans {7 * e:5.0f} m")
lo = 8 * meta_for(20)["eff_res"] ** 2
hi = 8 * meta_for(45)["eff_res"] ** 2
assert hi / lo > 4.5, "the drift this documents is real, not rounding"
print(f"   the same '8 px' setting is {hi / lo:.1f}x larger at 45 km than at 20 km")

print("\nSAR CHANGE-RASTER NAMING VERIFIED")
