"""Model maps open and step quickly: the run search runs in parallel, the run list is remembered, and the
look-ahead downloads a field's inputs without decoding them."""
import time
from datetime import datetime, timedelta, timezone

from radarforge.features import models


def test_run_search_is_parallel_and_remembered(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_dir", lambda: tmp_path)
    now = datetime(2026, 11, 11, 13, 40, tzinfo=timezone.utc)
    hrrr = models.MODELS["hrrr"]
    calls = []

    def slow_list(model, run):
        calls.append(run)
        time.sleep(0.25)                              # one S3 listing
        return [] if run.hour == 13 else list(range(0, 19))     # the 13Z run isn't out yet

    monkeypatch.setattr(models, "list_run", slow_list)
    t = time.perf_counter()
    out = models.latest_runs(hrrr, count=4, now=now)
    took = time.perf_counter() - t
    assert [r.hour for r, _h in out] == [12, 11, 10, 9]
    assert len(calls) >= 5 and took < 0.8, f"{len(calls)} listings took {took:.2f}s"
    # a live search is kept on disk, so the next time the model opens it has a run straight away
    monkeypatch.setattr(models, "_RUNS", {})
    real_now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    models.save_runs(hrrr, [(real_now - timedelta(hours=1), [0, 1, 2])])
    got = models.cached_runs(hrrr)
    assert got == [(real_now - timedelta(hours=1), [0, 1, 2])]
    assert models.cached_runs(models.MODELS["gfs"]) == []


def test_search_that_cannot_connect_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_dir", lambda: tmp_path)

    def down(model, run):
        raise OSError("no route")
    monkeypatch.setattr(models, "list_run", down)
    try:
        models.latest_runs(models.MODELS["rap"], count=4, now=datetime(2026, 11, 11, 13, tzinfo=timezone.utc))
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected ConnectionError")


def test_fetch_inputs_downloads_without_decoding(monkeypatch):
    rows = models.parse_idx("1:0:d=2026111100:CAPE:surface:2 hour fcst:\n"
                            "2:100:d=2026111100:HLCY:1000-0 m above ground:2 hour fcst:\n")
    monkeypatch.setattr(models, "get_idx", lambda m, r, fh: rows)
    got = []
    monkeypatch.setattr(models, "get_message", lambda m, r, fh, e: got.append(e.var) or b"")
    monkeypatch.setattr(models.grib2, "decode", lambda raw: (_ for _ in ()).throw(AssertionError("decoded")))
    run = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert models.fetch_inputs("hrrr", run, 2, "cape_sb") == 1
    assert got == ["CAPE"]
    assert models.fetch_inputs("hrrr", run, 2, "qpf") == 0          # running totals aren't fetched ahead
