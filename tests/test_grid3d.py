"""3-D gridding: beams interpolated in height, the cone of silence left empty, masking and product rules."""
from types import SimpleNamespace

import numpy as np

from radarforge.products.geometry import beam_height, slant_range
from radarforge.tools import grid3d


def _tilt(el, field, ng=800, gs=0.25):
    """A synthetic sweep: 360 one-degree radials, values = field(x, y, z) at each gate's beam centre."""
    az = np.arange(360) + 0.5
    r = 0.25 + np.arange(ng) * gs
    A, R = np.meshgrid(np.radians(az), r, indexing="ij")
    s = R * np.cos(np.radians(el))
    x, y = s * np.sin(A), s * np.cos(A)
    z = beam_height(R, el)
    v = field(x, y, z).astype(np.float32)
    return el, SimpleNamespace(values=v, az=az, az_lo=az - 0.5, az_hi=az + 0.5, first_gate=0.25,
                               gate_spacing=gs, ground_range=False)


def _storm(x, y, z):
    """60 dBZ at (-40, 0) km, 5 km up, falling off with distance; no echo (NaN) below 0 dBZ."""
    d = np.sqrt((x + 40) ** 2 + y ** 2 + ((z - 5) * 2) ** 2)
    v = 60 - 4 * d
    return np.where(v > 0, v, np.nan)


ELS = (0.5, 0.9, 1.3, 1.8, 2.4, 3.1, 4.0, 5.1, 6.4, 8.0, 10.0, 12.5, 15.6, 19.5)


def test_grid_follows_the_storm_between_beams():
    tilts = [_tilt(e, _storm) for e in ELS]
    g = grid3d.build(tilts, None, (-50, -10, -30, 10), 16.0, "REF")
    v = g.values.astype(np.float32)
    nz, ny, nx = v.shape
    zs = np.linspace(0, 16, nz)
    k = int(np.argmin(abs(zs - 5)))
    centre = v[k, ny // 2, nx // 2]
    assert 52 < centre <= 61                        # the core, between beams, smoothed a little
    assert v[k, 0, 0] < 20                          # its edge
    top = v[int(np.argmin(abs(zs - 14))), ny // 2, nx // 2]
    assert top < 10                                 # gone aloft
    assert g.base.shape == (ny, nx) and np.isfinite(g.base).any()
    assert g.values.dtype == np.float16


def test_cone_of_silence_stays_empty():
    """Above the highest beam (plus half a beamwidth) there is no data, however strong the echo below."""
    everywhere = lambda x, y, z: np.full(np.shape(x), 50.0)        # noqa: E731
    tilts = [_tilt(e, everywhere, ng=200) for e in ELS]
    g = grid3d.build(tilts, None, (5, -2, 9, 2), 16.0, "REF")         # 5-9 km from the radar
    v = g.values.astype(np.float32)
    nz, ny, nx = v.shape
    col = v[:, ny // 2, nx // 2]                    # the column 7 km east of the radar
    zs = np.linspace(0, 16, nz)
    top_beam = float(beam_height(slant_range(7.0, 19.5), 19.5))
    assert col[zs < top_beam - 0.5].min() > 45
    assert col[zs > top_beam + 1.0].max() < -20


def test_dual_pol_is_masked_by_reflectivity():
    cc = lambda x, y, z: np.full(np.shape(x), 0.7)                 # noqa: E731
    weak = lambda x, y, z: np.full(np.shape(x), 10.0)              # noqa: E731
    g = grid3d.build([_tilt(e, cc, ng=300) for e in ELS[:6]], [_tilt(e, weak, ng=300) for e in ELS[:6]],
                     (10, -5, 20, 5), 8.0, "CC")
    assert np.allclose(g.values.astype(np.float32), 1.0, atol=0.01)   # weak echo: CC hidden (floor 1.0)
    assert g.context is not None


def test_rules():
    r = grid3d.RULES["REF"]
    assert r.interest(r.threshold) == 0 and r.interest(r.full) == 1
    cc = grid3d.RULES["CC"]
    assert cc.interest(0.6) > 0 > cc.interest(0.98) and cc.inside_side(0.8) == -1
    v = grid3d.RULES["SRV"]
    assert v.interest(-40) == v.interest(40) and v.inside_side(-20) == -1 and v.inside_side(20) == 1


def test_plan_limits_the_grid():
    xs, ys, zs = grid3d.plan((-150, -150, 150, 150), 18, "normal")
    assert len(xs) <= 200 and len(ys) <= 200
    xs, ys, zs = grid3d.plan((0, 0, 20, 20), 18, "normal")
    assert abs((xs[1] - xs[0]) - 0.25) < 1e-9
