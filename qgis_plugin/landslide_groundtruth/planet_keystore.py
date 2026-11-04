"""Where the Planet API key is kept between QGIS sessions.

Preferred: the QGIS password store. The key is an auth config with the fixed id
'planetk' in the auth DB, which QGIS encrypts with the master password (and unlocks
from the macOS Keychain when the master password is kept there). Otherwise it is
QgsSettings 'landslide/planet_api_key', plain text in QGIS3.ini, as before. The
non-secret setting 'landslide/planet_key_store' = 'vault' says which, so the tab can
show "signed in" without opening the store.

Each rule below was measured in QGIS 3.44.11 against a throwaway profile:

* A config, not storeAuthSetting(). An encrypted auth *setting* is not re-encrypted
  when the master password is reset (Settings > Options > Authentication): it came
  back "Decryption failed!" and the key was lost. A config survived the reset.
* A method no QGIS has ('LandslideSecret'), not APIHeader or Basic. QGIS applies a
  config's method to any request or layer that names authcfg=<id>, so a crafted
  .qgz could send an APIHeader key to any host it likes. With no such method,
  updateNetworkRequest() refuses and adds nothing. The plugin never needs QGIS to
  attach the key: it reads it and adds it to Planet's requests itself. QGIS's own
  Authentication editor lists the config and copes with the unknown method.
* Only when a master password already exists. setMasterPassword(True) on a database
  without one CREATES one through the same prompt, so asking would push a "set a
  master password" dialog on someone who never chose to have one.
* Never at startup. has_key() reads QgsSettings only. The store is opened, and any
  master-password prompt shown, the first time a request needs the key, and only on
  the GUI thread. A prompt the user cancels is not repeated this session (the
  gallery asks for the key once per thumbnail) until reset_refusal(), which the tab
  calls when they press Log in.
* No silent fall back to plain text. If there is a store but it stays locked when a
  new key is saved, the key is kept for this session only and any saved copy is
  removed, so the next session cannot come back signed in as the wrong account.
* Removing the config needs no unlock, so Log out never prompts.
"""
from qgis.core import QgsApplication, QgsAuthMethodConfig
from qgis.PyQt.QtCore import QThread

CFG_ID = "planetk"                  # QGIS auth config ids are 7 characters
CFG_METHOD = "LandslideSecret"      # deliberately not a method QGIS ships
CFG_NAME = "Planet Labs API key (Landslide plugin)"
CFG_FIELD = "api_key"

S_PLAIN = "landslide/planet_api_key"      # the key itself, when there is no store
S_WHERE = "landslide/planet_key_store"    # 'vault' when the key is in the store
VAULT = "vault"

# This QGIS session only: whether the user cancelled the master-password prompt,
# and why the last load() came back empty ('locked', 'missing' or '').
_SESSION = {"refused": False, "problem": ""}


def _first(result):
    """Auth-manager calls return (bool, config) in PyQGIS 3.44, a bare bool before."""
    return bool(result[0] if isinstance(result, tuple) else result)


def _on_gui_thread():
    app = QgsApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def _plausible(key):
    """A key a header or a url can carry: non-empty ASCII, no spaces or controls."""
    return bool(key) and key.isascii() and not any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key)


def has_key(settings):
    """True when a key is saved in either place. Reads QgsSettings only: no store,
    no prompt, safe at startup."""
    return (settings.value(S_WHERE, "", type=str) == VAULT
            or bool(settings.value(S_PLAIN, "", type=str).strip()))


def in_vault(settings):
    return settings.value(S_WHERE, "", type=str) == VAULT


def problem():
    """Why the last load() found nothing: 'locked' (the prompt was cancelled),
    'missing' (the config is gone from the store), or ''."""
    return _SESSION["problem"]


def reset_refusal():
    """Allow the master-password prompt again (the user pressed Log in)."""
    _SESSION["refused"] = False


def _am():
    return QgsApplication.authManager()


def _store_available():
    """The store exists and already has a master password. Never prompts."""
    try:
        am = _am()
        return not am.isDisabled() and bool(am.masterPasswordHashInDatabase())
    except Exception:
        return False


def _unlock():
    """Unlock the store, prompting at most once a session. GUI thread only, and only
    after _store_available(): on a database without a master password this call
    would create one."""
    if _SESSION["refused"]:
        return False
    am = _am()
    if am.masterPasswordIsSet() or am.setMasterPassword(True):
        return True
    _SESSION["refused"] = True
    return False


def _write_vault(key):
    cfg = QgsAuthMethodConfig(CFG_METHOD)
    cfg.setId(CFG_ID)
    cfg.setName(CFG_NAME)
    cfg.setConfig(CFG_FIELD, key)
    try:
        return _first(_am().storeAuthenticationConfig(cfg, True))
    except Exception:
        return False


def _read_vault():
    cfg = QgsAuthMethodConfig()
    try:
        if not _first(_am().loadAuthenticationConfig(CFG_ID, cfg, True)):
            return ""
    except Exception:
        return ""
    key = (cfg.config(CFG_FIELD) or "").strip()
    return key if _plausible(key) else ""


def _remove_vault():
    try:
        am = _am()
        if CFG_ID in (am.configIds() or []):
            am.removeAuthenticationConfig(CFG_ID)
    except Exception:
        pass


def load(settings):
    """The saved key, or '' (nothing saved, the store stayed locked, or not on the
    GUI thread). May show QGIS's master-password prompt. A key still in plain text
    is moved into the store on the way, when there is one; if that cannot happen
    the plain copy is kept and still returned."""
    _SESSION["problem"] = ""
    if not _on_gui_thread():
        return ""
    if in_vault(settings):
        if not _store_available():      # the auth DB was erased: no key, and
            settings.remove(S_WHERE)    # _unlock() would create a master password
            _SESSION["problem"] = "missing"
            return ""
        if not _unlock():
            _SESSION["problem"] = "locked"
            return ""
        key = _read_vault()
        if not key:                     # removed or unreadable outside the plugin
            settings.remove(S_WHERE)
            _SESSION["problem"] = "missing"
        return key
    key = settings.value(S_PLAIN, "", type=str).strip()
    if key and _store_available() and _unlock() and _write_vault(key):
        settings.setValue(S_WHERE, VAULT)
        settings.remove(S_PLAIN)
    return key


def save(settings, key):
    """Keep `key` for later sessions. Returns where it went: 'vault' (the password
    store), 'settings' (QgsSettings, plain text: there is no master password), or
    'session' (there is a store but it stayed locked, or this is not the GUI thread,
    so nothing was saved and any older saved key was removed)."""
    key = (key or "").strip()
    if not key:
        return ""
    if not _on_gui_thread():
        return "session"
    if _store_available():
        if _unlock() and _write_vault(key):
            settings.setValue(S_WHERE, VAULT)
            settings.remove(S_PLAIN)
            return VAULT
        _remove_vault()
        settings.remove(S_WHERE)
        settings.remove(S_PLAIN)
        return "session"
    settings.remove(S_WHERE)
    settings.setValue(S_PLAIN, key)
    return "settings"


def forget(settings):
    """Remove the saved key from both places. Needs no unlock, so never prompts."""
    _remove_vault()
    settings.remove(S_WHERE)
    settings.remove(S_PLAIN)
