"""Signature flags: resampling a sweep onto a grid, rotation, debris and ZDR-column logic on synthetic storms."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from radarforge.services import detect


def polar_image(fn, nrad=720, ngates=480, spacing=0.25, first=0.125, elevation=0.5, ground=True):
    """A sweep whose value at (x, y) km is fn(x, y). Azimuths every 0.5 degrees, gates every 250 m."""
    az = (np.arange(nrad) + 0.5) * 360.0 / nrad
    r = first + spacing * np.arange(ngates)
    a, rr = np.meshgrid(np.radians(az), r, indexing="ij")
    values = fn(rr * np.sin(a), rr * np.cos(a)).astype(np.float32)
    return SimpleNamespace(values=values, az=az, az_lo=az - 180.0 / nrad, az_hi=az + 180.0 / nrad, first_gate=first,
                           gate_spacing=spacing, elevation=elevation, ground_range=ground)


def blob(cx, cy, peak, sigma=3.0, base=0.0):
    return lambda x, y: base + peak * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma ** 2))


def test_grid_puts_a_feature_where_it_is():
    img = polar_image(blob(30.0, 40.0, 25.0))
    g = detect.to_grid(img, 100.0, 1.0)
    assert g.shape == (200, 200) and g.dtype == np.float32
    row, col = np.unravel_index(int(np.nanargmax(g)), g.shape)
    x, y = -100.0 + (col + 0.5), 100.0 - (row + 0.5)                       # row 0 is the north edge
    assert (x, y) == pytest.approx((30.0, 40.0), abs=1.5)
    assert np.isnan(g[0, 0])                                                 # beyond the sweep's range
    assert np.nanmax(g) == pytest.approx(25.0, rel=0.05)


def test_pooling_keeps_a_narrow_maximum_that_nearest_sampling_misses():
    spike = polar_image(lambda x, y: np.where((np.abs(x - 40.2) < 0.3) & (np.abs(y - 30.3) < 0.3), 50.0, 2.0))
    nearest = detect.to_grid(spike, 100.0, 1.0)
    pooled = detect.to_grid(spike, 100.0, 1.0, "max")
    assert np.nanmax(pooled) == 50.0                                        # a 600 m wide spike survives in a 1 km pixel
    row, col = np.unravel_index(int(np.nanargmax(pooled)), pooled.shape)
    assert (-100.0 + col + 0.5, 100.0 - row - 0.5) == pytest.approx((40.5, 30.5), abs=1.01)
    assert np.nanmax(pooled) >= np.nanmax(nearest)
    low = polar_image(lambda x, y: np.where((np.abs(x - 20) < 0.3) & (np.abs(y - 10) < 0.3), 0.3, 0.98))
    assert np.nanmin(detect.to_grid(low, 100.0, 1.0, "min")) == pytest.approx(0.3, abs=0.01)
    assert np.isnan(detect.to_grid(spike, 100.0, 1.0, "max")[0, 0])


def test_grid_handles_slant_range_and_infinities():
    img = polar_image(lambda x, y: np.full(x.shape, 7.0), elevation=3.0, ground=False)
    img.values[:, 100] = np.inf                                              # range-folded gates
    g = detect.to_grid(img, 60.0, 2.0)
    assert np.nanmax(g) == 7.0 and not np.isinf(g).any()


def test_rotation_flags_levels_and_position():
    strong = blob(30.0, 40.0, 34.0, sigma=2.5)
    weak = blob(-50.0, -20.0, 22.0, sigma=2.5)
    img = polar_image(lambda x, y: strong(x, y) + weak(x, y))
    g = detect.to_grid(img, 100.0, 1.0)
    flags = detect.rotation_flags(g, 100.0, 1.0)
    assert [f.level for f in flags] == [2, 1] or sorted(f.level for f in flags) == [1, 2]
    top = max(flags, key=lambda f: f.value)
    assert (top.x, top.y) == pytest.approx((30.0, 40.0), abs=2.0) and top.value > 28 and top.kind == "ROT"
    assert "Strong rotation" in top.text and "azimuthal shear" in top.text
    assert detect.rotation_flags(np.zeros((200, 200), np.float32), 100.0, 1.0) == []
    noisy = np.zeros((200, 200), np.float32)
    noisy[50, 50] = 80.0                                                     # one bad gate is not a mesocyclone
    assert detect.rotation_flags(noisy, 100.0, 1.0, min_area_km2=3.0) == []
    pair = np.zeros((200, 200), np.float32)
    pair[60, 60:62] = 45.0                                                   # two touching pixels: a tornado-scale couplet
    assert [f.level for f in detect.rotation_flags(pair, 100.0, 1.0, min_area_km2=1.0)] == [3]
    near_radar = np.zeros((200, 200), np.float32)
    near_radar[99:101, 99:101] = 60.0                                        # right at the radar: ground clutter
    assert detect.rotation_flags(near_radar, 100.0, 1.0) == []


def test_rotation_must_be_in_echo_continuous_aloft_and_not_crowded():
    spot = np.zeros((200, 200), np.float32)
    spot[60:62, 150:152] = 22.0                                              # moderate rotation at x=50, y=39
    echo = np.full((200, 200), 45.0, np.float32)
    aloft = np.full((200, 200), 15.0, np.float32)
    assert len(detect.rotation_flags(spot, 100.0, 1.0, ref=echo, upper=aloft, min_area_km2=1.0)) == 1
    assert detect.rotation_flags(spot, 100.0, 1.0, ref=np.zeros((200, 200), np.float32), min_area_km2=1.0) == []     # clear air
    assert detect.rotation_flags(spot, 100.0, 1.0, ref=echo, upper=np.zeros((200, 200), np.float32), min_area_km2=1.0) == []
    strong = spot * 2.0                                                      # extreme: no vertical continuity needed
    assert len(detect.rotation_flags(strong, 100.0, 1.0, ref=echo, upper=np.zeros((200, 200), np.float32), min_area_km2=1.0)) == 1
    crowd = np.zeros((200, 200), np.float32)
    for k in range(4):                                                       # four maxima within 6 km: only the strongest
        crowd[60 + 2 * k:62 + 2 * k, 150:152] = 22.0 + k
    flags = detect.rotation_flags(crowd, 100.0, 1.0, min_area_km2=1.0)
    assert len(flags) <= 2
    many = np.zeros((200, 200), np.float32)
    for k in range(12):
        many[30 + 12 * (k // 4):32 + 12 * (k // 4), 20 + 40 * (k % 4):22 + 40 * (k % 4)] = 25.0
    assert len(detect.rotation_flags(many, 100.0, 1.0, min_area_km2=1.0)) == detect.ROT_MAX_FLAGS


def test_debris_needs_rotation_and_low_cc_in_strong_echo():
    rot = polar_image(blob(30.0, 40.0, 34.0, sigma=2.5))
    ref = polar_image(blob(30.0, 40.0, 40.0, sigma=8.0, base=25.0))
    cc_low = polar_image(lambda x, y: 0.98 - 0.30 * np.exp(-((x - 30) ** 2 + (y - 40) ** 2) / (2 * 2.0 ** 2)))
    g = [detect.to_grid(rot, 100.0, 1.0, "max"), detect.to_grid(ref, 100.0, 1.0, "max"), detect.to_grid(cc_low, 100.0, 1.0, "min")]
    flags = detect.all_flags(g[0], g[1], g[2], half_km=100.0)
    kinds = [f.kind for f in flags]
    assert "ROT" in kinds and "DEBRIS" in kinds
    d = next(f for f in flags if f.kind == "DEBRIS")
    assert d.value < 0.8 and d.level >= 2 and (d.x, d.y) == pytest.approx((30.0, 40.0), abs=3.0)
    # the same low CC without rotation: a clutter / bird / ground-target return, so no debris flag
    flat = np.zeros_like(g[0])
    assert [f.kind for f in detect.all_flags(flat, g[1], g[2], half_km=100.0)] == []
    # healthy CC with rotation: rotation only
    healthy = np.full_like(g[2], 0.98)
    assert [f.kind for f in detect.all_flags(g[0], g[1], healthy, half_km=100.0)] == ["ROT"]


def test_zdr_column_needs_height_above_freezing_and_strong_echo():
    ref = polar_image(blob(-40.0, 30.0, 30.0, sigma=6.0, base=30.0))
    zdr = polar_image(blob(-40.0, 30.0, 2.5, sigma=3.0))
    ref = polar_image(blob(-40.0, 30.0, 30.0, sigma=6.0, base=30.0))
    layers = [(e, detect.to_grid(zdr, 100.0, 1.0), detect.to_grid(ref, 100.0, 1.0), None) for e in (0.5, 1.3, 2.4, 3.1, 4.0, 5.1, 6.4)]
    fz = detect.freezing_km_arl(13000.0, 1200.0)                              # 13 kft MSL over a 1,200 ft radar
    assert fz == pytest.approx(3.6, abs=0.05)
    flags = detect.zdr_column_flags(layers, 100.0, 1.0, fz)
    assert len(flags) == 1 and flags[0].kind == "ZDRCOL"
    assert (flags[0].x, flags[0].y) == pytest.approx((-40.0, 30.0), abs=3.0) and flags[0].value > 1.5
    assert "kft" in flags[0].text
    # low tilts only, all below the freezing level at that range: no column
    low = [(e, detect.to_grid(zdr, 100.0, 1.0), detect.to_grid(ref, 100.0, 1.0), None) for e in (0.5, 1.3)]
    assert detect.zdr_column_flags(low, 100.0, 1.0, fz) == []
    weak_echo = polar_image(lambda x, y: np.full(x.shape, 20.0))
    dry = [(e, detect.to_grid(zdr, 100.0, 1.0), detect.to_grid(weak_echo, 100.0, 1.0), None) for e in (3.1, 4.0, 5.1)]
    assert detect.zdr_column_flags(dry, 100.0, 1.0, fz) == []
    one = layers[3:4]                                                          # a blip on a single tilt isn't a column
    assert detect.zdr_column_flags(one, 100.0, 1.0, fz) == []
    messy = polar_image(lambda x, y: np.full(x.shape, 0.6))                    # low CC: not clean liquid drops
    dirty = [(e, z, r, detect.to_grid(messy, 100.0, 1.0)) for e, z, r, _c in layers]
    assert detect.zdr_column_flags(dirty, 100.0, 1.0, fz) == []
    far_ref = polar_image(blob(-90.0, 80.0, 30.0, sigma=6.0, base=30.0))      # beyond 110 km: beams too coarse
    far_zdr = polar_image(blob(-90.0, 80.0, 2.5, sigma=3.0))
    far = [(e, detect.to_grid(far_zdr, 150.0, 1.0), detect.to_grid(far_ref, 150.0, 1.0), None) for e in (3.1, 4.0, 5.1, 6.4)]
    assert detect.zdr_column_flags(far, 150.0, 1.0, fz) == []


def test_all_flags_orders_worst_first_and_respects_want():
    img = polar_image(lambda x, y: blob(30.0, 40.0, 48.0, 2.5)(x, y) + blob(-50.0, -20.0, 24.0, 2.5)(x, y))
    g = detect.to_grid(img, 100.0, 1.0)
    flags = detect.all_flags(g, half_km=100.0)
    assert [f.level for f in flags] == sorted((f.level for f in flags), reverse=True) and flags[0].level == 3
    assert detect.all_flags(g, half_km=100.0, want=("DEBRIS",)) == []
