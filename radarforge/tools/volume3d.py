"""3-D storm view.

The selected box of the radar volume is put on a regular grid (tools/grid3d.py) and drawn on the graphics card by
ray marching: every pixel walks through the grid front to back. Two styles:

  Volume    a glowing, see-through cloud: the product's colour table, denser where values stand out
  Surfaces  lit surfaces at chosen levels (30 / 50 / 65 dBZ ...): the innermost solid, the outer ones glassy

Because every pixel walks through the data in order, see-through layers always blend correctly (the old mesh
renderer drew them in the wrong order). Around it: the lowest tilt and the map on the floor, a height scale, the
compass and a colour legend; a cut plane opens the storm up (north-south, east-west or at a height).
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict

import numpy as np
import OpenGL

OpenGL.ERROR_CHECKING = False      # glGetError after every call is very slow
OpenGL.ERROR_LOGGING = False
from OpenGL import GL  # noqa: E402
from PySide6.QtCore import (QObject, QPoint, QPointF, QRect, QRectF, QRunnable, QSize, Qt, QThreadPool,  # noqa: E402
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtOpenGL import QOpenGLWindow  # noqa: E402
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLayout,  # noqa: E402
                               QLineEdit, QMenu, QSizePolicy, QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from ..products import catalog  # noqa: E402
from ..render.fonts import ui_font  # noqa: E402
from . import grid3d  # noqa: E402

# --------------------------------------------------------------------------- shaders
TRI_VS = """
#version 330 core
out vec2 v_uv;
void main(){
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

BG_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform vec3 u_top;
uniform vec3 u_bottom;
void main(){
    float t = clamp(v_uv.y, 0.0, 1.0);
    frag = vec4(mix(u_bottom, u_top, t * t * (3.0 - 2.0 * t)), 1.0);
}
"""

PRESENT_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_img;
void main(){ frag = texture(u_img, v_uv); }
"""

SCENE_VS = """
#version 330 core
layout(location=0) in vec3 a_pos;
layout(location=1) in vec4 a_col;
layout(location=2) in vec2 a_uv;
uniform mat4 u_mvp;
uniform float u_zscale;
out vec4 v_col;
out vec2 v_uv;
void main(){
    v_col = a_col;
    v_uv = a_uv;
    gl_Position = u_mvp * vec4(a_pos.xy, a_pos.z * u_zscale, 1.0);
}
"""

SCENE_FS = """
#version 330 core
in vec4 v_col;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
uniform int u_textured;
void main(){
    if (u_textured == 1) {
        vec4 t = texture(u_tex, v_uv);
        frag = vec4(mix(v_col.rgb, t.rgb, t.a), 1.0);
    } else {
        frag = v_col;
    }
}
"""

RAY_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;

uniform mat4 u_inv;            // inverse of projection * view
uniform vec3 u_bmin;           // the box in world units (z already exaggerated)
uniform vec3 u_bmax;
uniform vec3 u_size;           // grid size (nx, ny, nz)
uniform sampler3D u_vol;       // values scaled to 0..1 over the colour table's range
uniform sampler3D u_ctx;       // reflectivity, for the outline around other products
uniform sampler1D u_tf;        // volume style: colour and density for each value
uniform sampler1D u_pal;       // the colour table (opaque), for the cut face
uniform int u_mode;            // 0 volume, 1 surfaces
uniform int u_nlev;
uniform float u_lev[6];
uniform vec4 u_levcol[6];      // rgb, alpha
uniform float u_levside[6];    // +1: values above the level are inside the surface
uniform int u_ctx_on;
uniform float u_ctx_lev;
uniform vec4 u_ctx_col;
uniform int u_cut;
uniform vec4 u_plane;          // xyz normal, w distance: dot(n, p) > w is cut away
uniform float u_density;       // volume style: opacity per world unit
uniform float u_step;          // world units between samples
uniform int u_steps;
uniform vec3 u_light;          // towards the light

vec3 to_tex(vec3 p) {
    vec3 f = (p - u_bmin) / (u_bmax - u_bmin);
    return (f * (u_size - 1.0) + 0.5) / u_size;
}
float val(vec3 p) { return texture(u_vol, to_tex(p)).r; }
float cval(vec3 p) { return texture(u_ctx, to_tex(p)).r; }

vec3 grad(vec3 p, bool ctx) {
    vec3 ext = u_bmax - u_bmin;
    vec3 t = to_tex(p);
    vec3 d = 1.5 / u_size;                       // a wide stencil: smooth shading across grid cells
    float gx, gy, gz;
    if (ctx) {
        gx = texture(u_ctx, t + vec3(d.x, 0, 0)).r - texture(u_ctx, t - vec3(d.x, 0, 0)).r;
        gy = texture(u_ctx, t + vec3(0, d.y, 0)).r - texture(u_ctx, t - vec3(0, d.y, 0)).r;
        gz = texture(u_ctx, t + vec3(0, 0, d.z)).r - texture(u_ctx, t - vec3(0, 0, d.z)).r;
    } else {
        gx = texture(u_vol, t + vec3(d.x, 0, 0)).r - texture(u_vol, t - vec3(d.x, 0, 0)).r;
        gy = texture(u_vol, t + vec3(0, d.y, 0)).r - texture(u_vol, t - vec3(0, d.y, 0)).r;
        gz = texture(u_vol, t + vec3(0, 0, d.z)).r - texture(u_vol, t - vec3(0, 0, d.z)).r;
    }
    return vec3(gx / (2.0 * d.x * ext.x), gy / (2.0 * d.y * ext.y), gz / (2.0 * d.z * ext.z));
}

vec3 shade(vec3 base, vec3 n, vec3 rd) {
    vec3 v = -rd;
    float diff = max(dot(n, u_light), 0.0);
    vec3 fl = normalize(vec3(-u_light.xy, 0.25));            // a soft fill light from the other side
    float fill = max(dot(n, fl), 0.0);
    float sky = 0.6 + 0.4 * n.z;
    vec3 hv = normalize(u_light + v);
    float spec = pow(max(dot(n, hv), 0.0), 32.0) * 0.32;
    return base * (0.48 * sky + 0.62 * diff + 0.22 * fill) + vec3(spec);
}

void blend(inout vec4 acc, vec3 c, float a) {
    acc.rgb += (1.0 - acc.a) * a * c;
    acc.a += (1.0 - acc.a) * a;
}

// a surface crossing between two samples: refine it, light it, add it
void surface(inout vec4 acc, vec3 pa, vec3 pb, float va, float vb, float L, vec4 col, float side, vec3 rd,
             bool ctx) {
    for (int it = 0; it < 2; it++) {
        float f = clamp((L - va) / (vb - va), 0.0, 1.0);
        vec3 pm = mix(pa, pb, f);
        float vm = ctx ? cval(pm) : val(pm);
        if ((va - L) * (vm - L) < 0.0) { pb = pm; vb = vm; } else { pa = pm; va = vm; }
    }
    vec3 ph = mix(pa, pb, clamp((L - va) / (vb - va), 0.0, 1.0));
    vec3 g = grad(ph, ctx);
    float glen = length(g);
    vec3 n = glen > 1e-6 ? -side * g / glen : -rd;
    if (dot(n, rd) > 0.0) n = -n;                 // seen from inside: light the side facing us
    float facing = abs(dot(n, rd));
    float a = col.a;
    if (a < 0.999) a = a + (1.0 - a) * pow(1.0 - facing, 3.0) * 0.65;   // glassy edges
    blend(acc, shade(col.rgb, n, rd), a);
}

void main() {
    vec2 ndc = v_uv * 2.0 - 1.0;
    vec4 pn = u_inv * vec4(ndc, -1.0, 1.0);
    vec4 pf = u_inv * vec4(ndc, 1.0, 1.0);
    vec3 ro = pn.xyz / pn.w;
    vec3 rd = normalize(pf.xyz / pf.w - ro);
    vec3 rds = mix(rd, vec3(1e-6), lessThan(abs(rd), vec3(1e-6)));
    vec3 inv = 1.0 / rds;
    vec3 t0s = (u_bmin - ro) * inv;
    vec3 t1s = (u_bmax - ro) * inv;
    vec3 tmin = min(t0s, t1s);
    vec3 tmax = max(t0s, t1s);
    float t0 = max(max(tmin.x, tmin.y), max(tmin.z, 0.0));
    float t1 = min(min(tmax.x, tmax.y), tmax.z);
    if (t1 <= t0) discard;

    // a different start offset per pixel: no wood-grain banding
    float jit = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
    float t = t0 + jit * u_step;
    vec4 acc = vec4(0.0);
    vec3 pprev = ro + rd * t;
    float prev = val(pprev);
    float cprev = u_ctx_on == 1 ? cval(pprev) : 0.0;
    bool started = false;
    bool removed_before = false;
    for (int i = 0; i < 4096; i++) {
        if (i >= u_steps || t > t1) break;
        vec3 p = ro + rd * t;
        if (u_cut == 1) {
            if (dot(u_plane.xyz, p) > u_plane.w) { removed_before = true; t += u_step; continue; }
            if (removed_before) {
                // came in through the cut: show the data on the cut face
                removed_before = false;
                float den = dot(u_plane.xyz, rd);
                if (abs(den) > 1e-6) {
                    vec3 pc = ro + rd * ((u_plane.w - dot(u_plane.xyz, ro)) / den);
                    float vc = val(pc);
                    if (texture(u_tf, vc).a > 0.0005) {
                        vec3 n = -u_plane.xyz;
                        if (dot(n, rd) > 0.0) n = -n;
                        vec3 c = texture(u_pal, vc).rgb;
                        blend(acc, c * (0.62 + 0.38 * max(dot(n, u_light), 0.0)), 1.0);
                        break;
                    }
                }
                started = false;
            }
        }
        float cur = val(p);
        float ccur = u_ctx_on == 1 ? cval(p) : 0.0;
        if (!started) {                          // first sample inside: nothing to compare with yet
            started = true;
            prev = cur; cprev = ccur; pprev = p; t += u_step;
            continue;
        }
        if (u_mode == 0) {
            vec4 s = texture(u_tf, cur);
            if (s.a > 0.002) {
                float a = 1.0 - exp(-u_density * s.a * u_step);
                vec3 c = s.rgb;
                vec3 g = grad(p, false);
                float glen = length(g);
                if (glen > 1e-5) {
                    vec3 n = -g / glen;
                    if (dot(n, rd) > 0.0) n = -n;
                    c = mix(c * 0.8, shade(c, n, rd), clamp(glen * 6.0, 0.0, 1.0));
                }
                blend(acc, c, a);
            }
        } else {
            for (int k = 0; k < 6; k++) {
                if (k >= u_nlev) break;
                float L = u_lev[k];
                if ((prev - L) * (cur - L) < 0.0)
                    surface(acc, pprev, p, prev, cur, L, u_levcol[k], u_levside[k], rd, false);
            }
        }
        if (u_ctx_on == 1 && (cprev - u_ctx_lev) * (ccur - u_ctx_lev) < 0.0)
            surface(acc, pprev, p, cprev, ccur, u_ctx_lev, u_ctx_col, 1.0, rd, true);
        if (acc.a > 0.985) break;
        prev = cur; cprev = ccur; pprev = p;
        t += u_step;
    }
    if (acc.a <= 0.0) discard;
    frag = acc;                                  // premultiplied
}
"""


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


class Look:
    """What the volume looks like (no rebuild needed to change it)."""

    def __init__(self):
        self.style = "surfaces"        # volume | surfaces
        self.threshold = None          # storage units (volume style); None: the product's default
        self.levels = None             # storage units (surfaces style)
        self.opacity = 0.6             # 0..1
        self.outline = True            # reflectivity outline around other products
        self.cut = "off"               # off | ns | ew | height
        self.cut_pos = 0.5


# --------------------------------------------------------------------------- the OpenGL view
class GLVolume(QOpenGLWindow):
    """The 3-D view: a native OpenGL window (see RadarView for why), embedded with a container."""
    interacted = Signal()

    BG_TOP = (0.115, 0.135, 0.18)
    BG_BOTTOM = (0.02, 0.025, 0.035)
    FLOOR = (0.085, 0.095, 0.115, 1.0)

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
        # camera (target in km, height unexaggerated)
        self.yaw, self.pitch = PRESETS["reset"]
        self.dist = 120.0
        self.target = np.array([0.0, 0.0, 3.0])
        self.fov = 34.0
        self._mouse = None
        self._last_input = 0.0
        self._cost = None              # seconds per pixel at full detail
        self._idle = QTimer()
        self._idle.setSingleShot(True)
        self._idle.setInterval(220)
        self._idle.timeout.connect(self.update)
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
        self._dirty |= {"vol", "luts", "scene"}
        if first and not keep_camera:
            self.reset_view()
        self.busy = ""
        self.message = ""
        self.update()

    def set_floor(self, rgba, lines, cities):
        self.floor_rgba = rgba
        self.lines = lines
        self.cities = cities
        self._dirty.add("scene")
        self.update()

    def set_look(self, look: Look):
        self.look = look
        self._dirty.add("luts")
        self.update()

    def set_zscale(self, z):
        self.zscale = float(z)
        self.update()

    def set_busy(self, text):
        self.busy = text
        self.update()

    def set_message(self, text):
        self.message = text
        self.update()

    def clear(self, message=""):
        self.grid = None
        self.message = message
        self.update()

    def reset_view(self, preset="reset"):
        self.yaw, self.pitch = PRESETS.get(preset, PRESETS["reset"])
        if self.grid is not None:
            x0, y0, x1, y1 = self.grid.box
            hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
            top = self.grid.top
            self.target = np.array([0.0, 0.0, min(top * 0.35, 6.0)])
            # the whole box in view: its bounding sphere in the field of view
            r = math.sqrt(hx * hx + hy * hy + (top * self.zscale * 0.5) ** 2)
            self.dist = r / math.sin(math.radians(self.fov / 2)) * (0.74 if preset == "reset" else 0.7)
        self.update()

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
        if self.look.style == "surfaces":
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

    # ------------------------------------------------------------------ GL setup
    def _reset_gl_ids(self):
        self._tex = {}
        self._fbo = None
        self._fbo_size = (0, 0)
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

    def _texture(self, name, target):
        t = self._tex.get(name)
        if t is None:
            t = GL.glGenTextures(1)
            self._tex[name] = t
        GL.glBindTexture(target, t)
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(target, p, v)
        if target == GL.GL_TEXTURE_3D:
            GL.glTexParameteri(target, GL.GL_TEXTURE_WRAP_R, GL.GL_CLAMP_TO_EDGE)
        return t

    def _upload(self):
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        g = self.grid
        if "vol" in self._dirty and g is not None:
            nz, ny, nx = g.values.shape
            norm = self.to_norm(g.values.astype(np.float32)).astype(np.float16)
            self._texture("vol", GL.GL_TEXTURE_3D)
            GL.glTexImage3D(GL.GL_TEXTURE_3D, 0, GL.GL_R16F, nx, ny, nz, 0, GL.GL_RED, GL.GL_HALF_FLOAT,
                            np.ascontiguousarray(norm))
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
        parts = [("quad", quad), ("floor_lines", uv(np.concatenate([floor_lines, maps, bottom]))),
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

    # ------------------------------------------------------------------ camera
    def _eye(self):
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        t = self.target * np.array([1.0, 1.0, self.zscale])
        d = np.array([math.cos(pitch) * math.sin(yaw), -math.cos(pitch) * math.cos(yaw), math.sin(pitch)])
        return t + self.dist * d, t

    def matrices(self, aspect):
        eye, t = self._eye()
        view = look_at(eye, t, np.array([0.0, 0.0, 1.0]))
        far = self.dist * 4 + 600
        proj = perspective(self.fov, aspect, max(0.05, self.dist * 0.02), far)
        return proj @ view, view, eye

    def _plane(self, eye):
        """The cut plane in world units (normal, distance), cutting away the side towards the camera."""
        g = self.grid
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        f = min(max(self.look.cut_pos, 0.0), 1.0)
        if self.look.cut == "ns":
            c = -hx + 2 * hx * f
            return (np.array([1.0, 0, 0]), c) if eye[0] > c else (np.array([-1.0, 0, 0]), -c)
        if self.look.cut == "ew":
            c = -hy + 2 * hy * f
            return (np.array([0, 1.0, 0]), c) if eye[1] > c else (np.array([0, -1.0, 0]), -c)
        if self.look.cut == "height":
            c = g.top * self.zscale * f
            return np.array([0, 0, 1.0]), c
        return None

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
        GL.glUseProgram(self.p_bg)
        GL.glUniform3f(self._loc(self.p_bg, "u_top"), *self.BG_TOP)
        GL.glUniform3f(self._loc(self.p_bg, "u_bottom"), *self.BG_BOTTOM)
        GL.glBindVertexArray(self.vao_tri)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
        g = self.grid
        if g is None or not self._vbo_n:
            return None
        mvp, view, eye = self.matrices(w / max(h, 1))
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        # floor, floor grid, map lines
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glUseProgram(self.p_scene)
        GL.glUniformMatrix4fv(self._loc(self.p_scene, "u_mvp"), 1, GL.GL_TRUE, mvp.astype(np.float32))
        GL.glUniform1f(self._loc(self.p_scene, "u_zscale"), self.zscale)
        GL.glUniform1i(self._loc(self.p_scene, "u_tex"), 0)
        GL.glBindVertexArray(self.vao_scene)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        has_floor = "floor" in self._tex
        if has_floor:
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex["floor"])
        GL.glUniform1i(self._loc(self.p_scene, "u_textured"), 1 if has_floor else 0)
        a, n = self._vbo_n["quad"]
        GL.glDrawArrays(GL.GL_TRIANGLES, a, n)
        GL.glUniform1i(self._loc(self.p_scene, "u_textured"), 0)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        a, n = self._vbo_n["floor_lines"]
        GL.glDrawArrays(GL.GL_LINES, a, n)
        # the volume
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        self._draw_ray(mvp, view, eye, hx, hy, g)
        # the box's upright edges and top over it, faintly
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glUseProgram(self.p_scene)
        GL.glBindVertexArray(self.vao_scene)
        a, n = self._vbo_n["frame"]
        GL.glDrawArrays(GL.GL_LINES, a, n)
        return mvp

    def _draw_ray(self, mvp, view, eye, hx, hy, g):
        p = self.p_ray
        L = lambda name: self._loc(p, name)        # noqa: E731
        GL.glUseProgram(p)
        GL.glUniformMatrix4fv(L("u_inv"), 1, GL.GL_TRUE, np.linalg.inv(mvp).astype(np.float32))
        top = g.top * self.zscale
        GL.glUniform3f(L("u_bmin"), -hx, -hy, 0.0)
        GL.glUniform3f(L("u_bmax"), hx, hy, top)
        nz, ny, nx = g.values.shape
        GL.glUniform3f(L("u_size"), float(nx), float(ny), float(nz))
        for unit, (name, target, uni) in enumerate((("vol", GL.GL_TEXTURE_3D, "u_vol"),
                                                    ("ctx", GL.GL_TEXTURE_3D, "u_ctx"),
                                                    ("tf", GL.GL_TEXTURE_1D, "u_tf"),
                                                    ("pal", GL.GL_TEXTURE_1D, "u_pal"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(target, self._tex.get(name, 0))
            GL.glUniform1i(L(uni), unit)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        look = self.look
        GL.glUniform1i(L("u_mode"), 1 if look.style == "surfaces" else 0)
        styles = self.level_styles() if look.style == "surfaces" else []
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
        ctx_on = look.outline and g.context is not None
        GL.glUniform1i(L("u_ctx_on"), 1 if ctx_on else 0)
        lo, hi = grid3d.REF_RANGE
        GL.glUniform1f(L("u_ctx_lev"), (30.0 - lo) / (hi - lo))
        GL.glUniform4f(L("u_ctx_col"), 0.82, 0.86, 0.95, 0.07)
        plane = self._plane(eye) if look.cut != "off" else None
        GL.glUniform1i(L("u_cut"), 1 if plane is not None else 0)
        if plane is not None:
            GL.glUniform4f(L("u_plane"), *plane[0], plane[1])
        # samples about half a grid cell apart (in the exaggerated scene), more for big views
        cell = min(2 * hx / max(nx - 1, 1), 2 * hy / max(ny - 1, 1), top / max(nz - 1, 1))
        diag = math.sqrt((2 * hx) ** 2 + (2 * hy) ** 2 + top ** 2)
        step = max(cell * 0.5, diag / 1400)
        GL.glUniform1f(L("u_step"), step)
        GL.glUniform1i(L("u_steps"), int(min(4000, diag / step + 4)))
        # density: with the opacity slider in the middle, a 2 km thick strong core is nearly opaque
        GL.glUniform1f(L("u_density"), 0.06 * math.exp(4.2 * look.opacity) / max(self.zscale, 0.5) ** 0.5)
        # light from over the viewer's left shoulder
        inv_view = np.linalg.inv(view)
        right, up, back = inv_view[:3, 0], inv_view[:3, 1], inv_view[:3, 2]
        light = -0.45 * right + 0.6 * up + 0.65 * back
        light[2] = max(light[2], 0.35)
        light /= np.linalg.norm(light)
        GL.glUniform3f(L("u_light"), *light)
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
        if g is not None and mvp is not None:
            self._labels(p, mvp, g, small)
            self._legend(p, W, small)
            self._compass(p, mvp, W, H, small)
        # title
        p.setFont(ui_font(10.5, True))
        p.setPen(QColor(236, 240, 248))
        if self.title:
            p.drawText(QPointF(14, 22), self.title)
        if self.subtitle:
            p.setFont(normal)
            p.setPen(QColor(170, 182, 204))
            p.drawText(QPointF(14, 40), self.subtitle)
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
            p.setFont(small)
            p.setPen(QColor(130, 142, 165))
            p.drawText(QPointF(14, H - 10), "Drag: rotate   Right-drag: move   Wheel: zoom   Double-click: centre here")
        p.end()

    def _labels(self, p, mvp, g, font):
        x0, y0, x1, y1 = g.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        # height scale on the box corner that is leftmost on screen
        corners = np.array([[-hx, -hy, 0], [hx, -hy, 0], [hx, hy, 0], [-hx, hy, 0]], np.float64)
        sc, ok = self._project(mvp, corners)
        if ok.any():
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
                p.setPen(QColor(200, 212, 235, 220))
                p.drawText(QPointF(tx, at[1] + 4), txt)
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
                path = QPainterPath()
                path.addText(q + QPointF(5, 4), font, name)
                p.setPen(QPen(QColor(0, 0, 0, 170), 2.6))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
                p.fillPath(path, QColor(232, 236, 244))

    def _legend(self, p, W, font):
        if self.palette is None:
            return
        fm = QFontMetricsF(font)
        p.setFont(font)
        if self.look.style == "surfaces":
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

    # ------------------------------------------------------------------ mouse
    def _touch(self):
        self._last_input = time.perf_counter()
        self._idle.start()
        self.update()
        self.interacted.emit()

    def mousePressEvent(self, ev):
        self._mouse = (ev.position(), ev.button(), ev.modifiers())

    def mouseMoveEvent(self, ev):
        if self._mouse is None:
            return
        p0, btn, mods = self._mouse
        d = ev.position() - p0
        if btn == Qt.LeftButton and not (mods & Qt.ShiftModifier):
            self.yaw = (self.yaw - d.x() * 0.35) % 360.0
            self.pitch = max(2.0, min(89.0, self.pitch + d.y() * 0.3))
        else:
            k = self.dist * 0.0016
            yaw = math.radians(self.yaw)
            right = np.array([math.cos(yaw), math.sin(yaw), 0.0])
            fwd = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
            self.target = self.target - right * d.x() * k + fwd * d.y() * k
        self._mouse = (ev.position(), btn, mods)
        self._touch()

    def mouseReleaseEvent(self, ev):
        self._mouse = None
        self.update()

    def wheelEvent(self, ev):
        self.dist = max(2.0, min(3000.0, self.dist * (0.88 ** (ev.angleDelta().y() / 120))))
        self._touch()

    def mouseDoubleClickEvent(self, ev):
        """Centre on the point of the floor under the mouse."""
        if self.grid is None:
            return
        dpr = 1.0
        w, h = self.width() * dpr, self.height() * dpr
        mvp, _v, eye = self.matrices(w / max(h, 1))
        inv = np.linalg.inv(mvp)
        x = ev.position().x() / max(self.width(), 1) * 2 - 1
        y = 1 - ev.position().y() / max(self.height(), 1) * 2
        a = inv @ np.array([x, y, -1.0, 1.0])
        b = inv @ np.array([x, y, 1.0, 1.0])
        a, b = a[:3] / a[3], b[:3] / b[3]
        d = b - a
        if abs(d[2]) < 1e-9:
            return
        t = -a[2] / d[2]
        if t <= 0:
            return
        hit = a + d * t
        x0, y0, x1, y1 = self.grid.box
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        self.target = np.array([min(max(hit[0], -hx), hx), min(max(hit[1], -hy), hy), self.target[2]])
        self._touch()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_R:
            self.reset_view()
        elif ev.key() in (Qt.Key_Left, Qt.Key_Right):
            self.yaw = (self.yaw + (-8 if ev.key() == Qt.Key_Left else 8)) % 360
            self._touch()
        elif ev.key() in (Qt.Key_Up, Qt.Key_Down):
            self.pitch = max(2.0, min(89.0, self.pitch + (5 if ev.key() == Qt.Key_Up else -5)))
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
            "zscale": 2.5, "top_km": 18.0, "detail": "normal", "follow": True, "outline": True,
            "cut": "off", "cut_pos": 0.5}


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


def _group(*widgets):
    w = QWidget()
    w.setFixedHeight(30)               # every group the same height: rows line up
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
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


class Volume3DWindow(QWidget):
    """The 3-D panel: the view and its controls. Follows the frame shown on the map."""
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
        self.cfg = cfg
        self.gl = GLVolume()
        self.gl.height_units = main.settings["height_units"] or "kft"
        self.gl.zscale = float(cfg["zscale"])
        self.gl_host = QWidget.createWindowContainer(self.gl, self)
        self.gl_host.setMinimumSize(240, 160)
        self.gl_host.setFocusPolicy(Qt.StrongFocus)
        self.gl_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ---- controls (in small groups that wrap onto more lines when the panel is narrow)
        self.area = QToolButton()
        self.area.setText("⬚ Select area")
        self.area.setToolTip("Drag a box around a storm on the map (B)")
        self.area.clicked.connect(lambda: self.main.set_tool("box3d"))
        self.product = QComboBox()
        for pid in grid3d.PRODUCTS:
            self.product.addItem(PRODUCT_NAMES.get(pid, catalog.get(pid).name), pid)
        i = self.product.findData(cfg["product"])
        self.product.setCurrentIndex(max(i, 0))
        self.b_volume = _chip("Volume", "A see-through cloud: denser where values stand out")
        self.b_surf = _chip("Surfaces", "Lit surfaces at the levels you choose; the innermost is solid")
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for b in (self.b_volume, self.b_surf):
            grp.addButton(b)
        (self.b_surf if cfg["style"] == "surfaces" else self.b_volume).setChecked(True)
        self.thr = QSlider(Qt.Horizontal)
        self.thr.setRange(0, 200)
        self.thr.setFixedWidth(110)
        self.thr.setToolTip("Values weaker than this are clear")
        self.thr_lbl = QLabel()
        self.thr_lbl.setMinimumWidth(78)
        self.levels = QLineEdit()
        self.levels.setFixedWidth(110)
        self.levels.setToolTip("Surface levels, separated by commas (for example 30, 50, 65)")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(int(round(float(cfg["opacity"]) * 100)))
        self.opacity.setFixedWidth(80)
        self.opacity.setToolTip("How see-through the volume (or the outer surfaces) is")
        self.follow = _chip("Follow loop", "Show the frame on the map, and play along with the loop")
        self.follow.setChecked(bool(cfg["follow"]))
        self.cut = QComboBox()
        for k, t in (("off", "No cut"), ("ns", "Cut north–south"), ("ew", "Cut east–west"),
                     ("height", "Cut at height")):
            self.cut.addItem(t, k)
        self.cut.setCurrentIndex(max(0, self.cut.findData(cfg["cut"])))
        self.cut.setToolTip("Open the storm up: the half towards you is cut away and the cut face shows the data")
        self.cut_pos = QSlider(Qt.Horizontal)
        self.cut_pos.setRange(0, 100)
        self.cut_pos.setValue(int(round(float(cfg["cut_pos"]) * 100)))
        self.cut_pos.setFixedWidth(100)
        self.cut_pos.setToolTip("Where the cut is")
        self.zs = QSlider(Qt.Horizontal)
        self.zs.setRange(10, 60)
        self.zs.setValue(int(round(float(cfg["zscale"]) * 10)))
        self.zs.setFixedWidth(80)
        self.zs.setToolTip("Vertical exaggeration (storms are much wider than they are tall)")
        self.zs_lbl = QLabel()
        self.zs_lbl.setMinimumWidth(26)
        self.top = QSpinBox()
        kft = self.gl.height_units != "km"
        if kft:
            self.top.setRange(20, 75)
            self.top.setSingleStep(5)
            self.top.setSuffix(" kft top")
            self.top.setValue(int(round(float(cfg["top_km"]) / 0.3048 / 5) * 5))
        else:
            self.top.setRange(6, 23)
            self.top.setSuffix(" km top")
            self.top.setValue(int(round(float(cfg["top_km"]))))
        self.top.setToolTip("Height of the box")
        self.detail = QComboBox()
        for k, t in (("fast", "Fast"), ("normal", "Normal"), ("high", "Fine")):
            self.detail.addItem(t, k)
        self.detail.setCurrentIndex(max(0, self.detail.findData(cfg["detail"])))
        self.detail.setToolTip("Grid detail: Fine takes longer to build and more memory")
        self.outline = _chip("Outline", "A faint 30 dBZ reflectivity outline around other products")
        self.outline.setChecked(bool(cfg["outline"]))
        self.view_btn = QToolButton()
        self.view_btn.setText("View ▾")
        self.view_btn.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self.view_btn)
        for k, t in (("reset", "Reset view (R)"), ("top", "From above"), ("south", "From the south"),
                     ("west", "From the west"), ("north", "From the north"), ("east", "From the east")):
            m.addAction(t, lambda k=k: self.gl.reset_view(k))
        m.addSeparator()
        m.addAction("Rebuild", lambda: self.refresh(force=True))
        m.addAction("Save image…", self.save_image)
        self.view_btn.setMenu(m)

        controls = QWidget()
        flow = FlowLayout(controls, 10, 4)
        for parts in ((self.area,), (self.product,), (self.b_volume, self.b_surf),
                      (self.thr, self.thr_lbl, self.levels), (QLabel("Opacity"), self.opacity), (self.follow,),
                      (self.cut, self.cut_pos), (QLabel("Height ×"), self.zs, self.zs_lbl), (self.top,),
                      (self.detail,), (self.outline,), (self.view_btn,)):
            flow.addWidget(_group(*parts))

        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 2)
        lay.setSpacing(4)
        lay.addWidget(controls)
        lay.addWidget(self.gl_host, 1)
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

        self.product.currentIndexChanged.connect(self._product_changed)
        grp.buttonClicked.connect(lambda _b: self._look_changed())
        self.thr.valueChanged.connect(lambda _v: self._look_changed())
        self.levels.editingFinished.connect(self._look_changed)
        self.opacity.valueChanged.connect(lambda _v: self._look_changed())
        self.outline.toggled.connect(lambda _v: self._look_changed())
        self.cut.currentIndexChanged.connect(lambda _i: self._look_changed())
        self.cut_pos.valueChanged.connect(lambda _v: self._look_changed())
        self.zs.valueChanged.connect(self._z)
        self.top.valueChanged.connect(lambda _v: self._rebuild_soon())
        self.detail.currentIndexChanged.connect(lambda _i: self._rebuild_soon())
        self.follow.toggled.connect(self._follow_toggled)
        main.stateChanged.connect(self._state_changed)
        self._product_controls()
        self._z(self.zs.value())
        self._look_changed(save=False)

    # ------------------------------------------------------------------ settings
    def _save(self):
        pid = self.pid()
        cfg = self.cfg
        cfg.update(product=pid, style="surfaces" if self.b_surf.isChecked() else "volume",
                   opacity=self.opacity.value() / 100.0, zscale=self.zs.value() / 10.0,
                   top_km=self.top_km(), detail=self.detail.currentData(), follow=self.follow.isChecked(),
                   outline=self.outline.isChecked(), cut=self.cut.currentData(), cut_pos=self.cut_pos.value() / 100.0)
        self.main.settings["volume3d"] = cfg
        self.main.settings.save()

    def pid(self):
        return self.product.currentData() or "REF"

    def top_km(self):
        v = float(self.top.value())
        return v * 0.3048 if self.gl.height_units != "km" else v

    def _palette(self):
        return self.main.palette_for(self.pid())

    def _units(self):
        return catalog.get(self.pid()).units

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
        if not lv:
            lv = ", ".join(f"{_nice(v * sc + off):g}" for v in r.levels)
        self.levels.setText(lv)
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

    def _look_changed(self, save=True):
        pid = self.pid()
        surf = self.b_surf.isChecked()
        self.thr.setVisible(not surf)
        self.thr_lbl.setVisible(not surf)
        self.levels.setVisible(surf)
        sc, off = self._scale()
        thr = self._threshold()
        units = self._palette().units or self._units()
        r = grid3d.RULES[pid]
        sym = "≥" if r.direction > 0 else "≤" if r.direction < 0 else "|v| ≥"
        self.thr_lbl.setText(f" {sym} {thr * sc + off:.3g} {units}")
        look = Look()
        look.style = "surfaces" if surf else "volume"
        look.threshold = thr
        look.levels = self._levels() or None
        look.opacity = self.opacity.value() / 100.0
        look.outline = self.outline.isChecked()
        look.cut = self.cut.currentData()
        look.cut_pos = self.cut_pos.value() / 100.0
        self.cut_pos.setEnabled(look.cut != "off")
        self.gl.set_look(look)
        self.gl.subtitle = self._subtitle(look)
        if save:
            self.cfg["thresholds"][pid] = round(thr * sc + off, 4)
            if self.levels.text().strip():
                self.cfg["levels"][pid] = self.levels.text().strip()
            self._save()

    def _subtitle(self, look):
        if look.style == "surfaces":
            return "Surfaces" + (" · cut" if look.cut != "off" else "")
        return "Volume" + (" · cut" if look.cut != "off" else "")

    def _product_changed(self):
        self._product_controls()
        self._look_changed()
        self.refresh(force=True)

    def _z(self, v):
        self.zs_lbl.setText(f"{v / 10:.1f} ")
        self.gl.set_zscale(v / 10.0)
        self.cfg["zscale"] = v / 10.0

    def _rebuild_soon(self):
        self._save()
        self._debounce.start()

    def _follow_toggled(self, on):
        self._save()
        if on:
            self.refresh()

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
            self.gl.set_message("Drag a box around a storm on the map (B, or “Select area”) to see it in 3-D.")
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
            return grid3d.build(tilts, ref, box, top, pid, detail, key=key)
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

    def _show(self, grid, frame):
        pid = grid.pid
        pal = self._palette()
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
                            "“Select area” for another storm.")

    def _floor(self, grid, pal):
        """The lowest tilt (dimmed), map lines and towns for the floor."""
        sc = pal.data_scale(catalog.get(grid.pid).units) or 1.0
        base = grid.base
        rgba = pal.color_at(np.where(np.isfinite(base), base * sc + pal.offset, np.nan)).astype(np.float32)
        r = grid3d.RULES.get(grid.pid, grid3d.RULES["REF"])
        weak = ~np.isfinite(base)
        if grid.pid == "REF":
            weak |= base < 5.0
        rgba[..., :3] *= 0.5
        rgba[..., 3] = np.where(weak, 0, rgba[..., 3] * 0.92)
        del r
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

    def hideEvent(self, ev):
        super().hideEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        if self.box is not None and self.follow.isChecked():
            self._debounce.start()
