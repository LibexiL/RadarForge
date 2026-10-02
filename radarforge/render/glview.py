"""Multi-panel OpenGL radar view (1-6 linked panels)."""
from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

import numpy as np
import OpenGL

OpenGL.ERROR_CHECKING = False      # glGetError after every call is very slow
OpenGL.ERROR_LOGGING = False
from OpenGL import GL  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPen, QPolygonF, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import QToolTip, QWidget

from .. import fmt
from ..data.sites import all_sites
from ..products.geometry import aeqd_forward, aeqd_inverse, az_range, ground_range
from ..tools import track as track_maths
from . import shaders
from .maps import DRAW_ORDER, LAYER_STYLE, MapData
from .fonts import ui_font

ND = -60000.0
RFV = 60000.0

# default map colours (a theme can override them, see themes.py)
DEFAULT_COLORS = {
    "map_bg": (8, 8, 12), "map_gap": (31, 31, 36), "panel_border": (70, 70, 80),
    "active_border": (90, 140, 220), "halo": (0, 0, 0, 220),
    "city_text": (225, 225, 225), "city_dot": (230, 230, 230),
    "site_text": (205, 230, 210), "site_88d": (40, 150, 90), "site_tdwr": (150, 110, 200),
    "site_current": (255, 215, 0), "rings": (200, 200, 215, 150),
    "label_bg": (0, 0, 0, 185), "label_text": (240, 240, 245), "cursor": (255, 255, 255, 230),
}
LEGEND_H = 32

LAYOUTS = {
    1: [(0, 0, 1, 1)],
    2: [(0, 0, .5, 1), (.5, 0, .5, 1)],
    3: [(0, 0, 1 / 3, 1), (1 / 3, 0, 1 / 3, 1), (2 / 3, 0, 1 / 3, 1)],
    4: [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5), (.5, .5, .5, .5)],
    5: [(0, 0, 1 / 3, .5), (1 / 3, 0, 1 / 3, .5), (2 / 3, 0, 1 / 3, .5), (0, .5, .5, .5), (.5, .5, .5, .5)],
    6: [(0, 0, 1 / 3, .5), (1 / 3, 0, 1 / 3, .5), (2 / 3, 0, 1 / 3, .5),
        (0, .5, 1 / 3, .5), (1 / 3, .5, 1 / 3, .5), (2 / 3, .5, 1 / 3, .5)],
}


def default_format():
    from ..gl_setup import make_format
    return make_format("core-msaa")


@dataclass
class Panel:
    index: int
    product: str
    rect: QRectF = field(default_factory=QRectF)
    image: object = None          # SweepImage
    palette: object = None        # ColorTable
    storage_units: str = ""
    header: str = ""
    message: str = ""
    readout: str = ""


class ViewTransform:
    """World (km) <-> screen mapping for one panel."""

    def __init__(self, rect: QRectF, cx: float, cy: float, scale: float):
        self.rect = rect
        self.cx, self.cy, self.scale = cx, cy, scale
        self.ox = rect.x() + rect.width() / 2.0
        self.oy = rect.y() + rect.height() / 2.0

    def to_screen(self, x, y):
        return self.ox + (x - self.cx) * self.scale, self.oy - (y - self.cy) * self.scale

    def to_world(self, sx, sy):
        return self.cx + (sx - self.ox) / self.scale, self.cy - (sy - self.oy) / self.scale

    def world_bounds(self, pad=0.0):
        hw = self.rect.width() / 2 / self.scale + pad
        hh = self.rect.height() / 2 / self.scale + pad
        return self.cx - hw, self.cy - hh, self.cx + hw, self.cy + hh

    @property
    def km_across(self):
        return self.rect.width() / self.scale


def window_format(depth=True):
    """Default format plus the depth/stencil buffers QPainter and 3-D drawing need."""
    fmt = QSurfaceFormat(QSurfaceFormat.defaultFormat())
    fmt.setStencilBufferSize(8)
    if depth:
        fmt.setDepthBufferSize(24)
    return fmt


class RadarView(QOpenGLWindow):
    """The radar panels. A native OpenGL window (embedded with make_container()).

    It draws straight to its own window instead of through Qt's widget compositor
    (QOpenGLWidget), which some NVIDIA drivers get wrong: black map, frozen or
    mirrored screen after a resize.
    """
    cursorMoved = Signal(float, float, int)       # world x, y (km), panel index (-1 outside)
    panelMenuRequested = Signal(int, object)      # panel index, global QPoint
    lineDrawn = Signal(str, float, float, float, float)   # tool, x0, y0, x1, y1 (km)
    boxDrawn = Signal(float, float, float, float)         # x0, y0, x1, y1 (km)
    siteClicked = Signal(str)
    viewChanged = Signal()
    panelActivated = Signal(int)
    trackChanged = Signal()                               # storm track placed / moved / cleared

    def __init__(self, parent=None):
        super().__init__(QOpenGLWindow.NoPartialUpdate)
        self.setFormat(window_format())
        self.host = None                  # the QWidget container (set by make_container)
        self.drop_handler = None          # callable(kind, event) for files dragged onto the map
        self.overlay_image = None         # drawn on top (panel drop zones while dragging a panel)
        self.panels: list = [Panel(0, "REF")]
        self.layout_n = 1
        self.cx, self.cy, self.scale = 0.0, 0.0, 2.0   # scale: px per km
        self.lat0, self.lon0 = 35.333, -97.278
        self.site_id = ""
        self.maps = MapData()
        self.map_visible = {n: True for n in DRAW_ORDER}
        self.show_cities = True
        self.show_range_rings = False
        self.show_sites = True
        self.show_tdwr = False
        self._sites_xy = []               # [(id, x, y, type)] projected radar sites
        self.show_legend = True
        self.smooth = False
        self.tool = "pan"
        self.overlays: list = []          # objects with paint(painter, view_transform, panel, widget)
        self.underlays: list = []         # objects with paint_below(...) drawn beneath the radar data
        self.hover_providers: list = []   # objects with hover(x, y, tolerance_km) -> str|None
        self.cursor_world = None
        self.cursor_panel = -1
        self.link_cursor = True
        self.invert_wheel = False
        self.hover_text = True
        self.distance_units = "nm"
        self.height_units = "kft"
        self.active_panel = 0
        self._drag = None
        self._line = None                 # (tool, x0, y0, x1, y1)
        self.persistent_lines: list = []  # [(label, x0, y0, x1, y1, QColor)]
        self._boxdrag = None              # box being dragged (x0, y0, x1, y1)
        self.drop_panel = -1              # panel highlighted while dragging a colour table
        self._hover_site = None
        self.box3d = None                 # selected 3-D area shown on the map
        # storm track tool: {"a": (x, y) storm now, "b": (x, y) storm after "minutes", "start": datetime,
        # "etas": [(town, minutes, x, y)]}; positions in km
        self.track = None
        self.track_minutes = 60
        self.track_time_fn = None         # () -> datetime of the frame shown (track times count from it)
        self.track_default_fn = None      # (x, y, minutes) -> (x, y): where the storm motion takes it
        self.track_half_width = 10.0      # km either side of the track for "towns in its path"
        self._track_drag = None           # "a" / "b" while dragging an end
        self._gl_ready = False
        self._gpu: OrderedDict = OrderedDict()
        self._luts: dict = {}
        self._map_vbos: dict = {}
        self._maps_dirty = True
        self._legend_cache: dict = {}
        self._pending_delete: list = []
        self.gl_info = ""
        self.colors = {k: QColor(*v) for k, v in DEFAULT_COLORS.items()}
        self.layer_style = dict(LAYER_STYLE)
        self.font_small = ui_font(8)
        self.font_label = ui_font(9)
        self.font_header = ui_font(9, True)
        # rendering caches: the finished scene (so hover only redraws the cursor), text sprites,
        # colour-bar images and city label placement
        self._scene_dirty = True
        self._cache = None                # (fbo, tex, w, h)
        import os
        self.scene_cache_enabled = os.environ.get("RADARFORGE_SCENE_CACHE", "1") != "0"
        self._cache_ok = self.scene_cache_enabled
        self._cache_valid = False
        self._wheel_timer = QTimer(self)
        self._wheel_timer.setSingleShot(True)
        self._wheel_timer.setInterval(180)
        self._wheel_timer.timeout.connect(self.update)
        self._sprites: dict = {}
        self._city_cache: dict = {}
        self._header_w = 0
        self._caption_y = 38              # next free line for draw_caption() (below the panel header)
        self.prog_blit = None
        self._blit_vao = None

    def make_container(self, parent=None):
        """Widget that holds this window inside the main window's layouts."""
        self.host = QWidget.createWindowContainer(self, parent)
        self.host.setMinimumSize(320, 240)
        self.host.setFocusPolicy(Qt.StrongFocus)
        self.host.setAcceptDrops(True)
        return self.host

    def devicePixelRatioF(self):
        return float(self.devicePixelRatio())

    def set_overlay_image(self, img):
        self.overlay_image = img
        self.update_cursor()

    @property
    def bg(self):
        return self.colors["map_bg"]

    def set_colors(self, colors: dict, layer_style: dict | None = None):
        """Apply theme colours (role -> QColor) and optional map layer styles."""
        for k, v in colors.items():
            if k in self.colors:
                self.colors[k] = QColor(v)
        if layer_style:
            self.layer_style = dict(layer_style)
        self._sprites.clear()
        self._legend_cache.clear()
        self.update()

    # QWidget.update() is used everywhere for "something changed": that invalidates the cached
    # scene. Cursor-only changes call update_cursor(), which reuses it.
    def update(self, *args):
        self._scene_dirty = True
        super().update(*args)

    def update_cursor(self):
        super().update()

    # ------------------------------------------------------------------ API
    def set_layout(self, n: int, products: list):
        n = max(1, min(6, n))
        self.layout_n = n
        old = {p.index: p for p in self.panels}
        self.panels = []
        for i in range(n):
            p = old.get(i) or Panel(i, products[i] if i < len(products) else "REF")
            p.product = products[i] if i < len(products) else p.product
            self.panels.append(p)
        self._layout()
        self.update()

    def set_projection(self, lat: float, lon: float, site_id: str = ""):
        if (lat, lon) != (self.lat0, self.lon0) or not self.maps.layers:
            self.lat0, self.lon0 = lat, lon
            self.maps.project(lat, lon)
            self._maps_dirty = True
            self._city_cache.clear()
            self._project_sites()
        elif not self._sites_xy:
            self._project_sites()
        self.site_id = site_id
        self.update()

    def _project_sites(self):
        sites = list(all_sites().values())
        lat = np.array([s.lat for s in sites])
        lon = np.array([s.lon for s in sites])
        x, y = aeqd_forward(lat, lon, self.lat0, self.lon0)
        self._sites_xy = [(s.id, float(xx), float(yy), s.type) for s, xx, yy in zip(sites, x, y)]

    def _visible_sites(self, vt):
        x0, y0, x1, y1 = vt.world_bounds(pad=20.0 / max(vt.scale, 1e-3))
        for sid, x, y, kind in self._sites_xy:
            if kind == "tdwr" and not self.show_tdwr:
                continue
            if x0 <= x <= x1 and y0 <= y <= y1:
                yield sid, x, y, kind

    def site_at(self, pos, radius_px=11.0):
        """Radar site id under a screen position (or None)."""
        if not self.show_sites:
            return None
        i = self.panel_at(pos)
        if i < 0:
            return None
        vt = self.transform(self.panels[i])
        best, bd = None, radius_px
        for sid, x, y, _k in self._visible_sites(vt):
            sx, sy = vt.to_screen(x, y)
            d = math.hypot(sx - pos.x(), sy - pos.y())
            if d < bd:
                best, bd = sid, d
        return best

    def set_view(self, cx, cy, scale):
        self.cx, self.cy, self.scale = cx, cy, max(0.02, min(400.0, scale))
        self.viewChanged.emit()
        self.update()

    def reset_view(self):
        w = min(r.width() for r in (p.rect for p in self.panels)) if self.panels else self.width()
        self.set_view(0.0, 0.0, max(0.2, w / 500.0))

    def transform(self, panel: Panel) -> ViewTransform:
        return ViewTransform(panel.rect, self.cx, self.cy, self.scale)

    def panel_at(self, pos) -> int:
        for p in self.panels:
            if p.rect.contains(pos):
                return p.index
        return -1

    def world_at(self, pos):
        i = self.panel_at(pos)
        if i < 0:
            return None, None, -1
        x, y = self.transform(self.panels[i]).to_world(pos.x(), pos.y())
        return x, y, i

    def world_to_latlon(self, x, y):
        lat, lon = aeqd_inverse(x, y, self.lat0, self.lon0)
        return float(lat), float(lon)

    # ------------------------------------------------------------------ layout
    def _layout(self):
        w, h = self.width(), self.height()
        gap = 2
        for p, (fx, fy, fw, fh) in zip(self.panels, LAYOUTS[self.layout_n]):
            p.rect = QRectF(fx * w + (gap / 2 if fx > 0 else 0), fy * h + (gap / 2 if fy > 0 else 0),
                            fw * w - (gap / 2 if fx > 0 else 0) - (gap / 2 if fx + fw < 0.999 else 0),
                            fh * h - (gap / 2 if fy > 0 else 0) - (gap / 2 if fy + fh < 0.999 else 0))

    def resizeGL(self, w, h):
        self._layout()
        self._city_cache.clear()
        self.update()          # a native window that shrinks gets no expose event: redraw it ourselves

    # ------------------------------------------------------------------ GL setup
    def initializeGL(self):
        try:
            self.gl_info = (GL.glGetString(GL.GL_VERSION) or b"").decode(errors="replace") + " / " + \
                           (GL.glGetString(GL.GL_RENDERER) or b"").decode(errors="replace")
        except Exception:
            self.gl_info = "?"
        self.prog_radar = self._program(shaders.RADAR_VS, shaders.RADAR_FS)
        self.prog_line = self._program(shaders.LINE_VS, shaders.LINE_FS, shaders.LINE_GS)
        self._u = {}
        for name in ("u_view", "u_vp", "u_offset", "u_data", "u_lut", "u_first", "u_spacing", "u_elev",
                     "u_ground", "u_ngates", "u_nrad", "u_dscale", "u_doffset", "u_lutmin", "u_lutmax",
                     "u_rf", "u_smooth", "u_alpha", "u_nyq"):
            self._u[name] = GL.glGetUniformLocation(self.prog_radar, name)
        self._ul = {n: GL.glGetUniformLocation(self.prog_line, n) for n in ("u_view", "u_vp", "u_width", "u_color")}
        self._cache_ok = False
        if self.scene_cache_enabled:
            try:
                self.prog_blit = self._program(shaders.BLIT_VS, shaders.BLIT_FS)
                self._u_blit = GL.glGetUniformLocation(self.prog_blit, "u_tex")
                self._blit_vao = GL.glGenVertexArrays(1)
                self._cache_ok = True
            except Exception as exc:
                print("RadarForge: scene cache disabled:", exc)
        self._cache = None
        self._scene_dirty = True
        self._gpu.clear()
        self._luts.clear()
        self._map_vbos = {}
        self._maps_dirty = True
        self._gl_ready = True
        self.context().aboutToBeDestroyed.connect(self._cleanup)

    def _program(self, vs, fs, gs=None):
        def comp(src, kind):
            s = GL.glCreateShader(kind)
            GL.glShaderSource(s, src)
            GL.glCompileShader(s)
            if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
                raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
            return s
        prog = GL.glCreateProgram()
        objs = [comp(vs, GL.GL_VERTEX_SHADER), comp(fs, GL.GL_FRAGMENT_SHADER)]
        if gs:
            objs.append(comp(gs, GL.GL_GEOMETRY_SHADER))
        for o in objs:
            GL.glAttachShader(prog, o)
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(prog).decode())
        for o in objs:
            GL.glDeleteShader(o)
        return prog

    def _cleanup(self):
        self.makeCurrent()
        for res in list(self._gpu.values()):
            self._free(res)
        self._gpu.clear()
        for tex, _lo, _hi, _ct in self._luts.values():
            GL.glDeleteTextures([tex])
        self._luts.clear()
        for vao, vbo, _n in self._map_vbos.values():
            GL.glDeleteVertexArrays(1, [vao])
            GL.glDeleteBuffers(1, [vbo])
        self._map_vbos.clear()
        self._free_cache()
        self.doneCurrent()

    def _free_cache(self):
        if self._cache is not None:
            fbo, tex, _w, _h = self._cache
            try:
                GL.glDeleteFramebuffers(1, [fbo])
                GL.glDeleteTextures([tex])
            except Exception:
                pass
            self._cache = None

    # ------------------------------------------------------------------ GPU resources
    @staticmethod
    def _free(res):
        tex, vao, vbo, _n = res
        GL.glDeleteTextures([tex])
        GL.glDeleteVertexArrays(1, [vao])
        GL.glDeleteBuffers(1, [vbo])

    def _gpu_image(self, img):
        key = img.key
        res = self._gpu.get(key)
        if res is not None:
            self._gpu.move_to_end(key)
            return res
        vals = img.gpu_values()
        nrad, ng = vals.shape
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R16F, ng, nrad, 0, GL.GL_RED, GL.GL_HALF_FLOAT,
                        np.ascontiguousarray(vals))
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
        # geometry: one wedge (2 triangles) per radial
        r_in = max(0.0, img.first_gate - img.gate_spacing / 2)
        r_out = img.max_range + img.gate_spacing
        if img.ground_range:
            s0, s1 = r_in, r_out
        else:
            s0, s1 = float(ground_range(r_in, img.elevation)), float(ground_range(r_out, img.elevation)) + 1.0
        lo = np.radians(img.az_lo.astype(np.float64))
        hi = np.radians(img.az_hi.astype(np.float64))
        rows = np.arange(nrad, dtype=np.float32)
        def pt(a, s):
            return np.stack([s * np.sin(a), s * np.cos(a)], 1)
        c0, c1, c2, c3 = pt(lo, s0), pt(hi, s0), pt(lo, s1), pt(hi, s1)
        verts = np.empty((nrad, 6, 5), np.float32)
        for k, c in enumerate((c0, c1, c2, c1, c3, c2)):
            verts[:, k, 0:2] = c
        verts[:, :, 2] = rows[:, None]
        verts[:, :, 3] = img.az_lo[:, None]
        verts[:, :, 4] = img.az_hi[:, None]
        verts = verts.reshape(-1, 5)
        vao = GL.glGenVertexArrays(1)
        vbo = GL.glGenBuffers(1)
        GL.glBindVertexArray(vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STATIC_DRAW)
        stride = 20
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, stride, GL.ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 1, GL.GL_FLOAT, False, stride, GL.ctypes.c_void_p(8))
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 2, GL.GL_FLOAT, False, stride, GL.ctypes.c_void_p(12))
        GL.glBindVertexArray(0)
        res = (tex, vao, vbo, len(verts))
        self._gpu[key] = res
        while len(self._gpu) > 96:
            _, old = self._gpu.popitem(last=False)
            self._free(old)
        return res

    def _lut(self, ct):
        k = id(ct)
        res = self._luts.get(k)
        if res is None or res[3] is not ct:
            lut, lo, hi = ct.lut(1024)
            tex = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, lut.shape[0], 1, 0, GL.GL_RGBA,
                            GL.GL_UNSIGNED_BYTE, np.ascontiguousarray(lut))
            for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                         (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
            res = (tex, lo, hi, ct)
            self._luts[k] = res
        return res

    def _upload_maps(self):
        for vao, vbo, _n in self._map_vbos.values():
            GL.glDeleteVertexArrays(1, [vao])
            GL.glDeleteBuffers(1, [vbo])
        self._map_vbos = {}
        for name, layer in self.maps.layers.items():
            data = np.ascontiguousarray(layer.segments, np.float32)
            if len(data) == 0:
                continue
            vao = GL.glGenVertexArrays(1)
            vbo = GL.glGenBuffers(1)
            GL.glBindVertexArray(vao)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(0)
            GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 8, GL.ctypes.c_void_p(0))
            GL.glBindVertexArray(0)
            self._map_vbos[name] = (vao, vbo, len(data))
        self._maps_dirty = False

    # ------------------------------------------------------------------ painting
    def _native(self, painter, fn):
        ok = True
        painter.beginNativePainting()
        try:
            fn()
        except Exception as exc:     # never let a GL error kill the event loop
            print("paintGL error:", exc)
            ok = False
        finally:
            GL.glBindVertexArray(0)
            GL.glUseProgram(0)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glDisable(GL.GL_SCISSOR_TEST)
            dpr = self.devicePixelRatioF()
            GL.glViewport(0, 0, int(self.width() * dpr), int(self.height() * dpr))
            painter.endNativePainting()
        return ok

    def paintGL(self):
        dpr = self.devicePixelRatioF()
        fw, fh = int(round(self.width() * dpr)), int(round(self.height() * dpr))
        GL.glViewport(0, 0, fw, fh)
        painter = QPainter(self)
        cache = self._cache
        reuse = (self._cache_ok and self._cache_valid and not self._scene_dirty and cache is not None
                 and cache[2:] == (fw, fh))
        if not (reuse and self._native(painter, lambda: self._restore_scene(fw, fh))):
            self._paint_scene(painter)
            self._cache_valid = False
            if self._cache_ok and not self._interacting():
                if self._native(painter, lambda: self._store_scene(fw, fh)):
                    self._cache_valid = True
                else:
                    self._cache_ok = False          # never try again this session
            self._scene_dirty = False
        self._paint_live(painter)
        painter.end()

    def _interacting(self):
        return self._drag is not None or self._wheel_timer.isActive()

    def _store_scene(self, fw, fh):
        cache = self._cache
        if cache is None or cache[2:] != (fw, fh):
            self._free_cache()
            tex = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, fw, fh, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
            for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                         (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
            fbo = GL.glGenFramebuffers(1)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
            complete = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
            self._cache = cache = (fbo, tex, fw, fh)
            if not complete:
                raise RuntimeError("scene cache framebuffer incomplete")
        GL.glDisable(GL.GL_SCISSOR_TEST)
        for _ in range(8):                              # clear old errors so we only see the blit's
            if GL.glGetError() == GL.GL_NO_ERROR:
                break
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, cache[0])
        GL.glBlitFramebuffer(0, 0, fw, fh, 0, 0, fw, fh, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        err = GL.glGetError()
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        if err != GL.GL_NO_ERROR:
            raise RuntimeError(f"copying the frame failed (GL error {err:#x}); drawing every frame instead")

    def _restore_scene(self, fw, fh):
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glViewport(0, 0, fw, fh)
        GL.glUseProgram(self.prog_blit)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._cache[1])
        GL.glUniform1i(self._u_blit, 0)
        GL.glBindVertexArray(self._blit_vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glEnable(GL.GL_BLEND)

    def _paint_scene(self, painter):
        # 1) background   2) layers that go *under* the radar   3) radar + maps   4) overlays
        self._native(painter, self._gl_clear)
        under = [u for u in self.underlays if getattr(u, "has_below", lambda: True)()]
        if under:
            painter.setRenderHint(QPainter.Antialiasing, True)
            for p in self.panels:
                vt = self.transform(p)
                painter.save()
                painter.setClipRect(p.rect)
                for u in under:
                    try:
                        u.paint_below(painter, vt, p, self)
                    except Exception as exc:
                        print("underlay error:", exc)
                painter.restore()
        self._native(painter, self._paint_gl)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        self._header_w = max((self._header_width(p) for p in self.panels), default=0)
        # map overlays depend only on the view, not the product: render them once per panel size
        # (a 2x2 layout paints them once instead of four times) and stamp the image into each panel
        layers = {}
        for p in self.panels:
            key = (round(p.rect.width()), round(p.rect.height()))
            if key not in layers:
                layers[key] = self._overlay_layer(p, *key)
            painter.save()
            painter.setClipRect(p.rect)
            painter.drawImage(QPointF(round(p.rect.left()), round(p.rect.top())), layers[key])
            try:
                self._paint_header(painter, p)
                if self.show_legend and p.palette is not None:
                    self._paint_legend(painter, p)
            except Exception as exc:
                print("panel label error:", exc)
            painter.restore()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self.colors["panel_border"], 1))
        for p in self.panels:
            painter.drawRect(p.rect.adjusted(0, 0, -1, -1))

    def _paint_live(self, painter):
        """Things that change with every mouse move: cursor, readouts, rubber bands, highlights."""
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        for p in self.panels:
            vt = self.transform(p)
            painter.save()
            painter.setClipRect(p.rect)
            try:
                self._paint_cursor(painter, p, vt)
                self._paint_live_tools(painter, vt)
            except Exception as exc:
                print("cursor paint error:", exc)
            painter.restore()
        if 0 <= self.drop_panel < len(self.panels):
            dp = self.panels[self.drop_panel]
            painter.setPen(QPen(QColor(255, 210, 60), 3))
            painter.setBrush(QColor(255, 210, 60, 30))
            painter.drawRect(dp.rect.adjusted(2, 2, -3, -3))
            from ..products import catalog as _cat
            self._halo_text(painter, dp.rect.left() + 12, dp.rect.center().y(),
                            f"Drop to use this colour table for {_cat.get(dp.product).name}",
                            QColor(255, 225, 120), self.font_header)
        painter.setBrush(Qt.NoBrush)
        if len(self.panels) > 1:
            ap = self.panels[min(self.active_panel, len(self.panels) - 1)]
            painter.setPen(QPen(self.colors["active_border"], 1.5))
            painter.drawRect(ap.rect.adjusted(0.5, 0.5, -1, -1))
        if self.overlay_image is not None:
            painter.drawImage(QPointF(0, 0), self.overlay_image)

    def _panel_viewport(self, r, dpr, H):
        return int(r.x() * dpr), int(H - (r.y() + r.height()) * dpr), int(r.width() * dpr), int(r.height() * dpr)

    def _gl_clear(self):
        dpr = self.devicePixelRatioF()
        H = self.height() * dpr
        GL.glDisable(GL.GL_SCISSOR_TEST)
        gap = self.colors["map_gap"]
        GL.glClearColor(gap.redF(), gap.greenF(), gap.blueF(), 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        for p in self.panels:
            x, y, w, h = self._panel_viewport(p.rect, dpr, H)
            if w <= 0 or h <= 0:
                continue
            GL.glScissor(x, y, w, h)
            GL.glClearColor(self.bg.redF(), self.bg.greenF(), self.bg.blueF(), 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)

    def _paint_gl(self):
        if self._maps_dirty:
            self._upload_maps()
        dpr = self.devicePixelRatioF()
        H = self.height() * dpr
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        for p in self.panels:
            x, y, w, h = self._panel_viewport(p.rect, dpr, H)
            if w <= 0 or h <= 0:
                continue
            GL.glViewport(x, y, w, h)
            GL.glScissor(x, y, w, h)
            view = (self.cx, self.cy, self.scale * dpr, 0.0)
            vp = (float(w), float(h))
            if p.image is not None and p.palette is not None:
                self._draw_image(p, view, vp)
            self._draw_maps(view, vp, dpr)

    def _draw_image(self, p, view, vp):
        img = p.image
        tex, vao, _vbo, n = self._gpu_image(img)
        lut_tex, lo, hi, _ct = self._lut(p.palette)
        u = self._u
        GL.glUseProgram(self.prog_radar)
        GL.glUniform4f(u["u_view"], *view)
        GL.glUniform2f(u["u_vp"], *vp)
        GL.glUniform2f(u["u_offset"], 0.0, 0.0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glUniform1i(u["u_data"], 0)
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, lut_tex)
        GL.glUniform1i(u["u_lut"], 1)
        GL.glUniform1f(u["u_first"], img.first_gate)
        GL.glUniform1f(u["u_spacing"], img.gate_spacing)
        GL.glUniform1f(u["u_elev"], math.radians(img.elevation))
        GL.glUniform1i(u["u_ground"], 1 if img.ground_range else 0)
        GL.glUniform1i(u["u_ngates"], img.values.shape[1])
        GL.glUniform1i(u["u_nrad"], img.values.shape[0])
        GL.glUniform1f(u["u_dscale"], p.palette.data_scale(p.storage_units))
        GL.glUniform1f(u["u_doffset"], p.palette.offset)
        GL.glUniform1f(u["u_lutmin"], lo)
        GL.glUniform1f(u["u_lutmax"], hi)
        rf = p.palette.rf or (119, 0, 125, 255)
        GL.glUniform4f(u["u_rf"], rf[0] / 255, rf[1] / 255, rf[2] / 255, (rf[3] if len(rf) > 3 else 255) / 255)
        from ..products import catalog
        prod = catalog.get(p.product)
        GL.glUniform1i(u["u_smooth"], 1 if (self.smooth and prod.smooth and not prod.categorical) else 0)
        GL.glUniform1f(u["u_alpha"], 1.0)
        # raw (possibly aliased) velocity: blend neighbours on the Nyquist circle
        fold = p.product in ("VEL", "SRV", "L3S") and not img.extra.get("dealiased") and img.nyquist
        GL.glUniform1f(u["u_nyq"], float(img.nyquist) if fold else 0.0)
        GL.glBindVertexArray(vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, n)
        GL.glBindVertexArray(0)
        GL.glActiveTexture(GL.GL_TEXTURE0)

    def _draw_maps(self, view, vp, dpr):
        GL.glUseProgram(self.prog_line)
        GL.glUniform4f(self._ul["u_view"], *view)
        GL.glUniform2f(self._ul["u_vp"], *vp)
        for name in DRAW_ORDER:
            if not self.map_visible.get(name, True) or name not in self._map_vbos:
                continue
            _label, rgba, width, min_scale = self.layer_style[name]
            if self.scale < min_scale:
                continue
            vao, _vbo, n = self._map_vbos[name]
            GL.glUniform4f(self._ul["u_color"], rgba[0] / 255, rgba[1] / 255, rgba[2] / 255, rgba[3] / 255)
            GL.glUniform1f(self._ul["u_width"], width * dpr)
            GL.glBindVertexArray(vao)
            GL.glDrawArrays(GL.GL_LINES, 0, n)
        GL.glBindVertexArray(0)

    # ------------------------------------------------------------------ overlays (QPainter)
    def _sprite(self, text, color, font):
        """Text with a dark halo, rendered once and reused as an image (far cheaper than 5 drawText calls)."""
        dpr = self.devicePixelRatioF()
        key = (text, color.rgba(), font.key(), dpr)
        spr = self._sprites.get(key)
        if spr is None:
            fm = QFontMetricsF(font)
            pad = 2
            w = fm.horizontalAdvance(text) + 2 * pad + 1
            h = fm.height() + 2 * pad
            img = QImage(max(1, math.ceil(w * dpr)), max(1, math.ceil(h * dpr)), QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(0)
            qp = QPainter(img)
            qp.setFont(font)
            qp.setRenderHint(QPainter.TextAntialiasing, True)
            base = pad + fm.ascent()
            qp.setPen(self.colors["halo"])
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                qp.drawText(QPointF(pad + dx, base + dy), text)
            qp.setPen(color)
            qp.drawText(QPointF(pad, base), text)
            qp.end()
            spr = (img, pad, base)
            if len(self._sprites) > 5000:
                self._sprites.clear()
            self._sprites[key] = spr
        return spr

    def _halo_text(self, painter, x, y, text, color=QColor(235, 235, 235), font=None):
        if font is not None:
            painter.setFont(font)
        else:
            font = painter.font()
        img, pad, base = self._sprite(text, QColor(color), font)
        painter.drawImage(QPointF(round(x - pad), round(y - base)), img)

    def _overlay_layer(self, p, w, h):
        dpr = self.devicePixelRatioF()
        img = QImage(max(1, int(w * dpr)), max(1, int(h * dpr)), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        qp = QPainter(img)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.setRenderHint(QPainter.TextAntialiasing, True)
        try:
            self._paint_overlays(qp, p, ViewTransform(QRectF(0, 0, w, h), self.cx, self.cy, self.scale))
        except Exception as exc:
            print("overlay error:", exc)
        qp.end()
        return img

    def draw_caption(self, painter, vt, text, color=QColor(190, 205, 225)):
        """A line of small text in the panel's top-right corner; each call stacks below the previous one."""
        font = ui_font(8, True)
        w = QFontMetricsF(font).horizontalAdvance(text)
        self._halo_text(painter, vt.rect.right() - w - 12, vt.rect.top() + self._caption_y, text, color, font)
        self._caption_y += 15

    def _paint_overlays(self, painter, p, vt):
        self._caption_y = 38
        if self.show_range_rings:
            self._paint_rings(painter, vt)
        if self.show_cities:
            self._paint_cities(painter, p, vt)
        if self.show_sites:
            self._paint_sites(painter, vt)
        for ov in self.overlays:
            try:
                ov.paint(painter, vt, p, self)
            except Exception as exc:
                print("overlay", type(ov).__name__, exc)
        self._paint_lines(painter, vt)
        self._paint_boxes(painter, vt)

    def _paint_rings(self, painter, vt):
        unit = {"nm": 1.852, "km": 1.0, "mi": 1.609344}[self.distance_units]
        step_u = 25 if vt.km_across / unit < 300 else 50
        step = step_u * unit
        ox, oy = vt.to_screen(0.0, 0.0)
        col = self.colors["rings"]
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, 1.2, Qt.SolidLine))
        nr = int(260 / step_u) if self.distance_units == "nm" else int(480 / step_u)
        for k in range(1, nr + 1):
            rr = k * step * vt.scale
            painter.drawEllipse(QPointF(ox, oy), rr, rr)
        faint = QColor(col)
        faint.setAlpha(max(30, col.alpha() // 2))
        painter.setPen(QPen(faint, 1, Qt.DotLine))
        for a in range(0, 360, 30):
            ar = math.radians(a)
            ex, ey = vt.to_screen(math.sin(ar) * nr * step, math.cos(ar) * nr * step)
            painter.drawLine(QPointF(ox, oy), QPointF(ex, ey))
        painter.setFont(self.font_small)
        label = QColor(col)
        label.setAlpha(255)
        for k in range(1, nr + 1):
            lx, ly = vt.to_screen(0.0, k * step)
            self._halo_text(painter, lx + 3, ly - 3, f"{k * step_u} {self.distance_units}", label)

    def _site_label_rects(self, vt):
        fm = QFontMetricsF(self.font_small)
        out = []
        for sid, x, y, _k in self._visible_sites(vt):
            sx, sy = vt.to_screen(x, y)
            w = fm.horizontalAdvance(sid)
            out.append(QRectF(sx - w / 2 - 3, sy - 7 - fm.height(), w + 6, fm.height() + 14))
        return out

    def _chrome_rects(self, r):
        """Screen areas covered by the panel header and colour bar (city labels keep out of them)."""
        out = [QRectF(r.left(), r.top(), self._header_w + 10, 32)]
        if self.show_legend:
            out.append(QRectF(r.left(), r.bottom() - LEGEND_H - 8, r.width(), LEGEND_H + 8))
        return out

    def _city_layout(self, vt):
        """Label placement for the current view; shared by all panels of the same size."""
        r = vt.rect
        key = (round(r.width()), round(r.height()), self.cx, self.cy, self.scale, self.show_sites,
               self.show_tdwr, self.show_legend, round(self._header_w), self.font_label.key())
        hit = self._city_cache.get(key)
        if hit is not None:
            return hit
        out = []
        xy = getattr(self.maps, "city_xy", None)
        if xy is not None and len(xy):
            x0, y0, x1, y1 = vt.world_bounds()
            inside = np.nonzero((xy[:, 0] > x0) & (xy[:, 0] < x1) & (xy[:, 1] > y0) & (xy[:, 1] < y1))[0]
            km_across = vt.km_across
            # population threshold by zoom level
            minpop = 2_000_000 if km_across > 3000 else 500_000 if km_across > 1500 else 100_000 if km_across > 700 \
                else 25_000 if km_across > 350 else 5_000 if km_across > 150 else 0
            inside = inside[self.maps.city_pop[inside] >= minpop][:400]
            fm = QFontMetricsF(self.font_label)
            sites = self._site_label_rects(vt) if self.show_sites else []
            chrome = self._chrome_rects(r)
            occupied = sites + chrome
            n_fixed = len(sites)
            ox, oy = r.left(), r.top()
            for i in inside:
                sx, sy = vt.to_screen(float(xy[i, 0]), float(xy[i, 1]))
                name = str(self.maps.city_name[i])
                w = fm.horizontalAdvance(name)
                h = fm.height() - 2
                big = self.maps.city_pop[i] >= 100_000
                dot = QRectF(sx - 3, sy - 3, 6, 6)
                if any(dot.intersects(o) for o in chrome):
                    continue
                if not big and any(dot.intersects(o) for o in occupied[:n_fixed]):
                    continue
                # try right, left, below, above the dot (big cities get every option)
                cands = [(sx + 5, sy + 4), (sx - 5 - w, sy + 4)]
                if big:
                    cands += [(sx - w / 2, sy + 4 + h), (sx - w / 2, sy - 6)]
                for tx, ty in cands:
                    box = QRectF(tx - 1, ty - fm.ascent() + 1, w + 2, h)
                    if not any(box.intersects(o) for o in occupied):
                        occupied.append(box.adjusted(-5, -2, 5, 2))
                        out.append((sx - ox, sy - oy, tx - ox, ty - oy, name))
                        break
                if len(out) >= 140:
                    break
        if len(self._city_cache) > 24:
            self._city_cache.clear()
        self._city_cache[key] = out
        return out

    def _paint_cities(self, painter, p, vt):
        layout = self._city_layout(vt)
        if not layout:
            return
        ox, oy = vt.rect.left(), vt.rect.top()
        painter.setFont(self.font_label)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.colors["city_dot"])
        for dx, dy, _tx, _ty, _n in layout:
            painter.drawEllipse(QPointF(ox + dx, oy + dy), 2.0, 2.0)
        col = self.colors["city_text"]
        for _dx, _dy, tx, ty, name in layout:
            self._halo_text(painter, ox + tx, oy + ty, name, col)

    def _paint_sites(self, painter, vt):
        painter.setFont(self.font_small)
        fm = QFontMetricsF(self.font_small)
        c = self.colors
        for sid, x, y, kind in self._visible_sites(vt):
            sx, sy = vt.to_screen(x, y)
            cur = sid == self.site_id
            hov = sid == self._hover_site
            size = 9.0 if (cur or hov) else 7.0
            r = QRectF(sx - size / 2, sy - size / 2, size, size)
            fill = c["site_current"] if cur else QColor(90, 200, 255) if hov else \
                c["site_88d"] if kind == "wsr88d" else c["site_tdwr"]
            painter.setPen(QPen(QColor(0, 0, 0), 2.2))
            painter.setBrush(fill)
            painter.drawRect(r)
            painter.setPen(QPen(QColor(235, 235, 235), 0.8))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)
            col = c["site_current"].lighter(115) if cur else QColor(150, 220, 255) if hov else c["site_text"]
            self._halo_text(painter, sx - fm.horizontalAdvance(sid) / 2, sy - size / 2 - 3, sid, col)

    def set_box(self, box):
        self.box3d = box
        self.update()

    def _tool_cursor(self):
        return Qt.CrossCursor if self.tool in ("box3d", "xsection", "measure", "track") else Qt.ArrowCursor

    def set_tool(self, tool):
        self.tool = tool
        self.setCursor(self._tool_cursor())

    def _draw_box(self, painter, vt, box, live):
        x0, y0, x1, y1 = box
        a = QPointF(*vt.to_screen(min(x0, x1), max(y0, y1)))
        b = QPointF(*vt.to_screen(max(x0, x1), min(y0, y1)))
        r = QRectF(a, b)
        painter.setBrush(QColor(80, 200, 255, 35 if live else 20))
        painter.setPen(QPen(QColor(0, 0, 0, 200), 3.5))
        painter.drawRect(r)
        painter.setPen(QPen(QColor(90, 210, 255), 1.8, Qt.DashLine if live else Qt.SolidLine))
        painter.drawRect(r)
        unit = {"nm": 1.852, "km": 1.0, "mi": 1.609344}[self.distance_units]
        txt = f"3-D  {abs(x1 - x0) / unit:.0f} × {abs(y1 - y0) / unit:.0f} {self.distance_units}"
        self._halo_text(painter, r.left() + 4, r.top() - 5, txt, QColor(120, 220, 255), self.font_label)

    def _paint_boxes(self, painter, vt):
        if self.box3d is not None:
            self._draw_box(painter, vt, self.box3d, False)

    def _draw_line(self, painter, vt, label, x0, y0, x1, y1, color):
        a = QPointF(*vt.to_screen(x0, y0))
        b = QPointF(*vt.to_screen(x1, y1))
        painter.setPen(QPen(QColor(0, 0, 0, 200), 4))
        painter.drawLine(a, b)
        painter.setPen(QPen(color, 2))
        painter.drawLine(a, b)
        painter.setBrush(color)
        painter.drawEllipse(a, 3, 3)
        painter.drawEllipse(b, 3, 3)
        dist = math.hypot(x1 - x0, y1 - y0)
        unit = {"nm": 1.852, "km": 1.0, "mi": 1.609344}[self.distance_units]
        brg = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
        txt = f"{dist / unit:.1f} {self.distance_units} @ {brg:03.0f}°"
        if label == "xsection":
            txt = "A → B  " + txt
        self._halo_text(painter, (a.x() + b.x()) / 2 + 6, (a.y() + b.y()) / 2 - 6, txt, color, self.font_label)

    def _paint_lines(self, painter, vt):
        for line in self.persistent_lines:
            self._draw_line(painter, vt, *line)

    # ------------------------------------------------------------------ storm track
    def _track_update(self):
        t = self.track
        if t is not None:
            maps = self.maps
            t["etas"] = track_maths.track_etas(t["a"], t["b"], self.track_minutes, getattr(maps, "city_xy", None),
                                         getattr(maps, "city_pop", None), getattr(maps, "city_name", None),
                                         self.track_half_width)
        self.trackChanged.emit()
        self.update_cursor()

    def set_track(self, a, b=None):
        """Places the storm at a (km); b defaults to where the storm motion takes it."""
        if b is None:
            b = self.track_default_fn(a[0], a[1], self.track_minutes) if self.track_default_fn else (a[0] + 30, a[1] + 15)
        start = self.track_time_fn() if self.track_time_fn else None
        self.track = {"a": tuple(a), "b": tuple(b), "start": start or datetime.now(timezone.utc), "etas": []}
        self._track_update()

    def set_track_minutes(self, m):
        t = self.track
        if t is not None and self.track_minutes > 0:
            f = m / self.track_minutes
            ax, ay = t["a"]
            t["b"] = (ax + (t["b"][0] - ax) * f, ay + (t["b"][1] - ay) * f)
        self.track_minutes = m
        self._track_update()

    def clear_track(self):
        if self.track is not None:
            self.track = None
            self._track_update()

    def track_motion(self):
        """(km/h, heading degrees) of the track, or None."""
        t = self.track
        if t is None:
            return None
        dx, dy = t["b"][0] - t["a"][0], t["b"][1] - t["a"][1]
        d = math.hypot(dx, dy)
        if d < 0.05:
            return None
        return d / (self.track_minutes / 60.0), (math.degrees(math.atan2(dx, dy)) + 360) % 360

    def _track_handle(self, pos):
        t = self.track
        i = self.panel_at(pos)
        if t is None or i < 0:
            return None
        vt = self.transform(self.panels[i])
        for name in ("b", "a"):
            sx, sy = vt.to_screen(*t[name])
            if math.hypot(sx - pos.x(), sy - pos.y()) < 11:
                return name
        return None

    def _paint_track(self, painter, vt):
        t = self.track
        if t is None:
            return
        yellow = QColor(255, 210, 60)
        ax, ay = vt.to_screen(*t["a"])
        bx, by = vt.to_screen(*t["b"])
        start = t["start"]
        font = self.font_label
        for name, mins, x, y in t["etas"]:
            sx, sy = vt.to_screen(x, y)
            painter.setPen(QPen(yellow, 1.8))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(sx, sy), 5.5, 5.5)
            self._halo_text(painter, sx - 14, sy + 17, fmt.local_hm(start + timedelta(minutes=mins)).split(" ")[0],
                            yellow, font)

        def seg(x0, y0, x1, y1, w):
            painter.setPen(QPen(QColor(0, 0, 0, 220), w + 2.5, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            painter.setPen(QPen(yellow, w, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        seg(ax, ay, bx, by, 2.6)
        dx, dy = bx - ax, by - ay
        ln = math.hypot(dx, dy)
        if ln > 4:
            ux, uy = dx / ln, dy / ln
            head = QPolygonF([QPointF(bx + ux * 3, by + uy * 3), QPointF(bx - ux * 12 - uy * 6, by - uy * 12 + ux * 6),
                              QPointF(bx - ux * 12 + uy * 6, by - uy * 12 - ux * 6)])
            painter.setPen(QPen(QColor(0, 0, 0, 220), 2))
            painter.setBrush(yellow)
            painter.drawPolygon(head)
            step = track_maths.tick_minutes(self.track_minutes)
            m = step
            while m < self.track_minutes:
                f = m / self.track_minutes
                px, py = ax + dx * f, ay + dy * f
                seg(px - uy * 6, py + ux * 6, px + uy * 6, py - ux * 6, 1.8)
                if ln > 80:
                    self._halo_text(painter, px + uy * 12 - 12, py - ux * 12 + 4,
                                    fmt.local_hm(start + timedelta(minutes=m)).split(" ")[0], yellow, font)
                m += step
            self._halo_text(painter, bx + ux * 16 - 12, by + uy * 16 + 4,
                            fmt.local_hm(start + timedelta(minutes=self.track_minutes)).split(" ")[0], yellow, font)
        painter.setPen(QPen(QColor(0, 0, 0), 1.5))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QPointF(ax, ay), 6, 6)
        painter.setBrush(yellow)
        painter.drawEllipse(QPointF(bx, by), 4.5, 4.5)

    def _paint_live_tools(self, painter, vt):
        self._paint_track(painter, vt)
        if self._line is not None:
            tool, x0, y0, x1, y1 = self._line
            self._draw_line(painter, vt, tool, x0, y0, x1, y1, QColor(255, 255, 255))
        if self._boxdrag is not None:
            self._draw_box(painter, vt, self._boxdrag, True)

    def _paint_cursor(self, painter, p, vt):
        if self.cursor_world is None:
            return
        if p.index != self.cursor_panel and not self.link_cursor:
            return
        sx, sy = vt.to_screen(*self.cursor_world)
        if p.index != self.cursor_panel:
            painter.setPen(QPen(self.colors["cursor"], 1.5))
            painter.drawLine(QPointF(sx - 9, sy), QPointF(sx + 9, sy))
            painter.drawLine(QPointF(sx, sy - 9), QPointF(sx, sy + 9))
            painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(sx, sy), 2.5, 2.5)
        if p.readout:
            painter.setFont(self.font_header)
            fm = QFontMetricsF(self.font_header)
            w = fm.horizontalAdvance(p.readout) + 12
            y = p.rect.bottom() - (LEGEND_H + 10 if self.show_legend and p.palette is not None else 6) - fm.height() - 4
            box = QRectF(p.rect.left() + 6, y, w, fm.height() + 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.colors["label_bg"])
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(self.colors["label_text"])
            painter.drawText(box, Qt.AlignCenter, p.readout)

    def _header_width(self, p):
        fm = QFontMetricsF(self.font_header)
        return min(fm.horizontalAdvance(p.header or p.product) + 14, p.rect.width() - 8)

    def _paint_header(self, painter, p):
        painter.setFont(self.font_header)
        fm = QFontMetricsF(self.font_header)
        text = p.header or p.product
        box = QRectF(p.rect.left() + 4, p.rect.top() + 4, self._header_width(p), fm.height() + 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.colors["label_bg"])
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(self.colors["label_text"])
        painter.drawText(box.adjusted(7, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                         fm.elidedText(text, Qt.ElideRight, box.width() - 11))
        if p.message:
            painter.setFont(self.font_label)
            fm2 = QFontMetricsF(self.font_label)
            mb = QRectF(p.rect.center().x() - fm2.horizontalAdvance(p.message) / 2 - 10,
                        p.rect.center().y() - 14, fm2.horizontalAdvance(p.message) + 20, 28)
            painter.setBrush(self.colors["label_bg"])
            painter.drawRoundedRect(mb, 5, 5)
            painter.setPen(self.colors["label_text"])
            painter.drawText(mb, Qt.AlignCenter, p.message)

    def _legend_image(self, ct, width):
        """The whole colour bar (background, gradient, ticks, units) as one cached image."""
        dpr = self.devicePixelRatioF()
        key = (id(ct), int(width), dpr)
        hit = self._legend_cache.get(key)
        if hit is not None and hit[0] is ct:
            return hit[1]
        W, H = int(width), LEGEND_H
        img = QImage(max(1, int(W * dpr)), max(1, int(H * dpr)), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        qp = QPainter(img)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.setRenderHint(QPainter.TextAntialiasing, True)
        qp.setPen(Qt.NoPen)
        qp.setBrush(self.colors["label_bg"])
        qp.drawRoundedRect(QRectF(0, 0, W, H), 4, 4)
        text = self.colors["label_text"]
        qp.setFont(self.font_small)
        fm = QFontMetricsF(self.font_small)
        units = (ct.units or "").strip()
        uw = fm.horizontalAdvance(units) + 8 if units else 0
        bx0, bx1, by, bh = 10.0, W - 10.0 - uw, 5.0, 11.0
        bw = bx1 - bx0
        if units:
            qp.setPen(text)
            qp.drawText(QRectF(bx1 + 4, by - 2, uw, bh + 4), Qt.AlignLeft | Qt.AlignVCenter, units)
        stops = ct.legend_stops(10)
        if ct.labels:
            n = max(1, len(stops))
            cw = bw / n
            for i, (_v, rgba, label) in enumerate(stops):
                qp.setPen(Qt.NoPen)
                qp.setBrush(QColor(*rgba))
                qp.drawRect(QRectF(bx0 + i * cw, by, cw - 1, bh))
                qp.setPen(text)
                qp.drawText(QRectF(bx0 + i * cw, by + bh + 1, cw, 13), Qt.AlignCenter,
                            fm.elidedText((label or "").split(" ")[0], Qt.ElideRight, cw))
        elif bw > 20:
            lo, hi = ct.vmin, ct.vmax
            n = max(20, int(bw))
            vals = lo + (np.arange(n) + 0.5) / n * (hi - lo)
            cols = np.ascontiguousarray(ct.color_at(vals)[None, :, :])
            strip = QImage(cols.data, n, 1, 4 * n, QImage.Format_RGBA8888).copy()
            qp.drawImage(QRectF(bx0, by, bw, bh), strip)
            qp.setPen(text)
            dec = ct.decimals if ct.decimals is not None else (0 if (hi - lo) > 20 else 1 if (hi - lo) > 2 else 2)
            last_x = -1e9
            for v, _rgba, _l in stops:
                if v < lo or v > hi:
                    continue
                tx = bx0 + (v - lo) / (hi - lo) * bw
                if tx - last_x < 28:
                    continue
                last_x = tx
                qp.drawLine(QPointF(tx, by + bh), QPointF(tx, by + bh + 3))
                qp.drawText(QRectF(tx - 20, by + bh + 2, 40, 12), Qt.AlignCenter, f"{v:.{dec}f}")
        qp.end()
        if len(self._legend_cache) > 64:
            self._legend_cache.clear()
        self._legend_cache[key] = (ct, img)
        return img

    def _paint_legend(self, painter, p):
        r = p.rect
        width = r.width() - 12
        if width < 100:
            return
        img = self._legend_image(p.palette, width)
        painter.drawImage(QPointF(round(r.left() + 6), round(r.bottom() - LEGEND_H - 5)), img)

    # ------------------------------------------------------------------ interaction
    def mousePressEvent(self, ev):
        pos = ev.position()
        i = self.panel_at(pos)
        if i >= 0 and i != self.active_panel:
            self.active_panel = i
            self.panelActivated.emit(i)
        if self.host is not None and not self.host.hasFocus():
            self.host.setFocus(Qt.MouseFocusReason)
        if ev.button() == Qt.RightButton:
            self._context_menu(pos, ev.globalPosition().toPoint())
            return
        if ev.button() == Qt.LeftButton:
            x, y, _ = self.world_at(pos)
            if x is None:
                return
            if self.tool == "track":
                h = self._track_handle(pos)
                if h is not None:
                    self._track_drag = h
                elif self.track is not None:
                    # click somewhere else: move the storm there, keep its motion
                    ax, ay = self.track["a"]
                    bx, by = self.track["b"]
                    self.set_track((x, y), (bx + x - ax, by + y - ay))
                    self._track_drag = "a"
                else:
                    self.set_track((x, y))
                    self._track_drag = "b"
                return
            if self.tool == "box3d":
                self._boxdrag = (x, y, x, y)
            elif self.tool in ("xsection", "measure") or ev.modifiers() & Qt.ShiftModifier:
                tool = self.tool if self.tool != "pan" else "measure"
                self._line = (tool, x, y, x, y)
            else:
                self._drag = (pos, self.cx, self.cy)
                self._press_pos = pos
                self.setCursor(Qt.ClosedHandCursor)
        elif ev.button() == Qt.MiddleButton:
            self._drag = (pos, self.cx, self.cy)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag is not None:
            p0, cx0, cy0 = self._drag
            self.cx = cx0 - (pos.x() - p0.x()) / self.scale
            self.cy = cy0 + (pos.y() - p0.y()) / self.scale
            self.viewChanged.emit()
            self.update()
            return
        x, y, i = self.world_at(pos)
        if self._track_drag is not None and x is not None and self.track is not None:
            t = self.track
            if self._track_drag == "b":
                t["b"] = (x, y)
            else:
                ax, ay = t["a"]
                t["b"] = (t["b"][0] + x - ax, t["b"][1] + y - ay)
                t["a"] = (x, y)
            self._track_update()
        if self._boxdrag is not None and x is not None:
            bx0, by0, _, _ = self._boxdrag
            self._boxdrag = (bx0, by0, x, y)
        if self._line is not None and x is not None:
            t, x0, y0, _, _ = self._line
            self._line = (t, x0, y0, x, y)
        if x is not None:
            self.cursor_world = (x, y)
            self.cursor_panel = i
            self.cursorMoved.emit(x, y, i)
            self._hover(ev, x, y)
        self.update_cursor()

    def _hover(self, ev, x, y):
        sid = self.site_at(ev.position()) if self.tool == "pan" and self._drag is None else None
        if sid != self._hover_site:
            self._hover_site = sid
            self.setCursor(Qt.PointingHandCursor if sid else self._tool_cursor())
            self.update()
        if sid:
            s = all_sites()[sid]
            kind = "TDWR" if s.type == "tdwr" else "WSR-88D"
            extra = "  (current radar)" if sid == self.site_id else "\nClick to load this radar"
            QToolTip.showText(ev.globalPosition().toPoint(), f"{sid} – {s.place}, {s.state}  {kind}{extra}",
                              self.host)
            return
        if not self.hover_text:
            QToolTip.hideText()
            return
        tol = 8.0 / self.scale
        for hp in self.hover_providers:
            try:
                txt = hp.hover(x, y, tol)
            except Exception:
                txt = None
            if txt:
                QToolTip.showText(ev.globalPosition().toPoint(), txt, self.host)
                return
        QToolTip.hideText()

    def mouseReleaseEvent(self, ev):
        if self._track_drag is not None:
            self._track_drag = None
            return
        if self._boxdrag is not None:
            x0, y0, x1, y1 = self._boxdrag
            self._boxdrag = None
            if abs(x1 - x0) * self.scale > 6 and abs(y1 - y0) * self.scale > 6:
                self.set_box((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
                self.boxDrawn.emit(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            self.update()
            return
        if self._drag is not None:
            self._drag = None
            self.setCursor(self._tool_cursor())
            self.update()
            pp = getattr(self, "_press_pos", None)
            if pp is not None and (ev.position() - pp).manhattanLength() < 4 and ev.button() == Qt.LeftButton:
                sid = self.site_at(ev.position())
                if sid:
                    self.siteClicked.emit(sid)
            return
        if self._line is not None:
            tool, x0, y0, x1, y1 = self._line
            self._line = None
            if math.hypot(x1 - x0, y1 - y0) > 1.0:
                if tool == "xsection":
                    self.persistent_lines = [l for l in self.persistent_lines if l[0] != "xsection"]
                    self.persistent_lines.append(("xsection", x0, y0, x1, y1, QColor(255, 220, 60)))
                elif tool == "measure":
                    # the measurement stays on the map until the next one (or Esc)
                    self.persistent_lines = [l for l in self.persistent_lines if l[0] != "measure"]
                    self.persistent_lines.append(("measure", x0, y0, x1, y1, QColor(255, 255, 255)))
                self.lineDrawn.emit(tool, x0, y0, x1, y1)
            self.update()

    def mouseDoubleClickEvent(self, ev):
        x, y, _ = self.world_at(ev.position())
        if x is not None:
            self.set_view(x, y, self.scale)

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y() / 120.0
        if steps == 0:
            return
        if self.invert_wheel:
            steps = -steps
        pos = ev.position()
        x, y, i = self.world_at(pos)
        factor = 1.2 ** steps
        new_scale = max(0.02, min(400.0, self.scale * factor))
        if x is not None:
            vt = self.transform(self.panels[i])
            # keep the world point under the cursor fixed
            self.cx = x - (pos.x() - vt.ox) / new_scale
            self.cy = y + (pos.y() - vt.oy) / new_scale
        self.scale = new_scale
        self.viewChanged.emit()
        self._wheel_timer.start()
        self.update()

    def _context_menu(self, pos, gpos):
        i = self.panel_at(QPointF(pos))
        if i >= 0:
            self.active_panel = i
            self.panelMenuRequested.emit(i, gpos)

    def event(self, ev):
        t = ev.type()
        if t == QEvent.Leave:
            self.cursor_world = None
            self.cursorMoved.emit(float("nan"), float("nan"), -1)
            QToolTip.hideText()
            self.update_cursor()
        elif t in (QEvent.DragEnter, QEvent.DragMove, QEvent.DragLeave, QEvent.Drop):
            if self.drop_handler is not None:
                try:
                    self.drop_handler(t, ev)
                except Exception as exc:
                    print("drop error:", exc)
                return True
        return super().event(ev)

    # ------------------------------------------------------------------ readout helpers
    def describe_point(self, x, y):
        """(lat, lon, dist_str, az, beam_height_str) for a world point."""
        lat, lon = self.world_to_latlon(x, y)
        az, s = az_range(x, y)
        unit = {"nm": 1.852, "km": 1.0, "mi": 1.609344}[self.distance_units]
        return lat, lon, float(s) / unit, float(az)

    def clear_lines(self, kind=None):
        self.persistent_lines = [l for l in self.persistent_lines if kind is not None and l[0] != kind]
        self.update()

    def grab_png(self, path):
        img = self.grabFramebuffer()
        return img.save(path)
