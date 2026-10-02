"""MRMS: file names, colours and resampling onto the radar's map grid (a synthetic file, no network)."""
from __future__ import annotations

import gzip
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from radarforge.data import mrms
from radarforge.products import mrmscolors

sys.path.insert(0, str(Path(__file__).parent))
from test_grib import grid_latlon, message, rep_head, section  # noqa: E402


def test_names_and_windows():
    assert mrms.folder("rotation", 60) == "RotationTrackML60min_00.50"
    assert mrms.folder("hail", 1440) == "MESH_Max_1440min_00.50"
    assert mrms.folder("qpe", 1) == "MultiSensor_QPE_01H_Pass2_00.00"
    assert mrms.folder("qpe", 24) == "MultiSensor_QPE_24H_Pass2_00.00"
    assert mrms.window_label("rotation", 30) == "30 min" and mrms.window_label("rotation", 120) == "2 h"
    assert mrms.window_label("qpe", 3) == "3 h"
    assert set(mrms.DEFAULT_WINDOW) == set(mrms.PRODUCTS)
    for p, d in mrms.DEFAULT_WINDOW.items():
        assert d in mrms.PRODUCTS[p]["windows"]


def test_file_times():
    k = "CONUS/RotationTrackML60min_00.50/20261002/MRMS_RotationTrackML60min_00.50_20261002-223600.grib2.gz"
    assert mrms.key_time(k) == datetime(2026, 10, 2, 22, 36, tzinfo=timezone.utc)
    assert mrms.key_time("nope") is None


def synthetic_png_file(field: np.ndarray, gz=True) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(field.astype(np.uint8), "L").save(buf, format="PNG")
    rep = section(5, rep_head(41, field.size, 0.0, 0, 0, 8))
    h, w = field.shape
    data = message(grid_latlon(w, h, 40.0, 260.0, 0.005, 0.005), rep, buf.getvalue(), discipline=209)
    return gzip.compress(data) if gz else data


def test_field_is_resampled_around_the_radar_and_thin_tracks_survive():
    field = np.zeros((400, 600), np.uint8)             # 2 x 3 degrees at 0.005 degrees
    field[100:103, 100:400] = 12                       # a thin east-west track, 3 pixels = 1.5 km wide
    field[300, 200] = 5
    msg = mrms.read_field(synthetic_png_file(field))
    assert msg.grid.ni == 600 and msg.grid.nj == 400 and msg.discipline == 209
    when = datetime(2026, 10, 2, 22, 36, tzinfo=timezone.utc)
    lat0, lon0 = 39.5, -99.0                           # radar inside the field (lat 39-40, lon -100..-97)
    g = mrms.to_grid(msg, "rotation", when, lat0, lon0, half_km=60.0, step_km=2.0)
    assert g.values.shape == (60, 60) and g.kind == "rotation" and g.time == when
    finite = np.isfinite(g.values)
    assert finite.any() and np.nanmax(g.values) == 12.0
    assert np.isnan(g.values[~finite]).all() and (g.values[finite] > 0).all()   # zeros and missing become clear
    # the track (lat 39.5 - 39.5+, roughly) spans pixels left to right across several columns
    row = np.nonzero(finite.any(axis=1))[0]
    assert len(row) >= 1 and finite.sum(axis=1).max() > 20
    # the same file read without gzip
    assert mrms.read_field(synthetic_png_file(field, gz=False)).grid.ni == 600


def test_missing_file_content_is_reported():
    with pytest.raises(Exception):
        mrms.read_field(gzip.compress(b"not grib"))


def test_colours():
    v = np.array([[np.nan, 0.0, 1.0, 2.0, 6.0, 30.0]], np.float32)
    rot = mrmscolors.colorize(v, "rotation")[0]
    assert rot[:3, 3].tolist() == [0, 0, 0]                     # missing, zero and below-threshold: clear
    assert rot[3, 3] > 0 and rot[4, 3] > rot[3, 3] and rot[5].tolist() == [255, 255, 255, 255]
    hail = mrmscolors.colorize(np.array([[5.0, 25.0, 60.0]], np.float32), "hail")[0]
    assert hail[0, 3] == 0 and hail[1, 3] > 0 and hail[2, 0] == 255
    assert mrmscolors.describe(38.1, "hail", "mm") == "MESH 1.50 in (38 mm)"
    assert mrmscolors.describe(25.4, "qpe", "mm") == "rain 25.4 mm (1.00 in)"
    assert mrmscolors.describe(9.0, "rotation", "x10⁻³ /s") == "rotation 9 x10⁻³ /s"
