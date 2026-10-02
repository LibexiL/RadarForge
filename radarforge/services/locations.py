"""Saved locations: the places you want to be warned about, and what to be warned about for each."""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

OUTLOOK_LEVELS = ("off", "SLGT", "ENH", "MDT", "HIGH")
OUTLOOK_NAMES = {"off": "Don't tell me", "SLGT": "Slight risk or higher", "ENH": "Enhanced risk or higher",
                 "MDT": "Moderate risk or higher", "HIGH": "High risk"}

# what a new location is warned about: warnings that cover it, nothing else
DEFAULT_RULES = {
    "tornado": {"on": True, "miles": 0},       # tornado warning (miles 0 = its area covers the location)
    "severe": {"on": True, "miles": 0},        # severe thunderstorm warning
    "flood": {"on": True, "miles": 0},         # flash flood warning
    "significant_only": False,                 # only PDS / considerable / destructive / emergency / observed tornado
    "watch": {"on": False},                    # a tornado or severe thunderstorm watch covers it
    "mcd": {"on": False},                      # an SPC mesoscale discussion covers it
    "outlook": {"min": "off"},                 # SPC day 1 outlook category at the location
    "lightning": {"miles": 0},                 # 0 = off; GLM flashes within this many miles
}
RADIUS_CHOICES = (0, 5, 10, 15, 25, 50)        # miles
LIGHTNING_CHOICES = (0, 5, 10, 20, 30)


def normalize_rules(rules: dict | None) -> dict:
    """The rules with every setting present and sensible (unknown or broken values fall back to the defaults)."""
    out = copy.deepcopy(DEFAULT_RULES)
    for key, default in DEFAULT_RULES.items():
        given = (rules or {}).get(key)
        if isinstance(default, bool):
            if isinstance(given, bool):
                out[key] = given
        elif isinstance(default, dict) and isinstance(given, dict):
            for sub, dval in default.items():
                v = given.get(sub, dval)
                if isinstance(dval, bool):
                    out[key][sub] = bool(v)
                elif isinstance(dval, (int, float)):
                    try:
                        out[key][sub] = max(0, min(200, int(v)))
                    except (TypeError, ValueError):
                        pass
                elif sub == "min" and v in OUTLOOK_LEVELS:
                    out[key][sub] = v
    return out


@dataclass
class Location:
    id: str
    name: str
    lat: float
    lon: float
    rules: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_RULES))

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "lat": round(self.lat, 5), "lon": round(self.lon, 5),
                "rules": self.rules}

    @staticmethod
    def from_json(d: dict) -> "Location | None":
        try:
            lat, lon = float(d["lat"]), float(d["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return Location(str(d.get("id") or uuid.uuid4().hex[:8]), str(d.get("name") or "Location")[:40], lat, lon,
                        normalize_rules(d.get("rules")))

    def describe_rules(self) -> str:
        """One line for lists: 'Warnings, watches, lightning within 10 mi'."""
        r = self.rules
        bits = []
        names = [n for k, n in (("tornado", "tornado"), ("severe", "severe"), ("flood", "flood")) if r[k]["on"]]
        if names:
            bits.append("/".join(names) + (" (significant only)" if r["significant_only"] else ""))
        if r["watch"]["on"]:
            bits.append("watches")
        if r["mcd"]["on"]:
            bits.append("discussions")
        if r["outlook"]["min"] != "off":
            bits.append(f"outlook {r['outlook']['min']}+")
        if r["lightning"]["miles"]:
            bits.append(f"lightning within {r['lightning']['miles']} mi")
        return ", ".join(bits) or "no alerts"


class LocationBook:
    """The saved locations, kept in the settings. The first one is "my location" (Ctrl+L, storm track arrival)."""

    def __init__(self, settings):
        self.settings = settings
        self.items: list[Location] = []
        self.load()

    def load(self):
        items = [loc for loc in (Location.from_json(d) for d in (self.settings["locations"] or []) if isinstance(d, dict))
                 if loc is not None]
        old = self.settings["my_location"]
        if not items and old and len(old) >= 2:                  # from before there were several: keep what they had
            try:
                items = [Location(uuid.uuid4().hex[:8], "Home", float(old[0]), float(old[1]))]
            except (TypeError, ValueError):
                pass
        self.items = items

    def save(self):
        self.settings["locations"] = [loc.to_json() for loc in self.items]
        p = self.primary()
        self.settings["my_location"] = [round(p.lat, 5), round(p.lon, 5)] if p else None
        self.settings.save()

    def primary(self) -> Location | None:
        return self.items[0] if self.items else None

    def get(self, loc_id: str) -> Location | None:
        return next((loc for loc in self.items if loc.id == loc_id), None)

    def add(self, name: str, lat: float, lon: float, primary: bool = False) -> Location:
        loc = Location(uuid.uuid4().hex[:8], (name or "Location").strip()[:40] or "Location", float(lat), float(lon))
        if primary:
            self.items.insert(0, loc)
        else:
            self.items.append(loc)
        self.save()
        return loc

    def remove(self, loc_id: str):
        self.items = [loc for loc in self.items if loc.id != loc_id]
        self.save()

    def make_primary(self, loc_id: str):
        loc = self.get(loc_id)
        if loc is not None:
            self.items.remove(loc)
            self.items.insert(0, loc)
            self.save()

    def set_primary_position(self, lat: float | None, lon: float | None):
        """'Set my location here' / 'Remove my location': moves (or creates, or deletes) the first location."""
        p = self.primary()
        if lat is None:
            if p is not None:
                self.remove(p.id)
            return
        if p is None:
            self.add("Home", lat, lon, primary=True)
        else:
            p.lat, p.lon = float(lat), float(lon)
            self.save()

    def unique_name(self, base: str) -> str:
        names = {loc.name for loc in self.items}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"
