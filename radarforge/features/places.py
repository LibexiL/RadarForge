"""Place search: towns from the built-in map data (instant, offline) and OpenStreetMap's Nominatim search
(towns, roads, addresses, landmarks; one request per search, as its usage policy asks), plus the marker that
shows the place found on the map.
"""
from __future__ import annotations

import re
import threading
import time

import numpy as np
import requests
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..products.geometry import aeqd_forward

NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "RadarForge (NEXRAD radar viewer; github.com/LibexiL/RadarForge)"}
ATTRIBUTION = "Search results © OpenStreetMap contributors"

STATE_FIPS = {
    1: "AL", 2: "AK", 4: "AZ", 5: "AR", 6: "CA", 8: "CO", 9: "CT", 10: "DE", 11: "DC", 12: "FL", 13: "GA",
    15: "HI", 16: "ID", 17: "IL", 18: "IN", 19: "IA", 20: "KS", 21: "KY", 22: "LA", 23: "ME", 24: "MD",
    25: "MA", 26: "MI", 27: "MN", 28: "MS", 29: "MO", 30: "MT", 31: "NE", 32: "NV", 33: "NH", 34: "NJ",
    35: "NM", 36: "NY", 37: "NC", 38: "ND", 39: "OH", 40: "OK", 41: "OR", 42: "PA", 44: "RI", 45: "SC",
    46: "SD", 47: "TN", 48: "TX", 49: "UT", 50: "VT", 51: "VA", 53: "WA", 54: "WV", 55: "WI", 56: "WY",
    60: "AS", 66: "GU", 69: "MP", 72: "PR", 78: "VI",
}
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}

_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coords(text: str):
    """(lat, lon) from "35.22, -97.44", or None."""
    m = _COORDS.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def split_state(text: str):
    """("springfield", "MO") from "Springfield, MO" / "springfield missouri"; the state is None if not given."""
    t = " ".join((text or "").replace(",", " , ").split()).strip().lower()
    if "," in t:
        name, rest = (p.strip() for p in t.split(",", 1))
        st = rest.upper() if len(rest) == 2 else STATE_NAMES.get(rest)
        if st:
            return name, st
        return t.replace(" ,", ","), None
    words = t.split()
    for n in (2, 1):
        if len(words) > n:
            tail = " ".join(words[-n:])
            st = STATE_NAMES.get(tail) or (tail.upper() if n == 1 and len(tail) == 2 and
                                            tail.upper() in STATE_FIPS.values() else None)
            if st:
                return " ".join(words[:-n]), st
    return t, None


class LocalPlaces:
    """Towns and cities in the built-in map data (name, position, population)."""

    def __init__(self, raw: dict):
        self.names = list(map(str, raw.get("city_name", [])))
        self.lower = [n.lower() for n in self.names]
        self.lat = np.asarray(raw.get("city_lat", []), float)
        self.lon = np.asarray(raw.get("city_lon", []), float)
        self.pop = np.asarray(raw.get("city_pop", []), np.int64)
        self._raw = raw
        self._county = None
        self._state_cache: dict = {}

    # ------------------------------------------------------------------ which state (from the county shapes)
    def _counties(self):
        if self._county is None:
            raw = self._raw
            if "counties_pts" not in raw:
                self._county = ()
                return self._county
            pts = np.asarray(raw["counties_pts"], float)
            st = np.asarray(raw["counties_starts"], np.int64)
            fips = np.asarray(raw["counties_fips"], np.int64)
            n = len(st) - 1
            lo, la = pts[:, 0], pts[:, 1]
            seg = st[:-1]
            bbox = np.stack([np.minimum.reduceat(lo, seg), np.maximum.reduceat(lo, seg),
                             np.minimum.reduceat(la, seg), np.maximum.reduceat(la, seg)], 1)
            self._county = (pts, st, fips[:n], bbox[:n])
        return self._county

    def state_at(self, lat: float, lon: float) -> str:
        """US state (or territory) abbreviation at a point, or "" outside the US counties."""
        key = (round(lat, 3), round(lon, 3))
        if key in self._state_cache:
            return self._state_cache[key]
        c = self._counties()
        out = ""
        if c:
            pts, st, fips, bbox = c
            cand = np.nonzero((bbox[:, 0] <= lon) & (bbox[:, 1] >= lon) & (bbox[:, 2] <= lat) & (bbox[:, 3] >= lat))[0]
            for i in cand:
                ring = pts[st[i]:st[i + 1]]
                if _inside(lon, lat, ring):
                    out = STATE_FIPS.get(int(fips[i]) // 1000, "")
                    break
        self._state_cache[key] = out
        return out

    # ------------------------------------------------------------------ search
    def search(self, text: str, limit: int = 8) -> list:
        """Towns whose name starts with (then has a word starting with, then contains) the text, biggest
        first. "Springfield, MO" or "springfield missouri" keeps those in that state."""
        name, st = split_state(text)
        if len(name) < 2:
            return []
        ranked = []
        for i, n in enumerate(self.lower):
            if n.startswith(name):
                r = 0 if len(n) == len(name) else 1
            elif (" " + name) in n or ("-" + name) in n:
                r = 2
            elif len(name) >= 3 and name in n:
                r = 3
            else:
                continue
            ranked.append((r, -int(self.pop[i]), i))
        ranked.sort()
        out, seen = [], set()
        for _r, _p, i in ranked:
            s = self.state_at(float(self.lat[i]), float(self.lon[i]))
            if st and s != st:
                continue
            # the data has a few towns twice (e.g. "New York" and "New York City"): keep one per spot
            key = (self.lower[i][:6], round(float(self.lat[i]), 1), round(float(self.lon[i]), 1))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(name=self.names[i], label=f"{self.names[i]}, {s}" if s else self.names[i],
                            kind="Town", lat=float(self.lat[i]), lon=float(self.lon[i]), pop=int(self.pop[i]),
                            bbox=None, geom=[], source="local"))
            if len(out) >= limit:
                break
        return out


def _inside(x, y, ring) -> bool:
    """Point in polygon (ring: N x 2 lon/lat), vectorised ray casting."""
    xs, ys = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    cross = (ys > y) != (yj > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = (xj - xs) * (y - ys) / (yj - ys) + xs
    return bool(np.count_nonzero(cross & (x < xint)) % 2)


# --------------------------------------------------------------------------- OpenStreetMap (Nominatim)
KINDS = {
    "city": "City", "town": "Town", "village": "Village", "hamlet": "Hamlet", "suburb": "Neighbourhood",
    "neighbourhood": "Neighbourhood", "county": "County", "state": "State", "road": "Road",
    "house": "Address", "building": "Building", "postcode": "ZIP code", "aeroway": "Airport",
    "amenity": "Place", "leisure": "Park", "natural": "Natural feature", "water": "Water",
    "municipality": "Town", "locality": "Place", "isolated_dwelling": "Place", "farm": "Farm",
}
_last_request = [0.0]
_lock = threading.Lock()


def osm_kind(r: dict) -> str:
    cat, typ, at = r.get("category") or r.get("class") or "", r.get("type") or "", r.get("addresstype") or ""
    if cat == "highway":
        return {"motorway": "Interstate / freeway", "trunk": "Highway", "primary": "Highway"}.get(typ, "Road")
    if cat == "aeroway":
        return "Airport"
    if cat == "place" and typ in KINDS:
        return KINDS[typ]
    return KINDS.get(at) or KINDS.get(typ) or (at or typ or cat or "Place").replace("_", " ").capitalize()


def short_label(display: str) -> str:
    """'Moore, Cleveland County, Oklahoma, 73160, United States' -> 'Moore, Cleveland County, Oklahoma'."""
    parts = [p.strip() for p in (display or "").split(",") if p.strip()]
    parts = [p for p in parts if p not in ("United States", "United States of America") and not re.fullmatch(r"\d{5}(-\d{4})?", p)]
    return ", ".join(parts[:4])


def _geom(gj) -> list:
    """GeoJSON -> list of (N x 2) lon/lat arrays (lines and polygon outlines)."""
    if not isinstance(gj, dict):
        return []
    t, c = gj.get("type"), gj.get("coordinates")
    try:
        if t == "LineString":
            return [np.asarray(c, float)]
        if t == "MultiLineString":
            return [np.asarray(l, float) for l in c]
        if t == "Polygon":
            return [np.asarray(r, float) for r in c[:1]]
        if t == "MultiPolygon":
            return [np.asarray(p[0], float) for p in c]
    except (TypeError, ValueError):
        return []
    return []


def parse_nominatim(js) -> list:
    out = []
    for r in js if isinstance(js, list) else []:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        bb = None
        try:
            s, n, w, e = (float(v) for v in r.get("boundingbox") or [])
            bb = (s, n, w, e)
        except (TypeError, ValueError):
            pass
        label = short_label(r.get("display_name", ""))
        name = r.get("name") or label.split(",")[0]
        out.append(dict(name=name, label=label or name, kind=osm_kind(r), lat=lat, lon=lon, pop=0, bbox=bb,
                        geom=[g for g in _geom(r.get("geojson")) if g.ndim == 2 and len(g) >= 2], source="osm"))
    return out


def search_osm(text: str, near=None, limit: int = 8) -> list:
    """Nominatim search, favouring places near [near] = (lat, lon). At most one request a second."""
    params = {"q": text, "format": "jsonv2", "limit": limit, "polygon_geojson": 1, "polygon_threshold": 0.0005,
              "countrycodes": "us,pr,gu,vi,as,mp,ca,mx"}
    if near is not None:
        lat, lon = near
        params["viewbox"] = f"{lon - 6:.3f},{lat + 4:.3f},{lon + 6:.3f},{lat - 4:.3f}"   # a nudge, not a limit
    with _lock:
        wait = 1.05 - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
    r = requests.get(NOMINATIM, params=params, headers=UA, timeout=15)
    r.raise_for_status()
    return parse_nominatim(r.json())


# --------------------------------------------------------------------------- the marker on the map
MARK_RGB = (255, 79, 216)


class SearchMarker:
    """The place found: a pin with its name, and the road or boundary outline when there is one."""

    def __init__(self):
        self.place = None
        self._proj = None
        self._xy = None

    def set(self, place):
        self.place, self._proj = place, None

    def clear(self):
        self.place, self._proj = None, None

    def _project(self, view):
        if self._proj != (view.lat0, view.lon0):
            p = self.place
            x, y = aeqd_forward(p["lat"], p["lon"], view.lat0, view.lon0)
            lines = []
            for g in p.get("geom") or []:
                gx, gy = aeqd_forward(g[:, 1], g[:, 0], view.lat0, view.lon0)
                lines.append(np.stack([gx, gy], 1))
            self._xy = ((float(x), float(y)), lines)
            self._proj = (view.lat0, view.lon0)
        return self._xy

    def paint(self, painter, vt, panel, view):
        if self.place is None:
            return
        (x, y), lines = self._project(view)
        col = QColor(*MARK_RGB)
        for xy in lines:
            sx, sy = vt.to_screen(xy[:, 0], xy[:, 1])
            poly = QPolygonF([QPointF(a, b) for a, b in zip(sx.tolist(), sy.tolist())])
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 200), 5.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(poly)
            painter.setPen(QPen(col, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(poly)
        sx, sy = vt.to_screen(x, y)
        # a map pin: a teardrop pointing at the spot
        painter.setPen(QPen(QColor(0, 0, 0), 1.5))
        painter.setBrush(col)
        tip = QPointF(sx, sy)
        head = QPointF(sx, sy - 18)
        painter.drawPolygon(QPolygonF([tip, QPointF(sx - 6, sy - 13), QPointF(sx + 6, sy - 13)]))
        painter.drawEllipse(head, 7.5, 7.5)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(head, 2.8, 2.8)
        view._halo_text(painter, sx + 11, sy - 14, self.place["name"], QColor(255, 220, 245), view.font_header)
