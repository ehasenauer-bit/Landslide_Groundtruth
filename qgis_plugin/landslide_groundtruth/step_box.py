"""The two pieces every tab is built from: a step-tinted box and the ? Help toggle.

**Tinted boxes.** A panel's colour says which step of the work it is (see
theme.STEP_TINTS): setup, inputs, options, results, advanced. `tinted(step, w)`
wraps any widget in a frame of that colour. The frame carries the tint, not the
box, because QgsCollapsibleGroupBox positions its title with its own stylesheet
and a stylesheet set on it replaces that (measured: the title is drawn over the
first row). A collapsed box shrinks its frame to a tinted title bar.

**? Help.** One HelpToggle for the whole dock: every tab's "? Help" button is
the same switch, so the explanatory prose is either on everywhere or off
everywhere, and remembered. Tooltips carry the detail either way, and live
warnings are not help text — they always show.
"""

from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtWidgets import QFrame, QLabel, QToolButton, QVBoxLayout

from . import theme

# The Volume tab had the toggle first, under this key; reading it first keeps the
# choice already made there.
HELP_SETTING = "landslide_groundtruth/show_help"
_OLD_HELP_SETTING = "landslide_groundtruth/volume_show_help"


def _truthy(v):
    return v in (True, 1, "1", "true", "True")


class StepFrame(QFrame):
    """A frame tinted for one workflow step. `step` is a theme.STEPS key."""

    def __init__(self, step, parent=None):
        super().__init__(parent)
        self.step = step
        self.setObjectName("lgt_step_" + step)
        bg, border = theme.step_tint(step)
        # The object-name selector keeps the rule on THIS frame: an unscoped
        # QFrame rule would also tint every QLabel inside (QLabel is a QFrame).
        self.setStyleSheet(
            "QFrame#%s { background: %s; border: 1px solid %s; "
            "border-radius: 4px; }" % (self.objectName(), bg, border))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)

    def add(self, widget, stretch=0):
        self.layout().addWidget(widget, stretch)
        return widget


def tinted(step, *widgets):
    """Wrap one or more widgets, stacked, in a frame tinted for `step`."""
    frame = StepFrame(step)
    for w in widgets:
        frame.add(w)
        # The frame owns the widget, and until the frame is in a layout only
        # Python owns the frame: a caller that keeps just the box (a builder that
        # returns it) would have both deleted under it. This keeps them together.
        w._step_frame = frame
    return frame


def step_box(step, title, collapsed=False):
    """(frame, box): a collapsible box inside its step tint.

    Add `frame` to the layout and fill `box`. The collapsed state is not saved
    between sessions, like every other box on the dock: each tab opens the same
    way every time."""
    box = QgsCollapsibleGroupBox(title)
    box.setSaveCollapsedState(False)
    box.setCollapsed(collapsed)
    box.setToolTip(theme.STEP_NAMES[step])
    return tinted(step, box), box


def advanced_box(title="Advanced"):
    """The one closed box per tab that holds the rarely-changed controls."""
    return step_box("advanced", title, collapsed=True)


class HelpToggle:
    """Shows and hides every registered explanation, from any tab's button."""

    def __init__(self, settings):
        self.settings = settings
        self._labels = []
        self._buttons = []
        v = settings.value(HELP_SETTING, None)
        if v is None:
            v = settings.value(_OLD_HELP_SETTING, False)
        self.on = _truthy(v)

    def button(self):
        """A "? Help" button for a tab's header. All of them move together."""
        btn = QToolButton()
        btn.setText("? Help")
        btn.setCheckable(True)
        btn.setChecked(self.on)
        btn.setToolTip(
            "Show or hide the explanations on every tab. Remembered for next "
            "time. Warnings always show.")
        btn.toggled.connect(self.set_on)
        self._buttons.append(btn)
        return btn

    def label(self, text=""):
        """A grey explanatory label that this toggle shows and hides."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("QLabel { color: palette(mid); }")
        lbl.setVisible(self.on)
        self._labels.append(lbl)
        return lbl

    def adopt(self, label):
        """Put an existing label under the toggle."""
        label.setVisible(self.on)
        self._labels.append(label)
        return label

    def set_on(self, on):
        self.on = bool(on)
        for lbl in self._labels:
            try:
                lbl.setVisible(self.on)
            except RuntimeError:        # label deleted with its tab
                pass
        for btn in self._buttons:
            try:
                if btn.isChecked() != self.on:
                    btn.blockSignals(True)
                    btn.setChecked(self.on)
                    btn.blockSignals(False)
            except RuntimeError:
                pass
        try:
            self.settings.setValue(HELP_SETTING, self.on)
        except Exception:
            pass
