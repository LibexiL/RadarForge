"""Parsing for the live feeds: Spotter Network chasers and reports, local storm report types,
SPC outlooks and mesoscale discussions. No Qt here."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- sources
SN_POSITIONS_ALL = "https://www.spotternetwork.org/feeds/gr.txt"
SN_POSITIONS_ACTIVE = "https://www.spotternetwork.org/feeds/gr-p.txt"   # 5+ accepted reports in 12 months
SN_REPORTS = "https://www.spotternetwork.org/feeds/reports.txt"
LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"       # ?hours=N (whole country)
MCD_URL = "https://mesonet.agron.iastate.edu/api/1/nws/spc_mcd.geojson"
OUTLOOK_URL = "https://mesonet.agron.iastate.edu/api/1/nws/spc_outlook.geojson"


def mcd_text_url(product_id: str) -> str:
    return f"https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}"


def mcd_page(number: int, year: int) -> str:
    return f"https://www.spc.noaa.gov/products/md/{year}/md{number:04d}.html"


def _utc(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- GR placefiles (SN feeds)
def _fields(s: str) -> list:
    """Comma-separated fields; commas inside double quotes don't split."""
    out, cur, quoted, any_q = [], [], False, False
    for ch in s:
        if ch == '"':
            quoted = not quoted
            any_q = True
        elif ch == "," and not quoted:
            out.append("".join(cur).strip())
            cur, any_q = [], False
        else:
            cur.append(ch)
    if cur or any_q or s.rstrip().endswith(","):
        out.append("".join(cur).strip())
    return out


def placefile_objects(text: str) -> tuple:
    """(icon files {n: url}, [(lat, lon, [(angle, file, index, hover)], [texts])]) from a placefile."""
    files, objs = {}, []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key, rest = key.strip().lower(), rest.strip()
        if key == "iconfile":
            f = _fields(rest)
            if len(f) >= 6 and f[0].isdigit():
                files[int(f[0])] = f[5]
        elif key == "object":
            if cur is not None:
                objs.append(cur)
            f = _fields(rest)
            try:
                cur = (float(f[0]), float(f[1]), [], [])
            except (ValueError, IndexError):
                cur = None
        elif key == "end":
            if cur is not None:
                objs.append(cur)
            cur = None
        elif key == "icon":
            f = _fields(rest)
            if len(f) < 5:
                continue
            try:
                icon = (float(f[2] or 0), int(f[3] or 0), int(f[4] or 0),
                        (f[5] if len(f) > 5 else "").replace("\\n", "\n").strip())
            except ValueError:
                continue
            if cur is not None:
                cur[2].append(icon)
            else:
                try:
                    objs.append((float(f[0]), float(f[1]), [icon], []))
                except ValueError:
                    pass
        elif key == "text":
            f = _fields(rest)
            if len(f) >= 4:
                t = f[3].replace("\\n", "\n").strip()
                if cur is not None:
                    cur[3].append(t)
                else:
                    try:
                        objs.append((float(f[0]), float(f[1]), [], [t]))
                    except ValueError:
                        pass
    if cur is not None:
        objs.append(cur)
    return files, objs


_SN_TIME = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_HEADING = re.compile(r"^Heading:\s*([A-Z]{0,3})\s*\((\d{1,3})\)", re.I)
_HIDDEN = {"phone", "email", "e-mail", "heading"}


def _sn_time(s):
    m = _SN_TIME.search(s or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_chasers(text: str) -> list:
    """Spotter Network positions: dicts with name, label, lat, lon, time, heading (None = stationary), info.
    Phone numbers and e-mail addresses in the feed are not kept."""
    files, objs = placefile_objects(text)
    out = []
    for lat, lon, icons, texts in objs:
        hover = next((i[3] for i in icons if i[3]), "")
        lines = [l.strip() for l in hover.split("\n") if l.strip()]
        if not lines:
            continue
        name, t, heading, stationary, info = lines[0], None, None, False, []
        for l in lines[1:]:
            if t is None and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*(UTC|Z|GMT)?", l):
                t = _sn_time(l)
                continue
            if l.upper() == "STATIONARY":
                stationary = True
                continue
            m = _HEADING.match(l)
            if m:
                heading = float(m.group(2)) % 360
                continue
            if ":" in l:
                k, v = l.split(":", 1)
                if k.strip().lower() not in _HIDDEN and v.strip():
                    info.append((k.strip(), v.strip()))
        if heading is None and not stationary:
            for ang, fnum, _idx, txt in icons:
                if not txt and "arrow" in files.get(fnum, "").lower():
                    heading = ang % 360
                    break
        out.append(dict(name=name, label=(texts[0] if texts and texts[0] else name), lat=lat, lon=lon,
                        time=t, heading=None if stationary else heading, info=info))
    return out


# --------------------------------------------------------------------------- storm report types
#   kind: (letter, rgb, label, filter group)
REPORT_KINDS = {
    "tornado": ("T", (255, 40, 40), "Tornado", "tornado"),
    "funnel": ("FC", (255, 150, 150), "Funnel cloud", "tornado"),
    "wall": ("WC", (220, 220, 220), "Wall cloud", "tornado"),
    "hail": ("H", (60, 220, 60), "Hail", "hail"),
    "wind_damage": ("W", (80, 160, 255), "Wind damage", "wind"),
    "wind_gust": ("G", (150, 210, 255), "Wind gust", "wind"),
    "flood": ("F", (30, 200, 180), "Flooding", "flood"),
    "other": ("•", (180, 180, 190), "Other", "other"),
}
REPORT_GROUPS = (("tornado", "Tornado"), ("hail", "Hail"), ("wind", "Wind"), ("flood", "Flood"),
                 ("other", "Other"))


def report_kind(type_text: str) -> str:
    u = (type_text or "").upper()
    if "FUNNEL" in u:
        return "funnel"
    if "WALL CLOUD" in u:
        return "wall"
    if "TORNADO" in u or "WATERSPOUT" in u or "LANDSPOUT" in u:
        return "tornado"
    if "HAIL" in u:
        return "hail"
    if "CHILL" in u:
        return "other"
    if any(k in u for k in ("WND DMG", "WIND DMG", "WIND DAMAGE", "DOWNBURST", "MICROBURST")):
        return "wind_damage"
    if "WND" in u or "WIND" in u or "GUST" in u:
        return "wind_gust"
    if "FLOOD" in u or "DEBRIS FLOW" in u:
        return "flood"
    return "other"


def parse_lsr(js: dict) -> list:
    """IEM lsr.geojson -> report dicts (lat, lon, type, kind, hover, time, source)."""
    out = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        mag = p.get("magnitude") or p.get("magf") or ""
        typ = (p.get("typetext") or "").upper()
        where = f"{p.get('city', '')}, {p.get('st') or p.get('state', '')}"
        hover = (f"{typ} {mag} {p.get('unit', '') or ''}".strip() + f"\n{where} {p.get('valid', '')}"
                 f"\n{p.get('source') or ''} · NWS {p.get('wfo') or ''}\n{(p.get('remark') or '')[:300]}").strip()
        out.append(dict(lat=float(lat), lon=float(lon), type=typ, kind=report_kind(typ), hover=hover,
                        time=_utc(p.get("valid")), source="NWS"))
    return out


def parse_sn_reports(text: str) -> list:
    """Spotter Network's reports placefile -> report dicts like parse_lsr's."""
    out = []
    for lat, lon, icons, texts in placefile_objects(text)[1]:
        hover = next((i[3] for i in icons if i[3]), texts[0] if texts else "")
        lines = [l.strip() for l in hover.split("\n") if l.strip()]
        if not lines:
            continue
        # the type line decides; the reporter's name and notes only when it doesn't say
        body = [l for l in lines if not l.lower().startswith(("reported by", "reporter"))]
        type_line = next((l for l in body if not _SN_TIME.search(l) and not l.lower().startswith(("notes", "note:"))), "")
        kind = report_kind(type_line)
        if kind == "other":
            kind = report_kind(" ".join(body))
        out.append(dict(lat=lat, lon=lon, type=(type_line.split(":")[-1].strip() or REPORT_KINDS[kind][2]).upper(),
                        kind=kind, hover="Spotter Network report\n" + "\n".join(lines), time=_sn_time(hover),
                        source="Spotter Network"))
    return out


# --------------------------------------------------------------------------- SPC
CATEGORIES = ("TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH")
CAT_RGB = {"TSTM": (193, 233, 193), "MRGL": (102, 163, 102), "SLGT": (255, 224, 102), "ENH": (255, 163, 102),
           "MDT": (224, 102, 102), "HIGH": (238, 153, 238)}     # SPC's fill colours (they read on a dark map)
CAT_NAME = {"TSTM": "General thunderstorms", "MRGL": "Marginal risk (1 of 5)", "SLGT": "Slight risk (2 of 5)",
            "ENH": "Enhanced risk (3 of 5)", "MDT": "Moderate risk (4 of 5)", "HIGH": "High risk (5 of 5)"}
MCD_RGB = (79, 143, 255)


def outlook_requests(now: datetime, limit: int = 4) -> list:
    """(valid date, cycle) of the day 1 outlooks that may be the newest at `now`, newest first.
    Day 1 is issued at 06, 13, 16:30, 20 and 01 UTC (01 UTC belongs to the previous day's date);
    each is tried 45 minutes early because SPC often issues ahead of time."""
    now = now.astimezone(timezone.utc)
    cands = []
    for back in (0, 1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        for cycle, minutes in ((6, 360), (13, 780), (16, 990), (20, 1200), (1, 1500)):
            t = day + timedelta(minutes=minutes)
            if t - timedelta(minutes=45) <= now:
                cands.append((t, day.strftime("%Y-%m-%d"), cycle))
    cands.sort(reverse=True)
    return [(d, c) for _t, d, c in cands[:limit]]


def _rings(geom: dict) -> list:
    """All rings (outer and holes) of a Polygon / MultiPolygon as lists of (lon, lat)."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates") or []
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else []
    out = []
    for poly in polys:
        for ring in poly:
            pts = [(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
            if len(pts) >= 3:
                out.append(pts)
    return out


def rings_contain(rings: list, lat: float, lon: float) -> bool:
    """Even-odd point in polygon over every ring (so holes and multi-part shapes work)."""
    inside = False
    for r in rings:
        n = len(r)
        j = n - 1
        for i in range(n):
            xi, yi = r[i]
            xj, yj = r[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def parse_outlook(js: dict) -> list:
    """Outlook areas: dicts with category (CATEGORICAL / TORNADO / WIND / HAIL), threshold, rings, issue, expire."""
    out = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        rings = _rings(f.get("geometry"))
        if not rings:
            continue
        out.append(dict(category=str(p.get("category") or "").upper(), threshold=str(p.get("threshold") or "").upper(),
                        rings=rings, issue=_utc(p.get("issue")), expire=_utc(p.get("expire"))))
    return out


def parse_mcd(js: dict) -> list:
    out = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        rings = _rings(f.get("geometry"))
        if not rings:
            continue
        wc = p.get("watch_confidence")
        out.append(dict(number=int(p.get("num") or 0), product_id=str(p.get("product_id") or ""),
                        issue=_utc(p.get("issue")), expire=_utc(p.get("expire")),
                        concerning=str(p.get("concerning") or "").strip(),
                        watch=None if wc is None else int(round(float(wc))), rings=rings))
    return out


def prob_text(threshold: str) -> str:
    if threshold == "SIGN":
        return "significant"
    try:
        return f"{round(float(threshold) * 100)}%"
    except ValueError:
        return threshold


def outlook_at(areas: list, lat: float, lon: float) -> dict | None:
    """Category and probabilities at a point: {cat, tornado, wind, hail, sig: set, expire} or None."""
    hits = [a for a in areas if rings_contain(a["rings"], lat, lon)]
    cats = [a for a in hits if a["category"] == "CATEGORICAL" and a["threshold"] in CATEGORIES]
    if not cats:
        return None
    best = max(cats, key=lambda a: CATEGORIES.index(a["threshold"]))
    res = dict(cat=best["threshold"], expire=best["expire"], issue=best["issue"], sig=set())
    for key in ("TORNADO", "WIND", "HAIL"):
        vals = []
        for a in hits:
            if a["category"] != key:
                continue
            if a["threshold"] == "SIGN":
                res["sig"].add(key)
            else:
                try:
                    vals.append(float(a["threshold"]))
                except ValueError:
                    pass
        res[key.lower()] = max(vals) if vals else None
    return res


def vtec_key(vtec: str) -> tuple:
    """('CON', 'KOUN.TO.W.0042') from a VTEC string, or ('', '')."""
    m = re.search(r"/[OTEX]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.", vtec or "")
    if not m:
        return "", ""
    return m.group(1), f"{m.group(2)}.{m.group(3)}.{m.group(4)}.{m.group(5)}"
