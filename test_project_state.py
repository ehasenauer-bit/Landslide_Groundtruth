"""Saved-project combo restore survives items being reordered or reworded.

Run with any python:  ./venv/bin/python test_project_state.py

project_state stores a combo choice as its LABEL. Nine of the QGIS projects on
the shared drive hold "dNDSI — snow index change (recommended)". When dBright
became the recommended default the marker moved, and a plain exact-text match
would have silently dropped all nine to the new default — turning six dNDSI
projects into dBright ones on open, with no message. combo_index is what stops
that, so it is tested against the strings those files actually contain.
"""
import re
import sys

SRC = "qgis_plugin/landslide_groundtruth/project_state.py"

# project_state imports qgis.PyQt; lift the pure helper out instead.
_src = open(SRC).read()
_m = re.search(r"\ndef combo_index\(texts, value\):.*?(?=\n\ndef )", _src, re.S)
assert _m, f"combo_index not found in {SRC} — was it renamed?"
_ns = {}
exec(_m.group(0), _ns)
combo_index = _ns["combo_index"]

OLD_DNDSI = "dNDSI — snow index change (recommended)"       # in 6 saved projects
OLD_DBRIGHT = "dBright — broadband brightness change"       # in 3 saved projects
NEW = ["dBright — broadband brightness change (recommended)",
       "dNDSI — snow index change"]
OLD_SAR = "Log-ratio, increase only (recommended)"          # in all 9

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


print("=== the nine real saved projects still restore correctly ===")
check("a stored dNDSI choice still selects dNDSI, not the new default",
      combo_index(NEW, OLD_DNDSI) == 1)
check("a stored dBright choice still selects dBright",
      combo_index(NEW, OLD_DBRIGHT) == 0)
check("the SAR label, which did not change, matches exactly",
      combo_index([OLD_SAR, "Int-corr"], OLD_SAR) == 0)

print("\n=== the guarantees that make that safe ===")
check("an exact match always wins",
      combo_index(["dNDSI — a", "dNDSI — b"], "dNDSI — b") == 1)
check("an AMBIGUOUS head is refused rather than guessed",
      combo_index(["dNDSI — a", "dNDSI — b"], "dNDSI — c") == -1)
check("an unknown label leaves the current choice alone",
      combo_index(NEW, "dNDVI — vegetation change") == -1)
check("reordering alone never changes the meaning",
      combo_index(list(reversed(NEW)), OLD_DNDSI) == 0)
check("a label with no em-dash still matches exactly",
      combo_index(["None", "3x3", "5x5"], "5x5") == 2)
check("a label with no em-dash and no exact match is refused",
      combo_index(["None", "3x3", "5x5"], "7x7") == -1)
check("an empty stored value is refused", combo_index(NEW, "") == -1)

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
