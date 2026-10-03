"""GOES satellite imagery (infrared, visible, water vapour) under the radar.

The Iowa Environmental Mesonet keeps the latest CONUS image of each channel as a PNG with a
world file in the satellite's fixed-grid projection:
  https://mesonet.agron.iastate.edu/data/gis/images/GOES/conus/channelNN/GOES-19_CNN.png (+ .wld, .json)
It is reprojected here onto the radar-centred map (GOES-R fixed-grid formulas).
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone

import numpy as np
import requests

from . import georaster
from .bglayer import UA, BackgroundLayer

BASE = "https://mesonet.agron.iastate.edu/data/gis/images/GOES/conus"
CHANNELS = {"ir": (13, "Infrared (clean IR, band 13)"), "vis": (2, "Visible (band 2)"),
            "wv": (9, "Water vapour (mid-level, band 9)")}
SAT_LON = {"GOES-19": -75.0, "GOES-18": -137.0}
PERSPECTIVE_H = 35786023.0
HALF_KM = 900.0
GRID_N = 900
# CONUS sector edges (scan-angle radians: x west, x east, y north, y south), used if the world file can't be read
SECTOR_EDGES = {"GOES-19": (-0.101332, 0.038612, 0.128212, 0.044268),
                "GOES-18": (-0.069986, 0.069986, 0.128212, 0.044268)}

# infrared enhancement (input 0-255 grey, bright = cold cloud tops)
IR_STOPS = [(0.0, (0, 0, 0, 255)), (0.55, (175, 175, 175, 255)), (0.70, (235, 235, 235, 255)),
            (0.74, (0, 120, 255, 255)), (0.80, (0, 230, 120, 255)), (0.86, (255, 240, 0, 255)),
            (0.91, (255, 120, 0, 255)), (0.95, (230, 0, 0, 255)), (1.0, (255, 255, 255, 255))]
WV_STOPS = [(0.0, (60, 30, 10, 255)), (0.35, (150, 100, 40, 255)), (0.5, (225, 225, 225, 255)),
            (0.75, (40, 120, 220, 255)), (1.0, (230, 255, 255, 255))]


def satellite_for(lon: float) -> str:
    """GOES-West for western radars, GOES-East for the rest."""
    return "GOES-18" if lon < -105.0 else "GOES-19"


def urls(sat: str, channel: int) -> dict:
    stem = f"{BASE}/channel{channel:02d}/{sat}_C{channel:02d}"
    return {k: f"{stem}.{k}" for k in ("png", "wld", "json")}


def lon0_from_proj(proj: str, default: float) -> float:
    m = re.search(r"\+lon_0=(-?[\d.]+)", proj or "")
    return float(m.group(1)) if m else default


def scan_world(wld, w, h, sat):
    """World file in scan-angle radians (IEM's are in projection metres)."""
    if wld is None:
        if sat not in SECTOR_EDGES:
            raise ValueError("no world file for this satellite")
        x0, x1, y0, y1 = SECTOR_EDGES[sat]
        a, e = (x1 - x0) / w, (y1 - y0) / h
        return (a, 0.0, 0.0, e, x0 + a / 2, y0 + e / 2)
    if max(abs(wld[4]), abs(wld[5])) > 10.0:                   # metres -> radians
        return tuple(v / PERSPECTIVE_H for v in wld)
    return wld


def image_rgba(im, kind: str, enhance: bool) -> np.ndarray:
    """(h, w, 4) uint8 from the PIL image; grey images get the channel's colour table."""
    if im.mode in ("RGB", "RGBA", "P", "PA"):
        rgba = np.asarray(im.convert("RGBA")).copy()
        black = (rgba[..., 0] == 0) & (rgba[..., 1] == 0) & (rgba[..., 2] == 0)
        if kind != "vis":
            rgba[..., 3][black] = 0                     # off the disk / no data
        return rgba
    g = np.asarray(im.convert("L"), np.float32) / 255.0
    if kind == "ir" and enhance:
        lut = georaster.ramp(IR_STOPS)
    elif kind == "wv" and enhance:
        lut = georaster.ramp(WV_STOPS)
    else:
        lut = georaster.ramp([(0.0, (0, 0, 0, 255)), (1.0, (255, 255, 255, 255))])
    rgba = lut[np.clip((g * 255).round().astype(np.int32), 0, 255)]
    if kind != "vis":
        rgba[..., 3][g <= 0.0] = 0
    return rgba


def reproject(rgba, wld, lon0, lat_c, lon_c, half_km=HALF_KM, n=GRID_N) -> np.ndarray:
    lat, lon = georaster.aeqd_grid(lat_c, lon_c, half_km, n)
    x, y, vis = georaster.geos_forward(lat, lon, lon0)
    row, col = georaster.world_pixels(wld, x, y)
    r = np.rint(row).astype(np.int64)
    c = np.rint(col).astype(np.int64)
    h, w = rgba.shape[:2]
    ok = vis & (r >= 0) & (r < h) & (c >= 0) & (c < w)
    out = np.zeros((n, n, 4), np.uint8)
    out[ok] = rgba[r[ok], c[ok]]
    return out


def fetch_image(sat, channel):
    from PIL import Image
    u = urls(sat, channel)
    meta = {}
    try:
        meta = requests.get(u["json"], headers=UA, timeout=20).json().get("meta") or {}
    except Exception:
        pass
    r = requests.get(u["png"], headers=UA, timeout=60)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content))
    im.load()
    wld = None
    try:
        wr = requests.get(u["wld"], headers=UA, timeout=20)
        wr.raise_for_status()
        wld = georaster.parse_world_file(wr.content.decode("ascii", "replace"))
    except Exception:
        wld = None
    valid = None
    try:
        valid = datetime.fromisoformat(str(meta.get("valid")).replace("Z", "+00:00"))
    except ValueError:
        valid = None
    return im, wld, meta, valid


class SatelliteOverlay(BackgroundLayer):
    """GOES imagery drawn beneath the radar (live data only: IEM keeps the latest picture)."""
    title = "Satellite"

    def __init__(self, settings, view, is_live, parent=None):
        super().__init__(settings, parent)
        self.view = view
        self.is_live = is_live
        self.raster = None
        self._src = None              # (sat, channel, valid, image, wld, lon0) last download
        self._attempted = None

    def enabled(self):
        return bool(self.settings["overlays"].get("satellite", False)) and self.is_live()

    def kind(self):
        k = self.settings["satellite_channel"]
        return k if k in CHANNELS else "ir"

    def jobs(self):
        if not self.enabled():
            return []
        kind = self.kind()
        channel = CHANNELS[kind][0]
        center = (self.view.lat0, self.view.lon0)
        sat = satellite_for(center[1])
        enhance = bool(self.settings["satellite_enhance"])
        want = (sat, kind, center, enhance)
        if want != self._attempted:
            self._attempted = want
            self._next.pop("sat", None)

        def work():
            src = self._src
            if src is None or src[:2] != (sat, channel) or self.raster is None or self.raster.pkey == want:
                im, wld, meta, valid = fetch_image(sat, channel)
                lon0 = lon0_from_proj(meta.get("proj4str"), SAT_LON[sat])
                src = self._src = (sat, channel, valid, im, wld, lon0)
            _sat, _ch, valid, im, wld, lon0 = src
            rgba = image_rgba(im, kind, enhance)
            sw = scan_world(wld, rgba.shape[1], rgba.shape[0], sat)
            out = reproject(rgba, sw, lon0, center[0], center[1])
            ras = georaster.Raster(georaster.to_qimage(out), HALF_KM, center, valid,
                                   f"{sat} {CHANNELS[kind][1].split(' (')[0].lower()}")
            ras.pkey = want
            self.raster = ras
        return [("sat", work, 5 * 60)]

    def has_below(self):
        r = self.raster
        return self.enabled() and r is not None and r.center == (self.view.lat0, self.view.lon0)

    def paint_below(self, painter, vt, panel, view):
        r = self.raster
        if r is None or r.center != (view.lat0, view.lon0):
            return
        r.paint(painter, vt, float(self.settings["satellite_opacity"] or 0.85))

    def caption(self):
        r = self.raster
        if not self.enabled() or r is None:
            return None
        t = r.time
        age = ""
        if t is not None:
            mins = (datetime.now(timezone.utc) - t).total_seconds() / 60
            age = f" · {t:%H:%MZ}" + (f" ({mins:.0f} min old)" if mins > 20 else "")
        return f"{r.label}{age}"
