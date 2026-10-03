"""Surface observations (ASOS / METAR stations) as station plots: temperature, dew point, sky cover
and a wind barb. Current conditions from the Iowa Environmental Mesonet:
  https://mesonet.agron.iastate.edu/api/1/currents.json?network=OK_ASOS
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from PySide6.QtCore import QLineF, QPointF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data.sites import all_sites, haversine_km
from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font
from .bglayer import UA, BackgroundLayer

CURRENTS_URL = "https://mesonet.agron.iastate.edu/api/1/currents.json"
SKY_FRAC = {"CLR": 0, "SKC": 0, "NCD": 0, "NSC": 0, "FEW": 0.25, "SCT": 0.5, "BKN": 0.75, "OVC": 1.0, "VV": 1.0}


def states_near(lat, lon, km=480.0) -> list:
    """US state codes of radar sites within `km` (the radar network doubles as a state lookup)."""
    out = set()
    for s in all_sites().values():
        if s.country in ("USA", "US", "") and len(s.state) == 2 and haversine_km(lat, lon, s.lat, s.lon) <= km:
            out.add(s.state)
    return sorted(out)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _t(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_currents(js: dict, now=None, max_age_h=2.0) -> list:
    now = now or datetime.now(timezone.utc)
    out = []
    for r in js.get("data", []) or []:
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        t = _t(r.get("utc_valid"))
        if lat is None or lon is None or t is None or now - t > timedelta(hours=max_age_h):
            continue
        out.append(dict(id=str(r.get("station") or ""), name=str(r.get("name") or ""), lat=lat, lon=lon, time=t,
                        tmpf=_num(r.get("tmpf")), dwpf=_num(r.get("dwpf")), sknt=_num(r.get("sknt")),
                        drct=_num(r.get("drct")), gust=_num(r.get("gust")), mslp=_num(r.get("mslp")),
                        alti=_num(r.get("alti")), vsby=_num(r.get("vsby")), sky=str(r.get("skyc1") or ""),
                        wx=str(r.get("wxcodes") or "") if r.get("wxcodes") else "", raw=str(r.get("raw") or "")))
    return out


def describe(o) -> str:
    lines = [f"{o['id']} – {o['name']}" if o["name"] else o["id"]]
    if o["tmpf"] is not None:
        lines.append(f"Temperature {o['tmpf']:.0f}°F" + (f", dew point {o['dwpf']:.0f}°F" if o["dwpf"] is not None
                                                          else ""))
    if o["sknt"] is not None:
        if o["sknt"] < 1:
            lines.append("Wind calm")
        else:
            d = f"{o['drct']:.0f}°" if o["drct"] is not None else "variable"
            lines.append(f"Wind {d} at {o['sknt']:.0f} kt" + (f", gusting {o['gust']:.0f} kt" if o["gust"] else ""))
    if o["mslp"] is not None:
        lines.append(f"Pressure {o['mslp']:.1f} mb")
    elif o["alti"] is not None:
        lines.append(f"Altimeter {o['alti']:.2f} inHg")
    if o["vsby"] is not None:
        lines.append(f"Visibility {o['vsby']:g} mi")
    lines.append(f"Observed {o['time']:%H:%MZ}")
    if o["raw"]:
        lines.append(o["raw"])
    return "\n".join(lines)


def barb_lines(cx, cy, drct, kts, length=22.0):
    """Wind barb as (lines, pennant triangles) in screen pixels; the staff points into the wind."""
    a = math.radians(drct)
    ux, uy = math.sin(a), -math.cos(a)           # screen: up is -y
    tip = (cx + ux * length, cy + uy * length)
    lines = [QLineF(cx, cy, *tip)]
    tris = []
    px, py = -uy, ux                              # perpendicular (barbs on the clockwise side)
    k = int(round(kts / 5.0)) * 5
    pos = 0.0
    step = 4.0
    while k >= 50:
        bx, by = tip[0] - ux * pos, tip[1] - uy * pos
        nx, ny = tip[0] - ux * (pos + 6), tip[1] - uy * (pos + 6)
        tris.append(QPolygonF([QPointF(bx, by), QPointF(bx + px * 10, by + py * 10), QPointF(nx, ny)]))
        pos += 7.0
        k -= 50
    while k >= 10:
        bx, by = tip[0] - ux * pos, tip[1] - uy * pos
        lines.append(QLineF(bx, by, bx + px * 10 + ux * 3, by + py * 10 + uy * 3))
        pos += step
        k -= 10
    if k >= 5:
        if pos == 0:
            pos = step
        bx, by = tip[0] - ux * pos, tip[1] - uy * pos
        lines.append(QLineF(bx, by, bx + px * 5 + ux * 1.5, by + py * 5 + uy * 1.5))
    return lines, tris


class SurfaceObsOverlay(BackgroundLayer):
    title = "Surface observations"

    def __init__(self, settings, view, is_live, parent=None):
        super().__init__(settings, parent, tick_ms=30_000)
        self.view = view
        self.is_live = is_live
        self.obs: list = []
        self._center = None
        self._xy = None
        self._shown = []

    def enabled(self):
        return bool(self.settings["overlays"].get("surface_obs", False)) and self.is_live()

    def jobs(self):
        if not self.enabled():
            return []
        center = (self.view.lat0, self.view.lon0)
        if center != self._center:
            self._next.pop("obs", None)

        def work():
            states = states_near(*center) or []
            got, errors = [], 0
            for st in states:
                try:
                    r = requests.get(CURRENTS_URL, params={"network": f"{st}_ASOS"}, headers=UA, timeout=25)
                    r.raise_for_status()
                    got += parse_currents(r.json())
                except Exception:
                    errors += 1
            if states and errors == len(states):
                raise RuntimeError("could not download observations")
            seen, out = set(), []
            for o in got:
                if o["id"] not in seen:
                    seen.add(o["id"])
                    out.append(o)
            self.obs = out
            self._center = center
            self._xy = None
        return [("obs", work, 10 * 60)]

    def _project(self, view):
        if self._xy is None or self._xy[0] != (view.lat0, view.lon0, len(self.obs)):
            obs = list(self.obs)
            if obs:
                x, y = aeqd_forward(np.array([o["lat"] for o in obs]), np.array([o["lon"] for o in obs]),
                                    view.lat0, view.lon0)
            else:
                x = y = np.zeros(0)
            self._xy = ((view.lat0, view.lon0, len(self.obs)), obs, np.asarray(x), np.asarray(y))
        return self._xy

    def paint(self, painter, vt, panel, view):
        if not self.enabled():
            return
        _k, obs, x, y = self._project(view)
        self._shown = []
        if not obs or vt.km_across > 2500:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=10)
        cell = 64.0 if vt.km_across > 600 else 48.0
        taken = set()
        font = ui_font(8, True)
        painter.setFont(font)
        for i in np.argsort([-(o["sknt"] or 0) for o in obs]).tolist():     # windiest first wins a cell
            if not (x0 <= x[i] <= x1 and y0 <= y[i] <= y1):
                continue
            sx, sy = vt.to_screen(float(x[i]), float(y[i]))
            c = (int(sx // cell), int(sy // cell))
            if c in taken:
                continue
            taken.add(c)
            o = obs[i]
            self._shown.append((float(x[i]), float(y[i]), o))
            self._station(painter, view, sx, sy, o, font)

    def _station(self, painter, view, sx, sy, o, font):
        if o["sknt"] is not None and o["sknt"] >= 2.5 and o["drct"] is not None:
            lines, tris = barb_lines(sx, sy, o["drct"], o["sknt"])
            painter.setPen(QPen(QColor(0, 0, 0, 200), 3.2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLines(lines)
            painter.setPen(QPen(QColor(235, 235, 235), 1.4, Qt.SolidLine, Qt.RoundCap))
            painter.drawLines(lines)
            painter.setBrush(QColor(235, 235, 235))
            for t in tris:
                painter.drawPolygon(t)
        frac = SKY_FRAC.get(o["sky"][:3], None)
        painter.setPen(QPen(QColor(235, 235, 235), 1.2))
        painter.setBrush(QColor(20, 20, 20, 200))
        painter.drawEllipse(QPointF(sx, sy), 4.5, 4.5)
        if frac:
            painter.setBrush(QColor(235, 235, 235))
            painter.setPen(Qt.NoPen)
            painter.drawPie(int(sx - 4.5), int(sy - 4.5), 9, 9, 90 * 16, -int(360 * 16 * frac))
        if o["tmpf"] is not None:
            view._halo_text(painter, sx - 26, sy - 4, f"{o['tmpf']:.0f}", QColor(255, 110, 110), font)
        if o["dwpf"] is not None:
            view._halo_text(painter, sx - 26, sy + 12, f"{o['dwpf']:.0f}", QColor(110, 230, 110), font)

    def hover(self, x, y, tol):
        if not self.enabled():
            return None
        best, bd = None, tol * 1.6
        for ox, oy, o in self._shown:
            d = math.hypot(ox - x, oy - y)
            if d < bd:
                best, bd = o, d
        return describe(best) if best else None
