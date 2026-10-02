"""GOES satellite picture drawn under the radar (cloud tops, water vapour, visible)."""
from __future__ import annotations

from ..data import goes
from ..products import satcolors
from .raster import GRID_HALF_KM, GRID_STEP_KM, RasterLayer, rgba_image


def to_image(grid, channel: str):
    return rgba_image(satcolors.colorize(grid.values, channel))


class SatelliteLayer(RasterLayer):
    key = "satellite"
    below = True
    label = "satellite"
    tolerance_min = 15.0

    # ---------------------------------------------------------------- settings
    def channel(self) -> str:
        c = self.settings["satellite_channel"]
        return c if c in goes.CHANNELS else "ir"

    def satellite(self, lon: float) -> str:
        s = self.settings["satellite_sat"]
        return s if s in goes.BUCKETS else goes.satellite_for(lon)

    def signature(self, lon0):
        return (self.channel(), self.satellite(lon0))

    # ---------------------------------------------------------------- the data
    def listing(self, signature, t):
        from datetime import timedelta
        channel, sat = signature
        return goes.list_abi(sat, channel, t - timedelta(minutes=self.tolerance_min + 30),
                             t + timedelta(minutes=self.tolerance_min))

    def load_grid(self, scan, signature, lat0, lon0):
        channel = signature[0]
        raw = goes.fetch_scan(scan, cache=channel != "vis")        # the 65 MB visible files aren't kept
        return goes.reproject(raw, lat0, lon0, GRID_HALF_KM, GRID_STEP_KM)

    def to_image(self, grid, signature):
        return to_image(grid, signature[0])

    def caption(self, scan, signature):
        channel, sat = signature
        return f"{goes.SATELLITE_NAMES[sat]} {goes.CHANNELS[channel]['short']}  {scan.time:%H:%M}Z"

    def describe(self, value, signature):
        channel = signature[0]
        return f"{goes.CHANNELS[channel]['short']}: {satcolors.describe(value, channel)}"
