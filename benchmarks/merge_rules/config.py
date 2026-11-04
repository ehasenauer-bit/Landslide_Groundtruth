"""Where the merge-rule benchmark reads its inputs and writes its outputs.

Every location can be overridden with an environment variable; the defaults
fit Ethan's machine without hard-coding an account name.

  LANDSLIDE_BENCH_DIR    work dir: scene cache, SAR maps, results
                         (default <repo>/out/benchmarks/merge_rules — out/ is
                         gitignored, so nothing generated here reaches git)
  LANDSLIDE_OPTICAL_DIR  the Run packages holding <event>_dbright_/_dndsi_ rasters
                         (default <repo>/out/interactive/qgis_packages)
  LANDSLIDE_SCAR_DIR     the folder of per-event 'Total Area.gpkg' scar outlines
                         (default: the aec_research shared drive's QGIS folder,
                         found under ~/Library/CloudStorage)
"""
import glob
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WORK = os.environ.get("LANDSLIDE_BENCH_DIR") or os.path.join(
    REPO, "out", "benchmarks", "merge_rules")
OPTICAL_DIR = os.environ.get("LANDSLIDE_OPTICAL_DIR") or os.path.join(
    REPO, "out", "interactive", "qgis_packages")


def _find_scar_dir():
    pat = os.path.expanduser(
        "~/Library/CloudStorage/GoogleDrive-*/Shared drives/aec_research/"
        "Ethan_Hasenauer/QGIS")
    hits = sorted(glob.glob(pat))
    return hits[0] if hits else ""


SCAR_DIR = os.environ.get("LANDSLIDE_SCAR_DIR") or _find_scar_dir()
EVENTS = os.path.join(HERE, "events.json")
CACHE = os.path.join(WORK, "cache")
