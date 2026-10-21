"""Background task: run the venv pipeline as a subprocess, stream its output.

This is the heart of "Option B": the plugin (in QGIS's Python) never imports the
heavy imagery stack. It launches the configured project venv as a separate
process running run_single.py, streams stdout to the dock log, and parses the
result.json the script writes so the dock can load the output layers.

QgsTask.run() executes on a worker thread (no GUI/layer access there); log lines
are emitted via a signal (queued to the GUI thread) and the layers are loaded by
the dock in response to taskCompleted/taskTerminated, which fire on the GUI thread.
"""
import json
import os
import re
import signal
import subprocess

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask


class PipelineTask(QgsTask):
    logLine = pyqtSignal(str)

    def __init__(self, python_exe, script, cwd, cli_args, out_dir,
                 result_name="result.json"):
        super().__init__("Landslide imagery run", QgsTask.CanCancel)
        self.python_exe = python_exe
        self.script = script
        self.cwd = cwd
        self.cli_args = list(cli_args)
        self.out_dir = out_dir          # absolute path
        self.result_name = result_name  # JSON the script writes (result/search)
        self.proc = None
        self.result = None
        self.returncode = None          # child exit code (negative = killed by signal)
        self.exit_note = ""             # plain-language reason when it exits non-zero

    def run(self):
        cmd = [self.python_exe, self.script] + self.cli_args
        self.logLine.emit("$ " + " ".join(cmd))

        # Drop any result file left by a PREVIOUS run so a crashed/killed run this
        # time can never be read as if it had produced fresh output (see below).
        rp = os.path.join(self.out_dir, self.result_name)
        try:
            os.remove(rp)
        except OSError:
            pass

        try:
            self.proc = subprocess.Popen(
                cmd, cwd=self.cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                env=self._clean_env(),
            )
        except Exception as e:
            self.logLine.emit(f"failed to launch: {e}")
            return False

        for line in self.proc.stdout:
            if self.isCanceled():
                self._terminate_and_reap()   # don't leave a zombie / leak the pipe
                return False
            self.logLine.emit(line.rstrip())
        code = self.proc.wait()
        self.returncode = code
        try:
            self.proc.stdout.close()
        except Exception:
            pass

        # Only trust result.json on a clean, un-cancelled exit. A non-zero or
        # cancelled run must NOT surface stale/partial output as this run's result
        # (taskTerminated loads layers too, so a stale read would be shown as real).
        if code == 0 and not self.isCanceled():
            try:
                with open(rp) as f:
                    self.result = json.load(f)
            except Exception as e:
                self.logLine.emit(f"could not read {self.result_name}: {e}")
        elif not self.isCanceled():
            # Non-zero exit -> say WHY in words. The usual killers here leave no Python
            # traceback: the OS out-of-memory reaper (SIGKILL, macOS 'jettison') and a
            # native-library crash (SIGSEGV/SIGBUS). Without this the caller only sees
            # "finished with no result", because result.json is never written on a kill.
            self.exit_note = exit_reason(code)
            self.logLine.emit(self.exit_note)
        return code == 0

    def _terminate_and_reap(self):
        """Terminate the child and actually WAIT for it, escalating to kill, then
        close its stdout — so a cancel leaves no zombie and no leaked pipe FD."""
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except Exception:
            pass

    @staticmethod
    def _clean_env():
        """Strip QGIS's Python/GDAL/PROJ environment so the venv Python uses its
        OWN stdlib and bundled data. Without this the child inherits QGIS's
        PYTHONHOME/PYTHONPATH and dies with 'Failed to import encodings module'."""
        env = os.environ.copy()
        for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
                    "PROJ_LIB", "PROJ_DATA", "GDAL_DATA", "GDAL_DRIVER_PATH",
                    "GDAL_PLUGINS", "QT_PLUGIN_PATH"):
            env.pop(var, None)
        # Run the child UNBUFFERED and with the fault handler armed. Its stdout is a
        # pipe, so Python block-buffers it by default: a SIGKILL (out of memory) or a
        # segfault then discards the whole buffered log and the failure reaches the
        # user as a bare "finished with no result". Unbuffered streams each line as it
        # is printed (so it survives a kill), and faulthandler dumps a native traceback
        # to stderr on a crashing signal (merged into the same log).
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONFAULTHANDLER"] = "1"
        return env

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        super().cancel()


# --------------------------------------------------------------------------
# Reading a failure back to the user
# --------------------------------------------------------------------------
# The child's stdout and stderr are merged and streamed to the tab's log, so
# when a run dies the real cause is almost always already on screen — the
# problem was never that the information was missing, it was that nothing said
# "look at the log", and nothing translated the traceback into an action.
#
# FAILURE_HINTS maps a signature in that output to a sentence naming the fix.
# Ordered most-specific first; the first match wins.
# Matched as REGEXES, not substrings. A bare "401" is not safe to look for: every
# PlanetScope scene acquired on 1 April is called 20240401_..., and "found 1401
# scenes" would otherwise report that the server rejected your credentials. HTTP
# codes are therefore only recognised next to HTTP-ish context.
# Ordered most-specific first; the first match wins.
FAILURE_HINTS = [
    # A Python that cannot even bootstrap. Modern CPython does not print
    # "Failed to import encodings module" any more — it prints
    # "init_fs_encoding: failed to get the Python codec of the filesystem
    # encoding" followed by "ModuleNotFoundError: No module named 'encodings'",
    # so match all three. Must stay ahead of the generic ModuleNotFoundError
    # rule below, which would otherwise claim the imagery tools are missing when
    # the interpreter never started at all.
    (r"Failed to import encodings module|init_fs_encoding"
     r"|No module named '?encodings'?",
     "The Python you chose in Environment cannot start at all. It is probably "
     "not the python inside the project's venv folder — pick venv/bin/python3 "
     "(macOS/Linux) or venv\\Scripts\\python.exe (Windows)."),
    (r"ModuleNotFoundError",
     "The Python you chose is missing the imagery tools. Point Environment at "
     "the python inside the project's venv folder, or re-run "
     "'pip install -r requirements.txt' in that venv."),
    (r"No module named",
     "The Python you chose is missing a package the pipeline needs. Re-run "
     "'pip install -r requirements.txt' in that venv."),
    (r"MemoryError|Unable to allocate|_ArrayMemoryError",
     "The run ran out of memory. Reduce the search radius and try again."),
    (r"No space left on device|Errno 28",
     "The disk holding the output folder is full."),
    (r"(?:HTTP\D{0,3}401\b|\b401\s+(?:Client\s+Error|Unauthorized))",
     "The server rejected the credentials. Check the login for that imagery "
     "source."),
    (r"(?:HTTP\D{0,3}403\b|\b403\s+(?:Client\s+Error|Forbidden))",
     "The server refused access. Check the login and your quota for that "
     "imagery source."),
    (r"(?:HTTP\D{0,3}429\b|\b429\s+(?:Client\s+Error|Too\s+Many))",
     "The imagery server is rate-limiting this account. Wait a minute and try "
     "again."),
    (r"(?:HTTP\D{0,3}5\d\d\b|\b5\d\d\s+Server\s+Error)",
     "The imagery server had an internal error. That is on their side — try "
     "again shortly."),
    (r"SSLError|CERTIFICATE_VERIFY_FAILED",
     "The secure connection to the imagery server failed. Check your network, "
     "then try again."),
    (r"ConnectionError|Failed to establish a new connection|NewConnectionError",
     "Could not reach the imagery server. Check your network connection and "
     "try again."),
    (r"Read timed out|ReadTimeout|ConnectTimeout",
     "The imagery server stopped responding. Try again — the search is free."),
    (r"CPLE_OpenFailed|Cannot open|unable to open",
     "A data file could not be opened — see the log for which one."),
    (r"Traceback \(most recent call last\)",
     "The imagery tools stopped with an error. The last lines of the log say "
     "where."),
]

_HINT_RE = [(re.compile(pat, re.I), hint) for pat, hint in FAILURE_HINTS]


def failure_hint(text):
    """A plain-language next step for a failed run, or '' if nothing is recognised.

    `text` is the tail of the run log. Returns one sentence to put in front of
    the user; the log itself stays available for the detail."""
    if not text:
        return ""
    for rx, hint in _HINT_RE:
        if rx.search(text):
            return hint
    return ""


def exit_reason(code):
    """Plain-language reason for a non-zero child exit code.

    A failure_hint reads the child's OUTPUT; this reads its EXIT CODE, which is the
    only evidence left when the child is killed without printing anything — an
    out-of-memory kill or a native crash. POSIX reports a child killed by signal N as
    returncode -N; some wrappers surface it shell-style as 128+N instead, so handle
    both. The out-of-memory case is called out by name because it's the common one and
    it has a clear next step (use a smaller radius)."""
    sig = None
    if code is None:
        return "The run stopped before it finished — see the log above."
    if code < 0:
        sig = -code
    elif code > 128:
        sig = code - 128
    if sig is not None:
        try:
            name = signal.Signals(sig).name
        except (ValueError, AttributeError):
            name = f"signal {sig}"
        if sig == getattr(signal, "SIGKILL", 9):
            return (f"The run was killed by the operating system ({name}) — almost "
                    f"always this means it ran out of memory. Reduce the search radius "
                    f"(or the number of scenes) and try again.")
        if sig in (getattr(signal, "SIGSEGV", 11), getattr(signal, "SIGBUS", 10),
                   getattr(signal, "SIGABRT", 6)):
            return (f"The run crashed inside a native library ({name}). The log above "
                    f"shows how far it got.")
        return f"The run was stopped by {name} (signal {sig}) — see the log above."
    return (f"The run exited with code {code} without producing a result — the error "
            f"is in the log above.")
