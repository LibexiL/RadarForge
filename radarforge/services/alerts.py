"""Deciding what to tell the user about their saved locations. No Qt here: it takes the current warnings,
discussions, outlook and lightning counts and returns the events that are new since the last check."""
from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass
from datetime import datetime

from .. import fmt
from ..data import feeds
from . import geo
from .locations import OUTLOOK_LEVELS, Location

EVENT_RULE = {"Tornado Warning": "tornado", "Tornado Emergency": "tornado",
              "Severe Thunderstorm Warning": "severe",
              "Flash Flood Warning": "flood", "Flash Flood Emergency": "flood",
              "Tornado Watch": "watch", "Severe Thunderstorm Watch": "watch"}
# variants that are more than a plain warning: PDS / emergency / considerable / destructive / observed
SIGNIFICANT = {"TORR", "TORP", "TORE", "SVRC", "SVRD", "FFWC", "FFWE"}
LIGHTNING_COOLDOWN_S = 900


@dataclass
class AlertEvent:
    key: str                    # the same for every update of one thing (so it is only announced once)
    kind: str                   # warning | watch | mcd | outlook | lightning
    priority: float             # higher is worse; an event is announced again when its priority goes up
    title: str
    body: str
    location: Location
    expires: float              # epoch seconds: when it stops mattering (or the cooldown ends)
    ref: object = None          # the warning / discussion, so "Show on map" can go to it

    @property
    def sound(self) -> str:
        if self.priority >= 11 and self.kind == "warning":
            return "tornado"
        return "severe" if self.priority >= 7 and self.kind == "warning" else "info"


def _bbox_near(rings, lat, lon, margin_deg) -> bool:
    """Cheap test first: is the point inside the rings' bounding boxes grown by a margin?"""
    mlon = margin_deg / max(0.2, math.cos(math.radians(lat)))
    return any(r[:, 1].min() - margin_deg <= lat <= r[:, 1].max() + margin_deg and
               r[:, 0].min() - mlon <= lon <= r[:, 0].max() + mlon for r in rings)


def warning_distance_km(alert, lat, lon, radius_km: float) -> float | None:
    """0 inside the warning, the distance to it when it is within radius_km, else None."""
    margin = radius_km / geo.KM_PER_DEG + 0.05
    if not _bbox_near(alert.rings, lat, lon, margin):
        return None
    d = geo.rings_distance_km(lat, lon, alert.rings)
    return d if d <= max(radius_km, 0.0) else None


def _until(t) -> str:
    return f" · until {fmt.local_hm(t)}" if t is not None else ""


def evaluate(locations, alerts, mcds, outlook, lightning_count, now: datetime, lightning_minutes: int = 10) -> list:
    """Every alert the locations' rules call for right now (the caller keeps only the new ones, see announce())."""
    events: list[AlertEvent] = []
    ts = now.timestamp()
    for loc in locations:
        r = loc.rules
        for a in alerts:
            rule = EVENT_RULE.get(a.event)
            if rule is None or a.action in ("CAN", "EXP") or (a.expires is not None and a.expires < now):
                continue
            if rule == "watch":
                if not r["watch"]["on"] or warning_distance_km(a, loc.lat, loc.lon, 0.0) is None:
                    continue
                events.append(AlertEvent(f"{loc.id}|{a.key}", "watch", float(a.style[3]), f"{a.event} – {loc.name}",
                                         f"{a.event} covers {loc.name}{_until(a.expires)}", loc,
                                         a.expires.timestamp() if a.expires else ts + 7200, a))
                continue
            cfg = r[rule]
            if not cfg["on"] or (r["significant_only"] and a.variant not in SIGNIFICANT):
                continue
            d = warning_distance_km(a, loc.lat, loc.lon, cfg["miles"] * geo.KM_PER_MILE)
            if d is None:
                continue
            where = "covers" if d == 0 else f"is {d / geo.KM_PER_MILE:.0f} mi from"
            area = f" ({a.area[:70]})" if getattr(a, "area", "") else ""
            events.append(AlertEvent(f"{loc.id}|{a.key}", "warning", float(a.style[3]),
                                     f"{a.variant_label} – {loc.name}",
                                     f"{a.variant_label} ({a.variant}) {where} {loc.name}{_until(a.expires)}{area}", loc,
                                     a.expires.timestamp() if a.expires else ts + 7200, a))
        if r["mcd"]["on"]:
            for m in mcds or []:
                if m.get("expire") is not None and m["expire"] < now:
                    continue
                if feeds.rings_contain(m["rings"], loc.lat, loc.lon):
                    why = (m.get("concerning") or "").capitalize()
                    events.append(AlertEvent(f"{loc.id}|MCD{m['number']}", "mcd", 3.0,
                                             f"SPC Mesoscale Discussion {m['number']} – {loc.name}",
                                             f"MD {m['number']} covers {loc.name}" + (f": {why}" if why else "") +
                                             _until(m.get("expire")), loc,
                                             m["expire"].timestamp() if m.get("expire") else ts + 7200, m))
        want = r["outlook"]["min"]
        o = feeds.outlook_at(outlook, loc.lat, loc.lon) if (want != "off" and outlook) else None
        if o is not None and o["cat"] in OUTLOOK_LEVELS and OUTLOOK_LEVELS.index(o["cat"]) >= OUTLOOK_LEVELS.index(want):
            chances = " · ".join(f"{k.capitalize()} {v * 100:.0f}%" for k in ("tornado", "wind", "hail")
                                 if (v := o.get(k)) is not None)
            exp = o.get("expire")
            day = f"{exp:%Y%m%d}" if exp is not None else "today"
            events.append(AlertEvent(f"{loc.id}|OUT|{day}", "outlook", 1.0 + feeds.CATEGORIES.index(o["cat"]) / 10,
                                     f"SPC {feeds.CAT_NAME[o['cat']]} – {loc.name}",
                                     f"{loc.name} is in the SPC day 1 outlook: {feeds.CAT_NAME[o['cat']]}"
                                     + (f" ({chances})" if chances else "") + _until(exp), loc,
                                     exp.timestamp() if exp else ts + 6 * 3600))
        miles = r["lightning"]["miles"]
        if miles and lightning_count is not None:
            n = lightning_count(loc.lat, loc.lon, miles * geo.KM_PER_MILE)
            if n:
                events.append(AlertEvent(f"{loc.id}|LTG", "lightning", 4.0, f"Lightning near {loc.name}",
                                         f"{n} lightning flash{'es' if n != 1 else ''} within {miles} mi of {loc.name} "
                                         f"in the last {lightning_minutes} minutes", loc, ts + LIGHTNING_COOLDOWN_S))
    return events


def announce(events: list, notified: dict, now: datetime) -> list:
    """The events not announced yet (or whose priority went up), recorded in `notified` {key: [priority, expires]}.
    Entries whose time has run out are dropped, so a lightning cooldown ends by itself."""
    ts = now.timestamp()
    for k in [k for k, v in notified.items() if v[1] < ts - 60]:
        del notified[k]
    fresh = []
    for e in events:
        old = notified.get(e.key)
        if old is None or old[1] < ts or e.priority > old[0]:
            fresh.append(e)
    for e in events:
        old = notified.get(e.key)
        live = old is not None and old[1] >= ts
        # a lightning cooldown counts from the announcement: it must not keep sliding while the flashes continue
        expires = old[1] if (live and e.kind == "lightning") else e.expires
        notified[e.key] = [max(e.priority, old[0] if live else 0), expires]
    fresh.sort(key=lambda e: -e.priority)
    return fresh


# ---------------------------------------------------------------------------------------------- status board
def status_lines(loc: Location, alerts, mcds, outlook, lightning_count, now: datetime, lightning_minutes: int = 10,
                 near_miles: float = 25.0) -> list:
    """What is going on at a location, for the Locations panel: [(priority, kind, text)], worst first.
    Unlike evaluate() this ignores the alert rules: it shows everything that is near."""
    out = []
    for a in alerts:
        rule = EVENT_RULE.get(a.event)
        if rule is None or a.action in ("CAN", "EXP") or (a.expires is not None and a.expires < now):
            continue
        d = warning_distance_km(a, loc.lat, loc.lon, near_miles * geo.KM_PER_MILE)
        if d is None:
            continue
        left = ""
        if a.expires is not None:
            mins = max(0, int((a.expires - now).total_seconds() // 60))
            left = f" · {mins} min left"
        if d == 0:
            out.append((float(a.style[3]) + 100, "inside", f"In a {a.variant_label} ({a.variant}){left}"))
        else:
            where = ""
            try:
                clat, clon = a.centroid()
                where = f" to the {fmt.compass(geo.bearing(loc.lat, loc.lon, clat, clon))}"
            except Exception:
                pass
            out.append((float(a.style[3]), "near", f"{a.variant_label} {d / geo.KM_PER_MILE:.0f} mi away{where}{left}"))
    for m in mcds or []:
        if m.get("expire") is not None and m["expire"] < now:
            continue
        if feeds.rings_contain(m["rings"], loc.lat, loc.lon):
            out.append((3.0, "mcd", f"In SPC Mesoscale Discussion {m['number']}"))
    o = feeds.outlook_at(outlook, loc.lat, loc.lon) if outlook else None
    if o is not None:
        out.append((1.0, "outlook", f"SPC outlook: {feeds.CAT_NAME[o['cat']]}"))
    if lightning_count is not None:
        for miles in (5, 10, 25):
            n = lightning_count(loc.lat, loc.lon, miles * geo.KM_PER_MILE)
            if n:
                out.append((2.0, "lightning", f"Lightning: {n} flash{'es' if n != 1 else ''} within {miles} mi "
                                              f"(last {lightning_minutes} min)"))
                break
    out.sort(key=lambda t: -t[0])
    return out


# ---------------------------------------------------------------------------------------------- sounds
SOUND_PATTERNS = {
    # (frequency Hz, seconds); 0 Hz is a pause
    "tornado": [(1175, 0.13), (880, 0.13)] * 7 + [(0, 0.1)],
    "severe": [(784, 0.22), (988, 0.3), (0, 0.15), (784, 0.22), (988, 0.3)],
    "info": [(880, 0.55)],
}


def tone_wav(kind: str, rate: int = 22050, volume: float = 0.55) -> bytes:
    """A short alert sound as a WAV file in memory: 'tornado' (urgent two-tone), 'severe' (chime) or 'info'."""
    pattern = SOUND_PATTERNS.get(kind, SOUND_PATTERNS["info"])
    samples = []
    for freq, dur in pattern:
        n = int(rate * dur)
        for i in range(n):
            if freq == 0:
                samples.append(0.0)
                continue
            t = i / rate
            fade = min(1.0, i / (0.006 * rate), (n - i) / (0.012 * rate))        # no clicks at the edges
            decay = math.exp(-2.2 * i / n) if kind == "info" else 1.0
            samples.append(volume * fade * decay * (0.8 * math.sin(2 * math.pi * freq * t) +
                                                    0.2 * math.sin(4 * math.pi * freq * t)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))
    return buf.getvalue()
