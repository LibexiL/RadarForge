"""Derived per-tilt and volume products computed from Level II data."""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .geometry import beam_height, fold, slant_range

KT = 0.514444


# --------------------------------------------------------------------------- #
# per tilt
# --------------------------------------------------------------------------- #
def storm_relative(vel: np.ndarray, az_deg: np.ndarray, dir_from: float, speed_kts: float) -> np.ndarray:
    """SRV = V - storm motion projected on each radial (dir_from = direction storm moves FROM)."""
    toward = np.radians(dir_from + 180.0)
    u = speed_kts * KT * np.sin(toward)
    v = speed_kts * KT * np.cos(toward)
    a = np.radians(az_deg)[:, None]
    return vel - (u * np.sin(a) + v * np.cos(a)).astype(np.float32)


def despeckle(v: np.ndarray, rf_mask: np.ndarray | None = None, min_neighbors: int = 3) -> np.ndarray:
    """Blank isolated gates (noise speckle): keep a gate only if at least *min_neighbors*
    of its 8 neighbours (azimuth wraps) also hold data. Range-folded gates count as data."""
    have = np.isfinite(v)
    if rf_mask is not None:
        have = have | rf_mask
    cnt = ndimage.uniform_filter(have.astype(np.float32), size=3, mode=("wrap", "constant")) * 9.0
    cnt -= have
    out = v.copy()
    out[np.isfinite(v) & (cnt < min_neighbors - 0.5)] = np.nan
    return out


def velocity_texture(v: np.ndarray, nyq: float | None) -> np.ndarray:
    """RMS of fold-aware differences to neighbouring gates (m/s). Noise ~0.6*Nyquist, weather a few m/s."""
    ok = np.isfinite(v)
    vz = np.where(ok, v, 0.0).astype(np.float64)
    acc = np.zeros(v.shape, np.float64)
    cnt = np.zeros(v.shape, np.float64)
    for da, dg in ((1, 0), (-1, 0), (0, 1), (0, -1), (0, 2), (0, -2)):
        sv = np.roll(vz, da, axis=0)
        so = np.roll(ok, da, axis=0)
        if dg:
            sv = np.roll(sv, dg, axis=1)
            so = np.roll(so, dg, axis=1)
            if dg > 0:
                so[:, :dg] = False
            else:
                so[:, dg:] = False
        d = sv - vz
        if nyq:
            d = fold(d, nyq)
        both = ok & so
        acc += np.where(both, d * d, 0.0)
        cnt += both
    with np.errstate(invalid="ignore", divide="ignore"):
        tex = np.sqrt(acc / cnt)
    tex[cnt < 2] = np.inf
    return tex.astype(np.float32)


def velocity_noise_filter(v: np.ndarray, rf_mask: np.ndarray, ref: np.ndarray | None, nyq: float | None,
                          level: int = 1) -> np.ndarray:
    """Remove noisy velocity gates. level 0 = off, 1 = normal, 2 = aggressive.

    A gate is dropped when its velocity texture is high *and* the echo is weak
    (strong echoes such as mesocyclones/tornadoes are never touched), then
    isolated leftovers are despeckled.
    """
    if level <= 0:
        return v
    nq = nyq or 30.0
    tex = velocity_texture(v, nyq)
    thr_tex = (0.42 if level == 1 else 0.32) * nq
    thr_ref = 20.0 if level == 1 else 30.0
    weak = np.ones(v.shape, bool) if ref is None else ~(np.nan_to_num(ref, nan=-99.0) >= thr_ref)
    out = v.copy()
    out[(tex > thr_tex) & weak] = np.nan
    return despeckle(out, rf_mask, min_neighbors=3 if level == 1 else 4)


def _nan_box(x: np.ndarray, size) -> np.ndarray:
    m = np.isfinite(x)
    xs = np.where(m, x, 0.0)
    num = ndimage.uniform_filter(xs.astype(np.float64), size=size, mode=("wrap", "nearest"))
    den = ndimage.uniform_filter(m.astype(np.float64), size=size, mode=("wrap", "nearest"))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den < 0.34] = np.nan
    return out.astype(np.float32)


def _llsd(vel, nyq, dealiased, coord_fn, na=2, ng=2):
    """Linear least-squares derivative of velocity; coord_fn(di, dj) -> offset distance (m)."""
    v = vel.astype(np.float64)
    num = np.zeros_like(v)
    den = np.zeros_like(v)
    cnt = np.zeros_like(v)
    for di in range(-na, na + 1):
        vr = np.roll(v, -di, axis=0)
        for dj in range(-ng, ng + 1):
            if dj > 0:
                vs = np.concatenate([vr[:, dj:], np.full((v.shape[0], dj), np.nan)], axis=1)
            elif dj < 0:
                vs = np.concatenate([np.full((v.shape[0], -dj), np.nan), vr[:, :dj]], axis=1)
            else:
                vs = vr
            d = vs - v
            if nyq and not dealiased:
                d = fold(d, nyq)
            ok = np.isfinite(d)
            s = coord_fn(di, dj)
            s = np.broadcast_to(s, v.shape)
            num += np.where(ok, s * d, 0.0)
            den += np.where(ok, s * s, 0.0)
            cnt += ok
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den * 1000.0
    out[(cnt < (2 * na + 1) * (2 * ng + 1) * 0.4) | ~np.isfinite(v)] = np.nan
    return out.astype(np.float32)


def azimuthal_shear(vel: np.ndarray, az_deg: np.ndarray, ranges_km: np.ndarray, nyq: float | None,
                    dealiased: bool = False) -> np.ndarray:
    """LLSD azimuthal shear (1/s x 1e-3). Positive = cyclonic. Fold-aware."""
    step = np.radians(np.median(np.diff(np.sort(az_deg))) if len(az_deg) > 2 else 1.0)
    na = 2 if step < np.radians(0.75) else 1
    r_m = np.maximum(ranges_km, 0.5)[None, :] * 1000.0
    out = _llsd(vel, nyq, dealiased, lambda di, dj: r_m * (di * step), na=na, ng=2)
    out[:, ranges_km < 3.0] = np.nan
    return out


def radial_divergence(vel: np.ndarray, gate_km: float, nyq: float | None, dealiased=False, k: int = 2):
    """LLSD radial divergence (1/s x 1e-3)."""
    return _llsd(vel, nyq, dealiased, lambda di, dj: np.float64(dj * gate_km * 1000.0), na=1, ng=3)


def kdp_from_phi(phi: np.ndarray, cc: np.ndarray | None, ref: np.ndarray | None,
                 gate_km: float) -> np.ndarray:
    """KDP (deg/km) by moving least-squares slope of unwrapped PHIDP."""
    p = phi.astype(np.float64)
    mask = np.isfinite(p)
    if cc is not None:
        mask &= np.nan_to_num(cc, nan=0) >= 0.90
    if ref is not None:
        mask &= np.nan_to_num(ref, nan=-99) >= 15
    # unwrap along range across valid gates
    pu = np.where(mask, p, np.nan)
    for i in range(pu.shape[0]):
        row = pu[i]
        ok = np.isfinite(row)
        if ok.sum() > 2:
            row[ok] = np.degrees(np.unwrap(np.radians(row[ok])))
    # despeckle: remove isolated valid gates
    cnt = ndimage.uniform_filter(mask.astype(float), size=(1, 5), mode="nearest")
    mask &= cnt > 0.5
    y = np.where(mask, pu, 0.0)
    m = mask.astype(np.float64)
    x = np.arange(p.shape[1], dtype=np.float64)[None, :].repeat(p.shape[0], 0)

    def slope(win):
        def wsum(a):
            c = np.cumsum(np.pad(a, ((0, 0), (1, 0))), axis=1)
            h = win // 2
            idx_hi = np.clip(np.arange(a.shape[1]) + h + 1, 0, a.shape[1])
            idx_lo = np.clip(np.arange(a.shape[1]) - h, 0, a.shape[1])
            return c[:, idx_hi] - c[:, idx_lo]
        s0 = wsum(m)
        sx = wsum(m * x)
        sxx = wsum(m * x * x)
        sy = wsum(y)
        sxy = wsum(y * x)
        den = s0 * sxx - sx * sx
        with np.errstate(invalid="ignore", divide="ignore"):
            sl = (s0 * sxy - sx * sy) / den
        sl[s0 < win * 0.5] = np.nan
        return sl / gate_km * 0.5

    short = max(5, int(round(2.0 / gate_km)) | 1)
    long_ = max(9, int(round(6.0 / gate_km)) | 1)
    k_short = slope(short)
    k_long = slope(long_)
    heavy = np.nan_to_num(ref, nan=-99) >= 40 if ref is not None else np.zeros_like(mask)
    kdp = np.where(heavy, k_short, k_long)
    kdp[~mask] = np.nan
    return np.clip(kdp, -2.0, 15.0).astype(np.float32)


# --------------------------------------------------------------------------- #
# volume products on a common ground-range polar grid
# --------------------------------------------------------------------------- #
GRID_AZ = 720
GRID_DR = 0.5      # km
GRID_NR = 920      # -> 460 km


def grid_azimuths():
    return (np.arange(GRID_AZ) + 0.5) * (360.0 / GRID_AZ)


def grid_ranges():
    return (np.arange(GRID_NR) + 0.5) * GRID_DR


def resample_to_grid(values, az_sorted, first_gate, gate_km, elev):
    """Map a sweep (radials sorted by azimuth) to the ground-range grid."""
    gaz = grid_azimuths()
    n = len(az_sorted)
    idx = np.searchsorted(az_sorted, gaz) % n
    idx_prev = (idx - 1) % n
    d_next = np.abs(((az_sorted[idx] - gaz + 180) % 360) - 180)
    d_prev = np.abs(((az_sorted[idx_prev] - gaz + 180) % 360) - 180)
    ai = np.where(d_prev < d_next, idx_prev, idx)
    far = np.minimum(d_prev, d_next) > 1.5
    r = slant_range(grid_ranges(), elev)
    g = np.round((r - first_gate) / gate_km).astype(np.int64)
    gok = (g >= 0) & (g < values.shape[1])
    g = np.clip(g, 0, values.shape[1] - 1)
    out = values[ai][:, g].astype(np.float32)
    out[:, ~gok] = np.nan
    out[far] = np.nan
    return out


class VolumeGrid:
    """Stack of reflectivity tilts on the common grid."""

    def __init__(self, tilt_values: list, elevations: list, radar_height_km: float):
        order = np.argsort(elevations)
        self.el = np.asarray(elevations, np.float64)[order]
        self.z = np.stack([tilt_values[i] for i in order]) if tilt_values else np.zeros((0, GRID_AZ, GRID_NR))
        s = grid_ranges()
        self.h = np.stack([beam_height(slant_range(s, e), e) for e in self.el]) if len(self.el) else None
        self.h0 = radar_height_km

    def composite(self):
        import warnings
        if not len(self.z):
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmax(self.z, axis=0)

    def echo_top(self, thresh: float) -> np.ndarray:
        """Echo top height (kft MSL)."""
        z = self.z
        K = len(z)
        if K == 0:
            return None
        ge = np.nan_to_num(z, nan=-99.0) >= thresh
        anyv = ge.any(axis=0)
        top = K - 1 - np.argmax(ge[::-1], axis=0)
        ii, jj = np.indices(top.shape)
        h_top = self.h[top, jj]
        z_top = z[top, ii, jj]
        nxt = np.minimum(top + 1, K - 1)
        z_nxt = z[nxt, ii, jj]
        h_nxt = self.h[nxt, jj]
        with np.errstate(invalid="ignore", divide="ignore"):
            frac = np.where(np.isfinite(z_nxt) & (nxt > top), (z_top - thresh) / (z_top - z_nxt), 0.0)
        frac = np.clip(np.nan_to_num(frac), 0, 1)
        h = h_top + frac * (h_nxt - h_top)
        out = (h + self.h0) * 3.28084
        out[~anyv] = np.nan
        return out.astype(np.float32)

    def vil(self) -> np.ndarray:
        z = np.nan_to_num(self.z, nan=-99.0)
        zl = 10.0 ** (np.minimum(z, 56.0) / 10.0)
        zl[z < 0] = 0.0
        tot = np.zeros(self.z.shape[1:], np.float64)
        for k in range(len(self.z) - 1):
            dh = (self.h[k + 1] - self.h[k]) * 1000.0
            tot += 3.44e-6 * ((zl[k] + zl[k + 1]) / 2.0) ** (4.0 / 7.0) * dh[None, :]
        tot[tot < 0.5] = np.nan
        return tot.astype(np.float32)

    def shi(self, h0_msl_km: float, hm20_msl_km: float) -> np.ndarray:
        z = np.nan_to_num(self.z, nan=-99.0)
        wz = np.clip((z - 40.0) / 10.0, 0.0, 1.0)
        e = 5e-6 * 10.0 ** (0.084 * z) * wz
        hmsl = (self.h + self.h0) * 1000.0
        H0, Hm20 = h0_msl_km * 1000.0, hm20_msl_km * 1000.0
        wt = np.clip((hmsl - H0) / max(Hm20 - H0, 1.0), 0.0, 1.0)
        integrand = wt[:, None, :] * e
        tot = np.zeros(z.shape[1:], np.float64)
        for k in range(len(z) - 1):
            dh = (hmsl[k + 1] - hmsl[k])
            tot += 0.5 * (integrand[k] + integrand[k + 1]) * dh[None, :]
        return 0.1 * tot

    def mesh(self, h0, hm20):
        shi = self.shi(h0, hm20)
        mesh = 2.54 * np.sqrt(np.maximum(shi, 0)) / 25.4     # inches
        mesh[mesh < 0.1] = np.nan
        return mesh.astype(np.float32)

    def posh(self, h0, hm20):
        shi = self.shi(h0, hm20)
        wt = max(57.5 * (h0 - self.h0) - 121.0, 20.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            p = 29.0 * np.log(shi / wt) + 50.0
        p = np.clip(p, 0, 100)
        p[~np.isfinite(p) | (p <= 0)] = np.nan
        return p.astype(np.float32)
