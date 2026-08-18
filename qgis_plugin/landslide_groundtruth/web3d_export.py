"""Export a self-contained WebGL "before/after" 3D viewer from the plugin.

The native QGIS 3D view re-textures the DEM every time the drape changes, so a
before/after flip always reloads tiles. This module writes a standalone HTML
file instead: a DEM mesh with BOTH images uploaded to the GPU as textures up
front, so flipping between them is a zero-cost texture swap — instant, with free
orbit, and no tile loading after the one-time export.

The file is fully self-contained (vanilla WebGL, no external libraries; terrain
and both textures embedded as data), so it opens offline in any browser.

`build_viewer_html(cfg)` is pure-stdlib (json/base64) so it can be unit-tested
without QGIS; the QGIS side (viewer3d_tab) renders the imagery + reads the DEM
and hands the encoded assets here.
"""
import base64
import json
import struct

__all__ = ["build_viewer_html", "encode_heightfield"]


def encode_heightfield(rows):
    """Pack a 2D elevation grid (list of rows, north-first) into base64 Float32.

    Returns (b64, ncols, nrows, zmin, zmax). NaN/None cells are carried as NaN;
    the viewer fills them flat. Kept dependency-free (no numpy) so the same code
    path works in tests and in QGIS."""
    nrows = len(rows)
    ncols = len(rows[0]) if nrows else 0
    flat = []
    zmin = float("inf")
    zmax = float("-inf")
    for r in rows:
        for v in r:
            if v is None:
                flat.append(float("nan"))
                continue
            fv = float(v)
            flat.append(fv)
            if fv == fv:                      # not NaN
                if fv < zmin:
                    zmin = fv
                if fv > zmax:
                    zmax = fv
    if zmin == float("inf"):
        zmin, zmax = 0.0, 0.0
    raw = struct.pack("<%df" % len(flat), *flat)
    return base64.b64encode(raw).decode("ascii"), ncols, nrows, zmin, zmax


def _signed_area(ring):
    a = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a * 0.5


def _in_tri(p, a, b, c):
    def cr(o, u, v):
        return (u[0] - o[0]) * (v[1] - o[1]) - (u[1] - o[1]) * (v[0] - o[0])
    d1, d2, d3 = cr(p, a, b), cr(p, b, c), cr(p, c, a)
    return not (((d1 < 0) or (d2 < 0) or (d3 < 0)) and
                ((d1 > 0) or (d2 > 0) or (d3 > 0)))


def triangulate_ring(ring):
    """Ear-clip a simple polygon ring ([[x,y], ...], not closed) into a flat list
    of triangles [[x,y],[x,y],[x,y], ...] (every 3 points = one triangle).

    Best-effort and dependency-free: handles convex and concave rings; ignores
    holes and self-intersections (bails with what it has). Returns [] if the ring
    is degenerate."""
    pts = [list(p) for p in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts.pop()
    n = len(pts)
    if n < 3:
        return []
    idx = list(range(n))
    if _signed_area(pts) < 0:                 # ensure CCW winding
        idx.reverse()
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 20000:
        guard += 1
        m = len(idx)
        clipped = False
        for k in range(m):
            i0, i1, i2 = idx[(k - 1) % m], idx[k], idx[(k + 1) % m]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) <= 0:
                continue                       # reflex corner, not an ear
            if any(j not in (i0, i1, i2) and _in_tri(pts[j], a, b, c) for j in idx):
                continue                       # another vertex inside
            tris.extend((a, b, c))             # flat point list: every 3 = a tri
            del idx[k]
            clipped = True
            break
        if not clipped:
            break                              # non-simple ring; stop early
    if len(idx) == 3:
        tris.extend((pts[idx[0]], pts[idx[1]], pts[idx[2]]))
    return tris


def densify_ring(ring, step):
    """Insert points along each closed-ring edge so no segment exceeds `step`.

    Draping only samples the terrain at vertices, so a sparse ring's long edges
    cut straight across relief. Densifying makes the OUTLINE hug the surface."""
    n = len(ring)
    if n < 2 or step <= 0:
        return [list(p) for p in ring]
    out = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        out.append([a[0], a[1]])
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = (dx * dx + dy * dy) ** 0.5
        if d > step:
            k = int(d // step)
            for j in range(1, k + 1):
                t = j * step / d
                if t < 1.0:
                    out.append([a[0] + dx * t, a[1] + dy * t])
    return out


def subdivide_tris(tris, step):
    """Longest-edge-bisect flat triangles until every edge <= `step`.

    `tris` is the flat [p0,p1,p2, p3,p4,p5, ...] list from triangulate_ring;
    returns the same flat format with more, smaller triangles. Once each vertex
    is draped onto the terrain, a fine fill CONFORMS to the surface instead of
    spanning flat sheets between the polygon's boundary vertices (which is what
    makes a draped polygon float over a valley)."""
    if step <= 0 or not tris:
        return tris
    s2 = step * step
    out = []
    stack = [(tris[i], tris[i + 1], tris[i + 2])
             for i in range(0, len(tris) - 2, 3)]
    guard = 0
    while stack and guard < 4000000:
        guard += 1
        a, b, c = stack.pop()
        ab = (a[0]-b[0])**2 + (a[1]-b[1])**2
        bc = (b[0]-c[0])**2 + (b[1]-c[1])**2
        ca = (c[0]-a[0])**2 + (c[1]-a[1])**2
        m = max(ab, bc, ca)
        if m <= s2:
            out.extend((a, b, c))
            continue
        if m == ab:
            mid = [(a[0]+b[0])*0.5, (a[1]+b[1])*0.5]
            stack.append((a, mid, c)); stack.append((mid, b, c))
        elif m == bc:
            mid = [(b[0]+c[0])*0.5, (b[1]+c[1])*0.5]
            stack.append((b, mid, a)); stack.append((mid, c, a))
        else:
            mid = [(c[0]+a[0])*0.5, (c[1]+a[1])*0.5]
            stack.append((c, mid, b)); stack.append((mid, a, b))
    return out


def build_viewer_html(cfg):
    """Assemble the standalone viewer HTML from a config dict.

    Required cfg keys: title, before_label, after_label, ncols, nrows, width_m,
    height_m, zmin, zmax, exaggeration, elev_b64, before_uri, after_uri.
    before_uri/after_uri are full data: URIs for the two RGB textures."""
    payload = json.dumps(cfg, separators=(",", ":"))
    # payload is embedded in a <script type=application/json>; base64 + numbers
    # only, so the sole thing that could break out is a literal </script>.
    payload = payload.replace("</", "<\\/")
    return (
        _HTML_HEAD
        + _escape_title(cfg.get("title", "3D before / after"))
        + _HTML_MID
        + payload
        + _HTML_SCRIPT_OPEN
        + _VIEWER_JS
        + _HTML_TAIL
    )


def _escape_title(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>"""

_HTML_MID = """</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; height: 100%; background: #0b0e13; overflow: hidden;
               font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  #gl { display: block; width: 100vw; height: 100vh; touch-action: none; cursor: grab; }
  #gl:active { cursor: grabbing; }
  #hud { position: fixed; left: 12px; top: 12px; display: flex; gap: 8px;
         align-items: center; }
  .btn { appearance: none; border: 1px solid #33405a; background: #161c27;
         color: #dfe6f2; padding: 8px 14px; border-radius: 8px; font-size: 14px;
         font-weight: 600; cursor: pointer; user-select: none; }
  .btn.active { background: #2d6cdf; border-color: #2d6cdf; color: #fff; }
  #tag { position: fixed; left: 12px; bottom: 12px; color: #aeb9cc;
         font-size: 13px; background: rgba(11,14,19,.6); padding: 6px 10px;
         border-radius: 6px; max-width: 60vw; }
  #help { position: fixed; right: 12px; bottom: 12px; color: #8593a8;
          font-size: 12px; text-align: right; line-height: 1.5; }
  #err { position: fixed; inset: 0; display: none; place-items: center;
         color: #ffb4b4; font-size: 15px; padding: 24px; text-align: center; }
  kbd { background: #202836; border: 1px solid #33405a; border-radius: 4px;
        padding: 1px 6px; font-size: 11px; }
  #labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
  .lbl { position: absolute; transform: translate(-50%, -150%);
         background: rgba(11,14,19,.74); color: #f2f5fb;
         border: 1px solid #99a; border-left-width: 3px; border-radius: 4px;
         padding: 1px 6px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  #layers { position: fixed; right: 12px; top: 12px; max-height: 42vh;
            overflow: auto; background: rgba(11,14,19,.72);
            border: 1px solid #33405a; border-radius: 8px; padding: 8px 10px;
            font-size: 13px; }
  .lyr-title { color: #aeb9cc; font-weight: 600; margin-bottom: 4px; }
  .lyr-row { display: flex; align-items: center; gap: 6px; color: #dfe6f2;
             cursor: pointer; padding: 2px 0; }
  .lyr-sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
  #exportModal { position: fixed; inset: 0; z-index: 50; padding: 18px; gap: 12px;
                 background: rgba(0,0,0,.9); display: flex; flex-direction: column;
                 align-items: center; justify-content: center; }
  #exportModal img { max-width: 96vw; max-height: 82vh; border: 1px solid #333;
                     background: #0b0e13; }
  .exp-bar { display: flex; gap: 10px; }
  .exp-bar a.btn { text-decoration: none; }
</style>
</head>
<body>
<canvas id="gl"></canvas>
<div id="labels"></div>
<div id="layers"></div>
<div id="hud">
  <button id="beforeBtn" class="btn active">◀ Before</button>
  <button id="afterBtn" class="btn">After ▶</button>
  <button id="exportBtn" class="btn">⬇ Export figure</button>
</div>
<div id="tag"></div>
<div id="help">
  left-drag orbit · middle/right/shift-drag pan · scroll zoom<br>
  <kbd>Space</kbd> flip · <kbd>B</kbd> before · <kbd>A</kbd> after
</div>
<div id="err"></div>
<script id="cfg" type="application/json">"""

_HTML_SCRIPT_OPEN = """</script>
<script>
"""

_HTML_TAIL = """
</script>
</body>
</html>
"""


# ---- the viewer: vanilla WebGL, no libraries. Kept as a static string (no
# ---- interpolation) so there is nothing to escape; all data arrives via #cfg.
_VIEWER_JS = r'''
(function () {
  "use strict";
  function fail(msg) {
    var e = document.getElementById("err");
    e.style.display = "grid";
    e.textContent = msg;
  }
  var CFG;
  try { CFG = JSON.parse(document.getElementById("cfg").textContent); }
  catch (ex) { return fail("Could not read scene data: " + ex); }

  var canvas = document.getElementById("gl");
  var glopt = { preserveDrawingBuffer: true, antialias: true };
  var gl = canvas.getContext("webgl", glopt) ||
           canvas.getContext("experimental-webgl", glopt);
  if (!gl) return fail("WebGL is not available in this browser.");

  // ---------- tiny mat4 (column-major) ----------
  function mIdent() { return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]; }
  function mMul(a, b) {
    var o = new Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c*4+r] = a[0*4+r]*b[c*4+0] + a[1*4+r]*b[c*4+1] +
                 a[2*4+r]*b[c*4+2] + a[3*4+r]*b[c*4+3];
    }
    return o;
  }
  function mPersp(fovy, asp, near, far) {
    var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return [f/asp,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0];
  }
  function sub(a,b){return [a[0]-b[0],a[1]-b[1],a[2]-b[2]];}
  function cross(a,b){return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];}
  function norm(a){var l=Math.hypot(a[0],a[1],a[2])||1;return [a[0]/l,a[1]/l,a[2]/l];}
  function dot(a,b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];}
  function mLookAt(eye, center, up) {
    var z = norm(sub(eye, center));
    var x = norm(cross(up, z));
    var y = cross(z, x);
    return [ x[0],y[0],z[0],0,  x[1],y[1],z[1],0,  x[2],y[2],z[2],0,
             -dot(x,eye),-dot(y,eye),-dot(z,eye),1 ];
  }

  // ---------- decode elevation ----------
  function b64Floats(b64) {
    var bin = atob(b64), n = bin.length, u = new Uint8Array(n);
    for (var i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
    return new Float32Array(u.buffer);
  }
  var NC = CFG.ncols|0, NR = CFG.nrows|0;
  var elev = b64Floats(CFG.elev_b64);
  if (elev.length < NC*NR) return fail("Elevation data is incomplete.");
  var W = +CFG.width_m, H = +CFG.height_m;
  var zmin = +CFG.zmin, zmax = +CFG.zmax;
  var zmid = (zmin + zmax) / 2;
  var exag = +CFG.exaggeration || 1;

  // fill NaN cells with zmid so the mesh stays continuous
  function z(i, j) {
    var v = elev[j*NC + i];
    return (v === v) ? v : zmid;
  }

  // ---------- build mesh (positions / uv / normals / indices) ----------
  var nv = NC * NR;
  var pos = new Float32Array(nv*3);
  var uv = new Float32Array(nv*2);
  var nrm = new Float32Array(nv*3);
  var dx = W/(NC-1 || 1), dy = H/(NR-1 || 1);
  for (var j = 0; j < NR; j++) {
    for (var i = 0; i < NC; i++) {
      var k = j*NC + i;
      var x = -W/2 + i*dx;                 // east
      var yy = H/2 - j*dy;                 // north (row 0 = north)
      var zz = (z(i,j) - zmid) * exag;
      pos[k*3] = x; pos[k*3+1] = yy; pos[k*3+2] = zz;
      uv[k*2] = i/(NC-1 || 1); uv[k*2+1] = j/(NR-1 || 1);
      // normal from central differences on the exaggerated surface
      var zl = (z(Math.max(i-1,0),j)-zmid)*exag, zr = (z(Math.min(i+1,NC-1),j)-zmid)*exag;
      var zd = (z(i,Math.max(j-1,0))-zmid)*exag, zu = (z(i,Math.min(j+1,NR-1))-zmid)*exag;
      var n = norm([-(zr-zl)/(2*dx), (zu-zd)/(2*dy), 1]);
      nrm[k*3] = n[0]; nrm[k*3+1] = n[1]; nrm[k*3+2] = n[2];
    }
  }
  var nCells = (NC-1)*(NR-1);
  var idx = (nv > 65535) ? new Uint32Array(nCells*6) : new Uint16Array(nCells*6);
  if (nv > 65535 && !gl.getExtension("OES_element_index_uint")) {
    return fail("Terrain grid too large for this browser (no 32-bit indices).");
  }
  var p = 0;
  for (var j2 = 0; j2 < NR-1; j2++) {
    for (var i2 = 0; i2 < NC-1; i2++) {
      var a = j2*NC + i2, b = a+1, c = a+NC, d = c+1;
      idx[p++]=a; idx[p++]=c; idx[p++]=b;
      idx[p++]=b; idx[p++]=c; idx[p++]=d;
    }
  }

  // ---------- shaders ----------
  function sh(type, src) {
    var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
      throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  var VS =
    "attribute vec3 aPos; attribute vec2 aUV; attribute vec3 aNrm;" +
    "uniform mat4 uMVP; varying vec2 vUV; varying vec3 vN;" +
    "void main(){ vUV=aUV; vN=aNrm; gl_Position=uMVP*vec4(aPos,1.0); }";
  var FS =
    "precision mediump float; varying vec2 vUV; varying vec3 vN;" +
    "uniform sampler2D uTex; uniform vec3 uLight;" +
    "void main(){ vec3 n=normalize(vN); float d=max(dot(n,normalize(uLight)),0.0);" +
    "vec3 c=texture2D(uTex,vUV).rgb; gl_FragColor=vec4(c*(0.55+0.6*d),1.0); }";
  var prog;
  try {
    prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS))
      throw new Error(gl.getProgramInfoLog(prog));
  } catch (ex) { return fail("Shader error: " + ex.message); }
  gl.useProgram(prog);

  function buf(data, target) {
    var b = gl.createBuffer();
    gl.bindBuffer(target, b); gl.bufferData(target, data, gl.STATIC_DRAW);
    return b;
  }
  var aPos = gl.getAttribLocation(prog, "aPos");
  var aUV  = gl.getAttribLocation(prog, "aUV");
  var aNrm = gl.getAttribLocation(prog, "aNrm");
  var posB = buf(pos, gl.ARRAY_BUFFER);
  var uvB  = buf(uv,  gl.ARRAY_BUFFER);
  var nrmB = buf(nrm, gl.ARRAY_BUFFER);
  var idxB = buf(idx, gl.ELEMENT_ARRAY_BUFFER);
  var uMVP = gl.getUniformLocation(prog, "uMVP");
  var uLight = gl.getUniformLocation(prog, "uLight");
  var uTex = gl.getUniformLocation(prog, "uTex");

  // ---------- polygon overlays (draped on the terrain) ----------
  var oprog;
  try {
    oprog = gl.createProgram();
    gl.attachShader(oprog, sh(gl.VERTEX_SHADER,
      "attribute vec3 aPos; uniform mat4 uMVP; uniform float uPtSize;" +
      "void main(){ gl_Position = uMVP*vec4(aPos,1.0); gl_PointSize = uPtSize; }"));
    gl.attachShader(oprog, sh(gl.FRAGMENT_SHADER,
      "precision mediump float; uniform vec4 uColor; uniform float uPoint;" +
      "void main(){" +
      // uPoint>0.5: shape the point sprite as an upward triangle (a peak glyph)
      "  if (uPoint > 0.5 && abs(gl_PointCoord.x - 0.5) > 0.5*gl_PointCoord.y) discard;" +
      "  gl_FragColor = uColor; }"));
    gl.linkProgram(oprog);
    if (!gl.getProgramParameter(oprog, gl.LINK_STATUS)) oprog = null;
  } catch (ex) { oprog = null; }
  var oPos = oprog ? gl.getAttribLocation(oprog, "aPos") : -1;
  var oMVP = oprog ? gl.getUniformLocation(oprog, "uMVP") : null;
  var oColor = oprog ? gl.getUniformLocation(oprog, "uColor") : null;
  var oPtSize = oprog ? gl.getUniformLocation(oprog, "uPtSize") : null;
  var oPoint = oprog ? gl.getUniformLocation(oprog, "uPoint") : null;

  // bilinear terrain height (in mesh Z space) at a mesh-local (x,y)
  function sampleZ(mx, my) {
    var gi = (mx + W/2)/dx, gj = (H/2 - my)/dy;
    gi = Math.max(0, Math.min(NC-1, gi)); gj = Math.max(0, Math.min(NR-1, gj));
    var i0 = Math.floor(gi), j0 = Math.floor(gj);
    var i1 = Math.min(NC-1, i0+1), j1 = Math.min(NR-1, j0+1);
    var fx = gi-i0, fy = gj-j0;
    function e(i,j){ var v = elev[j*NC+i]; return (v===v) ? v : zmid; }
    var z = e(i0,j0)*(1-fx)*(1-fy) + e(i1,j0)*fx*(1-fy) +
            e(i0,j1)*(1-fx)*fy + e(i1,j1)*fx*fy;
    return (z - zmid)*exag;
  }

  // multiply column-major mat4 by vec4 (to project label anchors to the screen)
  function mVec(m, v) {
    return [ m[0]*v[0]+m[4]*v[1]+m[8]*v[2]+m[12]*v[3],
             m[1]*v[0]+m[5]*v[1]+m[9]*v[2]+m[13]*v[3],
             m[2]*v[0]+m[6]*v[1]+m[10]*v[2]+m[14]*v[3],
             m[3]*v[0]+m[7]*v[1]+m[11]*v[2]+m[15]*v[3] ];
  }

  // build draw-ops for every overlay, draping each vertex onto the terrain:
  // polygons (translucent fill + outline), lines (polylines), points (a triangle
  // marker atop a short pole, plus an HTML label). Each op is tagged with its
  // layer name so the on-screen panel can toggle whole layers.
  var overlays = [], labelItems = [], vis = {}, layerMeta = [];
  function regLayer(name, color) {
    if (name && !(name in vis)) { vis[name] = true; layerMeta.push({name: name, color: color}); }
  }
  var lift = Math.max(0.4, (zmax - zmin)*exag*0.0025);   // tiny z-fight offset
  var poleH = Math.max(4, (zmax - zmin)*exag*0.06);
  function drape(list, liftMul) {
    var a = new Float32Array(list.length*3);
    for (var i=0;i<list.length;i++){ var p=list[i];
      a[i*3]=p[0]; a[i*3+1]=p[1]; a[i*3+2]=sampleZ(p[0],p[1])+lift*liftMul; }
    return a;
  }
  function op(arr, n, mode, color, alpha, noDepth, layer) {
    if (n > 0) overlays.push({ buf: buf(arr, gl.ARRAY_BUFFER), n: n, mode: mode,
      color: color, alpha: (alpha == null ? 1 : alpha), noDepth: !!noDepth,
      layer: layer });
  }
  (CFG.polys || []).forEach(function (P) {
    var c = P.color || [255, 80, 80]; regLayer(P.name, c);
    (P.rings || []).forEach(function (R) {
      var tris = R.tris || [], out = R.outline || [];
      if (tris.length >= 3) op(drape(tris, 1.0), tris.length, gl.TRIANGLES, c, 0.55, true, P.name);
      if (out.length >= 2) op(drape(out, 1.4), out.length, gl.LINE_LOOP, c, 1.0, false, P.name);
    });
  });
  (CFG.lines || []).forEach(function (L) {
    var c = L.color || [90, 190, 255]; regLayer(L.name, c);
    (L.paths || []).forEach(function (path) {
      if (path.length >= 2) op(drape(path, 1.4), path.length, gl.LINE_STRIP, c, 1.0, false, L.name);
    });
  });
  var labelsEl = document.getElementById("labels");
  (CFG.points || []).forEach(function (PT) {
    var c = PT.color || [255, 220, 60]; regLayer(PT.name, c);
    var coords = PT.coords || [], labels = PT.labels || [];
    if (!coords.length) return;
    var poles = new Float32Array(coords.length * 6);
    var marks = new Float32Array(coords.length * 3);
    for (var i=0;i<coords.length;i++){ var p=coords[i];
      var zb = sampleZ(p[0], p[1]) + lift, zt = zb + poleH;
      poles[i*6]=p[0]; poles[i*6+1]=p[1]; poles[i*6+2]=zb;
      poles[i*6+3]=p[0]; poles[i*6+4]=p[1]; poles[i*6+5]=zt;
      marks[i*3]=p[0]; marks[i*3+1]=p[1]; marks[i*3+2]=zt;
      var txt = labels[i];
      if (txt) {
        var el = document.createElement("div");
        el.className = "lbl"; el.textContent = txt;
        el.style.borderLeftColor = "rgb("+c[0]+","+c[1]+","+c[2]+")";
        labelsEl.appendChild(el);
        labelItems.push({ x: p[0], y: p[1], el: el, layer: PT.name });
      }
    }
    op(poles, coords.length*2, gl.LINES, c, 1.0, false, PT.name);
    op(marks, coords.length, gl.POINTS, c, 1.0, false, PT.name);
  });

  (function buildLayersPanel() {
    var panel = document.getElementById("layers");
    if (!layerMeta.length) { panel.style.display = "none"; return; }
    var title = document.createElement("div");
    title.className = "lyr-title"; title.textContent = "Overlays";
    panel.appendChild(title);
    layerMeta.forEach(function (m) {
      var row = document.createElement("label"); row.className = "lyr-row";
      var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = true;
      cb.onchange = function () { vis[m.name] = cb.checked; draw(); };
      var sw = document.createElement("span"); sw.className = "lyr-sw";
      sw.style.background = "rgb("+m.color[0]+","+m.color[1]+","+m.color[2]+")";
      var tx = document.createElement("span"); tx.textContent = m.name;
      row.appendChild(cb); row.appendChild(sw); row.appendChild(tx);
      panel.appendChild(row);
    });
  })();

  // ---------- textures (both preloaded on the GPU) ----------
  function makeTex() {
    var t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    // 1x1 grey placeholder until the image decodes
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,
                  new Uint8Array([90,90,90,255]));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    return t;
  }
  var texes = [makeTex(), makeTex()];
  var loaded = 0;
  function load(uri, slot) {
    var img = new Image();
    img.onload = function () {
      gl.bindTexture(gl.TEXTURE_2D, texes[slot]);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,img);
      loaded++; draw();
    };
    img.onerror = function () { fail("Could not decode the " +
      (slot ? "after" : "before") + " image."); };
    img.src = uri;
  }
  load(CFG.before_uri, 0);
  load(CFG.after_uri, 1);

  // ---------- camera (orbit, Z-up) ----------
  var span = Math.max(W, H);
  var cam = { az: -0.6, el: 0.75, dist: span*1.1,
              tgt: [0,0,0] };
  autoOrient();   // smart default facing (user can orbit freely from here)
  fitView();      // frame the whole box on load — no clipped corners
  function eyePos() {
    var ce = Math.cos(cam.el), se = Math.sin(cam.el);
    return [ cam.tgt[0] + cam.dist*ce*Math.cos(cam.az),
             cam.tgt[1] + cam.dist*ce*Math.sin(cam.az),
             cam.tgt[2] + cam.dist*se ];
  }

  // ---------- interaction ----------
  var drag = null;
  canvas.addEventListener("pointerdown", function (e) {
    if (e.button === 1) e.preventDefault();     // middle: suppress autoscroll
    // middle OR right OR shift+left = pan; plain left = orbit
    drag = { x: e.clientX, y: e.clientY,
             pan: e.shiftKey || e.button === 1 || e.button === 2 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var ddx = e.clientX - drag.x, ddy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY;
    if (drag.pan) {
      // planar pan (inverted: the terrain follows the cursor). Move the look-at
      // target opposite the drag in the ground plane — screen-right maps to
      // (sin az, -cos az); screen-up (drag toward viewer) to (cos az, sin az).
      var ce = Math.cos(cam.az), se = Math.sin(cam.az);
      var k = cam.dist / 700;
      cam.tgt[0] += se*ddx*k;  cam.tgt[1] -= ce*ddx*k;
      cam.tgt[0] -= ce*ddy*k;  cam.tgt[1] -= se*ddy*k;
    } else {
      cam.az -= ddx*0.006;
      cam.el += ddy*0.006;
      cam.el = Math.max(0.08, Math.min(1.5, cam.el));
    }
    draw();
  });
  canvas.addEventListener("pointerup", function (e) {
    drag = null; try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    cam.dist *= Math.exp((e.deltaY > 0 ? 1 : -1) * 0.12);
    cam.dist = Math.max(span*0.15, Math.min(span*6, cam.dist));
    draw();
  }, { passive: false });

  // ---------- flip ----------
  var active = 0;
  var bBtn = document.getElementById("beforeBtn");
  var aBtn = document.getElementById("afterBtn");
  var tag = document.getElementById("tag");
  function setSide(s) {
    active = s;
    bBtn.classList.toggle("active", s === 0);
    aBtn.classList.toggle("active", s === 1);
    tag.textContent = (s === 0 ? "BEFORE — " + (CFG.before_label||"")
                               : "AFTER — " + (CFG.after_label||""));
    draw();
  }
  bBtn.onclick = function () { setSide(0); };
  aBtn.onclick = function () { setSide(1); };
  window.addEventListener("keydown", function (e) {
    if (e.code === "Space") { e.preventDefault(); setSide(1 - active); }
    else if (e.key === "b" || e.key === "B") setSide(0);
    else if (e.key === "a" || e.key === "A") setSide(1);
  });

  // ---------- draw ----------
  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.floor(canvas.clientWidth*dpr), h = Math.floor(canvas.clientHeight*dpr);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, canvas.width, canvas.height);
  }
  gl.enable(gl.DEPTH_TEST);
  gl.clearColor(0.043, 0.055, 0.075, 1);
  var scheduled = false, lastMVP = null;

  // synchronous GL render (also used per-panel by the figure export); returns mvp
  function renderScene(skipResize) {
    if (!skipResize) resize();
    var asp = canvas.width / Math.max(1, canvas.height);
    var far = cam.dist*4 + span*2 + (zmax - zmin)*exag*4 + 10;
    var proj = mPersp(45*Math.PI/180, asp, Math.max(0.5, cam.dist*0.002), far);
    var view = mLookAt(eyePos(), cam.tgt, [0,0,1]);
    var mvp = mMul(proj, view); lastMVP = mvp;
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(prog);
    gl.uniformMatrix4fv(uMVP, false, new Float32Array(mvp));
    gl.uniform3fv(uLight, new Float32Array(norm([0.5, 0.6, 0.9])));
    gl.bindBuffer(gl.ARRAY_BUFFER, posB);
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER, uvB);
    gl.enableVertexAttribArray(aUV); gl.vertexAttribPointer(aUV,2,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER, nrmB);
    gl.enableVertexAttribArray(aNrm); gl.vertexAttribPointer(aNrm,3,gl.FLOAT,false,0,0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texes[active]);
    gl.uniform1i(uTex, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idxB);
    gl.drawElements(gl.TRIANGLES, idx.length,
      (idx instanceof Uint32Array) ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT, 0);
    if (oprog && overlays.length) {
      gl.useProgram(oprog);
      gl.uniformMatrix4fv(oMVP, false, new Float32Array(mvp));
      gl.uniform1f(oPtSize, Math.max(9, Math.min(20, canvas.height/50)));
      for (var oi=0; oi<overlays.length; oi++) {
        var o = overlays[oi], c = o.color;
        if (o.layer && !vis[o.layer]) continue;
        if (o.noDepth) {
          gl.enable(gl.BLEND);
          gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
          gl.depthMask(false);
        }
        gl.uniform1f(oPoint, o.mode === gl.POINTS ? 1.0 : 0.0);
        gl.uniform4f(oColor, c[0]/255, c[1]/255, c[2]/255, o.alpha);
        gl.bindBuffer(gl.ARRAY_BUFFER, o.buf);
        gl.enableVertexAttribArray(oPos);
        gl.vertexAttribPointer(oPos, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(o.mode, 0, o.n);
        if (o.noDepth) { gl.depthMask(true); gl.disable(gl.BLEND); }
      }
      gl.useProgram(prog);
    }
    return mvp;
  }

  function positionLabels(mvp) {
    if (!labelItems.length) return;
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    for (var li=0; li<labelItems.length; li++) {
      var it = labelItems[li];
      if (it.layer && !vis[it.layer]) { it.el.style.display = "none"; continue; }
      var vv = mVec(mvp, [it.x, it.y, sampleZ(it.x, it.y)+lift+poleH, 1]);
      if (vv[3] <= 0) { it.el.style.display = "none"; continue; }
      it.el.style.display = "block";
      it.el.style.left = ((vv[0]/vv[3]*0.5+0.5)*cw) + "px";
      it.el.style.top = ((1-(vv[1]/vv[3]*0.5+0.5))*ch) + "px";
    }
  }

  function draw() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      positionLabels(renderScene());
    });
  }

  // ---------- figure export (annotated PNG) ----------
  // tight XY bounding box of the actual (non-NaN) terrain, in mesh metres, so the
  // axis box hugs the topography instead of the full (possibly nodata-padded) AOI.
  var _dbb = null;
  function dataBBox() {
    if (_dbb) return _dbb;
    var i0=NC, i1=-1, j0=NR, j1=-1;
    for (var j=0;j<NR;j++) for (var i=0;i<NC;i++) {
      var v = elev[j*NC+i];
      if (v===v) { if(i<i0)i0=i; if(i>i1)i1=i; if(j<j0)j0=j; if(j>j1)j1=j; }
    }
    if (i1<0) { _dbb = {x0:-W/2, x1:W/2, y0:-H/2, y1:H/2}; return _dbb; }
    var ddx=W/(NC-1||1), ddy=H/(NR-1||1);
    _dbb = { x0:-W/2+i0*ddx, x1:-W/2+i1*ddx, y0:H/2-j1*ddy, y1:H/2-j0*ddy };
    return _dbb;
  }
  // Cap the BOX + elevation-axis top near the top of the terrain (~96th percentile)
  // so the box hugs the surface and the empty upper-box air shrinks; the true summit
  // (mesh) still renders and emerges through the capped top. peakPt() is added to the
  // camera fit so that emerging summit is never clipped.
  var _zcap;
  function zCapTop(){
    if(typeof _zcap==="number") return _zcap;   // order-independent memo (init runs before the var)
    var vals=[]; for(var i=0;i<elev.length;i++){ var v=elev[i]; if(v===v) vals.push(v); }
    if(!vals.length){ _zcap=zmax; return _zcap; }
    vals.sort(function(a,b){return a-b;});
    _zcap=vals[Math.min(vals.length-1, Math.floor(vals.length*0.96))];
    if(_zcap<=zmin) _zcap=zmax;
    return _zcap;
  }
  function peakPt(){
    var mi=-1,mv=-1e18;
    for(var i=0;i<elev.length;i++){ var v=elev[i]; if(v===v && v>mv){mv=v;mi=i;} }
    if(mi<0) return null;
    var ddx=W/(NC-1||1), ddy=H/(NR-1||1), j=Math.floor(mi/NC), ii=mi-j*NC;
    return [-W/2+ii*ddx, H/2-j*ddy, (mv-zmid)*exag];
  }
  // Longest line overlay = the landslide centerline. Its endpoints (+ their
  // elevations) give a robust runout axis and downhill sense -- far steadier than
  // a single centroid gradient on rugged terrain.
  function centerlineAxis() {
    var best=null, bestLen=-1;
    (CFG.lines||[]).forEach(function(L){ (L.paths||[]).forEach(function(pa){
      if (!pa || pa.length<2) return;
      var d=0; for (var i=1;i<pa.length;i++){ d+=Math.hypot(pa[i][0]-pa[i-1][0], pa[i][1]-pa[i-1][1]); }
      if (d>bestLen){ bestLen=d; best=pa; }
    }); });
    if (!best) return null;
    var A=best[0], B=best[best.length-1];
    var zA=sampleZ(A[0],A[1]), zB=sampleZ(B[0],B[1]);
    var hi=(zA>=zB)?A:B, lo=(zA>=zB)?B:A;
    var axx=B[0]-A[0], axy=B[1]-A[1], am=Math.hypot(axx,axy)||1;
    var dhx=lo[0]-hi[0], dhy=lo[1]-hi[1], dm=Math.hypot(dhx,dhy)||1;
    return { ax:[axx/am, axy/am], down:[dhx/dm, dhy/dm], len:bestLen, drop:Math.abs(zA-zB) };
  }
  // Smart DEFAULT orientation only (the user orbits freely afterwards; the figure
  // export uses whatever they framed). Prefer the centerline; fall back to PCA of
  // the overlay polygon + local slope aspect for a broadside 3/4 view.
  function autoOrient() {
    var TILT_IN = 0.35, az0 = -2.2, el0 = 0.55, ov = [];
    var cl = centerlineAxis();
    if (cl) {
      var ux0=-cl.down[0], uy0=-cl.down[1];                 // uphill unit
      var vx0=-cl.ax[1], vy0=cl.ax[0];                      // broadside to the runout
      if (vx0*ux0 + vy0*uy0 < 0) { vx0=-vx0; vy0=-vy0; }    // ...from the downhill side
      vx0=vx0*(1-TILT_IN)+ux0*TILT_IN; vy0=vy0*(1-TILT_IN)+uy0*TILT_IN;
      var grad = cl.len>0 ? cl.drop/cl.len : 0;
      cam.az = Math.atan2(-vy0, -vx0);                       // slide faces the camera
      cam.el = grad>1e-6 ? Math.max(0.5, Math.min(0.85, Math.atan2(1, grad))) : 0.6;
      return;
    }
    (CFG.polys||[]).forEach(function(P){(P.rings||[]).forEach(function(R){(R.outline||[]).forEach(function(p){ov.push(p);});});});
    (CFG.points||[]).forEach(function(P){(P.coords||[]).forEach(function(p){ov.push(p);});});
    (CFG.lines||[]).forEach(function(L){(L.paths||[]).forEach(function(pa){pa.forEach(function(p){ov.push(p);});});});
    if (ov.length >= 2) {
      var ccx=0, ccy=0; ov.forEach(function(p){ccx+=p[0]; ccy+=p[1];}); ccx/=ov.length; ccy/=ov.length;
      var sxx=0, sxy=0, syy=0;
      ov.forEach(function(p){ var dx=p[0]-ccx, dy=p[1]-ccy; sxx+=dx*dx; sxy+=dx*dy; syy+=dy*dy; });
      sxx/=ov.length; sxy/=ov.length; syy/=ov.length;
      var tr=sxx+syy, dsc=Math.sqrt(Math.max(0, tr*tr/4 - (sxx*syy - sxy*sxy)));
      var l1=tr/2+dsc, l2=tr/2-dsc, ex, ey;
      if (Math.abs(sxy) > 1e-9) { ex=l1-syy; ey=sxy; }
      else if (sxx >= syy)      { ex=1; ey=0; }
      else                      { ex=0; ey=1; }
      var em=Math.hypot(ex,ey)||1; ex/=em; ey/=em;
      var eps=meshSpan()/120;
      var gx=(sampleZ(ccx+eps,ccy)-sampleZ(ccx-eps,ccy))/(2*eps);
      var gy=(sampleZ(ccx,ccy+eps)-sampleZ(ccx,ccy-eps))/(2*eps);
      var gm=Math.hypot(gx,gy), vx, vy;
      if (l1 > 1.6*Math.max(l2, 1e-9)) {
        vx=-ey; vy=ex;
        if (gm>1e-6 && (vx*gx+vy*gy) < 0) { vx=-vx; vy=-vy; }
        if (gm>1e-6) { var ux=gx/gm, uy=gy/gm; vx=vx*(1-TILT_IN)+ux*TILT_IN; vy=vy*(1-TILT_IN)+uy*TILT_IN; }
      } else if (gm>1e-6) { vx=gx/gm; vy=gy/gm; }
      else { vx=Math.cos(az0); vy=Math.sin(az0); }
      az0 = Math.atan2(-vy, -vx);
      el0 = (gm>1e-6) ? Math.max(0.42, Math.min(0.72, Math.atan2(1, gm))) : 0.55;
    }
    cam.az = az0; cam.el = el0;
  }
  // Frame the data box for the current az/el as LARGE as possible without clipping
  // ANYTHING: fit the projected box corners into an INNER rect that reserves margin
  // for the axis labels (left = elevation, bottom = distance) and the N arrow (top-
  // right), then pan the box onto that rect's centre. m = margin fractions {L,R,T,B};
  // default ~symmetric (the live view draws no labels). The export passes larger
  // left/bottom margins so nothing runs off the panel.
  function fitView(m) {
    m = m || {L:0.06, R:0.06, T:0.06, B:0.06};
    var bb = dataBBox();
    var zB=(zmin-zmid)*exag, zT=(zCapTop()-zmid)*exag;   // capped box top
    cam.tgt=[(bb.x0+bb.x1)/2, (bb.y0+bb.y1)/2, (zB+zT)/2];
    // FIT to the box FLOOR + the TERRAIN SURFACE (the mountain silhouette), plus the
    // elevation-axis top corner (added per-iteration in boxScreen). We EXCLUDE the
    // other box-top corners so the empty upper box can't force dead air above the
    // terrain -- the summit fills the top of the frame instead.
    var fitPts=[];
    for (var a=0;a<2;a++) for (var b=0;b<2;b++) fitPts.push([a?bb.x1:bb.x0, b?bb.y1:bb.y0, zB]);
    var GS=12;
    for (var gy=0;gy<=GS;gy++) for (var gx=0;gx<=GS;gx++){
      var sx0=bb.x0+(bb.x1-bb.x0)*gx/GS, sy0=bb.y0+(bb.y1-bb.y0)*gy/GS;
      fitPts.push([sx0, sy0, sampleZ(sx0,sy0)]);          // terrain surface (exag mesh z)
    }
    var _pk=peakPt(); if(_pk) fitPts.push(_pk);            // exact summit vertex
    var topC=[];
    for (var a3=0;a3<2;a3++) for (var b3=0;b3<2;b3++) topC.push([a3?bb.x1:bb.x0, b3?bb.y1:bb.y0, zT]);
    var W=canvas.width, H=Math.max(1,canvas.height), asp=W/H, fovy=45*Math.PI/180;
    var innerW=(1-m.L-m.R)*W, innerH=(1-m.T-m.B)*H;
    var icx=(m.L+(1-m.R))*0.5*W, icy=(m.T+(1-m.B))*0.5*H;   // inner-rect centre (px)
    var rx=(bb.x1-bb.x0)/2, ry=(bb.y1-bb.y0)/2, rz=(zT-zB)/2;
    cam.dist=(Math.sqrt(rx*rx+ry*ry+rz*rz)||meshSpan()*0.5)/Math.sin(fovy/2);   // sphere seed
    function boxScreen(){
      var far=cam.dist*4+span*2+(zmax-zmin)*exag*4+10;
      var mvp=mMul(mPersp(fovy, asp, Math.max(0.5, cam.dist*0.002), far), mLookAt(eyePos(), cam.tgt, [0,0,1]));
      var minSx=1e9, axTop=null;                            // elevation axis = left-most vertical edge
      for (var k=0;k<4;k++){ var sb=project(mvp,[topC[k][0],topC[k][1],zB],W,H);
        if(sb && sb[0]<minSx){ minSx=sb[0]; axTop=topC[k]; } }
      var r={minx:1e9,maxx:-1e9,miny:1e9,maxy:-1e9,cx:0,cy:0,n:0};
      function acc(p){ var s=project(mvp,p,W,H); if(s){ r.n++; r.cx+=s[0]; r.cy+=s[1]; if(s[0]<r.minx)r.minx=s[0]; if(s[0]>r.maxx)r.maxx=s[0]; if(s[1]<r.miny)r.miny=s[1]; if(s[1]>r.maxy)r.maxy=s[1]; } }
      for (var ci=0;ci<fitPts.length;ci++) acc(fitPts[ci]);
      if(axTop) acc(axTop);
      if(r.n){ r.cx/=r.n; r.cy/=r.n; } return r;
    }
    // (A) size the box to the inner rect (it stays ~centred on the look-at point)
    for (var it=0; it<6; it++) {
      var r=boxScreen(); if(!r.n) break;
      var fill=Math.max((r.maxx-r.minx)/(innerW*0.99), (r.maxy-r.miny)/(innerH*0.99));
      if (fill<=1e-4) break; cam.dist *= fill;
    }
    // (B) pan the box centre onto the inner-rect centre (dist fixed -> size preserved)
    for (var it2=0; it2<4; it2++) {
      var r2=boxScreen(); if(!r2.n) break;
      var eye=eyePos(), wpp=2*cam.dist*Math.tan(fovy/2)/H;
      var vdir=norm([cam.tgt[0]-eye[0], cam.tgt[1]-eye[1], cam.tgt[2]-eye[2]]);
      var right=norm(cross(vdir,[0,0,1])), up=norm(cross(right,vdir));
      // centre the BOUNDING BOX (not the mass-weighted centroid) so a bottom-heavy
      // mountain isn't pushed down, leaving extra sky on top.
      var dsx=icx-(r2.minx+r2.maxx)/2, dsy=icy-(r2.miny+r2.maxy)/2;
      cam.tgt=[cam.tgt[0]+right[0]*(-dsx*wpp)+up[0]*(dsy*wpp),
               cam.tgt[1]+right[1]*(-dsx*wpp)+up[1]*(dsy*wpp),
               cam.tgt[2]+right[2]*(-dsx*wpp)+up[2]*(dsy*wpp)];
    }
  }
  function project(mvp, p, W, H) {
    var v = mVec(mvp, [p[0], p[1], p[2], 1]);
    if (v[3] <= 0) return null;
    return [ (v[0]/v[3]*0.5+0.5)*W, (1-(v[1]/v[3]*0.5+0.5))*H ];
  }
  function niceNum(range, round) {
    var e = Math.floor(Math.log(range)/Math.LN10), f = range/Math.pow(10, e), nf;
    if (round) nf = f<1.5?1:(f<3?2:(f<7?5:10));
    else nf = f<=1?1:(f<=2?2:(f<=5?5:10));
    return nf*Math.pow(10, e);
  }
  function niceTicks(lo, hi, n) {
    if (hi <= lo) return [lo];
    var step = niceNum((hi-lo)/((n||6)-1), true);   // ~n ticks across the range
    var out = [];
    for (var v=Math.ceil(lo/step)*step; v<=hi+step*0.5; v+=step) out.push(v);
    return out;
  }
  function drawTickAxis(ctx, mvp, W, H, ws, we, vs, ve, title, fs, boxCtr, fmt, n) {
    var ps = project(mvp, ws, W, H), pe = project(mvp, we, W, H);
    if (!ps || !pe) return;
    var axC = "rgba(246,249,253,0.97)", lw = Math.max(2, W/900);
    ctx.strokeStyle = axC; ctx.lineWidth = lw;
    ctx.beginPath(); ctx.moveTo(ps[0],ps[1]); ctx.lineTo(pe[0],pe[1]); ctx.stroke();
    var dx=pe[0]-ps[0], dy=pe[1]-ps[1], dl=Math.hypot(dx,dy)||1, nx=-dy/dl, ny=dx/dl;
    // orient ticks + labels OUTWARD (away from the box centre)
    var mx=(ps[0]+pe[0])/2, my=(ps[1]+pe[1])/2;
    if (boxCtr && ((mx+nx)-boxCtr[0])*nx + ((my+ny)-boxCtr[1])*ny < 0) { nx=-nx; ny=-ny; }
    var tl = Math.max(6, W/130);
    ctx.textBaseline = "middle"; ctx.textAlign = (nx < -0.2 ? "right" : (nx > 0.2 ? "left" : "center"));
    function label(x, y, txt, bold) {
      ctx.font = (bold?"bold ":"") + fs + "px sans-serif";
      ctx.lineWidth = Math.max(2.5, fs/4); ctx.strokeStyle = "rgba(0,0,0,0.7)";
      ctx.lineJoin = "round"; ctx.strokeText(txt, x, y);
      ctx.fillStyle = "#f6f9ff"; ctx.fillText(txt, x, y);
    }
    niceTicks(Math.min(vs,ve), Math.max(vs,ve), n||6).forEach(function (v) {
      var f = (v - vs)/(ve - vs); if (f<-0.001 || f>1.001) return;
      var wp = [ws[0]+(we[0]-ws[0])*f, ws[1]+(we[1]-ws[1])*f, ws[2]+(we[2]-ws[2])*f];
      var p = project(mvp, wp, W, H); if (!p) return;
      ctx.strokeStyle = axC; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.moveTo(p[0],p[1]); ctx.lineTo(p[0]+nx*tl, p[1]+ny*tl); ctx.stroke();
      label(p[0]+nx*(tl+5), p[1]+ny*(tl+5), (v*fmt.scale).toFixed(fmt.dec), false);
    });
    var mid = project(mvp, [(ws[0]+we[0])/2,(ws[1]+we[1])/2,(ws[2]+we[2])/2], W, H);
    if (mid) { ctx.textAlign="center"; label(mid[0]+nx*(tl+5.2*fs), mid[1]+ny*(tl+5.2*fs), title, true); }
  }
  function drawAxes(ctx, mvp, W, H) {
    var zBot = (zmin - zmid)*exag, zTop = (zCapTop() - zmid)*exag;   // box hugs terrain top
    var bb = dataBBox();                       // tight to the real terrain
    var spanE = bb.x1 - bb.x0, spanN = bb.y1 - bb.y0;
    function corner(ix,iy,iz){ return [ ix?bb.x1:bb.x0, iy?bb.y1:bb.y0, iz?zTop:zBot ]; }
    var Cp = {};
    for (var a=0;a<2;a++) for (var b=0;b<2;b++) for (var d=0;d<2;d++)
      Cp[a+""+b+d] = project(mvp, corner(a,b,d), W, H);
    // the bounding box, with terrain occlusion: hide edge segments the topography
    // is in FRONT of (ray-march eye->point vs the surface) so back edges don't
    // X-ray through the mountain.
    var eyeP = eyePos(), mgn = (zTop-zBot)*0.004 + 0.5;
    function occluded(P) {
      for (var t=0.14; t<0.995; t+=1/22) {
        var qx=eyeP[0]+(P[0]-eyeP[0])*t, qy=eyeP[1]+(P[1]-eyeP[1])*t, qz=eyeP[2]+(P[2]-eyeP[2])*t;
        if (qx<bb.x0 || qx>bb.x1 || qy<bb.y0 || qy>bb.y1) continue;
        if (sampleZ(qx,qy) > qz + mgn) return true;   // terrain in front of this point
      }
      return false;
    }
    function drawEdge3D(A, B) {
      var Nn=44, prev=null, prevVis=false;
      for (var i=0;i<=Nn;i++) {
        var t=i/Nn, Pw=[A[0]+(B[0]-A[0])*t, A[1]+(B[1]-A[1])*t, A[2]+(B[2]-A[2])*t];
        var vis=!occluded(Pw), scr=project(mvp, Pw, W, H);
        if (scr && prev && vis && prevVis) { ctx.beginPath(); ctx.moveTo(prev[0],prev[1]); ctx.lineTo(scr[0],scr[1]); ctx.stroke(); }
        prev=scr; prevVis=vis;
      }
    }
    ctx.strokeStyle = "rgba(234,240,250,0.62)"; ctx.lineWidth = Math.max(1.4, W/1050);
    // FLOOR rectangle only: no box lid and no vertical corner posts (those stuck up
    // into empty sky). The elevation + distance axes draw their own edges with ticks,
    // so the scale is still there without the stray frame lines.
    [["000","100"],["010","110"],["000","010"],["100","110"]].forEach(function (e) {
      drawEdge3D(corner(+e[0][0],+e[0][1],+e[0][2]), corner(+e[1][0],+e[1][1],+e[1][2]));
    });
    // origin = the bottom corner nearest the camera, so the two horizontal axes
    // fall on front-facing edges and read cleanly.
    var eye = eyePos(), O=null, best=1e18;
    [[0,0],[1,0],[0,1],[1,1]].forEach(function (b) {
      var w = corner(b[0],b[1],0);
      var dd = (w[0]-eye[0])*(w[0]-eye[0]) + (w[1]-eye[1])*(w[1]-eye[1]) + (w[2]-eye[2])*(w[2]-eye[2]);
      if (dd < best) { best=dd; O=b; }
    });
    if (!O) return;
    var ix=O[0], iy=O[1];
    var bc = project(mvp, [(bb.x0+bb.x1)/2, (bb.y0+bb.y1)/2, (zBot+zTop)/2], W, H);
    var fs = Math.max(13, Math.round(W/60));
    function fmtAxis(sp){ return sp>=3000 ? {scale:0.001,dec:1,unit:"km"} : {scale:1,dec:0,unit:"m"}; }
    var fE=fmtAxis(spanE), fN=fmtAxis(spanN);
    // horizontal axes = ground DISTANCE from the near corner (0..span, km/m), ~7 ticks
    drawTickAxis(ctx, mvp, W, H, corner(ix,iy,0), corner(1-ix,iy,0),
                 0, spanE, "Distance E ("+fE.unit+")", fs, bc, fE, 7);
    drawTickAxis(ctx, mvp, W, H, corner(ix,iy,0), corner(ix,1-iy,0),
                 0, spanN, "Distance N ("+fN.unit+")", fs, bc, fN, 7);
    // elevation (metres) on the LEFT-most vertical edge, so it never crowds the
    // Northing axis that shares the near corner.
    var Zc=null, zbest=1e18;
    [[0,0],[1,0],[0,1],[1,1]].forEach(function (b) {
      var pb=Cp[b[0]+""+b[1]+"0"], pt=Cp[b[0]+""+b[1]+"1"];
      if (pb && pt) { var sx=(pb[0]+pt[0])/2; if (sx<zbest) { zbest=sx; Zc=b; } }
    });
    if (Zc) drawTickAxis(ctx, mvp, W, H, corner(Zc[0],Zc[1],0), corner(Zc[0],Zc[1],1),
                 zmin, zCapTop(), "Elevation (m)", fs, bc, {scale:1,dec:0,unit:"m"}, 5);
  }
  function drawNorth(ctx, mvp, W, H) {
    var p0 = project(mvp, [0,0,0], W, H), p1 = project(mvp, [0, meshSpan()*0.15, 0], W, H);
    if (!p0 || !p1) return;
    var ang = Math.atan2(p1[1]-p0[1], p1[0]-p0[0]);
    // inset by the arrow+label reach (~1.6R) so the "N" never clips the panel edge,
    // whatever compass direction north points.
    var R = Math.max(16, W/42), pad = 1.6*R+6, cx = W-pad, cy = pad, L = R*0.85;
    ctx.save();
    ctx.lineWidth = Math.max(2, W/650);
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2*Math.PI); ctx.stroke();
    var tx=cx+Math.cos(ang)*L, ty=cy+Math.sin(ang)*L;
    var bx=cx-Math.cos(ang)*L*0.6, by=cy-Math.sin(ang)*L*0.6;
    ctx.strokeStyle="rgba(240,244,250,0.95)";
    ctx.beginPath(); ctx.moveTo(bx,by); ctx.lineTo(tx,ty); ctx.stroke();
    var ah=L*0.4;
    ctx.fillStyle="rgba(240,244,250,0.95)";
    ctx.beginPath(); ctx.moveTo(tx,ty);
    ctx.lineTo(tx-Math.cos(ang-0.42)*ah, ty-Math.sin(ang-0.42)*ah);
    ctx.lineTo(tx-Math.cos(ang+0.42)*ah, ty-Math.sin(ang+0.42)*ah);
    ctx.closePath(); ctx.fill();
    ctx.fillStyle="#fff"; ctx.font="bold "+Math.round(R*0.7)+"px sans-serif";
    ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillText("N", cx+Math.cos(ang)*(R+ah*0.5), cy+Math.sin(ang)*(R+ah*0.5));
    ctx.restore();
  }
  function meshSpan(){ return Math.max(+CFG.width_m, +CFG.height_m); }
  function drawPeakLabels(ctx, mvp, W, H) {
    ctx.save(); ctx.font = "bold "+Math.max(11, Math.round(W/95))+"px sans-serif";
    ctx.textAlign="center"; ctx.textBaseline="middle";
    for (var li=0; li<labelItems.length; li++) {
      var it=labelItems[li]; if (it.layer && !vis[it.layer]) continue;
      var p=project(mvp, [it.x, it.y, sampleZ(it.x,it.y)+lift+poleH], W, H); if(!p) continue;
      var txt=it.el.textContent, w=ctx.measureText(txt).width, fh=Math.max(11,Math.round(W/95));
      ctx.fillStyle="rgba(11,14,19,0.78)"; ctx.fillRect(p[0]-w/2-5, p[1]-fh*1.9, w+10, fh*1.5);
      ctx.fillStyle="#f2f5fb"; ctx.fillText(txt, p[0], p[1]-fh*1.15);
    }
    ctx.restore();
  }
  // Crown/toe + drop + runout, computed from the longest centreline overlay.
  function landslideMetrics(){
    var best=null, bestLen=-1;
    (CFG.lines||[]).forEach(function(L){ (L.paths||[]).forEach(function(pa){
      if(!pa||pa.length<2) return;
      var dd=0; for(var i=1;i<pa.length;i++){ dd+=Math.hypot(pa[i][0]-pa[i-1][0], pa[i][1]-pa[i-1][1]); }
      if(dd>bestLen){ bestLen=dd; best=pa; }
    }); });
    if(!best) return null;
    var A=best[0], B=best[best.length-1];
    var crown=(sampleZ(A[0],A[1])>=sampleZ(B[0],B[1]))?A:B, toe=(crown===A)?B:A;
    var crownZ=sampleZ(crown[0],crown[1]), toeZ=sampleZ(toe[0],toe[1]);
    var crownElev=crownZ/exag+zmid, toeElev=toeZ/exag+zmid;
    return { crown:crown, toe:toe, crownZ:crownZ, toeZ:toeZ, crownElev:crownElev,
             toeElev:toeElev, drop:crownElev-toeElev, runout:bestLen, mid:best[(best.length/2)|0] };
  }
  function drawLandslideAnnotations(ctx, mvp, W, H){
    var m=landslideMetrics(); if(!m) return;
    var pc=project(mvp,[m.crown[0],m.crown[1],m.crownZ],W,H);
    var pe=project(mvp,[m.toe[0],m.toe[1],m.toeZ],W,H);
    if(!pc && !pe) return;
    var fs=Math.max(12, Math.round(W/85)), CY="rgba(130,225,255,0.95)";
    function ci(n){ return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
    ctx.save(); ctx.textBaseline="middle"; ctx.lineJoin="round";
    // ---- corner callout box: all metrics off the scar ----
    var lines=[
      ["Crown · "+ci(m.crownElev)+" m", "#d6ecff"],
      ["Toe · "+ci(m.toeElev)+" m", "#d6ecff"],
      ["↕ "+ci(m.drop)+" m drop", "#d6ecff"],
      [(m.runout>=1000?(m.runout/1000).toFixed(2)+" km":ci(m.runout)+" m")+" runout", "#f6c9c9"]
    ];
    ctx.font="bold "+fs+"px sans-serif";
    var tw=0; lines.forEach(function(l){ tw=Math.max(tw, ctx.measureText(l[0]).width); });
    var padX=fs*0.7, padY=fs*0.55, lineH=fs*1.5, bw=tw+padX*2, bh=lines.length*lineH+padY*2;
    // place opposite the scar, at the top; the right slot sits below the N compass
    var midX=((pc?pc[0]:pe[0])+(pe?pe[0]:pc[0]))/2, onLeft=midX > W*0.5;
    var bx=onLeft ? W*0.035 : W-bw-W*0.035, by=onLeft ? H*0.05 : H*0.20;
    ctx.fillStyle="rgba(11,14,19,0.86)"; ctx.strokeStyle="rgba(150,185,215,0.55)"; ctx.lineWidth=Math.max(1,W/1500);
    ctx.beginPath(); ctx.rect(bx,by,bw,bh); ctx.fill(); ctx.stroke();
    ctx.textAlign="left";
    lines.forEach(function(l,i){ ctx.fillStyle=l[1]; ctx.fillText(l[0], bx+padX, by+padY+lineH*(i+0.5)); });
    // ---- leader lines from the box edge to crown + toe dots ----
    var ax=onLeft ? bx+bw : bx, ay=by+bh/2;
    ctx.strokeStyle="rgba(130,225,255,0.75)"; ctx.lineWidth=Math.max(1.1,W/1300);
    function leader(p){ if(!p) return;
      ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(p[0],p[1]); ctx.stroke();
      ctx.fillStyle=CY; ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(2.5,W/520),0,2*Math.PI); ctx.fill(); }
    leader(pc); leader(pe);
    ctx.restore();
  }
  // ============ download bundle: hand-rolled ZIP + inline GIF encoder ============
  function dataURLBytes(url){ var b=atob(url.split(",")[1]), n=b.length, a=new Uint8Array(n);
    for(var i=0;i<n;i++) a[i]=b.charCodeAt(i); return a; }
  function pngBytes(cv){ return dataURLBytes(cv.toDataURL("image/png")); }
  var _crcT=null;
  function crc32(buf){ if(!_crcT){ _crcT=new Uint32Array(256);
      for(var n=0;n<256;n++){ var c=n; for(var k=0;k<8;k++) c=(c&1)?(0xEDB88320^(c>>>1)):(c>>>1); _crcT[n]=c>>>0; } }
    var crc=0xFFFFFFFF; for(var i=0;i<buf.length;i++) crc=_crcT[(crc^buf[i])&255]^(crc>>>8); return (crc^0xFFFFFFFF)>>>0; }
  function zipStore(files){                       // files: [{name, data:Uint8Array}], STORED (no compress)
    var parts=[], cds=[], off=0;
    function u16(v){ return [v&255,(v>>8)&255]; } function u32(v){ return [v&255,(v>>8)&255,(v>>16)&255,(v>>24)&255]; }
    files.forEach(function(f){
      var nm=[]; for(var i=0;i<f.name.length;i++) nm.push(f.name.charCodeAt(i)&255);
      var crc=crc32(f.data), sz=f.data.length;
      var lfh=[0x50,0x4b,0x03,0x04].concat(u16(20),u16(0),u16(0),u16(0),u16(0),u32(crc),u32(sz),u32(sz),u16(nm.length),u16(0),nm);
      parts.push(new Uint8Array(lfh)); parts.push(f.data);
      cds.push(new Uint8Array([0x50,0x4b,0x01,0x02].concat(u16(20),u16(20),u16(0),u16(0),u16(0),u16(0),
        u32(crc),u32(sz),u32(sz),u16(nm.length),u16(0),u16(0),u16(0),u16(0),u32(0),u32(off),nm)));
      off+=lfh.length+sz;
    });
    var cdSz=0; cds.forEach(function(c){ cdSz+=c.length; });
    var eocd=new Uint8Array([0x50,0x4b,0x05,0x06].concat(u16(0),u16(0),u16(files.length),u16(files.length),u32(cdSz),u32(off),u16(0)));
    var tot=off+cdSz+eocd.length, out=new Uint8Array(tot), pos=0;
    parts.forEach(function(c){ out.set(c,pos); pos+=c.length; });
    cds.forEach(function(c){ out.set(c,pos); pos+=c.length; }); out.set(eocd,pos);
    return out;
  }
  function quantize(px, maxC){                     // median-cut -> up to maxC colours
    function mk(a){ var r0=255,r1=0,g0=255,g1=0,b0=255,b1=0;
      for(var i=0;i<a.length;i++){ var p=a[i]; if(p[0]<r0)r0=p[0]; if(p[0]>r1)r1=p[0]; if(p[1]<g0)g0=p[1]; if(p[1]>g1)g1=p[1]; if(p[2]<b0)b0=p[2]; if(p[2]>b1)b1=p[2]; }
      return {a:a,rr:r1-r0,gr:g1-g0,br:b1-b0}; }
    var boxes=[mk(px)];
    while(boxes.length<maxC){
      var bi=-1,bs=-1; for(var i=0;i<boxes.length;i++){ var b=boxes[i], m=Math.max(b.rr,b.gr,b.br); if(b.a.length>1&&m>bs){bs=m;bi=i;} }
      if(bi<0) break; var b=boxes[bi], ch=(b.rr>=b.gr&&b.rr>=b.br)?0:(b.gr>=b.br?1:2);
      b.a.sort(function(p,q){ return p[ch]-q[ch]; }); var mid=b.a.length>>1;
      boxes.splice(bi,1,mk(b.a.slice(0,mid)),mk(b.a.slice(mid)));
    }
    return boxes.map(function(b){ var r=0,g=0,bl=0,n=b.a.length||1; for(var i=0;i<b.a.length;i++){ r+=b.a[i][0]; g+=b.a[i][1]; bl+=b.a[i][2]; }
      return [Math.round(r/n),Math.round(g/n),Math.round(bl/n)]; });
  }
  function gifEncode(frames, w, h, delayCs){
    var sample=[], tot=w*h, step=Math.max(1, ((tot*frames.length)/20000)|0);
    for(var fi=0;fi<frames.length;fi++){ var fr=frames[fi]; for(var i=0;i<tot;i+=step) sample.push([fr[i*4],fr[i*4+1],fr[i*4+2]]); }
    var pal=quantize(sample,256); while(pal.length<256) pal.push([0,0,0]);
    var lut=new Uint8Array(32768);
    for(var q=0;q<32768;q++){ var r=((q>>10)&31)<<3,g=((q>>5)&31)<<3,bb=(q&31)<<3,best=0,bd=1e12;
      for(var p=0;p<256;p++){ var dr=r-pal[p][0],dg=g-pal[p][1],db=bb-pal[p][2],dd=dr*dr+dg*dg+db*db; if(dd<bd){bd=dd;best=p;} } lut[q]=best; }
    var out=[]; function B(v){ out.push(v&255); } function S(s){ for(var i=0;i<s.length;i++) out.push(s.charCodeAt(i)); }
    S("GIF89a"); B(w);B(w>>8);B(h);B(h>>8); B(0xF7);B(0);B(0);
    for(var i=0;i<256;i++){ B(pal[i][0]);B(pal[i][1]);B(pal[i][2]); }
    B(0x21);B(0xFF);B(0x0B);S("NETSCAPE2.0");B(0x03);B(0x01);B(0);B(0);B(0);   // loop forever
    for(var fi2=0;fi2<frames.length;fi2++){
      B(0x21);B(0xF9);B(0x04);B(0x00);B(delayCs&255);B((delayCs>>8)&255);B(0);B(0);
      B(0x2C);B(0);B(0);B(0);B(0);B(w);B(w>>8);B(h);B(h>>8);B(0);
      var rgba=frames[fi2], idx=new Uint8Array(tot);
      for(var i=0;i<tot;i++) idx[i]=lut[((rgba[i*4]>>3)<<10)|((rgba[i*4+1]>>3)<<5)|(rgba[i*4+2]>>3)];
      B(8); var lz=lzwEncode(8, idx);
      for(var pp=0;pp<lz.length;){ var n=Math.min(255,lz.length-pp); B(n); for(var k=0;k<n;k++) B(lz[pp+k]); pp+=n; } B(0);
    }
    B(0x3B); return Uint8Array.from(out);
  }
  function lzwEncode(minCode, idx){
    var clr=1<<minCode, eoi=clr+1, out=[], cur=0, bits=0, cs, nxt;
    var dict=new Int32Array(4096*256);
    function outp(code){ cur|=code<<bits; bits+=cs; while(bits>=8){ out.push(cur&255); cur>>>=8; bits-=8; } }
    function rst(){ dict.fill(0); cs=minCode+1; nxt=eoi+1; }
    rst(); outp(clr); var pfx=idx[0];
    for(var i=1;i<idx.length;i++){ var c=idx[i], key=pfx*256+c, e=dict[key];
      if(e!==0){ pfx=e; }
      else { outp(pfx);
        if(nxt<4096){ if(nxt===(1<<cs)&&cs<12) cs++; dict[key]=nxt++; }
        else { outp(clr); rst(); }
        pfx=c; } }
    outp(pfx); outp(eoi); if(bits>0) out.push(cur&255); return out;
  }
  // Clean orbit frames (after imagery + overlays, no axes) for the GIF.
  function renderOrbitFrames(nF, gw, gh){
    var sw=canvas.width, sh=canvas.height, sa=active, saz=cam.az, sel=cam.el, sd=cam.dist, stg=cam.tgt.slice(), sv={};
    for(var k in vis) sv[k]=vis[k];
    canvas.width=gw; canvas.height=gh; gl.viewport(0,0,gw,gh);
    active=1; for(var k2 in vis) vis[k2]=true;
    var bb=dataBBox(), zB=(zmin-zmid)*exag, zT=(zmax-zmid)*exag;
    var rx=(bb.x1-bb.x0)/2, ry=(bb.y1-bb.y0)/2, rz=(zT-zB)/2, R=Math.sqrt(rx*rx+ry*ry+rz*rz)||meshSpan()*0.5;
    cam.tgt=[(bb.x0+bb.x1)/2,(bb.y0+bb.y1)/2,(zB+zT)/2]; cam.dist=R/Math.sin(45*Math.PI/180/2)*1.1; cam.el=0.52;
    var az0=cam.az, tmp=document.createElement("canvas"); tmp.width=gw; tmp.height=gh; var t2=tmp.getContext("2d");
    var frames=[];
    for(var f=0;f<nF;f++){ cam.az=az0+f*(2*Math.PI/nF); renderScene(true);
      t2.clearRect(0,0,gw,gh); t2.drawImage(canvas,0,0); frames.push(new Uint8Array(t2.getImageData(0,0,gw,gh).data)); }
    active=sa; for(var k3 in vis) vis[k3]=sv[k3];
    cam.az=saz; cam.el=sel; cam.dist=sd; cam.tgt=stg;
    canvas.width=sw; canvas.height=sh; gl.viewport(0,0,sw,sh); positionLabels(renderScene(true)); draw();
    return {frames:frames, w:gw, h:gh};
  }
  function buildBundle(fig, shots, status, done){
    try {
      var files=[{name:"slide.png", data:pngBytes(fig)}];
      var nm=["before.png","after.png","after_overlays.png"];
      shots.forEach(function(s,i){ files.push({name:nm[i]||("panel"+(i+1)+".png"), data:pngBytes(s.img)}); });
      var orb=renderOrbitFrames(24, 480, 270);
      files.push({name:"orbit.gif", data:gifEncode(orb.frames, orb.w, orb.h, 16)});   // 16cs/frame = half speed
      var blob=new Blob([zipStore(files)], {type:"application/zip"});
      var a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="landslide_3d_bundle.zip";
      document.body.appendChild(a); a.click();
      setTimeout(function(){ document.body.removeChild(a); URL.revokeObjectURL(a.href); }, 1500);
      status.textContent="Done ✓  landslide_3d_bundle.zip";
    } catch(e){ status.textContent="Error: "+(e&&e.message||e); }
    done();
  }
  function exportFigure() {
    // 16:9 slide, 2x2 grid: Before | After / After+overlays | Details
    var FW=1920, FH=1080, m=24, TH=54, capH=28;
    var colW = Math.floor((FW - 3*m)/2), rowH = Math.floor((FH - TH - 3*m)/2);
    var imgH = rowH - capH, pxW = colW*2, pxH = imgH*2;   // 2x supersample
    var panels = [
      { cap: "BEFORE" + (CFG.before_date ? " · " + CFG.before_date : ""), side:0, ov:false },
      { cap: "AFTER" + (CFG.after_date ? " · " + CFG.after_date : ""), side:1, ov:false },
      { cap: "AFTER + overlays" + (CFG.after_date ? " · " + CFG.after_date : ""), side:1, ov:true }
    ];
    var sw=canvas.width, sh=canvas.height, sa=active, sv={}; for (var k in vis) sv[k]=vis[k];
    canvas.width=pxW; canvas.height=pxH; gl.viewport(0,0,pxW,pxH);   // fixed render size
    // Camera: keep the orientation the user framed in the live viewer (WYSIWYG).
    // Only distance + target are re-fitted (bounding-sphere) so the whole box stays
    // in frame at that angle -- no clipped corners, whatever angle they chose.
    var scam = { az:cam.az, el:cam.el, dist:cam.dist, tgt:cam.tgt.slice() };
    // reserve margin for the labels: left (elevation) + bottom (distance) big,
    // right small, top for the N arrow. Box fills the rest and is panned into it.
    fitView({L:0.13, R:0.075, T:0.07, B:0.085});
    var shots=[];
    panels.forEach(function (pn) {
      active = pn.side;
      for (var k in vis) vis[k] = pn.ov ? sv[k] : false;
      var mvp = renderScene(true);
      var cc = document.createElement("canvas"); cc.width=pxW; cc.height=pxH;
      var g = cc.getContext("2d"); g.drawImage(canvas, 0, 0);
      drawAxes(g, mvp, pxW, pxH); drawNorth(g, mvp, pxW, pxH);
      if (pn.ov) { drawPeakLabels(g, mvp, pxW, pxH); drawLandslideAnnotations(g, mvp, pxW, pxH); }
      shots.push({ img: cc, cap: pn.cap });
    });
    active=sa; for (var k in vis) vis[k]=sv[k];
    cam.az=scam.az; cam.el=scam.el; cam.dist=scam.dist; cam.tgt=scam.tgt;
    canvas.width=sw; canvas.height=sh; gl.viewport(0,0,sw,sh);       // restore live view
    positionLabels(renderScene(true));
    draw();
    composeFigure(shots, {FW:FW,FH:FH,m:m,TH:TH,capH:capH,colW:colW,rowH:rowH,imgH:imgH});
  }
  function composeFigure(shots, L) {
    var fig = document.createElement("canvas"); fig.width=L.FW; fig.height=L.FH;
    var g = fig.getContext("2d");
    g.fillStyle = "#0b0e13"; g.fillRect(0,0,L.FW,L.FH);
    g.fillStyle = "#f2f5fb"; g.font = "bold 26px sans-serif";
    g.textAlign = "left"; g.textBaseline = "middle";
    g.fillText((CFG.title||"Landslide 3D"), L.m, L.TH/2 + 4);
    var cells = [ [L.m, L.TH+L.m], [2*L.m+L.colW, L.TH+L.m],
                  [L.m, L.TH+2*L.m+L.rowH], [2*L.m+L.colW, L.TH+2*L.m+L.rowH] ];
    shots.forEach(function (s, i) {
      var x=cells[i][0], y=cells[i][1];
      g.drawImage(s.img, x, y, L.colW, L.imgH);
      g.strokeStyle="rgba(255,255,255,0.18)"; g.lineWidth=1; g.strokeRect(x,y,L.colW,L.imgH);
      g.fillStyle="#e6ecf6"; g.font="600 17px sans-serif";
      g.textAlign="center"; g.textBaseline="middle";
      g.fillText(s.cap, x+L.colW/2, y+L.imgH+L.capH/2);
    });
    // details cell (bottom-right)
    var ix=cells[3][0], iy=cells[3][1], yy=iy+8;
    g.textAlign="left"; g.textBaseline="top";
    g.fillStyle="#f2f5fb"; g.font="bold 22px sans-serif"; g.fillText("Details", ix, yy); yy+=38;
    g.font="17px sans-serif";
    [["Coordinate system", CFG.crs||"—"],
     ["Vertical exaggeration", "×"+(+CFG.exaggeration||1)],
     ["Before image", CFG.before_date||"—"],
     ["After image", CFG.after_date||"—"]].forEach(function (kv) {
      g.fillStyle="#8ea0bd"; g.fillText(kv[0]+":", ix, yy);
      g.fillStyle="#e6ecf6"; g.fillText(kv[1], ix+210, yy); yy+=27;
    });
    (CFG.extra_details||[]).forEach(function (kv) {
      if (!kv[1]) return;
      g.fillStyle="#8ea0bd"; g.fillText(kv[0]+":", ix, yy);
      g.fillStyle="#e6ecf6"; g.fillText(kv[1], ix+210, yy); yy+=27;
    });
    yy+=14;
    if (layerMeta.length) {
      g.fillStyle="#aeb9cc"; g.font="600 18px sans-serif"; g.fillText("Overlays", ix, yy); yy+=30;
      g.font="16px sans-serif";
      layerMeta.forEach(function (mm) {
        g.fillStyle="rgb("+mm.color[0]+","+mm.color[1]+","+mm.color[2]+")";
        g.fillRect(ix, yy, 16, 16);
        g.fillStyle="#e6ecf6"; g.fillText(mm.name, ix+24, yy+1); yy+=24;
      });
    }
    var url = fig.toDataURL("image/png");
    var ov = document.createElement("div"); ov.id = "exportModal";
    var bar = document.createElement("div"); bar.className = "exp-bar";
    var zb = document.createElement("button"); zb.className = "btn";
    zb.textContent = "⬇ Download ZIP (slide + panels + orbit GIF)";
    var st = document.createElement("span");
    st.style.cssText = "color:#cbd5e6;font:13px sans-serif;align-self:center;margin:0 6px;";
    var dl = document.createElement("a"); dl.className = "btn"; dl.textContent = "PNG only";
    dl.href = url; dl.download = "landslide_3d_figure.png";
    var cl = document.createElement("button"); cl.className = "btn"; cl.textContent = "Close";
    cl.onclick = function () { document.body.removeChild(ov); };
    zb.onclick = function () {
      zb.disabled = true; st.textContent = "Rendering orbit + encoding GIF… (a few seconds)";
      setTimeout(function () { buildBundle(fig, shots, st, function () { zb.disabled = false; }); }, 40);
    };
    var im = document.createElement("img"); im.src = url;
    bar.appendChild(zb); bar.appendChild(st); bar.appendChild(dl); bar.appendChild(cl);
    ov.appendChild(bar); ov.appendChild(im);
    document.body.appendChild(ov);
  }
  document.getElementById("exportBtn").onclick = exportFigure;

  window.addEventListener("resize", draw);
  setSide(0);
  draw();
})();
'''
