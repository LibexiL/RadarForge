"""A picture on the radar's map grid: north at the top, a fixed number of km per pixel, centred on the radar.
Satellite and MRMS data are both resampled onto this, so they draw (and are read under the cursor) the same way."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ..products.geometry import aeqd_inverse


@dataclass
class Grid:
    values: np.ndarray       # float32 (ny, nx); NaN = no data
    half_km: float
    step_km: float
    time: datetime
    kind: str                # what the numbers are: "bt", "refl", "rotation", "hail", "qpe"
    units: str

    def sample(self, x_km: float, y_km: float):
        """Value at a point in km east / north of the radar (None outside the picture or without data)."""
        ix = int(round((x_km + self.half_km) / self.step_km))
        iy = int(round((self.half_km - y_km) / self.step_km))
        if 0 <= ix < self.values.shape[1] and 0 <= iy < self.values.shape[0]:
            v = float(self.values[iy, ix])
            return None if v != v else v
        return None


def pixel_centres(half_km: float, step_km: float):
    """(x, y) km of every pixel centre, as 2-D arrays shaped like the picture."""
    n = int(round(2 * half_km / step_km))
    xs = -half_km + (np.arange(n) + 0.5) * step_km
    ys = half_km - (np.arange(n) + 0.5) * step_km
    return np.meshgrid(xs, ys)


def pixel_latlon(lat0: float, lon0: float, half_km: float, step_km: float):
    """Latitude and longitude of every pixel centre of the grid around a radar."""
    gx, gy = pixel_centres(half_km, step_km)
    return aeqd_inverse(gx, gy, lat0, lon0)
