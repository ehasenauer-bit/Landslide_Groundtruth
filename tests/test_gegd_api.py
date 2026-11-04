"""GEGD Pro wire format: the URLs we send, the catalog we parse, the errors we name.

Run with tests/run_all.sh, or any python at all — gegd_api is plain stdlib:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_api.py

What it protects, each found against the live server (2026-10-12/13):
  * the axis-order trap. A BBOX without 'CRS:84' is read lat,lon and the natural
    lon-first form returns ZERO strips with no error — an empty archive that is
    really a broken query. Every spatial filter must be the CRS:84 form, lon first.
  * the key leaking into a URL. GEGD copies a ?maxar_api_key= into every link it
    returns, and QGIS saves layer URLs into the .qgz; no URL may carry auth.
  * a 401 that means two different things: "key not sent" on streaming, "API keys
    can't use this" on Discovery/usage/analytics. And HTTP 400 "Bad data query."
    is a request bug, not a key problem.
  * cloud as a FRACTION, catalog ids that changed shape (B0…) and must never be
    decoded, and records whose third-party status shows only in productName.

Runs under TZ=America/Anchorage so any accidental local-time conversion shows up
as an 8-hour shift. Uses a dummy key built at run time; never the real one.
"""
import base64
import json
import os
import re
import sys
import time
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote, urlsplit

os.environ["TZ"] = "America/Anchorage"
time.tzset()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
sys.path.insert(0, PKG)

from landslide_groundtruth import gegd_api as A  # noqa: E402
from landslide_groundtruth import gegd_rank as R  # noqa: E402

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    return None


def raw(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def body(name):
    """A fixture as the server sent it: the generator's marker line removed."""
    lines = raw(name).decode("utf-8").splitlines(keepends=True)
    return "".join(l for l in lines if not l.startswith("# _synthetic")).encode("utf-8")


def qs(url):
    u = urlsplit(url)
    return u, {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}


# A key-shaped dummy (base64 of a UUID, 48 characters) built here so no key-shaped
# literal sits in the repo.
DUMMY_KEY = base64.b64encode(str(uuid.UUID(int=0x5EC2E7)).encode()).decode()
assert len(DUMMY_KEY) == 48

print("=== 0. the test really runs on Alaska time ===")
aug = datetime(2026, 8, 8, 12, tzinfo=timezone.utc).timestamp()
check("TZ=America/Anchorage is in effect (AKDT, UTC-8 in August)",
      time.strftime("%Z", time.localtime(aug)) == "AKDT"
      and time.localtime(aug).tm_gmtoff == -8 * 3600)

ILI = json.loads(raw("wfs_iliamna.json"))["_event"]
BBOX = R.event_bbox(ILI["lat"], ILI["lon"], ILI["r_km"])
T0 = datetime(2025, 8, 8, 13, 0, 0)                       # naive = UTC
T1 = datetime(2026, 11, 6, 13, 0, 0, tzinfo=timezone.utc)

print("\n=== 1. the spatial filter is always BBOX(…,'CRS:84'), lon first ===")
URLS = {
    "getfeature": A.wfs_getfeature_url(None, BBOX, T0, T1),
    "getfeature_neq": A.wfs_getfeature_url(None, BBOX, T0, T1, product_clause="neq"),
    "getfeature_none": A.wfs_getfeature_url(None, BBOX, T0, T1, product_clause="none"),
    "hits": A.wfs_hits_url(None, BBOX, T0, T1),
    "nearest_pre": A.wfs_nearest_url(None, BBOX, T0, "pre"),
    "nearest_post": A.wfs_nearest_url(None, BBOX, T1, "post"),
    "by_id": A.wfs_by_id_url(None, "ccaae319-0000-4000-8000-000000000826"),
    "thumb": A.wms_thumb_url(None, "ccaae319-0000-4000-8000-000000000826", BBOX, 320, 180),
    "xyz": A.wmts_xyz_template(None, "ccaae319-0000-4000-8000-000000000826"),
    "xyz_alt": A.wmts_xyz_template(None, "ccaae319-0000-4000-8000-000000000826", "wmts_alt"),
}
lonmin, latmin, lonmax, latmax = BBOX
bboxes_seen = 0
for name in ("getfeature", "getfeature_neq", "getfeature_none", "hits",
             "nearest_pre", "nearest_post"):
    _, q = qs(URLS[name])
    cql = q["cql_filter"]
    calls = re.findall(r"BBOX\(([^)]*)\)", cql)
    good = len(calls) == 1
    for c in calls:
        parts = c.split(",")
        good &= (len(parts) == 6 and parts[0] == "featureGeometry"
                 and parts[5] == "'CRS:84'")
        nums = [float(p) for p in parts[1:5]]
        # Alaska: longitudes negative in slots 1 and 3, latitudes ~60 in 2 and 4
        good &= nums[0] < -150 < nums[2] + 3 and 59 < nums[1] < nums[3] < 61
        good &= abs(nums[0] - lonmin) < 1e-6 and abs(nums[3] - latmax) < 1e-6
        bboxes_seen += 1
    check(f"{name}: one BBOX, 6 args, lon first, ends 'CRS:84'", good)
check("never a bare 4-number BBOX in any WFS url",
      not any(re.search(r"BBOX\(featureGeometry,[^,()]+,[^,()]+,[^,()]+,[^,()']+\)",
                        unquote(u)) for u in URLS.values()))
check("bbox_cql itself: exact text",
      A.bbox_cql((-153.1, 60.0, -153.0, 60.05))
      == "BBOX(featureGeometry,-153.1,60,-153,60.05,'CRS:84')")
check("a lat-first bbox is refused, not sent (it would return 0 silently)",
      raises(ValueError, A.bbox_cql, (60.0, -153.1, 60.05, -153.0)) is not None)
check("a min>max bbox is refused", raises(ValueError, A.bbox_cql, (-153.0, 60.0, -153.1, 60.05)) is not None)
check("a non-finite bbox is refused", raises(ValueError, A.bbox_cql, (float("nan"), 60, -153, 61)) is not None)

print("\n=== 2. the rest of the search request ===")
u, q = qs(URLS["getfeature"])
check("host and path", (u.scheme, u.netloc, u.path) == ("https", "pro.gegd.com", A.ENDPOINTS["wfs"]))
check("WFS 2.0.0 GetFeature on Maxar:FinishedFeature, JSON",
      (q["service"], q["version"], q["request"], q["typeNames"], q["outputFormat"])
      == ("WFS", "2.0.0", "GetFeature", "Maxar:FinishedFeature", "application/json"))
check("single page of up to 5000, newest first, ties by featureId",
      q["count"] == "5000" and q["sortBy"] == "acquisitionDate D,featureId A")
check("no startIndex (we never page; we never follow a server 'next' link)", "startIndex" not in q)
cql = q["cql_filter"]
check("window: >= t0 (naive read as UTC, NOT Alaska time)",
      "acquisitionDate>='2025-08-08T13:00:00Z'" in cql)
check("window: < t1, Z time", "acquisitionDate<'2026-11-06T13:00:00Z'" in cql)
akdt = timezone(timedelta(hours=-8))
_, q2 = qs(A.wfs_getfeature_url(None, BBOX, datetime(2025, 8, 8, 5, 0, tzinfo=akdt), T1))
check("an aware AKDT time is converted, not relabelled (05:00 AKDT = 13:00Z)",
      "acquisitionDate>='2025-08-08T13:00:00Z'" in q2["cql_filter"])
_, q3 = qs(A.wfs_getfeature_url(None, BBOX, datetime(2025, 8, 8, 13, 0, 0, 900000),
                                 datetime(2026, 1, 1, 0, 0, 0, 1)))
check("fractional bounds: start floors, exclusive end ceils (no gap, no overlap)",
      "acquisitionDate>='2025-08-08T13:00:00Z'" in q3["cql_filter"]
      and "acquisitionDate<'2026-01-01T00:00:01Z'" in q3["cql_filter"])
not_in = "productName NOT IN (" + ",".join(f"'{p}'" for p in A.EXCLUDED_PRODUCTS) + ")"
check("Vivid + BASE_MOSAIC excluded with NOT IN (all 7 names)", cql.endswith(not_in))
_, qn = qs(URLS["getfeature_neq"])
check("fallback form: a chain of <>, one per product",
      qn["cql_filter"].count("productName<>'") == len(A.EXCLUDED_PRODUCTS)
      and "NOT IN" not in qn["cql_filter"])
_, qz = qs(URLS["getfeature_none"])
check("'none' leaves products to the client", "productName" not in qz["cql_filter"])
check("unknown clause refused", raises(ValueError, A.wfs_getfeature_url, None, BBOX, T0, T1,
                                       product_clause="in") is not None)
check("count outside 1..5000 refused",
      raises(ValueError, A.wfs_getfeature_url, None, BBOX, T0, T1, count=5001) is not None)
check("raw url: spaces, quotes, parentheses and >= are percent-encoded",
      all(c not in URLS["getfeature"] for c in " '()<>") and "%27CRS:84%27" in URLS["getfeature"])
check("the encoded url decodes back to the exact CQL", unquote(urlsplit(URLS["getfeature"]).query)
      .endswith("cql_filter=" + cql))

_, qh = qs(URLS["hits"])
check("hits: resultType=hits, no outputFormat/count (answer is numberMatched)",
      qh.get("resultType") == "hits" and "outputFormat" not in qh and "count" not in qh)
_, qp = qs(URLS["nearest_pre"])
check("nearest pre: acq < t0, newest first, one strip",
      "acquisitionDate<'2025-08-08T13:00:00Z'" in qp["cql_filter"]
      and ">=" not in qp["cql_filter"] and qp["sortBy"].startswith("acquisitionDate D")
      and qp["count"] == "1")
_, qa = qs(URLS["nearest_post"])
check("nearest post: acq >= t1, oldest first",
      "acquisitionDate>='2026-11-06T13:00:00Z'" in qa["cql_filter"]
      and qa["sortBy"].startswith("acquisitionDate A"))
check("nearest: side must be pre/post", raises(ValueError, A.wfs_nearest_url, None, BBOX, T0, "before") is not None)
_, qi = qs(URLS["by_id"])
check("find by id: featureId OR legacyIdentifier, no product filter",
      qi["cql_filter"] == "featureId='ccaae319-0000-4000-8000-000000000826' OR "
                          "legacyIdentifier='ccaae319-0000-4000-8000-000000000826'")
check("find by id: a quote-injection id is refused",
      raises(ValueError, A.wfs_by_id_url, None, "x' OR '1'='1") is not None)
check("find by id: an '&' id is refused (would split the parameter)",
      raises(ValueError, A.wfs_by_id_url, None, "B030001100CBA910&x=1") is not None)
check("find by id: a catalog id is accepted, whitespace trimmed",
      "legacyIdentifier='B030001100CBA910'" in unquote(A.wfs_by_id_url(None, "  B030001100CBA910 ")))

print("\n=== 3. thumbnails and the stream template ===")
u, q = qs(URLS["thumb"])
check("WMS 1.1.1 GetMap of Maxar:Imagery, EPSG:4326, transparent, XML exceptions",
      (q["version"], q["request"], q["layers"], q["srs"], q["transparent"], q["exceptions"])
      == ("1.1.1", "GetMap", "Maxar:Imagery", "EPSG:4326", "true", "application/vnd.ogc.se_xml"))
check("WMS bbox is lon,lat,lon,lat (1.1.1 + 4326 = x,y)",
      [float(v) for v in q["bbox"].split(",")] == [round(v, 7) for v in BBOX])
check("one strip: cql_filter=featureId='…'",
      q["cql_filter"] == "featureId='ccaae319-0000-4000-8000-000000000826'")
check("empty styles parameter is present", "styles" in q and q["styles"] == "")
check("thumbnail size capped at the verified 2048 px",
      raises(ValueError, A.wms_thumb_url, None, "abcd-1234", BBOX, 4096, 100) is not None)
check("unknown image format refused",
      raises(ValueError, A.wms_thumb_url, None, "abcd-1234", BBOX, 10, 10, "image/gif") is not None)
x = URLS["xyz"]
check("xyz: GWC KVP path is primary", x.startswith("https://pro.gegd.com/streaming/v1/ogc/gwc/service/wmts?"))
check("xyz alt: the bare /wmts path", URLS["xyz_alt"].startswith("https://pro.gegd.com/streaming/v1/ogc/wmts?"))
check("xyz: literal {z} {x} {y}, once each, never percent-encoded",
      all(x.count(t) == 1 for t in ("{z}", "{x}", "{y}")) and "%7B" not in x)
check("xyz: tilerow={y}, tilecol={x}, matrix EPSG:3857:{z}",
      "tilerow={y}" in x and "tilecol={x}" in x and "tilematrix=EPSG:3857:{z}" in x)
check("xyz: one strip only (CQL_FILTER on featureId), png, layer Maxar:Imagery",
      x.endswith("&CQL_FILTER=featureId='ccaae319-0000-4000-8000-000000000826'")
      and "format=image/png" in x and "layer=Maxar:Imagery" in x)
check("short placeholder ids are fine (only the characters are policed)",
      A.wmts_xyz_template(None, "FID").endswith("featureId='FID'"))
check("xyz: an id with a quote or '&' is refused",
      raises(ValueError, A.wmts_xyz_template, None, "a'b") is not None
      and raises(ValueError, A.wmts_xyz_template, None, "a&b") is not None)
check("xyz: path key must be a WMTS path",
      raises(ValueError, A.wmts_xyz_template, None, "abcd-1234", "wms") is not None)

print("\n=== 4. no auth in any URL, and the host is checked ===")
FORBIDDEN = ("api_key", "api-key", "apikey", "authcfg", "token", "bearer", "password",
             "key=", DUMMY_KEY.lower())
leaks = [(n, f) for n, u in URLS.items() for f in FORBIDDEN if f in unquote(u).lower()]
check(f"none of {len(URLS)} built URLs carries a credential-like token {leaks}", not leaks)
check("custom host: trailing slash trimmed",
      A.wmts_xyz_template("https://pro.gegd.com/", "abcd-1234").startswith(
          "https://pro.gegd.com/streaming/"))
check("mock-server host accepted (http, port)",
      A.wfs_hits_url("http://127.0.0.1:8123", BBOX, T0, T1).startswith("http://127.0.0.1:8123/"))
for bad in ("https://user:pw@pro.gegd.com", "https://pro.gegd.com/?maxar_api_key=x",
            "pro.gegd.com", "ftp://pro.gegd.com", "https://pro.gegd.com/streaming"):
    check(f"host refused: {bad!r}", raises(ValueError, A.wfs_hits_url, bad, BBOX, T0, T1) is not None)

print("\n=== 5. parsing the catalog ===")
strips = A.parse_features(raw("wfs_iliamna.json"))
by = {s.feature_id: s for s in strips}
check(f"15 records parsed, in server order ({len(strips)})", len(strips) == 15)
s0801 = next(s for s in strips if s.acq_utc.date().isoformat() == "2026-08-01")
s0826 = next(s for s in strips if s.acq_utc.date().isoformat() == "2026-08-26")
check("acquisition time is aware UTC with its milliseconds",
      s0801.acq_utc == datetime(2026, 8, 1, 0, 18, 47, 500000, tzinfo=timezone.utc))
check("…and Alaska calls it the previous day (the trap the split must ignore)",
      R.alaska_str(s0801.acq_utc) == "2026-07-31 16:18 AKDT (previous day)")
ring = s0801.rings[0]
check("rings are (lon, lat): lon ~ -153, lat ~ 60 (GeoJSON order, not the urn's)",
      all(-154 < p[0] < -152 and 59.5 < p[1] < 60.5 for p in ring))
check("cloud stays a FRACTION (0.007, not 0.7 %)", s0801.cloud == 0.007)
check("sensor comes from 'source', never from the B0… catalog id",
      s0801.sensor == "WV01" and s0801.catalog_id.startswith("B0F"))
check("off-nadir, sun, level, band config, gsd read",
      (s0801.ona, s0801.sun_el, s0801.processing_level, s0801.band_config, s0801.gsd_m)
      == (29.6, 43.2, "LV3D", "P", 0.596))
check("own imagery credits Vantor; not third-party", s0826.vendor == "Vantor" and not s0826.is_third_party)
check("ingest lag computed (created − acquired)", abs(s0826.ingest_lag_h - 2.2833) < 0.01)
s0928 = next(s for s in strips if s.source == "WV03_VNIR")
check("'WV03_VNIR' normalised to 'WV03'; raw source kept", s0928.sensor == "WV03")
check("an older record with no processingLevel reads None", s0928.processing_level is None)
lv2a = next(s for s in strips if s.processing_level == "LV2A")
check("LV2A's junk ce90Accuracy (0.0014) is dropped, LV3D's 8.4 kept",
      lv2a.ce90 is None and s0826.ce90 == 8.4)
rdog = next(s for s in strips if s.product_name == "RDOG_BASE")
check("RDOG_BASE: multi-date, its 5-day span read from the mosaic time fields",
      rdog.multi_date and (rdog.latest_utc - rdog.earliest_utc).days == 4)
check("RDOG_BASE: copyright and licence type kept for the rights metadata",
      rdog.copyright == "Image Copyright 2023 DigitalGlobe Inc" and rdog.license_type == "nextview")
sar = next(s for s in strips if s.sensor_type == "Radar")
check("third-party SAR: flagged, vendor named for the credit line",
      sar.is_third_party and sar.vendor == "Umbra" and sar.ona is None and sar.sun_el is None)
viv = next(s for s in strips if (s.product_name or "").startswith("VIVID"))
check("Vivid parsed (exclusion is the ranker's job) with both MultiPolygon parts",
      len(viv.rings) == 2 and A.is_excluded(viv) and not A.is_excluded(s0826))
check("single-date strips are not multi-date", not s0826.multi_date and not s0801.multi_date)
check("Strip is frozen and hashable",
      raises(FrozenInstanceError, setattr, s0826, "ona", 1.0) is not None and hash(s0826) is not None)
check("label = 'YYYY-MM-DD SENSOR'", s0801.label == "2026-08-01 WV01")

# the third-party product literal, built in memory (the fixture scanner bans it in files)
def mem(props, geom=None):
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": geom, "properties": props}]}
    return json.dumps(doc).encode()


iceye = A.parse_features(mem({"featureId": "f0000000-0000-4000-8000-000000000001",
                              "acquisitionDate": "2026-10-11T07:36:38Z",
                              "productName": "THIRD" + "_PARTY", "source": "ICEYE"}))[0]
check("third-party by productName alone (no isThirdParty, no sensorType — as live ICEYE)",
      iceye.is_third_party and iceye.vendor == "ICEYE")
ingested = A.parse_features(mem({"featureId": "f0000000-0000-4000-8000-000000000002",
                                 "acquisitionDate": "2026-09-01T21:00:00Z",
                                 "productName": "PRIVATE" + "_FEATURE", "source": "WV02",
                                 "processingLevel": "LV3D", "offNadirAngle": 12.0}))[0]
check("an ingested strip is ordinary own optical imagery",
      not ingested.is_third_party and R.grade(ingested) == "mapping")
check("parse_time: 1-digit fraction, zone-less mosaic stamp, offset converted, junk -> None",
      A.parse_time("2026-08-01T00:18:47.5Z").microsecond == 500000
      and A.parse_time("2021-04-01T00:36:53.000000").tzinfo is not None
      and A.parse_time("2026-08-01T01:00:00+01:00").hour == 0
      and A.parse_time("not a date") is None)
check("empty FeatureCollection -> [] (an honest zero, not drift)",
      A.parse_features(raw("wfs_empty_petersdome.json")) == [])

print("\n=== 6. schema drift is loud ===")
e = raises(A.SchemaDrift, A.parse_features, mem({"acquisitionDate": "2026-08-01T00:00:00Z"}))
check("missing featureId -> SchemaDrift naming it",
      e is not None and e.missing == ("featureId",) and "featureId" in e.user_message)
check("SchemaDrift is a GegdError of kind schema_drift",
      isinstance(e, A.GegdError) and e.kind == "schema_drift")
e = raises(A.SchemaDrift, A.parse_features, mem({"featureId": "f0000000-0000-4000-8000-000000000003"}))
check("missing acquisitionDate -> SchemaDrift", e is not None and e.missing == ("acquisitionDate",))
e = raises(A.SchemaDrift, A.parse_features, mem({"featureId": "f0000000-0000-4000-8000-000000000004",
                                                 "acquisitionDate": "yesterday"}))
check("an unreadable date is drift too, not a silent None", e is not None)
check("not JSON -> SchemaDrift", raises(A.SchemaDrift, A.parse_features, b"{nope") is not None)
check("JSON without features -> SchemaDrift",
      raises(A.SchemaDrift, A.parse_features, b'{"type":"FeatureCollection"}') is not None)
e = raises(A.GegdError, A.parse_features, body("ogc_service_exception.xml"))
check("an OGC exception body -> ogc_exception (not drift)",
      e is not None and e.kind == "ogc_exception" and "CQL" in e.detail)
check("a point geometry -> SchemaDrift",
      raises(A.SchemaDrift, A.parse_features, mem({"featureId": "f0000000-0000-4000-8000-000000000005",
                                                   "acquisitionDate": "2026-08-01T00:00:00Z"},
                                                  {"type": "Point", "coordinates": [0, 0]})) is not None)

print("\n=== 7. errors are classified on (service, status, body) ===")
k401 = body("err_401_key_not_sent.txt")
for svc in sorted(A.STREAMING_SERVICES):
    e = A.classify_http(svc, 401, k401, "text/plain")
    check(f"401 'No JWT bearer token' on {svc} -> key_not_sent", e is not None and e.kind == "key_not_sent")
for svc in ("discovery", "usage", "apikey", "analytics"):
    e = A.classify_http(svc, 401, k401, "text/plain")
    check(f"…the SAME 401 on {svc} -> not_entitled (keys can't use it)",
          e is not None and e.kind == "not_entitled")
e = A.classify_http("discovery", 401, body("err_401_unauthorized.json"), "application/json")
check("401 {message: Unauthorized} on discovery -> not_entitled", e.kind == "not_entitled")
e = A.classify_http("analytics", 401, body("err_401_unrecognized_token.json"), "application/json")
check("401 'Unrecognized token' on analytics -> not_entitled", e.kind == "not_entitled")
e = A.classify_http("wms", 401, body("err_401_unauthorized.json"), "application/json")
check("but any 401 on a streaming service -> key_not_sent", e.kind == "key_not_sent")
check("key_not_sent tells you what to do", "Save key" in e.user_message)
e = A.classify_http("wfs", 403, body("err_403_malformed_token.txt"), "text/plain")
check("403 'Malformed token' -> wrong_auth_scheme", e.kind == "wrong_auth_scheme")
check("403 other on wfs -> key_rejected",
      A.classify_http("wfs", 403, b"Forbidden", "text/plain").kind == "key_rejected")
check("403 other on usage -> not_entitled",
      A.classify_http("usage", 403, b"Forbidden", "text/plain").kind == "not_entitled")
e = A.classify_http("wfs", 400, body("err_400_bad_data_query.xml"), "application/xml")
check("HTTP 400 'Bad data query.' -> query_error (a request bug, NOT auth)",
      e.kind == "query_error" and e.detail == "Bad data query." and "key" not in e.kind)
check("…and its message says the key is fine", "not a problem with your key" in e.user_message)
e = A.classify_http("wcs", 404, b"", None)
check("404 -> endpoint_moved, naming the service", e.kind == "endpoint_moved" and "WCS" in e.user_message)
e = A.classify_http("wcs", 410, b"", None)
check("410 -> endpoint_retired, naming the service", e.kind == "endpoint_retired" and "WCS" in e.user_message)
check("302 (ManualRedirectPolicy surfaces it) -> endpoint_moved",
      A.classify_http("wfs", 302, b"", None).kind == "endpoint_moved")
e = A.classify_http("wmts", 429, b"Too Many Requests", "text/plain")
check("429 -> rate_limited, retryable", e.kind == "rate_limited" and e.retryable)
for st in (500, 502, 503, 504):
    check(f"{st} -> server_busy, retryable", A.classify_http("wms", st, b"", None).kind == "server_busy")
check("no HTTP status -> network, retryable",
      A.classify_http("wfs", 0, b"Host not found", None).kind == "network"
      and A.classify_http("wfs", None, b"", None).retryable)
check("401 is NOT retryable (retrying a missing key wastes 35 s)",
      not A.classify_http("wfs", 401, k401, "text/plain").retryable)
e = A.classify_http("wms", 200, body("ogc_service_exception.xml"), "application/vnd.ogc.se_xml")
check("an OGC exception inside HTTP 200 -> ogc_exception with its text",
      e is not None and e.kind == "ogc_exception" and "Could not parse CQL filter list." in e.detail)
usage = (b'<?xml version="1.0"?><ServiceExceptionReport version="1.1.1"><ServiceException>'
         b'Daily usage limit reached for this account</ServiceException></ServiceExceptionReport>')
check("usage/quota text -> usage_limit (stops downloads)",
      A.classify_http("wms", 200, usage, "text/xml").kind == "usage_limit")
size = (b'<?xml version="1.0"?><ServiceExceptionReport version="1.1.1"><ServiceException>'
        b'Maximum image size exceeded: 8192x8192</ServiceException></ServiceExceptionReport>')
check("…but a SIZE error stays ogc_exception (the clip halves its tile; not a quota)",
      A.classify_http("wms", 200, size, "text/xml").kind == "ogc_exception")
check("a good JSON 200 -> None", A.classify_http("wfs", 200, raw("wfs_iliamna.json"), "application/json") is None)
check("a JSON 200 that merely contains the word 'limit' -> None",
      A.classify_http("wfs", 200, b'{"features":[],"note":"usage limit quota"}', "application/json") is None)
check("an image 200 -> None", A.classify_http("wmts", 200, b"\x89PNG\r\n", "image/png") is None)
check("an exception LABELLED image/png is still caught: the body is sniffed, the type isn't trusted",
      (A.classify_http("wmts", 200, body("ogc_service_exception.xml"), "image/png") or A.GegdError("network")).kind
      == "ogc_exception")
check("…and so is one behind a UTF-8 byte-order mark",
      (A.classify_http("wms", 200, b"\xef\xbb\xbf" + body("ogc_service_exception.xml"), "image/png")
       or A.GegdError("network")).kind == "ogc_exception")
check("a JSON catalog behind a byte-order mark still parses",
      len(A.parse_features(b"\xef\xbb\xbf" + raw("wfs_iliamna.json"))) == 15)
check("a hits 200 (XML, not an exception) -> None",
      A.classify_http("wfs", 200, raw("wfs_hits_canary.xml"), "text/xml") is None)
check("an unknown service name is a programming error",
      raises(ValueError, A.classify_http, "thumbs", 401, b"", None) is not None)
leaky = b"upstream error: header maxar-api-key: " + DUMMY_KEY.encode() + b" rejected"
e = A.classify_http("wfs", 500, leaky, "text/plain")
check("a body echoing the key never reaches the message or detail",
      DUMMY_KEY not in e.user_message and DUMMY_KEY not in e.detail and DUMMY_KEY not in str(e))
check("GegdError refuses an unknown kind", raises(ValueError, A.GegdError, "oops") is not None)
import copy  # noqa: E402
import pickle  # noqa: E402
sd = A.SchemaDrift(missing=("featureId",))
for nm, clone in (("copy", copy.copy(e)), ("pickle", pickle.loads(pickle.dumps(e))),
                  ("SchemaDrift pickle", pickle.loads(pickle.dumps(sd)))):
    src = sd if nm.startswith("Schema") else e
    check(f"GegdError survives {nm} (a task may hand it across threads)",
          type(clone) is type(src) and clone.kind == src.kind and str(clone) == str(src)
          and clone.detail == src.detail and getattr(clone, "missing", None) == getattr(src, "missing", None))
bad_q = A.classify_http("wfs", 400, body("err_400_bad_data_query.xml"), "application/xml")
bad_cql = A.classify_http("wms", 200, body("ogc_service_exception.xml"), "text/xml")
check("clause fallback: NOT IN -> <> -> client, then stop; never on auth errors",
      A.clause_fallback(bad_q, "not_in") == "neq"
      and A.clause_fallback(bad_cql, "neq") == "none"
      and A.clause_fallback(bad_q, "none") is None
      and A.clause_fallback(A.GegdError("key_not_sent"), "not_in") is None)
check("… nor on a failure that is not about the filter (a server hiccup inside a 200, "
      "a 400 about something else): those never change the query",
      A.clause_fallback(A.GegdError("ogc_exception", detail="java.lang.OutOfMemoryError: "
                                    "Java heap space"), "not_in") is None
      and A.clause_fallback(A.GegdError("query_error", detail="count must be at most 5000"),
                            "not_in") is None
      and A.clause_fallback(A.GegdError("query_error"), "not_in") is None)
check("parse_hits reads numberMatched", A.parse_hits(raw("wfs_hits_canary.xml")) == 34
      and A.parse_hits(raw("wfs_iliamna.json")) is None)

print("\n=== 8. parse_ogc_exception ===")
check("OWS 1.1 ExceptionReport", A.parse_ogc_exception(body("err_400_bad_data_query.xml")) == "Bad data query.")
check("WMS 1.1.1 ServiceExceptionReport, whitespace collapsed",
      A.parse_ogc_exception(body("ogc_service_exception.xml"))
      == 'Could not parse CQL filter list. Encountered "NOT" at line 1, column 120.')
ns13 = (b'<ServiceExceptionReport xmlns="http://www.opengis.net/ogc" version="1.3.0">'
        b'<ServiceException code="LayerNotDefined">No such layer</ServiceException>'
        b'</ServiceExceptionReport>')
check("WMS 1.3.0 namespaced", A.parse_ogc_exception(ns13) == "No such layer")
check("code only -> the code", A.parse_ogc_exception(
    b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1"><ows:Exception '
    b'exceptionCode="OperationNotSupported"/></ows:ExceptionReport>') == "OperationNotSupported")
check("a hits document is not an exception", A.parse_ogc_exception(raw("wfs_hits_canary.xml")) is None)
check("JSON / plain text / None -> None",
      A.parse_ogc_exception(raw("wfs_iliamna.json")) is None
      and A.parse_ogc_exception(b"No JWT bearer token") is None and A.parse_ogc_exception(None) is None)
dtd = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ServiceExceptionReport>'
       b'<ServiceException>boom &a;</ServiceException></ServiceExceptionReport>')
got = A.parse_ogc_exception(dtd)
check("a DTD-bearing body is read by regex, entities NOT expanded", got is not None and "aaaa" not in got)
check("truncated XML (root never closed) still yields its text",
      A.parse_ogc_exception(b"<ServiceExceptionReport><ServiceException>Layer missing"
                            b"</ServiceException>") == "Layer missing")

print("\n=== 9. redact() removes every form of the key ===")
K = DUMMY_KEY
cases = {
    "header form": f"maxar-api-key: {K}",
    "header form, any case": f"MAXAR-API-KEY:{K}",
    "query form": f"https://pro.gegd.com/wfs?maxar_api_key={K}&count=1",
    "url-encoded query form": "x?" + quote(f"maxar_api_key={K}", safe=""),
    "header dict repr": "{b'maxar-api-key': b'" + K + "'}",
    "json": json.dumps({"maxar-api-key": K}),
    "Authorization header": f"Authorization: Bearer {K}",
    "bare key, no name, no key given": f"the key is {K} ok",
}
for name, text in cases.items():
    out = A.redact(text)
    check(f"{name}: gone", K not in out and quote(K, safe="") not in out
          and K.lower() not in out.lower() and A.REDACTED in out)
decoded = str(uuid.UUID(int=0x5EC2E7))
check("with the key given: its decoded UUID and URL-encoded spellings go too",
      decoded not in A.redact(f"x {decoded} y", key=K)
      and quote(K, safe="") not in A.redact("a=" + quote(K, safe=""), key=K))
check("a short 'key' is not used to mangle text", A.redact("abc def", key="abc") == "abc def")
check("bytes in, str out; None -> ''", A.redact(b"ok") == "ok" and A.redact(None) == "")
benign = ("featureId ccaae319-0000-4000-8000-000000000826; layer Maxar:Imagery; "
          "send the header maxar-api-key; sha1 " + "a" * 40 + "; paste the API key")
check("ordinary text, UUIDs, hex digests and the header NAME survive", A.redact(benign) == benign)
check("idempotent", A.redact(A.redact(cases["query form"])) == A.redact(cases["query form"]))
# A key glued into a longer base64-alphabet run ('/', '-', '_', '+' are base64
# characters too) is found by the window scan, with no key to compare against.
for name, text in {"in a URL path": f"GET /apikey/v1/keys/{K}/usage failed",
                   "glued to a word after it": f"key {K}-expired",
                   "glued to a word before it": f"x_{K}",
                   "twice in a row": K + K}.items():
    out = A.redact(text)
    check(f"key {name}: gone", K[:12] not in out and K[-12:] not in out and A.REDACTED in out)
bare = base64.b64encode(uuid.UUID(int=0xB0B).hex.encode()).decode()     # undashed form
check("the base64 of an undashed UUID inside a path: gone",
      bare.rstrip("=")[:12] not in A.redact(f"/v1/{bare.rstrip('=')}/x"))
check("… while a long benign path or base64 blob survives untouched",
      A.redact("/a/b/" + "Zm9v" * 30 + "/c") == "/a/b/" + "Zm9v" * 30 + "/c")
# Redacted BEFORE it is cut: a key straddling the 300-character first-line cut,
# or the 500-character detail cut, must not leave its head behind.
for st in (403, 500):
    for pad in (270, 280, 290, 299):
        e = A.classify_http("wfs", st, ("y" * pad + " " + K + " tail").encode(), "text/plain")
        check(f"HTTP {st}, key at character {pad + 1} of the first line: no prefix left",
              K[:8] not in e.detail and K[:8] not in e.user_message and K[:8] not in str(e))
for pad in (440, 470, 495):
    xml = ("<ServiceExceptionReport><ServiceException>" + "w" * pad + " " + K + " tail"
           "</ServiceException></ServiceExceptionReport>").encode()
    e = A.classify_http("wms", 200, xml, "text/xml")
    check(f"OGC exception in a 200, key at character {pad + 1}: no prefix left",
          e is not None and K[:8] not in e.detail and K[:8] not in e.user_message)
e = A.GegdError("network", detail=" " * 1990 + K + " x")
check("a network error body that collapses below the cut: still redacted", K[:8] not in e.detail)
check("a long detail is still bounded", len(A.GegdError("network", detail="z " * 2000).detail) <= 500)

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
