"""Small captions in each panel's top-right corner naming the data layers shown (and their times)."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF

from ..render.fonts import ui_font


class LayerCaptions:
    def __init__(self, providers=None):
        self.providers = list(providers or [])

    def lines(self):
        out = []
        for p in self.providers:
            try:
                c = p.caption()
            except Exception:
                c = None
            if c:
                out.append(c)
        return out

    def paint(self, painter, vt, panel, view):
        lines = self.lines()
        if not lines:
            return
        font = ui_font(8)
        fm = QFontMetricsF(font)
        painter.setFont(font)
        r = vt.rect
        y = r.top() + 6
        for text in lines:
            w = fm.horizontalAdvance(text) + 12
            if w > r.width() * 0.6:
                text = fm.elidedText(text, Qt.ElideRight, r.width() * 0.6 - 12)
                w = fm.horizontalAdvance(text) + 12
            box = QRectF(r.right() - w - 6, y, w, fm.height() + 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor(225, 225, 225))
            painter.drawText(box, Qt.AlignCenter, text)
            y += fm.height() + 7
