"""The Models view: weather-model maps drawn in the radar panels (one field per panel), Pivotal-Weather style.

Each panel shows one field of the chosen model run at the chosen forecast hour. The picture is made for the
part of the map on screen (sharp at any zoom), on worker threads, and swapped in when it's ready.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from datetime import timedelta

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from . import models
from ..products.geometry import aeqd_forward, aeqd_inverse

DEFAULT_FIELDS = ["refc", "cape_sb", "srh01", "stp", "shear06", "td2m"]


class _Relay(QObject):
    done = Signal(object)


class ModelPicture:
    """One panel's finished map: colours over a world-km rectangle, plus isolines and barbs."""

    def __init__(self, key, image, bounds, values, contours, barbs, label, note=""):
        self.key = key
        self.image = image                  # QImage (or None: nothing to colour, e.g. contours only)
        self.bounds = bounds                # x0, y0, x1, y1 km
        self.values = values                # (ny, nx) display values behind the image (read-outs)
        self.contours = contours            # [(level, Nx2 km)]
        self.barbs = barbs                  # [(x km, y km, from-direction deg (screen), kt)]
        self.label = label
        self.note = note

    def value_at(self, x, y):
        if self.values is None:
            return None
        x0, y0, x1, y1 = self.bounds
        ny, nx = self.values.shape
        c = int((x - x0) / (x1 - x0) * nx)
        r = int((y1 - y) / (y1 - y0) * ny)
        if 0 <= r < ny and 0 <= c < nx:
            v = float(self.values[r, c])
            return v if math.isfinite(v) else None
        return None


_IDX_CACHE: "OrderedDict" = OrderedDict()
_IDX_LOCK = threading.Lock()


def _grid_key(g):
    return (g.ni, g.nj, round(g.la1, 4), round(g.lo1, 4), g.scan, None if g.lcc is None else round(g.lcc["dx"], 1))


def _sample_points(mf, lat0, lon0, bounds, nx, ny):
    """Row / column of every pixel's centre in the model grid (cached: every field of a run shares it)."""
    key = (_grid_key(mf.grid), round(lat0, 4), round(lon0, 4), tuple(round(b, 2) for b in bounds), nx, ny)
    with _IDX_LOCK:
        hit = _IDX_CACHE.get(key)
        if hit is not None:
            _IDX_CACHE.move_to_end(key)
            return hit
    x0, y0, x1, y1 = bounds
    xs = x0 + (np.arange(nx) + 0.5) * (x1 - x0) / nx
    ys = y1 - (np.arange(ny) + 0.5) * (y1 - y0) / ny
    X, Y = np.meshgrid(xs, ys)
    lat, lon = aeqd_inverse(X, Y, lat0, lon0)
    rc = mf.grid.index(lat, lon)
    with _IDX_LOCK:
        _IDX_CACHE[key] = rc
        while len(_IDX_CACHE) > 10:
            _IDX_CACHE.popitem(last=False)
    return rc


def _resample(src, grid, rc, bilinear):
    row, col = rc
    nj, ni = src.shape
    global_ll = grid.lcc is None and grid.ni * grid.di >= 359.0
    if not bilinear:
        r = np.rint(row).astype(np.int64)
        c = np.rint(col).astype(np.int64)
        if global_ll:
            c %= ni
        ok = (r >= 0) & (r < nj) & (c >= 0) & (c < ni)
        out = np.full(r.shape, np.nan, np.float32)
        out[ok] = src[r[ok], c[ok]]
        return out
    r0 = np.floor(row).astype(np.int64)
    c0 = np.floor(col).astype(np.int64)
    ok = (r0 >= 0) & (r0 < nj - 1) & (c0 >= 0) & ((c0 < ni - 1) | global_ll)
    out = np.full(r0.shape, np.nan, np.float32)
    fr = (row - r0)[ok].astype(np.float32)
    fc = (col - c0)[ok].astype(np.float32)
    r0, c0 = r0[ok], c0[ok]
    c1 = (c0 + 1) % ni if global_ll else c0 + 1
    a, b, c, d = src[r0, c0], src[r0, c1], src[r0 + 1, c0], src[r0 + 1, c1]
    v = (a * (1 - fc) + b * fc) * (1 - fr) + (c * (1 - fc) + d * fc) * fr
    bad = ~np.isfinite(v)
    if bad.any():
        v[bad] = np.where(fr[bad] < 0.5, np.where(fc[bad] < 0.5, a[bad], b[bad]),
                          np.where(fc[bad] < 0.5, c[bad], d[bad]))
    out[ok] = v
    return out


def make_picture(mf: models.ModelField, lat0, lon0, bounds, px_w, px_h, key):
    """Colours, isolines and barbs of a field for a world rectangle seen at px_w x px_h screen pixels."""
    from . import georaster
    fd = mf.fdef
    native_km = {"hrrr": 3.0, "nam": 3.0, "rap": 13.0, "gfs": 25.0}.get(mf.model, 10.0)
    x0, y0, x1, y1 = bounds
    w_km, h_km = x1 - x0, y1 - y0
    # as sharp as the screen, but no finer than half the model's own grid
    step = max(w_km / max(px_w, 1), native_km / 2.0, w_km / 1800.0, h_km / 1800.0)
    nx, ny = max(8, int(w_km / step)), max(8, int(h_km / step))
    rc = _sample_points(mf, lat0, lon0, bounds, nx, ny)
    vals = _resample(mf.value, mf.grid, rc, bilinear=not fd.categorical)
    rgba = models.colorize(fd, vals)
    image = georaster.to_qimage(rgba)
    contours = []
    if fd.contour is not None and fd.contour[0] in mf.extra:
        cv = _resample(mf.extra[fd.contour[0]], mf.grid, rc, True)
        f = max(1, int(round(min(nx, ny) / 500)))           # isolines don't need every pixel
        cv = cv[::f, ::f]
        contours += _contours(cv, bounds, fd.contour[1])
    if fd.zero_line is not None:
        contours += [("zero", c) for _lev, c in _contours(vals[::2, ::2], bounds, None, level=fd.zero_line)]
    barbs = []
    if fd.barbs is not None and fd.barbs[0] in mf.extra:
        gap_px = 46.0
        bx = max(2, int(px_w / gap_px))
        by = max(2, int(px_h / gap_px))
        xs = x0 + (np.arange(bx) + 0.5) * w_km / bx
        ys = y1 - (np.arange(by) + 0.5) * h_km / by
        X, Y = np.meshgrid(xs, ys)
        lat, lon = aeqd_inverse(X, Y, lat0, lon0)
        u = mf_sample(mf, fd.barbs[0], lat, lon)
        v = mf_sample(mf, fd.barbs[1], lat, lon)
        # which way is north on the map at each barb (the projection turns it a little away from the centre)
        nx_, ny_ = aeqd_forward(np.clip(lat + 0.05, -89.9, 89.9), lon, lat0, lon0)
        north = np.degrees(np.arctan2(nx_ - X, ny_ - Y))
        for i in range(X.size):
            uu, vv = float(u.flat[i]), float(v.flat[i])
            if not (math.isfinite(uu) and math.isfinite(vv)):
                continue
            kt = math.hypot(uu, vv)
            if kt < 2.5:
                continue
            frm = (math.degrees(math.atan2(-uu, -vv)) + float(north.flat[i])) % 360
            barbs.append((float(X.flat[i]), float(Y.flat[i]), frm, kt))
    return ModelPicture(key, image, bounds, vals, contours, barbs, fd.name, mf.note)


def mf_sample(mf, name, lat, lon):
    g = mf.grid
    tmp = models.grib2.GribGrid(values=mf.extra[name], ni=g.ni, nj=g.nj, la1=g.la1, lo1=g.lo1, la2=g.la2,
                                lo2=g.lo2, di=g.di, dj=g.dj, scan=g.scan, lcc=g.lcc)
    return tmp.sample(lat, lon, bilinear=True)


def _contours(v, bounds, interval, level=None):
    from skimage import measure
    ok = np.isfinite(v)
    if ok.sum() < 4:
        return []
    ny, nx = v.shape
    x0, y0, x1, y1 = bounds
    fill = np.where(ok, v, np.nanmean(v))
    if level is not None:
        levels = [level]
    else:
        lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
        if interval <= 0 or (hi - lo) / interval > 200:
            return []
        start = math.ceil(lo / interval) * interval
        levels = list(np.arange(start, hi + 1e-9, interval))
    out = []
    for lev in levels:
        for c in measure.find_contours(fill, lev, mask=ok):
            if len(c) < 4:
                continue
            x = x0 + (c[:, 1] + 0.5) * (x1 - x0) / nx
            y = y1 - (c[:, 0] + 0.5) * (y1 - y0) / ny
            out.append((float(lev), np.stack([x, y], 1)))
    return out


class ModelLayer(QObject):
    """State of the Models view and the pictures in its panels."""
    changed = Signal()
    runsChanged = Signal()
    status = Signal(str)
    busyChanged = Signal(bool)

    def __init__(self, settings, view, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.view = view
        self.active = False
        self.runs = {}                       # model id -> [(run, hours)]
        self.pictures = {}                   # panel index -> ModelPicture
        self.errors = {}                     # panel index -> message
        self._fields = {}                    # (model, run, fh, fid) -> ModelField (a few)
        self._gen = 0
        self._busy = 0
        self._relay = _Relay()
        self._relay.done.connect(self._done, Qt.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.request)
        self._runs_timer = QTimer(self)
        self._runs_timer.timeout.connect(lambda: self.refresh_runs(quiet=True))
        self._runs_timer.start(5 * 60_000)
        self._sem = threading.Semaphore(3)
        self._runs_busy = set()
        self._runs_failed = {}                # model id -> time of the last search that found nothing
        self._runs_error = {}                 # model id -> why

    # ---------------------------------------------------------------- state
    @property
    def model_id(self):
        m = self.settings["model_id"] or "hrrr"
        return m if m in models.MODELS else "hrrr"

    @property
    def model(self):
        return models.MODELS[self.model_id]

    def fields(self):
        f = list(self.settings["model_fields"] or DEFAULT_FIELDS)
        f += DEFAULT_FIELDS[len(f):]
        return [x if x in models.FIELDS else DEFAULT_FIELDS[i] for i, x in enumerate(f[:6])]

    def set_field(self, panel, fid):
        f = self.fields()
        if 0 <= panel < 6 and fid in models.FIELDS:
            f[panel] = fid
            self.settings["model_fields"] = f
            self.settings.save()
            self.pictures.pop(panel, None)
            self.request_soon(0)

    def set_model(self, mid):
        if mid not in models.MODELS or mid == self.model_id:
            return
        self.settings["model_id"] = mid
        self.settings["model_run"] = ""
        self.settings.save()
        self.pictures.clear()
        self.refresh_runs()
        self.changed.emit()

    def run_list(self):
        return self.runs.get(self.model_id, [])

    def run(self):
        """(run time, hours) chosen: the saved one if it's still listed, else the newest."""
        rl = self.run_list()
        want = self.settings["model_run"] or ""
        for r, hrs in rl:
            if want and r.strftime("%Y%m%d%H") == want:
                return r, hrs
        return rl[0] if rl else (None, [])

    def set_run(self, run):
        self.settings["model_run"] = run.strftime("%Y%m%d%H") if run is not None else ""
        self.settings.save()
        self.pictures.clear()
        self._clamp_hour()
        self.changed.emit()
        self.request_soon(0)

    def hours(self):
        return self.run()[1]

    def fh(self):
        hrs = self.hours()
        fh = int(self.settings["model_fh"] or 0)
        if hrs and fh not in hrs:
            fh = min(hrs, key=lambda h: abs(h - fh))
        return fh

    def set_fh(self, fh):
        hrs = self.hours()
        if hrs:
            fh = min(hrs, key=lambda h: abs(h - fh))
        if fh != int(self.settings["model_fh"] or 0):
            self.settings["model_fh"] = int(fh)
            self.changed.emit()
            self.request_soon(0)

    def step(self, d):
        hrs = self.hours()
        if not hrs:
            return
        fh = self.fh()
        i = hrs.index(fh) if fh in hrs else 0
        self.set_fh(hrs[(i + d) % len(hrs)] if d else fh)

    def busy_count(self):
        return self._busy

    def _clamp_hour(self):
        hrs = self.hours()
        if hrs and int(self.settings["model_fh"] or 0) not in hrs:
            self.settings["model_fh"] = hrs[0]

    def valid_time(self):
        r, _h = self.run()
        return r + timedelta(hours=self.fh()) if r else None

    # ---------------------------------------------------------------- runs
    def refresh_runs(self, quiet=False):
        mid = self.model_id
        # one search at a time per model, and after a failed one not again for a minute (no hammering the bucket)
        now = time.time()
        if mid in self._runs_busy or now - self._runs_failed.get(mid, 0.0) < 60:
            return
        self._runs_busy.add(mid)
        if not quiet:
            self.status.emit(f"Looking for the newest {self.model.name} runs…")

        def work():
            try:
                rl = models.latest_runs(models.MODELS[mid], count=4, max_age_s=0 if not quiet else 120)
                self._relay.done.emit(("runs", mid, rl, None))
            except Exception as exc:
                from ..data.aws import friendly_error
                self._relay.done.emit(("runs", mid, [], friendly_error(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- pictures
    def request_soon(self, delay=200):
        self._timer.start(delay)

    def request(self):
        if not self.active:
            return
        r, hrs = self.run()
        if r is None:
            if not self.run_list():
                self.refresh_runs()
            return
        fh = self.fh()
        self._gen += 1
        gen = self._gen
        lat0, lon0 = self.view.lat0, self.view.lon0
        dpr = self.view.devicePixelRatioF()
        for p in self.view.panels:
            fid = self.fields()[p.index]
            vt = self.view.transform(p)
            x0, y0, x1, y1 = vt.world_bounds()
            mx, my = (x1 - x0) * 0.15, (y1 - y0) * 0.15
            bounds = (x0 - mx, y0 - my, x1 + mx, y1 + my)
            px_w, px_h = p.rect.width() * dpr * 1.3, p.rect.height() * dpr * 1.3
            key = (self.model_id, r, fh, fid, lat0, lon0)
            cur = self.pictures.get(p.index)
            if cur is not None and cur.key == key and self._covers(cur.bounds, (x0, y0, x1, y1)) and \
                    (cur.bounds[2] - cur.bounds[0]) / max(x1 - x0, 1e-6) < 2.2:
                continue
            self._start(gen, p.index, key, bounds, px_w, px_h)

    @staticmethod
    def _covers(have, want):
        return have[0] <= want[0] and have[1] <= want[1] and have[2] >= want[2] and have[3] >= want[3]

    def _start(self, gen, panel, key, bounds, px_w, px_h):
        mid, run, fh, fid, lat0, lon0 = key
        self._busy += 1
        if self._busy == 1:
            self.busyChanged.emit(True)

        def work():
            with self._sem:
                if gen != self._gen and panel in self.pictures:
                    self._relay.done.emit(("pic", gen, panel, None, None))
                    return
                try:
                    mf = self._field(mid, run, fh, fid)
                    pic = make_picture(mf, lat0, lon0, bounds, px_w, px_h, key)
                    self._relay.done.emit(("pic", gen, panel, pic, None))
                except models.FieldUnavailable as exc:
                    self._relay.done.emit(("pic", gen, panel, None, str(exc)))
                except FileNotFoundError:
                    self._relay.done.emit(("pic", gen, panel, None, f"hour {fh} isn't on the server yet"))
                except Exception as exc:
                    from ..data.aws import friendly_error
                    self._relay.done.emit(("pic", gen, panel, None, friendly_error(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _field(self, mid, run, fh, fid):
        k = (mid, run, fh, fid)
        mf = self._fields.get(k)
        if mf is None:
            mf = models.compute(mid, run, fh, fid)
            self._fields[k] = mf
            while len(self._fields) > 12:
                self._fields.pop(next(iter(self._fields)))
        return mf

    def prefetch(self, fh):
        """Download (not draw) the next hour's fields while a loop plays."""
        r, _h = self.run()
        if r is None:
            return
        mid = self.model_id
        for fid in {self.fields()[p.index] for p in self.view.panels}:
            def work(fid=fid):
                try:
                    with self._sem:
                        self._field(mid, r, fh, fid)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

    def _done(self, msg):
        if msg[0] == "runs":
            _k, mid, rl, err = msg
            self._runs_busy.discard(mid)
            if rl:
                self.runs[mid] = rl
                self._runs_failed.pop(mid, None)
                self._runs_error.pop(mid, None)
            else:
                self._runs_failed[mid] = time.time()
                if not err:
                    err = "no recent runs found on NOAA's server"
                self._runs_error[mid] = err
                QTimer.singleShot(61_000, lambda: self.active and self.refresh_runs(quiet=True))
            if err and not rl:
                self.status.emit(f"{models.MODELS[mid].name}: {err}")
            elif mid == self.model_id and rl:
                self.status.emit(f"{models.MODELS[mid].name}: newest run {rl[0][0]:%HZ %a %d %b}, "
                                 f"{len(rl[0][1])} hours out so far")
            self._clamp_hour()
            self.runsChanged.emit()
            self.changed.emit()
            if rl:
                self.request_soon(0)
            return
        _k, gen, panel, pic, err = msg
        self._busy = max(0, self._busy - 1)
        if self._busy == 0:
            self.busyChanged.emit(False)
        if pic is not None:
            self.pictures[panel] = pic
            self.errors.pop(panel, None)
        elif err:
            self.errors[panel] = err
            if gen == self._gen:
                self.pictures.pop(panel, None)
        self.changed.emit()

    # ---------------------------------------------------------------- drawing
    def header(self, panel_index):
        fid = self.fields()[panel_index] if panel_index < 6 else "refc"
        fd = models.FIELDS[fid]
        r, _h = self.run()
        if r is None:
            return f"{self.model.name}  {fd.name}"
        v = r + timedelta(hours=self.fh())
        local = v.astimezone()
        return (f"{self.model.name}  {fd.name}  F{self.fh():02d}  ·  {v:%a %HZ} ({local:%-I %p} local)  ·  "
                f"run {r:%HZ %d %b}")

    def message(self, panel_index):
        if panel_index in self.errors:
            return self.errors[panel_index]
        if panel_index not in self.pictures:
            if not self.run_list():
                if self.model_id in self._runs_error and self.model_id not in self._runs_busy:
                    return (f"Couldn't find a recent {self.model.name} run: {self._runs_error[self.model_id]}. "
                            "Trying again every minute.")
                return f"Finding the newest {self.model.name} run…"
            return "Loading…"
        return ""

    def paint_below(self, painter, vt, panel, view):
        pic = self.pictures.get(panel.index)
        if not self.active or pic is None or pic.image is None or pic.key[4:] != (view.lat0, view.lon0):
            return
        x0, y0, x1, y1 = pic.bounds
        sx0, sy0 = vt.to_screen(x0, y1)
        sx1, sy1 = vt.to_screen(x1, y0)
        painter.save()
        painter.setOpacity(float(self.settings["model_opacity"] or 0.9))
        painter.setRenderHint(QPainter.SmoothPixmapTransform, not models.FIELDS[pic.key[3]].categorical)
        painter.drawImage(QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0), pic.image)
        painter.restore()

    def has_below(self):
        return self.active

    def paint(self, painter, vt, panel, view):
        pic = self.pictures.get(panel.index)
        if not self.active or pic is None or pic.key[4:] != (view.lat0, view.lon0):
            return
        fd = models.FIELDS[pic.key[3]]
        if pic.contours:
            col = QColor(*(fd.contour[3] if fd.contour else (20, 20, 20)))
            light = col.lightness() > 128
            halo = QColor(0, 0, 0, 170) if light else QColor(255, 255, 255, 150)
            font = QFont(view.font_small)
            font.setBold(True)
            painter.setFont(font)
            labels = []
            for lev, pts in pic.contours:
                path = QPainterPath()
                sp = [vt.to_screen(float(x), float(y)) for x, y in pts]
                path.moveTo(*sp[0])
                for q in sp[1:]:
                    path.lineTo(*q)
                if lev == "zero":
                    painter.setPen(QPen(QColor(255, 255, 255, 200), 3.4))
                    painter.drawPath(path)
                    painter.setPen(QPen(QColor(30, 60, 200), 1.8, Qt.DashLine))
                    painter.drawPath(path)
                    continue
                painter.setPen(QPen(halo, 2.6))
                painter.drawPath(path)
                painter.setPen(QPen(col, 1.3))
                painter.drawPath(path)
                if len(sp) > 30:
                    labels.append((sp[len(sp) // 2], lev))
            fmt = fd.contour[2] if fd.contour else "{:.0f}"
            taken = []
            for (sx, sy), lev in labels:
                if any(abs(sx - tx) < 60 and abs(sy - ty) < 22 for tx, ty in taken):
                    continue
                if not vt.rect.contains(QPointF(sx, sy)):
                    continue
                taken.append((sx, sy))
                view._halo_text(painter, sx - 10, sy + 4, fmt.format(lev), col if light else QColor(15, 15, 15), font)
        if pic.barbs and float(vt.scale) > 0:
            from .obs import barb_lines
            for x, y, frm, kt in pic.barbs:
                sx, sy = vt.to_screen(x, y)
                if not vt.rect.adjusted(-20, -20, 20, 20).contains(QPointF(sx, sy)):
                    continue
                lines, tris = barb_lines(sx, sy, frm, kt, 20.0)
                painter.setPen(QPen(QColor(255, 255, 255, 170), 2.6, Qt.SolidLine, Qt.RoundCap))
                painter.drawLines(lines)
                painter.setPen(QPen(QColor(15, 15, 15), 1.2, Qt.SolidLine, Qt.RoundCap))
                painter.drawLines(lines)
                painter.setBrush(QColor(15, 15, 15))
                for t in tris:
                    painter.drawPolygon(t)
                painter.setBrush(Qt.NoBrush)

    def value_text(self, panel_index, x, y):
        pic = self.pictures.get(panel_index)
        if pic is None:
            return ""
        v = pic.value_at(x, y)
        fd = models.FIELDS[pic.key[3]]
        return "—" if v is None else models.fmt_value(fd, v)

    def colortable(self, panel_index):
        return models.colortable_for(models.FIELDS[self.fields()[panel_index]])
