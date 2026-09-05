"""Glacier outlines -> a mask on the fusion grid.

Why outlines rather than terrain
--------------------------------
The intuition that "glaciers are flat, landslides are steep, so drop the flat
pixels" fails in Alaska in a specific way: an ice-covered basin's most confusing
members are the ones a slope rule keeps. Icefalls, hanging glaciers and serac
zones sit at 25-45 degrees, and a serac collapse produces optical darkening and a
backscatter increase that at 20 m resolution is not distinguishable from a small
rock avalanche. Meanwhile the debris-covered tongues that change appearance
wholesale between any two dates — supraglacial pond drainage, ice-cliff
backwasting, moraine migration — are flat, so a slope rule removes them, but only
at the price of removing every landslide deposit that ran out onto a valley floor.

Published outlines cut that knot: they identify ice because it IS ice, not
because of its slope, and they leave steep non-glacial rock alone.

Two caveats the tab surfaces to the user rather than hiding:

  * outlines are a snapshot. RGI's source imagery predates the present by years
    to decades, and Alaska termini have retreated substantially since — so the
    mask over-covers at the snout. fusion_core.glacier_weight feathers the edge
    for this reason, and the weight is a downweight, never a delete.
  * a rock avalanche onto a glacier is a real and common Alaska event. Deleting
    ice pixels would delete exactly those. Hence downweight-don't-erase.

GDAL/OGR only — QGIS ships no geopandas. Reprojection and the spatial filter are
both delegated to gdal.VectorTranslate, since OGR's rasterizer does NOT reproject
on the fly and would otherwise silently burn nothing when the outlines and the
fusion grid disagree on CRS (they will: RGI ships in EPSG:4326, the fusion grid
may be UTM).
"""
import os
import re

import numpy as np

from . import fusion_cloud
from . import fusion_grid

# ---------------------------------------------------------------------------
# RGI 7.0 acquisition from NSIDC
#
# The canonical host is Earthdata-gated: EVERY path under the data root 302s to
# urs.earthdata.nasa.gov, including paths that do not exist, so an
# unauthenticated probe cannot tell a real filename from a typo and the
# directory layout cannot be verified from outside. CMR knows the collection
# (C2768953486-NSIDCV0) but publishes ZERO granules for it, so there is no
# granule API to enumerate either.
#
# Rather than hardcode a guessed subdirectory, the file is DISCOVERED: log in,
# read the Apache index at the data root, and walk it for the known filename.
# That adapts if NSIDC reorganises, and it fails loudly instead of silently
# fetching the wrong thing.
#
# Credentials come from the plugin's existing NASA Earthdata Login panel, which
# already writes ~/.netrc (machine urs.earthdata.nasa.gov) and the EARTHDATA_*
# environment variables — see LandslideDock._earthdata_save_netrc.
# ---------------------------------------------------------------------------
RGI_ROOT = "https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0770_rgi_v7/"
RGI_LANDING = "https://nsidc.org/data/nsidc-0770/versions/7"
EDL_HOST = "urs.earthdata.nasa.gov"

# The C ("glacier complex") product: the G product dissolved along internal ice
# divides. Same total ice area, far fewer polygons, and no slivers or gaps at
# divide lines — which is what a binary "is this pixel ice" downweight wants.
RGI_C01_NAME = "RGI2000-v7.0-C-01_alaska.zip"

# Verified 2026-09-04 by walking the authenticated index. Tried first as a fast
# path; if NSIDC reorganises, find_rgi_url() re-discovers it rather than failing.
RGI_C01_URL = RGI_ROOT + "regional_files/RGI2000-v7.0-C/" + RGI_C01_NAME

RGI_CITATION = (
    "RGI 7.0 Consortium, 2023. Randolph Glacier Inventory — A Dataset of Global "
    "Glacier Outlines, Version 7.0. Boulder, Colorado USA. NSIDC. "
    "doi:10.5067/f6jmovy5navz. Licence: CC BY 4.0.")

# Region 01 outlines derive from 1999–2010 imagery, so Alaska termini have
# retreated well inside them. Surfaced in the UI, not buried here.
RGI_VINTAGE_NOTE = (
    "RGI 7.0 region 01 outlines come from 1999–2010 imagery; Alaska termini have "
    "retreated since, so the mask over-covers at the snout (the edge is feathered "
    "for exactly this reason).")


def _edl_opener():
    """A urllib opener that can follow the Earthdata Login redirect dance.

    EDL bounces daacdata -> urs.earthdata.nasa.gov -> back with a code, and the
    session then rides on a cookie, so both a cookie jar and credentials that
    SURVIVE the redirect are required. urllib strips Authorization across hosts
    by default (correctly — you must never replay credentials to an arbitrary
    redirect target), so the handler below re-attaches them for the URS host and
    only the URS host."""
    import base64
    import http.cookiejar
    import netrc
    import urllib.parse
    import urllib.request

    user = os.environ.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD")
    if not (user and password):
        try:
            auth = netrc.netrc().authenticators(EDL_HOST)
        except Exception:                            # noqa: BLE001
            auth = None
        if auth:
            user, _acct, password = auth
    if not (user and password):
        raise PermissionError(
            "No NASA Earthdata credentials found. Open the 'NASA Earthdata "
            "Login' box in the plugin's Environment header, enter your username "
            "and password and press Check — that writes ~/.netrc, which this "
            f"download reads. A free account: https://{EDL_HOST}/")

    token = base64.b64encode(f"{user}:{password}".encode()).decode()

    class _AuthToURS(urllib.request.HTTPRedirectHandler):
        """Attach Basic credentials on the hop to URS, and only to URS.

        HTTPBasicAuthHandler is no use here: it reacts to a 401 challenge, and
        NSIDC never issues one — it answers with a 302 to the URS authorize
        endpoint, which then serves a 200 HTML LOGIN FORM to an unauthenticated
        client. That looks like success to any code checking the status, so the
        credentials have to be attached up front on the redirect itself.

        The host test is exact equality, never a suffix match: credentials must
        go to urs.earthdata.nasa.gov and nowhere else, however a redirect chain
        is manipulated."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is None:
                return None
            host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
            if host == EDL_HOST:
                new.add_unredirected_header("Authorization", "Basic " + token)
            return new

    return urllib.request.build_opener(
        _AuthToURS(),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _index_links(html, base):
    """Absolute hrefs from an Apache autoindex page."""
    import urllib.parse
    out = []
    for m in re.finditer(r'href="([^"?][^"]*)"', html, re.I):
        href = m.group(1)
        if href.startswith("/") or href in ("../",):
            continue
        out.append(urllib.parse.urljoin(base, href))
    return out


def find_rgi_url(name=RGI_C01_NAME, root=RGI_ROOT, opener=None, log=None,
                 max_depth=3, timeout=120):
    """Locate `name` under the NSIDC data root by reading authenticated indexes.

    Breadth-first, so the common case (a shallow layout) costs one or two page
    reads. Returns the absolute URL, or raises FileNotFoundError naming the
    landing page so the user can fetch it by hand."""
    opener = opener or _edl_opener()
    seen, queue = set(), [(root, 0)]
    while queue:
        url, depth = queue.pop(0)
        if url in seen or depth > max_depth:
            continue
        seen.add(url)
        try:
            with opener.open(url, timeout=timeout) as r:
                if getattr(r, "status", 200) != 200:
                    continue
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "html" not in ctype:
                    continue
                html = r.read().decode("utf-8", "replace")
                final = r.geturl() or ""
            if EDL_HOST in final or "<title>Earthdata Login" in html:
                raise PermissionError(
                    "NASA Earthdata rejected the stored credentials (the login "
                    "page came back instead of the file listing). Re-enter your "
                    "username and password in the plugin's 'NASA Earthdata "
                    "Login' box, and make sure you have authorized the NSIDC "
                    f"application once at https://{EDL_HOST}/profile .")
        except Exception as e:                       # noqa: BLE001
            if log:
                log(f"    index {url}: {type(e).__name__}: {e}")
            continue
        links = _index_links(html, url)
        for href in links:
            if href.rsplit("/", 1)[-1] == name:
                return href
        for href in links:                           # then descend
            if href.endswith("/") and href.startswith(root):
                queue.append((href, depth + 1))
    raise FileNotFoundError(
        f"Could not find {name} under {root}. Download it by hand from "
        f"{RGI_LANDING} and point the Outlines field at the .zip.")


def ensure_rgi_c01(cache_dir, log=None, timeout=600):
    """Path to a cached RGI 7.0 C-01 (Alaska) zip, downloading it if absent.

    ~40-80 MB, fetched once and reused. Returns the local path."""
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, RGI_C01_NAME)
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        if log:
            log(f"    using cached {dest}")
        return dest

    opener = _edl_opener()
    if log:
        log("    signing in to NASA Earthdata…")
    tmp = dest + ".part"
    last = None
    for url in (RGI_C01_URL, None):
        if url is None:                              # fast path missed: discover
            if log:
                log("    known path did not serve the file; searching the index…")
            try:
                url = find_rgi_url(opener=opener, log=log)
            except Exception as e:                   # noqa: BLE001
                raise last or e
        if log:
            log(f"    downloading {url}")
        try:
            with opener.open(url, timeout=timeout) as r:
                if getattr(r, "status", 200) != 200:
                    raise IOError(f"download failed with status {r.status}")
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "html" in ctype:                  # an EDL login page, not data
                    raise PermissionError(
                        "NASA Earthdata returned a login page instead of the "
                        "dataset. Re-enter your credentials in the plugin's "
                        "'NASA Earthdata Login' box.")
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
        except PermissionError:
            raise
        except Exception as e:                       # noqa: BLE001
            last = e
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        break
    if not os.path.exists(tmp):
        raise last or IOError("the RGI download produced no file")
    size = os.path.getsize(tmp)
    if size < 1_000_000:
        os.remove(tmp)
        raise IOError(
            f"the download returned only {size} bytes — too small to be the "
            "dataset. Check the Earthdata credentials in the plugin's "
            "Environment header.")
    os.replace(tmp, dest)
    if log:
        log(f"    saved {dest} ({size / 1e6:.0f} MB)")
        log(f"    {RGI_CITATION}")
    return dest


def resolve_vector_path(path):
    """Accept a plain vector path, a .zip, or an explicit /vsizip/ path.

    RGI ships as a zipped shapefile and there is no GeoPackage distribution, so
    the zip case is the normal one; GDAL reads it in place via /vsizip/ with no
    unpacking."""
    p = str(path).strip()
    if not p:
        raise ValueError("no glacier outline path given")
    if p.startswith("/vsi"):
        return p
    if p.lower().endswith(".zip"):
        return "/vsizip/" + os.path.abspath(p)
    return p


def rasterize_outlines(path, gt, shape, proj, layer=None, where=None):
    """Boolean mask, True inside a glacier polygon, on the fusion grid.

    `path` is anything OGR can open: a shapefile, a GeoPackage, or a zipped
    shapefile via GDAL's virtual filesystem, e.g.
    ``/vsizip/C:/data/RGI2000-v7.0-C-01_alaska.zip/RGI2000-v7.0-C-01_alaska.shp``

    Returns (mask, info). Raises IOError/ValueError with a message meant to be
    shown to the user, never a bare GDAL error code."""
    from osgeo import gdal, ogr

    h, w = shape
    minx, miny, maxx, maxy = fusion_grid.grid_bbox(gt, shape)
    lon0, lat0, lon1, lat1 = fusion_cloud.bbox_4326(gt, shape, proj)

    path = resolve_vector_path(path)
    src = ogr.Open(path)
    if src is None:
        raise IOError(f"OGR could not open the glacier outlines at {path}")
    n_layers = src.GetLayerCount()
    lyr = (src.GetLayerByName(layer) if layer else src.GetLayer(0))
    if lyr is None:
        names = [src.GetLayer(i).GetName() for i in range(n_layers)]
        raise ValueError(f"no layer {layer!r} in {path}; found {names}")
    lyr_name = lyr.GetName()
    src = None

    # Reproject + clip to the AOI in one step. spatSRS says the filter box is in
    # lon/lat; dstSRS puts the survivors on the fusion grid's CRS so the burn
    # lands pixel-aligned.
    mem, last = None, None
    for fmt in ("Memory", "MEM"):        # driver name differs across GDAL versions
        try:
            mem = gdal.VectorTranslate(
                "", path, format=fmt, layers=[lyr_name],
                dstSRS=(proj or "EPSG:4326"), reproject=True,
                spatFilter=(lon0, lat0, lon1, lat1), spatSRS="EPSG:4326",
                where=where)
        except Exception as e:                       # noqa: BLE001
            last = e
            continue
        if mem is not None:
            break
    if mem is None:
        raise IOError("could not reproject the glacier outlines onto the fusion "
                      "grid" + (f": {type(last).__name__}: {last}" if last else
                                " (gdal.VectorTranslate returned nothing)"))
    mlyr = mem.GetLayer(0)
    n_feat = mlyr.GetFeatureCount()
    if not n_feat:
        mem = None
        return (np.zeros(shape, dtype=bool),
                {"n_features": 0, "layer": lyr_name,
                 "note": "no glacier polygons intersect this AOI"})

    drv = gdal.GetDriverByName("MEM")
    dst = drv.Create("", int(w), int(h), 1, gdal.GDT_Byte)
    dst.SetGeoTransform(gt)
    dst.SetProjection(proj or "")
    # ALL_TOUCHED: burn every pixel the polygon touches, not only those whose
    # CENTRE it covers. At a 20-30 m fusion pixel a small cirque glacier can be
    # narrower than one pixel and would otherwise vanish from the mask while
    # still being counted as burned in the log. Over-covering by half a pixel is
    # the safe direction here — this is a downweight, not a delete.
    err = gdal.RasterizeLayer(dst, [1], mlyr, burn_values=[1],
                              options=["ALL_TOUCHED=TRUE"])
    arr = dst.GetRasterBand(1).ReadAsArray()
    dst = None
    mem = None
    if err != 0:
        raise IOError("rasterizing the glacier outlines onto the fusion grid "
                      "failed")
    mask = np.asarray(arr).astype(bool)
    pct = 100.0 * mask.mean() if mask.size else 0.0
    note = f"{n_feat} glacier polygon(s), {pct:.1f}% of the AOI"
    if n_feat and not mask.any():
        # polygons survived the spatial filter but burned nothing: they are
        # sub-pixel, or just outside the grid the filter box rounded outward to
        note = (f"{n_feat} glacier polygon(s) intersect the AOI but none covers "
                "a pixel at this resolution — no downweighting applied")
    return mask, {"n_features": int(n_feat), "layer": lyr_name,
                  "pct_aoi": float(pct), "n_burned": int(mask.sum()),
                  "note": note}
