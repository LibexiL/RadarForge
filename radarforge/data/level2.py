"""NEXRAD Level II decoder (Archive II / AR2V, plus AWS real-time chunks).

Handles:
  * Message 31 (generic digital radar data, 2008+ / super-resolution / dual-pol)
  * Message 1  (legacy digital radar data, pre-2008)
  * bzip2-compressed LDM records (normal archive files and S/I/E chunks)
  * uncompressed legacy archives and gzip/bz2 wrapped files

Data is kept as raw uint8/uint16 gate codes with scale/offset, and those are
kept zlib-compressed in memory (radar data packs 3-7x), so memory stays small;
values are decoded on demand (value = (raw - offset) / scale, codes 0 and 1 are
"below threshold" and "range folded").
"""
from __future__ import annotations

import bz2
import gzip
import struct
import threading
import weakref
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

EPOCH = datetime(1969, 12, 31, tzinfo=timezone.utc)  # MJD day 1 == 1970-01-01

MOMENT_NAMES = ("REF", "VEL", "SW", "ZDR", "PHI", "RHO", "CFP")

_hdr = struct.Struct(">HBBHHIHH")          # 16 byte message header
_m31 = struct.Struct(">4sIHHfBBHBBBBfBBH")  # msg 31 fixed header (32 bytes)
_moment = struct.Struct(">c3sIHhhhhBBff")   # 28 byte generic moment header
_vol = struct.Struct(">c3sHBBffhHfffffHH")
_rad = struct.Struct(">c3sHhffh")


def nexrad_time(mjd: int, ms: int) -> datetime:
    return EPOCH + timedelta(days=int(mjd), milliseconds=int(ms))


@dataclass
class RadialMoment:
    first_gate: float   # km, centre of first gate
    gate_spacing: float  # km
    scale: float
    offset: float
    raw: np.ndarray     # uint8 or uint16 gate codes


@dataclass
class Radial:
    azimuth: float
    elevation: float
    elev_num: int
    status: int
    time_ms: int
    mjd: int
    az_res: float
    nyquist: float | None
    unamb_range: float | None
    moments: dict


# Unpacked gate codes of recently used moments stay in memory (up to this much) so working on a sweep
# doesn't unpack it again and again; everything else stays compressed.
UNPACKED_BUDGET = 64 << 20
_unpacked: OrderedDict = OrderedDict()       # id(field) -> (weakref to the field, bytes)
_unpacked_bytes = 0
_unpacked_lock = threading.RLock()


def _pack(raw: np.ndarray) -> bytes:
    c = zlib.compressobj(1, zlib.DEFLATED, 15, 9, zlib.Z_RLE)     # run-length: fast, and radar data has long runs
    return c.compress(raw.reshape(-1).view(np.uint8)) + c.flush()


def _forget_unpacked(key, ref):
    """A field was freed (its volume dropped): its unpacked codes went with it, so stop counting them."""
    global _unpacked_bytes
    with _unpacked_lock:
        e = _unpacked.get(key)
        if e is not None and e[0] is ref:
            del _unpacked[key]
            _unpacked_bytes -= e[1]


def _remember_unpacked(mf, nbytes: int):
    global _unpacked_bytes
    with _unpacked_lock:
        key = id(mf)
        old = _unpacked.pop(key, None)
        if old is not None:
            _unpacked_bytes -= old[1]
        _unpacked[key] = (weakref.ref(mf, lambda r, k=key: _forget_unpacked(k, r)), nbytes)
        _unpacked_bytes += nbytes
        while _unpacked_bytes > UNPACKED_BUDGET and len(_unpacked) > 1:
            _k, (ref, n) = _unpacked.popitem(last=False)
            _unpacked_bytes -= n
            f = ref()
            if f is not None and f is not mf:
                f._raw = None


def _touch_unpacked(mf):
    with _unpacked_lock:
        e = _unpacked.get(id(mf))
        if e is not None and e[0]() is mf:
            _unpacked.move_to_end(id(mf))


def unpacked_bytes() -> int:
    """Memory held by unpacked gate codes right now (for tests and the memory readout)."""
    return _unpacked_bytes


class MomentField:
    """One moment for one sweep: raw codes (n_radials x n_gates), kept compressed. `raw` unpacks them
    (read-only; recently used ones stay unpacked, see UNPACKED_BUDGET)."""
    __slots__ = ("name", "scale", "offset", "first_gate", "gate_spacing", "_shape", "_dtype", "_packed", "_raw",
                 "__weakref__")

    def __init__(self, name: str, raw: np.ndarray, scale: float, offset: float, first_gate: float,
                 gate_spacing: float):
        self.name = name
        self.scale = scale
        self.offset = offset
        self.first_gate = first_gate
        self.gate_spacing = gate_spacing
        raw = np.ascontiguousarray(raw)
        self._shape = raw.shape
        self._dtype = raw.dtype.str
        self._packed = _pack(raw)
        self._raw = None

    @property
    def raw(self) -> np.ndarray:
        r = self._raw
        if r is None:
            r = np.frombuffer(zlib.decompress(self._packed), dtype=self._dtype).reshape(self._shape)
            self._raw = r
            _remember_unpacked(self, r.nbytes)
        else:
            _touch_unpacked(self)
        return r

    @property
    def packed_bytes(self) -> int:
        return len(self._packed)

    @property
    def shape(self) -> tuple:
        return self._shape

    def __getstate__(self):                   # decoder process -> app: only the compressed codes travel
        return (self.name, self.scale, self.offset, self.first_gate, self.gate_spacing, self._shape, self._dtype,
                self._packed)

    def __setstate__(self, st):
        (self.name, self.scale, self.offset, self.first_gate, self.gate_spacing, self._shape, self._dtype,
         self._packed) = st
        self._raw = None

    def __repr__(self):
        return (f"MomentField({self.name}, {self._shape} {np.dtype(self._dtype).name}, "
                f"{len(self._packed) / 1e6:.2f} MB packed)")

    @property
    def ngates(self) -> int:
        return self._shape[1]

    @property
    def max_range(self) -> float:
        return self.first_gate + self.gate_spacing * (self.ngates - 0.5)

    def values(self, dtype=np.float32) -> np.ndarray:
        """Physical values with NaN for below-threshold/range-folded/no-data."""
        raw = self.raw
        out = (raw.astype(dtype) - dtype(self.offset)) / dtype(self.scale)
        out[raw <= 1] = np.nan
        return out

    def rf_mask(self) -> np.ndarray:
        return self.raw == 1


@dataclass
class Sweep:
    index: int                 # order within the volume
    elev_num: int              # RDA elevation number
    elevation: float           # mean elevation angle (deg)
    azimuths: np.ndarray       # float32 (n,)
    elevations: np.ndarray     # float32 (n,)
    times: np.ndarray          # float64 seconds since volume start
    az_res: float
    nyquist: float | None
    unamb_range: float | None
    moments: dict              # name -> MomentField
    start_time: datetime
    complete: bool = True

    @property
    def nrays(self) -> int:
        return len(self.azimuths)


@dataclass
class Level2Volume:
    site: str
    start_time: datetime
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None  # site elevation + feedhorn height (m MSL)
    vcp: int | None = None
    sweeps: list = field(default_factory=list)
    complete: bool = True
    source: str = ""

    def summary(self) -> str:
        parts = [f"{self.site} {self.start_time:%Y-%m-%d %H:%M:%S}Z VCP {self.vcp}"]
        for s in self.sweeps:
            m = ",".join(s.moments)
            parts.append(f"  #{s.index:2d} el{s.elev_num:2d} {s.elevation:5.2f}deg "
                         f"{s.nrays} rays [{m}] nyq={s.nyquist}")
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# raw byte handling
# --------------------------------------------------------------------------- #

def _unwrap_container(data: bytes) -> bytes:
    """Strip whole-file gzip/bzip2 wrappers."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:3] == b"BZh":
        # whole-file bzip2 (not LDM records): could be multi-stream
        try:
            return bz2.decompress(data)
        except OSError:
            return data
    return data


def split_records(data: bytes, offset: int) -> list:
    """Split LDM compressed records starting at *offset*.

    Returns a list of byte strings (still compressed) or a single uncompressed
    message stream if the file is not record-compressed.
    """
    recs = []
    pos = offset
    n = len(data)
    while pos + 4 <= n:
        size = struct.unpack_from(">i", data, pos)[0]
        size = abs(size)
        pos += 4
        if size == 0:
            continue
        chunk = data[pos:pos + size]
        pos += size
        recs.append(chunk)
    return recs


def _decompress_record(rec: bytes) -> bytes:
    if rec[:3] == b"BZh":
        try:
            return bz2.decompress(rec)
        except (OSError, ValueError, EOFError):
            # truncated record (live chunk still being written) - salvage what we can
            d = bz2.BZ2Decompressor()
            try:
                return d.decompress(rec)
            except Exception:
                return b""
    return rec


_pool = None


def _executor():
    global _pool
    if _pool is None:
        import os
        _pool = ThreadPoolExecutor(max_workers=max(2, min(8, (os.cpu_count() or 2))))
    return _pool


def decompress_stream(data: bytes) -> tuple[dict, bytes]:
    """Return (volume header dict, concatenated message bytes)."""
    data = _unwrap_container(data)
    header = {}
    offset = 0
    if data[:4] in (b"AR2V", b"ARCH"):
        tape, ext, mjd, ms, icao = struct.unpack_from(">9s3sII4s", data, 0)
        header = dict(version=tape.decode("ascii", "replace"),
                      time=nexrad_time(mjd, ms) if mjd else None,
                      icao=icao.decode("ascii", "replace").strip("\x00 "))
        offset = 24

    # Is the remainder LDM record-compressed?
    compressed = (len(data) >= offset + 7 and data[offset + 4:offset + 7] == b"BZh")
    if compressed:
        recs = split_records(data, offset)
        if len(recs) > 3:
            parts = list(_executor().map(_decompress_record, recs))
        else:
            parts = [_decompress_record(r) for r in recs]
        return header, b"".join(parts)
    return header, data[offset:]


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #

def _parse_msg31(buf: bytes, start: int, end: int, meta: dict) -> Radial | None:
    try:
        (ident, ms, mjd, az_num, az, comp, _sp, rlen, az_res, status, elev_num,
         cut, elev, blank, az_idx, nblocks) = _m31.unpack_from(buf, start)
    except struct.error:
        return None
    nblocks = min(nblocks, 10)
    ptrs = struct.unpack_from(f">{nblocks}I", buf, start + 32)
    moments = {}
    nyq = unamb = None
    for p in ptrs:
        if p == 0:
            continue
        b = start + p
        if b + 4 > end:
            continue
        btype = buf[b:b + 1]
        name = buf[b + 1:b + 4].decode("ascii", "replace").strip()
        if btype == b"R":
            if name == "VOL":
                try:
                    v = _vol.unpack_from(buf, b)
                except struct.error:
                    continue
                meta.setdefault("lat", v[5])
                meta.setdefault("lon", v[6])
                meta.setdefault("height", float(v[7]) + float(v[8]))
                meta.setdefault("vcp", v[14])
            elif name == "RAD":
                try:
                    r = _rad.unpack_from(buf, b)
                except struct.error:
                    continue
                unamb = r[3] / 10.0
                nyq = r[6] / 100.0
            continue
        if btype != b"D":
            continue
        try:
            (_t, _n, _res, ngates, first, spacing, _thr, _snr, _ctl, wsize,
             scale, offset) = _moment.unpack_from(buf, b)
        except struct.error:
            continue
        if ngates == 0 or scale == 0:
            continue
        ds = b + 28
        if wsize == 16:
            raw = np.frombuffer(buf, dtype=">u2", count=ngates, offset=ds).astype(np.uint16)
        else:
            raw = np.frombuffer(buf, dtype=np.uint8, count=ngates, offset=ds)
        if name == "RHO":
            name = "RHO"
        moments[name] = RadialMoment(first / 1000.0, spacing / 1000.0, scale, offset, raw)
    return Radial(az, elev, elev_num, status, ms, mjd, 0.5 if az_res == 1 else 1.0,
                  nyq, unamb, moments)


def _parse_msg1(buf: bytes, start: int) -> Radial | None:
    try:
        (ms, mjd, unamb, az_code, az_num, status, el_code, el_num, s_first, d_first,
         s_int, d_int, s_n, d_n, sector, cal, ref_p, vel_p, sw_p, dop_res, vcp) = \
            struct.unpack_from(">IHHHHHHHhhHHHHHfHHHHH", buf, start)
        nyq = struct.unpack_from(">H", buf, start + 60)[0] / 100.0
    except struct.error:
        return None
    az = az_code * 180.0 / 32768.0
    el = el_code * 180.0 / 32768.0
    moments = {}
    res = 0.5 if dop_res == 2 else 1.0
    if s_n and ref_p:
        raw = np.frombuffer(buf, np.uint8, count=s_n, offset=start + ref_p)
        moments["REF"] = RadialMoment(s_first / 1000.0, s_int / 1000.0, 2.0, 66.0, raw)
    if d_n and vel_p:
        raw = np.frombuffer(buf, np.uint8, count=d_n, offset=start + vel_p)
        moments["VEL"] = RadialMoment(d_first / 1000.0, d_int / 1000.0, 1.0 / res, 129.0, raw)
    if d_n and sw_p:
        raw = np.frombuffer(buf, np.uint8, count=d_n, offset=start + sw_p)
        moments["SW"] = RadialMoment(d_first / 1000.0, d_int / 1000.0, 2.0, 129.0, raw)
    r = Radial(az, el, el_num, status, ms, mjd, 1.0, nyq if nyq else None,
               unamb / 10.0 if unamb else None, moments)
    r.vcp = vcp  # type: ignore[attr-defined]
    return r


def iter_radials(msgs: bytes, meta: dict):
    """Walk the message stream yielding Radial objects."""
    pos = 0
    n = len(msgs)
    while pos + 28 <= n:
        try:
            size_hw, _chan, mtype, _seq, _mjd, _ms, _nseg, _seg = _hdr.unpack_from(msgs, pos + 12)
        except struct.error:
            break
        if mtype == 31:
            end = pos + 12 + size_hw * 2
            if size_hw == 0 or end > n:
                break
            r = _parse_msg31(msgs, pos + 28, end, meta)
            if r is not None:
                yield r
            pos = end
        else:
            if mtype == 1:
                r = _parse_msg1(msgs, pos + 28)
                if r is not None:
                    if getattr(r, "vcp", None):
                        meta.setdefault("vcp", r.vcp)
                    yield r
            elif mtype == 5 and "vcp" not in meta:
                try:
                    meta["vcp"] = struct.unpack_from(">H", msgs, pos + 28 + 4)[0]
                except struct.error:
                    pass
            if mtype == 0 and size_hw == 0:
                # padding / blank frame
                pos += 2432
                continue
            pos += 2432


# --------------------------------------------------------------------------- #
# sweep assembly
# --------------------------------------------------------------------------- #

class SweepBuilder:
    """Accumulates radials (possibly across live chunks) into sweeps."""

    def __init__(self):
        self.meta: dict = {}
        self.header: dict = {}
        self._groups: list = []   # list of [elev_num, [radials]]
        self._built: dict = {}    # group index -> Sweep for closed groups (live re-use)

    def add_bytes(self, data: bytes):
        hdr, msgs = decompress_stream(data)
        if hdr:
            self.header.update(hdr)
        self.add_messages(msgs)

    def add_messages(self, msgs: bytes):
        for r in iter_radials(msgs, self.meta):
            self._add_radial(r)

    def _add_radial(self, r: Radial):
        # start a new group when elevation number changes or a new-elevation flag appears
        if (not self._groups or self._groups[-1][0] != r.elev_num
                or r.status in (0, 3) and len(self._groups[-1][1]) > 10):
            self._groups.append([r.elev_num, [r]])
        else:
            self._groups[-1][1].append(r)

    def build(self, site_hint: str = "", source: str = "") -> Level2Volume:
        site = self.header.get("icao") or site_hint
        vol_time = self.header.get("time")
        sweeps = []
        first_time = None
        last_gi = len(self._groups) - 1
        for gi, g in enumerate(self._groups):
            en, rads = g[0], g[1]
            cached = self._built.get(gi)
            if cached is not None and (rads is None or cached[0] == len(rads)):
                sw = cached[1]
                sw.index = len(sweeps)
            else:
                if len(rads) < 10:
                    continue
                sw = _assemble(len(sweeps), en, rads)
                if sw is None:
                    continue
                if gi < last_gi:
                    self._built[gi] = (len(rads), sw)
                    g[1] = None                 # a closed sweep never changes: its radials aren't needed again
            sw.complete = gi < last_gi or (rads is not None and rads[-1].status in (2, 4))
            if first_time is None:
                first_time = sw.start_time
            sweeps.append(sw)
        if vol_time is None or vol_time.year < 1990:
            vol_time = first_time or datetime.now(timezone.utc)
        vol = Level2Volume(site=site, start_time=vol_time,
                           lat=self.meta.get("lat"), lon=self.meta.get("lon"),
                           height_m=self.meta.get("height"), vcp=self.meta.get("vcp"),
                           sweeps=sweeps, source=source)
        vol.complete = bool(self._groups) and self._groups[-1][1][-1].status == 4
        return vol


def _assemble(index: int, elev_num: int, rads: list) -> Sweep | None:
    names = set()
    for r in rads:
        names.update(r.moments)
    nr = len(rads)
    az = np.fromiter((r.azimuth for r in rads), np.float32, nr)
    el = np.fromiter((r.elevation for r in rads), np.float32, nr)
    t0 = nexrad_time(rads[0].mjd, rads[0].time_ms)
    base = rads[0].mjd * 86400.0 + rads[0].time_ms / 1000.0
    times = np.fromiter((r.mjd * 86400.0 + r.time_ms / 1000.0 - base for r in rads), np.float64, nr)
    moments = {}
    for name in names:
        ref = None
        maxg = 0
        for r in rads:
            m = r.moments.get(name)
            if m is not None:
                if ref is None:
                    ref = m
                maxg = max(maxg, len(m.raw))
        if ref is None or maxg == 0:
            continue
        dtype = np.uint16 if ref.raw.dtype == np.uint16 else np.uint8
        raws = [r.moments[name].raw if name in r.moments else None for r in rads]
        if all(x is not None and len(x) == maxg for x in raws):
            arr = np.stack(raws).astype(dtype, copy=False)      # common case: one C-level copy
        else:
            arr = np.zeros((nr, maxg), dtype=dtype)
            for i, x in enumerate(raws):
                if x is not None:
                    arr[i, :len(x)] = x
        # trim trailing empty gates
        nz = np.nonzero((arr > 1).any(axis=0))[0]
        if len(nz) == 0:
            arr = arr[:, :1]
        else:
            arr = arr[:, :nz[-1] + 1]
        moments[name] = MomentField(name, np.ascontiguousarray(arr), ref.scale, ref.offset,
                                    ref.first_gate, ref.gate_spacing)
    if not moments:
        return None
    nyqs = [r.nyquist for r in rads if r.nyquist]
    ur = [r.unamb_range for r in rads if r.unamb_range]
    return Sweep(index=index, elev_num=elev_num,
                 elevation=float(np.median(el)), azimuths=az, elevations=el, times=times,
                 az_res=rads[len(rads) // 2].az_res,
                 nyquist=float(np.median(nyqs)) if nyqs else None,
                 unamb_range=float(np.median(ur)) if ur else None,
                 moments=moments, start_time=t0)


def read_level2(data: bytes | str, site_hint: str = "") -> Level2Volume:
    """Decode a complete Level II file (bytes or path)."""
    source = ""
    if isinstance(data, (str, bytes)) and not isinstance(data, bytes):
        source = str(data)
        with open(data, "rb") as fh:
            data = fh.read()
    b = SweepBuilder()
    b.add_bytes(data)
    vol = b.build(site_hint, source)
    vol.complete = True
    return vol


def add_chunks(builder, datas: list, site_hint: str = "", source: str = ""):
    """Adds live chunks (bytes, in order) to [builder] (None: a new one) and builds the volume so far. Runs in a
    decoder process for a batch of chunks (catching up on a volume). Returns (builder, [chunk decoded ok],
    volume)."""
    if builder is None:
        builder = SweepBuilder()
    oks = []
    for d in datas:
        try:
            builder.add_bytes(d)
            oks.append(True)
        except Exception:              # a damaged chunk: the caller counts it as missing
            oks.append(False)
    return builder, oks, builder.build(site_hint, source)


def read_chunks(chunks: list, site_hint: str = "") -> Level2Volume:
    """Decode a (possibly partial) list of real-time chunk byte strings in order."""
    b = SweepBuilder()
    for c in chunks:
        b.add_bytes(c)
    return b.build(site_hint, "chunks")


# --------------------------------------------------------------------------- #
# out-of-process decoding (keeps the UI responsive: parsing is Python-heavy)
# --------------------------------------------------------------------------- #
_proc_pool = None          # Level II decoding (needs only NumPy)
_l3_pool = None            # Level III decoding (MetPy, which takes ~150 MB per process: only one has it)
_proc_failed = False
_pool_lock = threading.Lock()
_restarts = 0              # decoder pools replaced after a worker died or hung (3 -> decode in threads)


class DecodeCancelled(Exception):
    """A read-ahead decode that was no longer wanted when its turn came (another radar, frame gone)."""


class _Gate:
    """Hands out the Level II decoder processes' slots. Only as many decodes as there are processes are given
    to the pool at once, so a decode's time limit counts its own work (not its wait in a queue behind a
    loop's worth of others), and a decode the screen is waiting on goes ahead of read-ahead ones: the lowest
    priority number goes first, then first come first served."""

    def __init__(self):
        self._cv = threading.Condition()
        self._busy = 0
        self._waiting: list = []
        self._seq = 0

    BACKGROUND = 10      # priorities from here on are read-ahead: they leave one process free for the screen

    def acquire(self, slots: int, priority: float, cancelled=None) -> bool:
        import heapq
        limit = slots - 1 if priority >= self.BACKGROUND and slots > 1 else slots
        with self._cv:
            self._seq += 1
            me = (priority, self._seq)
            heapq.heappush(self._waiting, me)
            try:
                while not (self._busy < limit and self._waiting[0] == me):
                    if cancelled is not None and cancelled():
                        return False
                    self._cv.wait(0.25 if cancelled is not None else None)
                self._busy += 1
                return True
            finally:
                if me in self._waiting:
                    self._waiting.remove(me)
                    heapq.heapify(self._waiting)
                self._cv.notify_all()

    def release(self):
        with self._cv:
            self._busy = max(0, self._busy - 1)
            self._cv.notify_all()

    @property
    def waiting(self) -> int:
        with self._cv:
            return len(self._waiting)


_gate = _Gate()
_ctx = threading.local()


class decode_priority:
    """with decode_priority(p[, cancelled]): Level II decodes started by this thread wait their turn with
    priority p (0, the default, is something on screen; larger numbers wait behind it). [cancelled]: a
    callable; a decode still waiting when it returns True raises DecodeCancelled instead."""

    def __init__(self, priority: float, cancelled=None):
        self.p, self.c = priority, cancelled

    def __enter__(self):
        self._old = (getattr(_ctx, "priority", 0), getattr(_ctx, "cancelled", None))
        _ctx.priority, _ctx.cancelled = self.p, self.c
        return self

    def __exit__(self, *exc):
        _ctx.priority, _ctx.cancelled = self._old
        return False


class decode_slot:
    """with decode_slot(p[, cancelled]): holds one decoder slot for the decodes this thread runs inside it.
    Read-ahead takes its slot first and only then claims a volume, so a panel that wants the same volume never
    waits on a decode that is itself still queued. Raises DecodeCancelled if [cancelled]() turns True first."""

    def __init__(self, priority: float, cancelled=None):
        self.p, self.c = priority, cancelled

    def __enter__(self):
        if not _gate.acquire(decoder_count(), self.p, self.c):
            raise DecodeCancelled()
        self._old = getattr(_ctx, "held", False)
        _ctx.held = True
        return self

    def __exit__(self, *exc):
        _ctx.held = self._old
        _gate.release()
        return False


def _new_pool(n: int):
    # (no max_tasks_per_child: some Python 3.11/3.12 releases can hang replacing a retired worker; each task
    # gives its memory back instead, see _call_and_trim)
    import multiprocessing as mp
    import os
    from concurrent.futures import ProcessPoolExecutor
    return ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"), initializer=_worker_init,
                               initargs=(os.getpid(),))


def decoder_count() -> int:
    """Level II decoder processes (and so the decodes that run side by side): 2 on a dual core, 3 from four
    cores up (read-ahead decoding uses all but one, so something on screen never waits for a process)."""
    import os
    return max(2, min(3, (os.cpu_count() or 2) // 2 + 1))


def _proc_executor(level3: bool = False):
    global _proc_pool, _l3_pool
    with _pool_lock:                   # (two threads starting at once must not make two pools)
        if level3:
            if _l3_pool is None:
                _l3_pool = _new_pool(1)
            return _l3_pool
        if _proc_pool is None:
            _proc_pool = _new_pool(decoder_count())
        return _proc_pool


def _kill(pool):
    procs = list((getattr(pool, "_processes", None) or {}).values())
    for pr in procs:
        try:
            pr.terminate()
        except Exception:
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def limit_malloc_arenas():
    """glibc gives every thread its own heap arena, and memory freed in one rarely goes back to the system:
    with several decoding threads that alone can hold hundreds of MB. Two arenas are plenty here."""
    import sys
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(-8, 2)                  # M_ARENA_MAX
    except Exception:
        pass


def release_memory():
    """Hand freed heap memory back to the system (glibc keeps it otherwise). Cheap; Linux only."""
    import sys
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _worker_init(parent_pid: int):
    """Decoder worker: exit by itself if the app goes away without shutting the pool down."""
    import os
    import threading
    import time
    limit_malloc_arenas()
    _lower_priority()

    def watch():
        while True:
            time.sleep(2.0)
            if os.getppid() != parent_pid:
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def _lower_priority():
    """Decoder processes run a notch below the app, so a loop being decoded never makes the map stutter."""
    import os
    import sys
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)     # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:
        pass


def _call_and_trim(fn, *args):
    """Runs in a decoder process: the decode, then give the memory it used for parsing back."""
    try:
        return fn(*args)
    finally:
        release_memory()


def run_isolated(fn, *args, timeout: float = 180, level3: bool = False):
    """Run fn(*args) in a decoder process when possible, falling back to this thread.

    Parsing is Python-heavy; doing it in another process keeps the UI thread from
    fighting worker threads for the GIL (which is what makes the map stutter).
    [level3]: the Level III decoder process (the only one that loads MetPy).
    Level II decodes wait their turn for a process (see _Gate and decode_priority).
    """
    global _proc_failed, _proc_pool, _l3_pool, _restarts
    from concurrent.futures import TimeoutError as FutTimeout
    from concurrent.futures.process import BrokenProcessPool
    gated = not level3 and not getattr(_ctx, "held", False)
    if gated:
        slots = decoder_count()
        if not _gate.acquire(slots, getattr(_ctx, "priority", 0), getattr(_ctx, "cancelled", None)):
            raise DecodeCancelled()
    try:
        if not _proc_failed:
            pool = None
            try:
                pool = _proc_executor(level3)
                return pool.submit(_call_and_trim, fn, *args).result(timeout=timeout)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and "after shutdown" in str(exc):
                    return fn(*args)        # interpreter exiting
                if not _is_pool_problem(exc):
                    raise                   # a genuine decode error from the worker
                import sys
                with _pool_lock:
                    current = _l3_pool if level3 else _proc_pool
                    if pool is not None and pool is current:
                        if isinstance(exc, (FutTimeout, BrokenProcessPool)) and _restarts < 3:
                            # a worker hung or died (out of memory?): start fresh processes next time
                            _restarts += 1
                            print(f"radarforge: restarting the decoder processes ({type(exc).__name__})",
                                  file=sys.stderr)
                            if level3:
                                _l3_pool = None
                            else:
                                _proc_pool = None
                            _kill(pool)
                        else:
                            # e.g. a launcher without a __main__ guard, a pickling problem, a frozen build…
                            print("radarforge: process decoding unavailable, using threads:", exc, file=sys.stderr)
                            _proc_failed = True
                            for p in (_proc_pool, _l3_pool):
                                if p is not None:
                                    _kill(p)
                            _proc_pool = _l3_pool = None
                if isinstance(exc, FutTimeout):
                    raise RuntimeError("decoding took too long") from None
        return fn(*args)
    finally:
        if gated:
            _gate.release()


def _is_pool_problem(exc) -> bool:
    """True when the process pool itself is broken (not when the file it was given is bad)."""
    import pickle
    from concurrent.futures import TimeoutError as FutTimeout
    from concurrent.futures.process import BrokenProcessPool
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, EOFError)):
        return False                     # a missing or unreadable file: the pool is fine
    # (ImportError: a worker that can't load what the decoder needs, e.g. in an incomplete frozen build)
    return isinstance(exc, (BrokenProcessPool, pickle.PicklingError, FutTimeout, OSError, AttributeError,
                            ImportError))


def read_level2_isolated(path: str, site_hint: str = "") -> Level2Volume:
    """Decode in a worker process when possible (run_isolated falls back to this thread when the processes
    can't be used; a file that can't be decoded raises, once)."""
    return run_isolated(read_level2, path, site_hint)


def _warm():
    try:
        import metpy.io   # slow import, done once per worker process
        metpy.io.Level3File
    except Exception:
        pass
    return True


def _ready():
    return True


def warm_up():
    """Start the decoder processes early so the first volume doesn't wait for them."""
    if _proc_failed:
        return
    try:
        ex = _proc_executor()
        for _ in range(ex._max_workers):
            ex.submit(_ready)
        _proc_executor(level3=True).submit(_warm)       # MetPy is slow to import: only the Level III process
    except Exception:
        pass


def shutdown_pool():
    """Stop the decoder processes now (used before the app restarts itself)."""
    global _proc_pool, _l3_pool
    pools = [p for p in (_proc_pool, _l3_pool) if p is not None]
    _proc_pool = _l3_pool = None
    for pool in pools:
        procs = list((getattr(pool, "_processes", None) or {}).values())
        for pr in procs:                      # stop the workers first, even mid-decode …
            try:
                pr.terminate()
            except Exception:
                pass
        try:                                  # … so the pool's own shutdown returns at once
            pool.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
