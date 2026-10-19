"""The Maxar/Vantor GEGD Pro key: where it lives, and the one host it may reach.

GEGD Pro authenticates every OGC request with a header, ``maxar-api-key: <KEY>``
(verified live on WFS, WMS, WMTS, WCS and the seamlines service). The query form
``?maxar_api_key=`` also works, but the server copies it into every capabilities
link and WFS "next" link, so nothing here ever uses it.

Storage
  The key lives in the QGIS password store (the auth DB, unlocked by the master
  password or the macOS Keychain) as an **APIHeader** auth config with the fixed
  id ``gegdpro``. QGIS adds the header itself wherever a request or a layer names
  ``authcfg=gegdpro``: QgsBlockingNetworkRequest on the GUI thread and inside a
  QgsTask, XYZ tiles, WMS GetCapabilities/GetMap and WMTS tiles. A saved ``.qgz``
  therefore holds only ``authcfg=gegdpro``, never the key, and a teammate who
  opens the project renders it with their own key under the same id. The id is
  seven characters on purpose: QGIS matches ``authcfg=`` against a 7-character
  pattern.

Why the HostGuard is not optional
  An auth config sends its header to ANY host a layer names. A crafted or
  hand-edited ``.qgz`` that points ``authcfg=gegdpro`` at another server would
  receive the key; in the M0 check a local test server collected it from 15
  different QGIS request paths. The guard is a request preprocessor, installed
  when the plugin loads (before any project can open), that removes the header
  from every request unless

    * the scheme is ``https`` (``http://pro.gegd.com`` would send it in clear),
    * the host is exactly ``pro.gegd.com`` (QUrl lower-cases it; a trailing dot,
      ``pro.gegd.com.evil.example`` or ``pro.gegd.com@evil.example`` all fail), and
    * the port is 443 or unset.

  Three details were each measured, not assumed:

    * The header is removed with a NULL ``QByteArray()``. An empty-but-non-null
      value still sends an empty ``maxar-api-key:`` line.
    * Allowed requests get ``RedirectPolicyAttribute = ManualRedirectPolicy``.
      QgsFileDownloader lets Qt follow a redirect internally, without running the
      preprocessors again, and carried the real key to a second host in a local
      test. Manual policy stops Qt doing that; QgsBlockingNetworkRequest and the
      XYZ handler follow redirects themselves, through the preprocessors, so the
      off-host hop arrives stripped. (SameOriginRedirectPolicy is not a substitute:
      it turns every redirect into a QgsBlockingNetworkRequest error.) The tab's
      own requests (gegd_net) go further: they follow a same-origin 3xx only, and
      send nothing at all to any other address.
    * Requests are counted on ``requestAboutToBeCreated``/``finished``, never in
      the preprocessor: the XYZ path runs the preprocessors TWICE per tile (once
      before the auth header is added), so a preprocessor count is double.

Side effect on other plugins
  The rule is about the header NAME, because the guard holds no key (it runs
  from plugin load, before anything may prompt for the master password). Any
  other plugin's ``maxar-api-key`` header to a host other than pro.gegd.com is
  stripped too; the official Vantor Hub plugin sends one to api.maxar.com for
  its browse download. A message-log line names each host it was withheld from.

What the guard cannot hide
  QGIS's Network Logger (F12) is fed by ``requestAboutToBeCreated``, which fires
  after every preprocessor, and it showed the full header on 30/30 GEGD requests.
  Nothing in a plugin can change that. Do not share a logger export, "Copy as
  cURL" or "Save Network Log" while GEGD layers are loaded.

Unloading the plugin
  The auth config 'gegdpro' stays in the password store when the plugin is
  disabled, and QGIS would go on adding the key to any layer that names it. So
  plugin.unload calls ``release_host_guard()``, which stops the usage counting
  and drops the fallback injector's in-memory key but leaves the strip-only
  preprocessor installed for the rest of the QGIS session (QGIS holds a strong
  reference to it, measured). Its id is kept as a QgsApplication property, so the
  next ``install_host_guard()`` (a plugin reload) replaces it instead of stacking a
  second one. A LATER session without the plugin has no guard at all: press
  Forget key before disabling or uninstalling the plugin.

Fallback
  A QGIS without the APIHeader method gets ``HeaderInjector``: a preprocessor that
  adds the header for the allowed host only. Layer sources are then built without
  ``authcfg`` (a missing auth config fails a request before any preprocessor runs),
  and the injector reads the key once, on the GUI thread, in ``ensure_ready``.

Threading
  Nothing here touches ``QgsApplication.authManager()`` at import time, so loading
  the plugin or building the dock never prompts for the master password.
  ``ensure_ready()`` runs on the GUI thread before every task launch and layer
  creation; any password prompt happens there. The preprocessors run on every
  network thread and touch nothing but the request they are handed.
"""
import threading
from datetime import datetime, timedelta, timezone

from qgis.core import (Qgis, QgsApplication, QgsAuthMethodConfig, QgsMessageLog,
                       QgsNetworkAccessManager, QgsNetworkReplyContent,
                       QgsNetworkRequestParameters, QgsSettings)
from qgis.PyQt.QtCore import QByteArray, QThread, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .gegd_api import ALLOWED_HOST

AUTHCFG_ID = "gegdpro"
KEY_HEADER = "maxar-api-key"
AUTH_METHOD = "APIHeader"
CONFIG_NAME = "GEGD Pro (Landslide plugin)"
LOG_TAG = "Landslide GEGD"

# The one place the key may go. (scheme, host, port); tests inject a local
# stand-in through HostGuard(allowed=...) / install_host_guard(allowed=...).
DEFAULT_ALLOWED = ("https", ALLOWED_HOST, 443)
_DEFAULT_PORT = {"https": 443, "http": 80}
_HDR = KEY_HEADER.encode("ascii")

# QgsSettings keys. Non-secrets only: the key itself is never written here.
S_KEY_SET = "landslide/gegd_key_set_utc"
S_HOST = "landslide/gegd_host"
S_USAGE = "landslide/gegd_usage"
USAGE_KEEP_DAYS = 60            # older per-day usage rows are pruned on flush
USAGE_FLUSH_MS = 30_000         # counts reach QgsSettings at most this late

# Probed once, on the GUI thread when possible (see _probe).
_STATE = {"apiheader": None, "disabled": None}


# --------------------------------------------------------------------------
# host rule
# --------------------------------------------------------------------------
def allowed_for(host_url):
    """'https://pro.gegd.com' -> ('https', 'pro.gegd.com', 443); None for anything
    that is not https with a host, so a cleartext host can never be configured."""
    u = QUrl(host_url or "")
    scheme = u.scheme().lower()
    if scheme != "https" or not u.host():
        return None
    return (scheme, u.host().lower(), u.port(443))


def configured_allowed():
    """The allowed (scheme, host, port) for this profile: https://pro.gegd.com:443.

    Deliberately NOT read from the ``landslide/gegd_host`` setting yet. Nothing in
    the plugin writes it, and the host field with its confirmation dialog (plan
    §3, M5) does not exist, so honouring a value someone put in the profile (or a
    deployed qgis_global_settings.ini) would only re-aim the key at that host and
    strip it from pro.gegd.com. When the dialog lands, it must write a separate
    confirmation stamp, accept only hosts under gegd.com, and re-aim the tab's
    host at the same time."""
    return DEFAULT_ALLOWED


def url_allowed(url, allowed=DEFAULT_ALLOWED):
    """True only for scheme == allowed scheme, host == allowed host (exactly, after
    QUrl's lower-casing) and the effective port == allowed port. An unset port
    counts as the scheme's default, so https://pro.gegd.com and
    https://pro.gegd.com:443 both pass and :8443 does not."""
    u = url if isinstance(url, QUrl) else QUrl(url)
    scheme, host, port = allowed
    s = u.scheme().lower()
    return (s == scheme and u.host().lower() == host
            and u.port(_DEFAULT_PORT.get(s, -1)) == port)


# --------------------------------------------------------------------------
# usage meter: per UTC day, in QgsSettings, counted by THIS QGIS
# --------------------------------------------------------------------------
# GEGD's own meter needs a full account login, so API keys cannot see quota. This
# is the next best thing: what this QGIS asked GEGD for today. Counts accumulate
# here as deltas and are ADDED to the stored day on flush, so two QGIS windows
# sharing a profile add up instead of overwriting each other.
_USAGE_LOCK = threading.Lock()
_PENDING = {}                   # 'YYYY-MM-DD' -> [requests, bytes] not yet stored


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_add(n_requests, n_bytes):
    day = _today()
    with _USAGE_LOCK:
        p = _PENDING.setdefault(day, [0, 0])
        p[0] += int(n_requests)
        p[1] += int(n_bytes)


def _stored(settings, day):
    def num(key):
        try:
            return int(settings.value(f"{S_USAGE}/{day}/{key}", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return num("requests"), num("bytes")


def flush_usage():
    """Write pending counts to QgsSettings and prune old days. GUI thread."""
    with _USAGE_LOCK:
        pending = dict(_PENDING)
        _PENDING.clear()
    s = QgsSettings()
    for day, (n, b) in pending.items():
        n0, b0 = _stored(s, day)
        s.setValue(f"{S_USAGE}/{day}/requests", n0 + n)
        s.setValue(f"{S_USAGE}/{day}/bytes", b0 + b)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=USAGE_KEEP_DAYS)).strftime("%Y-%m-%d")
    s.beginGroup(S_USAGE)
    old = [d for d in s.childGroups() if d < cutoff]
    s.endGroup()
    for d in old:
        s.remove(f"{S_USAGE}/{d}")


def usage_today():
    """(requests sent to the GEGD host, bytes received from it) by this QGIS today
    (UTC day), stored plus not yet flushed. Not GEGD's own meter, which API keys
    cannot read."""
    day = _today()
    n0, b0 = _stored(QgsSettings(), day)
    with _USAGE_LOCK:
        n, b = _PENDING.get(day, (0, 0))
    return n0 + n, b0 + b


# --------------------------------------------------------------------------
# HostGuard
# --------------------------------------------------------------------------
class HostGuard:
    """Strip the GEGD key from every request that is not https://pro.gegd.com:443,
    and count what does go there. See the module docstring for why each rule
    exists. One instance is installed for the life of the plugin
    (install_host_guard / remove_host_guard); the tab's teardown leaves it alone.
    """

    def __init__(self, allowed=None):
        self.allowed = tuple(allowed) if allowed else DEFAULT_ALLOWED
        self.stripped = 0           # diagnostics only, never usage: the XYZ path
        self.kept = 0               # runs the preprocessors twice per tile
        self._lock = threading.Lock()
        self._fn = self.preprocess  # hold the bound method QGIS keeps calling
        self._pre_id = None
        self._connected = False
        self._timer = None
        self._open = {}             # requestId -> bytes seen so far, GEGD only
        self._warned = set()        # hosts already logged as refused

    # ---- the preprocessor: every network thread, the request only ----------
    def preprocess(self, request):
        try:
            if not request.hasRawHeader(_HDR):
                return
            if url_allowed(request.url(), self.allowed):
                # Qt must never carry the key across a redirect by itself.
                request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                                     QNetworkRequest.ManualRedirectPolicy)
                with self._lock:
                    self.kept += 1
                return
            # NULL QByteArray removes the header; b"" would send it empty.
            request.setRawHeader(_HDR, QByteArray())
            host = request.url().host() or "?"
            with self._lock:
                self.stripped += 1
                first = host not in self._warned
                self._warned.add(host)
            if first:
                QgsMessageLog.logMessage(
                    f"Maxar/Vantor GEGD Pro key withheld from a request to {host}: "
                    f"it is only ever sent to {self.allowed[0]}://{self.allowed[1]}.",
                    LOG_TAG, Qgis.Warning)
        except Exception:
            # Fail closed: if anything above broke, the key does not leave.
            try:
                request.setRawHeader(_HDR, QByteArray())
            except Exception:
                pass

    # ---- usage: main-instance signals, delivered on the GUI thread ---------
    def _on_request(self, params):
        try:
            if url_allowed(params.request().url(), self.allowed):
                with self._lock:
                    if len(self._open) > 5000:     # 'finished' went missing; bytes
                        self._open.clear()         # are approximate anyway
                    self._open[params.requestId()] = 0
                _usage_add(1, 0)
        except Exception:
            pass

    def _on_progress(self, request_id, received, _total):
        try:
            with self._lock:
                if request_id in self._open and received > self._open[request_id]:
                    self._open[request_id] = int(received)
        except Exception:
            pass

    def _on_finished(self, content):
        try:
            with self._lock:
                seen = self._open.pop(content.requestId(), None)
            if seen is None:
                return
            try:
                length = int(bytes(content.rawHeader(b"Content-Length")) or 0)
            except ValueError:
                length = 0
            _usage_add(0, max(seen, length))
        except Exception:
            pass

    # ---- lifecycle --------------------------------------------------------
    @property
    def installed(self):
        return self._pre_id is not None

    def install(self):
        """Idempotent. Call on the GUI thread (the flush timer lives there)."""
        if self._pre_id is None:
            self._pre_id = QgsNetworkAccessManager.setRequestPreprocessor(self._fn)
        if not self._connected:
            nam = QgsNetworkAccessManager.instance()
            nam.requestAboutToBeCreated[QgsNetworkRequestParameters].connect(self._on_request)
            nam.finished[QgsNetworkReplyContent].connect(self._on_finished)
            try:
                nam.downloadProgress.connect(self._on_progress)
            except (AttributeError, TypeError):
                pass
            self._connected = True
        app = QgsApplication.instance()
        if self._timer is None and app is not None and QThread.currentThread() == app.thread():
            self._timer = QTimer()
            self._timer.setInterval(USAGE_FLUSH_MS)
            self._timer.timeout.connect(flush_usage)
            self._timer.start()
            app.aboutToQuit.connect(flush_usage)
        return self

    def remove(self):
        """Idempotent. Flushes the usage counts it was holding."""
        if self._pre_id is not None:
            QgsNetworkAccessManager.removeRequestPreprocessor(self._pre_id)
            self._pre_id = None
        if self._connected:
            nam = QgsNetworkAccessManager.instance()
            for sig, slot in ((nam.requestAboutToBeCreated[QgsNetworkRequestParameters], self._on_request),
                              (nam.finished[QgsNetworkReplyContent], self._on_finished),
                              (getattr(nam, "downloadProgress", None), self._on_progress)):
                try:
                    sig.disconnect(slot)
                except (AttributeError, TypeError, RuntimeError):
                    pass
            self._connected = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
            app = QgsApplication.instance()
            if app is not None:
                try:
                    app.aboutToQuit.disconnect(flush_usage)
                except (TypeError, RuntimeError):
                    pass
            try:
                flush_usage()
            except Exception:
                pass

    def detach(self):
        """Stop counting and flushing, but leave the strip-only preprocessor in
        place (release_host_guard). Idempotent."""
        pre_id = self._pre_id
        self._pre_id = None             # remove() then leaves the preprocessor alone
        try:
            self.remove()
        finally:
            self._pre_id = pre_id


_GUARD = None
# The installed guard's preprocessor id, kept where a reloaded copy of this module
# can find it (a QgsApplication dynamic property survives the reload).
_APP_PROP = "landslide_gegd_guard_id"


def _remember_guard_id(pre_id):
    app = QgsApplication.instance()
    if app is not None:
        app.setProperty(_APP_PROP, pre_id or "")


def _drop_stale_guard():
    """Remove a guard an earlier load of the plugin left installed (see
    release_host_guard), so a reload replaces it instead of stacking."""
    app = QgsApplication.instance()
    stale = app.property(_APP_PROP) if app is not None else None
    if stale and (_GUARD is None or _GUARD._pre_id != stale):
        try:
            QgsNetworkAccessManager.removeRequestPreprocessor(str(stale))
        except Exception:
            pass
    _remember_guard_id("")


def install_host_guard(allowed=None):
    """Install the plugin-wide guard (plugin.initGui). Idempotent: a second call
    re-aims the same instance instead of stacking a second preprocessor, and a
    guard left behind by an earlier load of the plugin is replaced.
    ``allowed`` defaults to configured_allowed(); pass a tuple only in tests."""
    global _GUARD
    target = tuple(allowed) if allowed else configured_allowed()
    if _GUARD is None:
        _drop_stale_guard()
        _GUARD = HostGuard(target)
    _GUARD.allowed = target
    _GUARD.install()
    _remember_guard_id(_GUARD._pre_id)
    if _INJECTOR is not None:
        _INJECTOR.allowed = target
    return _GUARD


def remove_host_guard():
    """Remove the guard completely, and the fallback injector with its in-memory
    key. Safe to call any number of times. For tests and teardown helpers; the
    plugin's unload uses release_host_guard()."""
    global _GUARD
    _remove_injector()
    if _GUARD is not None:
        _GUARD.remove()
        _GUARD = None
    _drop_stale_guard()


def release_host_guard():
    """plugin.unload: stop counting and drop the fallback injector (and the key it
    holds), but KEEP the strip-only preprocessor for the rest of this QGIS session.

    The 'gegdpro' auth config outlives the plugin, so without the guard a project
    layer naming it would send the key to whatever host it points at as soon as
    the plugin is switched off. The next install_host_guard() (a reload) removes
    this one by the id remembered on QgsApplication. Safe to call any number of
    times."""
    global _GUARD
    _remove_injector()
    if _GUARD is not None:
        _GUARD.detach()
        _GUARD = None


def host_guard():
    """The installed guard, or None."""
    return _GUARD


def active_allowed():
    """The (scheme, host, port) the key may currently reach."""
    return _GUARD.allowed if _GUARD is not None else DEFAULT_ALLOWED


# --------------------------------------------------------------------------
# HeaderInjector (fallback when QGIS has no APIHeader auth method)
# --------------------------------------------------------------------------
class HeaderInjector:
    """Add the header to allowed-host requests that do not carry it yet. Only used
    when mode() == 'preprocessor'; holds the key in memory, never logs it."""

    def __init__(self, key, allowed=None):
        self._value = QByteArray(key.encode("ascii"))
        self.allowed = tuple(allowed) if allowed else active_allowed()
        self._fn = self.preprocess
        self._pre_id = None

    def preprocess(self, request):
        try:
            if url_allowed(request.url(), self.allowed) and not request.hasRawHeader(_HDR):
                request.setRawHeader(_HDR, self._value)
                request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                                     QNetworkRequest.ManualRedirectPolicy)
        except Exception:
            pass

    def install(self):
        if self._pre_id is None:
            self._pre_id = QgsNetworkAccessManager.setRequestPreprocessor(self._fn)
        return self

    def remove(self):
        if self._pre_id is not None:
            QgsNetworkAccessManager.removeRequestPreprocessor(self._pre_id)
            self._pre_id = None
        self._value = QByteArray()


_INJECTOR = None


def _install_injector(key):
    global _INJECTOR
    _remove_injector()
    _INJECTOR = HeaderInjector(key).install()


def _remove_injector():
    global _INJECTOR
    if _INJECTOR is not None:
        _INJECTOR.remove()
        _INJECTOR = None


# --------------------------------------------------------------------------
# the auth DB
# --------------------------------------------------------------------------
def _am():
    return QgsApplication.authManager()


def _first(result):
    """storeAuthenticationConfig & co. return (bool, config) in PyQGIS 3.44 and a
    bare bool in older bindings."""
    return bool(result[0] if isinstance(result, tuple) else result)


def _on_gui_thread():
    app = QgsApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def _probe():
    try:
        am = _am()
        _STATE["disabled"] = bool(am.isDisabled())
        _STATE["apiheader"] = (not _STATE["disabled"]
                               and AUTH_METHOD in (am.authMethodsKeys() or []))
    except Exception:
        _STATE["disabled"], _STATE["apiheader"] = True, False


def has_apiheader():
    """True when this QGIS ships the APIHeader auth method (3.44 does).

    The first call (this or mode()) opens the auth manager to list its methods.
    That never asks for the master password, but it is still not for
    GegdTab.__init__ (threading rule 1); ensure_ready() probes it first anyway."""
    if _STATE["apiheader"] is None:
        _probe()
    return bool(_STATE["apiheader"])


def mode():
    """How the key reaches GEGD in this QGIS.

    'authcfg'       the APIHeader auth config adds it (layer sources carry
                    authcfg=gegdpro);
    'preprocessor'  no APIHeader method: HeaderInjector adds it, and layer
                    sources carry no authcfg;
    'none'          the QGIS password store is disabled, so there is nowhere to
                    keep a key and nothing is sent.
    """
    if _STATE["apiheader"] is None:
        _probe()
    if _STATE["disabled"]:
        return "none"
    return "authcfg" if _STATE["apiheader"] else "preprocessor"


def ensure_ready(parent=None):
    """Make the password store usable before a task or a layer needs the key.

    GUI thread only: refuses (False) anywhere else rather than risk a master-
    password dialog from a worker. Unlocks the auth DB, which is where QGIS asks
    for the master password or reads it from the macOS Keychain; installs the
    HostGuard if the plugin has not; and in fallback mode loads the key into the
    HeaderInjector. True when the store is unlocked. It does not say a key is
    stored: check has_key(). ``parent`` is accepted so a caller can pass its
    widget; QGIS shows its own password dialog.
    """
    if not _on_gui_thread():
        QgsMessageLog.logMessage("gegd_auth.ensure_ready() refused off the GUI thread.",
                                 LOG_TAG, Qgis.Warning)
        return False
    if _GUARD is None:              # plugin.initGui normally did this already
        install_host_guard()
    _probe()
    if _STATE["disabled"]:
        return False
    am = _am()
    if not am.masterPasswordIsSet() and not am.setMasterPassword(True):
        return False
    if mode() == "preprocessor":
        key = _load_key()
        if key:
            _install_injector(key)
        else:
            _remove_injector()
    return True


def _load_key():
    """The stored key, decrypted. Only for the fallback injector; never logged."""
    try:
        cfg = QgsAuthMethodConfig()
        if not _first(_am().loadAuthenticationConfig(AUTHCFG_ID, cfg, True)):
            return ""
        return cfg.config(KEY_HEADER) or ""
    except Exception:
        return ""


def store_key(key):
    """Save ``key`` as the APIHeader config 'gegdpro', replacing any earlier one.

    Whitespace around a pasted key is dropped; an empty key, one with spaces or
    control characters inside, or one that is not plain ASCII (an HTTP header
    cannot carry it) raises ValueError, whose text never contains the key. Call
    ensure_ready() first. Returns True on success. Records the save time (UTC) in
    QgsSettings for the key-age warning; the key itself is never written there.

    Call it with no GEGD request of the plugin's in flight (GegdTab._when_idle):
    writing the config while a worker thread loads it can deadlock QGIS's auth
    locks (measured in 3.44 with removeAuthenticationConfig).
    """
    key = (key or "").strip()
    if (not key or not key.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key)):
        raise ValueError("That does not look like a GEGD Pro API key "
                         "(empty, or it contains spaces or unusual characters).")
    cfg = QgsAuthMethodConfig(AUTH_METHOD)
    cfg.setId(AUTHCFG_ID)
    cfg.setName(CONFIG_NAME)
    cfg.setConfig(KEY_HEADER, key)
    am = _am()
    if not _first(am.storeAuthenticationConfig(cfg, True)):
        return False
    # Auth methods cache each config they have applied. In 3.44 an overwrite
    # already refreshes that cache (measured: the next request sent the new key);
    # clearing it explicitly keeps a rotated key from going stale if a later
    # release stops doing so.
    am.clearCachedConfig(AUTHCFG_ID)
    QgsSettings().setValue(S_KEY_SET, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if mode() == "preprocessor":
        _install_injector(key)
    return True


def has_key():
    """True when an APIHeader config 'gegdpro' exists. Reads the config list only:
    no decryption, so no master-password prompt. It does open the auth manager,
    so it is not for GegdTab.__init__ (threading rule 1)."""
    try:
        cfg = (_am().availableAuthMethodConfigs() or {}).get(AUTHCFG_ID)
    except Exception:
        return False
    return cfg is not None and cfg.method() == AUTH_METHOD


def forget_key():
    """Remove the stored key, its cached copy and its save time. Call
    ensure_ready() first. Returns True when nothing is left behind.

    Call it with no GEGD request of the plugin's in flight (GegdTab._when_idle):
    removing the config while a worker thread loads it deadlocked QGIS 3.44 (the
    GUI thread stuck here, the worker in its request)."""
    am = _am()
    ok = True
    if AUTHCFG_ID in (am.configIds() or []):
        ok = bool(am.removeAuthenticationConfig(AUTHCFG_ID))
    am.clearCachedConfig(AUTHCFG_ID)
    QgsSettings().remove(S_KEY_SET)
    _remove_injector()
    return ok and not has_key()


def key_set_utc():
    """When the key was last saved here (aware UTC datetime), or None."""
    raw = QgsSettings().value(S_KEY_SET, "", type=str) or ""
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def key_age_days():
    """Days since the key was saved here (float), or None. Drives the key-age
    warning; GEGD's real expiry is shown only in its web app."""
    t = key_set_utc()
    if t is None:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
