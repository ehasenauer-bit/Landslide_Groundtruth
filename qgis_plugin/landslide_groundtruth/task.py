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

    def run(self):
        cmd = [self.python_exe, self.script] + self.cli_args
        self.logLine.emit("$ " + " ".join(cmd))
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
                self.proc.terminate()
                return False
            self.logLine.emit(line.rstrip())
        code = self.proc.wait()

        rp = os.path.join(self.out_dir, self.result_name)
        try:
            with open(rp) as f:
                self.result = json.load(f)
        except Exception as e:
            self.logLine.emit(f"could not read {self.result_name}: {e}")
        return code == 0

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
        return env

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        super().cancel()
