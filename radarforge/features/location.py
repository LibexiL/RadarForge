"""My location (a marker set from the map's right-click menu – desktops have no GPS) and saved places."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF

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

    def saved(self, view):
        """[(x, y, loc)] of saved places with coordinates (not "My location")."""
        out = []
        for loc in self.settings["saved_locations"] or []:
            if loc.get("mine"):
                continue
            try:
                x, y = aeqd_forward(float(loc["lat"]), float(loc["lon"]), view.lat0, view.lon0)
            except (KeyError, TypeError, ValueError):
                continue
            out.append((float(x), float(y), loc))
        return out

    def paint(self, painter, vt, panel, view):
        self._saved_xy = self.saved(view)
        x0, y0, x1, y1 = vt.world_bounds(pad=5)
        for x, y, loc in self._saved_xy:
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            sx, sy = vt.to_screen(x, y)
            on = loc.get("enabled", True)
            dia = QPolygonF([QPointF(sx, sy - 6), QPointF(sx + 6, sy), QPointF(sx, sy + 6), QPointF(sx - 6, sy)])
            painter.setPen(QPen(QColor(0, 0, 0, 200), 1.2))
            painter.setBrush(QColor(80, 220, 200) if on else QColor(150, 150, 150))
            painter.drawPolygon(dia)
            if vt.km_across < 900:
                view._halo_text(painter, sx + 8, sy + 4, str(loc.get("name", "")), QColor(150, 240, 225))
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
        for sx, sy, loc in getattr(self, "_saved_xy", []):
            if math.hypot(sx - x, sy - y) < tol * 1.3:
                from .alerts import describe_rules, normalise
                return f"{loc.get('name', 'Saved place')}\nAlerts: {describe_rules(normalise(loc))}"
        ll = self.latlon()
        p = getattr(self, "_last_xy", None)
        if ll is None or p is None:
            return None
        if math.hypot(p[0] - x, p[1] - y) < tol * 1.3:
            return f"My location\n{abs(ll[0]):.4f}°{'N' if ll[0] >= 0 else 'S'} {abs(ll[1]):.4f}°{'W' if ll[1] < 0 else 'E'}"
        return None
