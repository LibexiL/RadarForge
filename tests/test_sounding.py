"""Soundings: wind and dewpoint helpers, cleaning, the HRRR height / wind maths and MetPy-based parameters."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from radarforge.data import hrrr
from radarforge.services import sounding as sd


def textbook_supercell():
    """A warm, moist boundary layer under a steep, dry mid-level, with winds that turn clockwise and strengthen."""
    z = np.array([0, 250, 500, 1000, 1500, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 12000, 14000.0])
    p = 1000.0 * np.exp(-z / 7900.0)
    t = np.array([30, 28.5, 27, 23.5, 20, 16.5, 10, 4, -2.5, -9, -17, -25, -33, -42, -58, -56.0])        # tropopause near 12 km
    td = np.array([22, 21.5, 21, 19.5, 16, 10, 0, -8, -16, -24, -31, -38, -45, -52, -66, -74.0])
    d = np.interp(z, [0, 1000, 3000, 6000, 14000], [140, 170, 215, 250, 270])
    s = np.interp(z, [0, 1000, 3000, 6000, 14000], [10, 25, 35, 50, 60])
    u, v = sd.wind_components(d, s)
    return sd.Sounding.build(p, z + 300.0, t, td, u, v, source="test", valid=datetime(2026, 10, 2, 22, tzinfo=timezone.utc))


def test_wind_helpers_and_dewpoint():
    u, v = sd.wind_components(270, 20)                 # from the west: blowing towards the east
    assert u == pytest.approx(20) and v == pytest.approx(0, abs=1e-9)
    assert sd.wind_direction(*sd.wind_components(200, 15)) == pytest.approx(200)
    assert sd.wind_direction(0, -10) == pytest.approx(0)                  # blowing south = a north wind
    assert sd.dewpoint_c(20.0, 100.0) == pytest.approx(20.0, abs=0.05)
    assert sd.dewpoint_c(30.0, 50.0) == pytest.approx(18.4, abs=0.3)
    assert sd.dewpoint_c(10.0, 0.0) < -20                                  # bone dry doesn't blow up


def test_build_cleans_the_levels():
    s = sd.Sounding.build([1000, 900, np.nan, 900, 800, 700], [100, 1000, 1500, 1000.2, 2000, 3100],
                          [20, 15, 12, 15, 8, 0], [10, 12, 5, 12, 9, -5], [5, np.nan, 7, 8, 9, 10], [0, np.nan, 0, 1, 2, 3])
    assert s.pressure.tolist() == [1000, 900, 800, 700]                    # NaN level and the duplicate are gone
    assert (np.diff(s.pressure) < 0).all()
    assert (s.dewpoint <= s.temp).all()                                    # dewpoint can't exceed the temperature
    assert np.isfinite(s.u).all() and np.isfinite(s.v).all()               # the missing wind was filled in
    assert s.agl[0] == 0 and s.agl[-1] == 3000


def test_parameters_for_a_supercell_environment():
    par = sd.parameters(textbook_supercell())
    assert par.sbcape > 1500 and par.mucape >= par.sbcape - 1 and par.sbcin <= 0
    assert 400 < par.lcl_m < 1500 and par.lfc_m >= par.lcl_m - 50 and par.el_m > 8000
    assert par.shear_6km > 30 and par.shear_1km > 5 and par.shear_3km > par.shear_1km
    assert par.srh_1km > 50 and par.srh_3km > par.srh_1km
    d, spd = par.right_mover
    assert 180 < d < 290 and 15 < spd < 50                                  # moves from the southwest / west
    assert 3000 < par.freezing_m < 5000 and par.minus20_m > par.freezing_m
    assert par.lapse_0_3km == pytest.approx((30 - 10) / 3.0, abs=0.1)       # 20 °C over 3 km
    assert par.pwat_in > 0.8
    assert par.stp is not None and par.scp is not None and par.scp > 1


def test_parameters_for_a_stable_night_and_too_little_data():
    z = np.array([0, 200, 500, 1000, 2000, 4000, 8000.0])
    p = 1000 * np.exp(-z / 7900)
    t = np.array([5, 9, 11, 9, 2, -12, -45.0])                              # a strong inversion
    td = t - 3
    u, v = sd.wind_components(np.full(7, 270.0), np.full(7, 5.0))
    par = sd.parameters(sd.Sounding.build(p, z, t, td, u, v))
    assert (par.sbcape or 0) < 5 and (par.mlcape or 0) < 5            # nothing rises from the cold surface layer
    empty = sd.parameters(sd.Sounding.build([1000, 900], [0, 1000], [20, 10], [10, 5], [0, 0], [0, 0]))
    assert empty.sbcape is None and empty.right_mover is None              # nothing computable, nothing invented


def test_negative_zero_is_not_shown():
    rows = dict(sd.table_rows(sd.Parameters(srh_1km=-0.2, stp=-0.01, sbcin=-0.4)))
    assert ("SRH 0–1 km", "0 m²/s²") in rows["Helicity and storm motion"]
    assert ("Sig. tornado", "0.0") in rows["Other (fixed-layer indices)"] and ("SBCIN", "0 J/kg") in rows["Instability"]
    assert ("SBCIN", "-5 J/kg") in dict(sd.table_rows(sd.Parameters(sbcin=-5.0)))["Instability"]


def test_table_rows_cope_with_missing_numbers():
    rows = sd.table_rows(sd.Parameters())
    assert [name for name, _ in rows][:2] == ["Instability", "Levels (above ground)"]
    assert all(text == "–" for _name, items in rows for _label, text in items)
    full = dict(sd.table_rows(sd.parameters(textbook_supercell())))
    assert any("J/kg" in text for _l, text in full["Instability"])
    assert any(text.endswith(" m") for _l, text in full["Levels (above ground)"])


# ------------------------------------------------------------------------------------------------ HRRR maths
def test_idx_parsing_and_the_plan():
    text = "\n".join(["1:0:d=2026100220:HGT:50 mb:anl:", "2:100:d=2026100220:TMP:50 mb:anl:",
                      "3:250:d=2026100220:TMP:1013.2 mb:anl:", "4:400:d=2026100220:TMP:850 mb:anl:",
                      "5:900:d=2026100220:RH:850 mb:anl:", "6:1000:d=2026100220:UGRD:825 mb:anl:",
                      "7:1100:d=2026100220:TMP:725 mb:anl:", "8:1300:d=2026100220:TMP:650 mb:anl:",
                      "9:1500:d=2026100220:TMP:700 mb:anl:", "10:1600:d=2026100220:TMP:500 mb:anl:"])
    entries = hrrr.parse_idx(text)
    assert entries[0] == hrrr.Entry("HGT", "50 mb", 0, 99) and entries[-1].end == -1
    assert hrrr.pressure("850 mb") == 850 and hrrr.pressure("2 m above ground") is None
    assert hrrr.thinned_levels([1013.2, 1000, 975, 925, 850, 725, 700, 650, 625, 600, 550, 500, 125, 100, 75]) == \
        [1000, 975, 925, 850, 725, 700, 650, 600, 550, 500, 100]
    sfc = hrrr.parse_idx("1:0:d=x:PRES:surface:anl:\n2:5:d=x:TMP:2 m above ground:anl:\n3:9:d=x:TMP:surface:anl:\n")
    pl, sf = hrrr.plan(entries, sfc)
    assert ("TMP", "850 mb") in {(e.var, e.level) for e in pl} and ("TMP", "1013.2 mb") not in {(e.var, e.level) for e in pl}
    assert {(e.var, e.level) for e in sf} == {("PRES", "surface"), ("TMP", "2 m above ground")}
    assert hrrr.key(datetime(2026, 10, 2, 20, tzinfo=timezone.utc), 3, "prs") == \
        "hrrr.20261002/conus/hrrr.t20z.wrfprsf03.grib2"


def test_grid_winds_are_turned_to_east_north():
    u, v = hrrr.earth_relative(10.0, 0.0, 262.5, 262.5, 38.5)               # on the central meridian: unchanged
    assert (u, v) == (10.0, pytest.approx(0.0))
    u, v = hrrr.earth_relative(10.0, 0.0, 262.5 + 20, 262.5, 38.5)          # east of it the grid leans: speed is kept
    g = math.radians(math.sin(math.radians(38.5)) * 20)
    assert math.hypot(u, v) == pytest.approx(10.0) and u == pytest.approx(10 * math.cos(g)) and v == pytest.approx(-10 * math.sin(g))
    u2, v2 = hrrr.earth_relative(10.0, 0.0, 262.5 - 20, 262.5, 38.5)        # west of it the other way round
    assert v2 == pytest.approx(10 * math.sin(g))
    u3, v3 = hrrr.earth_relative(0.0, 10.0, 262.5 + 20, 262.5, 38.5)        # a grid-north wind
    assert v3 == pytest.approx(10 * math.cos(g)) and u3 == pytest.approx(10 * math.sin(g))


def test_hypsometric_heights_match_the_standard_atmosphere():
    p = np.array([850.0, 700.0, 500.0, 300.0])
    # standard atmosphere temperatures at those pressures; dry air so virtual temperature = temperature
    t = np.array([275.15, 264.4, 252.7, 228.6]) - 0.0
    z = hrrr.heights(p, t, np.zeros(4), 1013.25, 0.0, 288.15, 0.0)
    assert z[0] == pytest.approx(1457, abs=25) and z[1] == pytest.approx(3012, abs=40)
    assert z[2] == pytest.approx(5574, abs=60) and z[3] == pytest.approx(9164, abs=100)
    assert (np.diff(z) > 0).all()
    moist = hrrr.heights(p, t, np.full(4, 90.0), 1013.25, 0.0, 288.15, 90.0)
    assert (moist > z).all()                                                # moist air is lighter: thicker layers


def test_sounding_from_a_hrrr_profile():
    raw = hrrr.Raw(datetime(2026, 10, 2, 21, tzinfo=timezone.utc), 1, 35.4, -97.5, np.array([950.0, 850.0, 700.0, 500.0]),
                   np.array([291.0, 285.0, 275.0, 255.0]), np.array([80.0, 70.0, 40.0, 20.0]),
                   np.array([-4.0, -5.0, 2.0, 8.0]), np.array([-6.0, -3.0, 4.0, 5.0]), 975.0, 375.0, 293.0, 81.0, -2.0, -3.0)
    s = sd.Sounding.from_hrrr(raw, "Oklahoma City")
    assert s.pressure[0] == 975 and len(s.pressure) == 5 and s.height[0] == 375 and s.source.startswith("HRRR 21Z")
    assert s.temp[0] == pytest.approx(19.85) and (s.dewpoint <= s.temp).all() and s.valid.hour == 22
    assert s.u[0] == pytest.approx(-2.0 * sd.MS_TO_KT)                       # converted to knots
    assert (np.diff(s.height) > 0).all()


# ------------------------------------------------------------------------------------------------ observed soundings
def test_launch_times():
    from radarforge.data import raob
    t = lambda h, m: datetime(2026, 10, 2, h, m, tzinfo=timezone.utc)      # noqa: E731
    assert raob.latest_launch(t(14, 0)) == t(12, 0)
    assert raob.latest_launch(t(13, 0)) == t(0, 0)                          # the 12Z data isn't out yet at 13:00Z
    assert raob.latest_launch(t(1, 0)) == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert raob.previous_launch(t(12, 0)) == t(0, 0)


def test_station_list_parsing_and_nearest():
    from radarforge.data import raob
    js = {"features": [{"properties": {"sid": "KOUN", "sname": "NORMAN"}, "geometry": {"coordinates": [-97.44, 35.18]}},
                       {"properties": {"sid": "KFWD", "sname": "FORT WORTH"}, "geometry": {"coordinates": [-97.30, 32.83]}},
                       {"properties": {}, "geometry": {"coordinates": [0, 0]}}]}
    got = raob.parse_stations(js)
    assert got == [("KFWD", "FORT WORTH", 32.83, -97.30), ("KOUN", "NORMAN", 35.18, -97.44)]
    sid, name, _la, _lo, km = raob.nearest_station(35.4, -97.5, got)
    assert sid == "KOUN" and km < 40
    assert raob.nearest_station(33.0, -97.0, raob.BUILTIN_STATIONS)[0] == "KFWD"
    assert len({s[0] for s in raob.BUILTIN_STATIONS}) == len(raob.BUILTIN_STATIONS) >= 60


def test_observed_profile_parsing():
    from radarforge.data import raob
    levels = [{"pres": 975.0, "hght": 357.0, "tmpc": 24.0, "dwpc": 20.0, "drct": 170, "sknt": 15},
              {"pres": 925.0, "hght": 760.0, "tmpc": 22.0, "dwpc": 18.0, "drct": 185, "sknt": 25},
              {"pres": 850.0, "hght": 1480.0, "tmpc": 18.0, "dwpc": 14.0, "drct": 200, "sknt": 35},
              {"pres": 700.0, "hght": 3120.0, "tmpc": 8.0, "dwpc": -2.0, "drct": 225, "sknt": 45},
              {"pres": 500.0, "hght": 5800.0, "tmpc": -12.0, "dwpc": -25.0, "drct": 250, "sknt": 60},
              {"pres": 400.0, "hght": 7400.0, "tmpc": None, "dwpc": None, "drct": None, "sknt": None},     # incomplete level
              {"pres": 300.0, "hght": 9600.0, "tmpc": -40.0, "dwpc": -50.0, "drct": 255, "sknt": 80}]
    js = {"profiles": [{"station": "KOUN", "valid": "2026-10-02T12:00:00Z", "profile": levels}]}
    s = raob.parse_profile(js, "KOUN", "Norman OK", 35.18, -97.44)
    assert s.source == "Observed KOUN" and s.place == "Norman OK" and s.valid.hour == 12
    assert s.pressure.tolist() == [975, 925, 850, 700, 500, 300]
    assert s.u[0] > 0 and s.v[0] < 0 or s.u[0] != 0                         # wind from 170 degrees: blows towards the north-north-west
    assert sd.wind_direction(s.u[0] / sd.MS_TO_KT, s.v[0] / sd.MS_TO_KT) == pytest.approx(170, abs=1)
    assert np.hypot(s.u[0], s.v[0]) == pytest.approx(15)                    # knots stay knots
    assert sd.parameters(s).sbcape is not None
    for bad in ({}, {"profiles": []}, {"profiles": [{"profile": levels[:2]}]}):
        with pytest.raises(ValueError):
            raob.parse_profile(bad)
    assert raob.parse_profile([{"station": "X", "profile": levels}]).pressure.size == 6        # the bare list form
