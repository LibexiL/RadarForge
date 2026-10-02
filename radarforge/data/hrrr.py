"""HRRR model soundings from NOAA's public AWS bucket.

A HRRR file is one big GRIB2 file with a text index (.idx) listing every message and where it starts, so a
sounding at one point needs only a few dozen of the messages (temperature, humidity and wind at each pressure
level, plus the surface), fetched with HTTP range requests and decoded by data/grib.py. Heights come from the
hypsometric equation, which saves a fifth of the download.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from . import aws, grib

BUCKET = "noaa-hrrr-bdp-pds"
MAX_FORECAST_HOUR = 18                       # the hourly runs; every sixth run goes to 48 hours
VARIABLES = ("TMP", "RH", "UGRD", "VGRD")     # at each pressure level
RD, G = 287.04, 9.80665


def thinned_levels(levels) -> list:
    """The pressure levels to use: every 25 mb from 1000 to 700 mb, then every 50 mb up to 100 mb (the 1013.2 mb
    entry is a pseudo level under the surface)."""
    return sorted((p for p in levels if 100 <= p <= 1000.5 and (p >= 700 or abs(p / 50 - round(p / 50)) < 1e-6)),
                  reverse=True)


@dataclass(frozen=True)
class Entry:
    var: str
    level: str                   # "500 mb", "2 m above ground", "surface"
    start: int
    end: int                     # inclusive; -1 for the last message (its end is unknown until fetched)


def key(run: datetime, fhr: int, kind: str) -> str:
    """Object name of a HRRR file: kind 'prs' (pressure levels) or 'sfc' (surface)."""
    return f"hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z.wrf{kind}f{fhr:02d}.grib2"


def parse_idx(text: str) -> list:
    """The .idx lines ('12:3456789:d=2026100220:TMP:500 mb:anl:') as entries with byte ranges."""
    rows = []
    for line in text.splitlines():
        f = line.strip().split(":")
        if len(f) >= 5 and f[1].isdigit():
            rows.append((int(f[1]), f[3], f[4]))
    return [Entry(var, level, off, (rows[i + 1][0] - 1) if i + 1 < len(rows) else -1)
            for i, (off, var, level) in enumerate(rows)]


def read_idx(run: datetime, fhr: int, kind: str) -> list:
    r = aws.session().get(f"{aws.bucket_url(BUCKET)}/{key(run, fhr, kind)}.idx", timeout=30)
    r.raise_for_status()
    return parse_idx(r.text)


def run_exists(run: datetime, fhr: int = 0) -> bool:
    objs, _ = aws.list_objects(BUCKET, key(run, fhr, "prs") + ".idx", max_pages=1)
    return bool(objs)


def latest_run(now: datetime | None = None, fhr: int = 0, tries: int = 8) -> datetime | None:
    """The newest hourly run whose pressure-level file for forecast hour `fhr` is complete."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(tries):
        if run_exists(run, fhr):
            return run
        run -= timedelta(hours=1)
    return None


def pressure(level: str) -> float | None:
    """500.0 from '500 mb' (None for anything else)."""
    parts = level.split()
    if len(parts) == 2 and parts[1] == "mb":
        try:
            return float(parts[0])
        except ValueError:
            return None
    return None


def plan(prs: list, sfc: list) -> tuple:
    """(pressure-level entries to fetch, surface entries to fetch) for a sounding."""
    levels = thinned_levels({p for e in prs if e.var == "TMP" and (p := pressure(e.level)) is not None})
    keep = set(levels)
    pl = [e for e in prs if e.var in VARIABLES and pressure(e.level) in keep]
    want = {("PRES", "surface"), ("HGT", "surface"), ("TMP", "2 m above ground"), ("RH", "2 m above ground"),
            ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")}
    sf = [e for e in sfc if (e.var, e.level) in want]
    return pl, sf


def earth_relative(u, v, lon, lov: float, latin: float):
    """HRRR winds blow along the grid's axes; this turns them to east / north. The grid is rotated against the
    meridians by n * (longitude - LoV), n = sin(Latin) for a Lambert conformal projection."""
    g = math.radians(math.sin(math.radians(latin)) * (((lon - lov) + 180.0) % 360.0 - 180.0))
    return u * math.cos(g) + v * math.sin(g), v * math.cos(g) - u * math.sin(g)


def heights(p, t_k, rh, psfc, zsfc, t2m, rh2m) -> np.ndarray:
    """Geopotential height (m) at each level (p in hPa, from the ground up) by the hypsometric equation with
    virtual temperature, starting from the surface."""
    def tv(tk, r, pp):
        es = 6.112 * np.exp(17.67 * (tk - 273.15) / (tk - 29.65))
        e = np.clip(r, 0, 100) / 100.0 * es
        w = 0.622 * e / np.maximum(pp - e, 1.0)
        return tk * (1.0 + 0.61 * w)
    pp = np.concatenate(([psfc], p))
    tvs = np.concatenate(([tv(t2m, rh2m, psfc)], tv(np.asarray(t_k, float), np.asarray(rh, float), np.asarray(p, float))))
    mean_tv = 0.5 * (tvs[1:] + tvs[:-1])
    dz = RD / G * mean_tv * np.log(pp[:-1] / pp[1:])
    return zsfc + np.cumsum(dz)


@dataclass
class Raw:
    """What came out of a HRRR run at one point (units as in the files: K, %, m/s, hPa)."""
    run: datetime
    fhr: int
    lat: float
    lon: float
    p: np.ndarray                      # hPa, from the ground up
    t: np.ndarray                      # K
    rh: np.ndarray                     # %
    u: np.ndarray                      # m/s, east
    v: np.ndarray                      # m/s, north
    psfc: float
    zsfc: float
    t2m: float
    rh2m: float
    u10: float
    v10: float

    @property
    def valid(self) -> datetime:
        return self.run + timedelta(hours=self.fhr)


def fetch_profile(lat: float, lon: float, run: datetime, fhr: int = 0, progress=None, cancelled=None) -> Raw:
    """Downloads and decodes a HRRR sounding at a point. progress(done, total) is called as messages arrive;
    cancelled() can stop it early (raises InterruptedError)."""
    prs, sfc = plan(read_idx(run, fhr, "prs"), read_idx(run, fhr, "sfc"))
    if not prs or not sfc:
        raise OSError("the HRRR file doesn't hold the fields a sounding needs")
    jobs = [("prs", e) for e in prs] + [("sfc", e) for e in sfc]
    total, done = len(jobs), [0]
    point_lat, point_lon = lat, lon

    def fetch(job):
        kind, e = job
        if cancelled is not None and cancelled():
            raise InterruptedError
        k = key(run, fhr, kind)
        end = e.end if e.end >= 0 else e.start + 40_000_000
        try:
            data = aws.fetch_range(BUCKET, k, e.start, end)
        except OSError:
            if e.end >= 0:
                raise
            data = aws.session().get(f"{aws.bucket_url(BUCKET)}/{k}", headers={"Range": f"bytes={e.start}-"}, timeout=120).content
        msg = grib.read_messages(data)[0]
        value = float(msg.sample(point_lat, point_lon))
        done[0] += 1
        if progress is not None:
            progress(done[0], total)
        return kind, e, value, msg.grid
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(fetch, jobs))
    grid = out[0][3]
    vals = {(kind, e.var, e.level): v for kind, e, v, _g in out}

    levels = sorted({pressure(e.level) for _k, e, _v, _g in out if _k == "prs"}, reverse=True)
    sf = lambda var, level: vals[("sfc", var, level)]                                  # noqa: E731
    psfc = sf("PRES", "surface") / 100.0
    lons = lon % 360.0
    above = [p for p in levels if p < psfc - 1.0]                                      # drop levels below the ground
    col = lambda var: np.array([vals[("prs", var, f"{p:g} mb")] for p in above], np.float64)   # noqa: E731
    u, v = earth_relative(col("UGRD"), col("VGRD"), lons, grid.lov, grid.latin1)
    u10, v10 = earth_relative(sf("UGRD", "10 m above ground"), sf("VGRD", "10 m above ground"), lons, grid.lov, grid.latin1)
    return Raw(run, fhr, lat, lon, np.array(above), col("TMP"), col("RH"), np.asarray(u), np.asarray(v), psfc,
               sf("HGT", "surface"), sf("TMP", "2 m above ground"), sf("RH", "2 m above ground"),
               float(u10), float(v10))
