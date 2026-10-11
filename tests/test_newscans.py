"""New scans in live mode: Level III slices, complete SAILS passes and sweeps that fill in (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import numpy as np

from radarforge.products import catalog
from radarforge.products.engine import FULL_SWEEP, SweepImage, Tilt, fill_partial, image_coverage


def test_level3_slices():
    b, g, s = catalog.get("L3B"), catalog.get("L3G"), catalog.get("L3S")
    assert [b.l3_code(i) for i in b.l3_slices()] == ["N0B", "NAB", "N1B", "NBB", "N2B", "N3B"]
    assert [g.l3_code(i) for i in g.l3_slices()] == ["N0G", "NAG", "N1G"]
    assert b.l3_code(b.l3_slice_for(0.9)) == "NAB" and b.l3_code(b.l3_slice_for(1.8)) == "NBB"
    assert g.l3_code(g.l3_slice_for(2.4)) == "N1G"                 # velocity tops out at N1
    assert "NAS" not in [s.l3_code(i) for i in s.l3_slices()]
    assert b.l3_candidates(b.l3_slice_for(0.9)) == ["NAB", "NAQ", "NAR"]


def _sweep(az, complete=None):
    sw = SimpleNamespace(azimuths=np.asarray(az, float), az_res=0.5, moments={"REF": 1})
    if complete is not None:
        sw.complete = complete
    return sw


def test_sails_keeps_the_complete_pass():
    full = _sweep(np.arange(0, 360, 0.5), complete=True)
    half = _sweep(np.arange(0, 180, 0.5), complete=False)
    t = Tilt(0.5, [SimpleNamespace(sweeps={"REF": full}), SimpleNamespace(sweeps={"REF": half})])
    assert t.sweep("REF") is full                                   # not the half-done second pass
    t2 = Tilt(0.5, [SimpleNamespace(sweeps={"REF": half})])
    assert t2.sweep("REF") is half                                  # nothing complete yet: what there is
    old = _sweep(np.arange(0, 360, 0.5))                            # an archive sweep: judged by its coverage
    assert Tilt(0.5, [SimpleNamespace(sweeps={"REF": old})]).sweep("REF") is old


def _img(az, val, ng=4):
    az = np.asarray(az, np.float32)
    return SweepImage(("k", val), "REF", np.full((len(az), ng), val, np.float16), az, az - 0.25, az + 0.25,
                      2.0, 0.25, 0.5, False, None)


def test_partial_sweep_fills_in_over_the_volume_before():
    old = _img(np.arange(0.25, 360, 0.5), 10.0)
    new = _img(np.arange(90.25, 180, 0.5), 50.0, ng=6)              # a quarter of the circle so far
    assert image_coverage(new) < FULL_SWEEP and image_coverage(old) >= FULL_SWEEP
    f = fill_partial(new, old)
    assert f.extra["filled"] and len(f.az) == 720 and np.all(np.diff(f.az) > 0)
    inside = (f.az > 90) & (f.az < 180)
    assert np.all(f.values[inside, 0] == 50.0) and np.all(f.values[~inside, 0] == 10.0)
    assert f.values.shape[1] == 6 and np.isnan(f.values[~inside, 5]).all()     # padded to the longer one
    other = _img(np.arange(0.25, 360, 0.5), 10.0)
    other.gate_spacing = 1.0                                         # different gates: left as it is
    assert fill_partial(new, other) is new
