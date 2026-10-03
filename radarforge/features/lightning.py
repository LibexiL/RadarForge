"""Lightning: GOES GLM flashes (every 20 s, from the noaa-goes19 / noaa-goes18 buckets on AWS) as
age-coloured marks, plus an optional NLDN cloud-to-ground density map from MRMS.

GLM keys: GLM-L2-LCFA/<YYYY>/<DDD>/<HH>/OR_GLM-L2-LCFA_G19_s<YYYYDDDHHMMSSt>_e..._c....nc
"""
from __future__ import annotations

import io
import math
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import numpy as np
from PySide6.QtCore import QLineF
from PySide6.QtGui import QColor, QPen

from ..data import aws
from ..products.geometry import aeqd_forward
from .bglayer import BackgroundLayer

GLM_PRODUCT = "GLM-L2-LCFA"
START_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)")
AGE_COLORS = [(0.1, (255, 255, 255)), (0.25, (255, 255, 0)), (0.5, (255, 170, 0)), (0.75, (255, 60, 0)),
              (1.01, (170, 0, 0))]
KEEP_KM = 700.0


def glm_bucket(lon: float) -> str:
    return "noaa-goes18" if lon < -105.0 else "noaa-goes19"


def glm_start(key: str):
    m = START_RE.search(key)
    if not m:
        return None
    y, doy, hh, mm, ss, tenth = (int(g) for g in m.groups())
    return (datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss,
                                                                 milliseconds=100 * tenth))


def glm_prefixes(start: datetime, end: datetime) -> list:
    out, t = [], start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        out.append(f"{GLM_PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/")
        t += timedelta(hours=1)
    return out


def _var(f, name):
    """A netCDF variable with its packing (scale, offset, unsigned, fill) applied."""
    ds = f[name]
    raw = ds[()]
    a = np.asarray(raw)
    attrs = ds.attrs

    def attr(k):
        v = attrs.get(k)
        if v is None:
            return None
        v = np.asarray(v).ravel()
        return v[0] if v.size else None
    uns = attr("_Unsigned")
    if isinstance(uns, bytes):
        uns = uns.decode()
    if str(uns).lower() == "true" and a.dtype.kind == "i":
        a = a.view(a.dtype.str.replace("i", "u"))
    fill = attr("_FillValue")
    out = a.astype(np.float64)
    if fill is not None:
        if str(uns).lower() == "true" and np.asarray(fill).dtype.kind == "i":
            fill = np.asarray(fill).view(np.asarray(fill).dtype.str.replace("i", "u"))
        out[a == fill] = np.nan
    sf, off = attr("scale_factor"), attr("add_offset")
    if sf is not None:
        out = out * float(sf)
    if off is not None:
        out = out + float(off)
    return out


def parse_glm(raw: bytes):
    """(lat, lon, energy) arrays of the flashes in one GLM LCFA file."""
    try:
        import h5py
    except ImportError:
        raise RuntimeError("reading lightning files needs the h5py package – run the installer again "
                           "(or: pip install h5py)") from None
    with h5py.File(io.BytesIO(raw), "r") as f:
        if "flash_lat" not in f:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        lat = _var(f, "flash_lat")
        lon = _var(f, "flash_lon")
        en = _var(f, "flash_energy") if "flash_energy" in f else np.zeros_like(lat)
    ok = np.isfinite(lat) & np.isfinite(lon)
    return lat[ok], lon[ok], en[ok]


def age_color(frac: float):
    for lim, rgb in AGE_COLORS:
        if frac <= lim:
            return rgb
    return AGE_COLORS[-1][1]


class FlashStore:
    """Flashes per GLM file, kept for a while so each file is downloaded once."""

    def __init__(self, cap=400):
        self.files: OrderedDict = OrderedDict()      # key -> (time, lat, lon)
        self.cap = cap
        self.lock = threading.Lock()

    def has(self, key):
        with self.lock:
            return key in self.files

    def add(self, key, t, lat, lon):
        with self.lock:
            self.files[key] = (t, lat, lon)
            while len(self.files) > self.cap:
                self.files.popitem(last=False)

    def window(self, start, end, bucket=None):
        """(times, lat, lon) of flashes from files starting in [start, end]."""
        with self.lock:
            parts = [(t, la, lo) for k, (t, la, lo) in self.files.items()
                     if start <= t <= end and (bucket is None or k.startswith(bucket))]
        if not parts:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        times = np.concatenate([np.full(len(la), t.timestamp()) for t, la, _ in parts])
        return times, np.concatenate([p[1] for p in parts]), np.concatenate([p[2] for p in parts])


class LightningOverlay(BackgroundLayer):
    title = "Lightning"

    def __init__(self, settings, view, time_fn, need_fn=None, parent=None):
        super().__init__(settings, parent, tick_ms=15_000)
        self.view = view
        self.time_fn = time_fn            # shown frame time, or None when live
        self.need_fn = need_fn or (lambda: False)     # alerts want flashes even with the layer off
        self.store = FlashStore()
        self._xy = None                   # (center, end, times, x, y) projected for drawing
        self.last_ok = None

    def enabled(self):
        return bool(self.settings["overlays"].get("lightning", False))

    def minutes(self):
        try:
            return max(1, int(self.settings["lightning_minutes"] or 10))
        except (TypeError, ValueError):
            return 10

    def end_time(self):
        return (self.time_fn() or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def jobs(self):
        live = self.time_fn() is None
        if not (self.enabled() or (live and self.need_fn())):
            return []
        end = self.end_time()
        mins = max(self.minutes(), 15 if self.need_fn() else 0)
        start = end - timedelta(minutes=mins)
        bucket = glm_bucket(self.view.lon0)
        lat0, lon0 = self.view.lat0, self.view.lon0
        want = (bucket, int(end.timestamp() // 60), mins)
        if want != getattr(self, "_attempted", None):
            self._attempted = want
            self._next.pop("glm", None)

        def work():
            keys = []
            for prefix in glm_prefixes(start - timedelta(seconds=30), end):
                objs, _ = aws.list_objects(bucket, prefix, max_pages=2)
                keys += [o.key for o in objs]
            todo = []
            for k in keys:
                t = glm_start(k)
                if t is not None and start <= t <= end and not self.store.has(bucket + "/" + k):
                    todo.append((t, k))
            for t, k in sorted(todo, reverse=True)[:100]:         # newest first; at most ~30 minutes
                lat, lon, _en = parse_glm(aws.fetch(bucket, k, cache=not live))
                x, y = aeqd_forward(lat, lon, lat0, lon0)
                keep = np.hypot(x, y) < KEEP_KM
                self.store.add(bucket + "/" + k, t, lat[keep], lon[keep])
            self.last_ok = datetime.now(timezone.utc)
            self._xy = None
        return [("glm", work, 60 if live else 3600)]

    def flashes(self, minutes=None, end=None):
        """(times epoch s, lat, lon) in the last `minutes` before `end` (default: the shown time)."""
        end = end or self.end_time()
        start = end - timedelta(minutes=minutes or self.minutes())
        return self.store.window(start, end, glm_bucket(self.view.lon0))

    def flashes_near(self, lat, lon, miles, minutes):
        """Flashes within `miles` of a point in the last `minutes`, nearest distance (mi) and newest time."""
        times, la, lo = self.flashes(minutes, datetime.now(timezone.utc))
        if len(la) == 0:
            return 0, None, None
        x, y = aeqd_forward(la, lo, lat, lon)
        d = np.hypot(x, y) / 1.609344
        hit = d <= miles
        if not hit.any():
            return 0, None, None
        return int(hit.sum()), float(d[hit].min()), datetime.fromtimestamp(float(times[hit].max()), timezone.utc)

    def _projected(self, view):
        end = self.end_time()
        key = ((view.lat0, view.lon0), int(end.timestamp() // 20), self.minutes(), len(self.store.files))
        if self._xy is None or self._xy[0] != key:
            times, la, lo = self.flashes()
            x, y = aeqd_forward(la, lo, view.lat0, view.lon0) if len(la) else (np.zeros(0), np.zeros(0))
            self._xy = (key, end.timestamp(), times, np.asarray(x), np.asarray(y))
        return self._xy

    def paint(self, painter, vt, panel, view):
        if not self.enabled():
            return
        _k, end, times, x, y = self._projected(view)
        if len(x) == 0:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=5)
        sel = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
        if not sel.any():
            return
        span = self.minutes() * 60.0
        age = np.clip((end - times[sel]) / span, 0, 1)
        sx, sy = vt.to_screen(x[sel], y[sel])
        r = 3.5 if vt.scale > 1.5 else 2.5
        order = np.argsort(-age)                       # newest drawn last (on top)
        buckets = {}
        for i in order.tolist():
            buckets.setdefault(age_color(float(age[i])), []).append(i)
        for rgb in [c for _l, c in reversed(AGE_COLORS)]:
            idx = buckets.get(rgb)
            if not idx:
                continue
            lines = []
            for i in idx:
                cx, cy = float(sx[i]), float(sy[i])
                lines.append(QLineF(cx - r, cy, cx + r, cy))
                lines.append(QLineF(cx, cy - r, cx, cy + r))
            painter.setPen(QPen(QColor(0, 0, 0, 170), 3.0))
            painter.drawLines(lines)
            painter.setPen(QPen(QColor(*rgb), 1.4))
            painter.drawLines(lines)

    def caption(self):
        if not self.enabled():
            return None
        times = self._xy[2] if self._xy else []
        return f"GLM lightning · last {self.minutes()} min · {len(times)} flashes"

    def hover(self, x, y, tol):
        if not self.enabled() or self._xy is None:
            return None
        _k, end, times, fx, fy = self._xy
        if len(fx) == 0:
            return None
        d = np.hypot(fx - x, fy - y)
        near = d < max(tol * 1.2, 1.0)
        if not near.any():
            return None
        ages = (end - times[near]) / 60.0
        n = int(near.sum())
        return (f"Lightning (GOES GLM): {n} flash{'es' if n > 1 else ''} here\n"
                f"Newest {math.floor(ages.min())} min before the shown time")
