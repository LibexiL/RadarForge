"""Bundled basemap layers, projected to the radar-centred frame."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..products.geometry import aeqd_forward

ASSET = Path(__file__).resolve().parent.parent / "assets" / "maps.npz"

# name -> (label, rgba, width px, min px-per-km to show)
LAYER_STYLE = {
    "lakes": ("Lakes", (70, 110, 170, 200), 1.0, 0.0),
    "countries": ("Countries / coast", (215, 215, 215, 255), 1.8, 0.0),
    "counties": ("Counties", (105, 105, 105, 230), 1.0, 0.35),
    "roads2": ("Highways", (120, 85, 60, 220), 1.0, 1.2),
    "roads": ("Interstates", (175, 70, 70, 235), 1.4, 0.25),
    "states": ("States / provinces", (225, 225, 225, 255), 1.8, 0.0),
}
DRAW_ORDER = ["lakes", "counties", "roads2", "roads", "states", "countries"]


@dataclass
class ProjectedLayer:
    name: str
    segments: np.ndarray       # float32 (n*2, 2) GL_LINES vertex pairs (km)
    bbox: tuple


class MapData:
    def __init__(self, path: Path = ASSET):
        self.ok = path.exists()
        self.raw = dict(np.load(path, allow_pickle=False)) if self.ok else {}
        self._proj_center = None
        self.layers: dict = {}
        self.city_xy = np.zeros((0, 2), np.float32)

    def layer_names(self):
        return [n for n in DRAW_ORDER if f"{n}_pts" in self.raw]

    def project(self, lat0: float, lon0: float, max_km: float = 1400.0):
        if self._proj_center == (lat0, lon0):
            return
        self._proj_center = (lat0, lon0)
        self.layers = {}
        # rough lat/lon pre-filter for speed
        dlat = max_km / 111.0
        dlon = max_km / (111.0 * max(np.cos(np.radians(lat0)), 0.2))
        for name in self.layer_names():
            pts = self.raw[f"{name}_pts"]
            starts = self.raw[f"{name}_starts"]
            lon, lat = pts[:, 0], pts[:, 1]
            near = (np.abs(lat - lat0) < dlat) & (np.abs(((lon - lon0 + 180) % 360) - 180) < dlon)
            x, y = aeqd_forward(lat, lon, lat0, lon0)
            xy = np.stack([x, y], 1).astype(np.float32)
            # segment i connects point i -> i+1 unless i+1 starts a new line
            n = len(pts)
            if n < 2:
                continue
            idx = np.arange(n - 1)
            brk = np.zeros(n, bool)
            brk[starts[1:-1] - 1] = True       # last point of each line
            ok = ~brk[:-1] & (near[:-1] | near[1:])
            a = idx[ok]
            seg = np.empty((len(a) * 2, 2), np.float32)
            seg[0::2] = xy[a]
            seg[1::2] = xy[a + 1]
            self.layers[name] = ProjectedLayer(name, seg, (0, 0, 0, 0))
        if "city_lat" in self.raw:
            x, y = aeqd_forward(self.raw["city_lat"], self.raw["city_lon"], lat0, lon0)
            self.city_xy = np.stack([x, y], 1).astype(np.float32)
            self.city_name = self.raw["city_name"]
            self.city_pop = self.raw["city_pop"]

    def county_polygons(self):
        """[(fips, lon/lat array)] for watch shading."""
        if "counties_pts" not in self.raw:
            return {}
        pts = self.raw["counties_pts"]
        st = self.raw["counties_starts"]
        fips = self.raw["counties_fips"]
        out: dict = {}
        for i, f in enumerate(fips):
            out.setdefault(int(f), []).append(pts[st[i]:st[i + 1]])
        return out
