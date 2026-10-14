"""The Planet API key goes in the QGIS password store, and asking for it never nags.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_key_in_password_store.py

Until 2026-10-14 the key sat in plain text in QGIS3.ini. With a QGIS master password
it is now an auth config in the encrypted auth DB (planet_keystore.py); without one
it stays in QgsSettings, because asking would create a master password the user
never chose. The cost of the store is the master-password prompt, so most of this
test is about WHEN it may appear: never at startup, once at first use, never again
after a cancel until Log in, never for Log out. A fake QgsCredentials answers and
counts every prompt.

Also pinned: an old plain-text key is moved into the store; a locked store at sign-in
keeps the key for this session only instead of writing plain text; a crafted
authcfg=planetk request gets nothing; the key survives a master-password reset; and
a store erased under a stale marker does not create a master password.

Runs in a throwaway QGIS profile (QGIS_CUSTOM_CONFIG_PATH) with QGIS's password
helper switched off before anything else, so the real profile, auth DB and Keychain
are never touched. The email, password, key and master passwords are synthetic, and
nothing touches the network.
"""
import base64, json, os, shutil, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)
sys.path.insert(0, ROOT)

# A throwaway profile, set BEFORE QgsApplication exists: QGIS reads this once to place
# QgsSettings and the auth DB, so the real QGIS3.ini and qgis-auth.db are never opened.
TMP = tempfile.mkdtemp(prefix="planet_vault_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = TMP

from qgis.core import QgsApplication, QgsCredentials, QgsSettings
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject, QByteArray, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

am = QgsApplication.authManager()
# The password helper keeps the master password in the macOS Keychain under one
# entry for every profile: off, so nothing here can read or overwrite the real one.
am.setPasswordHelperEnabled(False)
_real = os.path.realpath(TMP)
if (am.passwordHelperEnabled()
        or not os.path.realpath(QgsSettings().fileName()).startswith(_real)
        or not os.path.realpath(am.authenticationDatabasePath()).startswith(_real)):
    print("not in the throwaway profile (or the password helper is on); refusing to run")
    sys.exit(2)

OK = FAIL = 0


def check(msg, cond):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


class FakeCreds(QgsCredentials):
    """QGIS's master-password prompt: counts each one and answers `answer`
    (None = the user cancels)."""
    def __init__(s):
        super().__init__()
        s.asks = 0
        s.answer = None
        s.setInstance(s)
    def request(s, realm, username, password, message=""):
        return False, username, password
    def requestMasterPassword(s, password, stored=False):
        s.asks += 1
        return s.answer is not None, s.answer or ""
creds = FakeCreds()


class FakeBar(QObject):
    def pushMessage(s, *a, **k): pass
    def pushInfo(s, *a): pass
    def pushWarning(s, *a): pass
    def pushCritical(s, *a): pass
class FakeCanvas(QWidget):
    def mapSettings(s):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()
    def mapTool(s): return None
    def setMapTool(s, t): pass
    def unsetMapTool(s, t): pass
    def setExtent(s, *a): pass
    def refresh(s): pass
class FakeIface(QObject):
    def __init__(s):
        super().__init__(); s._bar = FakeBar(); s._c = FakeCanvas(); s._w = QMainWindow()
    def messageBar(s): return s._bar
    def mapCanvas(s): return s._c
    def mainWindow(s): return s._w
    def addDockWidget(s, *a): pass
    def removeDockWidget(s, *a): pass
    def activeLayer(s): return None


# ---- the network: the login POST, answered by hand ----
class FakeReply(QObject):
    finished = pyqtSignal()
    status = 200
    body = b""
    def attribute(s, attr):
        return s.status if attr == QNetworkRequest.HttpStatusCodeAttribute else None
    def error(s): return QNetworkReply.NoError
    def readAll(s): return QByteArray(s.body)
    def abort(s): pass


posts = []
class FakeNAM:
    @staticmethod
    def instance(): return FakeNAM
    @staticmethod
    def post(req, body):
        reply = FakeReply()
        posts.append(reply)
        return reply


USER = "slide.watcher@example.org"
PW = "Synthetic-Passw0rd-" + "a1b2c3d4e5"            # synthetic, never a real one
KEY = "PLAK" + "5ca1ab1e0ddba11c0ffee0dd5eedbed5"     # synthetic, Planet-shaped
ENV_KEY = "PLAK" + "0123456789abcdef0123456789abcdef"  # a stray ambient key
MASTER = "synthetic-master-" + "9f8e7d"
MASTER2 = "synthetic-master-" + "6c5b4a"
THUMB = "https://example.org/thumb.png"


def first(result):
    """Auth-manager calls with an out-parameter return (bool, …) in PyQGIS."""
    return bool(result[0] if isinstance(result, tuple) else result)


def jwt_for(key):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return json.dumps({"token": f"{b64({'alg': 'HS512'})}.{b64({'api_key': key})}.sig"})


def profile_files_holding(secret):
    """Files anywhere in the throwaway profile that hold `secret`, raw or base64."""
    QgsSettings().sync()
    needles = (secret.encode(), base64.b64encode(secret.encode()))
    hits = []
    for d, _, files in os.walk(TMP):
        for f in files:
            with open(os.path.join(d, f), "rb") as fh:
                data = fh.read()
            if any(n in data for n in needles):
                hits.append(os.path.relpath(os.path.join(d, f), TMP))
    return hits


def sign_in(tab):
    tab.user_edit.setText(USER)
    tab.pass_edit.setText(PW)
    n = len(posts)
    tab._planet_login()
    if len(posts) != n + 1:
        return False
    posts[-1].body = jwt_for(KEY).encode()
    posts[-1].finished.emit()
    return True


from landslide_groundtruth import planet_tab as pt
from landslide_groundtruth import planet_keystore as pk
from landslide_groundtruth.dock import LandslideDock
pt.QgsNetworkAccessManager = FakeNAM
writes = [0]
_write = pk._write_vault
def counting_write(key):
    writes[0] += 1
    return _write(key)
pk._write_vault = counting_write

S = QgsSettings
in_store = lambda: pk.CFG_ID in (am.configIds() or [])
plain = lambda: S().value(pk.S_PLAIN, "", type=str)
docks = []


def new_session(answer=None, env=None):
    """A fresh QGIS session: store locked again, no cancel remembered, a new dock."""
    for d in docks:
        d.teardown()
    docks.clear()
    am.clearMasterPassword()
    pk.reset_refusal()
    creds.answer = answer
    creds.asks = 0
    os.environ.pop("PL_API_KEY", None)
    if env:
        os.environ["PL_API_KEY"] = env
    d = LandslideDock(FakeIface())
    docks.append(d)
    return d, d.planet_tab


print("\n=== no master password: the key stays in QgsSettings, nothing is created ===")
check("(the throwaway auth DB starts with no master password)",
      not am.masterPasswordHashInDatabase())
dock, tab = new_session()
check("building the dock asked for no master password", creds.asks == 0)
check("signed in", sign_in(tab) and tab._auth_source() == "account")
check("the key went to QgsSettings", plain() == KEY and not pk.in_vault(S()))
check("no master password was created to hold it",
      not am.masterPasswordHashInDatabase() and creds.asks == 0)
check("the status line says it is plain text and how to change that",
      "plain text" in tab.login_status.text() and "master password" in tab.login_status.text())

print("\n=== the user sets a master password: the old plain key moves into the store ===")
check("(master password set, as in Settings ▸ Options ▸ Authentication)",
      am.setMasterPassword(MASTER, True))
dock, tab = new_session(answer=MASTER)
check("startup asked for no master password", creds.asks == 0)
check("the panel shows signed in without reading the key",
      tab._auth_source() == "account" and tab.login_box.isCollapsed()
      and tab.logout_btn.isEnabled() and tab.key_edit.text() == "")
check("the first request gets the key", tab._api_key() == KEY)
check("... after exactly one master-password prompt", creds.asks == 1)
check("the key is now in the store", in_store() and pk.in_vault(S()))
check("... and gone from QgsSettings", plain() == "")
hits = profile_files_holding(KEY)
check(f"no file in the profile holds the key in the clear ({hits})", hits == [])
check("a second request reads the field, not the store", tab._api_key() == KEY and creds.asks == 1)

print("\n=== the next session: one prompt, and the account key beats PL_API_KEY ===")
dock, tab = new_session(answer=MASTER, env=ENV_KEY)
check("startup asked for no master password", creds.asks == 0)
check("signed in from the account", tab._auth_source() == "account")
check("startup did not read the store to export the key",
      os.environ.get("PL_API_KEY") == ENV_KEY)
url = dock._auth_thumb_url(THUMB, "PlanetScope")
check("the main tab's thumbnail auth gets the account key, not the env key",
      ("api_key=" + KEY) in url and ENV_KEY not in url)
check("... after one prompt", creds.asks == 1)
n_writes = writes[0]
tab._apply_key_to_env(tab._api_key())
check("a search exports the account key to PL_API_KEY", os.environ.get("PL_API_KEY") == KEY)
check("... without another prompt or rewriting the store",
      creds.asks == 1 and writes[0] == n_writes)

print("\n=== a cancelled prompt is not repeated until Log in ===")
dock, tab = new_session(answer=None, env=ENV_KEY)
urls = [dock._auth_thumb_url(THUMB, "PlanetScope") for _ in range(5)]
check("five gallery thumbnails asked once between them", creds.asks == 1)
check("... and fell back to the env key", all(("api_key=" + ENV_KEY) in u for u in urls))
check("the status line says the store stayed locked",
      "stayed locked" in tab.login_status.text())
check("still shown as signed in: the key is saved, just not unlocked",
      tab._auth_source() == "account" and in_store())
creds.answer = MASTER
check("pressing Log in signs in", sign_in(tab) and tab._api_key() == KEY)
check("... asking for the master password again, once", creds.asks == 2)
check("... and saving to the store", "password store" in tab.login_status.text()
      and pk.in_vault(S()) and plain() == "")

print("\n=== the store stays locked at sign-in: this session only, never plain text ===")
dock, tab = new_session(answer=None)
check("signed in", sign_in(tab) and tab._api_key() == KEY and creds.asks == 1)
check("the status line says this session only", "this session only" in tab.login_status.text())
check("nothing saved: not in QgsSettings", plain() == "" and not pk.has_key(S()))
check("... and the older saved key was removed", not in_store())
hits = profile_files_holding(KEY)
check(f"no file in the profile holds the key ({hits})", hits == [])
dock, tab = new_session(answer=MASTER)
check("the next session is signed out, not someone's old key",
      tab._auth_source() == "" and tab._api_key() == "" and creds.asks == 0)

print("\n=== Log out with the store locked never prompts ===")
creds.answer = MASTER
check("(signed in again)", sign_in(tab) and in_store())
dock, tab = new_session(answer=MASTER)
tab._planet_logout()
check("Log out asked for no master password", creds.asks == 0)
check("... and removed the key everywhere",
      not in_store() and not pk.has_key(S()) and tab._auth_source() == "")

print("\n=== a crafted project naming authcfg=planetk gets nothing ===")
check("(signed in again)", sign_in(tab) and in_store())
req = QNetworkRequest(QUrl("https://evil.example/tiles/1/2/3"))
check("QGIS refuses to apply the config to a request",
      not first(am.updateNetworkRequest(req, pk.CFG_ID)))
check("... and adds no header or url parameter",
      list(req.rawHeaderList()) == [] and req.url().toString() == "https://evil.example/tiles/1/2/3")

print("\n=== the key survives a master-password reset ===")
check("(master password reset)", first(am.resetMasterPassword(MASTER2, MASTER, False)))
dock, tab = new_session(answer=MASTER2)
check("the next session still reads the key", tab._api_key() == KEY and creds.asks == 1)

print("\n=== a store erased under a stale marker does not create a master password ===")
check("(auth DB erased)", first(am.eraseAuthenticationDatabase(False)))
dock, tab = new_session(answer=MASTER)
check("(the marker still says the store)", pk.in_vault(S()))
check("the first request finds no key", tab._api_key() == "")
check("... without a prompt or a new master password",
      creds.asks == 0 and not am.masterPasswordHashInDatabase())
check("the status line says to log in again", "no longer in the QGIS password store"
      in tab.login_status.text())
check("the stale marker is gone, so the panel reads signed out",
      not pk.has_key(S()) and not tab.logout_btn.isEnabled())

for d in docks:
    d.teardown()
os.environ.pop("PL_API_KEY", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
