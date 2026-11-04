"""GEGD Pro (Maxar/Vantor) wire format: request URLs, the catalog parser, the error map.

The Maxar VHR tab talks to one server, ``https://pro.gegd.com`` — NGA's GEGD Pro
front end to Vantor's (formerly Maxar's) streaming OGC services — with an API key.
Everything in this module is plain stdlib, so it unit-tests without QGIS. The
network itself is ``gegd_net`` (QGIS's network manager inside a QgsTask) and the
key lives only in the QGIS auth DB (``gegd_auth``).

**No URL built here ever carries auth.** The key travels as the request header
``maxar-api-key``, added by the ``gegdpro`` authcfg at send time. The query form
``?maxar_api_key=`` also works on the server, but GEGD then copies the key into
every capabilities href and every WFS ``next`` link, and QGIS saves layer URLs into
the .qgz verbatim — which is exactly how 22 of 60 shared projects came to hold the
Planet key in plain text.

Facts this module encodes (read-only probes of pro.gegd.com, 2026-10-12/13):

* **Spatial filter.** ``BBOX(featureGeometry,lonmin,latmin,lonmax,latmax,'CRS:84')``.
  A BBOX *without* the CRS argument is read lat,lon (EPSG:4326 URN order), and the
  natural lon-first form then returns **0 features and no error**. INTERSECTS with
  a point returns 0 in either order and DWITHIN ignores its distance. So every
  builder emits the CRS:84 form only, and ``gegd_rank`` re-checks each footprint
  against the search circle client-side, so an axis bug can never read as "no
  imagery here".
* GeoJSON output is lon,lat, whatever ``urn:ogc:def:crs:EPSG::4326`` the
  collection declares.
* ``cloudCover`` is a strip-wide FRACTION 0-1 (at most ~0.35 seen), not percent.
* ``legacyIdentifier`` changed shape around Aug 2026 (``B0…``). The sensor is never
  decoded from an ID; everything is keyed on ``featureId``.
* ``RDOG_BASE`` records are legacy mosaic tiles (``outputMosaic``), not single
  dates: Iliamna's spans 2021-03-27 -> 04-01. ``earliest/latestAcquisitionTime``
  differ only on multi-date records, which makes them a generic test.
* Third-party records (Umbra, ICEYE, Capella SAR) come back as product
  ``THIRD_PARTY``, sometimes with no ``isThirdParty`` or ``sensorType`` at all.
* Sorting on ``productName`` is HTTP 400 "Bad data query." — a request bug, not an
  auth failure. That is why errors are classified on (service, status, body) and
  never on the status code alone: a 401 means "key not sent, mistyped or expired"
  on the streaming services but "not available to API keys" on Discovery, usage,
  API-key management and Raster Analytics.
"""
import base64
import binascii
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus
import xml.etree.ElementTree as ET

__all__ = [
    "HOST_DEFAULT", "ALLOWED_HOST", "ENDPOINTS", "FEATURE_TYPE", "LAYER",
    "EXCLUDED_PRODUCTS", "REQUIRED_FIELDS", "ARCHIVE_FLOOR_UTC", "MAX_FEATURES",
    "SORT_BY", "PRODUCT_CLAUSES", "OPTICAL_PRODUCTS", "THIRD_PARTY_PRODUCT",
    "MOSAIC_PRODUCTS", "MULTI_DATE_TOL_S", "STREAMING_SERVICES", "SERVICES",
    "KINDS", "Strip", "GegdError", "SchemaDrift",
    "wfs_getfeature_url", "wfs_hits_url", "wfs_by_id_url", "wfs_nearest_url",
    "wms_thumb_url", "wmts_xyz_template", "bbox_cql", "product_cql",
    "parse_features", "parse_hits", "parse_time", "classify_http",
    "parse_ogc_exception", "redact", "clause_fallback", "is_excluded",
]

# ---------------------------------------------------------------- server

HOST_DEFAULT = "https://pro.gegd.com"
# The only host the key may ever be sent to (gegd_auth.HostGuard strips the header
# everywhere else, https + port 443 only).
ALLOWED_HOST = "pro.gegd.com"

# Every path is a constant so an API move is a one-line fix (and the M5 access
# check can name exactly which one broke).
ENDPOINTS = {
    "wfs": "/streaming/v1/ogc/wfs",
    "wms": "/streaming/v1/ogc/wms",
    # GeoWebCache's advertised KVP path is primary. The bare /wmts path honours
    # CQL_FILTER too and returned byte-identical tiles in M0, so it is the drift
    # fallback, not a second opinion.
    "wmts": "/streaming/v1/ogc/gwc/service/wmts",
    "wmts_alt": "/streaming/v1/ogc/wmts",
    "wcs": "/streaming/v1/ogc/wcs",
    "seamlines": "/basemaps/v1/seamlines/ows",
}
FEATURE_TYPE = "Maxar:FinishedFeature"
LAYER = "Maxar:Imagery"

# Vivid is a multi-year composite (Iliamna's record is dated AFTER the event while
# every pixel patch under it predates it), and BASE_MOSAIC is the same idea, so
# neither may ever be offered as a before or after image. They are excluded
# server-side (Vivid footprints are 3280-vertex MultiPolygons, so this also keeps
# responses small) and again client-side in gegd_rank.
EXCLUDED_PRODUCTS = ("VIVID_STANDARD_30", "VIVID_STANDARD", "VIVID_BASIC",
                     "VIVID_ADVANCED", "VIVID_ADVANCED_15", "VIVID_PREMIUM",
                     "BASE_MOSAIC")
# Single-date optical products. DAILY_TAKE is the archive; PRIVATE_FEATURE is a
# strip someone ingested on request. Anything else is "context" (gegd_rank.grade).
OPTICAL_PRODUCTS = ("DAILY_TAKE", "PRIVATE_FEATURE")
THIRD_PARTY_PRODUCT = "THIRD_PARTY"
# Legacy mosaic tiles: multi-date even when their time span is short (Bagley's
# GE01 tile spans 35 s, Iliamna's WV01 tile 5 days).
MOSAIC_PRODUCTS = ("RDOG_BASE",)
# latest - earliest acquisition above this flags a record multi-date. A single
# pass images a strip in well under a minute; stereo mates are separate records.
MULTI_DATE_TOL_S = 300.0

REQUIRED_FIELDS = ("featureId", "acquisitionDate")
# The online Daily Take archive starts here (Alaska-wide earliest, observed
# 2026-10-12; may be a rolling window). Peters Dome and Bagley (2023) have nothing.
ARCHIVE_FLOOR_UTC = datetime(2024, 7, 20, tzinfo=timezone.utc)
# The server's CountDefault. An event window never comes close, so the search is
# a single page and there is no paging-order risk.
MAX_FEATURES = 5000
# Accepted by the server (M0 #6). The second key makes ties deterministic; sorting
# on productName is the one that 400s.
SORT_BY = "acquisitionDate D,featureId A"
# How Vivid/BASE_MOSAIC are excluded server-side. Both CQL forms returned the
# identical 5 of 6 Iliamna records in M0; "none" leaves it to the client.
PRODUCT_CLAUSES = ("not_in", "neq", "none")

# Sizes the server is verified to answer (M0: 2048² WMS GeoTIFF, ~7 s, 16.8 MB).
MAX_WMS_PX = 2048
_WMS_FORMATS = ("image/png", "image/jpeg", "image/geotiff", "image/vnd.jpeg-png")

# Services, for the error map. A 401 means "key not sent" ONLY on these.
STREAMING_SERVICES = frozenset({"wfs", "wms", "wmts", "wcs", "seamlines"})
SERVICES = STREAMING_SERVICES | {"discovery", "usage", "apikey", "analytics"}
SERVICE_LABEL = {
    "wfs": "catalog (WFS)", "wms": "imagery (WMS)", "wmts": "tile streaming (WMTS)",
    "wcs": "coverage download (WCS)", "seamlines": "Vivid seamlines",
    "discovery": "Discovery catalog", "usage": "usage / quota",
    "apikey": "API-key management", "analytics": "Raster Analytics",
}

# Third-party vendors as the credit line should name them. Kept distinct from
# Vantor's own sensors: their imagery carries a separate licence.
THIRD_PARTY_VENDORS = {
    "UMBRA": "Umbra", "ICEYE": "ICEYE", "CAPELLA": "Capella Space",
    "SKYSAT": "Planet Labs (SkySat)", "BLACKSKY": "BlackSky",
}
# Vantor is Maxar's name since 2025; the server still says "Maxar" in layer names.
OWN_VENDOR = "Vantor"


# ---------------------------------------------------------------- time

def _as_utc(d):
    """Aware UTC datetime. A NAIVE datetime is taken to be UTC already.

    That is the plugin's convention (``Detection.origin_utc`` is naive UTC, and
    the dock keeps the UTC wall clock in a LocalTime-spec QDateTime). Never
    ``.astimezone()`` a naive value: Python would read it as the Mac's local
    time and move the pre/post boundary 8-9 h in Alaska."""
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


_FRACTION = re.compile(r"(\.\d+)(?=[+-]\d\d:?\d\d$|$)")


def parse_time(value):
    """ISO-8601 string (or datetime) -> aware UTC datetime, or None.

    Takes what GEGD sends: '2026-08-01T00:18:47.500Z', '2026-10-11T07:36:38Z' and
    the zone-less '2021-04-01T00:36:53.000000' of the mosaic time fields (UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    s = str(value).strip()
    if not s:
        return None
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    # Python < 3.11 accepts only 3 or 6 fractional digits; pad/trim to 6.
    s = _FRACTION.sub(lambda m: "." + (m.group(1)[1:] + "000000")[:6], s)
    try:
        return _as_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _iso_z(t, ceil=False):
    """CQL timestamp in whole UTC seconds. Lower bounds floor, exclusive upper
    bounds ceil, so the window and the nearest-outside queries tile the time
    axis with no gap and no overlap."""
    if not isinstance(t, datetime):
        raise TypeError(f"expected a datetime, got {type(t).__name__}")
    u = _as_utc(t)
    if ceil and u.microsecond:
        u = u + timedelta(seconds=1)
    return u.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the strip

@dataclass(frozen=True)
class Strip:
    """One catalog record of ``Maxar:FinishedFeature``, as the tab uses it.

    Datetimes are aware UTC. Numbers are None when the record lacks them (older
    records have no processingLevel; SAR has no sun). ``cloud`` is the server's
    strip-wide FRACTION (0.007 means 0.7 %), never an AOI measurement.
    ``rings`` holds the exterior ring of each footprint part as (lon, lat)
    tuples; GEGD footprints have no holes."""
    feature_id: str = ""
    catalog_id: str = None          # legacyIdentifier (display only; never decoded)
    acq_utc: datetime = None
    created_utc: datetime = None    # when it went online (median 2.3 h after collect)
    source: str = None              # raw: 'WV02', 'WV03_VNIR', 'UMBRA'
    sensor: str = None              # normalised: 'WV03_VNIR' -> 'WV03'
    product_name: str = None
    sensor_type: str = None         # 'Optical' | 'Radar' | None
    is_third_party: bool = False
    vendor: str = None              # for the credit line: 'Vantor', 'Umbra', …
    ona: float = None               # off-nadir angle, degrees
    sun_el: float = None
    sun_az: float = None
    cloud: float = None             # fraction 0-1, whole strip
    gsd_m: float = None
    band_config: str = None         # 'RGB' (pansharpened) | 'P' (WV01 pan) | …
    processing_level: str = None    # 'LV3D' fine DEM | 'LV2A' coarse DEM | None
    dem_correction: str = None
    ce90: float = None              # metres; None where the server's value is junk
    earliest_utc: datetime = None
    latest_utc: datetime = None
    multi_date: bool = False        # mosaic tile: pixels may come from several dates
    copyright: str = None
    license_type: str = None
    rings: tuple = ()

    @property
    def label(self):
        """'2026-08-01 WV01' — the short handle used in names and messages."""
        day = self.acq_utc.strftime("%Y-%m-%d") if self.acq_utc else "????-??-??"
        return f"{day} {self.sensor or '?'}"

    @property
    def ingest_lag_h(self):
        """Hours from collection to online, or None."""
        if self.acq_utc is None or self.created_utc is None:
            return None
        return (self.created_utc - self.acq_utc).total_seconds() / 3600.0


# ---------------------------------------------------------------- errors

KINDS = ("key_not_sent", "not_entitled", "wrong_auth_scheme", "key_rejected",
         "endpoint_moved", "endpoint_retired", "rate_limited", "server_busy",
         "ogc_exception", "query_error", "usage_limit", "schema_drift",
         "empty_image", "network")
# gegd_net retries these (5/10/20 s, honouring Retry-After); nothing else.
RETRYABLE = ("rate_limited", "server_busy", "network")


def _message(kind, service, status, detail):
    """The sentence the analyst reads. It says what to DO, and names the service:
    "401" alone sends people re-pasting a key that is fine."""
    svc = SERVICE_LABEL.get(service, service or "GEGD")
    det = f": {detail}" if detail else ""
    if kind == "key_not_sent":
        return ("GEGD Pro did not receive a valid key (HTTP 401): it was not sent, is "
                "mistyped, or has expired. Paste a fresh key in the GEGD Pro "
                "connection box and press Save key.")
    if kind == "not_entitled":
        return (f"The GEGD {svc} service is not available to API keys "
                f"(HTTP {status}). Nothing is wrong with the key; this tab does not "
                "need that service.")
    if kind == "wrong_auth_scheme":
        return ("GEGD read the key as a Bearer token (HTTP 403 'Malformed token'): "
                "the auth config is wrong. It must send the header maxar-api-key; "
                "press Save key again to rebuild it.")
    if kind == "key_rejected":
        return (f"GEGD refused the key for {svc} (HTTP 403): expired, revoked, or "
                f"outside its entitlement{det}. Check it in the GEGD Pro web app.")
    if kind == "endpoint_moved":
        return (f"GEGD {svc} was not found at its usual address (HTTP {status}): the "
                "service has moved. The plugin needs an update (paths are in "
                "gegd_api.ENDPOINTS).")
    if kind == "endpoint_retired":
        return (f"GEGD has retired the {svc} endpoint (HTTP 410). The plugin needs "
                "an update.")
    if kind == "rate_limited":
        return ("GEGD is rate-limiting this key (HTTP 429). Wait a minute and try "
                "again.")
    if kind == "server_busy":
        return (f"GEGD {svc} had a server error (HTTP {status}). It is usually "
                "temporary: try again in a minute.")
    if kind == "ogc_exception":
        return f"GEGD {svc} returned an error{det}"
    if kind == "query_error":
        return (f"GEGD could not run the {svc} query (HTTP {status}{det}). This is "
                "a request the plugin built wrongly, not a problem with your key.")
    if kind == "usage_limit":
        return (f"GEGD reports a usage limit{det}. Downloads are stopped; streaming "
                "may also be refused until it resets.")
    if kind == "schema_drift":
        return (f"GEGD's catalog format has changed{det}. The plugin needs an "
                "update before it can read search results.")
    if kind == "empty_image":
        return ("The strip does not cover this box (or the key is not entitled to "
                "it): GEGD returned a fully transparent image.")
    return f"Could not reach GEGD Pro{det}. Check the network connection."


class GegdError(Exception):
    """A GEGD failure, already classified and already redacted.

    ``kind`` is one of KINDS; ``user_message`` is the sentence to show;
    ``detail`` is the server's own text (OGC exception, first line of the
    body); ``status``/``service`` say where it came from. Every string passes
    through redact() on the way in, so it is safe to log or show."""

    def __init__(self, kind, user_message=None, *, detail="", status=None,
                 service=None, key=None):
        if kind not in KINDS:
            raise ValueError(f"unknown GegdError kind {kind!r}")
        self.kind = kind
        self.status = status
        self.service = service
        self.detail = _squash(detail, key=key)          # redacted before it is cut
        if user_message is None:
            user_message = _message(kind, service, status, self.detail)
        self.user_message = redact(user_message, key)
        super().__init__(self.user_message)

    @property
    def retryable(self):
        return self.kind in RETRYABLE

    def __reduce__(self):
        # copy/pickle would otherwise call GegdError(user_message) and fail on
        # an unknown kind; rebuild from the already-redacted fields instead.
        return (_rebuild, (type(self), self.__dict__.copy()))


def _rebuild(cls, state):
    err = Exception.__new__(cls)
    Exception.__init__(err, state.get("user_message", ""))
    err.__dict__.update(state)
    return err


class SchemaDrift(GegdError):
    """The catalog no longer looks like what the plugin was written against.

    ``missing`` names the required attributes that were absent, so the message
    says exactly what moved instead of "no results"."""

    def __init__(self, missing=(), detail="", user_message=None):
        self.missing = tuple(missing)
        if self.missing and not detail:
            detail = "missing " + ", ".join(self.missing)
        super().__init__("schema_drift", user_message, detail=detail, service="wfs")


# What a server says when it could not read the CQL: GeoServer's "Could not parse
# CQL filter list. Encountered "NOT" …", anything naming the attribute or the
# operator, and GEGD's own generic 400 "Bad data query." (what sorting on
# productName returns, so the likeliest wording if NOT IN ever breaks too).
_CLAUSE_ERR_RE = re.compile(r"cql|filter|pars(e|ing)|productname|not\s+in|<>|bad data query",
                            re.I)


def clause_fallback(err, current):
    """The next product-exclusion form to retry with after a query failure, or None.

    One retry per form: NOT IN -> a chain of <> -> leave it to the client. Only a
    failure that reads like the server could not parse the FILTER qualifies (a
    4xx or an OGC exception whose text says CQL / filter / parse / productName /
    NOT IN / <> / "Bad data query"). An auth error, a server hiccup, or an OGC
    exception about something else never changes the query: falling back on one
    of those would download Vivid's 3280-vertex footprints for nothing."""
    if not isinstance(err, GegdError) or err.kind not in ("query_error", "ogc_exception"):
        return None
    if not _CLAUSE_ERR_RE.search(err.detail or ""):
        return None
    order = list(PRODUCT_CLAUSES)
    if current not in order or current == order[-1]:
        return None
    return order[order.index(current) + 1]


# ---------------------------------------------------------------- redaction

REDACTED = "<KEY>"
# The header or query form with its value: 'maxar-api-key: X', 'maxar_api_key=X',
# "'maxar-api-key': b'X'", and URL-encoded 'maxar_api_key%3DX'.
_KEYVAL_RE = re.compile(
    r"(maxar[-_ ]?api[-_ ]?key)"
    r"((?:[\"']|%22|%27)?\s*(?:[:=]|%3[aAdD])(?:\s|%20|\+)*(?:b?(?:[\"']|%22|%27))?)"
    r"([^\s\"'&;,)}\]<>]+)", re.I)
_AUTHZ_RE = re.compile(
    r"(authorization[\"']?\s*[:=]\s*(?:b?[\"'])?)"
    r"((?:bearer|apikey|api-key|basic|token)\s+)?([^\s\"'&;,)}\]<>]+)", re.I)
# A long base64 run: the GEGD key's shape (base64 of a UUID, 48 characters).
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-=")
# base64 of a UUID's text: 48 characters for the dashed form (36 bytes), 43 plus
# one '=' for the bare 32-hex form. The window scan in _scrub_run tries both.
_B64_UUID_LENS = (48, 43)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                      r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def _b64_uuid(token):
    """The UUID a base64 token decodes to, or None (std and URL-safe alphabets)."""
    t = token.rstrip("=")
    for alt in (None, b"-_"):
        try:
            raw = base64.b64decode(t + "=" * (-len(t) % 4), altchars=alt, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            s = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            continue
        if _UUID_RE.match(s):
            return s
    return None


def _key_forms(key):
    """Every spelling of the key worth scrubbing, longest first."""
    k = str(key).strip()
    if len(k) < 8:                    # too short to replace without mangling text
        return []
    forms = {k, quote(k, safe=""), quote_plus(k)}
    u = _b64_uuid(k)
    if u:
        forms |= {u, u.lower(), u.upper(), u.replace("-", ""),
                  u.replace("-", "").lower(), u.replace("-", "").upper()}
    return sorted((f for f in forms if f), key=len, reverse=True)


def _scrub_run(run):
    """One base64-alphabet run with every key-shaped stretch inside it replaced.

    The whole run first (the key on its own). Otherwise every 48- and 43-character
    window: '/', '-', '_' and '+' are base64 characters too, so a key in a URL path
    (``/keys/<K>/usage``) or glued to a word (``<K>-expired``, ``x_<K>``) sits
    INSIDE a longer run that does not decode as a whole. Error-sized text costs a
    few hundred tiny decodes at most."""
    if _b64_uuid(run):
        return REDACTED
    out, i, last, n = [], 0, 0, len(run)
    while i + min(_B64_UUID_LENS) <= n:
        hit = next((w for w in _B64_UUID_LENS
                    if i + w <= n and _b64_uuid(run[i:i + w])), 0)
        if hit:
            out.append(run[last:i])
            out.append(REDACTED)
            i = last = i + hit
        else:
            i += 1
    out.append(run[last:])
    return "".join(out)


def redact(text, key=None):
    """``text`` with every trace of an API key replaced by ``<KEY>``.

    Scrubs, in order: the literal ``key`` and its URL-encoded and base64-decoded
    (UUID) spellings when the caller has it; any ``maxar-api-key: …`` /
    ``maxar_api_key=…`` header, dict or query form; any ``Authorization:`` value;
    and, with no key to compare against, any stretch of a 40+ character base64 run
    that decodes to a UUID, which is the key's shape — also when the key is glued
    into a longer run (a URL path, ``<K>-expired``). bytes in, str out; None -> ''.

    Redact BEFORE cutting text short (``_squash`` does): a key cut in half leaves a
    prefix too short to recognise."""
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    s = str(text)
    if key:
        for f in _key_forms(key):
            s = s.replace(f, REDACTED)
    s = _KEYVAL_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, s)
    s = _AUTHZ_RE.sub(lambda m: m.group(1) + (m.group(2) or "") + REDACTED, s)
    s = _B64_RUN_RE.sub(lambda m: _scrub_run(m.group(0)), s)
    return s


def _cut(s, n):
    """``s[:n]``, backed off to the start of a base64 run that straddles ``n``.

    The cut can then never leave the head of a key behind: a run is kept whole or
    not at all, so redact() always sees a key complete."""
    if len(s) <= n:
        return s
    if s[n] in _B64_CHARS:
        while n > 0 and s[n - 1] in _B64_CHARS:
            n -= 1
    return s[:n]


def _squash(text, limit=500, key=None):
    """One line, whitespace collapsed, redacted, bounded — for error details.

    Redacted BEFORE it is shortened, and shortened only between base64 runs
    (``_cut``), so a key that straddled the limit is dropped whole instead of
    leaving an 18-character prefix in a message. Only the head that can survive
    the limit is scanned, so a megabyte body costs nothing."""
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    s = redact(_cut(" ".join(str(text).split()), limit + 64), key)
    # Every key is already <KEY> here, so a plain cut can only split harmless text.
    return s if len(s) <= limit else s[:limit - 1] + "…"


# ---------------------------------------------------------------- OGC exceptions

_EXC_TEXT_TAGS = ("ExceptionText", "ServiceException")
_EXC_ROOTS = ("ExceptionReport", "ServiceExceptionReport", "Exception",
              "ServiceException")
_EXC_FALLBACK_RE = re.compile(
    r"<(?:[\w.-]+:)?(?:ExceptionText|ServiceException)\b[^>]*>(.*?)</", re.S)


def _text(body, limit=1_000_000):
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body[:limit]).decode("utf-8", "replace")
    return str(body)[:limit]


def _looks_xml(text, ctype=""):
    return "xml" in (ctype or "").lower() or text.lstrip("﻿ \t\r\n").startswith("<")


def parse_ogc_exception(body):
    """The message inside an OGC exception document, or None if it is not one.

    Handles OWS 1.1 ``ExceptionReport/Exception/ExceptionText`` (WFS/WCS 2.0,
    and GEGD's own 400 "Bad data query.") and WMS ``ServiceExceptionReport/
    ServiceException`` (1.1.1 bare, 1.3.0 namespaced). A WFS hits document or
    any other XML is not an exception: None. A document carrying a DTD is never
    handed to the XML parser (entity expansion); the tag text is read with a
    regex instead."""
    text = _text(body)
    if not text or "<" not in text or "Exception" not in text:
        return None
    msgs = []
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        root = None
    else:
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            root = None
    if root is not None:
        local = root.tag.rsplit("}", 1)[-1]
        if local not in _EXC_ROOTS:
            return None
        codes = []
        for el in root.iter():
            name = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            if name in _EXC_TEXT_TAGS:
                t = " ".join("".join(el.itertext()).split())
                if t:
                    msgs.append(t)
            code = el.get("exceptionCode") or el.get("code")
            if code:
                codes.append(code)
        if not msgs:
            msgs = codes or ["unspecified OGC exception"]
    else:
        msgs = [" ".join(m.split()) for m in _EXC_FALLBACK_RE.findall(text)]
        msgs = [m for m in msgs if m]
        if not msgs:
            return None
    seen, out = set(), []
    for m in msgs:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return _squash("; ".join(out))


# ---------------------------------------------------------------- classification

# Quota/credit wording. Deliberately NOT bare "limit"/"exceeded": a WMS size error
# ("Maximum image size exceeded") must stay an OGC exception so the clip code can
# halve its tile, rather than stopping every download for the day.
_USAGE_RE = re.compile(r"\b(usage|quota|credits?|(?:daily|monthly|request|download)"
                       r"\s+limit)\b", re.I)


def classify_http(service, status, body, ctype=None):
    """GegdError for a failed (or failed-inside-200) response, None for a good one.

    ``service`` is one of SERVICES; the same status means different things on
    different services (see the module docstring). An OGC exception inside an
    HTTP 200 is a failure too. ``status`` 0/None means no HTTP response at all
    (DNS, TLS, timeout)."""
    if service not in SERVICES:
        raise ValueError(f"unknown GEGD service {service!r}; one of {sorted(SERVICES)}")
    ct = (ctype or "").lower()
    streaming = service in STREAMING_SERVICES
    if not status:
        # Not pre-cut: _squash redacts, then bounds it (a cut here could halve a key).
        return GegdError("network", detail=_text(body), status=status, service=service)
    status = int(status)

    if 200 <= status < 300:
        # Only an XML body can be an OGC exception. Sniff its first bytes rather
        # than trust the content type: an exception labelled image/png would
        # otherwise pass as a picture, and a good tile (megabytes) is never decoded.
        if not _looks_xml(_text(body, 512), ct):
            return None
        exc = parse_ogc_exception(body)
        if exc is None:
            return None
        kind = "usage_limit" if _USAGE_RE.search(exc) else "ogc_exception"
        return GegdError(kind, detail=exc, status=status, service=service)

    text = _text(body)
    detail = parse_ogc_exception(text) if _looks_xml(text, ct) else None
    if detail is None:
        detail = _squash(text.strip().splitlines()[0] if text.strip() else "", 300)
    low = text.lower()

    if status == 401:
        # Streaming: "No JWT bearer token found … or API Key provided" (or any 401)
        # means the header did not arrive. Elsewhere the same text is the server
        # saying API keys are not accepted there at all.
        kind = "key_not_sent" if streaming else "not_entitled"
        return GegdError(kind, detail=detail, status=status, service=service)
    if status == 403:
        if "malformed token" in low:
            return GegdError("wrong_auth_scheme", detail=detail, status=status,
                             service=service)
        if _USAGE_RE.search(detail or ""):
            return GegdError("usage_limit", detail=detail, status=status, service=service)
        kind = "key_rejected" if streaming else "not_entitled"
        return GegdError(kind, detail=detail, status=status, service=service)
    if status == 410:
        return GegdError("endpoint_retired", detail=detail, status=status, service=service)
    if status == 404 or 300 <= status < 400:
        # A 3xx surfaces only because HostGuard sets ManualRedirectPolicy on
        # keyed requests: following it could carry the key off-host.
        return GegdError("endpoint_moved", detail=detail, status=status, service=service)
    if status == 429:
        return GegdError("rate_limited", detail=detail, status=status, service=service)
    if status >= 500:
        return GegdError("server_busy", detail=detail, status=status, service=service)
    if 400 <= status < 500:
        if _USAGE_RE.search(detail or ""):
            return GegdError("usage_limit", detail=detail, status=status, service=service)
        return GegdError("query_error", detail=detail, status=status, service=service)
    return GegdError("network", detail=detail, status=status, service=service)


# ---------------------------------------------------------------- URL builders

_HOST_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?$")
# featureId is a UUID; legacyIdentifier is 16 alphanumerics. The CHARACTERS are
# what matter: anything else is refused rather than quoted, because these go
# inside CQL string literals AND inside an XYZ template where '&' would end the
# parameter and a quote would end the literal.
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _base(host, path_key):
    h = (host or HOST_DEFAULT).strip().rstrip("/")
    if not _HOST_RE.match(h):
        # No path, query or user-info: a host with "user:pass@" or "?x=" in it
        # would put credentials or junk into every saved layer URL.
        raise ValueError(f"GEGD host must look like https://name[:port], got {h!r}")
    if path_key not in ENDPOINTS:
        raise ValueError(f"unknown GEGD endpoint {path_key!r}")
    return h + ENDPOINTS[path_key]


def _q(v):
    # The encoding the live probes used: quotes, spaces, parentheses and >= are
    # escaped; ':' '/' ',' stay readable (typeNames, srs, CRS:84, bbox lists).
    return quote(str(v), safe=":/,")


def _kvp(base, params):
    return base + "?" + "&".join(f"{k}={_q(v)}" for k, v in params)


def _fmt(x):
    """Coordinate text: up to 7 decimals (~1 cm), no trailing zeros, no '-0'."""
    s = f"{float(x):.7f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def _check_bbox(bbox):
    """(lonmin, latmin, lonmax, latmax) as floats, or ValueError.

    Catches the axis swap that silently returns nothing: a lat-first tuple puts a
    longitude like -153 in a latitude slot, which is out of range."""
    try:
        lonmin, latmin, lonmax, latmax = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise ValueError("bbox must be four numbers (lonmin, latmin, lonmax, latmax)")
    vals = (lonmin, latmin, lonmax, latmax)
    if not all(math.isfinite(v) for v in vals):
        raise ValueError(f"bbox has a non-finite value: {vals}")
    if not (-180.0 <= lonmin < lonmax <= 180.0):
        raise ValueError(f"bbox longitudes out of order or range (lon first!): {vals}")
    if not (-90.0 <= latmin < latmax <= 90.0):
        raise ValueError(f"bbox latitudes out of order or range (lat second!): {vals}")
    return vals


def bbox_cql(bbox):
    """The ONLY spatial filter form the server reads unambiguously (lon first)."""
    lonmin, latmin, lonmax, latmax = _check_bbox(bbox)
    return (f"BBOX(featureGeometry,{_fmt(lonmin)},{_fmt(latmin)},"
            f"{_fmt(lonmax)},{_fmt(latmax)},'CRS:84')")


def product_cql(product_clause="not_in"):
    """The Vivid/BASE_MOSAIC exclusion in the chosen form ('' for 'none')."""
    if product_clause == "not_in":
        return "productName NOT IN (" + ",".join(f"'{p}'" for p in EXCLUDED_PRODUCTS) + ")"
    if product_clause == "neq":
        return " AND ".join(f"productName<>'{p}'" for p in EXCLUDED_PRODUCTS)
    if product_clause == "none":
        return ""
    raise ValueError(f"product_clause must be one of {PRODUCT_CLAUSES}, got {product_clause!r}")


def _ident(ident):
    s = str(ident or "").strip()
    if not _IDENT_RE.match(s):
        raise ValueError(f"not a GEGD featureId or catalog id: {ident!r}")
    return s


def _and(*parts):
    return " AND ".join(p for p in parts if p)


def wfs_getfeature_url(host, bbox, t0, t1, count=MAX_FEATURES, product_clause="not_in"):
    """The event search: every FinishedFeature in ``bbox`` with t0 <= acq < t1.

    One page of up to ``count`` (the server's 5000 cap), newest first, as
    GeoJSON. ``bbox`` is (lonmin, latmin, lonmax, latmax); t0/t1 are datetimes
    (naive = UTC)."""
    count = int(count)
    if not 1 <= count <= MAX_FEATURES:
        raise ValueError(f"count must be 1..{MAX_FEATURES}, got {count}")
    cql = _and(bbox_cql(bbox),
               f"acquisitionDate>='{_iso_z(t0)}'",
               f"acquisitionDate<'{_iso_z(t1, ceil=True)}'",
               product_cql(product_clause))
    return _kvp(_base(host, "wfs"), [
        ("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
        ("typeNames", FEATURE_TYPE), ("outputFormat", "application/json"),
        ("count", count), ("sortBy", SORT_BY), ("cql_filter", cql)])


def wfs_hits_url(host, bbox, t0, t1, product_clause="not_in"):
    """Count only (``resultType=hits``; the answer is ``numberMatched``). Used as
    the canary that tells "no coverage here" from "query broken"."""
    cql = _and(bbox_cql(bbox),
               f"acquisitionDate>='{_iso_z(t0)}'",
               f"acquisitionDate<'{_iso_z(t1, ceil=True)}'",
               product_cql(product_clause))
    return _kvp(_base(host, "wfs"), [
        ("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
        ("typeNames", FEATURE_TYPE), ("resultType", "hits"), ("cql_filter", cql)])


def wfs_by_id_url(host, ident):
    """Find one strip by featureId OR catalog id (legacyIdentifier); the OR form
    returned exactly the two strips asked for in M0. No product exclusion: a
    strip asked for by id is shown whatever it is."""
    i = _ident(ident)
    return _kvp(_base(host, "wfs"), [
        ("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
        ("typeNames", FEATURE_TYPE), ("outputFormat", "application/json"),
        ("count", 10), ("sortBy", SORT_BY),
        ("cql_filter", f"featureId='{i}' OR legacyIdentifier='{i}'")])


def wfs_nearest_url(host, bbox, t, side, product_clause="not_in", count=1):
    """The nearest strip OUTSIDE the search window on one side.

    side 'pre': acq < t, newest first (pass the window start t0).
    side 'post': acq >= t, oldest first (pass the window end t1).
    Feeds "nearest earlier strip 2025-06-14 (WV02, ONA 22°)" when a side is empty."""
    if side == "pre":
        cond, sort = f"acquisitionDate<'{_iso_z(t)}'", "acquisitionDate D,featureId A"
    elif side == "post":
        cond, sort = f"acquisitionDate>='{_iso_z(t, ceil=True)}'", "acquisitionDate A,featureId A"
    else:
        raise ValueError(f"side must be 'pre' or 'post', got {side!r}")
    count = int(count)
    if not 1 <= count <= MAX_FEATURES:
        raise ValueError(f"count must be 1..{MAX_FEATURES}, got {count}")
    return _kvp(_base(host, "wfs"), [
        ("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
        ("typeNames", FEATURE_TYPE), ("outputFormat", "application/json"),
        ("count", count), ("sortBy", sort),
        ("cql_filter", _and(bbox_cql(bbox), cond, product_cql(product_clause)))])


def wms_thumb_url(host, fid, bbox, w, h, fmt="image/png"):
    """One strip, rendered over ``bbox`` (lon/lat), transparent outside its footprint.

    WMS 1.1.1 with EPSG:4326 is always x,y = lon,lat, so the bbox order matches
    the CQL one. Exceptions come back as XML (se_xml) instead of a blank image."""
    lonmin, latmin, lonmax, latmax = _check_bbox(bbox)
    w, h = int(w), int(h)
    if not (1 <= w <= MAX_WMS_PX and 1 <= h <= MAX_WMS_PX):
        raise ValueError(f"WMS size must be 1..{MAX_WMS_PX} px a side, got {w}x{h}")
    if fmt not in _WMS_FORMATS:
        raise ValueError(f"fmt must be one of {_WMS_FORMATS}, got {fmt!r}")
    return _kvp(_base(host, "wms"), [
        ("service", "WMS"), ("version", "1.1.1"), ("request", "GetMap"),
        ("layers", LAYER), ("styles", ""), ("srs", "EPSG:4326"),
        ("bbox", ",".join(_fmt(v) for v in (lonmin, latmin, lonmax, latmax))),
        ("width", w), ("height", h), ("format", fmt), ("transparent", "true"),
        ("exceptions", "application/vnd.ogc.se_xml"),
        ("cql_filter", f"featureId='{_ident(fid)}'")])


def wmts_xyz_template(host, fid, path_key="wmts"):
    """XYZ template over WMTS GetTile for ONE strip, with literal {z}/{x}/{y}.

    For ``QgsDataSourceUri.setParam("url", …)`` (which does the encoding) — NOT
    for the WMTS provider, which silently drops CQL_FILTER and would show the
    newest-on-top mosaic instead of the chosen strip. Left unencoded on purpose:
    a percent-encoded template gives a "valid" layer that draws nothing."""
    if path_key not in ("wmts", "wmts_alt"):
        raise ValueError(f"path_key must be 'wmts' or 'wmts_alt', got {path_key!r}")
    return (_base(host, path_key)
            + "?service=WMTS&request=GetTile&version=1.0.0"
            + f"&layer={LAYER}&style=raster&tilematrixset=EPSG:3857"
            + "&tilematrix=EPSG:3857:{z}&tilerow={y}&tilecol={x}&format=image/png"
            + f"&CQL_FILTER=featureId='{_ident(fid)}'")


# ---------------------------------------------------------------- parsing

def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _sensor(props):
    raw = _str(props.get("source")) or _str(props.get("sensorCode")) \
        or _str(props.get("sensorName")) or ""
    s = raw.upper()
    # 'WV03_VNIR' and friends are the same platform; keep the platform.
    s = re.sub(r"_(VNIR|SWIR|PAN|MS|CAVIS)$", "", s)
    return s or None


def _rings(geom):
    """Exterior ring of each footprint part, as tuples of (lon, lat)."""
    if not geom:
        return ()
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        raise SchemaDrift(detail=f"featureGeometry is a {gtype!r}, not a polygon")
    out = []
    try:
        for poly in polys or []:
            if not poly:
                continue
            ring = tuple((float(p[0]), float(p[1])) for p in poly[0])
            if len(ring) >= 3:
                out.append(ring)
    except (TypeError, ValueError, IndexError):
        raise SchemaDrift(detail="featureGeometry coordinates are unreadable")
    return tuple(out)


def _strip(props, geom):
    product = _str(props.get("productName"))
    pu = (product or "").upper()
    sensor = _sensor(props)
    third = bool(props.get("isThirdParty")) or pu == THIRD_PARTY_PRODUCT
    vendor = _str(props.get("vendorName")) or _str(props.get("sourceCompanyName"))
    if not vendor:
        vendor = THIRD_PARTY_VENDORS.get(sensor or "", sensor) if third else OWN_VENDOR

    level = _str(props.get("processingLevel"))
    ce90 = _num(props.get("ce90Accuracy"))
    # LV2A reports ce90Accuracy=0.0014: junk, and a CE90 shown beside a coarse-DEM
    # strip would read as a promise. Sub-half-metre CE90 is not physical here.
    if ce90 is not None and ((level or "").upper() == "LV2A" or ce90 < 0.5):
        ce90 = None

    acq = parse_time(props.get("acquisitionDate"))
    if acq is None:
        raise SchemaDrift(detail=f"unreadable acquisitionDate {props.get('acquisitionDate')!r}")
    earliest = parse_time(props.get("earliestAcquisitionTime"))
    latest = parse_time(props.get("latestAcquisitionTime"))
    span = (latest - earliest).total_seconds() if earliest and latest else 0.0
    multi = (abs(span) > MULTI_DATE_TOL_S or props.get("outputMosaic") is True
             or pu in MOSAIC_PRODUCTS)

    return Strip(
        feature_id=str(props.get("featureId")).strip(),
        catalog_id=_str(props.get("legacyIdentifier")),
        acq_utc=acq,
        created_utc=parse_time(props.get("createdDate")),
        source=_str(props.get("source")),
        sensor=sensor,
        product_name=product,
        sensor_type=_str(props.get("sensorType")),
        is_third_party=third,
        vendor=vendor,
        ona=_num(props.get("offNadirAngle")),
        sun_el=_num(props.get("sunElevation")),
        sun_az=_num(props.get("sunAzimuth")),
        cloud=_num(props.get("cloudCover")),
        gsd_m=_num(props.get("groundSampleDistance")),
        band_config=_str(props.get("bandConfiguration")),
        processing_level=level,
        dem_correction=_str(props.get("demCorrection")),
        ce90=ce90,
        earliest_utc=earliest,
        latest_utc=latest,
        multi_date=multi,
        copyright=_str(props.get("copyright")),
        license_type=_str(props.get("licenseType")),
        rings=_rings(geom),
    )


def parse_features(body):
    """GeoJSON FeatureCollection bytes -> [Strip], in server order.

    Raises SchemaDrift when the response is not a FeatureCollection, or when any
    feature lacks a REQUIRED_FIELDS attribute (one bad record makes the whole
    response suspect: a renamed field would otherwise read as "no imagery").
    An OGC exception document raises GegdError('ogc_exception') instead.
    Filtering, de-duplication and the AOI check are gegd_rank's job."""
    text = _text(body, limit=200_000_000).lstrip("﻿")
    if not text.strip():
        raise SchemaDrift(detail="empty response")
    if text.lstrip().startswith("<"):
        exc = parse_ogc_exception(text)
        if exc is not None:
            raise GegdError("ogc_exception", detail=exc, service="wfs")
        raise SchemaDrift(detail="expected GeoJSON, got XML")
    try:
        doc = json.loads(text)
    except ValueError:
        raise SchemaDrift(detail="response is not JSON")
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise SchemaDrift(detail="no 'features' list in the response")
    missing = []
    for f in feats:
        props = (f or {}).get("properties") if isinstance(f, dict) else None
        if not isinstance(props, dict):
            props = {}
        for k in REQUIRED_FIELDS:
            if props.get(k) in (None, "") and k not in missing:
                missing.append(k)
    if missing:
        raise SchemaDrift(missing=[k for k in REQUIRED_FIELDS if k in missing])
    return [_strip(f["properties"], f.get("geometry")) for f in feats]


_HITS_RE = re.compile(r"\bnumberMatched\s*=\s*[\"'](\d+)[\"']")


def parse_hits(body):
    """``numberMatched`` of a resultType=hits response, or None."""
    m = _HITS_RE.search(_text(body))
    return int(m.group(1)) if m else None


def is_excluded(strip):
    """True for products that must never be a before/after candidate."""
    p = (getattr(strip, "product_name", None) or "").upper()
    return p in EXCLUDED_PRODUCTS or p.startswith("VIVID")
