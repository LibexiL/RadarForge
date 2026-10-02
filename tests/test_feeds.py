"""Storm chasers, storm reports, SPC and storm-track tests (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timezone

import numpy as np
import pytest

from radarforge import fmt
from radarforge.data import feeds
from radarforge.tools import track

SN_POSITIONS = """Refresh: 1
Threshold: 999
Title: Spotter Network Positions (96dpi) - All
IconFile: 1, 30, 30, 15, 15, "https://www.spotternetwork.org/iconsheets/Spotternet_096.png"
IconFile: 2, 21, 35, 10, 17, "https://www.spotternetwork.org/iconsheets/Arrows_096.png"
Object: 40.0495262,-111.6625595
Icon: 0,0,000,6,6,"Ryatt Brown\\n2026-10-01 21:45:06 UTC\\nSTATIONARY"
Text: 15, 10, 1, "Ryatt Brown"
End:
Object: 32.7556152,-97.6989136
Icon: 0,0,3,2,15,
Icon: 0,0,000,6,2,"Victor Florez\\n2026-10-01 21:59:38 UTC\\nHeading: N (3)\\nPhone: 555-0100\\nEmail: a@example.com\\nNote: FF/EMR"
Text: 15, 10, 1, "Victor Florez"
End:
Object: 39.05,-96.77
Icon: 0,0,265,2,15,
Icon: 0,0,000,6,6,"Richard Gantt (K8RCG)\\n2026-10-01 21:43:07 UTC\\nWeb: https://example.com/a, b"
Text: 15, 10, 1, "K8RCG"
End:
"""


def test_chasers_parse():
    c = feeds.parse_chasers(SN_POSITIONS)
    assert len(c) == 3
    assert c[0]["name"] == "Ryatt Brown" and c[0]["heading"] is None
    assert c[0]["time"] == datetime(2026, 10, 1, 21, 45, 6, tzinfo=timezone.utc)
    assert c[1]["heading"] == 3
    assert c[1]["info"] == [("Note", "FF/EMR")]              # phone / e-mail not kept
    assert c[2]["label"] == "K8RCG" and c[2]["heading"] == 265  # from the arrow icon
    assert ("Web", "https://example.com/a, b") in c[2]["info"]


def test_report_kinds_and_lsr():
    for text, kind in (("TORNADO", "tornado"), ("FUNNEL CLOUD", "funnel"), ("WALL CLOUD", "wall"), ("HAIL", "hail"),
                       ("TSTM WND DMG", "wind_damage"), ("TSTM WND GST", "wind_gust"), ("FLASH FLOOD", "flood"),
                       ("EXTR WIND CHILL", "other"), ("SNOW", "other"), ("WATERSPOUT", "tornado")):
        assert feeds.report_kind(text) == kind, text
    js = {"features": [
        {"properties": {"typetext": "HAIL", "magf": 1.75, "unit": "Inch", "city": "3 N Norman", "st": "OK",
                        "valid": "2026-10-01T21:10:00Z", "wfo": "OUN", "source": "Trained Spotter"},
         "geometry": {"type": "Point", "coordinates": [-97.44, 35.26]}},
        {"properties": {"typetext": "TSTM WND DMG"}, "geometry": None}]}
    r = feeds.parse_lsr(js)
    assert len(r) == 1 and r[0]["kind"] == "hail" and r[0]["source"] == "NWS"
    assert r[0]["time"] == datetime(2026, 10, 1, 21, 10, tzinfo=timezone.utc)
    assert "Norman" in r[0]["hover"]


def test_spotter_network_reports():
    text = """Object: 35.20,-97.60
Icon: 0,0,000,3,1,"Reported By: Hailey Smith\\nTornado\\nTime: 2026-10-01 21:30:00 UTC\\nNotes: rope"
End:
Object: 35.40,-97.30
Icon: 0,0,000,3,4,"Reported By: Pat Lee\\nHail\\nTime: 2026-10-01 21:40:00 UTC\\nNotes: no tornado seen"
End:
"""
    r = feeds.parse_sn_reports(text)
    assert [x["kind"] for x in r] == ["tornado", "hail"]       # names and notes don't decide the type
    assert r[0]["time"] == datetime(2026, 10, 1, 21, 30, tzinfo=timezone.utc)
    assert r[0]["source"] == "Spotter Network"


def test_spc_outlook_and_mcd():
    js = {"features": [
        {"properties": {"category": "CATEGORICAL", "threshold": "SLGT", "issue": "2026-10-01T20:00:00Z",
                        "expire": "2026-10-02T12:00:00Z"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[[[-100, 30], [-90, 30], [-90, 40], [-100, 40], [-100, 30]],
                                                                [[-97, 34], [-96, 34], [-96, 35], [-97, 35], [-97, 34]]]]}},
        {"properties": {"category": "CATEGORICAL", "threshold": "TSTM"},
         "geometry": {"type": "Polygon", "coordinates": [[[-105, 25], [-85, 25], [-85, 45], [-105, 45], [-105, 25]]]}},
        {"properties": {"category": "TORNADO", "threshold": "0.05"},
         "geometry": {"type": "Polygon", "coordinates": [[[-98, 33], [-95, 33], [-95, 36], [-98, 36], [-98, 33]]]}},
        {"properties": {"category": "WIND", "threshold": "SIGN"},
         "geometry": {"type": "Polygon", "coordinates": [[[-98, 33], [-95, 33], [-95, 36], [-98, 36], [-98, 33]]]}},
    ]}
    areas = feeds.parse_outlook(js)
    o = feeds.outlook_at(areas, 33.5, -96.5)
    assert o["cat"] == "SLGT" and o["tornado"] == pytest.approx(0.05) and o["wind"] is None and "WIND" in o["sig"]
    hole = feeds.outlook_at(areas, 34.5, -96.5)                 # inside the SLGT hole: only TSTM
    assert hole["cat"] == "TSTM"
    assert feeds.outlook_at(areas, 50.0, -96.0) is None
    assert feeds.prob_text("0.15") == "15%" and feeds.prob_text("SIGN") == "significant"

    def at(s):
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    assert feeds.outlook_requests(at("2026-10-01T21:00:00"))[:2] == [("2026-10-01", 20), ("2026-10-01", 16)]
    assert feeds.outlook_requests(at("2026-10-02T02:00:00"))[0] == ("2026-10-01", 1)
    assert feeds.outlook_requests(at("2026-10-01T05:20:00"))[0] == ("2026-10-01", 6)

    mcd = feeds.parse_mcd({"features": [{"properties": {"num": 2335, "product_id": "p", "watch_confidence": 20.0,
                                                        "concerning": "SEVERE POTENTIAL", "expire": "2026-10-02T01:45:00Z"},
                                         "geometry": {"type": "Polygon", "coordinates": [[[-104, 31], [-101, 31], [-101, 34],
                                                                                          [-104, 34], [-104, 31]]]}}]})
    assert mcd[0]["number"] == 2335 and mcd[0]["watch"] == 20
    assert feeds.rings_contain(mcd[0]["rings"], 32, -102) and not feeds.rings_contain(mcd[0]["rings"], 35, -102)
    assert feeds.mcd_page(2335, 2026) == "https://www.spc.noaa.gov/products/md/2026/md2335.html"


def test_storm_track_maths():
    xy = np.array([[30.0, 2.0], [15.0, -4.0], [45.0, 20.0], [70.0, 0.0], [-5.0, 0.0]])
    pop = np.array([9000, 8000, 7000, 6000, 5000])
    names = np.array(["Mid", "Early", "OffTrack", "Beyond", "Behind"])
    e = track.track_etas((0, 0), (60, 0), 60, xy, pop, names, 8.0)
    assert [n for n, *_ in e] == ["Early", "Mid"]
    assert e[0][1] == pytest.approx(15) and e[1][1] == pytest.approx(30)
    assert track.eta_at((0, 0), (60, 0), 60, (90, 3), 8.0) == pytest.approx(90)
    assert track.eta_at((0, 0), (60, 0), 60, (-10, 0), 8.0) is None
    assert track.tick_minutes(60) == 15 and fmt.compass(47) == "NE"
    assert feeds.vtec_key("/O.CON.KOUN.TO.W.0042.261001T0230Z-261001T0315Z/") == ("CON", "KOUN.TO.W.0042")


def test_track_tool_and_new_ui(tmp_path):
    """The storm track tool, favourites and location in the real window classes (offscreen)."""
    from PySide6.QtWidgets import QApplication
    assert QApplication.instance() or QApplication([])
    from radarforge.config import Settings
    from radarforge.render.glview import RadarView
    s = Settings(tmp_path / "s.json")
    v = RadarView()
    v.track_default_fn = lambda x, y, m: (x + 40.0 * m / 60, y)
    v.track_minutes = 60
    v.maps.city_xy = np.array([[20.0, 1.0], [80.0, 0.0]])
    v.maps.city_pop = np.array([5000, 5000])
    v.maps.city_name = np.array(["Town", "Far"])
    seen = []
    v.trackChanged.connect(lambda: seen.append(1))
    v.set_track((0.0, 0.0))
    assert v.track["b"] == (40.0, 0.0) and [e[0] for e in v.track["etas"]] == ["Town"]
    kmh, heading = v.track_motion()
    assert kmh == pytest.approx(40) and heading == pytest.approx(90)
    v.set_track_minutes(120)                                     # keeps the speed, arrow twice as long
    assert v.track["b"] == pytest.approx((80.0, 0.0)) and {e[0] for e in v.track["etas"]} == {"Town", "Far"}
    v.clear_track()
    assert v.track is None and seen
    from radarforge.overlays.locations import LocationsOverlay
    from radarforge.services.locations import LocationBook
    s["my_location"] = [35.0, -97.0]
    assert LocationsOverlay(LocationBook(s)).latlon() == (35.0, -97.0)
    from radarforge.ui.dialogs import SiteDialog
    d = SiteDialog("KTLX", None, s)
    d.list.setCurrentRow(0)
    first = d.selected()
    d._toggle_fav()
    assert s["favorite_sites"] == [first]
    assert d.list.item(0).text().startswith("★")


# ------------------------------------------------------------------------------------------------ METARs
def test_metar_parsing_prefers_the_structured_fields_and_falls_back_to_the_raw_text():
    from radarforge.data import metar
    reports = [
        {"icaoId": "KOKC", "name": "Oklahoma City", "lat": 35.39, "lon": -97.60, "obsTime": 1790980800, "temp": 24.0,
         "dewp": 18.0, "wdir": 190, "wspd": 14, "wgst": 22, "visib": "10+", "slp": 1013.2, "cover": "SCT", "wxString": "-RA",
         "rawOb": "KOKC 022153Z 19014G22KT 10SM SCT050 24/18 A2992"},
        {"icaoId": "KOKC", "lat": 35.39, "lon": -97.60, "obsTime": 1790977200, "temp": 20.0},            # older: dropped
        {"icaoId": "KPWA", "name": "Wiley Post", "lat": 35.53, "lon": -97.65, "reportTime": "2026-10-02T21:53:00Z",
         "rawOb": "KPWA 022153Z VRB03KT 10SM BKN040 M02/M05 A2992"},                                       # only raw text
        {"icaoId": "KBAD", "lat": "x"}, {"lat": 1, "lon": 1}, "junk"]
    obs = {o.station: o for o in metar.parse(reports)}
    assert set(obs) == {"KOKC", "KPWA"}
    k = obs["KOKC"]
    assert (k.temp_c, k.dew_c, k.wdir, k.wspd, k.wgst, k.vis_mi, k.slp_hpa, k.cover, k.wx) == (24.0, 18.0, 190.0, 14.0, 22.0, 10.0, 1013.2, "SCT", "-RA")
    assert k.temp_f() == pytest.approx(75.2) and k.sky == 0.4
    p = obs["KPWA"]
    assert p.temp_c == -2.0 and p.dew_c == -5.0 and p.wdir is None and p.cover == "BKN" and p.sky == 0.75
    assert p.time == datetime(2026, 10, 2, 21, 53, tzinfo=timezone.utc)
    assert metar.parse({"not": "a list"}) == []
    assert metar._vis("1 1/2") == 1.5 and metar._vis("1/4") == 0.25 and metar._vis("10+") == 10 and metar._vis(None) is None


def test_station_model_helpers():
    from radarforge.data import metar
    assert metar.barb_parts(0) == [] and metar.barb_parts(4) == ["half"] and metar.barb_parts(5) == ["half"]
    assert metar.barb_parts(10) == ["full"] and metar.barb_parts(15) == ["full", "half"]
    assert metar.barb_parts(25) == ["full", "full", "half"] and metar.barb_parts(50) == ["pennant"]
    assert metar.barb_parts(65) == ["pennant", "full", "half"] and metar.barb_parts(115) == ["pennant", "pennant", "full", "half"]
    assert metar.pressure_code(1013.2) == "132" and metar.pressure_code(998.7) == "987" and metar.pressure_code(None) == ""
    mk = lambda sid, t, w: metar.Obs(sid, sid, 0, 0, datetime(2026, 10, 2, tzinfo=timezone.utc), t, None, None, w, None, None, None, "", "", "")   # noqa: E731
    near = [mk("A", 20.0, None), mk("B", 21.0, 5.0), mk("C", None, None), mk("D", 5.0, 5.0)]
    spots = {"A": (10, 10), "B": (12, 12), "C": (200, 200), "D": (300, 300)}
    kept = metar.declutter(near, lambda o: spots[o.station], 50)
    assert sorted(o.station for o, _p in kept) == ["B", "C", "D"]                        # B is fuller than A in the same square
    assert metar.declutter(near, lambda o: None, 50) == []


def test_spc_day2_geojson_and_outlook_days():
    js = {"features": [
        {"properties": {"LABEL": "SLGT", "ISSUE": "202610021730", "EXPIRE": "202610031200", "VALID": "202610021730"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[[[-100, 30], [-95, 30], [-95, 35], [-100, 35], [-100, 30]]]]}},
        {"properties": {"LABEL": "ENH", "ISSUE": "202610021730", "EXPIRE": "202610031200"},
         "geometry": {"type": "Polygon", "coordinates": [[[-98, 32], [-96, 32], [-96, 34], [-98, 34], [-98, 32]]]}},
        {"properties": {"LABEL": "ZZZ"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
        {"properties": {"LABEL": "MRGL"}, "geometry": None}]}
    areas = feeds.parse_spc_categorical(js)
    assert [a["threshold"] for a in areas] == ["SLGT", "ENH"] and all(a["category"] == "CATEGORICAL" for a in areas)
    assert areas[0]["expire"] == datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert feeds.outlook_at(areas, 33, -97)["cat"] == "ENH" and feeds.outlook_at(areas, 31, -99)["cat"] == "SLGT"
    assert feeds.outlook_at(areas, 40, -80) is None
    assert feeds.spc_outlook_url(2).endswith("day2otlk_cat.nolyr.geojson")
    assert feeds._spc_time("junk") is None


def test_alert_text_and_lookup_by_point(tmp_path):
    from radarforge.config import Settings
    from radarforge.overlays import warnings as w
    s = Settings(tmp_path / "s.json")
    ov = w.WarningsOverlay(s, lambda: {})
    ring = np.array([[-98.0, 34.0], [-96.0, 34.0], [-96.0, 36.0], [-98.0, 36.0], [-98.0, 34.0]])
    tor = w.Alert("Tornado Warning", [ring], "hover", None, None, "NWS Norman", "Cleveland, OK", variant="TORP")
    tor.text = "TORNADO WARNING...\n\nTake cover now."
    svr = w.Alert("Severe Thunderstorm Warning", [ring], "hover", None, None, "NWS Norman", "x", variant="SVR")
    ov.alerts = [svr, tor]
    assert [a.variant for a in ov.alerts_at(35.0, -97.0)] == ["TORP", "SVR"]          # worst first
    assert ov.alerts_at(40.0, -97.0) == [] and tor.text.startswith("TORNADO WARNING")
    assert w.Alert("Tornado Watch", [ring], "h", None, None).text == ""
