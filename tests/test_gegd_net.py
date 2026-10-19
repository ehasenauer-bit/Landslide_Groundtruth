"""gegd_net: a cancelled GEGD request always ends, and the key rules still hold.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_gegd_net.py

Why this file exists. gegd_net used to sit on QgsBlockingNetworkRequest, which in
QGIS 3.44 hangs its thread for good when a cancel lands in the first milliseconds
of a request: doRequest links the feedback to its own abort() before the reply
exists and to the reply's abort() only after, both queued, and abort() alone
deleteLater()s the reply without it ever emitting finished. test_gegd_tab's
"superseded key test" stalled in about 4 of 7 runs. gegd_net now drives a
non-blocking reply itself and polls the feedback, and this file pins that:

  * a cancel at every point of a request ends the QgsTask within 2 s: before it
    starts; from inside get() (a request preprocessor and a reply preprocessor
    cancel on the worker thread, deterministically in the old race window); from
    the GUI thread 0-15 ms after the task starts; mid-transfer (a server trickling
    bytes); during a retry wait; and a burst of concurrent tasks cancelled at
    random. 50+ iterations. Every reply is deleted, and the HostGuard sees a
    finished for every request it counted;
  * 6 task threads x 60 quick fetches at once: none fails. Deleting replies on
    the worker threads instead killed 4-19 % of the live reply wrappers in such
    a burst (PyQt marks a wrapper dead by address, late, for an object destroyed
    off the GUI thread), and gegd_net keeps no QObject subclass (one deadlocked
    a stress run: see gegd_net's docstring);
  * the key reaches the allowed host from a worker thread and is stripped
    off-host; each request is counted once, with the initiator kept for the
    Network Logger;
  * a missing auth config sends nothing (no request is even created);
  * a server that never answers, or stops after the headers, is a timeout:
    retried, then 'no answer within 1 s', in bounded time, never a truncated 200;
  * a 3xx to another origin is not followed (nothing reaches the other server);
    a same-origin 3xx (absolute or relative) is followed with the key; a loop
    stops after MAX_REDIRECTS hops;
  * KeyTestTask names the newest Vantor optical strip, skipping THIRD_PARTY,
    radar, records without sensorType, Vivid and multi-date mosaics.

Everything is offline: a throwaway profile and auth DB (Keychain helper off
BEFORE the master password is set), two local HTTP servers and a synthetic key.
The guard is aimed at http://127.0.0.1:<A> as the stand-in for pro.gegd.com.
"""
import json, os, random, secrets, shutil, sys, tempfile, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _base in ("/Applications/QGIS-LTR.app", "/Applications/QGIS.app"):
    _proj = os.path.join(_base, "Contents", "Resources", "qgis", "proj")
    if os.path.isdir(_proj):
        os.environ.setdefault("PROJ_DATA", _proj)
        os.environ.setdefault("PROJ_LIB", _proj)
        os.environ.setdefault("GDAL_DATA", os.path.join(_base, "Contents", "Resources", "qgis", "gdal"))
        break
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)

# A throwaway profile and auth DB, set BEFORE QgsApplication exists.
TMP = tempfile.mkdtemp(prefix="gegd_net_")
for _d in ("auth", "config", "cache"):
    os.makedirs(os.path.join(TMP, _d))
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(TMP, "auth")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "config")
os.environ["LANDSLIDE_GEGD_CACHE"] = os.path.join(TMP, "cache")

from qgis.core import (QgsApplication, QgsFeedback, QgsNetworkAccessManager,
                       QgsNetworkRequestParameters, QgsSettings, QgsTask)
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)       # the APIHeader auth method lives here
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtCore import QObject, pyqtSlot
from qgis.PyQt.QtNetwork import QNetworkRequest

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
    sys.exit(2)

from landslide_groundtruth import gegd_auth, gegd_net, gegd_tasks
from landslide_groundtruth.gegd_api import GegdError

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


am = QgsApplication.authManager()
am.setPasswordHelperEnabled(False)            # BEFORE any master-password call
if am.passwordHelperEnabled() or not os.path.realpath(
        am.authenticationDatabasePath()).startswith(os.path.realpath(TMP)):
    print("auth DB not isolated or Keychain helper still on; refusing to continue")
    sys.exit(2)
if not am.setMasterPassword(secrets.token_urlsafe(24), True):
    print("could not set the throwaway master password")
    sys.exit(2)

# Synthetic, shaped like nothing real. Never a real GEGD key.
KEY = "dummy-gegd-net-key-" + secrets.token_hex(12)
HDR = gegd_auth.KEY_HEADER

# ---------------------------------------------------------------- the servers
SEEN = []                       # {"srv", "tag", "key"} per request received
LK = threading.Lock()
SLOW_S = 3.0                    # longer than LIMIT: a cancel that only waits for the
                                # answer (instead of aborting) fails the 2 s check
PAGE = {"mode": "mixed"}        # what the mini catalog answers the key test with


def _feat(n, acq, source, product, sensor_type=None, third=None, **extra):
    props = {"featureId": "00000000-0000-4000-8000-%012d" % n, "acquisitionDate": acq,
             "source": source, "productName": product, "offNadirAngle": 20.0,
             "sunElevation": 30.0, "cloudCover": 0.01, "processingLevel": "LV3D"}
    if sensor_type is not None:
        props["sensorType"] = sensor_type
    if third is not None:
        props["isThirdParty"] = third
    props.update(extra)
    ring = [[-150.0, 61.0], [-149.9, 61.0], [-149.9, 61.1], [-150.0, 61.1], [-150.0, 61.0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": props}


def _day(d):
    return time.strftime("%Y-%m-%dT21:15:00Z", time.gmtime(time.time() - d * 86400))


PAGES = {
    # newest first, as the server sorts: the strip to name is the 7th record
    "mixed": [_feat(1, _day(1), "UMBRA", "THIRD_PARTY"),                    # no sensorType at all
              _feat(2, _day(2), "SKYSAT", "THIRD_PARTY", "Optical", True),  # third-party optical
              _feat(3, _day(3), "ICEYE", "DAILY_TAKE", "Radar"),            # radar, not flagged
              _feat(4, _day(4), "WV03", "DAILY_TAKE"),                      # no sensorType
              _feat(5, _day(5), "MULTIPLE", "VIVID_STANDARD_30", "Optical"),
              _feat(6, _day(6), "WV01", "RDOG_BASE", "Optical"),            # multi-date mosaic tile
              _feat(7, _day(7), "WV02", "DAILY_TAKE", "Optical"),           # <- this one
              _feat(8, _day(9), "GE01", "DAILY_TAKE", "Optical")],
    "third_only": [_feat(1, _day(1), "UMBRA", "THIRD_PARTY", "Radar", True),
                   _feat(2, _day(2), "CAPELLA", "THIRD_PARTY")],
    "empty": [],
}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=()):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        tag = self.path.lstrip("/").split("/")[0].split("?")[0]
        with LK:
            SEEN.append({"srv": self.server.label, "tag": tag, "key": self.headers.get(HDR),
                         "path": self.path})
        try:
            self._route(tag)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self, tag):
        if tag == "streaming":                         # the key test's WFS page
            feats = PAGES[PAGE["mode"]]
            doc = {"type": "FeatureCollection", "features": feats,
                   "numberReturned": len(feats)}
            return self._send(200, json.dumps(doc).encode())
        if tag.startswith("redir_off"):                # another host
            return self._redirect("http://localhost:%d/after_%s" % (PB, tag))
        if tag.startswith("redir_port"):               # same host name, another port
            return self._redirect("http://127.0.0.1:%d/after_%s" % (PB, tag))
        if tag.startswith("redir_same"):               # same origin, absolute
            return self._redirect("http://127.0.0.1:%d/json_after_%s" % (PA, tag))
        if tag.startswith("redir_rel"):                # same origin, relative
            return self._redirect("/json_after_%s?x=1" % tag)
        if tag.startswith("redir_loop"):
            return self._redirect(self.path)
        if tag.startswith("slow"):
            time.sleep(SLOW_S)
            return self._send(200, b'{"late": 1}')
        if tag.startswith("trickle"):                  # 4000 B at 800 B/s
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "4000")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b" " * 40)
                self.wfile.flush()
                time.sleep(0.05)
            return
        if tag.startswith("busy"):
            return self._send(503, b"busy", "text/plain")
        if tag.startswith("silent"):                   # nothing at all, for longer than the timeout
            time.sleep(2.5)
            return self._send(200, b'{"late": 1}')
        if tag.startswith("stallhdr"):                 # headers and a little body, then nothing
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"partial": ')
            self.wfile.flush()
            time.sleep(2.5)
            return
        return self._send(200, b'{"type": "FeatureCollection", "features": []}')

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Srv(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that aborts (every cancel here) resets the connection: expected.
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)


def serve(label):
    srv = Srv(("127.0.0.1", 0), H)
    srv.label = label
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SRV_A, SRV_B = serve("A"), serve("B")
PA, PB = SRV_A.server_port, SRV_B.server_port
A = "http://127.0.0.1:%d" % PA                  # the stand-in for pro.gegd.com
B_LOCAL = "http://localhost:%d" % PB            # every other host


def seen(prefix, srv=None):
    with LK:
        return [s for s in SEEN if s["tag"].startswith(prefix) and (srv is None or s["srv"] == srv)]


def keys(rows):
    return [r["key"] for r in rows]


def pump(seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        QgsApplication.processEvents()
        time.sleep(0.01)


def wait_until(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        QgsApplication.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    QgsApplication.processEvents()
    return pred()


# ---------------------------------------------------------------- instrumentation
# What the Network Logger and the HostGuard see: the main manager's
# requestAboutToBeCreated (worker managers forward theirs to it).
TAPS = []


def _tap(p):
    r = p.request()
    TAPS.append({"url": r.url().toString(), "has": r.hasRawHeader(HDR.encode()),
                 "initiator": p.initiatorClassName(),
                 "policy": r.attribute(QNetworkRequest.RedirectPolicyAttribute)})


QgsNetworkAccessManager.instance().requestAboutToBeCreated[QgsNetworkRequestParameters].connect(_tap)


def taps(fragment):
    return [t for t in TAPS if fragment in t["url"]]


LIFE = {"created": 0, "destroyed": 0}           # replies to the two test servers
CANCEL_AT = {}                                  # tag -> (feedback, [t_cancel])


def _ours(url):
    return url.host() in ("127.0.0.1", "localhost") and url.port() in (PA, PB)


def _cancel_here(url, where):
    """Cancel the feedback registered for this request's tag, from INSIDE get()
    on the worker thread: the window the old code could not survive."""
    tag = url.path().lstrip("/").split("/")[0]
    if where in tag and tag in CANCEL_AT:
        fb, when = CANCEL_AT[tag]
        when.append(time.monotonic())
        fb.cancel()


class _Life(QObject):
    """Counts destroyed replies. A real slot on a GUI-thread QObject, not a plain
    function: PyQt would wrap a function in a slot proxy living on the worker
    thread, and a reply that gegd_net hands to the GUI thread would then signal
    a proxy whose thread is done with it (a direct connection crashed there; a
    queued one is never delivered)."""

    @pyqtSlot()
    def gone(self):
        with LK:
            LIFE["destroyed"] += 1


LIFE_RX = _Life()


def _on_request(req):                           # a request preprocessor
    try:
        if _ours(req.url()):
            _cancel_here(req.url(), "cancel_in_pre")
    except Exception as e:                      # noqa: BLE001 -- shown, never swallowed silently
        print("  request preprocessor:", e)


def _on_reply(req, reply):                      # a reply preprocessor
    try:
        if _ours(req.url()):
            with LK:
                LIFE["created"] += 1
            reply.destroyed.connect(LIFE_RX.gone)
            _cancel_here(req.url(), "cancel_in_reply")
    except Exception as e:                      # noqa: BLE001
        print("  reply preprocessor:", e)


PRE_ID = QgsNetworkAccessManager.setRequestPreprocessor(_on_request)
REPLY_ID = QgsNetworkAccessManager.setReplyPreprocessor(_on_reply)


class FetchTask(QgsTask):
    """gegd_net.fetch in a QgsTask worker, the way gegd_tasks runs it."""

    def __init__(self, url, **kw):
        super().__init__("gegd_net test fetch", QgsTask.CanCancel)
        self.url, self.kw = url, kw
        self.fb = QgsFeedback()
        self.result_ = self.error_ = None
        self.ran = False
        self.t_end = None
        self.tb = None                  # traceback of anything but a GegdError

    def cancel(self):
        self.fb.cancel()
        super().cancel()

    def run(self):
        self.ran = True
        try:
            self.result_ = gegd_net.fetch(self.url, "wfs", feedback=self.fb, **self.kw)
            return True
        except Exception as e:                  # noqa: BLE001 -- checked below
            self.error_ = e
            if not isinstance(e, GegdError):
                self.tb = traceback.format_exc()
            return False
        finally:
            self.t_end = time.monotonic()


ALL_TASKS = []


def start(task):
    ALL_TASKS.append(task)
    QgsApplication.taskManager().addTask(task)
    return task


def done(task):
    try:
        return task.status() in (QgsTask.Complete, QgsTask.Terminated)
    except RuntimeError:            # the task manager deletes a task once it has ended
        return True


def run_task(task, timeout=15):
    start(task)
    wait_until(lambda: done(task), timeout)
    return task


def kind(task):
    e = task.error_
    return None if e is None else ("cancelled" if isinstance(e, gegd_net.Cancelled)
                                   else getattr(e, "kind", type(e).__name__))


# ---------------------------------------------------------------- setup
print("\n=== setup: a synthetic key, the guard aimed at the local stand-in ===")
check("store_key saves the synthetic key", gegd_auth.store_key(KEY) is True)
check("mode() is 'authcfg' (APIHeader available)", gegd_auth.mode() == "authcfg")
guard = gegd_auth.install_host_guard(allowed=("http", "127.0.0.1", PA))
check("ensure_ready() on the GUI thread", gegd_auth.ensure_ready() is True)
gegd_net.BACKOFF_S = (0.05, 0.1, 0.2)           # the real 5/10/20 s, shrunk
U0 = gegd_auth.usage_today()[0]

print("\n=== the key from a worker thread: allowed host yes, anywhere else no ===")
t = run_task(FetchTask(A + "/json_ok?a=1"))
check(f"fetch in a QgsTask returns the reply ({t.error_!r})",
      t.error_ is None and t.result_ is not None and t.result_.status == 200
      and b"FeatureCollection" in t.result_.body)
check("… and the request carried the key", keys(seen("json_ok")) == [KEY])
pump(0.3)
tp = taps("/json_ok")
check(f"one request created, seen once by the main manager ({len(tp)})", len(tp) == 1)
check("… with the initiator kept for the Network Logger and ManualRedirectPolicy set",
      tp and tp[0]["initiator"] == gegd_net.INITIATOR and tp[0]["has"]
      and tp[0]["policy"] == QNetworkRequest.ManualRedirectPolicy)
check("the usage meter counted it once", gegd_auth.usage_today()[0] - U0 == 1)
s0 = guard.stripped
t = run_task(FetchTask(B_LOCAL + "/json_offhost"))
check(f"a worker request to another host: no key arrives ({kind(t)})",
      t.error_ is None and keys(seen("json_offhost")) == [None] and guard.stripped > s0)
u1 = gegd_auth.usage_today()[0]
check("… and it is not counted as GEGD usage", u1 - U0 == 1)

print("\n=== no auth config: nothing is sent, nothing is even created ===")
gegd_auth.forget_key()
n_created = LIFE["created"]
t = run_task(FetchTask(A + "/json_nokey"))
pump(0.2)
check(f"fetch says no key is stored ({kind(t)}: {t.error_})",
      kind(t) == "key_not_sent" and "No Maxar/Vantor GEGD Pro key is stored" in str(t.error_))
check("… the server saw nothing, and no request or reply was created",
      seen("json_nokey") == [] and taps("/json_nokey") == [] and LIFE["created"] == n_created)
check("store_key again", gegd_auth.store_key(KEY) is True)

print("\n=== timeouts: bounded, retried, never a truncated 200 ===")
t0 = time.monotonic()
t = run_task(FetchTask(A + "/silent_1", timeout_s=1, retries=1))
dt = time.monotonic() - t0
check(f"a server that never answers: retried once, then 'no answer within 1 s' "
      f"({kind(t)}, {dt:.1f} s, {len(seen('silent_1'))} requests)",
      kind(t) == "network" and "no answer within 1 s (2 tries)" in str(t.error_.detail)
      and len(seen("silent_1")) == 2 and dt < 4.0)
t0 = time.monotonic()
t = run_task(FetchTask(A + "/stallhdr_1", timeout_s=1, retries=0))
dt = time.monotonic() - t0
check(f"headers then silence: a timeout too, not a truncated 200 handed to the parser "
      f"({kind(t)}, {dt:.1f} s)",
      kind(t) == "network" and "no answer within 1 s" in str(t.error_.detail)
      and t.result_ is None and dt < 3.0)

print("\n=== redirects: the same origin is followed, nothing else is ===")
t = run_task(FetchTask(A + "/redir_off_1/x"))
check(f"302 to another host: refused ({kind(t)})", kind(t) == "endpoint_moved")
check("… the first hop carried the key; the other host received nothing at all",
      keys(seen("redir_off_1", "A")) == [KEY] and seen("after_redir_off_1") == []
      and taps("after_redir_off_1") == [])
t = run_task(FetchTask(A + "/redir_port_1/x"))
check(f"302 to the same host name on another port: refused, nothing sent ({kind(t)})",
      kind(t) == "endpoint_moved" and seen("after_redir_port_1") == [])
u_before = gegd_auth.usage_today()[0]
t = run_task(FetchTask(A + "/redir_same_1/x"))
check(f"302 within the same origin: followed to the answer ({t.error_!r})",
      t.error_ is None and t.result_ is not None and t.result_.status == 200)
pump(0.3)
check("… both hops carried the key, and each was counted once",
      keys(seen("redir_same_1")) == [KEY] and keys(seen("json_after_redir_same_1")) == [KEY]
      and gegd_auth.usage_today()[0] - u_before == 2)
t = run_task(FetchTask(A + "/redir_rel_1/x"))
check(f"a relative Location is resolved against the request and followed ({t.error_!r})",
      t.error_ is None and keys(seen("json_after_redir_rel_1")) == [KEY])
t = run_task(FetchTask(A + "/redir_loop_1/x"))
check(f"a redirect loop stops after {gegd_net.MAX_REDIRECTS} hops ({kind(t)}, "
      f"{len(seen('redir_loop_1'))} requests)",
      kind(t) == "network" and len(seen("redir_loop_1")) == gegd_net.MAX_REDIRECTS + 1)

# ---------------------------------------------------------------- the race
print("\n=== cancel at every point of a request: each task ends within 2 s ===")
LIMIT = 2.0
RESULTS = []                    # (mode, ok, detail)


def record(mode, task, t_cancel, expect_cancelled=True, extra_ok=True, note=""):
    """Wait for ``task`` to end; ok when it ended within LIMIT of the cancel, as a
    Cancelled (or before it ever ran)."""
    finished = wait_until(lambda: done(task), LIMIT + 1.0)
    end = task.t_end if task.t_end is not None else time.monotonic()
    dt = (end - t_cancel) if t_cancel is not None else float("nan")
    honest = (isinstance(task.error_, gegd_net.Cancelled) or not task.ran) if expect_cancelled \
        else True
    ok = finished and dt <= LIMIT and honest and extra_ok
    RESULTS.append((mode, ok, f"{dt:.2f} s, ran={task.ran}, {kind(task)}{note}"))
    return ok


random.seed(int(os.environ.get("GEGD_NET_SEED", "0")) or None)
n = 0

# 1. cancelled before it starts: nothing sent
for i in range(5):
    n += 1
    tk = FetchTask(A + "/slow_before_%d" % n)
    t_c = time.monotonic()
    tk.fb.cancel()
    start(tk)
    record("before", tk, t_c, extra_ok=seen("slow_before_%d" % n) == [])

# 2./3. cancelled on the worker thread from inside get(): in the request
# preprocessor (before the reply exists) and in the reply preprocessor (after it
# exists, before get() returns -- where the old code's two connections straddled)
for where in ("cancel_in_pre", "cancel_in_reply"):
    for i in range(8):
        n += 1
        tag = "slow_%s_%d" % (where, n)
        tk = FetchTask(A + "/" + tag)
        when = []
        CANCEL_AT[tag] = (tk.fb, when)
        start(tk)
        wait_until(lambda: done(tk) or when, 5.0)
        record(where, tk, when[0] if when else None)

# 4. cancelled from the GUI thread in the first milliseconds (the race
# test_gegd_tab used to lose about 4 times in 7): every 4th one 0-15 ms after it
# was queued, the rest 0-5 ms after the worker entered run(), while it is still
# building the request or inside get() -- the old code's window itself
START = {"n": 0, "running": 0, "sent": 0}
DELAYS = (0.0, 0.0002, 0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005)
for i in range(36):
    n += 1
    tag = "slow_start_%d" % n
    tk = start(FetchTask(A + "/" + tag))
    if i % 4 == 0:
        time.sleep(random.uniform(0.0, 0.015))     # from the moment it was queued
    else:
        t_wait = time.monotonic()
        while not tk.ran and time.monotonic() - t_wait < 1.0:
            time.sleep(0.0001)                     # lets the worker take the GIL
        time.sleep(random.choice(DELAYS))
    t_c = time.monotonic()
    START["n"] += 1
    START["running"] += bool(tk.ran)
    tk.cancel()
    record("start", tk, t_c)
    START["sent"] += bool(seen(tag))

# 5. mid-transfer: the server is trickling the body
for i in range(8):
    n += 1
    tag = "trickle_%d" % n
    tk = start(FetchTask(A + "/" + tag))
    wait_until(lambda: seen(tag), 3.0)
    time.sleep(random.uniform(0.1, 0.6))
    t_c = time.monotonic()
    tk.cancel()
    record("mid", tk, t_c, extra_ok=len(seen(tag)) == 1)

# 6. during a retry wait (a 503, then a 1.5 s backoff)
gegd_net.BACKOFF_S = (1.5,)
for i in range(8):
    n += 1
    tag = "busy_%d" % n
    tk = start(FetchTask(A + "/" + tag))
    wait_until(lambda: seen(tag), 3.0)
    time.sleep(random.uniform(0.15, 1.0))
    t_c = time.monotonic()
    tk.cancel()
    record("sleep", tk, t_c, extra_ok=len(seen(tag)) == 1, note=f", {len(seen(tag))} sent")
gegd_net.BACKOFF_S = (0.05, 0.1, 0.2)

# 7. a burst: 12 tasks at once, each cancelled at its own random moment
burst = [start(FetchTask(A + "/slow_burst_%d" % (n + k))) for k in range(12)]
n += 12
cancels = []
for tk in random.sample(burst, len(burst)):
    time.sleep(random.uniform(0.0, 0.03))
    cancels.append((tk, time.monotonic()))
    tk.cancel()
for tk, t_c in cancels:
    record("burst", tk, t_c)

by_mode = {}
for mode, ok, detail in RESULTS:
    by_mode.setdefault(mode, []).append((ok, detail))
for mode, rows in by_mode.items():
    bad = [d for ok, d in rows if not ok]
    worst = max(float(d.split(" s")[0]) if d.split(" s")[0] != "nan" else 99 for _ok, d in rows)
    check(f"{mode:16s} {len(rows) - len(bad)}/{len(rows)} ended within {LIMIT:g} s "
          f"(slowest {worst:.2f} s){': ' + '; '.join(bad[:3]) if bad else ''}", not bad)
check(f"{len(RESULTS)} cancels in all (50+ wanted)", len(RESULTS) >= 50)
ran_and_sent = sum(1 for tk in ALL_TASKS if tk.ran)
check(f"the races really ran: {ran_and_sent} tasks started work; of the {START['n']} "
      f"GUI-thread cancels, {START['running']} landed while the task was running and "
      f"{START['n'] - START['sent']} before its request reached the server",
      ran_and_sent >= 40 and START["running"] >= START["n"] // 2)

# ---------------------------------------------------------------- wrappers
print("\n=== quick fetches on 6 task threads at once: no live reply loses its wrapper ===")


class LoopTask(QgsTask):
    """``n`` quick fetches in a row on one task thread, as a thumbnail task makes
    them. Deleting replies on worker threads killed 4-19 % of the live wrappers
    in a burst like this (PyQt marks a wrapper dead by address, late, for an
    object destroyed off the GUI thread); gegd_net deletes them on the GUI
    thread instead."""

    def __init__(self, tag, n):
        super().__init__("gegd_net test burst", QgsTask.CanCancel)
        self.url = A + "/json_quick_%s" % tag
        self.tag, self.n = tag, n
        self.fb = QgsFeedback()
        self.ok = 0
        self.errors = []
        self.tb = None                  # traceback of anything but a GegdError
        self.ran = False
        self.t_end = None

    def run(self):
        self.ran = True
        try:
            for i in range(self.n):
                try:
                    r = gegd_net.fetch("%s_%d" % (self.url, i), "wfs", feedback=self.fb, retries=0)
                    self.ok += r.status == 200
                except Exception as e:          # noqa: BLE001 -- checked below
                    self.errors.append(f"{type(e).__name__}: {e}")
                    if not isinstance(e, GegdError) and self.tb is None:
                        self.tb = traceback.format_exc()
        finally:
            self.t_end = time.monotonic()
        return True


N_LOOP, PER_LOOP = 6, 60
loops = [start(LoopTask(k, PER_LOOP)) for k in range(N_LOOP)]
wait_until(lambda: all(done(tk) for tk in loops), 60)
answered = sum(tk.ok for tk in loops)
errs = [e for tk in loops for e in tk.errors]
check(f"{answered}/{N_LOOP * PER_LOOP} answered, {len(errs)} failed"
      f"{' (first: ' + errs[0] + ')' if errs else ''}",
      answered == N_LOOP * PER_LOOP and not errs)
check("gegd_net defines no QObject subclass (its docstring: one deadlocked a stress run)",
      not [n for n, v in vars(gegd_net).items()
           if isinstance(v, type) and issubclass(v, QObject)
           and v.__module__ == gegd_net.__name__])

print("\n=== after the races: nothing left behind ===")
odd = [tk for tk in ALL_TASKS if getattr(tk, "tb", None)]
check(f"no fetch raised anything but a GegdError ({len(odd)})", not odd)
for tk in odd[:2]:
    print("       " + tk.url + "\n" + tk.tb)
still = [tk for tk in ALL_TASKS if not done(tk)]
check(f"no task is still running ({len(still)})", not still)
pump(0.5)
with LK:
    life = dict(LIFE)
check(f"every reply was deleted ({life['destroyed']}/{life['created']})",
      life["created"] > 0 and life["destroyed"] == life["created"])
check(f"the HostGuard saw a finished for every request it counted ({len(guard._open)} open)",
      not guard._open)
allowed = [x for x in TAPS if x["url"].startswith(A + "/")]
check(f"usage = one per request created on the allowed host "
      f"({gegd_auth.usage_today()[0] - U0} counted, {len(allowed)} created)",
      gegd_auth.usage_today()[0] - U0 == len(allowed))
check("every allowed-host request carried the key and manual redirects",
      allowed and all(x["has"] and x["policy"] == QNetworkRequest.ManualRedirectPolicy
                      for x in allowed))
check("nothing the servers received carried the key off the allowed host",
      all(k is None for k in keys([s for s in SEEN if s["srv"] == "B"])))

print("\n=== KeyTestTask names the newest Vantor optical strip ===")


def key_test(mode):
    PAGE["mode"] = mode
    tk = gegd_tasks.KeyTestTask(host=A)
    ALL_TASKS.append(tk)
    QgsApplication.taskManager().addTask(tk)
    wait_until(lambda: done(tk), 15)
    return tk


before = len(seen("streaming"))
kt = key_test("mixed")
req = seen("streaming")[before:]
check(f"the key test succeeded ({kt.error!r})", kt.error is None and done(kt))
check(f"one request, with the key, for a page of {gegd_tasks.KEY_TEST_COUNT} newest first",
      len(req) == 1 and req[0]["key"] == KEY
      and "count=%d" % gegd_tasks.KEY_TEST_COUNT in req[0]["path"]
      and "acquisitionDate+D" in req[0]["path"].replace("%20", "+"))
nw = kt.newest
check(f"it names the WV02 Daily Take, skipping third-party (with and without a flag), "
      f"radar, no sensorType, Vivid and a mosaic tile ({nw and nw.label})",
      nw is not None and nw.feature_id.endswith("000000000007") and nw.sensor == "WV02"
      and kt.n_records == 8)
kt = key_test("third_only")
check(f"only third-party records: nothing named, the page still counted "
      f"({kt.newest}, {kt.n_records})",
      kt.error is None and kt.newest is None and kt.n_records == 2)
kt = key_test("empty")
check("an empty window: nothing named, 0 records", kt.error is None and kt.newest is None
      and kt.n_records == 0)
def mk(**k):
    base = dict(product_name="DAILY_TAKE", is_third_party=False, sensor_type="Optical",
                multi_date=False, acq_utc=1)
    base.update(k)
    return type("S", (), base)()



check("vantor_optical: Optical Daily Take yes; THIRD_PARTY, flagged third-party, radar, "
      "no sensorType, Vivid, BASE_MOSAIC, multi-date no",
      gegd_tasks.vantor_optical(mk()) and gegd_tasks.vantor_optical(mk(sensor_type=" optical "))
      and not any(gegd_tasks.vantor_optical(mk(**k)) for k in (
          dict(is_third_party=True), dict(sensor_type="Radar"), dict(sensor_type=None),
          dict(product_name="VIVID_STANDARD_30"), dict(product_name="BASE_MOSAIC"),
          dict(multi_date=True), dict(acq_utc=None))))

# ---------------------------------------------------------------- teardown
QgsNetworkAccessManager.removeRequestPreprocessor(PRE_ID)
QgsNetworkAccessManager.removeReplyPreprocessor(REPLY_ID)
gegd_auth.forget_key()
gegd_auth.remove_host_guard()
SRV_A.shutdown(); SRV_B.shutdown()
hung = [tk for tk in ALL_TASKS if not done(tk)]
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
if hung:
    # A worker stuck for good would hold QGIS's exit forever: leave without it.
    print(f"{len(hung)} task(s) never ended; exiting without waiting for them")
    sys.stdout.flush()
    os._exit(1)
sys.exit(1 if FAIL else 0)
