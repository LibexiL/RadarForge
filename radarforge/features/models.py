"""Weather models (HRRR, NAM 3 km, RAP, GFS) from NOAA's buckets on AWS, Pivotal-Weather style.

Each model run is a set of GRIB2 files, one per forecast hour, with a text index (.idx) listing every field
and where it starts. Only the fields a map needs are downloaded, with HTTP byte ranges (one field is about
0.5-3 MB instead of a 150 MB-1 GB file).

  HRRR   noaa-hrrr-bdp-pds  hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcfFF.grib2            hourly, 18 h (48 h at 00/06/12/18)
  NAM 3k noaa-nam-pds       nam.YYYYMMDD/nam.tHHz.conusnest.hiresfFF.tm00.grib2     00/06/12/18, 60 h
  RAP    noaa-rap-pds       rap.YYYYMMDD/rap.tHHz.awp130pgrbfFF.grib2               hourly, 21 h (51 h at 03/09/15/21)
  GFS    noaa-gfs-bdp-pds   gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF          00/06/12/18, 384 h

The field catalog (FIELDS) is shared in spirit with the Android app's Models.kt: same ids, inputs and maths.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from . import grib2

# --------------------------------------------------------------------------- models


@dataclass(frozen=True)
class ModelDef:
    id: str
    name: str
    long: str
    bucket: str
    cycle_every: int            # hours between runs
    res_km: float
    region: str                 # "CONUS" or "Global"
    delay_h: float              # how long after its cycle a run usually starts arriving

    def key(self, run: datetime, fh: int) -> str:
        d, h = run.strftime("%Y%m%d"), run.strftime("%H")
        if self.id == "hrrr":
            return f"hrrr.{d}/conus/hrrr.t{h}z.wrfsfcf{fh:02d}.grib2"
        if self.id == "nam":
            return f"nam.{d}/nam.t{h}z.conusnest.hiresf{fh:02d}.tm00.grib2"
        if self.id == "rap":
            return f"rap.{d}/rap.t{h}z.awp130pgrbf{fh:02d}.grib2"
        return f"gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{fh:03d}"

    def prefix(self, run: datetime) -> str:
        """The listing prefix covering every forecast hour of a run."""
        k = self.key(run, 0)
        return k[:k.rindex("f")]

    def fh_of(self, key: str):
        m = re.search(r"f(\d{2,3})(?:\.tm00)?(?:\.grib2)?\.idx$", key)
        return int(m.group(1)) if m else None

    def max_fh(self, run: datetime) -> int:
        h = run.hour
        if self.id == "hrrr":
            return 48 if h % 6 == 0 else 18
        if self.id == "rap":
            return 51 if h % 6 == 3 else 21
        if self.id == "nam":
            return 60
        return 384

    def hours(self, run: datetime) -> list:
        """Forecast hours this run will have."""
        if self.id == "gfs":
            return list(range(0, 121)) + list(range(123, 385, 3))
        return list(range(0, self.max_fh(run) + 1))


MODELS = OrderedDict((m.id, m) for m in [
    ModelDef("hrrr", "HRRR", "High-Resolution Rapid Refresh · 3 km · hourly", "noaa-hrrr-bdp-pds", 1, 3, "CONUS", 0.8),
    ModelDef("nam", "NAM 3 km", "North American Mesoscale nest · 3 km · every 6 h", "noaa-nam-pds", 6, 3, "CONUS", 1.5),
    ModelDef("rap", "RAP", "Rapid Refresh · 13 km · hourly", "noaa-rap-pds", 1, 13, "CONUS", 0.8),
    ModelDef("gfs", "GFS", "Global Forecast System · 0.25° · every 6 h", "noaa-gfs-bdp-pds", 6, 25, "Global", 3.5),
])

# --------------------------------------------------------------------------- index files


@dataclass
class IdxEntry:
    num: int
    start: int
    end: int | None             # inclusive; None = to the end of the file
    var: str
    level: str
    fcst: str

    @property
    def acc_span(self):
        """(a, b) hours of an accumulation / average / max "a-b hour acc fcst", else None."""
        m = re.match(r"(\d+)-(\d+) hour (acc|ave|max|min)", self.fcst)
        return (int(m.group(1)), int(m.group(2))) if m else None

    @property
    def stat(self):
        m = re.match(r"\d+-\d+ hour (acc|ave|max|min)", self.fcst)
        return m.group(1) if m else "fcst"


def parse_idx(text: str) -> list:
    """wgrib2-style inventory -> [IdxEntry]. Lines: 'n:offset:d=YYYYMMDDHH:VAR:LEVEL:FCST:...'."""
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue
        try:
            num, off = int(parts[0].split(".")[0]), int(parts[1])
        except ValueError:
            continue
        rows.append(IdxEntry(num, off, None, parts[3].strip(), parts[4].strip(), parts[5].strip()))
    rows.sort(key=lambda r: r.start)
    for a, b in zip(rows, rows[1:]):
        a.end = b.start - 1 if b.start > a.start else None
    return rows


def find_entry(rows: list, var: str, level: str, stat: str = "fcst", fh: int | None = None, span=None):
    """The row for var at level. stat: 'fcst' (instantaneous / analysis), 'acc0' (accumulated from the start),
    'acc1' (over the last hour), 'max1' (maximum over the last hour), 'ave' (any average), 'any'."""
    lv = level.lower()
    cands = [r for r in rows if r.var.upper() == var.upper() and r.level.lower() == lv]
    if not cands:
        return None
    if stat == "any":
        return cands[0]
    if stat == "fcst":
        for r in cands:
            if r.fcst in ("anl",) or r.fcst.endswith("hour fcst") or r.fcst.endswith("min fcst") or r.fcst == "0 hour fcst":
                return r
        return None
    for r in cands:
        sp = r.acc_span
        if sp is None:
            continue
        if span is not None and sp != tuple(span):
            continue
        if stat == "acc0" and r.stat == "acc" and sp[0] == 0 and (fh is None or sp[1] == fh):
            return r
        if stat == "acc1" and r.stat == "acc" and sp[1] - sp[0] == 1 and (fh is None or sp[1] == fh):
            return r
        if stat == "accspan" and r.stat == "acc":
            return r
        if stat == "max1" and r.stat == "max" and (fh is None or sp[1] == fh):
            return r
        if stat == "ave" and r.stat == "ave" and (fh is None or sp[1] == fh):
            return r
    return None


# --------------------------------------------------------------------------- downloads + caches
_CACHE_LOCK = threading.Lock()


def _cache_dir():
    from ..config import CACHE_DIR
    d = CACHE_DIR / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http_get(url, headers=None, timeout=60):
    from ..data import aws
    r = aws.session().get(url, headers=headers or {}, timeout=timeout)
    if r.status_code == 404:
        raise FileNotFoundError(url)
    r.raise_for_status()
    return r.content


def _cached(name: str, fetch, max_age_s=None):
    path = _cache_dir() / name
    try:
        st = path.stat()
        if st.st_size > 0 and (max_age_s is None or time.time() - st.st_mtime < max_age_s):
            return path.read_bytes()
    except OSError:
        pass
    data = fetch()
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def prune_cache(max_bytes=1.5e9):
    """Oldest model downloads go first once the folder passes max_bytes."""
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in _cache_dir().iterdir() if p.is_file()]
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _t, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def s3_url(model: ModelDef, key: str) -> str:
    return f"https://{model.bucket}.s3.amazonaws.com/{key}"


def get_idx(model: ModelDef, run: datetime, fh: int) -> list:
    key = model.key(run, fh) + ".idx"
    raw = _cached("idx_" + _h(model.bucket + key), lambda: _http_get(s3_url(model, key), timeout=30))
    return parse_idx(raw.decode("utf-8", "replace"))


def get_message(model: ModelDef, run: datetime, fh: int, e: IdxEntry) -> bytes:
    key = model.key(run, fh)
    rng = f"bytes={e.start}-" + ("" if e.end is None else str(e.end))
    return _cached("msg_" + _h(f"{model.bucket}/{key}/{e.start}/{e.end}"),
                   lambda: _http_get(s3_url(model, key), headers={"Range": rng}, timeout=90))


_RUNS: dict = {}            # model id -> (time fetched, [(run, sorted hours)])


def list_run(model: ModelDef, run: datetime) -> list:
    """Forecast hours of a run that are on the bucket now (their .idx is there)."""
    from ..data import aws
    objs, _ = aws.list_objects(model.bucket, model.prefix(run), max_pages=4)
    hours = {model.fh_of(o.key) for o in objs if o.key.endswith(".idx")}
    hours.discard(None)
    allowed = set(model.hours(run))
    return sorted(h for h in hours if h in allowed)


def latest_runs(model: ModelDef, count: int = 4, now: datetime | None = None, max_age_s: float = 120) -> list:
    """[(run time, [available hours])], newest first: the runs that have at least their first hours."""
    live = now is None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if live:
        cached = _RUNS.get(model.id)
        if cached and time.time() - cached[0] < max_age_s:
            return cached[1][:count]
    step = model.cycle_every
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % step)
    out = []
    tries = 0
    while len(out) < count and tries < count + 6:
        tries += 1
        if t <= now - timedelta(hours=model.delay_h * 0.25) or not live:
            try:
                hrs = list_run(model, t)
            except Exception:
                hrs = []
            if hrs and 0 in hrs:
                out.append((t, hrs))
        t -= timedelta(hours=step)
    if live:
        _RUNS[model.id] = (time.time(), out)
    return out


# --------------------------------------------------------------------------- the field catalog
@dataclass
class Spec:
    var: str
    level: str
    stat: str = "fcst"


S = Spec
K2F = lambda k: (k - 273.15) * 1.8 + 32.0          # noqa: E731
K2C = lambda k: k - 273.15                          # noqa: E731
MS2KT = 1.943844
G = 9.80665


@dataclass
class Recipe:
    inputs: dict                 # name -> [Spec, ...] (first found wins); names starting with '?' are optional
    compute: object              # f(d: dict name -> ndarray, grid) -> ndarray (or dict for vectors)
    note: str = ""


@dataclass
class FieldDef:
    id: str
    name: str
    group: str
    units: str
    stops: list                  # [(value, (r, g, b, a))], or palette name (str) for a radar colour table
    recipes: list
    desc: str = ""
    decimals: int = 0
    categorical: bool = False
    contour: tuple | None = None     # (input name, interval, label fmt, colour)
    barbs: tuple | None = None       # (u name, v name) of earth or grid winds in kt
    accum: str | None = None         # 'stepsum' recipe source for models without a running total
    zero_line: float | None = None   # draw this value as a bold contour (0 °C at 850 mb, 32 °F)


def _ramp(*pairs):
    return [(v, c if len(c) == 4 else (*c, 235)) for v, c in pairs]


CAPE_STOPS = _ramp((100, (150, 190, 150, 130)), (250, (120, 180, 100, 190)), (500, (60, 160, 60)),
                   (1000, (230, 225, 60)), (1500, (240, 180, 40)), (2000, (240, 120, 30)), (3000, (215, 30, 30)),
                   (4000, (200, 30, 150)), (5000, (140, 40, 200)), (6000, (235, 215, 255)))
CIN_STOPS = _ramp((-500, (20, 30, 110)), (-300, (40, 70, 170)), (-200, (60, 110, 210)), (-100, (110, 160, 235)),
                  (-50, (160, 200, 245)), (-25, (200, 225, 250, 180)), (-10, (220, 235, 255, 0)))
SRH_STOPS = _ramp((50, (170, 190, 210, 150)), (100, (110, 170, 220)), (150, (60, 180, 120)), (200, (230, 220, 60)),
                  (300, (240, 140, 40)), (400, (220, 40, 40)), (500, (200, 40, 160)), (700, (150, 60, 220)),
                  (1000, (245, 220, 255)))
SHEAR_STOPS = _ramp((20, (180, 200, 220, 140)), (30, (100, 170, 220)), (40, (60, 180, 110)), (50, (230, 220, 60)),
                    (60, (240, 140, 40)), (70, (220, 40, 40)), (80, (190, 40, 170)), (100, (240, 200, 255)))
STP_STOPS = _ramp((0.5, (170, 200, 230, 150)), (1, (90, 160, 230)), (2, (60, 180, 90)), (3, (230, 220, 60)),
                  (4, (240, 150, 40)), (6, (220, 40, 40)), (8, (200, 40, 170)), (10, (140, 60, 220)),
                  (15, (240, 210, 255)))
SCP_STOPS = _ramp((1, (170, 200, 230, 150)), (2, (90, 160, 230)), (4, (60, 180, 90)), (8, (230, 220, 60)),
                  (12, (240, 150, 40)), (16, (220, 40, 40)), (20, (200, 40, 170)), (30, (240, 210, 255)))
UH_STOPS = _ramp((25, (140, 170, 230, 200)), (50, (60, 180, 90)), (75, (230, 220, 60)), (100, (240, 140, 40)),
                 (150, (220, 40, 40)), (200, (200, 40, 170)), (300, (255, 255, 255)))
T_STOPS = _ramp((-40, (230, 210, 255)), (-20, (150, 80, 200)), (0, (70, 60, 190)), (20, (40, 120, 230)),
                (32, (110, 200, 240)), (40, (60, 170, 110)), (50, (130, 200, 80)), (60, (230, 225, 80)),
                (70, (245, 175, 50)), (80, (235, 110, 40)), (90, (210, 40, 40)), (100, (150, 20, 40)),
                (115, (240, 200, 220)))
TD_STOPS = _ramp((-10, (120, 90, 60)), (20, (150, 120, 80)), (40, (200, 180, 140)), (50, (170, 210, 140)),
                 (55, (110, 190, 100)), (60, (40, 160, 60)), (65, (20, 120, 50)), (70, (30, 150, 170)),
                 (75, (60, 90, 210)), (80, (150, 70, 200)))
RH_STOPS = _ramp((0, (140, 90, 40)), (20, (190, 150, 90)), (40, (230, 220, 180)), (60, (170, 215, 160)),
                 (80, (70, 170, 90)), (90, (30, 130, 70)), (100, (20, 90, 60)))
WIND_STOPS = _ramp((10, (180, 200, 220, 120)), (20, (110, 170, 220)), (30, (60, 180, 110)), (40, (230, 220, 60)),
                   (50, (240, 140, 40)), (60, (220, 40, 40)), (70, (190, 40, 170)), (90, (240, 200, 255)))
JET_STOPS = _ramp((50, (180, 200, 220, 130)), (70, (110, 170, 220)), (90, (60, 180, 110)), (110, (230, 220, 60)),
                  (130, (240, 140, 40)), (150, (220, 40, 40)), (170, (190, 40, 170)), (200, (240, 200, 255)))
PWAT_STOPS = _ramp((0.25, (150, 110, 70, 160)), (0.5, (200, 170, 110)), (0.75, (180, 210, 150)),
                   (1.0, (90, 180, 90)), (1.25, (40, 150, 80)), (1.5, (40, 150, 190)), (1.75, (50, 90, 210)),
                   (2.0, (130, 60, 200)), (2.5, (220, 60, 160)), (3.0, (250, 200, 240)))
LAPSE_STOPS = _ramp((5.5, (200, 200, 200, 0)), (6.0, (200, 210, 160, 150)), (6.5, (230, 220, 80)),
                    (7.0, (240, 170, 50)), (7.5, (230, 100, 40)), (8.0, (210, 40, 40)), (8.5, (190, 40, 160)),
                    (9.5, (240, 200, 255)))
LI_STOPS = _ramp((-14, (240, 200, 255)), (-10, (170, 50, 200)), (-8, (210, 40, 40)), (-6, (240, 130, 40)),
                 (-4, (230, 220, 60)), (-2, (120, 190, 100)), (0, (170, 200, 170, 120)), (0.01, (0, 0, 0, 0)))
QPF_STOPS = _ramp((0.01, (190, 230, 190, 200)), (0.1, (120, 200, 120)), (0.25, (40, 160, 60)),
                  (0.5, (30, 110, 180)), (0.75, (60, 60, 200)), (1.0, (150, 60, 200)), (1.5, (220, 50, 160)),
                  (2.0, (200, 30, 40)), (3.0, (240, 130, 40)), (4.0, (240, 220, 60)), (6.0, (255, 255, 200)),
                  (10.0, (255, 255, 255)))
SNOW_STOPS = _ramp((0.1, (200, 225, 245, 180)), (1, (150, 200, 240)), (2, (90, 160, 230)), (4, (40, 100, 210)),
                   (6, (40, 50, 170)), (8, (120, 60, 200)), (12, (190, 70, 210)), (18, (230, 110, 200)),
                   (24, (250, 170, 220)), (36, (230, 230, 230)), (48, (180, 180, 180)))
ICE_STOPS = _ramp((0.01, (240, 200, 230, 200)), (0.05, (230, 150, 210)), (0.1, (210, 90, 190)),
                  (0.25, (170, 40, 170)), (0.5, (120, 20, 150)), (0.75, (80, 10, 110)), (1.0, (40, 0, 60)))
VORT_STOPS = _ramp((8, (240, 230, 160, 120)), (12, (240, 210, 80)), (16, (240, 150, 40)), (20, (220, 70, 40)),
                   (28, (190, 30, 90)), (36, (150, 30, 160)), (50, (240, 200, 255)))
H500_STOPS = _ramp((30, (180, 200, 220, 120)), (40, (110, 170, 220)), (50, (60, 180, 110)), (60, (230, 220, 60)),
                   (70, (240, 150, 40)), (85, (220, 40, 40)), (100, (190, 40, 170)), (130, (240, 200, 255)))
T850_STOPS = _ramp((-30, (230, 210, 255)), (-20, (150, 80, 200)), (-10, (60, 70, 200)), (-5, (60, 130, 230)),
                   (0, (120, 200, 240)), (5, (70, 180, 110)), (10, (150, 210, 80)), (15, (235, 220, 70)),
                   (20, (240, 150, 40)), (25, (215, 50, 40)), (30, (150, 20, 50)))
# precipitation type: values = 100 * type + dBZ (rain 0xx, snow 1xx, sleet 2xx, freezing rain 3xx)
PTYPE_NAMES = ("Rain", "Snow", "Sleet", "Freezing rain")
PTYPE_STOPS = _ramp(
    (10, (130, 220, 120)), (20, (40, 170, 50)), (30, (20, 110, 30)), (35, (240, 230, 60)), (45, (240, 140, 30)),
    (55, (210, 30, 30)), (65, (130, 10, 20)), (99, (130, 10, 20)),
    (110, (180, 215, 245)), (20 + 100, (110, 170, 235)), (30 + 100, (40, 100, 220)), (40 + 100, (20, 40, 160)),
    (199, (20, 30, 120)),
    (210, (250, 200, 160)), (25 + 200, (245, 150, 90)), (35 + 200, (225, 100, 40)), (299, (170, 60, 20)),
    (310, (245, 180, 235)), (25 + 300, (220, 110, 210)), (35 + 300, (170, 40, 170)), (399, (100, 10, 110)))


def _speed(u, v):
    return np.hypot(u, v) * MS2KT


def _lcl_m(t2, td2):
    return np.maximum(125.0 * (t2 - td2), 0.0)


def _stp(d, _g):
    cape = np.maximum(d["cape"], 0)
    lcl = _lcl_m(d["t2"], d["td2"])
    lcl_t = np.clip((2000.0 - lcl) / 1000.0, 0.0, 1.0)
    shear = np.hypot(d["su"], d["sv"])                       # m/s
    sh_t = np.where(shear < 12.5, 0.0, np.minimum(shear, 30.0) / 20.0)
    return (cape / 1500.0) * lcl_t * (d["srh1"] / 150.0) * sh_t


def _stp_500(d, g):
    return _stp(dict(d, su=d["u5"] - d["u10"], sv=d["v5"] - d["v10"]), g)


def _scp(d, _g):
    cape = np.maximum(d["cape"], 0)
    shear = np.hypot(d["su"], d["sv"])
    sh_t = np.where(shear < 10, 0.0, np.minimum(shear / 20.0, 1.0))
    return (cape / 1000.0) * (d["srh3"] / 50.0) * sh_t


def _scp_500(d, g):
    return _scp(dict(d, su=d["u5"] - d["u10"], sv=d["v5"] - d["v10"]), g)


def _vort(d, g):
    """Absolute vorticity (1e-5 /s) from winds on the native grid (earth-relative winds, map factor ignored)."""
    u, v = d["u"], d["v"]
    rows, cols = np.indices(u.shape)
    lat, _lon = g.latlon(rows, cols)
    if g.lcc is not None:
        dx = dy = g.lcc["dx"]
        sign_y = 1.0 if g.scan & 0x40 else -1.0
        rel = np.gradient(v, axis=1) / dx - np.gradient(u, axis=0) / (dy * sign_y)
    else:
        dx = 6371229.0 * np.cos(np.radians(lat)) * math.radians(g.di)
        dy = 6371229.0 * math.radians(g.dj) * (1.0 if g.scan & 0x40 else -1.0)
        rel = np.gradient(v, axis=1) / np.maximum(dx, 1.0) - np.gradient(u, axis=0) / dy
    f = 2 * 7.292e-5 * np.sin(np.radians(lat))
    return (rel + f) * 1e5


def _ptype(d, _g):
    dbz = np.nan_to_num(d["refc"], nan=-99.0)
    t = np.full(dbz.shape, 0.0)
    if "csnow" in d:
        t = np.where(d["csnow"] > 0.5, 1.0, t)
    if "cicep" in d:
        t = np.where(d["cicep"] > 0.5, 2.0, t)
    if "cfrzr" in d:
        t = np.where(d["cfrzr"] > 0.5, 3.0, t)
    none = np.ones(dbz.shape, bool)
    for k in ("crain", "csnow", "cicep", "cfrzr"):
        if k in d:
            none &= ~(d[k] > 0.5)
    if "t2" in d:                                # echo without a model precip type: snow if it's below freezing
        t = np.where(none & (d["t2"] <= 273.15), 1.0, t)
    out = np.where(dbz >= 10, 100.0 * t + np.clip(dbz, 10, 99), np.nan)
    return out


FIELDS: "OrderedDict[str, FieldDef]" = OrderedDict()


def _f(*a, **k):
    fd = FieldDef(*a, **k)
    FIELDS[fd.id] = fd
    return fd


R = Recipe
# ---- severe
_f("refc", "Simulated radar (composite)", "Severe", "dBZ", "REF",
   [R({"refc": [S("REFC", "entire atmosphere"), S("REFC", "entire atmosphere (considered as a single layer)")]},
      lambda d, g: np.where(d["refc"] >= 5, d["refc"], np.nan))],
   "What the radar would show if the model is right: the strongest echo in each column.")
_f("cape_sb", "Surface-based CAPE", "Severe", "J/kg", CAPE_STOPS,
   [R({"cape": [S("CAPE", "surface")]}, lambda d, g: d["cape"])],
   "Energy for storms, for a parcel lifted from the ground. 1000+ is enough for strong storms, 3000+ is a lot.")
_f("cape_ml", "Mixed-layer CAPE", "Severe", "J/kg", CAPE_STOPS,
   [R({"cape": [S("CAPE", "90-0 mb above ground")]}, lambda d, g: d["cape"])],
   "CAPE for a parcel mixed through the lowest 90 hPa – the usual measure of how unstable the air is.")
_f("cape_mu", "Most-unstable CAPE", "Severe", "J/kg", CAPE_STOPS,
   [R({"cape": [S("CAPE", "255-0 mb above ground"), S("CAPE", "180-0 mb above ground")]}, lambda d, g: d["cape"])],
   "CAPE of the most unstable parcel in the lowest 255 hPa – includes storms rooted above the ground (elevated).")
_f("cin_sb", "Surface-based CIN", "Severe", "J/kg", CIN_STOPS,
   [R({"cin": [S("CIN", "surface")]}, lambda d, g: np.where(d["cin"] < -10, d["cin"], np.nan))],
   "The 'cap': energy holding surface parcels down. Below -50, storms struggle to form from the ground; "
   "below -200 the cap is strong.")
_f("srh01", "0–1 km helicity", "Severe", "m²/s²", SRH_STOPS,
   [R({"h": [S("HLCY", "1000-0 m above ground")]}, lambda d, g: d["h"])],
   "Storm-relative helicity in the lowest kilometre: low-level spin a storm can tilt up. 100+ supports tornadoes, "
   "200+ strongly.")
_f("srh03", "0–3 km helicity", "Severe", "m²/s²", SRH_STOPS,
   [R({"h": [S("HLCY", "3000-0 m above ground")]}, lambda d, g: d["h"])],
   "Storm-relative helicity in the lowest 3 km: rotation potential for supercells. 150+ favours supercells, 300+ is high.")
_f("shear06", "0–6 km bulk shear", "Severe", "kt", SHEAR_STOPS,
   [R({"su": [S("VUCSH", "0-6000 m above ground")], "sv": [S("VVCSH", "0-6000 m above ground")]},
      lambda d, g: {"value": _speed(d["su"], d["sv"]), "u": d["su"] * MS2KT, "v": d["sv"] * MS2KT}),
    R({"u5": [S("UGRD", "500 mb")], "v5": [S("VGRD", "500 mb")], "u10": [S("UGRD", "10 m above ground")],
       "v10": [S("VGRD", "10 m above ground")]},
      lambda d, g: {"value": _speed(d["u5"] - d["u10"], d["v5"] - d["v10"]), "u": (d["u5"] - d["u10"]) * MS2KT,
                    "v": (d["v5"] - d["v10"]) * MS2KT},
      "10 m to 500 hPa")],
   "Difference between the wind at the ground and at 6 km. 35+ kt organises storms into supercells; arrows show "
   "the shear vector.", barbs=("u", "v"))
_f("stp", "Significant tornado parameter", "Severe", "", STP_STOPS,
   [R({"cape": [S("CAPE", "surface")], "srh1": [S("HLCY", "1000-0 m above ground")],
       "su": [S("VUCSH", "0-6000 m above ground")], "sv": [S("VVCSH", "0-6000 m above ground")],
       "t2": [S("TMP", "2 m above ground")], "td2": [S("DPT", "2 m above ground")]}, _stp),
    R({"cape": [S("CAPE", "surface")], "srh1": [S("HLCY", "1000-0 m above ground"), S("HLCY", "3000-0 m above ground")],
       "u5": [S("UGRD", "500 mb")], "v5": [S("VGRD", "500 mb")], "u10": [S("UGRD", "10 m above ground")],
       "v10": [S("VGRD", "10 m above ground")], "t2": [S("TMP", "2 m above ground")],
       "td2": [S("DPT", "2 m above ground")]}, _stp_500, "approximate: 10 m–500 hPa shear")],
   "Fixed-layer STP: CAPE, cloud base, 0–1 km helicity and deep shear together. Above 1, the ingredients for "
   "significant tornadoes (EF2+) are there.", decimals=1)
_f("scp", "Supercell composite", "Severe", "", SCP_STOPS,
   [R({"cape": [S("CAPE", "255-0 mb above ground"), S("CAPE", "180-0 mb above ground"), S("CAPE", "surface")],
       "srh3": [S("HLCY", "3000-0 m above ground")],
       "su": [S("VUCSH", "0-6000 m above ground")], "sv": [S("VVCSH", "0-6000 m above ground")]}, _scp),
    R({"cape": [S("CAPE", "255-0 mb above ground"), S("CAPE", "180-0 mb above ground"), S("CAPE", "surface")],
       "srh3": [S("HLCY", "3000-0 m above ground")], "u5": [S("UGRD", "500 mb")], "v5": [S("VGRD", "500 mb")],
       "u10": [S("UGRD", "10 m above ground")], "v10": [S("VGRD", "10 m above ground")]}, _scp_500,
      "approximate: 10 m–500 hPa shear")],
   "Supercell composite parameter: most-unstable CAPE, 0–3 km helicity and deep shear. Above 1 favours supercells.",
   decimals=1)
_f("uh25", "Updraft helicity (2–5 km)", "Severe", "m²/s²", UH_STOPS,
   [R({"uh": [S("MXUPHL", "5000-2000 m above ground", "max1"), S("MXUPHL", "5000-2000 m above ground", "any")]},
      lambda d, g: np.where(d["uh"] >= 25, d["uh"], np.nan))],
   "The strongest rotating updraft in the past hour – the model's own supercells. Tracks of 75+ mark storms "
   "the model rotates strongly.")
_f("lapse75", "700–500 mb lapse rate", "Severe", "°C/km", LAPSE_STOPS,
   [R({"t7": [S("TMP", "700 mb")], "t5": [S("TMP", "500 mb")], "z7": [S("HGT", "700 mb")], "z5": [S("HGT", "500 mb")]},
      lambda d, g: (d["t7"] - d["t5"]) / np.maximum(d["z5"] - d["z7"], 1.0) * 1000.0)],
   "How fast it cools with height between about 3 and 5.5 km. 7+ °C/km is steep (an elevated mixed layer): "
   "big CAPE and hail.", decimals=1)
_f("li", "Lifted index", "Severe", "°C", LI_STOPS,
   [R({"li": [S("LFTX", "500-1000 mb"), S("LFTX", "surface")]}, lambda d, g: d["li"])],
   "Temperature of the air at 500 hPa minus a lifted surface parcel's. Negative is unstable; -6 or lower is very unstable.",
   decimals=1)
# ---- surface
_f("t2m", "2 m temperature", "Surface", "°F", T_STOPS,
   [R({"t": [S("TMP", "2 m above ground")]}, lambda d, g: K2F(d["t"]))],
   "Air temperature at 2 m (the height a thermometer is at).", zero_line=32.0)
_f("td2m", "2 m dewpoint", "Surface", "°F", TD_STOPS,
   [R({"t": [S("DPT", "2 m above ground")]}, lambda d, g: K2F(d["t"]))],
   "How much moisture is in the air. Dewpoints of 60 °F+ feed severe storms; 70 °F+ is tropical.")
_f("rh2m", "2 m relative humidity", "Surface", "%", RH_STOPS,
   [R({"rh": [S("RH", "2 m above ground")]}, lambda d, g: d["rh"])],
   "How close the air is to saturation. Low values dry storms out and feed fires; near 100% means fog or rain.")
_f("wind10", "10 m wind", "Surface", "kt", WIND_STOPS,
   [R({"u": [S("UGRD", "10 m above ground")], "v": [S("VGRD", "10 m above ground")]},
      lambda d, g: {"value": _speed(d["u"], d["v"]), "u": d["u"] * MS2KT, "v": d["v"] * MS2KT})],
   "Sustained wind at 10 m, with barbs.", barbs=("u", "v"))
_f("gust", "Wind gusts", "Surface", "kt", WIND_STOPS,
   [R({"g": [S("GUST", "surface")]}, lambda d, g: d["g"] * MS2KT)], "Expected gusts at the surface.")
_f("mslp", "Sim. radar & sea-level pressure", "Surface", "dBZ", "REF",
   [R({"refc": [S("REFC", "entire atmosphere"), S("REFC", "entire atmosphere (considered as a single layer)")],
       "p": [S("MSLMA", "mean sea level"), S("PRMSL", "mean sea level"), S("MSLET", "mean sea level")]},
      lambda d, g: {"value": np.where(d["refc"] >= 5, d["refc"], np.nan), "p": d["p"] / 100.0})],
   "Simulated radar with isobars every 4 hPa: lows, highs and fronts.", contour=("p", 4.0, "{:.0f}", (235, 235, 235)))
_f("pwat", "Precipitable water", "Surface", "in", PWAT_STOPS,
   [R({"pw": [S("PWAT", "entire atmosphere (considered as a single layer)")]}, lambda d, g: d["pw"] / 25.4)],
   "All the water vapour above a point, as inches of rain. 1.5 in+ means heavy rain and flooding are possible.",
   decimals=2)
# ---- upper air
_f("h500", "500 mb heights & wind", "Upper air", "kt", H500_STOPS,
   [R({"z": [S("HGT", "500 mb")], "u": [S("UGRD", "500 mb")], "v": [S("VGRD", "500 mb")]},
      lambda d, g: {"value": _speed(d["u"], d["v"]), "z": d["z"] / 10.0, "u": d["u"] * MS2KT, "v": d["v"] * MS2KT})],
   "The mid-level steering pattern (about 5.5 km): troughs, ridges and the jet. Heights every 6 decametres.",
   contour=("z", 6.0, "{:.0f}", (15, 15, 15)), barbs=("u", "v"))
_f("vort500", "500 mb vorticity", "Upper air", "10⁻⁵/s", VORT_STOPS,
   [R({"a": [S("ABSV", "500 mb")], "z": [S("HGT", "500 mb")]},
      lambda d, g: {"value": d["a"] * 1e5, "z": d["z"] / 10.0}),
    R({"u": [S("UGRD", "500 mb")], "v": [S("VGRD", "500 mb")], "z": [S("HGT", "500 mb")]},
      lambda d, g: {"value": _vort(d, g), "z": d["z"] / 10.0}, "from the model winds")],
   "Spin in the mid-level flow; shortwaves (lift for storms) show up as maxima moving through the pattern.",
   contour=("z", 6.0, "{:.0f}", (15, 15, 15)))
_f("t850", "850 mb temperature & wind", "Upper air", "°C", T850_STOPS,
   [R({"t": [S("TMP", "850 mb")], "z": [S("HGT", "850 mb")], "u": [S("UGRD", "850 mb")], "v": [S("VGRD", "850 mb")]},
      lambda d, g: {"value": K2C(d["t"]), "z": d["z"] / 10.0, "u": d["u"] * MS2KT, "v": d["v"] * MS2KT})],
   "Temperature about 1.5 km up, with the low-level jet. The 0 °C line is the rain / snow line aloft.",
   contour=("z", 3.0, "{:.0f}", (15, 15, 15)), barbs=("u", "v"), zero_line=0.0)
_f("w250", "250 mb jet stream", "Upper air", "kt", JET_STOPS,
   [R({"z": [S("HGT", "250 mb")], "u": [S("UGRD", "250 mb")], "v": [S("VGRD", "250 mb")]},
      lambda d, g: {"value": _speed(d["u"], d["v"]), "z": d["z"] / 10.0, "u": d["u"] * MS2KT, "v": d["v"] * MS2KT})],
   "The jet stream near 10 km. Storms often form under the jet's exit and entrance regions.",
   contour=("z", 12.0, "{:.0f}", (15, 15, 15)), barbs=("u", "v"))
# ---- precipitation and winter
_f("qpf", "Total precipitation", "Precip & winter", "in", QPF_STOPS,
   [R({"p": [S("APCP", "surface", "acc0")]}, lambda d, g: d["p"] / 25.4)],
   "All rain and melted snow from the start of the run.", decimals=2, accum="APCP")
_f("ptype", "Precipitation type", "Precip & winter", "dBZ", PTYPE_STOPS,
   [R({"refc": [S("REFC", "entire atmosphere"), S("REFC", "entire atmosphere (considered as a single layer)")],
       "?crain": [S("CRAIN", "surface")], "?csnow": [S("CSNOW", "surface")], "?cicep": [S("CICEP", "surface")],
       "?cfrzr": [S("CFRZR", "surface")], "?t2": [S("TMP", "2 m above ground")]}, _ptype)],
   "Simulated radar coloured by what's falling: rain, snow, sleet or freezing rain.", categorical=True)
_f("snow10", "Total snowfall (10:1)", "Precip & winter", "in", SNOW_STOPS,
   [R({"w": [S("WEASD", "surface", "acc0")]}, lambda d, g: d["w"] * 10.0 / 25.4)],
   "Snow from the start of the run, at 10 inches of snow per inch of water (wetter snow is less, powder more).",
   decimals=1, accum="CSNOW")
_f("asnow", "Total snowfall (model)", "Precip & winter", "in", SNOW_STOPS,
   [R({"s": [S("ASNOW", "surface", "acc0")]}, lambda d, g: d["s"] * 39.3701)],
   "The model's own snowfall, with its own snow-to-liquid ratio.", decimals=1)
_f("frzr", "Freezing rain (ice)", "Precip & winter", "in", ICE_STOPS,
   [R({"f": [S("FRZR", "surface", "acc0")]}, lambda d, g: d["f"] / 25.4)],
   "Liquid-equivalent freezing rain from the start of the run. A quarter inch of ice brings down trees and power lines.",
   decimals=2, accum="CFRZR")
_f("snod", "Snow depth", "Precip & winter", "in", SNOW_STOPS,
   [R({"s": [S("SNOD", "surface")]}, lambda d, g: d["s"] * 39.3701)], "Snow on the ground.", decimals=1)

GROUPS = ["Severe", "Surface", "Upper air", "Precip & winter"]


# --------------------------------------------------------------------------- computing a field
class FieldUnavailable(RuntimeError):
    pass


@dataclass
class ModelField:
    fid: str
    model: str
    run: datetime
    fh: int
    valid: datetime
    grid: grib2.GribGrid          # geometry of the native grid (values = the main field)
    value: np.ndarray
    extra: dict = field(default_factory=dict)       # contour / barb grids (native)
    note: str = ""

    @property
    def fdef(self) -> FieldDef:
        return FIELDS[self.fid]


_GRIDS: "OrderedDict" = OrderedDict()           # decoded messages (they are big: keep a few)
_GRID_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model")


def _decoded(model, run, fh, entry: IdxEntry) -> grib2.GribGrid:
    k = (model.id, run, fh, entry.start)
    with _GRID_LOCK:
        g = _GRIDS.get(k)
        if g is not None:
            _GRIDS.move_to_end(k)
            return g
    g = grib2.decode(get_message(model, run, fh, entry))
    with _GRID_LOCK:
        _GRIDS[k] = g
        while len(_GRIDS) > 36:
            _GRIDS.popitem(last=False)
    return g


def _earth_winds(g: grib2.GribGrid, u, v):
    """Grid-relative Lambert winds -> earth-relative (east, north)."""
    if g.lcc is None or not g.lcc.get("uv_grid", True):
        return u, v
    rows, cols = np.indices(u.shape)
    _lat, lon = g.latlon(rows, cols)
    a = g.lcc["n"] * np.radians((lon - g.lcc["lov"] + 540.0) % 360.0 - 180.0)
    ca, sa = np.cos(a), np.sin(a)
    return (ca * u + sa * v).astype(np.float32), (-sa * u + ca * v).astype(np.float32)


_WIND_PAIRS = (("u", "v"), ("u5", "v5"), ("u10", "v10"), ("su", "sv"))


def compute(model_id: str, run: datetime, fh: int, fid: str, rows: list | None = None) -> ModelField:
    """Downloads what a field needs for one forecast hour and computes it on the model's own grid."""
    model = MODELS[model_id]
    fd = FIELDS[fid]
    rows = rows if rows is not None else get_idx(model, run, fh)
    last_err = None
    for rec in fd.recipes:
        picks = {}
        ok = True
        for name, specs in rec.inputs.items():
            opt = name.startswith("?")
            key = name.lstrip("?")
            hit = None
            for sp in specs:
                hit = find_entry(rows, sp.var, sp.level, sp.stat, fh=fh)
                if hit is None and sp.stat == "fcst" and fh == 0:
                    hit = find_entry(rows, sp.var, sp.level, "any")
                if hit is not None:
                    break
            if hit is None:
                if opt:
                    continue
                ok = False
                last_err = f"{model.name} has no {specs[0].var} at {specs[0].level}"
                break
            picks[key] = hit
        if not ok:
            continue
        grids = dict(zip(picks, _POOL.map(lambda e: _decoded(model, run, fh, e), picks.values())))
        shapes = {g.values.shape for g in grids.values()}
        if len(shapes) != 1:
            raise FieldUnavailable("the inputs are on different grids")
        g0 = next(iter(grids.values()))
        d = {k: g.values for k, g in grids.items()}
        for a, b in _WIND_PAIRS:
            if a in d and b in d:
                d[a], d[b] = _earth_winds(g0, d[a], d[b])
        with np.errstate(invalid="ignore", divide="ignore"):
            out = rec.compute(d, g0)
        extra = {}
        if isinstance(out, dict):
            extra = {k: np.asarray(v, np.float32) for k, v in out.items() if k != "value"}
            out = out["value"]
        valid = run + timedelta(hours=fh)
        return ModelField(fid, model_id, run, fh, valid, g0, np.asarray(out, np.float32), extra, rec.note)
    if fd.accum:
        return accumulate(model_id, run, fh, fid)
    raise FieldUnavailable(last_err or f"{fd.name} isn't available from {model.name}")


# ---- totals for models that only give each step's amount (GFS)
def _increment(model, run, prev, cur, var="APCP"):
    """Amount of an accumulated variable over (prev, cur] hours, from whatever buckets the files carry."""
    rows = get_idx(model, run, cur)
    accs = [r for r in rows if r.var == var and r.level == "surface" and r.stat == "acc" and r.acc_span]
    for r in accs:
        if r.acc_span == (prev, cur):
            return _decoded(model, run, cur, r).values
    for r in sorted(accs, key=lambda r: r.acc_span[0]):
        a, b = r.acc_span
        if b != cur or a > prev:
            continue
        total = _decoded(model, run, cur, r).values
        if a == prev:
            return total
        prow = find_entry(get_idx(model, run, prev), var, "surface", "accspan", span=(a, prev))
        if prow is None:
            continue
        return total - _decoded(model, run, prev, prow).values
    raise FieldUnavailable(f"no {var} amount for hours {prev}-{cur}")


def accumulate(model_id, run, fh, fid, progress=None) -> ModelField:
    """Sums each step's precipitation (times the share of it that's snow / freezing rain) up to fh."""
    model = MODELS[model_id]
    fd = FIELDS[fid]
    if fh == 0:
        raise FieldUnavailable("nothing has fallen yet at hour 0")
    avail = [h for h in latest_hours(model, run) if 0 < h <= fh]
    if fh not in avail:
        raise FieldUnavailable(f"hour {fh} isn't out yet")
    total = None
    prev = 0
    g0 = None
    for i, h in enumerate(avail):
        inc = _increment(model, run, prev, h)
        if fd.accum in ("CSNOW", "CFRZR"):
            rows = get_idx(model, run, h)
            e = (find_entry(rows, fd.accum, "surface", "ave", fh=h) or find_entry(rows, fd.accum, "surface", "fcst"))
            if e is None:
                raise FieldUnavailable(f"{model.name} has no {fd.accum}")
            share = np.clip(_decoded(model, run, h, e).values, 0, 1)
            inc = inc * share
        total = inc.copy() if total is None else total + inc
        if g0 is None:
            g0 = _decoded(model, run, h, find_entry(get_idx(model, run, h), "APCP", "surface", "accspan"))
        prev = h
        if progress:
            progress(i + 1, len(avail))
    mm = np.maximum(total, 0)
    value = mm * 10.0 / 25.4 if fd.accum == "CSNOW" else mm / 25.4
    return ModelField(fid, model_id, run, fh, run + timedelta(hours=fh), g0, value.astype(np.float32), {},
                      "summed from each step")


def latest_hours(model, run):
    for r, hrs in _RUNS.get(model.id, (0, []))[1]:
        if r == run:
            return hrs
    return list_run(model, run)


# --------------------------------------------------------------------------- drawing helpers
# how a radar colour table name ("REF") becomes a ColorTable: the app sets this to use the user's own tables
PALETTE_RESOLVER = None


def _palette(name):
    try:
        if PALETTE_RESOLVER is not None:
            return PALETTE_RESOLVER(name)
        from ..products import colortable
        return colortable.builtin(name)
    except Exception:
        return None


def colorize(fd: FieldDef, values: np.ndarray) -> np.ndarray:
    from . import georaster
    if isinstance(fd.stops, str):
        ct = _palette(fd.stops)
        if ct is not None:
            rgba = np.zeros(values.shape + (4,), np.uint8)
            ok = np.isfinite(values)
            if ok.any():
                rgba[ok] = ct.color_at(values[ok])
            return rgba
        return georaster.colorize(values, REF_STOPS)
    return georaster.colorize(values, fd.stops)


REF_STOPS = _ramp((5, (40, 80, 90, 120)), (15, (0, 155, 100)), (30, (0, 60, 0)), (31, (255, 213, 0)),
                  (45, (250, 0, 0)), (58, (60, 0, 10)), (62, (255, 255, 255)), (75, (0, 0, 0)))


def aeqd_sample(mf: ModelField, lat0, lon0, half_km, n, key="value"):
    """The field resampled onto the view's azimuthal-equidistant square (row 0 = north)."""
    from . import georaster
    lat, lon = georaster.aeqd_grid(lat0, lon0, half_km, n)
    src = mf.value if key == "value" else mf.extra[key]
    g = mf.grid
    tmp = grib2.GribGrid(values=src, ni=g.ni, nj=g.nj, la1=g.la1, lo1=g.lo1, la2=g.la2, lo2=g.lo2, di=g.di,
                         dj=g.dj, scan=g.scan, lcc=g.lcc)
    return tmp.sample(lat, lon, bilinear=not mf.fdef.categorical)


def contour_lines(values: np.ndarray, half_km: float, interval: float):
    """[(level, Nx2 array of world km)] isolines of an aeqd-sampled grid."""
    from skimage import measure
    v = np.asarray(values, np.float64)
    ok = np.isfinite(v)
    if not ok.any():
        return []
    n = v.shape[0]
    lo, hi = np.nanmin(v), np.nanmax(v)
    fill = np.where(ok, v, np.nanmean(v))
    out = []
    start = math.ceil(lo / interval) * interval
    lev = start
    while lev <= hi and len(out) < 4000:
        for c in measure.find_contours(fill, lev, mask=ok):
            if len(c) < 3:
                continue
            x = -half_km + (c[:, 1] + 0.5) * (2 * half_km / n)
            y = half_km - (c[:, 0] + 0.5) * (2 * half_km / n)
            out.append((lev, np.stack([x, y], 1)))
        lev += interval
    return out


def fmt_value(fd: FieldDef, v: float) -> str:
    if fd.id in ("ptype",) and math.isfinite(v):
        kind = PTYPE_NAMES[int(v // 100) % 4]
        return f"{kind}, {v % 100:.0f} dBZ"
    return f"{v:.{fd.decimals}f} {fd.units}".strip()


def colortable_for(fd: FieldDef):
    """A ColorTable carrying the field's colours, so the panels' colour bar can draw it."""
    from ..products.colortable import ColorTable, Entry
    if isinstance(fd.stops, str):
        ct = _palette(fd.stops)
        if ct is not None:
            return ct
        stops = REF_STOPS
    else:
        stops = fd.stops
    ents = [Entry(float(v), tuple(c), None, False) for v, c in stops]
    ct = ColorTable(name=fd.name, product=fd.id, units=fd.units, scale=1.0, offset=0.0, entries=ents,
                    decimals=fd.decimals, rf=None)
    if fd.id == "ptype":
        ct.labels = {25.0 + 100 * i: name for i, name in enumerate(PTYPE_NAMES)}
        ct.units = ""
    return ct


REGIONS = OrderedDict([
    # name: (lat, lon, width km)
    ("CONUS", (38.5, -97.0, 4900)),
    ("Northeast", (42.5, -74.5, 1300)),
    ("Mid-Atlantic", (38.8, -77.5, 1100)),
    ("Southeast", (32.5, -84.5, 1500)),
    ("Great Lakes", (43.5, -85.5, 1400)),
    ("Midwest", (40.5, -90.5, 1400)),
    ("Northern Plains", (45.5, -99.5, 1500)),
    ("Central Plains", (39.0, -98.0, 1300)),
    ("Southern Plains", (33.5, -98.5, 1300)),
    ("Gulf Coast", (30.5, -89.0, 1500)),
    ("Southwest", (34.5, -111.5, 1500)),
    ("Northwest", (45.0, -119.5, 1500)),
    ("California", (37.0, -120.0, 1300)),
    ("Atlantic tropics", (24.0, -70.0, 5500)),
])
