"""Map layers for the Maxar VHR tab: streamed strips, footprints, the search circle.

Everything here builds or recognises QGIS layers and nothing else: no network, no
widgets. Layers are created on the main thread only (threading rule 4), and the
tab calls ``gegd_auth.ensure_ready()`` first, because ``strip_xyz_uri`` asks
``gegd_auth.mode()`` whether the key travels by auth config or by the fallback
header injector.

**Streams are XYZ layers, never the WMTS provider.** GEGD renders ONE strip when
its WMTS GetTile carries ``CQL_FILTER=featureId='…'``, but QGIS's WMTS provider
silently drops that parameter and draws the newest-on-top mosaic instead — a
"valid" layer showing the wrong date, which is the worst failure a before/after
comparison can have. An XYZ layer whose template is set through
``QgsDataSourceUri.setParam("url", …)`` keeps it (verified offline in QGIS 3.44.11
and live in M0, on both WMTS paths). The WMS provider keeps ``cql_filter`` too and
is the fallback (``strip_wms_uri``), at the price of one GetCapabilities per layer.

**What a saved project holds.** The source of a stream layer is the tile template
plus ``authcfg=gegdpro`` — never the key. A teammate who opens the .qgz renders it
with THEIR key under the same id, and the plugin's HostGuard keeps that header off
every host but pro.gegd.com however the source is edited. In the fallback mode (no
APIHeader auth method) the source carries no authcfg at all: a missing auth config
fails a request before any preprocessor can add the header.

**Names carry no ``_pre_`` / ``_post_`` token.** The 3D tab pairs rasters by those
tokens, and a stream is not a file it can drape:
``Maxar before · 2026-08-01 WV01 · ONA 30° (stream)``.

**Recognising our layers again.** Every layer gets ``landslide/gegd_*`` custom
properties (kind, side, featureId, dates, rights), which travel in the .qgz, so
``adopt_layers`` can rebuild the tab's state when a project is reopened without
trusting a layer name the analyst may have edited.

**Rights.** Vantor's own strips and third-party imagery (Umbra, ICEYE, Capella …)
carry different licences, so the credit line is built per strip and written into
the layer metadata (Layer Properties > Metadata > Rights) and a custom property.
GEGD Pro access is for AEC internal use; the line says so.
"""
from qgis.core import (QgsCentroidFillSymbolLayer, QgsDataSourceUri, QgsFeature,
                       QgsField, QgsFillSymbol, QgsGeometry, QgsMarkerSymbol,
                       QgsPointXY, QgsRasterLayer, QgsRuleBasedRenderer,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QVariant

try:                                    # Qgis.SelectionRenderingMode: QGIS >= 3.34
    from qgis.core import Qgis
except ImportError:                     # pragma: no cover (very old QGIS)
    Qgis = None

from . import gegd_api as api
from . import gegd_auth
from . import gegd_rank as R

__all__ = [
    "PROP", "strip_xyz_uri", "strip_wms_uri", "stream_name", "make_stream_layer",
    "footprint_layer", "aoi_layer", "style_selection", "set_props", "get_prop",
    "set_rights", "credit_line", "adopt_layers",
]

# Custom-property prefix on every layer this module makes ("landslide/gegd_kind" …).
PROP = "landslide/gegd_"
KIND_STREAM, KIND_FOOTPRINTS, KIND_AOI = "stream", "footprints", "aoi"
SIDE_WORD = {"pre": "before", "post": "after"}

# XYZ zoom range. z20 is ~0.08 m/px at 60°N, finer than any strip's 0.3-0.6 m GSD,
# so the deepest zoom still asks for real tiles rather than stretching z19.
ZMIN, ZMAX = 0, 20

# Outline colours: the same blue/green the PlanetScope and SAR footprints use, so
# "before" reads the same in every tab.
PRE_RGB, POST_RGB = "0,90,200", "0,150,60"
AOI_RGB = "200,40,40"
# A selected footprint: QGIS's selection yellow, but as an outline only. The default
# selection rendering FILLS the polygon with translucent yellow, which veils the very
# strip being looked at.
SELECTED_RGB = "255,210,0"


# ---------------------------------------------------------------- sources

def strip_xyz_uri(fid, host=api.HOST_DEFAULT, path_key="wmts"):
    """The data source of one streamed strip: an XYZ layer over WMTS GetTile.

    ``path_key`` 'wmts' is the GeoWebCache path GEGD advertises; 'wmts_alt' is
    the other path, which returned byte-identical tiles in M0 and is the drift
    fallback. The template is passed UNencoded to setParam, which does the
    encoding: a pre-encoded template gives a valid layer that draws nothing."""
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", api.wmts_xyz_template(host, fid, path_key))
    uri.setParam("zmin", str(ZMIN))
    uri.setParam("zmax", str(ZMAX))
    if gegd_auth.mode() == "authcfg":
        uri.setAuthConfigId(gegd_auth.AUTHCFG_ID)
    return bytes(uri.encodedUri()).decode()


def strip_wms_uri(fid, host=api.HOST_DEFAULT):
    """The WMS fallback for one strip (``cql_filter`` in the base URL).

    ``IgnoreGetMapUrl`` keeps QGIS on our URL (and our filter) instead of the
    GetMap address the capabilities advertise; it costs one GetCapabilities per
    layer, which is why XYZ is the default."""
    # The id is validated by the same rule every GEGD URL builder applies: it
    # lands inside a CQL string literal.
    url = api._base(host, "wms") + f"?cql_filter=featureId='{api._ident(fid)}'"
    uri = QgsDataSourceUri()
    uri.setParam("url", url)
    uri.setParam("layers", api.LAYER)
    uri.setParam("styles", "")
    uri.setParam("format", "image/png")
    uri.setParam("crs", "EPSG:3857")
    uri.setParam("IgnoreGetMapUrl", "1")
    uri.setParam("dpiMode", "7")
    if gegd_auth.mode() == "authcfg":
        uri.setAuthConfigId(gegd_auth.AUTHCFG_ID)
    return bytes(uri.encodedUri()).decode()


# ---------------------------------------------------------------- properties

def set_props(lyr, **kv):
    """Write ``landslide/gegd_<key>`` custom properties (None becomes '')."""
    for k, v in kv.items():
        if v is None:
            v = ""
        elif not isinstance(v, (str, int, float, bool)):
            v = str(v)
        lyr.setCustomProperty(PROP + k, v)
    return lyr


def get_prop(lyr, key, default=""):
    """A ``landslide/gegd_<key>`` property, or ``default`` (also for a deleted layer)."""
    try:
        v = lyr.customProperty(PROP + key, default)
    except (RuntimeError, AttributeError):
        return default
    return default if v is None else v


def credit_line(strip):
    """'Imagery © 2026 Vantor, via NGA GEGD Pro. AEC internal use only.'

    Third-party strips name their own vendor and say the licence is separate: it
    is a different contract from Vantor's, even when GEGD serves both."""
    year = strip.acq_utc.year if strip.acq_utc else ""
    if strip.is_third_party:
        vendor = strip.vendor or strip.sensor or "third-party vendor"
        return (f"Imagery © {year} {vendor} (third-party, separate licence), via NGA "
                "GEGD Pro. AEC internal use only.")
    return f"Imagery © {year} {api.OWN_VENDOR}, via NGA GEGD Pro. AEC internal use only."


def set_rights(lyr, strip):
    """Vendor-aware rights in the layer metadata, plus the record's own
    ``copyright`` / ``licenseType`` when GEGD supplies them."""
    credit = credit_line(strip)
    rights = [credit]
    if strip.copyright:
        rights.append(f"Catalog copyright: {strip.copyright}")
    if strip.license_type:
        rights.append(f"Catalog licence type: {strip.license_type}")
    md = lyr.metadata()
    md.setRights(rights)
    acq = f"{strip.acq_utc:%Y-%m-%d %H:%M:%S} UTC" if strip.acq_utc else "unknown time"
    bits = [f"Maxar/Vantor GEGD Pro strip {strip.feature_id}",
            f"acquired {acq}", f"sensor {strip.sensor or '?'}"]
    if strip.catalog_id:
        bits.append(f"catalog id {strip.catalog_id}")
    if strip.ona is not None:
        bits.append(f"off-nadir {strip.ona:.1f}°")
    if strip.processing_level:
        bits.append(f"processing {strip.processing_level}")
    md.setAbstract(
        "; ".join(bits) + ". 8-bit DRA-stretched display imagery: for looking and "
        "tracing, never for radiometry (no NIR/SWIR; Fusion refuses it).")
    lyr.setMetadata(md)
    set_props(lyr, credit=credit, copyright=strip.copyright or "",
              license_type=strip.license_type or "", vendor=strip.vendor or "")
    return lyr


# ---------------------------------------------------------------- stream layers

def stream_name(strip, side):
    """'Maxar before · 2026-08-01 WV01 · ONA 30° (stream)'."""
    parts = [f"Maxar {SIDE_WORD.get(side, side)}", strip.label]
    if strip.ona is not None:
        parts.append(f"ONA {strip.ona:.0f}°")
    return " · ".join(parts) + " (stream)"


def make_stream_layer(rstrip, side, host=api.HOST_DEFAULT, path_key="wmts"):
    """One strip as a streamed XYZ raster layer, named, tagged and credited.

    ``rstrip`` is a gegd_rank.RankedStrip (or a bare Strip). Not added to the
    project: the tab places it in its layer-tree group."""
    s = getattr(rstrip, "strip", rstrip)
    lyr = QgsRasterLayer(strip_xyz_uri(s.feature_id, host, path_key),
                         stream_name(s, side), "wms")
    set_props(
        lyr, kind=KIND_STREAM, side=side, fid=s.feature_id,
        catalog_id=s.catalog_id or "",
        acq_utc=s.acq_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if s.acq_utc else "",
        sensor=s.sensor or "", ona="" if s.ona is None else round(float(s.ona), 2),
        level=s.processing_level or "",
        grade=getattr(rstrip, "grade", "") or "",
        shift50="" if getattr(rstrip, "shift50", None) is None else round(rstrip.shift50, 1),
        path_key=path_key)
    set_rights(lyr, s)
    return lyr


# ---------------------------------------------------------------- vectors

def _ring_points(ring):
    return [QgsPointXY(float(x), float(y)) for x, y in ring]


def _footprint_geom(strip):
    rings = [r for r in (strip.rings or ()) if len(r) >= 3]
    if not rings:
        return None
    if len(rings) == 1:
        return QgsGeometry.fromPolygonXY([_ring_points(rings[0])])
    return QgsGeometry.fromMultiPolygonXY([[_ring_points(r)] for r in rings])


FOOTPRINT_FIELDS = (
    ("feature_id", QVariant.String), ("side", QVariant.String),
    ("date_utc", QVariant.String), ("sensor", QVariant.String),
    ("ona", QVariant.Double), ("sun", QVariant.Double),
    ("cover_pct", QVariant.Double), ("grade", QVariant.String),
    ("score", QVariant.Double), ("star", QVariant.Int), ("hidden", QVariant.String),
)


def footprint_layer(ranked, event_id, include_hidden=False):
    """Outline-only footprints of a ranked search (memory layer, EPSG:4326).

    Blue before, green after; ★ strips drawn thicker; visual- and context-grade
    strips dashed (look, don't trace). Rows the Scene filters hide are left out
    unless ``include_hidden``. One feature per strip, keyed by ``feature_id`` so
    table selection and map selection can follow each other."""
    lyr = QgsVectorLayer("Polygon?crs=EPSG:4326", f"Maxar footprints {event_id}".strip(),
                         "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField(n, t) for n, t in FOOTPRINT_FIELDS])
    lyr.updateFields()
    feats = []
    for r in list(getattr(ranked, "pre", []) or []) + list(getattr(ranked, "post", []) or []):
        if r.hidden_reason is not None and not include_hidden:
            continue
        g = _footprint_geom(r.strip)
        if g is None:
            continue
        s = r.strip
        f = QgsFeature(lyr.fields())
        f.setGeometry(g)
        f.setAttributes([
            s.feature_id, r.side,
            s.acq_utc.strftime("%Y-%m-%d %H:%M") if s.acq_utc else "",
            s.sensor or "", s.ona, s.sun_el,
            round(100.0 * float(r.cover or 0.0), 1), r.grade, round(float(r.score), 3),
            1 if r.star else 0, r.hidden_reason or ""])
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()

    root = QgsRuleBasedRenderer.Rule(None)
    for side, rgb, word in (("pre", PRE_RGB, "before"), ("post", POST_RGB, "after")):
        for label, expr, width, style in (
                (f"★ {word}", f"\"side\" = '{side}' AND \"star\" = 1", "1.2", "solid"),
                (word, f"\"side\" = '{side}' AND \"star\" = 0 AND \"grade\" = 'mapping'",
                 "0.6", "solid"),
                (f"{word}, visual or context only",
                 f"\"side\" = '{side}' AND \"star\" = 0 AND \"grade\" <> 'mapping'",
                 "0.5", "dash")):
            sym = QgsFillSymbol.createSimple({
                "style": "no", "outline_color": rgb, "outline_width": width,
                "outline_style": style})
            root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, label))
    lyr.setRenderer(QgsRuleBasedRenderer(root))
    style_selection(lyr)
    set_props(lyr, kind=KIND_FOOTPRINTS, event_id=event_id)
    return lyr


def style_selection(lyr):
    """Draw selected footprints as a thick yellow OUTLINE instead of QGIS's default
    translucent yellow fill, so selecting a row never veils the imagery under it.

    Per-layer selection symbols arrived in QGIS 3.34; older QGIS keeps its default
    fill (Clear selection on the tab still removes it). Returns True when applied."""
    try:
        props = lyr.selectionProperties()
        props.setSelectionSymbol(QgsFillSymbol.createSimple({
            "style": "no", "outline_color": SELECTED_RGB, "outline_width": "1.4"}))
        props.setSelectionRenderingMode(Qgis.SelectionRenderingMode.CustomSymbol)
        return True
    except (AttributeError, NameError, RuntimeError):
        return False


def aoi_layer(lat, lon, r_km, event_id):
    """The search circle with a cross at the epicentre (memory layer, EPSG:4326).

    The same 64-gon gegd_rank measures coverage against, so what the map shows is
    exactly the area a strip had to touch to be listed."""
    lyr = QgsVectorLayer("Polygon?crs=EPSG:4326",
                         f"Maxar search area {event_id} ({float(r_km):g} km)".strip(),
                         "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPolygonXY([_ring_points(R.aoi_disc(lat, lon, r_km))]))
    lyr.dataProvider().addFeatures([f])
    lyr.updateExtents()
    sym = QgsFillSymbol.createSimple({
        "style": "no", "outline_color": AOI_RGB, "outline_width": "0.5",
        "outline_style": "dash"})
    # The disc's centroid IS the epicentre: a centroid marker draws it without a
    # second (point) layer to keep in step.
    centre = QgsCentroidFillSymbolLayer()
    centre.setSubSymbol(QgsMarkerSymbol.createSimple({
        "name": "cross2", "color": AOI_RGB, "outline_color": AOI_RGB, "size": "3"}))
    sym.appendSymbolLayer(centre)
    lyr.renderer().setSymbol(sym)
    set_props(lyr, kind=KIND_AOI, event_id=event_id, lat=float(lat), lon=float(lon),
              r_km=float(r_km))
    return lyr


# ---------------------------------------------------------------- reopen

def adopt_layers(project):
    """The tab's layers in ``project``, found by custom property (not by name).

    Returns ``{"stream": [layers…], "footprints": layer|None, "aoi": layer|None}``,
    streams in layer-tree order (top first) so the tab can tell which pair was on
    top. Used on readProject: the layers themselves already render without the
    dock (their sources carry authcfg only); this hands them back to the tab."""
    out = {"stream": [], "footprints": None, "aoi": None}
    try:
        root = project.layerTreeRoot()
        order = [n.layerId() for n in root.findLayers()]
        layers = project.mapLayers()
    except (RuntimeError, AttributeError):
        return out
    rank = {lid: i for i, lid in enumerate(order)}
    for lid, lyr in sorted(layers.items(), key=lambda kv: rank.get(kv[0], len(rank))):
        kind = get_prop(lyr, "kind")
        if kind == KIND_STREAM:
            out["stream"].append(lyr)
        elif kind == KIND_FOOTPRINTS and out["footprints"] is None:
            out["footprints"] = lyr
        elif kind == KIND_AOI and out["aoi"] is None:
            out["aoi"] = lyr
    return out
