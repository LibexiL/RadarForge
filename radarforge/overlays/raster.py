"""Base class for map layers that are a picture on the radar's map grid, matched to the radar frame's time
(GOES satellite imagery, MRMS tracks and totals). It finds the picture closest in time, loads it on a worker
thread, keeps the last few, and draws it either under or over the radar."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QImage, QPainter

from ..data.mapgrid import Grid

GRID_HALF_KM = 1200.0           # a picture covers this far each way from the radar
GRID_STEP_KM = 2.0


def rgba_image(rgba: np.ndarray) -> QImage:
    """A QImage from an (h, w, 4) uint8 array (copied, so the array can go)."""
    rgba = np.ascontiguousarray(rgba)
    h, w = rgba.shape[:2]
    return QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


class RasterLayer(QObject):
    changed = Signal()
    status = Signal(str)

    key = ""                    # the name in settings["overlays"]
    below = True                # drawn under the radar (True) or over it (False)
    tolerance_min = 15.0        # a picture further than this from the frame's time isn't shown
    cache_size = 16
    refresh_ms = 90_000

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"rf-{self.key}")
        self._lock = threading.Lock()
        self._index: dict = {}
        self._cache: OrderedDict = OrderedDict()
        self._shown = None                       # (grid, image, scan, (lat, lon), signature)
        self._want = None
        self._gen = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.refresh())
        self._timer.start(self.refresh_ms)

    # ---------------------------------------------------------------- what a layer provides
    def signature(self, lon0: float):
        """What the picture depends on besides the time (channel, product…): a change means a new picture."""
        return ()

    def listing(self, signature, t: datetime) -> list:
        """The pictures around time t (objects with .time and .key), oldest first. Runs on the worker thread."""
        raise NotImplementedError

    def load_grid(self, scan, signature, lat0: float, lon0: float) -> Grid:
        raise NotImplementedError

    def to_image(self, grid: Grid, signature) -> QImage:
        raise NotImplementedError

    def caption(self, scan, signature) -> str:
        return ""

    def describe(self, value: float, signature) -> str:
        return ""

    # ---------------------------------------------------------------- settings
    def enabled(self) -> bool:
        return bool(self.settings["overlays"].get(self.key, False))

    def opacity(self) -> float:
        try:
            return max(0.1, min(1.0, float(self.settings[f"{self.key}_opacity"])))
        except (TypeError, ValueError):
            return 0.8

    # ---------------------------------------------------------------- what to show
    def set_target(self, t: datetime | None, lat0: float, lon0: float):
        """The radar frame's time and position: show the picture closest to it."""
        if t is None:
            return
        want = (t, round(lat0, 4), round(lon0, 4))
        same = want == self._want and self._shown is not None and self._shown[4] == self.signature(lon0)
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
        self._pool.submit(self._work, self._gen, t, lat0, lon0, self.signature(lon0))

    def scans(self, signature, t: datetime) -> list:
        """The listing around t, re-listed when stale (live data keeps arriving)."""
        need0, need1 = t - timedelta(minutes=self.tolerance_min + 30), t + timedelta(minutes=self.tolerance_min)
        idx = self._index.get(signature)
        recent = t > datetime.now(timezone.utc) - timedelta(hours=1)
        if idx and idx["start"] <= need0 and idx["end"] >= need1 and not (recent and time.monotonic() - idx["listed"] > 120):
            return idx["scans"]
        scans = self.listing(signature, t)
        self._index[signature] = {"scans": scans, "start": need0, "end": need1, "listed": time.monotonic()}
        return scans

    def _load(self, scan, signature, lat0: float, lon0: float):
        key = (scan.key, signature, round(lat0, 4), round(lon0, 4))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        grid = self.load_grid(scan, signature, lat0, lon0)
        item = (grid, self.to_image(grid, signature))
        with self._lock:
            self._cache[key] = item
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return item

    def _nearest(self, scans, t):
        best, gap = None, self.tolerance_min * 60.0
        for s in scans:
            d = abs((s.time - t).total_seconds())
            if d <= gap:
                best, gap = s, d
        return best

    def _work(self, gen, t, lat0, lon0, signature):
        if gen != self._gen:
            return                                   # a newer request replaced this one
        try:
            scan = self._nearest(self.scans(signature, t), t)
            if scan is None:
                self._shown = None
                self.status.emit(f"No {self.label} picture within {self.tolerance_min:.0f} minutes of this time")
            else:
                grid, image = self._load(scan, signature, lat0, lon0)
                if gen != self._gen:
                    return
                self._shown = (grid, image, scan, (round(lat0, 4), round(lon0, 4)), signature)
        except Exception as exc:
            self._shown = None
            self.status.emit(f"{self.label} unavailable: {exc}")
        self.changed.emit()

    label = "picture"

    def prefetch(self, times: list, lat0: float, lon0: float):
        """Load the pictures for these radar frame times in the background (so a loop plays smoothly)."""
        if not self.enabled() or not times:
            return
        sig = self.signature(lon0)
        picks = sorted(times)[-24:]

        def work():
            for t in reversed(picks):
                try:
                    scan = self._nearest(self.scans(sig, t), t)
                    if scan is not None:
                        self._load(scan, sig, lat0, lon0)
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
        return self.below and self.enabled() and self._shown is not None

    def _draw(self, painter, vt, s):
        grid, image = s[0], s[1]
        half = grid.half_km
        x0, y0, x1, y1 = vt.world_bounds(pad=0.0)
        # only the part of the picture that is on screen, in picture pixels
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

    def paint_below(self, painter, vt, panel, view):
        s = self.current(view)
        if s is not None and self.below:
            self._draw(painter, vt, s)

    def paint(self, painter, vt, panel, view):
        """Over the radar: the picture itself (for layers drawn on top) and the caption."""
        s = self.current(view)
        if s is None:
            return
        if not self.below:
            self._draw(painter, vt, s)
        text = self.caption(s[2], s[4])
        if text:
            view.draw_caption(painter, vt, text)

    def hover(self, x, y, tol):
        return None

    def readout(self, x_km: float, y_km: float, view) -> str:
        """Text for the cursor readout (empty when there's no picture or no value there)."""
        s = self.current(view)
        if s is None:
            return ""
        v = s[0].sample(x_km, y_km)
        return "" if v is None else self.describe(v, s[4])
