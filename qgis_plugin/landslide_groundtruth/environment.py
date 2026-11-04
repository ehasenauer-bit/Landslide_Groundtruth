"""The imagery tools' Python, installed by the plugin itself ("Install imagery tools").

The imagery stack (stackstac, rasterio, torch, ...) cannot live inside QGIS's own
Python, so every search runs run_single.py in a separate interpreter (task.py).
That interpreter used to be a venv the user built by hand and typed the path of.
For a plugin installed from the QGIS repository that is a dead end, so the dock
builds it instead, in five steps (SetupTask):

  1. uv (Astral's Python package manager, MIT/Apache-2.0) is fetched as its PyPI
     wheel at a pinned version, checked against a pinned sha256, and only its one
     binary is unpacked. Fetched through QGIS's network stack, so QGIS's proxy and
     certificate settings apply.
  2. uv downloads its own standalone CPython 3.12. No system Python, no QGIS
     Python, no python3-venv package on Debian — none of those can break it.
  3. uv installs requirements.lock: every package pinned to the set the pipeline
     was tested with, every file hash-checked. Windows/Linux get CPU-only torch
     (the default Linux torch drags in ~3 GB of CUDA); macOS keeps MPS.
  4. setup_check.py imports the whole stack and fetches the cloud-mask weights,
     so the first real search doesn't stall on a 58 MB download.
  5. env.json is written LAST: a cancelled or failed setup never reads as ready.

Everything lives in one per-user folder, data_dir(), outside any project and
shared by QGIS 3 and 4, and "Remove" deletes it whole.

Two ways to run the pipeline, chosen by whether "Python for the imagery tools"
(the dock's advanced box) is filled in:
  * managed — blank: the Python built here, and the pipeline scripts shipped in
    the plugin (bundled_pipeline_dir()).
  * custom  — filled in: that interpreter, as before. This is how a developer
    checkout keeps running against its own venv.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import zipfile
from urllib.parse import quote

from qgis.PyQt.QtCore import QStandardPaths, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsNetworkAccessManager, QgsSettings, QgsTask

PYTHON_VERSION = "3.12"
UV_VERSION = "0.12.24"
# One uv wheel per (OS, CPU), from PyPI. The sha256 is checked before the wheel is
# opened; a mismatch aborts the setup. Bump all of these together.
_PYPI = "https://files.pythonhosted.org/packages/"
UV_WHEELS = {
    ("Darwin", "arm64"): (
        _PYPI + "d9/82/a77c9ebb95f55e9e6c7d98202f9697110b634fe650b07b5d9eb5a51bba0e/"
        "uv-0.12.24-py3-none-macosx_11_0_arm64.whl",
        "7b3403f72dfda049295daf5c4c2da716138feecb0e9d5f22ad4b9734518c77dd"),  # pragma: allowlist secret
    ("Darwin", "x86_64"): (
        _PYPI + "44/21/690d3097136c268fdd833d42f00afd63302a7ab9df610f2ac163b1780141/"
        "uv-0.12.24-py3-none-macosx_10_12_x86_64.whl",
        "48cc70ecf59eece47e28d37c5c5eb9bb093e16881b1cc03e4a202286765a2022"),  # pragma: allowlist secret
    ("Windows", "x86_64"): (
        _PYPI + "71/d7/d573b4eda67a7ee79fdef3476138ff3a4d18b2bc97980d048709add81ea0/"
        "uv-0.12.24-py3-none-win_amd64.whl",
        "83f5dbd2389eb6f998e195faf7efb83199a3df825d4ec954b4d6e45f286ee52d"),  # pragma: allowlist secret
    ("Windows", "arm64"): (
        _PYPI + "8a/e6/ed50a59e7324414294049840ee33bfce299e41a16db25f19fe372fb8e94e/"
        "uv-0.12.24-py3-none-win_arm64.whl",
        "8b22387c8a23e9440103d925cb4762dc503932daa47020df6b1fa782192fcb76"),  # pragma: allowlist secret
    ("Linux", "x86_64"): (
        _PYPI + "89/f8/2ef356d9d066686c98854be9e3e41d0ba9d69f7b2495512b6394115fa40c/"
        "uv-0.12.24-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "e47005957dca320272ad68960b591b854b6bf0c3288368efb096d9ccd06b8404"),  # pragma: allowlist secret
    ("Linux", "arm64"): (
        _PYPI + "f8/68/ebea2e5af6bc85d974520fc24ee81b9247339de14289cee6f865de48fd3f/"
        "uv-0.12.24-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64"
        ".musllinux_1_1_aarch64.whl",
        "94c9ed22c1b5d6861a72639e23c5707bb07a354b67ec0462e9a0169a9665b5fd"),  # pragma: allowlist secret
}

# What the setup costs, said up front in the Setup box. Measured on a clean install
# (macOS arm64, 2026-11-02): 260 MB downloaded, 0.94 GB on disk, 134 s. The PyPI
# wheel sizes put Windows about the same and Linux somewhat higher.
DOWNLOAD_MB = 300
DISK_GB = 1.0

READY, STALE, MISSING, BROKEN = "ready", "stale", "missing", "broken"
_WINDOWS = platform.system() == "Windows"
# A console window would flash up for every uv/python call on Windows otherwise.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------
def data_dir():
    """The per-user folder holding uv, its Python and the environment.

    ~/Library/Application Support/landslide_groundtruth on macOS,
    %LOCALAPPDATA%\\landslide_groundtruth on Windows, ~/.local/share/... on Linux.
    LANDSLIDE_TOOLS_DIR overrides it (the tests build throwaway ones)."""
    override = os.environ.get("LANDSLIDE_TOOLS_DIR", "").strip()
    if override:
        return override
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.GenericDataLocation)
    return os.path.join(base, "landslide_groundtruth")


def env_dir():
    return os.path.join(data_dir(), "env")


def env_python():
    if _WINDOWS:
        return os.path.join(env_dir(), "Scripts", "python.exe")
    return os.path.join(env_dir(), "bin", "python")


def uv_exe():
    return os.path.join(data_dir(), "uv", "uv.exe" if _WINDOWS else "uv")


def marker_path():
    return os.path.join(data_dir(), "env.json")


def plugin_dir():
    # realpath: a development checkout is loaded through a symlink in the QGIS
    # profile, and bundled_pipeline_dir() must look beside the REAL plugin folder.
    return os.path.dirname(os.path.realpath(__file__))


def lock_path():
    return os.path.join(plugin_dir(), "requirements.lock")


def bundled_pipeline_dir():
    """The pipeline scripts that came with the plugin, or '' if there are none.

    A release zip carries them in <plugin>/pipeline (scripts/build_release.py puts
    them there). A development checkout has none there, but the plugin folder
    sits at <repo>/qgis_plugin/landslide_groundtruth, so the repo root — where
    run_single.py lives — is two levels up."""
    here = plugin_dir()
    for d in (os.path.join(here, "pipeline"), os.path.dirname(os.path.dirname(here))):
        if os.path.isfile(os.path.join(d, "run_single.py")):
            return d
    return ""


def default_results_dir():
    """Where results go before a project is saved: a visible folder in Documents,
    not somewhere inside the plugin (replaced on every plugin update)."""
    docs = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation)
    return os.path.join(docs or os.path.expanduser("~"), "Landslide Ground-Truthing")


def platform_key():
    """(system, cpu) in UV_WHEELS' terms, e.g. ('Darwin', 'arm64')."""
    cpu = platform.machine().lower()
    cpu = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(cpu, cpu)
    return platform.system(), cpu


# ---------------------------------------------------------------------------
# Is it there?
# ---------------------------------------------------------------------------
def lock_digest():
    try:
        with open(lock_path(), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def read_marker():
    try:
        with open(marker_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def status():
    """(state, message) for the managed environment.

    READY and STALE can both run searches. STALE means this plugin version pins a
    different package set than the one installed — usually still fine, but
    Update brings it in line (and is what a ModuleNotFoundError after a plugin
    update needs)."""
    marker = read_marker()
    if not marker:
        if os.path.exists(env_dir()):
            return BROKEN, ("A previous install didn't finish. Click Install imagery "
                            "tools to complete it.")
        return MISSING, "The imagery tools aren't installed yet."
    if not os.path.isfile(env_python()):
        return BROKEN, ("The imagery tools folder is incomplete. Click Install imagery "
                        "tools to repair it.")
    if not bundled_pipeline_dir():
        return BROKEN, ("This copy of the plugin is missing its pipeline scripts. "
                        "Reinstall the plugin from Plugins → Manage and Install.")
    if marker.get("lock") != lock_digest():
        return STALE, ("Ready, but this plugin version expects updated packages — "
                       "click Update when you have a few minutes.")
    when = (marker.get("installed") or "")[:10]
    return READY, "Ready" + (f" — installed {when}." if when else ".")


def usable(state):
    return state in (READY, STALE)


def installed_size_bytes():
    total = 0
    for root, _dirs, files in os.walk(data_dir()):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def remove():
    """Delete the whole tools folder. Results are elsewhere and are not touched."""
    d = data_dir()
    if os.path.isdir(d):
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------
def proxy_env():
    """HTTP(S)_PROXY for child processes, from QGIS's own proxy settings.

    Only an explicit HTTP proxy needs passing on. "Default proxy" means the
    system's, which a child already inherits; SOCKS isn't understood by uv."""
    s = QgsSettings()
    if not s.value("proxy/proxyEnabled", False, type=bool):
        return {}
    if s.value("proxy/proxyType", "", type=str) not in ("HttpProxy", "HttpCachingProxy"):
        return {}
    host = s.value("proxy/proxyHost", "", type=str).strip()
    if not host:
        return {}
    port = s.value("proxy/proxyPort", "", type=str).strip()
    user = s.value("proxy/proxyUser", "", type=str)
    auth = ""
    if user:
        auth = quote(user, safe="") + ":" + quote(
            s.value("proxy/proxyPassword", "", type=str), safe="") + "@"
    url = f"http://{auth}{host}" + (f":{port}" if port else "")
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url}


def child_env(extra=None):
    """os.environ minus QGIS's Python/GDAL/PROJ variables (see task.PipelineTask)."""
    from .task import PipelineTask
    env = PipelineTask._clean_env()
    env.update(extra or {})
    return env


class SetupError(Exception):
    pass


class SetupTask(QgsTask):
    """Build (or update) the managed environment. Cancellable at every step.

    Runs on a QgsTask worker thread; logLine is queued to the GUI thread. Built on
    the GUI thread, which is where the QgsSettings-backed proxy is read."""
    logLine = pyqtSignal(str)

    STEPS = ("Downloading uv", "Downloading Python " + PYTHON_VERSION,
             "Installing packages", "Checking the install")

    def __init__(self):
        super().__init__("Install imagery tools", QgsTask.Flag.CanCancel)
        self.proc = None
        self.error = ""
        self._env = child_env(proxy_env())
        self._env.update({
            # No lasting package cache: it would hold a second ~0.9 GB copy of the
            # environment for the rare Update, and make Remove's size look doubled.
            "UV_NO_CACHE": "1",
            "UV_PYTHON_INSTALL_DIR": os.path.join(data_dir(), "python"),
            "UV_NO_CONFIG": "1",          # a user's own uv.toml must not steer this
            "UV_PYTHON_DOWNLOADS": "automatic",
            "NO_COLOR": "1",
        })

    def run(self):
        started = time.time()
        try:
            os.makedirs(data_dir(), exist_ok=True)
            try:
                os.remove(marker_path())     # not ready until step 5 says so
            except OSError:
                pass
            self._step(0)
            self._fetch_uv()
            self._step(1)
            if not os.path.isfile(env_python()):
                self._run([uv_exe(), "venv", "--clear", "--managed-python",
                           "--python", PYTHON_VERSION, env_dir()])
            self._step(2)
            self._run([uv_exe(), "pip", "sync", "--python", env_python(),
                       "--torch-backend", "cpu", "--require-hashes", lock_path()])
            self._step(3)
            check = os.path.join(bundled_pipeline_dir(), "setup_check.py")
            self._run([env_python(), check], cwd=data_dir())
            with open(marker_path(), "w", encoding="utf-8") as f:
                json.dump({"lock": lock_digest(), "python": PYTHON_VERSION,
                           "uv": UV_VERSION,
                           "installed": time.strftime("%Y-%m-%d %H:%M"),
                           "seconds": round(time.time() - started)}, f, indent=1)
            self.setProgress(100)
            return True
        except SetupError as e:
            self.error = str(e)
            if not self.isCanceled():
                self.logLine.emit(self.error)
            return False
        except Exception as e:                # disk full, permissions, ...
            self.error = f"{type(e).__name__}: {e}"
            self.logLine.emit(self.error)
            return False

    def _step(self, i):
        if self.isCanceled():
            raise SetupError("Cancelled.")
        self.setProgress(5 + 90 * i / len(self.STEPS))
        self.logLine.emit(f"[{i + 1}/{len(self.STEPS)}] {self.STEPS[i]}…")

    def _fetch_uv(self):
        exe = uv_exe()
        if os.path.isfile(exe):
            return
        key = platform_key()
        if key not in UV_WHEELS:
            raise SetupError(
                f"No installer is available for this computer ({key[0]} on {key[1]}). "
                "Use 'Python for the imagery tools' in the advanced box instead.")
        url, sha = UV_WHEELS[key]
        # No feedback object: a cancel that lands in the first milliseconds of a
        # QgsBlockingNetworkRequest can hang the task (QGIS 3.44). The wheel is
        # ~15 MB, and Cancel takes effect as soon as it arrives.
        content = QgsNetworkAccessManager.blockingGet(QNetworkRequest(QUrl(url)))
        if content.error() != QNetworkReply.NetworkError.NoError:
            raise SetupError(f"Could not download uv: {content.errorString()} "
                             "Check the network connection (and Settings → Options → "
                             "Network if you are behind a proxy).")
        data = bytes(content.content())
        if hashlib.sha256(data).hexdigest() != sha:
            raise SetupError("The downloaded uv did not match its checksum, so it was "
                             "not used. Try again; if it keeps happening, report it.")
        name = "uv.exe" if _WINDOWS else "uv"
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        whl = exe + ".whl"
        with open(whl, "wb") as f:
            f.write(data)
        try:
            with zipfile.ZipFile(whl) as z:
                member = f"uv-{UV_VERSION}.data/scripts/{name}"
                with z.open(member) as src, open(exe + ".part", "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.chmod(exe + ".part", 0o700)
            os.replace(exe + ".part", exe)
        finally:
            os.remove(whl)

    def _run(self, cmd, cwd=None):
        self.logLine.emit("$ " + " ".join(os.path.basename(c) if i == 0 else c
                                          for i, c in enumerate(cmd)))
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=cwd or data_dir(), env=self._env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except OSError as e:
            raise SetupError(f"Could not start {os.path.basename(cmd[0])}: {e}")
        for line in self.proc.stdout:
            if self.isCanceled():
                break
            line = line.rstrip()
            if line:
                self.logLine.emit(line)
        if self.isCanceled():
            self._stop()
            raise SetupError("Cancelled.")
        code = self.proc.wait()
        self.proc.stdout.close()
        if code != 0:
            raise SetupError(f"{os.path.basename(cmd[0])} stopped with exit code "
                             f"{code} — the lines above say why.")

    def _stop(self):
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc.stdout.close()
        except Exception:
            pass

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        super().cancel()
