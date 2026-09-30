"""Pick an OpenGL setup that works on this machine.

Some driver / display-server combinations reject the context RadarForge asks for
(e.g. EGL_BAD_MATCH 0x3009 on some NVIDIA + Wayland setups), or accept it but then
can't show it (Qt prints "QRhiGles2: Failed to make context current" and the map
stays black). Startup therefore walks through a list of *attempts*, each a
(display platform, context format) pair:

  native -> X11/XWayland -> X11 software -> native software, and for each of them
  3.3 core + MSAA, 3.3 core, 3.3 compatibility.

The format has to be set before the QApplication exists (NVIDIA's GLX driver
insists on it), and Qt / PyOpenGL pick their windowing system when first
loaded, so moving to the next attempt restarts the program. The first attempt
that really draws is remembered for later launches.
"""
import os
import sys

# (key, human label, environment)
PLATFORMS = [
    ("native", "native display server", {}),
    ("x11", "X11 / XWayland (GLX)", {"QT_QPA_PLATFORM": "xcb", "QT_XCB_GL_INTEGRATION": "xcb_glx",
                                     "PYOPENGL_PLATFORM": "glx"}),
    ("x11-software", "X11 with software rendering", {"QT_QPA_PLATFORM": "xcb", "QT_XCB_GL_INTEGRATION": "xcb_glx",
                                                     "PYOPENGL_PLATFORM": "glx", "LIBGL_ALWAYS_SOFTWARE": "1",
                                                     "__GLX_VENDOR_LIBRARY_NAME": "mesa",
                                                     "GALLIUM_DRIVER": "llvmpipe"}),
    ("native-software", "native display server with software rendering",
     {"LIBGL_ALWAYS_SOFTWARE": "1", "__GLX_VENDOR_LIBRARY_NAME": "mesa", "GALLIUM_DRIVER": "llvmpipe",
      "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/50_mesa.json"}),
]
FORMATS = ["core-msaa", "core", "compat"]

ENV_ATTEMPT = "RADARFORGE_GL_ATTEMPT"      # index into attempt_order() for this launch
ENV_FORCE = "RADARFORGE_GL"                # user override, e.g. RADARFORGE_GL=x11
ENV_SIMULATE_FAIL = "RADARFORGE_GL_SIMULATE_FAIL"   # testing: comma list of platform keys to treat as broken


IS_LINUX = sys.platform.startswith("linux")


def _wayland_session() -> bool:
    if not IS_LINUX:
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def platform_order(saved: str | None, forced: str | None) -> list:
    keys = [p[0] for p in PLATFORMS]
    if not IS_LINUX:
        # Windows / macOS have one display system; only the context format can vary.
        # (Qt's software OpenGL on Windows can't be driven from PyOpenGL, so no software option.)
        keys = ["native"]
        forced = None if forced not in keys else forced
        saved = None if saved not in keys else saved
    elif not _wayland_session():
        # plain X11 session: "native" already is X11
        keys = [k for k in keys if k != "native-software"]
    order = []
    for k in ([forced] if forced else []) + ([saved] if saved else []) + keys:
        if k in keys and k not in order:
            order.append(k)
    return order


def apply_platform_env(key: str):
    """Set environment for a platform before Qt / PyOpenGL are imported."""
    if not IS_LINUX:
        return
    env = dict(next(p[2] for p in PLATFORMS if p[0] == key))
    if key.startswith("native"):
        qpa = os.environ.get("QT_QPA_PLATFORM", "")
        if _wayland_session() and not qpa.startswith("xcb"):
            env.setdefault("PYOPENGL_PLATFORM", "egl")
    for k, v in env.items():
        os.environ[k] = v
    if key.endswith("software") and not os.path.exists(os.environ.get("__EGL_VENDOR_LIBRARY_FILENAMES", "/")):
        os.environ.pop("__EGL_VENDOR_LIBRARY_FILENAMES", None)


def label(key: str) -> str:
    return next((p[1] for p in PLATFORMS if p[0] == key), key)


def make_format(kind: str):
    from PySide6.QtGui import QSurfaceFormat
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CompatibilityProfile if kind == "compat" else QSurfaceFormat.CoreProfile)
    f.setSamples(4 if kind == "core-msaa" else 0)
    f.setSwapInterval(1)
    return f


def probe_format(kind: str) -> tuple:
    """Try to create/make-current a context of this kind and use it from PyOpenGL. -> (ok, info)."""
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
    if kind == "core-msaa" and "msaa" in os.environ.get(ENV_SIMULATE_FAIL, "").split(","):
        return False, "simulated failure"
    ctx = QOpenGLContext()
    ctx.setFormat(make_format(kind))
    if not ctx.create():
        return False, "context creation failed"
    surf = QOffscreenSurface()
    surf.setFormat(ctx.format())
    surf.create()
    if not ctx.makeCurrent(surf):
        return False, "could not make context current"
    try:
        f = ctx.format()
        if (f.majorVersion(), f.minorVersion()) < (3, 3):
            return False, f"only OpenGL {f.majorVersion()}.{f.minorVersion()}"
        from OpenGL import GL    # also proves PyOpenGL can talk to this context
        ver = GL.glGetString(GL.GL_VERSION)
        ren = GL.glGetString(GL.GL_RENDERER)
        if not ver:
            return False, "PyOpenGL could not query the context"
        return True, f"{ver.decode(errors='replace')} / {(ren or b'?').decode(errors='replace')}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        ctx.doneCurrent()


def attempt_order(saved_platform, saved_format, forced=None) -> list:
    """All (platform, format) pairs to try, remembered / forced ones first."""
    fmts = []
    for k in ([saved_format] if saved_format else []) + FORMATS:
        if k in FORMATS and k not in fmts:
            fmts.append(k)
    out = []
    for p in platform_order(saved_platform, forced):
        for f in fmts:
            out.append((p, f))
    return out


def describe(attempt) -> str:
    p, f = attempt
    return f"{label(p)}, {FORMAT_LABELS.get(f, f)}"


FORMAT_LABELS = {"core-msaa": "OpenGL 3.3 core + antialiasing", "core": "OpenGL 3.3 core",
                 "compat": "OpenGL 3.3 compatibility"}

# Qt messages that mean the window can't actually show OpenGL content
FAILURE_MARKERS = ("Failed to make context current", "Failed to create wrapper texture",
                   "Failed to create context", "QOpenGLWidget: Failed", "Could not create EGL",
                   "failed to create drawable", "GLXBadDrawable", "BadMatch")


BEFORE_EXEC: list = []      # callbacks run just before the program re-executes itself


def restart_with_next(order: list, attempt: int, reason: str) -> bool:
    """Re-exec this program with the next attempt. Returns False if nothing is left to try."""
    nxt = attempt + 1
    if nxt >= len(order):
        return False
    for fn in BEFORE_EXEC:
        try:
            fn()
        except Exception:
            pass
    env = dict(os.environ)
    # drop anything the previous platform set so the next one starts clean
    for _k, _l, penv in PLATFORMS:
        for var in penv:
            env.pop(var, None)
    env.pop("PYOPENGL_PLATFORM", None)
    env[ENV_ATTEMPT] = str(nxt)
    print(f"RadarForge: {describe(order[attempt])} failed ({reason}); trying {describe(order[nxt])}…",
          file=sys.stderr, flush=True)
    args = [sys.executable, "-m", "radarforge"] + sys.argv[1:]
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        if IS_LINUX or sys.platform == "darwin":
            os.execve(sys.executable, args, env)          # replaces this process
        else:
            import subprocess                             # Windows: start the new copy, then leave
            subprocess.Popen(args, env=env, close_fds=True)
            os._exit(0)
    except OSError as exc:
        print("RadarForge: could not restart:", exc, file=sys.stderr)
        return False
    return True


def is_software(info: str) -> bool:
    """True if a GL renderer string looks like CPU rendering."""
    low = (info or "").lower()
    return any(k in low for k in ("llvmpipe", "softpipe", "swrast", "software rasterizer", "lavapipe"))
