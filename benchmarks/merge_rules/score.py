"""Step 2: fuse every SAR merge variant with the optical pair, exactly as the Fusion
tab's default preset does, and score it against the hand-digitised scar.

Fusion (FusionTab._fuse, preset 'default'): grid = coarser input cropped to the
common extent; warp 'average'; orient; cloud mask (on); detrend (on); rank above
absolute floors (dBright 0.05, dNDSI 0.10, log-ratio 3.0 and each SAR sibling's
own floor); optical pair averaged, the four SAR detectors MAX-pooled; fuse mean,
smooth 5, SAR-only weight; 0.05 km2 sieve at 0.20; candidates by peak.

Metrics, per event (a pixel the map does not score counts as NOT flagged):
  bg50   background flagged at 50% recall (the 6-event benchmark's metric)
  rank   rank of the first candidate blob touching the scar (by peak)
  area   share of the scar at or above the 0.20 display threshold
  false  share of NON-scar ground at or above 0.20 — the noise the map shows

Variants: none (no SAR), asc, desc, merged (stronger-wins, no masks),
merged+masks (stronger-wins with shadow|layover — the shipped default),
fill-auto / fill-asc / fill-desc (fill-only merge).
Reads <work>/sar_*.npz from make_sar.py; writes <work>/results_<cloud|nocloud>.json
and prints per-event rows plus worst/mean tables. Network: DEM + cloud mask.
"""
import json, math, os, sys, tempfile
import numpy as np
from osgeo import gdal, ogr, osr
gdal.UseExceptions(); ogr.UseExceptions()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config                                                   # noqa: E402
sys.path.insert(0, os.path.join(config.REPO, "qgis_plugin"))
from landslide_groundtruth import (fusion_core, fusion_grid, fusion_cloud,   # noqa: E402
                                   sar_change, layover_dim)

KINDS = ("logratio", "intcorr", "mtcorr", "tsint")
SIEVE_T, MIN_KM2 = 0.20, 0.05


def load_geom(name, d):
    p = os.path.join(config.WORK, f"sar_{name}_{d}.npz")
    if not os.path.exists(p):
        return None
    z = np.load(p, allow_pickle=False)
    return dict(maps={k: z[k] for k in KINDS}, gt=tuple(z["gt"].tolist()),
                proj=str(z["proj"]), meta=json.loads(str(z["meta"])))


def terrain_mask(g, dem):
    gt, shape, m = g["gt"], g["maps"]["logratio"].shape, g["meta"]
    lat_c = gt[3] + gt[5] * shape[0] / 2.0
    lon_c = gt[0] + gt[1] * shape[1] / 2.0
    inc = layover_dim.iw_incidence_deg(m["footprint"], lon_c, lat_c, m["direction"])
    inc = inc if inc is not None else layover_dim.IW_INCIDENCE_DEG
    sh, _ = layover_dim.radar_shadow(dem, gt, m["direction"], incidence_deg=inc, lat_hint=lat_c)
    lo, _ = layover_dim.radar_layover(dem, gt, m["direction"], incidence_deg=inc, lat_hint=lat_c)
    return sh | lo, inc


def variants(geoms, masks):
    """{variant: {kind: array}} on the shared SAR grid, plus notes."""
    a, d = geoms.get("ascending"), geoms.get("descending")
    out, notes = {"none": None}, {}
    if a:
        out["asc"] = a["maps"]
    if d:
        out["desc"] = d["maps"]
    if a and d:
        mA, mD = masks["ascending"], masks["descending"]
        out["merged"] = {k: sar_change.merge_geometries(
            [a["maps"][k], d["maps"][k]], k, _thr(k))[0] for k in KINDS}
        out["merged+masks"] = {k: sar_change.merge_geometries(
            [a["maps"][k], d["maps"][k]], k, _thr(k), masks=[mA, mD])[0] for k in KINDS}
        p, pm = sar_change.choose_primary(
            [a["maps"]["logratio"], d["maps"]["logratio"]], "logratio", [mA, mD])
        notes["auto_primary"] = ("asc", "desc")[p]
        notes["auto_reason"] = pm["reason"]
        notes["noise"] = pm["noise"]
        for tag, idx in (("fill-auto", p), ("fill-asc", 0), ("fill-desc", 1)):
            out[tag] = {k: sar_change.fill_geometries(
                [a["maps"][k], d["maps"][k]], idx, [mA, mD])[0] for k in KINDS}
    return out, notes


def _thr(k):
    return {"logratio": 3.0, "tsint": 3.0, "intcorr": 0.3, "mtcorr": 0.8}[k]


def scar_mask(gpkg, gt, shape, proj):
    ds = ogr.Open(gpkg)
    lyr = ds.GetLayerByName("landslide_scars")
    src = lyr.GetSpatialRef(); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromWkt(proj)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    mem = ogr.GetDriverByName("Memory").CreateDataSource("s")
    ml = mem.CreateLayer("s", dst, ogr.wkbMultiPolygon)
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone(); g.Transform(tr)
        nf = ogr.Feature(ml.GetLayerDefn()); nf.SetGeometry(g); ml.CreateFeature(nf)
    r = gdal.GetDriverByName("MEM").Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
    r.SetGeoTransform(gt); r.SetProjection(proj)
    gdal.RasterizeLayer(r, [1], ml, burn_values=[1])
    return r.GetRasterBand(1).ReadAsArray().astype(bool)


def rank_layer(path, kind, gt, shape, proj, floor, cloud=None):
    raw = fusion_grid.warp_to_reference(path, gt, shape, proj, resample="average")
    ev = fusion_core.orient_evidence(raw, kind)
    if cloud is not None:
        ev = np.where(cloud, np.nan, ev).astype(np.float32)
    admit = ev
    ev, _o = fusion_core.detrend_median(ev)
    r, _i = fusion_core.robust_rank(ev, kind, floor=floor, admit_values=admit)
    return r


def score_map(sc, scar, domain, gt, shape):
    s = np.where(np.isfinite(sc), sc, 0.0)
    sc_px, bg_px = scar & domain, (~scar) & domain
    t50 = float(np.median(s[sc_px]))
    bg50 = float(np.mean(s[bg_px] >= t50)) if t50 > 0 else 1.0
    area = float(np.mean(s[sc_px] >= SIEVE_T))
    # the false area the MAP shows: non-scar ground at or above the display cut
    bg_flag = float(np.mean(s[bg_px] >= SIEVE_T))
    mask = np.isfinite(sc) & (sc >= SIEVE_T)
    ys, xs, roots = sar_change.label_blobs(mask)
    dxm, dym = layover_dim.metric_pixel_size(gt, shape[0])
    min_px = max(1, int(round(MIN_KM2 * 1e6 / (dxm * dym))))
    rank, nb = None, 0
    if ys.size:
        order = np.argsort(roots, kind="mergesort")
        r_s = roots[order]
        starts = np.flatnonzero(np.r_[True, r_s[1:] != r_s[:-1]])
        ends = np.r_[starts[1:], r_s.size]
        blobs = []
        for a_, b_ in zip(starts.tolist(), ends.tolist()):
            sel = order[a_:b_]
            if sel.size < min_px:
                continue
            blobs.append((-float(sc[ys[sel], xs[sel]].max()), -sel.size,
                          bool(scar[ys[sel], xs[sel]].any())))
        blobs.sort()
        nb = len(blobs)
        for i, bl in enumerate(blobs, 1):
            if bl[2]:
                rank = i
                break
    return dict(bg50=bg50, rank=rank, area=area, n_blobs=nb, bg_flag=bg_flag)


def run(ev, use_cloud=True):
    geoms = {d: load_geom(ev["name"], d) for d in ("ascending", "descending")}
    geoms = {d: g for d, g in geoms.items() if g}
    if not geoms:
        return None, {"error": "no SAR for either direction"}
    g0 = next(iter(geoms.values()))
    gt_s, proj_s = g0["gt"], g0["proj"]
    shp = g0["maps"]["logratio"].shape
    minx, maxy = gt_s[0], gt_s[3]
    maxx, miny = gt_s[0] + gt_s[1] * shp[1], gt_s[3] + gt_s[5] * shp[0]
    dem = layover_dim.fetch_dem_on_grid(minx, miny, maxx, maxy, shp[1], shp[0])
    masks, incs = {}, {}
    for d, g in geoms.items():
        masks[d], incs[d] = terrain_mask(g, dem)
    vs, notes = variants(geoms, masks)
    notes["incidence"] = incs
    notes["blind"] = {d: float(m.mean()) for d, m in masks.items()}
    if len(masks) == 2:
        notes["blind_both"] = float((masks["ascending"] & masks["descending"]).mean())
    tmp = tempfile.mkdtemp(prefix=f"bench_{ev['name']}_")
    paths = {}
    for vname, maps in vs.items():
        if maps is None:
            continue
        paths[vname] = {}
        for k in KINDS:
            p = os.path.join(tmp, f"{vname}_{k}.tif")
            sar_change.write_gtiff(p, maps[k], gt_s, proj_s)
            paths[vname][k] = p
    ref_sar = next(iter(paths.values()))["logratio"]
    opt = os.path.join(config.OPTICAL_DIR, ev["optical"])
    dndsi = opt.replace("_dbright_", "_dndsi_")
    _r, gt, shape, proj, _gi = fusion_grid.pick_reference([opt, ref_sar])
    gt, shape, _x = fusion_grid.crop_to_common(gt, shape, proj, [opt, ref_sar])
    cloud = None
    if use_cloud:
        try:
            meta, _mp = fusion_cloud.find_metadata(opt)
            if meta:
                cloud, cnote = fusion_cloud.cloud_mask_on_grid(
                    meta, gt, shape, proj, frac_thresh=0.5, mask_dark=False, log=None)
                notes["cloud"] = cnote
        except Exception as e:                           # noqa: BLE001
            notes["cloud"] = f"skipped: {type(e).__name__}: {e}"
    o_rank = rank_layer(opt, "dbright", gt, shape, proj, 0.05, cloud)
    sib = rank_layer(dndsi, "dndsi", gt, shape, proj, fusion_core.DEFAULT_FLOORS["dndsi"], cloud)
    o_rank = fusion_core.combine_optical(o_rank, sib)
    scar = scar_mask(os.path.join(config.SCAR_DIR, ev["scar_folder"], "Total Area.gpkg"),
                     gt, shape, proj)
    notes["scar_px"] = int(scar.sum())
    s_ranks = {}
    for vname, ps in paths.items():
        s = None
        for k in KINDS:
            floor = 3.0 if k == "logratio" else fusion_core.DEFAULT_FLOORS.get(k)
            r = rank_layer(ps[k], k, gt, shape, proj, floor)
            s = r if s is None else fusion_core.combine_sar(s, r)
        s_ranks[vname] = s
    base = s_ranks.get("merged", next(iter(s_ranks.values())))
    domain = np.isfinite(o_rank) | np.isfinite(base)
    dxm, dym = layover_dim.metric_pixel_size(gt, shape[0])
    min_px = max(1, int(round(MIN_KM2 * 1e6 / (dxm * dym))))
    res = {}
    for vname in ["none"] + list(s_ranks):
        s_rank = s_ranks.get(vname, np.full(shape, np.nan, np.float32))
        bands, _m = fusion_core.fuse(o_rank, s_rank, mode="mean", allow_sar_only=True,
                                     sar_only_weight=fusion_core.SAR_ONLY_WEIGHT,
                                     smooth_k=5, optical_n=2)
        sc = bands["optical_only"] if vname == "none" else bands["score"]
        sig = np.isfinite(sc) & (sc >= SIEVE_T)
        sc = sar_change.sieve_small_blobs(sc, sig, min_px, fill=0.0)
        res[vname] = score_map(sc, scar, domain, gt, shape)
        res[vname]["scar_unscored"] = float(np.mean(~np.isfinite(sc[scar & domain])))
    return res, notes


def summarize(out):
    """Worst case and mean per variant. An event with only one usable pass
    (Knik: no ascending) scores every SAR rule as that pass."""
    evs = [e for e in out if out[e].get("res")]
    variants_ = ["none", "asc", "desc", "merged", "merged+masks",
                 "fill-auto", "fill-asc", "fill-desc"]

    def get(e, v, k):
        res = out[e]["res"]
        if v not in res and v != "none":
            v = "desc" if "desc" in res else "asc"
        return res[v][k] if v in res else None

    for metric, fmt, worst in (("bg50", "{:6.2%}", max), ("bg_flag", "{:6.1%}", max),
                               ("area", "{:6.1%}", min), ("rank", "{}", max)):
        print(f"\n{metric}" + ("  (higher is better)" if metric == "area" else ""))
        print(f"{'':13s}" + "".join(f"{e:>9s}" for e in evs) + f"{'worst':>9s}{'mean':>9s}")
        for v in variants_:
            vals = [get(e, v, metric) for e in evs]
            if metric == "rank":
                vv = [x if x is not None else 999 for x in vals]
                cells = "".join(f"{(str(x) if x is not None else 'miss'):>9s}" for x in vals)
                print(f"{v:13s}{cells}{max(vv):>9d}{sum(vv) / len(vv):>9.1f}")
            else:
                cells = "".join(f"{fmt.format(x):>9s}" for x in vals)
                print(f"{v:13s}{cells}{fmt.format(worst(vals)):>9s}"
                      f"{fmt.format(sum(vals) / len(vals)):>9s}")


if __name__ == "__main__":
    use_cloud = "--no-cloud" not in sys.argv
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = {}
    for ev in json.load(open(config.EVENTS, encoding="utf-8")):
        if names and ev["name"] not in names:
            continue
        res, notes = run(ev, use_cloud)
        out[ev["name"]] = dict(res=res, notes=notes)
        print(f"== {ev['name']}  {json.dumps(notes, default=str)[:400]}", flush=True)
        if res:
            for v, r in res.items():
                rk = r["rank"] if r["rank"] is not None else "miss"
                print(f"   {v:13s} bg50 {r['bg50']:7.2%}  rank {str(rk):>4s}/{r['n_blobs']:<4d}"
                      f" area {r['area']:6.1%}  false-area {r['bg_flag']:6.2%}  scar unscored {r['scar_unscored']:5.1%}", flush=True)
    tag = "cloud" if use_cloud else "nocloud"
    os.makedirs(config.WORK, exist_ok=True)
    json.dump(out, open(os.path.join(config.WORK, f"results_{tag}.json"), "w"),
              indent=1, default=str)
    summarize(out)
