"""Radar site table."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

_PATH = Path(__file__).with_name("sites.json")


@dataclass(frozen=True)
class Site:
    id: str
    type: str
    lat: float
    lon: float
    elev_ft: float
    place: str
    state: str
    country: str
    tz: str

    @property
    def l3_id(self) -> str:
        """Three-letter id used by Level III products (KTLX -> TLX)."""
        return self.id[1:] if len(self.id) == 4 else self.id

    @property
    def label(self) -> str:
        loc = f"{self.place}, {self.state}" if self.state else self.place
        kind = "TDWR" if self.type == "tdwr" else "WSR-88D"
        return f"{self.id} – {loc} ({kind})"


_SITES: dict | None = None


def all_sites() -> dict:
    global _SITES
    if _SITES is None:
        with open(_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        _SITES = {d["id"]: Site(d["id"], d["type"], d["lat"], d["lon"], d.get("elev_ft") or 0.0,
                                d.get("place", ""), d.get("state", ""), d.get("country", ""),
                                d.get("tz", "")) for d in raw}
    return _SITES


def get_site(site_id: str) -> Site | None:
    return all_sites().get(site_id.upper())


def nearest_site(lat: float, lon: float, kinds=("wsr88d",)) -> Site | None:
    best, bd = None, 1e18
    for s in all_sites().values():
        if s.type not in kinds:
            continue
        d = haversine_km(lat, lon, s.lat, s.lon)
        if d < bd:
            best, bd = s, d
    return best


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))
