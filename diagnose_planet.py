#!/usr/bin/env python3
"""Why isn't a given PlanetScope scene found? Diagnostic for the search filters.

Lists every PSScene near the event in the pre and post windows together with the
two attributes the pipeline filters on but Planet Explorer does NOT:
  - download permission  (planet_imagery uses df.permission_filter())
  - quality_category     (planet_imagery uses df.std_quality_filter() == "standard")
so you can see exactly which filter drops a scene you can see in Planet Explorer.

This search drops the permission + quality filters on purpose (geometry + date +
cloud only) so the dropped scenes still SHOW here, each marked keep/DROP and why.

Free: Data API search only — no orders, no downloads, no quota consumed.

Usage:
  python diagnose_planet.py --lat 61.05 --lon -148.45 \
      --datetime "2024-09-20 12:00" --radius-km 5 --pre-days 60 --post-days 90
"""
from __future__ import annotations
import argparse
import datetime as dt

import imagery as im            # windows()
import planet_imagery as pi     # _client, _bbox_geojson, _acquired, ITEM_TYPE


def _parse_dt(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognized datetime: {s!r}")


def _can_download(item) -> bool:
    """True if the account can download this item — what df.permission_filter() keeps.
    Items carry a '_permissions' list like ['assets.ortho_analytic_4b_sr:download', ...]."""
    return any(str(p).endswith(":download") for p in (item.get("_permissions") or []))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--datetime", dest="when", type=_parse_dt, required=True,
                    help="event time (UTC), e.g. '2024-09-20 12:00'")
    ap.add_argument("--radius-km", type=float, default=5.0)
    ap.add_argument("--pre-days", type=int, default=60)
    ap.add_argument("--post-days", type=int, default=90)
    ap.add_argument("--max-cloud", type=float, default=100.0,
                    help="cloud %% cap for the diagnostic (default 100 = show every scene)")
    ap.add_argument("--top", type=int, default=20,
                    help="how many nearest scenes to print per window (default 20)")
    a = ap.parse_args()

    from planet import data_filter as df
    pl = pi._client()
    aoi = pi._bbox_geojson(a.lat, a.lon, a.radius_km)
    pre0, pre1, post0, post1 = im.windows(a.when, a.pre_days, a.post_days, False)

    print(f"event {a.when}  ({a.lat},{a.lon})  r={a.radius_km}km")
    print("columns: kept? = what the plugin's permission + std_quality filters keep")

    any_dropped = False
    for side, start, end in (("PRE", pre0, pre1), ("POST", post0, post1)):
        # geometry + date + cloud ONLY: deliberately omit permission & std_quality
        # so the scenes those two filters would remove still appear below.
        flt = df.and_filter([
            df.geometry_filter(aoi),
            df.date_range_filter("acquired", gte=start, lte=end),
            df.range_filter("cloud_cover", lte=a.max_cloud / 100.0),
        ])
        items = list(pl.data.search([pi.ITEM_TYPE], search_filter=flt, limit=0))
        items.sort(key=lambda i: abs(pi._acquired(i) - a.when))

        print(f"\n=== {side}  window {start.date()}..{end.date()}  "
              f"({len(items)} PSScene items overlap the AOI) ===")
        print(f"{'date (UTC)':16} {'gap':>4} {'cloud%':>6} {'quality':10} "
              f"{'download?':9} {'kept?':5}  scene id")
        for i in items[:a.top]:
            d = pi._acquired(i)
            p = i["properties"]
            cloud = p.get("cloud_cover")
            q = p.get("quality_category", "?")
            dl = _can_download(i)
            kept = dl and q == "standard"
            any_dropped |= not kept
            cloud_s = "" if cloud is None else f"{round(cloud * 100)}"
            print(f"{d.isoformat()[:16]:16} {abs((d - a.when).days):>4} "
                  f"{cloud_s:>6} {q:10} {('yes' if dl else 'NO'):9} "
                  f"{('keep' if kept else 'DROP'):5}  {i['id']}")

    print("\nHow to read it:")
    print("  DROP + download?=NO   -> excluded by permission_filter() (you can't ORDER it;")
    print("                           relaxing the filter would only let you SEE it, not use it)")
    print("  DROP + quality!=standard -> excluded by std_quality_filter() (a 'test'/non-standard")
    print("                           acquisition; this one is safe to relax to recover the scene)")
    if not any_dropped:
        print("  (nothing was dropped — every overlapping scene already passes both filters)")


if __name__ == "__main__":
    main()
