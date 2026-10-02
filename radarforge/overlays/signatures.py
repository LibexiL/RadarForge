"""Automatic signature flags on the map: strong rotation, possible debris and ZDR columns (services/detect.py)."""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data.sites import get_site
from ..render.fonts import ui_font
from ..services import detect

LEVEL_COLORS = {1: QColor(255, 220, 70), 2: QColor(255, 140, 40), 3: QColor(255, 60, 60)}
DEBRIS_COLOR, ZDR_COLOR = QColor(255, 70, 220), QColor(80, 220, 255)
LABELS = {"ROT": "ROT", "DEBRIS": "DEBRIS?", "ZDRCOL": "ZDR COL"}
HALF_KM = 150.0
STEP_KM = 1.0


class SignatureOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, engine, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.engine = engine
        self.flags: list = []
        self.info = ""                                   # "KTLX 0.5° 19:55Z"
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rf-signatures")
        self._gen = 0
        self._done_key = None
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        return bool(self.settings["overlays"].get("signatures", False))

    def which(self) -> tuple:
        w = self.settings["signature_kinds"] or {}
        return tuple(k for k in ("ROT", "DEBRIS", "ZDRCOL") if w.get(k, True))

    # ---------------------------------------------------------------- analysis
    def analyse(self, frame, tilt_elev: float):
        """Looks for signatures in a frame's Level II volume (on a worker thread). Cheap to call repeatedly."""
        if not self.enabled() or frame is None or not frame.has_level2():
            return
        key = (frame.uid, frame.l2_rev, round(tilt_elev, 1), self.which(), self.settings["freezing_level_ft"])
        if key == self._done_key:
            return
        self._gen += 1
        gen = self._gen
        self._pool.submit(self._work, gen, frame, tilt_elev, key)

    def _work(self, gen, frame, tilt_elev, key):
        if gen != self._gen:
            return
        try:
            eng = self.engine
            tilts = eng.tilts(frame)
            if not tilts:
                return
            import numpy as np
            elevs = np.array([t.elevation for t in tilts])
            ti = int(np.argmin(np.abs(elevs - tilt_elev)))
            want = self.which()
            rot_wanted = "ROT" in want or "DEBRIS" in want
            azsh = self._grid(eng.image(frame, "AZSH", ti), "max") if rot_wanted else None
            azsh_up = self._grid(eng.image(frame, "AZSH", ti + 1), "max") if rot_wanted and ti + 1 < len(tilts) else None
            ref = self._grid(eng.image(frame, "REF", ti), "max") if rot_wanted else None
            cc = self._grid(eng.image(frame, "CC", ti), "min") if "DEBRIS" in want else None
            layers = []
            if "ZDRCOL" in want:
                for i, t in enumerate(tilts):
                    if t.elevation > 12.0 or len(layers) >= 9:
                        continue
                    z, r, c = eng.image(frame, "ZDR", i), eng.image(frame, "REF", i), eng.image(frame, "CC", i)
                    if z is not None and r is not None:
                        layers.append((t.elevation, self._grid(z, "max"), self._grid(r, "max"),
                                       None if c is None else self._grid(c, "min")))
                    if gen != self._gen:
                        return
            site = get_site(frame.site)
            fz = detect.freezing_km_arl(float(self.settings["freezing_level_ft"]), site.elev_ft if site else 0.0)
            flags = detect.all_flags(azsh, ref, cc, layers, HALF_KM, STEP_KM, fz, want, azsh_up)
            if gen != self._gen:
                return
            with self._lock:
                self.flags = flags
                self.info = f"{frame.site} {tilts[ti].label} {frame.time:%H:%M}Z"
                self._done_key = key
        except Exception as exc:
            self.status.emit(f"Signature flags unavailable: {exc}")
            return
        self.changed.emit()

    @staticmethod
    def _grid(img, reduce="nearest"):
        return None if img is None else detect.to_grid(img, HALF_KM, STEP_KM, reduce)

    # ---------------------------------------------------------------- drawing
    def _color(self, f):
        return DEBRIS_COLOR if f.kind == "DEBRIS" else ZDR_COLOR if f.kind == "ZDRCOL" else LEVEL_COLORS[f.level]

    def paint(self, painter, vt, panel, view):
        if not self.enabled() or not self.flags:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=30.0 / max(vt.scale, 1e-3))
        font = ui_font(7, True)
        for f in list(self.flags):
            if not (x0 <= f.x <= x1 and y0 <= f.y <= y1):
                continue
            sx, sy = vt.to_screen(f.x, f.y)
            col = self._color(f)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 230), 4.6))
            self._shape(painter, f, sx, sy)
            painter.setPen(QPen(col, 2.2))
            self._shape(painter, f, sx, sy)
            view._halo_text(painter, sx + 13, sy + 4, LABELS[f.kind] + (f" {f.value:.0f}" if f.kind == "ROT" else ""), col, font)

    @staticmethod
    def _shape(painter, f, sx, sy):
        if f.kind == "ROT":
            painter.drawEllipse(QPointF(sx, sy), 9.0, 9.0)
            painter.drawArc(int(sx - 4), int(sy - 4), 8, 8, 40 * 16, 270 * 16)       # a curl: it turns
        elif f.kind == "DEBRIS":
            painter.drawPolygon(QPolygonF([QPointF(sx, sy - 10), QPointF(sx + 10, sy), QPointF(sx, sy + 10),
                                           QPointF(sx - 10, sy)]))
        else:
            painter.drawPolygon(QPolygonF([QPointF(sx, sy - 10), QPointF(sx + 9, sy + 7), QPointF(sx - 9, sy + 7)]))

    def hover(self, x, y, tol):
        if not self.enabled():
            return None
        best, bd = None, tol * 1.8 + 6.0
        for f in self.flags:
            d = math.hypot(f.x - x, f.y - y)
            if d < bd:
                best, bd = f, d
        if best is None:
            return None
        return f"{best.text}\n{best.detail}\n(automatic flag from {self.info}; a hint, not a warning)"
