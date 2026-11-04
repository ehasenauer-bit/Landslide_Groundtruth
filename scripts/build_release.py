"""Build the zip that goes to plugins.qgis.org (or 'Install from ZIP').

    python3 scripts/build_release.py            # -> dist/landslide_groundtruth-<version>.zip
    python3 scripts/build_release.py --relock   # recompile requirements.lock first

The plugin folder alone can't run a search: the imagery pipeline (run_single.py
and the modules it imports) lives at the repository root, where the command-line
workflow and the tests use it. The zip therefore carries a copy of exactly those
files in landslide_groundtruth/pipeline/, which environment.bundled_pipeline_dir()
finds. Nothing at the repo root moves.

Also checked here, before anything is uploaded: the metadata keys the repository
requires, that only the files a plugin should hold are in the zip (no
__pycache__, no shell scripts or binaries the upload scan blocks), the 25 MB
limit, and file modes (some files here are 0600 on disk).

--relock recompiles qgis_plugin/landslide_groundtruth/requirements.lock from
requirements.txt with uv, constrained to the versions installed in ./venv when
it exists (so the lock pins what was actually tested). Needs uv on PATH, or
--uv /path/to/uv.

Standard library only; run it with any Python 3.9+.
"""
import argparse
import configparser
import os
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
NAME = "landslide_groundtruth"

# What `run_single.py` imports, directly or lazily, plus the two files QGIS's own
# Python loads from the pipeline folder (planet_cache, larsen_BR_volume) and the
# setup's smoke test. Re-check when a pipeline module gains a local import.
PIPELINE = ("run_single.py", "run_groundtruth.py", "imagery.py", "review_package.py",
            "planet_imagery.py", "planet_cache.py", "sar_imagery.py",
            "nisar_imagery.py", "dem_imagery.py", "larsen_BR_volume.py",
            "setup_check.py")
EXTRA = ("LICENSE",)

REQUIRED_META = ("name", "qgisMinimumVersion", "description", "about", "version",
                 "author", "email", "repository")
WANTED_META = ("homepage", "tracker", "qgisMaximumVersion", "changelog", "tags")
BLOCKED_EXT = (".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".pyc",
               ".pyd", ".zip", ".whl")
MAX_BYTES = 25 * 1024 * 1024


def read_metadata():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8") as f:
        cp.read_file(f)
    return dict(cp["general"])


def plugin_files():
    """(path on disk, path in zip) for the plugin folder, minus build junk."""
    out = []
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d != "__pycache__"
                         and d != "pipeline")
        for name in sorted(files):
            if name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, PLUGIN).replace(os.sep, "/")
            out.append((src, f"{NAME}/{rel}"))
    return out


def check(meta, entries):
    problems = []
    for key in REQUIRED_META:
        if not meta.get(key, "").strip():
            problems.append(f"metadata.txt: '{key}' is required by the repository")
    for key in WANTED_META:
        if not meta.get(key, "").strip():
            print(f"  note: metadata.txt has no '{key}'")
    if not re.fullmatch(r"\d+\.\d+\.\d+", meta.get("version", "")):
        problems.append(f"metadata.txt: version {meta.get('version')!r} is not X.Y.Z")
    total = 0
    for src, arc in entries:
        total += os.path.getsize(src)
        if arc.lower().endswith(BLOCKED_EXT):
            problems.append(f"{arc}: file type the upload scan blocks")
        if arc.endswith(".png") or arc.endswith(".svg"):
            continue
        with open(src, "rb") as f:
            if b"\0" in f.read(8192):
                problems.append(f"{arc}: binary file")
    if total > MAX_BYTES:
        problems.append(f"package is {total / 1e6:.1f} MB; the limit is 25 MB")
    return problems, total


def build(out_dir):
    meta = read_metadata()
    entries = plugin_files()
    for name in PIPELINE:
        entries.append((os.path.join(ROOT, name), f"{NAME}/pipeline/{name}"))
    for name in EXTRA:
        entries.append((os.path.join(ROOT, name), f"{NAME}/{name}"))
    missing = [src for src, _ in entries if not os.path.isfile(src)]
    if missing:
        sys.exit("missing: " + ", ".join(missing))
    problems, total = check(meta, entries)
    if problems:
        sys.exit("not built:\n  " + "\n  ".join(problems))

    os.makedirs(out_dir, exist_ok=True)
    zpath = os.path.join(out_dir, f"{NAME}-{meta['version']}.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in entries:
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16          # rw-r--r--, never executable
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())
    print(f"{zpath}\n  {len(entries)} files, {total / 1e6:.2f} MB unpacked, "
          f"{os.path.getsize(zpath) / 1e6:.2f} MB zipped, version {meta['version']}")
    return zpath


def relock(uv):
    src = open(os.path.join(PLUGIN, "environment.py"), encoding="utf-8").read()
    py = re.search(r'^PYTHON_VERSION = "([\d.]+)"', src, re.M).group(1)
    cmd = [uv, "pip", "compile", os.path.join(ROOT, "requirements.txt"),
           "--universal", "--python-version", py, "--torch-backend", "cpu",
           "--generate-hashes", "--no-annotate",
           "--custom-compile-command", "scripts/build_release.py --relock",
           "-o", os.path.join(PLUGIN, "requirements.lock")]
    venv_py = os.path.join(ROOT, "venv", "bin", "python")
    with tempfile.TemporaryDirectory() as tmp:
        if os.path.exists(venv_py):
            freeze = subprocess.run([venv_py, "-m", "pip", "freeze"], check=True,
                                    capture_output=True, text=True).stdout
            path = os.path.join(tmp, "tested.txt")
            with open(path, "w") as f:
                f.write(freeze)
            cmd += ["--constraint", path]
            print("constrained to the versions installed in ./venv")
        subprocess.run(cmd, check=True, cwd=ROOT)
    print("requirements.lock rewritten — test it with tests/test_setup_task.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--relock", action="store_true",
                    help="recompile requirements.lock with uv before building")
    ap.add_argument("--uv", default="uv", help="uv executable for --relock")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()
    if a.relock:
        relock(a.uv)
    build(a.out)


if __name__ == "__main__":
    main()
