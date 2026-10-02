"""GOES satellite and lightning: file names, geometry, resampling and colours (no network needed)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from radarforge.data import goes  # noqa: E402
from radarforge.products import satcolors  # noqa: E402
from radarforge.products.geometry import aeqd_forward  # noqa: E402

h5py = pytest.importorskip("h5py")

PROJ = goes.Projection(35786023.0, 6378137.0, 6356752.31414, -75.0)


def test_key_times_and_nearest():
    k = "ABI-L2-CMIPC/2026/275/21/OR_ABI-L2-CMIPC-M6C13_G19_s20262752101174_e20262752103561_c20262752104053.nc"
    t = goes.key_time(k)
    assert t == datetime(2026, 10, 2, 21, 1, 17, 400000, tzinfo=timezone.utc)
    assert goes.key_time("nothing here") is None
    scans = [goes.Scan(t + timedelta(minutes=5 * i), f"k{i}", "b") for i in range(4)]
    assert goes.nearest(scans, t + timedelta(minutes=6)).key == "k1"
    assert goes.nearest(scans, t + timedelta(minutes=40), 15) is None
    assert goes.nearest([], t) is None


def test_which_satellite():
    assert goes.satellite_for(-97.3) == "east" and goes.satellite_for(-121.9) == "west"


def test_hours_listed_across_a_boundary():
    a = datetime(2026, 10, 2, 21, 40, tzinfo=timezone.utc)
    hrs = list(goes._hours(a, a + timedelta(minutes=50)))
    assert [h.hour for h in hrs] == [21, 22]


def test_scan_angles_match_the_reference_projection():
    pyproj = pytest.importorskip("pyproj")
    geos = pyproj.Proj(proj="geos", h=PROJ.height, lon_0=-75, sweep="x", a=PROJ.req, rf=298.257222096)
    lats = np.array([35.0, 40.0, 30.0, 47.5, 25.0, 0.0])
    lons = np.array([-97.0, -105.0, -85.0, -122.0, -80.0, -75.0])
    ax, ay = goes.scan_angles(lats, lons, PROJ)
    px, py = geos(lons, lats)
    assert np.allclose(ax, px / PROJ.height, atol=1e-9) and np.allclose(ay, py / PROJ.height, atol=1e-9)
    assert abs(ax[-1]) < 1e-12 and abs(ay[-1]) < 1e-12                      # straight below the satellite


def test_beyond_the_horizon_has_no_angle():
    ax, ay = goes.scan_angles(np.array([0.0]), np.array([105.0]), PROJ)       # 180 degrees away
    assert np.isnan(ax[0]) and np.isnan(ay[0])


def make_cmi(path, fill_fn, n=600, span=0.07, band_units="K"):
    """A little ABI-style file: n x n pixels covering +-span radians around the sub-satellite point."""
    xs = np.linspace(-span, span, n)
    ys = np.linspace(span, -span, n)
    with h5py.File(path, "w") as f:
        f.create_dataset("x", data=np.round(xs / 5.6e-5).astype(np.int16)).attrs.update(
            {"scale_factor": np.float32(5.6e-5), "add_offset": np.float32(0.0)})
        f.create_dataset("y", data=np.round(ys / -5.6e-5).astype(np.int16)).attrs.update(
            {"scale_factor": np.float32(-5.6e-5), "add_offset": np.float32(0.0)})
        p = f.create_dataset("goes_imager_projection", data=np.int32(0))
        p.attrs["perspective_point_height"] = np.array([PROJ.height])
        p.attrs["semi_major_axis"] = np.array([PROJ.req])
        p.attrs["semi_minor_axis"] = np.array([PROJ.rpol])
        p.attrs["longitude_of_projection_origin"] = np.array([PROJ.lon0])
        gx, gy = np.meshgrid(np.arange(n), np.arange(n))
        raw = fill_fn(gx, gy).astype(np.int16)
        c = f.create_dataset("CMI", data=raw)
        c.attrs.update({"scale_factor": np.float32(0.5), "add_offset": np.float32(100.0),
                        "_FillValue": np.array([-1], np.int16), "units": band_units})
        f.create_dataset("t", data=np.float64(844246956.78))


def test_reproject_puts_values_in_the_right_place(tmp_path):
    path = tmp_path / "c13.nc"
    make_cmi(path, lambda gx, gy: gx // 3 + 100)                    # value grows eastwards
    lat0, lon0 = 0.0, -75.0                                          # radar right below the satellite
    g = goes.reproject(str(path), lat0, lon0, half_km=200.0, step_km=4.0)
    assert g.values.shape == (100, 100) and g.kind == "bt" and g.units == "K"
    assert g.time == datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(seconds=844246956.78)
    west, east = g.sample(-150, 0), g.sample(150, 0)
    assert west is not None and east is not None and east > west     # east is brighter: the picture isn't mirrored
    # north-up: a value that only depends on the row must change from top to bottom
    path2 = tmp_path / "rows.nc"
    make_cmi(path2, lambda gx, gy: gy // 3 + 100)
    g2 = goes.reproject(str(path2), lat0, lon0, half_km=200.0, step_km=4.0)
    assert g2.sample(0, 150) < g2.sample(0, -150)                    # row index grows southwards
    assert g2.sample(500, 0) is None                                 # outside the grid


def test_missing_pixels_are_nan(tmp_path):
    path = tmp_path / "c.nc"
    make_cmi(path, lambda gx, gy: np.where(gx < 300, 100, -1))       # the east half has no data
    g = goes.reproject(str(path), 0.0, -75.0, half_km=200.0, step_km=4.0)
    assert g.sample(-100, 0) is not None and g.sample(100, 0) is None


def make_glm(path, lat, lon, offsets, quality=None):
    with h5py.File(path, "w") as f:
        f.create_dataset("flash_lat", data=np.array(lat, np.float32))
        f.create_dataset("flash_lon", data=np.array(lon, np.float32))
        # like the real files: unsigned 16-bit integers stored as int16 with _Unsigned = "true"
        raw_t = np.round((np.array(offsets) + 5.0) / 0.00038148).astype(np.uint16).view(np.int16)
        t = f.create_dataset("flash_time_offset_of_first_event", data=raw_t)
        t.attrs.update({"scale_factor": np.float32(0.00038148), "add_offset": np.float32(-5.0), "_Unsigned": "true"})
        e = f.create_dataset("flash_energy", data=np.array([100] * len(lat), np.uint16).view(np.int16))
        e.attrs.update({"scale_factor": np.float32(1e-15), "add_offset": np.float32(0.0), "_Unsigned": "true",
                        "_FillValue": np.array([-1], np.int16)})
        f.create_dataset("product_time", data=np.float64(844246820.0))
        if quality is not None:
            f.create_dataset("flash_quality_flag", data=np.array(quality, np.int16))


def test_glm_flashes_get_real_times(tmp_path):
    path = tmp_path / "glm.nc"
    # offsets past 7.5 s only fit in an unsigned 16-bit integer: the old reader got those wrong
    make_glm(path, [35.0, 36.0, 37.0], [-97.0, -96.0, -95.0], [1.0, 10.0, 19.0], quality=[0, 0, 1])
    fl = goes.read_glm(str(path))
    assert len(fl) == 2                                              # the poor-quality flash is dropped
    base = (datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(seconds=844246820.0)).timestamp()
    assert fl.t[0] == pytest.approx(base + 1.0, abs=0.01) and fl.t[1] == pytest.approx(base + 10.0, abs=0.01)
    assert fl.energy[0] == pytest.approx(100.0, rel=0.01)           # 1e-13 J = 100 fJ


def test_glm_empty_and_concat(tmp_path):
    path = tmp_path / "empty.nc"
    make_glm(path, [], [], [])
    assert len(goes.read_glm(str(path))) == 0
    assert len(goes.Flashes.concat([])) == 0
    a = goes.Flashes(np.zeros(2, np.float32), np.zeros(2, np.float32), np.zeros(2), np.zeros(2, np.float32))
    assert len(goes.Flashes.concat([a, goes.Flashes.empty(), a])) == 4


def test_colours():
    k = np.array([[310.0, 273.15, 243.0, 210.0, np.nan]], np.float32)
    rgba = satcolors.colorize(k, "ir")[0]
    assert rgba[0].tolist()[:3] == [28, 28, 28] or rgba[0, 0] < 40            # hot ground: dark
    assert rgba[1, 0] == rgba[1, 1] == rgba[1, 2]                            # 0 °C: grey
    assert rgba[3, 2] > rgba[3, 1]                                           # very cold top: blue / violet / pink
    assert rgba[4, 3] == 0 and rgba[:4, 3].tolist() == [255] * 4            # no data: transparent
    vis = satcolors.colorize(np.array([[0.0, 0.25, 1.0]], np.float32), "vis")[0]
    assert vis[0, 0] == 0 and vis[2, 0] == 255 and vis[1, 0] > 100            # gamma lifts mid-tones
    assert satcolors.describe(213.15, "ir") == "-60 °C" and satcolors.describe(0.456, "vis") == "46% reflectance"


def test_layer_picks_flashes_by_age_and_counts_near(tmp_path):
    from radarforge.config import Settings
    from radarforge.overlays import lightning
    assert lightning.age_bucket(0.0) == 0 and lightning.age_bucket(0.3) == 1 and lightning.age_bucket(1.0) == 3
    from PySide6.QtWidgets import QApplication
    assert QApplication.instance() or QApplication([])
    layer = lightning.LightningOverlay(Settings(tmp_path / "s.json"))
    layer._flashes = goes.Flashes(np.array([35.0, 35.1, 40.0], np.float32), np.array([-97.0, -97.0, -90.0], np.float32),
                                  np.zeros(3), np.ones(3, np.float32))
    assert layer.counts_near(35.0, -97.0, 20) == 2 and layer.counts_near(35.0, -97.0, 5) == 1
    assert layer.minutes() == 10
    x, y = aeqd_forward(35.0, -97.0, 35.0, -97.0)
    assert float(x) == pytest.approx(0.0, abs=1e-6)
