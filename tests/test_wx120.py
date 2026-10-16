"""1.20: model GRIB2 decoding and fields, winter (precipitation type, snowfall files), tropical parsers and
arrival times, county-shaded alerts, winter storm reports (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gzip
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from radarforge.features import feeds, grib2, models, tropical as tr, winter
from radarforge.features import warnings as W

DATA = Path(__file__).parent / "data"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- GRIB2: model packings and grids
def test_lambert_packings_match_eccodes():
    raw = (DATA / "model_packings.grib2").read_bytes()          # made with eccodes from an HRRR message
    truth = np.load(DATA / "model_packings_truth.npz")
    grids = grib2.decode_all(raw)
    assert len(grids) == 5
    for g in grids[:4]:                                        # simple+bitmap, complex, spatial diff order 1 and 2
        assert g.lcc is not None and (g.ni, g.nj) == (60, 40)
        assert (np.isnan(g.values) == truth["miss"]).all()
        assert np.nanmax(np.abs(g.values - truth["truth"])) < 0.07     # (the packing keeps 1/8 K)
        # eccodes' own latitudes / longitudes land exactly on the grid points
        r, c = g.index(truth["lats"], truth["lons"])
        jj, ii = np.meshgrid(np.arange(0, 40, 7), np.arange(0, 60, 9), indexing="ij")
        assert np.abs(r - jj).max() < 1e-3 and np.abs(c - ii).max() < 1e-3
    gfs = grids[4]                                             # global lat/lon, complex + spatial differencing
    assert (gfs.ni, gfs.nj) == tuple(truth["gfs_shape"][::-1])
    assert np.abs(gfs.values.ravel()[:1000] - truth["gfs"]).max() < 1e-3
    # a global grid wraps across the dateline / prime meridian when sampled
    v = gfs.sample(np.array([10.0, 10.0]), np.array([179.9, -179.9]), bilinear=True)
    assert np.isfinite(v).all()


def test_lcc_round_trip():
    p = grib2.lcc_params(38.5, 38.5, -97.5, 6371229.0)
    lat = np.array([25.0, 38.5, 49.0])
    lon = np.array([-120.0, -97.5, -70.0])
    x, y = grib2.lcc_forward(p, lat, lon)
    la, lo = grib2.lcc_inverse(p, x, y)
    assert np.abs(la - lat).max() < 1e-6 and np.abs(lo - lon).max() < 1e-6


# --------------------------------------------------------------------------- model index files and fields
IDX = """1:0:d=2026101512:REFC:entire atmosphere:2 hour fcst:
2:100:d=2026101512:APCP:surface:0-2 hour acc fcst:
3:200:d=2026101512:APCP:surface:1-2 hour acc fcst:
4:300:d=2026101512:MXUPHL:5000-2000 m above ground:1-2 hour max fcst:
5:400:d=2026101512:TMP:2 m above ground:2 hour fcst:
"""


def test_parse_idx_and_find():
    rows = models.parse_idx(IDX)
    assert rows[0].end == 99 and rows[-1].end is None
    assert models.find_entry(rows, "APCP", "surface", "acc0", fh=2).start == 100
    assert models.find_entry(rows, "APCP", "surface", "acc1", fh=2).start == 200
    assert models.find_entry(rows, "MXUPHL", "5000-2000 m above ground", "max1", fh=2).start == 300
    assert models.find_entry(rows, "TMP", "2 m above ground").start == 400
    assert models.find_entry(rows, "CAPE", "surface") is None
    hrrr = models.MODELS["hrrr"]
    run = utc(2026, 10, 15, 12)
    assert hrrr.key(run, 3) == "hrrr.20261015/conus/hrrr.t12z.wrfsfcf03.grib2"
    assert hrrr.fh_of(hrrr.key(run, 3) + ".idx") == 3 and hrrr.max_fh(run) == 48
    gfs = models.MODELS["gfs"]
    assert gfs.key(run, 6) == "gfs.20261015/12/atmos/gfs.t12z.pgrb2.0p25.f006" and gfs.fh_of("x.f123.idx") == 123
    assert models.MODELS["nam"].fh_of("nam.t12z.conusnest.hiresf07.tm00.grib2.idx") == 7


def _fake_run(monkeypatch, fields):
    """Serves a small Lambert-grid "model run" from the packings file: each (var, level, fcst) is one message."""
    msg = list(grib2.messages((DATA / "model_packings.grib2").read_bytes()))[2]
    data, lines, off = b"", [], 0
    for i, (var, level, fc) in enumerate(fields, 1):
        lines.append(f"{i}:{off}:d=2026101512:{var}:{level}:{fc}:")
        data += msg
        off += len(msg)
    idx = "\n".join(lines) + "\n"

    def fake(url, headers=None, timeout=60):
        if url.endswith(".idx"):
            return idx.encode()
        a, b = headers["Range"][6:].split("-")
        return data[int(a):(int(b) + 1 if b else len(data))]
    monkeypatch.setattr(models, "_http_get", fake)
    monkeypatch.setattr(models, "_cached", lambda name, fetch, max_age_s=None: fetch())
    models._GRIDS.clear()


def test_compute_fields(monkeypatch):
    _fake_run(monkeypatch, [("CAPE", "surface", "2 hour fcst"), ("HLCY", "1000-0 m above ground", "2 hour fcst"),
                            ("HLCY", "3000-0 m above ground", "2 hour fcst"),
                            ("VUCSH", "0-6000 m above ground", "2 hour fcst"),
                            ("VVCSH", "0-6000 m above ground", "2 hour fcst"), ("TMP", "2 m above ground", "2 hour fcst"),
                            ("DPT", "2 m above ground", "2 hour fcst"), ("HGT", "500 mb", "2 hour fcst"),
                            ("UGRD", "500 mb", "2 hour fcst"), ("VGRD", "500 mb", "2 hour fcst")])
    run = utc(2026, 10, 15, 12)
    truth = np.load(DATA / "model_packings_truth.npz")["truth"]
    sh = models.compute("hrrr", run, 2, "shear06")
    assert sh.extra.keys() >= {"u", "v"}
    assert np.nanmax(np.abs(sh.value - np.hypot(truth, truth) * models.MS2KT)) < 0.5   # (rotated to earth winds)
    stp = models.compute("hrrr", run, 2, "stp")
    assert stp.valid == run + timedelta(hours=2) and np.isfinite(stp.value[~np.isnan(stp.value)]).all()
    # every value is 0 here: T = Td gives a cloud base at the ground, and CAPE ~ 250 J/kg
    assert np.nanmax(stp.value) == pytest.approx(np.nanmax((truth / 1500) * (truth / 150) * 1.5), rel=0.01)
    h5 = models.compute("hrrr", run, 2, "h500")
    assert {"z", "u", "v"} <= set(h5.extra)
    v = models.compute("hrrr", run, 2, "vort500")
    assert v.note.startswith("from the model winds")
    with pytest.raises(models.FieldUnavailable):
        models.compute("hrrr", run, 2, "pwat")


def test_accumulated_buckets(monkeypatch):
    """GFS-style buckets: 0-3 at hour 3, 0-6 at hour 6, 6-9 at hour 9 -> each step's own amount."""
    g = grib2.decode(list(grib2.messages((DATA / "model_packings.grib2").read_bytes()))[2])
    rows = {3: [models.IdxEntry(1, 0, 9, "APCP", "surface", "0-3 hour acc fcst")],
            6: [models.IdxEntry(1, 0, 9, "APCP", "surface", "0-6 hour acc fcst")],
            9: [models.IdxEntry(1, 0, 9, "APCP", "surface", "6-9 hour acc fcst")]}
    amounts = {3: 2.0, 6: 5.0, 9: 1.5}
    monkeypatch.setattr(models, "get_idx", lambda m, r, fh: rows[fh])

    def dec(m, r, fh, e):
        return grib2.GribGrid(values=np.full((2, 2), amounts[fh], np.float32), ni=2, nj=2, la1=0, lo1=0, di=1, dj=1)
    monkeypatch.setattr(models, "_decoded", dec)
    m = models.MODELS["gfs"]
    assert models._increment(m, None, 0, 3)[0, 0] == 2.0
    assert models._increment(m, None, 3, 6)[0, 0] == 3.0
    assert models._increment(m, None, 6, 9)[0, 0] == 1.5
    del g


def test_field_catalog_is_complete():
    for fd in models.FIELDS.values():
        assert fd.group in models.GROUPS and fd.recipes and fd.desc
        ct = models.colortable_for(fd)
        assert ct.entries
    ct = models.colortable_for(models.FIELDS["ptype"])
    assert sorted(ct.labels.values()) == sorted(models.PTYPE_NAMES)
    assert models.fmt_value(models.FIELDS["ptype"], 135.0) == "Snow, 35 dBZ"


# --------------------------------------------------------------------------- winter
def test_ptype_values():
    n = 10
    codes = np.zeros((n, n), np.int8)
    codes[: n // 2] = winter.SNOW                          # the northern half snows
    grid = winter.SurfaceGrid(codes, np.zeros((n, n), np.float32), utc(2026, 1, 5, 12), 100.0, (35, -97), "test")
    ref = np.array([[30.0, 30.0, 2.0]] * 2)
    az = np.array([0.0, 180.0])                            # north, south
    out = winter.ptype_values(ref, az, 10.0, 20.0, 0.5, grid)
    assert out[0, 0] == pytest.approx(130.0) and out[1, 0] == pytest.approx(30.0)
    assert np.isnan(out[0, 2])                              # below 5 dBZ: nothing
    assert winter.ptype_values(ref, az, 10.0, 20.0, 0.5, None)[0, 0] == pytest.approx(30.0)


def test_snowfall_listing():
    html = ('<a href="sfav2_CONUS_24h_2026011512.grb2">x</a> <a href="sfav2_CONUS_24h_2026011512_grid184.grb2">x</a>'
            '<a href="sfav2_CONUS_48h_2026011600_grid184.grb2">x</a> <a href="sfav2_CONUS_24h_2026011600_grid184.grb2">')
    files = winter.snowfall_files(html, 24)
    assert [t for t, _n in files] == [utc(2026, 1, 15, 12), utc(2026, 1, 16)]
    assert files[0][1].endswith("_grid184.grb2")
    assert len(winter.snowfall_files(html, 48)) == 1


def test_winter_reports():
    assert feeds.report_kind("HEAVY SNOW") == "snow" and feeds.report_kind("FREEZING RAIN") == "ice"
    assert feeds.report_kind("SLEET") == "sleet" and feeds.report_kind("BLIZZARD") == "blizzard"
    assert feeds.report_kind("SNOWMELT FLOODING") == "flood"
    assert feeds.snow_inches({"kind": "snow", "magnitude": "8.5 INCH"}) == 8.5
    assert feeds.snow_inches({"kind": "hail", "magnitude": "1.00 INCH"}) is None


# --------------------------------------------------------------------------- county-shaded alerts
def test_area_alerts_from_county_codes(monkeypatch):
    ring = np.array([(-97.7, 35.1), (-97.3, 35.1), (-97.3, 35.4), (-97.7, 35.1)])
    js = {"features": [{"geometry": None, "properties": {
        "event": "Winter Storm Warning", "sent": "2026-01-05T10:00:00Z", "expires": "2026-01-06T10:00:00Z",
        "senderName": "NWS Norman OK", "geocode": {"SAME": ["040109", "040017"]}, "parameters": {}, "id": "x"}}]}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return js
    monkeypatch.setattr(W.requests, "get", lambda *a, **k: Resp())
    alerts = W.fetch_live_alerts({40109: [ring], 40017: [ring + 0.5]})
    assert len(alerts) == 1 and alerts[0].is_area and len(alerts[0].rings) == 2
    assert alerts[0].variant == "WSW" and W.EVENT_GROUP["Winter Storm Warning"] == "WIN"
    assert W.EVENT_GROUP["Hurricane Warning"] == "TRO" and W.STYLES["Hurricane Warning"][2] > 0
    # storm-based warnings still draw above the county-based ones
    assert W.VARIANT["SVR"][4] > W.VARIANT["HUW"][4] > W.VARIANT["WWY"][4]


# --------------------------------------------------------------------------- tropical
CURRENT = {"activeStorms": [{"id": "al142026", "binNumber": "AT1", "name": "Harper", "classification": "HU",
                             "intensity": "105", "pressure": "958", "latitudeNumeric": 27.0, "longitudeNumeric": -89.0,
                             "movementDir": 335, "movementSpeed": 10, "lastUpdate": "2026-09-10T21:00:00.000Z",
                             "publicAdvisory": {"advNum": "12", "url": "https://example/adv"}}]}


def test_current_storms_and_categories():
    s = tr.parse_current(CURRENT)[0]
    assert s.title == "Major Hurricane Harper" and s.bin == "AT1" and s.links["Public advisory"]
    assert tr.category(105) == "3" and tr.category(33) == "TD" and tr.category(140) == "5"
    assert tr.compass(335) == "NNW"


def test_forecast_points_and_arrival():
    adv = utc(2026, 9, 10, 21)
    feats = [{"geometry": {"type": "Point", "coordinates": [lo, la]},
              "properties": {"tau": tau, "maxwind": w, "validtime": f"{(adv + timedelta(hours=tau)):%d/%H%M}"}}
             for tau, la, lo, w in [(0, 27.0, -89.0, 105), (12, 28.0, -89.6, 110), (24, 29.5, -90.0, 100),
                                    (36, 31.0, -90.2, 70)]]
    pts = tr.parse_forecast_points({"features": feats}, adv)
    assert [p.tau for p in pts] == [0, 12, 24, 36] and pts[2].time == adv + timedelta(hours=24)
    st = tr.parse_current(CURRENT)[0]
    st.forecast = pts
    st.radii = [(tau, 34, {"ne": 150, "se": 150, "sw": 120, "nw": 120}, adv + timedelta(hours=tau)) for tau in (0, 12, 24, 36)]
    a = tr.arrival(st, 29.95, -90.07)                      # New Orleans
    assert a["closest_km"] < 60 and adv + timedelta(hours=20) < a["closest_time"] < adv + timedelta(hours=32)
    assert a["ts_arrive"] is not None and a["ts_arrive"] < a["closest_time"]
    cone = tr.cone_polygon(pts, "AL")
    assert cone.shape[1] == 2 and np.allclose(cone[0], cone[-1])


def test_adeck_newest_cycles():
    lines = []
    for cyc, off in (("2026091012", 0.0), ("2026091018", 0.5)):
        for tau in (0, 12, 24):
            lines.append(f"AL, 14, {cyc}, 03, AVNO, {tau:3d}, {270 + tau + int(off * 10)}N, 0890W, 90, 960, HU")
    lines.append("AL, 14, 2026090100, 03, NVGM,   0, 200N, 0700W, 30, 1005, TD")     # an old run: dropped
    aids = tr.parse_adeck("\n".join(lines))
    cyc, pts = aids["AVNO"]
    assert cyc == utc(2026, 9, 10, 18) and pts[1].lat == pytest.approx(28.7) and pts[0].lon == pytest.approx(-89.0)
    assert "NVGM" not in aids
    allc = tr.parse_adeck("\n".join(lines), newest_only=False)
    assert len(allc["AVNO"]) == 2


HURDAT = """AL092022,                IAN,     4,
20220927, 0000,  , HU, 23.4N,  83.2W, 100,  960,
20220928, 0000,  , HU, 25.6N,  82.9W, 135,  937,
20220928, 1905, L, HU, 26.7N,  82.2W, 130,  941,
20220929, 1200,  , TS, 28.6N,  80.4W,  55,  990,
AL012022,               ALEX,     1,
20220605, 1800,  , TS, 31.0N,  77.0W,  45, 1000,
"""


def test_hurdat_and_history():
    storms = tr.parse_hurdat2(HURDAT)
    ian = storms[0]
    assert ian.name == "IAN" and ian.year == 2022 and ian.peak == 135 and len(ian.landfalls) == 1
    t, lat, lon = tr.key_moment(ian)
    assert t == utc(2022, 9, 28, 19, 5) and lat == pytest.approx(26.7)
    near = tr.tracks_near(storms, 26.6, -82.0, km=100)
    assert [s.name for s, _d in near] == ["IAN"]
    track = tr.interpolate_track(ian.points, 60)
    assert track[0][0] == ian.points[0].time and track[-1][0] == ian.points[-1].time


def test_ndbc_parse():
    text = ("#STN       LAT      LON  YYYY MM DD hh mm WDIR WSPD   GST WVHT  DPD APD MWD   PRES  PTDY  ATMP  WTMP  DEWP  VIS   TIDE\n"
            "#text\n42040  29.207  -88.237 2026 09 10 20 50 110  10.0  14.0  2.0  MM  MM  MM 1004.5  MM  27.0  28.0  MM  MM  MM\n")
    b = tr.parse_ndbc(text)[0]
    assert b.id == "42040" and b.wspd_kt == pytest.approx(19.44, abs=0.01) and b.wave_ft == pytest.approx(6.56, abs=0.01)
    assert b.pres == 1004.5 and b.water_f == pytest.approx(82.4)
