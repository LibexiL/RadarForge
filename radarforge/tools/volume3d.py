"""3-D volume explorer: isosurfaces from the Level II volume (marching cubes)."""
from __future__ import annotations

import math

import numpy as np
import OpenGL

OpenGL.ERROR_CHECKING = False      # glGetError after every call is very slow
OpenGL.ERROR_LOGGING = False
from OpenGL import GL  # noqa: E402
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

from ..products import catalog
from .sampler import sample_volume
from ..render.fonts import ui_font

VS = """
#version 330 core
layout(location=0) in vec3 a_pos;
layout(location=1) in vec3 a_nrm;
uniform mat4 u_mvp;
uniform float u_zscale;
out vec3 v_nrm;
out float v_z;
void main(){
    vec3 p = vec3(a_pos.xy, a_pos.z * u_zscale);
    v_nrm = normalize(vec3(a_nrm.xy, a_nrm.z / max(u_zscale, 1e-3)));
    v_z = a_pos.z;
    gl_Position = u_mvp * vec4(p, 1.0);
}
"""
FS = """
#version 330 core
in vec3 v_nrm;
in float v_z;
uniform vec4 u_color;
uniform int u_lit;
out vec4 frag;
void main(){
    if (u_lit == 0) { frag = u_color; return; }
    vec3 L = normalize(vec3(0.4, -0.5, 0.8));
    float d = abs(dot(normalize(v_nrm), L));
    float amb = 0.35;
    frag = vec4(u_color.rgb * (amb + (1.0 - amb) * d), u_color.a);
}
"""


def perspective(fovy, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy) / 2)
    m = np.zeros((4, 4), np.float32)
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
    s = s / np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[0, 3], m[1, 3], m[2, 3] = -s @ eye, -u @ eye, f @ eye
    return m


class _Sig(QObject):
    done = Signal(object)


class _Job(QRunnable):
    """Runs fn in the thread pool and emits the result through a long-lived relay object."""

    def __init__(self, fn, relay):
        super().__init__()
        self.fn = fn
        self.sig = relay

    def run(self):
        try:
            r = self.fn()
        except Exception as exc:
            r = exc
        self.sig.done.emit(r)


MAX_COLUMNS = 260 * 260        # keeps memory/time bounded for big boxes


def effective_resolution(w_km, h_km, res_km):
    need = math.sqrt(max(w_km, 1e-3) * max(h_km, 1e-3) / MAX_COLUMNS)
    return max(res_km, need)


def build_isosurfaces(engine, frame, pid, box, res_km, top_km, levels_disp, palette, units, radar_h_km):
    """box = (x0, y0, x1, y1) km in the radar frame; mesh coordinates are relative to its centre."""
    from scipy import ndimage
    from skimage.measure import marching_cubes
    tilts = engine.tilt_sweeps(frame, pid)
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    res_km = effective_resolution(x1 - x0, y1 - y0, res_km)
    nx = int((x1 - x0) / res_km) + 1
    ny = int((y1 - y0) / res_km) + 1
    nz = int(top_km / 0.25) + 1
    xs = x0 + np.arange(nx) * res_km
    ys = y0 + np.arange(ny) * res_km
    zs = np.arange(nz) * 0.25          # km MSL
    Z, Y, X = np.meshgrid(zs, ys, xs, indexing="ij")
    vals = sample_volume(tilts, X, Y, np.maximum(Z - radar_h_km, 0.0), smooth=True)
    vals[np.isinf(vals)] = np.nan
    scale = palette.data_scale(units)
    meshes = []
    for lv in levels_disp:
        store = (lv - palette.offset) / scale
        sign = -1.0 if store < 0 else 1.0
        g = np.where(np.isfinite(vals), vals * sign, -1e4)
        g = ndimage.gaussian_filter(g, 0.6)
        if np.nanmax(g) < store * sign:
            continue
        try:
            verts, faces, normals, _ = marching_cubes(g, level=store * sign, spacing=(0.25, res_km, res_km))
        except (ValueError, RuntimeError):
            continue
        # verts are (z, y, x) offsets
        pos = np.stack([verts[:, 2] + xs[0] - cx, verts[:, 1] + ys[0] - cy, verts[:, 0]], 1).astype(np.float32)
        nrm = np.stack([normals[:, 2], normals[:, 1], normals[:, 0]], 1).astype(np.float32)
        tri = pos[faces.ravel()]
        tnr = nrm[faces.ravel()]
        rgba = palette.color_at(np.array([lv]))[0]
        meshes.append(dict(level=lv, pos=tri, nrm=tnr, color=tuple(int(c) for c in rgba)))
    return meshes, (xs, ys, zs), vals, res_km


class GLVolume(QOpenGLWindow):
    """3-D view: a native OpenGL window (see RadarView for why), embedded with a container."""

    def __init__(self, parent=None):
        super().__init__(QOpenGLWindow.NoPartialUpdate)
        from ..render.glview import window_format
        self.setFormat(window_format(depth=True))
        self.meshes = []
        self.ground = np.zeros((0, 3), np.float32)
        self.half = (40.0, 40.0)
        self.yaw, self.pitch, self.dist = -35.0, 30.0, 140.0
        self.zscale = 2.0
        self.target = np.array([0, 0, 3.0], np.float32)
        self._gpu = []
        self._ground_buf = None
        self._dirty = True
        self._last = None
        self.title = ""

    def set_scene(self, meshes, ground, half, title):
        self.meshes = meshes
        self.ground = ground
        self.half = half
        self.target = np.array([0, 0, 3.0], np.float32)
        self.dist = max(half) * 3.2
        self.title = title
        self._dirty = True
        self.update()

    def initializeGL(self):
        # also runs again after the dock is floated/re-docked (new context): drop old GPU objects
        self._gpu = []
        self._dirty = True

        def comp(src, kind):
            s = GL.glCreateShader(kind)
            GL.glShaderSource(s, src)
            GL.glCompileShader(s)
            if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
                raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
            return s
        self.prog = GL.glCreateProgram()
        for s in (comp(VS, GL.GL_VERTEX_SHADER), comp(FS, GL.GL_FRAGMENT_SHADER)):
            GL.glAttachShader(self.prog, s)
        GL.glLinkProgram(self.prog)
        self.u = {n: GL.glGetUniformLocation(self.prog, n) for n in ("u_mvp", "u_zscale", "u_color", "u_lit")}

    def resizeGL(self, w, h):
        self.update()          # see RadarView.resizeGL

    def _upload(self):
        for vao, vbo, _n, _c, _l in self._gpu:
            GL.glDeleteVertexArrays(1, [vao])
            GL.glDeleteBuffers(1, [vbo])
        self._gpu = []

        def buf(pos, nrm):
            data = np.ascontiguousarray(np.concatenate([pos, nrm], 1), np.float32)
            vao = GL.glGenVertexArrays(1)
            vbo = GL.glGenBuffers(1)
            GL.glBindVertexArray(vao)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(0)
            GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, False, 24, GL.ctypes.c_void_p(0))
            GL.glEnableVertexAttribArray(1)
            GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, False, 24, GL.ctypes.c_void_p(12))
            GL.glBindVertexArray(0)
            return vao, vbo, len(data)
        # ground plane + map lines
        hx, hy = self.half
        plane = np.array([[-hx, -hy, 0], [hx, -hy, 0], [hx, hy, 0], [-hx, -hy, 0], [hx, hy, 0], [-hx, hy, 0]],
                         np.float32)
        vao, vbo, n = buf(plane, np.tile([[0, 0, 1]], (6, 1)).astype(np.float32))
        self._gpu.append((vao, vbo, n, (22, 24, 30, 255), "plane"))
        if len(self.ground):
            g = self.ground.copy()
            g[:, 2] = 0.02
            vao, vbo, n = buf(g, np.tile([[0, 0, 1]], (len(g), 1)).astype(np.float32))
            self._gpu.append((vao, vbo, n, (150, 150, 160, 255), "lines"))
        for m in sorted(self.meshes, key=lambda m: -abs(m["level"])):
            vao, vbo, n = buf(m["pos"], m["nrm"])
            self._gpu.append((vao, vbo, n, m["color"], "mesh"))
        self._dirty = False

    def paintGL(self):
        if self._dirty:
            self._upload()
        GL.glClearColor(0.05, 0.05, 0.07, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        dpr = float(self.devicePixelRatio())
        w, h = int(round(self.width() * dpr)), int(round(self.height() * dpr))
        GL.glViewport(0, 0, w, h)
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        t = self.target * np.array([1, 1, self.zscale], np.float32)
        eye = t + self.dist * np.array([math.cos(pitch) * math.sin(yaw), -math.cos(pitch) * math.cos(yaw),
                                        math.sin(pitch)], np.float32)
        mvp = perspective(40, w / max(h, 1), 0.5, 5000) @ look_at(eye, t, np.array([0, 0, 1], np.float32))
        GL.glUseProgram(self.prog)
        GL.glUniformMatrix4fv(self.u["u_mvp"], 1, GL.GL_TRUE, mvp)
        GL.glUniform1f(self.u["u_zscale"], self.zscale)
        meshes = [g for g in self._gpu if g[4] == "mesh"]
        for vao, _vbo, n, col, kind in self._gpu:
            if kind == "mesh":
                continue
            GL.glUniform1i(self.u["u_lit"], 0)
            GL.glUniform4f(self.u["u_color"], *(c / 255 for c in col))
            GL.glBindVertexArray(vao)
            GL.glDrawArrays(GL.GL_TRIANGLES if kind == "plane" else GL.GL_LINES, 0, n)
        # innermost (highest) surfaces opaque, outer ones translucent
        for i, (vao, _vbo, n, col, _k) in enumerate(meshes):
            alpha = 1.0 if i == 0 else 0.38 if i == 1 else 0.13
            GL.glDepthMask(GL.GL_TRUE if alpha >= 0.99 else GL.GL_FALSE)
            GL.glUniform1i(self.u["u_lit"], 1)
            GL.glUniform4f(self.u["u_color"], col[0] / 255, col[1] / 255, col[2] / 255, alpha)
            GL.glBindVertexArray(vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, n)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        p = QPainter(self)
        p.setPen(QColor(230, 230, 230))
        p.setFont(ui_font(9))
        p.drawText(10, 18, self.title)
        p.drawText(10, self.height() - 10, "Left-drag: rotate   Right-drag: pan   Wheel: zoom")
        p.end()

    def mousePressEvent(self, ev):
        self._last = (ev.position(), ev.button())

    def mouseMoveEvent(self, ev):
        if self._last is None:
            return
        p0, btn = self._last
        d = ev.position() - p0
        if btn == Qt.LeftButton:
            self.yaw += d.x() * 0.4
            self.pitch = max(2.0, min(89.0, self.pitch + d.y() * 0.4))
        else:
            k = self.dist / 600.0
            yaw = math.radians(self.yaw)
            right = np.array([math.cos(yaw), math.sin(yaw), 0])
            fwd = np.array([-math.sin(yaw), math.cos(yaw), 0])
            self.target = (self.target - right * d.x() * k + fwd * d.y() * k).astype(np.float32)
        self._last = (ev.position(), btn)
        self.update()

    def mouseReleaseEvent(self, ev):
        self._last = None

    def wheelEvent(self, ev):
        self.dist = max(5.0, min(2000.0, self.dist * (0.88 ** (ev.angleDelta().y() / 120))))
        self.update()


class Volume3DWindow(QWidget):
    PRODUCTS = ["REF", "DVEL", "SRV", "ZDR", "CC", "KDP", "SW"]
    DEFAULT_LEVELS = {"REF": "30, 50, 65", "DVEL": "-60, 60", "SRV": "-50, 50", "ZDR": "3", "CC": "0.95",
                      "KDP": "2, 4", "SW": "10"}

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.gl = GLVolume()
        self.gl_host = QWidget.createWindowContainer(self.gl, self)
        self.gl_host.setMinimumSize(240, 160)
        self.gl_host.setFocusPolicy(Qt.StrongFocus)
        self.product = QComboBox()
        for pid in self.PRODUCTS:
            self.product.addItem(catalog.get(pid).name, pid)
        self.levels = QLineEdit(", ".join(str(v).rstrip("0").rstrip(".") for v in main.settings["volume3d_levels"]))
        self.select = QPushButton("⬚ Select area…")
        self.select.setToolTip("Drag a box on the map around the storm you want in 3-D")
        self.size = QDoubleSpinBox()
        self.size.setRange(10, 300)
        self.size.setValue(60)
        self.size.setSuffix(" km box")
        self.size.setToolTip("Box size used when no area has been selected (centred on the view)")
        self.res = QComboBox()
        self.res.addItems(["0.5 km", "1 km", "0.25 km"])
        self.top = QDoubleSpinBox()
        self.top.setRange(5, 25)
        self.top.setValue(16)
        self.top.setSuffix(" km top")
        self.zs = QSlider(Qt.Horizontal)
        self.zs.setRange(10, 80)
        self.zs.setValue(20)
        self.go = QPushButton("Build")
        self.status = QLabel("Click “Select area…” and drag a box around a storm on the map.")
        row = QHBoxLayout()
        for w in (self.select, QLabel("Product"), self.product, QLabel("Levels"), self.levels, self.size, self.res,
                  self.top, QLabel("Vert ×"), self.zs, self.go):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.gl_host, 1)
        lay.addWidget(self.status)
        self.go.clicked.connect(self.build)
        self.select.clicked.connect(lambda: self.main.set_tool("box3d"))
        self.box = None
        self.zs.valueChanged.connect(self._z)
        self.product.currentIndexChanged.connect(self._prod)
        self._running = False
        self._relay = _Sig()
        self._relay.done.connect(self._done)
        self._ctx = None

    def _z(self, v):
        self.gl.zscale = v / 10.0
        self.gl.update()

    def _prod(self):
        pid = self.product.currentData()
        self.levels.setText(self.DEFAULT_LEVELS.get(pid, "30"))

    closed = Signal()

    def set_box(self, x0, y0, x1, y1):
        self.box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.build()

    def on_closed(self):
        """Called when the 3-D window is closed: forget the selected area."""
        self.box = None
        self.closed.emit()

    def build(self):
        if self._running:
            return
        frame = self.main.current_frame()
        if frame is None or not frame.has_level2():
            self.status.setText("3D needs Level II data")
            return
        try:
            levels = [float(x) for x in self.levels.text().replace(";", ",").split(",") if x.strip()]
        except ValueError:
            self.status.setText("Levels must be numbers separated by commas")
            return
        pid = self.product.currentData()
        pal = self.main.palette_for(pid)
        units = catalog.get(pid).units
        view = self.main.view
        if self.box is None:
            h = self.size.value() / 2
            self.box = (view.cx - h, view.cy - h, view.cx + h, view.cy + h)
            self.main.view.set_box(self.box)
        box = self.box
        center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        half = ((box[2] - box[0]) / 2, (box[3] - box[1]) / 2)
        res = {"0.5 km": 0.5, "1 km": 1.0, "0.25 km": 0.25}[self.res.currentText()]
        top = self.top.value()
        vol_h = (frame.level2().height_m or 0.0) / 1000.0
        engine = self.main.engine
        # ground map lines inside the box
        segs = []
        for name in ("states", "counties", "roads"):
            lay = view.maps.layers.get(name)
            if lay is None:
                continue
            s = lay.segments
            a, b = s[0::2], s[1::2]
            m = (np.abs(a[:, 0] - center[0]) < half[0]) & (np.abs(a[:, 1] - center[1]) < half[1]) & \
                (np.abs(b[:, 0] - center[0]) < half[0]) & (np.abs(b[:, 1] - center[1]) < half[1])
            if m.any():
                seg = np.empty((m.sum() * 2, 3), np.float32)
                seg[0::2, :2] = a[m] - center
                seg[1::2, :2] = b[m] - center
                seg[:, 2] = 0
                segs.append(seg)
        ground = np.concatenate(segs) if segs else np.zeros((0, 3), np.float32)
        self._running = True
        eff = effective_resolution(box[2] - box[0], box[3] - box[1], res)
        self.status.setText(f"Building isosurfaces for a {box[2] - box[0]:.0f} × {box[3] - box[1]:.0f} km area "
                            f"at {eff:.2f} km…")
        self._ctx = (ground, half, pid, frame)
        QThreadPool.globalInstance().start(
            _Job(lambda: build_isosurfaces(engine, frame, pid, box, res, top, levels, pal, units, vol_h),
                 self._relay))

    def _done(self, res):
        ground, half, pid, frame = self._ctx
        self._running = False
        if isinstance(res, Exception):
            self.status.setText(f"Error: {res}")
            return
        meshes, _axes, _vals, eff = res
        n = sum(len(m["pos"]) // 3 for m in meshes)
        title = f"{frame.site} {catalog.get(pid).name} {frame.time:%Y-%m-%d %H:%M:%S}Z  levels: " + \
                ", ".join(f"{m['level']:g}" for m in meshes)
        self.gl.set_scene(meshes, ground, half, title)
        w_km, h_km = 2 * half[0], 2 * half[1]
        self.status.setText(f"{w_km:.0f} × {h_km:.0f} km area, {eff:.2f} km grid: {len(meshes)} surfaces, "
                            f"{n:,} triangles.  “Select area…” to pick another storm.")
