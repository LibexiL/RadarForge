"""Colours for the MRMS layers. They sit over the radar, so they are drawn semi-transparent and start above a
threshold: weak values are left clear."""
from __future__ import annotations

import numpy as np

# (value, (r, g, b, alpha)): colours are blended between the points; below the first value nothing is drawn
STOPS = {
    # rotation tracks, azimuthal shear in 10^-3 per second: yellow (a mesocyclone) up to white (violent)
    "rotation": [(2, (255, 235, 130, 110)), (4, (255, 185, 40, 190)), (6, (255, 110, 20, 215)), (9, (255, 40, 40, 235)),
                 (13, (255, 0, 170, 245)), (18, (190, 50, 255, 250)), (25, (255, 255, 255, 255))],
    # maximum expected size of hail in mm: 13 mm = 0.5 in, 25 = 1 in, 51 = 2 in, 76 = 3 in
    "hail": [(10, (70, 215, 110, 120)), (19, (170, 230, 40, 185)), (25, (255, 235, 40, 210)), (32, (255, 170, 30, 225)),
             (38, (255, 100, 30, 235)), (51, (255, 30, 30, 245)), (76, (255, 0, 190, 250)), (102, (200, 80, 255, 255))],
    # rainfall in mm
    "qpe": [(0.5, (150, 225, 255, 90)), (2, (80, 180, 255, 150)), (5, (30, 130, 235, 185)), (10, (0, 200, 110, 200)),
            (20, (150, 225, 0, 210)), (30, (255, 235, 0, 220)), (50, (255, 150, 0, 230)), (75, (255, 40, 0, 240)),
            (125, (220, 0, 150, 248)), (200, (160, 80, 235, 255))],
}


def colorize(values: np.ndarray, kind: str) -> np.ndarray:
    """RGBA (uint8, h x w x 4) for a field; NaN and values below the first threshold are transparent."""
    stops = STOPS.get(kind, STOPS["qpe"])
    xs = np.array([s[0] for s in stops], np.float64)
    v = np.asarray(values, np.float32)
    good = np.isfinite(v) & (v >= xs[0])
    out = np.zeros(v.shape + (4,), np.uint8)
    if good.any():
        vv = np.where(good, v, xs[0]).astype(np.float64)
        for c in range(4):
            out[..., c] = np.where(good, np.interp(vv, xs, [s[1][c] for s in stops]), 0).astype(np.uint8)
    return out


def describe(value: float, kind: str, units: str) -> str:
    """'MESH 1.5 in (38 mm)' / 'rotation 9 ×10⁻³ /s' / 'rain 12.0 mm (0.47 in)'."""
    if kind == "hail":
        return f"MESH {value / 25.4:.2f} in ({value:.0f} mm)"
    if kind == "qpe":
        return f"rain {value:.1f} mm ({value / 25.4:.2f} in)"
    return f"rotation {value:.0f} {units}"
