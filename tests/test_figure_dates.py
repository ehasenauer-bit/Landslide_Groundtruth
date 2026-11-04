#!/usr/bin/env python3
"""The exported figure must not lose the imagery dates.

The captions ("AFTER · 2025-10-10") are the only place the slide records WHEN
the imagery is from, and they are derived, not entered: the tab reads a date out
of whatever layers are ticked as Before/After. It used to read only the joined
layer NAMES, so a layer the analyst renamed — or a streamed scene whose name is
a product label — silently produced a dateless slide that still looked finished.

Checks the resolver against the layer shapes this plugin really produces.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from landslide_groundtruth.viewer3d_tab import Viewer3DTab            # noqa: E402

resolve = Viewer3DTab._imagery_date          # a staticmethod: no tab needed
sniff = Viewer3DTab._sniff_date


class L:                                     # the two accessors the resolver uses
    def __init__(self, name, source=""):
        self._n, self._s = name, source
    def name(self):
        return self._n
    def source(self):
        return self._s


def date(layers, side="before"):
    return resolve(layers, side)


# ---- the run's own output: the date is in the layer name ---------------------
run = L("event_251007_1042_pre_2025-10-07_highlight",
        "/out/interactive/qgis_packages/event_251007_1042_pre_2025-10-07_highlight.tif")
assert date([run]) == "2025-10-07", date([run])
# the event id in front of it (251007_1042) must not be mistaken for a date
assert date([L("event_230825_1406_post_2023-08-27_highlight")]) == "2023-08-27"

# ---- the run names the EVENT first and the image second ----------------------
# `2026-06-15_pre_2026-05-24_highlight` is a 2026-05-24 image of a 2026-06-15
# event. Reading position instead of the pre/post marker captioned both panels
# with the event date — and two panels agreeing looks plausible enough to ship.
pre = L("2026-06-15_pre_2026-05-24_highlight")
post = L("2026-06-15_post_2026-07-28_highlight")
assert date([pre], "before") == "2026-05-24", date([pre], "before")
assert date([post], "after") == "2026-07-28", date([post], "after")
# assert the negative: plain first-date order really does return the event date,
# so the marker rule above is doing the work and not agreeing by luck.
assert sniff(pre.name()) == "2026-06-15" and sniff(post.name()) == "2026-06-15"
# a marker binds to the layer it is on, whichever side that layer is ticked as
assert date([post], "before") == "2026-07-28"
# ...and when one text carries both markers, this side's marker wins
both = L("dNDVI_pre_2026-05-24_post_2026-07-28")
assert date([both], "before") == "2026-05-24"
assert date([both], "after") == "2026-07-28", date([both], "after")

# ---- renamed layer: the name has no date, the file it reads does -------------
renamed = L("After (HONC)", "/out/interactive/qgis_packages/"
                            "event_251007_1042_post_2025-10-10_highlight.tif")
assert date([renamed], "after") == "2025-10-10", date([renamed], "after")
assert sniff(renamed.name()) is None, "the name alone must NOT yield a date here"

# ---- streamed Planetary Computer scene: date only in the /vsicurl URL --------
pc = L("Sentinel-2 L2A · Highlight Optimized Natural Color",
       "/vsicurl/https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/"
       "05/V/NK/2025/10/10/S2B_MSIL2A_20251010T211529_N0511_R011_T05VNK_"
       "20251011T003021.SAFE/GRANULE/x/IMG_DATA/R10m/T05VNK_20251010T211529_B04.jp2")
# the SENSING time, not the processing time that follows it in the product id
assert date([pc], "after") == "2025-10-10", date([pc], "after")
assert date([pc], "before") == "2025-10-10"

# ---- a change raster carries BOTH dates: the side decides which one ----------
chg = L("fusion_event_260831_1219_dndsi_logratio_2026-08-23_to_2026-09-04")
assert date([chg], "before") == "2026-08-23", date([chg], "before")
assert date([chg], "after") == "2026-09-04", date([chg], "after")

# ---- several ticked layers: the first one that knows its date wins -----------
stack = [L("hillshade"), L("Landslide scars"), run]
assert date(stack) == "2025-10-07"
# ...and names are exhausted before any source is read, so one dateless layer
# ticked first cannot hand the caption a date from a different scene's file.
mixed = [L("dNDSI 2026-07-04", "/tmp/whatever.tif"),
         L("unnamed", "/out/event_x_pre_2011-01-01_highlight.tif")]
assert date(mixed) == "2026-07-04", date(mixed)

# ---- nothing anywhere: empty, not a crash and not a wrong date ---------------
assert date([L("hillshade", "/tmp/hillshade.tif")]) == ""
assert date([]) == ""

# ---- _sniff_date is a staticmethod now --------------------------------------
# It used to be declared `def _sniff_date(*texts)` inside the class, so every
# call passed the tab itself as texts[0] and searched its repr — "<... object at
# 0x7f9c20251007>" is a date to that regex.
assert isinstance(Viewer3DTab.__dict__["_sniff_date"], staticmethod)
assert sniff("2025-10-07") == "2025-10-07"
# ...and the hazard that made it worth fixing is real, not theoretical: handed
# such a repr, the sniffer believes it. The fix is that it is never handed one.
assert sniff("<Viewer3DTab object at 0x7f9c20251007>") == "2025-10-07"

print("figure dates OK — pre/post marker beats the leading event date, renamed-layer "
      "source, PC sensing-vs-processing id, change-raster A_to_B by side, "
      "first-knowing-layer wins, dateless is empty")
