#!/usr/bin/env python3
"""Strip the Planet API key out of Planet layer sources in saved QGIS projects.

One-off cleanup. Until 2026-10-13 the PlanetScope tab built its tile-preview layers as
    https://tiles{0-3}.planet.com/data/v1/layers/<hash>/{z}/{x}/{y}?api_key=<KEY>
and QGIS saves every layer source into the project, so each .qgz that ever held a
preview carries the key in plain text. The tab no longer does this: the tile hash is
all a tile request needs (checked live: no key, a bogus key and the real key all get
the same bytes back), and the key is only sent on the POST that creates the hash.

This rewrites old projects to that same form: it removes `api_key=...` from every
*.planet.com URL in the project XML and leaves everything else byte-for-byte alone.
Other vendors' keys (a Stadia Maps basemap, say) are reported but never touched.

    python3 strip_planet_keys.py DIR_OR_FILE [...]            # dry run: report only
    python3 strip_planet_keys.py DIR_OR_FILE [...] --apply    # back up, then rewrite

--apply copies each file it will change into --backup-dir first (default
~/planet_key_backups/<timestamp>/, mode 700), checks the copy byte-for-byte, writes
the cleaned project beside the original, checks THAT (valid zip, XML parses, no
Planet key left), and only then swaps it in. The backups still hold the key: delete
them once the key is rotated and the projects open.

Close these projects in QGIS first. An open project keeps the keyed layer source in
memory and puts it straight back on the next save.

Prints key FINGERPRINTS (first 8 hex digits of sha256), never key values.
Standard library only, so it runs on any python3.
"""
import argparse
import base64
import hashlib
import os
import re
import shutil
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

# A URL on any planet.com host, up to the end of the attribute or element it sits in.
PLANET_URL = re.compile(r'https?://(?:[a-z0-9-]+\.)*planet\.com/[^"<\s]*', re.I)
# One api_key parameter inside such a URL, with the separator in front of it. The url
# may be stored raw (?api_key=K) or percent-encoded (%3Fapi_key%3DK); `follow` is a
# percent-encoded '&' starting the NEXT url parameter, which then needs the '?' back.
# A raw '&amp;' after the key is not consumed: in QGIS's raw-url form it starts the
# next datasource parameter (zmax=...), not a url parameter.
KEY_PARAM = re.compile(
    r'(?P<sep>\?|%3F|&amp;|%26)api_key(?:=|%3D)(?P<key>[A-Za-z0-9_-]{8,})(?P<follow>%26)?',
    re.I)
# Any api_key on any host, for the report of keys this script does NOT touch.
ANY_KEY = re.compile(r'api_key(?:=|%3D)([A-Za-z0-9_-]{8,})', re.I)

PROJECT_EXT = (".qgz", ".qgs")


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def strip_text(text):
    """(cleaned text, [planet key values removed]) for one project XML string."""
    removed = []

    def drop_param(m):
        removed.append(m.group("key"))
        sep, follow = m.group("sep"), m.group("follow")
        if follow:                       # another url param follows: keep one separator
            return sep if sep.lower() in ("?", "%3f") else follow
        return ""

    def clean_url(m):
        return KEY_PARAM.sub(drop_param, m.group(0))

    return PLANET_URL.sub(clean_url, text), removed


def other_keys(text):
    """Fingerprints of api_key values on hosts other than planet.com."""
    planet = set()
    for m in PLANET_URL.finditer(text):
        planet.update(k.group("key") for k in KEY_PARAM.finditer(m.group(0)))
    return sorted({fingerprint(k) for k in ANY_KEY.findall(text) if k not in planet})


def leftover(blob, keys):
    """True if any of `keys` is still in `blob`, raw or as an HTTP Basic token."""
    return any(k.encode() in blob or base64.b64encode(f"{k}:".encode()) in blob
               for k in keys)


def read_project(path):
    """{member name: bytes} for a .qgz, or {'': bytes} for a plain .qgs."""
    if path.lower().endswith(".qgs"):
        with open(path, "rb") as f:
            return {"": f.read()}
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def plan(path):
    """What --apply would do to one project: a dict, or None if it holds no Planet key."""
    members = read_project(path)
    changed, removed, others, examples = {}, [], set(), []
    for name, data in members.items():
        if not (name == "" or name.lower().endswith(".qgs")):
            continue
        text = data.decode("utf-8")
        new, gone = strip_text(text)
        others.update(other_keys(text))
        if gone:
            changed[name] = new.encode("utf-8")
            removed += gone
            m = next(u for u in PLANET_URL.finditer(text) if KEY_PARAM.search(u.group(0)))
            examples.append((redact(m.group(0)), strip_text(m.group(0))[0]))
    # a key anywhere OUTSIDE the project XML (aux storage, attachments) can't be
    # rewritten safely here; refuse that file rather than half-clean it
    keys = set(removed)
    stray = [n for n, d in members.items() if n not in changed and n != "" and
             not n.lower().endswith(".qgs") and leftover(d, keys)]
    if not removed and not stray:
        return {"path": path, "others": sorted(others)} if others else None
    return {"path": path, "members": members, "changed": changed, "keys": keys,
            "hits": len(removed), "others": sorted(others), "stray": stray,
            "example": examples[0] if examples else None}


def redact(url):
    return KEY_PARAM.sub(lambda m: f"{m.group('sep')}api_key=<KEY {fingerprint(m.group('key'))}>"
                         + (m.group("follow") or ""), url)


def write_project(path, members, changed):
    """Write the cleaned project beside `path`; return the temp path (not yet swapped in)."""
    tmp = path + ".stripping.tmp"
    if path.lower().endswith(".qgs"):
        with open(tmp, "wb") as f:
            f.write(changed[""])
        return tmp
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
        for info in src.infolist():         # same members, order, dates, compression
            dst.writestr(info, changed.get(info.filename, members[info.filename]),
                         compress_type=info.compress_type)
    return tmp


def verify(tmp, keys, is_qgs):
    """Problems with the cleaned file, [] if none."""
    problems = []
    try:
        members = ({"": open(tmp, "rb").read()} if is_qgs else read_project(tmp))
        if not is_qgs:
            with zipfile.ZipFile(tmp) as z:
                bad = z.testzip()
                if bad:
                    problems.append(f"corrupt zip member {bad}")
    except (zipfile.BadZipFile, OSError) as e:
        return [f"unreadable: {e}"]
    for name, data in members.items():
        if name == "" or name.lower().endswith(".qgs"):
            try:
                ET.fromstring(data)
            except ET.ParseError as e:
                problems.append(f"{name or 'project'} no longer parses: {e}")
        if leftover(data, keys):
            problems.append(f"{name or 'project'} still holds a Planet key")
    return problems


def find_projects(roots, skip):
    for root in roots:
        if os.path.isfile(root):
            if root.lower().endswith(PROJECT_EXT):
                yield root
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = sorted(d for d in dn if os.path.join(dp, d) != skip
                           and not d.startswith("."))
            for f in sorted(fn):
                if f.lower().endswith(PROJECT_EXT):
                    yield os.path.join(dp, f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", help="project files or folders to scan")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the files (default: dry run, change nothing)")
    ap.add_argument("--backup-dir", default=os.path.expanduser(
        os.path.join("~", "planet_key_backups", time.strftime("%Y%m%d-%H%M%S"))),
        help="where originals are copied before rewriting (default: %(default)s)")
    a = ap.parse_args(argv)
    backup_root = os.path.abspath(a.backup_dir)

    scanned = 0
    plans = []
    for path in find_projects(a.paths, backup_root):
        scanned += 1
        try:
            p = plan(path)
        except (zipfile.BadZipFile, UnicodeDecodeError, OSError, KeyError) as e:
            print(f"  SKIP  {path}: cannot read ({e.__class__.__name__}: {e})")
            continue
        if p:
            plans.append(p)

    todo = [p for p in plans if p.get("hits") or p.get("stray")]
    keys = sorted({fingerprint(k) for p in todo for k in p["keys"]})
    print(f"{scanned} project(s) scanned; {len(todo)} hold a Planet key "
          f"({sum(p['hits'] for p in todo)} layer source(s); key fingerprints: "
          f"{', '.join(keys) or '-'})")
    for p in todo:
        fps = ", ".join(sorted(fingerprint(k) for k in p["keys"]))
        print(f"\n  {p['path']}\n      {p['hits']} source(s), key {fps}")
        if p["example"]:
            print(f"      - {p['example'][0]}\n      + {p['example'][1]}")
        if p["stray"]:
            print(f"      ! key also in {', '.join(p['stray'])} -- will NOT be rewritten")
    others = [p for p in plans if p["others"]]
    if others:
        print(f"\nNot touched -- api_key on a non-Planet host in {len(others)} project(s) "
              f"(fingerprints {', '.join(sorted({f for p in others for f in p['others']}))}):")
        for p in others:
            print(f"      {p['path']}")

    if not a.apply:
        print("\nDry run: nothing was changed. Re-run with --apply to back up and rewrite.")
        return 0
    if not todo:
        return 0

    os.makedirs(backup_root, mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    common = os.path.commonpath([os.path.abspath(p["path"]) for p in todo])
    if os.path.isfile(common):
        common = os.path.dirname(common)
    done = failed = 0
    print(f"\nBacking up to {backup_root}")
    for p in todo:
        path = p["path"]
        if p["stray"]:
            print(f"  SKIP  {path}: key outside the project XML, fix by hand")
            failed += 1
            continue
        rel = os.path.relpath(os.path.abspath(path), common)
        bak = os.path.join(backup_root, rel)
        os.makedirs(os.path.dirname(bak), mode=0o700, exist_ok=True)
        shutil.copy2(path, bak)
        os.chmod(bak, 0o600)
        with open(path, "rb") as f1, open(bak, "rb") as f2:
            if f1.read() != f2.read():
                print(f"  FAIL  {path}: backup does not match the original; left alone")
                failed += 1
                continue
        tmp = write_project(path, p["members"], p["changed"])
        problems = verify(tmp, p["keys"], path.lower().endswith(".qgs"))
        if problems:
            os.remove(tmp)
            print(f"  FAIL  {path}: {'; '.join(problems)}; left alone")
            failed += 1
            continue
        os.replace(tmp, path)
        # re-read what is now on disk: a synced folder can serve a stale copy
        after = verify(path, p["keys"], path.lower().endswith(".qgs"))
        if after:
            print(f"  FAIL  {path}: after the swap: {'; '.join(after)} -- restore from {bak}")
            failed += 1
            continue
        print(f"  done  {path}  ({p['hits']} source(s))")
        done += 1
    print(f"\n{done} rewritten, {failed} failed. Backups (they still hold the key): "
          f"{backup_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
