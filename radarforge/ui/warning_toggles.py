"""The warning-type switches at the right end of the main toolbar: one compact strip, one click per type.

Each type is a coloured dot (its warning line colour) and a short code - TOR, SVR, FFW, WIN, TRO, OTH, WAT. A
click turns that type on or off on the map and in the Warnings panel; off types show a hollow dot and dimmed
code. Right-click a type to show only it, or to turn everything back on. In a narrow window the codes go and just
the dots stay.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QMenu, QToolTip, QWidget


class WarningToggles(QWidget):
    changed = Signal()

    PAD_X = 8            # inside each segment (full mode)
    DOT = 8.0
    GAP = 5              # dot to code

    def __init__(self, items, color_of, open_panel=None, parent=None):
        """[items]: [(key, full name, short code, line code, checkable button holding the state)];
        [color_of](line code) -> (r, g, b); [open_panel]() opens the Warnings panel."""
        super().__init__(parent)
        self.items = list(items)
        self.color_of = color_of
        self.open_panel = open_panel
        self.compact = False
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        for _k, _f, _s, _c, b in self.items:
            b.toggled.connect(self._state_changed)

    # ------------------------------------------------------------------ state
    def _state_changed(self, _on=False):
        self.update()
        self.changed.emit()

    def is_on(self, i):
        return self.items[i][4].isChecked()

    def set_on(self, i, on):
        b = self.items[i][4]
        if b.isChecked() != on:
            b.setChecked(on)

    def set_compact(self, on):
        if on != self.compact:
            self.compact = on
            self.updateGeometry()
            self.update()

    # ------------------------------------------------------------------ geometry
    def _seg_widths(self):
        fm = QFontMetricsF(self.font())
        if self.compact:
            return [self.DOT + 12.0 for _ in self.items]
        return [self.PAD_X * 2 + self.DOT + self.GAP + fm.horizontalAdvance(s) for _k, _f, s, _c, _b in self.items]

    def _segments(self):
        out, x = [], 1.0
        h = self.height()
        for w in self._seg_widths():
            out.append(QRectF(x, 1.0, w, h - 2.0))
            x += w
        return out

    def sizeHint(self):
        fm = QFontMetricsF(self.font())
        return QSize(int(sum(self._seg_widths()) + 2), int(max(fm.height() + 12, 26)))

    def minimumSizeHint(self):
        return self.sizeHint()

    def _index_at(self, pos):
        for i, r in enumerate(self._segments()):
            if r.contains(QPointF(pos)):
                return i
        return -1

    # ------------------------------------------------------------------ drawing
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        text = pal.windowText().color()
        dim = pal.placeholderText().color()
        tray = QColor(text)
        tray.setAlpha(16)
        edge = QColor(text)
        edge.setAlpha(34)
        segs = self._segments()
        if not segs:
            return
        whole = QRectF(segs[0].left(), segs[0].top(), segs[-1].right() - segs[0].left(), segs[0].height())
        rad = whole.height() / 2
        p.setPen(QPen(edge, 1))
        p.setBrush(tray)
        p.drawRoundedRect(whole, rad, rad)
        fm = QFontMetricsF(self.font())
        for i, (r, (_k, full, short, code, _b)) in enumerate(zip(segs, self.items)):
            on = self.is_on(i)
            if i == self._hover:
                hv = QColor(text)
                hv.setAlpha(26)
                p.setPen(Qt.NoPen)
                p.setBrush(hv)
                # (the end segments follow the tray's rounded ends)
                p.save()
                clip = QRectF(r.adjusted(0.5, 0.5, -0.5, -0.5))
                p.setClipRect(clip)
                p.drawRoundedRect(whole.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
                p.restore()
            try:
                rgb = self.color_of(code)
            except Exception:
                rgb = (200, 200, 200)
            col = QColor(*rgb[:3])
            if self.compact:
                cx = r.center().x()
            else:
                cx = r.left() + self.PAD_X + self.DOT / 2
            cy = r.center().y()
            if on:
                p.setPen(QPen(QColor(0, 0, 0, 90), 1))
                p.setBrush(col)
            else:
                p.setPen(QPen(dim, 1.3))
                p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), self.DOT / 2, self.DOT / 2)
            if not self.compact:
                p.setPen(text if on else dim)
                f = self.font()
                f.setStrikeOut(not on)
                p.setFont(f)
                tx = cx + self.DOT / 2 + self.GAP
                p.drawText(QRectF(tx, r.top(), fm.horizontalAdvance(short) + 2, r.height()),
                           Qt.AlignVCenter | Qt.AlignLeft, short)
            if i > 0:                                   # hairline between segments
                p.setPen(QPen(edge, 1))
                p.drawLine(QPointF(r.left(), r.top() + 6), QPointF(r.left(), r.bottom() - 6))
        p.end()

    # ------------------------------------------------------------------ mouse
    def mouseMoveEvent(self, ev):
        i = self._index_at(ev.position())
        if i != self._hover:
            self._hover = i
            self.update()
            if i >= 0:
                _k, full, short, _c, _b = self.items[i]
                state = "shown" if self.is_on(i) else "hidden"
                QToolTip.showText(ev.globalPosition().toPoint(),
                                  f"{full} ({short}): {state} – click to {'hide' if self.is_on(i) else 'show'}"
                                  "\nRight-click: show only this type, or all", self)

    def leaveEvent(self, ev):
        self._hover = -1
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        i = self._index_at(ev.position())
        if i < 0:
            return
        if ev.button() == Qt.LeftButton:
            self.set_on(i, not self.is_on(i))
        elif ev.button() == Qt.RightButton:
            self._menu(i, ev.globalPosition().toPoint())

    def _menu(self, i, gpos):
        m = QMenu(self)
        full = self.items[i][1]
        m.addAction(f"Only {full}", lambda: [self.set_on(j, j == i) for j in range(len(self.items))])
        m.addAction("Show all types", lambda: [self.set_on(j, True) for j in range(len(self.items))])
        if self.open_panel is not None:
            m.addSeparator()
            m.addAction("Warnings panel…", self.open_panel)
        m.exec(gpos)
