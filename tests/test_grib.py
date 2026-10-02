"""The built-in GRIB2 reader, tested on messages built here from the format's specification."""
from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from radarforge.data import grib


# ------------------------------------------------------------------------------------------------ builders
def sm(value: int, nbytes: int) -> bytes:
    """Sign-and-magnitude integer, as GRIB2 stores signed numbers."""
    top = 1 << (8 * nbytes - 1)
    return (abs(value) | (top if value < 0 else 0)).to_bytes(nbytes, "big")


def section(num: int, body: bytes) -> bytes:
    return struct.pack(">IB", 5 + len(body), num) + body


def grid_latlon(ni, nj, la1, lo1, di, dj, mode=0):
    body = struct.pack(">BIBBBIB", 0, ni * nj, 0, 0, 0, 0, 0)[:6 + 4 - 4]
    body = bytes([0]) + struct.pack(">I", ni * nj) + bytes([0, 0]) + struct.pack(">H", 0)          # template 3.0
    body += bytes([6]) + b"\x00" * 15                                                              # sphere + earth params
    body += struct.pack(">II", ni, nj) + struct.pack(">II", 0, 0xFFFFFFFF)
    body += sm(round(la1 * 1e6), 4) + sm(round(lo1 * 1e6), 4) + bytes([48])
    body += sm(round((la1 - (nj - 1) * dj) * 1e6), 4) + sm(round((lo1 + (ni - 1) * di) * 1e6), 4)
    body += struct.pack(">II", round(di * 1e6), round(dj * 1e6)) + bytes([mode])
    return section(3, body)


def grid_lambert(nx, ny, la1, lo1, dx, dy, lov, latin, mode=0x40):
    body = bytes([0]) + struct.pack(">I", nx * ny) + bytes([0, 0]) + struct.pack(">H", 30)
    body += bytes([6]) + b"\x00" * 15
    body += struct.pack(">II", nx, ny) + sm(round(la1 * 1e6), 4) + sm(round(lo1 * 1e6), 4) + bytes([8])
    body += sm(round(latin * 1e6), 4) + sm(round(lov * 1e6), 4) + struct.pack(">II", round(dx * 1e3), round(dy * 1e3))
    body += bytes([0, mode]) + sm(round(latin * 1e6), 4) + sm(round(latin * 1e6), 4) + sm(-90000000, 4) + sm(0, 4)
    return section(3, body)


def product(category=0, number=0, level_type=100, level=50000):
    body = struct.pack(">HH", 0, 0) + bytes([category, number, 2, 0, 0]) + struct.pack(">HBBI", 0, 0, 1, 0)
    return section(4, body + bytes([level_type, 0]) + sm(level, 4) + bytes([255, 0]) + sm(0, 4))


def rep_head(template, npoints, ref, e, d, bits):
    return struct.pack(">IH", npoints, template) + struct.pack(">f", ref) + sm(e, 2) + sm(d, 2) + bytes([bits, 0])


def message(grid, rep_sec, data, bitmap=None, discipline=0, prod=None):
    s1 = section(1, struct.pack(">HHBBBHBBBBBBB", 7, 0, 28, 0, 1, 2026, 10, 2, 20, 0, 0, 0, 1))
    s6 = section(6, bytes([255]) if bitmap is None else bytes([0]) + bitmap)
    body = s1 + grid + (prod or product()) + rep_sec + s6 + section(7, data)
    return b"GRIB" + b"\x00\x00" + bytes([discipline, 2]) + struct.pack(">Q", 16 + len(body) + 4) + body + b"7777"


def pack_bits(values, widths) -> bytes:
    acc, nbits = 0, 0
    for v, w in zip(values, widths):
        acc = (acc << w) | int(v)
        nbits += w
    pad = -nbits % 8
    return (acc << pad).to_bytes((nbits + pad) // 8, "big")


def simple_message(field, ref, e, d, bits, grid):
    ints = np.rint((field * 10.0 ** d - ref) / 2.0 ** e).astype(np.int64).ravel()
    rep = section(5, rep_head(0, field.size, ref, e, d, bits))
    return message(grid, rep, pack_bits(ints, [bits] * len(ints)))


def complex_message(field_ints, ref, e, d, grid, order=2, group=7, bits_ref=10):
    """Complex packing with spatial differencing (template 5.3), written from the specification."""
    y = np.asarray(field_ints, np.int64).ravel()
    if order == 2:
        diff = np.concatenate(([0, y[1] - y[0]], y[2:] - 2 * y[1:-1] + y[:-2]))
        first = [int(y[0]), int(y[1])]
    else:
        diff = np.concatenate(([0], np.diff(y)))
        first = [int(y[0])]
    hmin = int(diff[order:].min())
    x = diff - hmin
    x[:order] = 0
    n = len(x)
    groups = [x[i:i + group] for i in range(0, n, group)]
    refs = [int(g.min()) for g in groups]
    widths = [int(g.max() - g.min()).bit_length() for g in groups]
    lens = [len(g) for g in groups]
    ng = len(groups)
    extra = b"".join(sm(v, 2) for v in first) + sm(hmin, 2)
    blob = bytearray(extra)
    blob += pack_bits(refs, [bits_ref] * ng)
    blob += pack_bits(widths, [5] * ng)                                     # reference for widths 0, 5 bits each
    blob += pack_bits([ln for ln in lens[:-1]] + [0], [8] * ng)             # increment 1, reference 0
    flat = []
    for g, r, w in zip(groups, refs, widths):
        flat += [(int(v) - r, w) for v in g]
    blob += pack_bits([v for v, _w in flat], [w for _v, w in flat])
    head = rep_head(3, n, ref, e, d, bits_ref)
    head += bytes([1, 0]) + struct.pack(">II", 0xFFFFFFFF, 0xFFFFFFFF) + struct.pack(">I", ng)
    head += bytes([0, 5]) + struct.pack(">I", 0) + bytes([1]) + struct.pack(">I", lens[-1]) + bytes([8, order, 2])
    return message(grid, section(5, head), bytes(blob))


# ------------------------------------------------------------------------------------------------ tests
def test_sign_and_magnitude():
    assert grib._sm(sm(-5, 4)) == -5 and grib._sm(sm(300000, 4)) == 300000 and grib._sm(sm(-1, 2)) == -1


def test_simple_packing_on_a_latlon_grid():
    field = np.arange(12, dtype=np.float64).reshape(3, 4) * 0.5 + 100.0
    data = simple_message(field, ref=100.0 * 10, e=0, d=1, bits=12, grid=grid_latlon(4, 3, 40.0, 250.0, 1.0, 1.0))
    (m,) = grib.read_messages(data)
    assert (m.grid.ni, m.grid.nj, m.grid.la1, m.grid.lo1) == (4, 3, 40.0, 250.0) and m.shape == (3, 4)
    assert m.reference_time == (2026, 10, 2, 20, 0, 0) and (m.category, m.number, m.level_type) == (0, 0, 100)
    assert m.level_value == 50000
    assert np.allclose(m.values(), field, atol=0.051)
    lat, lon = m.grid.latlon()
    assert lat[0, 0] == pytest.approx(40.0) and lat[2, 0] == pytest.approx(38.0) and lon[0, 1] == pytest.approx(-109.0)


def test_sample_interpolates_between_grid_points():
    field = np.add.outer(np.arange(3.0) * 10, np.arange(4.0))                  # row j adds 10, column i adds 1
    data = simple_message(field, ref=0.0, e=0, d=0, bits=8, grid=grid_latlon(4, 3, 40.0, 250.0, 1.0, 1.0))
    (m,) = grib.read_messages(data)
    assert m.sample(40.0, -110.0) == pytest.approx(0.0)                          # first grid point
    assert m.sample(39.5, -109.5) == pytest.approx(5.5)                          # halfway between four points
    assert np.isnan(m.sample(41.0, -110.0)) and np.isnan(m.sample(39.0, -100.0))  # outside the grid
    out = m.sample(np.array([40.0, 38.0]), np.array([-110.0, -107.0]))
    assert out.tolist() == [0.0, 23.0]
    assert m.sample(39.6, -109.4, "nearest") == pytest.approx(1.0)                # j = 0.4 -> 0, i = 0.6 -> 1
    assert m.sample(39.4, -109.4, "nearest") == pytest.approx(11.0)               # j = 0.6 -> 1, i = 0.6 -> 1


def test_max_sampling_keeps_a_thin_swath():
    field = np.zeros((4, 6))
    field[1, 2] = 7.0                                                              # one narrow bright cell
    data = simple_message(field, ref=0.0, e=0, d=0, bits=8, grid=grid_latlon(6, 4, 40.0, 250.0, 1.0, 1.0))
    (m,) = grib.read_messages(data)
    lat, lon = 38.6, -107.6                                                         # between rows 1-2 and columns 2-3
    assert m.sample(lat, lon, "max") == 7.0
    assert m.sample(lat, lon, "linear") < 7.0 and m.sample(39.0, -108.0, "nearest") == 7.0
    assert m.sample(np.array([lat, 41.0]), np.array([lon, 0.0]), "max")[0] == 7.0


def test_png_packing_keeps_the_small_integer_type(tmp_path):
    from PIL import Image
    field = (np.arange(6 * 5).reshape(5, 6) % 17).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(field, "L").save(buf, format="PNG")
    rep = section(5, rep_head(41, field.size, -5.0, 0, 1, 8))
    (m,) = grib.read_messages(message(grid_latlon(6, 5, 50.0, 260.0, 0.5, 0.5), rep, buf.getvalue()))
    assert m.raw().dtype == np.uint8                                             # 98 MB for MRMS, not 784 MB
    assert np.allclose(m.values(), (field - 5.0) / 10.0)


def test_complex_packing_with_spatial_differencing_order_2():
    rng = np.random.default_rng(3)
    ints = np.cumsum(np.cumsum(rng.integers(-3, 4, 41 * 31))) + 2000               # smooth-ish, like a real field
    ints = np.clip(ints, 0, None)
    grid = grid_lambert(41, 31, 30.0, -100.0, 3000.0, 3000.0, -97.5, 38.5)
    (m,) = grib.read_messages(complex_message(ints, ref=200.0, e=-2, d=0, grid=grid))
    assert m.grid.template == 30 and m.shape == (31, 41) and m.grid.j_positive
    assert np.array_equal(m.raw(), ints)
    assert np.allclose(m.values().ravel(), (200.0 + ints * 2.0 ** -2))


def test_complex_packing_order_1_and_a_short_last_group():
    ints = np.array([5, 7, 4, 4, 9, 20, 18, 2, 0, 1, 6])
    grid = grid_latlon(11, 1, 35.0, 270.0, 0.1, 0.1)
    (m,) = grib.read_messages(complex_message(ints, ref=0.0, e=0, d=0, grid=grid, order=1, group=4))
    assert np.array_equal(m.raw(), ints)


def test_lambert_grid_indices_round_trip():
    pytest.importorskip("pyproj")
    grid = grib.read_messages(simple_message(np.zeros((20, 30)), 0.0, 0, 0, 1,
                                             grid_lambert(30, 20, 36.0, -98.0, 3000.0, 3000.0, -97.5, 38.5)))[0].grid
    lat, lon = grid.latlon()
    i, j = grid.ij(lat, lon)
    ii, jj = np.meshgrid(np.arange(30), np.arange(20))
    assert np.allclose(i, ii, atol=1e-6) and np.allclose(j, jj, atol=1e-6)
    assert lat[1, 0] > lat[0, 0]                                                   # scan mode 64: rows go north


def test_bitmap_marks_missing_values():
    field = np.arange(8, dtype=np.float64).reshape(2, 4) + 1
    present = [1, 1, 0, 1, 1, 0, 0, 1]
    data_ints = [int(v) for v, p in zip(field.ravel(), present) if p]
    rep = section(5, rep_head(0, len(data_ints), 0.0, 0, 0, 8))
    bitmap = np.packbits(np.array(present, np.uint8)).tobytes()
    (m,) = grib.read_messages(message(grid_latlon(4, 2, 45.0, 250.0, 1.0, 1.0), rep, pack_bits(data_ints, [8] * 5), bitmap))
    v = m.values().ravel()
    assert np.isnan(v[[2, 5, 6]]).all() and v[[0, 1, 3, 4, 7]].tolist() == [1, 2, 4, 5, 8]
    assert np.isnan(m.sample(45.0, -108.0))                                         # a missing point stays missing


def test_unsupported_things_fail_loudly_not_silently():
    bad_edition = b"GRIB\x00\x00\x00\x01" + b"\x00" * 8
    with pytest.raises(grib.Grib2Error, match="edition"):
        grib.read_messages(bad_edition + b"7777")
    rep = section(5, rep_head(61, 4, 0.0, 0, 0, 8))
    (m,) = grib.read_messages(message(grid_latlon(2, 2, 45.0, 250.0, 1.0, 1.0), rep, b"\x00" * 4))
    with pytest.raises(grib.Grib2Error, match="5.61"):
        m.values()
    assert grib.read_messages(b"nothing like a GRIB file") == []


def test_several_messages_and_filtering():
    a = simple_message(np.ones((2, 2)), 0.0, 0, 0, 8, grid_latlon(2, 2, 45.0, 250.0, 1.0, 1.0))
    b = simple_message(np.full((2, 2), 3.0), 0.0, 0, 0, 8, grid_latlon(2, 2, 45.0, 250.0, 1.0, 1.0))
    both = grib.read_messages(a + b)
    assert len(both) == 2 and both[1].values()[0, 0] == 3.0
    assert grib.read_messages(a + b, want=lambda *x: False) == []
    assert len(grib.read_messages(a + b[:-30])) == 1                                # a truncated download is skipped
