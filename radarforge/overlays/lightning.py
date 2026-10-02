"""GOES GLM lightning flashes, drawn over the radar and fading with age."""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data import goes
from ..products.geometry import aeqd_forward

WINDOWS = (5, 10, 15, 30)                  # minutes of flashes to show
# age (fraction of the window) -> colour: new flashes are white-yellow, old ones fade to red
AGE_COLORS = ((0.15, (255, 255, 210), 4.4), (0.45, (255, 225, 40), 4.0), (0.75, (255, 150, 20), 3.6),
              (1.01, (230, 70, 40), 3.2))


def age_bucket(fraction: float) -> int:
    for i, (top, _c, _w) in enumerate(AGE_COLORS):
        if fraction < top:
            return i
    return len(AGE_COLORS) - 1


class LightningOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rf-lightning")
        self._lock = threading.Lock()
        self._files: OrderedDict = OrderedDict()      # file key -> Flashes
        self._index: dict = {}                        # satellite -> {"scans", "start", "end", "listed"}
        self.force = False                            # location alerts need the flashes even when the layer is off
        self._flashes = goes.Flashes.empty()
        self._shown_at = None                         # frame time the flashes belong to
        self._want = None
        self._gen = 0
        self._proj = None                             # (lat0, lon0, x, y) flashes projected for the view
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.refresh())
        self._timer.start(30_000)

    def enabled(self) -> bool:
        return bool(self.settings["overlays"].get("lightning", False))

    def wanted(self) -> bool:
        return self.enabled() or self.force

    def minutes(self) -> int:
        try:
            m = int(self.settings["lightning_minutes"])
        except (TypeError, ValueError):
            m = 10
        return m if m in WINDOWS else 10

    def satellite(self, lon: float) -> str:
        s = self.settings["satellite_sat"]
        return s if s in goes.BUCKETS else goes.satellite_for(lon)

    # ---------------------------------------------------------------- loading
    def set_target(self, t: datetime | None, lat0: float, lon0: float):
        if t is None:
            return
        same = self._want is not None and self._want[0] == t and self._shown_at == t
        self._want = (t, lat0, lon0)
        if not same:
            self.refresh()

    def refresh(self, force=False):
        if not self.wanted() or self._want is None:
            return
        if force:
            self._index.clear()
            self._files.clear()
        t, lat0, lon0 = self._want
        self._gen += 1
        self._pool.submit(self._work, self._gen, t, self.satellite(lon0), self.minutes())

    def _listing(self, sat: str, start: datetime, end: datetime) -> list:
        idx = self._index.get(sat)
        recent = end > datetime.now(timezone.utc) - timedelta(hours=1)
        if idx and idx["start"] <= start and idx["end"] >= end and not (recent and time.monotonic() - idx["listed"] > 25):
            return idx["scans"]
        lo, hi = start - timedelta(minutes=5), end + timedelta(minutes=2)
        scans = goes.list_glm(sat, lo, hi)
        self._index[sat] = {"scans": scans, "start": lo, "end": hi, "listed": time.monotonic()}
        return scans

    def _file(self, scan) -> goes.Flashes:
        with self._lock:
            hit = self._files.get(scan.key)
            if hit is not None:
                self._files.move_to_end(scan.key)
                return hit
        fl = goes.read_glm(goes.fetch_scan(scan))
        with self._lock:
            self._files[scan.key] = fl
            while len(self._files) > 400:
                self._files.popitem(last=False)
        return fl

    def _work(self, gen, t, sat, minutes):
        if gen != self._gen:
            return
        try:
            start = t - timedelta(minutes=minutes)
            scans = [s for s in self._listing(sat, start, t) if start - timedelta(seconds=20) <= s.time <= t]
            parts = []
            for sc in scans:
                if gen != self._gen:
                    return
                parts.append(self._file(sc))
            fl = goes.Flashes.concat(parts)
            keep = (fl.t >= start.timestamp()) & (fl.t <= t.timestamp())
            self._flashes = goes.Flashes(fl.lat[keep], fl.lon[keep], fl.t[keep], fl.energy[keep])
            self._shown_at = t
            self._proj = None
        except Exception as exc:
            self._flashes = goes.Flashes.empty()
            self.status.emit(f"Lightning unavailable: {exc}")
        self.changed.emit()

    def prefetch(self, times: list, lat0: float, lon0: float):
        """Load the lightning files for a loop in the background."""
        if not self.wanted() or not times:
            return
        sat, minutes = self.satellite(lon0), self.minutes()
        lo, hi = min(times) - timedelta(minutes=minutes), max(times)

        def work():
            try:
                for sc in self._listing(sat, lo, hi):
                    self._file(sc)
            except Exception:
                return
        self._pool.submit(work)

    # ---------------------------------------------------------------- drawing
    def _xy(self, view):
        key = (round(view.lat0, 4), round(view.lon0, 4))
        if self._proj is None or self._proj[0] != key:
            x, y = aeqd_forward(self._flashes.lat, self._flashes.lon, view.lat0, view.lon0)
            self._proj = (key, np.asarray(x), np.asarray(y))
        return self._proj[1], self._proj[2]

    def paint(self, painter, vt, panel, view):
        if not self.enabled() or self._shown_at is None:
            return
        fl = self._flashes
        n = 0                                        # flashes in view
        if len(fl):
            x, y = self._xy(view)
            x0, y0, x1, y1 = vt.world_bounds(pad=2.0)
            vis = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
            n = int(vis.sum())
            if n:
                sx, sy = vt.to_screen(x[vis], y[vis])
                frac = (self._shown_at.timestamp() - fl.t[vis]) / (self.minutes() * 60.0)
                bucket = np.array([age_bucket(f) for f in np.clip(frac, 0, 1)]) if len(frac) < 400 else \
                    np.digitize(np.clip(frac, 0, 1), [c[0] for c in AGE_COLORS[:-1]])
                painter.save()
                painter.setBrush(Qt.NoBrush)
                for i in range(len(AGE_COLORS) - 1, -1, -1):             # oldest first, newest on top
                    sel = bucket == i
                    if not sel.any():
                        continue
                    poly = QPolygonF([QPointF(a, b) for a, b in zip(sx[sel].tolist(), sy[sel].tolist())])
                    _top, rgb, width = AGE_COLORS[i]
                    painter.setPen(QPen(QColor(0, 0, 0, 200), width + 2.2, Qt.SolidLine, Qt.RoundCap))
                    painter.drawPoints(poly)
                    painter.setPen(QPen(QColor(*rgb), width, Qt.SolidLine, Qt.RoundCap))
                    painter.drawPoints(poly)
                painter.restore()
        view.draw_caption(painter, vt, f"GLM lightning: {n} flashes in view (last {self.minutes()} min)", QColor(255, 225, 90))

    def hover(self, x, y, tol):
        return None

    # ---------------------------------------------------------------- numbers for other features
    def counts_near(self, lat: float, lon: float, radius_km: float) -> int:
        """Flashes in the current window within radius_km of a point."""
        fl = self._flashes
        if not len(fl):
            return 0
        dy = (fl.lat - lat) * 111.19
        dx = (fl.lon - lon) * 111.19 * math.cos(math.radians(lat))
        return int(np.count_nonzero(dx * dx + dy * dy <= radius_km * radius_km))
