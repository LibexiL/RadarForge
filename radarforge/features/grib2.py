"""A small GRIB2 reader: enough for NOAA MRMS grids.

Supports one regular latitude/longitude grid (template 3.0) per message with simple (5.0),
PNG (5.41) or JPEG 2000 (5.40, when Pillow can read it) packing, and an optional bitmap.
No eccodes needed, so the packaged app stays small.
"""
from __future__ import annotations

import gzip
import io
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np


class GribError(ValueError):
    pass


@dataclass
class GribGrid:
    values: np.ndarray            # float32 (nj, ni), row 0 = first row in the file; NaN where the bitmap says none
    ni: int
    nj: int
    la1: float                    # first grid point (degrees, longitude in -180..180)
    lo1: float
    la2: float
    lo2: float
    di: float                     # degrees (always positive)
    dj: float
    scan: int
    ref_time: datetime | None
    discipline: int = 0
    category: int = 0
    number: int = 0
    extra: dict = field(default_factory=dict)

    def lat_of_row(self, j):
        """Latitude of row j (respects the scanning direction)."""
        step = self.dj if self.scan & 0x40 else -self.dj
        return self.la1 + step * np.asarray(j)

    def index(self, lat, lon):
        """(row, col) float indices of lat/lon points (nearest neighbour = round them)."""
        lat = np.asarray(lat, np.float64)
        lon = (np.asarray(lon, np.float64) - self.lo1 + 540.0) % 360.0 - 180.0   # east of lo1, wrapped
        if self.scan & 0x80:
            col = -lon / self.di
        else:
            col = lon / self.di
        row = (lat - self.la1) / self.dj if self.scan & 0x40 else (self.la1 - lat) / self.dj
        return row, col

    def sample(self, lat, lon):
        """Nearest grid values at lat/lon (NaN outside the grid)."""
        row, col = self.index(lat, lon)
        r = np.rint(row).astype(np.int64)
        c = np.rint(col).astype(np.int64)
        ok = (r >= 0) & (r < self.nj) & (c >= 0) & (c < self.ni)
        out = np.full(np.shape(r), np.nan, np.float32)
        out[ok] = self.values[r[ok], c[ok]]
        return out


def _u(b, off, n):
    return int.from_bytes(b[off:off + n], "big")


def _s(b, off, n):
    """GRIB2 signed integers are sign and magnitude, not two's complement."""
    v = int.from_bytes(b[off:off + n], "big")
    top = 1 << (8 * n - 1)
    return -(v - top) if v & top else v


def _unpack_bits(data: bytes, nbits: int, count: int) -> np.ndarray:
    if nbits == 0:
        return np.zeros(count, np.uint32)
    if nbits in (8, 16, 32):
        dt = {8: ">u1", 16: ">u2", 32: ">u4"}[nbits]
        return np.frombuffer(data, dt, count).astype(np.uint32)
    bits = np.unpackbits(np.frombuffer(data, np.uint8))[:count * nbits].reshape(count, nbits)
    weights = (1 << np.arange(nbits - 1, -1, -1, dtype=np.uint64))
    return (bits.astype(np.uint64) @ weights).astype(np.uint32)


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
    ref_time = None
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
            tmpl = _u(sec, 12, 2)
            if tmpl != 0:
                raise GribError(f"grid template 3.{tmpl} is not supported")
            npts = _u(sec, 6, 4)
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
            grid = dict(ni=ni, nj=nj, la1=la1, lo1=wrap(lo1), la2=la2, lo2=wrap(lo2), di=di, dj=dj, scan=scan)
        elif num_sec == 4:
            if length >= 11:
                cat, num = sec[9], sec[10]
        elif num_sec == 5:
            tmpl = _u(sec, 9, 2)
            if tmpl not in (0, 40, 41):
                raise GribError(f"data template 5.{tmpl} is not supported")
            drs = dict(n=_u(sec, 5, 4), tmpl=tmpl, R=struct.unpack(">f", bytes(sec[11:15]))[0],
                       E=_s(sec, 15, 2), D=_s(sec, 17, 2), nbits=sec[19])
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
    if drs["tmpl"] == 0:
        packed = _unpack_bits(data, drs["nbits"], nvals)
    else:
        packed = np.zeros(nvals, np.uint32) if drs["nbits"] == 0 or not data else _image_values(data, drs["nbits"])
        if packed.size < nvals:
            raise GribError("image smaller than the field")
        packed = packed[:nvals]
    vals = packed.astype(np.float32)                 # float32 throughout: MRMS grids have 24 million points
    del packed
    if drs["E"]:
        vals *= np.float32(2.0 ** drs["E"])
    vals += np.float32(drs["R"])
    if drs["D"]:
        vals /= np.float32(10.0 ** drs["D"])
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
    return GribGrid(values=vals, ref_time=ref_time, discipline=discipline, category=cat, number=num, **grid)
