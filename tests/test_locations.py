"""Saved locations and the alert engine: rules, distances, de-duplication, status lines, alert sounds."""
from __future__ import annotations

import io
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from radarforge.config import Settings
from radarforge.services import alerts, geo
from radarforge.services.locations import DEFAULT_RULES, Location, LocationBook, normalize_rules

NOW = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)


def box(lat, lon, half=0.2):
    """A square warning area (lon, lat pairs) around a point."""
    return [np.array([[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
                      [lon - half, lat + half], [lon - half, lat - half]])]


def warning(event="Tornado Warning", variant="TOR", lat=35.0, lon=-97.0, half=0.2, minutes=30, key="KOUN.TO.W.1",
            priority=11, action="NEW", label="Tornado"):
    return SimpleNamespace(event=event, variant=variant, rings=box(lat, lon, half), expires=NOW + timedelta(minutes=minutes),
                           key=key, action=action, style=((255, 0, 0), 3.0, 0, priority), variant_label=label,
                           area="Cleveland, OK", centroid=lambda: (lat, lon))


def loc(**rules):
    r = normalize_rules(rules)
    return Location("home", "Home", 35.0, -97.0, r)


# ------------------------------------------------------------------------------------------------ geometry
def test_distances_and_bearing():
    assert geo.distance_km(35, -97, 35, -97) == 0
    assert geo.distance_km(0, 0, 0, 1) == pytest.approx(111.19, rel=0.01)
    assert geo.bearing(35, -97, 36, -97) == pytest.approx(0, abs=0.5)
    assert geo.bearing(35, -97, 35, -96) == pytest.approx(90, abs=1)
    ring = box(35, -97, 0.1)[0]
    assert geo.rings_distance_km(35.0, -97.0, [ring]) == 0                          # inside
    d = geo.rings_distance_km(35.0, -96.8, [ring])                                  # 0.1 degrees east of the edge
    assert d == pytest.approx(0.1 * 111.19 * np.cos(np.radians(35)), rel=0.02)


# ------------------------------------------------------------------------------------------------ rules
def test_rules_are_always_complete_and_clamped():
    r = normalize_rules({"tornado": {"on": False, "miles": 999}, "outlook": {"min": "nonsense"}, "bogus": 1,
                         "lightning": {"miles": "x"}})
    assert r["tornado"] == {"on": False, "miles": 200} and r["outlook"]["min"] == "off"
    assert r["lightning"]["miles"] == 0 and set(r) == set(DEFAULT_RULES)
    assert normalize_rules(None) == DEFAULT_RULES


def test_book_migrates_the_old_single_location(tmp_path):
    s = Settings(tmp_path / "s.json")
    s["my_location"] = [35.2, -97.4]
    book = LocationBook(s)
    assert len(book.items) == 1 and book.primary().name == "Home" and book.primary().lat == 35.2
    book.add("Cabin", 36.0, -98.0)
    book.add("Mum", 34.0, -96.0)
    book.make_primary(book.items[2].id)
    assert [x.name for x in book.items] == ["Mum", "Home", "Cabin"] and s["my_location"] == [34.0, -96.0]
    book.set_primary_position(40.0, -100.0)                                       # right-click "set my location here"
    assert book.primary().lat == 40.0 and book.primary().name == "Mum"
    book.set_primary_position(None, None)                                        # remove my location
    assert [x.name for x in book.items] == ["Home", "Cabin"]
    again = LocationBook(s)                                                       # survives a restart
    assert [x.name for x in again.items] == ["Home", "Cabin"] and again.unique_name("Home") == "Home 2"


def test_book_ignores_broken_entries(tmp_path):
    s = Settings(tmp_path / "s.json")
    s["locations"] = [{"name": "bad"}, {"name": "far", "lat": 999, "lon": 0}, {"name": "ok", "lat": 1, "lon": 2}, "x"]
    assert [x.name for x in LocationBook(s).items] == ["ok"]


# ------------------------------------------------------------------------------------------------ evaluate
def test_warning_covering_the_location_alerts_once():
    ev = alerts.evaluate([loc()], [warning()], [], None, None, NOW)
    assert len(ev) == 1 and ev[0].kind == "warning" and ev[0].sound == "tornado" and "covers Home" in ev[0].body
    notified = {}
    assert len(alerts.announce(ev, notified, NOW)) == 1
    assert alerts.announce(alerts.evaluate([loc()], [warning()], [], None, None, NOW), notified, NOW) == []   # same again
    # upgraded to PDS: announced again
    pds = warning(variant="TORP", priority=13, label="Tornado - PDS")
    assert len(alerts.announce(alerts.evaluate([loc()], [pds], [], None, None, NOW), notified, NOW)) == 1


def test_warning_elsewhere_or_cancelled_or_off_does_not_alert():
    far = warning(lat=38.0)
    assert alerts.evaluate([loc()], [far], [], None, None, NOW) == []
    assert alerts.evaluate([loc()], [warning(action="CAN")], [], None, None, NOW) == []
    assert alerts.evaluate([loc()], [warning(minutes=-5)], [], None, None, NOW) == []                # expired
    assert alerts.evaluate([loc(tornado={"on": False, "miles": 0})], [warning()], [], None, None, NOW) == []


def test_radius_and_significant_only():
    near = warning(lat=35.4, half=0.1)                                           # about 30 km north of the location
    assert alerts.evaluate([loc()], [near], [], None, None, NOW) == []
    ev = alerts.evaluate([loc(tornado={"on": True, "miles": 25})], [near], [], None, None, NOW)
    assert len(ev) == 1 and "mi from Home" in ev[0].body
    plain, pds = warning(), warning(variant="TORP", key="b")
    only = loc(significant_only=True)
    assert [e.key for e in alerts.evaluate([only], [plain, pds], [], None, None, NOW)] == ["home|b"]


def test_watch_mcd_outlook_and_lightning_rules():
    watch = warning(event="Tornado Watch", variant="TOA", half=1.0, key="W1", priority=1, label="Tornado Watch")
    mcd = {"number": 1234, "rings": [[(-98.0, 34.0), (-96.0, 34.0), (-96.0, 36.0), (-98.0, 36.0)]],
           "expire": NOW + timedelta(hours=1), "concerning": "severe thunderstorm watch likely"}
    outlook = [{"category": "CATEGORICAL", "threshold": "ENH", "rings": mcd["rings"], "issue": NOW,
                "expire": NOW + timedelta(hours=8)},
               {"category": "TORNADO", "threshold": "0.05", "rings": mcd["rings"], "issue": NOW,
                "expire": NOW + timedelta(hours=8)}]
    default = alerts.evaluate([loc()], [watch], [mcd], outlook, lambda *a: 9, NOW)
    assert default == []                                                          # off unless asked for
    everything = loc(watch={"on": True}, mcd={"on": True}, outlook={"min": "ENH"}, lightning={"miles": 10})
    ev = {e.kind: e for e in alerts.evaluate([everything], [watch], [mcd], outlook, lambda lat, lon, km: 7, NOW, 10)}
    assert set(ev) == {"watch", "mcd", "outlook", "lightning"}
    assert "Enhanced" in ev["outlook"].title and "Tornado 5%" in ev["outlook"].body
    assert "7 lightning flashes within 10 mi" in ev["lightning"].body and "Discussion 1234" in ev["mcd"].title.replace("Mesoscale ", "")
    assert alerts.evaluate([loc(outlook={"min": "MDT"})], [], [], outlook, None, NOW) == []     # Enhanced < Moderate


def test_lightning_cooldown_and_dedupe_housekeeping():
    where = loc(lightning={"miles": 10})
    notified = {}
    first = alerts.announce(alerts.evaluate([where], [], [], None, lambda *a: 3, NOW), notified, NOW)
    assert len(first) == 1
    soon = NOW + timedelta(minutes=5)
    assert alerts.announce(alerts.evaluate([where], [], [], None, lambda *a: 3, soon), notified, soon) == []
    later = NOW + timedelta(minutes=20)                                          # cooldown over: tell me again
    assert len(alerts.announce(alerts.evaluate([where], [], [], None, lambda *a: 3, later), notified, later)) == 1
    assert alerts.announce([], notified, NOW + timedelta(days=1)) == [] and notified == {}


def test_several_locations_each_get_their_own_alert():
    a, b = loc(), Location("cabin", "Cabin", 35.1, -97.1, normalize_rules(None))
    ev = alerts.evaluate([a, b], [warning()], [], None, None, NOW)
    assert sorted(e.location.name for e in ev) == ["Cabin", "Home"] and len({e.key for e in ev}) == 2


# ------------------------------------------------------------------------------------------------ status board
def test_status_lines_show_what_is_near_regardless_of_rules():
    inside, near = warning(), warning(event="Severe Thunderstorm Warning", variant="SVR", lat=35.45, half=0.1, key="S1",
                                      priority=8, label="Severe Thunderstorm")
    lines = alerts.status_lines(loc(tornado={"on": False, "miles": 0}), [inside, near], [], None, lambda lat, lon, km: 2, NOW)
    kinds = [k for _p, k, _t in lines]
    assert kinds[0] == "inside" and "near" in kinds and "lightning" in kinds
    assert "In a Tornado (TOR)" in lines[0][2] and "30 min left" in lines[0][2]
    assert any("to the N" in t for _p, _k, t in lines)
    assert alerts.status_lines(loc(), [], [], None, None, NOW) == []


# ------------------------------------------------------------------------------------------------ sounds
@pytest.mark.parametrize("kind", ["tornado", "severe", "info", "unknown"])
def test_alert_sounds_are_valid_wav_files(kind):
    data = alerts.tone_wav(kind)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 22050
        frames = w.readframes(w.getnframes())
        assert 0.4 < w.getnframes() / w.getframerate() < 3.0
    samples = np.frombuffer(frames, np.int16)
    assert abs(int(samples[0])) < 800 and np.abs(samples).max() > 5000           # starts quietly, is audible
    assert len(alerts.tone_wav("tornado")) > len(alerts.tone_wav("info"))
