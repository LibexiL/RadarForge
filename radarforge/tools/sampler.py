"""Sample a radar volume (all tilts of one product) at arbitrary 3-D points."""
from __future__ import annotations

import numpy as np

from ..products.geometry import az_range, point_to_beam

BEAM_HALF = 0.5   # degrees (NEXRAD beamwidth ~0.95 deg)


def _lookup(img, az, r):
    """Vectorised value lookup in a SweepImage at azimuth/slant range arrays."""
    n = len(img.az)
    out = np.full(az.shape, np.nan, np.float32)
    if n == 0 or az.size == 0:
        return out
    i = np.searchsorted(img.az, az) % n
    j = (i - 1) % n
    di = np.abs(((img.az[i] - az + 180) % 360) - 180)
    dj = np.abs(((img.az[j] - az + 180) % 360) - 180)
    row = np.where(dj < di, j, i)
    half = (img.az_hi[row] - img.az_lo[row]) % 360 / 2 + 1e-3
    ok = np.minimum(di, dj) <= half
    g = np.floor((r - (img.first_gate - img.gate_spacing / 2)) / img.gate_spacing).astype(np.int64)
    ok &= (g >= 0) & (g < img.values.shape[1])
    g = np.clip(g, 0, img.values.shape[1] - 1)
    out[ok] = img.values[row[ok], g[ok]].astype(np.float32)
    return out


def sample_volume(tilt_images, x, y, z_km_agl, smooth=True):
    """Values at points (x, y km from radar; z km above antenna).

    tilt_images: [(elevation, SweepImage)] (any order)
    """
    if not tilt_images:
        return np.full(np.shape(x), np.nan, np.float32)
    tilt_images = sorted(tilt_images, key=lambda t: t[0])
    els = np.array([t[0] for t in tilt_images])
    az, s = az_range(np.asarray(x, np.float64), np.asarray(y, np.float64))
    theta, r = point_to_beam(s, z_km_agl)
    shape = theta.shape
    theta, r, az = theta.ravel(), r.ravel(), az.ravel()
    k = np.clip(np.searchsorted(els, theta), 0, len(els) - 1)
    km = np.clip(k - 1, 0, len(els) - 1)
    near = np.where(np.abs(els[km] - theta) < np.abs(els[k] - theta), km, k)
    out = np.full(theta.shape, np.nan, np.float32)

    def tilt_vals(idx, mask):
        img = tilt_images[idx][1]
        return _lookup(img, az[mask], r[mask])

    # nearest tilt within the beam
    for t in range(len(els)):
        m = (near == t) & (np.abs(els[t] - theta) <= BEAM_HALF)
        if m.any():
            out[m] = tilt_vals(t, m)
    if smooth:
        # fill gaps between adjacent tilts by linear interpolation in elevation
        gap = ~np.isfinite(out) & (theta > els[0]) & (theta < els[-1])
        if gap.any():
            lo_i = np.clip(np.searchsorted(els, theta) - 1, 0, len(els) - 2)
            for t in range(len(els) - 1):
                m = gap & (lo_i == t)
                if not m.any():
                    continue
                a = tilt_vals(t, m)
                b = tilt_vals(t + 1, m)
                w = (theta[m] - els[t]) / max(els[t + 1] - els[t], 1e-6)
                both = np.isfinite(a) & np.isfinite(b) & ~np.isinf(a) & ~np.isinf(b)
                res = np.full(a.shape, np.nan, np.float32)
                res[both] = (a[both] * (1 - w[both]) + b[both] * w[both]).astype(np.float32)
                sub = out[m]
                sub[both] = res[both]
                out[m] = sub
    return out.reshape(shape)
