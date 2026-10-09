"""Loading a radar: the 10-frame default, one decode per volume, decode priorities, downloads that stop when
another radar is chosen, and work for an earlier radar never touching the new one's state (1.14)."""
import json
import os
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from radarforge.data import aws, frames, level2

KDDC = Path(os.environ.get("RF_TESTDATA", "/home/claude/testdata")) / "Level2_KDDC_20200823_204121.ar2v"


# ---------------------------------------------------------------- settings
def test_default_loop_is_ten_and_the_old_default_migrates(tmp_path):
    from radarforge.config import DEFAULTS, Settings
    assert DEFAULTS["loop_frames"] == 10
    assert Settings(path=tmp_path / "new.json")["loop_frames"] == 10
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"settings_version": 3, "loop_frames": 12}))
    s = Settings(path=p)
    assert s["loop_frames"] == 10 and s["settings_version"] == 4
    p.write_text(json.dumps({"settings_version": 3, "loop_frames": 24}))         # picked by the user: kept
    assert Settings(path=p)["loop_frames"] == 24
    p.write_text(json.dumps({"settings_version": 4, "loop_frames": 12}))         # chosen after 1.14: kept
    assert Settings(path=p)["loop_frames"] == 12


def test_frame_choices_go_past_ten():
    from radarforge.ui.main_menus import LOOP_FRAMES
    assert 10 in LOOP_FRAMES and max(LOOP_FRAMES) >= 48 and list(LOOP_FRAMES) == sorted(LOOP_FRAMES)


# ---------------------------------------------------------------- volume cache
def test_volume_cache_decodes_each_file_once(monkeypatch):
    calls = []
    started = threading.Event()

    def slow_read(path, site=""):
        calls.append(path)
        started.set()
        time.sleep(0.3)
        return level2.Level2Volume(site="KTLX", start_time=datetime.now(timezone.utc))
    monkeypatch.setattr(frames, "read_level2_isolated", slow_read)
    cache = frames.VolumeCache(4)
    out = []
    ts = [threading.Thread(target=lambda: out.append(cache.get("a"))) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert calls == ["a"] and len(out) == 4 and all(v is out[0] for v in out)


def test_volume_cache_shares_a_failed_decode(monkeypatch):
    calls = []

    def bad(path, site=""):
        calls.append(path)
        time.sleep(0.2)
        raise ValueError("not a radar file")
    monkeypatch.setattr(frames, "read_level2_isolated", bad)
    cache = frames.VolumeCache(4)
    errs = []

    def get():
        try:
            cache.get("x")
        except ValueError as exc:
            errs.append(exc)
    ts = [threading.Thread(target=get) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert calls == ["x"] and len(errs) == 3


# ---------------------------------------------------------------- decode slots
def test_gate_serves_the_screen_first_and_keeps_a_slot_free():
    g = level2._Gate()
    order = []
    assert g.acquire(2, 0)                       # one slot busy
    # read-ahead may not take the last slot ...
    bg = threading.Thread(target=lambda: (g.acquire(2, 12), order.append("bg")))
    bg.start()
    time.sleep(0.2)
    assert order == []
    # ... which something on screen gets straight away
    assert g.acquire(2, 0)
    order.append("screen")
    g.release()
    g.release()                                  # both free: the read-ahead goes
    bg.join(2)
    assert order == ["screen", "bg"]
    g.release()


def test_gate_orders_waiters_by_priority_and_drops_cancelled_ones():
    g = level2._Gate()
    assert g.acquire(1, 0)
    order = []
    stop = threading.Event()

    def wait(p, name, cancelled=None):
        if g.acquire(1, p, cancelled):
            order.append(name)
            g.release()
        else:
            order.append("cancelled " + name)
    ts = [threading.Thread(target=wait, args=(20, "old")), threading.Thread(target=wait, args=(11, "new")),
          threading.Thread(target=wait, args=(15, "gone", stop.is_set))]
    for t in ts:
        t.start()
        time.sleep(0.05)
    stop.set()
    time.sleep(0.4)
    g.release()
    for t in ts:
        t.join(3)
    assert order == ["cancelled gone", "new", "old"]


def test_cancelled_read_ahead_raises(monkeypatch):
    monkeypatch.setattr(level2, "_gate", level2._Gate())
    level2._gate.acquire(level2.decoder_count(), 0)
    level2._gate._busy = 99                       # every process busy
    with pytest.raises(level2.DecodeCancelled):
        with level2.decode_priority(10, cancelled=lambda: True):
            level2.run_isolated(len, b"x")


def test_a_held_slot_is_not_waited_for_twice(monkeypatch):
    monkeypatch.setattr(level2, "_gate", level2._Gate())
    monkeypatch.setattr(level2, "decoder_count", lambda: 2)
    monkeypatch.setattr(level2, "_proc_failed", True)          # (decode in this thread)
    with level2.decode_slot(10):                               # read-ahead holds a slot ...
        assert level2.run_isolated(lambda: level2._gate._busy) == 1      # ... and its decode doesn't take another
    assert level2._gate._busy == 0


# ---------------------------------------------------------------- downloads
class _Resp:
    def __init__(self, data, delay=0.0):
        self.data, self.delay, self.status_code = data, delay, 200

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.data), 4):
            time.sleep(self.delay)
            yield self.data[i:i + 4]

    def close(self):
        pass


def test_fetch_file_streams_to_the_cache_and_reuses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(aws, "CACHE_DIR", tmp_path)
    gets = []

    class S:
        def get(self, url, **kw):
            gets.append((url, kw.get("stream")))
            return _Resp(b"0123456789")
    monkeypatch.setattr(aws, "session", lambda: S())
    p = aws.fetch_file("bucket", "2026/10/09/KTLX/KTLX20261009_120000_V06")
    assert p.read_bytes() == b"0123456789" and gets[0][1] is True
    assert aws.fetch_file("bucket", "2026/10/09/KTLX/KTLX20261009_120000_V06") == p and len(gets) == 1
    assert not list(p.parent.glob("*.part"))


def test_fetch_file_stops_when_no_longer_wanted(tmp_path, monkeypatch):
    monkeypatch.setattr(aws, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(aws, "session", lambda: type("S", (), {"get": lambda self, u, **k: _Resp(b"x" * 400, 0.01)})())
    t0 = time.time()
    with pytest.raises(aws.Cancelled):
        aws.fetch_file("bucket", "k", cancelled=lambda: time.time() - t0 > 0.1)
    assert not any(tmp_path.rglob("k*"))         # no partial file left behind


# ---------------------------------------------------------------- data manager
@pytest.fixture
def dm(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(aws, "prune_cache", lambda *a, **k: 0)
    from radarforge.config import Settings
    from radarforge.ui.datamanager import DataManager
    s = Settings(path=tmp_path / "s.json")
    s["site"] = "KTLX"
    d = DataManager(s)
    yield d
    d.stop_live()
    for pool in (d._pool, d._dl_pool, d._l3_pool, d._decode_pool, d._live_pool):
        pool.shutdown(wait=False, cancel_futures=True)


def _until(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_insert_frame_makes_a_new_list(dm):
    from radarforge.data.frames import Frame
    t = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    dm._insert_frame(Frame("KTLX", t, l2_path="a"))
    before = dm.frames
    dm._insert_frame(Frame("KTLX", t - timedelta(minutes=5), l2_path="b"))
    assert len(before) == 1 and [f.l2_path for f in dm.frames] == ["b", "a"]


def test_level3_poll_for_a_new_radar_is_not_held_up_by_the_old_one(dm, monkeypatch):
    t = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    release = threading.Event()
    listed = []

    def latest_level3(site3, code, count=1):
        listed.append(site3)
        if site3 == "TLX":
            release.wait(5)                       # the old radar's listing hangs
        return [aws.L3File(f"{site3}_{code}_2026_10_09_12_00_00", site3, code, t)]
    loaded = []
    monkeypatch.setattr(aws, "latest_level3", latest_level3)
    monkeypatch.setattr(dm, "_load_l3_keys", lambda gen, keys, code, site: loaded.append((gen, keys)))
    dm.l3_needed = {"N0G"}
    dm._poll_l3(force=True)
    assert _until(lambda: "TLX" in listed)
    dm.site_id = "KDDC"
    dm._reset()
    dm._poll_l3(force=True)
    assert _until(lambda: "DDC" in listed, 2)     # didn't wait for the old radar's poll
    release.set()
    assert _until(lambda: dm._l3_gen == -1)
    time.sleep(0.2)
    # the old poll's answer never landed in the new radar's state
    assert dm._l3_last == {"N0G": "DDC_N0G_2026_10_09_12_00_00"}
    assert [k for _g, k in loaded] == [["DDC_N0G_2026_10_09_12_00_00"]]


def test_downloads_for_a_radar_left_behind_stop(dm, monkeypatch):
    t = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    files = [aws.L2File(f"2026/10/09/KTLX/KTLX20261009_{12 - i:02d}0000_V06", t - timedelta(minutes=5 * i), 1)
             for i in range(8)]
    done = []

    def fetch_file(bucket, key, timeout=60, cancelled=None):
        for _ in range(20):
            if cancelled is not None and cancelled():
                raise aws.Cancelled(key)
            time.sleep(0.02)
        done.append(key)
        return Path("/nonexistent") / key
    monkeypatch.setattr(aws, "fetch_file", fetch_file)
    monkeypatch.setattr(dm, "_decode_ahead", lambda gen, f: None)
    dm.mode = "live"
    gen = dm._gen
    th = threading.Thread(target=dm._load_l2_files, args=(gen, files))
    th.start()
    assert _until(lambda: len(done) >= 1)          # the newest file is in, the rest are under way
    dm._reset()                                    # another radar was chosen
    th.join(5)
    time.sleep(0.5)
    assert len(done) < len(files) and dm.frames == [] and not dm.loading


# ---------------------------------------------------------------- images
def test_engine_remembers_products_a_frame_does_not_have(tmp_path):
    from radarforge.config import Settings
    from radarforge.data.frames import Frame
    from radarforge.products.engine import ProductEngine
    e = ProductEngine(Settings(path=tmp_path / "s.json"))
    f = Frame("KTLX", datetime(2026, 10, 9, 12, tzinfo=timezone.utc))      # no volume, no Level III
    assert not e.known(f, "REF", 0)
    assert e.image(f, "REF", 0) is None
    assert e.known(f, "REF", 0) and e.cached(f, "REF", 0) is None        # a loop doesn't wait on it
    f.l2_path = "volume"
    f.l2_rev += 1                                  # its volume arrived: no longer known
    assert not e.known(f, "REF", 0)
    assert e.image(f, "L3G", 0) is None and e.known(f, "L3G", 0)
    f.revision += 1                                # (a Level III product came in)
    assert not e.known(f, "L3G", 0)
    e.forget_frames([])
    assert not e._missing


# ---------------------------------------------------------------- live chunks off the UI process
def _chunks():
    raw = KDDC.read_bytes()
    hdr, pos, recs = raw[:24], 24, []
    while pos + 4 <= len(raw):
        n = abs(struct.unpack_from(">i", raw, pos)[0])
        recs.append(raw[pos:pos + 4 + n])
        pos += 4 + n
    return [hdr + recs[0]] + [b"".join(recs[i:i + 5]) for i in range(1, len(recs), 5)]


@pytest.mark.skipif(not KDDC.exists(), reason="needs the KDDC test volume")
def test_add_chunks_matches_adding_them_one_by_one():
    chunks = _chunks()[:6]
    ref = level2.SweepBuilder()
    for c in chunks:
        ref.add_bytes(c)
    want = ref.build("KDDC", "x")
    b, oks, vol = level2.run_isolated(level2.add_chunks, level2.SweepBuilder(), chunks[:4], "KDDC", "x")
    b, oks2, vol = level2.run_isolated(level2.add_chunks, b, chunks[4:], "KDDC", "x")
    assert oks == [True] * 4 and oks2 == [True, True]
    assert [(s.elevation, len(s.azimuths)) for s in vol.sweeps] == \
        [(s.elevation, len(s.azimuths)) for s in want.sweeps]


def test_level3_waits_for_its_own_volume_while_the_loop_downloads(dm):
    from types import SimpleNamespace
    from radarforge.data.frames import Frame
    t0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    old = Frame("KTLX", t0, l2_path="a")
    dm._insert_frame(old)
    dm.mode = "live"
    dm._loading = 1                                     # the 12:05 volume is still downloading
    prod = SimpleNamespace(awips="N0G", vol_time=t0 + timedelta(minutes=5), time=t0 + timedelta(minutes=6),
                           uid=1)
    dm.attach_l3(prod, dm._gen)
    assert "N0G" not in old.l3                          # not shown on the 12:00 volume
    own = [f for f in dm.frames if f is not old]
    assert len(own) == 1 and own[0].l3["N0G"] is prod and not own[0].has_level2()
    dm._loading = 0
    # when the download is done, the volume joins that frame
    dm._find_frame(t0 + timedelta(minutes=5), 60).l2_path = "b"
    assert len(dm.frames) == 2


def test_level3_beyond_the_live_loop_makes_no_frames(dm):
    from types import SimpleNamespace
    t0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    dm.mode = "live"
    dm.settings["loop_frames"] = 3
    got = []
    for i in range(5):                                  # newest first, before any volume is in
        t = t0 - timedelta(minutes=5 * i)
        got.append(dm.attach_l3(SimpleNamespace(awips="N0G", vol_time=t, time=t, uid=i), dm._gen))
    assert got == [True, True, True, False, False] and len(dm.frames) == 3


def test_archive_level3_waits_for_all_its_volumes(dm):
    from types import SimpleNamespace
    from radarforge.data.frames import Frame
    t0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    dm.mode = "archive"
    dm._l2_expected = True
    dm._insert_frame(Frame("KTLX", t0, l2_path="a"))         # the first volume is in, the rest aren't
    prod = SimpleNamespace(awips="N0G", vol_time=t0 + timedelta(minutes=5), time=t0 + timedelta(minutes=5),
                           uid=1)
    dm.attach_l3(prod, dm._gen)
    assert dm._pending_l3 == [prod] and all("N0G" not in f.l3 for f in dm.frames)
    late = Frame("KTLX", t0 + timedelta(minutes=5), l2_path="b")
    dm._insert_frame(late)
    dm._flush_pending_l3()
    assert late.l3["N0G"] is prod and "N0G" not in dm.frames[0].l3
