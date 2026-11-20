"""The PlanetScope search must list every scene in the window, and a paid order must
load whatever Planet delivered.

Run with tests/run_all.sh, or directly:
    ./venv/bin/python tests/test_planet_nothing_hidden.py

An audit on 2026-11-19 found five places that kept imagery from the tab:
  - permission_filter dropped every scene the account can't download yet (here: the
    whole month after acquisition), so a fresh event had no post scenes at all, not
    even a browse image or a tile preview. Now listed, flagged downloadable=False.
  - the windows stopped a day short of the event on both sides, so a scene within
    24 h of the slide was never searched. Now they run to the event time.
  - SEARCH_LIMIT kept 60 scenes per side with no word about the rest. Now 35 per
    side (Ethan's pick, 2026-11-19), with a note saying how many were left out.
  - the cloud predicate stayed on at 100%, and a range query also drops an item that
    has no cloud_cover. Now off at 100%.
  - an order that finished 'partial' raised like 'failed', throwing away the scenes
    Planet did deliver. Now they load.
Offline: the Planet client is faked; no key, no network, no quota.

Needs the Planet SDK, which lives in the project venv, so re-exec there if we were
started under QGIS's Python.
"""
import datetime as dt
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import planet  # noqa: F401
    import rioxarray  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with the Planet SDK; Planet search not checked")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

os.environ["LANDSLIDE_PLANET_CACHE"] = tempfile.mkdtemp(prefix="planet_hidden_")
sys.path.insert(0, ROOT)
import planet_imagery as pi
import planet_cache as pc

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def walk(flt):
    """Every leaf filter dict in a (nested) Planet search filter."""
    if flt.get("type") in ("AndFilter", "OrFilter", "NotFilter"):
        cfg = flt.get("config")
        for sub in (cfg if isinstance(cfg, list) else [cfg]):
            yield from walk(sub)
    else:
        yield flt


EVENT = dt.datetime(2026, 8, 18, 10, 22)
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def item(i, when, perms=True, cloud=0.3):
    it = {"id": f"2026{i:04d}_000000_00_0000",
          "properties": {"acquired": when.isoformat() + "Z"},
          "geometry": {"type": "Polygon", "coordinates": [[[-141, 60], [-140, 60],
                                                           [-140, 61], [-141, 60]]]},
          "_links": {"thumbnail": f"https://tiles.planet.com/thumb/{i}"}}
    if cloud is not None:
        it["properties"]["cloud_cover"] = cloud
    if perms is not None:
        it["_permissions"] = (["assets.ortho_analytic_4b_sr:download"] if perms else [])
    return it


class FakeData:
    """pl.data.search: records each filter and returns the items whose acquisition
    falls inside its date range (so the windows are tested, not assumed)."""
    def __init__(self, items):
        self.items, self.calls = items, []

    def search(self, item_types, search_filter=None, limit=None):
        self.calls.append((search_filter, limit))
        rng = next(f for f in walk(search_filter) if f.get("type") == "DateRangeFilter")
        cfg = rng["config"]
        lo = dt.datetime.fromisoformat(cfg["gte"].replace("Z", "+00:00")).replace(tzinfo=None)
        hi = dt.datetime.fromisoformat(cfg["lte"].replace("Z", "+00:00")).replace(tzinfo=None)
        return iter([i for i in self.items if lo <= pi._acquired(i) <= hi])


class FakePl:
    def __init__(self, items):
        self.data = FakeData(items)


print("=== search_scenes: no permission filter, cloud off at 100%, all on request ===")
many = [item(i, EVENT + dt.timedelta(hours=6 * i), perms=(i % 2 == 0)) for i in range(1, 101)]
pl = FakePl(many)
got = pi.search_scenes(pl, pi._bbox_geojson(60.26, -140.8, 5), EVENT,
                       EVENT + dt.timedelta(days=30), EVENT, max_cloud=1.0,
                       limit=None, allow_test_quality=True)
types = [f.get("type") for f in walk(pl.data.calls[-1][0])]
check(f"no permission filter in the query ({types})", "PermissionFilter" not in types)
check("no cloud_cover predicate at 100%",
      not any(f.get("field_name") == "cloud_cover" for f in walk(pl.data.calls[-1][0])))
check("every page requested (limit=0)", pl.data.calls[-1][1] == 0)
check(f"limit=None returns all 100 in-window scenes ({len(got)})", len(got) == 100)
pi.search_scenes(pl, pi._bbox_geojson(60.26, -140.8, 5), EVENT,
                 EVENT + dt.timedelta(days=30), EVENT, max_cloud=0.5, limit=None)
check("a cloud cap below 100% is still applied",
      any(f.get("field_name") == "cloud_cover" and f["config"].get("lte") == 0.5
          for f in walk(pl.data.calls[-1][0])))
check("'Standard quality only' still filters when asked",
      any(f.get("field_name") == "quality_category"
          for f in walk(pl.data.calls[-1][0])))

print("\n=== search_event: windows run to the event time; locked scenes listed ===")
near = [item(1, EVENT - dt.timedelta(hours=5)),            # 5 h before: hidden before
        item(2, EVENT + dt.timedelta(hours=11), perms=False),   # 11 h after (St. Elias)
        item(3, EVENT - dt.timedelta(days=3)),
        item(4, EVENT + dt.timedelta(days=4), perms=None, cloud=None),   # no metadata
        item(5, NOW - dt.timedelta(days=2), perms=False)]  # fresh: not downloadable yet
pl = FakePl(near)
pi._client = lambda: pl
res = pi.search_event(60.26, -140.817, 5.0, EVENT, pre_days=30, post_days=150,
                      max_cloud_pct=100, allow_test_quality=True)
pre_ids = [c["id"] for c in res["pre"]]
post_ids = [c["id"] for c in res["post"]]
check(f"the scene 5 h BEFORE the event is listed as pre ({pre_ids})",
      near[0]["id"] in pre_ids)
check(f"the scene 11 h AFTER the event is listed as post ({post_ids})",
      near[1]["id"] in post_ids)
check("no scene is listed on both sides", not set(pre_ids) & set(post_ids))
check("a scene with no cloud_cover and no _permissions is still listed",
      near[3]["id"] in post_ids)
by_id = {c["id"]: c for c in res["pre"] + res["post"]}
check("downloadable True / False / None follow _permissions",
      by_id[near[0]["id"]]["downloadable"] is True
      and by_id[near[1]["id"]]["downloadable"] is False
      and by_id[near[3]["id"]]["downloadable"] is None)
check("a fresh locked scene says when it should open up",
      by_id[near[4]["id"]]["orderable_after"]
      == (pi._acquired(near[4]) + dt.timedelta(days=30)).date().isoformat())
check("an old locked scene doesn't promise a date already past",
      by_id[near[1]["id"]]["orderable_after"] is None)
check("the browse image link survives on a locked scene",
      by_id[near[1]["id"]]["thumb_url"] == near[1]["_links"]["thumbnail"])
check(f"a note counts the preview-only scenes ({res.get('notes')})",
      any("2 listed scene(s) can't be downloaded" in n for n in res.get("notes", [])))

print("\n=== the list stops at 35 per side, and says what it left out ===")
check("SEARCH_LIMIT is 35", pi.SEARCH_LIMIT == 35)
pl = FakePl([item(i, EVENT + dt.timedelta(hours=3 * i)) for i in range(1, 51)]
            + [item(100 + i, EVENT - dt.timedelta(days=i)) for i in range(1, 11)])
pi._client = lambda: pl
res = pi.search_event(60.26, -140.817, 5.0, EVENT, pre_days=30, post_days=30,
                      max_cloud_pct=100, allow_test_quality=True)
check(f"50 post matches -> 35 listed ({len(res['post'])})", len(res["post"]) == 35)
check("... the 35 nearest the event (same cloud, so gap decides)",
      max(c["gap_days"] for c in res["post"]) <= round(35 * 3 / 24))
check(f"10 pre matches -> all 10 listed ({len(res['pre'])})", len(res["pre"]) == 10)
cap_notes = [n for n in res["notes"] if "best-ranked" in n]
check(f"a note says 35 of 50 after, and nothing about before ({cap_notes})",
      len(cap_notes) == 1 and "of 50 after" in cap_notes[0]
      and "before" not in cap_notes[0])
res = pi.search_event(60.26, -140.817, 5.0, EVENT, pre_days=30, post_days=30,
                      max_cloud_pct=100, allow_test_quality=True, auto_window=True)
check("auto-window still lists one per side, with no cap note",
      len(res["post"]) == 1 and len(res["pre"]) == 1
      and not any("best-ranked" in n for n in res["notes"]))

print("\n=== a 'partial' order loads what Planet delivered ===")


class FakeOrders:
    def __init__(self, state, files):
        self.state, self.files = state, files

    def wait(self, order_id, delay=None, max_attempts=None, callback=None):
        callback and callback(self.state)
        return self.state

    def download_order(self, order_id, directory=None, overwrite=False):
        for name in self.files:
            p = os.path.join(directory, "PSScene", name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()


class FakeOrderPl:
    def __init__(self, state, files):
        self.orders = FakeOrders(state, files)


delivered = ["20260820_211000_00_2500_3B_AnalyticMS_SR_clip.tif",
             "20260820_211000_00_2500_3B_udm2_clip.tif"]
logs = []
pairs = pi._wait_download(FakeOrderPl("partial", delivered), "partial-order-1",
                          dict(side="post", event_id="ev", lat=60.26, lon=-140.8,
                               radius_km=5.0), log=logs.append)
check(f"partial: the delivered scene is returned ({len(pairs)} pair)", len(pairs) == 1)
check("partial: it is recorded in the ledger (recallable, not lost)",
      any(r["order_id"] == "partial-order-1" for r in pc.load()))
check("partial: the log says some scenes failed", any("'partial'" in l for l in logs))
for state, files in (("failed", []), ("partial", [])):
    try:
        pi._wait_download(FakeOrderPl(state, files), f"{state}-order", {}, log=logs.append)
        check(f"{state} with nothing delivered raises", False)
    except RuntimeError:
        check(f"{state} with nothing delivered raises", True)

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
