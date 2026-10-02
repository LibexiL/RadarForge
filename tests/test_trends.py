from datetime import datetime, timedelta, timezone

from radarforge.services import trends

T0 = datetime(2013, 5, 20, 19, 30, tzinfo=timezone.utc)


def cell(i, x=10.0, y=20.0, **kw):
    base = dict(id=i, x=x, y=y, motion=(230, 25), posh=None, size=None, meso=None, tvs=None)
    base.update(kw)
    return base


def hist():
    return [(T0 + timedelta(minutes=5 * k), cells) for k, cells in enumerate([
        [cell("A1", posh=20), cell("B2")],
        [cell("A1", posh=40, size=0.75, meso=3)],
        [cell("B2")],                                    # A1 missing from this frame
        [cell("A1", posh=70, size=1.5, meso=5, tvs="TVS")],
    ])]


def test_series_skips_frames_without_the_cell():
    s = trends.build(hist(), "A1")
    assert len(s) == 3 and s.posh == [20, 40, 70] and s.tvs == [0, 0, 2]
    assert s.speed == [25, 25, 25] and abs(s.range_km[0] - 22.36) < 0.01


def test_series_is_time_ordered_even_if_history_is_not():
    s = trends.build(list(reversed(hist())), "A1")
    assert s.times == sorted(s.times)


def test_trend_words_and_summary():
    s = trends.build(hist(), "A1")
    assert trends.trend_word(s.posh) == "rising"
    assert trends.trend_word([5, 5, 5]) == "steady"
    assert trends.trend_word([None, 3]) == "steady"
    line = trends.summary(s)
    assert "POSH 70% (rising)" in line and "TVS" in line
    assert "no data" in trends.summary(trends.build(hist(), "ZZ"))


def test_chart_draws(tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from radarforge.tools.trends import draw
    fig = Figure(figsize=(8, 6))
    draw(fig, trends.build(hist(), "A1"), now=T0 + timedelta(minutes=5), dark=True)
    out = tmp_path / "t.png"
    fig.savefig(out)
    assert out.stat().st_size > 5000
