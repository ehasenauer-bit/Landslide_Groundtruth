"""OPERA Sentinel-1 (ASF DAAC) discovery + layover/shadow mask ingest.

Implements quick-win #3 of the Alaska SAR report: get a *trustworthy* geometry
mask so steep-terrain layover/shadow pixels are excluded from the change-
detection stats instead of being read as change. The report first suggested
OPERA DIST-ALERT-S1 as a turnkey disturbance prior, but a live NASA CMR check
(2026-08-20) found **no DIST-ALERT-S1 collection exists yet** — only the optical
DIST-ALERT-HLS (cloud/snow/night-limited, weak for Alaska). The real, available
OPERA Sentinel-1 lever is the RTC-S1 product, whose every granule ships a
per-pixel ``_mask.tif`` layover/shadow layer.

Two halves, deliberately split by their auth needs:
  * DISCOVERY  — NASA CMR granule search is PUBLIC (no login). ``search_rtc_s1``
                 and ``search_dist_hls`` run anonymously; that is the half this
                 module fully exercises and the plugin can call during a search.
  * DOWNLOAD   — the COGs live on datapool.asf.alaska.edu behind NASA Earthdata
                 Login. ``download`` uses a bearer token (env EARTHDATA_TOKEN) or
                 ~/.netrc; it no-ops loudly if neither is present.

CLI self-test (discovery only, no credentials needed):
    python -m landslide_groundtruth.opera_s1 --lat 63.07 --lon -151.0 \
        --datetime 2023-09-13T09:30 --pre-days 12 --post-days 12
"""
import argparse
import datetime as dt
import math
import os
import re

import requests

CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
RTC_S1 = "OPERA_L2_RTC-S1_V1"
DIST_HLS = "OPERA_L3_DIST-ALERT-HLS_V1"
EDL_HOST = "urs.earthdata.nasa.gov"

_ACQ_RE = re.compile(r"_(\d{8}T\d{6}Z)_")          # first = acquisition start
_TRACK_RE = re.compile(r"_T(\d+)-")                 # burst track id


# --------------------------------------------------------------------------
# discovery (public CMR — no auth)
# --------------------------------------------------------------------------
def bbox_from_point(lat, lon, radius_km):
    """(W, S, E, N) degrees around a point. Longitude scaled by latitude so the
    box stays ~square at Alaska latitudes."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _acq_dt(granule_ur):
    m = _ACQ_RE.search(granule_ur or "")
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ")


def _assets(umm):
    """Pull the HTTPS COG asset URLs out of a granule's RelatedUrls."""
    out = {}
    for ru in umm.get("RelatedUrls", []):
        url = ru.get("URL", "")
        if not url.startswith("http"):
            continue
        low = url.lower()
        if low.endswith("_vv.tif"):
            out["vv"] = url
        elif low.endswith("_vh.tif"):
            out["vh"] = url
        elif low.endswith("_mask.tif"):
            out["mask"] = url
        elif low.endswith(".h5"):
            out["h5"] = url
        elif "browse" in low and low.endswith(".png") \
                and "low-res" not in low and "thumbnail" not in low:
            out["browse"] = url
    return out


def _search(short_name, bbox, event_dt, pre_days, post_days, page_size=200):
    w, s, e, n = bbox
    t0 = (event_dt - dt.timedelta(days=pre_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = (event_dt + dt.timedelta(days=post_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "short_name": short_name,
        "bounding_box": ",".join(f"{v:.4f}" for v in (w, s, e, n)),
        "temporal": f"{t0},{t1}",
        "page_size": page_size,
        "sort_key": "start_date",
    }
    r = requests.get(CMR, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("items", [])


def search_rtc_s1(lat, lon, event_dt, pre_days=12, post_days=12, radius_km=8):
    """List OPERA RTC-S1 granules imaging the AOI within the event window.

    Returns dicts: {granule_ur, datetime, side ('pre'/'post'), gap_days,
    track, assets{vv,vh,mask,h5,browse}} — sorted by absolute gap from the event.
    """
    bbox = bbox_from_point(lat, lon, radius_km)
    items = _search(RTC_S1, bbox, event_dt, pre_days, post_days)
    out = []
    for it in items:
        umm = it["umm"]
        ur = umm.get("GranuleUR", "")
        d = _acq_dt(ur)
        track_m = _TRACK_RE.search(ur)
        out.append(dict(
            granule_ur=ur,
            datetime=d,
            date=d.isoformat() if d else None,
            side=("pre" if d and d < event_dt else "post") if d else None,
            gap_days=(round((d - event_dt).total_seconds() / 86400.0) if d else None),
            track=int(track_m.group(1)) if track_m else None,
            assets=_assets(umm),
        ))
    out.sort(key=lambda g: (abs(g["gap_days"]) if g["gap_days"] is not None else 1e9))
    return out


def search_dist_hls(lat, lon, event_dt, pre_days=0, post_days=14, radius_km=8):
    """List OPERA DIST-ALERT-HLS (optical) granules — the only OPERA disturbance
    alert that currently exists. Useful ONLY as a clear-sky secondary confirmation;
    over Alaska cloud/snow/polar-night make it unreliable as a primary trigger."""
    bbox = bbox_from_point(lat, lon, radius_km)
    items = _search(DIST_HLS, bbox, event_dt, pre_days, post_days)
    return [dict(granule_ur=it["umm"].get("GranuleUR", ""),
                 datetime=_acq_dt(it["umm"].get("GranuleUR", "")),
                 assets=_assets(it["umm"])) for it in items]


def nearest_mask_granule(granules, side):
    """The pre- or post-event RTC-S1 granule closest to the event that actually
    carries a _mask.tif, or None."""
    cands = [g for g in granules if g.get("side") == side and g["assets"].get("mask")]
    return cands[0] if cands else None      # already gap-sorted


# --------------------------------------------------------------------------
# download (needs NASA Earthdata Login)
# --------------------------------------------------------------------------
def edl_session():
    """A requests session authenticated for ASF DAAC downloads. Prefers a bearer
    token (env EARTHDATA_TOKEN); otherwise relies on ~/.netrc entry for
    urs.earthdata.nasa.gov. Returns (session, how) where `how` names the method,
    or (None, reason) if no credentials are available."""
    token = os.environ.get("EARTHDATA_TOKEN")
    s = requests.Session()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
        return s, "bearer-token"
    netrc = os.path.expanduser("~/.netrc")
    if os.path.exists(netrc) and EDL_HOST in open(netrc, encoding="utf-8", errors="ignore").read():
        s.trust_env = True                  # requests reads ~/.netrc for the redirect host
        return s, "netrc"
    return None, ("no Earthdata credentials — set EARTHDATA_TOKEN or add a "
                  f"~/.netrc entry for {EDL_HOST}")


def download(url, dest, session=None, chunk=1 << 20):
    """Stream one ASF DAAC asset to `dest`. Raises RuntimeError with actionable
    guidance if not authenticated or if EDL bounces the request."""
    if session is None:
        session, how = edl_session()
        if session is None:
            raise RuntimeError(how)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with session.get(url, stream=True, timeout=180, allow_redirects=True) as r:
        if r.status_code in (401, 403) or "oauth" in r.url or EDL_HOST in r.url:
            raise RuntimeError(f"Earthdata Login rejected/challenged the download "
                               f"(HTTP {r.status_code}). Check your token/.netrc.")
        r.raise_for_status()
        with open(dest, "wb") as f:
            for c in r.iter_content(chunk):
                f.write(c)
    return dest


# --------------------------------------------------------------------------
# mask math (rasterio; QGIS ships GDAL, the venv ships rasterio)
# --------------------------------------------------------------------------
# OPERA RTC-S1 `mask` layer (uint8): 0 = valid (no layover/shadow); nonzero flags
# layover / shadow (and combinations); the file's own nodata marks fill. Exclude
# every nonzero/fill pixel from change statistics. Confirm exact bit codes against
# the current OPERA RTC-S1 Product Specification before production use.
def exclude_mask(mask_tif, keep_value=0):
    """Boolean ndarray, True where a pixel should be EXCLUDED from change stats
    (layover/shadow/fill). `keep_value` is the mask code that means 'good'."""
    import rasterio
    import numpy as np
    with rasterio.open(mask_tif) as ds:
        a = ds.read(1)
        nod = ds.nodata
    excl = (a != keep_value)
    if nod is not None:
        excl |= (a == nod)
    return np.asarray(excl, dtype=bool)


# --------------------------------------------------------------------------
def _parse_dt(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognized datetime: {s!r}")


def main():
    ap = argparse.ArgumentParser(description="OPERA S1 discovery / mask ingest (Alaska SAR plugin)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--datetime", dest="when", type=_parse_dt, required=True,
                    help="event time UTC, e.g. 2023-09-13T09:30")
    ap.add_argument("--pre-days", type=int, default=12)
    ap.add_argument("--post-days", type=int, default=12)
    ap.add_argument("--radius-km", type=float, default=8.0)
    ap.add_argument("--download", metavar="DIR", default=None,
                    help="also download the nearest pre & post _mask.tif here (needs Earthdata Login)")
    a = ap.parse_args()

    print(f"OPERA RTC-S1 over ({a.lat:.3f}, {a.lon:.3f}) "
          f"±{a.pre_days}/{a.post_days} d around {a.when:%Y-%m-%d %H:%M} UTC\n")
    gr = search_rtc_s1(a.lat, a.lon, a.when, a.pre_days, a.post_days, a.radius_km)
    if not gr:
        print("  no RTC-S1 granules found in window.")
    for g in gr:
        m = "mask✓" if g["assets"].get("mask") else "mask✗"
        print(f"  [{g['side'] or '?':4}] {g['date']}  T{g['track']}  gap {g['gap_days']:+} d  {m}")
        print(f"        {g['assets'].get('mask','(no mask asset)')}")

    dh = search_dist_hls(a.lat, a.lon, a.when, 0, a.post_days, a.radius_km)
    print(f"\nOPERA DIST-ALERT-HLS (optical, secondary): {len(dh)} granule(s) in window "
          f"(cloud/snow/night limited over Alaska).")

    if a.download:
        for side in ("pre", "post"):
            g = nearest_mask_granule(gr, side)
            if not g:
                print(f"\n[{side}] no mask granule to download.")
                continue
            dest = os.path.join(a.download, os.path.basename(g["assets"]["mask"]))
            try:
                download(g["assets"]["mask"], dest)
                excl = exclude_mask(dest)
                pct = 100.0 * excl.mean()
                print(f"\n[{side}] {dest}\n   layover/shadow/fill excluded: {pct:.1f}% of pixels")
            except Exception as ex:
                print(f"\n[{side}] download/mask failed: {ex}")


if __name__ == "__main__":
    main()
