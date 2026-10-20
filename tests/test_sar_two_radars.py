"""The SAR tab with two radars in one table: pairs and change maps never mix
Sentinel-1 and NISAR  (sar_tab._default_picks / _cd_pick, sar_pairing).

Run with tests/run_all.sh, or any python at all — it needs no QGIS:
    ./venv/bin/python tests/test_sar_two_radars.py

Like test_sar_pick, the methods under test are lifted OUT of sar_tab.py (via
ast, with the module-level helpers they call) and bound to a stub tab, so the
shipped source is what runs.

The trap this pins down: both missions number their tracks from 1, and the
Iliamna AOI is imaged by Sentinel-1 AND NISAR "track 150". Pairing on
relative_orbit alone stars a NISAR after-scene against a Sentinel-1 before-
scene as "same track" — two instruments at 5.5 and 24 cm differenced as if
they were one — and change detection would feed such a stack to the
detectors. Events from before NISAR must behave exactly as they always did.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUG = os.path.join(ROOT, "qgis_plugin", "landslide_groundtruth")
SRC = os.path.join(PLUG, "sar_tab.py")
sys.path.insert(0, PLUG)
import sar_pairing  # noqa: E402

TOP = {"S1", "NISAR", "CD_PRODUCTS", "CD_NEED", "CD_NAMES", "MT_MAX_PRE",
       "_sensor", "_tag", "_match_pol"}
METHODS = {"_pair_for", "_default_picks", "_default_picks_for", "_cd_pick",
           "_cd_post", "_one_per_day", "_cd_radar", "_cd_pol"}


def _lift():
    tree = ast.parse(open(SRC).read())
    body, methods = [], []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in TOP for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in TOP:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "SarTab":
            methods = [n for n in node.body
                       if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    found = {getattr(n, "name", None) for n in methods}
    assert found == METHODS, f"missing in sar_tab.py: {METHODS - found}"
    cls = ast.ClassDef(name="Tab", bases=[], keywords=[], body=methods,
                       decorator_list=[])
    mod = ast.Module(body=body + [cls], type_ignores=[])
    ast.fix_missing_locations(mod)

    class Qt:
        Checked, Unchecked, UserRole = 2, 0, 256
    ns = {"Qt": Qt}
    exec(compile(mod, SRC, "exec"), ns)
    return ns


NS = _lift()
Tab, S1, NISAR = NS["Tab"], NS["S1"], NS["NISAR"]
_match_pol = NS["_match_pol"]

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


class Combo:
    def __init__(self, data, enabled=True):
        self.data, self.enabled = data, enabled

    def currentData(self):
        return self.data

    def isEnabled(self):
        return self.enabled

    def findData(self, value):
        return 0 if value in (S1, NISAR) else -1

    def setCurrentIndex(self, _i):
        pass                                 # the tab sets it via findData's radar

    def setTo(self, radar):
        self.data = radar


class Head:
    def __init__(self, cid, ticked):
        self.cid, self.ticked = cid, ticked

    def checkState(self):
        return 2 if self.ticked else 0

    def data(self, role):
        return self.cid if role == 256 + 1 else None


class Table:
    def __init__(self, heads):
        self.heads = heads

    def rowCount(self):
        return len(self.heads)

    def item(self, r, _c):
        return self.heads[r]


LOG, WARN = [], []


def make_tab(result, orbit="track", radar=S1, nisar_enabled=True, ticked=()):
    t = Tab()
    t._search_result = result
    t.orbit_combo = Combo(orbit)
    t.pol_combo = Combo("vv")
    t.cd_radar_combo = Combo(radar, nisar_enabled)
    t._covers_event = lambda c: True
    t._gap = lambda c: 1e9 if c.get("gap_days") is None else c["gap_days"]
    t._append_log = LOG.append
    t._warn = WARN.append
    every = [("pre", c) for c in result.get("pre", [])] + \
            [("post", c) for c in result.get("post", [])]
    t.table = Table([Head(c["id"], c["id"] in ticked) for _s, c in every])

    def by_id(cid):
        for side, c in every:
            if c["id"] == cid:
                return side, c
        return None, None
    t._candidate_by_id = by_id
    return t


def scene(cid, src, date, trk, gap, orbit="ascending", pols=None):
    return dict(id=cid, source=src, date=date, relative_orbit=trk, gap_days=gap,
                orbit_state=orbit,
                polarizations=pols or (["HH", "HV"] if src == NISAR else ["VV", "VH"]))


print("=== polarization: 'VV (recommended)' means co-pol on both radars ===")
check("vv on a NISAR HH/HV scene -> hh", _match_pol("vv", ["HH", "HV"]) == "hh")
check("vh on a NISAR scene -> hv (cross-pol)", _match_pol("vh", ["HH", "HV"]) == "hv")
check("vv on Sentinel-1 stays vv", _match_pol("vv", ["VV", "VH"]) == "vv")

# Iliamna-shaped: both radars have a 'track 150', and the NEAREST before-scene
# overall is Sentinel-1's while the nearest after-scene is NISAR's.
PRE = [scene("s1_pre_t150", S1, "2026-08-07", 150, 1),
       scene("s1_pre2_t150", S1, "2026-07-26", 150, 13),
       scene("s1_pre3_t150", S1, "2026-07-14", 150, 25),
       scene("n_pre_t150", NISAR, "2026-07-18", 150, 21),
       scene("n_pre2_t150", NISAR, "2026-07-06", 150, 33),
       scene("n_pre_t78", NISAR, "2026-07-25", 78, 14)]
POST = [scene("n_post_t150", NISAR, "2026-08-11", 150, 3),
        scene("s1_post_t150", S1, "2026-08-19", 150, 11)]
BOTH = dict(pre=PRE, post=POST)

print("\n=== the default pair: one per radar, never across ===")
picks, notes = make_tab(BOTH)._default_picks()
by_radar = {}
for side, c in picks:
    by_radar.setdefault(c["source"], []).append((side, c["id"]))
check("a Sentinel-1 pair AND a NISAR pair are starred", set(by_radar) == {S1, NISAR})
check("  the S1 pair is S1 before + S1 after",
      sorted(by_radar[S1]) == [("post", "s1_post_t150"), ("pre", "s1_pre_t150")])
check("  the NISAR pair is NISAR t150 before + NISAR t150 after (not S1 t150's)",
      sorted(by_radar[NISAR]) == [("post", "n_post_t150"), ("pre", "n_pre_t150")])
for mode in ("direction", "any", "both"):
    p, _n = make_tab(BOTH, orbit=mode)._default_picks()
    pairs_ok = all(len({c["source"] for _s, c in p if c["source"] == r}) <= 1
                   for r in (S1, NISAR))
    cross = any(a[1]["source"] != b[1]["source"]
                for a, b in zip(p[::2], p[1::2]))
    check(f"'{mode}' pairing never crosses radars", pairs_ok and not cross)

print("\n=== an event from before NISAR behaves exactly as before ===")
S1_ONLY = dict(pre=[c for c in PRE if c["source"] == S1],
               post=[c for c in POST if c["source"] == S1])
picks, notes = make_tab(S1_ONLY, nisar_enabled=False)._default_picks()
check("one same-track pair", [(s, c["id"]) for s, c in picks]
      == [("pre", "s1_pre_t150"), ("post", "s1_post_t150")])
check("  and no radar-prefixed notes", not any(n.startswith(S1) for n in notes))

print("\n=== change detection uses the chosen radar only ===")
LOG.clear(), WARN.clear()
got = make_tab(BOTH, radar=NISAR)._cd_pick(["logratio", "tsint"])
check("Radar=NISAR -> a run", got is not None)
if got:
    products, post, pres = got
    check("  after-scene is NISAR t150", post["id"] == "n_post_t150")
    check("  every before-scene is NISAR t150 — none of S1's t150",
          [c["id"] for c in pres] == ["n_pre_t150", "n_pre2_t150"])
LOG.clear(), WARN.clear()
got = make_tab(BOTH, radar=S1)._cd_pick(["logratio"])
check("Radar=Sentinel-1 -> S1 after + S1 before, though NISAR's after is nearer",
      got and got[1]["id"] == "s1_post_t150"
      and [c["id"] for c in got[2]] == ["s1_pre_t150"])
LOG.clear(), WARN.clear()
# The Seaward trap (2026-10-19): Radar box left on NISAR, every NISAR row
# unticked, only Sentinel-1 ticked — that must run Sentinel-1, not NISAR.
got = make_tab(BOTH, radar=NISAR, ticked=("s1_post_t150", "s1_pre_t150"))._cd_pick(
    ["logratio"])
check("only Sentinel-1 ticked + Radar box on NISAR -> a Sentinel-1 run",
      got and got[1]["source"] == S1 and all(c["source"] == S1 for c in got[2]))
check("  and the log says the ticks overrode the box",
      any("every ticked after-scene is Sentinel-1" in m for m in LOG))
LOG.clear(), WARN.clear()
got = make_tab(BOTH, radar=S1, ticked=("n_post_t150",))._cd_pick(["logratio"])
check("only a NISAR after-scene ticked + box on Sentinel-1 -> a NISAR run",
      got and got[1]["id"] == "n_post_t150")
LOG.clear(), WARN.clear()
got = make_tab(BOTH, radar=NISAR, ticked=("s1_post_t150", "n_post_t150",
                                          "s1_pre_t150"))._cd_pick(["logratio"])
check("both radars' after-scenes ticked (the default stars) -> the box decides",
      got and got[1]["source"] == NISAR and all(c["source"] == NISAR for c in got[2]))
check("  and the other radar's ticked rows are left out, logged",
      any("other radar left out" in m for m in LOG))
LOG.clear(), WARN.clear()
got = make_tab(dict(pre=PRE, post=[POST[0]]), radar=S1)._cd_pick(["logratio"])
check("no S1 after-scene -> stops, and points at the Radar switch",
      got is None and WARN and "switch Radar" in WARN[-1])
t = make_tab(BOTH, radar=NISAR, nisar_enabled=False)
check("a disabled Radar combo means Sentinel-1", t._cd_radar() == S1)
check("NISAR stack compares HH", make_tab(BOTH)._cd_pol(
    [c for c in PRE if c["source"] == NISAR]) == "hh")

print("\n=== the event-bracket summary knows the radars apart ===")
txt = sar_pairing.summarize(
    [("pre", PRE[0]), ("post", POST[1]), ("pre", PRE[3]), ("post", POST[0])],
    "2026-08-08T11:37:00")
lines = txt.splitlines()
check("two lines, one per pair", len(lines) == 2)
check("the NISAR line is labelled NISAR",
      any(l.startswith("NISAR ") for l in lines))
check("both pairs read 'same track' (within their radar)",
      all("same track" in l for l in lines))
cross = sar_pairing.summarize([("pre", PRE[0]), ("post", POST[0])],
                              "2026-08-08T11:37:00")
check("S1 t150 before + NISAR t150 after is NOT 'same track'",
      "cross-orbit" in cross)

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
