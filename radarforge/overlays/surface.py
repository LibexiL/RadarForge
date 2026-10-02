"""Surface observations (METARs) drawn as station models: sky cover, wind barb, temperature, dewpoint, pressure."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data import metar
from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font

TEMP_COLOR, DEW_COLOR, WIND_COLOR = QColor(255, 110, 100), QColor(110, 220, 130), QColor(235, 238, 245)
FULL_MODELS_BELOW_KM = 900                # wind barbs and all numbers
TEMPERATURE_BELOW_KM = 2600               # temperatures only; beyond this, dots


class SurfaceObsOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, is_live, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.is_live = is_live
        self.obs: list = []
        self.updated = 0.0
        self._centre = None
        self._busy = False
        self._shown: list = []                 # [(ob, (x, y) km)] drawn last, for hovering
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)

    def enabled(self) -> bool:
        return bool(self.settings["overlays"].get("surface", False))

    def set_centre(self, lat0: float, lon0: float):
        """The radar's position: observations are fetched for the area around it."""
        moved = self._centre is None or abs(self._centre[0] - lat0) > 1 or abs(self._centre[1] - lon0) > 1
        self._centre = (lat0, lon0)
        if moved:
            self.refresh(force=True)

    def refresh(self, force=False):
        if self._busy or not self.enabled() or not self.is_live() or self._centre is None:
            return
        if not force and time.time() - self.updated < 280:
            return
        self._busy = True
        lat0, lon0 = self._centre

        def work():
            try:
                self.obs = metar.fetch(lat0, lon0)
                self.updated = time.time()
                self.status.emit(f"Surface observations: {len(self.obs)} stations")
            except Exception as exc:
                self.status.emit(f"Surface observations unavailable: {exc}")
            finally:
                self._busy = False
                self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- drawing
    def _temp(self, c):
        if c is None:
            return None
        return f"{c:.0f}" if self.settings["temp_units"] == "C" else f"{c * 9 / 5 + 32:.0f}"

    def paint(self, painter, vt, panel, view):
        self._shown = []
        if not self.enabled() or not self.is_live() or not self.obs:
            return
        km = vt.km_across
        x0, y0, x1, y1 = vt.world_bounds(pad=20.0 / max(vt.scale, 1e-3))
        positions = {}

        def project(o):
            if o.station not in positions:
                x, y = aeqd_forward(o.lat, o.lon, view.lat0, view.lon0)
                positions[o.station] = (float(x), float(y))
            x, y = positions[o.station]
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                return None
            return vt.to_screen(x, y)
        cell = 70.0 if km < FULL_MODELS_BELOW_KM else 46.0 if km < TEMPERATURE_BELOW_KM else 22.0
        now = datetime.now(timezone.utc)
        font = ui_font(7, True)
        painter.save()
        for o, (sx, sy) in metar.declutter(self.obs, project, cell):
            old = (now - o.time).total_seconds() > 5400
            painter.setOpacity(0.55 if old else 1.0)
            self._shown.append((o, positions[o.station]))
            if km < TEMPERATURE_BELOW_KM:
                self._model(painter, view, o, sx, sy, font, full=km < FULL_MODELS_BELOW_KM)
            else:
                painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
                painter.setBrush(WIND_COLOR)
                painter.drawEllipse(QPointF(sx, sy), 2.6, 2.6)
        painter.restore()

    def _model(self, painter, view, o, sx, sy, font, full):
        r = 4.0
        painter.setPen(QPen(QColor(0, 0, 0, 220), 3.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(sx, sy), r, r)
        painter.setPen(QPen(WIND_COLOR, 1.3))
        painter.drawEllipse(QPointF(sx, sy), r, r)
        sky = o.sky
        if sky:
            painter.setPen(Qt.NoPen)
            painter.setBrush(WIND_COLOR)
            if sky >= 0.99:
                painter.drawEllipse(QPointF(sx, sy), r - 0.6, r - 0.6)
            else:
                painter.drawPie(int(sx - r + 0.6), int(sy - r + 0.6), int(2 * r - 1.2), int(2 * r - 1.2), 90 * 16,
                                -int(sky * 360 * 16))
        if o.wspd is not None and o.wspd < 2:
            painter.setPen(QPen(WIND_COLOR, 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(sx, sy), r + 2.8, r + 2.8)
        elif o.wspd is not None and o.wdir is not None:
            self._barb(painter, sx, sy, r, o.wdir, o.wspd)
        if not full:
            t = self._temp(o.temp_c)
            if t is not None:
                view._halo_text(painter, sx - 6 - len(t) * 5, sy - 5, t, TEMP_COLOR, font)
            return
        t, d = self._temp(o.temp_c), self._temp(o.dew_c)
        if t is not None:
            view._halo_text(painter, sx - 8 - len(t) * 5.2, sy - 4, t, TEMP_COLOR, font)
        if d is not None:
            view._halo_text(painter, sx - 8 - len(d) * 5.2, sy + 9, d, DEW_COLOR, font)
        code = metar.pressure_code(o.slp_hpa)
        if code:
            view._halo_text(painter, sx + 8, sy - 4, code, WIND_COLOR, font)
        if o.wx:
            view._halo_text(painter, sx - 10 - len(o.wx.split()[0]) * 5.2, sy + 3, o.wx.split()[0], QColor(255, 220, 120), font)

    @staticmethod
    def _barb(painter, sx, sy, r, wdir, wspd):
        """A wind barb: the staff points to where the wind comes from; barbs sit on its clockwise side."""
        a = math.radians(wdir)
        sxv, syv = math.sin(a), -math.cos(a)                 # staff direction on screen
        px, py = -syv, sxv                                  # clockwise perpendicular
        length = 21.0
        ex, ey = sx + sxv * length, sy + syv * length
        parts = metar.barb_parts(wspd)
        painter.setBrush(WIND_COLOR)
        for outline, width in ((QColor(0, 0, 0, 220), 3.4), (WIND_COLOR, 1.4)):
            painter.setPen(QPen(outline, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawLine(QPointF(sx + sxv * r, sy + syv * r), QPointF(ex, ey))
            pos = length
            for part in parts:
                bx, by = sx + sxv * pos, sy + syv * pos
                tip = (bx + px * 8.0 * 0.87 + sxv * 4.0, by + py * 8.0 * 0.87 + syv * 4.0)
                if part == "pennant":
                    inner = (sx + sxv * (pos - 5.0), sy + syv * (pos - 5.0))
                    painter.drawPolygon(QPolygonF([QPointF(bx, by), QPointF(*tip), QPointF(*inner)]))
                    pos -= 6.0
                elif part == "full":
                    painter.drawLine(QPointF(bx, by), QPointF(*tip))
                    pos -= 3.6
                else:
                    if pos >= length - 0.1:
                        pos -= 3.6                          # a lone half barb sits in from the end of the staff
                        bx, by = sx + sxv * pos, sy + syv * pos
                    painter.drawLine(QPointF(bx, by), QPointF(bx + (tip[0] - bx) * 0.5, by + (tip[1] - by) * 0.5))

    def hover(self, x, y, tol):
        if not self.enabled() or not self._shown:
            return None
        best, bd = None, tol * 1.6
        for o, (ox, oy) in self._shown:
            d = math.hypot(ox - x, oy - y)
            if d < bd:
                best, bd = o, d
        if best is None:
            return None
        o = best
        wind = "calm" if (o.wspd or 0) < 1 else (f"{o.wdir:.0f}° at {o.wspd:.0f} kt" if o.wdir is not None else f"variable {o.wspd:.0f} kt")
        if o.wgst:
            wind += f", gusts {o.wgst:.0f}"
        lines = [f"{o.station}  {o.name}", f"{o.time:%H:%M}Z", f"Temperature {self._temp(o.temp_c) or '–'}°  Dewpoint {self._temp(o.dew_c) or '–'}°",
                 f"Wind {wind}"]
        if o.vis_mi is not None:
            lines.append(f"Visibility {o.vis_mi:g} mi")
        if o.slp_hpa is not None:
            lines.append(f"Sea level pressure {o.slp_hpa:.1f} hPa")
        if o.wx or o.cover:
            lines.append(" ".join(x for x in (o.cover, o.wx) if x))
        if o.raw:
            lines.append(o.raw[:140])
        return "\n".join(lines)
