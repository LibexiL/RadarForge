"""Saved locations on the map: your main location (blue) and the others (teal), with their names."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen

from ..products.geometry import aeqd_forward
from ..services.locations import Location, LocationBook


class LocationsOverlay:
    def __init__(self, book: LocationBook):
        self.book = book
        self.states: dict = {}                # location id -> "inside" | "near" (a warning is on it / close to it)
        self._xy: dict = {}                   # id -> (projection key, x, y)

    # ---------------------------------------------------------------- the main location
    def latlon(self):
        p = self.book.primary()
        return (p.lat, p.lon) if p else None

    def xy(self, view):
        p = self.book.primary()
        return self._position(p, view) if p else None

    def _position(self, loc: Location, view):
        key = (round(view.lat0, 4), round(view.lon0, 4), loc.lat, loc.lon)
        hit = self._xy.get(loc.id)
        if hit is None or hit[0] != key:
            x, y = aeqd_forward(loc.lat, loc.lon, view.lat0, view.lon0)
            hit = (key, float(x), float(y))
            self._xy[loc.id] = hit
        return hit[1], hit[2]

    # ---------------------------------------------------------------- drawing
    def paint(self, painter, vt, panel, view):
        x0, y0, x1, y1 = vt.world_bounds(pad=40.0 / max(vt.scale, 1e-3))
        names = vt.km_across < 2500
        for i, loc in enumerate(self.book.items):
            x, y = self._position(loc, view)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            sx, sy = vt.to_screen(x, y)
            main = i == 0
            core = QColor(74, 163, 255) if main else QColor(60, 205, 190)
            r = 4.5 if main else 3.8
            state = self.states.get(loc.id)
            painter.setPen(Qt.NoPen)
            if state:
                ring = QColor(255, 60, 50) if state == "inside" else QColor(255, 170, 40)
                painter.setBrush(QColor(ring.red(), ring.green(), ring.blue(), 90))
                painter.drawEllipse(QPointF(sx, sy), 15, 15)
                painter.setPen(QPen(ring, 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(sx, sy), 11, 11)
                painter.setPen(Qt.NoPen)
            elif main:
                painter.setBrush(QColor(58, 141, 255, 85))
                painter.drawEllipse(QPointF(sx, sy), 12, 12)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(QPointF(sx, sy), r + 2, r + 2)
            painter.setBrush(core)
            painter.drawEllipse(QPointF(sx, sy), r, r)
            painter.setPen(QPen(QColor(0, 0, 0, 160), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(sx, sy), r + 2.5, r + 2.5)
            if names:
                view._halo_text(painter, sx + 11, sy + 4, loc.name, QColor(225, 240, 255) if main else QColor(190, 240, 235))

    # ---------------------------------------------------------------- finding and describing
    def at(self, x, y, tol) -> Location | None:
        """The location under a map point (km), if any."""
        best, bd = None, tol * 1.4
        for loc in self.book.items:
            pos = self._xy.get(loc.id)
            if pos is None:
                continue
            d = math.hypot(pos[1] - x, pos[2] - y)
            if d < bd:
                best, bd = loc, d
        return best

    def hover(self, x, y, tol):
        loc = self.at(x, y, tol)
        if loc is None:
            return None
        main = "My location" if loc is self.book.primary() else "Saved location"
        return (f"{loc.name} ({main})\n{abs(loc.lat):.4f}°{'N' if loc.lat >= 0 else 'S'} "
                f"{abs(loc.lon):.4f}°{'W' if loc.lon < 0 else 'E'}\nAlerts: {loc.describe_rules()}")
