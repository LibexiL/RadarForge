"""Hurricanes and tropical storms: the National Hurricane Center's storms, cones, tracks, wind fields and
coastal watches / warnings, the tropical weather outlook, model "spaghetti" tracks, offshore buoys, and past
storms (best tracks, replays and historic tracks near a place).

Sources (all public):
  * https://www.nhc.noaa.gov/CurrentStorms.json – the active storms and their latest advisory
  * https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer –
    each storm's forecast points, track, cone, watches / warnings, wind radii and past track, and the
    graphical tropical weather outlook, as GeoJSON
  * https://ftp.nhc.noaa.gov/atcf/ – model forecast tracks ("aids", a-decks) and best tracks (b-decks)
  * https://www.nhc.noaa.gov/data/hurdat/ – HURDAT2, every Atlantic / NE Pacific storm since 1851 / 1949
  * https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt – buoys and coastal stations
"""
from __future__ import annotations

import gzip
import io
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QPolygonF

from ..data.aws import friendly_error
from ..products.geometry import aeqd_forward
from .bglayer import UA, BackgroundLayer

CURRENT_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
MAPSERVER = "https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer"
ATCF = "https://ftp.nhc.noaa.gov/atcf"
HURDAT_INDEX = "https://www.nhc.noaa.gov/data/"
NDBC_LATEST = "https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt"
KM_PER_NM = 1.852

# --------------------------------------------------------------------------- categories
CAT_COLORS = {  # Saffir-Simpson colours
    "TD": (94, 186, 255), "TS": (0, 250, 244), "1": (255, 255, 204), "2": (255, 231, 117), "3": (255, 193, 64),
    "4": (255, 143, 32), "5": (255, 96, 96), "EX": (200, 200, 200)}
CLASS_NAMES = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression", "STS": "Subtropical Storm",
               "STD": "Subtropical Depression", "PTC": "Potential Tropical Cyclone", "PC": "Post-Tropical Cyclone",
               "EX": "Extratropical", "LO": "Low", "DB": "Disturbance", "SS": "Subtropical Storm",
               "SD": "Subtropical Depression", "TY": "Typhoon", "MH": "Major Hurricane"}


def category(kt) -> str:
    """'TD', 'TS' or '1' … '5' for a wind in knots."""
    try:
        kt = float(kt)
    except (TypeError, ValueError):
        return "TD"
    if kt < 34:
        return "TD"
    if kt < 64:
        return "TS"
    for c, lim in (("1", 83), ("2", 96), ("3", 113), ("4", 137)):
        if kt < lim:
            return c
    return "5"


def cat_label(kt) -> str:
    c = category(kt)
    return {"TD": "Tropical depression", "TS": "Tropical storm"}.get(c, f"Category {c} hurricane")


def cat_color(kt) -> tuple:
    return CAT_COLORS[category(kt)]


def _get(url, timeout=30, **kw):
    r = requests.get(url, headers=UA, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def _t(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(deg):
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------- active storms
@dataclass
class TrackPoint:
    time: datetime | None
    lat: float
    lon: float
    wind: float | None = None         # kt
    pressure: float | None = None
    label: str = ""                    # "D", "S", "H", "M"… (NHC's forecast letters)
    tau: int | None = None


@dataclass
class Storm:
    id: str                            # al092026
    bin: str                           # AT1
    name: str
    classification: str
    wind: float                        # kt
    pressure: float | None
    lat: float
    lon: float
    move_dir: float | None
    move_kt: float | None
    updated: datetime | None
    advisory: str = ""
    links: dict = field(default_factory=dict)
    forecast: list = field(default_factory=list)       # [TrackPoint] (tau 0 first)
    track_line: list = field(default_factory=list)     # [(lon, lat) arrays]
    cone: list = field(default_factory=list)           # [(lon, lat) arrays]
    past: list = field(default_factory=list)           # [TrackPoint]
    warnings: list = field(default_factory=list)       # [(code, (lon, lat) array)]
    wind_now: list = field(default_factory=list)       # [(kt, (lon, lat) array)] advisory wind field
    radii: list = field(default_factory=list)          # [(tau, kt, {ne, se, sw, nw nm}, valid time)]
    radii_polys: list = field(default_factory=list)    # [(tau, kt, (lon, lat) array)]
    loaded: float = 0.0

    @property
    def title(self):
        cls = self.classification
        name = CLASS_NAMES.get(cls, cls)
        if cls == "HU" and self.wind >= 96:
            name = "Major Hurricane"
        return f"{name} {self.name}"

    @property
    def basin(self):
        return self.id[:2].upper()


def parse_current(js) -> list:
    """CurrentStorms.json -> [Storm]."""
    items = js.get("activeStorms", js if isinstance(js, list) else [js]) if isinstance(js, (dict, list)) else []
    if isinstance(items, dict):
        items = [items]
    out = []
    for s in items:
        if not isinstance(s, dict) or "id" not in s:
            continue
        try:
            lat = float(s.get("latitudeNumeric"))
            lon = float(s.get("longitudeNumeric"))
        except (TypeError, ValueError):
            continue
        links = {}
        for k, label in (("publicAdvisory", "Public advisory"), ("forecastDiscussion", "Forecast discussion"),
                         ("forecastAdvisory", "Forecast advisory"), ("windSpeedProbabilities", "Wind probabilities"),
                         ("forecastGraphics", "NHC graphics")):
            v = s.get(k)
            if isinstance(v, dict) and v.get("url"):
                links[label] = v["url"]
        adv = (s.get("publicAdvisory") or {}).get("advNum", "") if isinstance(s.get("publicAdvisory"), dict) else ""

        def num(k):
            try:
                return float(s.get(k))
            except (TypeError, ValueError):
                return None
        out.append(Storm(id=str(s["id"]).lower(), bin=str(s.get("binNumber") or ""), name=str(s.get("name") or "?"),
                         classification=str(s.get("classification") or "TS"), wind=num("intensity") or 0.0,
                         pressure=num("pressure"), lat=lat, lon=lon, move_dir=num("movementDir"),
                         move_kt=num("movementSpeed"), updated=_t(s.get("lastUpdate")), advisory=str(adv),
                         links=links))
    return out


def _geo_rings(geom):
    """(lon, lat) arrays of a GeoJSON geometry's lines / outer rings."""
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString":
        return [np.asarray(c, float)[:, :2]] if len(c) > 1 else []
    if t == "MultiLineString":
        return [np.asarray(p, float)[:, :2] for p in c if len(p) > 1]
    if t == "Polygon":
        return [np.asarray(c[0], float)[:, :2]] if c and len(c[0]) > 2 else []
    if t == "MultiPolygon":
        return [np.asarray(p[0], float)[:, :2] for p in c if p and len(p[0]) > 2]
    return []


def _valid_from(adv_time: datetime | None, text: str):
    """'15/1200' (day / HHMM, the advisory's month) -> datetime."""
    m = re.match(r"\s*(\d{1,2})/(\d{2})(\d{2})", str(text or ""))
    if not m or adv_time is None:
        return None
    d, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y, mo = adv_time.year, adv_time.month
    if d < adv_time.day - 15:              # into next month
        mo += 1
        if mo > 12:
            y, mo = y + 1, 1
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_forecast_points(js, adv_time) -> list:
    pts = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        tau = p.get("tau")
        t = _valid_from(adv_time, p.get("validtime"))
        if t is None and adv_time is not None and tau is not None:
            t = adv_time + timedelta(hours=float(tau))
        pts.append(TrackPoint(t, float(lat), float(lon), _f(p.get("maxwind")), _f(p.get("mslp")),
                              str(p.get("dvlbl") or ""), int(tau) if tau is not None else None))
    pts.sort(key=lambda q: (q.tau if q.tau is not None else 999))
    return pts


def parse_past_points(js) -> list:
    pts = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        t = None
        dtg = p.get("dtg")
        try:
            t = datetime.strptime(str(int(float(dtg))), "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            t = None
        pts.append(TrackPoint(t, float(lat), float(lon), _f(p.get("intensity")), _f(p.get("mslp")),
                              str(p.get("stormtype") or "")))
    pts.sort(key=lambda q: q.time or datetime.min.replace(tzinfo=timezone.utc))
    return pts


def parse_radii(js, adv_time) -> tuple:
    rows, polys = [], []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        kt = _f(p.get("radii"))
        tau = int(_f(p.get("tau")) or 0)
        if kt is None:
            continue
        q = {k: _f(p.get(k)) or 0.0 for k in ("ne", "se", "sw", "nw")}
        rows.append((tau, int(kt), q, _valid_from(adv_time, p.get("validtime"))))
        for ring in _geo_rings(f.get("geometry")):
            polys.append((tau, int(kt), ring))
    return rows, polys


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class NhcService:
    """Finds the MapServer layer numbers by name ("AT1 Forecast Cone") and queries them as GeoJSON."""

    def __init__(self):
        self._layers = {}
        self._at = 0.0

    def layers(self):
        if not self._layers or time.time() - self._at > 6 * 3600:
            js = _get(MAPSERVER + "?f=json", timeout=30).json()
            self._layers = {str(l.get("name", "")).strip(): int(l["id"]) for l in js.get("layers", []) if "id" in l}
            self._at = time.time()
        return self._layers

    def query(self, name):
        lid = self.layers().get(name)
        if lid is None:
            return {"features": []}
        return _get(f"{MAPSERVER}/{lid}/query", timeout=40,
                    params={"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson"}).json()


SERVICE = NhcService()


def load_storm_layers(st: Storm):
    adv = st.updated
    b = st.bin.upper()
    js = SERVICE.query(f"{b} Forecast Points")
    st.forecast = parse_forecast_points(js, adv)
    st.track_line = [r for f in SERVICE.query(f"{b} Forecast Track").get("features", [])
                     for r in _geo_rings(f.get("geometry"))]
    st.cone = [r for f in SERVICE.query(f"{b} Forecast Cone").get("features", []) for r in _geo_rings(f.get("geometry"))]
    st.past = parse_past_points(SERVICE.query(f"{b} Past Points"))
    st.warnings = [(str((f.get("properties") or {}).get("tcww") or ""), r)
                   for f in SERVICE.query(f"{b} Watch-Warning").get("features", []) for r in _geo_rings(f.get("geometry"))]
    st.radii, st.radii_polys = parse_radii(SERVICE.query(f"{b} Forecast Wind Radii"), adv)
    wf = []
    for f in SERVICE.query(f"{b} Advisory Wind Field").get("features", []):
        kt = _f((f.get("properties") or {}).get("radii"))
        for r in _geo_rings(f.get("geometry")):
            wf.append((int(kt or 34), r))
    st.wind_now = wf
    st.loaded = time.time()


WARN_STYLE = {"HWR": ((255, 0, 0), 6.0, "Hurricane warning"), "HWA": ((255, 127, 127), 6.0, "Hurricane watch"),
              "TWR": ((0, 77, 168), 4.0, "Tropical storm warning"), "TWA": ((255, 255, 0), 4.0, "Tropical storm watch")}


# --------------------------------------------------------------------------- outlook
@dataclass
class OutlookArea:
    ring: np.ndarray
    p2: str
    p7: str
    risk7: str
    basin: str


@dataclass
class OutlookPoint:
    lat: float
    lon: float
    p2: str
    p7: str


def load_outlook():
    areas, points = [], []
    for f in SERVICE.query("Seven-Day: Potential Development Region").get("features", []):
        p = f.get("properties") or {}
        for r in _geo_rings(f.get("geometry")):
            areas.append(OutlookArea(r, str(p.get("prob2day") or ""), str(p.get("prob7day") or ""),
                                     str(p.get("risk7day") or ""), str(p.get("basin") or "")))
    for name in ("Two-Day: Current Location", "Seven-Day: Current Location"):
        for f in SERVICE.query(name).get("features", []):
            p = f.get("properties") or {}
            g = f.get("geometry") or {}
            if g.get("type") == "Point":
                lon, lat = g["coordinates"][:2]
                if not any(abs(q.lat - lat) < 0.01 and abs(q.lon - lon) < 0.01 for q in points):
                    points.append(OutlookPoint(float(lat), float(lon), str(p.get("prob2day") or ""),
                                               str(p.get("prob7day") or "")))
    return areas, points


def outlook_color(p7: str):
    try:
        v = float(str(p7).strip("% "))
    except ValueError:
        return (255, 255, 0)
    return (230, 0, 0) if v >= 70 else (230, 152, 0) if v >= 40 else (255, 255, 0)


# --------------------------------------------------------------------------- model tracks (a-decks)
AID_NAMES = {"OFCL": "Official (NHC)", "AVNO": "GFS", "AVNI": "GFS", "HFAI": "HAFS-A", "HFBI": "HAFS-B",
             "HFSA": "HAFS-A", "HFSB": "HAFS-B", "HWRF": "HWRF", "HWFI": "HWRF", "CMC": "Canadian", "CMCI": "Canadian",
             "NVGM": "Navy", "NVGI": "Navy", "UKX": "UK Met", "UKXI": "UK Met", "UKM": "UK Met", "UKMI": "UK Met",
             "EMXI": "European", "EMX": "European", "ECMF": "European", "ECM": "European", "TVCN": "Consensus",
             "TVCA": "Consensus", "IVCN": "Consensus", "HCCA": "HCCA consensus", "CLP5": "CLIPER", "XTRP": "Extrapolation",
             "BAMD": "BAM deep", "BAMM": "BAM medium", "BAMS": "BAM shallow", "NNIB": "NN", "AEMN": "GFS ensemble mean",
             "AEMI": "GFS ensemble mean"}
AID_COLORS = {"OFCL": (255, 255, 255), "AVNO": (60, 140, 255), "AVNI": (60, 140, 255), "HFAI": (255, 90, 90),
              "HFBI": (255, 160, 60), "HWRF": (255, 120, 120), "HWFI": (255, 120, 120), "CMC": (120, 220, 120),
              "CMCI": (120, 220, 120), "NVGM": (190, 140, 255), "NVGI": (190, 140, 255), "UKX": (255, 220, 60),
              "UKXI": (255, 220, 60), "UKM": (255, 220, 60), "EMXI": (255, 80, 200), "EMX": (255, 80, 200),
              "TVCN": (240, 240, 140), "IVCN": (240, 240, 140), "CLP5": (160, 160, 160), "XTRP": (160, 160, 160),
              "AEMN": (110, 180, 255), "AEMI": (110, 180, 255)}


def _ll(text):
    """'254N' / '0885W' -> degrees."""
    t = str(text).strip()
    if not t or not t[:-1].strip().isdigit():
        return None
    v = int(t[:-1]) / 10.0
    return -v if t[-1] in "SW" else v


def parse_adeck(text: str, techs=None, newest_only=True) -> dict:
    """ATCF a-deck lines -> {tech: (cycle time, [TrackPoint])} (each tech's newest cycle)."""
    by = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        tech = parts[4].upper()
        if techs and tech not in techs:
            continue
        try:
            cyc = datetime.strptime(parts[2], "%Y%m%d%H").replace(tzinfo=timezone.utc)
            tau = int(parts[5])
        except ValueError:
            continue
        lat, lon = _ll(parts[6]), _ll(parts[7])
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        wind = _f(parts[8])
        by.setdefault(tech, {}).setdefault(cyc, {})[tau] = TrackPoint(cyc + timedelta(hours=tau), lat, lon,
                                                                      wind if wind and wind > 0 else None, tau=tau)
    out = {}
    newest = max((c for d in by.values() for c in d), default=None)
    for tech, cycles in by.items():
        if newest_only:
            cyc = max(cycles)
            if newest is not None and (newest - cyc) > timedelta(hours=12):
                continue                      # an old run of a model that hasn't run since
            out[tech] = (cyc, [cycles[cyc][k] for k in sorted(cycles[cyc])])
        else:
            out[tech] = {c: [pts[k] for k in sorted(pts)] for c, pts in cycles.items()}
    return out


def fetch_adeck(storm_id: str, year=None, archive=False) -> str:
    sid = storm_id.lower()
    if archive:
        y = year or int(sid[-4:])
        url = f"{ATCF}/archive/{y}/a{sid}.dat.gz"
    else:
        url = f"{ATCF}/aid_public/a{sid}.dat.gz"
    raw = _get(url, timeout=60).content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("latin-1", "replace")


# --------------------------------------------------------------------------- best tracks (HURDAT2, b-decks)
@dataclass
class PastStorm:
    id: str                       # AL092022
    name: str
    points: list                  # [TrackPoint] (label: status, e.g. HU / TS; "L" in .tau slot no – see landfalls)
    landfalls: list = field(default_factory=list)       # [TrackPoint]

    @property
    def year(self):
        return int(self.id[-4:])

    @property
    def peak(self):
        return max((p.wind or 0 for p in self.points), default=0)

    @property
    def title(self):
        nm = self.name.title() if self.name and self.name.upper() != "UNNAMED" else f"Storm {self.id[2:4]}"
        return f"{nm} ({self.year}) – {cat_label(self.peak).lower()}, {self.peak:.0f} kt"


def parse_hurdat2(text: str) -> list:
    out = []
    cur = None
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and re.match(r"^(AL|EP|CP)\d{6}$", parts[0]):
            cur = PastStorm(parts[0], parts[1], [])
            out.append(cur)
            continue
        if cur is None or len(parts) < 8:
            continue
        try:
            t = datetime.strptime(parts[0] + parts[1].zfill(4), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lat = _hll(parts[4])
        lon = _hll(parts[5])
        if lat is None or lon is None:
            continue
        w = _f(parts[6])
        p = _f(parts[7])
        tp = TrackPoint(t, lat, lon, w if w is not None and w >= 0 else None,
                        p if p is not None and p > 0 else None, parts[3])
        cur.points.append(tp)
        if parts[2] == "L":
            cur.landfalls.append(tp)
    return out


def _hll(text):
    t = str(text).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)([NSEW])$", t)
    if not m:
        return None
    v = float(m.group(1))
    return -v if m.group(2) in "SW" else v


def parse_bdeck(text: str, sid: str) -> PastStorm:
    pts = {}
    name = ""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue
        try:
            t = datetime.strptime(parts[2], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lat, lon = _ll(parts[6]), _ll(parts[7])
        if lat is None or lon is None:
            continue
        if len(parts) > 27 and parts[27]:
            name = parts[27]
        pts[t] = TrackPoint(t, lat, lon, _f(parts[8]), _f(parts[9]), parts[10])
    return PastStorm(sid.upper(), name or sid.upper(), [pts[k] for k in sorted(pts)])


_HURDAT = {}


def hurdat_storms(basin="AL") -> list:
    """Every storm in HURDAT2 for a basin (downloaded once, then kept in the cache folder)."""
    if basin in _HURDAT:
        return _HURDAT[basin]
    from .models import _cached
    idx = _cached("hurdat_index.html", lambda: _get(HURDAT_INDEX, timeout=30).content, max_age_s=7 * 86400)
    html = idx.decode("utf-8", "replace")
    pat = r"hurdat2-1851-\d{4}-\d+\.txt" if basin == "AL" else r"hurdat2-nepac-1949-\d{4}-\d+\.txt"
    names = sorted(set(re.findall(pat, html)))
    if not names:
        raise RuntimeError("couldn't find HURDAT2 on the NHC data page")
    name = names[-1]
    raw = _cached(name, lambda: _get(HURDAT_INDEX + "hurdat/" + name, timeout=120).content)
    storms = parse_hurdat2(raw.decode("latin-1", "replace"))
    _HURDAT[basin] = storms
    return storms


def recent_season(basin="al", year=None) -> list:
    """This season's storms (not yet in HURDAT2): best tracks from the ATCF b-decks."""
    year = year or datetime.now(timezone.utc).year
    html = _get(f"{ATCF}/btk/", timeout=30).text
    out = []
    for fn in sorted(set(re.findall(rf"b({basin}\d\d{year})\.dat", html))):
        try:
            out.append(parse_bdeck(_get(f"{ATCF}/btk/b{fn}.dat", timeout=30).text, fn))
        except Exception:
            continue
    return out


def key_moment(ps: PastStorm):
    """(time, lat, lon) worth replaying: its first US landfall, else its strongest point near land."""
    us = [p for p in ps.landfalls if -125 <= p.lon <= -64 and 17 <= p.lat <= 50]
    if us:
        p = us[0]
        return p.time, p.lat, p.lon
    near = [p for p in ps.points if -125 <= p.lon <= -64 and 17 <= p.lat <= 50]
    pool = near or ps.points
    p = max(pool, key=lambda q: q.wind or 0)
    return p.time, p.lat, p.lon


def tracks_near(storms, lat, lon, km=150.0, min_year=1950) -> list:
    out = []
    for s in storms:
        if s.year < min_year:
            continue
        best = min((distance_km(lat, lon, p.lat, p.lon) for p in s.points), default=1e9)
        if best <= km:
            out.append((s, best))
    out.sort(key=lambda x: -x[0].year)
    return out


# approximate cone circles (nm at 12…120 h) – NHC's recent sizes; used only for replays of past advisories
CONE_NM = {"AL": {0: 0, 12: 26, 24: 39, 36: 53, 48: 67, 60: 81, 72: 99, 96: 145, 120: 205},
           "EP": {0: 0, 12: 23, 24: 34, 36: 45, 48: 56, 60: 66, 72: 77, 96: 101, 120: 128}}


def cone_polygon(points: list, basin="AL", steps=24):
    """A cone (lon, lat ring) around forecast points: the union of circles, approximated by their hull."""
    sizes = CONE_NM.get(basin.upper()[:2], CONE_NM["AL"])
    pts = []
    for p in points:
        if p.tau is None or p.tau > 120:
            continue
        taus = sorted(sizes)
        r_nm = float(np.interp(p.tau, taus, [sizes[t] for t in taus]))
        r_deg = r_nm * KM_PER_NM / 111.0
        for k in range(steps):
            a = 2 * math.pi * k / steps
            pts.append((p.lon + r_deg * math.cos(a) / max(0.2, math.cos(math.radians(p.lat))),
                        p.lat + r_deg * math.sin(a)))
    if len(pts) < 3:
        return None
    return _hull(np.asarray(pts))


def _hull(p):
    p = sorted(map(tuple, p))
    if len(p) <= 2:
        return np.asarray(p)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    ring = lower[:-1] + upper[:-1]
    return np.asarray(ring + [ring[0]])


# --------------------------------------------------------------------------- arrival times
def interpolate_track(points: list, step_min=15):
    """[(time, lat, lon, wind)] every step along a track (points need times)."""
    pts = [p for p in points if p.time is not None]
    out = []
    for a, b in zip(pts, pts[1:]):
        n = max(1, int((b.time - a.time).total_seconds() / 60 / step_min))
        for k in range(n):
            f = k / n
            w = None if a.wind is None or b.wind is None else a.wind + (b.wind - a.wind) * f
            out.append((a.time + (b.time - a.time) * f, a.lat + (b.lat - a.lat) * f, a.lon + (b.lon - a.lon) * f, w))
    if pts:
        p = pts[-1]
        out.append((p.time, p.lat, p.lon, p.wind))
    return out


def _quadrant(bearing):
    return "ne" if bearing < 90 else "se" if bearing < 180 else "sw" if bearing < 270 else "nw"


def radius_at(radii, t, kt, quad):
    """Wind radius (km) of kt in a quadrant at time t, from the forecast radii (linear in time)."""
    rows = sorted((r[3], r[2][quad]) for r in radii if r[1] == kt and r[3] is not None)
    if not rows:
        return None
    ts = [r[0].timestamp() for r in rows]
    vs = [r[1] for r in rows]
    x = t.timestamp()
    if x < ts[0] - 3600 or x > ts[-1] + 3600:
        return None
    return float(np.interp(x, ts, vs)) * KM_PER_NM


def arrival(storm: Storm, lat, lon):
    """Closest approach and tropical-storm-force wind arrival at a point, from the official forecast."""
    pts = storm.forecast or [TrackPoint(storm.updated, storm.lat, storm.lon, storm.wind, tau=0)]
    track = interpolate_track(pts)
    if not track:
        return None
    best = None
    ts_arrive = ts_leave = hu_arrive = None
    for t, la, lo, w in track:
        d = distance_km(la, lo, lat, lon)
        if best is None or d < best[1]:
            best = (t, d, w, bearing_deg(lat, lon, la, lo))
        q = _quadrant(bearing_deg(la, lo, lat, lon))
        r34 = radius_at(storm.radii, t, 34, q)
        r64 = radius_at(storm.radii, t, 64, q)
        if r34 is not None and r34 > 0 and d <= r34:
            ts_arrive = ts_arrive or t
            ts_leave = t
        if r64 is not None and r64 > 0 and d <= r64:
            hu_arrive = hu_arrive or t
    return {"closest_time": best[0], "closest_km": best[1], "closest_wind": best[2], "closest_dir": best[3],
            "ts_arrive": ts_arrive, "ts_leave": ts_leave, "hu_arrive": hu_arrive}


# --------------------------------------------------------------------------- buoys
@dataclass
class Buoy:
    id: str
    lat: float
    lon: float
    time: datetime | None
    wdir: float | None
    wspd_kt: float | None
    gust_kt: float | None
    wave_ft: float | None
    pres: float | None
    air_f: float | None
    water_f: float | None


def parse_ndbc(text: str) -> list:
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 19:
            continue

        def v(i):
            try:
                return None if p[i] == "MM" else float(p[i])
            except (ValueError, IndexError):
                return None
        try:
            t = datetime(int(p[3]), int(p[4]), int(p[5]), int(p[6]), int(p[7]), tzinfo=timezone.utc)
        except ValueError:
            t = None
        c2f = lambda c: None if c is None else c * 1.8 + 32        # noqa: E731
        ms = lambda x: None if x is None else x * 1.943844         # noqa: E731
        out.append(Buoy(p[0], float(p[1]), float(p[2]), t, v(8), ms(v(9)), ms(v(10)),
                        None if v(11) is None else v(11) * 3.28084, v(15), c2f(v(17)), c2f(v(18))))
    return out


# --------------------------------------------------------------------------- the map layer
class TropicalOverlay(BackgroundLayer):
    """Active storms (with cone, track, wind field and coastal warnings), the outlook, model tracks and buoys;
    or, while replaying a past storm, its best track and the forecast of the time."""
    title = "Tropical"

    def __init__(self, settings, view, is_live, time_fn, parent=None):
        super().__init__(settings, parent, tick_ms=30_000)
        self.view = view
        self.is_live = is_live
        self.time_fn = time_fn
        self.storms: list = []
        self.outlook = ([], [])
        self.aids: dict = {}                 # storm id -> {tech: (cycle, [TrackPoint])}
        self.buoys: list = []
        self.replay: PastStorm | None = None
        self.replay_aids: dict = {}          # {tech: {cycle: [TrackPoint]}} (OFCL) for the replayed storm
        self.history: list = []              # [(PastStorm, km)] historic tracks near a place
        self.history_center = None
        self.selected = None                 # storm id
        self._proj = {}

    # ---------------------------------------------------------------- switches
    def ov(self, key, default=False):
        return bool(self.settings["overlays"].get(key, default))

    def enabled(self):
        return self.ov("tropical") or self.ov("tropical_outlook") or self.ov("buoys") or self.replay is not None \
            or bool(self.history)

    def jobs(self):
        jobs = []
        if self.ov("tropical") or self.ov("tropical_outlook"):
            jobs.append(("storms", self._load_storms, 10 * 60))
        if self.ov("tropical_outlook"):
            jobs.append(("outlook", self._load_outlook, 30 * 60))
        if self.ov("tropical") and self.ov("tropical_spaghetti") and self.storms:
            jobs.append(("aids", self._load_aids, 30 * 60))
        if self.ov("buoys"):
            jobs.append(("buoys", self._load_buoys, 10 * 60))
        return jobs

    def _load_storms(self):
        storms = parse_current(_get(CURRENT_URL, timeout=30).json())
        old = {s.id: s for s in self.storms}
        errors = []
        for s in storms:
            o = old.get(s.id)
            if o is not None and o.advisory == s.advisory and time.time() - o.loaded < 25 * 60 and o.forecast:
                s.forecast, s.track_line, s.cone, s.past = o.forecast, o.track_line, o.cone, o.past
                s.warnings, s.radii, s.radii_polys, s.wind_now, s.loaded = (o.warnings, o.radii, o.radii_polys,
                                                                            o.wind_now, o.loaded)
                continue
            try:
                load_storm_layers(s)
            except Exception as exc:
                errors.append(f"{s.name}: {friendly_error(exc)}")
        self.storms = storms
        self._proj.clear()
        n = len(storms)
        self.status.emit("Tropical: " + (f"{n} active storm" + ("s" if n != 1 else "") if n else "no active storms") +
                         (f" ({'; '.join(errors)})" if errors else ""))

    def _load_outlook(self):
        self.outlook = load_outlook()
        self._proj.clear()

    def _load_aids(self):
        techs = set(self.settings["tropical_models"] or []) | {"OFCL"}
        out = {}
        for s in self.storms:
            try:
                out[s.id] = parse_adeck(fetch_adeck(s.id), techs)
            except Exception as exc:
                self.status.emit(f"Model tracks for {s.name}: {friendly_error(exc)}")
        self.aids = out
        self._proj.clear()

    def _load_buoys(self):
        self.buoys = parse_ndbc(_get(NDBC_LATEST, timeout=40).text)
        self._proj.clear()

    # ---------------------------------------------------------------- replays and history
    def set_replay(self, ps: PastStorm | None):
        self.replay = ps
        self.replay_aids = {}
        self._proj.clear()
        if ps is None:
            return

        def work():
            try:
                txt = fetch_adeck(ps.id.lower(), ps.year, archive=ps.year < datetime.now(timezone.utc).year)
                aids = parse_adeck(txt, {"OFCL"}, newest_only=False)
                if self.replay is ps:
                    self.replay_aids = aids.get("OFCL", {})
                    self._proj.clear()
                    self.changed.emit()
            except Exception as exc:
                self.status.emit(f"Past forecasts for {ps.name.title()}: {friendly_error(exc)} (best track only)")
        threading.Thread(target=work, daemon=True).start()

    def set_history(self, items, center):
        self.history = items or []
        self.history_center = center
        self._proj.clear()

    # ---------------------------------------------------------------- projection
    def _xy(self, key, lon_lat, view):
        k = (key, view.lat0, view.lon0)
        hit = self._proj.get(k)
        if hit is None:
            a = np.asarray(lon_lat, float)
            x, y = aeqd_forward(a[:, 1], a[:, 0], view.lat0, view.lon0)
            hit = np.stack([x, y], 1)
            if len(self._proj) > 3000:
                self._proj.clear()
            self._proj[k] = hit
        return hit

    def _pt(self, lat, lon, view):
        x, y = aeqd_forward(lat, lon, view.lat0, view.lon0)
        return float(x), float(y)

    @staticmethod
    def _poly(vt, xy):
        sx, sy = vt.to_screen(xy[:, 0], xy[:, 1])
        return QPolygonF([QPointF(a, b) for a, b in zip(sx.tolist(), sy.tolist())])

    # ---------------------------------------------------------------- drawing
    def paint(self, painter, vt, panel, view):
        font = QFont(view.font_small)
        font.setBold(True)
        if self.history:
            self._paint_history(painter, vt, view, font)
        if self.ov("tropical_outlook"):
            self._paint_outlook(painter, vt, view, font)
        if self.replay is not None:
            self._paint_replay(painter, vt, view, font)
        elif self.ov("tropical"):
            for s in self.storms:
                self._paint_storm(painter, vt, view, s, font)
        if self.ov("buoys"):
            self._paint_buoys(painter, vt, view, font)

    def _paint_storm(self, painter, vt, view, s: Storm, font):
        # wind field now (and faint forecast radii)
        if self.ov("tropical_wind", True):
            for kt, ring in sorted(s.wind_now, key=lambda x: x[0]):
                col = {34: (255, 230, 60, 70), 50: (255, 150, 30, 85), 64: (230, 30, 30, 100)}.get(kt, (255, 230, 60, 60))
                painter.setPen(QPen(QColor(*col[:3], 200), 1.2))
                painter.setBrush(QColor(*col))
                painter.drawPolygon(self._poly(vt, self._xy(("wn", s.id, kt, id(ring)), ring, view)))
            painter.setBrush(Qt.NoBrush)
        # cone
        for ring in s.cone:
            poly = self._poly(vt, self._xy(("cone", s.id, id(ring)), ring, view))
            painter.setPen(QPen(QColor(255, 255, 255, 220), 1.6))
            painter.setBrush(QColor(255, 255, 255, 45))
            painter.drawPolygon(poly)
            painter.setBrush(Qt.NoBrush)
        # coastal watches / warnings
        for code, ring in s.warnings:
            rgb, w, _label = WARN_STYLE.get(code, ((255, 255, 255), 3.0, code))
            poly = self._poly(vt, self._xy(("ww", s.id, id(ring)), ring, view))
            painter.setPen(QPen(QColor(0, 0, 0, 200), w + 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(poly)
            painter.setPen(QPen(QColor(*rgb), w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(poly)
        # past track
        self._paint_track(painter, vt, view, s.past, ("past", s.id), dashed=False, dots=True)
        # forecast track + points
        if s.forecast:
            line = np.array([[p.lon, p.lat] for p in s.forecast])
            if len(line) > 1:
                poly = self._poly(vt, self._xy(("ft", s.id, len(line)), line, view))
                painter.setPen(QPen(QColor(0, 0, 0, 200), 3.6))
                painter.drawPolyline(poly)
                painter.setPen(QPen(QColor(255, 255, 255), 1.6, Qt.DashLine))
                painter.drawPolyline(poly)
            for p in s.forecast[1:]:
                x, y = self._pt(p.lat, p.lon, view)
                sx, sy = vt.to_screen(x, y)
                letter = p.label[:1] if p.label else ("H" if (p.wind or 0) >= 64 else "S" if (p.wind or 0) >= 34 else "D")
                if (p.wind or 0) >= 96:
                    letter = "M"
                self._dot(painter, sx, sy, cat_color(p.wind), letter, 7.5)
                if vt.km_across < 2500 and p.time is not None:
                    view._halo_text(painter, sx + 10, sy + 4, f"{p.time.astimezone():%a %-I %p}", QColor(240, 240, 240),
                                    font)
        if self.ov("tropical_spaghetti") and s.id in self.aids:
            self._paint_aids(painter, vt, view, s.id, self.aids[s.id], font)
        # the storm itself
        x, y = self._pt(s.lat, s.lon, view)
        sx, sy = vt.to_screen(x, y)
        self._hurricane_symbol(painter, sx, sy, s.wind, s.classification)
        label = f"{s.title}  {s.wind:.0f} kt" + (f" · {s.pressure:.0f} mb" if s.pressure else "")
        view._halo_text(painter, sx + 16, sy - 12, label, QColor(255, 255, 255), font)

    def _paint_track(self, painter, vt, view, pts, key, dashed=False, dots=True, width=2.2):
        pts = [p for p in pts if p.lat is not None]
        if len(pts) < 2:
            return
        xy = self._xy(key + (len(pts),), np.array([[p.lon, p.lat] for p in pts]), view)
        sx, sy = vt.to_screen(xy[:, 0], xy[:, 1])
        for i in range(len(pts) - 1):
            col = QColor(*cat_color(pts[i + 1].wind))
            painter.setPen(QPen(QColor(0, 0, 0, 180), width + 2.0))
            painter.drawLine(QPointF(sx[i], sy[i]), QPointF(sx[i + 1], sy[i + 1]))
            painter.setPen(QPen(col, width, Qt.DashLine if dashed else Qt.SolidLine))
            painter.drawLine(QPointF(sx[i], sy[i]), QPointF(sx[i + 1], sy[i + 1]))
        if dots and vt.km_across < 3500:
            for i, p in enumerate(pts):
                painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
                painter.setBrush(QColor(*cat_color(p.wind)))
                painter.drawEllipse(QPointF(sx[i], sy[i]), 3.0, 3.0)
            painter.setBrush(Qt.NoBrush)

    def _paint_aids(self, painter, vt, view, sid, aids, font):
        for tech, (cyc, pts) in sorted(aids.items(), key=lambda kv: kv[0] == "OFCL"):
            if len(pts) < 2:
                continue
            rgb = AID_COLORS.get(tech, (200, 200, 200))
            xy = self._xy(("aid", sid, tech, cyc), np.array([[p.lon, p.lat] for p in pts]), view)
            poly = self._poly(vt, xy)
            w = 2.6 if tech == "OFCL" else 1.5
            painter.setPen(QPen(QColor(0, 0, 0, 150), w + 1.6))
            painter.drawPolyline(poly)
            painter.setPen(QPen(QColor(*rgb), w))
            painter.drawPolyline(poly)
            end = poly[poly.size() - 1]
            if vt.rect.contains(end):
                view._halo_text(painter, end.x() + 3, end.y() + 4, AID_NAMES.get(tech, tech).split(" (")[0],
                                QColor(*rgb), font)

    def _paint_outlook(self, painter, vt, view, font):
        areas, points = self.outlook
        for i, a in enumerate(areas):
            rgb = outlook_color(a.p7)
            poly = self._poly(vt, self._xy(("two", i, a.p7), a.ring, view))
            painter.setPen(QPen(QColor(*rgb), 2.0))
            br = QBrush(QColor(*rgb, 110), Qt.BDiagPattern)
            painter.setBrush(br)
            painter.drawPolygon(poly)
            painter.setBrush(Qt.NoBrush)
            c = poly.boundingRect().center()
            if vt.rect.contains(c):
                view._halo_text(painter, c.x() - 30, c.y(), f"2 d {a.p2 or '–'} · 7 d {a.p7 or '–'}", QColor(*rgb),
                                font)
        for p in points:
            x, y = self._pt(p.lat, p.lon, view)
            sx, sy = vt.to_screen(x, y)
            rgb = outlook_color(p.p7 or p.p2)
            painter.setPen(QPen(QColor(0, 0, 0), 5.0, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(sx - 7, sy - 7), QPointF(sx + 7, sy + 7))
            painter.drawLine(QPointF(sx - 7, sy + 7), QPointF(sx + 7, sy - 7))
            painter.setPen(QPen(QColor(*rgb), 3.0, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(sx - 7, sy - 7), QPointF(sx + 7, sy + 7))
            painter.drawLine(QPointF(sx - 7, sy + 7), QPointF(sx + 7, sy - 7))

    def _paint_replay(self, painter, vt, view, font):
        ps = self.replay
        t = self.time_fn() or (ps.points[-1].time if ps.points else None)
        self._paint_track(painter, vt, view, ps.points, ("rp", ps.id), dots=True)
        if t is None or not ps.points:
            return
        # the forecast issued last before the scan (with an approximate cone)
        if self.replay_aids:
            cycles = [c for c in self.replay_aids if c <= t]
            if cycles:
                cyc = max(cycles)
                pts = self.replay_aids[cyc]
                cone = cone_polygon(pts, ps.id[:2])
                if cone is not None:
                    poly = self._poly(vt, self._xy(("rcone", ps.id, cyc), cone, view))
                    painter.setPen(QPen(QColor(255, 255, 255, 200), 1.4))
                    painter.setBrush(QColor(255, 255, 255, 40))
                    painter.drawPolygon(poly)
                    painter.setBrush(Qt.NoBrush)
                line = np.array([[p.lon, p.lat] for p in pts])
                if len(line) > 1:
                    poly = self._poly(vt, self._xy(("rft", ps.id, cyc), line, view))
                    painter.setPen(QPen(QColor(255, 255, 255), 1.6, Qt.DashLine))
                    painter.drawPolyline(poly)
                end = self._poly(vt, self._xy(("rft", ps.id, cyc), line, view))
                if end.size():
                    q = end[end.size() - 1]
                    view._halo_text(painter, q.x() + 4, q.y() + 4, f"NHC forecast {cyc:%HZ %d %b} (approx. cone)",
                                    QColor(230, 230, 230), font)
        # where the storm was at the scan's time
        track = interpolate_track(ps.points, 10)
        if track:
            near = min(track, key=lambda r: abs((r[0] - t).total_seconds()))
            if abs((near[0] - t).total_seconds()) < 6 * 3600:
                x, y = self._pt(near[1], near[2], view)
                sx, sy = vt.to_screen(x, y)
                self._hurricane_symbol(painter, sx, sy, near[3] or 0, "HU" if (near[3] or 0) >= 64 else "TS")
                view._halo_text(painter, sx + 16, sy - 12, f"{ps.name.title()}  {near[3] or 0:.0f} kt (best track)",
                                QColor(255, 255, 255), font)

    def _paint_history(self, painter, vt, view, font):
        for ps, _km in self.history:
            self._paint_track(painter, vt, view, ps.points, ("hist", ps.id), dots=False, width=1.6)
            pts = [p for p in ps.points]
            if pts and vt.km_across < 3000:
                best = max(pts, key=lambda p: p.wind or 0)
                x, y = self._pt(best.lat, best.lon, view)
                sx, sy = vt.to_screen(x, y)
                if vt.rect.contains(QPointF(sx, sy)):
                    view._halo_text(painter, sx + 4, sy - 4, f"{ps.name.title()} {ps.year}", QColor(*cat_color(best.wind)),
                                    font)

    def _paint_buoys(self, painter, vt, view, font):
        x0, y0, x1, y1 = vt.world_bounds(pad=20)
        if vt.km_across > 3000:
            return
        from .obs import barb_lines
        cell = 56.0
        taken = set()
        for b in self.buoys:
            x, y = self._pt(b.lat, b.lon, view)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            sx, sy = vt.to_screen(x, y)
            c = (int(sx // cell), int(sy // cell))
            if c in taken:
                continue
            taken.add(c)
            if b.wspd_kt is not None and b.wdir is not None and b.wspd_kt >= 2.5:
                lines, tris = barb_lines(sx, sy, b.wdir, b.wspd_kt, 20.0)
                painter.setPen(QPen(QColor(0, 0, 0, 200), 3.0))
                painter.drawLines(lines)
                painter.setPen(QPen(QColor(140, 230, 255), 1.3))
                painter.drawLines(lines)
                painter.setBrush(QColor(140, 230, 255))
                for t in tris:
                    painter.drawPolygon(t)
            painter.setPen(QPen(QColor(0, 0, 0), 1))
            painter.setBrush(QColor(255, 210, 60))
            painter.drawRect(QRectF(sx - 3.5, sy - 3.5, 7, 7))
            painter.setBrush(Qt.NoBrush)
            bits = []
            if b.gust_kt is not None:
                bits.append(f"G{b.gust_kt:.0f}")
            if b.wave_ft is not None:
                bits.append(f"{b.wave_ft:.0f} ft")
            if bits and vt.km_across < 1200:
                view._halo_text(painter, sx + 6, sy + 12, " ".join(bits), QColor(140, 230, 255), font)

    @staticmethod
    def _dot(painter, sx, sy, rgb, letter, r):
        painter.setPen(QPen(QColor(0, 0, 0), 1.2))
        painter.setBrush(QColor(*rgb))
        painter.drawEllipse(QPointF(sx, sy), r, r)
        painter.setBrush(Qt.NoBrush)
        f = painter.font()
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(QRectF(sx - r, sy - r, 2 * r, 2 * r), Qt.AlignCenter, letter)
        painter.setFont(f)

    @staticmethod
    def _hurricane_symbol(painter, sx, sy, wind, cls):
        """The usual cyclone glyph: a disc with two curved arms (just a disc for depressions)."""
        rgb = cat_color(wind)
        r = 8.0
        if (wind or 0) >= 34:
            path = QPainterPath()
            path.moveTo(sx, sy - r)
            path.cubicTo(sx + r * 0.2, sy - r * 2.2, sx + r * 1.8, sy - r * 2.0, sx + r * 1.9, sy - r * 1.7)
            path.cubicTo(sx + r * 0.9, sy - r * 1.4, sx + r * 0.6, sy - r * 0.6, sx + r * 0.9, sy - r * 0.2)
            path.moveTo(sx, sy + r)
            path.cubicTo(sx - r * 0.2, sy + r * 2.2, sx - r * 1.8, sy + r * 2.0, sx - r * 1.9, sy + r * 1.7)
            path.cubicTo(sx - r * 0.9, sy + r * 1.4, sx - r * 0.6, sy + r * 0.6, sx - r * 0.9, sy + r * 0.2)
            painter.setPen(QPen(QColor(0, 0, 0), 4.0, Qt.SolidLine, Qt.RoundCap))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(*rgb), 2.4, Qt.SolidLine, Qt.RoundCap))
            painter.drawPath(path)
        painter.setPen(QPen(QColor(0, 0, 0), 1.6))
        painter.setBrush(QColor(*rgb))
        painter.drawEllipse(QPointF(sx, sy), r, r)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawEllipse(QPointF(sx, sy), r * 0.35, r * 0.35)
        painter.setBrush(Qt.NoBrush)

    # ---------------------------------------------------------------- hover / click
    def storm_at(self, x, y, tol):
        if not self.ov("tropical") or self.replay is not None:
            return None
        view = self.view
        for s in self.storms:
            sx, sy = self._pt(s.lat, s.lon, view)
            if math.hypot(sx - x, sy - y) < tol * 2.2:
                return s
        return None

    def hover(self, x, y, tol):
        s = self.storm_at(x, y, tol)
        if s is not None:
            mv = (f"\nMoving {compass(s.move_dir)} at {s.move_kt:.0f} kt" if s.move_dir is not None and s.move_kt
                  else "")
            return (f"{s.title}\n{cat_label(s.wind)}: {s.wind:.0f} kt ({s.wind * 1.15078:.0f} mph)"
                    + (f", {s.pressure:.0f} mb" if s.pressure else "") + mv +
                    (f"\nAdvisory {s.advisory}, {s.updated:%H:%MZ %d %b}" if s.updated else "") +
                    "\nClick for details, arrival times and links")
        if self.ov("tropical") and self.replay is None:
            for s in self.storms:
                for p in s.forecast[1:]:
                    px, py = self._pt(p.lat, p.lon, self.view)
                    if math.hypot(px - x, py - y) < tol * 1.5:
                        return (f"{s.name}: forecast for {p.time.astimezone():%a %-I %p}" if p.time else s.name) + \
                            f"\n{cat_label(p.wind)}, {p.wind or 0:.0f} kt" + (f" (+{p.tau} h)" if p.tau else "")
        if self.ov("buoys"):
            best, bd = None, tol * 1.6
            for b in self.buoys:
                bx, by = self._pt(b.lat, b.lon, self.view)
                d = math.hypot(bx - x, by - y)
                if d < bd:
                    best, bd = b, d
            if best is not None:
                b = best
                bits = [f"Buoy / station {b.id}" + (f" · {b.time:%H:%MZ}" if b.time else "")]
                if b.wspd_kt is not None:
                    bits.append(f"Wind {compass(b.wdir or 0)} {b.wspd_kt:.0f} kt" +
                                (f", gusts {b.gust_kt:.0f} kt" if b.gust_kt is not None else ""))
                if b.pres is not None:
                    bits.append(f"Pressure {b.pres:.1f} mb")
                if b.wave_ft is not None:
                    bits.append(f"Waves {b.wave_ft:.0f} ft")
                if b.water_f is not None:
                    bits.append(f"Water {b.water_f:.0f} °F")
                return "\n".join(bits)
        if self.ov("tropical_outlook"):
            for p in self.outlook[1]:
                px, py = self._pt(p.lat, p.lon, self.view)
                if math.hypot(px - x, py - y) < tol * 1.8:
                    return f"Area to watch (NHC outlook)\nChance of forming: {p.p2 or '–'} in 2 days, {p.p7 or '–'} in 7 days"
        return None

    def caption(self):
        if self.replay is not None:
            return f"Replay: {self.replay.title}"
        if self.ov("tropical") and self.storms:
            return "NHC: " + ", ".join(s.name for s in self.storms)
        return None
