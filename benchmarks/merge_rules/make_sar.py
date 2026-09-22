"""Step 1: headless replica of SarTab._cd_compute, per event and orbit direction.

Recipe = the SAR tab's defaults: VV, 20 m render (px = round(2r*1000/20)), all
four detectors, Lee 5x5 per scene, k=7 window, radiometric normalisation of
log-ratio and brightness-z, 8 px blob sieve at each detector's significance
cut. Scene picking = _cd_pick/_cd_post: the after-scene must image the event
point and have >= 3 same-track before-dates imaging it; ordered by (after date,
staleness of the 3rd before-date, gap); up to MT_MAX_PRE = 6 before-scenes,
one per day, nearest first. Window: 60 days before, 30 after (the tab
defaults to 30/30, too short for the texture detector's 3 before-dates on
a 12-day repeat). AOI radius 10 km: Hubbard's scar reaches 8.3 km from its
event point, and an 8 km AOI clips it.

Writes <work>/sar_<event>_<direction>.npz; downloads are cached in
<work>/cache. Needs network (Planetary Computer STAC + data API).
"""
import datetime as dt, json, math, os, sys, urllib.request
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config                                                   # noqa: E402
sys.path.insert(0, os.path.join(config.REPO, "qgis_plugin"))
from landslide_groundtruth import sar_change                    # noqa: E402

CACHE = config.CACHE
os.makedirs(CACHE, exist_ok=True)
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
DATA = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
RADIUS_KM, RES, K, NEED, MT_MAX_PRE = 10.0, 20, 7, 3, 6
PRE_DAYS, POST_DAYS = 60, 30
SIG = {"logratio": ("abs", 3.0), "tsint": ("abs", 3.0),
       "intcorr": ("gt", 0.3), "mtcorr": ("gt", 0.8)}


def aoi(lat, lon, r=RADIUS_KM):
    dlat = r / 111.32
    dlon = r / (111.32 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def search(lat, lon, t0, t1):
    body = {"collections": ["sentinel-1-rtc"],
            "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{t0:%Y-%m-%dT%H:%M:%SZ}/{t1:%Y-%m-%dT%H:%M:%SZ}",
            "limit": 250}
    out, url, data = [], STAC, json.dumps(body).encode()
    while url:
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        page = json.load(urllib.request.urlopen(req, timeout=120))
        out += page["features"]
        nxt = [l for l in page.get("links", []) if l.get("rel") == "next"]
        if not nxt:
            break
        url = nxt[0]["href"]
        data = json.dumps(nxt[0].get("body", {})).encode() if nxt[0].get("body") else None
    return out


def ring(geom):
    c = geom["coordinates"]
    return c[0][0] if geom["type"] == "MultiPolygon" else c[0]


def covers(geom, lon, lat):
    pts, inside = ring(geom), False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def cand(f, event):
    t = dt.datetime.fromisoformat(f["properties"]["datetime"].replace("Z", "+00:00")).replace(tzinfo=None)
    return dict(id=f["id"], t=t, date=t.date().isoformat(),
                gap=abs((t - event).total_seconds()) / 86400.0,
                orbit=f["properties"].get("sat:orbit_state"),
                track=f["properties"].get("sat:relative_orbit"),
                geometry=f["geometry"])


def one_per_day(cs):
    seen, out = set(), []
    for c in sorted(cs, key=lambda c: c["gap"]):
        if c["date"] not in seen:
            seen.add(c["date"])
            out.append(c)
    return out


def pick(items, event, lat, lon, direction):
    """(_cd_post + _cd_pick) restricted to one orbit direction."""
    posts = [c for c in items if c["orbit"] == direction and c["t"] > event
             and covers(c["geometry"], lon, lat)]
    by_track = {}
    for c in items:
        if c["t"] < event and covers(c["geometry"], lon, lat):
            by_track.setdefault(c["track"], []).append(c)
    stack = lambda c: one_per_day(by_track.get(c["track"], []))
    usable = [c for c in posts if len(stack(c)) >= NEED]
    if not usable:
        return None, None
    post = min(usable, key=lambda c: (c["date"], stack(c)[NEED - 1]["gap"], c["gap"]))
    return post, stack(post)[:MT_MAX_PRE]


def fetch(item_id, box, px):
    path = os.path.join(CACHE, f"{item_id}_{px}_{box[0]:.4f}_{box[1]:.4f}.tif")
    if not os.path.exists(path):
        url = (f"{DATA}/{box[0]:.6f},{box[1]:.6f},{box[2]:.6f},{box[3]:.6f}.tif"
               f"?collection=sentinel-1-rtc&item={item_id}&assets=vv"
               f"&nodata=-32768&width={px}&height={px}")
        data = urllib.request.urlopen(url, timeout=300).read()
        with open(path, "wb") as fh:
            fh.write(data)
    return path


def compute(paths_by_role, pre_roles):
    """SarTab._cd_compute, products = all four."""
    arrs, valids, gt, proj, shape = {}, {}, None, None, None
    for role, path in paths_by_role.items():
        arr, valid, g, p = sar_change.read_band(path)
        if shape is None:
            shape, gt, proj = arr.shape, g, p
        elif arr.shape != shape:
            raise ValueError("scene grids differ")
        arr = sar_change.lee_filter(arr, valid, 5)
        arrs[role], valids[role] = arr, valid
    pres = [arrs[r] for r in pre_roles]
    pvs = [valids[r] for r in pre_roles]
    post, vpost = arrs["post"], valids["post"]
    outs = {"logratio": sar_change.log_ratio(pres[0], post, pvs[0] & vpost, K)}
    rho_ref = sar_change.intensity_correlation(pres[1], pres[0], pvs[1] & pvs[0], K)
    rho_co = sar_change.intensity_correlation(pres[0], post, pvs[0] & vpost, K)
    outs["intcorr"] = sar_change.corr_norm_diff(rho_ref, rho_co)
    outs["tsint"] = sar_change.intensity_zscore(pres, post, pvs, vpost, K)
    refs, cos = [], []
    for i in range(len(pres)):
        for j in range(i + 1, len(pres)):
            refs.append(sar_change.intensity_correlation(pres[i], pres[j], pvs[i] & pvs[j], K))
        cos.append(sar_change.intensity_correlation(pres[i], post, pvs[i] & vpost, K))
    outs["mtcorr"] = sar_change.multi_temporal_possibility(refs, cos)
    for mkey in list(outs):
        out = outs[mkey]
        if mkey in ("logratio", "tsint"):                      # radiometric normalise
            f0 = out[np.isfinite(out)]
            if f0.size:
                out = out - float(np.median(f0))
        kind, thr = SIG[mkey]                                   # 8 px sieve
        sig = ((np.abs(out) > thr) if kind == "abs" else (out > thr)) & np.isfinite(out)
        out = sar_change.sieve_small_blobs(out, sig, 8, fill=0.5 if mkey == "mtcorr" else 0.0)
        outs[mkey] = out.astype(np.float32)
    return outs, gt, proj


def run_event(name, lat, lon, event):
    box = aoi(lat, lon)
    px = int(min(2048, max(128, round(RADIUS_KM * 2 * 1000 / RES))))
    items = [cand(f, event) for f in search(
        lat, lon, event - dt.timedelta(days=PRE_DAYS), event + dt.timedelta(days=POST_DAYS))]
    report = {}
    for direction in ("ascending", "descending"):
        post, pres = pick(items, event, lat, lon, direction)
        if post is None:
            report[direction] = "no usable after-scene (needs >= 3 same-track before-dates)"
            continue
        roles = {"post": fetch(post["id"], box, px)}
        pre_roles = []
        for i, c in enumerate(pres):
            roles[f"pre{i}"] = fetch(c["id"], box, px)
            pre_roles.append(f"pre{i}")
        outs, gt, proj = compute(roles, pre_roles)
        np.savez_compressed(
            os.path.join(config.WORK, f"sar_{name}_{direction}.npz"),
            **outs, gt=np.array(gt), proj=np.array(proj),
            meta=np.array(json.dumps(dict(
                direction=direction, track=post["track"], post=post["date"],
                post_id=post["id"], pres=[c["date"] for c in pres],
                footprint=post["geometry"], lat=lat, lon=lon, px=px,
                event=event.isoformat()))))
        report[direction] = (f"t{post['track']} post {post['date']} pres "
                             f"{','.join(c['date'] for c in pres)}")
    return report


if __name__ == "__main__":
    EVENTS = json.load(open(config.EVENTS, encoding="utf-8"))
    for ev in EVENTS:
        if len(sys.argv) > 1 and ev["name"] not in sys.argv[1:]:
            continue
        t = dt.datetime.fromisoformat(ev["datetime"])
        rep = run_event(ev["name"], ev["lat"], ev["lon"], t)
        for d, r in rep.items():
            print(f"{ev['name']:8s} {d:10s} {r}", flush=True)
