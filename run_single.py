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

Already looked at this event? Don't order it again — Planet quota is spent at order
creation, and every order is cached (see planet_cache.py):
  --planet-list-orders   what has already been paid for near this location
  --planet-recall        load those orders back onto the map (no order, no quota)
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


def _adopt_legacy_orders(out_dir):
    """Register Planet downloads made before the shared order cache existed.

    Older runs unpacked orders inside whichever project produced them —
    <out>/planet_cache/<event>/<side>/<order>/ (batch) and <out>/planet_render/...
    (plugin renders). Those orders are already paid for, so scan the obvious roots and
    record what's there IN PLACE: nothing is moved, copied or deleted, they simply
    become recallable. Idempotent and cheap (adopt() skips ids already in the ledger),
    so it's safe to run on every recall/list."""
    import planet_cache as pc
    roots, seen = [], set()
    for d in (out_dir, os.path.dirname(os.path.abspath(out_dir)), "out", "Output"):
        p = os.path.abspath(d)
        if p not in seen:
            seen.add(p)
            roots.append(p)
    n = pc.adopt(roots)
    if n:
        print(f"    [planet-cache] adopted {n} previously-downloaded order(s) — they "
              f"are recallable now without re-ordering")


def _write_render_json(a, event_id, r):
    """Render each side's true-colour GeoTIFF from the composites in `r` and write
    <out>/render.json (layer paths + notes + any pending order ids). Shared by
    --planet-render, --planet-resume, --planet-retone and --planet-recall so all four
    produce the exact same file the plugin loads.

    The tone curve is whichever --planet-tone selected; the output filename carries
    the mode so switching modes writes a SEPARATE layer instead of overwriting the
    one already on the canvas — that's what makes the two comparable side by side.

    render.json's 'pending' block carries {side: order_id} for orders Planet is still
    processing, PLUS the AOI they were placed for, so the plugin's Resume button can
    finish them later without re-ordering. It's {} when nothing is pending.
    'reused' is {side: order_id} for sides served from an already-paid-for order, and
    'available' lists every cached order for the event so the plugin can offer a
    picker — together they're how you can tell what this render actually cost."""
    import review_package as rp
    out = dict(pre=None, post=None, notes=[], pending={}, tone=a.planet_tone,
               event_id=event_id, reused=r.get("reused") or {},
               available=r.get("available") or [])
    base = os.path.join(a.out, event_id)
    if a.planet_tone == "linear":
        # "None" tone mode: a plain black/white stretch with NO shaping — no rolloff,
        # cube root, desaturation, or S-curve. It honours the manual stretch spin boxes
        # but NOT the auto-stretch (a scene-fitted stretch is itself a processing choice
        # this mode exists to switch off), and contrast doesn't apply. See rp._linear.
        tone = dict(white=a.planet_white if a.planet_white is not None else rp.WHITE,
                    black=a.planet_black if a.planet_black is not None else 0.0)
        out["stretch"] = dict(tone)
        render = rp._linear
    else:
        tone = dict(contrast=a.planet_contrast if a.planet_contrast is not None
                    else rp.CONTRAST)
        if a.planet_tone == "knee":
            tone["knee"] = a.planet_knee if a.planet_knee is not None else rp.KNEE
            tone["white"] = a.planet_white if a.planet_white is not None else rp.WHITE
            tone["desat"] = a.planet_desat if a.planet_desat is not None else rp.DESAT
            # Fit the stretch to the scene when the scene needs it — an all-ice AOI has
            # nothing inside the knee curve's untouched linear zone and renders flat white
            # otherwise (see review_package.auto_stretch). Computed ONCE from both sides so
            # the before/after layers stay photometrically comparable under the swipe tool.
            # An explicit --planet-white/--planet-black means the caller has already decided
            # what stretch they want, so auto steps aside; auto is skipped entirely by
            # --planet-no-auto-stretch.
            manual = a.planet_white is not None or a.planet_black is not None
            if a.planet_black is not None:
                tone["black"] = a.planet_black
            comps = [c for c in (r.get("pre"), r.get("post")) if c is not None]
            if manual or a.planet_no_auto_stretch:
                out["notes"].append(
                    "auto-stretch: off — " + ("explicit --planet-white/--planet-black"
                                              if manual else "--planet-no-auto-stretch"))
            elif comps:
                black, white_used, onset, note = rp.auto_stretch(
                    comps, white=tone["white"], knee=tone["knee"])
                tone.update(black=black, white=white_used, onset=onset)
                out["notes"].append(note)
            tone.setdefault("black", 0.0)
            # what the pixels were actually rendered with, for the log and for reproducing it
            out["stretch"] = {k: v for k, v in tone.items() if k != "contrast"}
        render = rp._highlight_rolloff if a.planet_tone == "knee" else rp._highlight_natural
    for side in ("pre", "post"):
        comp = r.get(side)
        if comp is None:
            continue
        path = f"{base}_{side}_planet_{a.planet_tone}.tif"
        try:
            render(comp, path, comp.rio.crs, **tone)
            out[side] = path
        except Exception as e:
            out["notes"].append(f"{side}: render failed: {e}")
    out["notes"] += r.get("notes", [])
    orders = r.get("pending") or {}
    if orders:
        out["pending"] = {"orders": orders, "lat": a.lat, "lon": a.lon,
                          "radius_km": a.radius_km, "event_id": event_id}
    render_path = os.path.join(a.out, "render.json")
    with open(render_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    for n in out["notes"]:
        print(f"note: {n}")
    if out["reused"]:
        print("    [planet] served from orders already paid for (no quota): "
              + ", ".join(f"{s} = order {o[:12]}" for s, o in out["reused"].items()))
    print(f"Render -> {render_path}")


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
        elif s == "s1":
            import sar_imagery as si
            r = si.search_event(lat, lon, radius_km, when, pre_days=args.pre_days,
                                post_days=args.post_days, seasonal=args.seasonal,
                                auto_window=args.auto_window)
        elif s == "dem":
            import dem_imagery as di
            r = di.search_event(lat, lon, radius_km, when, pre_days=args.pre_days,
                                post_days=args.post_days, seasonal=args.seasonal,
                                auto_window=args.auto_window)
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

    'auto' previews the Planetary Computer STAC sources — Sentinel-2 + Landsat —
    so you can compare coverage and pick a source; a specific --prefer previews
    just that source. PlanetScope is NOT part of 'auto' anymore: it has its own
    plugin tab, and is searched only when explicitly requested with
    '--prefer planet' (which the PlanetScope tab uses). Pre-2016 events drop
    Sentinel-2 (no coverage) in favor of Landsat. Per-source failures (e.g. Planet
    not authenticated) become notes instead of aborting the whole preview."""
    from concurrent.futures import ThreadPoolExecutor

    sensors = {"auto": ["s2", "landsat"], "planet": ["planet"],
               "s2": ["s2"], "landsat": ["landsat"], "s1": ["s1"],
               "dem": ["dem"]}[args.prefer]
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
            notes += r.get("notes", [])   # source-specific hints (e.g. DEM strips)
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
                    choices=["auto", "planet", "s2", "landsat", "s1", "dem"],
                    help="imagery source; 'auto' = Sentinel-2 -> Landsat (STAC). "
                         "'planet' searches PlanetScope only and is used by the "
                         "plugin's PlanetScope tab (search/preview; runs need a "
                         "PlanetScope order flow, added separately). 's1' searches "
                         "Sentinel-1 RTC (SAR amplitude) and is used by the plugin's "
                         "SAR tab — search/preview only (--search-only required). "
                         "'dem' searches PGC ArcticDEM/EarthDEM/REMA time-stamped DEM "
                         "strips and is used by the plugin's DEM differencing tab — "
                         "also search/preview only.")
    ap.add_argument("--pre-days", type=int, default=60)
    ap.add_argument("--post-days", type=int, default=90)
    ap.add_argument("--cloud-weight", type=float, default=0.5,
                    help="scene ranking: gap-days willing to travel from the event date "
                         "to avoid 1%% cloud (gap_days + cloud_weight*cloud_pct). Lower = "
                         "stay closer to the event date; higher = prefer clearer scenes. "
                         "Ignored with --auto-window. (default 0.5)")
    ap.add_argument("--max-cloud", type=float, default=None,
                    help="max whole-scene cloud cover %% to consider. 100 (or omitted, for "
                         "--search-only) = NO CAP: every acquisition in the window is listed, "
                         "clouds and all, so scenes can be judged by eye rather than by "
                         "metadata. Scene-wide metric, and it only picks WHICH scenes are used "
                         "— a Run composites them as acquired, with no per-pixel cloud masking "
                         "(Sentinel-2/Landsat; see imagery._composite), so a high cap surfaces "
                         "scenes clear over the AOI but cloudy elsewhere (what Planet Explorer "
                         "shows). A Run without --max-cloud "
                         "still caps (PlanetScope 80, Sentinel-2/Landsat 60, either one 20 "
                         "with --auto-window), since it picks scenes for you rather than "
                         "showing them to you.")
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
                         "only). PlanetScope and the windowed search are skipped. "
                         "Ignored with --search-only.")
    ap.add_argument("--post-scene-ids", default=None,
                    help="comma-separated scene IDs for the POST side (see "
                         "--pre-scene-ids). Either side may be given ALONE: a one-sided "
                         "run exports just that side's imagery and skips dNDVI / dNDSI / "
                         "dBrightness, which are pre->post differences. Useful for a "
                         "fresh event with no usable pre-scene yet.")
    ap.add_argument("--seasonal", action="store_true",
                    help="winter event: use prior-year pre window")
    ap.add_argument("--auto-window", action="store_true",
                    help="tightest window: use only the clear scene nearest the event on "
                         "each side; --pre-days/--post-days act as the max search range")
    ap.add_argument("--search-only", action="store_true",
                    help="free dry-run: search candidate pre/post scenes per source and "
                         "write <out>/search.json; no Planet orders or downloads")
    ap.add_argument("--planet-render", action="store_true",
                    help="PlanetScope on-map detail preview: order the scenes given by "
                         "--pre-scene-ids / --post-scene-ids (clipped to the AOI), "
                         "composite each side, render a Highlight Optimized Natural "
                         "Color GeoTIFF per side from the raw surface reflectance, and "
                         "write <out>/render.json with the layer paths. Unlike the free "
                         "tile preview this PLACES A PLANET ORDER (uses quota); used by "
                         "the plugin's PlanetScope tab.")
    ap.add_argument("--planet-recall", action="store_true",
                    help="load PlanetScope imagery ALREADY ORDERED for this event back "
                         "onto the map from the shared order cache: no search, no order, "
                         "NO QUOTA. Takes the newest cached order per side for this "
                         "event/AOI (pin exact ones with --recall-pre-order / "
                         "--recall-post-order), composites and renders it, and writes "
                         "<out>/render.json exactly like --planet-render. Works offline "
                         "when the clips are still on disk; if they've been deleted the "
                         "order is re-downloaded, which is also free. Use this instead of "
                         "--planet-render whenever you've looked at this event before.")
    ap.add_argument("--planet-list-orders", action="store_true",
                    help="list the PlanetScope orders already paid for near this "
                         "location/event and write <out>/planet_orders.json (order id, "
                         "side, date, scene ids, whether the clips are still on disk). "
                         "No API call, no quota. Backs the plugin's order picker; run it "
                         "to see what --planet-recall can load.")
    ap.add_argument("--planet-force-order", action="store_true",
                    help="with --planet-render, ignore the order cache and place a NEW "
                         "order even for scenes already paid for (USES QUOTA). Only "
                         "needed if a cached order is suspected bad.")
    ap.add_argument("--recall-pre-order", default=None,
                    help="Planet order id to recall for the PRE side (see "
                         "--planet-recall). Defaults to the newest cached pre order.")
    ap.add_argument("--recall-post-order", default=None,
                    help="Planet order id to recall for the POST side.")
    ap.add_argument("--planet-resume", action="store_true",
                    help="finish PlanetScope order(s) that a previous --planet-render "
                         "placed but that were still processing when it timed out. Give "
                         "the order ids with --resume-pre-order / --resume-post-order; "
                         "this DOWNLOADS the already-placed order (no new order, no extra "
                         "quota), composites and renders it, and writes <out>/render.json "
                         "just like --planet-render. Backs the plugin's 'Resume pending "
                         "order' button.")
    ap.add_argument("--planet-retone", action="store_true",
                    help="re-render an EARLIER --planet-render with a different "
                         "--planet-tone, reading the SR clips already downloaded under "
                         "<out>/planet_render/<event-id>. No Planet API call, no order, "
                         "no quota, no network — seconds instead of minutes. Needs the "
                         "same --lat/--lon/--radius-km/--datetime (or --event-id) the "
                         "original render used, so it finds the same workdir. Backs the "
                         "plugin's tone-mode switch.")
    ap.add_argument("--planet-tone", default="knee",
                    choices=["knee", "natural", "linear"],
                    help="tone curve for the --planet-render / --planet-resume GeoTIFFs. "
                         "'knee' (DEFAULT) = highlight rolloff: a plain linear stretch "
                         "below the knee, so midtones/shadows are untouched and only "
                         "highlights get compressed. 'natural' = Highlight Optimized "
                         "Natural Color (cube root: even detail across the whole range, "
                         "including inside bright ice, at the cost of softer global "
                         "contrast). 'linear' = None: a plain black/white stretch with NO "
                         "shaping at all (no rolloff, cube root, desaturation, or S-curve) "
                         "— the un-toned reference; bright ice clips to flat white. knee "
                         "and natural get a gentle S-curve (see --planet-contrast); linear "
                         "never does. See review_package.TONE_MODES. 'knee' additionally "
                         "fits its black/white points to a frame that is all snow/ice — see "
                         "--planet-no-auto-stretch; 'linear' honours --planet-white/"
                         "--planet-black but never auto-fits.")
    ap.add_argument("--planet-knee", type=float, default=None,
                    help="knee position for --planet-tone knee, on the 0-1 ramp that "
                         "--planet-white maps to white (default 0.55 -> 0.165 reflectance). "
                         "Lower = more headroom for snow texture, less untouched midtone.")
    ap.add_argument("--planet-white", type=float, default=None,
                    help="reflectance mapped to full white by the linear part of the knee "
                         "curve (default 0.30). Raise it for more highlight headroom at the "
                         "cost of dimmer midtones. Setting this DISABLES the auto-stretch "
                         "(see --planet-no-auto-stretch) — an explicit white point is taken "
                         "as a decision already made.")
    ap.add_argument("--planet-black", type=float, default=None,
                    help="reflectance mapped to black by the knee curve (default 0.0, i.e. "
                         "no black point — see review_package._highlight_rolloff). Raise it "
                         "only for a frame with no dark end, such as an all-ice AOI, where "
                         "the default renders flat white; on ordinary terrain it throws "
                         "away shadow detail. Setting this DISABLES the auto-stretch.")
    ap.add_argument("--planet-no-auto-stretch", action="store_true",
                    help="don't fit the knee curve's black/white points to the scene. By "
                         "DEFAULT --planet-tone knee derives them (see "
                         "review_package.auto_stretch) for a frame with essentially no "
                         "pixels in the curve's untouched linear zone — an AOI that is all "
                         "snow/ice, which otherwise renders flat white with its texture "
                         "compressed into the top few DN. A scene with dark ground to "
                         "protect is left alone automatically, so this flag is only needed "
                         "to force the fixed 0.0-0.30 stretch back on (e.g. to read inside "
                         "a small dark scar that the fitted black point clips).")
    ap.add_argument("--planet-desat", type=float, default=None,
                    help="highlight desaturation strength for --planet-tone knee "
                         "(0-1, default 1.0). The knee curve preserves a pixel's colour "
                         "ratio all the way to white, which faithfully reproduces the "
                         "rainbow speckle Planet's atmospheric correction leaves on "
                         "bright snow at low sun (reflectance overshooting 1.0 per band, "
                         "by different amounts). This fades compressed highlights toward "
                         "neutral to remove it; midtones and shadows below the knee are "
                         "untouched at any setting. 0 = off (pure ratio-preserving).")
    ap.add_argument("--planet-contrast", type=float, default=None,
                    help="S-curve contrast strength for the knee/natural tone modes "
                         "(default 1.15; 1.0 = off/identity). No effect on --planet-tone "
                         "linear, which never adds contrast.")
    ap.add_argument("--resume-pre-order", default=None,
                    help="Planet order id for the PRE side to resume (see --planet-resume).")
    ap.add_argument("--resume-post-order", default=None,
                    help="Planet order id for the POST side to resume.")
    a = ap.parse_args()

    if a.prefer in ("s1", "dem") and not a.search_only:
        print(f"--prefer {a.prefer} is search/preview only "
              "(no composite Run pipeline); add --search-only")
        return 1

    event_id = a.event_id or f"event_{a.when:%y%m%d_%H%M}"
    os.makedirs(a.out, exist_ok=True)

    # What PlanetScope imagery has this account already paid for near here? Pure
    # ledger read — no API call, no quota — so it's safe to run any time to see what
    # --planet-recall could load instead of ordering again.
    if a.planet_list_orders:
        import planet_cache as pc
        _adopt_legacy_orders(a.out)
        entries = pc.entries(event_id=event_id, lat=a.lat, lon=a.lon,
                             radius_km=a.radius_km)
        res = dict(event_id=event_id, lat=a.lat, lon=a.lon, radius_km=a.radius_km,
                   cache_root=pc.cache_root(), orders=entries)
        path = os.path.join(a.out, "planet_orders.json")
        with open(path, "w") as f:
            json.dump(res, f, indent=2, default=str)
        if entries:
            print(f"    {len(entries)} PlanetScope order(s) already paid for near "
                  f"{a.lat:.4f},{a.lon:.4f}:")
            for e in entries:
                print(f"      {e['label']}")
        else:
            print(f"    no cached PlanetScope orders near {a.lat:.4f},{a.lon:.4f} "
                  f"(ledger: {pc.cache_root()})")
        print(f"Orders -> {path}")
        return 0

    # Recall imagery already ordered for this event straight onto the map: no search,
    # no order, no quota. The cheapest path to a second look at an event — and the
    # reason 'Render detail' only ever has to be paid for once per scene.
    if a.planet_recall:
        import planet_imagery as pi
        _adopt_legacy_orders(a.out)
        orders = {}
        if a.recall_pre_order:
            orders["pre"] = a.recall_pre_order.strip()
        if a.recall_post_order:
            orders["post"] = a.recall_post_order.strip()
        print("    [planet-recall] loading PlanetScope scenes already ordered for this "
              "event from the order cache — no search, no order, no quota"
              + (f" (pinned: {', '.join(f'{s}={o[:12]}' for s, o in orders.items())})"
                 if orders else ""))
        try:
            r = pi.recall_preview(a.lat, a.lon, a.radius_km, event_id=event_id,
                                  orders=orders or None)
        except Exception as e:
            traceback.print_exc()
            r = dict(pre=None, post=None, notes=[f"planet recall failed: {e}"],
                     pending={})
        _write_render_json(a, event_id, r)
        return 0

    # PlanetScope on-map detail preview: order + composite the hand-picked scenes and
    # render the Highlight Optimized Natural Color GeoTIFF(s) the plugin loads. Kept
    # separate from process_one because that path routes through imagery.fetch_event,
    # which no longer handles PlanetScope — this calls planet_imagery directly.
    if a.planet_render:
        import planet_imagery as pi
        pre_ids = [x.strip() for x in (a.pre_scene_ids or "").split(",") if x.strip()]
        post_ids = [x.strip() for x in (a.post_scene_ids or "").split(",") if x.strip()]
        if not pre_ids and not post_ids:
            print("--planet-render needs --pre-scene-ids and/or --post-scene-ids")
            return 1
        # Adopt first: a scene ordered by an earlier run (in this project or another)
        # is reused below instead of bought again, so this render may cost nothing.
        _adopt_legacy_orders(a.out)
        print(f"    [planet-render] {len(pre_ids)} pre + {len(post_ids)} post SR "
              f"scene(s), clipped to the AOI. Scenes already ordered are reused free; "
              f"only the rest are ordered"
              + (" (--planet-force-order: re-ordering everything, USES QUOTA)"
                 if a.planet_force_order else ""))
        try:
            r = pi.render_preview(a.lat, a.lon, a.radius_km, pre_ids=pre_ids,
                                  post_ids=post_ids, event_id=event_id,
                                  reuse=not a.planet_force_order)
        except Exception as e:
            traceback.print_exc()
            r = dict(pre=None, post=None, notes=[f"planet render failed: {e}"], pending={})
        _write_render_json(a, event_id, r)
        return 0

    # Re-render an earlier --planet-render with a different tone curve, straight from
    # the clips it left on disk. Cheapest path there is: no Planet client is even
    # constructed, so it works offline and can't touch quota. Backs the plugin's tone
    # switch, which is why switching modes is free and near-instant.
    if a.planet_retone:
        import planet_imagery as pi
        workdir = os.path.join(a.out, "planet_render", event_id)
        print(f"    [planet-retone] re-rendering '{a.planet_tone}' from clips already on "
              f"disk — {workdir} or the order cache (no order, no quota)")
        try:
            r = pi.retone_preview(a.lat, a.lon, a.radius_km, workdir=workdir,
                                  event_id=event_id)
        except Exception as e:
            traceback.print_exc()
            r = dict(pre=None, post=None, notes=[f"planet retone failed: {e}"],
                     pending={})
        _write_render_json(a, event_id, r)
        return 0

    # Finish PlanetScope order(s) that a previous --planet-render placed but that were
    # still processing when it timed out. This DOWNLOADS the already-placed order (no
    # new order, no extra quota) and renders it exactly like --planet-render. Backs the
    # plugin's 'Resume pending order' button.
    if a.planet_resume:
        import planet_imagery as pi
        orders = {}
        if a.resume_pre_order:
            orders["pre"] = a.resume_pre_order.strip()
        if a.resume_post_order:
            orders["post"] = a.resume_post_order.strip()
        if not orders:
            print("--planet-resume needs --resume-pre-order and/or --resume-post-order")
            return 1
        print("    [planet-resume] finishing "
              + ", ".join(f"{s} order {o[:12]}" for s, o in orders.items())
              + " (already placed — no new order, no extra quota)")
        try:
            r = pi.resume_preview(a.lat, a.lon, a.radius_km, orders=orders,
                                  event_id=event_id)
        except Exception as e:
            traceback.print_exc()
            r = dict(pre=None, post=None, notes=[f"planet resume failed: {e}"], pending={})
        _write_render_json(a, event_id, r)
        return 0

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
