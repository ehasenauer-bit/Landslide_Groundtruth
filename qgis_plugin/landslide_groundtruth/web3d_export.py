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
</style>
</head>
<body>
<canvas id="gl"></canvas>
<div id="hud">
  <button id="beforeBtn" class="btn active">◀ Before</button>
  <button id="afterBtn" class="btn">After ▶</button>
</div>
<div id="tag"></div>
<div id="help">
  drag orbit · scroll zoom · shift-drag pan<br>
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
    drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var ddx = e.clientX - drag.x, ddy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY;
    if (drag.pan) {
      // planar pan: move the look-at target in the ground plane. Screen-right
      // maps to (sin az, -cos az); screen-up (drag toward viewer) to (cos az, sin az).
      var ce = Math.cos(cam.az), se = Math.sin(cam.az);
      var k = cam.dist / 700;
      cam.tgt[0] -= se*ddx*k;  cam.tgt[1] += ce*ddx*k;
      cam.tgt[0] += ce*ddy*k;  cam.tgt[1] += se*ddy*k;
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
    });
  }
  window.addEventListener("resize", draw);
  setSide(0);
  draw();
})();
'''
