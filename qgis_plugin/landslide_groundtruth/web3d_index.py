"""Build a browsable index of the exported 3D web viewers.

Every `Export instant-flip 3D web viewer` writes a self-contained HTML page into
`<out>/viewer3d/web/`. They pile up there and, until now, the only way back to a
past event was to reopen its .qgz in QGIS and export again — the pages were on
disk the whole time with nothing linking them.

This module writes an `index.html` beside them: a thumbnail card per event that
opens its viewer. It is plain stdlib (no QGIS, no numpy) so the scanning rules
and the page itself are unit-testable; the caller supplies a `thumb_fn` to
downsample a scraped PNG, because that is the one step that wants Qt.

Two constraints shape the design:

  * a file:// page cannot list a directory or fetch a manifest (CORS), so the
    card list is baked in at write time and the index is REWRITTEN on every
    export (and on demand from the tab's button) rather than kept live.
  * links between the index and the viewers are relative, so the whole folder
    can be zipped, moved, or synced to another machine and still work.

Older pages predate the metadata block, so their dates and thumbnail are
recovered by streaming the file (`scrape_viewer`) — the "after" texture is
already in there as a base64 PNG.
"""
import base64
import html
import json
import os
import re
import time
import urllib.parse

__all__ = ["META_ID", "read_head_meta", "scrape_viewer",
           "base_stem", "event_key", "collect", "mark_stale",
           "build_index_html", "INDEX_NAME"]

INDEX_NAME = "index.html"

# id of the small JSON block new exports carry in <head>; web3d_export writes
# it (as _META_ID — it cannot import this module, see the note there) and this
# reads it, with test_web3d_index round-tripping the pair. The viewer's real
# payload (#cfg) holds the same facts, but it sits behind multi-megabyte base64
# textures — this copy is a few hundred bytes at the top of the file so the
# index can read a folder of 9 MB pages without touching their bodies.
META_ID = "l3d-meta"

_META_RE = re.compile(
    r'<script[^>]+id="%s"[^>]*>(.*?)</script>' % META_ID, re.S)
_COPY_RE = re.compile(r"^(.*?)\s*\((\d+)\)$")       # "AK2026_3d (2)" -> stem, 2
_HEAD_BYTES = 65536                                  # enough for <head> + meta


def read_head_meta(text):
    """Pull the metadata dict out of an exported page's head; {} if absent."""
    m = _META_RE.search(text)
    if not m:
        return {}
    try:
        out = json.loads(m.group(1).replace("<\\/", "</"))
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _find_json_str(buf, key):
    """Value of a "key":"..." pair inside a JSON blob, or "" — no full parse.

    The viewer's #cfg payload is one JSON object several megabytes wide; parsing
    it to read a date would mean holding the textures in memory too."""
    i = buf.find('"%s":"' % key)
    if i < 0:
        return ""
    i += len(key) + 4
    j = buf.find('"', i)
    return buf[i:j] if j > i else ""


def _scan_meta(buf, out):
    """First-wins harvest of the small facts from one chunk of an export."""
    for key in ("before_date", "after_date"):
        if key not in out:
            v = _find_json_str(buf, key)
            if v:
                out[key] = v
    if "title" not in out:
        m = re.search(r"<title>(.*?)</title>", buf, re.S)
        if m:
            out["title"] = html.unescape(m.group(1)).strip()


def scrape_viewer(path, want_thumb=True, chunk=1 << 20):
    """Recover title/dates/after-texture from a page written before META_ID.

    Streams the file so a 9 MB export costs one pass and one texture in memory
    rather than a parsed multi-megabyte JSON tree. Returns a dict with any of
    title/before_date/after_date/png_b64 it could find (missing keys absent),
    and never raises on a malformed or truncated page — an unreadable card is
    better than a failed index.

    Every chunk is scanned for metadata BEFORE the texture is carved out of it:
    the two live at opposite ends of the payload (<title> in the head, the dates
    behind the textures), and with a 2.7 MB export the after-texture marker can
    fall inside the very first chunk — carving first would drop the title that
    sat earlier in the same buffer."""
    out = {}
    tail = ""
    want = '"after_uri":"data:image/png;base64,'
    grabbing = False
    png = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                buf = tail + block
                if grabbing:                      # mid-texture from the last chunk
                    q = buf.find('"')
                    if q < 0:
                        png.append(buf)
                        tail = ""
                        continue
                    png.append(buf[:q])
                    grabbing = False
                    buf = buf[q:]
                _scan_meta(buf, out)
                if want_thumb and not png:
                    k = buf.find(want)
                    if k >= 0:
                        rest = buf[k + len(want):]
                        q = rest.find('"')
                        if q < 0:
                            png.append(rest)
                            grabbing = True
                            tail = ""
                            continue
                        png.append(rest[:q])
                        _scan_meta(rest[q:], out)  # dates sit just past the texture
                done = ("before_date" in out and "after_date" in out
                        and (not want_thumb or png))
                if done and not grabbing:
                    break
                # overlap so a marker split across the boundary is still found
                tail = buf[-len(want) - 32:]
    except OSError:
        return out
    if png:
        out["png_b64"] = "".join(png)
    return out


def base_stem(stem):
    """Split a filename stem into (stem without a copy suffix, copy number).

    The exporter never clobbers: a second export of one event lands as
    'AK2026-0204_3d (2)'. Those copies are the bulk of a crowded folder."""
    m = _COPY_RE.match(stem)
    if m:
        try:
            return m.group(1), int(m.group(2))
        except ValueError:
            pass
    return stem, 0


def event_key(stem, meta=None):
    """What counts as 'the same event' for supersession.

    Prefers the event id the export recorded; otherwise the filename stem minus
    its copy suffix and the '_3d' the exporter appends."""
    if meta:
        ev = str(meta.get("event") or "").strip()
        if ev:
            return ev.lower()
    s, _ = base_stem(stem)
    if s.endswith("_3d"):
        s = s[:-3]
    return s.lower()


def collect(directory, scrape=None, thumb_fn=None, cache=None):
    """Scan a viewer folder and return the index entries, newest first.

    `scrape` defaults to `scrape_viewer` (injectable for tests). `thumb_fn`
    takes raw PNG bytes and returns a small data: URI — supplied by the QGIS
    side, since downsampling wants QImage; without one the cards render a
    placeholder tile. `cache` is an optional dict keyed by
    "name:size:mtime" so re-indexing a folder does not re-scrape every page."""
    scrape = scrape or scrape_viewer
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    entries = []
    used = set()
    for name in names:
        if not name.lower().endswith(".html") or name == INDEX_NAME:
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        stem = name[:-5]
        ck = "%s:%d:%d" % (name, st.st_size, int(st.st_mtime))
        used.add(ck)
        hit = (cache or {}).get(ck)
        if hit:
            meta, thumb = dict(hit.get("meta") or {}), hit.get("thumb", "")
        else:
            meta, thumb = _read_one(path, scrape, thumb_fn)
            if cache is not None:
                cache[ck] = {"meta": meta, "thumb": thumb}
        entries.append({
            "file": name, "stem": stem, "path": path,
            "event": str(meta.get("event") or "").strip(),
            "key": event_key(stem, meta),
            "title": str(meta.get("title") or "").strip(),
            "before_date": str(meta.get("before_date") or ""),
            "after_date": str(meta.get("after_date") or ""),
            "imagery": str(meta.get("imagery") or ""),
            "mtime": float(st.st_mtime), "size": int(st.st_size),
            "thumb": thumb, "stale": "",
        })
    if cache is not None:
        # Each cached thumbnail is ~30 KB of base64 and the key carries the
        # file's mtime, so every re-export of an event would otherwise leave its
        # old entry behind forever. Keep only what this scan actually used.
        for gone in set(cache) - used:
            del cache[gone]
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return mark_stale(entries)


def _read_one(path, scrape, thumb_fn):
    """Metadata + thumbnail for one page: head block first, scrape as fallback."""
    meta, thumb = {}, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            meta = read_head_meta(f.read(_HEAD_BYTES))
    except OSError:
        meta = {}
    thumb = str(meta.get("thumb") or "")
    need_dates = not (meta.get("before_date") and meta.get("after_date"))
    if need_dates or (thumb_fn and not thumb):
        got = scrape(path, want_thumb=bool(thumb_fn) and not thumb)
        for k, v in got.items():
            if k != "png_b64" and not meta.get(k):
                meta[k] = v
        if thumb_fn and not thumb and got.get("png_b64"):
            try:
                thumb = thumb_fn(base64.b64decode(got["png_b64"])) or ""
            except Exception:
                thumb = ""                    # a bad texture must not kill the index
    return meta, thumb


def mark_stale(entries):
    """Flag superseded exports (newest per event wins) with a reason.

    Two kinds, both hidden behind the index's toggle: a '(2)'-style copy of a
    stem that also exists, and an older export of the same event under any
    name. Everything else — including the pre-naming 'instant_flip_3d' pages —
    stays in the default list."""
    best = {}
    for e in sorted(entries, key=lambda e: e["mtime"], reverse=True):
        k = e["key"]
        if k not in best:
            best[k] = e
            e["stale"] = ""
            continue
        # One stem with '(N)' copies is one family — the plain original is as
        # superseded as its numbered siblings, so they read the same way. A
        # match on event id ALONE is a genuine re-export under another name.
        same_base = base_stem(e["stem"])[0] == base_stem(best[k]["stem"])[0]
        e["stale"] = "superseded copy" if same_base else "older export"
    return entries


def _fmt_size(n):
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%d KB" % (n // 1024)


def _fmt_when(ts, now=None):
    now = time.time() if now is None else now
    lt = time.localtime(ts)
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("today %H:%M", lt)
    return time.strftime("%b %-d %H:%M", lt) if os.name != "nt" else \
        time.strftime("%b %d %H:%M", lt)


def _label(e):
    """What the card calls this export: the event id, else the filename stem."""
    if e.get("event"):
        return e["event"]
    s, n = base_stem(e["stem"])
    if s.endswith("_3d"):
        s = s[:-3]
    # the stem came from a filename-safe event name; give the underscores back.
    # The '(N)' is dropped on the live card — it is an artifact of never
    # clobbering a previous export, not something about this event — but kept
    # on superseded ones, which otherwise all read as the same name.
    return s.replace("_", " ").strip() + (" (%d)" % n if n and e.get("stale") else "")


def _dates_line(e):
    b, a = e.get("before_date", ""), e.get("after_date", "")
    if b and a:
        line = "%s → %s" % (b, a)
    elif a:
        line = "after %s" % a
    elif b:
        line = "before %s" % b
    else:
        line = "dates unknown"
    return line + (" · " + e["imagery"] if e.get("imagery") else "")


def _esc(s):
    return html.escape(str(s), quote=True)


def build_index_html(entries, folder="", now=None):
    """Assemble the index page. `entries` is what `collect` returns."""
    now = time.time() if now is None else now
    live = [e for e in entries if not e["stale"]]
    stale = [e for e in entries if e["stale"]]
    cards = []
    for i, e in enumerate(entries):
        hue = (i * 47 + 200) % 360                 # placeholder tile tint, stable
        thumb = ('<img class="thumb" loading="lazy" alt="" src="%s">' % _esc(e["thumb"])
                 if e.get("thumb") else
                 '<div class="thumb ph" style="--h:%d"><span>no preview</span></div>' % hue)
        badge = ('<span class="badge">%s</span>' % _esc(e["stale"])) if e["stale"] else ""
        cards.append(
            '<a class="card%s" href="%s" data-name="%s" data-mtime="%d" '
            'data-stale="%d"%s>\n'
            '  <div class="thumbwrap">%s%s</div>\n'
            '  <div class="meta"><p class="name">%s</p><p class="dates">%s</p>\n'
            '  <div class="foot"><span>%s</span><span>%s</span></div></div>\n</a>'
            # percent-encoded: an event name is free text, so the filename can
            # hold spaces, parens, or non-ASCII. Relative, never absolute — the
            # folder has to keep working after it is moved or zipped.
            % (" stale" if e["stale"] else "",
               _esc(urllib.parse.quote(e["file"])),
               _esc(_label(e).lower() + " " + _dates_line(e).lower()),
               int(e["mtime"]), 1 if e["stale"] else 0,
               " hidden" if e["stale"] else "", thumb, badge,
               _esc(_label(e)), _esc(_dates_line(e)),
               _esc(_fmt_when(e["mtime"], now)), _esc(_fmt_size(e["size"]))))
    if not entries:
        cards.append('<p class="empty">No viewers exported here yet. Run '
                     '<em>Export instant-flip 3D web viewer</em> in the plugin’s '
                     '3D Viewer tab and this page will list it.</p>')
    n = len(live)
    sub = "%d event%s" % (n, "" if n == 1 else "s")
    if folder:
        sub += " in <code>%s</code>" % _esc(folder)
    sub += " · rebuilt " + time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    toggle = ('<label class="toggle"><input type="checkbox" id="showStale"> '
              'Show %d superseded</label>' % len(stale)) if stale else ""
    # token substitution, not %-formatting: the template is full of CSS percents
    out = _INDEX_TMPL
    for token, value in (("@SUB@", sub), ("@TOGGLE@", toggle),
                         ("@CARDS@", "\n".join(cards))):
        out = out.replace(token, value)
    return out


_INDEX_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landslide 3D viewers</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; background: #0b0e13; color: #dfe6f2;
               font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  .wrap { max-width: 1100px; margin: 0 auto;
          padding-block: 22px; padding-left: 16px; padding-right: 16px; }
  h1 { font-size: 19px; margin: 0 0 4px; font-weight: 600; letter-spacing: .2px; }
  .sub { color: #8593a8; font-size: 13px; margin: 0 0 18px; }
  .sub code { color: #aeb9cc; background: #161c27; border: 1px solid #232c3c;
              border-radius: 4px; padding: 1px 5px; font-size: 12px;
              overflow-wrap: anywhere; }
  .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
         margin-bottom: 16px; }
  input[type=search], select { appearance: none; background: #161c27;
      border: 1px solid #33405a; color: #dfe6f2; border-radius: 8px;
      padding: 7px 10px; font-size: 13px; font-family: inherit; }
  input[type=search] { flex: 1 1 200px; min-width: 0; }
  .toggle { display: flex; align-items: center; gap: 7px; color: #aeb9cc;
            font-size: 13px; background: #161c27; border: 1px solid #33405a;
            border-radius: 8px; padding: 7px 11px; cursor: pointer; }
  .toggle input { accent-color: #2d6cdf; margin: 0; }
  .grid { display: grid; gap: 14px;
          grid-template-columns: repeat(auto-fill, minmax(228px, 1fr)); }
  .card { display: block; text-decoration: none; color: inherit;
          background: #161c27; border: 1px solid #33405a; border-radius: 10px;
          overflow: hidden; transition: border-color .12s, transform .12s; }
  .card:hover, .card:focus-visible { border-color: #2d6cdf; transform: translateY(-2px); }
  /* .card sets display, and an author rule outranks the UA's [hidden] one — so
     without this the filter and the superseded toggle set .hidden and nothing
     moves. */
  .card[hidden] { display: none; }
  .thumbwrap { position: relative; }
  .thumb { display: block; width: 100%; max-width: 100%; aspect-ratio: 16/10;
           object-fit: cover; background: #0d1220; }
  .thumb.ph { display: grid; place-items: center; color: #8593a8; font-size: 12px;
              background: linear-gradient(155deg, hsl(var(--h) 14% 26%),
                                          hsl(var(--h) 18% 13%)); }
  .badge { position: absolute; left: 8px; top: 8px; font-size: 11px;
           letter-spacing: .3px; background: rgba(11,14,19,.8);
           border: 1px solid #5a4a26; border-radius: 999px; padding: 2px 8px;
           color: #d9a441; }
  .meta { padding: 10px 12px 12px; }
  .name { font-size: 14px; font-weight: 600; margin: 0 0 3px;
          overflow-wrap: anywhere; }
  .dates { color: #aeb9cc; font-size: 12px; margin: 0 0 6px; }
  .foot { color: #8593a8; font-size: 11.5px; display: flex;
          justify-content: space-between; gap: 8px; }
  .card.stale { opacity: .55; }
  .empty { color: #8593a8; font-size: 13px; line-height: 1.6; }
  .note { margin-top: 22px; color: #8593a8; font-size: 12px;
          border-top: 1px solid #1e2634; padding-top: 12px; line-height: 1.6; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Landslide 3D viewers</h1>
  <p class="sub">@SUB@</p>
  <div class="bar">
    <input type="search" id="q" placeholder="Filter by event or date…"
           autocomplete="off">
    <select id="sort">
      <option value="new">Newest export first</option>
      <option value="old">Oldest first</option>
      <option value="name">Event name</option>
    </select>
    @TOGGLE@
  </div>
  <div class="grid" id="grid">
@CARDS@
  </div>
  <p class="note">Each card opens a self-contained viewer next to this file —
  no QGIS, no network. Move or zip the whole folder and the links still work.
  Rebuilt automatically on every export.</p>
</div>
<script>
(function () {
  var grid = document.getElementById("grid");
  var cards = [].slice.call(grid.querySelectorAll(".card"));
  var q = document.getElementById("q"), sort = document.getElementById("sort");
  var showStale = document.getElementById("showStale");
  function apply() {
    var t = (q.value || "").trim().toLowerCase();
    var stale = showStale && showStale.checked;
    cards.forEach(function (c) {
      var hit = !t || (c.dataset.name || "").indexOf(t) >= 0;
      c.hidden = !hit || (c.dataset.stale === "1" && !stale);
    });
    var mode = sort.value;
    cards.slice().sort(function (a, b) {
      if (mode === "name")
        return (a.dataset.name || "").localeCompare(b.dataset.name || "");
      var d = (+b.dataset.mtime) - (+a.dataset.mtime);
      return mode === "old" ? -d : d;
    }).forEach(function (c) { grid.appendChild(c); });
  }
  q.addEventListener("input", apply);
  sort.addEventListener("change", apply);
  if (showStale) showStale.addEventListener("change", apply);
  apply();
})();
</script>
</body>
</html>
"""
