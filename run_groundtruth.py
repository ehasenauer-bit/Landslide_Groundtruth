#!/usr/bin/env python3
"""Gather pre/post satellite imagery for detected Alaskan landslides.

Pipeline per event:
  1. read predicted lat/lon + time from the inventory
  2. pull pre/post composites + dNDVI + dBrightness via the imagery chain
     (PlanetScope ~3 m -> Sentinel-2 ~10 m -> Landsat ~30 m)
  3. export a QGIS review package (true/false-colour pre+post, change rasters,
     predicted point) plus a per-event metadata.json of the scenes used

This is the imagery-gathering stage only. Confirming the slide and digitizing
the scar happens by eye in QGIS against the exported layers; this tool just makes
sure the right before/after imagery is on the table.

Usage:
  python run_groundtruth.py --inventory LandslideInventory.xlsx \\
      --out out/ [--event 240807_Pedersen] [--limit 5] \\
      [--prefer planet] [--pre-days 60 --post-days 90] [--cloud-weight 0.5] \\
      [--seasonal] [--auto-window] [--dry-run]

--dry-run skips imagery and just reports what would be processed (no network).
"""
from __future__ import annotations
import argparse
import os
import sys
import traceback
import pandas as pd

import inventory as inv


def process_one(ev, args):
    import imagery as im
    import review_package as rp
    from pyproj import Transformer

    # imagery footprint is widened relative to the detection search radius so the
    # full scar fits in frame (matches what you see when you pan out in QGIS)
    img_radius = ev["search_radius_km"] * args.radius_scale
    planet_dir = os.path.join(args.out, "planet_cache", ev["event_id"])
    img = im.fetch_event(ev["lat"], ev["lon"], img_radius, ev["datetime_utc"],
                         pre_days=args.pre_days, post_days=args.post_days,
                         seasonal=args.seasonal, prefer=args.prefer, workdir=planet_dir,
                         auto_window=getattr(args, "auto_window", False),
                         cloud_weight=getattr(args, "cloud_weight", 0.5),
                         max_cloud_pct=getattr(args, "max_cloud", None),
                         require_point=getattr(args, "coverage", "aoi") == "point",
                         allow_test_quality=getattr(args, "quality", "standard") == "any")
    if img is None:
        return dict(status="no_imagery", event_id=ev["event_id"])

    src_crs = img["dndvi"].rio.crs
    tf = Transformer.from_crs(4326, src_crs, always_xy=True)
    near = tf.transform(ev["lon"], ev["lat"])

    # scenes may arrive as a comma-separated string (CLI) or a list (caller);
    # normalize to a list[str] so set(scenes) can't iterate a bare string's chars.
    scenes = getattr(args, "scenes", None)
    if isinstance(scenes, str):
        scenes = [s.strip() for s in scenes.split(",") if s.strip()]

    pkg_dir = os.path.join(args.out, "qgis_packages")
    layers = rp.export_review_package(pkg_dir, ev["event_id"], img, src_crs, near,
                                      scenes=scenes)
    meta_path = rp.write_metadata(pkg_dir, ev, img, layers)

    return dict(
        status="ok", event_id=ev["event_id"], sensor=img["sensor"],
        n_pre_scenes=len(img["pre_scenes"]), n_post_scenes=len(img["post_scenes"]),
        pre_scenes=img["pre_scenes"], post_scenes=img["post_scenes"],
        layers=layers, metadata=meta_path,
        fallback_note=img.get("fallback_note"),   # why PlanetScope wasn't used, if it fell back
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--event", help="process a single event_id")
    ap.add_argument("--limit", type=int, default=0, help="cap number of events (0 = all)")
    ap.add_argument("--prefer", default="auto", choices=["auto", "planet", "s2", "landsat"],
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
                         "(default), 'any' = also include 'test'-quality scenes (often the only "
                         "ones near a fresh event; looser calibration, fine for visual review).")
    ap.add_argument("--radius-scale", type=float, default=1.5,
                    help="multiply the search radius to widen the downloaded imagery footprint")
    ap.add_argument("--scenes", default=None,
                    help="comma-separated review layers to write: true_color, "
                         "highlight_natural, false_color, ndvi, dndvi, dbright "
                         "(default: all). The predicted-point layer is always written.")
    ap.add_argument("--seasonal", action="store_true", help="winter event: use prior-year pre window")
    ap.add_argument("--auto-window", action="store_true",
                    help="tightest window: use only the clear scene nearest the event "
                         "on each side; --pre-days/--post-days act as the max search range")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    df = inv.load_events(args.inventory)
    todo = inv.needs_groundtruth(df)
    if args.event:
        todo = todo[todo.event_id == args.event]
        if todo.empty:
            sys.exit(f"event {args.event} not found among ground-truthing candidates")
    if args.limit:
        todo = todo.head(args.limit)

    print(f"{len(df)} events in inventory; {len(todo)} to process")
    flagged = df[df.qc_flags != ""]
    if not flagged.empty:
        print("\nCOORDINATE QC FLAGS (fix in the source sheet before trusting output):")
        for _, r in flagged.iterrows():
            print(f"  {r.event_id}: {r.qc_flags}")

    if args.dry_run:
        cols = ["event_id", "datetime_utc", "lat", "lon", "loc_source",
                "search_radius_km", "in_esec", "qc_flags"]
        print("\n" + todo[cols].to_string(index=False))
        return

    os.makedirs(args.out, exist_ok=True)
    results = []
    for _, ev in todo.iterrows():
        print(f"\n>>> {ev.event_id}  ({ev.datetime_utc}, {ev.lat:.4f},{ev.lon:.4f}, "
              f"r={ev.search_radius_km}km, src={ev.loc_source})")
        try:
            res = process_one(ev.to_dict(), args)
            print(f"    {res}")
        except Exception as e:
            traceback.print_exc()
            res = dict(status="error", event_id=ev.event_id, error=str(e))
        results.append(res)

    summary = pd.DataFrame(results)
    sp = os.path.join(args.out, "summary.csv")
    # scalar columns only in the CSV; per-event scene ids live in each metadata.json
    cols = [c for c in ("status", "event_id", "sensor", "n_pre_scenes",
                        "n_post_scenes", "metadata", "error") if c in summary.columns]
    summary.to_csv(sp, index=False, columns=cols)
    print(f"\nSummary -> {sp}")
    print(summary.to_string(index=False, columns=cols))


if __name__ == "__main__":
    main()
