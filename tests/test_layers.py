"""1.9.0 data layers, alerts, soundings and storm tools (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from radarforge.features import alerts, feeds, georaster, grib2, lightning, mrms, obs, satellite
from radarforge.features import sounding as snd
from radarforge.features import stormtools as st

DATA = Path(__file__).parent / "data"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- GRIB2 / MRMS
@pytest.mark.parametrize("name", ["png", "simple", "bitmap"])
def test_grib2_decoder(name):
    expected = np.load(DATA / "mrms_expected.npy")
    g = grib2.decode((DATA / f"mrms_{name}.grib2.gz").read_bytes())      # made with eccodes
    assert (g.ni, g.nj) == (40, 25) and g.la1 == pytest.approx(40.0) and g.lo1 == pytest.approx(-100.0)
    assert g.di == pytest.approx(0.01) and g.ref_time == utc(2026, 10, 1, 21, 30)
    if name == "bitmap":
        assert np.isnan(g.values[10, 10])
        expected = expected.copy()
        expected[10, 10] = np.nan
    assert np.nanmax(np.abs(g.values - expected)) < 0.01
    assert g.sample(np.array([39.9]), np.array([-99.95]))[0] == pytest.approx(expected[10, 5], abs=0.01)
    assert np.isnan(g.sample(np.array([45.0]), np.array([-99.95]))[0])


def test_grib2_rejects_garbage():
    with pytest.raises(grib2.GribError):
        grib2.decode(b"hello world")


def test_mrms_keys_and_units():
    keys = [f"CONUS/RotationTrack60min_00.50/20261001/MRMS_RotationTrack60min_00.50_20261001-21{m:02d}00.grib2.gz"
            for m in range(0, 40, 2)]
    assert mrms.key_time(keys[3]) == utc(2026, 10, 1, 21, 6)
    k, t = mrms.pick_key(keys, utc(2026, 10, 1, 21, 14, 50), 30)
    assert t == utc(2026, 10, 1, 21, 14) and k == keys[7]
    assert mrms.pick_key(keys, utc(2026, 10, 1, 21, 15, 30), 30)[1] == utc(2026, 10, 1, 21, 16)   # a minute's grace
    assert mrms.pick_key(keys, utc(2026, 10, 1, 23, 0), 30) == (None, None)       # all too old
    v = mrms.to_display("rot", np.array([-999.0, -99.0, 0.0, 0.012], np.float32))
    assert np.isnan(v[:2]).all() and v[3] == pytest.approx(12.0)                   # 1/s -> 1e-3/s
    assert mrms.to_display("mesh", np.array([25.4], np.float32))[0] == pytest.approx(1.0)


def test_colorize_and_raster():
    rgba = georaster.colorize(np.array([[np.nan, 2.0, 4.0, 100.0]]), mrms.ROT_STOPS)
    assert rgba[0, 0, 3] == 0 and rgba[0, 1, 3] == 0 and rgba[0, 2, 3] == 190
    assert tuple(rgba[0, 3]) == (255, 255, 255, 255)
    lat, lon = georaster.aeqd_grid(35.0, -97.0, 100.0, 4)
    assert lat[0, 0] > lat[-1, 0] and lon[0, 0] < lon[0, -1]                  # north up, east right
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    r = georaster.Raster(georaster.to_qimage(np.zeros((4, 4, 4), np.uint8)), 100.0, (35.0, -97.0),
                         values=np.arange(16, dtype=float).reshape(4, 4))
    assert r.value_at(-99.0, 99.0) == 0 and r.value_at(99.0, -99.0) == 15 and r.value_at(200, 0) is None


# --------------------------------------------------------------------------- satellite
def test_geos_projection_matches_pyproj():
    pyproj = pytest.importorskip("pyproj")
    lat = np.array([35.3, 45.0, 25.0, 30.0])
    lon = np.array([-97.5, -120.0, -80.0, -100.0])
    x, y, vis = georaster.geos_forward(lat, lon, -75.0)
    p = pyproj.Proj("+proj=geos +h=35786023.0 +lon_0=-75.0 +sweep=x +ellps=GRS80")
    X, Y = p(lon, lat)
    assert np.allclose(x * 35786023.0, X, atol=1.0) and np.allclose(y * 35786023.0, Y, atol=1.0) and vis.all()
    assert not georaster.geos_forward(np.array([0.0]), np.array([100.0]), -75.0)[2][0]    # far side


def test_world_file_and_reprojection():
    wld = georaster.parse_world_file("2004.0\n0\n0\n-2004.0\n-3627271.0\n4589200.0\n")
    row, col = georaster.world_pixels(wld, np.array([-3627271.0 + 2004 * 10]), np.array([4589200.0 - 2004 * 3]))
    assert col[0] == pytest.approx(10) and row[0] == pytest.approx(3)
    sw = satellite.scan_world(wld, 100, 100, "GOES-19")
    assert sw[0] == pytest.approx(2004.0 / satellite.PERSPECTIVE_H)
    assert satellite.satellite_for(-120.0) == "GOES-18" and satellite.satellite_for(-97.0) == "GOES-19"
    assert satellite.lon0_from_proj("+proj=geos +h=35786023.0 +lon_0=-75.2 +sweep=x", -75) == -75.2
    from PIL import Image
    im = Image.fromarray(np.full((150, 250), 200, np.uint8), "L")
    rgba = satellite.image_rgba(im, "ir", True)
    out = satellite.reproject(rgba, satellite.scan_world(None, 250, 150, "GOES-19"), -75.0, 35.3, -97.3, 300, 50)
    assert (out[..., 3] > 0).mean() > 0.99


# --------------------------------------------------------------------------- lightning
def test_glm_names_and_parse(tmp_path):
    h5py = pytest.importorskip("h5py")
    k = "GLM-L2-LCFA/2026/275/21/OR_GLM-L2-LCFA_G19_s20262752130200_e20262752130400_c20262752130420.nc"
    assert lightning.glm_start(k) == utc(2026, 10, 2, 21, 30, 20)
    assert lightning.glm_prefixes(utc(2026, 10, 2, 21, 50), utc(2026, 10, 2, 22, 5)) == [
        "GLM-L2-LCFA/2026/275/21/", "GLM-L2-LCFA/2026/275/22/"]
    assert lightning.glm_bucket(-97) == "noaa-goes19" and lightning.glm_bucket(-118) == "noaa-goes18"
    path = tmp_path / "glm.nc"
    with h5py.File(path, "w") as f:                       # packed like the real files: unsigned short + scale
        d = f.create_dataset("flash_lat", data=np.array([1000, 2000, 65535], np.uint16).view(np.int16))
        d.attrs["_Unsigned"] = np.bytes_(b"true")
        d.attrs["scale_factor"] = np.float32(0.01)
        d.attrs["add_offset"] = np.float32(20.0)
        d.attrs["_FillValue"] = np.int16(-1)
        f.create_dataset("flash_lon", data=np.array([-97.5, -97.0, -96.0], np.float32))
    lat, lon, _en = lightning.parse_glm(path.read_bytes())
    assert lat.tolist() == pytest.approx([30.0, 40.0]) and lon.tolist() == pytest.approx([-97.5, -97.0])


def test_flash_store_window():
    s = lightning.FlashStore(cap=3)
    t = utc(2026, 10, 2, 21, 0)
    for i in range(5):
        s.add(f"b/{i}", t + timedelta(minutes=i), np.array([35.0 + i]), np.array([-97.0]))
    times, lat, lon = s.window(t, t + timedelta(minutes=10))
    assert lat.tolist() == [37.0, 38.0, 39.0]                                     # oldest dropped (cap)
    assert len(s.window(t + timedelta(minutes=4), t + timedelta(minutes=4))[0]) == 1


# --------------------------------------------------------------------------- surface obs
def test_surface_obs():
    now = utc(2026, 10, 2, 21, 0)
    js = {"data": [dict(station="OKC", name="Oklahoma City", lat=35.39, lon=-97.6, utc_valid="2026-10-02T20:52:00Z",
                        tmpf=84, dwpf=70, sknt=22, drct=160, gust=31, mslp=1002.3, skyc1="BKN", raw="KOKC ..."),
                   dict(station="OLD", lat=35, lon=-97, utc_valid="2026-10-02T15:00:00Z", tmpf=60),
                   dict(station="BAD", lat=None, lon=-97, utc_valid="2026-10-02T20:52:00Z")]}
    o = obs.parse_currents(js, now)
    assert [x["id"] for x in o] == ["OKC"]
    txt = obs.describe(o[0])
    assert "gusting 31 kt" in txt and "84°F" in txt and "1002.3 mb" in txt
    lines, tris = obs.barb_lines(0, 0, 270, 65)            # 65 kt: pennant + 1 barb + half barb
    assert len(tris) == 1 and len(lines) == 3
    assert lines[0].x2() < 0                                  # staff points west, into the wind
    assert "OK" in obs.states_near(35.33, -97.28) and "TX" in obs.states_near(35.33, -97.28)


# --------------------------------------------------------------------------- SPC day 2 / 3
def test_spc_day2_day3_requests_and_any_severe():
    r = feeds.outlook_requests_ahead(utc(2026, 10, 2, 18, 0), 2)
    assert r[0] == ("2026-10-03", 17) and ("2026-10-02", 17) in r
    assert feeds.outlook_requests_ahead(utc(2026, 10, 2, 3, 0), 3)[0] == ("2026-10-03", 20)
    js = {"features": [
        {"properties": {"category": "CATEGORICAL", "threshold": "SLGT"},
         "geometry": {"type": "Polygon", "coordinates": [[[-100, 30], [-90, 30], [-90, 40], [-100, 40], [-100, 30]]]}},
        {"properties": {"category": "ANY SEVERE", "threshold": "0.15"},
         "geometry": {"type": "Polygon", "coordinates": [[[-100, 30], [-90, 30], [-90, 40], [-100, 40], [-100, 30]]]}}]}
    o = feeds.outlook_at(feeds.parse_outlook(js), 35, -95)
    assert o["cat"] == "SLGT" and o["any"] == pytest.approx(0.15) and o["tornado"] is None


# --------------------------------------------------------------------------- alerts
class _A:
    def __init__(self, event, ring, key, pri, action="NEW", minutes=30):
        self.event, self.rings, self.key, self.action = event, [np.array(ring, float)], key, action
        self.expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.style = (None, None, None, pri)
        self.variant_label = event
        self.hover = event


def test_alert_rules():
    from radarforge.features.warnings import EVENT_GROUP
    now = datetime.now(timezone.utc)
    ring = [(-97.7, 35.1), (-97.3, 35.1), (-97.3, 35.4), (-97.7, 35.4), (-97.7, 35.1)]
    locs = [alerts.normalise(alerts.new_location("Mine", None, None, mine=True)),
            alerts.normalise(alerts.new_location("Far", 40.0, -90.0, lightning=True)),
            alerts.normalise(alerts.new_location("Moore", 35.34, -97.49, reports=True, report_miles=5,
                                                 watches=True, lightning=True, lightning_miles=10))]
    locs[0]["warn"]["SVR"] = False
    warns = [_A("Tornado Warning", ring, "TOR1", 8), _A("Severe Thunderstorm Warning", ring, "SVR1", 5),
             _A("Tornado Watch", ring, "TOA1", 1), _A("Tornado Warning", ring, "TOR0", 8, action="CAN")]
    reps = [dict(kind="hail", lat=35.30, lon=-97.48, time=now - timedelta(minutes=5), hover="HAIL"),
            dict(kind="hail", lat=35.30, lon=-97.48, time=now - timedelta(hours=3), hover="old"),
            dict(kind="tornado", lat=36.30, lon=-97.48, time=now, hover="far")]
    flashes = lambda lat, lon, mi, mins: (3, 4.0, now) if lat < 38 else (0, None, None)   # noqa: E731
    notified = {}
    ev = alerts.evaluate(locs, (35.33, -97.49), warns, reps, flashes, now, notified, EVENT_GROUP.get)
    got = sorted((e["loc"]["name"], e["kind"], e["title"].split(" – ")[0].split(" reported")[0]) for e in ev)
    assert got == [("Mine", "warning", "Tornado Warning"),
                   ("Moore", "lightning", "Lightning near Moore"), ("Moore", "report", "Hail"),
                   ("Moore", "warning", "Severe Thunderstorm Warning"), ("Moore", "warning", "Tornado Warning"),
                   ("Moore", "watch", "Tornado Watch")]
    assert ev[0]["priority"] == 8
    assert alerts.evaluate(locs, (35.33, -97.49), warns, reps, flashes, now, notified, EVENT_GROUP.get) == []
    warns[0].style = (None, None, None, 9)                   # upgraded (e.g. to an emergency): alert again
    again = alerts.evaluate(locs, (35.33, -97.49), warns, reps, flashes, now, notified, EVENT_GROUP.get)
    assert sorted(e["loc"]["name"] for e in again) == ["Mine", "Moore"]
    later = now + timedelta(minutes=alerts.LIGHTNING_COOLDOWN_MIN + 1)
    ev3 = alerts.evaluate(locs, (35.33, -97.49), warns, [], flashes, later, notified, EVENT_GROUP.get)
    assert [e["kind"] for e in ev3] == ["lightning"]
    locs[2]["enabled"] = False
    assert alerts.describe_rules(locs[2]) == "alerts off"


def test_alert_migration_and_sounds(tmp_path, monkeypatch):
    from radarforge.config import Settings
    s = Settings(tmp_path / "s.json")
    s["warn_at_location"] = False
    assert alerts.migrate(s) and s["saved_locations"][0]["mine"] and not s["saved_locations"][0]["enabled"]
    assert not alerts.migrate(s)
    monkeypatch.setattr(alerts, "CACHE_DIR", tmp_path)
    import wave
    for name in ("chime", "siren", "beep"):
        with wave.open(str(alerts.sound_file(name))) as w:
            assert w.getframerate() == 22050 and w.getnframes() > 5000
    assert alerts.sound_file("none") is None


# --------------------------------------------------------------------------- soundings
def _profile_js():
    z = np.linspace(350, 16000, 50)
    h = z - z[0]
    p = 1000 * np.exp(-h / 8000.0)
    t = np.maximum(28 - 6.5 * h / 1000, -60)
    td = np.minimum(t, 21 - 2.5 * h / 1000)
    dr = np.interp(h, [0, 1000, 3000, 6000, 16000], [160, 200, 230, 260, 270])
    sk = np.interp(h, [0, 1000, 3000, 6000, 16000], [15, 30, 40, 50, 80])
    lv = [dict(PRES=a, TMPC=b, DWPC=c, HGHT=d, DRCT=e, SKNT=f) for a, b, c, d, e, f in zip(p, t, td, z, dr, sk)]
    lv.append(dict(PRES=None, TMPC=1, DWPC=1, HGHT=1, DRCT=1, SKNT=1))
    return {"source": {"station": "OUN"}, "profiles": [{"forecast_hour": 1, "time": "2026-10-02T21:00:00Z",
                                                        "levels": lv}]}


def test_sounding_numbers():
    prof = snd.parse_bufkit(_profile_js(), "RAP")[0]
    assert len(prof.p) == 50 and prof.station == "OUN" and prof.fhour == 1 and prof.p[0] > prof.p[-1]
    u, v = prof.uv()
    assert u[0] == pytest.approx(-15 * math.sin(math.radians(160)))
    assert snd.bulk_shear(prof, 6000) == pytest.approx(54.5, abs=1)
    right, left, mean = snd.bunkers(prof)
    assert snd.srh(prof, 1000, right) > 100 > snd.srh(prof, 1000, left)
    assert snd.lapse_rate(prof) == pytest.approx(6.5, abs=0.1)
    d, k = snd.motion_from(*right)
    assert 230 < d < 280 and 15 < k < 40
    ix = snd.indices(prof)
    assert ix["sbcape"] > 500 and ix["lcl_m"] == pytest.approx(820, abs=150) and ix["stp"] > 0


# --------------------------------------------------------------------------- storm tools
def _sweep(pid, fill, blobs):
    from radarforge.products.engine import SweepImage
    az = np.arange(720) * 0.5 + 0.25
    v = np.full((720, 600), fill, np.float32)
    s = 2.125 + 0.25 * np.arange(600)
    a = np.radians(az)[:, None]
    x, y = s * np.sin(a), s * np.cos(a)
    for a0, s0, val, rad in blobs:
        x0, y0 = s0 * math.sin(math.radians(a0)), s0 * math.cos(math.radians(a0))
        v[np.hypot(x - x0, y - y0) < rad] = val
    return SweepImage(key=(pid,), product=pid, values=v, az=az, az_lo=az - 0.25, az_hi=az + 0.25, first_gate=2.125,
                      gate_spacing=0.25, elevation=0.5, ground_range=False, time=None)


def test_storm_flags_and_rotation():
    ref = _sweep("REF", np.nan, [(45, 50, 50, 8), (45, 50, 66, 2), (200, 80, 62, 2), (90, 8, 45, 3)])
    azsh = _sweep("AZSH", 0.0, [(46, 50, 18, 1.5), (90, 8, 20, 1.5)])            # second one: too near the radar
    cc = _sweep("CC", 0.98, [(46, 50.5, 0.7, 1.2), (90, 8, 0.5, 2)])
    zdr = _sweep("ZDR", 1.5, [(200, 80, 0.3, 3)])
    flags = st.detect(ref, azsh, cc, zdr)
    assert len(flags) == 2
    tor = next(f for f in flags if f["kind"] == "tds")
    assert set(tor["kinds"]) == {"tds", "rotation", "hail"} and math.hypot(tor["x"] - 35.4, tor["y"] - 35.4) < 3
    assert next(f for f in flags if f["kind"] == "hail")["text"].startswith("Hail likely")
    sh, vr, x, y = st.rotation_point(azsh, None, 35.5, 35.5)
    assert sh == pytest.approx(18) and vr is None
    vel = _sweep("VEL", 0.0, [(44.5, 50, -25.0, 1.0), (47.5, 50, 25.0, 1.0)])
    assert st.rotation_point(azsh, vel, 35.4, 35.4)[1] == pytest.approx(25 * st.KTS)
    cx, cy = st.follow_step(ref, 33, 33)
    assert math.hypot(cx - 35.36, cy - 35.36) < 1.0
    assert st.follow_step(ref, -60, -60) is None
    dx, dy = st.motion_xy(270, 60)                            # from the west at 60 kt: east 1.852 km/min
    assert dx == pytest.approx(1.852) and dy == pytest.approx(0, abs=1e-9)


def test_learn_mode_text():
    notes = st.explain({"L3B": 63.0, "CC": 0.75, "ZDR": 0.3, "VEL": -30.0, "AZSH": 16.0}, 12500)
    joined = " ".join(notes)
    assert "hail is possible" in joined and "debris" in joined and "classic hail signature" in joined
    assert "58 kt toward" in joined and "strong rotation" in joined and "12,500 ft" in joined
    assert st.explain({"REF": float("nan")}) == []
