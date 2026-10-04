"""Level III graphic overlays: storm tracks, mesocyclones, TVS, hail, melting layer."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font

ML_COLORS = {1: QColor(120, 200, 255, 220), 2: QColor(60, 255, 160, 220),
             3: QColor(255, 220, 90, 220), 4: QColor(255, 120, 120, 220)}
ML_LABELS = {1: "ML bottom (beam bottom)", 2: "ML bottom (beam centre)", 3: "ML top (beam centre)",
             4: "ML top (beam top)"}


class Level3Overlay:
    def __init__(self, settings):
        self.settings = settings
        self.frame = None
        self._offset_cache = {}

    def _products(self):
        f = self.frame
        if f is None:
            return {}
        return f.l3

    def _offset(self, prod, view):
        """Products are relative to their own radar; shift into the view frame."""
        self._view_ll = (view.lat0, view.lon0)
        k = (prod.lat, prod.lon, view.lat0, view.lon0)
        if k not in self._offset_cache:
            x, y = aeqd_forward(prod.lat, prod.lon, view.lat0, view.lon0)
            self._offset_cache[k] = (float(x), float(y))
        return self._offset_cache[k]

    def paint(self, painter, vt, panel, view):
        ov = self.settings["overlays"]
        prods = self._products()
        if not prods:
            return
        font = ui_font(8, True)
        painter.setFont(font)
        bx0, by0, bx1, by1 = vt.world_bounds(pad=40.0 / max(vt.scale, 1e-3) + 30.0)

        def S(prod, x, y):
            ox, oy = self._offset(prod, view)
            return QPointF(*vt.to_screen(x + ox, y + oy))

        def near(prod, g):
            ox, oy = self._offset(prod, view)
            return bx0 <= g["x"] + ox <= bx1 and by0 <= g["y"] + oy <= by1

        if ov.get("melting_layer"):
            code = None
            for c in ("N0M", "NAM", "N1M", "NBM", "N2M", "N3M"):
                if c in prods:
                    code = c
                    break
            if code:
                prod = prods[code]
                labelled = False
                for g in prod.graphics:
                    if g["kind"] != "line":
                        continue
                    ci = g.get("color") or 1
                    pts = [S(prod, x, y) for x, y in g["points"]]
                    pen = QPen(ML_COLORS.get(ci, QColor(200, 200, 200)), 1.6, Qt.DotLine)
                    painter.setPen(pen)
                    painter.drawPolyline(QPolygonF(pts))
                    if ci == 4 and pts and not labelled:
                        top = min(pts, key=lambda q: q.y())      # label at the northern-most point
                        view._halo_text(painter, top.x() + 4, top.y() - 4, f"Melting layer ({code})",
                                        ML_COLORS[4])
                        labelled = True

        if ov.get("hail") and "NHI" in prods:
            prod = prods["NHI"]
            for g in prod.graphics:
                if g["kind"] != "hail" or not near(prod, g):
                    continue
                posh, poh = g.get("posh") or 0, g.get("poh") or 0
                if poh < 30 and posh < 30:
                    continue
                p = S(prod, g["x"], g["y"])
                tri = QPolygonF([QPointF(p.x(), p.y() - 11), QPointF(p.x() - 9, p.y() + 6), QPointF(p.x() + 9, p.y() + 6)])
                painter.setPen(QPen(QColor(0, 0, 0), 2.5))
                painter.setBrush(Qt.NoBrush)
                painter.drawPolygon(tri)
                col = QColor(0, 230, 0) if posh < 50 else QColor(255, 255, 0) if posh < 70 else QColor(255, 60, 0)
                painter.setPen(QPen(col, 1.8))
                painter.setBrush(col if posh >= 50 else Qt.NoBrush)
                painter.drawPolygon(tri)
                if g.get("size"):
                    view._halo_text(painter, p.x() + 10, p.y() + 6, f'{g["size"]:.2f}"', col)

        if ov.get("storm_tracks") and "NST" in prods:
            prod = prods["NST"]
            for g in prod.graphics:
                if g["kind"] != "storm" or not near(prod, g):
                    continue
                cur = S(prod, g["x"], g["y"])
                if g.get("past"):
                    pts = [S(prod, x, y) for x, y in g["past"]]
                    painter.setPen(QPen(QColor(0, 0, 0, 200), 3))
                    painter.drawPolyline(QPolygonF(pts))
                    painter.setPen(QPen(QColor(255, 255, 255), 1.3))
                    painter.drawPolyline(QPolygonF(pts))
                    painter.setBrush(QColor(255, 255, 255))
                    for q in pts[1:]:
                        painter.drawEllipse(q, 2, 2)
                if g.get("fcst"):
                    pts = [S(prod, x, y) for x, y in g["fcst"]]
                    painter.setPen(QPen(QColor(0, 0, 0, 200), 3))
                    painter.drawPolyline(QPolygonF(pts))
                    painter.setPen(QPen(QColor(90, 200, 255), 1.5))
                    painter.drawPolyline(QPolygonF(pts))
                    for q in pts[1:]:
                        painter.drawLine(QPointF(q.x() - 3, q.y() - 3), QPointF(q.x() + 3, q.y() + 3))
                        painter.drawLine(QPointF(q.x() - 3, q.y() + 3), QPointF(q.x() + 3, q.y() - 3))
                painter.setPen(QPen(QColor(0, 0, 0), 1))
                painter.setBrush(QColor(255, 255, 255))
                painter.drawEllipse(cur, 3.5, 3.5)
                if g.get("id"):
                    view._halo_text(painter, cur.x() + 6, cur.y() - 5, g["id"],
                                    view.colors.get("label_text", QColor(255, 255, 255)))

    def hover(self, x, y, tol):
        prods = self._products()
        if not prods:
            return None
        ov = self.settings["overlays"]
        cands = []
        for code, kind, key in (("NHI", "hail", "hail"), ("NST", "storm", "storm_tracks")):
            if not ov.get(key) or code not in prods:
                continue
            prod = prods[code]
            ox, oy = (0.0, 0.0)
            vll = getattr(self, "_view_ll", None)
            if vll is not None:
                ox, oy = self._offset_cache.get((prod.lat, prod.lon) + vll, (0.0, 0.0))
            for g in prod.graphics:
                if g["kind"] != kind:
                    continue
                d = math.hypot(g["x"] + ox - x, g["y"] + oy - y)
                if d < tol * 1.5:
                    if kind == "hail":
                        txt = f"Hail: POSH {g.get('posh')}%  POH {g.get('poh')}%  max {g.get('size')}\""
                    else:
                        txt = f"Storm {g.get('id') or ''}"
                        if g.get("fcst") and len(g["fcst"]) > 1:
                            (x0, y0), (x1, y1) = g["fcst"][0], g["fcst"][-1]
                            mins = 15 * (len(g["fcst"]) - 1)
                            spd = math.hypot(x1 - x0, y1 - y0) / (mins / 60) / 1.852
                            dirn = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 180) % 360
                            txt += f"  motion {dirn:03.0f}° @ {spd:.0f} kt"
                    cands.append((d, txt))
        if cands:
            return min(cands)[1]
        return None

    def mean_storm_motion(self):
        """(dir_from deg, speed kt) averaged over NST forecast tracks, or None."""
        prods = self._products()
        prod = prods.get("NST") if prods else None
        if prod is None:
            return None
        us, vs = [], []
        for g in prod.graphics:
            f = g.get("fcst") if g["kind"] == "storm" else None
            if f and len(f) > 1:
                mins = 15 * (len(f) - 1)
                us.append((f[-1][0] - f[0][0]) / (mins / 60))
                vs.append((f[-1][1] - f[0][1]) / (mins / 60))
        if not us:
            return None
        u, v = sum(us) / len(us), sum(vs) / len(vs)
        spd = math.hypot(u, v) / 1.852
        dir_from = (math.degrees(math.atan2(u, v)) + 180.0) % 360.0
        return dir_from, spd
