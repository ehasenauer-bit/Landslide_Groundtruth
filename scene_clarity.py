"""List Sentinel-2 (and optionally Landsat) scenes near a point, sorted by how
clear they actually are OVER YOUR AOI — a sanity check on the scenes imagery.py
picked for an event.

Why this exists
---------------
imagery.py ranks scenes by a BLEND of cloud cover and temporal distance to the
event (gap_days + cloud_weight*cloud_pct), so the scenes it shows are "closest
acceptably-clear", not "clearest". And the cloud number it shows is the WHOLE
Sentinel-2 tile's eo:cloud_cover, which can differ wildly from the cloudiness
over a small landslide footprint.

This tool answers the two questions that actually matter:
  1. Was a clearer scene skipped?  -> it ranks the whole window by clarity, not
     by date proximity, and prints imagery.py's blend score alongside so you can
     see why the picked scenes won.
  2. Is the cloud over MY AOI?      -> for Sentinel-2 it reads the SCL band (for
     Landsat, qa_pixel) over the AOI box and reports the cloud/snow fraction
     there, next to the whole-tile number.

It reuses imagery.py's signed STAC client and helpers, so what you see here is
the same catalog the plugin queries — no full-scene downloads, just a small
windowed read of the classification band per scene.

Usage
-----
  python scene_clarity.py LAT LON EVENT_DATE [options]

  LAT LON       event location, decimal degrees (e.g. 61.23 -149.87)
  EVENT_DATE    YYYY-MM-DD

Options
  --pre-days N    pre-window length in days (default 60)
  --post-days N   post-window length in days (default 60)
  --radius-km R   AOI half-width in km (default 10)
  --max-cloud P   whole-tile cloud cap for the STAC query (default 100 = no cap,
                  so you can see clearer scenes the plugin's 60% cap would hide)
  --seasonal      shift the pre window one year earlier (snow-season events)
  --landsat       also list Landsat C2 L2 scenes (adds dates Sentinel-2 missed)
  --tile-only     skip the per-AOI band read (much faster; tile cloud only)
  --urls          print each scene's browse-preview URL under its row
"""
from __future__ import annotations
import argparse
import datetime as dt
import numpy as np

import imagery  # reuse the signed STAC client, bbox/utm helpers, windows()

# SCL cloud-ish classes (cloud shadow, cloud med/high prob, thin cirrus).
# 11 = snow/ice is reported separately: in Alaska it is a real confounder for
# dNDVI but it is NOT cloud, so collapsing them would mislead.
_S2_CLOUD_SCL = [3, 8, 9, 10]
_S2_SNOW_SCL = [11]


def _search_window(lat, lon, radius_km, start, end, coll, max_cloud):
    """Every scene in [start, end] intersecting the AOI, cloud-capped, unranked."""
    cat = imagery._client()
    items = imagery._search_items(
        cat,
        collections=[coll],
        bbox=imagery._bbox(lat, lon, radius_km),
        datetime=f"{start.date()}/{end.date()}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )
    return [i for i in items if i.datetime is not None]


def _aoi_stats(item, lat, lon, radius_km, sensor):
    """Cloud/snow fraction and AOI coverage from the scene-classification band.

    Reads only the SCL (S2) / qa_pixel (Landsat) band, windowed to the AOI box,
    so it streams a few hundred KB per scene rather than the full tile. Returns
    cloud_pct/snow_pct as a fraction of the VALID (non-nodata) AOI pixels, plus
    coverage_pct = how much of the AOI box this acquisition actually fills (low
    coverage flags the diagonal nodata gaps imagery._has_coverage guards against).
    """
    import stackstac
    epsg = imagery._utm_epsg(lat, lon)
    asset, res = ("SCL", 20) if sensor == "s2" else ("qa_pixel", 30)
    stack = stackstac.stack(
        [item], assets=[asset], epsg=epsg, resolution=res,
        bounds_latlon=imagery._bbox(lat, lon, radius_km),
        chunksize=2048, rescale=False, dtype="float64", fill_value=np.nan,
    )
    arr = stack.isel(time=0, band=0).compute().values
    valid = np.isfinite(arr)
    n = int(valid.sum())
    if n == 0:
        return dict(cloud_pct=None, snow_pct=None, coverage_pct=0.0)
    if sensor == "s2":
        cloud = np.isin(arr, _S2_CLOUD_SCL) & valid
        snow = np.isin(arr, _S2_SNOW_SCL) & valid
    else:
        qa = np.where(valid, arr, 0).astype("uint16")
        # QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow
        cloud = ((((qa >> 1) | (qa >> 2) | (qa >> 3) | (qa >> 4)) & 1).astype(bool)) & valid
        snow = (((qa >> 5) & 1).astype(bool)) & valid
    return dict(cloud_pct=100.0 * cloud.sum() / n,
                snow_pct=100.0 * snow.sum() / n,
                coverage_pct=100.0 * n / arr.size)


def _browse_url(item):
    for key in ("rendered_preview", "thumbnail"):
        asset = item.assets.get(key)
        if asset is not None:
            return asset.href
    return None


def _row(item, event_time, lat, lon, radius_km, sensor, tile_only):
    d = item.datetime.replace(tzinfo=None)
    tile_cloud = item.properties.get("eo:cloud_cover")
    gap = abs((d - event_time).days)
    # imagery.py's default ranking cost (cloud_weight=0.5); lower = picked sooner
    blend = gap + 0.5 * (100.0 if tile_cloud is None else tile_cloud)
    aoi = dict(cloud_pct=None, snow_pct=None, coverage_pct=None)
    if not tile_only:
        try:
            aoi = _aoi_stats(item, lat, lon, radius_km, sensor)
        except Exception as e:  # one bad COG read must not sink the whole listing
            aoi = dict(cloud_pct=None, snow_pct=None, coverage_pct=None,
                       error=type(e).__name__)
    return dict(id=item.id, date=d, gap=gap, tile_cloud=tile_cloud,
                blend=blend, browse=_browse_url(item), **aoi)


def _fmt(v, spec):
    return ("{:" + spec + "}").format(v) if v is not None else "  -"


def _print_table(rows, tile_only, urls):
    if not rows:
        print("    (no scenes found)")
        return
    # clearest first: by AOI cloud when we have it, else by whole-tile cloud.
    # Scenes with no AOI coverage (nodata gap) sort last so they don't masquerade
    # as "clearest" just because their cloud fraction is undefined.
    def key(r):
        c = r["cloud_pct"] if r["cloud_pct"] is not None else r["tile_cloud"]
        no_cov = (r.get("coverage_pct") == 0.0)
        return (no_cov, 1e9 if c is None else c, r["gap"])
    rows = sorted(rows, key=key)

    hdr = f"    {'date':10}  {'gap':>4}  {'tile%':>6}"
    if not tile_only:
        hdr += f"  {'AOI%':>6}  {'snow%':>6}  {'cov%':>5}"
    hdr += f"  {'blend':>6}  id"
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for r in rows:
        line = (f"    {r['date'].date().isoformat():10}  {r['gap']:>4}  "
                f"{_fmt(r['tile_cloud'], '6.1f')}")
        if not tile_only:
            cov = r.get("coverage_pct")
            covstr = "  GAP" if cov == 0.0 else _fmt(cov, '5.0f')
            line += (f"  {_fmt(r['cloud_pct'], '6.1f')}  "
                     f"{_fmt(r['snow_pct'], '6.1f')}  {covstr}")
        line += f"  {r['blend']:>6.1f}  {r['id']}"
        if r.get("error"):
            line += f"  [AOI read failed: {r['error']}]"
        print(line)
        if urls and r["browse"]:
            print(f"        {r['browse']}")


def main():
    ap = argparse.ArgumentParser(
        description="Rank Sentinel-2/Landsat scenes near a point by clarity over the AOI.")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("event_date", help="YYYY-MM-DD")
    ap.add_argument("--pre-days", type=int, default=60)
    ap.add_argument("--post-days", type=int, default=60)
    ap.add_argument("--radius-km", type=float, default=10.0)
    ap.add_argument("--max-cloud", type=float, default=100.0,
                    help="whole-tile cloud cap for the STAC query (default 100)")
    ap.add_argument("--seasonal", action="store_true")
    ap.add_argument("--landsat", action="store_true",
                    help="also list Landsat C2 L2 scenes")
    ap.add_argument("--tile-only", action="store_true",
                    help="skip the per-AOI band read (faster; tile cloud only)")
    ap.add_argument("--urls", action="store_true",
                    help="print each scene's browse-preview URL")
    args = ap.parse_args()

    event_time = dt.datetime.strptime(args.event_date, "%Y-%m-%d")
    pre0, pre1, post0, post1 = imagery.windows(
        event_time, args.pre_days, args.post_days, args.seasonal)

    sensors = [("s2", "sentinel-2-l2a", "Sentinel-2")]
    if args.landsat:
        sensors.append(("landsat", "landsat-c2-l2", "Landsat"))

    print(f"\nEvent: {args.lat}, {args.lon} @ {event_time.date()}   "
          f"AOI radius {args.radius_km} km")
    print(f"Ranking by cloud OVER THE AOI (clearest first). 'blend' = imagery.py's "
          f"pick score (gap + 0.5*tile%); lower won.\n")

    for sensor, coll, label in sensors:
        for side, (s, e) in (("PRE", (pre0, pre1)), ("POST", (post0, post1))):
            print(f"=== {label} {side}  {s.date()} -> {e.date()} ===")
            items = _search_window(args.lat, args.lon, args.radius_km, s, e,
                                   coll, args.max_cloud)
            rows = [_row(i, event_time, args.lat, args.lon, args.radius_km,
                         sensor, args.tile_only) for i in items]
            _print_table(rows, args.tile_only, args.urls)
            print()


if __name__ == "__main__":
    main()
