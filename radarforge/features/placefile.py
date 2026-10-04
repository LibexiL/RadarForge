"""GRLevelX placefile support: parsing, fetching, refreshing and drawing."""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainterPath, QPen, QPolygonF, QTransform

from ..config import PLACEFILE_CACHE
from ..data.aws import friendly_error
from ..products.geometry import aeqd_forward

NM = 1.852


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class PFFont:
    size: int = 11
    bold: bool = False
    italic: bool = False
    face: str = "Sans"


@dataclass
class PFIconFile:
    width: int
    height: int
    hotx: int
    hoty: int
    url: str
    image: QImage | None = None


@dataclass
class PFItem:
    kind: str                      # line, polygon, triangles, text, icon, place, image
    lat: np.ndarray                # anchor or vertex latitudes
    lon: np.ndarray
    color: tuple = (255, 255, 255, 255)
    colors: list | None = None     # per-vertex colours (triangles/polygons)
    width: float = 1.0
    text: str = ""
    hover: str = ""
    font: int = 1
    angle: float = 0.0
    icon_file: int = 1
    icon_num: int = 1
    offsets: np.ndarray | None = None   # pixel offsets (Object children)
    threshold: float = 999.0
    t0: datetime | None = None
    t1: datetime | None = None
    image_url: str = ""
    uv: np.ndarray | None = None
    x: np.ndarray | None = None    # projected km
    y: np.ndarray | None = None


@dataclass
class Placefile:
    url: str
    title: str = ""
    refresh_s: float = 0.0
    items: list = field(default_factory=list)
    fonts: dict = field(default_factory=dict)
    icon_files: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    loaded_at: float = 0.0
    error: str = ""
    enabled: bool = True
    _proj: tuple | None = None


_num = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _split_args(s: str) -> list:
    """Split comma separated args honouring double-quoted strings."""
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
            continue
        if ch == "," and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _unescape(t: str) -> str:
    return t.replace("\\n", "\n").replace("\\t", "\t")


def _parse_time(t: str):
    t = t.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_placefile(text: str, url: str = "") -> Placefile:
    pf = Placefile(url=url)
    color = (255, 255, 255, 255)
    threshold = 999.0
    t0 = t1 = None
    block = None           # current multi-line block dict
    obj = None             # (lat, lon) of current Object
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("//"):
            continue
        low = line.lower()
        if low.startswith("end:"):
            if block is not None:
                pf.items.extend(_finish_block(block, obj))
                block = None
            elif obj is not None:
                obj = None
            continue
        if block is not None and ":" not in line.split(",")[0]:
            # vertex line
            parts = _split_args(line)
            try:
                nums = [float(p) for p in parts if p != ""]
            except ValueError:
                continue
            block["verts"].append(nums)
            continue
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key = key.strip().lower()
        rest = rest.strip()
        args = _split_args(rest)
        try:
            if key == "title":
                pf.title = rest
            elif key == "refresh":
                pf.refresh_s = float(args[0]) * 60.0
            elif key == "refreshseconds":
                pf.refresh_s = float(args[0])
            elif key == "threshold":
                threshold = float(args[0])
            elif key == "color":
                nums = [int(float(x)) for x in _num.findall(rest)]
                if len(nums) >= 3:
                    color = (nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 255)
            elif key == "font":
                n = int(float(args[0]))
                size = int(float(args[1])) if len(args) > 1 else 11
                flags = int(float(args[2])) if len(args) > 2 else 0
                face = args[3] if len(args) > 3 else "Sans"
                pf.fonts[n] = PFFont(size, bool(flags & 1), bool(flags & 2), face)
            elif key == "iconfile":
                n = int(float(args[0]))
                pf.icon_files[n] = PFIconFile(int(float(args[1])), int(float(args[2])), int(float(args[3])),
                                              int(float(args[4])), _join(url, args[5]))
            elif key == "timerange":
                parts = rest.split()
                if len(parts) >= 2:
                    t0, t1 = _parse_time(parts[0]), _parse_time(parts[1])
            elif key == "object":
                obj = (float(args[0]), float(args[1]))
            elif key == "place":
                lat, lon = float(args[0]), float(args[1])
                label = ",".join(args[2:]).strip()
                pf.items.append(PFItem("place", np.array([lat]), np.array([lon]), color, text=label,
                                       threshold=threshold, t0=t0, t1=t1))
            elif key == "text":
                a, b = float(args[0]), float(args[1])
                font = int(float(args[2])) if len(args) > 2 and args[2] else 1
                txt = _unescape(args[3]) if len(args) > 3 else ""
                hover = _unescape(args[4]) if len(args) > 4 else ""
                it = PFItem("text", np.array([a]), np.array([b]), color, text=txt, hover=hover, font=font,
                            threshold=threshold, t0=t0, t1=t1)
                _anchor(it, obj)
                pf.items.append(it)
            elif key == "icon":
                a, b = float(args[0]), float(args[1])
                it = PFItem("icon", np.array([a]), np.array([b]), color, angle=float(args[2] or 0),
                            icon_file=int(float(args[3])), icon_num=int(float(args[4])),
                            hover=_unescape(args[5]) if len(args) > 5 else "",
                            threshold=threshold, t0=t0, t1=t1)
                _anchor(it, obj)
                pf.items.append(it)
            elif key in ("line", "polygon", "triangles", "image"):
                block = dict(kind=key, color=color, threshold=threshold, t0=t0, t1=t1, verts=[],
                             width=float(args[0]) if key == "line" and args and args[0] else 1.0,
                             hover=_unescape(args[2]) if key == "line" and len(args) > 2 else "",
                             image=_join(url, args[0]) if key == "image" and args else "")
        except (ValueError, IndexError):
            continue
    return pf


def _anchor(it: PFItem, obj):
    if obj is not None:
        # inside an Object the first two numbers are pixel offsets (x right, y up)
        it.offsets = np.array([[float(it.lat[0]), float(it.lon[0])]])
        it.lat = np.array([obj[0]])
        it.lon = np.array([obj[1]])


def _finish_block(b, obj) -> list:
    verts = [v for v in b["verts"] if len(v) >= 2]
    if not verts:
        return []
    kind = b["kind"]
    lat = np.array([v[0] for v in verts])
    lon = np.array([v[1] for v in verts])
    it = PFItem({"line": "line", "polygon": "polygon", "triangles": "triangles", "image": "image"}[kind],
                lat, lon, b["color"], width=b["width"], hover=b["hover"], threshold=b["threshold"],
                t0=b["t0"], t1=b["t1"])
    if kind in ("polygon", "triangles"):
        cols = []
        for v in verts:
            if len(v) >= 5:
                cols.append((int(v[2]), int(v[3]), int(v[4]), int(v[5]) if len(v) > 5 else 255))
            else:
                cols.append(None)
        if any(c is not None for c in cols):
            first = next(c for c in cols if c is not None)
            it.colors = [c or first for c in cols]
            it.color = first
    if kind == "image":
        it.image_url = b["image"]
        it.uv = np.array([[v[2], v[3]] if len(v) >= 4 else [0, 0] for v in verts])
    if obj is not None:
        it.offsets = np.stack([lat, lon], 1)      # x, y pixel offsets
        it.lat = np.full(len(verts), obj[0])
        it.lon = np.full(len(verts), obj[1])
    return [it]


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _fetch_bytes(url: str, timeout=30) -> bytes:
    if url.startswith(("http://", "https://")):
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "RadarForge/1.0 (placefile client)"})
        r.raise_for_status()
        return r.content
    with open(_local_path(url), "rb") as fh:
        return fh.read()


def _local_path(url: str) -> str:
    """A local file path from 'file://…' or a plain path (works for C:\\… paths on Windows)."""
    if url.lower().startswith("file:"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        return url2pathname(urlparse(url).path)
    return url


def _join(base: str, rel: str) -> str:
    """Resolve an icon/image reference relative to the placefile it came from."""
    import os
    if not base or rel.lower().startswith(("http://", "https://", "file:")) or os.path.isabs(rel):
        return rel
    if base.lower().startswith(("http://", "https://")):
        return urljoin(base, rel)
    return os.path.join(os.path.dirname(_local_path(base)), rel.replace("/", os.sep))


def load_placefile(url: str) -> Placefile:
    data = _fetch_bytes(url)
    if data[:2] == b"\x1f\x8b":
        import gzip
        data = gzip.decompress(data)
    pf = parse_placefile(data.decode("utf-8", errors="replace"), url)
    # icon sheets
    for n, icf in pf.icon_files.items():
        try:
            img = _cached_image(icf.url)
            icf.image = img
        except Exception as exc:
            pf.error += f"icon file {n}: {exc}; "
    for it in pf.items:
        if it.kind == "image" and it.image_url and it.image_url not in pf.images:
            try:
                pf.images[it.image_url] = _cached_image(it.image_url)
            except Exception as exc:
                pf.error += f"image: {exc}; "
    pf.loaded_at = time.time()
    return pf


def _cached_image(url: str) -> QImage:
    PLACEFILE_CACHE.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(url.encode()).hexdigest()
    path = PLACEFILE_CACHE / h
    fresh = path.exists() and time.time() - path.stat().st_mtime < 86400
    if not fresh:
        data = _fetch_bytes(url)
        path.write_bytes(data)
    img = QImage(str(path))
    if img.isNull():
        raise ValueError("unreadable image")
    return img


# --------------------------------------------------------------------------- #
# manager + overlay
# --------------------------------------------------------------------------- #
class PlacefileManager(QObject):
    changed = Signal()
    status = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.files: dict = {}          # url -> Placefile
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(5000)
        self._busy: set = set()
        self._lock = threading.Lock()
        self.frame_time = None         # datetime for TimeRange filtering
        self.lat0 = self.lon0 = None

    def entries(self):
        return list(self.settings["placefiles"])

    def add(self, url: str, enabled=True):
        pfs = self.settings["placefiles"]
        if not any(p["url"] == url for p in pfs):
            pfs.append({"url": url, "enabled": enabled, "below": False, "title": ""})
            self.settings.save()
        self.reload(url)

    def remove(self, url: str):
        self.settings["placefiles"] = [p for p in self.settings["placefiles"] if p["url"] != url]
        self.settings.save()
        self.files.pop(url, None)
        self.changed.emit()

    def set_enabled(self, url: str, on: bool):
        for p in self.settings["placefiles"]:
            if p["url"] == url:
                p["enabled"] = on
        self.settings.save()
        if on and url not in self.files:
            self.reload(url)
        self.changed.emit()

    def enabled(self, url):
        return any(p["url"] == url and p.get("enabled", True) for p in self.settings["placefiles"])

    def reload_all(self):
        for p in self.settings["placefiles"]:
            if p.get("enabled", True):
                self.reload(p["url"])

    def reload(self, url: str):
        if url in self._busy:
            return
        self._busy.add(url)

        def work():
            try:
                pf = load_placefile(url)
                with self._lock:
                    self.files[url] = pf
                for p in self.settings["placefiles"]:
                    if p["url"] == url and pf.title:
                        p["title"] = pf.title
                self.status.emit(f"Placefile loaded: {pf.title or url} ({len(pf.items)} items)")
            except Exception as exc:
                old = self.files.get(url)
                if old is not None:
                    old.error = str(exc)
                else:
                    self.files[url] = Placefile(url=url, error=str(exc), loaded_at=time.time())
                self.status.emit(f"Placefile {url}: {friendly_error(exc)}")
            finally:
                self._busy.discard(url)
                self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    def _tick(self):
        now = time.time()
        for p in self.settings["placefiles"]:
            if not p.get("enabled", True):
                continue
            pf = self.files.get(p["url"])
            if pf is None:
                self.reload(p["url"])
            elif pf.refresh_s and now - pf.loaded_at >= max(pf.refresh_s, 10):
                self.reload(p["url"])

    # ------------------------------------------------------------------ drawing
    def _project(self, pf, lat0, lon0):
        if pf._proj == (lat0, lon0):
            return
        for it in pf.items:
            it.x, it.y = aeqd_forward(it.lat, it.lon, lat0, lon0)
        pf._proj = (lat0, lon0)

    def below(self, url):
        return any(p["url"] == url and p.get("below", False) for p in self.settings["placefiles"])

    def set_below(self, url: str, on: bool):
        for p in self.settings["placefiles"]:
            if p["url"] == url:
                p["below"] = on
        self.settings.save()
        self.changed.emit()

    def has_below(self):
        return any(p.get("enabled", True) and p.get("below", False) for p in self.settings["placefiles"])

    def _visible_items(self, vt, view, below=False):
        zoom_nm = vt.km_across / NM / 2.0
        with self._lock:
            files = [pf for url, pf in self.files.items() if self.enabled(url) and self.below(url) == below]
        x0, y0, x1, y1 = vt.world_bounds(pad=50.0 / max(vt.scale, 1e-3))
        t = self.frame_time
        for pf in files:
            self._project(pf, view.lat0, view.lon0)
            for it in pf.items:
                if it.x is None or zoom_nm > it.threshold:
                    continue
                if t is not None and it.t0 is not None and it.t1 is not None and not (it.t0 <= t <= it.t1):
                    continue
                if it.x.max() < x0 or it.x.min() > x1 or it.y.max() < y0 or it.y.min() > y1:
                    continue
                yield pf, it

    def paint_below(self, painter, vt, panel, view):
        self.paint(painter, vt, panel, view, below=True)

    def paint(self, painter, vt, panel, view, below=False):
        for pf, it in self._visible_items(vt, view, below):
            k = it.kind
            if k in ("line", "polygon", "triangles"):
                sx, sy = vt.to_screen(it.x, it.y)
                if it.offsets is not None:
                    sx = sx + it.offsets[:, 0]
                    sy = sy - it.offsets[:, 1]
                pts = [QPointF(a, b) for a, b in zip(sx.tolist(), sy.tolist())]
                c = QColor(*it.color)
                if k == "line":
                    painter.setPen(QPen(c, max(it.width, 0.5), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPolyline(QPolygonF(pts))
                elif k == "polygon":
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(c)
                    painter.drawPolygon(QPolygonF(pts))
                else:
                    painter.setPen(Qt.NoPen)
                    for i in range(0, len(pts) - 2, 3):
                        col = it.colors[i] if it.colors else it.color
                        painter.setBrush(QColor(*col))
                        painter.drawPolygon(QPolygonF(pts[i:i + 3]))
            elif k in ("text", "place"):
                sx, sy = vt.to_screen(float(it.x[0]), float(it.y[0]))
                if it.offsets is not None:
                    sx += float(it.offsets[0, 0])
                    sy -= float(it.offsets[0, 1])
                f = pf.fonts.get(it.font, PFFont())
                font = QFont(f.face, 1)
                font.setPixelSize(max(6, f.size))
                font.setBold(f.bold)
                font.setItalic(f.italic)
                painter.setFont(font)
                fm = QFontMetricsF(font)
                c = QColor(*it.color)
                if k == "place":
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(c)
                    painter.drawEllipse(QPointF(sx, sy), 2.5, 2.5)
                    view._halo_text(painter, sx + 5, sy + fm.ascent() / 2 - 1, it.text, c)
                else:
                    lines = it.text.split("\n")
                    h = fm.height()
                    for li, ln in enumerate(lines):
                        w = fm.horizontalAdvance(ln)
                        view._halo_text(painter, sx - w / 2, sy + fm.ascent() / 2 - (len(lines) - 1) * h / 2 + li * h,
                                        ln, c)
            elif k == "icon":
                icf = pf.icon_files.get(it.icon_file)
                if icf is None or icf.image is None:
                    continue
                cols = max(1, icf.image.width() // max(icf.width, 1))
                idx = it.icon_num - 1
                src = QRectF((idx % cols) * icf.width, (idx // cols) * icf.height, icf.width, icf.height)
                sx, sy = vt.to_screen(float(it.x[0]), float(it.y[0]))
                if it.offsets is not None:
                    sx += float(it.offsets[0, 0])
                    sy -= float(it.offsets[0, 1])
                painter.save()
                painter.translate(sx, sy)
                if it.angle:
                    painter.rotate(it.angle)
                painter.drawImage(QRectF(-icf.hotx, -icf.hoty, icf.width, icf.height), icf.image, src)
                painter.restore()
            elif k == "image":
                img = pf.images.get(it.image_url)
                if img is None:
                    continue
                sx, sy = vt.to_screen(it.x, it.y)
                w, h = img.width(), img.height()
                for i in range(0, len(sx) - 2, 3):
                    _draw_textured_triangle(painter, img, sx[i:i + 3], sy[i:i + 3], it.uv[i:i + 3], w, h)

    def hover(self, x, y, tol):
        best = None
        bd = tol
        with self._lock:
            files = [pf for url, pf in self.files.items() if self.enabled(url)]
        for pf in files:
            for it in pf.items:
                if not it.hover or it.x is None:
                    continue
                if it.kind == "line" and it.offsets is None and len(it.x) > 1:
                    d = _dist_to_polyline(x, y, it.x, it.y)
                else:
                    d = math.hypot(float(it.x[0]) - x, float(it.y[0]) - y)
                if d < bd:
                    bd, best = d, it.hover
        return best


def _dist_to_polyline(px, py, xs, ys):
    ax, ay, bx, by = xs[:-1], ys[:-1], xs[1:], ys[1:]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / np.where(L > 0, L, 1), 0, 1)
    cx, cy = ax + t * dx, ay + t * dy
    return float(np.min(np.hypot(px - cx, py - cy)))


def _draw_textured_triangle(painter, img, sx, sy, uv, w, h):
    src = np.array([[uv[0][0] * w, uv[0][1] * h], [uv[1][0] * w, uv[1][1] * h], [uv[2][0] * w, uv[2][1] * h]])
    dst = np.stack([sx, sy], 1)
    A = np.array([[src[0][0], src[0][1], 1], [src[1][0], src[1][1], 1], [src[2][0], src[2][1], 1]])
    try:
        mx = np.linalg.solve(A, dst[:, 0])
        my = np.linalg.solve(A, dst[:, 1])
    except np.linalg.LinAlgError:
        return
    tr = QTransform(mx[0], my[0], mx[1], my[1], mx[2], my[2])
    path = QPainterPath()
    path.moveTo(sx[0], sy[0])
    path.lineTo(sx[1], sy[1])
    path.lineTo(sx[2], sy[2])
    path.closeSubpath()
    painter.save()
    painter.setClipPath(path, Qt.IntersectClip)
    painter.setTransform(tr, True)
    painter.drawImage(0, 0, img)
    painter.restore()
