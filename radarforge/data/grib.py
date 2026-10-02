"""A small GRIB2 reader (pure numpy; Pillow for PNG / JPEG 2000), enough for the model and MRMS data RadarForge uses.

Why not eccodes or pygrib: neither can be installed with pip on Windows. This reads:
  grids      3.0  latitude / longitude          (MRMS)
             3.30 Lambert conformal             (HRRR, RAP, NAM)
  packing    5.0  simple                        5.3  complex with spatial differencing (HRRR)
             5.41 PNG (MRMS)                    5.40 JPEG 2000
Anything else raises Grib2Error, so a new kind of file fails loudly instead of showing wrong numbers.
Checked against ECMWF's eccodes on real HRRR and MRMS files (values identical to float precision).
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

import numpy as np


class Grib2Error(ValueError):
    pass


def _sm(b: bytes) -> int:
    """GRIB2 signed integers are sign-and-magnitude (top bit = sign), not two's complement."""
    n = int.from_bytes(b, "big")
    top = 1 << (8 * len(b) - 1)
    return -(n & (top - 1)) if n & top else n


@dataclass
class Grid:
    template: int
    ni: int
    nj: int
    la1: float
    lo1: float
    di: float = 0.0                 # degrees (lat/lon grids) or metres (Lambert)
    dj: float = 0.0
    j_positive: bool = False        # first row is the southern one
    la2: float = 0.0
    lo2: float = 0.0
    lov: float = 0.0                # Lambert: orientation longitude
    lad: float = 0.0
    latin1: float = 0.0
    latin2: float = 0.0
    radius: float = 6371229.0
    _proj: object = field(default=None, repr=False)

    def ij(self, lat, lon):
        """Fractional grid indices (i along a row, j along a column) of latitudes / longitudes (arrays)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        if self.template == 0:
            lo = (lon - self.lo1) % 360.0
            i = lo / self.di
            j = (lat - self.la1) / self.dj if self.j_positive else (self.la1 - lat) / self.dj
            return i, j
        if self._proj is None:
            from pyproj import Proj
            self._proj = Proj(proj="lcc", lat_1=self.latin1, lat_2=self.latin2, lat_0=self.lad, lon_0=self.lov,
                              a=self.radius, b=self.radius)
            self._origin = self._proj(self.lo1, self.la1)
        x, y = self._proj(lon, lat)
        i = (np.asarray(x) - self._origin[0]) / self.di
        j = (np.asarray(y) - self._origin[1]) / self.dj
        if not self.j_positive:
            j = -j
        return i, j

    def latlon(self):
        """Latitude and longitude of every grid point, shape (nj, ni) (used by tests and small grids)."""
        jj, ii = np.mgrid[0:self.nj, 0:self.ni]
        if self.template == 0:
            lat = self.la1 + (jj if self.j_positive else -jj) * self.dj
            lon = (self.lo1 + ii * self.di + 180.0) % 360.0 - 180.0
            return lat, lon
        self.ij(self.la1, self.lo1)
        x = self._origin[0] + ii * self.di
        y = self._origin[1] + (jj if self.j_positive else -jj) * self.dj
        lon, lat = self._proj(x, y, inverse=True)
        return lat, lon


@dataclass
class Message:
    discipline: int
    category: int
    number: int
    level_type: int
    level_value: float
    reference_time: tuple             # (year, month, day, hour, minute, second)
    forecast_time: int
    grid: Grid
    _rep: dict = field(repr=False, default_factory=dict)
    _data: bytes = field(repr=False, default=b"")
    _bitmap: bytes | None = field(repr=False, default=None)

    @property
    def shape(self):
        return self.grid.nj, self.grid.ni

    # ---------------------------------------------------------------- decoding
    def raw(self) -> np.ndarray:
        """The stored integers, flat, one per grid point (bitmap-masked points hold -1)."""
        rep = self._rep
        t = rep["template"]
        n = rep["npoints"]
        if t == 0:
            x = _simple(self._data, rep, n)
        elif t == 3 or t == 2:
            x = _complex(self._data, rep, n)
        elif t == 41:
            x = _image(self._data, "PNG")
        elif t == 40:
            x = _image(self._data, "JPEG2000")
        else:
            raise Grib2Error(f"data representation template 5.{t} is not supported")
        if self._bitmap is not None:
            present = np.unpackbits(np.frombuffer(self._bitmap, np.uint8))[:self.grid.ni * self.grid.nj].astype(bool)
            full = np.full(len(present), -1, np.int64)
            full[present] = x[:int(present.sum())]
            x = full
        if len(x) < self.grid.ni * self.grid.nj:
            raise Grib2Error("the message holds fewer values than the grid has points")
        return x[:self.grid.ni * self.grid.nj]

    def scale(self, x):
        """Stored integers -> physical values: (reference + x * 2**E) / 10**D."""
        r = self._rep
        out = (r["ref"] + np.asarray(x, np.float64) * (2.0 ** r["E"])) / (10.0 ** r["D"])
        return out.astype(np.float32)

    def values(self) -> np.ndarray:
        """The whole field as float32, shape (nj, ni), NaN where the bitmap says there's no value.
        Row 0 is the first row of the file (see Grid.j_positive for which way that points)."""
        x = self.raw()
        v = self.scale(x)
        if self._bitmap is not None:
            v[x < 0] = np.nan
        return v.reshape(self.grid.nj, self.grid.ni)

    def sample(self, lat, lon, method="linear") -> np.ndarray:
        """Field values at latitudes / longitudes (arrays or scalars). NaN outside the grid. Only the points
        asked for are converted to floating point, so a 14000 x 7000 MRMS field stays small in memory."""
        g = self.grid
        i, j = g.ij(lat, lon)
        scalar = np.ndim(i) == 0
        i, j = np.atleast_1d(i), np.atleast_1d(j)
        x = self.raw()
        inside = (i >= 0) & (i <= g.ni - 1) & (j >= 0) & (j <= g.nj - 1)
        out = np.full(i.shape, np.nan, np.float32)
        if inside.any():
            ii, jj = i[inside], j[inside]
            if method == "nearest":
                k = np.rint(jj).astype(np.int64) * g.ni + np.rint(ii).astype(np.int64)
                vals = self._pick(x, k)
            else:
                i0 = np.minimum(np.floor(ii).astype(np.int64), g.ni - 2)
                j0 = np.minimum(np.floor(jj).astype(np.int64), g.nj - 2)
                fi, fj = ii - i0, jj - j0
                k = j0 * g.ni + i0
                v00, v10 = self._pick(x, k), self._pick(x, k + 1)
                v01, v11 = self._pick(x, k + g.ni), self._pick(x, k + g.ni + 1)
                vals = (v00 * (1 - fi) * (1 - fj) + v10 * fi * (1 - fj) + v01 * (1 - fi) * fj + v11 * fi * fj)
            out[inside] = vals
        return float(out[0]) if scalar else out

    def _pick(self, x, k):
        raw = x[k]
        v = self.scale(raw)
        if self._bitmap is not None:
            v = np.where(raw < 0, np.nan, v)
        return v.astype(np.float64)


# ------------------------------------------------------------------------------------------------ bit unpacking
def _bits(buf: np.ndarray, start_bits: np.ndarray, width) -> np.ndarray:
    """Unsigned integers of `width` bits (an array or one number, up to 32) that start at the given bit offsets
    in buf (a uint8 array ending with 8 spare zero bytes). Uses 32-bit arithmetic when the widths allow it:
    this is the hot loop when decoding a 1.9 million point model field."""
    start_bits = np.asarray(start_bits)
    wmax = int(np.max(width)) if np.size(width) else 0
    byte = (start_bits >> 3)
    off = (start_bits & 7).astype(np.uint8)
    if wmax <= 25:                                              # 4 bytes hold the offset (<= 7) plus the value
        word = buf[byte].astype(np.uint32) << np.uint32(24)
        for k, sh in ((1, 16), (2, 8), (3, 0)):
            word |= buf[byte + k].astype(np.uint32) << np.uint32(sh)
        w32 = np.asarray(width).astype(np.uint32)
        shift = np.uint32(32) - off.astype(np.uint32) - w32
        return ((word >> shift) & ((np.uint32(1) << w32) - np.uint32(1))).astype(np.int64)
    word = np.zeros(len(start_bits), np.uint64)
    for k in range(5):
        word = (word << np.uint64(8)) | buf[byte + k].astype(np.uint64)
    w64 = np.asarray(width).astype(np.uint64)
    shift = np.uint64(40) - off.astype(np.uint64) - w64
    return ((word >> shift) & ((np.uint64(1) << w64) - np.uint64(1))).astype(np.int64)


def _padded(data: bytes) -> np.ndarray:
    return np.frombuffer(data + b"\x00" * 8, np.uint8)


def _simple(data: bytes, rep: dict, n: int) -> np.ndarray:
    w = rep["bits"]
    if w == 0:
        return np.zeros(n, np.int64)
    buf = _padded(data)
    return _bits(buf, np.arange(n, dtype=np.int64) * w, w)


def _complex(data: bytes, rep: dict, n: int) -> np.ndarray:
    """Complex packing (5.2) and complex packing with spatial differencing (5.3)."""
    if rep["missing_mgmt"]:
        raise Grib2Error("complex packing with missing-value management is not supported")
    ng = rep["ng"]
    buf = _padded(data)
    pos = 0
    first, hmin = [], 0
    order = rep.get("order", 0)
    if rep["template"] == 3:
        nb = rep["extra"]
        for _ in range(order):
            first.append(_sm(data[pos:pos + nb]))
            pos += nb
        hmin = _sm(data[pos:pos + nb])
        pos += nb
    bits = pos * 8

    def block(count, width):
        nonlocal bits
        out = _bits(buf, bits + np.arange(count, dtype=np.int64) * width, width) if width else np.zeros(count, np.int64)
        bits = (bits + count * width + 7) // 8 * 8            # every block starts on a byte boundary
        return out
    refs = block(ng, rep["bits"])
    widths = block(ng, rep["width_bits"]) + rep["width_ref"]
    lens = block(ng, rep["len_bits"]) * rep["len_inc"] + rep["len_ref"]
    lens[-1] = rep["last_len"]
    total = int(lens.sum())
    if total != n:
        raise Grib2Error(f"the groups hold {total} values but the field has {n}")
    # one value per grid point: its group's bit width, and where its bits start
    gstart = np.concatenate(([0], np.cumsum(lens * widths)[:-1])) + bits
    gfirst = np.concatenate(([0], np.cumsum(lens)[:-1]))
    dt = np.int32 if int((lens * widths).sum()) + bits < 2 ** 31 - 64 else np.int64
    w = np.repeat(widths.astype(np.uint8), lens)
    # bit offset of value i = (start of its group) + (its position in the group) * width
    start = np.repeat((gstart - gfirst * widths).astype(dt), lens) + np.arange(n, dtype=dt) * w.astype(dt)
    x = _bits(buf, start, np.maximum(w, 1))
    x[w == 0] = 0
    x += np.repeat(refs, lens)
    if rep["template"] == 3 and order:
        x = x.astype(np.int64) + hmin                          # the stored numbers are differences minus their minimum
        if order == 1:
            x[0] = first[0]
            x = np.cumsum(x)                                    # y[i] = d[i] + y[i-1]
        elif order == 2:
            x[0] = 0
            x[1] = first[1] - first[0]
            x = first[0] + np.cumsum(np.cumsum(x))              # y[i] = d[i] + 2 y[i-1] - y[i-2]
        else:
            raise Grib2Error(f"spatial differencing of order {order} is not supported")
    return x


# ------------------------------------------------------------------------------------------------ images
def _image(data: bytes, fmt: str) -> np.ndarray:
    try:
        from PIL import Image
    except ImportError as exc:                                  # pragma: no cover
        raise Grib2Error("Pillow is needed to read PNG / JPEG 2000 packed GRIB2 data") from exc
    old = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None                               # an MRMS field is 98 million pixels
    try:
        img = Image.open(io.BytesIO(data))
        if img.format != fmt and not (fmt == "JPEG2000" and img.format in ("JPEG2000", "JP2")):
            raise Grib2Error(f"expected {fmt} data, found {img.format}")
        return np.asarray(img).ravel()                          # uint8 / uint16: not widened (98 MB, not 784 MB)
    finally:
        Image.MAX_IMAGE_PIXELS = old


# ------------------------------------------------------------------------------------------------ messages
def read_messages(data: bytes, want=None) -> list:
    """All messages in a GRIB2 file / byte string. `want(discipline, category, number, level_type, level_value)`
    can skip messages cheaply (their data isn't decoded anyway until values() / sample() is called)."""
    out, pos, n = [], 0, len(data)
    while True:
        pos = data.find(b"GRIB", pos)
        if pos < 0 or pos + 16 > n:
            break
        edition = data[pos + 7]
        if edition != 2:
            raise Grib2Error(f"GRIB edition {edition} is not supported (only 2)")
        discipline = data[pos + 6]
        total = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        end = pos + total
        if end > n:
            break                                               # truncated download
        msg = _parse_message(data[pos:end], discipline)
        if msg is not None and (want is None or want(msg.discipline, msg.category, msg.number, msg.level_type,
                                                    msg.level_value)):
            out.append(msg)
        pos = end
    return out


def _parse_message(m: bytes, discipline: int):
    pos = 16
    ref_time, grid, prod, rep, bitmap, payload = (0,) * 6, None, {}, {}, None, b""
    bitmap_ind = 255
    while pos < len(m) - 4:
        ln = struct.unpack(">I", m[pos:pos + 4])[0]
        num = m[pos + 4]
        sec = m[pos:pos + ln]
        if num == 1:
            ref_time = (struct.unpack(">H", sec[12:14])[0], sec[14], sec[15], sec[16], sec[17], sec[18])
        elif num == 3:
            grid = _parse_grid(sec)
        elif num == 4:
            prod = _parse_product(sec)
        elif num == 5:
            rep = _parse_rep(sec)
        elif num == 6:
            bitmap_ind = sec[5]
            if bitmap_ind == 0:
                bitmap = sec[6:]
            elif bitmap_ind != 255:
                raise Grib2Error(f"bitmap indicator {bitmap_ind} is not supported")
        elif num == 7:
            payload = sec[5:]
        pos += ln
    if grid is None or not rep:
        return None
    return Message(discipline, prod.get("category", 255), prod.get("number", 255), prod.get("level_type", 255),
                   prod.get("level_value", 0.0), ref_time, prod.get("forecast_time", 0), grid, rep, payload, bitmap)


def _parse_grid(s: bytes) -> Grid:
    tmpl = struct.unpack(">H", s[12:14])[0]
    shape = s[14]
    radius = {6: 6371229.0, 0: 6367470.0, 1: 6371229.0}.get(shape)
    if tmpl == 0:
        ni, nj = struct.unpack(">II", s[30:38])
        la1, lo1 = _sm(s[46:50]) * 1e-6, _sm(s[50:54]) * 1e-6
        la2, lo2 = _sm(s[55:59]) * 1e-6, _sm(s[59:63]) * 1e-6
        di, dj = struct.unpack(">II", s[63:71])
        mode = s[71]
        if mode & 0x20:
            raise Grib2Error("grids scanned along columns are not supported")
        return Grid(0, ni, nj, la1, lo1 % 360.0, di * 1e-6, dj * 1e-6, bool(mode & 0x40), la2, lo2 % 360.0)
    if tmpl == 30:
        if radius is None:
            raise Grib2Error(f"earth shape {shape} is not supported")
        ni, nj = struct.unpack(">II", s[30:38])
        la1, lo1 = _sm(s[38:42]) * 1e-6, _sm(s[42:46]) * 1e-6
        lad, lov = _sm(s[47:51]) * 1e-6, _sm(s[51:55]) * 1e-6
        dx, dy = struct.unpack(">II", s[55:63])
        mode = s[64]
        if mode & 0x20:
            raise Grib2Error("grids scanned along columns are not supported")
        l1, l2 = _sm(s[65:69]) * 1e-6, _sm(s[69:73]) * 1e-6
        return Grid(30, ni, nj, la1, lo1 % 360.0, dx * 1e-3, dy * 1e-3, bool(mode & 0x40), 0.0, 0.0,
                    lov % 360.0, lad, l1, l2, radius)
    raise Grib2Error(f"grid definition template 3.{tmpl} is not supported")


def _parse_product(s: bytes) -> dict:
    tmpl = struct.unpack(">H", s[7:9])[0]
    out = {"template": tmpl, "category": s[9], "number": s[10]}
    if tmpl <= 15 or tmpl in (60, 61):
        out["forecast_time"] = struct.unpack(">I", s[18:22])[0]
        out["level_type"] = s[22]
        scale = s[23]
        val = _sm(s[24:28])
        out["level_value"] = val * (10.0 ** -(scale if scale < 128 else scale - 256))
    return out


def _parse_rep(s: bytes) -> dict:
    npoints = struct.unpack(">I", s[5:9])[0]
    tmpl = struct.unpack(">H", s[9:11])[0]
    rep = {"template": tmpl, "npoints": npoints, "ref": struct.unpack(">f", s[11:15])[0], "E": _sm(s[15:17]),
           "D": _sm(s[17:19]), "bits": s[19]}
    if tmpl in (2, 3):
        rep.update(ng=struct.unpack(">I", s[31:35])[0], width_ref=s[35], width_bits=s[36],
                   len_ref=struct.unpack(">I", s[37:41])[0], len_inc=s[41],
                   last_len=struct.unpack(">I", s[42:46])[0], len_bits=s[46])
        rep["missing_mgmt"] = s[22]
        if tmpl == 3:
            rep.update(order=s[47], extra=s[48])
    return rep                                       # unsupported templates are reported when the data is read
