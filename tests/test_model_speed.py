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
    monkeypatch.setattr(models, "fetch_entries", lambda m, r, fh, entries: got.extend(e.var for e in entries))
    monkeypatch.setattr(models.grib2, "decode", lambda raw: (_ for _ in ()).throw(AssertionError("decoded")))
    run = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert models.fetch_inputs("hrrr", run, 2, "cape_sb") == 1
    assert got == ["CAPE"]
    assert models.fetch_inputs("hrrr", run, 2, "qpf") == 0          # running totals aren't fetched ahead


def test_fast_decoder_matches_eccodes_on_a_real_hrrr_message():
    import numpy as np
    from pathlib import Path
    from radarforge.features import grib2
    data = Path(__file__).parent / "data"
    g = grib2.decode((data / "hrrr_real_message.grib2").read_bytes())    # complex packing, 2nd-order differences
    truth = np.load(data / "hrrr_real_truth.npz")
    assert g.values.size == int(truth["n"]) and g.values.shape == (1059, 1799)
    got = g.values.ravel()[truth["idx"]].astype(np.float64)
    assert np.max(np.abs(got - truth["vals"]) / np.maximum(1.0, np.abs(truth["vals"]))) < 1e-6


def test_colour_lookup_matches_the_stops():
    import numpy as np
    from radarforge.features import georaster
    fd = models.FIELDS["cape_sb"]
    v = np.linspace(-100, 7000, 20001, dtype=np.float32)
    v[::50] = np.nan
    a = models.colorize(fd, v).astype(int)
    b = georaster.colorize(v, fd.stops).astype(int)
    assert np.percentile(np.abs(a - b).max(axis=-1), 99.5) <= 3
    assert (a[np.isnan(v)] == 0).all() and (a[v < fd.stops[0][0]] == 0).all()


def test_fetch_entries_coalesces_neighbours(monkeypatch, tmp_path):
    """U and V (next to each other in the file) come in one request; a far-away piece in its own."""
    monkeypatch.setattr(models, "_cache_dir", lambda: tmp_path)
    rows = models.parse_idx("1:0:d=2026111100:CAPE:surface:2 hour fcst:\n"
                            "2:1000:d=2026111100:UGRD:10 m above ground:2 hour fcst:\n"
                            "3:1600:d=2026111100:VGRD:10 m above ground:2 hour fcst:\n"
                            "4:2200:d=2026111100:REFC:entire atmosphere:2 hour fcst:\n"
                            "5:9000000:d=2026111100:TMP:2 m above ground:2 hour fcst:\n")
    blob = bytes(range(256)) * 40000
    calls = []

    def fake(url, headers=None, timeout=60):
        a, b = headers["Range"][6:].split("-")
        calls.append((int(a), int(b) if b else None))
        return blob[int(a):(int(b) + 1 if b else len(blob))]
    monkeypatch.setattr(models, "_http_get", fake)
    run = datetime(2026, 11, 11, tzinfo=timezone.utc)
    hrrr = models.MODELS["hrrr"]
    n = models.fetch_entries(hrrr, run, 2, [rows[1], rows[2], rows[4]])
    assert n == 2 and sorted(calls) == [(1000, 2199), (9000000, None)]
    # each piece landed in the cache under its own name, byte-exact
    assert models.get_message(hrrr, run, 2, rows[1]) == blob[1000:1600]
    assert models.get_message(hrrr, run, 2, rows[2]) == blob[1600:2200]
    assert models.get_message(hrrr, run, 2, rows[4]) == blob[9000000:]
    assert len(calls) == 2                                      # (served from disk, no new requests)
    assert models.fetch_entries(hrrr, run, 2, [rows[1], rows[2]]) == 0


def test_background_downloads_wait_for_the_foreground(monkeypatch):
    import threading
    order = []
    monkeypatch.setattr(models, "_http_get", lambda url, headers=None, timeout=60: order.append(url) or b"x")
    gate = threading.Event()

    def slow_fg(url, headers=None, timeout=60):
        order.append("fg-start")
        gate.wait(2)
        order.append("fg-end")
        return b"x"
    monkeypatch.setattr(models, "_http_get", slow_fg)
    t_fg = threading.Thread(target=lambda: models._fetch("u1"))
    t_fg.start()
    while "fg-start" not in order:
        time.sleep(0.01)

    def bg():
        with models.background():
            models._fetch("u2")
    t_bg = threading.Thread(target=bg)
    t_bg.start()
    time.sleep(0.25)
    assert order == ["fg-start"]                                # the background request is still waiting
    gate.set()
    t_fg.join(3)
    t_bg.join(3)
    assert order == ["fg-start", "fg-end", "fg-start", "fg-end"]
