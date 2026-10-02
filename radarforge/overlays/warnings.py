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
from ..data import feeds

UA = {"User-Agent": "RadarForge/1.0 (NEXRAD viewer)", "Accept": "application/geo+json"}

# Default outline colours: the National Weather Service hazard map colours (weather.gov/help-map).
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


# Warning lines: every warning type and threat level has a code and its own line - colour,
# width (px) and kind: "solid", "center" (black centre line) or "double". Defaults use the NWS
# colours; the NWS has no separate colours for threat levels, so higher levels are told apart
# by the line style.
#   code, event, label, (colour, width, kind), priority
VARIANTS = [
    ("TORE", "Tornado Emergency", "Tornado - Emergency", ("#ff0000", 6.0, "double"), 14),
    ("TORP", "Tornado Warning", "Tornado - PDS", ("#ff0000", 4.0, "center"), 13),
    ("TORR", "Tornado Warning", "Tornado - Reported", ("#ff0000", 4.0, "solid"), 12),
    ("TOR", "Tornado Warning", "Tornado", ("#ff0000", 3.0, "solid"), 11),
    ("EWW", "Extreme Wind Warning", "Extreme Wind", ("#ff8c00", 3.0, "solid"), 10),
    ("FFWE", "Flash Flood Emergency", "Flash Flood - Emergency", ("#8b0000", 5.5, "double"), 9.5),
    ("SVRD", "Severe Thunderstorm Warning", "Severe Thunderstorm - Destructive", ("#ffa500", 4.0, "center"), 9),
    ("SVRC", "Severe Thunderstorm Warning", "Severe Thunderstorm - Considerable", ("#ffa500", 3.5, "solid"), 8.5),
    ("SVR", "Severe Thunderstorm Warning", "Severe Thunderstorm", ("#ffa500", 2.5, "solid"), 8),
    ("FFWC", "Flash Flood Warning", "Flash Flood - Considerable", ("#8b0000", 3.5, "solid"), 7.5),
    ("FFW", "Flash Flood Warning", "Flash Flood", ("#8b0000", 2.5, "solid"), 7),
    ("SMW", "Special Marine Warning", "Special Marine", ("#ffa500", 2.0, "solid"), 6),
    ("SQW", "Snow Squall Warning", "Snow Squall", ("#c71585", 2.5, "solid"), 6),
    ("DSW", "Dust Storm Warning", "Dust Storm", ("#ffe4c4", 2.0, "solid"), 5),
    ("SPS", "Special Weather Statement", "Special Weather Statement", ("#ffe4b5", 1.5, "solid"), 2),
    ("TOA", "Tornado Watch", "Tornado Watch", ("#ffff00", 1.5, "solid"), 1),
    ("SVA", "Severe Thunderstorm Watch", "Severe Thunderstorm Watch", ("#db7093", 1.5, "solid"), 1),
]
VARIANT = {v[0]: v for v in VARIANTS}
LINE_KINDS = ("solid", "center", "double")
BASE_CODE = {"Tornado Warning": "TOR", "Tornado Emergency": "TORE", "Severe Thunderstorm Warning": "SVR",
             "Flash Flood Warning": "FFW", "Flash Flood Emergency": "FFWE", "Special Marine Warning": "SMW",
             "Extreme Wind Warning": "EWW", "Snow Squall Warning": "SQW", "Dust Storm Warning": "DSW",
             "Special Weather Statement": "SPS", "Tornado Watch": "TOA", "Severe Thunderstorm Watch": "SVA"}

# "Classic colours" preset: green flash flood, yellow severe, magenta reported/PDS/emergency tornado
CLASSIC_PRESET = {
    "SQW": ("#8080ff", 2.5, "solid"), "SMW": ("#00e0e0", 2.0, "solid"),
    "FFW": ("#00ff00", 2.5, "solid"), "FFWC": ("#00ff00", 3.5, "solid"), "FFWE": ("#00ff00", 5.5, "double"),
    "SVR": ("#ffff00", 2.5, "solid"), "SVRC": ("#ffff00", 3.5, "solid"), "SVRD": ("#ffff00", 4.0, "center"),
    "TOR": ("#ff0000", 3.0, "solid"), "TORR": ("#ff00ff", 3.5, "solid"), "TORP": ("#ff00ff", 4.0, "center"),
    "TORE": ("#ff00ff", 6.0, "double"),
}

# event name -> (default rgb, outline width, fill alpha, priority) of the event's base line
STYLES = {}
for _ev, _code in BASE_CODE.items():
    _c, _w, _k = VARIANT[_code][3]
    STYLES[_ev] = (hex_rgb(_c), _w, 45 if _ev.endswith("Watch") else 0, VARIANT[_code][4])

# filter groups (Warnings panel buttons); "WAT" is the same switch as Map -> Watches
FILTERS = [("TOR", "Tornado", ("Tornado Warning", "Tornado Emergency")),
           ("SVR", "Severe", ("Severe Thunderstorm Warning",)),
           ("FFW", "Flood", ("Flash Flood Warning", "Flash Flood Emergency")),
           ("OTH", "Other", ("Special Marine Warning", "Extreme Wind Warning", "Snow Squall Warning",
                             "Dust Storm Warning", "Special Weather Statement")),
           ("WAT", "Watches", ("Tornado Watch", "Severe Thunderstorm Watch"))]
EVENT_GROUP = {ev: key for key, _label, events in FILTERS for ev in events}


def _first(params, key):
    v = params.get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v or "").strip().upper()


def variant_of(event: str, params: dict) -> str:
    """Warning-line code from the NWS impact tags (api.weather.gov parameters, or IEM tags)."""
    tor_det = _first(params, "tornadoDetection") or _first(params, "tornadotag")
    dmg = (_first(params, "tornadoDamageThreat") or _first(params, "thunderstormDamageThreat")
           or _first(params, "flashFloodDamageThreat") or _first(params, "damagetag"))
    if event == "Tornado Emergency":
        return "TORE"
    if event == "Tornado Warning":
        if dmg == "CATASTROPHIC":
            return "TORE"
        if dmg == "CONSIDERABLE" or params.get("is_pds"):
            return "TORP"
        if tor_det == "OBSERVED":
            return "TORR"
        return "TOR"
    if event == "Severe Thunderstorm Warning":
        return {"DESTRUCTIVE": "SVRD", "CONSIDERABLE": "SVRC"}.get(dmg, "SVR")
    if event == "Flash Flood Emergency":
        return "FFWE"
    if event == "Flash Flood Warning":
        return {"CATASTROPHIC": "FFWE", "CONSIDERABLE": "FFWC"}.get(dmg, "FFW")
    return BASE_CODE.get(event, "SPS")


def default_line(code: str) -> tuple:
    """(colour hex, width, kind) the code has by default."""
    return VARIANT.get(code, VARIANT["SPS"])[3]


def line_style(settings, code: str) -> tuple:
    """((r, g, b), width, kind) for a warning-line code: the user's choice, else the default."""
    color, width, kind = default_line(code)
    legacy = settings["warning_colors"] or {}           # 1.5.0 stored one colour per event
    ev = VARIANT.get(code, VARIANT["SPS"])[1]
    if BASE_CODE.get(ev) == code and legacy.get(ev):
        color = legacy[ev]
    o = (settings["warning_lines"] or {}).get(code) or {}
    color = o.get("color", color)
    try:
        width = float(o.get("width", width))
    except (TypeError, ValueError):
        pass
    kind = o.get("kind", kind) if o.get("kind", kind) in LINE_KINDS else kind
    try:
        rgb = hex_rgb(color)
    except ValueError:
        rgb = hex_rgb(default_line(code)[0])
    return rgb, max(0.5, min(width, 12.0)), kind


def warning_color(settings, event_or_code: str) -> tuple:
    """The outline colour for a warning-line code (or an event's base line)."""
    code = event_or_code if event_or_code in VARIANT else BASE_CODE.get(event_or_code, "SPS")
    return line_style(settings, code)[0]


VTEC_EVENT = {("TO", "W"): "Tornado Warning", ("SV", "W"): "Severe Thunderstorm Warning",
              ("FF", "W"): "Flash Flood Warning", ("MA", "W"): "Special Marine Warning",
              ("EW", "W"): "Extreme Wind Warning", ("SQ", "W"): "Snow Squall Warning",
              ("DS", "W"): "Dust Storm Warning", ("TO", "A"): "Tornado Watch",
              ("SV", "A"): "Severe Thunderstorm Watch"}


class Alert:
    __slots__ = ("event", "rings", "hover", "issued", "expires", "style", "xy", "_proj", "office", "area",
                 "tags", "uid", "variant", "key", "action", "text")

    def __init__(self, event, rings, hover, issued, expires, office="", area="", tags=(), uid="", variant=None,
                 key="", action=""):
        self.event = event
        self.variant = variant if variant in VARIANT else BASE_CODE.get(event, "SPS")
        self.rings = rings          # list of (lon, lat) arrays
        self.hover = hover
        self.issued = issued
        self.expires = expires
        rgb, width, fill, _pri = STYLES.get(event, STYLES["Special Weather Statement"])
        self.style = (rgb, width, fill, VARIANT[self.variant][4])      # priority of the variant
        self.xy = None
        self._proj = None
        self.office = office
        self.area = area
        self.tags = list(tags)
        self.uid = uid or f"{event}|{office}|{issued}"
        self.key = key or self.uid          # the same for every update of one warning (VTEC)
        self.action = action                # VTEC action: NEW, CON, EXT, CAN, EXP...
        self.text = ""                      # the full warning text (live alerts)

    @property
    def variant_label(self):
        return VARIANT[self.variant][2]

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
        if ev == "Tornado Warning" and ("TORNADO EMERGENCY" in desc.upper() or
                                        _first(params, "tornadoDamageThreat") == "CATASTROPHIC"):
            ev = "Tornado Emergency"
        if ev == "Flash Flood Warning" and ("FLASH FLOOD EMERGENCY" in desc.upper() or
                                            _first(params, "flashFloodDamageThreat") == "CATASTROPHIC"):
            ev = "Flash Flood Emergency"
        variant = variant_of(ev, params)
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
        hover = (f"{VARIANT[variant][2]} ({variant})\n{p.get('senderName', '')}\n"
                 f"Issued {p.get('sent', '')[:16]}  Expires {p.get('expires', '')[:16]}")
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
        vt = params.get("VTEC") or []
        action, key = feeds.vtec_key(vt[0] if isinstance(vt, (list, tuple)) and vt else str(vt))
        alert = Alert(ev, rings, hover, _t(p.get("sent")), _t(p.get("expires")), office,
                      p.get("areaDesc") or "", short, p.get("id") or "", variant, key, action)
        alert.text = "\n\n".join(x.strip() for x in (p.get("headline") or "", p.get("description") or "",
                                                       p.get("instruction") or "") if x and x.strip())
        out.append(alert)
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
        dmg = str(p.get("damagetag") or "").upper()
        if ev == "Tornado Warning" and (p.get("is_emergency") or dmg == "CATASTROPHIC"):
            ev = "Tornado Emergency"
        if ev == "Flash Flood Warning" and (p.get("is_emergency") or dmg == "CATASTROPHIC"):
            ev = "Flash Flood Emergency"
        variant = variant_of(ev, p)
        rings = _rings(f.get("geometry"))
        if not rings:
            continue
        hover = (f"{VARIANT[variant][2]} ({variant}) #{p.get('eventid')} ({p.get('wfo')})\n"
                 f"{p.get('polygon_begin', '')} → {p.get('polygon_end', '')}")
        for k in ("hailtag", "windtag", "tornadotag", "damagetag"):
            if p.get(k):
                hover += f"\n{k}: {p[k]}"
        short = [f"{k[:-3]} {p[k]}" for k in ("tornadotag", "hailtag", "windtag", "damagetag") if p.get(k)]
        out.append(Alert(ev, rings, hover, _t(p.get("polygon_begin")), _t(p.get("polygon_end")),
                         p.get("wfo") or "", f"#{p.get('eventid')}", short,
                         f"{p.get('wfo')}.{p.get('phenomena')}.{p.get('significance')}.{p.get('eventid')}", variant))
    return out


def fetch_lsr(start: datetime, end: datetime, lat0=None, lon0=None, radius_deg=5.0) -> list:
    """NWS local storm reports between start and end (the feed covers the whole country)."""
    params = {"sts": start.strftime("%Y-%m-%dT%H:%MZ"), "ets": end.strftime("%Y-%m-%dT%H:%MZ")}
    r = requests.get(feeds.LSR_URL, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return feeds.parse_lsr(r.json())


def fetch_sn_reports() -> list:
    """Spotter Network reports (recent, live only)."""
    r = requests.get(feeds.SN_REPORTS, headers={"User-Agent": UA["User-Agent"]}, timeout=20)
    r.raise_for_status()
    return feeds.parse_sn_reports(r.text)


def report_on(settings, r) -> bool:
    """Whether a storm report's type is switched on (Map -> Storm report options)."""
    group = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])[3]
    return bool((settings["report_types"] or {}).get(group, group != "other"))


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
        self.force_alerts = False        # location alerts need the warnings even when their layer is off
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
        want_w = ov.get("warnings", True) or ov.get("watches", True) or self.force_alerts
        want_r = ov.get("reports", False)
        if self._busy or not (want_w or want_r):
            return
        if not force and self.mode == "archive":
            return
        self._busy = True
        mode, t = self.mode, self.archive_time

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
                    hours = int(self.settings["report_hours"] or 3)
                    if mode == "live":
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(hours=hours)
                    else:
                        start, end = t - timedelta(hours=2), t + timedelta(minutes=30)
                    key = (mode, start.strftime("%Y%m%d%H"), hours)
                    if key != self._report_key or mode == "live":
                        reps = fetch_lsr(start, end)
                        if mode == "live" and self.settings["spotter_reports"]:
                            try:
                                reps += [r for r in fetch_sn_reports() if r["time"] is None or r["time"] >= start]
                            except Exception as exc:
                                print("Spotter Network reports:", exc)
                        self.reports = reps
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

    def color(self, a) -> tuple:
        """Line colour of an alert (or of an event / code name)."""
        if isinstance(a, str):
            return warning_color(self.settings, a)
        return line_style(self.settings, a.variant)[0]

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
        x0, y0, x1, y1 = vt.world_bounds(pad=10)
        for a in list(self.alerts):
            if not self.visible(a) or not self._in_time(a):
                continue
            is_watch = a.event.endswith("Watch")
            _rgb, _w, fill, _pri = a.style
            rgb, width, kind = line_style(self.settings, a.variant)
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
                draw_line(painter, poly, rgb, width, kind, halo=not is_watch)
                if a.uid == self.selected_uid:
                    painter.setPen(QPen(QColor(255, 255, 255), width + 1.5, Qt.DashLine))
                    painter.drawPolygon(poly)
        if ov.get("reports", False):
            now = datetime.now(timezone.utc)
            window = max(2, int(self.settings["report_hours"] or 3)) * 3600.0
            f1, f2 = ui_font(8, True), ui_font(6, True)
            # oldest first, so the newest end up on top
            for r in sorted(self.visible_reports(), key=lambda r: r["time"] or now):
                if "xy" not in r or r.get("_p") != (view.lat0, view.lon0):
                    x, y = aeqd_forward(r["lat"], r["lon"], view.lat0, view.lon0)
                    r["xy"] = (float(x), float(y))
                    r["_p"] = (view.lat0, view.lon0)
                if not (x0 <= r["xy"][0] <= x1 and y0 <= r["xy"][1] <= y1):
                    continue
                sx, sy = vt.to_screen(*r["xy"])
                letter, rgb, _label, _g = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])
                # live reports fade with age: full colour for the first hour, about half at the end of the window
                alpha = 255
                if self.mode == "live" and r["time"] is not None:
                    age = (now - r["time"]).total_seconds()
                    if age > 3600:
                        alpha = int(255 * max(0.45, 1 - 0.55 * (age - 3600) / max(1.0, window - 3600)))
                rad = 8 if len(letter) > 1 else 7
                painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
                painter.setBrush(QColor(*rgb, alpha))
                painter.drawEllipse(QPointF(sx, sy), rad, rad)
                painter.setFont(f2 if len(letter) > 1 else f1)
                painter.setPen(QColor(0, 0, 0, alpha))
                painter.drawText(int(sx - rad), int(sy - rad), 2 * rad, 2 * rad, Qt.AlignCenter, letter)

    def alerts_at(self, lat, lon) -> list:
        """The alerts (that are shown and valid for the displayed time) whose area contains a point, worst first."""
        hits = []
        for a in list(self.alerts):
            if not self.visible(a) or not self._in_time(a):
                continue
            if any(r[:, 1].min() <= lat <= r[:, 1].max() and r[:, 0].min() <= lon <= r[:, 0].max() for r in a.rings) and \
                    feeds.rings_contain([[(float(x), float(y)) for x, y in r] for r in a.rings], lat, lon):
                hits.append(a)
        return sorted(hits, key=lambda a: -a.style[3])

    def visible_reports(self):
        """Reports drawn now: types switched on, and (archive) near the frame's time."""
        t = self.frame_time
        out = []
        for r in list(self.reports):
            if not report_on(self.settings, r):
                continue
            if self.mode == "archive" and t is not None and r["time"] is not None:
                if not (t - timedelta(minutes=60) <= r["time"] <= t + timedelta(minutes=5)):
                    continue
            out.append(r)
        return out

    def hover(self, x, y, tol):
        ov = self.settings["overlays"]
        if ov.get("reports", False):
            for r in self.visible_reports():
                if "xy" in r and math.hypot(r["xy"][0] - x, r["xy"][1] - y) < tol * 1.2:
                    return r["hover"]
        best = None
        for a in list(self.alerts):
            # only what is drawn: a hidden watch (or warning type) must not pop up its text
            if a.xy is None or not self.visible(a) or not self._in_time(a):
                continue
            # only on the outline, so the text doesn't cover the storm while you look inside the box
            if any(near_edge(x, y, xy, tol) for xy in a.xy):
                if best is None or a.style[3] > best.style[3]:
                    best = a
        return best.hover if best else None


def draw_line(painter, shape, rgb, width, kind, halo=True):
    """A warning line: dark halo, the colour, then a black centre for "center" / "double"."""
    poly = isinstance(shape, QPolygonF)
    draw = painter.drawPolygon if poly else painter.drawLine
    args = (shape,) if poly else shape
    if halo:
        painter.setPen(QPen(QColor(0, 0, 0, 220), width + 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        draw(*args)
    painter.setPen(QPen(QColor(*rgb), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    draw(*args)
    inner = {"center": 0.25, "double": 0.46}.get(kind, 0.0) * width
    if inner > 0:
        painter.setPen(QPen(QColor(0, 0, 0), max(1.0, inner), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        draw(*args)


def near_edge(x, y, poly, tol) -> bool:
    """Whether (x, y) is within tol of a polygon's outline (poly: N x 2 array, km)."""
    xs, ys = poly[:, 0], poly[:, 1]
    if len(xs) < 2 or x < xs.min() - tol or x > xs.max() + tol or y < ys.min() - tol or y > ys.max() + tol:
        return False
    a = poly
    b = np.roll(poly, -1, axis=0)                 # each vertex to the next (closes the ring)
    ab = b - a
    ap = np.array([x, y]) - a
    den = (ab * ab).sum(1)
    t = np.clip((ap * ab).sum(1) / np.where(den > 0, den, 1.0), 0.0, 1.0)
    d = ap - ab * t[:, None]
    return bool(((d * d).sum(1)).min() <= tol * tol)


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
