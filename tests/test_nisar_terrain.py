"""NISAR scenes get the layover fade and the merge's terrain masks from their
OWN look geometry — the SAR tab's wiring of what nisar_read measures.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_nisar_terrain.py

Like test_sar_two_radars, the methods under test are lifted OUT of sar_tab.py
(via ast, with the module-level helpers they call) and bound to a stub tab, so
the shipped source is what runs; layover_dim and sar_change are the real ones.

Why it matters: NISAR looks LEFT. An ascending NISAR pass at Iliamna looks
241.4 deg (WSW — measured from the track-150 granule's radarGrid), so it is
lit from the ENE and lays over EAST-facing slopes, where an ascending
Sentinel-1 pass is lit from the WSW and lays over WEST-facing ones. Feeding a
NISAR scene to the Sentinel-1 orbit model would fade and mask exactly the
wrong slopes, which is why the tab used to skip both for NISAR. What is pinned:

  * the geometry the read task hands back reaches the place each feature
    reads it (the amplitude candidate, the change map's post role, the
    _cd_results entry) without mutating the search result itself;
  * with it, the fade and the masks are exactly layover_dim's answer for the
    measured look and incidence — the east face, not the west;
  * without it (no radarGrid, or one that failed nisar_read's cross-checks)
    both are skipped with the reason logged, never silently modelled as S1;
  * Sentinel-1 scenes go through exactly the calls they always did.
"""
import ast
import math
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
SRC = os.path.join(PKG, "landslide_groundtruth", "sar_tab.py")
sys.path.insert(0, PKG)

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from landslide_groundtruth import layover_dim as ld  # noqa: E402
from landslide_groundtruth import nisar_read as nr  # noqa: E402
from landslide_groundtruth import sar_change as sc  # noqa: E402

TOP = {"S1", "NISAR", "CD_RAMP", "NISAR_GEOM_KEYS", "_sensor", "_nisar_look"}
METHODS = {"_nisar_amp_done", "_nisar_cd_done", "_terrain_masks",
           "_write_change_with_fade", "_apply_layover_amplitude"}


class _Qt:
    WaitCursor = 3


class _QApplication:
    @staticmethod
    def setOverrideCursor(_c):
        pass

    @staticmethod
    def restoreOverrideCursor():
        pass


def _lift():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    body, methods = [], []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in TOP for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in TOP:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "SarTab":
            methods = [n for n in node.body
                       if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    found = {getattr(n, "name", None) for n in methods}
    assert found == METHODS, f"missing in sar_tab.py: {METHODS - found}"
    cls = ast.ClassDef(name="Tab", bases=[], keywords=[], body=methods,
                       decorator_list=[])
    mod = ast.Module(body=body + [cls], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"layover_dim": ld, "sar_change": sc, "Qt": _Qt,
          "QApplication": _QApplication}
    exec(compile(mod, SRC, "exec"), ns)
    return ns


NS = _lift()
Tab, S1, NISAR = NS["Tab"], NS["S1"], NS["NISAR"]

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


TMP = tempfile.mkdtemp(prefix="test_nisar_terrain_")
LAT, LON = 60.0272, -153.061
N = 120
# the SAR tab's AOI grid: north-up EPSG:4326, ~28 m pixels at 60 N
GT = (LON - 0.03, 0.0005, 0.0, LAT + 0.015, 0.0, -0.00025)
WKT = nr._srs(4326).ExportToWkt()
dx, dy = ld.metric_pixel_size(GT, N)
# a north-south ridge with two 45 deg faces: the EAST face (cols 60-114) falls
# to the east, the WEST face (cols 6-60) to the west
_col = np.arange(N)
_east_m = (_col - 60) * dx
DEM = np.tile(np.maximum(0.0, 1500.0 - np.abs(_east_m) * math.tan(math.radians(45.0))),
              (N, 1)).astype(np.float32)
EAST = np.zeros((N, N), bool)
EAST[:, 75:100] = True                      # mid-face, off the crest and the foot
WEST = np.zeros((N, N), bool)
WEST[:, 20:45] = True

# the look measured on the Iliamna track-150 granule (ascending), at NISAR's
# near range (33 deg) rather than Iliamna's 46: shadow starts at 90 - 46 = 44
# deg and layover at 46, so at 46 deg a 45 deg face is lost to EITHER look and
# the two sides cannot be told apart; at 33 the face toward the radar lays over
# (eff. 41 deg > 33) while the face away is lit (41 < 57). Plus its left-
# looking mirror for a descending pass.
GEOM_ASC = dict(look_azimuth_deg=241.4, incidence_deg=33.0,
                geometry_note="from the granule's radarGrid (EPSG:3413, Left-looking)")
GEOM_DESC = dict(look_azimuth_deg=118.6, incidence_deg=40.0,
                 geometry_note="from the granule's radarGrid (EPSG:3413, Left-looking)")
NO_GEOM = dict(look_azimuth_deg=None, incidence_deg=None,
               geometry_note="no radarGrid cube in the granule")


class Check:
    def __init__(self, on):
        self.on = on

    def isChecked(self):
        return self.on


def make_tab(fade=True, dem=DEM):
    t = Tab()
    t.layover_check = Check(fade)
    t.log = []
    t._append_log = t.log.append
    t._aoi_dem = lambda _gt, _shape: dem
    t._cd_meta = None
    t._nisar_cd_batch = t._nisar_amp_batch = 7
    t._nisar_amp_jobs = {}
    t._nisar_amp_task = object()
    t.role_done, t.ready = [], []
    t._cd_role_done = lambda role, path, err: t.role_done.append((role, path, err))
    t._amp_file_ready = lambda path, label, cand: t.ready.append(cand) or True
    t._amp_one_done = lambda: None
    t._amp_failed = lambda label, why: t.ready.append(("failed", label, why))
    return t


def nisar_scene(orbit="ascending"):
    return {"source": NISAR, "orbit_state": orbit, "relative_orbit": 150,
            "date": "2026-08-11", "h5_url": "https://example.test/x.h5"}


def result(radar, direction, **geom):
    return dict(radar=radar, direction=direction, gt=GT, shape=(N, N),
                footprint=None, **geom)


print("=== the read task's geometry reaches the scene it describes ===")
t = make_tab()
post, pre = nisar_scene(), nisar_scene()
t._cd_meta = {"roles": {"post": post, "pre0": pre}}
t._nisar_cd_done(7, "post", "/tmp/post.tif", "", dict(GEOM_ASC))
roles = t._cd_meta["roles"]
check("change detection: the post role carries the measured look",
      roles["post"]["look_azimuth_deg"] == 241.4
      and roles["post"]["incidence_deg"] == 33.0)
check("  on this compute's own copy — the search result is not mutated",
      "look_azimuth_deg" not in post and roles["post"] is not post)
check("  and the role still completes", t.role_done == [("post", "/tmp/post.tif", "")])
t._nisar_cd_done(7, "pre0", "", "HTTPError: 500", {})
check("a failed read copies nothing", "look_azimuth_deg" not in roles["pre0"])
t._nisar_cd_done(6, "pre0", "/tmp/old.tif", "", dict(GEOM_DESC))
check("an abandoned batch is ignored entirely",
      "look_azimuth_deg" not in roles["pre0"] and len(t.role_done) == 2)

t = make_tab()
scene = nisar_scene()
t._nisar_amp_jobs = {"amp0": ("NISAR 2026-08-11", scene)}
t._nisar_amp_done(7, "amp0", "/tmp/amp.tif", "", dict(GEOM_ASC))
got = t.ready[0] if t.ready else {}
check("amplitude: the candidate handed to the fade carries the look",
      got.get("look_azimuth_deg") == 241.4 and got.get("incidence_deg") == 33.0)
check("  as a copy — the table's candidate is untouched",
      "look_azimuth_deg" not in scene and got is not scene)

print("\n=== merge terrain masks: the granule's geometry, not Sentinel-1's ===")


def direct(look, inc, orbit=None):
    sh, _ = ld.radar_shadow(DEM, GT, orbit, incidence_deg=inc,
                            lat_hint=GT[3] + GT[5] * N / 2.0, look_azimuth=look)
    lo, _ = ld.radar_layover(DEM, GT, orbit, incidence_deg=inc,
                             lat_hint=GT[3] + GT[5] * N / 2.0, look_azimuth=look)
    return sh | lo


t = make_tab()
masks = t._terrain_masks([result(NISAR, "ascending", **GEOM_ASC),
                          result(NISAR, "descending", **GEOM_DESC)], {})
want_a, want_d = direct(241.4, 33.0), direct(118.6, 40.0)
check("NISAR merge is masked, one mask per pass", masks is not None and len(masks) == 2)
check("  each exactly layover_dim's answer for the measured look + incidence",
      masks is not None and np.array_equal(masks[0], want_a)
      and np.array_equal(masks[1], want_d))
check(f"  ascending (lit from the ENE) loses the EAST face "
      f"({masks[0][EAST].mean():.0%}) and sees the west ({masks[0][WEST].mean():.0%})",
      masks[0][EAST].mean() > 0.9 and masks[0][WEST].mean() < 0.1)
s1_model = direct(None, 33.0, "ascending")
check(f"  where the Sentinel-1 ascending model would have masked the WEST face "
      f"({s1_model[WEST].mean():.0%}) — the bug the skip used to avoid",
      s1_model[WEST].mean() > 0.9 and s1_model[EAST].mean() < 0.1)
check("  the log names the source and the look",
      any("radar grid" in m and "241°" in m for m in t.log))

t = make_tab()
masks = t._terrain_masks([result(NISAR, "ascending", **GEOM_ASC),
                          result(NISAR, "descending", **NO_GEOM)], {})
check("a NISAR pass without geometry -> the merge runs unmasked (None)",
      masks is None)
check("  and says why", any("no radarGrid cube" in m and "unmasked" in m
                            for m in t.log))

t = make_tab()
masks = t._terrain_masks([result(S1, "ascending"), result(S1, "descending")], {})
lat_c = GT[3] + GT[5] * N / 2.0
want_s1 = [ld.radar_shadow(DEM, GT, st, incidence_deg=ld.IW_INCIDENCE_DEG,
                           lat_hint=lat_c)[0]
           | ld.radar_layover(DEM, GT, st, incidence_deg=ld.IW_INCIDENCE_DEG,
                              lat_hint=lat_c)[0]
           for st in ("ascending", "descending")]
check("Sentinel-1 merges are exactly the calls they always were",
      masks is not None and all(np.array_equal(m, w) for m, w in zip(masks, want_s1)))

# one cache serves every merge in a Merge click: a NISAR and a Sentinel-1 pass
# that share direction and incidence must not share a mask
cache = {}
t = make_tab()
s1m = t._terrain_masks([result(S1, "ascending")], cache)[0]
nim = t._terrain_masks([result(NISAR, "ascending", look_azimuth_deg=241.4,
                               incidence_deg=ld.IW_INCIDENCE_DEG,
                               geometry_note="x")], cache)[0]
check("the mask cache keys on the look too (same direction + incidence, "
      "opposite looks -> different masks)", not np.array_equal(s1m, nim))

print("\n=== the layover fade on a NISAR change map ===")
OUT = np.full((N, N), 5.0, np.float32)       # +5 dB everywhere: all 'high'


def alpha_of(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(4).ReadAsArray().astype(float) if ds.RasterCount == 4 else None
    n = ds.RasterCount
    ds = None
    return a, n


t = make_tab()
p = os.path.join(TMP, "nisar_change.tif")
faded = t._write_change_with_fade(p, OUT, GT, WKT, "ascending", 3.0, "logratio",
                                  radar=NISAR, geom=dict(nisar_scene(), **GEOM_ASC))
a, _n = alpha_of(p)
check("NISAR with its geometry is faded (RGBA written)", faded and a is not None)
check(f"  the EAST face is dimmed (alpha {a[EAST].mean():.0f}) and the west "
      f"kept ({a[WEST].mean():.0f})" if a is not None else "  (no alpha band)",
      a is not None and a[EAST].mean() < 0.5 * a[WEST].mean())
check("  and the log says which slopes", any("aspect~61°" in m for m in t.log))

t = make_tab()
p = os.path.join(TMP, "s1_change.tif")
faded = t._write_change_with_fade(p, OUT, GT, WKT, "ascending", 3.0, "logratio")
a, _n = alpha_of(p)
check("Sentinel-1 ascending (lit from the WSW) dims the WEST face instead",
      faded and a is not None and a[WEST].mean() < 0.5 * a[EAST].mean())

t = make_tab()
p = os.path.join(TMP, "nisar_nogeom.tif")
faded = t._write_change_with_fade(p, OUT, GT, WKT, "ascending", 3.0, "logratio",
                                  radar=NISAR, geom=dict(nisar_scene(), **NO_GEOM))
_a, n = alpha_of(p)
check("NISAR without geometry: not faded, plain one-band raster",
      not faded and n == 1)
check("  with the reason in the log",
      any("not applied to this NISAR scene" in m and "no radarGrid" in m
          for m in t.log))

t = make_tab(fade=False)
p = os.path.join(TMP, "fade_off.tif")
faded = t._write_change_with_fade(p, OUT, GT, WKT, "ascending", 3.0, "logratio",
                                  radar=NISAR, geom=dict(nisar_scene(), **NO_GEOM))
check("fade off: no fade and no NISAR note either", not faded and not t.log)

print("\n=== the layover fade on a NISAR amplitude preview ===")
GRAY = np.where(EAST | WEST, 220, 90).astype(np.uint8)   # bright slopes


def gray_tif(name):
    path = os.path.join(TMP, name)
    nr.write_display(path, GRAY, GT, WKT)
    return path


t = make_tab()
p = gray_tif("amp_geom.tif")
t._apply_layover_amplitude(p, dict(nisar_scene(), **GEOM_ASC))
a, n = alpha_of(p)
check("NISAR amplitude with its geometry is rewritten as RGBA", n == 4)
check("  dimming the EAST face, not the west",
      a is not None and a[EAST].mean() < 0.5 * a[WEST].mean())

t = make_tab()
p = gray_tif("amp_nogeom.tif")
t._apply_layover_amplitude(p, dict(nisar_scene(), **NO_GEOM))
_a, n = alpha_of(p)
check("NISAR amplitude without geometry is left alone, reason logged",
      n == 1 and any("not applied to this NISAR scene" in m for m in t.log))
t = make_tab()
p = gray_tif("amp_unread.tif")
t._apply_layover_amplitude(p, nisar_scene())
_a, n = alpha_of(p)
check("  likewise a scene never read at all (no geometry keys)",
      n == 1 and any("not read from the granule" in m for m in t.log))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
