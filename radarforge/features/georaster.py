"""Gridded data (satellite, MRMS) reprojected onto the radar-centred map.

A raster is resampled once onto a square grid in the view's azimuthal equidistant
projection (km around the radar) and kept as a QImage; painting is one scaled drawImage.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter

from ..products.geometry import aeqd_inverse

# --------------------------------------------------------------------------- target grid


def aeqd_grid(lat0, lon0, half_km, n):
    """lat/lon (n x n, row 0 = north) of pixel centres covering [-half, half] km around lat0/lon0."""
    step = 2.0 * half_km / n
    c = -half_km + step * (np.arange(n) + 0.5)
    x, y = np.meshgrid(c, c[::-1])
    return aeqd_inverse(x, y, lat0, lon0)


# --------------------------------------------------------------------------- GOES fixed grid
GOES_REQ = 6378137.0
GOES_RPOL = 6356752.31414
GOES_H = 42164160.0                      # from the earth's centre (perspective height + req)
GOES_E = 0.0818191910435


def geos_forward(lat, lon, lon0, sweep="x"):
    """Scan angles (radians) of lat/lon seen from a geostationary satellite over lon0, and a
    visibility mask. GOES-R product user guide, section 4.2.8."""
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64) - lon0)
    phic = np.arctan((GOES_RPOL ** 2 / GOES_REQ ** 2) * np.tan(phi))
    rc = GOES_RPOL / np.sqrt(1.0 - GOES_E ** 2 * np.cos(phic) ** 2)
    sx = GOES_H - rc * np.cos(phic) * np.cos(lam)
    sy = -rc * np.cos(phic) * np.sin(lam)
    sz = rc * np.sin(phic)
    visible = GOES_H * (GOES_H - sx) >= sy ** 2 + (GOES_REQ ** 2 / GOES_RPOL ** 2) * sz ** 2
    if sweep == "x":
        y = np.arctan(sz / sx)
        x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    else:
        x = np.arctan(-sy / sx)
        y = np.arcsin(sz / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    return x, y, visible


def parse_world_file(text: str):
    """(A, D, B, E, C, F) from a .wld file: x = A*col + B*row + C, y = D*col + E*row + F (pixel centres)."""
    nums = [float(v) for v in text.replace(",", " ").split()[:6]]
    if len(nums) < 6 or nums[0] == 0 or nums[3] == 0:
        raise ValueError("bad world file")
    return tuple(nums)


def world_pixels(wld, x, y):
    """Fractional (row, col) of map coordinates x, y for an axis-aligned world file."""
    A, D, B, E, C, F = wld
    if abs(B) > 1e-12 or abs(D) > 1e-12:
        det = A * E - B * D
        dx, dy = np.asarray(x) - C, np.asarray(y) - F
        col = (E * dx - B * dy) / det
        row = (-D * dx + A * dy) / det
        return row, col
    return (np.asarray(y) - F) / E, (np.asarray(x) - C) / A


# --------------------------------------------------------------------------- colours


def ramp(stops, n=256):
    """A lookup table (n, 4) uint8 from [(position 0..1, (r, g, b, a)), ...]."""
    pos = np.array([s[0] for s in stops], float)
    col = np.array([s[1] for s in stops], float)
    t = np.linspace(0, 1, n)
    return np.stack([np.interp(t, pos, col[:, k]) for k in range(4)], 1).round().astype(np.uint8)


def colorize(values, stops):
    """RGBA uint8 for values from [(value, (r, g, b, a)), ...] stops (ascending), linear between them.
    Values below the first stop, and NaN, are transparent; above the last get its colour."""
    v = np.asarray(values, np.float32)
    lv = np.array([s[0] for s in stops], np.float32)
    cols = np.array([s[1] for s in stops], np.float32)
    out = np.zeros(v.shape + (4,), np.uint8)
    ok = np.isfinite(v) & (v >= lv[0])
    vv = np.clip(v[ok], lv[0], lv[-1])
    for k in range(4):
        out[..., k][ok] = np.interp(vv, lv, cols[:, k]).round().astype(np.uint8)
    return out


def to_qimage(rgba: np.ndarray) -> QImage:
    rgba = np.ascontiguousarray(rgba, np.uint8)
    h, w = rgba.shape[:2]
    img = QImage(rgba.data, w, h, 4 * w, QImage.Format_RGBA8888)
    return img.copy()               # own the pixels


class Raster:
    """A QImage covering [-half, half] km around a projection centre."""

    def __init__(self, image: QImage, half_km: float, center: tuple, time=None, label="", values=None):
        self.image = image
        self.half_km = half_km
        self.center = center          # (lat0, lon0) it was projected for
        self.time = time
        self.label = label
        self.values = values          # (n, n) float grid behind the image (for read-outs), optional
        self.pkey = self.fkey = self.kind = None     # product key, source file and kind (set by the layer)

    def paint(self, painter, vt, opacity=1.0, smooth=True):
        h = self.half_km
        sx0, sy0 = vt.to_screen(-h, h)
        sx1, sy1 = vt.to_screen(h, -h)
        painter.save()
        painter.setOpacity(opacity)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
        painter.drawImage(QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0), self.image)
        painter.restore()

    def value_at(self, x, y):
        """The grid value under world point x, y (km), or None."""
        if self.values is None:
            return None
        n = self.values.shape[0]
        col = int((x + self.half_km) / (2 * self.half_km) * n)
        row = int((self.half_km - y) / (2 * self.half_km) * n)
        if 0 <= row < n and 0 <= col < n:
            v = float(self.values[row, col])
            return v if np.isfinite(v) else None
        return None


__all__ = ["aeqd_grid", "geos_forward", "parse_world_file", "world_pixels", "ramp", "colorize", "to_qimage",
           "Raster"]
