"""Colour tables for satellite pictures: brightness temperature (infrared, water vapour) and visible."""
from __future__ import annotations

import numpy as np

# (temperature in °C, (r, g, b)). Warm = dark, cold cloud tops step into a cool blue -> violet -> pink
# ramp, which can't be mistaken for the radar's green / yellow / red echoes drawn over it.
IR_STOPS = [(50, (0, 0, 0)), (-10, (130, 130, 130)), (-30, (215, 215, 215)), (-32, (150, 190, 235)),
            (-42, (100, 150, 240)), (-52, (95, 100, 235)), (-62, (170, 90, 235)), (-72, (255, 120, 220)),
            (-80, (255, 215, 245)), (-95, (255, 255, 255))]
WV_STOPS = [(10, (45, 32, 22)), (-20, (120, 110, 100)), (-35, (210, 210, 215)), (-42, (150, 195, 235)),
            (-52, (100, 120, 235)), (-62, (175, 100, 235)), (-72, (255, 130, 225)), (-85, (255, 255, 255))]
SWIR_STOPS = [(60, (0, 0, 0)), (-50, (255, 255, 255))]

STOPS = {"ir": IR_STOPS, "wv": WV_STOPS, "swir": SWIR_STOPS}



def _ramp(celsius: np.ndarray, stops) -> np.ndarray:
    temps = np.array([s[0] for s in stops], np.float64)[::-1]              # np.interp wants ascending x
    rgb = np.array([s[1] for s in stops], np.float64)[::-1]
    out = np.empty(celsius.shape + (3,), np.float32)
    for c in range(3):
        out[..., c] = np.interp(celsius, temps, rgb[:, c])
    return out


def colorize(values: np.ndarray, channel: str, alpha: int = 255) -> np.ndarray:
    """RGBA (uint8, h x w x 4) for a picture: brightness temperature in K, or reflectance (0-1) for "vis".
    Pixels without data (NaN) are transparent."""
    v = np.asarray(values, np.float32)
    good = np.isfinite(v)
    if channel == "vis":
        g = np.clip(np.nan_to_num(v), 0.0, 1.0) ** 0.5                        # gamma brightens the shadows
        rgb = np.repeat((g * 255.0)[..., None], 3, axis=2)
    else:
        rgb = _ramp(np.nan_to_num(v, nan=300.0) - 273.15, STOPS.get(channel, IR_STOPS))
    out = np.empty(v.shape + (4,), np.uint8)
    out[..., :3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = np.where(good, alpha, 0)
    return out


def describe(value: float, channel: str) -> str:
    """Readable value for the cursor readout: '-62 °C' or '45 % reflectance'."""
    if channel == "vis":
        return f"{value * 100:.0f}% reflectance"
    return f"{value - 273.15:.0f} °C"
