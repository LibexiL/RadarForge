"""Warnings at the scan's time, the new-warning flash and the classic default colours (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone

import numpy as np

from radarforge.features import warnings as W

T0 = datetime(2026, 5, 20, 20, 0, tzinfo=timezone.utc)
RING = np.array([(-97.7, 35.1), (-97.3, 35.1), (-97.3, 35.4), (-97.7, 35.4), (-97.7, 35.1)])


def _overlay(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from radarforge.config import Settings
    return W.WarningsOverlay(Settings(tmp_path / "s.json"), lambda: {})


def _piece(key, begin, end, variant="TOR", event="Tornado Warning"):
    a = W.Alert(event, [RING], "x", T0, T0 + timedelta(hours=1), "OUN", "#1", [], f"{key}@{begin}", variant, key)
    a.valid = (begin, end)
    return a


def test_iem_pieces_and_event_keys():
    js = {"features": [{"properties": {"phenomena": "TO", "significance": "W", "wfo": "OUN", "eventid": 42,
                                        "issue": "2026-05-20T20:00:00Z", "expire": "2026-05-20T20:45:00Z",
                                        "polygon_begin": "2026-05-20T20:10:00Z", "polygon_end": "2026-05-20T20:25:00Z",
                                        "status": "CON", "tornadotag": "OBSERVED"},
                         "geometry": {"type": "Polygon", "coordinates": [RING.tolist()]}},
                        {"properties": {"phenomena": "FL", "significance": "W"}, "geometry": None}]}
    (a,) = W.parse_iem_sbw(js)
    assert a.key == "OUN.TO.W.42" and a.variant == "TORR" and a.issued.minute == 0 and a.expires.minute == 45
    assert a.valid[0].minute == 10 and a.valid[1].minute == 25
    assert W.event_key("KOUN.TO.W.0042") == W.event_key("OUN.TO.W.42") == ("OUN", "TO", "W", 42)
    assert W.event_key("KWNS.TO.A.0200") == W.event_key("OUN.TO.A.200")          # watches: national numbers


def test_each_scan_shows_its_own_warnings(tmp_path):
    o = _overlay(tmp_path)
    o.mode = "archive"
    big, small = _piece("OUN.TO.W.42", T0, T0 + timedelta(minutes=15)), \
        _piece("OUN.TO.W.42", T0 + timedelta(minutes=15), T0 + timedelta(minutes=40))
    later = _piece("OUN.SV.W.7", T0 + timedelta(minutes=30), T0 + timedelta(minutes=60), "SVR",
                   "Severe Thunderstorm Warning")
    o.history = [big, small, later]
    o._ver += 1
    o.frame_time = T0 + timedelta(minutes=5)
    assert o.shown_alerts() == [big]                                  # the polygon of that time
    o.frame_time = T0 + timedelta(minutes=20)
    assert o.shown_alerts() == [small]                                # after the update shrank it
    o.frame_time = T0 + timedelta(minutes=35)
    assert set(o.shown_alerts()) == {small, later}
    o.frame_time = T0 - timedelta(minutes=5)
    assert o.shown_alerts() == []                                     # before any was issued


def test_live_newest_scan_shows_what_is_in_force_now(tmp_path):
    o = _overlay(tmp_path)
    o.mode = "live"
    now_tor = W.Alert("Tornado Warning", [RING], "x", T0 + timedelta(minutes=50), T0 + timedelta(minutes=95),
                      "Norman OK", "", [], "nws1", "TOR", "KOUN.TO.W.0050")
    still = W.Alert("Severe Thunderstorm Warning", [RING], "x", T0 + timedelta(minutes=10), T0 + timedelta(minutes=90),
                    "Norman OK", "", [], "nws2", "SVR", "KOUN.SV.W.0007")
    o.alerts = [still, now_tor]
    o.history = [_piece("OUN.SV.W.7", T0 + timedelta(minutes=10), T0 + timedelta(minutes=90), "SVR",
                        "Severe Thunderstorm Warning")]
    o._ver += 1
    o.latest_time = T0 + timedelta(minutes=48)
    o.frame_time = o.latest_time
    assert set(o.shown_alerts()) == {still, now_tor}                  # newest scan: everything in force now
    o.frame_time = T0 + timedelta(minutes=20)
    shown = o.shown_alerts()
    assert len(shown) == 1 and shown[0].key == "OUN.SV.W.7"           # earlier scan: no duplicate, no future one


def test_new_and_upgraded_warnings_flash(tmp_path):
    o = _overlay(tmp_path)
    now = datetime.now(timezone.utc)
    tor = W.Alert("Tornado Warning", [RING], "x", now, now + timedelta(minutes=30), "", "", [], "a", "TOR",
                  "KOUN.TO.W.0001")
    o._note_new([tor], first=True)
    assert o.flashing(tor) is None                                    # what's there at start doesn't flash
    svr = W.Alert("Severe Thunderstorm Warning", [RING], "x", now, now + timedelta(minutes=30), "", "", [], "b",
                  "SVR", "KOUN.SV.W.0002")
    o._note_new([tor, svr], first=False)
    assert o.flashing(svr) is not None and o.flashing(tor) is None    # a new one does
    torr = W.Alert("Tornado Warning", [RING], "x", now, now + timedelta(minutes=30), "", "", [], "a", "TORR",
                   "KOUN.TO.W.0001")
    o._note_new([torr, svr], first=False)
    assert o.flashing(torr) is not None                               # upgraded to "observed": flashes again
    o.settings["warning_flash_s"] = 0
    o._flash.clear()
    o._note_new([torr, svr, W.Alert("Tornado Warning", [RING], "x", now, now, "", "", [], "c", "TOR",
                                    "KOUN.TO.W.0003")], first=False)
    assert not o._flash                                               # switched off


def test_classic_colours_are_the_default():
    from radarforge.config import Settings
    s = Settings.__new__(Settings)
    s.data = {"warning_lines": {}, "warning_colors": {}}
    assert W.line_style(s, "SVR")[0] == (255, 255, 0) and W.line_style(s, "FFW")[0] == (0, 255, 0)
    assert W.line_style(s, "TORR")[0] == (255, 0, 255) and W.NWS_PRESET["SVR"][0] == "#ffa500"
