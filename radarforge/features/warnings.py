"""NWS warnings/watches (live: api.weather.gov, archive: IEM) and local storm reports."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..data.aws import friendly_error
from ..products.geometry import aeqd_forward
from ..render.fonts import ui_font
from . import feeds

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
    # (1.20) winter, cold and tropical: whole counties / zones, shaded
    "Blizzard Warning": "#ff4500",
    "Winter Storm Warning": "#ff69b4",
    "Ice Storm Warning": "#8b008b",
    "Lake Effect Snow Warning": "#008b8b",
    "Winter Storm Watch": "#4682b4",
    "Winter Weather Advisory": "#7b68ee",
    "Freezing Rain Advisory": "#da70d6",
    "Extreme Cold Warning": "#0000ff",
    "Extreme Cold Watch": "#5f9ea0",
    "Cold Weather Advisory": "#afeeee",
    "Freeze Warning": "#483d8b",
    "Frost Advisory": "#6495ed",
    "Hurricane Warning": "#dc143c",
    "Hurricane Watch": "#ff00ff",
    "Tropical Storm Warning": "#b22222",
    "Tropical Storm Watch": "#f08080",
    "Storm Surge Warning": "#b524f7",
    "Storm Surge Watch": "#db7ff7",
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
    # (1.20) issued for whole counties / zones: shaded, under the storm warnings
    ("HUW", "Hurricane Warning", "Hurricane Warning", ("#dc143c", 2.0, "solid"), 4.6),
    ("SSW", "Storm Surge Warning", "Storm Surge Warning", ("#b524f7", 2.0, "solid"), 4.5),
    ("TRW", "Tropical Storm Warning", "Tropical Storm Warning", ("#b22222", 1.8, "solid"), 4.4),
    ("HUA", "Hurricane Watch", "Hurricane Watch", ("#ff00ff", 1.5, "solid"), 4.3),
    ("SSA", "Storm Surge Watch", "Storm Surge Watch", ("#db7ff7", 1.5, "solid"), 4.2),
    ("TRA", "Tropical Storm Watch", "Tropical Storm Watch", ("#f08080", 1.5, "solid"), 4.1),
    ("BZW", "Blizzard Warning", "Blizzard Warning", ("#ff4500", 2.0, "solid"), 3.9),
    ("ISW", "Ice Storm Warning", "Ice Storm Warning", ("#8b008b", 2.0, "solid"), 3.8),
    ("WSW", "Winter Storm Warning", "Winter Storm Warning", ("#ff69b4", 1.8, "solid"), 3.7),
    ("LEW", "Lake Effect Snow Warning", "Lake Effect Snow Warning", ("#008b8b", 1.8, "solid"), 3.6),
    ("ECW", "Extreme Cold Warning", "Extreme Cold Warning", ("#0000ff", 1.5, "solid"), 3.4),
    ("WSA", "Winter Storm Watch", "Winter Storm Watch", ("#4682b4", 1.5, "solid"), 3.3),
    ("ZRY", "Freezing Rain Advisory", "Freezing Rain Advisory", ("#da70d6", 1.5, "solid"), 3.2),
    ("WWY", "Winter Weather Advisory", "Winter Weather Advisory", ("#7b68ee", 1.5, "solid"), 3.1),
    ("ECA", "Extreme Cold Watch", "Extreme Cold Watch", ("#5f9ea0", 1.2, "solid"), 2.9),
    ("CWY", "Cold Weather Advisory", "Cold Weather Advisory", ("#afeeee", 1.2, "solid"), 2.8),
    ("FZW", "Freeze Warning", "Freeze Warning", ("#483d8b", 1.2, "solid"), 2.7),
    ("FRY", "Frost Advisory", "Frost Advisory", ("#6495ed", 1.2, "solid"), 2.6),
]
VARIANT = {v[0]: v for v in VARIANTS}
LINE_KINDS = ("solid", "center", "double")
BASE_CODE = {"Tornado Warning": "TOR", "Tornado Emergency": "TORE", "Severe Thunderstorm Warning": "SVR",
             "Flash Flood Warning": "FFW", "Flash Flood Emergency": "FFWE", "Special Marine Warning": "SMW",
             "Extreme Wind Warning": "EWW", "Snow Squall Warning": "SQW", "Dust Storm Warning": "DSW",
             "Special Weather Statement": "SPS", "Tornado Watch": "TOA", "Severe Thunderstorm Watch": "SVA"}
BASE_CODE.update({v[1]: v[0] for v in VARIANTS if v[0] in (
    "HUW", "SSW", "TRW", "HUA", "SSA", "TRA", "BZW", "ISW", "WSW", "LEW", "ECW", "WSA", "ZRY", "WWY", "ECA", "CWY",
    "FZW", "FRY")})
WINTER_EVENTS = ("Blizzard Warning", "Ice Storm Warning", "Winter Storm Warning", "Lake Effect Snow Warning",
                 "Winter Storm Watch", "Freezing Rain Advisory", "Winter Weather Advisory", "Extreme Cold Warning",
                 "Extreme Cold Watch", "Cold Weather Advisory", "Freeze Warning", "Frost Advisory")
TROPICAL_EVENTS = ("Hurricane Warning", "Storm Surge Warning", "Tropical Storm Warning", "Hurricane Watch",
                   "Storm Surge Watch", "Tropical Storm Watch")
# issued for whole counties / forecast zones (no storm polygon): drawn as shaded areas
AREA_EVENTS = set(WINTER_EVENTS) | set(TROPICAL_EVENTS)
STORM_WATCHES = ("Tornado Watch", "Severe Thunderstorm Watch")

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
    STYLES[_ev] = (hex_rgb(_c), _w, (35 if _ev.endswith("Watch") else 60) if _ev in AREA_EVENTS else
                   (45 if _ev.endswith("Watch") else 0), VARIANT[_code][4])

# filter groups (Warnings panel buttons); "WAT" is the same switch as Map -> Watches
FILTERS = [("TOR", "Tornado", ("Tornado Warning", "Tornado Emergency")),
           ("SVR", "Severe", ("Severe Thunderstorm Warning",)),
           ("FFW", "Flood", ("Flash Flood Warning", "Flash Flood Emergency")),
           ("WIN", "Winter", WINTER_EVENTS),
           ("TRO", "Tropical", TROPICAL_EVENTS),
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


def nws_line(code: str) -> tuple:
    """(colour hex, width, kind) of the code in the National Weather Service's colours."""
    return VARIANT.get(code, VARIANT["SPS"])[3]


NWS_PRESET = {code: nws_line(code) for code in VARIANT}


def default_line(code: str) -> tuple:
    """(colour hex, width, kind) the code has by default: the classic colours (green flash flood, yellow severe,
    magenta reported / PDS tornado), the NWS ones for the rest."""
    return CLASSIC_PRESET.get(code) or nws_line(code)


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
              ("SV", "A"): "Severe Thunderstorm Watch", ("BZ", "W"): "Blizzard Warning",
              ("WS", "W"): "Winter Storm Warning", ("IS", "W"): "Ice Storm Warning",
              ("LE", "W"): "Lake Effect Snow Warning", ("WS", "A"): "Winter Storm Watch",
              ("WW", "Y"): "Winter Weather Advisory", ("ZR", "Y"): "Freezing Rain Advisory",
              ("EC", "W"): "Extreme Cold Warning", ("EC", "A"): "Extreme Cold Watch",
              ("CW", "Y"): "Cold Weather Advisory", ("FZ", "W"): "Freeze Warning", ("FR", "Y"): "Frost Advisory",
              ("HU", "W"): "Hurricane Warning", ("HU", "A"): "Hurricane Watch",
              ("TR", "W"): "Tropical Storm Warning", ("TR", "A"): "Tropical Storm Watch",
              ("SS", "W"): "Storm Surge Warning", ("SS", "A"): "Storm Surge Watch"}


class Alert:
    __slots__ = ("event", "rings", "hover", "issued", "expires", "style", "xy", "_proj", "office", "area",
                 "tags", "uid", "variant", "key", "action", "info", "valid", "_path")

    def __init__(self, event, rings, hover, issued, expires, office="", area="", tags=(), uid="", variant=None,
                 key="", action="", info=None):
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
        # for the details panel: {"description", "instruction", "headline", "facts": [(label, value)],
        # "motion": (from_deg, kt) | None, "iem": {...} (archive: where its text can be downloaded)}
        self.info = info or {}
        # when this polygon applies: (from, until). Iowa State's archive has one piece per polygon (a warning's
        # update can shrink it); live alerts are valid from issue to expiry
        self.valid = (issued, expires)
        self._path = None           # (projection, QPainterPath in km): a county-based alert's merged outline

    @property
    def is_area(self):
        return self.event in AREA_EVENTS

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


# warnings that can't wait: checked on their own every few seconds (a small request), the rest once a minute
URGENT_EVENTS = ("Tornado Warning", "Severe Thunderstorm Warning", "Flash Flood Warning", "Extreme Wind Warning",
                 "Special Marine Warning", "Snow Squall Warning", "Dust Storm Warning", "Tornado Watch",
                 "Severe Thunderstorm Watch")
URGENT_SET = set(URGENT_EVENTS) | {"Tornado Emergency", "Flash Flood Emergency"}
# Iowa State's live storm-based warnings (it reads the NWS text feed directly: often a minute or more ahead of
# api.weather.gov)
IEM_SBW_PHENOMENA = {"TO", "SV", "FF", "MA", "EW", "SQ", "DS"}


class FilterRejected(Exception):
    """api.weather.gov didn't accept the event filter (then the full list is used instead)."""


def fetch_live_alerts(county_polys: dict, events=None, session=None) -> list:
    params = {"status": "actual"}
    if events:
        params["event"] = ",".join(events)
    r = (session or requests).get("https://api.weather.gov/alerts/active", params=params, headers=UA, timeout=30)
    if events and r.status_code == 400:
        raise FilterRejected(r.text[:200])
    r.raise_for_status()
    return parse_live_alerts(r.json(), county_polys)


def fetch_iem_active(session=None, now=None) -> list:
    """The storm-based warnings in force right now from Iowa State (one per warning: its newest polygon)."""
    r = (session or requests).get(IEM_SBW, headers={"User-Agent": UA["User-Agent"]}, timeout=20)
    r.raise_for_status()
    return iem_active(r.json(), now)


def iem_active(js: dict, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    feats = [f for f in js.get("features", []) if (f.get("properties") or {}).get("phenomena") in IEM_SBW_PHENOMENA
             and str((f.get("properties") or {}).get("status") or "").upper() not in ("CAN", "EXP", "UPG")]
    newest = {}
    for a in parse_iem_sbw({"features": feats}):
        if a.expires is not None and a.expires <= now:
            continue
        k = event_key(a.key)
        b = a.valid[0]
        old = newest.get(k)
        if old is None or (b is not None and (old.valid[0] is None or b > old.valid[0])):
            newest[k] = a
    out = list(newest.values())
    for a in out:
        a.info["source"] = "iem"
    return out


def merge_alerts(full, fast, iem, fast_newer=True) -> list:
    """One list from the three live sources. The urgent warnings come from the quick check when it is newer than
    the full list; a warning Iowa State has but the NWS service doesn't list yet is added (and one it shows
    upgraded - a tornado warning becoming observed - takes the higher threat until the NWS catches up)."""
    if fast is not None and fast_newer:
        out = [a for a in full if a.event not in URGENT_SET] + list(fast)
    else:
        out = list(full)
    import copy
    have = {event_key(a.key): i for i, a in enumerate(out)}
    for a0 in iem or ():
        k = event_key(a0.key)
        i = have.get(k)
        a = copy.copy(a0)                    # (the source list is merged again on every check: never edited)
        a.info = dict(a0.info)
        if i is None:
            a.hover = a.hover + "\n(from Iowa State's live feed: the NWS alert service hasn't listed it yet)"
            out.append(a)
            have[k] = len(out) - 1
        elif a.style[3] > out[i].style[3]:
            nws = out[i]
            a.info = {**nws.info, **{k2: v for k2, v in a.info.items() if k2 in ("facts", "iem", "source")}}
            a.area = nws.area or a.area
            a.hover = nws.hover + f"\nUpgraded to {VARIANT[a.variant][2]} (Iowa State's live feed)"
            out[i] = a
    out.sort(key=lambda a: a.style[3])
    return out


def parse_live_alerts(js: dict, county_polys: dict) -> list:
    out = []
    for f in js.get("features", []):
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
        if not rings and (ev.endswith("Watch") or ev in AREA_EVENTS):
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
        info = dict(description=desc.strip(), instruction=(p.get("instruction") or "").strip(),
                    headline=_first(params, "NWSheadline"),
                    facts=live_facts(params), motion=storm_motion(_first(params, "eventMotionDescription")))
        out.append(Alert(ev, rings, hover, _t(p.get("sent")), _t(p.get("expires")), office,
                         p.get("areaDesc") or "", short, p.get("id") or "", variant, key, action, info))
    return out


# NWS impact tags shown in the details panel: (parameter, label, unit added when the value has none)
LIVE_FACTS = (("tornadoDetection", "Tornado", ""), ("tornadoDamageThreat", "Tornado damage threat", ""),
              ("maxHailSize", "Hail", " in"), ("hailThreat", "Hail threat", ""),
              ("maxWindGust", "Wind gusts", ""), ("windThreat", "Wind threat", ""),
              ("thunderstormDamageThreat", "Damage threat", ""), ("flashFloodDetection", "Flash flooding", ""),
              ("flashFloodDamageThreat", "Flood damage threat", ""), ("waterspoutDetection", "Waterspout", ""))


def alert_title(a) -> str:
    """'Tornado Warning – PDS', 'Severe Thunderstorm Warning – Considerable', 'Tornado Watch'…"""
    label = VARIANT[a.variant][2]
    if " - " in label:
        extra = label.split(" - ", 1)[1]
        if extra.lower() not in a.event.lower():
            return f"{a.event} – {extra}"
    return a.event


def time_left(t_end, now) -> str:
    s = (t_end - now).total_seconds()
    if s <= 0:
        return "expired"
    m = int(s // 60)
    return f"{m} min left" if m < 60 else f"{m // 60} h {m % 60:02d} min left"


def live_facts(params: dict) -> list:
    out = []
    for key, label, unit in LIVE_FACTS:
        v = params.get(key)
        v = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v or "")
        v = v.strip()
        if v:
            if unit and v.replace(".", "", 1).isdigit():
                v += unit
            out.append((label, v.capitalize() if v.isupper() else v))
    return out


def storm_motion(text: str):
    """(from_deg, kt) from a VTEC motion line "TIME...MOT...LOC" (e.g. "...STORM...229DEG...35KT..."), or None."""
    import re
    m = re.search(r"(\d{1,3})DEG\.\.\.(\d{1,3})KT", text or "", re.I)
    return (int(m.group(1)) % 360, int(m.group(2))) if m else None


IEM_SBW = "https://mesonet.agron.iastate.edu/geojson/sbw.geojson"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def fetch_archive_alerts(ts: datetime) -> list:
    """Warnings (and watches) in force at one time, from Iowa State's archive."""
    r = requests.get(IEM_SBW, params={"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}, headers=UA, timeout=30)
    r.raise_for_status()
    return parse_iem_sbw(r.json())


def fetch_alert_history(start: datetime, end: datetime) -> list:
    """Every warning polygon in force at some time between start and end (a loop's span), each with the time
    it applied: going through the loop then shows each scan the warnings of its own time, polygons as they were."""
    r = requests.get(IEM_SBW, params={"sts": _iso(start), "ets": _iso(end)}, headers=UA, timeout=40)
    r.raise_for_status()
    return parse_iem_sbw(r.json())


def parse_iem_sbw(js: dict) -> list:
    out = []
    for f in js.get("features", []):
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
        facts = []
        for k, label, unit in (("tornadotag", "Tornado", ""), ("hailtag", "Hail", " in"),
                               ("windtag", "Wind gusts", " mph"), ("damagetag", "Damage threat", "")):
            v = str(p.get(k) or "").strip()
            if v:
                if unit and v.replace(".", "", 1).isdigit():
                    v += unit
                facts.append((label, v.capitalize() if v.isupper() else v))
        begin = _t(p.get("polygon_begin"))
        issue = _t(p.get("issue")) or begin
        try:
            iem = dict(wfo=str(p.get("wfo") or ""), year=issue.year, phenomena=str(p.get("phenomena") or ""),
                       significance=str(p.get("significance") or ""), etn=int(p.get("eventid")))
        except (TypeError, ValueError, AttributeError):
            iem = None
        end = _t(p.get("polygon_end"))
        expire = _t(p.get("expire")) or end
        if end is None or begin is None or end <= begin:
            end = expire                       # (the archive has the odd polygon that ends before it begins)
        key = f"{p.get('wfo')}.{p.get('phenomena')}.{p.get('significance')}.{p.get('eventid')}"
        a = Alert(ev, rings, hover, issue or begin, expire, p.get("wfo") or "", f"#{p.get('eventid')}", short,
                  f"{key}@{p.get('polygon_begin') or ''}", variant, key, str(p.get("status") or ""),
                  info=dict(facts=facts, iem=iem))
        a.valid = (begin, end)
        out.append(a)
    return out


def event_key(key: str) -> tuple:
    """A warning's identity whatever the source: ('OUN', 'TO', 'W', 42) from 'KOUN.TO.W.0042' (NWS) or
    'OUN.TO.W.42' (Iowa State). Watches are numbered nationally, so the office is left out for them."""
    try:
        office, ph, sig, etn = key.split("@")[0].split(".")[:4]
        return ("" if sig == "A" else office[-3:].upper(), ph, sig, int(etn))
    except (ValueError, AttributeError):
        return (key,)


IEM_EVENT = "https://mesonet.agron.iastate.edu/json/vtec_event.py"


def fetch_warning_text(iem: dict) -> str:
    """The warning's text and its updates (severe weather statements), from Iowa State's VTEC archive."""
    r = requests.get(IEM_EVENT, params=iem, headers={"User-Agent": UA["User-Agent"]}, timeout=20)
    r.raise_for_status()
    return warning_text(r.json())


def warning_text(js: dict) -> str:
    parts = []
    rep = js.get("report") or {}
    if rep.get("text"):
        parts.append(rep["text"].strip())
    for st in js.get("svs") or []:
        if st.get("text"):
            parts.append(st["text"].strip())
    if not parts:
        raise ValueError("no text for this warning")
    return ("\n\n" + "─" * 40 + "\n\n").join(parts)


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
    _flashStart = Signal()            # (from the fetch thread: starts the flash animation on the UI thread)

    def __init__(self, settings, county_polys_fn, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.county_polys_fn = county_polys_fn
        self.alerts: list = []            # live: what's in force right now (NWS)
        self.history: list = []           # every warning polygon over the loop's span, each with its time (IEM)
        self.reports: list = []
        self.mode = "live"
        self.archive_time = None
        self._last_fetch = 0.0
        self._busy = False
        self._report_key = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self.FULL_S * 1000)
        # the urgent warnings are checked much more often, from two sources, whichever has a warning first
        self._full: list = []
        self._fast = None                 # urgent warnings from the quick check (None: none yet)
        self._iem: list = []
        self._full_at = self._fast_at = 0.0
        self._fast_ok = True              # api.weather.gov takes the event filter
        self._fast_busy = self._iem_busy = False
        self._seeded = False
        self._sess = {k: requests.Session() for k in ("full", "fast", "iem")}      # (connections kept open)
        self._fast_timer = QTimer(self)
        self._fast_timer.timeout.connect(self._fast_poll)
        self._fast_timer.start(self.FAST_S * 1000)
        self._iem_timer = QTimer(self)
        self._iem_timer.timeout.connect(self._iem_poll)
        self._iem_timer.start(self.IEM_S * 1000)
        self._merge_lock = threading.Lock()
        self.frame_time = None            # the scan on screen
        self.latest_time = None           # the newest scan of the loop
        self.center = (35.0, -97.0)
        self._span = None                 # (start, end) the loop covers
        self._hist_span = None            # (start, end) self.history covers
        self._hist_at = 0.0               # when it was fetched
        self._hist_busy = False
        self._hist_gen = 0
        self._ver = 0                     # bumps whenever alerts or history change (for the cache below)
        self._shown = (None, None)        # ((frame_time, ver, latest, mode), alerts) last worked out
        # new warnings flash for a few seconds when they first appear (and when one is upgraded, e.g. a tornado
        # warning becoming "observed")
        self._known: dict = {}            # event key -> priority seen (live)
        self._flash: dict = {}            # event key -> (start, end) epoch seconds
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(60)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flashStart.connect(self._flash_timer.start)

    FULL_S = 60                       # every warning, watch and advisory
    FAST_S = 10                       # the urgent ones (tornado, severe, flash flood…) from api.weather.gov
    IEM_S = 15                        # ... and from Iowa State's live feed

    def set_live(self):
        if self.mode != "live":
            self._known.clear()               # (what's in force now isn't "new" just because we came back)
            self._last_fetch = 0.0
            self._seeded = False
            self._fast, self._iem = None, []
        self.mode = "live"
        self.refresh(force=True)
        self._fast_poll()
        self._iem_poll()

    def _want_live_warnings(self):
        ov = self.settings["overlays"]
        return self.mode == "live" and (ov.get("warnings", True) or ov.get("watches", True))

    def _combine(self):
        """Merge the live sources into self.alerts (any thread)."""
        with self._merge_lock:
            alerts = merge_alerts(self._full, self._fast, self._iem if self.settings["warnings_iem"] else [],
                                  fast_newer=self._fast_at >= self._full_at)
            first = not self._seeded
            self._note_new(alerts, first)
            self._seeded = True
            carry_geometry(self.alerts, alerts)
            self.alerts = alerts
            self._ver += 1

    def _fast_poll(self):
        if self._fast_busy or not self._fast_ok or not self._want_live_warnings() or not self._full_at:
            return
        self._fast_busy = True

        def work():
            try:
                t = time.time()
                fast = fetch_live_alerts(self.county_polys_fn(), URGENT_EVENTS, self._sess["fast"])
                if self.mode == "live":
                    self._fast, self._fast_at = fast, t
                    self._combine()
                    self.changed.emit()
            except FilterRejected:
                self._fast_ok = False                    # (the full list once a minute still has them)
            except Exception:
                pass                                     # (the next check is seconds away)
            finally:
                self._fast_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _iem_poll(self):
        if self._iem_busy or not self.settings["warnings_iem"] or not self._want_live_warnings() \
                or not self._full_at:
            return
        self._iem_busy = True

        def work():
            try:
                iem = fetch_iem_active(self._sess["iem"])
                if self.mode == "live":
                    self._iem = iem
                    self._combine()
                    self.changed.emit()
            except Exception:
                pass
            finally:
                self._iem_busy = False
        threading.Thread(target=work, daemon=True).start()

    def set_archive_time(self, t: datetime):
        """Archive / files: the scan on screen (storm reports follow it; the warnings come from the history)."""
        self.mode = "archive"
        if self.archive_time is None or abs((t - self.archive_time).total_seconds()) >= 120:
            self.archive_time = t
            self.refresh(force=True)
        if self._span is None or not (self._span[0] - timedelta(hours=6) <= t <= self._span[1] + timedelta(hours=6)):
            self.set_span(t, t, t)            # (a time far from the loop's: at least this scan's warnings)

    # ---------------------------------------------------------------- warnings at the scan's time
    HISTORY_REFRESH_S = 150           # live: the recent end of the history is fetched again this often

    def set_span(self, start, end, latest):
        """The loop covers start … end (its newest scan: latest). The warnings of that whole span are fetched
        once (and their recent end again every few minutes in live mode)."""
        if start is None or end is None:
            return
        self._span = (start, end)
        if latest != self.latest_time:
            self.latest_time = latest
            self._ver += 1
        self._ensure_history()

    def _ensure_history(self, force=False):
        if self._span is None or self._hist_busy:
            return
        ov = self.settings["overlays"]
        if not (ov.get("warnings", True) or ov.get("watches", True)):
            return
        start, end = self._span
        start = start - timedelta(minutes=2)
        if self.mode == "live":
            end = datetime.now(timezone.utc)
        hs = self._hist_span
        covered = hs is not None and hs[0] <= start and (hs[1] >= end - timedelta(seconds=30) if self.mode != "live"
                                                         else time.time() - self._hist_at < self.HISTORY_REFRESH_S)
        if covered and not force:
            return
        if end - start > timedelta(hours=24):
            start = end - timedelta(hours=24)     # (a loop is never that long; don't ask for days of warnings)
        self._hist_busy = True
        gen = self._hist_gen

        def work():
            try:
                hist = fetch_alert_history(start, end + timedelta(minutes=1))
                if gen == self._hist_gen:
                    hist.sort(key=lambda a: a.style[3])
                    self.history = hist
                    self._hist_span = (start, end)
                    self._hist_at = time.time()
                    self._ver += 1
            except Exception as exc:
                self.status.emit(f"Earlier warnings unavailable – {friendly_error(exc)} (the newest are shown)")
                self._hist_at = time.time() - self.HISTORY_REFRESH_S + 60      # try again in a minute
            finally:
                self._hist_busy = False
                self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    def forget_history(self):
        """Another radar's or another time's loop: the history starts again."""
        self._hist_gen += 1
        self.history = []
        self._hist_span = None
        self._span = None
        self._ver += 1

    def shown_alerts(self) -> list:
        """The warnings and watches in force at the scan on screen. The newest live scan shows what's in force
        now; an earlier scan (or any archive scan) the polygons of its own time."""
        t = self.frame_time
        key = (t, self._ver, self.latest_time, self.mode, id(self.alerts), id(self.history))
        if self._shown[0] == key:
            return self._shown[1]
        live = self.mode == "live"
        if live and (t is None or self.latest_time is None or t >= self.latest_time - timedelta(seconds=1)):
            out = list(self.alerts)
        elif t is None:
            out = []
        else:
            out, seen = [], set()
            for a in list(self.history):
                b, e = a.valid
                if b is not None and e is not None and b <= t < e:
                    out.append(a)
                    seen.add(event_key(a.key))
            if live:
                # in force now and already then, but not in the archive yet (it is a few minutes behind)
                for a in list(self.alerts):
                    b, e = a.valid
                    if event_key(a.key) in seen or (b is not None and b > t) or (e is not None and e < t):
                        continue
                    out.append(a)
            out.sort(key=lambda a: a.style[3])
        self._shown = (key, out)
        return out

    # ---------------------------------------------------------------- new warnings flash
    def _note_new(self, alerts, first):
        """Flash warnings that weren't there before (or were upgraded to a higher threat), live only."""
        secs = float(self.settings["warning_flash_s"] or 0)
        now = time.time()
        known = {}
        for a in alerts:
            if a.event.endswith(("Watch", "Statement")):
                continue
            k = event_key(a.key)
            pri = a.style[3]
            known[k] = max(pri, known.get(k, 0))
            old = self._known.get(k)
            if first or secs <= 0:
                continue
            fresh = a.issued is None or (datetime.now(timezone.utc) - a.issued).total_seconds() < 20 * 60
            if (old is None and fresh) or (old is not None and pri > old):
                self._flash[k] = (now, now + secs)
        self._known = known
        if self._flash:
            self._flashStart.emit()

    def _flash_tick(self):
        now = time.time()
        self._flash = {k: v for k, v in self._flash.items() if v[1] > now}
        if not self._flash:
            self._flash_timer.stop()
        self.changed.emit()                                     # (redraws the map)

    def flashing(self, a):
        """0 … 1 how bright a flashing warning's pulse is right now, or None."""
        f = self._flash.get(event_key(a.key)) if self._flash else None
        if f is None:
            return None
        now = time.time()
        if now >= f[1]:
            return None
        return 0.5 + 0.5 * math.cos((now - f[0]) * 2 * math.pi * 1.4)        # 1.4 pulses a second

    def refresh(self, force=False):
        ov = self.settings["overlays"]
        want_w = ov.get("warnings", True) or ov.get("watches", True)
        want_r = ov.get("reports", False)
        if self._busy or not (want_w or want_r):
            return
        if not force and self.mode == "archive":
            return
        self._busy = True
        mode, t = self.mode, self.archive_time

        def work():
            try:
                if want_w and mode == "live":
                    t_start = time.time()
                    full = fetch_live_alerts(self.county_polys_fn(), session=self._sess["full"])
                    self._full, self._full_at = full, t_start
                    self._combine()
                    self._last_fetch = time.time()
                    if self._fast is None:
                        self._fast_poll_soon = True
                elif want_w and self.alerts:
                    self.alerts = []                # (archive: the warnings come from the history)
                    self._ver += 1
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
                n = len(self.alerts) if mode == "live" else len(self.shown_alerts())
                self.status.emit(f"Warnings: {n} {'active' if mode == 'live' else 'at this time'}" +
                                 (f", {len(self.reports)} reports" if want_r else ""))
            except Exception as exc:
                self.status.emit(f"Warnings unavailable – {friendly_error(exc)} (retrying)")
            finally:
                self._busy = False
                self.changed.emit()
                if getattr(self, "_fast_poll_soon", False):
                    self._fast_poll_soon = False
                    self._fast_poll()                    # (the quick checks start once the full list is in)
                    self._iem_poll()
        threading.Thread(target=work, daemon=True).start()
        if self.mode == "live":
            self._ensure_history()            # (its recent end, every few minutes)

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
        if a.event in STORM_WATCHES:
            return bool(ov.get("watches", True))
        if not ov.get("warnings", True):
            return False
        return bool((self.settings["warning_types"] or {}).get(EVENT_GROUP.get(a.event, "OTH"), True))

    def _in_time(self, a) -> bool:
        return True                           # (shown_alerts() is already the scan's own time)

    def active_alerts(self):
        """The warnings in force at the scan on screen (see shown_alerts)."""
        return self.shown_alerts()

    def paint(self, painter, vt, panel, view):
        ov = self.settings["overlays"]
        x0, y0, x1, y1 = vt.world_bounds(pad=10)
        sel = self.selected_uid
        sel_key = sel.split("@")[0] if sel and "@" in sel else None
        shown = [a for a in self.shown_alerts() if self.visible(a)]
        areas = [a for a in shown if a.event in AREA_EVENTS]
        if areas:
            self._paint_area_layer(painter, vt, view, areas, sel, sel_key)
        for a in shown:
            if a.event in AREA_EVENTS:
                continue
            is_watch = a.event.endswith("Watch")
            pulse = self.flashing(a)
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
                if pulse is not None:
                    # newly issued: a glowing, pulsing band around it and a light wash inside
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(*rgb, int(25 + 70 * pulse)))
                    painter.drawPolygon(poly)
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(QPen(QColor(*rgb, int(60 + 150 * pulse)), width + 4 + 8 * pulse, Qt.SolidLine,
                                        Qt.RoundCap, Qt.RoundJoin))
                    painter.drawPolygon(poly)
                    painter.setPen(QPen(QColor(255, 255, 255, int(200 * pulse)), width + 1.5, Qt.SolidLine,
                                        Qt.RoundCap, Qt.RoundJoin))
                    painter.drawPolygon(poly)
                draw_line(painter, poly, rgb, width, kind, halo=not is_watch)
                if a.uid == sel or (sel_key is not None and a.key == sel_key):
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
                inches = feeds.snow_inches(r)
                if inches is not None and vt.km_across < 900:
                    view._halo_text(painter, sx + rad + 2, sy + 4, f"{inches:g}″", QColor(190, 215, 255), f1)

    def _area_geometry(self, view, a, scale):
        """(fill path, outline path) for a county-based alert at this zoom, cached on the alert: the outline
        is its outer edge only, and both carry no more detail than the screen can show."""
        xys = self._xy(a, view.lat0, view.lon0)
        g = a._path if isinstance(a._path, dict) and a._path.get("proj") == (view.lat0, view.lon0) else None
        if g is None:
            g = {"proj": (view.lat0, view.lon0), "runs": merged_outline(xys), "levels": {}}
            pts = np.concatenate(xys) if xys else np.zeros((0, 2))
            g["bbox"] = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()) if len(pts) else None
            a._path = g
        # detail: about half a pixel for the outline, a pixel for the fill (its edge is under the outline)
        lvl = int(math.floor(math.log2(max(0.5 / max(scale, 1e-6), 1e-3))))
        geo = g["levels"].get(lvl)
        if geo is None:
            tol = 2.0 ** lvl
            fill = qpath([snap(xy, tol * 2) for xy in xys], close=True)
            line = qpath([snap(r, tol) for r in g["runs"]])
            geo = (fill, line)
            if len(g["levels"]) > 6:
                g["levels"].clear()
            g["levels"][lvl] = geo
        return g["bbox"], geo

    def _paint_area(self, painter, vt, view, a, selected, pulse=None):
        """A county / zone based alert (winter storm, hurricane…): its counties shaded as one area, one outline."""
        from PySide6.QtGui import QPainter as _QP, QTransform
        bbox, (fill_path, line_path) = self._area_geometry(view, a, vt.scale)
        if bbox is None:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=10)
        if bbox[2] < x0 or bbox[0] > x1 or bbox[3] < y0 or bbox[1] > y1:
            return
        rgb, width, _kind = line_style(self.settings, a.variant)
        _rgb, _w, fill, _pri = a.style
        painter.save()
        painter.setTransform(QTransform(vt.scale, 0, 0, -vt.scale, vt.ox - vt.cx * vt.scale, vt.oy + vt.cy * vt.scale),
                             True)
        painter.setRenderHint(_QP.Antialiasing, False)        # (the fill's edge is under the outline anyway)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*rgb, int(min(255, fill + (60 * pulse if pulse is not None else 0)))))
        painter.drawPath(fill_path)
        painter.setBrush(Qt.NoBrush)
        for w, col, aa in ((width + 2.0, QColor(0, 0, 0, 150), False), (width, QColor(*rgb), True)):
            painter.setRenderHint(_QP.Antialiasing, aa)        # (the dark halo needn't be smooth: it's quicker)
            pen = QPen(col, w, Qt.DashLine if a.event.endswith("Watch") else Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(line_path)
        if selected:
            pen = QPen(QColor(255, 255, 255), width + 1.5, Qt.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(line_path)
        painter.restore()

    AREA_MARGIN = 0.25                # the area picture reaches this far past each side of the view (panning)
    AREA_SETTLE_S = 0.18              # zooming: the stretched picture is redrawn sharp once zooming pauses

    def _paint_area_layer(self, painter, vt, view, areas, sel, sel_key):
        """All the county-based alerts as one picture, a little bigger than the view. A loop playing or the
        map redrawing for any other reason just stamps it; panning slides it; zooming stretches it until
        zooming pauses, then it's drawn again (a winter outbreak covers hundreds of counties)."""
        from PySide6.QtCore import QRectF, QTimer
        from PySide6.QtGui import QImage, QPainter as _QP
        r = vt.rect
        dev = painter.device()
        dpr = dev.devicePixelRatioF() if hasattr(dev, "devicePixelRatioF") else 1.0
        flashing = [a for a in areas if self.flashing(a) is not None]
        steady = [a for a in areas if a not in flashing]
        caches = self.__dict__.setdefault("_area_caches", {})
        slot = (round(r.x()), round(r.y()))
        static = (round(r.width()), round(r.height()), dpr, view.lat0, view.lon0,
                  tuple((a.uid, a.variant, len(a.rings), sum(len(x) for x in a.rings)) for a in steady), sel,
                  sel_key, self._style_sig())
        e = caches.get(slot)
        now = time.monotonic()
        W, H = r.width(), r.height()
        mx, my = W * self.AREA_MARGIN, H * self.AREA_MARGIN
        if steady:
            if e is not None and e["static"] == static:
                if abs(vt.scale - e.get("seen_scale", e["scale"])) > 1e-9:
                    e["seen_scale"] = vt.scale
                    e["scale_changed"] = now
                k = vt.scale / e["scale"]
                left = r.x() + W / 2 + (e["cx"] - vt.cx) * vt.scale - (mx + W / 2) * k
                top = r.y() + H / 2 - (e["cy"] - vt.cy) * vt.scale - (my + H / 2) * k
                target = QRectF(left, top, (W + 2 * mx) * k, (H + 2 * my) * k)
                covers = target.contains(r)
                if abs(k - 1.0) < 1e-6 and covers:
                    painter.drawImage(target.topLeft(), e["img"])            # (the usual case: just stamp it)
                    e = "done"
                elif covers and now - e.get("scale_changed", 0) < self.AREA_SETTLE_S:
                    painter.save()
                    painter.setRenderHint(_QP.SmoothPixmapTransform, False)
                    painter.drawImage(target, e["img"])                       # zooming: stretched for now
                    painter.restore()
                    if not getattr(self, "_area_settle_pending", False):
                        self._area_settle_pending = True

                        def settle():
                            self._area_settle_pending = False
                            self.changed.emit()
                        QTimer.singleShot(int(self.AREA_SETTLE_S * 1000) + 20, settle)
                    e = "done"
            if e != "done":
                img = QImage(max(1, int((W + 2 * mx) * dpr)), max(1, int((H + 2 * my) * dpr)),
                             QImage.Format_ARGB32_Premultiplied)
                img.setDevicePixelRatio(dpr)
                img.fill(0)
                p = _QP(img)
                p.setRenderHint(_QP.Antialiasing, True)
                p.translate(mx - r.x(), my - r.y())
                big = _Grown(vt, mx, my)
                for a in steady:
                    self._paint_area(p, big, view, a, a.uid == sel or (sel_key is not None and a.key == sel_key))
                p.end()
                caches[slot] = {"static": static, "scale": vt.scale, "seen_scale": vt.scale, "cx": vt.cx,
                                "cy": vt.cy, "img": img, "scale_changed": 0.0}
                painter.drawImage(QRectF(r.x() - mx, r.y() - my, W + 2 * mx, H + 2 * my).topLeft(), img)
        for a in flashing:
            self._paint_area(painter, vt, view, a, a.uid == sel or (sel_key is not None and a.key == sel_key),
                             self.flashing(a))

    def _style_sig(self):
        """What the area picture depends on in the settings (line styles and colours)."""
        s = self.settings
        try:
            return hash((repr(s["warning_lines"]), repr(s["warning_colors"])))
        except Exception:
            return 0

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

    def report_at(self, x, y, tol):
        if not self.settings["overlays"].get("reports", False):
            return None
        for r in self.visible_reports():
            if "xy" in r and math.hypot(r["xy"][0] - x, r["xy"][1] - y) < tol * 1.2:
                return r
        return None

    def alerts_at(self, x, y, tol, inside=True):
        """The drawn alerts under a point (inside them, or [inside]=False: only on their outline), most
        important first."""
        hits = []
        for a in self.shown_alerts():
            # only what is drawn: a hidden watch (or warning type) must not answer
            if a.xy is None or not self.visible(a):
                continue
            if any(near_edge(x, y, xy, tol) or (inside and _inside(x, y, xy)) for xy in a.xy):
                hits.append(a)
        hits.sort(key=lambda a: -a.style[3])
        return hits

    def now(self):
        """The time warnings are shown for: now (live), or the frame's time (archive)."""
        if self.mode == "live" or self.frame_time is None:
            return datetime.now(timezone.utc)
        return self.frame_time

    def reports_in(self, a) -> list:
        """The storm reports on the map that are inside an alert."""
        if not self.settings["overlays"].get("reports", False) or a.xy is None:
            return []
        return [r for r in self.visible_reports() if "xy" in r and any(_inside(*r["xy"], xy) for xy in a.xy)]

    @staticmethod
    def reports_text(reps) -> str:
        """'2 tornado, 3 hail (largest 1.75 in)'."""
        counts = {}
        for r in reps:
            name = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])[2].lower()
            counts[name] = counts.get(name, 0) + 1
        parts = [f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        sizes = []
        for r in reps:
            if r.get("kind") == "hail":
                try:
                    sizes.append(float(str(r.get("magnitude") or "").split()[0]))
                except (ValueError, IndexError):
                    pass
        if sizes:
            parts = [p + (f" (largest {max(sizes):g} in)" if p.endswith(" hail") else "") for p in parts]
        return ", ".join(parts)

    def summary(self, a) -> str:
        """The short text for the hover box: what it is, when it ends, its tags, motion and reports inside."""
        info = a.info or {}
        lines = [alert_title(a)]
        if info.get("headline"):
            lines.append(info["headline"].capitalize())
        when = []
        if a.office:
            when.append(a.office if a.office.startswith(("NWS", "Storm")) else "NWS " + a.office)
        if a.expires:
            when.append(f"until {feeds.local_hm(a.expires)} ({time_left(a.expires, self.now())})")
        if when:
            lines.append(" · ".join(when))
        facts = info.get("facts") or []
        if facts:
            lines.append(" · ".join(f"{k}: {v}" for k, v in facts))
        mot = info.get("motion")
        if mot:
            lines.append(f"Moving {feeds.compass(mot[0] + 180)} at {round(mot[1] * 1.15078)} mph")
        reps = self.reports_in(a)
        if reps:
            lines.append("Storm reports inside: " + self.reports_text(reps))
        what = "watch" if a.event.endswith("Watch") else "statement" if a.event.endswith("Statement") else "warning"
        lines.append(f"Click the outline for the full {what}")
        return "\n".join(lines)

    def hover(self, x, y, tol):
        r = self.report_at(x, y, tol)
        if r is not None:
            return r["hover"] + "\n(click for details)"
        # only on the outline, so the text doesn't cover the storm while you look inside the box
        hits = self.alerts_at(x, y, tol * 1.5, inside=False)
        return self.summary(hits[0]) if hits else None


def draw_line(painter, shape, rgb, width, kind, halo=True):
    """A warning line: dark halo, the colour, then a black centre for "center" / "double"."""
    poly = isinstance(shape, QPolygonF)
    draw = painter.drawPolygon if poly else painter.drawLine
    args = (shape,) if poly else shape
    if poly:
        painter.setBrush(Qt.NoBrush)          # an outline: never filled with whatever brush was set before
    if halo:
        painter.setPen(QPen(QColor(0, 0, 0, 220), width + 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        draw(*args)
    painter.setPen(QPen(QColor(*rgb), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    draw(*args)
    inner = {"center": 0.25, "double": 0.46}.get(kind, 0.0) * width
    if inner > 0:
        painter.setPen(QPen(QColor(0, 0, 0), max(1.0, inner), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        draw(*args)


# --------------------------------------------------------------------------- county-based alerts, drawn fast
def carry_geometry(old, new):
    """Alerts fetched again come back as new objects: keep the projected outlines (and the merged county
    outline, which takes a moment) of those whose shape hasn't changed."""
    prev = {a.uid: a for a in old or () if a.xy is not None}
    for a in new:
        o = prev.get(a.uid)
        if o is None or o is a or a.xy is not None or len(o.rings) != len(a.rings):
            continue
        if all(r1 is r2 or (r1.shape == r2.shape and np.array_equal(r1, r2)) for r1, r2 in zip(o.rings, a.rings)):
            a.xy, a._proj, a._path = o.xy, o._proj, o._path


class _Grown:
    """A view transform whose visible bounds reach [mx], [my] pixels past each side (for the area picture)."""

    def __init__(self, vt, mx, my):
        self._vt, self._mx, self._my = vt, mx, my

    def __getattr__(self, name):
        return getattr(self._vt, name)

    def world_bounds(self, pad=0.0):
        x0, y0, x1, y1 = self._vt.world_bounds(pad)
        dx, dy = self._mx / self._vt.scale, self._my / self._vt.scale
        return x0 - dx, y0 - dy, x1 + dx, y1 + dy


def qpath(polylines, close=False, winding=True):
    """A QPainterPath from [N x 2 arrays] in one go (Qt's own serialised path format, filled from numpy) -
    a hundred times quicker than adding the points one by one."""
    import struct
    from PySide6.QtCore import QByteArray, QDataStream
    from PySide6.QtGui import QPainterPath
    parts = [np.asarray(pl, np.float64) for pl in polylines if len(pl) >= 2]
    if close:
        parts = [np.vstack([pl, pl[:1]]) if (pl[0] != pl[-1]).any() else pl for pl in parts]
    path = QPainterPath()
    if not parts:
        return path
    n = sum(len(pl) for pl in parts)
    rec = np.empty(n, dtype=[("c", ">i4"), ("x", ">f8"), ("y", ">f8")])
    xy = np.concatenate(parts)
    rec["x"], rec["y"] = xy[:, 0], xy[:, 1]
    rec["c"] = 1                                          # LineTo ...
    starts = np.cumsum([0] + [len(pl) for pl in parts[:-1]])
    rec["c"][starts] = 0                                  # ... MoveTo at each part's start
    data = struct.pack(">i", n) + rec.tobytes() + struct.pack(">ii", 0, 1 if winding else 0)
    QDataStream(QByteArray(data)) >> path
    return path


def snap(pl, tol):
    """Points snapped to a [tol] grid with repeats dropped: detail no finer than a fraction of a pixel. Shared
    county borders snap identically, so neighbours still meet exactly."""
    if tol <= 0 or len(pl) < 3:
        return pl
    q = np.round(np.asarray(pl, np.float64) / tol)
    keep = np.r_[True, (q[1:] != q[:-1]).any(axis=1)]
    return q[keep] * tol


def outline_runs(rings):
    """The outer edge of a group of county rings (km): the edges only one county has (borders between two
    of its counties drop out), as runs of connected points."""
    rings = [np.asarray(r, np.float64) for r in rings if len(r) >= 3]
    if not rings:
        return []
    a = np.concatenate([r for r in rings])
    b = np.concatenate([np.roll(r, -1, axis=0) for r in rings])
    qa = np.round(a * 1000.0).astype(np.int64)             # (to the metre: shared vertices match exactly)
    qb = np.round(b * 1000.0).astype(np.int64)
    swap = (qa[:, 0] > qb[:, 0]) | ((qa[:, 0] == qb[:, 0]) & (qa[:, 1] > qb[:, 1]))
    lo = np.where(swap[:, None], qb, qa)
    hi = np.where(swap[:, None], qa, qb)
    keys = np.c_[lo, hi]
    degenerate = (qa == qb).all(axis=1)
    _u, inv, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    outer = (counts[inv.ravel()] == 1) & ~degenerate
    runs = []
    k = 0
    for r in rings:
        n = len(r)
        o = outer[k:k + n]
        k += n
        if not o.any():
            continue
        if o.all():
            runs.append(np.vstack([r, r[:1]]))
            continue
        # rotate so the ring starts at an inner edge, then cut it into runs of outer edges
        start = int(np.argmin(o))
        idx = (np.arange(n) + start) % n
        oo = o[idx]
        i = 0
        while i < n:
            if not oo[i]:
                i += 1
                continue
            j = i
            while j < n and oo[j]:
                j += 1
            pts = r[idx[i:j]]
            end = r[(idx[j - 1] + 1) % n]
            runs.append(np.vstack([pts, end[None, :]]))
            i = j
    return runs


def merged_outline(rings):
    """The outline of a group of county rings (km) as one shape - Qt merges them exactly, so borders between
    two of its counties go even where the two don't share every point - as [N x 2] rings."""
    try:
        # (points within ~100 m snapped together first: closes the slivers the county map has between some
        # neighbours, which would otherwise leave short stubs of border inside the area)
        path = qpath([snap(r, 0.1) for r in rings], close=True).simplified()
        out = []
        for poly in path.toSubpathPolygons():
            n = poly.size()
            if n >= 2:
                out.append(np.array([(poly.at(i).x(), poly.at(i).y()) for i in range(n)], np.float64))
        return out
    except Exception:
        return outline_runs(rings)


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
