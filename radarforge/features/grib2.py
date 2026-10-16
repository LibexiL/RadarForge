"""A small GRIB2 reader: enough for NOAA MRMS grids and the NCEP weather models (HRRR, NAM, RAP, GFS).

Grids: regular latitude/longitude (template 3.0) and Lambert conformal (3.30).
Packing: simple (5.0), complex (5.2), complex with spatial differencing (5.3), PNG (5.41) and
JPEG 2000 (5.40, when Pillow can read it); with or without a bitmap.
No eccodes needed, so the packaged app stays small.
"""
from __future__ import annotations

import gzip
import io
import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np


class GribError(ValueError):
    pass


# --------------------------------------------------------------------------- grids
@dataclass
class GribGrid:
    values: np.ndarray            # float32 (nj, ni), row 0 = first row in the file; NaN where there is no data
    ni: int
    nj: int
    la1: float                    # first grid point (degrees, longitude in -180..180)
    lo1: float
    la2: float = 0.0
    lo2: float = 0.0
    di: float = 0.0               # lat/lon grids: degrees (always positive)
    dj: float = 0.0
    scan: int = 0
    ref_time: datetime | None = None
    discipline: int = 0
    category: int = 0
    number: int = 0
    extra: dict = field(default_factory=dict)
    lcc: dict | None = None       # Lambert conformal: n, F, rho0, lov, R, x1, y1, dx, dy (metres)
    valid_time: datetime | None = None

    # ---------------------------------------------------------------- lat/lon grids
    def lat_of_row(self, j):
        """Latitude of row j (lat/lon grids; respects the scanning direction)."""
        step = self.dj if self.scan & 0x40 else -self.dj
        return self.la1 + step * np.asarray(j)

    def index(self, lat, lon):
        """(row, col) float indices of lat/lon points."""
        if self.lcc is not None:
            x, y = lcc_forward(self.lcc, lat, lon)
            col = (x - self.lcc["x1"]) / self.lcc["dx"]
            row = (y - self.lcc["y1"]) / self.lcc["dy"]
            if not self.scan & 0x40:          # rows run north -> south
                row = -row
            if self.scan & 0x80:
                col = -col
            return row, col
        lat = np.asarray(lat, np.float64)
        lon = (np.asarray(lon, np.float64) - self.lo1 + 540.0) % 360.0 - 180.0   # east of lo1, wrapped
        if self.ni * self.di >= 359.0:        # a global grid: -180..180 east of lo1 -> 0..360
            lon = np.where(lon < 0, lon + 360.0, lon)
        col = -lon / self.di if self.scan & 0x80 else lon / self.di
        row = (lat - self.la1) / self.dj if self.scan & 0x40 else (self.la1 - lat) / self.dj
        return row, col

    def sample(self, lat, lon, bilinear=False):
        """Grid values at lat/lon (NaN outside the grid). Nearest neighbour, or bilinear for smooth fields."""
        row, col = self.index(lat, lon)
        global_ll = self.lcc is None and self.ni * self.di >= 359.0
        if not bilinear:
            r = np.rint(row).astype(np.int64)
            c = np.rint(col).astype(np.int64)
            if global_ll:
                c = c % self.ni
            ok = (r >= 0) & (r < self.nj) & (c >= 0) & (c < self.ni)
            out = np.full(np.shape(r), np.nan, np.float32)
            out[ok] = self.values[r[ok], c[ok]]
            return out
        r0 = np.floor(row).astype(np.int64)
        c0 = np.floor(col).astype(np.int64)
        fr = (row - r0).astype(np.float32)
        fc = (col - c0).astype(np.float32)
        ok = (r0 >= 0) & (r0 < self.nj - 1) & (c0 >= 0) & ((c0 < self.ni - 1) | global_ll)
        out = np.full(np.shape(r0), np.nan, np.float32)
        r0, c0, fr, fc = r0[ok], c0[ok], fr[ok], fc[ok]
        c1 = (c0 + 1) % self.ni if global_ll else c0 + 1
        v = self.values
        a, b, c, d = v[r0, c0], v[r0, c1], v[r0 + 1, c0], v[r0 + 1, c1]
        val = (a * (1 - fc) + b * fc) * (1 - fr) + (c * (1 - fc) + d * fc) * fr
        # next to missing data: fall back to the nearest point
        bad = ~np.isfinite(val)
        if bad.any():
            near = np.where(fr[bad] < 0.5, np.where(fc[bad] < 0.5, a[bad], b[bad]),
                            np.where(fc[bad] < 0.5, c[bad], d[bad]))
            val[bad] = near
        out[ok] = val
        return out

    def latlon(self, rows, cols):
        """lat/lon of (float) grid indices: the inverse of index()."""
        rows = np.asarray(rows, np.float64)
        cols = np.asarray(cols, np.float64)
        if self.lcc is not None:
            p = self.lcc
            sr = rows if self.scan & 0x40 else -rows
            sc = -cols if self.scan & 0x80 else cols
            return lcc_inverse(p, p["x1"] + sc * p["dx"], p["y1"] + sr * p["dy"])
        lat = self.la1 + (rows * self.dj if self.scan & 0x40 else -rows * self.dj)
        lon = self.lo1 + (-cols * self.di if self.scan & 0x80 else cols * self.di)
        return lat, (lon + 540.0) % 360.0 - 180.0

    def bounds(self):
        """(lat_min, lat_max, lon_min, lon_max) of the grid's edge (approximate for Lambert grids)."""
        j = np.r_[np.zeros(self.ni), np.full(self.ni, self.nj - 1), np.arange(self.nj), np.arange(self.nj)]
        i = np.r_[np.arange(self.ni), np.arange(self.ni), np.zeros(self.nj), np.full(self.nj, self.ni - 1)]
        lat, lon = self.latlon(j, i)
        return float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max())


# --------------------------------------------------------------------------- Lambert conformal (sphere)
def lcc_params(latin1, latin2, lov, R):
    p1, p2 = math.radians(latin1), math.radians(latin2)
    if abs(latin1 - latin2) < 1e-9:
        n = math.sin(p1)
    else:
        n = (math.log(math.cos(p1) / math.cos(p2)) /
             math.log(math.tan(math.pi / 4 + p2 / 2) / math.tan(math.pi / 4 + p1 / 2)))
    F = math.cos(p1) * math.tan(math.pi / 4 + p1 / 2) ** n / n
    return dict(n=n, F=F, lov=lov, R=R)


def lcc_forward(p, lat, lon):
    """Metres east / north on the cone (origin: the pole's projection)."""
    lat = np.radians(np.clip(np.asarray(lat, np.float64), -89.9, 89.9))
    dlon = np.radians((np.asarray(lon, np.float64) - p["lov"] + 540.0) % 360.0 - 180.0)
    n = p["n"]
    rho = p["R"] * p["F"] / np.tan(np.pi / 4 + lat / 2) ** n
    th = n * dlon
    return rho * np.sin(th), -rho * np.cos(th)


def lcc_inverse(p, x, y):
    n = p["n"]
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    rho = np.sign(n) * np.hypot(x, y)
    th = np.arctan2(x * np.sign(n), -y * np.sign(n))
    lat = 2 * np.arctan((p["R"] * p["F"] / np.where(rho == 0, 1e-9, rho)) ** (1 / n)) - np.pi / 2
    lon = p["lov"] + np.degrees(th / n)
    return np.degrees(lat), (lon + 540.0) % 360.0 - 180.0


# --------------------------------------------------------------------------- bit helpers
def _u(b, off, n):
    return int.from_bytes(b[off:off + n], "big")


def _s(b, off, n):
    """GRIB2 signed integers are sign and magnitude, not two's complement."""
    v = int.from_bytes(b[off:off + n], "big")
    top = 1 << (8 * n - 1)
    return -(v - top) if v & top else v


def _unpack_bits(data: bytes, nbits: int, count: int) -> np.ndarray:
    if nbits == 0 or count == 0:
        return np.zeros(count, np.uint32)
    if nbits in (8, 16, 32):
        dt = {8: ">u1", 16: ">u2", 32: ">u4"}[nbits]
        return np.frombuffer(data, dt, count).astype(np.uint32)
    return _read_bits(np.frombuffer(data, np.uint8), np.arange(count, dtype=np.int64) * nbits,
                      np.full(count, nbits, np.int64))


def _read_bits(buf: np.ndarray, offsets: np.ndarray, widths: np.ndarray) -> np.ndarray:
    """Unsigned integers of `widths` bits (0..32) starting at bit `offsets` of buf (big-endian)."""
    out = np.zeros(offsets.shape, np.uint32)
    if offsets.size == 0:
        return out
    pad = np.concatenate([buf, np.zeros(8, np.uint8)])
    byte = offsets >> 3
    if byte.size and int(byte.max()) + 5 > pad.size:
        raise GribError("packed data is shorter than expected")
    word = np.zeros(offsets.shape, np.uint64)
    for k in range(5):                           # 40 bits: enough for 32 bits at any bit position
        word = (word << np.uint64(8)) | pad[byte + k].astype(np.uint64)
    shift = (40 - (offsets & 7) - widths).astype(np.uint64)
    mask = ((np.uint64(1) << widths.astype(np.uint64)) - np.uint64(1))
    vals = (word >> shift) & mask
    out[:] = vals.astype(np.uint32)
    out[widths == 0] = 0
    return out


def _image_values(data: bytes, nbits: int) -> np.ndarray:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    a = np.asarray(im)
    if a.ndim == 3:                     # 24 / 32-bit packed into RGB(A) channels
        a = a.astype(np.uint32)
        v = np.zeros(a.shape[:2], np.uint32)
        for k in range(a.shape[2]):
            v = (v << 8) | a[:, :, k]
        a = v
    return a.astype(np.uint32).ravel()


def _complex_unpack(drs: dict, data: bytes, nvals: int):
    """Templates 5.2 / 5.3: returns (int64 values, missing mask) before scaling."""
    buf = np.frombuffer(data, np.uint8)
    ng = drs["ng"]
    pos_bits = 0
    ival1 = ival2 = minsd = 0
    if drs["tmpl"] == 3 and drs["order"] > 0:
        nb = drs["octets"]
        def sval(off):
            v = int.from_bytes(data[off:off + nb], "big")
            top = 1 << (8 * nb - 1)
            return -(v - top) if v & top else v
        off = 0
        ival1 = sval(off); off += nb
        if drs["order"] == 2:
            ival2 = sval(off); off += nb
        minsd = sval(off); off += nb
        pos_bits = off * 8

    def field(count, nbits):
        nonlocal pos_bits
        v = _read_bits(buf, pos_bits + np.arange(count, dtype=np.int64) * nbits, np.full(count, nbits, np.int64))
        pos_bits += count * nbits
        pos_bits = (pos_bits + 7) // 8 * 8          # each group field ends on a byte boundary
        return v.astype(np.int64)

    refs = field(ng, drs["nbits"])
    widths = field(ng, drs["gw_bits"]) + drs["gw_ref"]
    lens = field(ng, drs["gl_bits"]) * drs["gl_inc"] + drs["gl_ref"]
    if ng:
        lens[-1] = drs["gl_last"]
    if int(lens.sum()) != nvals:
        raise GribError(f"complex packing: group lengths add up to {int(lens.sum())}, not {nvals}")
    # value i belongs to group g: its bit offset is the start of g plus (i - first of g) * width of g
    gidx = np.repeat(np.arange(ng), lens)
    gstart_bits = pos_bits + np.concatenate([[0], np.cumsum(lens * widths)[:-1]])
    first = np.concatenate([[0], np.cumsum(lens)[:-1]])
    within = np.arange(nvals, dtype=np.int64) - first[gidx]
    w = widths[gidx]
    raw = _read_bits(buf, gstart_bits[gidx] + within * w, w).astype(np.int64)
    mm = drs["miss_mgmt"]
    missing = np.zeros(nvals, bool)
    if mm in (1, 2):
        maxv = (np.int64(1) << w) - 1
        gref_all = (np.int64(1) << drs["nbits"]) - 1
        zero_w = w == 0
        if mm == 1:
            missing = np.where(zero_w, refs[gidx] == gref_all, raw == maxv)
        else:
            missing = np.where(zero_w, (refs[gidx] == gref_all) | (refs[gidx] == gref_all - 1),
                               (raw == maxv) | (raw == maxv - 1))
    vals = raw + refs[gidx]
    if drs["tmpl"] == 3 and drs["order"] > 0:
        good = np.flatnonzero(~missing)
        d = vals[good]
        if d.size:
            if drs["order"] == 1:
                d[1:] += minsd
                d[0] = ival1
                d = np.cumsum(d)
            else:
                f = np.empty_like(d)
                f[0] = ival1
                if d.size > 1:
                    g = d.copy()
                    g[2:] += minsd
                    g[1] = ival2 - ival1
                    g[0] = 0
                    g = np.cumsum(g[1:])               # first differences f[n] - f[n-1], n >= 1
                    f[1:] = ival1 + np.cumsum(g)
                d = f
            vals[good] = d
    return vals, missing


# --------------------------------------------------------------------------- messages
def messages(raw: bytes):
    """Yields each GRIB2 message (bytes) of a file or byte range (gzip-compressed is fine too)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    pos = 0
    while True:
        start = raw.find(b"GRIB", pos)
        if start < 0 or start + 16 > len(raw):
            return
        if raw[start + 7] != 2:
            raise GribError(f"GRIB edition {raw[start + 7]} is not supported")
        total = int.from_bytes(raw[start + 8:start + 16], "big")
        if total < 16 or start + total > len(raw):
            raise GribError("truncated GRIB message")
        yield raw[start:start + total]
        pos = start + total


def decode_all(raw: bytes) -> list:
    return [decode(m) for m in messages(raw)]


def decode(raw: bytes) -> GribGrid:
    """Decode the first field of a GRIB2 file (gzip-compressed is fine too)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    start = raw.find(b"GRIB")
    if start < 0:
        raise GribError("not a GRIB file")
    b = memoryview(raw)[start:]
    if b[7] != 2:
        raise GribError(f"GRIB edition {b[7]} is not supported")
    discipline = b[6]
    total = _u(b, 8, 8)
    pos = 16
    grid = drs = bitmap = data = None
    ref_time = valid = None
    cat = num = 0
    while pos < total - 4:
        if bytes(b[pos:pos + 4]) == b"7777":
            break
        length = _u(b, pos, 4)
        num_sec = b[pos + 4]
        sec = b[pos:pos + length]
        if length < 5:
            raise GribError("corrupt section")
        if num_sec == 1:
            try:
                ref_time = datetime(_u(sec, 12, 2), sec[14], sec[15], sec[16], sec[17], sec[18], tzinfo=timezone.utc)
            except ValueError:
                ref_time = None
        elif num_sec == 3:
            grid = _grid(sec)
        elif num_sec == 4:
            if length >= 11:
                cat, num = sec[9], sec[10]
            tmpl4 = _u(sec, 7, 2)
            if ref_time is not None and tmpl4 in (0, 1, 8, 11) and length >= 22:
                unit, ft = sec[17], _s(sec, 18, 4)
                mult = {0: 60, 1: 3600, 2: 86400, 10: 3 * 3600, 11: 6 * 3600, 12: 12 * 3600, 13: 1}.get(unit)
                if mult is not None:
                    valid = ref_time + timedelta(seconds=ft * mult)
        elif num_sec == 5:
            drs = _drs(sec)
        elif num_sec == 6:
            ind = sec[5]
            if ind == 0:
                bitmap = bytes(sec[6:])
            elif ind != 255:
                raise GribError("predefined bitmaps are not supported")
        elif num_sec == 7:
            data = bytes(sec[5:])
            break                       # first field only
        pos += length
    if grid is None or drs is None or data is None:
        raise GribError("incomplete GRIB message")
    npts = grid["ni"] * grid["nj"]
    nvals = drs["n"]
    t = drs["tmpl"]
    missing = None
    if t == 0:
        packed = _unpack_bits(data, drs["nbits"], nvals).astype(np.float32)
    elif t in (2, 3):
        if drs["ng"] == 0 or nvals == 0:
            packed = np.zeros(nvals, np.float32)
        else:
            iv, missing = _complex_unpack(drs, data, nvals)
            packed = iv.astype(np.float32)
            del iv
    else:
        packed = np.zeros(nvals, np.uint32) if drs["nbits"] == 0 or not data else _image_values(data, drs["nbits"])
        if packed.size < nvals:
            raise GribError("image smaller than the field")
        packed = packed[:nvals].astype(np.float32)
    vals = packed                                    # float32 throughout: MRMS grids have 24 million points
    if drs["E"]:
        vals *= np.float32(2.0 ** drs["E"])
    vals += np.float32(drs["R"])
    if drs["D"]:
        vals /= np.float32(10.0 ** drs["D"])
    if missing is not None and missing.any():
        vals[missing] = np.nan
    if bitmap is not None:
        mask = np.unpackbits(np.frombuffer(bitmap, np.uint8))[:npts].astype(bool)
        full = np.full(npts, np.nan, np.float32)
        full[mask] = vals[:int(mask.sum())]
        vals = full
    elif vals.size != npts:
        raise GribError("value count mismatch")
    vals = vals.reshape(grid["nj"], grid["ni"])
    if grid["scan"] & 0x20:
        raise GribError("column-major grids are not supported")
    if grid["scan"] & 0x10:              # boustrophedonic rows: every other row runs backwards
        vals[1::2] = vals[1::2, ::-1].copy()
    return GribGrid(values=vals, ref_time=ref_time, valid_time=valid, discipline=discipline, category=cat,
                    number=num, **grid)


def _earth_radius(sec, off):
    """Earth radius (m) from the shape-of-the-earth octets at `off` (spheres; ellipsoids -> mean radius)."""
    shape = sec[off]
    if shape == 0:
        return 6367470.0
    if shape == 1:
        f, v = sec[off + 1], _u(sec, off + 2, 4)
        return v / 10 ** f if v not in (0, 0xFFFFFFFF) else 6371229.0
    if shape == 6:
        return 6371229.0
    if shape == 8:
        return 6371200.0
    return 6371229.0                     # WGS84 / GRS80 and the like: close enough at these scales


def _grid(sec):
    tmpl = _u(sec, 12, 2)
    npts = _u(sec, 6, 4)
    if tmpl == 0:
        ni, nj = _u(sec, 30, 4), _u(sec, 34, 4)
        basic, sub = _u(sec, 38, 4), _u(sec, 42, 4)
        unit = (basic / sub) if basic not in (0, 0xFFFFFFFF) and sub not in (0, 0xFFFFFFFF) else 1e-6
        la1, lo1 = _s(sec, 46, 4) * unit, _u(sec, 50, 4) * unit
        la2, lo2 = _s(sec, 55, 4) * unit, _u(sec, 59, 4) * unit
        di, dj = _u(sec, 63, 4) * unit, _u(sec, 67, 4) * unit
        scan = sec[71]
        if ni * nj != npts:
            raise GribError("grid size mismatch")
        if di <= 0 or di > 360:
            di = abs((lo2 - lo1 + 360.0) % 360.0) / max(ni - 1, 1)
        if dj <= 0 or dj > 180:
            dj = abs(la2 - la1) / max(nj - 1, 1)
        wrap = lambda v: (v + 540.0) % 360.0 - 180.0           # noqa: E731
        return dict(ni=ni, nj=nj, la1=la1, lo1=wrap(lo1), la2=la2, lo2=wrap(lo2), di=di, dj=dj, scan=scan)
    if tmpl == 30:
        R = _earth_radius(sec, 14)
        ni, nj = _u(sec, 30, 4), _u(sec, 34, 4)
        if ni * nj != npts:
            raise GribError("grid size mismatch")
        la1, lo1 = _s(sec, 38, 4) * 1e-6, _u(sec, 42, 4) * 1e-6
        lov = _u(sec, 51, 4) * 1e-6
        dx, dy = _u(sec, 55, 4) * 1e-3, _u(sec, 59, 4) * 1e-3
        scan = sec[64]
        latin1, latin2 = _s(sec, 65, 4) * 1e-6, _s(sec, 69, 4) * 1e-6
        if sec[63] & 0x80:
            raise GribError("south-pole Lambert grids are not supported")
        p = lcc_params(latin1, latin2, (lov + 540.0) % 360.0 - 180.0, R)
        x1, y1 = lcc_forward(p, la1, lo1)
        p.update(x1=float(x1), y1=float(y1), dx=dx, dy=dy, uv_grid=bool(sec[46] & 0x08))
        wrap = lambda v: (v + 540.0) % 360.0 - 180.0           # noqa: E731
        return dict(ni=ni, nj=nj, la1=la1, lo1=wrap(lo1), scan=scan, lcc=p)
    raise GribError(f"grid template 3.{tmpl} is not supported")


def _drs(sec):
    tmpl = _u(sec, 9, 2)
    if tmpl not in (0, 2, 3, 40, 41):
        raise GribError(f"data template 5.{tmpl} is not supported")
    d = dict(n=_u(sec, 5, 4), tmpl=tmpl, R=struct.unpack(">f", bytes(sec[11:15]))[0],
             E=_s(sec, 15, 2), D=_s(sec, 17, 2), nbits=sec[19])
    if tmpl in (2, 3):
        d.update(split=sec[21], miss_mgmt=sec[22], ng=_u(sec, 31, 4), gw_ref=sec[35], gw_bits=sec[36],
                 gl_ref=_u(sec, 37, 4), gl_inc=sec[41], gl_last=_u(sec, 42, 4), gl_bits=sec[46])
        if tmpl == 3:
            d.update(order=sec[47], octets=sec[48])
    return d
