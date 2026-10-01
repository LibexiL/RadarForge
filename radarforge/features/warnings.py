"""NWS warnings/watches (live: api.weather.gov, archive: IEM) and local storm reports."""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font

UA = {"User-Agent": "RadarForge/1.0 (NEXRAD viewer)", "Accept": "application/geo+json"}

# Default outline colours: the National Weather Service hazard map colours (weather.gov/help-map).
# Tornado / flash flood emergencies have no NWS colour of their own, so by default they use the
# warning's colour and are drawn thicker. Every colour can be changed in Settings -> Warnings.
NWS_COLORS = {
    "Tornado Warning": "#ff0000",
    "Tornado Emergency": "#ff0000",
    "Severe Thunderstorm Warning": "#ffa500",
    "Flash Flood Warning": "#8b0000",
    "Flash Flood Emergency": "#8b0000",
    "Special Marine Warning": "#ffa500",
    "Extreme Wind Warning": "#ff8c00",
    "Snow Squall Warning": "#c71585",
    "Dust Storm Warning": "#ffe4c4",
    "Special Weather Statement": "#ffe4b5",
    "Tornado Watch": "#ffff00",
    "Severe Thunderstorm Watch": "#db7093",
}


def hex_rgb(text: str) -> tuple:
    """'#rrggbb' or '#rrggbbaa' -> (r, g, b)."""
    t = str(text).strip().lstrip("#")
    if len(t) not in (6, 8):
        raise ValueError(f"not a colour: {text}")
    return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)


# event name -> (default rgb, outline width, fill alpha, priority)
STYLES = {
    ev: (hex_rgb(NWS_COLORS[ev]), width, fill, pri) for ev, width, fill, pri in (
        ("Tornado Warning", 3.0, 0, 10),
        ("Tornado Emergency", 4.0, 0, 11),
        ("Severe Thunderstorm Warning", 2.5, 0, 8),
        ("Flash Flood Warning", 2.5, 0, 7),
        ("Flash Flood Emergency", 3.5, 0, 9),
        ("Special Marine Warning", 2.0, 0, 6),
        ("Extreme Wind Warning", 3.0, 0, 10),
        ("Snow Squall Warning", 2.5, 0, 6),
        ("Dust Storm Warning", 2.0, 0, 5),
        ("Special Weather Statement", 1.5, 0, 2),
        ("Tornado Watch", 1.5, 45, 1),
        ("Severe Thunderstorm Watch", 1.5, 45, 1),
    )
}

# filter groups (Warnings panel buttons); "WAT" is the same switch as Map -> Watches
FILTERS = [("TOR", "Tornado", ("Tornado Warning", "Tornado Emergency")),
           ("SVR", "Severe", ("Severe Thunderstorm Warning",)),
           ("FFW", "Flood", ("Flash Flood Warning", "Flash Flood Emergency")),
           ("OTH", "Other", ("Special Marine Warning", "Extreme Wind Warning", "Snow Squall Warning",
                             "Dust Storm Warning", "Special Weather Statement")),
           ("WAT", "Watches", ("Tornado Watch", "Severe Thunderstorm Watch"))]
EVENT_GROUP = {ev: key for key, _label, events in FILTERS for ev in events}


def warning_color(settings, event: str) -> tuple:
    """The outline colour for an event: the user's choice, else the NWS colour."""
    custom = (settings["warning_colors"] or {}).get(event)
    if custom:
        try:
            return hex_rgb(custom)
        except ValueError:
            pass
    return hex_rgb(NWS_COLORS.get(event, "#ffffff"))


VTEC_EVENT = {("TO", "W"): "Tornado Warning", ("SV", "W"): "Severe Thunderstorm Warning",
              ("FF", "W"): "Flash Flood Warning", ("MA", "W"): "Special Marine Warning",
              ("EW", "W"): "Extreme Wind Warning", ("SQ", "W"): "Snow Squall Warning",
              ("DS", "W"): "Dust Storm Warning", ("TO", "A"): "Tornado Watch",
              ("SV", "A"): "Severe Thunderstorm Watch"}


class Alert:
    __slots__ = ("event", "rings", "hover", "issued", "expires", "style", "xy", "_proj", "office", "area",
                 "tags", "uid")

    def __init__(self, event, rings, hover, issued, expires, office="", area="", tags=(), uid=""):
        self.event = event
        self.rings = rings          # list of (lon, lat) arrays
        self.hover = hover
        self.issued = issued
        self.expires = expires
        self.style = STYLES.get(event)
        self.xy = None
        self._proj = None
        self.office = office
        self.area = area
        self.tags = list(tags)
        self.uid = uid or f"{event}|{office}|{issued}"

    def centroid(self):
        pts = np.concatenate(self.rings)
        return float(pts[:, 1].mean()), float(pts[:, 0].mean())      # lat, lon


def _rings(geom):
    if not geom:
        return []
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "Polygon":
        return [np.asarray(c[0], float)]
    if t == "MultiPolygon":
        return [np.asarray(p[0], float) for p in c]
    return []


def _t(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_live_alerts(county_polys: dict) -> list:
    r = requests.get("https://api.weather.gov/alerts/active", params={"status": "actual"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        ev = p.get("event", "")
        params = p.get("parameters") or {}
        if ev not in STYLES:
            continue
        desc = (p.get("description") or "")
        if ev == "Tornado Warning" and "TORNADO EMERGENCY" in desc.upper():
            ev = "Tornado Emergency"
        if ev == "Flash Flood Warning" and "FLASH FLOOD EMERGENCY" in desc.upper():
            ev = "Flash Flood Emergency"
        rings = _rings(f.get("geometry"))
        if not rings and ev.endswith("Watch"):
            for same in (p.get("geocode") or {}).get("SAME", []):
                try:
                    fips = int(same[1:])
                except ValueError:
                    continue
                for ring in county_polys.get(fips, []):
                    rings.append(np.asarray(ring, float))
        if not rings:
            continue
        tags = []
        for k in ("maxHailSize", "maxWindGust", "tornadoDetection", "thunderstormDamageThreat",
                  "tornadoDamageThreat", "flashFloodDamageThreat"):
            if params.get(k):
                tags.append(f"{k}: {', '.join(map(str, params[k]))}")
        hover = f"{ev}\n{p.get('senderName', '')}\nIssued {p.get('sent', '')[:16]}  Expires {p.get('expires', '')[:16]}"
        if tags:
            hover += "\n" + "\n".join(tags)
        if p.get("headline"):
            hover += "\n" + p["headline"]
        short = []
        for k, lab in (("tornadoDetection", ""), ("maxHailSize", "hail "), ("maxWindGust", "wind "),
                       ("tornadoDamageThreat", ""), ("thunderstormDamageThreat", ""), ("flashFloodDamageThreat", "")):
            if params.get(k):
                short.append(f"{lab}{params[k][0]}")
        office = (p.get("senderName") or "").replace("NWS ", "")
        out.append(Alert(ev, rings, hover, _t(p.get("sent")), _t(p.get("expires")), office,
                         p.get("areaDesc") or "", short, p.get("id") or ""))
    return out


def fetch_archive_alerts(ts: datetime) -> list:
    r = requests.get("https://mesonet.agron.iastate.edu/geojson/sbw.geojson",
                     params={"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        ev = VTEC_EVENT.get((p.get("phenomena"), p.get("significance")))
        if ev is None:
            continue
        if ev == "Tornado Warning" and p.get("is_emergency"):
            ev = "Tornado Emergency"
        rings = _rings(f.get("geometry"))
        if not rings:
            continue
        hover = f"{ev} #{p.get('eventid')} ({p.get('wfo')})\n{p.get('polygon_begin', '')} → {p.get('polygon_end', '')}"
        for k in ("hailtag", "windtag", "tornadotag", "damagetag"):
            if p.get(k):
                hover += f"\n{k}: {p[k]}"
        short = [f"{k[:-3]} {p[k]}" for k in ("tornadotag", "hailtag", "windtag", "damagetag") if p.get(k)]
        out.append(Alert(ev, rings, hover, _t(p.get("polygon_begin")), _t(p.get("polygon_end")),
                         p.get("wfo") or "", f"#{p.get('eventid')}", short,
                         f"{p.get('wfo')}.{p.get('phenomena')}.{p.get('significance')}.{p.get('eventid')}"))
    return out


LSR_STYLE = {"TORNADO": ("T", (255, 40, 40)), "FUNNEL CLOUD": ("F", (255, 150, 150)),
             "HAIL": ("H", (60, 220, 60)), "TSTM WND DMG": ("W", (80, 160, 255)),
             "TSTM WND GST": ("G", (120, 200, 255)), "NON-TSTM WND DMG": ("W", (150, 150, 255)),
             "FLASH FLOOD": ("F", (0, 200, 120)), "FLOOD": ("F", (0, 160, 100)),
             "WALL CLOUD": ("C", (220, 220, 220))}


def fetch_lsr(start: datetime, end: datetime, lat0, lon0, radius_deg=5.0) -> list:
    params = {"sts": start.strftime("%Y-%m-%dT%H:%MZ"), "ets": end.strftime("%Y-%m-%dT%H:%MZ"),
              "west": lon0 - radius_deg * 1.3, "east": lon0 + radius_deg * 1.3,
              "south": lat0 - radius_deg, "north": lat0 + radius_deg}
    r = requests.get("https://mesonet.agron.iastate.edu/geojson/lsr.geojson", params=params, headers=UA,
                     timeout=30)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        mag = p.get("magnitude") or p.get("magf") or ""
        hover = f"{p.get('typetext', '')} {mag} {p.get('unit', '') or ''}\n{p.get('city', '')}, {p.get('state', '')} " \
                f"{p.get('valid', '')}\n{(p.get('remark') or '')[:300]}"
        out.append(dict(lat=lat, lon=lon, type=(p.get("typetext") or "").upper(), hover=hover,
                        time=_t(p.get("valid"))))
    return out


class WarningsOverlay(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, county_polys_fn, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.county_polys_fn = county_polys_fn
        self.alerts: list = []
        self.reports: list = []
        self.mode = "live"
        self.archive_time = None
        self._last_fetch = 0.0
        self._busy = False
        self._report_key = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)
        self.frame_time = None
        self.center = (35.0, -97.0)

    def set_live(self):
        self.mode = "live"
        self.refresh(force=True)

    def set_archive_time(self, t: datetime):
        self.mode = "archive"
        if self.archive_time is None or abs((t - self.archive_time).total_seconds()) >= 120:
            self.archive_time = t
            self.refresh(force=True)

    def refresh(self, force=False):
        ov = self.settings["overlays"]
        want_w = ov.get("warnings", True) or ov.get("watches", True)
        want_r = ov.get("reports", False)
        if self._busy or not (want_w or want_r):
            return
        if not force and self.mode == "archive":
            return
        self._busy = True
        mode, t, center = self.mode, self.archive_time, self.center

        def work():
            try:
                if want_w:
                    if mode == "live":
                        alerts = fetch_live_alerts(self.county_polys_fn())
                    else:
                        alerts = fetch_archive_alerts(t) if t else []
                    alerts.sort(key=lambda a: a.style[3])
                    self.alerts = alerts
                if want_r:
                    if mode == "live":
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(hours=3)
                    else:
                        start, end = t - timedelta(hours=2), t + timedelta(minutes=30)
                    key = (mode, start.strftime("%Y%m%d%H"), round(center[0], 1), round(center[1], 1))
                    if key != self._report_key or mode == "live":
                        self.reports = fetch_lsr(start, end, *center)
                        self._report_key = key
                self.status.emit(f"Warnings: {len(self.alerts)} active" + (f", {len(self.reports)} reports"
                                                                          if want_r else ""))
            except Exception as exc:
                self.status.emit(f"Warnings unavailable: {exc}")
            finally:
                self._busy = False
                self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- drawing
    def _xy(self, a, lat0, lon0):
        if a._proj != (lat0, lon0):
            a.xy = [np.stack(aeqd_forward(r[:, 1], r[:, 0], lat0, lon0), 1) for r in a.rings]
            a._proj = (lat0, lon0)
        return a.xy

    selected_uid = None

    def color(self, event: str) -> tuple:
        return warning_color(self.settings, event)

    def visible(self, a) -> bool:
        """Whether an alert's type is switched on (Map menu and the Warnings panel buttons)."""
        ov = self.settings["overlays"]
        if a.event.endswith("Watch"):
            return bool(ov.get("watches", True))
        if not ov.get("warnings", True):
            return False
        return bool((self.settings["warning_types"] or {}).get(EVENT_GROUP.get(a.event, "OTH"), True))

    def _in_time(self, a) -> bool:
        t = self.frame_time
        if self.mode == "archive" and t is not None and a.issued and a.expires:
            return a.issued <= t <= a.expires
        return True

    def active_alerts(self):
        """Alerts valid for the displayed time (all current alerts in live mode)."""
        t = self.frame_time
        out = []
        for a in list(self.alerts):
            if self.mode == "archive" and t is not None and a.issued and a.expires:
                if not (a.issued <= t <= a.expires):
                    continue
            out.append(a)
        return out

    def paint(self, painter, vt, panel, view):
        ov = self.settings["overlays"]
        t = self.frame_time
        x0, y0, x1, y1 = vt.world_bounds(pad=10)
        for a in list(self.alerts):
            if not self.visible(a) or not self._in_time(a):
                continue
            is_watch = a.event.endswith("Watch")
            _rgb, width, fill, _pri = a.style
            rgb = self.color(a.event)
            for xy in self._xy(a, view.lat0, view.lon0):
                if xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1:
                    continue
                sx, sy = vt.to_screen(xy[:, 0], xy[:, 1])
                poly = QPolygonF([QPointF(a_, b_) for a_, b_ in zip(sx.tolist(), sy.tolist())])
                if fill:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(*rgb, fill))
                    painter.drawPolygon(poly)
                painter.setBrush(Qt.NoBrush)
                if not is_watch:
                    painter.setPen(QPen(QColor(0, 0, 0, 220), width + 2.5))
                    painter.drawPolygon(poly)
                painter.setPen(QPen(QColor(*rgb), width))
                painter.drawPolygon(poly)
                if a.uid == self.selected_uid:
                    painter.setPen(QPen(QColor(255, 255, 255), width + 1.5, Qt.DashLine))
                    painter.drawPolygon(poly)
        if ov.get("reports", False):
            painter.setFont(ui_font(8, True))
            for r in self.reports:
                if self.mode == "archive" and t is not None and r["time"] is not None:
                    if not (t - timedelta(minutes=60) <= r["time"] <= t + timedelta(minutes=5)):
                        continue
                if "xy" not in r or r.get("_p") != (view.lat0, view.lon0):
                    x, y = aeqd_forward(r["lat"], r["lon"], view.lat0, view.lon0)
                    r["xy"] = (float(x), float(y))
                    r["_p"] = (view.lat0, view.lon0)
                sx, sy = vt.to_screen(*r["xy"])
                letter, rgb = LSR_STYLE.get(r["type"], ("•", (230, 230, 230)))
                painter.setPen(QPen(QColor(0, 0, 0), 1))
                painter.setBrush(QColor(*rgb))
                painter.drawEllipse(QPointF(sx, sy), 7, 7)
                painter.setPen(QColor(0, 0, 0))
                painter.drawText(int(sx - 7), int(sy - 7), 14, 14, Qt.AlignCenter, letter)

    def hover(self, x, y, tol):
        ov = self.settings["overlays"]
        if ov.get("reports", False):
            for r in self.reports:
                if "xy" in r and math.hypot(r["xy"][0] - x, r["xy"][1] - y) < tol * 1.2:
                    return r["hover"]
        best = None
        for a in list(self.alerts):
            # only what is drawn: a hidden watch (or warning type) must not pop up its text
            if a.xy is None or not self.visible(a) or not self._in_time(a):
                continue
            for xy in a.xy:
                if _inside(x, y, xy):
                    if best is None or a.style[3] > best.style[3]:
                        best = a
        return best.hover if best else None


def _inside(x, y, poly):
    xs, ys = poly[:, 0], poly[:, 1]
    if x < xs.min() or x > xs.max() or y < ys.min() or y > ys.max():
        return False
    j = len(xs) - 1
    c = False
    for i in range(len(xs)):
        if ((ys[i] > y) != (ys[j] > y)) and (x < (xs[j] - xs[i]) * (y - ys[i]) / (ys[j] - ys[i] + 1e-12) + xs[i]):
            c = not c
        j = i
    return c
