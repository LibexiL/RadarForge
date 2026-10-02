"""GOES-R weather satellite data from NOAA's public AWS buckets (no account needed):
ABI cloud and moisture imagery (the CONUS sector, a new picture every 5 minutes) and GLM lightning.

Nothing here draws anything: it lists what exists, downloads it, and turns it into numbers on the same
map grid the radar uses. See overlays/satellite.py and overlays/lightning.py for the drawing.
"""
from __future__ import annotations

import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from ..products.geometry import aeqd_inverse
from . import aws

ABI_PRODUCT = "ABI-L2-CMIPC"          # cloud and moisture imagery, CONUS sector
GLM_PRODUCT = "GLM-L2-LCFA"           # lightning flashes, a file every 20 seconds

# the first bucket that has data wins (GOES-19 replaced GOES-16 as GOES-East in 2025)
BUCKETS = {"east": ("noaa-goes19", "noaa-goes16"), "west": ("noaa-goes18",)}
SATELLITE_NAMES = {"east": "GOES-East", "west": "GOES-West"}

# key: band, name, "bt" (brightness temperature, K) or "refl" (reflectance factor), download size in MB
CHANNELS = {
    "ir": dict(band=13, label="Clean infrared", short="IR", kind="bt", mb=4),
    "wv": dict(band=9, label="Mid-level water vapour", short="Water vapour", kind="bt", mb=4),
    "swir": dict(band=7, label="Shortwave infrared", short="Shortwave IR", kind="bt", mb=4),
    "vis": dict(band=2, label="Visible (65 MB per picture)", short="Visible", kind="refl", mb=65),
}


def satellite_for(lon: float) -> str:
    """Which satellite sees a longitude best: GOES-West over the western states, GOES-East elsewhere."""
    return "west" if lon < -105.0 else "east"


# --------------------------------------------------------------------------- listing
@dataclass(frozen=True)
class Scan:
    time: datetime
    key: str
    bucket: str
    size: int = 0


_KEY_TIME = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_")


def key_time(key: str) -> datetime | None:
    """Start time of a GOES file from its name (…_s20262752101174_… = 2026, day 275, 21:01:17.4)."""
    m = _KEY_TIME.search(key)
    if not m:
        return None
    y, doy, hh, mm, ss, tenth = (int(g) for g in m.groups())
    try:
        return (datetime(y, 1, 1, tzinfo=timezone.utc)
                + timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss + tenth / 10.0))
    except ValueError:
        return None


def _hours(start: datetime, end: datetime):
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    while t <= end:
        yield t
        t += timedelta(hours=1)


def _list(product: str, sat: str, start: datetime, end: datetime, keep) -> list:
    """Scans between start and end from the first bucket of `sat` that has any."""
    for bucket in BUCKETS[sat]:
        scans = []
        for h in _hours(start, end):
            prefix = f"{product}/{h:%Y}/{h.timetuple().tm_yday:03d}/{h:%H}/"
            objs, _ = aws.list_objects(bucket, prefix)
            for o in objs:
                t = key_time(o.key)
                if t is not None and start <= t <= end and keep(o.key):
                    scans.append(Scan(t, o.key, bucket, o.size))
        if scans:
            return sorted(scans, key=lambda s: s.time)
    return []


def list_abi(sat: str, channel: str, start: datetime, end: datetime) -> list:
    """Imagery scans of one channel between two times, oldest first."""
    band = CHANNELS[channel]["band"]
    pat = re.compile(rf"-M\dC{band:02d}_G\d+_s")
    return _list(ABI_PRODUCT, sat, start, end, lambda k: bool(pat.search(k)) and k.endswith(".nc"))


def list_glm(sat: str, start: datetime, end: datetime) -> list:
    """Lightning files (20 seconds each) between two times, oldest first."""
    return _list(GLM_PRODUCT, sat, start, end, lambda k: k.endswith(".nc"))


def nearest(scans: list, t: datetime, tolerance_min: float = 15.0) -> Scan | None:
    """The scan closest to t, or None when none is within the tolerance."""
    best, gap = None, tolerance_min * 60.0
    for s in scans:
        d = abs((s.time - t).total_seconds())
        if d <= gap:
            best, gap = s, d
    return best


# --------------------------------------------------------------------------- the satellite's view of the earth
@dataclass(frozen=True)
class Projection:
    """GOES fixed grid: positions on the picture are angles (radians) seen from the satellite."""
    height: float            # perspective point height above the surface, m
    req: float               # equatorial radius, m
    rpol: float              # polar radius, m
    lon0: float              # sub-satellite longitude, degrees

    @property
    def h(self) -> float:    # distance from the earth's centre to the satellite
        return self.height + self.req


def scan_angles(lat, lon, proj: Projection):
    """(x, y) scan angles in radians for latitudes / longitudes (NaN where the point is beyond the horizon)."""
    lat = np.radians(np.asarray(lat, np.float64))
    lon = np.radians(np.asarray(lon, np.float64))
    lam0 = np.radians(proj.lon0)
    req, rpol, big_h = proj.req, proj.rpol, proj.h
    e2 = (req * req - rpol * rpol) / (req * req)
    phi_c = np.arctan((rpol * rpol) / (req * req) * np.tan(lat))
    rc = rpol / np.sqrt(1.0 - e2 * np.cos(phi_c) ** 2)
    sx = big_h - rc * np.cos(phi_c) * np.cos(lon - lam0)
    sy = -rc * np.cos(phi_c) * np.sin(lon - lam0)
    sz = rc * np.sin(phi_c)
    visible = big_h * (big_h - sx) >= sy * sy + (req * req) / (rpol * rpol) * sz * sz
    with np.errstate(invalid="ignore", divide="ignore"):
        y = np.arctan(sz / sx)
        x = np.arcsin(-sy / np.sqrt(sx * sx + sy * sy + sz * sz))
    return np.where(visible, x, np.nan), np.where(visible, y, np.nan)


@dataclass
class Grid:
    """Picture values on the radar's map grid: north at the top, `step` km per pixel, centred on the radar."""
    values: np.ndarray       # float32 (ny, nx); NaN = no data
    half_km: float
    step_km: float
    time: datetime
    kind: str                # "bt" or "refl"
    units: str

    def sample(self, x_km: float, y_km: float):
        """Value at a point in km east / north of the radar (None outside the picture or without data)."""
        n = self.values.shape[0]
        ix = int(round((x_km + self.half_km) / self.step_km))
        iy = int(round((self.half_km - y_km) / self.step_km))
        if 0 <= ix < self.values.shape[1] and 0 <= iy < n:
            v = float(self.values[iy, ix])
            return None if v != v else v
        return None


_angle_cache: OrderedDict = OrderedDict()
_angle_lock = threading.Lock()


def _grid_angles(proj: Projection, lat0: float, lon0: float, half_km: float, step_km: float):
    """Scan angles of every map pixel (cached: the map grid only changes with the radar)."""
    key = (proj, round(lat0, 4), round(lon0, 4), half_km, step_km)
    with _angle_lock:
        hit = _angle_cache.get(key)
        if hit is not None:
            _angle_cache.move_to_end(key)
            return hit
    n = int(round(2 * half_km / step_km))
    xs = -half_km + (np.arange(n) + 0.5) * step_km
    ys = half_km - (np.arange(n) + 0.5) * step_km
    gx, gy = np.meshgrid(xs, ys)
    lat, lon = aeqd_inverse(gx, gy, lat0, lon0)
    out = scan_angles(lat, lon, proj)
    out = (out[0].astype(np.float32), out[1].astype(np.float32))
    with _angle_lock:
        _angle_cache[key] = out
        while len(_angle_cache) > 3:
            _angle_cache.popitem(last=False)
    return out


def _attr(ds, name, default=None):
    v = ds.attrs.get(name, default)
    if isinstance(v, np.ndarray):
        v = v.ravel()[0] if v.size else default
    return v.decode() if isinstance(v, bytes) else v


def _unsigned(ds) -> bool:
    return str(_attr(ds, "_Unsigned", "false")).lower() == "true"


def _raw(ds, key=Ellipsis):
    """The stored integers of a variable. netCDF marks unsigned data with _Unsigned = "true", which h5py
    doesn't apply, so 16-bit values above 32767 would otherwise come back negative."""
    a = ds[key]
    if _unsigned(ds) and a.dtype.kind == "i":
        return a.view(f"u{a.dtype.itemsize}")
    return a


def _fill(ds):
    v = _attr(ds, "_FillValue")
    if v is None:
        return None
    return int(v) & ((1 << (8 * ds.dtype.itemsize)) - 1) if _unsigned(ds) else float(v)


def open_h5(data):
    """An HDF5 / netCDF4 file from bytes or a path."""
    import h5py
    return h5py.File(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data, "r")


def reproject(data, lat0: float, lon0: float, half_km: float = 1200.0, step_km: float = 2.0) -> Grid:
    """One ABI picture (the bytes or path of a CMIPC file) resampled onto the radar-centred map grid."""
    f = open_h5(data)
    try:
        p = f["goes_imager_projection"]
        proj = Projection(float(_attr(p, "perspective_point_height")), float(_attr(p, "semi_major_axis")),
                          float(_attr(p, "semi_minor_axis")), float(_attr(p, "longitude_of_projection_origin")))
        xv, yv = f["x"], f["y"]
        xs = xv[:].astype(np.float64) * float(_attr(xv, "scale_factor", 1.0)) + float(_attr(xv, "add_offset", 0.0))
        ys = yv[:].astype(np.float64) * float(_attr(yv, "scale_factor", 1.0)) + float(_attr(yv, "add_offset", 0.0))
        ax, ay = _grid_angles(proj, lat0, lon0, half_km, step_km)
        # nearest picture pixel for every map pixel (x grows to the right, y downwards: spacing may be negative)
        dx = (xs[-1] - xs[0]) / (len(xs) - 1)
        dy = (ys[-1] - ys[0]) / (len(ys) - 1)
        with np.errstate(invalid="ignore"):
            ix = np.rint((ax - xs[0]) / dx)
            iy = np.rint((ay - ys[0]) / dy)
        ok = np.isfinite(ix) & np.isfinite(iy) & (ix >= 0) & (ix < len(xs)) & (iy >= 0) & (iy < len(ys))
        values = np.full(ax.shape, np.nan, np.float32)
        if ok.any():
            ixi = ix[ok].astype(np.int64)
            iyi = iy[ok].astype(np.int64)
            x0, x1, y0, y1 = ixi.min(), ixi.max() + 1, iyi.min(), iyi.max() + 1
            cmi = f["CMI"]
            win = _raw(cmi, (slice(y0, y1), slice(x0, x1)))         # only the part of the picture we need
            raw = win[iyi - y0, ixi - x0].astype(np.float32)
            fill = _fill(cmi)
            scale, offset = float(_attr(cmi, "scale_factor", 1.0)), float(_attr(cmi, "add_offset", 0.0))
            good = np.ones(raw.shape, bool) if fill is None else raw != float(fill)
            vals = raw * scale + offset
            vals[~good] = np.nan
            values[ok] = vals
        units = str(_attr(f["CMI"], "units", "K"))
        t0 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(seconds=float(f["t"][()]))
        return Grid(values, half_km, step_km, t0, "bt" if units == "K" else "refl", units)
    finally:
        f.close()


# --------------------------------------------------------------------------- lightning
@dataclass
class Flashes:
    lat: np.ndarray
    lon: np.ndarray
    t: np.ndarray            # seconds since the Unix epoch
    energy: np.ndarray       # femtojoules

    def __len__(self):
        return len(self.lat)

    @staticmethod
    def empty():
        z = np.zeros(0, np.float64)
        return Flashes(z.astype(np.float32), z.astype(np.float32), z, z.astype(np.float32))

    @staticmethod
    def concat(parts: list) -> "Flashes":
        parts = [p for p in parts if len(p)]
        if not parts:
            return Flashes.empty()
        return Flashes(*(np.concatenate([getattr(p, k) for p in parts]) for k in ("lat", "lon", "t", "energy")))


def read_glm(data) -> Flashes:
    """Flashes (position, time, energy) from one GLM-L2-LCFA file; poor-quality flashes are dropped."""
    f = open_h5(data)
    try:
        if "flash_lat" not in f or f["flash_lat"].shape[0] == 0:
            return Flashes.empty()
        lat = f["flash_lat"][:].astype(np.float32)
        lon = f["flash_lon"][:].astype(np.float32)
        tv = f["flash_time_offset_of_first_event"]
        off = _raw(tv).astype(np.float64) * float(_attr(tv, "scale_factor", 1.0)) + float(_attr(tv, "add_offset", 0.0))
        base = datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(seconds=float(f["product_time"][()]))
        t = base.timestamp() + off
        ev = f["flash_energy"]
        raw = _raw(ev).astype(np.float64)
        energy = (raw * float(_attr(ev, "scale_factor", 1.0)) + float(_attr(ev, "add_offset", 0.0))) * 1e15
        energy[raw == (_fill(ev) if _fill(ev) is not None else -1)] = 0.0
        keep = np.isfinite(lat) & np.isfinite(lon)
        if "flash_quality_flag" in f:
            keep &= f["flash_quality_flag"][:] == 0
        return Flashes(lat[keep], lon[keep], t[keep], np.nan_to_num(energy[keep]).astype(np.float32))
    finally:
        f.close()


def fetch_scan(scan: Scan, cache: bool = True) -> bytes:
    return aws.fetch(scan.bucket, scan.key, cache=cache, timeout=180)
