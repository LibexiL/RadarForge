from datetime import datetime, timezone

import numpy as np

from radarforge.tools import sky


def test_sun_noon_south_evening_west_night_below():
    # Oklahoma City, summer solstice
    az, el = sky.sun_position(datetime(2024, 6, 21, 18, 30, tzinfo=timezone.utc), 35.33, -97.28)
    assert 160 < az < 200 and el > 70
    az, el = sky.sun_position(datetime(2024, 5, 6, 23, 10, tzinfo=timezone.utc), 35.33, -97.28)
    assert 255 < az < 295 and 15 < el < 35          # early evening, sun in the west
    _az, el = sky.sun_position(datetime(2024, 6, 21, 6, 0, tzinfo=timezone.utc), 35.33, -97.28)
    assert el < -10                                  # 1 am local


def test_low_sun_is_warm_and_night_is_dark():
    high = sky.Sky(sky.sun_vector(200, 60))
    low = sky.Sky(sky.sun_vector(280, 3))
    night = sky.Sky(sky.sun_vector(0, -20))
    assert high.sun_col[2] / high.sun_col[0] > 0.7          # near white
    assert low.sun_col[2] / low.sun_col[0] < 0.2            # orange-red
    assert night.night and night.sun_col.max() == 0 and night.sky_amb.max() < 0.01
    assert night.exposure > high.exposure


def test_sky_bluer_overhead_and_haze_greyer():
    s = sky.sun_vector(180, 45)
    clear = sky.sky_radiance([[0, 0, 1]], s, sky.haze_tau(150))[0]
    hazy = sky.sky_radiance([[0, 0, 1]], s, sky.haze_tau(8))[0]
    assert clear[2] > clear[0] * 1.6
    assert hazy[2] / hazy[0] < clear[2] / clear[0]


def test_noise_tiles():
    n = sky.noise_volume(32)
    assert n.shape == (32, 32, 32) and n.dtype == np.uint8
    # wraps: opposite faces are as alike as neighbouring slices
    edge = np.abs(n[0].astype(int) - n[-1].astype(int)).mean()
    inner = np.abs(n[10].astype(int) - n[11].astype(int)).mean()
    assert edge < inner * 2.5
