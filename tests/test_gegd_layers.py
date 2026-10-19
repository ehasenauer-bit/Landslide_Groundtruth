"""Maxar stream layers must ask for THE strip they name, and say what they are.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_layers.py

The worst failure a before/after comparison can have is a "valid" layer showing
the wrong date. GEGD renders ONE strip only when each tile request carries
CQL_FILTER=featureId='…'; QGIS's WMTS provider silently drops that parameter and
draws the newest-on-top mosaic instead. So gegd_layers streams through an XYZ
layer whose template goes in via QgsDataSourceUri.setParam("url", …). This
renders those layers against a local tile server and reads what QGIS actually
asked for: every tile must carry the exact filter, with z/x/y filled in, on both
WMTS paths, and the WMS fallback must keep its cql_filter too. It also pins the
names (no _pre_/_post_ token, so 3D never pairs a stream), the custom properties
adopt_layers finds them by, and the vendor-aware rights.

Offline: a throwaway profile and auth DB (Keychain helper off BEFORE the master
password is set), a dummy key, local servers only, and a request preprocessor
that diverts anything bound elsewhere to a closed local port and fails the test.
The HostGuard is aimed at the local stand-in so the dummy key arrives there.
"""
import os, re, secrets, shutil, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _base in ("/Applications/QGIS-LTR.app", "/Applications/QGIS.app"):
    _proj = os.path.join(_base, "Contents", "Resources", "qgis", "proj")
    if os.path.isdir(_proj):
        os.environ.setdefault("PROJ_DATA", _proj)
        os.environ.setdefault("PROJ_LIB", _proj)
        os.environ.setdefault("GDAL_DATA", os.path.join(_base, "Contents", "Resources", "qgis", "gdal"))
        break
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
sys.path.insert(0, PKG)

TMP = tempfile.mkdtemp(prefix="gegd_layers_")
for _d in ("auth", "config", "cache"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")
os.environ["LANDSLIDE_GEGD_CACHE"] = os.path.join(TMP, "cache")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsMapRendererParallelJob, QgsMapSettings, QgsNetworkAccessManager,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsRuleBasedRenderer,
                       QgsSettings)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)       # the APIHeader auth method lives here
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QSize, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QImage

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
    sys.exit(2)
if not QgsCoordinateReferenceSystem("EPSG:3857").isValid():
    print("EPSG:3857 does not resolve (no proj.db: set PROJ_DATA); the renders would ask "
          "for no tiles and pass for the wrong reason")
    sys.exit(2)

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


# ---- offline: anything not bound for this machine is diverted and recorded ----
OFFHOST = []


def _offline(request):
    host = request.url().host().lower()
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        OFFHOST.append(host)
        request.setUrl(QUrl("http://127.0.0.1:9/diverted"))


QgsNetworkAccessManager.setRequestPreprocessor(_offline)

# ---- a local stand-in for pro.gegd.com: tiles, and a WMS for the fallback ----
_img = QImage(256, 256, QImage.Format_ARGB32); _img.fill(QColor(30, 140, 200))
_ba = QByteArray(); _buf = QBuffer(_ba); _buf.open(QIODevice.WriteOnly); _img.save(_buf, "PNG")
PNG = bytes(_ba)
SEEN = []                       # (path, query dict, maxar-api-key header) per request
LK = threading.Lock()


def caps(base):
    return ('<?xml version="1.0" encoding="UTF-8"?><WMS_Capabilities version="1.3.0" '
            'xmlns="http://www.opengis.net/wms" xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<Service><Name>WMS</Name><Title>t</Title><OnlineResource xlink:href="%s"/></Service>'
            '<Capability><Request><GetCapabilities><Format>text/xml</Format><DCPType><HTTP><Get>'
            '<OnlineResource xlink:href="%s"/></Get></HTTP></DCPType></GetCapabilities>'
            '<GetMap><Format>image/png</Format><DCPType><HTTP><Get><OnlineResource xlink:href="%s"/>'
            '</Get></HTTP></DCPType></GetMap></Request><Exception><Format>XML</Format></Exception>'
            '<Layer><Title>root</Title><CRS>EPSG:3857</CRS><Layer queryable="0">'
            '<Name>Maxar:Imagery</Name><Title>L</Title><CRS>EPSG:3857</CRS>'
            '<BoundingBox CRS="EPSG:3857" minx="-20037508" miny="-20037508" maxx="20037508" '
            'maxy="20037508"/></Layer></Layer></Capability></WMS_Capabilities>'
            % (base, base, base)).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k.lower(): v for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        with LK:
            SEEN.append((u.path, q, self.headers.get("maxar-api-key"), unquote(self.path)))
        if q.get("request", [""])[0].lower() == "getcapabilities":
            body, ctype = caps("http://%s%s?" % (self.headers.get("Host"), u.path)), "text/xml"
        else:
            body, ctype = PNG, "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SRV = ThreadingHTTPServer(("127.0.0.1", 0), H)
SRV.daemon_threads = True
threading.Thread(target=SRV.serve_forever, daemon=True).start()
PORT = SRV.server_address[1]
HOST = "http://127.0.0.1:%d" % PORT


def take():
    """The requests received since the last call (after the server threads settle)."""
    lp = QEventLoop(); QTimer.singleShot(300, lp.quit); lp.exec_()
    with LK:
        got = list(SEEN)
        SEEN.clear()
    return got


def render(layer, rect4326):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    dst = QgsCoordinateReferenceSystem("EPSG:3857")
    ms.setDestinationCrs(dst)
    ms.setExtent(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), dst,
                                        QgsProject.instance()).transformBoundingBox(rect4326))
    ms.setOutputSize(QSize(256, 256))
    job = QgsMapRendererParallelJob(ms)
    lp = QEventLoop(); job.finished.connect(lp.quit)
    job.start()
    QTimer.singleShot(30000, lp.quit)
    lp.exec_()
    return job.renderedImage()


# ---- the password store: isolated, helper off BEFORE the master password ----
am = QgsApplication.authManager()
am.setPasswordHelperEnabled(False)
if am.passwordHelperEnabled() or not os.path.realpath(
        am.authenticationDatabasePath()).startswith(os.path.realpath(TMP)):
    print("auth DB not isolated or Keychain helper still on; refusing to continue")
    sys.exit(2)
if not am.setMasterPassword(secrets.token_urlsafe(24), True):
    print("could not set the throwaway master password")
    sys.exit(2)

from landslide_groundtruth import gegd_api as api, gegd_auth, gegd_layers as gl  # noqa: E402
from landslide_groundtruth import gegd_rank as R  # noqa: E402

DUMMY = "dummy-gegd-layers-key-" + secrets.token_hex(12)
check("dummy key stored in the throwaway store (APIHeader auth config)",
      gegd_auth.ensure_ready() and gegd_auth.store_key(DUMMY) and gegd_auth.mode() == "authcfg")
# The local server stands in for https://pro.gegd.com, so the key may reach it here.
gegd_auth.install_host_guard(allowed=("http", "127.0.0.1", PORT))

strips = api.parse_features(open(os.path.join(FIX, "wfs_iliamna.json"), "rb").read())
origin = R._utc("2026-08-08T13:00:00Z")
ranked = R.rank(strips, origin, 60.0265, -153.0559, 5.0)
sp, sq = ranked.star_pre, ranked.star_post
FID = sq.strip.feature_id
# A 1 km box at the epicentre: inside the ★ after-strip's footprint.
LAT, LON = 60.0265, -153.0559
BOX = QgsRectangle(LON - 0.009, LAT - 0.0045, LON + 0.009, LAT + 0.0045)

print("=== the source: XYZ over WMTS GetTile, never the WMTS provider ===")
src = gl.strip_xyz_uri(FID, host=HOST)
check("type=xyz with the template in 'url' and authcfg=gegdpro (no key)",
      "type=xyz" in src and "authcfg=gegdpro" in src and DUMMY not in src)
check("not a WMTS-provider URI (no tileMatrixSet / layers params)",
      "tileMatrixSet=" not in src and "layers=" not in src)
check("the template is percent-encoded once, by setParam (braces arrive as %7B…%7D)",
      "%7Bz%7D" in src and "{z}" not in src and "%257B" not in src)
try:
    gl.strip_xyz_uri("x' OR '1'='1", host=HOST)
    refused = False
except ValueError:
    refused = True
check("an id that could break out of the CQL literal is refused", refused)

print("\n=== every tile asks for THE strip, z/x/y filled, on both WMTS paths ===")
for pk in ("wmts", "wmts_alt"):
    lyr = gl.make_stream_layer(sq, "post", host=HOST, path_key=pk)
    check(f"[{pk}] valid layer, provider 'wms' (the XYZ handler)",
          lyr.isValid() and lyr.providerType() == "wms")
    take()
    img = render(lyr, BOX)
    got = take()
    tiles = [g for g in got if g[0] == api.ENDPOINTS[pk]]
    check(f"[{pk}] tiles requested on {api.ENDPOINTS[pk]} ({len(tiles)} of {len(got)})",
          tiles and len(tiles) == len(got))
    want = f"featureId='{FID}'"
    check(f"[{pk}] every tile carries CQL_FILTER={want}",
          all(g[1].get("cql_filter") == [want] for g in tiles))
    check(f"[{pk}] tilematrix=EPSG:3857:<z>, numeric tilerow/tilecol, no braces left",
          all(re.fullmatch(r"EPSG:3857:\d{1,2}", g[1].get("tilematrix", [""])[0])
              and g[1].get("tilerow", [""])[0].isdigit() and g[1].get("tilecol", [""])[0].isdigit()
              and "{" not in g[3] and "}" not in g[3] for g in tiles))
    check(f"[{pk}] WMTS GetTile of Maxar:Imagery, PNG, EPSG:3857",
          all(g[1].get("service") == ["WMTS"] and g[1].get("request") == ["GetTile"]
              and g[1].get("layer") == [api.LAYER] and g[1].get("format") == ["image/png"]
              and g[1].get("tilematrixset") == ["EPSG:3857"] for g in tiles))
    check(f"[{pk}] the key travels as the header (authcfg), never in the URL",
          all(g[2] == DUMMY and DUMMY not in g[3] for g in tiles))
    check(f"[{pk}] the tiles were drawn", img.pixelColor(128, 128).alpha() == 255)
    check(f"[{pk}] path_key recorded on the layer", gl.get_prop(lyr, "path_key") == pk)

print("\n=== the WMS fallback keeps its filter ===")
w = QgsRasterLayer(gl.strip_wms_uri(FID, host=HOST), "fallback", "wms")
check("valid WMS layer, authcfg=gegdpro in its source",
      w.isValid() and "authcfg=gegdpro" in w.source() and DUMMY not in w.source())
take()
render(w, BOX)
got = take()
maps = [g for g in got if g[1].get("request", [""])[0].lower() == "getmap"]
check(f"GetMap requested on our URL ({len(maps)} request(s))",
      maps and all(g[0] == api.ENDPOINTS["wms"] for g in maps))
check("… each with cql_filter=featureId='<fid>' (IgnoreGetMapUrl keeps our URL)",
      all(g[1].get("cql_filter") == [f"featureId='{FID}'"] for g in maps))

print("\n=== names, properties, rights ===")
pre_l = gl.make_stream_layer(sp, "pre", host=HOST)
post_l = gl.make_stream_layer(sq, "post", host=HOST)
check(f"names read like the plan ({pre_l.name()} | {post_l.name()})",
      pre_l.name() == "Maxar before · 2026-08-01 WV01 · ONA 30° (stream)"
      and post_l.name() == "Maxar after · 2026-08-26 WV02 · ONA 20° (stream)")
check("no _pre_/_post_ token: the 3D tab never pairs a stream",
      not any(re.search(r"_(pre|post)_", l.name()) for l in (pre_l, post_l)))
props = {k: gl.get_prop(post_l, k) for k in ("kind", "side", "fid", "acq_utc", "sensor",
                                             "ona", "level", "grade", "shift50")}
check(f"custom properties for adopt_layers ({props})",
      props["kind"] == "stream" and props["side"] == "post" and props["fid"] == FID
      and props["acq_utc"].startswith("2026-08-26T") and props["acq_utc"].endswith("Z")
      and props["sensor"] == "WV02" and abs(float(props["ona"]) - 20.2) < 1e-6
      and props["grade"] == "mapping" and abs(float(props["shift50"]) - 21.0) < 0.6)
rights = " ".join(post_l.metadata().rights())
check("rights: 'Imagery © 2026 Vantor, via NGA GEGD Pro. AEC internal use only.'",
      "Imagery © 2026 Vantor, via NGA GEGD Pro. AEC internal use only." in rights
      and gl.get_prop(post_l, "credit") in rights)
check("abstract says display imagery, never radiometry",
      "never for radiometry" in post_l.metadata().abstract()
      and FID in post_l.metadata().abstract())
tp = api.Strip(feature_id="00000000-0000-4000-8000-0000000000aa", acq_utc=origin,
               sensor="UMBRA", source="UMBRA", product_name="THIRD_PARTY",
               is_third_party=True, vendor="Umbra", copyright="(c) 2026 Umbra Lab",
               license_type="EULA-X", rings=sq.strip.rings)
tl = gl.make_stream_layer(tp, "post", host=HOST)
tr = " ".join(tl.metadata().rights())
check("third-party: names its vendor and a separate licence, plus the record's own "
      "copyright and licenceType",
      "Umbra (third-party, separate licence)" in tr and "Vantor" not in tr
      and "Catalog copyright: (c) 2026 Umbra Lab" in tr and "EULA-X" in tr
      and gl.get_prop(tl, "vendor") == "Umbra")
gl.set_props(tl, nothing=None)
check("set_props writes None as ''", gl.get_prop(tl, "nothing", "x") == "")

print("\n=== footprints, search circle, adoption by property ===")
fp = gl.footprint_layer(ranked, "AK2026-0808")
listed = [r for r in ranked.pre + ranked.post if r.hidden_reason is None]
fids = {f["feature_id"] for f in fp.getFeatures()}
check(f"footprints: one per listed strip ({fp.featureCount()}), keyed by feature_id",
      fp.featureCount() == len(listed) and fids == {r.strip.feature_id for r in listed})
fp_all = gl.footprint_layer(ranked, "AK2026-0808", include_hidden=True)
check("include_hidden adds the rows the filters hide",
      fp_all.featureCount() == len(ranked.pre) + len(ranked.post) > fp.featureCount())
stars = sorted(f["side"] for f in fp.getFeatures() if f["star"] == 1)
check("the ★ pair is flagged in the attributes", stars == ["post", "pre"])
rules = fp.renderer().rootRule().children() if isinstance(fp.renderer(), QgsRuleBasedRenderer) else []
check("outline-only, rule-based (★ / mapping / dashed visual-or-context, per side)",
      len(rules) == 6 and all(r.symbol().symbolLayer(0).brushStyle() == 0 for r in rules))
aoi = gl.aoi_layer(LAT, LON, 5.0, "AK2026-0808")
c = next(aoi.getFeatures()).geometry().centroid().asPoint()
check("search circle: one polygon centred on the epicentre, tagged",
      aoi.featureCount() == 1 and abs(c.x() - LON) < 1e-6 and abs(c.y() - LAT) < 1e-6
      and gl.get_prop(aoi, "kind") == "aoi" and gl.get_prop(fp, "kind") == "footprints")
project = QgsProject.instance()
root = project.layerTreeRoot()
for lyr in (aoi, fp, pre_l, post_l):
    project.addMapLayer(lyr)                 # each lands on top: post_l ends up first
post_l.setName("renamed by the analyst")
found = gl.adopt_layers(project)
check("adopt_layers finds them by property, not name, streams top-first",
      [l.id() for l in found["stream"]] == [post_l.id(), pre_l.id()]
      and found["footprints"].id() == fp.id() and found["aoi"].id() == aoi.id())
project.clear()

print("\n=== fallback mode (no APIHeader method): no authcfg in any source ===")
_real_mode = gegd_auth.mode
gegd_auth.mode = lambda: "preprocessor"
try:
    x, wm = gl.strip_xyz_uri(FID, host=HOST), gl.strip_wms_uri(FID, host=HOST)
finally:
    gegd_auth.mode = _real_mode
check("XYZ and WMS sources carry no authcfg (a missing config would block the injector)",
      "authcfg" not in x and "authcfg" not in wm and "type=xyz" in x)

print("\n=== offline throughout ===")
check(f"not one request left this machine ({OFFHOST})", OFFHOST == [])

gegd_auth.forget_key()
gegd_auth.remove_host_guard()
SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
