"""Live chunk tracker against a fake chunks bucket: gaps, failed downloads, stalls, volume changes."""
from datetime import datetime, timezone

import pytest

from radarforge.data import aws, live


class FakeBucket:
    def __init__(self):
        self.vols = {}            # number -> (stamp, {chunk number: kind})
        self.fail = set()         # keys whose download fails once

    def add(self, vol, stamp, chunks):
        self.vols.setdefault(vol, [stamp, {}])
        self.vols[vol][0] = stamp
        self.vols[vol][1].update(chunks)

    def key(self, vol, stamp, n, kind):
        return f"KTLX/{vol}/{stamp}-{n:03d}-{kind}"

    def list_chunks(self, site, vol):
        if vol not in self.vols:
            return []
        stamp, ch = self.vols[vol]
        return [aws.Chunk(self.key(vol, stamp, n, k), vol, stamp, n, k) for n, k in sorted(ch.items())]

    def fetch_many(self, bucket, keys, cache=False, timeout=30, cancelled=None):
        out = []
        for k in keys:
            if k in self.fail:
                self.fail.discard(k)
                out.append((k, RuntimeError("timeout")))
            else:
                out.append((k, k.encode()))
        return out


class FakeBuilder:
    def __init__(self):
        self.added = []

    def add_bytes(self, data):
        self.added.append(data.decode().rsplit("/", 1)[-1])

    def build(self, site, source=""):
        class V:
            pass
        v = V()
        v.sweeps = list(self.added)
        return v


@pytest.fixture
def env(monkeypatch):
    b = FakeBucket()
    clock = {"t": 1000.0}
    monkeypatch.setattr(aws, "list_chunks", b.list_chunks)
    monkeypatch.setattr(aws, "fetch_many", b.fetch_many)
    monkeypatch.setattr(aws, "find_latest_volume", lambda site: max(b.vols) if b.vols else None)
    monkeypatch.setattr(live, "SweepBuilder", FakeBuilder)
    monkeypatch.setattr(live.time, "time", lambda: clock["t"])
    return b, clock


def test_normal_volume_and_next(env):
    b, clock = env
    b.add(10, "20261004-150000", {1: "S", 2: "I", 3: "I"})
    tr = live.ChunkTracker("KTLX")
    ev = tr.poll()
    assert len(ev) == 1 and ev[0]["volume"].sweeps == ["20261004-150000-001-S", "20261004-150000-002-I",
                                                        "20261004-150000-003-I"]
    assert ev[0]["time"] == datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc) and not ev[0]["complete"]
    b.add(10, "20261004-150000", {4: "I", 5: "E"})
    ev = tr.poll()
    assert ev[0]["complete"] and ev[0]["missing"] == 0 and tr.volume == 11
    assert tr.poll() == []                                        # next volume not started yet
    b.add(11, "20261004-150500", {1: "S"})
    assert tr.poll()[0]["time"].minute == 5


def test_missing_chunk_is_skipped_after_a_wait(env):
    b, clock = env
    b.add(10, "20261004-150000", {1: "S", 2: "I", 4: "I", 5: "E"})
    tr = live.ChunkTracker("KTLX")
    ev = tr.poll()
    assert ev[0]["volume"].sweeps[-1].endswith("002-I") and not ev[0]["complete"]
    clock["t"] += 10
    assert tr.poll() == []                                         # still waiting for chunk 3
    clock["t"] += live.GAP_WAIT
    ev = tr.poll()
    assert ev[0]["complete"] and ev[0]["missing"] == 1 and ev[0]["volume"].sweeps[-1].endswith("005-E")
    assert tr.volume == 11


def test_failed_download_retried_in_order(env):
    b, clock = env
    b.add(10, "20261004-150000", {1: "S", 2: "I", 3: "I"})
    b.fail.add(b.key(10, "20261004-150000", 2, "I"))
    tr = live.ChunkTracker("KTLX")
    assert tr.poll()[0]["volume"].sweeps == ["20261004-150000-001-S"]
    assert tr.poll()[0]["volume"].sweeps[1:] == ["20261004-150000-002-I", "20261004-150000-003-I"]


def test_stalled_volume_is_abandoned_for_a_newer_one(env):
    b, clock = env
    b.add(10, "20261004-150000", {1: "S", 2: "I"})
    tr = live.ChunkTracker("KTLX")
    tr.poll()
    b.add(11, "20261004-150600", {1: "S", 2: "I"})                 # the radar moved on; volume 10 never ended
    clock["t"] += 30
    assert tr.poll() == []
    clock["t"] += live.STALL_S
    ev = tr.poll()
    assert ev[0]["abandoned"] and ev[0]["complete"] and ev[0]["time"].minute == 0
    assert ev[1]["time"].minute == 6 and tr.volume == 11


def test_leftovers_and_cancel(env):
    b, clock = env
    b.add(10, "20261004-150000", {1: "S", 2: "E"})
    b.add(11, "20250101-000000", {1: "S", 2: "E"})                 # 999 volumes ago
    tr = live.ChunkTracker("KTLX")
    tr.volume = None
    b.vols.pop(11)
    tr.poll()
    b.add(11, "20250101-000000", {1: "S", 2: "E"})
    assert tr.volume == 11 and tr.poll() == []                      # old leftovers ignored
    tr.cancelled = True
    b.add(11, "20261004-150500", {1: "S"})
    assert tr.poll() == []


def test_previous_volume(env):
    b, clock = env
    b.add(9, "20261004-145500", {1: "S", 2: "I", 4: "E"})
    b.add(10, "20261004-150000", {1: "S"})
    tr = live.ChunkTracker("KTLX")
    tr.poll()
    ev = tr.load_previous(None)
    assert ev["time"].minute == 55 and ev["missing"] == 1 and len(ev["volume"].sweeps) == 3
    assert tr.load_previous(datetime(2026, 10, 4, 14, 58, tzinfo=timezone.utc)) is None   # archive has it


def test_prune_cache(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setattr(aws, "CACHE_DIR", tmp_path)
    d = tmp_path / "s3" / "b" / "2026"
    d.mkdir(parents=True)
    for i, age in enumerate((20, 5, 1, 0)):
        p = d / f"f{i}"
        p.write_bytes(b"x" * 100)
        t = time.time() - age * 86400
        os.utime(p, (t, t))
    assert aws.prune_cache(max_bytes=150, max_age_days=10) == 3
    assert [p.name for p in d.iterdir()] == ["f3"]


def test_new_frames_keep_level3_until_their_own_arrives(qtbot=None):
    """A new live volume starts without its N0G: it shows the previous volume's until its own comes in."""
    from datetime import timedelta
    from types import SimpleNamespace
    from radarforge.data.frames import Frame
    from radarforge.ui.datamanager import DataManager
    dm = DataManager.__new__(DataManager)
    import threading
    dm._lock = threading.RLock()
    dm.frames = []
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    f1 = Frame("KTLX", t0, l2_path="a")
    dm._insert_frame(f1)
    old = SimpleNamespace(awips="N0G", vol_time=t0, time=t0 + timedelta(seconds=40))
    f1.l3["N0G"] = old
    f2 = Frame("KTLX", t0 + timedelta(minutes=5), l2_path="b")
    dm._insert_frame(f2)
    assert f2.l3["N0G"] is old                      # carried over
    f3 = Frame("KTLX", t0 + timedelta(minutes=20), l2_path="c")
    dm._insert_frame(f3)
    assert "N0G" not in f3.l3                       # too old to carry (12 minutes)
