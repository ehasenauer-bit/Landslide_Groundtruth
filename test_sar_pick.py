"""Which after-scene does change detection run on?  (sar_tab._cd_post)

Run with any python that has no QGIS:  ./venv/bin/python test_sar_pick.py

sar_tab imports qgis.PyQt, so the method under test is read OUT of the file and
bound to a stub tab. That is deliberate: the test exercises the shipped source
rather than a copy, so it cannot drift, and it needs no QGIS to run.

What it protects: two Sentinel-1 tracks routinely image one event on the SAME
DAY. Picking the after-scene by time-to-event alone can land on a track with no
usable before-scenes, which starves every detector and stops the run — measured
on Knik-Barry 2026-08-09, where t65 and t160 both image on 2026-08-21 but only
t160 has recent before-scenes.
"""
import re
import sys
import textwrap

SRC = "qgis_plugin/landslide_groundtruth/sar_tab.py"


def _bind(*names):
    """The named methods of SarTab, on a stub class with no QGIS behind it."""
    src = open(SRC).read()
    body = ""
    for n in names:
        m = re.search(rf"\n    def {n}\(self.*?(?=\n    def )", src, re.S)
        assert m, f"{n} not found in {SRC} — was it renamed?"
        body += textwrap.dedent(m.group(0))
    ns = {}
    exec("class Tab:\n" + "\n".join("    " + l if l.strip() else l
                                    for l in body.splitlines()), ns)
    return ns["Tab"]


Tab = _bind("_cd_post", "_one_per_day")
LOG = []
Tab._append_log = lambda self, m: LOG.append(m)
Tab._gap = lambda self, c: c.get("gap_days") or 9e9
Tab._covers_event = lambda self, c: c.get("cov", True)
tab = Tab()

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def scene(i, date, trk, gap, cov=True):
    return dict(id=i, date=date, relative_orbit=trk, gap_days=gap, cov=cov)


# The real Knik-Barry shape: both tracks image on 2026-08-21, and t65's pass is
# a few hours earlier so its rounded gap is SMALLER (11 vs 12 days).
POST = [scene("p65", "2026-08-21", 65, 11), scene("p160", "2026-08-21", 160, 12)]
PRE_WIDE = (
    [scene(f"a{i}", d, 65, g) for i, (d, g) in enumerate(
        [("2026-05-23", 78), ("2026-04-16", 115), ("2026-04-04", 127)])]
    + [scene(f"b{i}", d, 160, g) for i, (d, g) in enumerate(
        [("2026-07-28", 12), ("2026-07-16", 24), ("2026-07-04", 36),
         ("2026-06-27", 43), ("2026-06-15", 55)])])
# 60-day before-window: t65 has nothing at all
PRE_NARROW = [c for c in PRE_WIDE if c["relative_orbit"] == 160]

print("=== the track with no usable before-scenes is not chosen ===")
LOG.clear()
check("60-day window: t65 has 0 before-scenes -> t160",
      tab._cd_post(POST, PRE_NARROW, 3)["id"] == "p160")
check("  and the log says how short it fell",
      any("0 of 3 before-date(s)" in m for m in LOG))

print("\n=== a same-day track with a fresher before-stack wins ===")
LOG.clear()
got = tab._cd_post(POST, PRE_WIDE, 3)
check("150-day window: t65 looks usable but reaches back 127 days -> t160",
      got["id"] == "p160")
check("  and the log contrasts the two stack ages",
      any("reach back" in m and "127" in m and "36" in m for m in LOG))
LOG.clear()
check("the same holds for log-ratio, which needs only one before-scene",
      tab._cd_post(POST, PRE_WIDE, 1)["id"] == "p160")

print("\n=== an earlier after-DATE still wins outright ===")
LOG.clear()
early = scene("early", "2026-08-10", 65, 1)
check("a genuinely nearer after-scene is not traded for a fresher stack",
      tab._cd_post([early] + POST, PRE_WIDE, 1)["id"] == "early")
LOG.clear()
fresh = scene("x", "2026-08-10", 160, 1)
check("when the nearest after-scene is already the best, nothing changes",
      tab._cd_post([fresh] + POST, PRE_WIDE, 3)["id"] == "x")
check("  and nothing is logged", LOG == [])

print("\n=== safety rails ===")
LOG.clear()
check("no track usable at all -> nearest, as before",
      tab._cd_post(POST, [scene("z", "2026-01-01", 65, 200)], 3)["id"] == "p65")
check("  silently: the existing 'not enough before-scenes' warning explains it",
      LOG == [])
LOG.clear()
one = [scene("tick", "2026-09-02", 65, 24)]
check("a single ticked after-scene is honoured even when unusable",
      tab._cd_post(one, PRE_NARROW, 3)["id"] == "tick")
LOG.clear()
off = ([scene(f"o{i}", d, 160, g, cov=False) for i, (d, g) in enumerate(
            [("2026-07-28", 12), ("2026-07-16", 24), ("2026-07-04", 36)])]
       + [c for c in PRE_WIDE if c["relative_orbit"] == 65])
check("before-frames that miss the event point don't make a track usable",
      tab._cd_post(POST, off, 3)["id"] == "p65")
LOG.clear()
dupes = [scene(f"d{i}", "2026-07-28", 160, 12) for i in range(5)]
check("five same-day frames are one DATE, not five",
      tab._cd_post(POST, dupes + [c for c in PRE_WIDE
                                  if c["relative_orbit"] == 65], 3)["id"] == "p65")

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
