"""Small vector icons drawn with QPainter, tinted with the theme's text colour.

Drawn icons look the same on every system (emoji / symbol fonts differ a lot
between desktops) and follow the theme.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

_cache: dict = {}


def clear_cache():
    _cache.clear()


def icon(name: str, color: QColor | None = None) -> QIcon:
    pal = QApplication.palette()
    col = QColor(color) if color is not None else pal.color(QPalette.WindowText)
    key = (name, col.rgba())
    ic = _cache.get(key)
    if ic is None:
        ic = QIcon()
        for size in (16, 20, 24, 32, 48):
            ic.addPixmap(_render(name, col, size))
        dim = QColor(col)
        dim.setAlpha(90)
        for size in (16, 24, 32):
            ic.addPixmap(_render(name, dim, size), QIcon.Disabled)
        _cache[key] = ic
    return ic


def _render(name, col, size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 24.0, size / 24.0)           # draw on a 24x24 grid
    pen = QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    fn = _DRAW.get(name)
    if fn is not None:
        fn(p, col, pen)
    p.end()
    return pm


def _fill(p, col):
    p.setPen(Qt.NoPen)
    p.setBrush(col)


# ---- individual icons (24x24 grid) -------------------------------------------
def _live(p, col, pen):
    _fill(p, QColor(70, 210, 110))
    p.drawEllipse(QPointF(12, 12), 3.2, 3.2)
    p.setBrush(Qt.NoBrush)
    for r, a in ((6.5, 200), (10, 130)):
        c = QColor(70, 210, 110, a)
        p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(12 - r, 12 - r, 2 * r, 2 * r), 30 * 16, 120 * 16)
        p.drawArc(QRectF(12 - r, 12 - r, 2 * r, 2 * r), 210 * 16, 120 * 16)


def _archive(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.drawLine(QPointF(12, 12), QPointF(12, 7))
    p.drawLine(QPointF(12, 12), QPointF(15.5, 14))
    p.drawLine(QPointF(3.5, 5.5), QPointF(3.5, 9.5))
    p.drawLine(QPointF(3.5, 9.5), QPointF(7.2, 9.5))


def _open(p, col, pen):
    path = QPainterPath()
    path.moveTo(3, 7)
    path.lineTo(3, 18.5)
    path.lineTo(19, 18.5)
    path.lineTo(21, 10.5)
    path.lineTo(7, 10.5)
    path.lineTo(5, 18.5)
    p.drawPath(path)
    p.drawPolyline(QPolygonF([QPointF(3, 7), QPointF(3, 5), QPointF(9, 5), QPointF(10.5, 7), QPointF(18, 7),
                              QPointF(18, 10.5)]))


def _layout(n):
    cells = {1: [(0, 0, 1, 1)], 2: [(0, 0, .5, 1), (.5, 0, .5, 1)], 3: [(0, 0, 1 / 3, 1), (1 / 3, 0, 1 / 3, 1),
                                                                        (2 / 3, 0, 1 / 3, 1)],
             4: [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5), (.5, .5, .5, .5)],
             5: [(0, 0, 1 / 3, .5), (1 / 3, 0, 1 / 3, .5), (2 / 3, 0, 1 / 3, .5), (0, .5, .5, .5), (.5, .5, .5, .5)],
             6: [(x / 3, y / 2, 1 / 3, .5) for y in range(2) for x in range(3)]}[n]

    def draw(p, col, pen):
        p.setPen(QPen(col, 1.4))
        p.setBrush(Qt.NoBrush)
        box = QRectF(3, 5, 18, 14)
        for fx, fy, fw, fh in cells:
            p.drawRect(QRectF(box.x() + fx * box.width(), box.y() + fy * box.height(), fw * box.width(),
                              fh * box.height()))
    return draw


def _tri(direction):
    def draw(p, col, pen):
        _fill(p, col)
        if direction == "up":
            p.drawPolygon(QPolygonF([QPointF(12, 6.5), QPointF(19, 16.5), QPointF(5, 16.5)]))
        else:
            p.drawPolygon(QPolygonF([QPointF(12, 17.5), QPointF(19, 7.5), QPointF(5, 7.5)]))
    return draw


def _play(p, col, pen):
    _fill(p, col)
    p.drawPolygon(QPolygonF([QPointF(7, 5), QPointF(19, 12), QPointF(7, 19)]))


def _pause(p, col, pen):
    _fill(p, col)
    p.drawRoundedRect(QRectF(6.5, 5, 4, 14), 1, 1)
    p.drawRoundedRect(QRectF(13.5, 5, 4, 14), 1, 1)


def _step(forward, to_end):
    def draw(p, col, pen):
        _fill(p, col)
        if forward:
            p.drawPolygon(QPolygonF([QPointF(6, 6), QPointF(15, 12), QPointF(6, 18)]))
            if to_end:
                p.drawRect(QRectF(16, 6, 2.6, 12))
            else:
                p.drawRect(QRectF(15.5, 6, 2.2, 12))
        else:
            p.drawPolygon(QPolygonF([QPointF(18, 6), QPointF(9, 12), QPointF(18, 18)]))
            p.drawRect(QRectF(5.4 if to_end else 6.3, 6, 2.6 if to_end else 2.2, 12))
    return draw


def _pan(p, col, pen):
    p.drawLine(QPointF(12, 3.5), QPointF(12, 20.5))
    p.drawLine(QPointF(3.5, 12), QPointF(20.5, 12))
    for (x, y), (dx, dy) in (((12, 3.5), (0, 1)), ((12, 20.5), (0, -1)), ((3.5, 12), (1, 0)), ((20.5, 12), (-1, 0))):
        a = QPointF(x + dx * 3 - dy * 2.6, y + dy * 3 - dx * 2.6)
        b = QPointF(x + dx * 3 + dy * 2.6, y + dy * 3 + dx * 2.6)
        p.drawPolyline(QPolygonF([a, QPointF(x, y), b]))


def _xsection(p, col, pen):
    p.drawLine(QPointF(4, 18), QPointF(20, 6))
    _fill(p, col)
    p.drawEllipse(QPointF(4, 18), 2.4, 2.4)
    p.drawEllipse(QPointF(20, 6), 2.4, 2.4)
    p.setPen(QPen(col, 1.3))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(4, 21.5), QPointF(20, 21.5))


def _measure(p, col, pen):
    p.save()
    p.translate(12, 12)
    p.rotate(-35)
    p.drawRoundedRect(QRectF(-10, -3.5, 20, 7), 1.2, 1.2)
    p.setPen(QPen(col, 1.2))
    for i in range(-7, 9, 3):
        p.drawLine(QPointF(i, -3.5), QPointF(i, -0.8 if i % 2 else 0.4))
    p.restore()


def _track(p, col, pen):
    # a storm (circle) with an arrow and tick marks along its path
    p.drawEllipse(QPointF(5.5, 18.5), 3, 3)
    p.drawLine(QPointF(8, 16), QPointF(19, 5))
    p.drawPolyline(QPolygonF([QPointF(13.5, 5), QPointF(19.5, 4.5), QPointF(19, 10.5)]))
    p.setPen(QPen(col, 1.4))
    for t in (0.35, 0.65):
        x, y = 8 + 11 * t, 16 - 11 * t
        p.drawLine(QPointF(x - 2.2, y - 2.2), QPointF(x + 2.2, y + 2.2))


def _cube(p, col, pen):
    top = QPolygonF([QPointF(12, 3.5), QPointF(20, 7.5), QPointF(12, 11.5), QPointF(4, 7.5)])
    p.drawPolygon(top)
    p.drawLine(QPointF(4, 7.5), QPointF(4, 16.5))
    p.drawLine(QPointF(20, 7.5), QPointF(20, 16.5))
    p.drawLine(QPointF(12, 11.5), QPointF(12, 20.5))
    p.drawLine(QPointF(4, 16.5), QPointF(12, 20.5))
    p.drawLine(QPointF(20, 16.5), QPointF(12, 20.5))


def _motion(p, col, pen):
    p.drawEllipse(QPointF(8, 15), 4, 4)
    p.drawLine(QPointF(11, 12), QPointF(19.5, 4.5))
    p.drawPolyline(QPolygonF([QPointF(14, 4.5), QPointF(19.5, 4.5), QPointF(19.5, 10)]))


def _side(p, col, pen):
    p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2, 2)
    _fill(p, col)
    p.drawRect(QRectF(14, 5.4, 6, 13.2))


def _gear(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 3, 3)
    path = QPainterPath()
    n = 8
    for i in range(n * 2):
        a = math.pi * 2 * i / (n * 2)
        r = 9 if i % 2 == 0 else 7
        a2 = a + math.pi * 2 / (n * 2)
        pt1 = QPointF(12 + r * math.cos(a), 12 + r * math.sin(a))
        pt2 = QPointF(12 + r * math.cos(a2), 12 + r * math.sin(a2))
        if i == 0:
            path.moveTo(pt1)
        else:
            path.lineTo(pt1)
        path.lineTo(pt2)
    path.closeSubpath()
    p.setPen(QPen(col, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)


def _radar(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.setPen(QPen(col, 1.2))
    p.drawEllipse(QPointF(12, 12), 4.5, 4.5)
    sweep = QColor(col)
    sweep.setAlpha(110)
    _fill(p, sweep)
    p.drawPie(QRectF(3.5, 3.5, 17, 17), 45 * 16, 50 * 16)
    p.setPen(QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(12, 12), QPointF(18, 6))


def _save(p, col, pen):
    p.drawRoundedRect(QRectF(4, 4, 16, 16), 2, 2)
    p.drawRect(QRectF(8, 4, 8, 5))
    p.drawRect(QRectF(7, 13, 10, 7))


def _theme(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    _fill(p, col)
    p.drawPie(QRectF(3.5, 3.5, 17, 17), 90 * 16, 180 * 16)


def _layers(p, col, pen):
    for dy in (0, 4.5, 9):
        p.drawPolygon(QPolygonF([QPointF(12, 4 + dy), QPointF(20, 8 + dy), QPointF(12, 12 + dy), QPointF(4, 8 + dy)]))


def _palette(p, col, pen):
    p.drawRoundedRect(QRectF(3, 7, 18, 10), 2, 2)
    for i, a in enumerate((60, 110, 170, 230)):
        c = QColor(col)
        c.setAlpha(a)
        _fill(p, c)
        p.drawRect(QRectF(4.5 + i * 3.8, 8.5, 3.6, 7))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _lock(p, col, pen):
    p.drawRoundedRect(QRectF(5.5, 10.5, 13, 9.5), 1.5, 1.5)
    p.drawArc(QRectF(8, 4, 8, 12), 0, 180 * 16)
    p.drawLine(QPointF(8, 10), QPointF(8, 10.5))
    p.drawLine(QPointF(16, 10), QPointF(16, 10.5))


def _warning(p, col, pen):
    p.drawPolygon(QPolygonF([QPointF(12, 3.5), QPointF(21.5, 20), QPointF(2.5, 20)]))
    p.drawLine(QPointF(12, 9.5), QPointF(12, 14))
    _fill(p, col)
    p.drawEllipse(QPointF(12, 17), 1.2, 1.2)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _quick(p, col, pen):
    """Quick panel: three sliders."""
    for y, kx in ((6.5, 15), (12, 8), (17.5, 13)):
        p.drawLine(QPointF(3.5, y), QPointF(20.5, y))
        _fill(p, col)
        p.drawEllipse(QPointF(kx, y), 2.4, 2.4)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)


def _pin(p, col, pen):
    path = QPainterPath()
    path.moveTo(12, 21)
    path.cubicTo(6, 14, 5.5, 11, 5.5, 9.5)
    path.arcTo(QRectF(5.5, 3, 13, 13), 180, -180)
    path.cubicTo(18.5, 11, 18, 14, 12, 21)
    p.drawPath(path)
    p.drawEllipse(QPointF(12, 9.5), 2.4, 2.4)


def _camera(p, col, pen):
    p.drawRoundedRect(QRectF(3, 8, 12.5, 9), 1.8, 1.8)
    p.drawPolygon(QPolygonF([QPointF(15.5, 11), QPointF(21, 8), QPointF(21, 17), QPointF(15.5, 14)]))


def _bolt(p, col, pen):
    p.drawPolygon(QPolygonF([QPointF(13.5, 2.5), QPointF(6, 13.5), QPointF(11.5, 13.5), QPointF(10, 21.5),
                             QPointF(18, 10), QPointF(12.5, 10)]))


def _satellite(p, col, pen):
    p.save()
    p.translate(12, 12)
    p.rotate(-35)
    p.drawRect(QRectF(-3, -3, 6, 6))
    p.drawRect(QRectF(-10.5, -2.5, 6, 5))
    p.drawRect(QRectF(4.5, -2.5, 6, 5))
    p.drawLine(QPointF(-4.5, 0), QPointF(-3, 0))
    p.drawLine(QPointF(3, 0), QPointF(4.5, 0))
    p.restore()


def _image(p, col, pen):
    p.drawRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
    p.drawPolyline(QPolygonF([QPointF(4, 17), QPointF(9.5, 11), QPointF(13, 14.5), QPointF(15.5, 12.5),
                              QPointF(20, 17)]))
    _fill(p, col)
    p.drawEllipse(QPointF(16, 8.8), 1.6, 1.6)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _film(p, col, pen):
    p.drawRoundedRect(QRectF(4, 3.5, 16, 17), 1.5, 1.5)
    p.drawLine(QPointF(7.5, 3.5), QPointF(7.5, 20.5))
    p.drawLine(QPointF(16.5, 3.5), QPointF(16.5, 20.5))
    p.setPen(QPen(col, 1.2))
    for y in (6.5, 10, 13.5, 17):
        p.drawLine(QPointF(4.5, y), QPointF(7, y))
        p.drawLine(QPointF(17, y), QPointF(19.5, y))


def _info(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.drawLine(QPointF(12, 10.5), QPointF(12, 16.5))
    _fill(p, col)
    p.drawEllipse(QPointF(12, 7.5), 1.2, 1.2)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _refresh(p, col, pen):
    p.drawArc(QRectF(4.5, 4.5, 15, 15), 60 * 16, 280 * 16)
    p.drawPolyline(QPolygonF([QPointF(15, 3.5), QPointF(16.5, 6.2), QPointF(13.5, 7.5)]))


def _star(p, col, pen):
    pts = []
    for i in range(10):
        a = math.radians(-90 + 36 * i)
        r = 8.5 if i % 2 == 0 else 3.8
        pts.append(QPointF(12 + r * math.cos(a), 12.5 + r * math.sin(a)))
    p.drawPolygon(QPolygonF(pts))


def _flag(p, col, pen):
    p.drawLine(QPointF(6, 3.5), QPointF(6, 21))
    p.drawPolygon(QPolygonF([QPointF(6, 4), QPointF(19, 8), QPointF(6, 12.5)]))


def _book(p, col, pen):
    p.drawPolyline(QPolygonF([QPointF(12, 6.5), QPointF(7, 4.5), QPointF(3, 5), QPointF(3, 18.5),
                              QPointF(7, 18), QPointF(12, 20), QPointF(17, 18), QPointF(21, 18.5), QPointF(21, 5),
                              QPointF(17, 4.5), QPointF(12, 6.5)]))
    p.drawLine(QPointF(12, 6.5), QPointF(12, 20))


def _chart(p, col, pen):
    p.drawPolyline(QPolygonF([QPointF(3.5, 3.5), QPointF(3.5, 20.5), QPointF(20.5, 20.5)]))
    p.drawPolyline(QPolygonF([QPointF(6, 17), QPointF(10, 11), QPointF(13.5, 14), QPointF(19.5, 6)]))


def _sounding(p, col, pen):
    p.drawRect(QRectF(3.5, 3.5, 17, 17))
    p.setPen(QPen(col, 1.2))
    p.drawLine(QPointF(6, 20), QPointF(15, 4))
    p.setPen(QPen(col, 1.8))
    p.drawPolyline(QPolygonF([QPointF(11, 20), QPointF(13, 15), QPointF(12, 11), QPointF(16, 6)]))


def _target(p, col, pen):
    p.drawEllipse(QPointF(12, 12), 7, 7)
    p.drawLine(QPointF(12, 2.5), QPointF(12, 7))
    p.drawLine(QPointF(12, 17), QPointF(12, 21.5))
    p.drawLine(QPointF(2.5, 12), QPointF(7, 12))
    p.drawLine(QPointF(17, 12), QPointF(21.5, 12))
    _fill(p, col)
    p.drawEllipse(QPointF(12, 12), 1.8, 1.8)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _keyboard(p, col, pen):
    p.drawRoundedRect(QRectF(2.5, 6.5, 19, 11), 2, 2)
    p.setPen(QPen(col, 1.2))
    for y in (9.5, 12):
        for x in range(5, 20, 3):
            p.drawLine(QPointF(x, y), QPointF(x + 1, y))
    p.drawLine(QPointF(7.5, 14.8), QPointF(16.5, 14.8))


def _smooth(p, col, pen):
    """Smoothing: a block shading smoothly from white to grey (GR style; not tinted)."""
    from PySide6.QtGui import QLinearGradient
    g = QLinearGradient(0, 4, 0, 20)
    g.setColorAt(0, QColor(255, 255, 255, col.alpha()))
    g.setColorAt(1, QColor(150, 150, 150, col.alpha()))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawRect(QRectF(7, 4, 10, 16))


def _dealias(p, col, pen):
    """Velocity dealiasing: a folded bar (red band) joined to an unfolded one (not tinted)."""
    a = col.alpha()
    dark, bright, red = QColor(0, 130, 0, a), QColor(0, 235, 0, a), QColor(235, 0, 0, a)
    p.setPen(Qt.NoPen)
    for x in (3.5, 15.0):
        p.setBrush(dark)
        p.drawRect(QRectF(x, 4, 5.5, 16))
        p.setBrush(bright)
        p.drawRect(QRectF(x, 7, 5.5, 10))
    p.setBrush(red)
    p.drawRect(QRectF(3.5, 10.5, 5.5, 3))
    p.setPen(QPen(col, 1.6))
    p.drawLine(QPointF(9, 12), QPointF(15, 12))
    p.drawLine(QPointF(12, 10.5), QPointF(12, 13.5))


def _sigma(p, col, pen):
    path = QPainterPath()
    path.moveTo(18, 6.5)
    path.lineTo(18, 4)
    path.lineTo(6, 4)
    path.lineTo(12.5, 12)
    path.lineTo(6, 20)
    path.lineTo(18, 20)
    path.lineTo(18, 17.5)
    p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
    p.drawPath(path)


_DRAW = {
    "live": _live, "archive": _archive, "open": _open,
    "up": _tri("up"), "down": _tri("down"), "play": _play, "pause": _pause,
    "first": _step(False, True), "prev": _step(False, False), "next": _step(True, False), "last": _step(True, True),
    "pan": _pan, "xsection": _xsection, "measure": _measure, "track": _track, "box3d": _cube, "motion": _motion,
    "side": _side, "settings": _gear, "radar": _radar, "save": _save, "theme": _theme, "layers": _layers,
    "lock": _lock, "palette": _palette, "warning": _warning,
    "smooth": _smooth, "dealias": _dealias, "trail": _sigma,
    "quick": _quick, "pin": _pin, "camera": _camera, "bolt": _bolt, "satellite": _satellite, "image": _image,
    "film": _film, "info": _info, "refresh": _refresh, "star": _star, "flag": _flag, "book": _book, "chart": _chart,
    "sounding": _sounding, "target": _target, "keyboard": _keyboard,
}
for _n in range(1, 7):
    _DRAW[f"layout{_n}"] = _layout(_n)
