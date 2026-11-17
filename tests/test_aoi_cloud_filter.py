"""The search's cloud filter judges a scene by its cloud over the AOI, not the tile.

On 2026-11-16 Ethan saw a clear Sentinel-2 image of Iliamna (2026-11-15) in
Copernicus Browser that the plugin never listed. Planetary Computer had it, but
its tile eo:cloud_cover was 96.6 % with ~0 % snow: at low winter sun Sen2Cor
files clear snow as cloud, and the 50 % "Hide scenes cloudier than" cap was a STAC
predicate on that number, so the scene was dropped before OmniCloudMask (24-37 %
over the AOI) ever saw it. Every tile there from 10-28 to 11-15 read 88-99 %.

Pinned here offline (the STAC search and the cloud read are stubbed):
  1. a scene the tile calls overcast but the AOI calls clear is listed;
  2. one the AOI calls cloudy is hidden, and counted in the notes;
  3. the search reads further back, nearest-first, until the side's list is full,
     and stops after AOI_FILL_ROUNDS batches, saying how many went unchecked;
  4. with the filter off, the nearest scenes are listed in one batch, clouds and all;
  5. a scene with no AOI measurement falls back to the tile number; one with
     neither is kept.

Needs the imagery stack (stackstac), which lives in the project venv, not in
QGIS's Python — so re-exec there if we were started under the wrong one.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import stackstac  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with stackstac; AOI cloud filter check not run")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

import datetime as dt
import warnings
from types import SimpleNamespace

warnings.simplefilter("ignore")
sys.path.insert(0, ROOT)
import imagery as im

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


EVENT = dt.datetime(2026, 11, 13, 14, 33)
LAT, LON, R = 60.030282, -153.094067, 15.0


def scene(iid, when, tile_cloud):
    props = {} if tile_cloud is None else {"eo:cloud_cover": tile_cloud}
    return SimpleNamespace(id=iid, datetime=when, properties=props, assets={},
                           geometry=None, bbox=None)


# synthetic scenes: id -> (tile eo:cloud_cover, AOI cloud or None)
POST = [scene("post_1115", dt.datetime(2026, 11, 15, 21, 47), 96.6)]
PRE = [scene(f"pre_{d:02d}", EVENT - dt.timedelta(days=d), 90.0) for d in range(1, 61)]
AOI = {"post_1115": 24.4}
for i, s in enumerate(PRE):
    AOI[s.id] = 100.0 if i < 40 else (10.0 if i % 2 else 95.0)   # 40 cloudy, then mixed

reads = []                                       # one entry per _aoi_cloud_fractions call


def fake_fractions(items, lat, lon, radius_km, sensor, method="ocm"):
    reads.append([i.id for i in items])
    return {i.id: (AOI.get(i.id), 0.0, "ocm", None) for i in items}, None


def fake_search(lat, lon, r, start, end, coll, event_time, max_cloud=60, limit=6,
                cloud_weight=0.5):
    check(max_cloud is None, f"STAC search carries no tile-cloud predicate ({max_cloud})")
    pool = POST if start > EVENT else PRE
    return pool[:limit]


im._aoi_cloud_fractions = fake_fractions
im.search_scenes = fake_search

print("=== 1-3. filter on at 50 %, a cloudy month before the event ===")
res = im.search_event(LAT, LON, R, EVENT, pre_days=60, post_days=90, sensor="s2",
                      max_cloud_pct=50)
post_ids = [c["id"] for c in res["post"]]
check(post_ids == ["post_1115"],
      f"the 11-15 scene (tile 96.6 %, AOI 24.4 %) is listed: {post_ids}")
pre_ids = [c["id"] for c in res["pre"]]
lim = im._preview_limit_for(60)
check(all(AOI[i] <= 50 for i in pre_ids), "every listed pre scene is clear over the AOI")
check(pre_ids == sorted(pre_ids, key=lambda i: int(i.split("_")[1])),
      "pre scenes stay nearest-first")
# 12 per batch: 1-36 all cloudy, then 37-48 gives 4 clear (41, 43, 45, 47)
check(pre_ids == ["pre_42", "pre_44", "pre_46", "pre_48"],
      f"the pre list holds what the rounds found: {pre_ids}")
check(len(reads) <= im.AOI_FILL_ROUNDS,
      f"{len(reads)} read batches, at most AOI_FILL_ROUNDS={im.AOI_FILL_ROUNDS}")
read_ids = [i for b in reads for i in b]
check(len(read_ids) == len(set(read_ids)), "no scene is read twice")
check(reads[0] == [s.id for s in PRE[:lim]] + ["post_1115"],
      "the first batch is the nearest scenes, both sides together")
notes = " | ".join(res["notes"])
check("hidden, cloudier than 50% over the AOI" in notes, f"hidden count noted: {notes[:90]}")
hidden_pre = sum(1 for i in read_ids if i.startswith("pre_") and AOI[i] > 50)
check(f"{hidden_pre} pre / 0 post" in notes, f"... with the right count ({hidden_pre} pre)")
unchecked = len(PRE) - sum(1 for i in read_ids if i.startswith("pre_"))
check(unchecked == 12 and f"{unchecked} older scene(s)" in notes,
      f"a short list says how many were left unchecked ({unchecked})")
check(res["pre"][0]["aoi_cloud_pct"] is not None, "listed rows carry their AOI cloud")

print("\n=== 4. filter off ===")
reads.clear()
res = im.search_event(LAT, LON, R, EVENT, pre_days=60, post_days=90, sensor="s2",
                      max_cloud_pct=100)
check(len(reads) == 1, f"one read batch ({len(reads)})")
check([c["id"] for c in res["pre"]] == [s.id for s in PRE[:lim]],
      "the nearest scenes, clouds and all")
check(not res["notes"], f"nothing hidden, nothing noted ({res['notes']})")

print("\n=== 5. no AOI measurement ===")
NOAOI = [scene("tile_clear", EVENT - dt.timedelta(days=1), 10.0),
         scene("tile_cloudy", EVENT - dt.timedelta(days=2), 90.0),
         scene("no_metadata", EVENT - dt.timedelta(days=3), None)]
kept, frac, _n, hidden, _u = im._keep_by_aoi_cloud(
    {"pre": (NOAOI, 12)}, 50, LAT, LON, R, "s2")
ids = [i.id for i in kept["pre"]]
check(ids == ["tile_clear", "no_metadata"],
      f"tile number used where the AOI wasn't measured; unjudgeable kept: {ids}")
check(hidden["pre"] == 1, "the tile-overcast one is hidden")

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)
