"""The Fusion candidate shortlist: blob labelling, and ranking by peak not area.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_candidates.py

Why it matters: the fused score puts the real scar in the top 2% of PIXELS but
almost never makes it the brightest pixel, so the heatmap buries it. As BLOBS
ranked by peak score the scar came 1st on four of six truthed events and never
below 15th of ~120-190. The shortlist is what turns that into something you can
see, so the ranking rule and the filters are pinned down here.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
PLUG = os.path.join(PKG, "landslide_groundtruth")
sys.path.insert(0, PKG)

import numpy as np
from landslide_groundtruth import sar_change as sc
from landslide_groundtruth import fusion_core as fc


def blobs(mask, conn=8):
    return sc.label_blobs(np.asarray(mask, bool), conn)


print("=== 1. label_blobs finds the components ===")
m = np.zeros((9, 9), bool)
m[1:3, 1:3] = True                      # 4 px
m[5:8, 5:8] = True                      # 9 px
ys, xs, roots = blobs(m)
sizes = sorted(c for c in np.bincount(roots).tolist() if c)
print("   two squares ->", len(set(roots.tolist())), "components, sizes", sizes)
assert len(set(roots.tolist())) == 2
assert sizes == [4, 9]
assert ys.size == int(m.sum()), "every mask pixel must be labelled"
assert blobs(np.zeros((5, 5), bool))[0].size == 0, "an empty mask yields nothing"

d = np.zeros((5, 5), bool)
d[1, 1] = d[2, 2] = True                # touching only at a corner
assert len(set(blobs(d, 8)[2].tolist())) == 1, "conn 8 must join diagonals"
assert len(set(blobs(d, 4)[2].tolist())) == 2, "conn 4 must NOT join diagonals"
print("   diagonals: joined at connectivity 8, separate at 4")

print("=== 2. ranked by PEAK score, not area ===")
# the negative case that matters: a big weak blob must not outrank a small
# strong one. The biggest blob is usually terrain or illumination, not a slide.
score = np.zeros((20, 20), dtype="float32")
score[2:4, 2:4] = 0.9                   # 4 px, strong
score[10:18, 10:18] = 0.5               # 64 px, weak
ys, xs, roots = blobs(score > 0)
cands = fc.candidates(score, ys, xs, roots, 20.0, 20.0, min_px=1, limit=20)
print(f"   first = {cands[0]['n_px']} px @ {cands[0]['peak']:.2f},  "
      f"second = {cands[1]['n_px']} px @ {cands[1]['peak']:.2f}")
assert len(cands) == 2
assert cands[0]["n_px"] == 4 and abs(cands[0]["peak"] - 0.9) < 1e-6, \
    "the small strong blob must lead — ranking on area would invert this"
assert cands[1]["n_px"] == 64
assert abs(cands[0]["area_km2"] - 0.0016) < 1e-9, "4 px x 20 m x 20 m = 0.0016 km2"
assert 2 <= cands[0]["peak_row"] <= 3 and 2 <= cands[0]["peak_col"] <= 3
assert (cands[1]["row0"], cands[1]["row1"],
        cands[1]["col0"], cands[1]["col1"]) == (10, 17, 10, 17), \
    "the bounding box is what the zoom frames"

print("=== 3. the filters that keep the list readable ===")
assert len(fc.candidates(score, ys, xs, roots, 20.0, 20.0, min_px=10)) == 1
assert len(fc.candidates(score, ys, xs, roots, 20.0, 20.0, min_px=1, limit=1)) == 1
assert len(fc.candidates(score, ys, xs, roots, 20.0, 20.0, min_px=1, limit=None)) == 2
assert fc.candidates(score, *blobs(np.zeros_like(score, bool)), 20.0, 20.0) == []
many = np.zeros((60, 60), dtype="float32")
for i in range(6):
    for j in range(6):
        many[i * 10:i * 10 + 3, j * 10:j * 10 + 3] = 0.3 + 0.01 * (i * 6 + j)
c2 = fc.candidates(many, *blobs(many > 0), 20.0, 20.0, min_px=1, limit=20)
print(f"   36 blobs -> kept {len(c2)}, peaks {c2[0]['peak']:.2f} down to {c2[-1]['peak']:.2f}")
assert len(c2) == 20
assert all(c2[i]["peak"] >= c2[i + 1]["peak"] for i in range(len(c2) - 1))
assert abs(c2[0]["peak"] - many.max()) < 1e-6

print("=== 4. NaN is 'not measured', never a candidate ===")
n = np.full((10, 10), np.nan, dtype="float32")
n[2:5, 2:5] = 0.8
c3 = fc.candidates(n, *blobs(np.isfinite(n) & (n >= 0.2)), 20.0, 20.0, min_px=1)
assert len(c3) == 1 and c3[0]["n_px"] == 9, "NaN pixels must not join a blob"
assert abs(c3[0]["peak"] - 0.8) < 1e-6
print("   NaN surroundings excluded; blob is the 9 real pixels")

print("=== 5. the sieve still behaves after label_blobs was lifted out of it ===")
rng = np.random.default_rng(7)
for _ in range(20):
    h, w = rng.integers(20, 60, 2)
    vals = rng.normal(0, 2, (h, w)).astype("float32")
    mask = rng.random((h, w)) < 0.3
    ma, conn = int(rng.integers(2, 25)), int(rng.choice([4, 8]))
    out = sc.sieve_small_blobs(vals, mask, ma, fill=0.5, connectivity=conn)
    yy, xx, rr = sc.label_blobs(mask & np.isfinite(vals), conn)
    counts = np.bincount(rr, minlength=yy.size)
    small = counts[rr] < ma
    assert np.all(out[yy[small], xx[small]] == 0.5), "small blobs must be filled"
    big = ~small
    assert np.array_equal(out[yy[big], xx[big]], vals[yy[big], xx[big]]), \
        "pixels in surviving blobs must be untouched"
print("   20 random cases: sieve agrees with the labels it now shares")

print("\nOK  tests/test_candidates.py")
