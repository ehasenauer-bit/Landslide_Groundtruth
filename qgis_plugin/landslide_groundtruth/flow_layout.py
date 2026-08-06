"""A wrapping ("flow") layout for button rows in the dock.

The dock is narrow and its font is rescaled with the panel size
(LandslideDock._rescale_fonts), so a row of buttons in a plain QHBoxLayout has no
width that works everywhere: the dock's inner widget carries an explicit
setMinimumWidth(360), which overrides what the layout says it needs, so the row
gets squeezed below the buttons' own minimum widths and their labels are clipped
mid-word.

FlowLayout instead gives every child exactly its sizeHint width and wraps to a
new line when the next one wouldn't fit — so labels stay whole at any dock width,
font scale or screen DPI, and the row simply grows taller when it must. It's the
usual Qt flow-layout implementation (heightForWidth-driven), with a FlowRow
widget wrapper that reports the wrapped height to its parent layout.
"""

from qgis.PyQt.QtCore import Qt, QPoint, QRect, QSize
from qgis.PyQt.QtWidgets import QLayout, QSizePolicy, QWidget


class FlowLayout(QLayout):
    """Lay items out left-to-right, wrapping onto further lines as needed."""

    def __init__(self, parent=None, margin=0, hspacing=6, vspacing=6):
        super().__init__(parent)
        self._items = []
        self._hspacing = hspacing
        self._vspacing = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    # --- QLayout plumbing (Qt owns the items once added) ---
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)      # never asks for extra space, only wraps

    # --- height is a function of the width we're given ---
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        """Only as wide as the widest single item: anything narrower would clip a
        label, anything wider would force a horizontal scrollbar on the dock."""
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(QSize(item.sizeHint().width(),
                                         item.sizeHint().height()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _do_layout(self, rect, test_only):
        """Place items (or, when test_only, just measure) and return the total
        height used, including margins."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._hspacing
            if next_x - self._hspacing > area.right() + 1 and line_height > 0:
                x = area.x()                       # wrap onto the next line
                y += line_height + self._vspacing
                next_x = x + hint.width() + self._hspacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class FlowRow(QWidget):
    """A widget hosting a FlowLayout, for dropping into a QVBoxLayout.

    A bare nested layout relies on the parent honouring heightForWidth all the way
    up (through the tab widget and the dock's scroll area); reporting the wrapped
    height from sizeHint() as well means the row always gets the vertical space it
    needs, so a second line of buttons can't be cut off.
    """

    def __init__(self, parent=None, hspacing=6, vspacing=6):
        super().__init__(parent)
        self.flow = FlowLayout(self, 0, hspacing, vspacing)
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def addWidget(self, widget):
        self.flow.addWidget(widget)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.flow.heightForWidth(width)

    def sizeHint(self):
        width = self.width() if self.width() > 0 else self.flow.minimumSize().width()
        return QSize(self.flow.minimumSize().width(), self.heightForWidth(width))

    def minimumSizeHint(self):
        return self.sizeHint()

    def resizeEvent(self, event):
        """Re-advertise the height once wrapping changes, so the parent layout
        re-reserves space when the dock is dragged narrower or wider."""
        super().resizeEvent(event)
        self.updateGeometry()
