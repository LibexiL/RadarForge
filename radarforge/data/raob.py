"""Observed soundings (weather balloon launches, twice a day) from the Iowa Environmental Mesonet.

The JSON service answers `?station=KOUN&ts=202610021200` with the levels of that launch: pressure (hPa), height (m),
temperature and dewpoint (°C), wind direction (degrees) and speed (knots).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from ..config import CACHE_DIR
from ..services.sounding import Sounding, wind_components

PROFILE_URL = "https://mesonet.agron.iastate.edu/json/raob.py"
STATIONS_URL = "https://mesonet.agron.iastate.edu/geojson/network/RAOB.geojson"
UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)"}

# the US launch sites (id, name, lat, lon), used when the station list can't be downloaded
BUILTIN_STATIONS = [
    ("KABQ", "Albuquerque NM", 35.04, -106.62), ("KABR", "Aberdeen SD", 45.45, -98.41), ("KALB", "Albany NY", 42.69, -73.83),
    ("KAMA", "Amarillo TX", 35.23, -101.71), ("KBIS", "Bismarck ND", 46.77, -100.76), ("KBMX", "Birmingham AL", 33.17, -86.77),
    ("KBNA", "Nashville TN", 36.25, -86.57), ("KBOI", "Boise ID", 43.57, -116.21), ("KBRO", "Brownsville TX", 25.92, -97.42),
    ("KBUF", "Buffalo NY", 42.94, -78.74), ("KCAR", "Caribou ME", 46.87, -68.02), ("KCHH", "Chatham MA", 41.67, -69.97),
    ("KCHS", "Charleston SC", 32.90, -80.03), ("KCRP", "Corpus Christi TX", 27.78, -97.51), ("KDDC", "Dodge City KS", 37.76, -99.97),
    ("KDNR", "Denver CO", 39.77, -104.87), ("KDRT", "Del Rio TX", 29.37, -100.92), ("KDTX", "Detroit MI", 42.70, -83.47),
    ("KDVN", "Davenport IA", 41.61, -90.58), ("KEPZ", "Santa Teresa NM", 31.87, -106.70), ("KEYW", "Key West FL", 24.55, -81.79),
    ("KFFC", "Peachtree City GA", 33.36, -84.57), ("KFGZ", "Flagstaff AZ", 35.23, -111.82), ("KFWD", "Fort Worth TX", 32.83, -97.30),
    ("KGGW", "Glasgow MT", 48.21, -106.63), ("KGJT", "Grand Junction CO", 39.12, -108.53), ("KGRB", "Green Bay WI", 44.50, -88.11),
    ("KGSO", "Greensboro NC", 36.10, -79.94), ("KGYX", "Gray ME", 43.89, -70.25), ("KILN", "Wilmington OH", 39.42, -83.82),
    ("KILX", "Lincoln IL", 40.15, -89.34), ("KINL", "International Falls MN", 48.57, -93.40), ("KJAN", "Jackson MS", 32.32, -90.08),
    ("KJAX", "Jacksonville FL", 30.50, -81.70), ("KLBF", "North Platte NE", 41.13, -100.68), ("KLCH", "Lake Charles LA", 30.12, -93.22),
    ("KLIX", "Slidell LA", 30.34, -89.83), ("KLKN", "Elko NV", 40.87, -115.73), ("KLZK", "Little Rock AR", 34.84, -92.26),
    ("KMAF", "Midland TX", 31.95, -102.19), ("KMFL", "Miami FL", 25.75, -80.38), ("KMFR", "Medford OR", 42.37, -122.87),
    ("KMHX", "Newport NC", 34.78, -76.88), ("KMPX", "Chanhassen MN", 44.85, -93.57), ("KOAK", "Oakland CA", 37.73, -122.22),
    ("KOKX", "Upton NY", 40.87, -72.86), ("KOTX", "Spokane WA", 47.68, -117.63), ("KOUN", "Norman OK", 35.18, -97.44),
    ("KPIT", "Pittsburgh PA", 40.53, -80.22), ("KREV", "Reno NV", 39.57, -119.80), ("KRIW", "Riverton WY", 43.06, -108.48),
    ("KRNK", "Blacksburg VA", 37.20, -80.41), ("KSGF", "Springfield MO", 37.24, -93.40), ("KSHV", "Shreveport LA", 32.45, -93.84),
    ("KSLC", "Salt Lake City UT", 40.77, -111.95), ("KSLE", "Salem OR", 44.92, -123.03), ("KTBW", "Tampa Bay FL", 27.70, -82.40),
    ("KTFX", "Great Falls MT", 47.46, -111.38), ("KTLH", "Tallahassee FL", 30.40, -84.35), ("KTOP", "Topeka KS", 39.07, -95.63),
    ("KTUS", "Tucson AZ", 32.23, -110.96), ("KUIL", "Quillayute WA", 47.93, -124.55), ("KUNR", "Rapid City SD", 44.07, -103.21),
    ("KVBG", "Vandenberg CA", 34.75, -120.57), ("KVEF", "Las Vegas NV", 36.05, -115.18), ("KWAL", "Wallops Island VA", 37.93, -75.47),
    ("KXMR", "Cape Canaveral FL", 28.48, -80.55),
]


def latest_launch(now: datetime | None = None) -> datetime:
    """The newest regular launch (00Z or 12Z) that should be available: data arrives 1-2 hours after the launch."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(hours=1, minutes=30)
    return now.replace(hour=12 if now.hour >= 12 else 0, minute=0, second=0, microsecond=0)


def previous_launch(t: datetime) -> datetime:
    return t - timedelta(hours=12)


def parse_stations(js: dict) -> list:
    """[(id, name, lat, lon)] from the station GeoJSON."""
    out = []
    for f in (js or {}).get("features", []):
        p = f.get("properties") or {}
        g = (f.get("geometry") or {}).get("coordinates") or []
        sid = str(p.get("sid") or p.get("id") or f.get("id") or "").upper()
        if len(g) >= 2 and sid:
            out.append((sid, str(p.get("sname") or p.get("name") or sid), float(g[1]), float(g[0])))
    return sorted(out)


def stations(refresh: bool = False) -> list:
    """The launch sites: the downloaded list (kept in the cache folder), else the built-in one."""
    path = CACHE_DIR / "raob_stations.json"
    try:
        if not refresh and path.exists():
            got = parse_stations(json.loads(path.read_text(encoding="utf-8")))
            if got:
                return got
        r = requests.get(STATIONS_URL, headers=UA, timeout=15)
        r.raise_for_status()
        got = parse_stations(r.json())
        if got:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(r.text, encoding="utf-8")
            return got
    except Exception:
        pass
    return list(BUILTIN_STATIONS)


def _get(d: dict, *names):
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return None


def parse_profile(js, station: str = "", name: str = "", lat: float = 0.0, lon: float = 0.0) -> Sounding:
    """A Sounding from the service's JSON (a dict with 'profiles', or the list of profiles itself)."""
    profiles = js.get("profiles") if isinstance(js, dict) else js
    if not profiles:
        raise ValueError("no sounding for this station and time")
    prof = profiles[0]
    levels = prof.get("profile") or []
    rows = []
    for lv in levels:
        p, z, t, td = (_get(lv, "pres", "pressure"), _get(lv, "hght", "height", "hgt"), _get(lv, "tmpc", "temp", "tmpk"),
                       _get(lv, "dwpc", "dwpt", "dewpoint"))
        if None in (p, z, t, td):
            continue
        rows.append((float(p), float(z), float(t), float(td), _get(lv, "drct", "dir", "direction"),
                     _get(lv, "sknt", "speed", "spd")))
    if len(rows) < 5:
        raise ValueError("this sounding has too few levels")
    a = np.array([[r[0], r[1], r[2], r[3]] for r in rows], float)
    d = np.array([np.nan if r[4] is None else float(r[4]) for r in rows])
    s = np.array([np.nan if r[5] is None else float(r[5]) for r in rows])
    u, v = wind_components(d, s)
    valid = None
    try:
        valid = datetime.fromisoformat(str(prof.get("valid")).replace("Z", "+00:00"))
    except ValueError:
        pass
    return Sounding.build(a[:, 0], a[:, 1], a[:, 2], a[:, 3], u, v, source=f"Observed {prof.get('station') or station}",
                          place=name or station, lat=lat, lon=lon, valid=valid)


def fetch(station: str, when: datetime, name: str = "", lat: float = 0.0, lon: float = 0.0) -> Sounding:
    r = requests.get(PROFILE_URL, params={"station": station, "ts": when.strftime("%Y%m%d%H%M")}, headers=UA, timeout=30)
    r.raise_for_status()
    return parse_profile(r.json(), station, name, lat, lon)


def nearest_station(lat: float, lon: float, items: list | None = None):
    """(id, name, lat, lon, km) of the launch site closest to a point."""
    from ..services.geo import distance_km
    items = items or stations()
    best = min(items, key=lambda s: distance_km(lat, lon, s[2], s[3]))
    return (*best, distance_km(lat, lon, best[2], best[3]))
