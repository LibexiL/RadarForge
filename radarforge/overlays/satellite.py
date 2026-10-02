"""GOES satellite picture drawn under the radar (cloud tops, water vapour, visible)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QImage, QPainter

from ..data import goes
from ..products import satcolors

TOLERANCE_MIN = 15.0            # a picture older than this (compared with the radar frame) isn't shown
GRID_HALF_KM = 1200.0           # the picture covers this far each way from the radar
GRID_STEP_KM = 2.0              # (the ABI infrared pixels are 2 km; the visible ones are finer but downsampled)


def to_image(grid: goes.Grid, channel: str) -> QImage:
    rgba = np.ascontiguousarray(satcolors.colorize(grid.values, channel))
    h, w = rgba.shape[:2]
    return QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


class SatelliteLayer(QObject):
    """Keeps the satellite picture that matches the radar frame on screen, and draws it below the radar."""
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rf-satellite")
        self._lock = threading.Lock()
        self._index: dict = {}                   # (satellite, channel) -> {"scans", "start", "end", "listed"}
        self._cache: OrderedDict = OrderedDict()  # (file, lat, lon) -> (Grid, QImage)
        self._shown = None                       # (Grid, QImage, scan, (lat, lon), channel, satellite)
        self._want = None                        # (time, lat0, lon0) the window last asked for
        self._gen = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.refresh())
        self._timer.start(90_000)                # new pictures arrive every 5 minutes

    # ---------------------------------------------------------------- settings
    def enabled(self) -> bool:
        return bool(self.settings["overlays"].get("satellite", False))

    def channel(self) -> str:
        c = self.settings["satellite_channel"]
        return c if c in goes.CHANNELS else "ir"

    def satellite(self, lon: float) -> str:
        s = self.settings["satellite_sat"]
        return s if s in goes.BUCKETS else goes.satellite_for(lon)

    def opacity(self) -> float:
        try:
            return max(0.1, min(1.0, float(self.settings["satellite_opacity"])))
        except (TypeError, ValueError):
            return 0.8

    # ---------------------------------------------------------------- what to show
    def set_target(self, t: datetime | None, lat0: float, lon0: float):
        """The radar frame's time and the radar's position: show the picture closest to it."""
        if t is None:
            return
        want = (t, round(lat0, 4), round(lon0, 4))
        same = want == self._want and self._shown is not None and self._shown[4] == self.channel()
        self._want = (t, lat0, lon0)
        if not same:
            self.refresh()

    def refresh(self, force=False):
        if not self.enabled() or self._want is None:
            return
        if force:
            self._index.clear()
        t, lat0, lon0 = self._want
        self._gen += 1
        gen = self._gen
        self._pool.submit(self._work, gen, t, lat0, lon0, self.channel())

    def _scans(self, sat: str, channel: str, t: datetime) -> list:
        """Scans around t (listed on demand and re-listed for live data, which keeps arriving)."""
        need0, need1 = t - timedelta(minutes=TOLERANCE_MIN + 10), t + timedelta(minutes=TOLERANCE_MIN)
        idx = self._index.get((sat, channel))
        recent = t > datetime.now(timezone.utc) - timedelta(hours=1)
        if idx and idx["start"] <= need0 and idx["end"] >= need1 and not (recent and time.monotonic() - idx["listed"] > 120):
            return idx["scans"]
        scans = goes.list_abi(sat, channel, need0 - timedelta(minutes=20), need1)
        self._index[(sat, channel)] = {"scans": scans, "start": need0 - timedelta(minutes=20), "end": need1,
                                       "listed": time.monotonic()}
        return scans

    def _load(self, scan, lat0: float, lon0: float, channel: str):
        key = (scan.key, round(lat0, 4), round(lon0, 4))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        raw = goes.fetch_scan(scan, cache=channel != "vis")        # the 65 MB visible files aren't kept
        grid = goes.reproject(raw, lat0, lon0, GRID_HALF_KM, GRID_STEP_KM)
        item = (grid, to_image(grid, channel))
        with self._lock:
            self._cache[key] = item
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
        return item

    def _work(self, gen, t, lat0, lon0, channel):
        if gen != self._gen:
            return                                   # a newer request replaced this one
        sat = self.satellite(lon0)
        try:
            scan = goes.nearest(self._scans(sat, channel, t), t, TOLERANCE_MIN)
            if scan is None:
                self._shown = None
                self.status.emit("No satellite picture within 15 minutes of this time")
            else:
                grid, image = self._load(scan, lat0, lon0, channel)
                if gen != self._gen:
                    return
                self._shown = (grid, image, scan, (round(lat0, 4), round(lon0, 4)), channel, sat)
        except Exception as exc:
            self._shown = None
            self.status.emit(f"Satellite unavailable: {exc}")
        self.changed.emit()

    def prefetch(self, times: list, lat0: float, lon0: float):
        """Load the pictures for these radar frame times in the background (so a loop plays smoothly)."""
        if not self.enabled() or not times:
            return
        channel = self.channel()
        sat = self.satellite(lon0)
        picks = sorted(times)[-24:]

        def work():
            for t in reversed(picks):
                try:
                    scan = goes.nearest(self._scans(sat, channel, t), t, TOLERANCE_MIN)
                    if scan is not None:
                        self._load(scan, lat0, lon0, channel)
                except Exception:
                    return
        self._pool.submit(work)

    # ---------------------------------------------------------------- drawing
    def current(self, view):
        """The picture to draw for this map projection, or None."""
        s = self._shown
        if not self.enabled() or s is None or s[3] != (round(view.lat0, 4), round(view.lon0, 4)):
            return None
        return s

    def has_below(self) -> bool:
        return self.enabled() and self._shown is not None

    def paint_below(self, painter, vt, panel, view):
        s = self.current(view)
        if s is None:
            return
        grid, image = s[0], s[1]
        half = grid.half_km
        x0, y0, x1, y1 = vt.world_bounds(pad=0.0)
        # the part of the picture that is on screen, in picture pixels
        px0 = max(0.0, (x0 + half) / grid.step_km)
        px1 = min(float(image.width()), (x1 + half) / grid.step_km)
        py0 = max(0.0, (half - y1) / grid.step_km)
        py1 = min(float(image.height()), (half - y0) / grid.step_km)
        if px1 <= px0 or py1 <= py0:
            return
        sx0, sy0 = vt.to_screen(px0 * grid.step_km - half, half - py0 * grid.step_km)
        sx1, sy1 = vt.to_screen(px1 * grid.step_km - half, half - py1 * grid.step_km)
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, vt.scale * grid.step_km < 6)
        painter.setOpacity(self.opacity())
        painter.drawImage(QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0), image, QRectF(px0, py0, px1 - px0, py1 - py0))
        painter.restore()

    def paint(self, painter, vt, panel, view):
        """A small caption: which satellite, channel and time."""
        s = self.current(view)
        if s is None:
            return
        scan, channel, sat = s[2], s[4], s[5]
        view.draw_caption(painter, vt, f"{goes.SATELLITE_NAMES[sat]} {goes.CHANNELS[channel]['short']}  "
                                       f"{scan.time:%H:%M}Z")

    def hover(self, x, y, tol):
        return None

    def readout(self, x_km: float, y_km: float, view) -> str:
        """'IR -62 °C' for the cursor position (empty when there's no picture there)."""
        s = self.current(view)
        if s is None:
            return ""
        v = s[0].sample(x_km, y_km)
        return "" if v is None else f"{goes.CHANNELS[s[4]]['short']}: {satcolors.describe(v, s[4])}"

