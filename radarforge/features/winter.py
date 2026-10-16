"""Winter weather: precipitation type on the radar, the freezing line, and NOAA's snowfall analysis.

* Precipitation type comes from the HRRR's analysis for the hour of the scan (categorical rain / snow / sleet /
  freezing rain, plus its 2 m temperature where it shows no precipitation). The radar's own reflectivity is then
  coloured by type: the PTYPE product.
* The freezing line is the HRRR's 32 °F isotherm at 2 m for the hour of the scan.
* Snowfall totals are the National Operational Hydrologic Remote Sensing Center's gridded snowfall analysis
  (24 / 48 / 72 hours, updated at 00 and 12 UTC): https://www.nohrsc.noaa.gov/snowfall_v2/
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainterPath, QPen

from . import grib2, models
from .bglayer import BackgroundLayer
from ..products.geometry import aeqd_forward, aeqd_inverse

PTYPE_HALF_KM = 480.0
PTYPE_N = 480                       # 2 km cells around the radar

RAIN, SNOW, SLEET, FRZR = 0, 1, 2, 3
TYPE_NAMES = ("rain", "snow", "sleet", "freezing rain")


# --------------------------------------------------------------------------- HRRR surface analysis
class SurfaceGrid:
    """HRRR precipitation type and 2 m temperature on a square around a radar."""

    def __init__(self, codes, t2f, valid, half_km, center, source):
        self.codes = codes              # (n, n) int8: 0 rain, 1 snow, 2 sleet, 3 freezing rain, -1 none
        self.t2f = t2f                  # (n, n) float32 °F
        self.valid = valid
        self.half_km = half_km
        self.center = center
        self.source = source            # "HRRR 18Z analysis" …

    def lookup(self, x, y):
        """Type codes at world km points (radar-centred): -1 where unknown."""
        n = self.codes.shape[0]
        c = ((np.asarray(x) + self.half_km) / (2 * self.half_km) * n).astype(np.int64)
        r = ((self.half_km - np.asarray(y)) / (2 * self.half_km) * n).astype(np.int64)
        ok = (r >= 0) & (r < n) & (c >= 0) & (c < n)
        out = np.full(np.shape(c), -1, np.int8)
        out[ok] = self.codes[r[ok], c[ok]]
        return out


_GRIDS: "OrderedDict" = OrderedDict()
_LOCK = threading.Lock()
_FAILED: dict = {}
VERSION = [0]                       # bumps when a new grid arrives (the PTYPE images are made again)


def _hours_for(t: datetime):
    """(run, forecast hour) candidates for the hour nearest t: its analysis first, then older runs' forecasts."""
    h = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    return [(h - timedelta(hours=k), k) for k in range(0, 4)]


def surface_grid(lat0, lon0, t: datetime | None, wait=True):
    """The HRRR surface grid for the hour of t (None: now) around (lat0, lon0); None if it can't be had."""
    t = (t or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hour = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    key = (round(lat0, 3), round(lon0, 3), hour)
    with _LOCK:
        g = _GRIDS.get(key)
        if g is not None:
            _GRIDS.move_to_end(key)
            return g
        if time.time() - _FAILED.get(key, 0) < 300:
            return None
    if not wait:
        threading.Thread(target=surface_grid, args=(lat0, lon0, t), daemon=True).start()
        return None
    m = models.MODELS["hrrr"]
    last = None
    for run, fh in _hours_for(t):
        try:
            rows = models.get_idx(m, run, fh)
        except Exception as exc:
            last = exc
            continue
        picks = {}
        for name, var in (("crain", "CRAIN"), ("csnow", "CSNOW"), ("cicep", "CICEP"), ("cfrzr", "CFRZR"),
                          ("t2", "TMP")):
            lev = "2 m above ground" if var == "TMP" else "surface"
            e = models.find_entry(rows, var, lev, "fcst") or models.find_entry(rows, var, lev, "any")
            if e is not None:
                picks[name] = e
        if "t2" not in picks:
            continue
        try:
            grids = {k: models._decoded(m, run, fh, e) for k, e in picks.items()}
        except Exception as exc:
            last = exc
            continue
        g0 = grids["t2"]
        lat, lon = _square(lat0, lon0)
        rc = g0.index(lat, lon)

        def samp(name):
            if name not in grids:
                return None
            r = np.rint(rc[0]).astype(np.int64)
            c = np.rint(rc[1]).astype(np.int64)
            ok = (r >= 0) & (r < g0.nj) & (c >= 0) & (c < g0.ni)
            out = np.full(r.shape, np.nan, np.float32)
            out[ok] = grids[name].values[r[ok], c[ok]]
            return out
        t2 = samp("t2")
        codes = np.where(t2 <= 273.15, SNOW, RAIN).astype(np.int8)        # no model precip: by temperature
        flags = {k: samp(k) for k in ("crain", "csnow", "cicep", "cfrzr")}
        # the model's own types, in rising order of importance (a rain / snow mix shows as snow)
        if flags["crain"] is not None:
            codes = np.where(flags["crain"] > 0.5, RAIN, codes)
        if flags["csnow"] is not None:
            codes = np.where(flags["csnow"] > 0.5, SNOW, codes)
        if flags["cicep"] is not None:
            codes = np.where(flags["cicep"] > 0.5, SLEET, codes)
        if flags["cfrzr"] is not None:
            codes = np.where(flags["cfrzr"] > 0.5, FRZR, codes)
        codes = np.where(np.isfinite(t2), codes, -1).astype(np.int8)
        t2f = ((t2 - 273.15) * 1.8 + 32.0).astype(np.float32)
        label = f"HRRR {run:%HZ}" + (" analysis" if fh == 0 else f" +{fh} h")
        g = SurfaceGrid(codes, t2f, run + timedelta(hours=fh), PTYPE_HALF_KM, (lat0, lon0), label)
        with _LOCK:
            _GRIDS[key] = g
            while len(_GRIDS) > 12:
                _GRIDS.popitem(last=False)
            VERSION[0] += 1
        return g
    with _LOCK:
        _FAILED[key] = time.time()
    if last is not None:
        print("precipitation type:", last)
    return None


def _square(lat0, lon0, half=PTYPE_HALF_KM, n=PTYPE_N):
    step = 2 * half / n
    c = -half + step * (np.arange(n) + 0.5)
    x, y = np.meshgrid(c, c[::-1])
    return aeqd_inverse(x, y, lat0, lon0)


def ptype_values(ref, az_deg, first_gate, gate_km, elev_deg, grid: SurfaceGrid | None):
    """Reflectivity (dBZ) coded by type for the PTYPE colour table: 100 x type + dBZ (5 dBZ and up)."""
    ref = np.asarray(ref, np.float32)
    out = np.full(ref.shape, np.nan, np.float32)
    ok = np.isfinite(ref) & (ref >= 5)
    if grid is None:
        out[ok] = ref[ok]
        return out
    rng = first_gate + np.arange(ref.shape[1]) * gate_km
    s = rng * np.cos(np.radians(elev_deg))
    a = np.radians(np.asarray(az_deg, np.float64))[:, None]
    x = s[None, :] * np.sin(a)
    y = s[None, :] * np.cos(a)
    codes = grid.lookup(x, y).astype(np.float32)
    codes = np.where(codes < 0, 0, codes)
    out[ok] = 100.0 * codes[ok] + np.clip(ref[ok], 5, 99)
    return out


# --------------------------------------------------------------------------- the freezing line
class FreezingLineOverlay(BackgroundLayer):
    """The HRRR's 32 °F line (2 m) for the hour of the scan on screen."""
    title = "Freezing line"

    def __init__(self, settings, view, time_fn, parent=None):
        super().__init__(settings, parent)
        self.view = view
        self.time_fn = time_fn
        self.grid = None
        self.lines = None              # (key, [Nx2 km])

    def enabled(self):
        return bool(self.settings["overlays"].get("freezing_line", False))

    def jobs(self):
        if not self.enabled():
            return []
        lat0, lon0 = self.view.lat0, self.view.lon0
        t = self.time_fn()
        g = self.grid
        hour = (t or datetime.now(timezone.utc)) + timedelta(minutes=30)
        want = (round(lat0, 3), round(lon0, 3), hour.replace(minute=0, second=0, microsecond=0))
        if g is not None and (round(g.center[0], 3), round(g.center[1], 3),
                              g.valid.replace(minute=0, second=0, microsecond=0)) == want:
            return []

        def work():
            g2 = surface_grid(lat0, lon0, t)
            if g2 is None:
                raise RuntimeError("no HRRR temperatures for this hour yet")
            self.grid = g2
            self.lines = None
        return [("frz", work, 600)]

    def on_frame(self):
        if self.enabled():
            self.refresh()

    def _contours(self):
        g = self.grid
        if g is None:
            return []
        key = id(g)
        if self.lines is None or self.lines[0] != key:
            from skimage import measure
            v = np.nan_to_num(g.t2f, nan=60.0)
            n = v.shape[0]
            out = []
            for c in measure.find_contours(v, 32.0):
                if len(c) < 6:
                    continue
                x = -g.half_km + (c[:, 1] + 0.5) * (2 * g.half_km / n)
                y = g.half_km - (c[:, 0] + 0.5) * (2 * g.half_km / n)
                out.append(np.stack([x, y], 1))
            self.lines = (key, out)
        return self.lines[1]

    def paint(self, painter, vt, panel, view):
        if not self.enabled() or self.grid is None or self.grid.center != (view.lat0, view.lon0):
            return
        lines = self._contours()
        font = QFont(view.font_small)
        font.setBold(True)
        for pts in lines:
            path = QPainterPath()
            sx, sy = vt.to_screen(pts[:, 0], pts[:, 1])
            path.moveTo(float(sx[0]), float(sy[0]))
            for a, b in zip(sx[1:].tolist(), sy[1:].tolist()):
                path.lineTo(a, b)
            painter.setPen(QPen(QColor(255, 255, 255, 210), 4.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(40, 90, 255), 2.2, Qt.DashLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
            if len(sx) > 40:
                i = len(sx) // 2
                if vt.rect.contains(QPointF(float(sx[i]), float(sy[i]))):
                    view._halo_text(painter, float(sx[i]) + 4, float(sy[i]) - 4, "32°F", QColor(130, 170, 255), font)

    def caption(self):
        if not self.enabled() or self.grid is None:
            return None
        return f"Freezing line · {self.grid.source}"

    def hover(self, x, y, tol):
        g = self.grid
        if not self.enabled() or g is None:
            return None
        n = g.t2f.shape[0]
        c = int((x + g.half_km) / (2 * g.half_km) * n)
        r = int((g.half_km - y) / (2 * g.half_km) * n)
        if not (0 <= r < n and 0 <= c < n):
            return None
        v = float(g.t2f[r, c])
        if abs(v - 32.0) > 1.0:
            return None
        return f"Freezing line (32 °F at 2 m)\n{g.source}, valid {g.valid:%H:%MZ}"


# --------------------------------------------------------------------------- snowfall analysis (NOHRSC)
NOHRSC = "https://www.nohrsc.noaa.gov/snowfall_v2/data/"
SNOW_FILE = re.compile(r"sfav2_CONUS_(\d+)h_(\d{10})(?:_grid184)?\.grb2")


def snowfall_files(month_html: str, hours: int) -> list:
    """[(valid time, file name)] of an NOHRSC month listing for one accumulation period, oldest first."""
    out = {}
    for m in SNOW_FILE.finditer(month_html):
        if int(m.group(1)) != hours:
            continue
        t = datetime.strptime(m.group(2), "%Y%m%d%H").replace(tzinfo=timezone.utc)
        name = m.group(0)
        if t not in out or "grid184" in name:            # prefer the 2.5 km NDFD-grid version
            out[t] = name
    return sorted(out.items())


class SnowfallOverlay(BackgroundLayer):
    """NOHRSC snowfall over the last 24 / 48 / 72 hours, shaded over the map."""
    title = "Snowfall"

    def __init__(self, settings, view, time_fn, parent=None):
        super().__init__(settings, parent)
        self.view = view
        self.time_fn = time_fn
        self.raster = None
        self._loaded = None

    def enabled(self):
        return bool(self.settings["overlays"].get("snowfall", False))

    def hours(self):
        h = int(self.settings["snowfall_hours"] or 24)
        return h if h in (24, 48, 72) else 24

    def jobs(self):
        if not self.enabled():
            return []
        t = (self.time_fn() or datetime.now(timezone.utc)).astimezone(timezone.utc)
        hours = self.hours()
        lat0, lon0 = self.view.lat0, self.view.lon0
        want = (hours, round(lat0, 3), round(lon0, 3), t.strftime("%Y%m%d%H") if self.time_fn() else "live")
        if want == self._loaded:
            return []

        def work():
            from ..data import aws
            files = []
            for month in sorted({t.strftime("%Y%m"), (t - timedelta(days=4)).strftime("%Y%m")}):
                r = aws.session().get(NOHRSC + month + "/", timeout=30)
                if r.status_code == 200:
                    files += snowfall_files(r.text, hours)
            files = [f for f in sorted(files) if f[0] <= t + timedelta(minutes=30)]
            if not files:
                raise RuntimeError(f"no {hours}-hour snowfall analysis found for {t:%d %b}")
            valid, name = files[-1]
            month = valid.strftime("%Y%m")
            raw = models._cached("nohrsc_" + name, lambda: aws.session().get(f"{NOHRSC}{month}/{name}",
                                                                             timeout=60).content)
            g = grib2.decode(raw)
            from . import georaster
            half, n = 700.0, 700
            lat, lon = georaster.aeqd_grid(lat0, lon0, half, n)
            v = g.sample(lat, lon, bilinear=True)
            if np.nanmax(np.nan_to_num(v, nan=0.0), initial=0.0) < 5.0:        # metres -> inches
                v = v * 39.3701
            else:
                v = v / 2.54 if np.nanmax(v) > 200 else v
            v = np.where(v >= 0.1, v, np.nan).astype(np.float32)
            img = georaster.to_qimage(georaster.colorize(v, models.SNOW_STOPS))
            ras = georaster.Raster(img, half, (lat0, lon0), valid, f"{hours}-hour snowfall", v)
            self.raster = ras
            self._loaded = want
        return [("snow", work, 1800)]

    def on_frame(self):
        if self.enabled():
            self.refresh()

    def paint(self, painter, vt, panel, view):
        r = self.raster
        if not self.enabled() or r is None or r.center != (view.lat0, view.lon0):
            return
        r.paint(painter, vt, 0.75, smooth=True)

    def caption(self):
        r = self.raster
        if not self.enabled() or r is None:
            return None
        return f"Snowfall, {r.label} to {r.time:%d %b %HZ} (NOHRSC)"

    def hover(self, x, y, tol):
        r = self.raster
        if not self.enabled() or r is None:
            return None
        v = r.value_at(x, y)
        if v is None:
            return None
        return f"Snowfall: {v:.1f} in ({r.label} to {r.time:%d %b %HZ}, NOHRSC analysis)"
