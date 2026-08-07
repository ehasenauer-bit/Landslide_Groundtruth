#!/usr/bin/env python3
"""Per-event imagery pipeline for detected Alaskan landslides.

`process_one` pulls the pre/post composites + dNDVI + dBrightness for a single
event (PlanetScope ~3 m -> Sentinel-2 ~10 m -> Landsat ~30 m) and exports a QGIS
review package plus a per-event metadata.json of the scenes used.

This is the imagery-gathering stage only. Confirming the slide and digitizing
the scar happens by eye in QGIS against the exported layers.

This module is a library: `run_single.py` (and the QGIS plugin, which shells out
to it) imports `process_one` and feeds it a manually-constructed event dict +
argparse Namespace. There is no batch CLI here anymore.
"""
from __future__ import annotations
import os


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
                         allow_test_quality=getattr(args, "quality", "standard") == "any",
                         pre_ids=getattr(args, "pre_scene_ids", None),
                         post_ids=getattr(args, "post_scene_ids", None))
    if img is None:
        return dict(status="no_imagery", event_id=ev["event_id"])

    # Grid CRS to write every layer in. dNDVI is absent on a one-sided run (it is a
    # difference), so fall back to whichever composite the run does have — they all
    # share one grid.
    ref = next((img[k] for k in ("dndvi", "pre", "post") if img.get(k) is not None),
               None)
    if ref is None:
        return dict(status="no_imagery", event_id=ev["event_id"])
    src_crs = ref.rio.crs
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
        # acquisition dates of the composited scenes; the plugin logs them, and
        # they are already baked into the layer filenames (see review_package)
        pre_dates=img.get("pre_dates"), post_dates=img.get("post_dates"),
        layers=layers, metadata=meta_path,
        fallback_note=img.get("fallback_note"),   # why PlanetScope wasn't used, if it fell back
    )
