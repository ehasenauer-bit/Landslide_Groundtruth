"""Signing in to Planet must never write the password to QGIS settings.

Run with tests/run_all.sh, or directly with QGIS's bundled python:
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 tests/test_planet_password_not_saved.py

Until 2026-10-13 a successful login saved 'landslide/planet_pass' next to the API key,
and QgsSettings is a plain-text file (QGIS3.ini): the account password sat there in
the clear. Planet hands back the account's API key for it, and the key alone keeps
the next session signed in, so the password is now dropped after the login POST and
any copy an older build left is removed when the tab loads.

Drives the real _planet_login -> login reply -> _planet_login_done chain with only the
network faked, in a throwaway QGIS profile (QGIS_CUSTOM_CONFIG_PATH), then searches
every file in that profile for the password. A second dock stands in for the next
QGIS session: it must come back signed in, and its account key must still outrank a
PL_API_KEY env var. The email, password and key are synthetic; nothing here touches
the network or the real profile.
"""
import base64, json, os, shutil, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "qgis_plugin")
sys.path.insert(0, PKG)
sys.path.insert(0, ROOT)

# A throwaway profile, set BEFORE QgsApplication exists: QGIS reads this once to place
# QgsSettings and the auth DB, so the real QGIS3.ini is never opened.
TMP = tempfile.mkdtemp(prefix="planet_pass_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = TMP

from qgis.core import QgsApplication, QgsSettings
QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
for _plug in ("/Applications/QGIS-LTR.app/Contents/PlugIns/qgis",
              "/Applications/QGIS.app/Contents/PlugIns/qgis"):
    if os.path.isdir(_plug):
        QgsApplication.setPluginPath(_plug)
        break
app = QgsApplication([], True); app.initQgis()
from qgis.PyQt.QtWidgets import QMainWindow, QWidget
from qgis.PyQt.QtCore import QObject, QByteArray, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

if not os.path.realpath(QgsSettings().fileName()).startswith(os.path.realpath(TMP)):
    print("QgsSettings is not in the throwaway profile; refusing to touch the real one")
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
    def error(s):
        return (QNetworkReply.NoError if s.status == 200
                else QNetworkReply.AuthenticationRequiredError)
    def readAll(s): return QByteArray(s.body)
    def abort(s): pass


posts = []                                   # [(url, body bytes, FakeReply)]
class FakeNAM:
    @staticmethod
    def instance(): return FakeNAM
    @staticmethod
    def post(req, body):
        reply = FakeReply()
        posts.append((req.url().toString(), bytes(body), reply))
        return reply


USER = "slide.watcher@example.org"
PW = "Synthetic-Passw0rd-" + "a1b2c3d4e5"            # synthetic, never a real one
OLD_PW = "Synthetic-OldBuild-" + "f6e5d4c3b2"        # what a pre-fix build left behind
KEY = "PLAK" + "5ca1ab1e0ddba11c0ffee0dd5eedbed5"     # synthetic, Planet-shaped
ENV_KEY = "PLAK" + "0123456789abcdef0123456789abcdef"  # a stray ambient key


def jwt_for(key):
    """A login reply shaped like Planet's: {"token": "<header>.<payload>.<sig>"}."""
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return json.dumps({"token": f"{b64({'alg': 'HS512'})}.{b64({'api_key': key})}.sig"})


def profile_files_holding(secret):
    """Files anywhere in the throwaway profile that hold `secret`, raw or base64."""
    QgsSettings().sync()
    needles = (secret.encode(), base64.b64encode(secret.encode()))
    hits = []
    for d, _, files in os.walk(TMP):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                data = fh.read()
            if any(n in data for n in needles):
                hits.append(os.path.relpath(p, TMP))
    return hits


def sign_in(tab, status=200):
    tab.user_edit.setText(USER)
    tab.pass_edit.setText(PW)
    n = len(posts)
    tab._planet_login()
    if len(posts) != n + 1:
        return None
    url, body, reply = posts[-1]
    reply.status = status
    reply.body = jwt_for(KEY).encode() if status == 200 else b'{"message": "nope"}'
    reply.finished.emit()
    return url, body


from landslide_groundtruth import planet_tab as pt
from landslide_groundtruth.dock import LandslideDock
pt.QgsNetworkAccessManager = FakeNAM
os.environ.pop("PL_API_KEY", None)

print("\n=== a password an older build saved is removed on load ===")
s = QgsSettings()
s.setValue("landslide/planet_pass", OLD_PW)
s.sync()
check("(the seeded password reached the throwaway QGIS.ini)",
      profile_files_holding(OLD_PW) != [])
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
check("the tab dropped 'landslide/planet_pass'",
      not QgsSettings().contains("landslide/planet_pass"))
check(f"no file in the profile holds it any more ({profile_files_holding(OLD_PW)})",
      profile_files_holding(OLD_PW) == [])
check("the password field starts empty", tab.pass_edit.text() == "")

print("\n=== sign in: the real login chain, network faked ===")
sent = sign_in(tab)
check("Log in sent one POST to Planet's login endpoint",
      sent is not None and sent[0] == pt.PLANET_LOGIN_URL)
check("... carrying the email and password (the one place the password goes)",
      sent is not None and json.loads(sent[1]) == {"email": USER, "password": PW})
check("signed in from the account", tab._auth_source() == "account")
check("the recovered key is the effective key", tab._api_key() == KEY)
check("... and exported to PL_API_KEY for the search subprocess",
      os.environ.get("PL_API_KEY") == KEY)
check("the password field is cleared once it has been used", tab.pass_edit.text() == "")
check("'landslide/planet_pass' was not written",
      not QgsSettings().contains("landslide/planet_pass"))
hits = profile_files_holding(PW)
check(f"no file in the profile holds the password ({hits})", hits == [])
check("the email is kept, so a re-login only asks for the password",
      QgsSettings().value("landslide/planet_user", "", type=str) == USER)

print("\n=== the next session: the key alone keeps you signed in ===")
dock.teardown()
os.environ["PL_API_KEY"] = ENV_KEY        # a stray ambient key at the next launch
dock = LandslideDock(FakeIface())
tab = dock.planet_tab
check("it comes back signed in from the account", tab._auth_source() == "account")
check("the account key outranks the PL_API_KEY env var", tab._api_key() == KEY)
check("... and replaces it in this process's environment",
      os.environ.get("PL_API_KEY") == KEY)
check("the thumbnail auth on the main tab uses the account key too",
      ("api_key=" + KEY) in dock._auth_thumb_url("https://example.org/thumb", "PlanetScope"))
check("the password field is empty (nothing to prefill from)", tab.pass_edit.text() == "")

print("\n=== a failed sign-in saves no password either ===")
tab._planet_logout()
check("Log out leaves no stored key", tab._stored_key() == "" and
      not QgsSettings().contains("landslide/planet_api_key"))
os.environ.pop("PL_API_KEY", None)
sent = sign_in(tab, status=401)
check("the 401 reply was handled", sent is not None and tab._login_reply is None)
check("not signed in", tab._auth_source() == "")
hits = profile_files_holding(PW)
check(f"no file in the profile holds the password ({hits})", hits == [])

dock.teardown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
