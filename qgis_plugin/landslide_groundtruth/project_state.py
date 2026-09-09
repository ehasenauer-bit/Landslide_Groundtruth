"""Per-project persistence of the dock's inputs (lat/lon, radius, event time,
window, source, per-tab options) inside the QGIS project file itself.

Why the project file and not QgsSettings: QgsSettings is one global store shared
by every project, so the last-opened AOI would overwrite the previous one. Each
landslide lives in its own .qgz, and its coordinates/date/window belong to THAT
project — so they go in the project's own custom-property store via
QgsProject.writeEntry/readEntry, and travel with the file.

Wiring (see LandslideDock):
  * every edit    -> that one entry is written into the in-memory project
  * writeProject  -> save()     full flush as the .qgz is written
  * readProject   -> restore()  a project's own values come back on open
  * cleared       -> restore()  New Project falls back to construction defaults
Entries are written as values change rather than only on save, because the
writeProject signal fires late in QgsProject::write() — an entry added from that
slot can miss the property store being serialised. Writing on change also marks
the project dirty (writeEntry does that for us), so QGIS offers to save on close
exactly as it does after a layer change.

The widget list is discovered by reflection over the dock and its tabs, so a
control added later persists with no extra bookkeeping — with two exclusions:

  SECRET_ATTRS        credentials (Planet key, Earthdata/PC login). A .qgz is
                      shared and often committed; keys stay in QgsSettings.
  GLOBAL_PATH_ATTRS   venv/project/output paths. Machine-specific, so they are
                      saved per project AND kept in QgsSettings — a project
                      without them keeps whatever the global store provided
                      rather than being blanked.
"""
from functools import partial

from qgis.PyQt.QtCore import Qt, QDateTime, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QLineEdit, QSlider,
    QSpinBox,
)
from qgis.core import QgsProject

# QgsProject custom-property scope ("plugin name") all our entries live under.
SCOPE = "landslide_groundtruth"

# Stamped alongside the values so a future key/meaning change can migrate an
# older project instead of silently misreading it. Restore itself is
# version-agnostic: a key that is absent simply falls back to its default.
VERSION_KEY = "state/version"
VERSION = "1"

# never written to the project file — see module docstring
SECRET_ATTRS = {
    "key_edit", "user_edit", "pass_edit", "ed_user_edit", "ed_pass_edit",
}

# saved per project, but a project without them keeps the global QgsSettings value
GLOBAL_PATH_ATTRS = {"python_edit", "project_edit", "out_edit"}

# owner attribute on the dock -> key prefix ("" = the dock itself)
OWNERS = (
    ("", ""),
    ("planet_tab", "planet"),
    ("sar_tab", "sar"),
    ("fusion_tab", "fusion"),
    ("viewer3d_tab", "viewer3d"),
    ("volume_tab", "volume"),
)


class ProjectState:
    """Saves/restores the dock's input widgets in the current QGIS project."""

    def __init__(self, dock):
        self.dock = dock
        self._restoring = False
        self._widgets = self._discover()
        # construction-time values, so a project that never saved plugin state
        # (or File > New) starts from the defaults instead of inheriting the
        # previously open project's AOI.
        self._defaults = {key: _read_widget(w) for key, w in self._widgets}
        for key, w in self._widgets:
            self._track(key, w)

    # ---------- discovery ----------
    def _discover(self):
        """[(key, widget)] for every persistable input on the dock and its tabs."""
        found = []
        for attr, prefix in OWNERS:
            owner = self.dock if not attr else getattr(self.dock, attr, None)
            if owner is None:
                continue
            for name, obj in sorted(vars(owner).items()):
                if name.startswith("_") or name in SECRET_ATTRS:
                    continue
                base = f"{prefix}/{name}" if prefix else name
                # dict-of-widgets (e.g. dock.scene_checks) keyed by its own token
                if isinstance(obj, dict):
                    for sub, w in sorted(obj.items()):
                        if _persistable(w):
                            found.append((f"{base}/{sub}", w))
                elif _persistable(obj):
                    found.append((base, obj))
        return found

    def _track(self, key, widget):
        """Push each change straight into the project, which also marks it dirty
        so the edit can't be silently lost on close."""
        signal = _change_signal(widget)
        if signal is not None:
            signal.connect(partial(self._on_edit, key, widget))

    def _on_edit(self, key, widget, *_args):
        if self._restoring:
            return               # applying stored values, not a user edit
        try:
            project = QgsProject.instance()
            project.writeEntry(SCOPE, VERSION_KEY, VERSION)
            project.writeEntry(SCOPE, key, _read_widget(widget))
        except RuntimeError:
            pass

    # ---------- save / restore ----------
    def save(self, *_args):
        """Write current values into the project (called as it is being saved)."""
        project = QgsProject.instance()
        project.writeEntry(SCOPE, VERSION_KEY, VERSION)
        for key, widget in self._widgets:
            try:
                project.writeEntry(SCOPE, key, _read_widget(widget))
            except RuntimeError:      # widget deleted (dock torn down mid-save)
                pass

    def restore(self, *_args):
        """Apply the project's saved values; keys the project doesn't carry fall
        back to the construction-time defaults (or, for the environment paths,
        to whatever the global QgsSettings already put in the field).

        Deferred by a zero-timer so it runs after QGIS has finished loading the
        project and any pending layout/signal work has settled."""
        QTimer.singleShot(0, self._restore_now)

    def _restore_now(self):
        project = QgsProject.instance()
        self._restoring = True
        try:
            for key, widget in self._widgets:
                value, ok = project.readEntry(SCOPE, key, "")
                if ok and not (value == "" and _is_global_path(key)):
                    # the project's own value wins — including a deliberately
                    # empty field, so a cleared input comes back cleared
                    _write_widget(widget, value)
                elif not _is_global_path(key):
                    # no value in this project (never saved, or a control added
                    # by a later plugin version): start from the default rather
                    # than inheriting the previously open project's AOI
                    _write_widget(widget, self._defaults.get(key, ""))
        except RuntimeError:
            pass
        finally:
            self._restoring = False

    def clear(self):
        """Drop every plugin entry from the current project."""
        project = QgsProject.instance()
        project.removeEntry(SCOPE, VERSION_KEY)
        for key, _ in self._widgets:
            project.removeEntry(SCOPE, key)


def _is_global_path(key):
    """True for the environment-path fields, which must not be reset to a
    construction-time default — the global QgsSettings value stands instead."""
    return key.rsplit("/", 1)[-1] in GLOBAL_PATH_ATTRS


def _persistable(w):
    """True for input widgets whose value is worth keeping. Read-only and
    password fields are skipped: the former are computed output, the latter are
    credentials that must not enter a shared project file."""
    if isinstance(w, QLineEdit):
        return not w.isReadOnly() and w.echoMode() == QLineEdit.Normal
    return isinstance(w, (QDateTimeEdit, QDoubleSpinBox, QSpinBox, QSlider,
                          QCheckBox, QComboBox))


def _change_signal(w):
    # QDateTimeEdit first: it is a QAbstractSpinBox, not a QSpinBox.
    if isinstance(w, QDateTimeEdit):
        return w.dateTimeChanged
    if isinstance(w, (QDoubleSpinBox, QSpinBox, QSlider)):
        return w.valueChanged
    if isinstance(w, QCheckBox):
        return w.toggled
    if isinstance(w, QComboBox):
        return w.currentIndexChanged
    if isinstance(w, QLineEdit):
        # textChanged, not textEdited: the PlanetScope/SAR/DEM tabs fill their
        # lat/lon/date fields with setText from the "copy from other tab" and
        # canvas-pick buttons, and those edits must persist too. The _restoring
        # guard is what keeps a restore from writing back.
        return w.textChanged
    return None


def _read_widget(w):
    """Widget value as a string (project entries are stored as text)."""
    if isinstance(w, QDateTimeEdit):
        return w.dateTime().toString(Qt.ISODate)
    if isinstance(w, (QDoubleSpinBox, QSpinBox, QSlider)):
        return str(w.value())
    if isinstance(w, QCheckBox):
        return "1" if w.isChecked() else "0"
    if isinstance(w, QComboBox):
        return w.currentText()
    return w.text()


def combo_index(texts, value):
    """Index of the saved combo choice in `texts`, or -1 to leave it alone.

    Matching is on the LABEL, not the position, so reordering the items between
    plugin versions cannot silently reinterpret a saved project.

    An exact match wins. Failing that, the label is matched on the part before
    the em-dash — the name of the thing — so that rewording the descriptive tail
    does not lose the choice either. That case is real: nine saved projects
    store "dNDSI — snow index change (recommended)", and moving the
    "(recommended)" marker onto dBright would otherwise drop every one of them
    to whatever happened to be the new default.

    The head match must be UNAMBIGUOUS. If two items share a head, or none do,
    the stored value is treated as unknown and the current choice stands —
    guessing here would be worse than leaving a visible default in place."""
    try:
        return texts.index(value)
    except ValueError:
        pass
    head = value.split("\u2014")[0].strip()
    if not head:
        return -1
    hits = [i for i, t in enumerate(texts)
            if t.split("\u2014")[0].strip() == head]
    return hits[0] if len(hits) == 1 else -1


def _write_widget(w, value):
    """Apply a stored string. Signals are left connected so dependent labels
    (day counts, cloud %) and handlers update exactly as on a user edit; a
    malformed or now-invalid value is ignored rather than raising."""
    if isinstance(w, QDateTimeEdit):
        dt = QDateTime.fromString(value, Qt.ISODate)
        if dt.isValid():
            w.setDateTime(dt)
    elif isinstance(w, QDoubleSpinBox):
        try:
            w.setValue(float(value))
        except (TypeError, ValueError):
            pass
    elif isinstance(w, (QSpinBox, QSlider)):
        try:
            w.setValue(int(round(float(value))))
        except (TypeError, ValueError):
            pass
    elif isinstance(w, QCheckBox):
        w.setChecked(value in ("1", "true", "True"))
    elif isinstance(w, QComboBox):
        idx = combo_index([w.itemText(i) for i in range(w.count())], value)
        if idx >= 0:
            w.setCurrentIndex(idx)
    elif isinstance(w, QLineEdit):
        w.setText(value)
