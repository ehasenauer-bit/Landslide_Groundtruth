#!/usr/bin/env python3
"""The index of exported 3D viewers must find, and must not lose, past events.

The index exists because a self-contained viewer is a dead end: the pages pile
up in one folder with nothing linking them, so the only way back to a past event
was to reopen its .qgz and export again. A file:// page cannot list a directory,
so the card list is baked in at write time — which makes the scan rules, not a
live query, the thing that can silently drop an event.

Covers the three ways an event goes missing from that list: a page exported
before the metadata block existed (must still be scraped), a copy-suffixed
re-export (must be collapsed, not shown twice), and a supersession rule that is
too eager (a distinct event must not be hidden behind the toggle).
"""
import base64
import io
import os
import struct
import sys
import tempfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from landslide_groundtruth import web3d_index as X                   # noqa: E402
from landslide_groundtruth import web3d_export as W                  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        fails.append(name)


def png_bytes(w=8, h=5):
    """A real, decodable PNG — the scraper's output has to survive a decode."""
    raw = b"".join(b"\x00" + bytes([(x * 31) % 256] * 3 * w) for x in range(h))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_B64 = base64.b64encode(png_bytes()).decode("ascii")


def legacy_page(before="2026-02-01", after="2026-02-05", pad=3000):
    """A viewer as exported BEFORE the head metadata block: one huge JSON blob
    with the title at the top and the dates behind the textures."""
    return ('<!doctype html><html><head><title>Landslide 3D — before / after'
            '</title></head><body><script id="cfg" type="application/json">'
            '{"title":"Landslide 3D","elev_b64":"' + "A" * pad + '",'
            '"before_uri":"data:image/png;base64,' + "B" * pad + '",'
            '"after_uri":"data:image/png;base64,' + PNG_B64 + '",'
            '"before_date":"' + before + '","after_date":"' + after + '"}'
            '</script></body></html>')


# --- 1. a page written today announces itself in its own head ---------------
meta = {"event": "AK2026-0204", "before_date": "2026-02-01",
        "after_date": "2026-02-05", "imagery": "Sentinel-2", "thumb": "data:x"}
html = W.build_viewer_html({"title": "Landslide 3D", "ncols": 2, "nrows": 2,
                            "elev_b64": "", "before_uri": "a", "after_uri": "b",
                            "index_meta": meta})
head = html[:65536]
# the writer (web3d_export._META_ID) cannot import the reader, so the block's
# id lives in both files; this is what stops them drifting apart silently.
check("writer and reader agree on the block id", X.META_ID == W._META_ID,
      "%r vs %r" % (X.META_ID, W._META_ID))
check("head metadata round-trips", X.read_head_meta(head) == meta,
      repr(X.read_head_meta(head))[:120])
check("metadata sits in the head, not behind the payload",
      html.index(X.META_ID) < html.index('id="cfg"'))
check("index_meta is not duplicated into the payload",
      '"index_meta"' not in html.split('id="cfg"')[1])
check("the viewer still carries a way back", 'href="index.html"' in html)
check("the title survived the insertion", "<title>Landslide 3D</title>" in html)

# --- 2. an older page is recovered by scraping ------------------------------
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "old_3d.html")
    io.open(p, "w", encoding="utf-8").write(legacy_page())
    for chunk in (1 << 20, 512, 97, 13):   # force splits mid-marker, mid-base64
        got = X.scrape_viewer(p, chunk=chunk)
        ok = (got.get("before_date") == "2026-02-01"
              and got.get("after_date") == "2026-02-05"
              and got.get("png_b64") == PNG_B64
              and "before / after" in got.get("title", ""))
        check("scrape survives a %d-byte chunk boundary" % chunk, ok,
              str({k: v[:24] for k, v in got.items()}))
    check("scraped texture still decodes to a PNG",
          base64.b64decode(X.scrape_viewer(p)["png_b64"])[:4] == b"\x89PNG")

# --- 3. what counts as superseded ------------------------------------------
def entry(stem, mtime, event=""):
    return {"file": stem + ".html", "stem": stem, "path": "", "event": event,
            "key": X.event_key(stem, {"event": event} if event else None),
            "title": "", "before_date": "", "after_date": "", "imagery": "",
            "mtime": mtime, "size": 1024, "thumb": "", "stale": ""}


rows = X.mark_stale([
    entry("AK2026-0204_3d", 300, "AK2026-0204"),
    entry("AK2026-0204_3d (2)", 100, "AK2026-0204"),
    entry("AK2026-0204_3d (3)", 200, "AK2026-0204"),
    entry("landslide_2025-10-09_3d", 250),
    entry("instant_flip_3d", 50),
])
by = {r["stem"]: r["stale"] for r in rows}
check("newest copy is the live one", by["AK2026-0204_3d"] == "")
check("older copies are superseded",
      by["AK2026-0204_3d (2)"] == "superseded copy"
      and by["AK2026-0204_3d (3)"] == "superseded copy", str(by))
check("a different event is not collateral", by["landslide_2025-10-09_3d"] == "")
check("the pre-naming page still shows", by["instant_flip_3d"] == "", str(by))

# an event re-exported under a DIFFERENT filename is still one event
rows = X.mark_stale([entry("azumi_3d", 100, "AZ-1"), entry("azumi_v2_3d", 200, "AZ-1")])
by = {r["stem"]: r["stale"] for r in rows}
check("older export of the same event id is superseded",
      by["azumi_3d"] == "older export" and by["azumi_v2_3d"] == "", str(by))

check("copy suffix parses", X.base_stem("AK_3d (2)") == ("AK_3d", 2)
      and X.base_stem("AK_3d") == ("AK_3d", 0))
check("an event name with its own parens is not a copy",
      X.base_stem("Azumi (north) slide") == ("Azumi (north) slide", 0))

# --- 4. the page itself -----------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    for name, body in (("old_3d.html", legacy_page("2026-01-01", "2026-01-09")),
                       ("old_3d (2).html", legacy_page("2026-01-01", "2026-01-09"))):
        io.open(os.path.join(d, name), "w", encoding="utf-8").write(body)
    io.open(os.path.join(d, "index.html"), "w", encoding="utf-8").write("stale")
    cache = {}
    got = X.collect(d, thumb_fn=lambda raw: "data:image/jpeg;base64,ZZ", cache=cache)
    check("collect skips its own index page",
          [e["file"] for e in got] and all(e["file"] != "index.html" for e in got),
          str([e["file"] for e in got]))
    check("collect found both exports", len(got) == 2, str(len(got)))
    check("collect scraped the dates",
          all(e["after_date"] == "2026-01-09" for e in got), str(got[0]))
    check("collect built thumbnails", all(e["thumb"].endswith("ZZ") for e in got))
    check("collect filled the cache", len(cache) == 2, str(list(cache)))

    calls = []
    def counting_scrape(path, want_thumb=True, chunk=1 << 20):
        calls.append(path)
        return X.scrape_viewer(path, want_thumb, chunk)
    X.collect(d, scrape=counting_scrape,
              thumb_fn=lambda raw: "x", cache=cache)
    check("a cached folder is not re-scraped", calls == [], str(calls))

    cache["old_3d.html:1:1"] = {"meta": {}, "thumb": "x" * 40000}   # a past export
    X.collect(d, thumb_fn=lambda raw: "x", cache=cache)
    check("the cache drops entries for files that are gone",
          "old_3d.html:1:1" not in cache and len(cache) == 2, str(list(cache)))

    page = X.build_index_html(got, folder="out/interactive/viewer3d/web")
    check("one card per export", page.count('class="card') == 2, str(page.count("card")))
    check("the superseded copy is hidden by default",
          'data-stale="1" hidden>' in page and "Show 1 superseded" in page)
    # .card sets `display`, which outranks the UA's [hidden] rule: without an
    # explicit author rule the toggle and the filter set .hidden and nothing
    # visibly moves — the page looks broken while the DOM says it is fine.
    check("hiding a card actually hides it",
          ".card[hidden] { display: none; }" in page)
    check("CSS percentages survived templating",
          "width: 100%" in page and "@SUB@" not in page and "@CARDS@" not in page)
    check("cards link relatively, not to this machine",
          'href="old_3d.html"' in page and d not in page)
    check("a filename with spaces/parens is percent-encoded",
          'href="old_3d%20%282%29.html"' in page,
          [l for l in page.splitlines() if "href=" in l][:2])
    check("the empty folder still renders a page",
          "No viewers exported here yet" in X.build_index_html([]))

# --- 5. nothing here may raise on a broken or truncated page ----------------
with tempfile.TemporaryDirectory() as d:
    io.open(os.path.join(d, "trunc_3d.html"), "w", encoding="utf-8").write(
        legacy_page()[:1200])
    io.open(os.path.join(d, "empty_3d.html"), "w", encoding="utf-8").write("")
    try:
        got = X.collect(d, thumb_fn=lambda raw: 1 / 0)   # a thumb that explodes
        page = X.build_index_html(got)
        check("a truncated / empty / unthumbnailable page still lists",
              len(got) == 2 and page.count('class="card') == 2)
    except Exception as e:
        check("a truncated / empty / unthumbnailable page still lists", False,
              "%s: %s" % (type(e).__name__, e))

print(("FAILED: " + ", ".join(fails)) if fails else "all web3d_index checks passed")
sys.exit(1 if fails else 0)
