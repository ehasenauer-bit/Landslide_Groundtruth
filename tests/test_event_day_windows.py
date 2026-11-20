"""Every tab's search covers the hours around the event, on exactly one side.

Run with tests/run_all.sh, or directly:
    ./venv/bin/python tests/test_event_day_windows.py

imagery.windows used to end the pre window a day before the event and start the post
window a day after it, and every STAC search sent its window as whole dates, so the
whole span from the day before the event to the day after it was searched by neither
side: the scenes nearest the slide never reached the Sentinel-2/Landsat, Sentinel-1,
DEM or PlanetScope lists, nor a Sentinel-2 Run (St. Elias 2026-08-18 had a
PlanetScope scene 11 h after it). Since 2026-11-19 the sides meet at the event time
and the searches send timestamps (imagery.stac_range). Pinned here, offline: the
STAC search is faked and honours the interval it is sent, so a day-rounded query
or a one-day gap shows up as a missing or duplicated scene.

Needs the imagery stack, which lives in the project venv, not in QGIS's Python, so
re-exec there if we were started under the wrong one.
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
        print("SKIP: no venv with stackstac; event-day windows not checked")
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
import sar_imagery as si
import dem_imagery as di
import scene_clarity as sc

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


EVENT = dt.datetime(2026, 8, 18, 10, 22)
LAT, LON, R = 60.26, -140.817, 5.0
H = dt.timedelta(hours=1)
D = dt.timedelta(days=1)


def scene(iid, when):
    return SimpleNamespace(id=iid, datetime=when, geometry=None, bbox=None, assets={},
                           properties={"eo:cloud_cover": 10.0,
                                       "sar:instrument_mode": "IW"})


SCENES = [scene("pre_5h", EVENT - 5 * H),           # same day, before the slide
          scene("pre_day_before", EVENT - D - 2 * H),
          scene("post_11h", EVENT + 11 * H),        # same day, after (St. Elias)
          scene("post_day_after", EVENT + D + 3 * H),
          scene("pre_far", EVENT - 30 * D + 13 * H),  # inside the first pre day
          scene("post_far", EVENT + 30 * D + 13 * H)]  # inside the last post day
sent = []


def parse(s):
    s = s.replace("Z", "")
    return dt.datetime.fromisoformat(s if "T" in s else s + "T00:00:00")


def fake_search_items(cat, collections=None, bbox=None, datetime=None, query=None, **_):
    """A STAC search over SCENES that honours the interval. A bare date as the END
    covers that whole day, as pystac-client expands it."""
    sent.append(datetime)
    a, b = datetime.split("/")
    lo, hi = parse(a), parse(b)
    if "T" not in b:
        hi += D - dt.timedelta(microseconds=1)
    return [s for s in SCENES if lo <= s.datetime <= hi]


im._search_items = fake_search_items
im._client = lambda: None
di._client = lambda: None
si._one_frame_per_pass = lambda items, lat, lon: items     # no footprints here

print("=== imagery.windows: the sides meet at the event time ===")
pre0, pre1, post0, post1 = im.windows(EVENT, 30, 30)
check(f"pre ends 1 s before the event ({pre1})", pre1 == EVENT - dt.timedelta(seconds=1))
check(f"post starts at the event ({post0})", post0 == EVENT)
check(f"outer ends are whole days ({pre0} .. {post1})",
      pre0 == dt.datetime(2026, 7, 19) and post1 == dt.datetime(2026, 9, 17, 23, 59, 59))
check("stac_range sends seconds, UTC",
      im.stac_range(pre1, post0) == "2026-08-18T10:21:59Z/2026-08-18T10:22:00Z")


def sides(search):
    """{'pre': ids, 'post': ids} from a source's own search over the two windows."""
    return {"pre": [s.id for s in search(pre0, pre1)],
            "post": [s.id for s in search(post0, post1)]}


SOURCES = {
    "Sentinel-2/Landsat": lambda a, b: im.search_scenes(
        LAT, LON, R, a, b, "sentinel-2-l2a", EVENT, max_cloud=None, limit=None),
    "Sentinel-1": lambda a, b: si.search_scenes(LAT, LON, R, a, b, EVENT, limit=99),
    "DEM strips": lambda a, b: di.search_scenes(LAT, LON, R, a, b, EVENT, limit=99),
    "scene_clarity": lambda a, b: sc._search_window(LAT, LON, R, a, b,
                                                    "sentinel-2-l2a", 100.0),
}
for name, search in SOURCES.items():
    print(f"\n=== {name} ===")
    got = sides(search)
    check(f"the scene 5 h before is pre ({got['pre']})", "pre_5h" in got["pre"])
    check(f"the scene 11 h after is post ({got['post']})", "post_11h" in got["post"])
    check("the days either side are listed too",
          "pre_day_before" in got["pre"] and "post_day_after" in got["post"])
    check("the far ends of both windows are still listed",
          "pre_far" in got["pre"] and "post_far" in got["post"])
    check("no scene is on both sides", not set(got["pre"]) & set(got["post"]))
    check(f"the query was timestamps, not whole dates ({sent[-1]})",
          "T" in sent[-1].split("/")[0] and "T" in sent[-1].split("/")[1])

print("\n=== DEM: the 'nearest strip outside the window' search ===")
sent.clear()
for side in ("pre", "post"):
    try:
        di._nearest_outside_note(LAT, LON, R, EVENT, side)
    except Exception:
        pass                              # only the interval it sent matters here
check(f"pre looks back to 1 s before the event ({sent[0]})",
      sent[0].endswith("/2026-08-18T10:21:59Z"))
check(f"post looks forward from the event ({sent[1]})",
      sent[1].startswith("2026-08-18T10:22:00Z/"))

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
