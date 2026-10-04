"""Vertical cross-section window."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

from ..products import catalog
from .sampler import sample_volume
from ..render.fonts import ui_font

XS_PRODUCTS = ["REF", "VEL", "DVEL", "SRV", "SW", "ZDR", "CC", "KDP", "PHI", "AZSH", "DIV"]


class _Sig(QObject):
    done = Signal(object)


class _Job(QRunnable):
    """Runs fn in the thread pool and emits the result through a long-lived relay object."""

    def __init__(self, fn, relay):
        super().__init__()
        self.fn = fn
        self.sig = relay

    def run(self):
        try:
            res = self.fn()
        except Exception as exc:
            res = exc
        self.sig.done.emit(res)


def compute_xsection(engine, frame, pid, p0, p1, top_km, nx=480, nz=220, smooth=True, radar_h_km=0.0):
    tilts = engine.tilt_sweeps(frame, pid)
    L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    t = np.linspace(0, 1, nx)
    xs = p0[0] + (p1[0] - p0[0]) * t
    ys = p0[1] + (p1[1] - p0[1]) * t
    zs = np.linspace(0, top_km, nz)            # km MSL
    X = np.broadcast_to(xs[None, :], (nz, nx))
    Y = np.broadcast_to(ys[None, :], (nz, nx))
    Z = np.broadcast_to((zs - radar_h_km)[:, None], (nz, nx))
    vals = sample_volume(tilts, X, Y, np.maximum(Z, 0.0), smooth=smooth)
    vals[Z < 0] = np.nan
    return vals, L, zs, [e for e, _ in tilts]


class XSectionCanvas(QWidget):
    hovered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(500, 280)
        self.img = None
        self.vals = None
        self.L = 1.0
        self.top = 18.0
        self.palette = None
        self.units = ""
        self.dunit = ("nm", 1.852)
        self.message = "Draw a line with the cross-section tool (or Shift+drag)"

    def set_data(self, vals, L, zs, palette, storage_units, dunit):
        self.vals, self.L, self.top = vals, L, float(zs[-1])
        self.palette, self.units, self.dunit = palette, storage_units, dunit
        v = vals.astype(np.float64)
        scale = palette.data_scale(storage_units)
        disp = v * scale + palette.offset
        rgba = palette.color_at(np.where(np.isinf(v), np.nan, disp))
        rf = np.isinf(v) & (v > 0)
        if rf.any() and palette.rf:
            rgba[rf] = palette.rf[:4] if len(palette.rf) == 4 else (*palette.rf[:3], 255)
        rgba = np.ascontiguousarray(rgba[::-1])       # row 0 = top
        h, w, _ = rgba.shape
        self._buf = rgba
        self.img = QImage(self._buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
        self.message = ""
        self.update()

    def _plot_rect(self):
        return QRectF(52, 10, max(10, self.width() - 64), max(10, self.height() - 40))

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 10, 14))
        r = self._plot_rect()
        p.fillRect(r, QColor(0, 0, 0))
        if self.img is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawImage(r, self.img)
        p.setPen(QPen(QColor(90, 90, 100), 1))
        p.drawRect(r)
        p.setFont(ui_font(8))
        # height axis (kft)
        top_kft = self.top * 3.28084
        step = 5 if top_kft <= 40 else 10
        k = 0
        while k <= top_kft + 1e-6:
            y = r.bottom() - k / top_kft * r.height()
            p.setPen(QPen(QColor(60, 60, 70), 1, Qt.DotLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(200, 200, 200))
            p.drawText(QRectF(0, y - 7, 46, 14), Qt.AlignRight | Qt.AlignVCenter, f"{k:.0f}")
            k += step
        p.drawText(QRectF(2, r.top() - 10, 50, 12), Qt.AlignLeft, "kft")
        # distance axis
        name, f = self.dunit
        Ld = self.L / f
        step = 5 if Ld < 40 else 10 if Ld < 120 else 25
        d = 0.0
        while d <= Ld + 1e-6:
            x = r.left() + d / max(Ld, 1e-6) * r.width()
            p.setPen(QColor(200, 200, 200))
            p.drawLine(QPointF(x, r.bottom()), QPointF(x, r.bottom() + 4))
            p.drawText(QRectF(x - 20, r.bottom() + 5, 40, 14), Qt.AlignCenter, f"{d:.0f}")
            d += step
        p.drawText(QRectF(r.right() - 60, r.bottom() + 18, 60, 14), Qt.AlignRight, name)
        p.setPen(QColor(255, 220, 60))
        p.drawText(QRectF(r.left(), r.bottom() + 18, 30, 14), Qt.AlignLeft, "A")
        p.drawText(QRectF(r.right() - 90, r.bottom() + 18, 20, 14), Qt.AlignLeft, "B")
        if self.message:
            p.setPen(QColor(200, 200, 200))
            p.drawText(r, Qt.AlignCenter, self.message)
        p.end()

    def mouseMoveEvent(self, ev):
        if self.vals is None:
            return
        r = self._plot_rect()
        pos = ev.position()
        if not r.contains(pos):
            return
        fx = (pos.x() - r.left()) / r.width()
        fz = (r.bottom() - pos.y()) / r.height()
        nz, nx = self.vals.shape
        i = min(nx - 1, max(0, int(fx * nx)))
        j = min(nz - 1, max(0, int(fz * nz)))
        v = float(self.vals[j, i])
        name, f = self.dunit
        txt = f"{fx * self.L / f:.1f} {name}, {fz * self.top * 3.28084:.1f} kft: "
        if math.isnan(v):
            txt += "—"
        elif math.isinf(v):
            txt += "RF"
        else:
            disp = v * self.palette.data_scale(self.units) + self.palette.offset
            txt += f"{disp:.2f} {self.palette.units}"
        self.hovered.emit(txt)


class CrossSectionWindow(QWidget):
    closed = Signal()

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.resize(820, 420)
        self.line = None
        self.canvas = XSectionCanvas(self)
        self.product = QComboBox()
        for pid in XS_PRODUCTS:
            self.product.addItem(catalog.get(pid).name, pid)
        self.smooth = QCheckBox("Interpolate between tilts")
        self.smooth.setChecked(True)
        self.top = QDoubleSpinBox()
        self.top.setRange(10, 80)
        self.top.setSuffix(" kft")
        self.top.setValue(float(main.settings["xsection_top_kft"]))
        self.info = QLabel("")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Product"))
        bar.addWidget(self.product)
        bar.addWidget(self.smooth)
        bar.addWidget(QLabel("Top"))
        bar.addWidget(self.top)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.info)
        self.product.currentIndexChanged.connect(self.refresh)
        self.smooth.toggled.connect(self.refresh)
        self.top.valueChanged.connect(self.refresh)
        self.canvas.hovered.connect(self.info.setText)
        self._pending = False
        self._running = False
        self._relay = _Sig()
        self._relay.done.connect(self._done)
        self._pid = None

    def set_line(self, x0, y0, x1, y1):
        self.line = ((x0, y0), (x1, y1))
        self.refresh()

    def refresh(self):
        if self.line is None or not self.isVisible():
            return
        if self._running:
            self._pending = True
            return
        frame = self.main.current_frame()
        if frame is None or not frame.has_level2():
            self.canvas.message = "Cross sections need Level II data"
            self.canvas.update()
            return
        pid = self.product.currentData()
        engine = self.main.engine
        top_km = self.top.value() / 3.28084
        smooth = self.smooth.isChecked()
        (p0, p1) = self.line
        self._running = True
        self.canvas.message = "Computing…"
        self.canvas.update()
        self._pid = pid
        QThreadPool.globalInstance().start(
            _Job(lambda: compute_xsection(engine, frame, pid, p0, p1, top_km, smooth=smooth,
                                          radar_h_km=(frame.level2().height_m or 0.0) / 1000.0),   # decode off the UI thread
                 self._relay))

    def _done(self, res):
        pid = self._pid
        self._running = False
        if isinstance(res, Exception):
            self.canvas.message = f"Error: {res}"
            self.canvas.update()
        else:
            vals, L, zs, _els = res
            prod = catalog.get(pid)
            pal = self.main.palette_for(pid)
            du = self.main.settings["distance_units"]
            f = {"nm": 1.852, "km": 1.0, "mi": 1.609344}[du]
            self.canvas.set_data(vals, L, zs, pal, prod.units, (du, f))
            fr = self.main.current_frame()
            self.set_title(f"Cross Section – {prod.name} – {fr.time:%H:%M:%S}Z" if fr else "Cross Section")
        if self._pending:
            self._pending = False
            self.refresh()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.refresh()

    def set_title(self, t):
        self.setWindowTitle(t)          # the panel's tab follows the window title

    def on_closed(self):
        """Called when the cross-section window is closed: forget the line."""
        self.line = None
        self.canvas.vals = None
        self.canvas.img = None
        self.canvas.message = "Draw a line with the cross-section tool (X, or Shift+drag)"
        self.canvas.update()
        self.set_title("Cross Section")
        self.closed.emit()
