"""Candidate-scene tables only as tall as their rows.

A candidate table used to take all the spare height of its tab, so a search
that found a few scenes left a page of blank white table under them.
fit_rows() caps the table at its rows: header + every row + frame, plus the
horizontal scrollbar while the columns overflow. It may shrink to `min_height`
before it scrolls. Many rows still fill the room the tab has and scroll inside
it, as before.

For the space the table gives up to land below the tab instead of inside the
results box (measured: 410 px of gap between the last row and the gallery):
  - every widget between the table and the tab's layout must be capped at its
    layout's maximum (`containers`): a plain QWidget/QFrame does NOT inherit
    its layout's maximum height, so it takes the space and its layout spreads
    it out as gaps between the rows;
  - the rows beside the table must not grow: a heading QLabel is Preferred by
    default and would take it (bound_height);
  - the tab's layout needs a trailing addStretch() with stretch 0, which only
    gets the space once the stretched results box has hit its cap.
"""
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import QLayout, QSizePolicy, QStyle


class _RowFit(QObject):
    _EVENTS = (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.FontChange,
               QEvent.Type.StyleChange)

    def __init__(self, table, min_height):
        super().__init__(table)
        self._t = table
        self._min = min_height
        m = table.model()
        for sig in (m.rowsInserted, m.rowsRemoved, m.modelReset, m.layoutChanged):
            sig.connect(self.refit)
        table.verticalHeader().sectionResized.connect(self.refit)
        # the columns overflowing (or not) shows/hides the horizontal scrollbar
        table.horizontalScrollBar().rangeChanged.connect(self.refit)
        table.installEventFilter(self)
        self.refit()

    def eventFilter(self, obj, ev):
        if ev.type() in self._EVENTS:
            self.refit()
        return False

    def _gone(self):
        # signals still arrive while the dock is torn down
        return sip.isdeleted(self._t)

    def content_height(self):
        t = self._t
        rows = t.verticalHeader().length()     # hidden rows count 0
        if not rows:
            # an empty table keeps one blank row, so it still reads as a table
            rows = t.verticalHeader().defaultSectionSize()
        h = 2 * t.frameWidth() + rows
        head = t.horizontalHeader()
        if not head.isHidden():
            h += head.sizeHint().height()
        bar = t.horizontalScrollBar()
        overlay = t.style().styleHint(QStyle.StyleHint.SH_ScrollBar_Transient, None, t)
        if (bar.maximum() > 0 and not overlay
                and t.horizontalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff):
            h += bar.sizeHint().height()
        return h

    def refit(self, *_):
        if self._gone():
            return
        h = self.content_height()
        lo = min(h, self._min)
        if self._t.maximumHeight() != h:
            self._t.setMaximumHeight(h)
        if self._t.minimumHeight() != lo:
            self._t.setMinimumHeight(lo)


def fit_rows(table, min_height=160, containers=()):
    """Keep `table` (a QTableWidget/QTableView) no taller than its rows, and at
    least min(rows, min_height) tall. `containers`: the widgets that hold it,
    innermost first, up to the one the tab's layout stretches; each is capped at
    its layout's maximum. Returns the fitter (owned by the table)."""
    for w in containers:
        w.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
    table._row_fit = _RowFit(table, min_height)
    return table._row_fit


def bound_height(widget):
    """Stop a label (or any row beside a fitted table) from growing taller than
    it needs, so it can't soak up the height the table gives up."""
    widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(),
                         QSizePolicy.Policy.Maximum)
    return widget
