"""Following a storm from frame to frame."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from radarforge.services import follow

T0 = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)


def cell(i, x, y, motion=None):
    return {"id": i, "x": x, "y": y, "motion": motion}


def test_prediction_carries_on_with_the_motion():
    t = follow.Target(0.0, 0.0, T0, motion=(270.0, 30.0))                     # moving FROM the west = towards the east
    x, y = t.predicted(T0 + timedelta(minutes=10))
    assert x == pytest.approx(30 * 1.852 / 60 * 10) and y == pytest.approx(0, abs=1e-6)
    assert follow.Target(5, 6, T0).predicted(T0 + timedelta(hours=1)) == (5, 6)                    # no motion: stays put
    sw = follow.Target(0, 0, T0, motion=(225.0, 20.0)).predicted(T0 + timedelta(minutes=30))        # from SW = towards NE
    assert sw[0] > 0 and sw[1] > 0 and sw[0] == pytest.approx(sw[1])


def test_same_id_wins_over_a_nearer_stranger():
    t = follow.Target(10.0, 10.0, T0, motion=(270, 30), cell_id="B7", site="KTLX")
    cells = [cell("B7", 18.0, 11.0), cell("C1", 14.0, 10.0)]                  # C1 is nearer the prediction (~14, 10)
    new, c = follow.update(t, cells, T0 + timedelta(minutes=5), "KTLX")
    assert c["id"] == "B7" and (new.x, new.y) == (18.0, 11.0) and new.cell_id == "B7"


def test_a_different_radar_matches_by_position_not_id():
    t = follow.Target(10.0, 10.0, T0, motion=(270, 30), cell_id="B7", site="KTLX")
    cells = [cell("Q9", 22.0, 10.0), cell("B7", 80.0, 90.0)]                  # ids mean nothing across radars
    new, c = follow.update(t, cells, T0 + timedelta(minutes=5), "KFDR")
    assert c["id"] == "Q9" and new.site == "KFDR" and new.cell_id == "Q9"


def test_no_cell_nearby_means_dead_reckoning():
    t = follow.Target(0.0, 0.0, T0, motion=(270, 36), cell_id="B7", site="KTLX", label="x")
    new, c = follow.update(t, [cell("Z", 200.0, 200.0)], T0 + timedelta(minutes=10), "KTLX")
    assert c is None and new.x == pytest.approx(36 * 1.852 / 60 * 10) and new.time == T0 + timedelta(minutes=10)
    assert new.cell_id == "B7"                                                # still looking for the same cell
    none, c2 = follow.update(t, [], T0 + timedelta(minutes=10), "KTLX")
    assert c2 is None and none.x == pytest.approx(new.x)


def test_motion_is_learned_from_the_fixes_and_smoothed():
    t = follow.Target(0.0, 0.0, T0, motion=None, cell_id="A1", site="KTLX")
    t2, _ = follow.update(t, [cell("A1", 15.0, 0.0)], T0 + timedelta(minutes=10), "KTLX")        # 1.5 km/min east
    assert t2.motion[0] == pytest.approx(270, abs=0.5) and t2.motion[1] == pytest.approx(1.5 / follow.KT_TO_KM_PER_MIN, rel=0.01)
    t3, _ = follow.update(t2, [cell("A1", 15.0 + 3.0, 4.0)], T0 + timedelta(minutes=12), "KTLX")
    assert 240 < t3.motion[0] < 290 and t3.motion[1] > 20                      # one odd step only nudges the estimate
    cm, _ = follow.update(follow.Target(0, 0, T0), [cell("A", 1.0, 1.0, motion=(250.0, 25.0))], T0 + timedelta(seconds=30), "K")
    assert cm.motion == (250.0, 25.0)                                           # a fresh start takes the cell's own motion


def test_the_search_radius_grows_with_time_between_frames():
    t = follow.Target(0.0, 0.0, T0, motion=(270, 30))
    far = [cell("A", 60.0, 0.0)]
    assert follow.update(t, far, T0 + timedelta(minutes=2), "K")[1] is None            # 2 minutes at 30 kt is under 2 km
    assert follow.update(t, far, T0 + timedelta(minutes=60), "K")[1] is not None
    assert follow.nearest_cell(far, 0, 0, 100)["id"] == "A" and follow.nearest_cell(far, 0, 0, 10) is None


def test_label():
    assert follow.label(follow.Target(0, 0, T0, (245.0, 32.4), "B7")) == "B7 · 245° / 32 kt"
    assert follow.label(follow.Target(0, 0, T0)) == "storm"
    assert math.isclose(follow.KT_TO_KM_PER_MIN * 60, 1.852)
