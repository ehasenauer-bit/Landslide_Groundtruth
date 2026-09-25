"""A Planet hiccup must not fail the whole PlanetScope search.

Run with tests/run_all.sh, or directly:
    ./venv/bin/python tests/test_planet_spec_fallback.py

Every Planet search and order first makes the SDK download its bundle spec, with
one bare httpx.get (5 s default timeout) tried twice, in a fresh process each time.
On 2026-09-25 13:31 that fetch failed and the tab listed no scenes; the identical
search minutes later found 60 pre / 13 post. planet_imagery._prime_bundle_spec now
fetches it with a real timeout and retries, keeps the last good copy in the order
cache, and falls back to that copy. And a search that still fails is reported as
FAILED (run_single's 'failed'), not as zero scenes. Offline: httpx.get is faked.

Needs the Planet SDK, which lives in the project venv, so re-exec there if we were
started under QGIS's Python.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import planet  # noqa: F401
    import rioxarray  # noqa: F401
except ModuleNotFoundError:                       # started under QGIS's Python
    venv = os.path.join(ROOT, "venv", "bin", "python")
    if not os.path.exists(venv) or os.environ.get("_RELAUNCHED"):
        print("SKIP: no venv with the Planet SDK; spec fallback not checked")
        sys.exit(0)
    env = dict(os.environ, _RELAUNCHED="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    sys.exit(subprocess.call([venv, os.path.abspath(__file__)], env=env))

os.environ["LANDSLIDE_PLANET_CACHE"] = tempfile.mkdtemp(prefix="planet_spec_")
sys.path.insert(0, ROOT)
import httpx
from planet import specs
import planet_imagery as pi

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


SPEC = {"bundles": {"analytic_sr_udm2": {"assets": {"PSScene": ["ortho_analytic_4b_sr"]}},
                    "analytic_udm2": {"assets": {"PSScene": ["ortho_analytic_4b"]}}}}
calls = []


class Resp:
    def raise_for_status(self): pass
    def json(self): return SPEC


def fake_get(script):
    """httpx.get that plays `script` out: 'ok' answers, 'timeout'/'down' raise."""
    steps = list(script)

    def get(url, *a, **k):
        calls.append(k.get("timeout"))
        step = steps.pop(0) if steps else "down"
        if step == "ok":
            return Resp()
        raise httpx.ReadTimeout("slow") if step == "timeout" else httpx.ConnectError("down")
    return get


def fresh(script):
    """A new process's state: the SDK holds no spec yet; httpx follows `script`."""
    specs.PRODUCT_BUNDLES.cache = None
    calls.clear()
    httpx.get = fake_get(script)
    pi.time.sleep = lambda s: None               # no real back-off waits in a test


def sdk_knows_psscene():
    try:
        return specs.validate_data_item_type("psscene") == "PSScene"
    except specs.FetchBundlesSpecError:
        return False


copy = os.path.join(os.environ["LANDSLIDE_PLANET_CACHE"], "bundles_spec.json")

print("=== the 13:31 case: a slow first answer no longer fails the search ===")
fresh(["timeout", "ok"])
pi._prime_bundle_spec()
check("the spec is loaded after one timeout + one answer", sdk_knows_psscene())
check(f"it waited with a real timeout ({calls[0]} s, not httpx's 5 s)",
      calls and calls[0] and calls[0] > 5)
check("and saved a copy for next time", os.path.exists(copy))
n = len(calls)
sdk_knows_psscene()
check("the SDK does not fetch it again itself", len(calls) == n)

print("\n=== Planet down, copy on disk: the search still validates ===")
fresh(["down", "down", "down"])
pi._prime_bundle_spec()
check(f"all {pi.SPEC_TRIES} tries made, then the saved copy used",
      len(calls) == pi.SPEC_TRIES and sdk_knows_psscene())

print("\n=== Planet down, no copy: the SDK's own error, as before (no crash here) ===")
os.remove(copy)
fresh(["down"] * 10)
pi._prime_bundle_spec()                           # must return quietly
check("nothing faked into the SDK", specs.PRODUCT_BUNDLES.cache is None)
check("the SDK then fails exactly as it used to", not sdk_knows_psscene())

print("\n=== a search that still fails is reported as FAILED, not as 0 scenes ===")
import run_single as rs
from argparse import Namespace


def boom(*a, **k):
    raise specs.FetchBundlesSpecError("Unable to fetch spec from API. Please retry!")


pi.search_event = boom
args = Namespace(prefer="planet", pre_days=60, post_days=90, seasonal=False,
                 auto_window=False, cloud_weight=0.5, max_cloud=100,
                 coverage="aoi", quality="any")
import datetime as dt
res = rs._search_candidates(60.26, -140.817, 5.0, dt.datetime(2026, 8, 18, 10, 22), args)
check(f"'failed' names the source and the error: {res.get('failed')}",
      res.get("failed") and res["failed"][0].startswith("planet: FetchBundlesSpecError"))
check("and no scenes (none were searched)", not res["pre"] and not res["post"])

print(f"\n{OK} passed, {FAIL} failed")
print("ALL PASS" if not FAIL else "FAILURES")
sys.exit(1 if FAIL else 0)
