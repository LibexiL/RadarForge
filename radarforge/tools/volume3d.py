"""3-D storm view.

The selected box of the radar volume is put on a regular grid (tools/grid3d.py) and drawn on the graphics card by
ray marching: every pixel walks through the grid front to back. Three styles:

  Surfaces   lit surfaces at chosen levels (30 / 50 / 65 dBZ ...): the innermost solid, the outer ones glassy
  Volume     a glowing, see-through cloud: the product's colour table, denser where values stand out
  Realistic  what the storm would look like to the eye: cloud where there's echo (with billowed edges), rain
             shafts below the cloud base, lit by the sun where it was at the scan time, over fields and a sky,
             with cloud shadows on the ground, haze, and lightning

Shadows come from a light volume: for a coarser copy of the grid, the optical depth towards the light (and
straight up, for ambient occlusion) worked out on the graphics card whenever the light or the data changes.
Around the storm: the lowest tilt and the map on the floor, a height scale, the compass, a colour legend, storm
numbers (strongest echo, tops, VIL) and the value under the mouse; a cut plane opens the storm up.
"""
from __future__ import annotations

import math
import random
import time
from collections import OrderedDict

import numpy as np
import OpenGL

OpenGL.ERROR_CHECKING = False      # glGetError after every call is very slow
OpenGL.ERROR_LOGGING = False
from OpenGL import GL  # noqa: E402
from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF, QRunnable, QSize, Qt,  # noqa: E402
                            QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtOpenGL import QOpenGLWindow  # noqa: E402
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,  # noqa: E402
                               QLayout, QLineEdit, QMenu, QScrollArea, QSizePolicy, QSlider, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from ..products import catalog  # noqa: E402
from ..render.fonts import ui_font  # noqa: E402
from . import grid3d, sky  # noqa: E402
from .volume3d_shaders import (BG_FS, LIGHT_FS, PRESENT_FS, RAY_FS, SCENE_FS, SCENE_VS, SKY_FS,  # noqa: E402
                               TRI_VS)


# --------------------------------------------------------------------------- maths
def perspective(fovy, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy) / 2)
    m = np.zeros((4, 4), np.float64)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1
    return m


def look_at(eye, target, up):
    f = target - eye
    f = f / np.linalg.norm(f)
    s = np.cross(f, up)
    s = s / max(np.linalg.norm(s), 1e-9)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float64)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[0, 3], m[1, 3], m[2, 3] = -s @ eye, -u @ eye, f @ eye
    return m


PRESETS = {           # yaw, pitch: where the camera looks from
    "reset": (20.0, 28.0),
    "top": (0.0, 89.0),
    "south": (0.0, 6.0),
    "west": (-90.0, 6.0),
    "north": (180.0, 6.0),
    "east": (90.0, 6.0),
}
FOV = 34.0
GROUND_FOV = 54.0
EYE_MIN_KM = 0.04                     # the camera stays at least this far above the ground


def _ease(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


class Look:
    """What the volume looks like (no rebuild needed to change it)."""

    def __init__(self):
        self.style = "surfaces"        # volume | surfaces | realistic
        self.threshold = None          # storage units (volume style); None: the product's default
        self.levels = None             # storage units (surfaces style)
        self.opacity = 0.6             # 0..1
        self.outline = True            # reflectivity outline around other products
        self.cut = "off"               # off | ns | ew | height
        self.cut_pos = 0.5
        self.shadows = True            # surfaces / volume: shadows and ambient occlusion
        self.floor_map = True          # the lowest tilt and map lines on the floor (realistic: lines only)
        self.labels = True             # towns, heights, storm numbers
        # realistic
        self.sun_az = 235.0            # degrees from north
        self.sun_el = 35.0
        self.cloud_base = 1.3          # km above the radar
        self.visibility = 80.0         # km
        self.rain = True
        self.lightning = True


# --------------------------------------------------------------------------- the OpenGL view
class GLVolume(QOpenGLWindow):
    """The 3-D view: a native OpenGL window (see RadarView for why), embedded with a container."""
    interacted = Signal()

    BG_TOP = (0.115, 0.135, 0.18)
    BG_BOTTOM = (0.02, 0.025, 0.035)
    FLOOR = (0.085, 0.095, 0.115, 1.0)
    UNITS = {"vol": 0, "ctx": 1, "tf": 2, "pal": 3, "light": 4, "noise": 5}

    def __init__(self):
        super().__init__(QOpenGLWindow.NoPartialUpdate)
        from ..render.glview import window_format
        self.setFormat(window_format(depth=True))
        self.grid = None
        self.rule = grid3d.RULES["REF"]
        self.palette = None
        self.units = "dBZ"
        self.disp_units = "dBZ"
        self.scale = 1.0               # storage -> display
        self.offset = 0.0
        self.lo = 0.0                  # storage range the texture is scaled to
        self.hi = 1.0
        self.look = Look()
        self.zscale = 2.0
        self.height_units = "kft"
        self.title = ""
        self.subtitle = ""
        self.busy = ""
        self.message = "Drag a box around a storm on the map (B, or “Select area”) to see it in 3-D."
        self.cities = []               # (x, y, name) relative to the box centre
        self.lines = np.zeros((0, 7), np.float32)
        self.floor_rgba = None
        self.stats = {}
        # camera (target in km, height unexaggerated)
        self.yaw, self.pitch = PRESETS["reset"]
        self.dist = 120.0
        self.target = np.array([0.0, 0.0, 3.0])
        self.fov = FOV
        self._mouse = None
        self._moved = False
        self._last_input = 0.0
        self._cost = None              # seconds per pixel at full detail
        self._idle = QTimer()
        self._idle.setSingleShot(True)
        self._idle.setInterval(220)
        self._idle.timeout.connect(self.update)
        self._anim = None
        self._anim_timer = QTimer()
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._anim_step)
        self.spin = False
        self._spin_timer = QTimer()
        self._spin_timer.setInterval(33)
        self._spin_timer.timeout.connect(self._spin_step)
        self._probe = None             # (QPointF, [lines])
        self._probe_pos = None
        self._probe_timer = QTimer()
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(45)
        self._probe_timer.timeout.connect(self._run_probe)
        self._flash = None             # lightning: dict(pos, strokes, bolt, t0)
        self._flash_timer = QTimer()
        self._flash_timer.setInterval(30)
        self._flash_timer.timeout.connect(self._flash_step)
        self._next_flash = QTimer()
        self._next_flash.setSingleShot(True)
        self._next_flash.timeout.connect(self._start_flash)
        self._active = True
        self._sky_cache = None
        self._gl_ok = False
        self._gl_error = ""
        self._reset_gl_ids()

    # ------------------------------------------------------------------ public
    def set_grid(self, grid, palette, units, title, keep_camera=False):
        first = self.grid is None or self.grid.box != grid.box
        self.grid = grid
        self.palette = palette
        self.units = units
        self.rule = grid3d.RULES.get(grid.pid, grid3d.RULES["REF"])
        self.scale = palette.data_scale(units) or 1.0
        self.offset = palette.offset
        self.disp_units = palette.units or units
        a = (palette.vmin - self.offset) / self.scale
        b = (palette.vmax - self.offset) / self.scale
        self.lo, self.hi = min(a, b), max(a, b)
        if self.hi - self.lo < 1e-6:
            self.hi = self.lo + 1.0
        self.title = title
        self.stats = grid.info.get("stats") or {}
        self._dirty |= {"vol", "luts", "scene"}
        self._lv_state = None
        if first and not keep_camera:
            self.reset_view(animate=False)
        self.busy = ""
        self.message = ""
        self._probe = None
        self._lightning_check()
        self.update()

    def set_floor(self, rgba, lines, cities):
        self.floor_rgba = rgba
        self.lines = lines
        self.cities = cities
        self._dirty.add("scene")
        self.update()

    def set_look(self, look: Look):
        old = self.look
        self.look = look
        self._dirty.add("luts")
        self._sky_cache = None
        self._lightning_check()
        if old.style != look.style and self._probe is not None:
            self._probe = None
        self.update()

    def set_zscale(self, z):
        self.zscale = float(z)
        self._clamp()
        self.update()

    def set_spin(self, on):
        self.spin = bool(on)
        if self.spin and self._active:
            self._spin_timer.start()
        else:
            self._spin_timer.stop()
            self.update()

    def set_active(self, on):
        """Panel shown / hidden: timers only run while it's on screen."""
        self._active = bool(on)
        self.set_spin(self.spin)
        self._lightning_check()

    def set_busy(self, text):
        self.busy = text
        self.update()

    def set_message(self, text):
        self.message = text
        self.update()

    def clear(self, message=""):
        self.grid = None
        self.message = message
        self._lightning_check()
        self.update()

    def realistic(self) -> bool:
        return self.look.style == "realistic" and self.grid is not None and self.grid.pid == "REF"

    # ------------------------------------------------------------------ camera
    def _centre(self):
        c = self.stats.get("centre") if self.stats else None
        return (0.0, 0.0) if c is None else c

    def _view_for(self, preset):
        """(yaw, pitch, dist, target, fov) for a preset."""
        g = self.grid
        yaw, pitch = PRESETS.get(preset, PRESETS["reset"])
        if g is None:
            return yaw, pitch, self.dist, self.target.copy(), FOV
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        top = g.top
        real = self.realistic()
        sun_side = None
        if real and -2.0 < self.look.sun_el < 75.0:
            sun_side = (180.0 - self.look.sun_az) % 360.0       # the camera between the sun and the storm
        if preset == "ground":
            cx, cy = self._centre()
            base_yaw = sun_side + 22.0 if sun_side is not None else 200.0
            r = max(hx, hy) * 0.5 + 8.0                      # a storm chaser's distance
            tz = min(top * 0.3, 4.5)
            ez = EYE_MIN_KM * 1.5
            dist = math.hypot(r, (tz - ez) * self.zscale)
            pitch = math.degrees(math.atan2((ez - tz) * self.zscale, r))
            return base_yaw, pitch, dist, np.array([cx, cy, tz]), GROUND_FOV
        if preset == "reset" and real:
            yaw, pitch = (sun_side + 28.0 if sun_side is not None else 200.0), 9.0
        target = np.array([0.0, 0.0, min(top * 0.35, 6.0)])
        # the whole box in view: its bounding sphere in the field of view
        r = math.sqrt(hx * hx + hy * hy + (top * self.zscale * 0.5) ** 2)
        dist = r / math.sin(math.radians(FOV / 2)) * (0.74 if preset == "reset" else 0.7)
        if real and preset == "reset":
            dist *= 0.82
        return yaw, pitch, dist, target, FOV

    def reset_view(self, preset="reset", animate=True):
        v = self._view_for(preset)
        if animate and self.grid is not None and self.isExposed():
            self._fly(*v)
        else:
            self._anim = None
            self.yaw, self.pitch, self.dist, self.target, self.fov = v[0] % 360.0, v[1], v[2], v[3], v[4]
            self._clamp()
            self.update()

    def _fly(self, yaw, pitch, dist, target, fov, ms=520):
        start = (self.yaw, self.pitch, self.dist, self.target.copy(), self.fov)
        dyaw = ((yaw - start[0] + 180.0) % 360.0) - 180.0
        self._anim = (time.perf_counter(), ms / 1000.0, start, (start[0] + dyaw, pitch, dist, np.asarray(target,
                                                                                                         float), fov))
        self._anim_timer.start()

    def _anim_step(self):
        if self._anim is None:
            self._anim_timer.stop()
            return
        t0, dur, a, b = self._anim
        f = (time.perf_counter() - t0) / dur
        e = _ease(f)
        self.yaw = (a[0] + (b[0] - a[0]) * e) % 360.0
        self.pitch = a[1] + (b[1] - a[1]) * e
        self.dist = math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * e)
        self.target = a[3] + (b[3] - a[3]) * e
        self.fov = a[4] + (b[4] - a[4]) * e
        if f >= 1.0:
            self._anim = None
            self._anim_timer.stop()
        self._clamp()
        self._last_input = time.perf_counter()
        self._idle.start()
        self.update()

    def _spin_step(self):
        if self._mouse is not None or self._anim is not None:
            return
        self.yaw = (self.yaw - 0.25) % 360.0
        self._last_input = time.perf_counter()
        self._idle.start()
        self.update()

    def _clamp(self):
        """Keep the camera above the ground."""
        tz = self.target[2] * self.zscale
        s = (EYE_MIN_KM * self.zscale - tz) / max(self.dist, 1e-6)
        lo = math.degrees(math.asin(min(max(s, -1.0), 1.0)))
        self.pitch = min(89.0, max(self.pitch, lo, -80.0))

    def _eye(self):
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        t = self.target * np.array([1.0, 1.0, self.zscale])
        d = np.array([math.cos(pitch) * math.sin(yaw), -math.cos(pitch) * math.cos(yaw), math.sin(pitch)])
        return t + self.dist * d, t

    def matrices(self, aspect):
        eye, t = self._eye()
        view = look_at(eye, t, np.array([0.0, 0.0, 1.0]))
        far = self.dist * 4 + 600
        if self.realistic():
            far = max(far, 900.0)
        near = max(0.02, min(self.dist * 0.02, max(eye[2] * 0.5, 0.02)))
        proj = perspective(self.fov, aspect, near, far)
        return proj @ view, view, eye

    def _plane(self, eye):
        """The cut plane in world units (normal, distance): the part of the storm on the camera's side is cut
        away. The position is how far the cut reaches in from that side (never all the way, so something always
        stays), and the side only changes when the camera crosses the middle of the box. Not in the realistic
        picture (nothing would show where the cut is)."""
        if self.look.cut == "off" or self.realistic():
            return None
        g = self.grid
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        f = min(max(self.look.cut_pos, 0.0), 1.0) * 0.94
        if self.look.cut == "ns":
            if eye[0] >= 0:                                 # camera to the east: cut the east part away
                return np.array([1.0, 0, 0]), hx - 2 * hx * f
            return np.array([-1.0, 0, 0]), hx - 2 * hx * f
        if self.look.cut == "ew":
            if eye[1] >= 0:
                return np.array([0, 1.0, 0]), hy - 2 * hy * f
            return np.array([0, -1.0, 0]), hy - 2 * hy * f
        if self.look.cut == "height":
            top = g.top * self.zscale
            return np.array([0, 0, 1.0]), top - top * f        # everything above this height goes
        return None

    # ------------------------------------------------------------------ derived look data
    def to_norm(self, v):
        return (np.asarray(v, np.float64) - self.lo) / (self.hi - self.lo)

    def display(self, v):
        return np.asarray(v, np.float64) * self.scale + self.offset

    def thresholds(self):
        """(threshold, full) in storage units for the volume style."""
        r = self.rule
        thr = r.threshold if self.look.threshold is None else self.look.threshold
        return thr, thr + (r.full - r.threshold)

    def levels(self):
        lv = self.look.levels if self.look.levels else list(self.rule.levels)
        return [float(v) for v in lv][:6]

    def level_styles(self):
        """[(level, (r, g, b, a), side)] with the innermost solid and the outer ones glassy."""
        lv = self.levels()
        r = self.rule
        out = []
        groups = {}
        for v in lv:
            side = r.inside_side(v)
            groups.setdefault(side if r.direction == 0 else 0, []).append(v)
        for v in lv:
            side = r.inside_side(v)
            grp = groups[side if r.direction == 0 else 0]
            order = sorted(grp, key=lambda x: float(r.interest(x)))      # outermost first
            rank = order.index(v)
            n = len(order)
            if rank == n - 1:
                a = 1.0
            else:
                a = (0.17 + 0.55 * rank / max(n - 1, 1)) * (0.45 + self.look.opacity)
            rgba = self.palette.color_at(np.array([self.display(v)]))[0] if self.palette is not None else (200,) * 4
            out.append((v, (rgba[0] / 255, rgba[1] / 255, rgba[2] / 255, min(a, 1.0)), side))
        return out

    def _luts(self):
        n = 256
        s = self.lo + (np.arange(n) + 0.5) / n * (self.hi - self.lo)
        pal = self.palette.color_at(self.display(s)).astype(np.float64) if self.palette is not None else \
            np.full((n, 4), 200.0)
        thr, full = self.thresholds()
        if self.look.style != "volume":
            # the cut face shows everything beyond the outermost level
            lv = self.levels()
            outer = min(lv, key=lambda v: float(self.rule.interest(v))) if lv else thr
            thr = outer if self.rule.direction != 0 else abs(outer)
            full = thr + (self.rule.full - self.rule.threshold)
        x = np.asarray(grid3d.Rule(self.rule.direction, thr, full, (), 0).interest(s), np.float64)
        alpha = np.where(x > 0, 0.015 + 0.985 * np.clip(x, 0, 1) ** 2.0, 0.0) * (pal[:, 3] / 255.0)
        tf = np.empty((n, 4), np.uint8)
        tf[:, :3] = pal[:, :3].astype(np.uint8)
        tf[:, 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
        opaque = pal.astype(np.uint8)
        opaque[:, 3] = 255
        return np.ascontiguousarray(tf), np.ascontiguousarray(opaque)

    def sun(self):
        """Sky for the look's sun: cached."""
        k = (round(self.look.sun_az, 2), round(self.look.sun_el, 2), round(self.look.visibility, 1))
        if self._sky_cache is None or self._sky_cache[0] != k:
            self._sky_cache = (k, sky.Sky(sky.sun_vector(self.look.sun_az, self.look.sun_el), self.look.visibility))
        return self._sky_cache[1]

    def _density(self):
        """Volume style: opacity per world unit (with the slider in the middle a 2 km strong core is nearly
        opaque)."""
        return 0.06 * math.exp(4.2 * self.look.opacity) / max(self.zscale, 0.5) ** 0.5

    # ------------------------------------------------------------------ GL setup
    def _reset_gl_ids(self):
        self._tex = {}
        self._fbo = None
        self._fbo_size = (0, 0)
        self._lfbo = None
        self._lv_dims = None
        self._lv_state = None
        self._vbo_n = {}
        self._dirty = {"vol", "luts", "scene"}

    def initializeGL(self):
        # also runs again after the panel is floated / docked (a new context): everything is made again
        self._reset_gl_ids()
        try:
            self.p_bg = self._program(TRI_VS, BG_FS)
            self.p_present = self._program(TRI_VS, PRESENT_FS)
            self.p_scene = self._program(SCENE_VS, SCENE_FS)
            self.p_ray = self._program(TRI_VS, RAY_FS)
            self.p_light = self._program(TRI_VS, LIGHT_FS)
            self.p_sky = self._program(TRI_VS, SKY_FS)
            self._gl_ok = True
        except Exception as exc:          # shown in the view instead of crashing
            self._gl_ok = False
            self._gl_error = str(exc)[:400]
            print("3-D view: shader problem:", exc)
            return
        self.vao_tri = GL.glGenVertexArrays(1)
        self.vao_scene = GL.glGenVertexArrays(1)
        self.vbo_scene = GL.glGenBuffers(1)
        self.u = {}

    def _program(self, vs, fs):
        def comp(src, kind):
            s = GL.glCreateShader(kind)
            GL.glShaderSource(s, src)
            GL.glCompileShader(s)
            if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
                raise RuntimeError(GL.glGetShaderInfoLog(s).decode(errors="replace"))
            return s
        p = GL.glCreateProgram()
        for s in (comp(vs, GL.GL_VERTEX_SHADER), comp(fs, GL.GL_FRAGMENT_SHADER)):
            GL.glAttachShader(p, s)
        GL.glLinkProgram(p)
        if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(p).decode(errors="replace"))
        return p

    def _loc(self, prog, name):
        k = (prog, name)
        if k not in self.u:
            self.u[k] = GL.glGetUniformLocation(prog, name)
        return self.u[k]

    def resizeGL(self, w, h):
        self.update()          # see RadarView.resizeGL

    def _texture(self, name, target, mip=False, wrap=GL.GL_CLAMP_TO_EDGE):
        t = self._tex.get(name)
        if t is None:
            t = GL.glGenTextures(1)
            self._tex[name] = t
        GL.glBindTexture(target, t)
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR if mip else GL.GL_LINEAR),
                     (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR),
                     (GL.GL_TEXTURE_WRAP_S, wrap), (GL.GL_TEXTURE_WRAP_T, wrap)):
            GL.glTexParameteri(target, p, v)
        if target == GL.GL_TEXTURE_3D:
            GL.glTexParameteri(target, GL.GL_TEXTURE_WRAP_R, wrap)
        return t

    def _upload(self):
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        g = self.grid
        if "noise" not in self._tex:
            n = sky.noise_volume(64)
            self._texture("noise", GL.GL_TEXTURE_3D, wrap=GL.GL_REPEAT)
            GL.glTexImage3D(GL.GL_TEXTURE_3D, 0, GL.GL_R8, 64, 64, 64, 0, GL.GL_RED, GL.GL_UNSIGNED_BYTE,
                            np.ascontiguousarray(n))
        if "vol" in self._dirty and g is not None:
            nz, ny, nx = g.values.shape
            norm = self.to_norm(g.values.astype(np.float32)).astype(np.float16)
            self._texture("vol", GL.GL_TEXTURE_3D, mip=True)
            GL.glTexImage3D(GL.GL_TEXTURE_3D, 0, GL.GL_R16F, nx, ny, nz, 0, GL.GL_RED, GL.GL_HALF_FLOAT,
                            np.ascontiguousarray(norm))
            GL.glGenerateMipmap(GL.GL_TEXTURE_3D)       # (the widened field for realistic clouds)
            ctx = g.context if g.context is not None else np.zeros((2, 2, 2), np.float16)
            lo, hi = grid3d.REF_RANGE
            cn = ((ctx.astype(np.float32) - lo) / (hi - lo)).astype(np.float16)
            self._texture("ctx", GL.GL_TEXTURE_3D)
            cz, cy, cx = cn.shape
            GL.glTexImage3D(GL.GL_TEXTURE_3D, 0, GL.GL_R16F, cx, cy, cz, 0, GL.GL_RED, GL.GL_HALF_FLOAT,
                            np.ascontiguousarray(cn))
            self._dirty.discard("vol")
        if "luts" in self._dirty and g is not None:
            tf, pal = self._luts()
            for name, data in (("tf", tf), ("pal", pal)):
                self._texture(name, GL.GL_TEXTURE_1D)
                GL.glTexImage1D(GL.GL_TEXTURE_1D, 0, GL.GL_RGBA8, len(data), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
                                data)
            self._dirty.discard("luts")
        if "scene" in self._dirty:
            if self.floor_rgba is not None:
                h, w = self.floor_rgba.shape[:2]
                self._texture("floor", GL.GL_TEXTURE_2D)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
                                np.ascontiguousarray(self.floor_rgba))
            self._scene_vertices()
            self._dirty.discard("scene")

    def _scene_vertices(self):
        """Floor quad, floor grid, map lines and the box frame in one buffer: (x, y, z, r, g, b, a, u, v)."""
        if self.grid is None:
            self._vbo_n = {}
            return
        x0, y0, x1, y1 = self.grid.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        top = self.grid.top
        fr, fg, fb, fa = self.FLOOR
        quad = np.array([[-hx, -hy, 0, fr, fg, fb, fa, 0, 0], [hx, -hy, 0, fr, fg, fb, fa, 1, 0],
                         [hx, hy, 0, fr, fg, fb, fa, 1, 1], [-hx, -hy, 0, fr, fg, fb, fa, 0, 0],
                         [hx, hy, 0, fr, fg, fb, fa, 1, 1], [-hx, hy, 0, fr, fg, fb, fa, 0, 1]], np.float32)
        lift = 0.012
        segs = []
        step = 10.0 if max(hx, hy) > 15 else 5.0           # faint grid every 10 km
        gx = np.arange(math.ceil(-hx / step) * step, hx + 1e-6, step)
        gy = np.arange(math.ceil(-hy / step) * step, hy + 1e-6, step)
        gc = (0.55, 0.62, 0.75, 0.10)
        for x in gx:
            segs += [[x, -hy, lift, *gc], [x, hy, lift, *gc]]
        for y in gy:
            segs += [[-hx, y, lift, *gc], [hx, y, lift, *gc]]
        floor_lines = np.array(segs, np.float32).reshape(-1, 7) if segs else np.zeros((0, 7), np.float32)
        maps = self.lines.copy() if len(self.lines) else np.zeros((0, 7), np.float32)
        if len(maps):
            maps[:, 2] = lift * 1.5
        ec = (0.75, 0.82, 0.95, 0.55)
        bottom = np.array([[-hx, -hy, lift, *ec], [hx, -hy, lift, *ec], [hx, -hy, lift, *ec], [hx, hy, lift, *ec],
                           [hx, hy, lift, *ec], [-hx, hy, lift, *ec], [-hx, hy, lift, *ec], [-hx, -hy, lift, *ec]],
                          np.float32)
        tc = (0.75, 0.82, 0.95, 0.22)
        upper = []
        for cx, cy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
            upper += [[cx, cy, 0, *tc], [cx, cy, top, *tc]]
        for (ax, ay), (bx, by) in (((-hx, -hy), (hx, -hy)), ((hx, -hy), (hx, hy)), ((hx, hy), (-hx, hy)),
                                   ((-hx, hy), (-hx, -hy))):
            upper += [[ax, ay, top, *tc], [bx, by, top, *tc]]
        upper = np.array(upper, np.float32)

        def uv(a):
            out = np.zeros((len(a), 9), np.float32)
            out[:, :7] = a
            return out
        parts = [("quad", quad), ("floor_grid", uv(np.concatenate([floor_lines, bottom]))), ("maps", uv(maps)),
                 ("frame", uv(upper))]
        data = np.concatenate([p for _n, p in parts]).astype(np.float32)
        self._vbo_n = {}
        start = 0
        for name, p in parts:
            self._vbo_n[name] = (start, len(p))
            start += len(p)
        GL.glBindVertexArray(self.vao_scene)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo_scene)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
        for loc, size, off in ((0, 3, 0), (1, 4, 12), (2, 2, 28)):
            GL.glEnableVertexAttribArray(loc)
            GL.glVertexAttribPointer(loc, size, GL.GL_FLOAT, False, 36, GL.ctypes.c_void_p(off))
        GL.glBindVertexArray(0)

    def _ensure_fbo(self, w, h):
        if self._fbo is not None and self._fbo_size == (w, h):
            return
        if self._fbo is not None:
            GL.glDeleteFramebuffers(1, [self._fbo[0]])
            GL.glDeleteTextures([self._fbo[1]])
            GL.glDeleteRenderbuffers(1, [self._fbo[2]])
        fbo = GL.glGenFramebuffers(1)
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
        rb = GL.glGenRenderbuffers(1)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rb)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH_COMPONENT24, w, h)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, rb)
        self._fbo = (fbo, tex, rb)
        self._fbo_size = (w, h)

    # ------------------------------------------------------------------ shared uniforms
    def _mode(self):
        if self.realistic():
            return 2
        return 1 if self.look.style != "volume" else 0

    def _box_world(self):
        x0, y0, x1, y1 = self.grid.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        return hx, hy, self.grid.top * self.zscale

    def _common(self, p, light_tex=True):
        """Uniforms and textures every volume program shares."""
        g = self.grid
        L = lambda name: self._loc(p, name)        # noqa: E731
        hx, hy, top = self._box_world()
        GL.glUniform3f(L("u_bmin"), -hx, -hy, 0.0)
        GL.glUniform3f(L("u_bmax"), hx, hy, top)
        nz, ny, nx = g.values.shape
        GL.glUniform3f(L("u_size"), float(nx), float(ny), float(nz))
        GL.glUniform1f(L("u_zscale"), self.zscale)
        mode = self._mode()
        GL.glUniform1i(L("u_mode"), mode)
        styles = self.level_styles() if mode == 1 else []
        lev = np.zeros(6, np.float32)
        col = np.zeros((6, 4), np.float32)
        side = np.ones(6, np.float32)
        for i, (v, c, sd) in enumerate(styles[:6]):
            lev[i] = self.to_norm(v)
            col[i] = c
            side[i] = sd
        GL.glUniform1i(L("u_nlev"), len(styles[:6]))
        GL.glUniform1fv(L("u_lev"), 6, lev)
        GL.glUniform4fv(L("u_levcol"), 6, col)
        GL.glUniform1fv(L("u_levside"), 6, side)
        GL.glUniform1f(L("u_density"), self._density())
        GL.glUniform1f(L("u_sdens"), 0.9)
        look = self.look
        GL.glUniform1f(L("u_lo"), self.lo)
        GL.glUniform1f(L("u_hi"), self.hi)
        GL.glUniform1f(L("u_base"), float(look.cloud_base))
        GL.glUniform1f(L("u_cloud_dbz"), 13.0)
        GL.glUniform1f(L("u_rain_on"), 1.0 if look.rain else 0.0)
        res = float(g.info.get("res_km") or (2 * hx / max(nx - 1, 1)))
        GL.glUniform1f(L("u_blur_lod"), float(np.clip(math.log2(0.65 / max(res, 0.05)), 0.4, 2.5)))
        GL.glUniform1f(L("u_sig_cloud"), 6.0)
        GL.glUniform1f(L("u_sig_rain"), 2.4)
        for name, target in (("vol", GL.GL_TEXTURE_3D), ("ctx", GL.GL_TEXTURE_3D), ("tf", GL.GL_TEXTURE_1D),
                             ("pal", GL.GL_TEXTURE_1D), ("light", GL.GL_TEXTURE_3D), ("noise", GL.GL_TEXTURE_3D)):
            unit = self.UNITS[name]
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            tex = self._tex.get(name, 0) if (name != "light" or light_tex) else 0
            GL.glBindTexture(target, tex)
            GL.glUniform1i(L("u_" + ("lightv" if name == "light" else name)), unit)
        GL.glActiveTexture(GL.GL_TEXTURE0)

    def _sky_uniforms(self, p):
        L = lambda name: self._loc(p, name)        # noqa: E731
        s = self.sun()
        sunp = s.sun
        sunw = np.array([sunp[0], sunp[1], sunp[2] * self.zscale])
        sunw /= max(np.linalg.norm(sunw), 1e-9)
        GL.glUniform3f(L("u_sunp"), *sunp)
        GL.glUniform3f(L("u_sunw"), *sunw)
        GL.glUniform3f(L("u_sun_col"), *s.sun_col)
        GL.glUniform3f(L("u_sky_amb"), *s.sky_amb)
        GL.glUniform3f(L("u_tauR"), *s.tau_r)
        GL.glUniform1f(L("u_tauM"), s.tau_m)
        GL.glUniform1f(L("u_E0"), sky.E0)
        GL.glUniform1f(L("u_vis"), float(self.look.visibility))
        GL.glUniform1f(L("u_exposure"), s.exposure)
        f = self._flash_uniform()
        GL.glUniform4f(L("u_flash"), *f)
        return f

    def _flash_uniform(self):
        fl = self._flash
        if fl is None:
            return (0.0, 0.0, 0.0, 0.0)
        x, y, z = fl["pos"]
        # as bright on screen by night as by day (by day it hardly shows, as in life)
        k = 1.3 / self.sun().exposure if self.sun().night else 0.5
        return (float(x), float(y), float(z) * self.zscale, float(fl["now"]) * k)

    # ------------------------------------------------------------------ the light volume
    def _light_dir(self, view):
        if self._mode() == 2:
            s = self.sun().sun
            d = np.array([s[0], s[1], max(s[2], 0.02) * self.zscale])
            return d / np.linalg.norm(d)
        # analysis styles: light from over the viewer's left shoulder
        inv_view = np.linalg.inv(view)
        right, up, back = inv_view[:3, 0], inv_view[:3, 1], inv_view[:3, 2]
        light = -0.45 * right + 0.6 * up + 0.65 * back
        light[2] = max(light[2], 0.35)
        return light / np.linalg.norm(light)

    def _need_light(self):
        return self.grid is not None and (self._mode() == 2 or self.look.shadows)

    def _update_light(self, ldir):
        """(Re)make the light volume when the light, the data or the look changed."""
        g = self.grid
        nz, ny, nx = g.values.shape
        dims = (max(4, min(160, (nx + 1) // 2)), max(4, min(160, (ny + 1) // 2)), max(4, min(96, (nz + 1) // 2)))
        look = self.look
        mode = self._mode()
        key = (id(g), mode, dims, round(self.zscale, 3), round(look.opacity, 3), tuple(self.levels()),
               self.thresholds(), round(look.cloud_base, 3), look.rain, id(self.palette))
        st = self._lv_state
        if st is not None and st[0] == key and float(np.dot(st[1], ldir)) > (0.9999 if mode == 2 else 0.9976):
            return
        if self._lv_dims != dims:
            self._texture("light", GL.GL_TEXTURE_3D)
            GL.glTexImage3D(GL.GL_TEXTURE_3D, 0, GL.GL_RG16F, dims[0], dims[1], dims[2], 0, GL.GL_RG,
                            GL.GL_HALF_FLOAT, None)
            self._lv_dims = dims
        if self._lfbo is None:
            self._lfbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._lfbo)
        GL.glViewport(0, 0, dims[0], dims[1])
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        p = self.p_light
        GL.glUseProgram(p)
        self._common(p, light_tex=False)
        hx, hy, top = self._box_world()
        cell = min(2 * hx / max(dims[0] - 1, 1), 2 * hy / max(dims[1] - 1, 1), top / max(dims[2] - 1, 1))
        GL.glUniform3f(self._loc(p, "u_ldir"), *ldir)
        GL.glUniform1f(self._loc(p, "u_lstep"), max(cell, 1e-3))
        GL.glBindVertexArray(self.vao_tri)
        loc_layer = self._loc(p, "u_layer_f")
        tex = self._tex["light"]
        for z in range(dims[2]):
            GL.glFramebufferTextureLayer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, tex, 0, z)
            GL.glUniform1f(loc_layer, (z + 0.5) / dims[2])
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        self._lv_state = (key, np.array(ldir, float))

    # ------------------------------------------------------------------ drawing
    def _detail_scale(self, pixels):
        interacting = (time.perf_counter() - self._last_input) < 0.2
        if self._cost is None:
            return 0.6 if interacting else 1.0
        target = 0.035 if interacting else 0.45
        s = math.sqrt(target / max(self._cost * pixels, 1e-9))
        return max(0.3, min(1.0, s))

    def paintGL(self):
        dpr = float(self.devicePixelRatio())
        w, h = max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))
        if not self._gl_ok:
            GL.glClearColor(*self.BG_BOTTOM, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            self._overlay(None)
            return
        self._upload()
        t0 = time.perf_counter()
        s = self._detail_scale(w * h) if self.grid is not None else 1.0
        mvp = None
        if self.grid is not None and self._vbo_n and self._need_light():
            # (analysis styles while dragging: the shadows catch up when the mouse stops, keeping it smooth)
            busy = (time.perf_counter() - self._last_input) < 0.2 and self._lv_state is not None and \
                self._mode() != 2 and self._lv_state[0][0] == id(self.grid)
            if not busy:
                _m, view, _e = self.matrices(w / max(h, 1))
                self._update_light(self._light_dir(view))
        if s < 0.97:
            sw, sh = max(1, int(w * s)), max(1, int(h * s))
            self._ensure_fbo(sw, sh)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo[0])
            mvp = self._draw_scene(sw, sh)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
            GL.glViewport(0, 0, w, h)
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_BLEND)
            GL.glUseProgram(self.p_present)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._fbo[1])
            GL.glUniform1i(self._loc(self.p_present, "u_img"), 0)
            GL.glBindVertexArray(self.vao_tri)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        else:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
            mvp = self._draw_scene(w, h)
        GL.glFinish()
        dt = time.perf_counter() - t0
        if self.grid is not None:
            px = max(1.0, w * h * s * s)
            c = dt / px
            self._cost = c if self._cost is None else self._cost * 0.6 + c * 0.4
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        self._overlay(mvp)
        if s < 0.97 and not self._idle.isActive():
            self._idle.start()           # then once more at full detail

    def _draw_scene(self, w, h):
        GL.glViewport(0, 0, w, h)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        g = self.grid
        real = self.realistic()
        if g is None or not self._vbo_n:
            GL.glUseProgram(self.p_bg)
            GL.glUniform3f(self._loc(self.p_bg, "u_top"), *self.BG_TOP)
            GL.glUniform3f(self._loc(self.p_bg, "u_bottom"), *self.BG_BOTTOM)
            GL.glBindVertexArray(self.vao_tri)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
            return None
        mvp, view, eye = self.matrices(w / max(h, 1))
        inv = np.linalg.inv(mvp).astype(np.float32)
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        if real:
            # sky, sun and the ground (with the storm's shadow)
            p = self.p_sky
            GL.glUseProgram(p)
            self._common(p)
            f = self._sky_uniforms(p)
            GL.glUniformMatrix4fv(self._loc(p, "u_inv"), 1, GL.GL_TRUE, inv)
            GL.glUniform1i(self._loc(p, "u_has_grid"), 1)
            GL.glUniform1f(self._loc(p, "u_flash_sky"), f[3] * 0.012)
            GL.glBindVertexArray(self.vao_tri)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        else:
            GL.glUseProgram(self.p_bg)
            GL.glUniform3f(self._loc(self.p_bg, "u_top"), *self.BG_TOP)
            GL.glUniform3f(self._loc(self.p_bg, "u_bottom"), *self.BG_BOTTOM)
            GL.glBindVertexArray(self.vao_tri)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
        # floor, floor grid, map lines
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glUseProgram(self.p_scene)
        GL.glUniformMatrix4fv(self._loc(self.p_scene, "u_mvp"), 1, GL.GL_TRUE, mvp.astype(np.float32))
        GL.glUniform1f(self._loc(self.p_scene, "u_zscale"), self.zscale)
        GL.glUniform1i(self._loc(self.p_scene, "u_tex"), 0)
        GL.glUniform1f(self._loc(self.p_scene, "u_alpha"), 1.0)
        GL.glBindVertexArray(self.vao_scene)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        if not real:
            has_floor = "floor" in self._tex and self.look.floor_map
            if has_floor:
                GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex["floor"])
            GL.glUniform1i(self._loc(self.p_scene, "u_textured"), 1 if has_floor else 0)
            a, n = self._vbo_n["quad"]
            GL.glDrawArrays(GL.GL_TRIANGLES, a, n)
        GL.glUniform1i(self._loc(self.p_scene, "u_textured"), 0)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        if not real:
            a, n = self._vbo_n["floor_grid"]
            GL.glDrawArrays(GL.GL_LINES, a, n)
        if self.look.floor_map:
            GL.glUniform1f(self._loc(self.p_scene, "u_alpha"), 0.45 if real else 1.0)
            a, n = self._vbo_n["maps"]
            GL.glDrawArrays(GL.GL_LINES, a, n)
            GL.glUniform1f(self._loc(self.p_scene, "u_alpha"), 1.0)
        # the volume
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        self._draw_ray(mvp, view, eye, hx, hy, g, inv)
        if not real:
            # the box's upright edges and top over it, faintly
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glUseProgram(self.p_scene)
            GL.glBindVertexArray(self.vao_scene)
            a, n = self._vbo_n["frame"]
            GL.glDrawArrays(GL.GL_LINES, a, n)
        return mvp

    def _draw_ray(self, mvp, view, eye, hx, hy, g, inv):
        p = self.p_ray
        L = lambda name: self._loc(p, name)        # noqa: E731
        GL.glUseProgram(p)
        self._common(p)
        GL.glUniformMatrix4fv(L("u_inv"), 1, GL.GL_TRUE, inv)
        top = g.top * self.zscale
        nz, ny, nx = g.values.shape
        look = self.look
        real = self._mode() == 2
        ctx_on = look.outline and g.context is not None and not real
        GL.glUniform1i(L("u_ctx_on"), 1 if ctx_on else 0)
        lo, hi = grid3d.REF_RANGE
        GL.glUniform1f(L("u_ctx_lev"), (30.0 - lo) / (hi - lo))
        GL.glUniform4f(L("u_ctx_col"), 0.82, 0.86, 0.95, 0.07)
        plane = self._plane(eye)
        GL.glUniform1i(L("u_cut"), 1 if plane is not None else 0)
        if plane is not None:
            GL.glUniform4f(L("u_plane"), *plane[0], plane[1])
        # samples about half a grid cell apart (in the exaggerated scene), more for big views
        cell = min(2 * hx / max(nx - 1, 1), 2 * hy / max(ny - 1, 1), top / max(nz - 1, 1))
        diag = math.sqrt((2 * hx) ** 2 + (2 * hy) ** 2 + top ** 2)
        step = max(cell * (0.6 if real else 0.5), diag / (1600 if real else 1400))
        GL.glUniform1f(L("u_step"), step)
        GL.glUniform1i(L("u_steps"), int(min(6000 if real else 4000, diag / step + 4)))
        GL.glUniform1f(L("u_cell"), cell)
        GL.glUniform1i(L("u_shadow"), 1 if (look.shadows and not real) else 0)
        GL.glUniform3f(L("u_light"), *self._light_dir(view))
        if real:
            self._sky_uniforms(p)
        GL.glBindVertexArray(self.vao_tri)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

    # ------------------------------------------------------------------ text and labels on top
    def _project(self, mvp, pts):
        """World points (n, 3; z unexaggerated) -> logical screen points, and which are in front."""
        P = np.c_[pts[:, 0], pts[:, 1], pts[:, 2] * self.zscale, np.ones(len(pts))]
        c = P @ mvp.T
        ok = c[:, 3] > 1e-6
        w = np.where(ok, c[:, 3], 1.0)
        x = (c[:, 0] / w * 0.5 + 0.5) * self.width()
        y = (1 - (c[:, 1] / w * 0.5 + 0.5)) * self.height()
        return np.c_[x, y], ok

    def fmt_height(self, km):
        if self.height_units == "km":
            return f"{km:.1f} km"
        return f"{km / 0.3048:.0f} kft"

    def fmt_value(self, v):
        d = float(self.display(v))
        txt = f"{d:.0f}" if abs(d) >= 10 else f"{d:.2g}"
        return f"{txt} {self.disp_units}"

    def _overlay(self, mvp):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        small = ui_font(8.5)
        normal = ui_font(9.5)
        if not self._gl_ok:
            p.setPen(QColor(255, 170, 120))
            p.setFont(normal)
            p.drawText(QRectF(20, 20, W - 40, H - 40), Qt.TextWordWrap,
                       "The 3-D view couldn't start on this graphics driver:\n" + self._gl_error)
            p.end()
            return
        g = self.grid
        real = self.realistic()
        if g is not None and mvp is not None:
            if self._flash is not None and self._flash.get("bolt") is not None:
                self._bolt(p, mvp)
            if self.look.labels:
                self._labels(p, mvp, g, small)
            if not real:
                self._legend(p, W, small)
            self._compass(p, mvp, W, H, small)
        # title, subtitle, storm numbers (on a soft shadow: readable over a bright sky too)
        y = 22
        for text, font, col in ((self.title, ui_font(10.5, True), QColor(236, 240, 248)),
                                (self.subtitle, normal, QColor(190, 200, 220)),
                                (self._stats_text() if (g is not None and self.look.labels) else "", small,
                                 QColor(200, 210, 228))):
            if text:
                self._text(p, QPointF(14, y), text, font, col)
                y += 18
        if self._probe is not None and g is not None:
            self._draw_probe(p, W, H)
        if self.message:
            p.setFont(ui_font(11))
            p.setPen(QColor(200, 210, 228))
            p.drawText(QRectF(30, 0, W - 60, H), Qt.AlignCenter | Qt.TextWordWrap, self.message)
        if self.busy:
            p.setFont(normal)
            fm = QFontMetricsF(normal)
            tw = fm.horizontalAdvance(self.busy) + 28
            r = QRectF((W - tw) / 2, 12, tw, 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 26, 38, 215))
            p.drawRoundedRect(r, 13, 13)
            p.setPen(QColor(220, 230, 250))
            p.drawText(r, Qt.AlignCenter, self.busy)
        if g is not None:
            self._text(p, QPointF(14, H - 10), "Drag: rotate   Right-drag: move   Wheel: zoom   Double-click: "
                       "centre here   Hover: values", small, QColor(150, 162, 185))
        p.end()

    @staticmethod
    def _text(p, at, text, font, col):
        """Text with a soft dark shadow (readable over a bright sky as well as the dark background)."""
        p.setFont(font)
        p.setPen(QColor(0, 0, 0, 150))
        p.drawText(at + QPointF(1, 1), text)
        p.setPen(QColor(0, 0, 0, 70))
        p.drawText(at + QPointF(-0.6, 0.6), text)
        p.setPen(col)
        p.drawText(at, text)

    def _stats_text(self):
        st = self.stats or {}
        parts = []
        if "max" in st and self.rule.direction > 0:
            v, _x, _y, z = st["max"]
            parts.append(f"Max {self.fmt_value(v)} at {self.fmt_height(z)}")
        elif "min" in st and self.rule.direction < 0:
            v, _x, _y, z = st["min"]
            parts.append(f"Min {self.fmt_value(v)} at {self.fmt_height(z)}")
        elif "max" in st and "min" in st:
            parts.append(f"Out {self.fmt_value(st['max'][0])}, in {self.fmt_value(st['min'][0])}")
        tops = st.get("tops") or {}
        if tops:
            t = ", ".join(f"{self.fmt_height(z)} ({thr:.0f})" for thr, z in sorted(tops.items()) if thr != 30.0)
            parts.append(f"Tops {t}")
        if st.get("vil"):
            parts.append(f"VIL {st['vil']:.0f} kg/m²")
        return "   ·   ".join(parts)

    def _labels(self, p, mvp, g, font):
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        # height scale on the box corner that is leftmost on screen (not in the realistic picture: no box there)
        corners = np.array([[-hx, -hy, 0], [hx, -hy, 0], [hx, hy, 0], [-hx, hy, 0]], np.float64)
        sc, ok = self._project(mvp, corners)
        if ok.any() and not self.realistic():
            i = int(np.argmin(np.where(ok, sc[:, 0], 1e9)))
            cx, cy = corners[i, :2]
            kft = self.height_units != "km"
            unit_km = 0.3048 if kft else 1.0
            span = g.top / unit_km
            step = 10 if kft else 2
            if span / step > 9:
                step *= 2
            vals = np.arange(step, span + 1e-6, step)
            pts = np.c_[np.full(len(vals), cx), np.full(len(vals), cy), vals * unit_km]
            out = np.array([cx, cy]) / max(math.hypot(cx, cy), 1e-6)      # labels outside the box
            pts2 = pts.copy()
            pts2[:, 0] += out[0] * max(hx, hy) * 0.04
            pts2[:, 1] += out[1] * max(hx, hy) * 0.04
            a, oka = self._project(mvp, pts)
            b, okb = self._project(mvp, pts2)
            p.setFont(font)
            for k, v in enumerate(vals):
                if not (oka[k] and okb[k]):
                    continue
                p.setPen(QPen(QColor(190, 205, 230, 170), 1.2))
                p.drawLine(QPointF(*a[k]), QPointF(*b[k]))
                d = b[k] - a[k]
                n = d / max(np.linalg.norm(d), 1e-6)
                txt = f"{v:g} {'kft' if kft else 'km'}"
                fm = QFontMetricsF(font)
                tw = fm.horizontalAdvance(txt)
                at = b[k] + n * 4
                tx = at[0] if n[0] >= 0 else at[0] - tw
                self._text(p, QPointF(tx, at[1] + 4), txt, font, QColor(205, 216, 238))
        # towns on the floor (biggest first; one that would overlap a label already drawn is left out)
        if self.cities:
            pts = np.array([[x, y, 0.0] for x, y, _n in self.cities], np.float64)
            sc, ok = self._project(mvp, pts)
            p.setFont(font)
            fm = QFontMetricsF(font)
            taken = []
            for k, (_x, _y, name) in enumerate(self.cities):
                if not ok[k]:
                    continue
                q = QPointF(*sc[k])
                box = QRectF(q.x() - 3, q.y() - 9, fm.horizontalAdvance(name) + 10, 16)
                if any(box.intersects(b) for b in taken):
                    continue
                taken.append(box)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 210))
                p.drawEllipse(q, 2.2, 2.2)
                self._text(p, q + QPointF(5, 4), name, font, QColor(232, 236, 244))
        # the strongest echo
        st = self.stats or {}
        key = "max" if self.rule.direction >= 0 else "min"
        if key in st:
            v, x, y, z = st[key]
            s2, ok2 = self._project(mvp, np.array([[x, y, z]]))
            if ok2[0]:
                q = QPointF(*s2[0])
                p.setPen(QPen(QColor(255, 255, 255, 230), 1.4))
                p.setBrush(QColor(255, 80, 60, 200))
                d = 5.0
                path = QPainterPath()
                path.moveTo(q + QPointF(0, -d))
                path.lineTo(q + QPointF(d, 0))
                path.lineTo(q + QPointF(0, d))
                path.lineTo(q + QPointF(-d, 0))
                path.closeSubpath()
                p.drawPath(path)
                self._text(p, q + QPointF(8, 4), self.fmt_value(v), font, QColor(255, 236, 230))

    def _legend(self, p, W, font):
        if self.palette is None:
            return
        fm = QFontMetricsF(font)
        p.setFont(font)
        if self.look.style != "volume":
            styles = self.level_styles()
            items = sorted(styles, key=lambda s: -s[0])
            x = W - 14
            y = 18
            for v, c, _sd in items:
                txt = f"{self.display(v):g} {self.disp_units}"
                tw = fm.horizontalAdvance(txt)
                r = QRectF(x - tw - 22, y, 14, 14)
                p.setPen(QPen(QColor(255, 255, 255, 90), 1))
                p.setBrush(QColor(int(c[0] * 255), int(c[1] * 255), int(c[2] * 255), int(90 + 165 * c[3])))
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QColor(220, 228, 242))
                p.drawText(QPointF(x - tw, y + 11.5), txt)
                y += 20
            return
        # volume: the colour table from the threshold up, with its density
        thr, full = self.thresholds()
        r = self.rule
        if r.direction > 0:
            a, b = thr, max(self.hi, full)
        elif r.direction < 0:
            a, b = min(self.lo, full), thr
        else:
            a, b = -max(abs(self.lo), abs(self.hi)), max(abs(self.lo), abs(self.hi))
        h = 150
        x = W - 46
        y = 20
        grad = QLinearGradient(0, y + h, 0, y)
        n = 24
        for i in range(n + 1):
            s = a + (b - a) * i / n
            c = self.palette.color_at(np.array([self.display(s)]))[0]
            vis = float(r.interest(s, thr)) > 0
            grad.setColorAt(i / n, QColor(int(c[0]), int(c[1]), int(c[2]), 255 if vis else 40))
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(x, y, 12, h), 3, 3)
        p.setPen(QColor(220, 228, 242))
        for s in (a, (a + b) / 2, b):
            yy = y + h - (s - a) / max(b - a, 1e-9) * h
            txt = f"{self.display(s):.3g}"
            p.drawText(QPointF(x - fm.horizontalAdvance(txt) - 6, yy + 4), txt)
        p.drawText(QPointF(x - 4, y + h + 16), self.disp_units)

    def _compass(self, p, mvp, W, H, font):
        c = np.array([[0.0, 0.0, 0.0], [0.0, 10.0, 0.0]])
        sc, ok = self._project(mvp, c)
        if not ok.all():
            return
        d = sc[1] - sc[0]
        if np.linalg.norm(d) < 1e-6:
            return
        d = d / np.linalg.norm(d)
        cx, cy, r = W - 34, H - 40, 17
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
        p.setBrush(QColor(15, 20, 30, 160))
        p.drawEllipse(QPointF(cx, cy), r, r)
        tip = QPointF(cx + d[0] * r * 0.8, cy + d[1] * r * 0.8)
        tail = QPointF(cx - d[0] * r * 0.55, cy - d[1] * r * 0.55)
        side = QPointF(-d[1] * r * 0.28, d[0] * r * 0.28)
        path = QPainterPath(tip)
        path.lineTo(QPointF(cx, cy) + side)
        path.lineTo(tail)
        path.lineTo(QPointF(cx, cy) - side)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.fillPath(path, QColor(240, 90, 80))
        p.setFont(ui_font(8, True))
        p.setPen(QColor(240, 240, 245))
        n = QPointF(cx + d[0] * (r + 9), cy + d[1] * (r + 9))
        p.drawText(QRectF(n.x() - 8, n.y() - 8, 16, 16), Qt.AlignCenter, "N")

    # ------------------------------------------------------------------ the value under the mouse
    def _ray(self, pos):
        """World ray (origin, direction) through a point of the window."""
        mvp, _v, _eye = self.matrices(self.width() / max(self.height(), 1))
        inv = np.linalg.inv(mvp)
        x = pos.x() / max(self.width(), 1) * 2 - 1
        y = 1 - pos.y() / max(self.height(), 1) * 2
        a = inv @ np.array([x, y, -1.0, 1.0])
        b = inv @ np.array([x, y, 1.0, 1.0])
        a, b = a[:3] / a[3], b[:3] / b[3]
        d = b - a
        return a, d / max(np.linalg.norm(d), 1e-12)

    def probe(self, pos):
        """[lines] describing what's under the mouse, or None: the first thing the eye meets there, and the
        strongest value behind it."""
        g = self.grid
        if g is None:
            return None
        ro, rd = self._ray(pos)
        hx, hy, top = self._box_world()
        bmin, bmax = np.array([-hx, -hy, 0.0]), np.array([hx, hy, top])
        rds = np.where(np.abs(rd) < 1e-9, 1e-9, rd)
        t0s, t1s = (bmin - ro) / rds, (bmax - ro) / rds
        t0 = max(np.minimum(t0s, t1s).max(), 0.0)
        t1 = np.maximum(t0s, t1s).min()
        if t1 <= t0:
            return None
        n = int(min(900, max(60, (t1 - t0) / max(self._cell_world() * 0.5, 1e-3))))
        ts = np.linspace(t0, t1, n)
        pts = ro[None, :] + rd[None, :] * ts[:, None]
        nz, ny, nx = g.values.shape
        ix = np.clip(np.round((pts[:, 0] + hx) / (2 * hx) * (nx - 1)).astype(int), 0, nx - 1)
        iy = np.clip(np.round((pts[:, 1] + hy) / (2 * hy) * (ny - 1)).astype(int), 0, ny - 1)
        zk = pts[:, 2] / self.zscale
        iz = np.clip(np.round(zk / g.top * (nz - 1)).astype(int), 0, nz - 1)
        v = g.values[iz, iy, ix].astype(np.float64)
        keep = np.ones(n, bool)
        if self.look.cut != "off" and not self.realistic():
            plane = self._plane(self._eye()[0])
            if plane is not None:
                keep = pts @ plane[0] <= plane[1]
        r = self.rule
        if self.realistic():
            seen = (v >= 13.0) & ((zk >= self.look.cloud_base) | (v >= 30.0))
        elif self.look.style == "volume":
            seen = np.asarray(r.interest(v, self.thresholds()[0])) > 0.2
        else:
            lv = self.levels()
            outer = min(lv, key=lambda q: float(r.interest(q))) if lv else r.threshold
            seen = np.asarray(r.interest(v)) >= float(r.interest(outer))
        hit = np.nonzero(seen & keep)[0]
        if not len(hit):
            return None
        k = int(hit[0])
        lines = [f"{self.fmt_value(v[k])} at {self.fmt_height(zk[k])}"]
        rest = np.nonzero(keep[k:])[0] + k
        if len(rest):
            score = np.abs(v[rest]) if r.direction == 0 else (v[rest] * (1 if r.direction > 0 else -1))
            j = int(rest[int(np.argmax(score))])
            if abs(v[j] - v[k]) * abs(self.scale) >= 3 * (1 if abs(self.scale) >= 0.5 else 0.05):
                lines.append(f"Strongest behind: {self.fmt_value(v[j])} at {self.fmt_height(zk[j])}")
        return lines

    def _cell_world(self):
        g = self.grid
        hx, hy, top = self._box_world()
        nz, ny, nx = g.values.shape
        return min(2 * hx / max(nx - 1, 1), 2 * hy / max(ny - 1, 1), top / max(nz - 1, 1))

    def _run_probe(self):
        if self._probe_pos is None or self._mouse is not None:
            return
        try:
            lines = self.probe(self._probe_pos)
        except Exception:
            lines = None
        self._probe = (QPointF(self._probe_pos), lines) if lines else None
        self.update()

    def _draw_probe(self, p, W, H):
        at, lines = self._probe
        font = ui_font(9)
        fm = QFontMetricsF(font)
        tw = max(fm.horizontalAdvance(s) for s in lines) + 16
        th = 18 * len(lines) + 8
        x = at.x() + 16
        y = at.y() + 14
        if x + tw > W - 6:
            x = at.x() - tw - 10
        if y + th > H - 24:
            y = at.y() - th - 8
        r = QRectF(x, y, tw, th)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.setBrush(QColor(14, 18, 28, 225))
        p.drawRoundedRect(r, 6, 6)
        p.setFont(font)
        for i, s in enumerate(lines):
            p.setPen(QColor(236, 240, 250) if i == 0 else QColor(170, 182, 205))
            p.drawText(QPointF(x + 8, y + 17 + 18 * i), s)

    # ------------------------------------------------------------------ lightning (realistic)
    def _lightning_check(self):
        on = (self._active and self.grid is not None and self.look.style == "realistic" and self.look.lightning
              and self.realistic() and self.stats.get("flash") is not None)
        if on and not self._next_flash.isActive() and self._flash is None:
            self._next_flash.start(int(random.uniform(500, 2200)))
        elif not on:
            self._next_flash.stop()
            self._flash_timer.stop()
            if self._flash is not None:
                self._flash = None
                self.update()

    def _start_flash(self):
        pts = self.stats.get("flash")
        if pts is None or not len(pts) or not self._active:
            return
        x, y, z = (float(c) for c in pts[random.randrange(len(pts))])
        n = random.choice((1, 2, 2, 3, 4))
        strokes, t = [], 0.0
        for _ in range(n):
            strokes.append((t, random.uniform(2.5, 6.0)))
            t += random.uniform(0.05, 0.16)
        bolt = None
        base = float(self.look.cloud_base)
        if random.random() < 0.4:                          # cloud to ground: a jagged channel below the base
            bx, by = x + random.uniform(-3, 3), y + random.uniform(-3, 3)
            pts3 = [(bx, by, base + 0.3)]
            k = 18
            for i in range(1, k + 1):
                px, py, _pz = pts3[-1]
                pts3.append((px + random.uniform(-0.35, 0.35), py + random.uniform(-0.35, 0.35),
                             max(0.0, base + 0.3 - (base + 0.3) * i / k)))
            bolt = np.array(pts3)
            x, y, z = bx, by, base + 0.2
        self._flash = {"pos": (x, y, z), "strokes": strokes, "bolt": bolt, "t0": time.perf_counter(), "now": 0.0}
        self._flash_timer.start()
        self._flash_step()

    def _flash_step(self):
        fl = self._flash
        if fl is None:
            self._flash_timer.stop()
            return
        t = time.perf_counter() - fl["t0"]
        now = 0.0
        for ts, peak in fl["strokes"]:
            if t >= ts:
                now += peak * math.exp(-(t - ts) / 0.05)
        fl["now"] = now
        self._last_input = time.perf_counter()
        if t > fl["strokes"][-1][0] + 0.35:
            self._flash = None
            self._flash_timer.stop()
            night = self.sun().night
            self._next_flash.start(int(random.uniform(700, 2600) if night else random.uniform(1800, 5200)))
        self.update()

    def _bolt(self, p, mvp):
        fl = self._flash
        strength = min(1.0, fl["now"] / 3.0)
        if strength < 0.05:
            return
        sc, ok = self._project(mvp, fl["bolt"])
        if not ok.all():
            return
        path = QPainterPath(QPointF(*sc[0]))
        for q in sc[1:]:
            path.lineTo(QPointF(*q))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(170, 185, 255, int(90 * strength)), 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 255, 255, int(255 * strength)), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

    # ------------------------------------------------------------------ mouse
    def _touch(self):
        self._last_input = time.perf_counter()
        self._idle.start()
        self.update()
        self.interacted.emit()

    def mousePressEvent(self, ev):
        self._mouse = (ev.position(), ev.button(), ev.modifiers())
        self._anim = None
        self._probe = None

    def mouseMoveEvent(self, ev):
        if self._mouse is None:
            self._probe_pos = ev.position()
            self._probe_timer.start()
            return
        p0, btn, mods = self._mouse
        d = ev.position() - p0
        if btn == Qt.LeftButton and not (mods & Qt.ShiftModifier):
            self.yaw = (self.yaw - d.x() * 0.35) % 360.0
            self.pitch = self.pitch + d.y() * 0.3
            self._clamp()
        else:
            k = self.dist * 0.0016 * (self.fov / FOV)
            yaw = math.radians(self.yaw)
            right = np.array([math.cos(yaw), math.sin(yaw), 0.0])
            fwd = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
            self.target = self.target - right * d.x() * k + fwd * d.y() * k
        self._mouse = (ev.position(), btn, mods)
        self._touch()

    def mouseReleaseEvent(self, ev):
        self._mouse = None
        self.update()

    def event(self, ev):
        if ev.type() == QEvent.Leave:
            self._probe_pos = None
            if self._probe is not None:
                self._probe = None
                self.update()
        return super().event(ev)

    def wheelEvent(self, ev):
        self._anim = None
        self.dist = max(1.0, min(3000.0, self.dist * (0.88 ** (ev.angleDelta().y() / 120))))
        self._clamp()
        self._probe = None
        self._touch()

    def mouseDoubleClickEvent(self, ev):
        """Centre on the point of the floor under the mouse."""
        if self.grid is None:
            return
        a, d = self._ray(ev.position())
        if abs(d[2]) < 1e-9:
            return
        t = -a[2] / d[2]
        if t <= 0:
            return
        hit = a + d * t
        x0, y0, x1, y1 = self.grid.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        tgt = np.array([min(max(hit[0], -hx), hx), min(max(hit[1], -hy), hy), self.target[2]])
        self._fly(self.yaw, self.pitch, self.dist, tgt, self.fov, ms=380)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_R:
            self.reset_view()
        elif ev.key() == Qt.Key_G:
            self.reset_view("ground")
        elif ev.key() in (Qt.Key_Left, Qt.Key_Right):
            self.yaw = (self.yaw + (-8 if ev.key() == Qt.Key_Left else 8)) % 360
            self._touch()
        elif ev.key() in (Qt.Key_Up, Qt.Key_Down):
            self.pitch = self.pitch + (5 if ev.key() == Qt.Key_Up else -5)
            self._clamp()
            self._touch()
        else:
            super().keyPressEvent(ev)


# --------------------------------------------------------------------------- building grids off the UI thread
class _Sig(QObject):
    done = Signal(object, object)          # job, result (Grid3D or Exception)


class _Job(QRunnable):
    def __init__(self, job, fn, relay):
        super().__init__()
        self.job, self.fn, self.relay = job, fn, relay

    def run(self):
        try:
            r = self.fn()
        except Exception as exc:          # shown in the status line
            import traceback
            traceback.print_exc()
            r = exc
        finally:
            self.fn = None
        try:
            self.relay.done.emit(self.job, r)
        except RuntimeError:
            pass


PRODUCT_NAMES = {"REF": "Reflectivity", "DVEL": "Velocity (dealiased)", "SRV": "Storm-relative velocity",
                 "CC": "Correlation coefficient", "ZDR": "Differential reflectivity", "KDP": "Specific diff. phase",
                 "SW": "Spectrum width", "AZSH": "Azimuthal shear"}
CACHE_BYTES = 160 << 20
DEFAULTS = {"product": "REF", "style": "surfaces", "thresholds": {}, "levels": {}, "opacity": 0.6,
            "zscale": 2.5, "real_zscale": 1.0, "top_km": 18.0, "detail": "normal", "follow": True, "outline": True,
            "cut": "off", "cut_pos": 0.5, "shadows": True, "floor_map": True, "labels": True, "spin": False,
            "sun": "scan", "sun_az": 235.0, "sun_el": 35.0, "cloud_base_km": 1.3, "visibility_km": 80.0,
            "rain": True, "lightning": True, "options": None, "prev_product": "REF"}
LEVEL_PRESETS = {     # display units
    "REF": ("30, 50, 65", "20, 40, 60", "40, 55, 65", "35, 50", "50"),
    "DVEL": ("-25, 25", "-40, 40", "-15, 15"),
    "SRV": ("-20, 20", "-35, 35", "-15, 15"),
    "CC": ("0.80", "0.85, 0.70", "0.90"),
}
STYLES = (("surfaces", "Surfaces", "Lit surfaces at the levels you choose; the innermost is solid"),
          ("volume", "Volume", "A see-through glow: denser where values stand out"),
          ("realistic", "Realistic", "What it would look like to the eye: clouds and rain shafts lit by the sun "
                                     "where it was at the scan time, over the countryside (reflectivity)"))


def _nice(x):
    """A round number near x for default levels (48.6 kt -> 50)."""
    a = abs(x)
    if a >= 10:
        return round(x / 5) * 5
    if a >= 1:
        return round(x * 2) / 2
    return round(x, 2)


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto the next line when out of room."""

    def __init__(self, parent=None, hspacing=8, vspacing=4):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._place(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _place(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            if it.isEmpty():
                continue
            hint = it.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x = rect.x()
                y += line + self._v
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line = max(line, hint.height())
        return y + line - rect.y()


def _group(*widgets, spacing=4):
    w = QWidget()
    w.setFixedHeight(30)               # every group the same height: rows line up
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        lay.addWidget(x)
    return w


def _chip(text, tip=""):
    b = QToolButton()
    b.setText(text)
    b.setCheckable(True)
    b.setProperty("role", "chip")
    b.setToolTip(tip)
    return b


def _slider(lo, hi, value, tip=""):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(int(value))
    s.setToolTip(tip)
    s.setMinimumWidth(70)
    return s


class _Options(QWidget):
    """The options column: captioned sections of labelled rows."""

    def __init__(self):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(10, 6, 10, 8)
        self.lay.setSpacing(5)

    def section(self, title):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 4, 0, 2)
        v.setSpacing(5)
        cap = QLabel(title)
        cap.setProperty("role", "section")
        v.addWidget(cap)
        self.lay.addWidget(box)
        return box, v

    @staticmethod
    def row(v, label, widget, value=None, tip=""):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lab = QLabel(label)
        lab.setFixedWidth(66)
        if tip:
            lab.setToolTip(tip)
        h.addWidget(lab)
        h.addWidget(widget, 1)
        if value is not None:
            value.setMinimumWidth(52)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(value)
        v.addWidget(w)
        return w

    @staticmethod
    def chips(v, *chips):
        w = QWidget()
        flow = FlowLayout(w, 4, 4)
        for c in chips:
            flow.addWidget(c)
        v.addWidget(w)
        return w


class Volume3DWindow(QWidget):
    """The 3-D panel: a bar along the top, the view, and an options column that folds away. Follows the frame
    shown on the map."""
    closed = Signal()

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        import copy
        cfg = copy.deepcopy(DEFAULTS)
        cfg.update(copy.deepcopy(main.settings["volume3d"] or {}))
        for k in ("thresholds", "levels"):
            if not isinstance(cfg.get(k), dict):
                cfg[k] = {}
        if cfg.get("style") not in ("surfaces", "volume", "realistic"):
            cfg["style"] = "surfaces"
        self.cfg = cfg
        self.gl = GLVolume()
        self.gl.height_units = main.settings["height_units"] or "kft"
        self.gl_host = QWidget.createWindowContainer(self.gl, self)
        self.gl_host.setMinimumSize(240, 160)
        self.gl_host.setFocusPolicy(Qt.StrongFocus)
        self.gl_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._frame = None
        self._sun_scan = None            # (az, el) at the scan time

        # ---------------------------------------------------------------- the bar
        self.area = QToolButton()
        self.area.setText("⬚ Area")
        self.area.setToolTip("Drag a box around a storm on the map (B)")
        self.area.clicked.connect(lambda: self.main.set_tool("box3d"))
        self.product = QComboBox()
        for pid in grid3d.PRODUCTS:
            self.product.addItem(PRODUCT_NAMES.get(pid, catalog.get(pid).name), pid)
        self.product.setCurrentIndex(max(self.product.findData(cfg["product"]), 0))
        self.product.setToolTip("What to show in 3-D")
        self.product.setMaximumWidth(170)
        self.style_btns = {}
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for key, label, tip in STYLES:
            b = _chip(label, tip)
            grp.addButton(b)
            self.style_btns[key] = b
        self.style_btns[cfg["style"]].setChecked(True)
        self.follow = _chip("Follow", "Show the frame on the map, and play along with the loop")
        self.follow.setChecked(bool(cfg["follow"]))
        self.view_btn = QToolButton()
        self.view_btn.setText("View")
        self.view_btn.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self.view_btn)
        for k, t in (("reset", "Reset view (R)"), ("ground", "From the ground (G)"), ("top", "From above"),
                     ("south", "From the south"), ("west", "From the west"), ("north", "From the north"),
                     ("east", "From the east")):
            m.addAction(t, lambda k=k: self.gl.reset_view(k))
        m.addSeparator()
        self.spin_act = m.addAction("Spin slowly")
        self.spin_act.setCheckable(True)
        self.spin_act.setChecked(bool(cfg["spin"]))
        m.addSeparator()
        m.addAction("Rebuild", lambda: self.refresh(force=True))
        m.addAction("Save image…", self.save_image)
        m.addAction("Copy image", self.copy_image)
        self.view_btn.setMenu(m)
        self.opt_btn = QToolButton()
        self.opt_btn.setText("⚙")
        self.opt_btn.setCheckable(True)
        self.opt_btn.setToolTip("Show or hide the options beside the view")
        bar = QWidget()
        flow = FlowLayout(bar, 8, 4)
        for parts in ((self.area, self.product), tuple(self.style_btns.values()), (self.follow, self.view_btn,
                                                                                   self.opt_btn)):
            flow.addWidget(_group(*parts, spacing=3 if parts[0] in self.style_btns.values() else 4))

        # ---------------------------------------------------------------- the options column
        opts = _Options()
        self.opts = opts
        # surfaces
        self.sec_surf, v = opts.section("Surfaces")
        self.levels = QLineEdit()
        self.levels.setToolTip("Surface levels, separated by commas (for example 30, 50, 65)")
        self.lv_menu_btn = QToolButton()
        self.lv_menu_btn.setText("")
        self.lv_menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.lv_menu_btn.setToolTip("Common levels")
        self.lv_menu = QMenu(self.lv_menu_btn)
        self.lv_menu_btn.setMenu(self.lv_menu)
        lvw = QWidget()
        lh = QHBoxLayout(lvw)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.setSpacing(3)
        lh.addWidget(self.levels, 1)
        lh.addWidget(self.lv_menu_btn)
        self.lv_unit = QLabel()
        opts.row(v, "Levels", lvw, self.lv_unit)
        self.glass = _slider(0, 100, float(cfg["opacity"]) * 100, "How see-through the outer surfaces are")
        opts.row(v, "Glass", self.glass)
        # volume
        self.sec_vol, v = opts.section("Volume")
        self.thr = _slider(0, 200, 0, "Values weaker than this are clear")
        self.thr_lbl = QLabel()
        opts.row(v, "Show from", self.thr, self.thr_lbl)
        self.dens = _slider(0, 100, float(cfg["opacity"]) * 100, "How dense (opaque) the glow is")
        opts.row(v, "Density", self.dens)
        # realistic
        self.sec_real, v = opts.section("Sun & sky")
        self.sun = QComboBox()
        for k, (label, _a, _e) in sky.SUN_PRESETS.items():
            self.sun.addItem(label, k)
        self.sun.setCurrentIndex(max(0, self.sun.findData(cfg["sun"])))
        self.sun.setToolTip("Where the sun is: as it was when the radar scanned, a time of day, or your own")
        opts.row(v, "Sun", self.sun)
        self.sun_az = _slider(0, 359, float(cfg["sun_az"]), "Which direction the sunlight comes from")
        self.sun_az_lbl = QLabel()
        self.row_az = opts.row(v, "From", self.sun_az, self.sun_az_lbl)
        self.sun_el = _slider(-8, 85, float(cfg["sun_el"]), "How high the sun is (below 0: twilight and night)")
        self.sun_el_lbl = QLabel()
        self.row_el = opts.row(v, "Height", self.sun_el, self.sun_el_lbl)
        self.sun_info = QLabel()
        self.sun_info.setProperty("role", "hint")
        self.sun_info.setWordWrap(True)
        v.addWidget(self.sun_info)
        self.base = _slider(2, 30, float(cfg["cloud_base_km"]) * 10, "Height of the cloud base (rain falls below "
                                                                       "it). The radar can't see it: typically "
                                                                       "1–1.5 km on a humid day, higher when dry")
        self.base_lbl = QLabel()
        opts.row(v, "Cloud base", self.base, self.base_lbl)
        self.vis = _slider(0, 100, self._vis_to_slider(float(cfg["visibility_km"])), "How far you can see "
                                                                                     "through the haze")
        self.vis_lbl = QLabel()
        opts.row(v, "Visibility", self.vis, self.vis_lbl)
        self.rain = _chip("Rain shafts", "Rain falling below the cloud base, where the radar shows it")
        self.rain.setChecked(bool(cfg["rain"]))
        self.lightning = _chip("Lightning", "Flashes in the strongest cores (cloud-to-ground strokes now and then)")
        self.lightning.setChecked(bool(cfg["lightning"]))
        opts.chips(v, self.rain, self.lightning)
        # cut
        self.sec_cut, v = opts.section("Cut open")
        self.cut = QComboBox()
        for k, t in (("off", "No cut"), ("ns", "North–south"), ("ew", "East–west"), ("height", "At a height")):
            self.cut.addItem(t, k)
        self.cut.setCurrentIndex(max(0, self.cut.findData(cfg["cut"])))
        self.cut.setToolTip("Open the storm up: the half towards you is cut away and the cut face shows the data")
        opts.row(v, "Cut", self.cut)
        self.cut_pos = _slider(0, 100, float(cfg["cut_pos"]) * 100, "How far the cut reaches in from your "
                                                                     "side (the side facing you is cut away)")
        self.row_cut = opts.row(v, "Depth", self.cut_pos)
        # scene
        _box, v = opts.section("Scene")
        self.zs = _slider(10, 60, 25, "Vertical exaggeration (storms are much wider than they are tall)")
        self.zs_lbl = QLabel()
        opts.row(v, "Height ×", self.zs, self.zs_lbl)
        self.top = QSpinBox()
        kft = self.gl.height_units != "km"
        if kft:
            self.top.setRange(20, 75)
            self.top.setSingleStep(5)
            self.top.setSuffix(" kft")
            self.top.setValue(int(round(float(cfg["top_km"]) / 0.3048 / 5) * 5))
        else:
            self.top.setRange(6, 23)
            self.top.setSuffix(" km")
            self.top.setValue(int(round(float(cfg["top_km"]))))
        self.top.setToolTip("Height of the box (rebuilds)")
        opts.row(v, "Box top", self.top)
        self.detail = QComboBox()
        for k, t in (("fast", "Fast"), ("normal", "Normal"), ("high", "Fine")):
            self.detail.addItem(t, k)
        self.detail.setCurrentIndex(max(0, self.detail.findData(cfg["detail"])))
        self.detail.setToolTip("Grid detail: Fine takes longer to build and more memory (rebuilds)")
        opts.row(v, "Grid", self.detail)
        self.shadows = _chip("Shadows", "Surfaces and Volume: the storm shades itself, and the inside is darker")
        self.shadows.setChecked(bool(cfg["shadows"]))
        self.outline = _chip("Outline", "A faint 30 dBZ reflectivity outline around other products")
        self.outline.setChecked(bool(cfg["outline"]))
        self.floor_map = _chip("Map", "The lowest tilt and map lines on the floor (Realistic: map lines only)")
        self.floor_map.setChecked(bool(cfg["floor_map"]))
        self.labels = _chip("Labels", "Towns, heights, the strongest echo and the storm's numbers")
        self.labels.setChecked(bool(cfg["labels"]))
        opts.chips(v, self.shadows, self.outline, self.floor_map, self.labels)
        opts.lay.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(opts)
        sa.setFixedWidth(250)
        self.opt_area = sa

        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(4)
        middle.addWidget(self.gl_host, 1)
        middle.addWidget(sa)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 2)
        lay.setSpacing(4)
        lay.addWidget(bar)
        lay.addLayout(middle, 1)
        lay.addWidget(self.status)

        self.box = None
        self._cache: OrderedDict = OrderedDict()
        self._cache_bytes = 0
        self._running = None          # the job being built
        self._queued = None           # the newest job asked for meanwhile
        self._shown = None
        self._want = None
        self._relay = _Sig()
        self._relay.done.connect(self._done)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self.refresh)
        self._t_build = 0.0
        self._opts_auto = cfg.get("options") is None

        self.product.currentIndexChanged.connect(self._product_changed)
        grp.buttonClicked.connect(lambda b: self._style_clicked(b))
        for s in (self.thr, self.glass, self.dens, self.cut_pos, self.sun_az, self.sun_el, self.base, self.vis):
            s.valueChanged.connect(lambda _v: self._look_changed())
        self.glass.valueChanged.connect(lambda v: self._sync_opacity(self.dens, v))
        self.dens.valueChanged.connect(lambda v: self._sync_opacity(self.glass, v))
        self.levels.editingFinished.connect(self._look_changed)
        for c in (self.outline, self.shadows, self.floor_map, self.labels, self.rain, self.lightning):
            c.toggled.connect(lambda _v: self._look_changed())
        self.cut.currentIndexChanged.connect(lambda _i: self._look_changed())
        self.sun.currentIndexChanged.connect(lambda _i: self._sun_preset_changed())
        self.zs.valueChanged.connect(self._z)
        self.top.valueChanged.connect(lambda _v: self._rebuild_soon())
        self.detail.currentIndexChanged.connect(lambda _i: self._rebuild_soon())
        self.follow.toggled.connect(self._follow_toggled)
        self.spin_act.toggled.connect(self._spin_toggled)
        self.opt_btn.toggled.connect(self._options_toggled)
        main.stateChanged.connect(self._state_changed)
        self._product_controls()
        self._apply_style_scale()
        self._sun_preset_changed(save=False)
        self._look_changed(save=False)
        self.gl.set_spin(bool(cfg["spin"]))
        self._set_options_visible(bool(cfg["options"]) if cfg.get("options") is not None else True, save=False)

    # ------------------------------------------------------------------ settings
    def _save(self):
        pid = self.pid()
        cfg = self.cfg
        style = self.style()
        z = self.zs.value() / 10.0
        cfg.update(product=pid, style=style, opacity=self.glass.value() / 100.0,
                   top_km=self.top_km(), detail=self.detail.currentData(), follow=self.follow.isChecked(),
                   outline=self.outline.isChecked(), cut=self.cut.currentData(), cut_pos=self.cut_pos.value() / 100.0,
                   shadows=self.shadows.isChecked(), floor_map=self.floor_map.isChecked(),
                   labels=self.labels.isChecked(), spin=self.spin_act.isChecked(), sun=self.sun.currentData(),
                   sun_az=float(self.sun_az.value()), sun_el=float(self.sun_el.value()),
                   cloud_base_km=self.base.value() / 10.0, visibility_km=self._vis_km(),
                   rain=self.rain.isChecked(), lightning=self.lightning.isChecked())
        cfg["real_zscale" if style == "realistic" else "zscale"] = z
        self.main.settings["volume3d"] = cfg
        self.main.settings.save()

    def pid(self):
        return self.product.currentData() or "REF"

    def style(self):
        for k, b in self.style_btns.items():
            if b.isChecked():
                return k
        return "surfaces"

    def top_km(self):
        v = float(self.top.value())
        return v * 0.3048 if self.gl.height_units != "km" else v

    def _palette(self):
        return self.main.palette_for(self.pid())

    def _units(self):
        return catalog.get(self.pid()).units

    @staticmethod
    def _vis_to_slider(km):
        # 0..100 -> 8 .. 200 km (logarithmic)
        return int(round((math.log(max(km, 8.0)) - math.log(8.0)) / (math.log(200.0) - math.log(8.0)) * 100))

    def _vis_km(self):
        return math.exp(math.log(8.0) + self.vis.value() / 100.0 * (math.log(200.0) - math.log(8.0)))

    # ------------------------------------------------------------------ options column
    def _set_options_visible(self, on, save=True):
        self.opt_area.setVisible(on)
        self.opt_btn.blockSignals(True)
        self.opt_btn.setChecked(on)
        self.opt_btn.blockSignals(False)
        if save:
            self.cfg["options"] = bool(on)
            self._opts_auto = False
            self._save()

    def _options_toggled(self, on):
        self._set_options_visible(on)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._opts_auto:                  # until chosen: beside the view when there's room for both
            want = self.width() >= 760
            if want == self.opt_area.isHidden():
                self._set_options_visible(want, save=False)

    # ------------------------------------------------------------------ controls
    def _scale(self):
        pal = self._palette()
        return pal.data_scale(self._units()) or 1.0, pal.offset

    def _thr_range(self):
        """Storage-unit range of the threshold slider."""
        r = grid3d.RULES[self.pid()]
        pal = self._palette()
        sc, off = self._scale()
        a, b = sorted(((pal.vmin - off) / sc, (pal.vmax - off) / sc))
        if r.direction == 0:
            a = 0.0
        return a, b

    def _product_controls(self):
        """Threshold slider and levels for the product (remembered per product)."""
        pid = self.pid()
        r = grid3d.RULES[pid]
        sc, off = self._scale()
        thr = self.cfg["thresholds"].get(pid)
        thr = r.threshold if thr is None else (float(thr) - off) / sc
        a, b = self._thr_range()
        self.thr.blockSignals(True)
        v = abs(thr) if r.direction == 0 else thr
        self.thr.setValue(int(round((v - a) / max(b - a, 1e-9) * 200)))
        self.thr.blockSignals(False)
        lv = self.cfg["levels"].get(pid)
        default = ", ".join(f"{_nice(v * sc + off):g}" for v in r.levels)
        if not lv:
            lv = default
        self.levels.setText(lv)
        self.lv_menu.clear()
        presets = list(LEVEL_PRESETS.get(pid, ()))
        if default not in presets:
            presets.insert(0, default)
        for txt in presets:
            self.lv_menu.addAction(txt, lambda t=txt: (self.levels.setText(t), self._look_changed()))
        self.lv_unit.setText(self._palette().units or self._units())
        self.outline.setEnabled(pid != "REF")

    def _threshold(self):
        a, b = self._thr_range()
        v = a + (b - a) * self.thr.value() / 200.0
        sc, off = self._scale()
        d = v * sc + off                              # snap to a round value in the units shown (25 dBZ, not 25.1)
        span = abs(b - a) * abs(sc)
        q = 1.0 if span > 50 else 0.5 if span > 10 else 0.01
        return (round(d / q) * q - off) / sc

    def _levels(self):
        sc, off = self._scale()
        out = []
        for part in self.levels.text().replace(";", ",").split(","):
            try:
                out.append((float(part.strip()) - off) / sc)
            except ValueError:
                pass
        return out

    def _sync_opacity(self, other, v):
        if other.value() != v:
            other.blockSignals(True)
            other.setValue(v)
            other.blockSignals(False)

    def _style_clicked(self, b):
        style = self.style()
        if style == "realistic" and self.pid() != "REF":
            self.cfg["prev_product"] = self.pid()
            self.product.blockSignals(True)
            self.product.setCurrentIndex(max(0, self.product.findData("REF")))
            self.product.blockSignals(False)
            self._product_controls()
            self.status.setText("Realistic shows reflectivity: what the radar sees as rain, hail and cloud.")
            self.refresh(force=True)
        self._apply_style_scale()
        self._look_changed()
        if self.gl.grid is not None:
            # realistic opens where you'd see it from in life: on the ground, a storm chaser's distance away
            self.gl.reset_view("ground" if style == "realistic" else "reset")

    def _apply_style_scale(self):
        """Each style keeps its own height exaggeration (Realistic: true proportions to start with)."""
        z = float(self.cfg["real_zscale"] if self.style() == "realistic" else self.cfg["zscale"])
        self.zs.blockSignals(True)
        self.zs.setValue(int(round(z * 10)))
        self.zs.blockSignals(False)
        self.zs_lbl.setText(f"{z:.1f}")
        self.gl.set_zscale(z)

    def _sun_preset_changed(self, save=True):
        k = self.sun.currentData()
        _label, az, el = sky.SUN_PRESETS.get(k, (None, None, None))
        if k == "scan" and self._sun_scan is not None:
            az, el = self._sun_scan
        for s, val in ((self.sun_az, az), (self.sun_el, el)):
            if val is not None:
                s.blockSignals(True)
                s.setValue(int(round(val)))
                s.blockSignals(False)
        custom = k == "custom"
        self.row_az.setEnabled(custom)
        self.row_el.setEnabled(custom)
        self._look_changed(save=save)

    def _sun_text(self, look):
        el = look.sun_el
        if el < -6:
            where = "night"
        elif el < 0:
            where = f"twilight, sun just set in the {sky.compass(look.sun_az)}"
        else:
            where = f"sun {el:.0f}° up in the {sky.compass(look.sun_az)}"
        return where

    def _look_changed(self, save=True):
        pid = self.pid()
        style = self.style()
        self.sec_surf.setVisible(style == "surfaces")
        self.sec_vol.setVisible(style == "volume")
        self.sec_real.setVisible(style == "realistic")
        self.shadows.setEnabled(style != "realistic")
        self.outline.setEnabled(pid != "REF" and style != "realistic")
        sc, off = self._scale()
        thr = self._threshold()
        units = self._palette().units or self._units()
        r = grid3d.RULES[pid]
        sym = "≥" if r.direction > 0 else "≤" if r.direction < 0 else "|v| ≥"
        self.thr_lbl.setText(f"{sym} {thr * sc + off:.3g} {units}")
        look = Look()
        look.style = style
        look.threshold = thr
        look.levels = self._levels() or None
        look.opacity = self.glass.value() / 100.0
        look.outline = self.outline.isChecked()
        look.cut = self.cut.currentData()
        look.cut_pos = self.cut_pos.value() / 100.0
        look.shadows = self.shadows.isChecked()
        look.floor_map = self.floor_map.isChecked()
        look.labels = self.labels.isChecked()
        look.sun_az = float(self.sun_az.value())
        look.sun_el = float(self.sun_el.value())
        look.cloud_base = self.base.value() / 10.0
        look.visibility = self._vis_km()
        look.rain = self.rain.isChecked()
        look.lightning = self.lightning.isChecked()
        self.row_cut.setEnabled(look.cut != "off")
        self.sec_cut.setVisible(style != "realistic")          # (real storms don't open up)
        self.sun_az_lbl.setText(f"{sky.compass(look.sun_az)} {look.sun_az:.0f}°")
        self.sun_el_lbl.setText(f"{look.sun_el:.0f}°")
        self.base_lbl.setText(self.gl.fmt_height(look.cloud_base) if self.gl.height_units == "km"
                              else f"{look.cloud_base / 0.3048 * 1000:,.0f} ft")
        self.vis_lbl.setText(f"{look.visibility * 0.6214:.0f} mi" if self.gl.height_units != "km"
                             else f"{look.visibility:.0f} km")
        self.sun_info.setText(self._sun_info(look))
        self.gl.set_look(look)
        self.gl.subtitle = self._subtitle(look)
        if save:
            self.cfg["thresholds"][pid] = round(thr * sc + off, 4)
            if self.levels.text().strip():
                self.cfg["levels"][pid] = self.levels.text().strip()
            self._save()

    def _sun_info(self, look):
        k = self.sun.currentData()
        txt = self._sun_text(look).capitalize()
        if k == "scan":
            if self._sun_scan is None:
                return "The sun as it was at the scan time (shown once a storm is loaded)."
            local = self._local_time()
            txt = (f"At the scan time{(' (' + local + ')') if local else ''}: {self._sun_text(look)}.")
        if look.sun_el < -6:
            txt += " Only lightning lights it up: pick a time of day under Sun to see it in daylight."
        return txt

    def _local_time(self):
        f = self._frame
        if f is None or getattr(f, "time", None) is None:
            return ""
        try:
            from zoneinfo import ZoneInfo
            from ..data.sites import get_site
            site = get_site(f.site)
            tz = ZoneInfo(site.tz) if site is not None and site.tz else None
            if tz is None:
                return ""
            t = f.time.astimezone(tz)
            return (t.strftime("%I:%M %p").lstrip("0") + " " + (t.tzname() or "")).strip()
        except Exception:
            return ""

    def _subtitle(self, look):
        cut = " · cut open" if look.cut != "off" and look.style != "realistic" else ""
        if look.style == "realistic":
            return f"Realistic · {self._sun_text(look)}" + cut
        return ("Surfaces" if look.style == "surfaces" else "Volume") + cut

    def _product_changed(self):
        if self.style() == "realistic" and self.pid() != "REF":
            self.style_btns["surfaces"].setChecked(True)
            self._apply_style_scale()
        self._product_controls()
        self._look_changed()
        self.refresh(force=True)

    def _z(self, v):
        z = v / 10.0
        self.zs_lbl.setText(f"{z:.1f}")
        self.gl.set_zscale(z)
        self.cfg["real_zscale" if self.style() == "realistic" else "zscale"] = z

    def _rebuild_soon(self):
        self._save()
        self._debounce.start()

    def _follow_toggled(self, on):
        self._save()
        if on:
            self.refresh()

    def _spin_toggled(self, on):
        self.gl.set_spin(on)
        self._save()

    def _state_changed(self):
        if self.follow.isChecked() and self.box is not None and self.isVisible():
            self._debounce.start()

    # ------------------------------------------------------------------ area and building
    def set_box(self, x0, y0, x1, y1):
        self.box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.main.view.set_box(self.box)
        self.refresh(force=True)

    def on_closed(self):
        """Called when the 3-D panel is closed: forget the selected area."""
        self.box = None
        self.gl.set_active(False)
        self.closed.emit()

    def build(self):                  # (older callers)
        self.refresh(force=True)

    def _key(self, frame):
        pid = self.pid()
        pal = self._palette()
        extra = ()
        if pid == "SRV":
            s = self.main.settings
            extra = (s["storm_motion_dir"], s["storm_motion_kts"])
        return (frame.uid, frame.l2_rev, frame.site, pid, self.box, round(self.top_km(), 2),
                self.detail.currentData(), id(pal), extra)

    def refresh(self, force=False):
        frame = self.main.current_frame()
        if self.box is None:
            self.gl.set_message("Drag a box around a storm on the map (B, or “Area”) to see it in 3-D.")
            return
        if frame is None or not frame.has_level2():
            self.gl.set_message("The 3-D view needs Level II radar data.")
            return
        key = self._key(frame)
        self._want = key
        g = self._cache.get(key)
        if g is not None and not force:
            self._cache.move_to_end(key)
            self._show(g, frame)
            return
        if force:
            self._cache.pop(key, None)
        self._submit(key, frame)

    def _submit(self, key, frame, prefetch=False):
        job = (key, frame, prefetch)
        if self._running is not None:
            if self._running[0] != key:
                self._queued = job
            return
        self._start(job)

    def _start(self, job):
        key, frame, prefetch = job
        self._running = job
        engine = self.main.engine
        pid = self.pid()
        box = self.box
        top = self.top_km()
        detail = self.detail.currentData()
        if not prefetch:
            self.gl.set_busy(f"Building 3-D {PRODUCT_NAMES.get(pid, pid).lower()}…")
        self._t_build = time.perf_counter()

        def fn():
            tilts = engine.tilt_sweeps(frame, pid, cache=False)
            ref = None if pid == "REF" else engine.tilt_sweeps(frame, "REF", cache=False)
            if not tilts:
                raise RuntimeError("no Level II tilts for this product")
            g = grid3d.build(tilts, ref, box, top, pid, detail, key=key)
            try:
                g.info["stats"] = grid3d.stats(g)
            except Exception:
                pass
            return g
        QThreadPool.globalInstance().start(_Job(job, fn, self._relay))

    def _done(self, job, res):
        key, frame, prefetch = job
        self._running = None
        if isinstance(res, Exception):
            if not prefetch:
                self.gl.set_busy("")
                self.status.setText(f"Couldn't build the 3-D view: {res}")
        else:
            self._remember(key, res)
            if key == self._want:
                self._show(res, frame)
        q, self._queued = self._queued, None
        if q is not None and q[0] not in self._cache:
            self._start(q)
        elif self.follow.isChecked() and getattr(self.main, "playing", False):
            self._prefetch()
        if self._running is None and self.gl.busy:
            self.gl.set_busy("")

    def _remember(self, key, grid):
        self._cache[key] = grid
        self._cache_bytes += grid.nbytes
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            _k, old = self._cache.popitem(last=False)
            self._cache_bytes -= old.nbytes

    def _prefetch(self):
        """While the loop plays: build the next frames' grids so the 3-D loop gets smooth."""
        frames = list(getattr(self.main.data, "frames", []) or [])
        if not frames or self._running is not None or self._cache_bytes > CACHE_BYTES * 0.85:
            return
        i0 = max(0, getattr(self.main, "frame_index", 0))
        for k in range(1, len(frames)):
            f = frames[(i0 + k) % len(frames)]
            if f is None or not f.has_level2() or f.level2_if_ready() is None:
                continue
            key = self._key(f)
            if key not in self._cache:
                self._start((key, f, True))
                return

    def _scan_sun(self, frame):
        """(azimuth, elevation) of the sun at the radar at the scan time, or None."""
        try:
            from ..data.sites import get_site
            site = get_site(frame.site)
            if site is None or frame.time is None:
                return None
            return sky.sun_position(frame.time, site.lat, site.lon)
        except Exception:
            return None

    def _show(self, grid, frame):
        pid = grid.pid
        pal = self._palette()
        self._frame = frame
        sun = self._scan_sun(frame)
        if sun is not None and sun != self._sun_scan:
            self._sun_scan = sun
            if self.sun.currentData() == "scan":
                self._sun_preset_changed(save=False)
        if "stats" not in grid.info:
            try:
                grid.info["stats"] = grid3d.stats(grid)
            except Exception:
                pass
        title = f"{frame.site}  ·  {PRODUCT_NAMES.get(pid, pid)}"
        if frame.time is not None:
            title += f"  ·  {frame.time:%Y-%m-%d %H:%M:%S}Z"
        keep = self._shown is not None and self._shown.box == grid.box
        self.gl.set_grid(grid, pal, catalog.get(pid).units, title, keep_camera=keep)
        self._floor(grid, pal)
        self._shown = grid
        x0, y0, x1, y1 = grid.box
        nz, ny, nx = grid.shape
        self.status.setText(f"{x1 - x0:.0f} × {y1 - y0:.0f} km, {grid.info.get('res_km', 0):.2f} km grid "
                            f"({nx}×{ny}×{nz}), {len(grid.elevations)} tilts, built in {grid.build_s:.1f} s.  "
                            "“Area” picks another storm.")

    def _floor(self, grid, pal):
        """The lowest tilt (dimmed), map lines and towns for the floor."""
        sc = pal.data_scale(catalog.get(grid.pid).units) or 1.0
        base = grid.base
        rgba = pal.color_at(np.where(np.isfinite(base), base * sc + pal.offset, np.nan)).astype(np.float32)
        weak = ~np.isfinite(base)
        if grid.pid == "REF":
            weak |= base < 5.0
        rgba[..., :3] *= 0.5
        rgba[..., 3] = np.where(weak, 0, rgba[..., 3] * 0.92)
        x0, y0, x1, y1 = grid.box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        view = self.main.view
        lines = []
        colors = {"states": (0.95, 0.95, 1.0, 0.75), "counties": (0.72, 0.76, 0.85, 0.45),
                  "roads": (0.95, 0.72, 0.42, 0.42)}
        for name, col in colors.items():
            layer = view.maps.layers.get(name)
            if layer is None:
                continue
            s = layer.segments
            a, b = s[0::2], s[1::2]
            m = (np.abs(a[:, 0] - cx) <= hx) & (np.abs(a[:, 1] - cy) <= hy) & \
                (np.abs(b[:, 0] - cx) <= hx) & (np.abs(b[:, 1] - cy) <= hy)
            if not m.any():
                continue
            seg = np.zeros((m.sum() * 2, 7), np.float32)
            seg[0::2, :2] = a[m] - (cx, cy)
            seg[1::2, :2] = b[m] - (cx, cy)
            seg[:, 3:] = col
            lines.append(seg)
        cities = []
        maps = view.maps
        if len(getattr(maps, "city_xy", ())):
            xy = maps.city_xy
            inside = (np.abs(xy[:, 0] - cx) <= hx * 0.97) & (np.abs(xy[:, 1] - cy) <= hy * 0.97)
            idx = np.nonzero(inside)[0]
            if len(idx):
                pop = np.asarray(maps.city_pop)[idx]
                for i in idx[np.argsort(-pop)][:8]:
                    cities.append((float(xy[i, 0] - cx), float(xy[i, 1] - cy), str(maps.city_name[i])))
        self.gl.set_floor(np.clip(rgba, 0, 255).astype(np.uint8),
                          np.concatenate(lines) if lines else np.zeros((0, 7), np.float32), cities)

    def save_image(self):
        path, _f = QFileDialog.getSaveFileName(self, "Save 3-D image", "radarforge-3d.png", "PNG image (*.png)")
        if path:
            self.gl.grabFramebuffer().save(path)

    def copy_image(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setImage(self.gl.grabFramebuffer())
        self.status.setText("3-D picture copied – paste it anywhere.")

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self.gl.set_active(False)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.gl.set_active(True)
        if self.box is not None and self.follow.isChecked():
            self._debounce.start()
