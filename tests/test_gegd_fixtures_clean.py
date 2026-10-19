"""The GEGD test fixtures are synthetic: nothing key-shaped, licensed or personal.

Run with tests/run_all.sh, or any python at all:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_fixtures_clean.py

This repository is PUBLIC, and a real GEGD response is not safe to commit: it
carries licensed-catalog identifiers, factory order numbers, third-party vendor
records, and (from the projects endpoint) other users' e-mail addresses. The key
itself is a 48-character base64 string that decodes to a UUID, and the probes
showed the server copying a query-string key into every link it returns. So
tests/fixtures/gegd/ may hold only what make_fixtures.py writes, and this file
fails on:

  * any base64 run of 40+ characters that decodes to a UUID (the key's shape),
    raw or URL-encoded, and any maxar-api-key / maxar_api_key with a value;
  * e-mail addresses;
  * the third-party and ingested product names, factoryOrderNumber,
    projectName, order-number-shaped strings, and the projects endpoint's
    *_email fields;
  * a fixture without the synthetic marker, and any TIFF without the
    generator's tag;
  * any file the generator would not have written byte-for-byte (so a captured
    response dropped in by hand fails even if it happens to pass the rest).

Each rule is first shown to FIRE on a planted bad file, so a green run means the
scanner works, not just that it found nothing.
"""
import base64
import binascii
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from urllib.parse import quote, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "gegd")
GENERATOR = "make_fixtures.py"

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


# ---------------------------------------------------------------- the rules

TIFF_MARK = b"LANDSLIDE_GEGD_SYNTHETIC"
BANNED = ("PRIVATE_FEATURE", "THIRD_PARTY", "factoryOrderNumber", "projectName",
          "owner_email", "updated_by_email")
B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                     r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ORDER_NO = re.compile(r"\b\d{12}_\d{2}_P\d{3}\b")
KEY_VALUE = re.compile(r"maxar[-_ ]?api[-_ ]?key[\"']?\s*[:=]\s*[\"']?(?!<KEY>)[^\s\"'&<]",
                       re.I)
TEXT_EXT = (".json", ".xml", ".txt", ".py", ".csv", ".md", ".html")


def decodes_to_uuid(token):
    t = token.rstrip("=")
    for alt in (None, b"-_"):
        try:
            raw = base64.b64decode(t + "=" * (-len(t) % 4), altchars=alt, validate=True)
            s = raw.decode("ascii").strip()
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if UUID_RE.match(s):
            return True
    return False


def content_problems(text):
    """Rules that apply to every file's text, raw and URL-decoded."""
    out = []
    for form in {text, unquote(text)}:
        for m in B64_RUN.finditer(form):
            if decodes_to_uuid(m.group(0)):
                out.append(f"key-shaped base64 run ({len(m.group(0))} chars)")
        if KEY_VALUE.search(form):
            out.append("maxar api key with a value")
        if EMAIL.search(form):
            out.append("e-mail address")
        if ORDER_NO.search(form):
            out.append("order-number-shaped string")
        for b in BANNED:
            if b in form:
                out.append(f"banned token {b}")
    return sorted(set(out))


def marker_problem(name, data):
    """'' if the file carries the synthetic marker for its type."""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".py":
        return "" if name == GENERATOR else "stray script in the fixture folder"
    if ext == ".json":
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "unreadable JSON"
        ok = isinstance(doc, dict) and doc.get("_synthetic") is True
        return "" if ok else 'JSON without "_synthetic": true'
    if ext == ".xml":
        return "" if b"<!-- _synthetic" in data else "XML without the synthetic comment"
    if ext == ".txt":
        ok = any(l.startswith(b"# _synthetic") for l in data.splitlines())
        return "" if ok else "text without the # _synthetic line"
    if ext in (".tif", ".tiff"):
        return "" if TIFF_MARK in data else "TIFF without the generator's tag"
    return "" if TIFF_MARK in data else f"unknown {ext or 'extensionless'} file without the tag"


def scan(folder):
    """{relative path: [problems]} for every file under folder."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, folder)
            if fn == ".DS_Store":
                continue
            with open(path, "rb") as f:
                data = f.read()
            probs = []
            m = marker_problem(fn, data)
            if m:
                probs.append(m)
            text = data.decode("utf-8", "replace") if (fn.lower().endswith(TEXT_EXT)) \
                else data.decode("latin-1")
            probs += content_problems(text)
            if probs:
                found[rel] = probs
    return found


# ---------------------------------------------------------------- 1. the scanner fires

print("=== 1. every rule fires on a planted bad file ===")
KEYLIKE = base64.b64encode(str(uuid.UUID(int=0xBADC0DE)).encode()).decode()
AT = "@"
planted = {
    "key.json": json.dumps({"_synthetic": True, "x": KEYLIKE}),
    "key_urlenc.txt": "u=" + quote(KEYLIKE, safe="") + "\n# _synthetic\n",
    "key_urlsafe.json": json.dumps({"_synthetic": True,
                                    "x": base64.urlsafe_b64encode(str(uuid.UUID(int=7)).encode()).decode()}),
    "key_value.txt": "maxar_api_key=abcdefgh\n# _synthetic\n",
    "header_value.json": json.dumps({"_synthetic": True, "h": "maxar-api-key: zzz"}),
    "email.json": json.dumps({"_synthetic": True, "owner": "someone" + AT + "example.org"}),
    "third.json": json.dumps({"_synthetic": True, "productName": "THIRD" + "_PARTY"}),
    "ingested.json": json.dumps({"_synthetic": True, "productName": "PRIVATE" + "_FEATURE"}),
    "order.json": json.dumps({"_synthetic": True, "factory" + "OrderNumber": "x"}),
    "order_shape.txt": "1" * 12 + "_01_P001\n# _synthetic\n",
    "nomark.json": json.dumps({"type": "FeatureCollection", "features": []}),
    "nomark_list.json": json.dumps([{"_synthetic": True}]),
    "nomark.xml": "<a/>",
    "nomark.txt": "No JWT bearer token found\n",
    "untagged.tif": b"II*\x00" + b"\x00" * 64,
    "stray.py": "print('hi')\n",
    "capture.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
}
want = {
    "key.json": "key-shaped", "key_urlenc.txt": "key-shaped", "key_urlsafe.json": "key-shaped",
    "key_value.txt": "with a value", "header_value.json": "with a value",
    "email.json": "e-mail", "third.json": "THIRD_PARTY", "ingested.json": "PRIVATE_FEATURE",
    "order.json": "factoryOrderNumber", "order_shape.txt": "order-number",
    "nomark.json": "_synthetic", "nomark_list.json": "_synthetic", "nomark.xml": "synthetic comment",
    "nomark.txt": "# _synthetic", "untagged.tif": "TIFF without", "stray.py": "stray script",
    "capture.png": "without the tag",
}
benign = {
    "ok.json": json.dumps({"_synthetic": True,
                           "featureId": str(uuid.uuid5(uuid.NAMESPACE_URL, "x")),
                           "sha1": "a" * 40,
                           "b64": base64.b64encode(bytes(range(60))).decode(),
                           "header_name_only": "send maxar-api-key; value redacted <KEY>",
                           "redacted": "maxar_api_key=<KEY>",
                           "prose": "third-party imagery; factory order numbers are left out"}),
    "ok.xml": "<?xml version='1.0'?><!-- _synthetic: x --><a/>",
    "ok.txt": "body\n# _synthetic: x\n",
    "ok.tif": b"II*\x00" + b"\x00" * 16 + TIFF_MARK,
}
tmp = tempfile.mkdtemp(prefix="gegd_scan_")
try:
    for name, content in list(planted.items()) + list(benign.items()):
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode("utf-8"))
    got = scan(tmp)
    for name, needle in want.items():
        probs = got.get(name, [])
        check(f"{name}: flagged ({needle})", any(needle in p for p in probs))
    for name in benign:
        check(f"{name}: NOT flagged (UUID ids, hex digests, non-UUID base64, the header "
              f"name, <KEY>, prose)" if name == "ok.json" else f"{name}: NOT flagged",
              name not in got)
    check("nothing flagged that was not planted", set(got) == set(want))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 2. the real folder

print("\n=== 2. the committed fixtures pass every rule ===")
names = sorted(n for n in os.listdir(FIX)
               if n != "__pycache__" and n != ".DS_Store" and not n.startswith("."))
data_files = [n for n in names if n != GENERATOR]
check(f"the folder exists with the generator and {len(data_files)} fixtures",
      GENERATOR in names and len(data_files) >= 10)
problems = scan(FIX)
for rel, probs in problems.items():
    print(f"       {rel}: {probs}")
check(f"0 problems in {len(names)} files", not problems)

print("\n=== 3. every fixture is exactly what the generator writes ===")
spec = importlib.util.spec_from_file_location("gegd_make_fixtures", os.path.join(FIX, GENERATOR))
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)
out = tempfile.mkdtemp(prefix="gegd_regen_")
try:
    written = gen.build(out)
    check("the generator writes the same file set as is committed (no orphans, none missing)",
          sorted(written) == sorted(data_files))
    for n in written:
        with open(os.path.join(out, n), "rb") as a:
            fresh = a.read()
        committed = open(os.path.join(FIX, n), "rb").read() if n in data_files else None
        check(f"{n}: byte-identical to a fresh build", committed == fresh)
    check("a second build is identical too (deterministic)",
          all(open(os.path.join(out, n), "rb").read() == gen.files()[n].encode("utf-8")
              for n in written))
    check("the fresh build also scans clean", not scan(out))
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
