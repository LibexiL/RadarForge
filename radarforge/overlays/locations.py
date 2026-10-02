"""My location: a marker set from the map's right-click menu (desktops have no GPS)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen

from ..products.geometry import aeqd_forward


class MyLocation:
    def __init__(self, settings):
        self.settings = settings

    def latlon(self):
        loc = self.settings["my_location"]
        if not loc or len(loc) < 2:
            return None
        try:
            return float(loc[0]), float(loc[1])
        except (TypeError, ValueError):
            return None

    def xy(self, view):
        ll = self.latlon()
        if ll is None:
            return None
        x, y = aeqd_forward(ll[0], ll[1], view.lat0, view.lon0)
        return float(x), float(y)

    def paint(self, painter, vt, panel, view):
        p = self.xy(view)
        self._last_xy = p
        if p is None:
            return
        sx, sy = vt.to_screen(*p)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(58, 141, 255, 85))
        painter.drawEllipse(QPointF(sx, sy), 12, 12)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QPointF(sx, sy), 6.5, 6.5)
        painter.setBrush(QColor(74, 163, 255))
        painter.drawEllipse(QPointF(sx, sy), 4.5, 4.5)
        painter.setPen(QPen(QColor(0, 0, 0, 160), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(sx, sy), 7, 7)

    def hover(self, x, y, tol):
        ll = self.latlon()
        p = getattr(self, "_last_xy", None)
        if ll is None or p is None:
            return None
        if math.hypot(p[0] - x, p[1] - y) < tol * 1.3:
            return f"My location\n{abs(ll[0]):.4f}°{'N' if ll[0] >= 0 else 'S'} {abs(ll[1]):.4f}°{'W' if ll[1] < 0 else 'E'}"
        return None
