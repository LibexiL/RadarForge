"""Surface observations (METARs) from the Aviation Weather Center's data API, and station-model helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

URL = "https://aviationweather.gov/api/data/metar"
UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)", "Accept": "application/json"}

SKY_FRACTION = {"SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "NCD": 0.0, "FEW": 0.2, "SCT": 0.4, "BKN": 0.75, "OVC": 1.0, "OVX": 1.0, "VV": 1.0}


@dataclass
class Obs:
    station: str
    name: str
    lat: float
    lon: float
    time: datetime
    temp_c: float | None
    dew_c: float | None
    wdir: float | None            # degrees the wind blows FROM (None: calm or variable)
    wspd: float | None            # knots
    wgst: float | None
    vis_mi: float | None
    slp_hpa: float | None
    wx: str
    cover: str                    # SKC FEW SCT BKN OVC ... ("" when unknown)
    raw: str

    @property
    def sky(self) -> float | None:
        return SKY_FRACTION.get(self.cover)

    def temp_f(self) -> float | None:
        return None if self.temp_c is None else self.temp_c * 9 / 5 + 32

    def dew_f(self) -> float | None:
        return None if self.dew_c is None else self.dew_c * 9 / 5 + 32


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _vis(v):
    if v is None:
        return None
    s = str(v).replace("+", "").strip()
    if "/" in s:                                   # "1 1/2" or "1/4"
        try:
            parts = s.split()
            return sum(float(p.split("/")[0]) / float(p.split("/")[1]) if "/" in p else float(p) for p in parts)
        except (ValueError, ZeroDivisionError):
            return None
    return _num(s)


_RAW_WIND = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b")
_RAW_TEMP = re.compile(r"\b(M?\d{2})/(M?\d{2})?\b")
_RAW_SKY = re.compile(r"\b(SKC|CLR|FEW|SCT|BKN|OVC|VV)\d{3}")


def _raw_temp(txt: str):
    return -float(txt[1:]) if txt.startswith("M") else float(txt)


def parse(js) -> list:
    """Observations from the API's JSON (a list of reports), the newest per station. Reports without a position
    are skipped; anything the structured fields lack is read from the raw METAR text."""
    best: dict = {}
    for r in js if isinstance(js, list) else []:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        sid = str(r.get("icaoId") or r.get("stationId") or "").upper()
        if not sid:
            continue
        t = r.get("obsTime")
        try:
            when = datetime.fromtimestamp(float(t), timezone.utc) if t is not None else None
        except (TypeError, ValueError, OSError):
            when = None
        if when is None:
            try:
                when = datetime.fromisoformat(str(r.get("reportTime")).replace("Z", "+00:00"))
            except ValueError:
                continue
        raw = str(r.get("rawOb") or "")
        temp, dew = _num(r.get("temp")), _num(r.get("dewp"))
        wspd, wgst = _num(r.get("wspd")), _num(r.get("wgst"))
        wd = r.get("wdir")
        wdir = _num(wd) if str(wd).upper() != "VRB" else None
        m = _RAW_WIND.search(raw)
        if m and wspd is None:
            wdir = None if m.group(1) == "VRB" else float(m.group(1))
            wspd, wgst = float(m.group(2)), (float(m.group(3)) if m.group(3) else wgst)
        if temp is None:
            m = _RAW_TEMP.search(raw)
            if m:
                temp = _raw_temp(m.group(1))
                dew = _raw_temp(m.group(2)) if m.group(2) else dew
        cover = str(r.get("cover") or "").upper()
        if not cover:
            sky = _RAW_SKY.findall(raw)
            cover = max(sky, key=lambda c: SKY_FRACTION.get(c, 0)) if sky else ""
        ob = Obs(sid, str(r.get("name") or sid), lat, lon, when, temp, dew, wdir, wspd, wgst, _vis(r.get("visib")),
                 _num(r.get("slp")), str(r.get("wxString") or ""), cover, raw)
        old = best.get(sid)
        if old is None or ob.time > old.time:
            best[sid] = ob
    return list(best.values())


def fetch(lat0: float, lon0: float, half_lat: float = 9.0, half_lon: float = 12.0, hours: int = 2) -> list:
    """Observations in a box around a point (the radar), from the last `hours` hours."""
    bbox = f"{lat0 - half_lat:.2f},{lon0 - half_lon:.2f},{lat0 + half_lat:.2f},{lon0 + half_lon:.2f}"
    r = requests.get(URL, params={"bbox": bbox, "format": "json", "hours": hours}, headers=UA, timeout=30)
    r.raise_for_status()
    if not r.content.strip():
        return []
    return parse(r.json())


def barb_parts(speed_kt: float) -> list:
    """The pennants (50 kt), full barbs (10) and half barbs (5) of a wind barb for a speed, outermost first:
    ['pennant', 'full', 'full', 'half']. Speeds are rounded to the nearest 5 kt, as on a weather map."""
    n = int(round(max(0.0, speed_kt) / 5.0)) * 5
    parts = ["pennant"] * (n // 50)
    n %= 50
    parts += ["full"] * (n // 10)
    if n % 10:
        parts.append("half")
    return parts


def pressure_code(slp_hpa: float | None) -> str:
    """The three digits on a station plot: sea level pressure in tenths of a hPa without the leading 9 / 10 (1013.2 -> '132')."""
    if slp_hpa is None:
        return ""
    return f"{int(round(slp_hpa * 10)) % 1000:03d}"


def declutter(obs: list, project, cell: float) -> list:
    """Keeps the most complete report in each `cell` x `cell` pixel square so the plot stays readable.
    project(ob) -> (x, y) pixels, or None for off-screen reports."""
    cells: dict = {}
    for o in obs:
        p = project(o)
        if p is None:
            continue
        key = (int(p[0] // cell), int(p[1] // cell))
        score = sum(v is not None for v in (o.temp_c, o.dew_c, o.wspd, o.slp_hpa)) + (1 if o.cover else 0)
        if key not in cells or score > cells[key][0]:
            cells[key] = (score, o, p)
    return [(o, p) for _s, o, p in cells.values()]
