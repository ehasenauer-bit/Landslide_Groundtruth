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
</style>
</head>
<body>
<canvas id="gl"></canvas>
<div id="labels"></div>
<div id="layers"></div>
<div id="hud">
  <button id="beforeBtn" class="btn active">◀ Before</button>
  <button id="afterBtn" class="btn">After ▶</button>
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
  var gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
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
  var lift = Math.max(1, (zmax - zmin)*exag*0.02);
  var poleH = Math.max(2*lift, (zmax - zmin)*exag*0.06);
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
      if (tris.length >= 3) op(drape(tris, 1.0), tris.length, gl.TRIANGLES, c, 0.35, true, P.name);
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
  var scheduled = false;
  function draw() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      resize();
      var asp = canvas.width / Math.max(1, canvas.height);
      var far = cam.dist*4 + span*2 + (zmax - zmin)*exag*4 + 10;
      var proj = mPersp(45*Math.PI/180, asp, Math.max(0.5, cam.dist*0.002), far);
      var view = mLookAt(eyePos(), cam.tgt, [0,0,1]);
      var mvp = mMul(proj, view);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
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
      // draped overlays (polygons / lines / points), one draw-op each
      if (oprog && overlays.length) {
        gl.useProgram(oprog);
        gl.uniformMatrix4fv(oMVP, false, new Float32Array(mvp));
        gl.uniform1f(oPtSize, Math.max(9, Math.min(20, canvas.height/50)));
        for (var oi=0; oi<overlays.length; oi++) {
          var o = overlays[oi], c = o.color;
          if (o.layer && !vis[o.layer]) continue;   // layer toggled off
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
      // position the HTML peak labels by projecting each anchor to the screen
      if (labelItems.length) {
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
    });
  }
  window.addEventListener("resize", draw);
  setSide(0);
  draw();
})();
'''
