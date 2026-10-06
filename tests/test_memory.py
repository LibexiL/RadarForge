"""Memory: compressed gate codes, live sweeps that drop their radials, honest image-cache accounting."""
import json
import pickle
import sys

import numpy as np
import pytest

from radarforge.data import level2
from radarforge.data.level2 import MomentField, Radial, RadialMoment, SweepBuilder


def _field(shape=(360, 1000), seed=0, dtype=np.uint8):
    rng = np.random.default_rng(seed)
    a = np.zeros(shape, dtype)
    a[:, 100:400] = rng.integers(2, 200, size=(shape[0], 300))      # an echo; the rest "below threshold"
    return a


def test_moment_field_packs_and_unpacks():
    a = _field()
    mf = MomentField("REF", a, 2.0, 66.0, 2.125, 0.25)
    assert mf.packed_bytes < a.nbytes / 3                        # mostly empty radar data packs well
    r = mf.raw
    assert r.dtype == np.uint8 and r.shape == a.shape and np.array_equal(r, a)
    assert not r.flags.writeable                                 # shared: nobody may change it in place
    assert mf.raw is r                                           # stays unpacked while in use
    assert mf.ngates == 1000 and mf.max_range == pytest.approx(2.125 + 0.25 * 999.5)
    v = mf.values()
    assert np.isnan(v[0, 0]) and v[0, 150] == pytest.approx((a[0, 150] - 66.0) / 2.0)
    u16 = MomentField("PHI", _field(dtype=np.uint16), 2.8, 2.0, 2.125, 0.25)
    assert u16.raw.dtype == np.uint16
    empty = MomentField("CFP", np.zeros((0, 5), np.uint8), 1, 0, 0, 1)
    assert empty.raw.shape == (0, 5)


def test_moment_field_pickles_only_the_packed_codes():
    mf = MomentField("REF", _field(), 2.0, 66.0, 2.125, 0.25)
    mf.raw                                                       # unpacked here...
    blob = pickle.dumps(mf)
    assert len(blob) < mf.packed_bytes + 1000                    # ...but only the packed codes travel
    back = pickle.loads(blob)
    assert back.name == "REF" and back.scale == 2.0 and np.array_equal(back.raw, mf.raw)


def test_unpacked_codes_stay_within_budget(monkeypatch):
    monkeypatch.setattr(level2, "UNPACKED_BUDGET", 2_000_000)
    fields = [MomentField("REF", _field(seed=i), 2, 66, 2, 0.25) for i in range(12)]   # 360 kB each unpacked
    for f in fields:
        f.raw
    assert level2.unpacked_bytes() <= 2_000_000 + 360_000
    assert fields[0]._raw is None and fields[-1]._raw is not None        # the oldest were packed away again
    assert np.array_equal(fields[0].raw, _field(seed=0))                  # and unpack again on use


def _radials(elev_num, n=360, status_last=2, t0=0):
    out = []
    for i in range(n):
        status = 0 if i == 0 else (status_last if i == n - 1 else 1)
        raw = np.full(500, (i + elev_num) % 200 + 2, np.uint8)
        out.append(Radial(azimuth=i + 0.5, elevation=0.5 * elev_num, elev_num=elev_num, status=status,
                          time_ms=t0 + i * 10, mjd=20000, az_res=1.0, nyquist=26.0, unamb_range=460.0,
                          moments={"REF": RadialMoment(2.125, 0.25, 2.0, 66.0, raw)}))
    return out


def test_live_builder_drops_radials_of_closed_sweeps():
    rads = _radials(1) + _radials(2) + _radials(3, status_last=4)
    live = SweepBuilder()
    vols = []
    for i in range(0, len(rads), 90):                 # chunk by chunk, as the live feed does
        for r in rads[i:i + 90]:
            live._add_radial(r)
        vols.append(live.build("KTLX"))
    whole = SweepBuilder()
    for r in rads:
        whole._add_radial(r)
    ref = whole.build("KTLX")
    got = vols[-1]
    assert len(got.sweeps) == len(ref.sweeps) == 3
    for a, b in zip(got.sweeps, ref.sweeps):
        assert a.index == b.index and a.elev_num == b.elev_num and a.complete == b.complete
        assert np.array_equal(a.moments["REF"].raw, b.moments["REF"].raw)
    assert live._groups[0][1] is None and live._groups[1][1] is None     # closed: radials let go
    assert live._groups[2][1] is not None                                # the open sweep keeps them
    assert got.complete and vols[5].sweeps[0] is got.sweeps[0]           # closed sweeps are shared
    mid = vols[5]                                                        # partway through sweep 2
    assert mid.sweeps[0].complete and not mid.sweeps[1].complete


def test_engine_counts_everything_an_image_holds(tmp_path):
    from radarforge.config import Settings
    from radarforge.products.engine import ProductEngine, SweepImage
    s = Settings(path=tmp_path / "s.json")
    s["image_cache_mb"] = 1
    s["loop_frames"] = 1
    s["layout"] = 1
    e = ProductEngine(s)
    az = np.arange(360, dtype=np.float32)

    def img(key, extra=None):
        return SweepImage(key, "REF", np.zeros((360, 500), np.float16), az, az, az, 2.0, 0.25, 0.5, False, None,
                          extra=extra or {})
    a = img((1, 0, "REF", 0, ()), {"aux": np.zeros(100_000, np.float32)})
    assert a.nbytes == 360 * 500 * 2 + 360 * 4 * 3 + 400_000
    a.gpu_values()
    assert a.nbytes == 360 * 500 * 2 + 360 * 4 * 3 + 400_000             # the short-lived texture copy isn't
    e._put(a.key, a)
    assert e._bytes == a.nbytes
    a.extra.pop("_gpu")                                                   # sizes counted in = counted out
    for i in range(2, 14):
        e._put((i, 0, "REF", 0, ()), img((i, 0, "REF", 0, ())))
    assert e._bytes == sum(e._sizes.values()) <= 4_200_000 + 400_000
    assert e._bytes <= max(1e6, 3.2e6) + 400_000 or len(e._cache) <= 8


def test_engine_forgets_departed_frames_and_old_revisions(tmp_path):
    from radarforge.config import Settings
    from radarforge.products.engine import ProductEngine, SweepImage

    class F:
        def __init__(self, uid, rev):
            self.uid, self.l2_rev = uid, rev
    e = ProductEngine(Settings(path=tmp_path / "s.json"))
    az = np.arange(4, dtype=np.float32)
    for key in [(1, 0, "REF", 0, ()), (1, 1, "REF", 0, ()), (2, 0, "VEL", 0, ()), ("L3", 7, "NST", False)]:
        e._put(key, SweepImage(key, "REF", np.zeros((4, 4), np.float16), az, az, az, 2, 0.25, 0.5, False, None))
    e._tilt_cache[(1, 0)] = ["old"]
    e._tilt_cache[(1, 1)] = ["new"]
    e._tilt_cache[(2, 0)] = ["gone"]
    e.forget_frames([F(1, 1)])                          # frame 2 left the loop; frame 1 is at revision 1 now
    assert set(e._cache) == {(1, 1, "REF", 0, ()), ("L3", 7, "NST", False)}
    assert set(e._tilt_cache) == {(1, 1)}
    assert e._bytes == sum(e._sizes.values())


def test_settings_migrate_the_old_image_cache_default(tmp_path):
    from radarforge.config import Settings
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"settings_version": 2, "image_cache_mb": 600}))
    assert Settings(path=p)["image_cache_mb"] == 300
    p.write_text(json.dumps({"settings_version": 2, "image_cache_mb": 1500}))       # chosen by the user: kept
    assert Settings(path=p)["image_cache_mb"] == 1500
    assert Settings(path=tmp_path / "new.json")["image_cache_mb"] == 300


@pytest.mark.skipif(not sys.platform.startswith(("linux", "win")), reason="Linux and Windows readout")
def test_memory_readout():
    from radarforge import memory
    assert memory.rss_bytes() > 10_000_000
    assert "MB" in memory.describe()


def test_freed_fields_stop_counting():
    import gc
    from radarforge.data import level2
    before = level2.unpacked_bytes()
    fields = [MomentField("REF", _field(seed=i), 2, 66, 2, 0.25) for i in range(3)]
    for f in fields:
        f.raw
    assert level2.unpacked_bytes() > before
    del f, fields
    gc.collect()
    assert level2.unpacked_bytes() == before
