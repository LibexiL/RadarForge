"""Saving what you are looking at: bookmarks, shareable view files and named workspaces. No Qt here.

A *view* is a plain dict: which radar and time, the panels, the zoom and the layers. It is what a bookmark
stores, what a `.rfview` file holds, and what the "RFV1:…" text you can paste into a chat contains.
A *workspace* is the part of a view about layout only (panels, layers, which side panels are open): it
leaves the radar, the time and the zoom alone.
"""
from __future__ import annotations

import base64
import json
import re
import zlib
from datetime import datetime, timezone

VERSION = 1
PREFIX = "RFV1:"
EXTENSION = ".rfview"

OVERLAY_KEYS = ("warnings", "watches", "reports", "chasers", "spc_outlook", "spc_mcd", "storm_tracks", "meso", "tvs",
                "hail", "melting_layer", "satellite", "lightning", "mrms", "surface", "signatures")
_SITE = re.compile(r"^[A-Z0-9]{4}$")


def _time(v) -> str | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(v) -> datetime | None:
    t = _time(v)
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if t else None


def clean_view(raw: dict, known_products=None) -> dict:
    """A view with every field checked and clamped (anything unknown is dropped): safe to apply."""
    if not isinstance(raw, dict):
        raise ValueError("not a RadarForge view")
    out: dict = {"version": VERSION}
    site = str(raw.get("site") or "").upper()
    if _SITE.match(site):
        out["site"] = site
    out["time"] = _time(raw.get("time"))                     # None = live
    try:
        out["layout"] = max(1, min(6, int(raw.get("layout"))))
    except (TypeError, ValueError):
        pass
    panels = [str(p) for p in (raw.get("panels") or [])][:6]
    if known_products is not None:
        panels = [p for p in panels if p in known_products]
    if panels:
        out["panels"] = panels
    try:
        out["tilt"] = round(max(0.0, min(30.0, float(raw.get("tilt")))), 2)
    except (TypeError, ValueError):
        pass
    v = raw.get("view")
    if isinstance(v, dict):
        try:
            lat, lon, km = float(v["lat"]), float(v["lon"]), float(v["km_across"])
            if -90 <= lat <= 90 and -180 <= lon <= 180 and 1 <= km <= 20000:
                out["view"] = {"lat": round(lat, 4), "lon": round(lon, 4), "km_across": round(km, 1)}
        except (KeyError, TypeError, ValueError):
            pass
    ov = raw.get("overlays")
    if isinstance(ov, dict):
        out["overlays"] = {k: bool(ov[k]) for k in OVERLAY_KEYS if k in ov}
    sat = raw.get("satellite")
    if isinstance(sat, dict) and sat.get("channel") in ("ir", "wv", "swir", "vis"):
        out["satellite"] = {"channel": sat["channel"]}
    sm = raw.get("storm_motion")
    if isinstance(sm, (list, tuple)) and len(sm) == 2:
        try:
            out["storm_motion"] = [float(sm[0]) % 360.0, max(0.0, min(150.0, float(sm[1])))]
        except (TypeError, ValueError):
            pass
    out["name"] = str(raw.get("name") or "")[:80]
    out["notes"] = str(raw.get("notes") or "")[:2000]
    side = raw.get("side")
    if isinstance(side, list):
        out["side"] = [str(k) for k in side][:10]
    return out


# ------------------------------------------------------------------------------------------------ sharing
def encode(view: dict) -> str:
    """The view as one line of text ('RFV1:…') that survives being pasted into a chat or an e-mail."""
    data = json.dumps(view, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(zlib.compress(data, 9)).decode("ascii").rstrip("=")


def decode(text: str) -> dict:
    """A view from 'RFV1:…' text or from the JSON of a .rfview file. Raises ValueError for anything else."""
    t = (text or "").strip()
    try:
        if t.startswith(PREFIX):
            body = t[len(PREFIX):].strip()
            raw = json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).decode("utf-8"))
        else:
            raw = json.loads(t)
    except Exception as exc:
        raise ValueError("this isn't a RadarForge view") from exc
    return clean_view(raw)


def to_file_text(view: dict, app_version: str = "") -> str:
    return json.dumps({"radarforge_view": app_version or VERSION, **view}, indent=2, ensure_ascii=False)


# ------------------------------------------------------------------------------------------------ bookmarks
def new_bookmark(view: dict, name: str, notes: str = "") -> dict:
    bm = dict(view)
    bm["name"] = (name or "Bookmark").strip()[:80]
    bm["notes"] = notes
    bm["created"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bm


def default_bookmark_name(view: dict) -> str:
    t = parse_time(view.get("time"))
    site = view.get("site", "radar")
    return f"{site} {t:%Y-%m-%d %H:%MZ}" if t else f"{site} (live)"


# ------------------------------------------------------------------------------------------------ workspaces
# Layout only: how many panels, what they show, which layers are on and which side panels are open.
BUILTIN_WORKSPACES = {
    "Briefing": {
        "note": "Four panels, warnings, the SPC outlook and discussions, storm reports and your locations: "
                "for briefing someone, or screen-sharing.",
        "layout": 4, "panels": ["REF", "SRV", "CC", "AZSH"],
        "overlays": {"warnings": True, "watches": True, "reports": True, "spc_outlook": True, "spc_mcd": True,
                     "storm_tracks": True, "meso": True, "tvs": True, "lightning": True},
        "side": ["warnings", "locations"],
    },
    "Tornado hunt": {
        "note": "Reflectivity, storm-relative velocity, correlation coefficient and rotation, with the rotation "
                "and tornado signature overlays.",
        "layout": 4, "panels": ["REF", "SRV", "CC", "AZSH"],
        "overlays": {"warnings": True, "meso": True, "tvs": True, "storm_tracks": True},
        "side": ["warnings"],
    },
    "Hail": {
        "note": "Reflectivity, differential reflectivity, correlation coefficient and the hail products.",
        "layout": 4, "panels": ["REF", "ZDR", "CC", "MESH"],
        "overlays": {"warnings": True, "hail": True, "storm_tracks": True},
        "side": ["cells"],
    },
    "Flood": {
        "note": "Reflectivity, specific differential phase and the rainfall accumulations.",
        "layout": 3, "panels": ["REF", "KDP", "L3DTA"],
        "overlays": {"warnings": True, "reports": True},
        "side": ["warnings"],
    },
    "Satellite and storms": {
        "note": "One big reflectivity panel over GOES infrared with lightning: where the storms are strengthening.",
        "layout": 1, "panels": ["REF"],
        "overlays": {"warnings": True, "satellite": True, "lightning": True, "storm_tracks": True},
        "side": ["layers"],
    },
    "Everything": {
        "note": "Six panels: reflectivity, velocity, spectrum width and the three dual-pol moments.",
        "layout": 6, "panels": ["REF", "VEL", "SW", "ZDR", "CC", "KDP"],
        "overlays": {"warnings": True},
        "side": ["products"],
    },
}


def clean_workspace(raw: dict, known_products=None) -> dict:
    """The layout part of a view (panels, layers, open side panels)."""
    v = clean_view({**raw, "site": None})
    out = {k: v[k] for k in ("layout", "panels", "overlays", "side") if k in v}
    out["note"] = str(raw.get("note") or "")[:300]
    if known_products is not None and "panels" in out:
        out["panels"] = [p for p in out["panels"] if p in known_products]
    return out
