"""Saved locations with alert rules: warnings / watches covering the place, storm reports and lightning
nearby. evaluate() is pure (testable); Notifier plays a sound and shows a desktop notification."""
from __future__ import annotations

import copy
import math
import time
import wave
from datetime import timedelta

import numpy as np

from ..config import CACHE_DIR
from ..products.geometry import aeqd_forward
from . import feeds

MI = 1.609344
LIGHTNING_COOLDOWN_MIN = 30
REPORT_LOOKBACK_MIN = 60
SOUNDS = {"chime": "Chime", "siren": "Siren", "beep": "Beeps", "none": "No sound"}

DEFAULT_RULES = {"enabled": True, "warn": {"TOR": True, "SVR": True, "FFW": True, "OTH": False},
                 "watches": False, "reports": False, "report_miles": 10, "lightning": False, "lightning_miles": 10,
                 "sound": True, "desktop": True, "popup": True}


def new_location(name, lat, lon, mine=False, **rules) -> dict:
    loc = copy.deepcopy(DEFAULT_RULES)
    loc.update(id="mine" if mine else f"loc{int(time.time() * 1000) % 10**10}", name=name,
               lat=None if mine else round(float(lat), 5), lon=None if mine else round(float(lon), 5), mine=mine)
    for k, v in rules.items():
        loc[k] = copy.deepcopy(v)
    return loc


def normalise(loc: dict) -> dict:
    """Fill in rules missing from an older settings file."""
    out = copy.deepcopy(DEFAULT_RULES)
    out.update({k: copy.deepcopy(v) for k, v in loc.items() if k != "warn"})
    out["warn"].update(loc.get("warn") or {})
    out.setdefault("id", f"loc{abs(hash((loc.get('name'), loc.get('lat'), loc.get('lon')))) % 10**10}")
    out.setdefault("mine", False)
    return out


def migrate(settings) -> bool:
    """1.9.0: the single "my location" alert becomes the first saved location. Returns True if changed."""
    locs = settings["saved_locations"]
    if locs is None:
        locs = []
        mine = new_location("My location", None, None, mine=True)
        mine["enabled"] = bool(settings["warn_at_location"])
        locs.append(mine)
        settings["saved_locations"] = locs
        return True
    if not any(loc.get("mine") for loc in locs):
        mine = new_location("My location", None, None, mine=True)
        mine["enabled"] = bool(settings["warn_at_location"])
        settings["saved_locations"] = [mine] + list(locs)
        return True
    return False


def coords(loc, my_ll):
    if loc.get("mine"):
        return my_ll
    try:
        return float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def describe_rules(loc) -> str:
    parts = []
    w = [k for k in ("TOR", "SVR", "FFW", "OTH") if loc["warn"].get(k)]
    names = {"TOR": "tornado", "SVR": "severe", "FFW": "flash flood", "OTH": "other"}
    if w:
        parts.append(", ".join(names[k] for k in w) + " warnings")
    if loc.get("watches"):
        parts.append("watches")
    if loc.get("reports"):
        parts.append(f"storm reports within {loc['report_miles']} mi")
    if loc.get("lightning"):
        parts.append(f"lightning within {loc['lightning_miles']} mi")
    if not loc.get("enabled", True):
        return "alerts off"
    return "; ".join(parts) or "no alerts chosen"


def _inside(rings, lat, lon):
    return (any(r[:, 1].min() <= lat <= r[:, 1].max() and r[:, 0].min() <= lon <= r[:, 0].max() for r in rings)
            and feeds.rings_contain(rings, lat, lon))


def evaluate(locs, my_ll, alerts, reports, flashes_near, now, notified, group_of, report_on=None) -> list:
    """New alert events. `notified` maps event key -> [priority, expiry epoch s] and is updated in place.

    alerts: warning objects (event, rings, key, action, expires, style[3] = priority, variant_label, hover)
    reports: dicts (kind, lat, lon, time, hover); flashes_near(lat, lon, miles, minutes) -> (n, dist, newest)
    group_of: event name -> TOR / SVR / FFW / OTH / WAT
    """
    events = []
    t_now = now.timestamp()
    for loc in locs:
        if not loc.get("enabled", True):
            continue
        ll = coords(loc, my_ll)
        if ll is None:
            continue
        lat, lon = ll
        lid = loc.get("id", loc.get("name", "?"))
        name = loc.get("name") or "Saved location"
        # warnings and watches covering the place
        for a in alerts:
            g = group_of(a.event)
            if g is None or a.action in ("CAN", "EXP"):
                continue
            if g == "WAT":
                if not loc.get("watches"):
                    continue
            elif not loc["warn"].get(g, False):
                continue
            if a.expires is not None and a.expires < now:
                continue
            if not _inside(a.rings, lat, lon):
                continue
            key = f"{lid}|{a.key}"
            pri = a.style[3]
            old = notified.get(key)
            if old is not None and pri <= old[0]:
                continue
            notified[key] = [pri, a.expires.timestamp() if a.expires else t_now + 7200]
            events.append(dict(key=key, loc=loc, kind="watch" if g == "WAT" else "warning", priority=pri,
                               title=f"{a.variant_label} – {name}",
                               text=f"{a.variant_label} covers {name}" + (
                                   f" until {feeds.local_hm(a.expires)}" if a.expires else ""),
                               detail=a.hover, target=a))
        # storm reports nearby
        if loc.get("reports") and reports:
            miles = float(loc.get("report_miles") or 10)
            for r in reports:
                if r.get("time") is None or r["time"] < now - timedelta(minutes=REPORT_LOOKBACK_MIN):
                    continue
                if report_on is not None and not report_on(r):
                    continue
                x, y = aeqd_forward(r["lat"], r["lon"], lat, lon)
                d = math.hypot(float(x), float(y)) / MI
                if d > miles:
                    continue
                key = f"{lid}|rep|{r['time']:%Y%m%d%H%M}|{r['lat']:.3f},{r['lon']:.3f}|{r.get('kind')}"
                if key in notified:
                    continue
                notified[key] = [1, t_now + 6 * 3600]
                kind = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])[2]
                events.append(dict(key=key, loc=loc, kind="report", priority=3,
                                   title=f"{kind} reported {d:.0f} mi from {name}",
                                   text=f"{kind} reported {d:.0f} mi {feeds.compass(_bearing(lat, lon, r))} of {name} "
                                        f"at {feeds.local_hm(r['time'])}",
                                   detail=r.get("hover", ""), target=(r["lat"], r["lon"])))
        # lightning nearby
        if loc.get("lightning") and flashes_near is not None:
            miles = float(loc.get("lightning_miles") or 10)
            n, dist, newest = flashes_near(lat, lon, miles, 15)
            key = f"{lid}|ltg"
            old = notified.get(key)
            if n and (old is None or old[1] < t_now):
                notified[key] = [1, t_now + LIGHTNING_COOLDOWN_MIN * 60]
                events.append(dict(key=key, loc=loc, kind="lightning", priority=2,
                                   title=f"Lightning near {name}",
                                   text=f"{n} lightning flash{'es' if n > 1 else ''} within {miles:.0f} mi of {name} "
                                        f"in the last 15 min (closest {dist:.0f} mi)",
                                   detail="", target=(lat, lon)))
    for k in [k for k, v in notified.items() if v[1] < t_now - 3600]:
        del notified[k]
    events.sort(key=lambda e: -e["priority"])
    return events


def _bearing(lat, lon, r):
    x, y = aeqd_forward(r["lat"], r["lon"], lat, lon)
    return (math.degrees(math.atan2(float(x), float(y))) + 360) % 360


# --------------------------------------------------------------------------- sounds
def _tone(freqs, dur, rate, decay=3.0):
    t = np.arange(int(dur * rate)) / rate
    sig = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    env = np.exp(-decay * t) * np.minimum(1, t * 200)
    return sig * env


def synth(name: str, rate=22050) -> np.ndarray:
    if name == "siren":
        t = np.arange(int(1.6 * rate)) / rate
        f = 750 + 450 * (0.5 - 0.5 * np.cos(2 * np.pi * t / 0.8))
        sig = np.sin(2 * np.pi * np.cumsum(f) / rate)
        env = np.minimum(1, t * 50) * np.minimum(1, (t[-1] - t) * 30)
        return sig * env * 0.8
    if name == "beep":
        b = _tone([1000], 0.18, rate, decay=2) * 0.8
        gap = np.zeros(int(0.12 * rate))
        return np.concatenate([b, gap, b, gap, b])
    a = _tone([880, 1760], 0.45, rate, decay=5)
    b = _tone([660, 1320], 0.9, rate, decay=4)
    return np.concatenate([a, b]) * 0.8


def sound_file(name: str):
    """A WAV of the named alert sound in the cache folder (written once)."""
    if name not in SOUNDS or name == "none":
        return None
    path = CACHE_DIR / "sounds" / f"alert_{name}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(synth(name), -1, 1) * 32000).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(pcm.tobytes())
    return path

