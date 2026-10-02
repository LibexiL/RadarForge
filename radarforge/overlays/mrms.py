"""MRMS rotation tracks, hail swaths and rainfall totals drawn over the radar."""
from __future__ import annotations

from datetime import timedelta

from ..data import mrms
from ..products import mrmscolors
from .raster import GRID_HALF_KM, GRID_STEP_KM, RasterLayer, rgba_image


class MrmsLayer(RasterLayer):
    key = "mrms"
    below = False
    label = "MRMS"
    tolerance_min = 10.0                  # a new field every 2 minutes
    refresh_ms = 60_000

    def product(self) -> str:
        p = self.settings["mrms_product"]
        return p if p in mrms.PRODUCTS else "rotation"

    def window(self) -> int:
        w = self.settings["mrms_window"] or {}
        p = self.product()
        try:
            w = int(w.get(p, mrms.DEFAULT_WINDOW[p]))
        except (TypeError, ValueError, AttributeError):
            w = mrms.DEFAULT_WINDOW[p]
        return w if w in mrms.PRODUCTS[p]["windows"] else mrms.DEFAULT_WINDOW[p]

    def signature(self, lon0):
        return (self.product(), self.window())

    def listing(self, signature, t):
        product, window = signature
        return mrms.list_files(product, window, t - timedelta(minutes=self.tolerance_min + 30),
                               t + timedelta(minutes=self.tolerance_min))

    def load_grid(self, scan, signature, lat0, lon0):
        product = signature[0]
        msg = mrms.read_field(mrms.fetch(scan))
        return mrms.to_grid(msg, product, scan.time, lat0, lon0, GRID_HALF_KM, GRID_STEP_KM)

    def to_image(self, grid, signature):
        return rgba_image(mrmscolors.colorize(grid.values, grid.kind))

    def caption(self, scan, signature):
        product, window = signature
        return f"MRMS {mrms.PRODUCTS[product]['label'].split(' (')[0].lower()} ({mrms.window_label(product, window)})  " \
               f"{scan.time:%H:%M}Z"

    def describe(self, value, signature):
        p = mrms.PRODUCTS[signature[0]]
        return mrmscolors.describe(value, p["kind"], p["units"])
