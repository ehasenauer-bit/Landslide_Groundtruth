#!/usr/bin/env python3
"""Gather pre/post imagery for ONE manually-specified location/time.

This is the interactive / QGIS-plugin entry point: instead of reading the
inventory spreadsheet, it takes a location + time + windows straight from the
command line and runs the exact same imagery-gathering pipeline as the batch tool
by reusing `run_groundtruth.process_one` (one implementation, two front-ends).

It writes the usual QGIS review package under <out>/qgis_packages/<event_id>_*
and a machine-readable <out>/result.json that a caller (the plugin) can parse to
learn the outcome and the exact layer file paths to load.

Usage:
  python run_single.py --lat 60.465 --lon -142.10 \\
      --datetime "2023-08-07 11:19" --radius-km 3 \\
      --pre-days 60 --post-days 90 --prefer planet --out out/interactive
  # optional: --event-id Bagley_test --seasonal --auto-window
  # preview only (no orders/downloads): add --search-only
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
import traceback
from argparse import Namespace

from run_groundtruth import process_one


def _search_one_source(s, lat, lon, radius_km, when: dt.datetime, args):
    """Search ONE source ('planet'/'s2'/'landsat') for candidate pre/post scenes.

    Returns (result_dict, note): result_dict is search_event's output (or None on
    failure) and note is a per-source message (or None). Self-contained and
    touches no shared state, so the preview can fan several of these out across
    threads."""
    try:
        if s == "planet":
            import planet_imagery as pi
            r = pi.search_event(lat, lon, radius_km, when, pre_days=args.pre_days,
                                post_days=args.post_days, seasonal=args.seasonal,
                                auto_window=args.auto_window,
                                cloud_weight=args.cloud_weight,
                                max_cloud_pct=args.max_cloud,
                                require_point=args.coverage == "point",
                                allow_test_quality=args.quality == "any")
        else:
            import imagery as im
            r = im.search_event(lat, lon, radius_km, when, pre_days=args.pre_days,
                                post_days=args.post_days, seasonal=args.seasonal,
                                auto_window=args.auto_window, sensor=s,
                                cloud_weight=args.cloud_weight,
                                max_cloud_pct=args.max_cloud)
    except Exception as e:
        return None, f"{s}: {type(e).__name__}: {e}"
    note = None
    if not r["pre"] or not r["post"]:
        note = (f"{r['source']}: incomplete coverage "
                f"({len(r['pre'])} pre, {len(r['post'])} post)")
    return r, note


def _search_candidates(lat, lon, radius_km, when: dt.datetime, args) -> dict:
    """Free dry-run: search each source `prefer` could use and collect candidate
    pre/post scenes WITHOUT ordering or downloading anything.

    Mirrors fetch_event's source priority: 'auto' previews PlanetScope +
    Sentinel-2 + Landsat so you can compare coverage and pick a source; a
    specific --prefer previews just that source. Pre-2016 events drop Sentinel-2
    (no coverage) in favor of Landsat. Per-source failures (e.g. Planet not
    authenticated) become notes instead of aborting the whole preview."""
    from concurrent.futures import ThreadPoolExecutor

    sensors = {"auto": ["planet", "s2", "landsat"], "planet": ["planet"],
               "s2": ["s2"], "landsat": ["landsat"]}[args.prefer]
    if "s2" in sensors and when < dt.datetime(2016, 1, 1):
        sensors = [s for s in sensors if s != "s2"]
        if "landsat" not in sensors:
            sensors.append("landsat")

    # The per-source searches are independent network-bound calls, so fan them out
    # across threads: the preview then returns in ~the slowest single source
    # instead of their sum. ThreadPoolExecutor.map keeps input order, so results
    # merge back in the fixed `sensors` order — candidate lists and notes stay
    # deterministic regardless of which search finishes first.
    with ThreadPoolExecutor(max_workers=len(sensors)) as ex:
        results = list(ex.map(
            lambda s: _search_one_source(s, lat, lon, radius_km, when, args),
            sensors))

    pre, post, notes = [], [], []
    for r, note in results:
        if r is not None:
            pre += r["pre"]
            post += r["post"]
        if note is not None:
            notes.append(note)
    pre.sort(key=lambda c: c.get("gap_days") if c.get("gap_days") is not None else 1e9)
    post.sort(key=lambda c: c.get("gap_days") if c.get("gap_days") is not None else 1e9)
    return dict(pre=pre, post=post, notes=notes)


def _print_search_table(res: dict):
    """Readable stdout dump of the dry-run candidates (the plugin reads the JSON)."""
    hdr = f"{'side':4} {'date':16} {'gap':>4} {'cloud%':>6}  {'source':11} scene"
    print("\n" + hdr)
    print("-" * len(hdr))
    for side, rows in (("pre", res["pre"]), ("post", res["post"])):
        for c in rows:
            date = (c.get("date") or "")[:16].replace("T", " ")
            gap = "" if c.get("gap_days") is None else c["gap_days"]
            cloud = "" if c.get("cloud_pct") is None else f"{c['cloud_pct']:.0f}"
            print(f"{side:4} {date:16} {gap:>4} {cloud:>6}  "
                  f"{c.get('source',''):11} {c.get('id','')}")
    for n in res.get("notes", []):
        print(f"note: {n}")


def _parse_dt(s: str) -> dt.datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognized datetime: {s!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--datetime", dest="when", type=_parse_dt, required=True,
                    help="event time (UTC), e.g. '2023-08-07 11:19'")
    ap.add_argument("--radius-km", type=float, default=3.0,
                    help="imagery search/footprint radius around the point")
    ap.add_argument("--out", default="out/interactive")
    ap.add_argument("--event-id", default=None,
                    help="label for the output files (default: derived from time)")
    ap.add_argument("--prefer", default="auto",
                    choices=["auto", "planet", "s2", "landsat"],
                    help="imagery source priority; 'auto' = PlanetScope -> Sentinel-2 -> Landsat")
    ap.add_argument("--pre-days", type=int, default=60)
    ap.add_argument("--post-days", type=int, default=90)
    ap.add_argument("--cloud-weight", type=float, default=0.5,
                    help="scene ranking: gap-days willing to travel from the event date "
                         "to avoid 1%% cloud (gap_days + cloud_weight*cloud_pct). Lower = "
                         "stay closer to the event date; higher = prefer clearer scenes. "
                         "Ignored with --auto-window. (default 0.5)")
    ap.add_argument("--max-cloud", type=float, default=None,
                    help="max whole-scene cloud cover %% to consider (default 80, or 20 "
                         "with --auto-window). Scene-wide metric — per-pixel cloud masking "
                         "still applies, so a high cap surfaces scenes clear over the AOI but "
                         "cloudy elsewhere (what Planet Explorer shows).")
    ap.add_argument("--coverage", choices=["aoi", "point"], default="aoi",
                    help="PlanetScope coverage requirement: 'aoi' = any scene overlapping the "
                         "search box (matches Planet Explorer; default), 'point' = scene "
                         "footprint must contain the exact epicentre (stricter).")
    ap.add_argument("--quality", choices=["standard", "any"], default="standard",
                    help="PlanetScope quality: 'standard' = standard-quality scenes only "
                         "(default), 'any' = also include 'test'-quality scenes. Near a fresh "
                         "event the nearest/clearest scenes are often test-only (Planet Explorer "
                         "shows them); test = looser geo/radiometric calibration, fine for the "
                         "visual review.")
    ap.add_argument("--scenes", default=None,
                    help="comma-separated review layers to download: true_color, "
                         "highlight_natural, false_color, ndvi, dndvi, dbright "
                         "(default: all). The predicted-point layer is always written. "
                         "Ignored with --search-only.")
    ap.add_argument("--pre-scene-ids", default=None,
                    help="comma-separated scene IDs to composite for the PRE side, "
                         "overriding the automatic scene ranking (Sentinel-2 / Landsat "
                         "only). Requires --post-scene-ids; PlanetScope and the windowed "
                         "search are skipped. Ignored with --search-only.")
    ap.add_argument("--post-scene-ids", default=None,
                    help="comma-separated scene IDs for the POST side (see "
                         "--pre-scene-ids).")
    ap.add_argument("--seasonal", action="store_true",
                    help="winter event: use prior-year pre window")
    ap.add_argument("--auto-window", action="store_true",
                    help="tightest window: use only the clear scene nearest the event on "
                         "each side; --pre-days/--post-days act as the max search range")
    ap.add_argument("--search-only", action="store_true",
                    help="free dry-run: search candidate pre/post scenes per source and "
                         "write <out>/search.json; no Planet orders or downloads")
    a = ap.parse_args()

    event_id = a.event_id or f"event_{a.when:%y%m%d_%H%M}"
    os.makedirs(a.out, exist_ok=True)

    # the event row process_one() expects (mirrors an inventory row)
    ev = dict(event_id=event_id, datetime_utc=a.when, lat=a.lat, lon=a.lon,
              loc_source="manual", search_radius_km=a.radius_km)
    # hand-picked scene IDs (manual override) -> list[str] or None per side
    def _id_list(s):
        ids = [x.strip() for x in (s or "").split(",") if x.strip()]
        return ids or None

    # args namespace process_one() reads; radius_scale=1.0 so radius-km is literal
    args = Namespace(out=a.out, prefer=a.prefer,
                     pre_days=a.pre_days, post_days=a.post_days, seasonal=a.seasonal,
                     radius_scale=1.0, auto_window=a.auto_window,
                     cloud_weight=a.cloud_weight, max_cloud=a.max_cloud,
                     coverage=a.coverage, quality=a.quality, scenes=a.scenes,
                     pre_scene_ids=_id_list(a.pre_scene_ids),
                     post_scene_ids=_id_list(a.post_scene_ids))

    print(f">>> {event_id}  ({a.when}, {a.lat:.4f},{a.lon:.4f}, r={a.radius_km}km, "
          f"prefer={a.prefer})")

    if a.search_only:
        print("    [search-only] dry-run; no orders/downloads")
        try:
            res = _search_candidates(a.lat, a.lon, a.radius_km, a.when, args)
        except Exception as e:
            traceback.print_exc()
            res = dict(pre=[], post=[], notes=[f"search failed: {e}"])
        res.update(event_id=event_id, lat=a.lat, lon=a.lon,
                   datetime_utc=a.when.isoformat(),
                   params=dict(pre_days=a.pre_days, post_days=a.post_days,
                               seasonal=a.seasonal, auto_window=a.auto_window,
                               cloud_weight=a.cloud_weight, max_cloud=a.max_cloud,
                               coverage=a.coverage, quality=a.quality,
                               prefer=a.prefer, radius_km=a.radius_km))
        search_path = os.path.join(a.out, "search.json")
        with open(search_path, "w") as f:
            json.dump(res, f, indent=2, default=str)
        _print_search_table(res)
        print(f"Search -> {search_path}")
        return 0

    try:
        res = process_one(ev, args)
    except Exception as e:
        traceback.print_exc()
        res = dict(status="error", event_id=event_id, error=str(e))

    res.setdefault("layers", [])   # the layer files the run produced, for the plugin to load
    result_path = os.path.join(a.out, "result.json")
    with open(result_path, "w") as f:
        json.dump(res, f, indent=2, default=str)

    print(f"    {res}")
    print(f"Result -> {result_path}")
    return 0 if res.get("status") in ("ok", "no_imagery") else 1


if __name__ == "__main__":
    sys.exit(main())
