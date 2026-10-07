"""Turns one radar volume (every tilt of a product) into a regular 3-D grid for the 3-D view.

Each tilt is sampled at the grid's columns (bilinear in azimuth and range), which gives that tilt's value and
beam height above every column. A grid point between two beams is interpolated linearly in height between them,
the way a radar meteorologist reads a stack of tilts. Below the lowest beam the lowest tilt is carried down to the
ground; above the highest beam (plus half a beamwidth) there is no data, so the cone of silence stays empty.
Nothing here touches Qt or OpenGL.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..products.geometry import az_range, beam_height, slant_range

BEAMWIDTH = 0.95                 # degrees (WSR-88D)
REF_RANGE = (-30.0, 85.0)        # dBZ: how the outline (context) grid is normalised


@dataclass(frozen=True)
class Rule:
    """How a product is shown in 3-D. Values are in the product's storage units (dBZ, m/s, ...)."""
    direction: int               # +1 high values stand out, -1 low values (CC), 0 both signs (velocity)
    threshold: float             # volume style: anything weaker is clear
    full: float                  # ... and from here on it is at full density
    levels: tuple                # surfaces style: default levels, outermost first
    floor: float                 # value used where there is no echo
    mask_dbz: float | None = None    # hide it where reflectivity is weaker (dual-pol is noise in weak echo)
    smooth: float = 0.6          # Gaussian smoothing (grid cells)

    def interest(self, v, threshold=None):
        """0 at the threshold, 1 at 'full', negative below: how strongly a value stands out."""
        thr = self.threshold if threshold is None else threshold
        span = self.full - self.threshold
        v = np.asarray(v, np.float64)
        if self.direction > 0:
            return (v - thr) / span
        if self.direction < 0:
            return (thr - v) / -span
        return (np.abs(v) - abs(thr)) / span

    def inside_side(self, level: float) -> float:
        """+1 if values above [level] are inside its surface, -1 if values below are."""
        if self.direction > 0:
            return 1.0
        if self.direction < 0:
            return -1.0
        return 1.0 if level >= 0 else -1.0


RULES = {
    "REF": Rule(+1, 25.0, 65.0, (30.0, 50.0, 65.0), -30.0),
    "DVEL": Rule(0, 15.0, 40.0, (-25.0, 25.0), 0.0, 5.0, 0.5),
    "SRV": Rule(0, 15.0, 40.0, (-20.0, 20.0), 0.0, 5.0, 0.5),
    "CC": Rule(-1, 0.92, 0.55, (0.80,), 1.0, 20.0),
    "ZDR": Rule(+1, 2.0, 6.0, (3.0,), 0.0, 20.0),
    "KDP": Rule(+1, 1.0, 5.0, (2.5,), 0.0, 25.0),
    "SW": Rule(+1, 6.0, 14.0, (9.0,), 0.0, 10.0),
    "AZSH": Rule(+1, 4.0, 15.0, (6.0, 10.0), 0.0, 20.0, 0.5),
}
PRODUCTS = tuple(RULES)

DETAIL = {   # smallest grid cell (km), most columns across the box, vertical spacing (km)
    "fast": (0.5, 128, 0.5),
    "normal": (0.25, 200, 0.25),
    "high": (0.15, 320, 0.2),
}


@dataclass
class Grid3D:
    values: np.ndarray           # float16 (nz, ny, nx) storage units; rule.floor where there's nothing
    context: np.ndarray | None   # float16 reflectivity (dBZ) on the same grid (the outline), None for REF itself
    base: np.ndarray             # float32 (ny, nx) lowest tilt, NaN where no echo (the floor map)
    box: tuple                   # x0, y0, x1, y1: km east / north of the radar
    top: float                   # km above the radar
    pid: str
    elevations: tuple = ()
    build_s: float = 0.0
    key: tuple = ()
    info: dict = field(default_factory=dict)

    @property
    def shape(self):
        return self.values.shape

    @property
    def nbytes(self) -> int:
        n = self.values.nbytes + self.base.nbytes
        if self.context is not None:
            n += self.context.nbytes
        return n


def plan(box, top_km: float, detail: str = "normal"):
    """Grid axes for a box: (xs, ys, zs) in km (zs above the radar)."""
    x0, y0, x1, y1 = box
    w, h = max(x1 - x0, 0.5), max(y1 - y0, 0.5)
    min_res, cols, dz = DETAIL.get(detail, DETAIL["normal"])
    res = max(min_res, max(w, h) / (cols - 1))
    nx = max(2, int(round(w / res)) + 1)
    ny = max(2, int(round(h / res)) + 1)
    nz = max(2, int(round(top_km / dz)) + 1)
    return (np.linspace(x0, x1, nx), np.linspace(y0, y1, ny), np.linspace(0.0, top_km, nz))


# --------------------------------------------------------------------------- one tilt at the grid's columns
def _azimuths(img, AZ):
    """Bracketing radials and weights for azimuths AZ: (i0, i1, t, covered)."""
    az = np.asarray(img.az, np.float64)
    n = len(az)
    i1 = np.searchsorted(az, AZ) % n
    i0 = (i1 - 1) % n
    a0 = az[i0]
    span = (az[i1] - a0) % 360.0
    span = np.where(span <= 1e-9, 360.0, span)
    t = np.clip(((AZ - a0) % 360.0) / span, 0.0, 1.0)
    spacing = float(np.median(np.diff(az))) if n > 2 else 1.0
    gap = span > 2.5 * max(spacing, 1e-3)
    # across a gap (missing radials, a sector scan): only the nearest radial, and only within its own width
    use1 = t >= 0.5
    near = np.where(use1, i1, i0)
    lo = np.asarray(img.az_lo, np.float64)[near]
    hi = np.asarray(img.az_hi, np.float64)[near]
    half = ((hi - lo) % 360.0) / 2 + 1e-3
    dist = np.abs(((az[near] - AZ + 180.0) % 360.0) - 180.0)
    covered = ~gap | (dist <= half)
    t = np.where(gap, use1.astype(np.float64), t)
    return i0, i1, t, covered


def sample_tilt(img, AZ, S, elevation, floor: float, nearest: bool = False):
    """A tilt's values (storage units) at the columns AZ/S, its beam height there, and where it has data.

    Bilinear in azimuth and range; no echo / range folded count as [floor]. With [nearest], the closest gate
    is used and NaN is kept (for the floor map)."""
    vals = img.values
    nrad, ng = vals.shape
    if nrad == 0 or ng == 0:
        empty = np.full(AZ.shape, floor, np.float32)
        return empty, np.full(AZ.shape, np.inf, np.float32), np.zeros(AZ.shape, bool)
    r = S if getattr(img, "ground_range", False) else slant_range(S, elevation)
    h = beam_height(r, elevation).astype(np.float32)
    i0, i1, t, covered = _azimuths(img, AZ)
    g = (r - img.first_gate) / img.gate_spacing
    covered &= (g >= -0.5) & (g <= ng - 0.5)
    if nearest:
        row = np.where(t >= 0.5, i1, i0)
        gi = np.clip(np.round(g).astype(np.int64), 0, ng - 1)
        v = vals[row, gi].astype(np.float32)
        v[~np.isfinite(v)] = np.nan
        v[~covered] = np.nan
        return v, h, covered
    g = np.clip(g, 0.0, ng - 1.0)
    g0 = np.floor(g).astype(np.int64)
    g1 = np.minimum(g0 + 1, ng - 1)
    fg = (g - g0).astype(np.float32)
    t = t.astype(np.float32)

    def at(i, j):
        x = vals[i, j].astype(np.float32)
        x[~np.isfinite(x)] = floor
        return x
    v = (at(i0, g0) * (1 - fg) + at(i0, g1) * fg) * (1 - t) + (at(i1, g0) * (1 - fg) + at(i1, g1) * fg) * t
    v[~covered] = floor
    return v.astype(np.float32), h, covered


def grid_tilts(tilts, xs, ys, zs, floor: float):
    """[(elevation, SweepImage)] -> float32 (nz, ny, nx) grid and the lowest tilt's (ny, nx) map."""
    tilts = sorted([t for t in tilts if t[1] is not None], key=lambda t: t[0])
    # one image per elevation (keep the first of near-duplicates)
    uniq = []
    for el, img in tilts:
        if not uniq or el - uniq[-1][0] > 0.05:
            uniq.append((el, img))
    X, Y = np.meshgrid(xs, ys)
    AZ, S = az_range(X, Y)
    ny, nx = X.shape
    nz = len(zs)
    if not uniq:
        return np.full((nz, ny, nx), floor, np.float32), np.full((ny, nx), np.nan, np.float32)
    T = len(uniq)
    H = np.empty((T, ny, nx), np.float32)
    V = np.empty((T, ny, nx), np.float32)
    for k, (el, img) in enumerate(uniq):
        V[k], H[k], _cov = sample_tilt(img, AZ, S, el, floor)
    base, _h, _c = sample_tilt(uniq[0][1], AZ, S, uniq[0][0], floor, nearest=True)
    # top of the highest beam: its centre plus half a beamwidth
    el_top = uniq[-1][0]
    r_top = slant_range(S, el_top)
    top_edge = (H[-1] + r_top * math.tan(math.radians(BEAMWIDTH / 2))).astype(np.float32)
    out = np.empty((nz, ny, nx), np.float32)
    rows = np.arange(ny)[:, None]
    cols = np.arange(nx)[None, :]
    for zi, z in enumerate(np.asarray(zs, np.float32)):
        k = (H < z).sum(axis=0)                     # beams below this height, per column
        kl = np.clip(k - 1, 0, T - 1)
        ku = np.clip(k, 0, T - 1)
        hl, hu = H[kl, rows, cols], H[ku, rows, cols]
        vl, vu = V[kl, rows, cols], V[ku, rows, cols]
        w = np.where(ku > kl, (z - hl) / np.maximum(hu - hl, 1e-6), 0.0).astype(np.float32)
        v = vl + (vu - vl) * w                      # (below the lowest beam: kl == ku == 0, the lowest tilt)
        above = (k == T) & (z > top_edge)           # above the highest beam: nothing (faded over 0.5 km)
        if above.any():
            fade = np.clip(1.0 - (z - top_edge) / 0.5, 0.0, 1.0)
            v = np.where(above, floor + (v - floor) * fade, v)
        out[zi] = v
    return out, base


def build(tilts, ref_tilts, box, top_km, pid, detail="normal", rule=None, key=()):
    """The 3-D grid for [pid] from its tilts ([(elevation, SweepImage)]); [ref_tilts] are reflectivity's tilts
    (masking and the outline), None when [pid] is reflectivity."""
    t0 = time.perf_counter()
    rule = rule or RULES.get(pid, RULES["REF"])
    xs, ys, zs = plan(box, top_km, detail)
    vals, base = grid_tilts(tilts, xs, ys, zs, rule.floor)
    ctx = None
    if ref_tilts is not None:
        ref, _ = grid_tilts(ref_tilts, xs, ys, zs, REF_RANGE[0])
        if rule.mask_dbz is not None:
            weak = ref < rule.mask_dbz
            vals[weak] = rule.floor
            ref_base = _ref_base(ref_tilts, xs, ys)
            if ref_base is not None:
                base[~(ref_base >= rule.mask_dbz)] = np.nan
        ctx = ref
    if rule.smooth > 0:
        # gentle smoothing (more across than up): radar data is noisy from gate to gate, and surfaces through
        # raw data look ribbed
        from scipy import ndimage
        sig = (rule.smooth * 1.2, rule.smooth * 1.7, rule.smooth * 1.7)
        vals = ndimage.gaussian_filter(vals, sig, mode="nearest")
        if ctx is not None:
            ctx = ndimage.gaussian_filter(ctx, (0.8, 1.1, 1.1), mode="nearest")
    els = tuple(sorted(round(float(e), 2) for e, _ in tilts))
    return Grid3D(values=vals.astype(np.float16), context=None if ctx is None else ctx.astype(np.float16),
                  base=base, box=tuple(float(b) for b in box), top=float(top_km), pid=pid, elevations=els,
                  build_s=time.perf_counter() - t0, key=key,
                  info={"res_km": float(xs[1] - xs[0]), "dz_km": float(zs[1] - zs[0])})


def _ref_base(ref_tilts, xs, ys):
    tilts = sorted([t for t in ref_tilts if t[1] is not None], key=lambda t: t[0])
    if not tilts:
        return None
    X, Y = np.meshgrid(xs, ys)
    AZ, S = az_range(X, Y)
    v, _h, _c = sample_tilt(tilts[0][1], AZ, S, tilts[0][0], REF_RANGE[0], nearest=True)
    return v
