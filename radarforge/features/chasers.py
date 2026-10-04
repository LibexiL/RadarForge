"""Storm chasers: live Spotter Network positions (free for non-commercial use)."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

import requests
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data.aws import friendly_error
from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font
from . import feeds

UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)"}


def chaser_color(t, now) -> tuple:
    """Cyan: updated in the last 15 minutes, amber: within the hour, grey: older."""
    age = (now - t).total_seconds() if t is not None else 1e9
    return (79, 227, 255) if age < 900 else (255, 195, 90) if age < 3600 else (154, 160, 171)


def _age(t, now):
    if t is None:
        return "time not given"
    m = int((now - t).total_seconds() // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h {m % 60} min ago"


class ChasersOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, is_live, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.is_live = is_live            # callable: True while showing live data
        self.chasers: list = []
        self.updated = 0.0
        self._busy = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)         # the feed refreshes every minute

    def enabled(self):
        return bool(self.settings["overlays"].get("chasers", False))

    def refresh(self, force=False):
        if self._busy or not self.enabled() or not self.is_live():
            return
        if not force and time.time() - self.updated < 50:
            return
        self._busy = True
        url = feeds.SN_POSITIONS_ACTIVE if self.settings["chasers_active_only"] else feeds.SN_POSITIONS_ALL

        def work():
            try:
                r = requests.get(url, headers=UA, timeout=20)
                r.raise_for_status()
                self.chasers = feeds.parse_chasers(r.text)
                self.updated = time.time()
                self.status.emit(f"Storm chasers: {len(self.chasers)}")
            except Exception as exc:
                self.status.emit(f"Storm chasers unavailable – {friendly_error(exc)}")
            finally:
                self._busy = False
                self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    def _visible(self):
        return self.enabled() and self.is_live()

    def _xy(self, c, view):
        if c.get("_p") != (view.lat0, view.lon0):
            x, y = aeqd_forward(c["lat"], c["lon"], view.lat0, view.lon0)
            c["xy"] = (float(x), float(y))
            c["_p"] = (view.lat0, view.lon0)
        return c["xy"]

    def paint(self, painter, vt, panel, view):
        if not self._visible() or not self.chasers:
            return
        now = datetime.now(timezone.utc)
        x0, y0, x1, y1 = vt.world_bounds(pad=12.0 / max(vt.scale, 1e-3))
        small = vt.km_across > 1600
        names = bool(self.settings["chaser_names"]) and vt.km_across < 600
        font = ui_font(8, True)
        for c in list(self.chasers):
            x, y = self._xy(c, view)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            sx, sy = vt.to_screen(x, y)
            col = QColor(*chaser_color(c["time"], now))
            h = c["heading"]
            painter.setPen(QPen(QColor(0, 0, 0), 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(col)
            if h is not None and not small:
                a = math.radians(h)

                def pt(ang, ln):
                    return QPointF(sx + math.sin(ang) * ln, sy - math.cos(ang) * ln)
                painter.drawPolygon(QPolygonF([pt(a, 10), pt(a + math.radians(140), 8), pt(a + math.pi, 3.5),
                                               pt(a - math.radians(140), 8)]))
            else:
                r = 3.0 if small else 5.0
                painter.drawEllipse(QPointF(sx, sy), r, r)
            if names:
                view._halo_text(painter, sx + 11, sy + 4, c["label"], QColor(232, 238, 245), font)

    def hover(self, x, y, tol):
        if not self._visible():
            return None
        now = datetime.now(timezone.utc)
        best, bd = None, tol * 1.3
        for c in list(self.chasers):
            if "xy" not in c:
                continue
            d = math.hypot(c["xy"][0] - x, c["xy"][1] - y)
            if d < bd:
                best, bd = c, d
        if best is None:
            return None
        h = best["heading"]
        lines = [f"Storm chaser: {best['name']}",
                 (f"Driving {feeds.compass(h)} ({h:.0f}°)" if h is not None else "Stationary") +
                 f" · position {_age(best['time'], now)}"]
        lines += [f"{k}: {v}" for k, v in best["info"]]
        lines.append("Spotter Network")
        return "\n".join(lines)
