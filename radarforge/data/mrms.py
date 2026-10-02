"""MRMS (Multi-Radar Multi-Sensor) products from NOAA's public AWS bucket: rotation tracks, hail swaths and
rainfall totals for the whole country, a new field every 2 minutes, resampled onto the radar's map grid."""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from . import aws, grib
from .mapgrid import Grid, pixel_latlon

BUCKET = "noaa-mrms-pds"
REGION = "CONUS"

# product key -> label, the folder name (with {w} for the accumulation window), the windows, their unit, what the
# numbers are and the unit they're shown in
PRODUCTS = {
    "rotation": dict(label="Rotation tracks (azimuthal shear)", folder="RotationTrackML{w}min_00.50", windows=(30, 60, 120, 240, 360, 1440),
                     window_unit="min", kind="rotation", units="x10⁻³ /s"),
    "hail": dict(label="Hail swaths (MESH)", folder="MESH_Max_{w}min_00.50", windows=(30, 60, 120, 240, 360, 1440),
                 window_unit="min", kind="hail", units="mm"),
    "qpe": dict(label="Rainfall (multi-sensor QPE)", folder="MultiSensor_QPE_{w:02d}H_Pass2_00.00", windows=(1, 3, 6, 12, 24, 48, 72),
                window_unit="h", kind="qpe", units="mm"),
}
DEFAULT_WINDOW = {"rotation": 60, "hail": 60, "qpe": 1}


def folder(product: str, window: int) -> str:
    return PRODUCTS[product]["folder"].format(w=window)


def window_label(product: str, window: int) -> str:
    unit = PRODUCTS[product]["window_unit"]
    if unit == "min" and window >= 60:
        return f"{window // 60} h"
    return f"{window} {unit}"


@dataclass(frozen=True)
class File:
    time: datetime
    key: str


_KEY_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2")


def key_time(key: str) -> datetime | None:
    m = _KEY_TIME.search(key)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_files(product: str, window: int, start: datetime, end: datetime) -> list:
    """The MRMS files of a product between two times (UTC days are separate folders), oldest first."""
    out = []
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(timezone.utc)
    while day <= last:
        prefix = f"{REGION}/{folder(product, window)}/{day:%Y%m%d}/"
        objs, _ = aws.list_objects(BUCKET, prefix, max_pages=10)
        for o in objs:
            t = key_time(o.key)
            if t is not None and start <= t <= end:
                out.append(File(t, o.key))
        day += timedelta(days=1)
    return sorted(out, key=lambda f: f.time)


def read_field(data: bytes) -> grib.Message:
    """The one field in an MRMS file (the bytes of the .grib2, or of the .grib2.gz)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    msgs = grib.read_messages(data)
    if not msgs:
        raise grib.Grib2Error("no GRIB2 message in the MRMS file")
    return msgs[0]


def to_grid(msg: grib.Message, product: str, when: datetime, lat0: float, lon0: float, half_km=1200.0, step_km=2.0) -> Grid:
    """The field resampled onto the map grid around a radar. Values the product marks as missing (negative codes)
    and zeros (nothing there) become NaN so they draw as transparent."""
    lat, lon = pixel_latlon(lat0, lon0, half_km, step_km)
    # a swath is a km wide and a map pixel 2 km: take the largest value around each pixel so thin tracks survive
    vals = msg.sample(lat, lon, method="max" if product in ("rotation", "hail") else "linear").astype(np.float32)
    vals[(vals <= 0) | (vals > 9000)] = np.nan
    meta = PRODUCTS[product]
    return Grid(vals, half_km, step_km, when, meta["kind"], meta["units"])


def fetch(file: File) -> bytes:
    return aws.fetch(BUCKET, file.key, timeout=60)
