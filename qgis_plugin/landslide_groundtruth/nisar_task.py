"""Background task: read NISAR GCOV scenes onto the SAR tab's AOI grid.

A NISAR read is 5-15 s of blocking network I/O per scene (nisar_read), and
change detection needs up to seven scenes. On the GUI thread that freezes QGIS
in a way that looks exactly like a crash — the failure limits.py exists to
prevent — so the reads run here, one after another on QgsTask's worker thread.
Sequential on purpose, and nisar_read additionally holds one process-wide lock
around every HDF5 call: this GDAL build's HDF5 driver segfaults under concurrent
reads (measured), and a Preview task and a change-detection task can run side
by side.

Every job reports back through jobDone, carrying the batch number it was
started with, so the tab can drop the answers of a batch it has already
abandoned (a row double-click can start a new preview while an old one is
still reading). Every job reports exactly once — including the ones a cancel
skips — so the tab's pending counter always reaches zero.
"""
from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask

from . import nisar_read


class NisarReadTask(QgsTask):
    # batch, job key, output path ('' on failure), error ('' on success)
    jobDone = pyqtSignal(int, str, str, str)
    logLine = pyqtSignal(str)

    def __init__(self, batch, jobs, description="NISAR L-band read"):
        """jobs: dicts with key, url, pol, bbox, px, out_path, kind ('float' for
        change detection, 'display' for the amplitude preview), cache_dir, and
        for 'display' the stretch: scale, lo, hi, stretch."""
        super().__init__(description, QgsTask.CanCancel)
        self.batch = int(batch)
        self.jobs = list(jobs)

    def run(self):
        done = 0
        try:
            for job in self.jobs:
                if self.isCanceled():
                    break
                self.setProgress(100.0 * done / max(1, len(self.jobs)))
                try:
                    out, gt, wkt, info = nisar_read.read_aoi(
                        job["url"], job["pol"], job["bbox"], job["px"],
                        cache_dir=job.get("cache_dir"), log=self.logLine.emit)
                    if job.get("kind") == "display":
                        nisar_read.write_display(
                            job["out_path"],
                            nisar_read.to_display(out, job.get("scale", "db"),
                                                  job.get("lo", -25.0),
                                                  job.get("hi", 0.0),
                                                  job.get("stretch", 0.20)),
                            gt, wkt)
                    else:
                        nisar_read.write_float(job["out_path"], out, gt, wkt)
                    self.logLine.emit(
                        f"    {job['key']}: {info['valid_frac']:.0%} of the AOI valid"
                        f" (EPSG:{info['epsg']}, {info['native_res']:g} m native"
                        + (f", read every {info['step']}th pixel" if info["step"] > 1
                           else "") + ")")
                    self.jobDone.emit(self.batch, job["key"], job["out_path"], "")
                except Exception as e:      # noqa: BLE001 — report, never kill QGIS
                    self.jobDone.emit(self.batch, job["key"], "",
                                      f"{type(e).__name__}: {e}")
                done += 1
        finally:
            # skipped by a cancel (or an unexpected escape): still answer, so the
            # tab's counter reaches zero and its buttons come back
            for job in self.jobs[done:]:
                self.jobDone.emit(self.batch, job["key"], "", "cancelled")
        return not self.isCanceled()
