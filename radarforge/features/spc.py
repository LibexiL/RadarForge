"""SPC day 1 convective outlook and mesoscale discussions (via the Iowa Environmental Mesonet API)."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np
import requests
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPolygonF

from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font
from . import feeds
from .warnings import draw_line, near_edge

UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)", "Accept": "application/geo+json"}


def _get_json(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_outlook(now=None) -> list:
    """The newest day 1 outlook (tries the most recent issuances until one has areas)."""
    now = now or datetime.now(timezone.utc)
    last_exc, got = None, None
    for date, cycle in feeds.outlook_requests(now):
        try:
            areas = feeds.parse_outlook(_get_json(feeds.OUTLOOK_URL, {"day": 1, "valid": date, "cycle": cycle}))
        except Exception as exc:
            last_exc = exc
            continue
        if areas:
            return areas
        if got is None:
            got = areas
    if got is None and last_exc is not None:
        raise last_exc
    return got or []


def fetch_mcds() -> list:
    now = datetime.now(timezone.utc)
    return [m for m in feeds.parse_mcd(_get_json(feeds.MCD_URL)) if m["expire"] is None or m["expire"] > now]


class SpcOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, is_live, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.is_live = is_live
        self.outlook: list = []
        self.mcds: list = []
        self._next = {"outlook": 0.0, "mcd": 0.0}
        self._busy = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(30_000)

    def _on(self, key):
        return bool(self.settings["overlays"].get(key, False)) and self.is_live()

    def refresh(self, force=False):
        jobs = []
        if self._on("spc_outlook") and (force or time.time() >= self._next["outlook"]):
            jobs.append(("outlook", fetch_outlook, 15 * 60))
        if self._on("spc_mcd") and (force or time.time() >= self._next["mcd"]):
            jobs.append(("mcd", fetch_mcds, 3 * 60))
        for name, fn, period in jobs:
            if name in self._busy:
                continue
            self._busy.add(name)

            def work(name=name, fn=fn, period=period):
                try:
                    data = fn()
                    if name == "outlook":
                        self.outlook = data
                    else:
                        self.mcds = data
                    self._next[name] = time.time() + period
                except Exception as exc:
                    self._next[name] = time.time() + 60
                    self.status.emit(f"SPC {'outlook' if name == 'outlook' else 'discussions'} unavailable: {exc}")
                finally:
                    self._busy.discard(name)
                    self.changed.emit()
            threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- drawing
    @staticmethod
    def _proj(item, view):
        if item.get("_p") != (view.lat0, view.lon0):
            xy = []
            for ring in item["rings"]:
                a = np.asarray(ring, float)
                x, y = aeqd_forward(a[:, 1], a[:, 0], view.lat0, view.lon0)
                xy.append(np.stack([x, y], 1))
            item["xy"] = xy
            item["_p"] = (view.lat0, view.lon0)
        return item["xy"]

    def _poly(self, vt, xy, bounds):
        x0, y0, x1, y1 = bounds
        if xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1:
            return None
        sx, sy = vt.to_screen(xy[:, 0], xy[:, 1])
        return QPolygonF([QPointF(a, b) for a, b in zip(sx.tolist(), sy.tolist())])

    def paint(self, painter, vt, panel, view):
        bounds = vt.world_bounds(pad=10)
        font = ui_font(8, True)
        if self._on("spc_outlook"):
            cats = [a for a in self.outlook if a["category"] == "CATEGORICAL" and a["threshold"] in feeds.CATEGORIES]
            cats.sort(key=lambda a: feeds.CATEGORIES.index(a["threshold"]))
            painter.setBrush(Qt.NoBrush)
            for a in cats:
                rgb = feeds.CAT_RGB[a["threshold"]]
                for xy in self._proj(a, view):
                    poly = self._poly(vt, xy, bounds)
                    if poly is None:
                        continue
                    draw_line(painter, poly, rgb, 2.0, "solid", halo=True)
                    # label rings big enough to need one, just inside their northernmost point
                    if (xy[:, 0].max() - xy[:, 0].min()) * vt.scale > 60:
                        i = int(np.argmax(xy[:, 1]))
                        lx, ly = vt.to_screen(float(xy[i, 0]), float(xy[i, 1]))
                        view._halo_text(painter, lx - 12, ly + 16, a["threshold"], QColor(*rgb), font)
        if self._on("spc_mcd"):
            for m in self.mcds:
                for xy in self._proj(m, view):
                    poly = self._poly(vt, xy, bounds)
                    if poly is None:
                        continue
                    draw_line(painter, poly, feeds.MCD_RGB, 2.2, "solid", halo=True)
                    i = int(np.argmax(xy[:, 1]))
                    lx, ly = vt.to_screen(float(xy[i, 0]), float(xy[i, 1]))
                    view._halo_text(painter, lx - 22, ly - 6, f"MD {m['number']}", QColor(*feeds.MCD_RGB), font)

    # ---------------------------------------------------------------- hover / lookup
    def mcd_at(self, lat, lon):
        if not self._on("spc_mcd"):
            return None
        return next((m for m in self.mcds if feeds.rings_contain(m["rings"], lat, lon)), None)

    def outlook_at(self, lat, lon):
        if not self._on("spc_outlook"):
            return None
        return feeds.outlook_at(self.outlook, lat, lon)

    @staticmethod
    def describe_mcd(m) -> str:
        txt = f"SPC Mesoscale Discussion {m['number']}"
        if m["concerning"]:
            txt += "\n" + m["concerning"].capitalize()
        if m["expire"] is not None:
            txt += f"\nUntil {feeds.local_hm(m['expire'])}"
        if m["watch"] is not None:
            txt += f"\nChance of a watch: {m['watch']}%"
        return txt + "\n(right-click inside it → read the discussion)"

    def describe_outlook(self, lat, lon, cat=None) -> str | None:
        """The outlook category (the hovered line's, if given) and the chances at a point."""
        o = feeds.outlook_at(self.outlook, lat, lon)
        if o is None and cat is None:
            return None
        cat = cat or o["cat"]

        def p(key, floor):
            v = o.get(key.lower()) if o else None
            s = f"{round(v * 100)}%" if v is not None else f"under {floor}%"
            return s + (" (significant)" if o and key in o["sig"] else "")
        txt = f"SPC day 1 outlook: {feeds.CAT_NAME[cat]}"
        txt += f"\nTornado {p('TORNADO', 2)} · Wind {p('WIND', 5)} · Hail {p('HAIL', 5)}"
        exp = (o or {}).get("expire") or next((a["expire"] for a in self.outlook if a["expire"]), None)
        if exp is not None:
            txt += f"\nValid until {feeds.local_hm(exp)}"
        return txt

    def describe(self, lat, lon) -> str | None:
        """Everything SPC at a point (inside the shapes): used by the right-click menu."""
        m = self.mcd_at(lat, lon)
        if m is not None:
            return self.describe_mcd(m)
        return self.describe_outlook(lat, lon) if self.outlook_at(lat, lon) is not None else None

    def hover(self, x, y, tol, view=None):
        """Text for the outline under the mouse (like warnings: only on the line, not inside)."""
        view = view or self._view
        if view is None:
            return None
        lat, lon = view.world_to_latlon(x, y)
        if self._on("spc_mcd"):
            for m in self.mcds:
                if m.get("_p") == (view.lat0, view.lon0) and any(near_edge(x, y, xy, tol) for xy in m["xy"]):
                    return self.describe_mcd(m)
        if self._on("spc_outlook"):
            hit = None
            for a in self.outlook:
                if a["category"] != "CATEGORICAL" or a["threshold"] not in feeds.CATEGORIES:
                    continue
                if a.get("_p") == (view.lat0, view.lon0) and any(near_edge(x, y, xy, tol) for xy in a["xy"]):
                    if hit is None or feeds.CATEGORIES.index(a["threshold"]) > feeds.CATEGORIES.index(hit["threshold"]):
                        hit = a
            if hit is not None:
                return self.describe_outlook(lat, lon, hit["threshold"])
        return None

    _view = None
