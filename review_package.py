"""Export a per-event QGIS review package from pre/post imagery.

This is purely the visual-review hinge — there is NO automatic delineation. The
package gives a human everything needed to spot a landslide scar by eye and
digitize it in QGIS. Which of these "scenes" get written is selectable (see
SCENE_KEYS / the `scenes` arg); by default all are produced:

  <event>_pre_rgb.tif        true-colour BEFORE (context, false-positive check)
  <event>_post_rgb.tif       true-colour AFTER
  <event>_pre_highlight.tif  Highlight Optimized Natural Color BEFORE (see _highlight_natural)
  <event>_post_highlight.tif Highlight Optimized Natural Color AFTER
  <event>_pre_falsecolor.tif NIR-red-green BEFORE  (vegetation = bright red)
  <event>_post_falsecolor.tif NIR-red-green AFTER  (fresh scar = dark/bare)
  <event>_pre_ndvi.tif       NDVI BEFORE
  <event>_post_ndvi.tif      NDVI AFTER
  <event>_dndvi.tif          pre->post NDVI change  (vegetation loss = strong negative)
  <event>_dbright.tif        pre->post brightness/albedo change (bare rock/soil = positive)
  <event>_point.gpkg         the predicted (seismic) epicentre to search around (always written)
  <event>_metadata.json      sensor + scene ids/dates used, and the layer file list

Compare the pre/post pairs with the QGIS Swipe tool and read the two change
rasters (dNDVI + dBrightness) — where they agree is a strong scar signal.
"""
from __future__ import annotations
import json
import os

import numpy as np
import geopandas as gpd
from shapely.geometry import Point
import rioxarray  # noqa: F401  (registers the .rio accessor)

# Canonical, ordered list of selectable download "scenes". Each key is a token
# accepted by run_single.py --scenes and produced by export_review_package; the
# QGIS dock builds its checkboxes from the same keys. The predicted-point layer
# is NOT in here — it is always written as the digitizing anchor.
SCENE_KEYS = ["true_color", "highlight_natural", "false_color",
              "ndvi", "dndvi", "dbright"]


def _rgb(comp, bands, path, src_crs):
    """Write a 3-band uint8 GeoTIFF from reflectance bands, stretched 0-0.3 -> 0-255."""
    rgb = comp.sel(band=bands)
    rgb = (rgb.clip(0, 0.3) / 0.3 * 255).fillna(0).astype("uint8")
    # Drop source attrs that no longer match a 3-band uint8 image: masked-read
    # sources (e.g. Planet) carry a float NaN nodata that can't cast to uint8 and
    # a 4-band 'long_name' that trips rioxarray's band-name check.
    rgb.attrs = {}
    rgb.rio.write_crs(src_crs).rio.write_nodata(0).rio.to_raster(path, driver="GTiff")


def _highlight_natural(comp, path, src_crs):
    """Write the "Highlight Optimized Natural Color" rendering of the true-colour bands.

    This reproduces the Sentinel Hub custom script of that name (author Marko
    Repše, CC BY-SA 4.0) — the same look the Copernicus Browser offers. Instead
    of our linear 0-0.3 stretch it applies a cube-root tone curve per band,
        value = cbrt(0.6 * reflectance)  clipped to 0-1,
    which lifts shadow detail and compresses highlights so a single stretch reads
    well across the scene's whole dynamic range (less blown-out snow/cloud, more
    texture in dark forest/water). It is NOT a separate data download — just a
    second rendering of the same composite bands. This is the S2-L2A form; our
    bands are already 0-1 surface reflectance, so it applies as-is to Sentinel-2
    and approximately to Landsat too.
    """
    rgb = comp.sel(band=["red", "green", "blue"]).clip(0, None)
    rgb = np.cbrt(0.6 * rgb).clip(0, 1)
    rgb = (rgb * 255).fillna(0).astype("uint8")
    rgb.attrs = {}    # see _rgb: stale float-nodata / 4-band long_name would break the write
    rgb.rio.write_crs(src_crs).rio.write_nodata(0).rio.to_raster(path, driver="GTiff")


def export_review_package(out_dir, event_id, img, src_crs, near_pt, scenes=None):
    """Write the per-event review layers QGIS opens. Returns the list of layer paths.

    img: the dict returned by imagery.fetch_event / planet_imagery.fetch_event
         (pre, post, ndvi_pre/post, dndvi, dbright composites on a shared grid).
    near_pt: (x, y) predicted epicentre in src_crs, written as the search point.
    scenes: which products to write — a subset of SCENE_KEYS. None/empty -> all.
            The predicted-point layer is always written regardless.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, event_id)
    want = set(scenes) if scenes else set(SCENE_KEYS)
    layers = []

    # true-colour pre/post — context and false-positive checks (clearcut, burn, cloud)
    if "true_color" in want:
        _rgb(img["pre"], ["red", "green", "blue"], f"{base}_pre_rgb.tif", src_crs)
        _rgb(img["post"], ["red", "green", "blue"], f"{base}_post_rgb.tif", src_crs)
        layers += [f"{base}_pre_rgb.tif", f"{base}_post_rgb.tif"]

    # Highlight Optimized Natural Color pre/post — same bands, cube-root tone curve
    if "highlight_natural" in want:
        _highlight_natural(img["pre"], f"{base}_pre_highlight.tif", src_crs)
        _highlight_natural(img["post"], f"{base}_post_highlight.tif", src_crs)
        layers += [f"{base}_pre_highlight.tif", f"{base}_post_highlight.tif"]

    # false-colour (NIR-red-green) — vegetation pops bright red, fresh bare scar reads dark
    if "false_color" in want:
        _rgb(img["pre"], ["nir", "red", "green"], f"{base}_pre_falsecolor.tif", src_crs)
        _rgb(img["post"], ["nir", "red", "green"], f"{base}_post_falsecolor.tif", src_crs)
        layers += [f"{base}_pre_falsecolor.tif", f"{base}_post_falsecolor.tif"]

    # raw NDVI pre/post — the inputs behind dNDVI, handy for thresholding by eye
    if "ndvi" in want:
        img["ndvi_pre"].rename("ndvi").rio.write_crs(src_crs).rio.to_raster(
            f"{base}_pre_ndvi.tif", driver="GTiff")
        img["ndvi_post"].rename("ndvi").rio.write_crs(src_crs).rio.to_raster(
            f"{base}_post_ndvi.tif", driver="GTiff")
        layers += [f"{base}_pre_ndvi.tif", f"{base}_post_ndvi.tif"]

    # change rasters — where a scar lights up
    if "dndvi" in want:
        img["dndvi"].rio.write_crs(src_crs).rio.to_raster(f"{base}_dndvi.tif", driver="GTiff")
        layers.append(f"{base}_dndvi.tif")
    if "dbright" in want and img.get("dbright") is not None:
        img["dbright"].rio.write_crs(src_crs).rio.to_raster(f"{base}_dbright.tif", driver="GTiff")
        layers.append(f"{base}_dbright.tif")

    # the predicted epicentre to search around (digitize the scar against it)
    pt = gpd.GeoDataFrame([{"geometry": Point(near_pt), "kind": "predicted"}], crs=src_crs)
    pt.to_file(f"{base}_point.gpkg", driver="GPKG")
    layers.append(f"{base}_point.gpkg")

    return layers


def write_metadata(out_dir, ev, img, layers):
    """Write <event>_metadata.json describing the scenes used. Returns its path."""
    os.makedirs(out_dir, exist_ok=True)
    meta = dict(
        event_id=ev["event_id"],
        datetime_utc=ev.get("datetime_utc"),
        lat=ev.get("lat"), lon=ev.get("lon"),
        loc_source=ev.get("loc_source"),
        search_radius_km=ev.get("search_radius_km"),
        sensor=img["sensor"],
        n_pre_scenes=len(img["pre_scenes"]),
        n_post_scenes=len(img["post_scenes"]),
        pre_scenes=img["pre_scenes"],
        post_scenes=img["post_scenes"],
        layers=layers,
    )
    path = os.path.join(out_dir, f"{ev['event_id']}_metadata.json")
    with open(path, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return path
