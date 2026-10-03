"""NOAA MRMS swaths (rotation tracks, hail size, rainfall) from the noaa-mrms-pds bucket on AWS.

Keys: CONUS/<Product>/<YYYYMMDD>/MRMS_<Product>_<YYYYMMDD-HHMMSS>.grib2.gz (rotation / hail every
2 minutes, rainfall hourly). The newest file at or before the shown time is drawn, so swaths follow
archive cases too.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import numpy as np

from ..data import aws
from ..products.geometry import aeqd_forward
from . import georaster, grib2
from .bglayer import BackgroundLayer

BUCKET = "noaa-mrms-pds"
KEY_RE = re.compile(r"_(\d{8}-\d{6})\.grib2(\.gz)?$")

# colour stops in display units
ROT_STOPS = [(3.0, (90, 170, 90, 0)), (4.0, (110, 200, 110, 190)), (6.0, (255, 255, 0, 215)),
             (8.0, (255, 140, 0, 230)), (10.0, (255, 0, 0, 240)), (13.0, (255, 0, 210, 250)),
             (16.0, (255, 255, 255, 255))]
MESH_STOPS = [(0.15, (0, 200, 255, 0)), (0.25, (0, 200, 255, 190)), (0.5, (0, 255, 0, 210)),
              (0.75, (255, 255, 0, 225)), (1.0, (255, 150, 0, 235)), (1.5, (255, 0, 0, 245)),
              (2.0, (255, 0, 255, 255)), (3.0, (255, 255, 255, 255))]
QPE_STOPS = [(0.01, (150, 230, 150, 0)), (0.1, (80, 200, 80, 190)), (0.25, (30, 140, 30, 210)),
             (0.5, (255, 255, 0, 225)), (1.0, (255, 150, 0, 235)), (2.0, (255, 0, 0, 245)),
             (3.0, (200, 0, 200, 250)), (5.0, (255, 255, 255, 255))]
LTG_STOPS = [(0.002, (255, 255, 0, 0)), (0.01, (255, 255, 0, 170)), (0.05, (255, 170, 0, 210)),
             (0.1, (255, 80, 0, 230)), (0.3, (255, 0, 0, 245)), (1.0, (255, 255, 255, 255))]

# key -> (menu label, bucket product, kind, minutes covered, cadence minutes)
PRODUCTS = {
    "rot30": ("Rotation track – 30 min", "RotationTrack30min_00.50", "rot", 30, 2),
    "rot60": ("Rotation track – 1 hour", "RotationTrack60min_00.50", "rot", 60, 2),
    "rot120": ("Rotation track – 2 hours", "RotationTrack120min_00.50", "rot", 120, 2),
    "rot240": ("Rotation track – 4 hours", "RotationTrack240min_00.50", "rot", 240, 2),
    "rot1440": ("Rotation track – 24 hours", "RotationTrack1440min_00.50", "rot", 1440, 2),
    "mesh60": ("Hail swath (MESH) – 1 hour", "MESH_Max_60min_00.50", "mesh", 60, 2),
    "mesh240": ("Hail swath (MESH) – 4 hours", "MESH_Max_240min_00.50", "mesh", 240, 2),
    "mesh1440": ("Hail swath (MESH) – 24 hours", "MESH_Max_1440min_00.50", "mesh", 1440, 2),
    "qpe1": ("Rainfall – 1 hour", "MultiSensor_QPE_01H_Pass2_00.00", "qpe", 60, 60),
    "qpe3": ("Rainfall – 3 hours", "MultiSensor_QPE_03H_Pass2_00.00", "qpe", 180, 60),
    "qpe6": ("Rainfall – 6 hours", "MultiSensor_QPE_06H_Pass2_00.00", "qpe", 360, 60),
    "qpe24": ("Rainfall – 24 hours", "MultiSensor_QPE_24H_Pass2_00.00", "qpe", 1440, 60),
    "ltg5": ("Lightning density – 5 min (NLDN)", "NLDN_CG_005min_AvgDensity_00.00", "ltg", 5, 2),
}
UNITS = {"rot": "/s", "mesh": "in", "qpe": "in", "ltg": "flashes/km²/min"}
STOPS = {"rot": ROT_STOPS, "mesh": MESH_STOPS, "qpe": QPE_STOPS, "ltg": LTG_STOPS}
HALF_KM = 600.0
GRID_N = 1000


def key_time(key: str):
    m = KEY_RE.search(key)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def pick_key(keys: list, t: datetime, max_age_min: float):
    """The newest key at or before t (a minute's grace) that isn't older than max_age_min."""
    best, best_t = None, None
    for k in keys:
        kt = key_time(k)
        if kt is None or kt > t + timedelta(minutes=1) or kt < t - timedelta(minutes=max_age_min):
            continue
        if best_t is None or kt > best_t:
            best, best_t = k, kt
    return best, best_t


def find_file(product: str, t: datetime, cadence_min: int):
    """(key, time) of the newest MRMS file for `product` at or before t."""
    window = max(20, cadence_min * 2 + 10)
    keys = []
    for day in sorted({(t - timedelta(minutes=window)).date(), t.date()}):
        prefix = f"CONUS/{product}/{day:%Y%m%d}/"
        after = f"{prefix}MRMS_{product}_{(t - timedelta(minutes=window)):%Y%m%d-%H%M%S}"
        objs, _ = aws.list_objects(BUCKET, prefix, start_after=after if after > prefix else None, max_pages=3)
        keys += [o.key for o in objs]
    return pick_key(keys, t, window)


def to_display(kind: str, v: np.ndarray) -> np.ndarray:
    """MRMS units -> display units (rotation 1e-3/s, hail and rain inches); missing -> NaN."""
    v = np.where(v < 0, np.nan, v).astype(np.float32)             # -999 / -99 / -3: missing or no coverage
    if kind == "rot":
        if np.nanmax(v, initial=0.0) < 0.5:                          # file is in 1/s
            v = v * 1000.0
    elif kind in ("mesh", "qpe"):
        v = v / 25.4
    return v


def project(grid: grib2.GribGrid, lat0, lon0, half_km=HALF_KM, n=GRID_N):
    lat, lon = georaster.aeqd_grid(lat0, lon0, half_km, n)
    return grid.sample(lat, lon)


def build_raster(raw: bytes, kind: str, lat0, lon0, label, t, half_km=HALF_KM, n=GRID_N):
    g = grib2.decode(raw)
    vals = to_display(kind, project(g, lat0, lon0, half_km, n))
    del g
    img = georaster.to_qimage(georaster.colorize(vals, STOPS[kind]))
    return georaster.Raster(img, half_km, (lat0, lon0), t, label, vals)


def fmt_value(kind, v):
    if kind == "rot":
        return f"{v / 1000:.4f} /s"
    if kind == "ltg":
        return f"{v:.3f} flashes/km²/min"
    return f"{v:.2f} in"


class MrmsOverlay(BackgroundLayer):
    """One MRMS product drawn semi-transparent over the radar."""
    title = "MRMS"

    def __init__(self, settings, view, time_fn, parent=None, key_setting="mrms_product", overlay_key="mrms"):
        super().__init__(settings, parent)
        self.view = view
        self.time_fn = time_fn            # -> datetime of the shown frame, or None for "now" (live)
        self.key_setting = key_setting
        self.overlay_key = overlay_key
        self.raster = None
        self._attempted = self._loaded = None

    def enabled(self):
        return bool(self.settings["overlays"].get(self.overlay_key, False))

    def product(self):
        k = self.settings[self.key_setting]
        return k if k in PRODUCTS else "rot60"

    def target(self):
        return (self.time_fn() or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def jobs(self):
        if not self.enabled():
            return []
        key = self.product()
        label, prod, kind, _mins, cadence = PRODUCTS[key]
        live = self.time_fn() is None
        t = self.target()
        center = (self.view.lat0, self.view.lon0)
        want = (key, center, int(t.timestamp() // (cadence * 60)))
        name = f"mrms:{self.overlay_key}"
        if want != self._attempted:
            self._attempted = want
            self._next.pop(name, None)
        if want == self._loaded:
            return []

        def work():
            fk, ft = find_file(prod, t, cadence)
            if fk is None:
                if not live:
                    self.raster = None
                raise RuntimeError(f"no {label.lower()} file near {t:%H:%MZ}")
            r = self.raster
            if r is None or r.fkey != fk or r.center != center:
                ras = build_raster(aws.fetch(BUCKET, fk, cache=False), kind, center[0], center[1], label, ft)
                ras.pkey, ras.fkey, ras.kind = key, fk, kind
                self.raster = ras
            self._loaded = want
        return [(name, work, 30)]

    def on_frame(self):
        """The shown frame changed (archive): fetch if the time moved."""
        if self.enabled():
            self.refresh()

    def paint(self, painter, vt, panel, view):
        r = self.raster
        if not self.enabled() or r is None or r.center != (view.lat0, view.lon0):
            return
        r.paint(painter, vt, float(self.settings["mrms_opacity"] or 0.8), smooth=False)

    def caption(self):
        r = self.raster
        if not self.enabled() or r is None:
            return None
        return f"MRMS {r.label.lower()} · {r.time:%H:%MZ}"

    def hover(self, x, y, tol):
        r = self.raster
        if not self.enabled() or r is None:
            return None
        v = r.value_at(x, y)
        if v is None or v < STOPS[r.kind][1][0]:
            return None
        return f"MRMS {r.label.lower()}: {fmt_value(r.kind, v)}\n(valid {r.time:%H:%MZ})"


def sample_point(raster, lat, lon, lat0, lon0):
    """Grid value of a raster at lat/lon (None outside)."""
    if raster is None:
        return None
    x, y = aeqd_forward(lat, lon, lat0, lon0)
    return raster.value_at(float(x), float(y))
