"""Model sounding window: Skew-T, hodograph and severe-weather numbers for a point on the map."""
from __future__ import annotations

import math
import threading

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QSplitter,
                               QVBoxLayout, QWidget)

from ..features import sounding as snd

P_BOT, P_TOP = 1050.0, 100.0
HODO_COLORS = [(1000, QColor(255, 60, 255)), (3000, QColor(255, 70, 70)), (6000, QColor(70, 220, 70)),
               (9000, QColor(255, 230, 60)), (99999, QColor(80, 220, 255))]


def _fmt(v, nd=0, unit=""):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    return f"{v:.{nd}f}{unit}"


class SkewT(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.prof = None
        self.parcel = None
        self.setMinimumSize(420, 460)

    def set_profile(self, prof, parcel=None):
        self.prof, self.parcel = prof, parcel
        self.update()

    def _frame(self):
        return QRectF(44, 10, self.width() - 44 - 46, self.height() - 34)

    def _xy(self, t, p, r):
        yf = (math.log(P_BOT) - np.log(p)) / (math.log(P_BOT) - math.log(P_TOP))
        x = r.left() + r.width() * ((np.asarray(t) + 40.0) / 90.0 + yf * 0.75)
        y = r.bottom() - r.height() * yf
        return x, y

    def _path(self, t, p, r):
        x, y = self._xy(t, p, r)
        path = QPainterPath()
        for i, (a, b) in enumerate(zip(np.atleast_1d(x).tolist(), np.atleast_1d(y).tolist())):
            path.lineTo(a, b) if i else path.moveTo(a, b)
        return path

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.fillRect(self.rect(), QColor(18, 20, 24))
        r = self._frame()
        qp.save()
        qp.setClipRect(r)
        ps = np.exp(np.linspace(math.log(P_BOT), math.log(P_TOP), 60))
        qp.setPen(QPen(QColor(120, 90, 60, 90), 1))                   # dry adiabats
        for th in range(-30, 220, 10):
            tk = (th + 273.15) * (ps / 1000.0) ** 0.2857 - 273.15
            qp.drawPath(self._path(tk, ps, r))
        for t in range(-120, 60, 10):                                  # isotherms
            qp.setPen(QPen(QColor(80, 160, 255, 200) if t == 0 else QColor(90, 90, 100, 120), 1.4 if t == 0 else 1))
            qp.drawPath(self._path(np.full(2, float(t)), np.array([P_BOT, P_TOP]), r))
        qp.restore()
        qp.setPen(QPen(QColor(110, 110, 120), 1))
        font = QFont(self.font())
        font.setPointSizeF(8)
        qp.setFont(font)
        for p in (1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100):
            _x, y = self._xy(0.0, p, r)
            qp.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            qp.drawText(QRectF(0, y - 8, 40, 16), Qt.AlignRight | Qt.AlignVCenter, str(p))
        for t in range(-30, 50, 10):
            x, _y = self._xy(float(t), P_BOT, r)
            if r.left() <= x <= r.right():
                qp.drawText(QRectF(x - 15, r.bottom() + 2, 30, 14), Qt.AlignCenter, f"{t}°")
        qp.setPen(QPen(QColor(150, 150, 160), 1))
        qp.setBrush(Qt.NoBrush)
        qp.drawRect(r)
        prof = self.prof
        if prof is None:
            qp.setPen(QColor(200, 200, 200))
            qp.drawText(r, Qt.AlignCenter, "Loading…")
            return
        qp.save()
        qp.setClipRect(r)
        if self.parcel is not None and len(self.parcel) == len(prof.p):
            qp.setPen(QPen(QColor(240, 240, 240, 200), 1.6, Qt.DashLine))
            qp.drawPath(self._path(self.parcel, prof.p, r))
        qp.setPen(QPen(QColor(60, 220, 90), 2.4))
        qp.drawPath(self._path(prof.td, prof.p, r))
        qp.setPen(QPen(QColor(255, 70, 70), 2.4))
        qp.drawPath(self._path(prof.t, prof.p, r))
        qp.restore()
        # heights above ground
        qp.setPen(QColor(255, 210, 90))
        for km in (1, 3, 6, 9, 12):
            if km * 1000 > prof.hagl[-1]:
                break
            p = float(np.exp(np.interp(km * 1000.0, prof.hagl, np.log(prof.p))))
            _x, y = self._xy(0.0, p, r)
            qp.drawLine(QPointF(r.left(), y), QPointF(r.left() + 8, y))
            qp.drawText(QPointF(r.left() + 10, y + 4), f"{km} km")
        # wind barbs down the right side
        from ..features.obs import barb_lines
        bx = r.right() + 24
        last_y = -99
        for i in range(len(prof.p)):
            if prof.p[i] < P_TOP:
                break
            _x, y = self._xy(0.0, prof.p[i], r)
            if abs(y - last_y) < 16:
                continue
            last_y = y
            if prof.sknt[i] < 2.5:
                qp.setPen(QPen(QColor(220, 220, 220), 1))
                qp.drawEllipse(QPointF(bx, y), 2.5, 2.5)
                continue
            lines, tris = barb_lines(bx, y, float(prof.drct[i]), float(prof.sknt[i]), 18.0)
            qp.setPen(QPen(QColor(220, 220, 220), 1.2))
            qp.drawLines(lines)
            qp.setBrush(QColor(220, 220, 220))
            for t in tris:
                qp.drawPolygon(t)


class Hodograph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.prof = None
        self.marks = {}
        self.setMinimumSize(260, 260)

    def set_profile(self, prof, marks):
        self.prof, self.marks = prof, marks
        self.update()

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.fillRect(self.rect(), QColor(18, 20, 24))
        side = min(self.width(), self.height()) - 16
        c = QPointF(self.width() / 2, self.height() / 2)
        prof = self.prof
        vmax = 40.0
        if prof is not None:
            u, v = prof.uv()
            sel = prof.hagl <= 10000
            vmax = max(40.0, math.ceil(float(np.hypot(u[sel], v[sel]).max(initial=0)) / 10.0) * 10.0 + 10)
        k = side / 2 / vmax
        font = QFont(self.font())
        font.setPointSizeF(7.5)
        qp.setFont(font)
        for ring in range(10, int(vmax) + 1, 10):
            qp.setPen(QPen(QColor(90, 90, 100, 160), 1, Qt.SolidLine if ring % 20 == 0 else Qt.DotLine))
            qp.drawEllipse(c, ring * k, ring * k)
            if ring % 20 == 0:
                qp.setPen(QColor(130, 130, 140))
                qp.drawText(QPointF(c.x() + ring * k * 0.71 + 2, c.y() + ring * k * 0.71 + 10), f"{ring}")
        qp.setPen(QPen(QColor(110, 110, 120), 1))
        qp.drawLine(QPointF(c.x() - side / 2, c.y()), QPointF(c.x() + side / 2, c.y()))
        qp.drawLine(QPointF(c.x(), c.y() - side / 2), QPointF(c.x(), c.y() + side / 2))
        if prof is None:
            return
        h = np.arange(0, min(12000.0, float(prof.hagl[-1])) + 1, 250.0)
        uu = np.interp(h, prof.hagl, u)
        vv = np.interp(h, prof.hagl, v)
        for i in range(1, len(h)):
            col = next(cc for top, cc in HODO_COLORS if h[i] <= top)
            qp.setPen(QPen(col, 2.6, Qt.SolidLine, Qt.RoundCap))
            qp.drawLine(QPointF(c.x() + uu[i - 1] * k, c.y() - vv[i - 1] * k),
                        QPointF(c.x() + uu[i] * k, c.y() - vv[i] * k))
        for label, (mu, mv), col in ((n, m[0], m[1]) for n, m in self.marks.items()):
            pt = QPointF(c.x() + mu * k, c.y() - mv * k)
            qp.setPen(QPen(QColor(0, 0, 0), 1))
            qp.setBrush(col)
            qp.drawEllipse(pt, 4.5, 4.5)
            qp.setPen(col)
            qp.drawText(QPointF(pt.x() + 6, pt.y() - 5), label)
        qp.setPen(QColor(170, 170, 180))
        qp.drawText(QRectF(4, 2, self.width() - 8, 14), Qt.AlignLeft, "Hodograph (kt)  0–1 / 1–3 / 3–6 / 6–9 km")


class _Relay(QObject):
    done = Signal(object, object)       # profiles, error


class SoundingDialog(QDialog):
    def __init__(self, main, lat, lon, model="RAP"):
        super().__init__(main)
        self.main = main
        self.lat, self.lon = lat, lon
        self.profiles = []
        self.cur = None
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"Model sounding – {abs(lat):.2f}°{'N' if lat >= 0 else 'S'} "
                            f"{abs(lon):.2f}°{'W' if lon < 0 else 'E'}")
        self.resize(1080, 640)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Model"))
        self.model = QComboBox()
        self.model.addItems(list(snd.MODELS))
        self.model.setCurrentText(model)
        self.model.currentTextChanged.connect(self.load)
        top.addWidget(self.model)
        top.addSpacing(12)
        top.addWidget(QLabel("Forecast hour"))
        self.hour = QSlider(Qt.Horizontal)
        self.hour.setMinimumWidth(220)
        self.hour.valueChanged.connect(self._show)
        top.addWidget(self.hour)
        self.hour_lbl = QLabel("")
        self.hour_lbl.setMinimumWidth(220)
        top.addWidget(self.hour_lbl)
        top.addStretch(1)
        self.use_btn = QPushButton("Use right-mover as storm motion")
        self.use_btn.setToolTip("Sets the storm motion (used by storm-relative velocity and the track tool) to the "
                                "Bunkers right-mover from this sounding")
        self.use_btn.clicked.connect(self._use_motion)
        self.use_btn.setEnabled(False)
        top.addWidget(self.use_btn)
        lay.addLayout(top)
        split = QSplitter(Qt.Horizontal)
        self.skewt = SkewT()
        split.addWidget(self.skewt)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.hodo = Hodograph()
        rl.addWidget(self.hodo, 3)
        self.table = QLabel("")
        self.table.setTextFormat(Qt.RichText)
        self.table.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.table.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(self.table, 2)
        split.addWidget(right)
        split.setSizes([620, 440])
        lay.addWidget(split, 1)
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.relay = _Relay(self)            # goes away with the window, so a late answer is dropped
        self.relay.done.connect(self._loaded)
        self.load()

    def load(self, *_):
        model = self.model.currentText()
        self.status.setText(f"Downloading the {model} sounding from the Iowa Environmental Mesonet…")
        self.use_btn.setEnabled(False)
        lat, lon = self.lat, self.lon
        when = self._archive_time()

        relay = self.relay

        def work():
            try:
                res, err = snd.fetch(lat, lon, model, when), None
            except Exception as exc:
                res, err = None, exc
            try:
                relay.done.emit(res, err)
            except RuntimeError:
                pass                              # the window was closed meanwhile
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, profiles, err):
        if err is not None:
            self.status.setText(f"⚠ Couldn't get the sounding: {err}")
            return
        self.profiles = profiles
        self.hour.blockSignals(True)
        self.hour.setRange(0, len(profiles) - 1)
        self.hour.setValue(self._nearest_index())
        self.hour.blockSignals(False)
        p0 = profiles[0]
        past = " (archived, for the frame's time)" if self._archive_time() is not None else ""
        self.status.setText(f"{p0.model} point forecast from station {p0.station or '?'}"
                            + (f", run {p0.run}" if p0.run else "") + past + " – via the Iowa Environmental Mesonet")
        self._show()

    def _archive_time(self):
        """The frame's time when looking at a past case (older than 3 hours), else None (latest run)."""
        from datetime import datetime, timezone
        f = self.main.current_frame()
        t = getattr(f, "time", None)
        if t is None or (datetime.now(timezone.utc) - t).total_seconds() < 3 * 3600:
            return None
        return t

    def _nearest_index(self):
        """The forecast hour closest to the radar frame shown (or now)."""
        from datetime import datetime, timezone
        f = self.main.current_frame()
        t = getattr(f, "time", None) or datetime.now(timezone.utc)
        best, bd = 0, None
        for i, p in enumerate(self.profiles):
            if p.time is None:
                continue
            d = abs((p.time - t).total_seconds())
            if bd is None or d < bd:
                best, bd = i, d
        return best

    def _show(self, *_):
        if not self.profiles:
            return
        prof = self.profiles[self.hour.value()]
        self.cur = prof
        ix = snd.indices(prof)
        self.ix = ix
        when = f"{prof.time:%a %H:%MZ}" if prof.time else ""
        self.hour_lbl.setText(f"F{prof.fhour:02d}  valid {when}")
        self.skewt.set_profile(prof, ix.get("parcel"))
        sm = self._storm_motion_uv()
        marks = {"RM": (ix["right"], QColor(255, 90, 90)), "LM": (ix["left"], QColor(90, 160, 255)),
                 "Mean": (ix["mean"], QColor(200, 160, 110))}
        if sm is not None:
            marks["SM"] = (sm, QColor(255, 255, 255))
        self.hodo.set_profile(prof, marks)
        rd, rs = snd.motion_from(*ix["right"])
        rows = [
            ("CAPE (surface / mixed / most unstable)",
             f"{_fmt(ix['sbcape'])} / {_fmt(ix['mlcape'])} / {_fmt(ix['mucape'])} J/kg"),
            ("CIN (surface / mixed)", f"{_fmt(ix['sbcin'])} / {_fmt(ix['mlcin'])} J/kg"),
            ("Cloud base (LCL)", _fmt(ix["lcl_m"], 0, " m")),
            ("Freezing level", _fmt(ix["freezing_m"], 0, " m AGL")),
            ("700–500 mb lapse rate", _fmt(ix["lr75"], 1, " °C/km")),
            ("Precipitable water", _fmt(ix["pwat_in"], 2, " in")),
            ("Bulk shear 0–1 / 0–3 / 0–6 km",
             f"{_fmt(ix['shear01'])} / {_fmt(ix['shear03'])} / {_fmt(ix['shear06'])} kt"),
            ("Helicity 0–1 / 0–3 km (right-mover)", f"{_fmt(ix['srh01'])} / {_fmt(ix['srh03'])} m²/s²"),
            ("Bunkers right-mover", f"from {rd:.0f}° at {rs:.0f} kt"),
            ("Significant tornado parameter", _fmt(ix["stp"], 1)),
        ]
        html = "<table cellspacing='0' cellpadding='2'>" + "".join(
            f"<tr><td style='padding-right:14px;color:#aab'>{a}</td><td><b>{b}</b></td></tr>" for a, b in rows)
        html += "</table><p style='color:#889'>Model forecast, not an observation. STP is a rough guide (fixed layer).</p>"
        self.table.setText(html)
        self.use_btn.setEnabled(True)

    def _storm_motion_uv(self):
        s = self.main.settings
        try:
            d, k = float(s["storm_motion_dir"]), float(s["storm_motion_kts"])
        except (TypeError, ValueError):
            return None
        a = math.radians(d)
        return -k * math.sin(a), -k * math.cos(a)

    def _use_motion(self):
        if self.cur is None:
            return
        d, k = snd.motion_from(*self.ix["right"])
        if hasattr(self.main, "set_storm_motion"):
            self.main.set_storm_motion(d, k, "the sounding (Bunkers right-mover)")
        self.status.setText(f"Storm motion set to {d:.0f}° at {k:.0f} kt (Bunkers right-mover)")
        self._show()
