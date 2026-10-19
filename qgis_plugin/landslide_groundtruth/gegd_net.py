"""One request to Maxar/Vantor GEGD Pro, made the only way GEGD bytes travel here.

Every GEGD byte goes through QGIS's own network stack: QGIS's proxy, SSL and
cache settings apply, the key is added by the ``gegdpro`` auth config (or the
fallback HeaderInjector) and never by this code, and the HostGuard sees every
request. That rules out ``requests``/urllib (they bypass all three), a subprocess
or ``GDAL_HTTP_HEADERS`` (the key would sit in a command line or environment), and
``QgsFileDownloader``, which lets Qt follow a redirect internally, without the
preprocessors, and so carried the key to a second host in the M0 check.

``fetch()`` is meant for QgsTask threads (it also works on the GUI thread, which
the tests use): it blocks its caller while a local event loop runs, and its retry
waits sleep. A ``QgsFeedback`` aborts the transfer and any wait. It returns a
``Reply`` for a usable 2xx answer and raises ``GegdError`` for everything else
(``Cancelled`` when the feedback was cancelled); GegdError passes every string
through ``redact()``.

  * 429 and 5xx answers and timeouts are retried up to ``retries`` times after
    5, 10 and 20 s, or after ``Retry-After`` when the server asks for longer. A
    ``Retry-After`` beyond two minutes is reported at once instead of waited out
    inside a task. Refused connections, unknown hosts and TLS errors are not
    retried: they do not fix themselves in 20 s, and retrying only hides them.
  * An HTTP 200 can still be a failure: GeoServer reports a bad CQL filter or an
    oversized request as an OGC ``ServiceExceptionReport`` with status 200, and
    ``classify_http`` parses it before the body is handed back.
  * The status alone does not say what went wrong; ``classify_http`` reads
    (service, status, body). A 401 "No JWT bearer token" means the key was not
    sent only on the streaming services; on Discovery or usage endpoints the
    same 401 means "not available to API keys".
  * A missing ``gegdpro`` config is caught before anything is sent (the auth
    manager cannot apply it) and reported as "no key stored", not as a network
    fault or a 401.
  * Qt never follows a redirect (every request is ManualRedirectPolicy). A 3xx to
    the SAME origin (scheme, host, port) is followed here, through the auth config
    and the HostGuard again, up to MAX_REDIRECTS hops; a 3xx anywhere else is
    refused as ``endpoint_moved`` and nothing at all is sent to the new address.

Why not QgsBlockingNetworkRequest
  It hangs its thread for good when a cancel lands in the first milliseconds of a
  request (QGIS 3.44, measured: test_gegd_tab stalled in about 4 of 7 runs, and
  Cancel / Save key / Forget key / a plugin reload could leave a task running
  forever). doRequest connects the feedback to its own abort() before the reply
  exists, and to the reply's abort() only after; the feedback lives on the GUI
  thread, so both connections are queued. A cancel between the two delivers only
  abort(), which deleteLater()s the reply: the reply is deleted without ever
  emitting finished, and the loop inside waits for that finished forever.

  So ``_once`` drives the reply itself: a non-blocking get on the calling thread's
  ``QgsNetworkAccessManager.instance()`` (a worker thread gets its own; QGIS
  forwards its requestAboutToBeCreated / finished / downloadProgress to the main
  instance, which is where the HostGuard counts, once per request) and a local
  QEventLoop that ends on whichever comes first:
    - the reply's ``finished``;
    - the feedback being cancelled, seen by a POLL_MS timer that reads
      ``isCanceled()``. Polling, not a signal: no cancel can fall between two
      connections, and nothing is queued across threads to objects that are
      about to be deleted;
    - no bytes moving for timeout_s + STALL_GRACE_S, a backstop behind Qt's own
      transfer timeout in case an abort never reaches ``finished``.
  The loop is never left waiting on a signal alone, and a reply still running is
  aborted when it ends.

Where the reply is deleted: on the GUI thread
  PyQt (5.15) watches every C++-created QObject it has wrapped (its PyQtMonitor)
  and, when one is destroyed, marks dead whatever wrapper sits at that ADDRESS.
  For an object destroyed on the GUI thread that happens inside the destructor.
  For one destroyed on a worker it happens later, and by then the address can
  belong to a new reply on another task thread, whose live wrapper is killed
  ("wrapped C/C++ object of type QNetworkReply has been deleted"): the request
  fails with a RuntimeError and its reply is never deleted. Measured with 6 task
  threads x 400 quick GETs: deleting each reply on its worker killed 4-19 % of
  the live wrappers whether through deleteLater, deleteLater after dropping the
  wrapper, or sip.delete; moving it to the GUI thread and deleting it there
  killed 0 of 2400, the same as never deleting at all. So ``_release`` hands
  every finished reply to the GUI thread (unparented, C++-owned, moved,
  deleteLater) and only deletes in place when it already is on that thread.

No Python QObject subclass here (a deadlock, measured)
  A worker deleting a QObject with the GIL released holds a Qt signal/slot mutex
  while Qt calls disconnectNotify() on each object still connected to it; a
  Python-made sender (a QTimer from here) answers that call by taking the GIL.
  PyQt's get() and connect() hold the GIL while taking those same mutexes (a
  shared pool of 131, picked by address). The earlier helper QObject, deleted
  while its timer was still connected to it, deadlocked a stress run that way:
  one worker waiting for the GIL under a mutex, the other holding the GIL inside
  get() waiting for that mutex, and every other thread, GUI included, waiting
  for the GIL. ``_wait`` therefore uses a plain QEventLoop and QTimer, breaks
  every connection with the GIL held, and drops the timer before the loop, so
  nothing ever calls back into Python under a Qt mutex. Stall detection polls
  the reply instead of connecting Python slots to it.
"""
import os
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from qgis.core import QgsApplication, QgsNetworkAccessManager, QgsNetworkRequestParameters
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QEventLoop, QMetaObject, Qt, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from . import gegd_auth
from .gegd_api import SERVICES, GegdError, classify_http

Reply = namedtuple("Reply", "status ctype body error")

# Waits before retry 1, 2, 3 (s). A longer Retry-After wins, up to the cap.
BACKOFF_S = (5, 10, 20)
RETRY_AFTER_MAX_S = 120
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
POLL_MS = 100                   # how often the wait looks at the feedback
STALL_GRACE_S = 5.0             # backstop beyond the transfer timeout
MAX_REDIRECTS = 5               # same-origin hops followed per request
_TIMEOUTS = frozenset({QNetworkReply.TimeoutError, QNetworkReply.OperationCanceledError,
                       QNetworkReply.ProxyTimeoutError})
_TRANSIENT = _TIMEOUTS | {QNetworkReply.RemoteHostClosedError,
                          QNetworkReply.TemporaryNetworkFailureError,
                          QNetworkReply.NetworkSessionFailedError,
                          QNetworkReply.UnknownNetworkError}
INITIATOR = "LandslideGegd"     # what the Network Logger lists as the initiator
_DEFAULT_PORT = {"https": 443, "http": 80}

# One answer, as read off the reply: url is the address this hop asked, location
# the resolved-or-relative Location of a 3xx (None otherwise), stalled that the
# wait gave up because no bytes moved.
_Attempt = namedtuple("_Attempt", "status ctype body headers qerr message url location stalled")


class Cancelled(GegdError):
    """The caller's QgsFeedback was cancelled. kind stays 'network' (gegd_api's
    KINDS has no 'cancelled'); test for the class, or for ``.cancelled``."""
    cancelled = True

    def __init__(self, service=None):
        super().__init__("network", "Cancelled.", service=service)


def cache_dir(*parts):
    """``~/.cache/landslide_gegd/<parts>`` (or ``$LANDSLIDE_GEGD_CACHE``), created.

    Outside the Drive-synced project folder on purpose: raw tiles and thumbnails
    are rebuilt on demand, and Drive can serve a stale copy of a file it is still
    syncing."""
    base = (os.environ.get("LANDSLIDE_GEGD_CACHE")
            or os.path.join(os.path.expanduser("~"), ".cache", "landslide_gegd"))
    path = os.path.join(base, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _retry_after(value):
    """Seconds from a Retry-After header (delta-seconds or an HTTP date), or None."""
    v = (value or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _sleep(seconds, feedback, service):
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if feedback is not None and feedback.isCanceled():
            raise Cancelled(service)
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.1, left))


def _origin(u):
    s = u.scheme().lower()
    return s, u.host().lower(), u.port(_DEFAULT_PORT.get(s, -1))


def _timed_out(a):
    return a.stalled or a.qerr in _TIMEOUTS


def _stall_s(timeout_s):
    """How long the wait lets a transfer move no bytes before giving up itself.

    Qt's transfer timeout (timeout_s) or QGIS's network timeout normally ends it
    first, with an abort that emits finished; this is only the backstop."""
    try:
        base = float(timeout_s) if timeout_s else QgsNetworkAccessManager.timeout() / 1000.0
    except (TypeError, ValueError):
        base = 0.0
    return base + STALL_GRACE_S if base > 0 else None


def _no_key(service):
    return GegdError("key_not_sent",
                     "No Maxar/Vantor GEGD Pro key is stored in this QGIS "
                     f"(auth config {gegd_auth.AUTHCFG_ID}). Paste the key in "
                     "the GEGD Pro connection box and press Save key.",
                     service=service)


def _apply_authcfg(am, req):
    """(applied, request) for the 'gegdpro' config. False when QGIS cannot apply
    it (no such config, or the store is locked): the caller then sends nothing.
    PyQGIS 3.44 returns (bool, QNetworkRequest); older bindings edit in place."""
    res = am.updateNetworkRequest(req, gegd_auth.AUTHCFG_ID)
    if isinstance(res, tuple):
        return bool(res[0]), (res[1] if len(res) > 1 and res[1] is not None else req)
    return bool(res), req


def _wait(reply, feedback, stall_s):
    """Run a local event loop until ``reply`` finishes ('finished'), ``feedback``
    is cancelled ('cancelled'), no bytes have moved for ``stall_s`` ('stalled'),
    or this thread's event loops refuse to run ('interrupted').

    Never waits on a signal alone: a POLL_MS timer wakes the loop to re-check all
    of it. The feedback (GUI thread) is only read, never connected to. Nothing
    Python is connected to the reply: progress is read off it at each wake-up
    (bytes buffered so far, and whether the headers are in). See the module
    docstring for why there is no QObject subclass here and why the teardown
    order matters."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(POLL_MS)
    timer.timeout.connect(loop.quit)
    reply.finished.connect(loop.quit)
    try:
        timer.start()
        mark, moved = None, time.monotonic()
        while True:
            if reply.isFinished():
                return "finished"
            if feedback is not None and feedback.isCanceled():
                return "cancelled"
            now = time.monotonic()
            m = (reply.bytesAvailable(), reply.attribute(QNetworkRequest.HttpStatusCodeAttribute))
            if m != mark:
                mark, moved = m, now
            elif stall_s and now - moved > stall_s:
                return "stalled"
            if loop.exec_() < 0:
                # exec() refused to run (the thread is being told to quit): give
                # up rather than spin.
                return "finished" if reply.isFinished() else "interrupted"
    finally:
        # With the GIL held, so any disconnectNotify that wants it already has it.
        timer.stop()
        try:
            timer.timeout.disconnect(loop.quit)
        except (TypeError, RuntimeError):
            pass
        try:
            reply.finished.disconnect(loop.quit)
        except (TypeError, RuntimeError):
            pass
        # Deleted here, on this thread, and the timer (a sender) first: a dying
        # sender never calls back into Python, and the loop then has nothing
        # left connected to it. (Here, not whenever Python drops them: a
        # Cancelled's traceback keeps this frame alive on the task, which is
        # released on the GUI thread.)
        del timer
        del loop


_GUI = []                           # the GUI thread's QThread, looked up once


def _gui_thread():
    if not _GUI:
        app = QCoreApplication.instance()
        if app is None:
            return None
        _GUI.append(app.thread())
    return _GUI[0]


def _release(reply):
    """Delete ``reply`` (finished or aborted) on the GUI thread.

    On the GUI thread: at once. On a worker: unparented (C++ keeps ownership, so
    dropping the wrapper here deletes nothing), moved to the GUI thread, and its
    deleteLater() queued to run THERE, where PyQt marks its wrapper dead inside
    the destructor instead of later at an address that may have been reused (see
    the module docstring). Queued as a call rather than posted from here: a
    deleteLater() posted from another thread waits for a running event loop,
    while one called on the GUI thread is honoured by any event processing
    (QGIS's own loop, or a script's processEvents())."""
    try:
        gui = _gui_thread()
        if gui is None or threading.current_thread() is threading.main_thread():
            reply.deleteLater()
            QCoreApplication.sendPostedEvents(reply, QEvent.DeferredDelete)
            return
        reply.setParent(None)
        sip.transferto(reply, None)
        reply.moveToThread(gui)
        QMetaObject.invokeMethod(reply, "deleteLater", Qt.QueuedConnection)
    except RuntimeError:
        pass


def _dispose(reply):
    """Abort ``reply`` if it is still running (that emits finished, so the
    HostGuard closes its count), then hand it over for deletion."""
    try:
        if not reply.isFinished():
            reply.abort()
    except RuntimeError:
        return                      # its wrapper is dead: nothing can reach it now
    _release(reply)


def _read(reply, stalled):
    status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    headers = {}
    for name in reply.rawHeaderList():
        headers[bytes(name).decode("latin-1").lower()] = bytes(reply.rawHeader(name)).decode("latin-1")
    loc = reply.attribute(QNetworkRequest.RedirectionTargetAttribute)
    qerr = reply.error()
    if stalled or 0 < int(qerr) < 200:
        # A network- or proxy-layer failure (timeout, abort, connection closed
        # mid-body): the headers may have arrived, the body did not. Reading the
        # status would hand a truncated 200 to the parser as a good answer.
        status = None
    body = bytes(reply.readAll()) if reply.isOpen() else b""     # an aborted reply is closed
    return _Attempt(int(status) if status else None,
                    headers.get("content-type", ""), body, headers, qerr,
                    reply.errorString() if qerr != QNetworkReply.NoError else "",
                    QUrl(reply.url()),
                    QUrl(loc) if isinstance(loc, QUrl) and not loc.isEmpty() else None,
                    bool(stalled))


def _once(url, service, how, timeout_s, feedback, force_refresh):
    """One GET of ``url`` (a QUrl), no retry and no redirect followed.

    Returns an _Attempt. Raises Cancelled when ``feedback`` is cancelled first,
    and GegdError('key_not_sent') when the 'gegdpro' config cannot be applied,
    in which case nothing is sent. When this returns the reply has finished (or
    been aborted) and is on its way to deletion on the GUI thread."""
    req = QNetworkRequest(QUrl(url))
    req.setAttribute(QNetworkRequest.Attribute(int(QgsNetworkRequestParameters.AttributeInitiatorClass)),
                     INITIATOR)
    # Qt must never follow a redirect by itself (it would skip the preprocessors
    # and carry the key along); fetch() decides about every 3xx.
    req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
    req.setAttribute(QNetworkRequest.CacheLoadControlAttribute,
                     QNetworkRequest.AlwaysNetwork if force_refresh else QNetworkRequest.PreferCache)
    req.setAttribute(QNetworkRequest.CacheSaveControlAttribute, True)
    if timeout_s:
        # Qt aborts a transfer that has moved no bytes for this long; QGIS's own
        # global network timeout (Settings > Network) still applies on top.
        req.setTransferTimeout(int(timeout_s * 1000))
    am = None
    if how == "authcfg":
        am = QgsApplication.authManager()
        ok, req = _apply_authcfg(am, req)
        if not ok:
            raise _no_key(service)
    if feedback is not None and feedback.isCanceled():
        raise Cancelled(service)

    # This thread's manager (a worker gets its own); the preprocessors, the
    # HostGuard among them, run inside get().
    reply = QgsNetworkAccessManager.instance().get(req)
    try:
        if am is not None and not am.updateNetworkReply(reply, gegd_auth.AUTHCFG_ID):
            raise GegdError("key_not_sent",
                            "QGIS could not apply the Maxar/Vantor GEGD Pro auth config "
                            f"({gegd_auth.AUTHCFG_ID}) to the answer, so it was not used. "
                            "Press Save key again to rebuild it.", service=service)
        state = _wait(reply, feedback, _stall_s(timeout_s))
        if not reply.isFinished():
            reply.abort()               # cancelled, stalled or interrupted
        if state in ("cancelled", "interrupted"):
            raise Cancelled(service)
        return _read(reply, state == "stalled")
    finally:
        _dispose(reply)


def fetch(url, service, timeout_s=30, retries=3, feedback=None, force_refresh=True):
    """GET ``url`` from GEGD and return ``Reply(status, ctype, body, error=None)``.

    ``service`` is one of gegd_api.SERVICES ('wfs', 'wms', 'wmts', 'wcs',
    'seamlines', ...) and decides how an error status is read. ``url`` carries no
    auth: the key is added by the auth config. Raises GegdError on any failure
    (``Cancelled`` when ``feedback`` is cancelled). ``force_refresh`` bypasses
    QGIS's HTTP cache, so a search never shows a stale catalog. It blocks for up
    to timeout_s per try, plus the retry waits: call it from a task thread.
    """
    if service not in SERVICES:
        # A programming error, caught before anything is sent rather than after.
        raise ValueError(f"unknown GEGD service {service!r}; one of {sorted(SERVICES)}")
    how = gegd_auth.mode()
    if how == "none":
        raise GegdError("key_not_sent",
                        "The QGIS password store is disabled in this profile, so the "
                        "Maxar/Vantor GEGD Pro key cannot be kept or sent.", service=service)
    hop = QUrl(url)
    wanted = _origin(hop)
    retries = max(0, int(retries))
    attempt = 0
    hops = 0
    while True:
        if feedback is not None and feedback.isCanceled():
            raise Cancelled(service)
        a = _once(hop, service, how, timeout_s, feedback, force_refresh)
        if feedback is not None and feedback.isCanceled():
            raise Cancelled(service)

        if a.status is not None and 300 <= a.status < 400 and a.location is not None:
            target = hop.resolved(a.location)
            if not target.isValid() or _origin(target) != wanted:
                raise GegdError("endpoint_moved",
                                f"GEGD redirected the request to {target.host() or 'another address'}. "
                                "The redirect was not followed: the key is never sent there.",
                                status=a.status, service=service)
            hops += 1
            if hops > MAX_REDIRECTS:
                raise GegdError("network", detail=f"more than {MAX_REDIRECTS} redirects",
                                status=a.status, service=service)
            hop = target
            continue

        wait = None
        if a.status is None:
            if a.qerr not in _TRANSIENT and not _timed_out(a):
                raise GegdError("network", detail=a.message or "no answer", service=service)
            wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
        elif a.status in RETRY_STATUS:
            asked = _retry_after(a.headers.get("retry-after"))
            if asked is None or asked <= RETRY_AFTER_MAX_S:
                wait = max(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)], asked or 0)

        if wait is not None and attempt < retries:
            _sleep(wait, feedback, service)
            attempt += 1
            continue

        tries = f"{attempt + 1} {'try' if attempt == 0 else 'tries'}"
        if a.status is None:
            detail = (f"no answer within {timeout_s:g} s ({tries})" if _timed_out(a) and timeout_s
                      else f"{a.message or 'no answer'} ({tries})")
            raise GegdError("network", detail=detail, service=service)

        # Belt and braces: with manual redirects the answer always comes from the
        # address asked, and that is always on the starting origin.
        if a.url is not None and a.url.isValid() and _origin(a.url) != wanted:
            raise GegdError("endpoint_moved",
                            f"GEGD answered from {a.url.host()}. The key is never sent "
                            "there, so that answer was not used.",
                            status=a.status, service=service)

        err = classify_http(service, a.status, a.body, a.ctype)
        if err is None and not 200 <= a.status < 300:
            err = GegdError("network", detail=f"unexpected HTTP {a.status}",
                            status=a.status, service=service)
        if err is not None:
            raise err
        return Reply(a.status, a.ctype, a.body, None)
