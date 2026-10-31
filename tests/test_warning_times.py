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


def _nws(key, event="Tornado Warning", variant="TOR"):
    return W.Alert(event, [RING], "nws", T0, T0 + timedelta(hours=1), "OUN", "area", [], "urn:" + key, variant,
                   key, "NEW", {"description": "text"})


def test_fastest_source_wins_and_merges():
    full = [_nws("KOUN.TO.W.0001"), _nws("KOUN.FA.Y.0002", "Flood Advisory", "FAY")]
    fast = [_nws("KOUN.TO.W.0001"), _nws("KOUN.SV.W.0003", "Severe Thunderstorm Warning", "SVR")]
    iem_js = {"features": [
        {"properties": {"phenomena": "TO", "significance": "W", "wfo": "OUN", "eventid": 7, "status": "NEW",
                        "issue": "2026-05-20T20:00:00Z", "expire": "2026-05-20T20:45:00Z",
                        "polygon_begin": "2026-05-20T20:00:00Z", "polygon_end": "2026-05-20T20:45:00Z"},
         "geometry": {"type": "Polygon", "coordinates": [RING.tolist()]}},
        {"properties": {"phenomena": "TO", "significance": "W", "wfo": "OUN", "eventid": 1, "status": "CON",
                        "tornadotag": "OBSERVED", "issue": "2026-05-20T19:50:00Z", "expire": "2026-05-20T20:45:00Z",
                        "polygon_begin": "2026-05-20T20:05:00Z", "polygon_end": "2026-05-20T20:45:00Z"},
         "geometry": {"type": "Polygon", "coordinates": [RING.tolist()]}},
        {"properties": {"phenomena": "SV", "significance": "W", "wfo": "OUN", "eventid": 9, "status": "CAN",
                        "issue": "2026-05-20T19:50:00Z", "expire": "2026-05-20T20:45:00Z",
                        "polygon_begin": "2026-05-20T20:05:00Z", "polygon_end": "2026-05-20T20:45:00Z"},
         "geometry": {"type": "Polygon", "coordinates": [RING.tolist()]}}]}
    iem = W.iem_active(iem_js, now=T0 + timedelta(minutes=10))
    assert sorted(W.event_key(a.key)[3] for a in iem) == [1, 7]          # the cancelled one is left out
    out = W.merge_alerts(full, fast, iem, fast_newer=True)
    keys = {W.event_key(a.key): a for a in out}
    assert ("OUN", "SV", "W", 3) in keys                                   # from the quick check
    assert ("OUN", "FA", "Y", 2) in keys                                   # non-urgent: from the full list
    assert ("OUN", "TO", "W", 7) in keys and "Iowa State" in keys[("OUN", "TO", "W", 7)].hover   # IEM first
    up = keys[("OUN", "TO", "W", 1)]
    assert up.variant == "TORR" and up.info.get("description") == "text"   # upgraded, NWS text kept
    again = W.merge_alerts(full, fast, iem, fast_newer=True)
    assert [a.hover for a in again] == [a.hover for a in out]              # (sources never edited)
    # an older quick check doesn't hide what the newer full list has
    stale = W.merge_alerts(full, [], [], fast_newer=False)
    assert {W.event_key(a.key) for a in stale} == {W.event_key(a.key) for a in full}


def test_location_alert_once_across_feeds():
    from radarforge.features import alerts
    assert alerts._canon("KOUN.TO.W.0042") == alerts._canon("OUN.TO.W.42@2026-05-20T20:00Z")


def test_county_alerts_drawn_fast_and_merged():
    """Neighbouring counties merge into one outline (the shared border goes); paths build from arrays; the
    drawing of an alert fetched again with the same counties is kept."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    a = np.array([(0, 0), (10, 0), (10, 10), (0, 10)], float)
    b = np.array([(10, 0), (20, 0), (20, 10), (10, 10)], float)
    p = W.qpath([a, b], close=True)
    assert p.elementCount() == 10 and abs(p.boundingRect().width() - 20) < 1e-9
    outline = W.merged_outline([a, b])
    pts = np.concatenate(outline)
    assert len(outline) == 1 and pts[:, 0].min() == 0 and pts[:, 0].max() == 20
    assert not any(abs(x - 10) < 1e-6 and 0.5 < y < 9.5 for x, y in pts)        # no border down the middle
    runs = W.outline_runs([a, b])
    assert sum(len(r) - 1 for r in runs) == 6                                   # 8 edges less the shared one twice
    old = W.Alert("Winter Storm Warning", [a, b], "x", T0, T0, uid="u1")
    old.xy, old._proj, old._path = ["xy"], (35, -97), {"proj": (35, -97)}
    new = W.Alert("Winter Storm Warning", [a, b], "x", T0, T0, uid="u1")
    W.carry_geometry([old], [new])
    assert new.xy == ["xy"] and new._path is old._path
    moved = W.Alert("Winter Storm Warning", [a], "x", T0, T0, uid="u1")
    W.carry_geometry([old], [moved])
    assert moved.xy is None
