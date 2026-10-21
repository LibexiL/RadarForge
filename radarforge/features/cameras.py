"""Street / traffic cameras on the map.

Sources:
* Caltrans (California) – open data, no key: https://cwwp2.dot.ca.gov/data/dN/cctv/cctvStatusDNN.json
* State 511 systems built on the IBI platform (New York, Georgia, Idaho, Alaska, Louisiana, Utah, Wisconsin,
  Arizona, Nevada, Connecticut, Florida) – free developer key per state: https://<site>/api/v2/get/cameras
* Windy Webcams (worldwide, many traffic cameras) – free API key: https://api.windy.com/webcams/api/v3/webcams

Cameras are drawn only when zoomed in, one small icon per patch of screen, so they don't bury the radar.
Where a camera says which way it looks (Caltrans and 511 give a direction or "Looking North"-style view names;
Windy titles sometimes end "› South"), a small view cone shows it - dashed for Windy, where it is a guess.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import requests
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF

from ..products.geometry import aeqd_forward
from .bglayer import UA, BackgroundLayer

SHOW_BELOW_KM = 450.0          # cameras appear when the panel is narrower than this
CELL_PX = 30.0                 # one icon per CELL_PX square of screen

# state -> (name, 511 site host, rough bounding box lat0, lat1, lon0, lon1)
IBI_STATES = {
    "NY": ("New York", "511ny.org", (40.4, 45.1, -79.8, -71.8)),
    "GA": ("Georgia", "511ga.org", (30.3, 35.1, -85.7, -80.8)),
    "ID": ("Idaho", "511.idaho.gov", (41.9, 49.1, -117.3, -111.0)),
    "AK": ("Alaska", "511.alaska.gov", (51.0, 71.5, -170.0, -129.9)),
    "LA": ("Louisiana", "www.511la.org", (28.9, 33.1, -94.1, -88.8)),
    "UT": ("Utah", "prod-ut.ibi511.com", (36.9, 42.1, -114.1, -109.0)),
    "WI": ("Wisconsin", "511wi.gov", (42.4, 47.1, -92.9, -86.8)),
    "AZ": ("Arizona", "az511.gov", (31.3, 37.1, -114.9, -109.0)),
    "NV": ("Nevada", "nvroads.com", (35.0, 42.1, -120.1, -114.0)),
    "CT": ("Connecticut", "ctroads.org", (40.9, 42.1, -73.8, -71.7)),
    "FL": ("Florida", "fl511.com", (24.4, 31.1, -87.7, -79.9)),
}
CA_BOX = (32.4, 42.1, -124.5, -114.0)


@dataclass
class Camera:
    id: str
    name: str
    lat: float
    lon: float
    source: str
    views: list = field(default_factory=list)    # [(label, image url)]
    page: str = ""                               # web page for the camera, if any
    expires: float = 0.0                         # image URLs stop working after this (Windy), 0 = never
    facing: list = field(default_factory=list)   # per view: (degrees from north, "look" | "road") or None
    guess: bool = False                          # facing worked out from a title rather than given

    def headings(self) -> list:
        """The different directions this camera's views look (degrees), at most four."""
        out = []
        for f in self.facing:
            if f is not None and all(abs((f[0] - h + 180) % 360 - 180) > 10 for h in out):
                out.append(f[0])
        return out[:4]

    def facing_text(self, view: int = 0) -> str:
        """'Faces north', 'Northbound lanes' or 'Probably faces south (going by its title)'; '' when unknown."""
        f = self.facing[view] if 0 <= view < len(self.facing) else None
        if f is None:
            f = next((x for x in self.facing if x is not None), None)
        if f is None:
            return ""
        word = compass_word(f[0])
        if f[1] == "road":
            txt = f"{word.capitalize()}bound lanes" if word in ("north", "south", "east", "west") else \
                f"Traffic heading {word}"
        else:
            txt = f"Faces {word}"
        return f"Probably {txt[0].lower()}{txt[1:]} (going by its title)" if self.guess else txt


# --------------------------------------------------------------------------- which way a camera looks
_DIRS = {"n": 0.0, "nne": 22.5, "ne": 45.0, "ene": 67.5, "e": 90.0, "ese": 112.5, "se": 135.0, "sse": 157.5,
         "s": 180.0, "ssw": 202.5, "sw": 225.0, "wsw": 247.5, "w": 270.0, "wnw": 292.5, "nw": 315.0, "nnw": 337.5}
_WORD = r"(?:north|south|east|west)"
# a compass direction written out ("north", "north-east", "south west", "north-northwest") or abbreviated (NE, SSW)
_DIR = rf"(?:{_WORD}(?:[\s-]?{_WORD}){{0,2}}|[NSEW]{{1,3}})"
_LOOK = [
    rf"(?:looking|facing|look|faces|view(?:ing)?|pointing|aimed|towards?|toward)\s+(?:to(?:wards?)?\s+)?(?:the\s+)?({_DIR})\b",
    rf"[›»>|:–-]\s*({_DIR})\s*(?:view|side|facing)?\s*(?:$|[:,;()–-])",   # "Sölden › North-West: Glacier"
    rf"\b({_DIR})[\s-]+(?:view|facing|looking|camera|cam)\b",
    rf"\(({_DIR})\)",
]
_ROAD = [rf"\b({_WORD})[\s-]?bound\b", r"\b([NSEW])/?B\b"]


def _dir_degrees(word: str):
    w = word.lower().replace("-", " ").split()
    if len(w) == 1 and w[0] in _DIRS:
        return _DIRS[w[0]]
    if all(x in ("north", "south", "east", "west") for x in w) and 1 <= len(w) <= 3:
        key = "".join(x[0] for x in w)
        if len(key) == 2 and key in ("ns", "sn", "ew", "we"):
            return None
        if len(key) == 2 and key[0] in "ew":           # "east north" reads as north-east
            key = key[::-1]
        return _DIRS.get(key)
    return None


def heading_from_text(text: str):
    """(degrees from north, "look" | "road") from a camera or view name, or None.

    "Looking North", "facing SE", "› North-West", "(NE)", "west view" -> look; "Northbound", "I-95 NB" -> road.
    A lone direction inside a name ("North Main St", "N 5th Ave") is not read as one."""
    if not text:
        return None
    whole = text.strip().rstrip(".")
    if re.fullmatch(_DIR, whole, re.IGNORECASE) and (len(whole) > 3 or whole.isupper()):   # a view called "North"
        d = _dir_degrees(whole)
        if d is not None:
            return d, "look"
    for pat in _LOOK:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1)
            if raw.isalpha() and len(raw) <= 3 and not raw.isupper():     # "to the e..." - abbreviations in capitals only
                continue
            d = _dir_degrees(raw)
            if d is not None:
                return d, "look"
    for pat in _ROAD:
        m = re.search(pat, text, re.IGNORECASE if "bound" in pat else 0)
        if m:
            d = _dir_degrees(m.group(1))
            if d is not None:
                return d, "road"
    return None


def compass_word(deg: float) -> str:
    names = ("north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast", "southeast",
             "south-southeast", "south", "south-southwest", "southwest", "west-southwest", "west",
             "west-northwest", "northwest", "north-northwest")
    return names[int(((deg % 360) + 11.25) // 22.5) % 16]


def _f(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- parsers (testable)
def parse_caltrans(js: dict, district: int) -> list:
    out = []
    for rec in js.get("data", []) or []:
        c = rec.get("cctv") or {}
        loc = c.get("location") or {}
        lat, lon = _f(loc.get("latitude")), _f(loc.get("longitude"))
        if lat is None or lon is None or str(c.get("inService", "true")).lower() != "true":
            continue
        static = ((c.get("imageData") or {}).get("static") or {})
        url = static.get("currentImageURL")
        if not url:
            continue
        name = loc.get("locationName") or loc.get("nearbyPlace") or "Caltrans camera"
        route = loc.get("route")
        facing = heading_from_text(name)
        if facing is None or facing[1] == "road":
            d = _dir_degrees(str(loc.get("direction") or "").strip())       # the direction of travel it covers
            if d is not None:
                facing = (d, "road")
        out.append(Camera(f"ca{district}-{c.get('index', len(out))}", f"{route} – {name}" if route else name,
                          lat, lon, "Caltrans", [("Camera", url)], facing=[facing]))
    return out


def parse_ibi(js, state: str) -> list:
    out = []
    host = IBI_STATES.get(state, ("", "", None))[1]
    for rec in js if isinstance(js, list) else []:
        lat, lon = _f(rec.get("Latitude")), _f(rec.get("Longitude"))
        if lat is None or lon is None:
            continue
        name = rec.get("Location") or rec.get("Name") or "Camera"
        # the camera's own direction of travel ("Northbound"; "Both Directions" / "Unknown" say nothing)
        cam_dir = heading_from_text(str(rec.get("Direction") or "")) or heading_from_text(name)
        if cam_dir is None:
            d = _dir_degrees(str(rec.get("Direction") or "").strip())
            cam_dir = (d, "road") if d is not None else None
        views, facing = [], []
        for v in rec.get("Views") or []:
            if v.get("Url") and str(v.get("Status", "Enabled")).lower() != "disabled":
                label = v.get("Description") or f"View {len(views) + 1}"
                views.append((label, v["Url"]))
                facing.append(heading_from_text(label) or cam_dir)
        if not views:
            continue
        road = rec.get("Roadway")
        out.append(Camera(f"{state}-{rec.get('Id', len(out))}", f"{road} – {name}" if road and road not in name
                          else name, lat, lon, f"511 {IBI_STATES.get(state, (state,))[0]}", views,
                          page=f"https://{host}/map" if host else "", facing=facing))
    return out


def parse_windy(js: dict, fetched_at: float) -> list:
    out = []
    for w in js.get("webcams", []) or []:
        loc = w.get("location") or {}
        lat, lon = _f(loc.get("latitude")), _f(loc.get("longitude"))
        if lat is None or lon is None or str(w.get("status", "active")) != "active":
            continue
        imgs = (w.get("images") or {}).get("current") or {}
        url = imgs.get("preview") or imgs.get("thumbnail")
        if not url:
            continue
        wid = w.get("webcamId") or w.get("id")
        title = w.get("title") or "Webcam"
        out.append(Camera(f"windy-{wid}", title, lat, lon, "Windy Webcams", [("Webcam", url)],
                          page=f"https://www.windy.com/webcams/{wid}", expires=fetched_at + 9 * 60,
                          facing=[heading_from_text(title)], guess=True))
    return out


def box_near(box, lat, lon, km):
    lat0, lat1, lon0, lon1 = box
    dlat = km / 111.0
    dlon = km / (111.0 * max(math.cos(math.radians(lat)), 0.2))
    return lat0 - dlat <= lat <= lat1 + dlat and lon0 - dlon <= lon <= lon1 + dlon


def declutter(sx, sy, cell=CELL_PX):
    """Indices to draw (one per screen cell) and, for each, how many cameras share its cell."""
    if len(sx) == 0:
        return [], []
    keys = np.floor(sx / cell).astype(np.int64) * 100003 + np.floor(sy / cell).astype(np.int64)
    _u, first, counts = np.unique(keys, return_index=True, return_counts=True)
    return first.tolist(), counts.tolist()


# --------------------------------------------------------------------------- layer
class CamerasOverlay(BackgroundLayer):
    title = "Cameras"

    def __init__(self, settings, view, parent=None):
        super().__init__(settings, parent, tick_ms=30_000)
        self.view = view
        self.cams: dict = {}               # source key -> [Camera]
        self._shown = []                   # (x, y, Camera, count) drawn last
        self._xy = None
        self._windy_center = None
        self._lock = threading.Lock()

    def enabled(self):
        return bool(self.settings["overlays"].get("cameras", False))

    def keys(self) -> dict:
        return dict(self.settings["camera_keys"] or {})

    def all_cameras(self) -> list:
        with self._lock:
            return [c for lst in self.cams.values() for c in lst]

    def _center(self):
        return self.view.world_to_latlon(self.view.cx, self.view.cy)

    def jobs(self):
        if not self.enabled():
            return []
        lat, lon = self._center()
        keys = self.keys()
        jobs = []
        if self.settings["camera_caltrans"] and box_near(CA_BOX, lat, lon, 300):
            jobs.append(("caltrans", self._fetch_caltrans, 30 * 60))
        for st, (_n, host, box) in IBI_STATES.items():
            k = (keys.get(st) or "").strip()
            if k and box_near(box, lat, lon, 300):
                jobs.append((f"ibi-{st}", lambda st=st, host=host, k=k: self._fetch_ibi(st, host, k), 6 * 3600))
        wk = (keys.get("windy") or "").strip()
        if wk:
            moved = self._windy_center is None or \
                math.hypot(*aeqd_forward(lat, lon, *self._windy_center)) > 120
            if moved:
                self._next.pop("windy", None)
            jobs.append(("windy", lambda wk=wk, lat=lat, lon=lon: self._fetch_windy(wk, lat, lon), 8 * 60))
        return jobs

    def _store(self, key, cams):
        with self._lock:
            self.cams[key] = cams
        self._xy = None

    def _fetch_caltrans(self):
        cams, errors = [], 0
        for d in range(1, 13):
            try:
                r = requests.get(f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json", headers=UA,
                                 timeout=30)
                r.raise_for_status()
                cams += parse_caltrans(r.json(), d)
            except Exception:
                errors += 1
        if errors == 12:
            raise RuntimeError("Caltrans cameras unavailable")
        self._store("caltrans", cams)

    def _fetch_ibi(self, state, host, key):
        r = requests.get(f"https://{host}/api/v2/get/cameras", params={"key": key, "format": "json"}, headers=UA,
                         timeout=40)
        if r.status_code in (401, 403):
            raise RuntimeError(f"{IBI_STATES[state][0]} 511 refused the key – check it in Camera sources")
        r.raise_for_status()
        self._store(f"ibi-{state}", parse_ibi(r.json(), state))

    def _fetch_windy(self, key, lat, lon):
        cams = []
        now = time.time()
        for offset in (0, 50, 100, 150):
            r = requests.get("https://api.windy.com/webcams/api/v3/webcams",
                             params={"nearby": f"{lat:.3f},{lon:.3f},250", "limit": 50, "offset": offset,
                                     "include": "location,images"},
                             headers={**UA, "x-windy-api-key": key}, timeout=30)
            if r.status_code in (401, 403):
                raise RuntimeError("Windy refused the API key – check it in Camera sources")
            r.raise_for_status()
            js = r.json()
            got = parse_windy(js, now)
            cams += got
            if len(js.get("webcams") or []) < 50:
                break
        self._windy_center = (lat, lon)
        self._store("windy", cams)

    def refresh_camera(self, cam):
        """Fresh image URLs for a Windy camera whose links have expired (others never expire)."""
        if not cam.expires or time.time() < cam.expires:
            return cam
        key = (self.keys().get("windy") or "").strip()
        wid = cam.id.split("-", 1)[1]
        r = requests.get(f"https://api.windy.com/webcams/api/v3/webcams/{wid}", params={"include": "location,images"},
                         headers={**UA, "x-windy-api-key": key}, timeout=20)
        r.raise_for_status()
        js = r.json()
        got = parse_windy({"webcams": [js] if "webcamId" in js else js.get("webcams", [])}, time.time())
        return got[0] if got else cam

    # ---------------------------------------------------------------- drawing
    def _project(self, view):
        cams = self.all_cameras()
        key = (view.lat0, view.lon0, len(cams), id(self.cams.get("windy")))
        if self._xy is None or self._xy[0] != key:
            if cams:
                x, y = aeqd_forward(np.array([c.lat for c in cams]), np.array([c.lon for c in cams]),
                                    view.lat0, view.lon0)
            else:
                x = y = np.zeros(0)
            self._xy = (key, cams, np.asarray(x, float), np.asarray(y, float))
        return self._xy

    def paint(self, painter, vt, panel, view):
        self._shown = []
        if not self.enabled() or vt.km_across > SHOW_BELOW_KM:
            return
        _k, cams, x, y = self._project(view)
        if not cams:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=2)
        sel = np.nonzero((x >= x0) & (x <= x1) & (y >= y0) & (y <= y1))[0]
        if not len(sel):
            return
        sx, sy = vt.to_screen(x[sel], y[sel])
        idx, counts = declutter(sx - vt.rect.left(), sy - vt.rect.top())
        one, many = self._sprites(view)
        w, h = one.width() / one.devicePixelRatio(), one.height() / one.devicePixelRatio()
        cones = bool(self.settings["camera_facing"])
        for i, n in zip(idx, counts):
            cx, cy = float(sx[i]), float(sy[i])
            cam = cams[sel[i]]
            self._shown.append((float(x[sel[i]]), float(y[sel[i]]), cam, n))
            if cones and n == 1:
                self._draw_cones(painter, cx, cy, cam)
            painter.drawImage(QPointF(cx - w / 2, cy - h / 2), many if n > 1 else one)

    @staticmethod
    def _draw_cones(painter, cx, cy, cam):
        """A small view cone per direction the camera looks: solid when the agency says so, dashed for a guess."""
        heads = cam.headings()
        if not heads:
            return
        from PySide6.QtGui import QPainterPath
        r0, r1, half = 5.0, 19.0, 24.0
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        if cam.guess:
            pen = QPen(QColor(255, 214, 120, 235), 1.3, Qt.DashLine)
            fill = QColor(255, 214, 120, 45)
        else:
            pen = QPen(QColor(255, 196, 64, 245), 1.2)
            fill = QColor(255, 196, 64, 110)
        painter.setPen(pen)
        painter.setBrush(fill)
        for hdg in heads:
            path = QPainterPath()
            a0, a1 = math.radians(hdg - half), math.radians(hdg + half)
            # screen y points down; heading 0 = up (north)
            path.moveTo(cx + r0 * math.sin(a0), cy - r0 * math.cos(a0))
            path.lineTo(cx + r1 * math.sin(a0), cy - r1 * math.cos(a0))
            # Qt arcs: 0 deg = 3 o'clock, counter-clockwise positive
            path.arcTo(QRectF(cx - r1, cy - r1, 2 * r1, 2 * r1), 90 - (hdg - half), -2 * half)
            path.lineTo(cx + r0 * math.sin(a1), cy - r0 * math.cos(a1))
            path.closeSubpath()
            painter.drawPath(path)
        painter.restore()

    def _sprites(self, view):
        """Camera icons drawn once (one camera / several at one spot) and stamped onto the map."""
        dpr = view.devicePixelRatioF() if hasattr(view, "devicePixelRatioF") else 1.0
        if getattr(self, "_sprite_dpr", None) != dpr:
            from PySide6.QtGui import QImage, QPainter
            out = []
            for multi in (False, True):
                img = QImage(int(18 * dpr), int(16 * dpr), QImage.Format_ARGB32_Premultiplied)
                img.setDevicePixelRatio(dpr)
                img.fill(0)
                p = QPainter(img)
                p.setRenderHint(QPainter.Antialiasing, True)
                cx, cy = 8.0, 9.0
                p.setPen(QPen(QColor(0, 0, 0, 210), 1.2))
                p.setBrush(QColor(110, 200, 235))
                p.drawRoundedRect(QRectF(cx - 5, cy - 3.5, 8, 7), 1.5, 1.5)
                p.drawPolygon(QPolygonF([QPointF(cx + 3, cy - 1.5), QPointF(cx + 6, cy - 3.5),
                                         QPointF(cx + 6, cy + 3.5), QPointF(cx + 3, cy + 1.5)]))
                if multi:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255))
                    p.drawEllipse(QPointF(cx + 6, cy - 5), 2.2, 2.2)
                p.end()
                out.append(img)
            self._sprite_imgs, self._sprite_dpr = tuple(out), dpr
        return self._sprite_imgs

    def caption(self):
        if not self.enabled():
            return None
        n = len(self.all_cameras())
        vt_km = self.view.panels[0].rect.width() / max(self.view.scale, 1e-6) if self.view.panels else 0
        if n and vt_km > SHOW_BELOW_KM:
            return f"Cameras: {n} loaded – zoom in to see them"
        if not n and not self.busy():
            return "Cameras: none here (Layers → Satellite, MRMS & observations → Street cameras → Camera sources & keys)"
        return None

    def near(self, x, y, tol):
        """Cameras drawn near world point x, y: those within a few pixels' reach, nearest first."""
        if not self._shown:
            return []
        hits = sorted((math.hypot(cx - x, cy - y), id(c), c) for cx, cy, c, _n in self._shown
                      if math.hypot(cx - x, cy - y) < max(tol * 1.4, 0.3))
        if not hits:
            return []
        # include cameras hidden under the same icon
        _d, _i, first = hits[0]
        _k, cams, cx, cy = self._xy
        fx, fy = aeqd_forward(first.lat, first.lon, self.view.lat0, self.view.lon0)
        d = np.hypot(cx - float(fx), cy - float(fy))
        group = [cams[i] for i in np.argsort(d)[:12] if d[i] < max(tol * 2.5, 0.6)]
        return group or [first]

    def hover(self, x, y, tol):
        if not self.enabled():
            return None
        group = self.near(x, y, tol)
        if not group:
            return None
        txt = f"📷 {group[0].name}\n{group[0].source}"
        ft = group[0].facing_text()
        if ft:
            txt += f" · {ft}"
        if len(group) > 1:
            txt += f"\n+{len(group) - 1} more camera{'s' if len(group) > 2 else ''} here"
        return txt + "\nClick to view"
