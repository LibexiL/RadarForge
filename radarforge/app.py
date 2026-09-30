"""Application entry point."""
from __future__ import annotations

import argparse
import os
import sys


class _Tee:
    """Copies everything written to stderr (ours and Qt's) into a log file.

    It works at the file-descriptor level from a reader thread, so Qt never has to
    call back into Python (a Python Qt message handler can deadlock with some
    drivers). Repeated lines are shown once, and lines that mean OpenGL output
    can't reach the screen are counted.
    """

    def __init__(self, log_path, markers, append=False):
        import threading
        self.markers = markers
        self.gl_failures = 0
        self.first_failure = ""
        self.seen: dict = {}
        self.counting = False
        try:
            self.log = open(log_path, "a" if append else "w", buffering=1, encoding="utf-8", errors="replace")
        except OSError:
            self.log = None
        self.real = None
        try:
            self.real = os.dup(2)
            r, w = os.pipe()
            os.dup2(w, 2)
            os.close(w)
            self._r = r
            threading.Thread(target=self._pump, name="rf-stderr", daemon=True).start()
        except OSError:
            self.real = None          # no usable stderr (started from a menu): nothing to copy

    def close(self):
        """Give stderr back to the terminal (before the program re-executes itself)."""
        import time
        try:
            sys.stderr.flush()
        except Exception:
            pass
        if self.real is None:
            return
        time.sleep(0.15)                  # let the reader thread catch up
        os.dup2(self.real, 2)
        if self.log is not None:
            try:
                self.log.flush()
            except Exception:
                pass

    def _pump(self):
        buf = b""
        while True:
            try:
                chunk = os.read(self._r, 65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self._line(raw.decode("utf-8", "replace"))

    def _line(self, text):
        if self.counting and any(m in text for m in self.markers):
            self.gl_failures += 1
            self.first_failure = self.first_failure or text.strip()
        n = self.seen.get(text, 0) + 1
        self.seen[text] = n
        if n > 2 and text.strip():
            if n == 3:
                text = "  (that message keeps repeating; further copies are hidden)"
            else:
                return
        data = (text + "\n").encode("utf-8", "replace")
        try:
            os.write(self.real, data)
        except (OSError, TypeError):
            pass
        if self.log is not None:
            try:
                self.log.write(text + "\n")
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="radarforge", description="NEXRAD Level II/III radar viewer")
    parser.add_argument("files", nargs="*", help="Level II / Level III files to open")
    parser.add_argument("--site", help="radar site id, e.g. KTLX")
    parser.add_argument("--no-live", action="store_true", help="don't start live mode on launch")
    parser.add_argument("--layout", type=int, choices=range(1, 7), help="number of panels")
    parser.add_argument("--x11", action="store_true",
                        help="run through XWayland/GLX instead of native Wayland")
    parser.add_argument("--software", action="store_true",
                        help="use software OpenGL rendering (slow; for broken GPU drivers)")
    parser.add_argument("--gl-reset", action="store_true",
                        help="forget the remembered OpenGL setup and detect it again")
    parser.add_argument("--safe-graphics", action="store_true",
                        help="plainest OpenGL setup: no antialiasing, no frame reuse (for driver trouble)")
    args = parser.parse_args(argv)

    from . import gl_setup
    from .config import LOG_FILE, Settings, ensure_dirs
    ensure_dirs()
    # started without a console (Windows Start Menu shortcut / pythonw): there is no stdout/stderr
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    tee = _Tee(LOG_FILE, gl_setup.FAILURE_MARKERS, append=gl_setup.ENV_ATTEMPT in os.environ)
    if getattr(tee, "real", None) is None and tee.log is not None:
        sys.stderr = tee.log               # no console at all: send our messages to the log
    import datetime
    import faulthandler
    print(f"==== RadarForge start {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  python {sys.version.split()[0]}  "
          f"args {sys.argv[1:]}", file=sys.stderr, flush=True)
    if tee.log is not None:
        faulthandler.enable(tee.log)       # crashes are written to the log file
    gl_setup.BEFORE_EXEC.append(tee.close)
    settings = Settings()
    if args.gl_reset:
        settings["gl_platform"] = None
        settings["gl_format"] = None
        settings.save()

    forced = os.environ.get(gl_setup.ENV_FORCE) or None
    if args.software:
        forced = "x11-software" if not gl_setup._wayland_session() or args.x11 else "native-software"
    elif args.x11:
        forced = "x11"
    saved_format = settings["gl_format"]
    if args.safe_graphics:
        saved_format = "core"
        os.environ["RADARFORGE_SCENE_CACHE"] = "0"
    elif not settings["scene_cache"]:
        os.environ["RADARFORGE_SCENE_CACHE"] = "0"
    order = gl_setup.attempt_order(settings["gl_platform"], saved_format, forced)
    try:
        attempt = min(max(int(os.environ.get(gl_setup.ENV_ATTEMPT, "0")), 0), len(order) - 1)
    except ValueError:
        attempt = 0
    key, kind = order[attempt]
    first_launch_env = gl_setup.ENV_ATTEMPT not in os.environ
    if not (first_launch_env and "PYOPENGL_PLATFORM" in os.environ and key == "native"):
        gl_setup.apply_platform_env(key)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication, QMessageBox

    # The context format and context sharing must be set up *before* the QApplication exists:
    # NVIDIA's GLX driver otherwise can't make Qt's window compositor current (black map).
    QSurfaceFormat.setDefaultFormat(gl_setup.make_format(kind))
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    # the map and 3-D view are native windows; don't turn their neighbours into native windows too
    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("RadarForge")
    app.setDesktopFileName("radarforge")
    from pathlib import Path
    from PySide6.QtGui import QIcon
    icon = Path(__file__).resolve().parent / "assets" / "radarforge.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    if sys.platform == "win32":
        try:                               # own taskbar group + icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RadarForge.Viewer")
        except Exception:
            pass

    # Ctrl+C / kill in a terminal: close cleanly (Qt otherwise ignores them)
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: app.quit())
        except (ValueError, OSError):
            pass
    # Heartbeat: lets Python run signal handlers while Qt is idle, and re-arms a watchdog that
    # writes every thread's stack to the log if the window ever stops responding for 10 s.
    def heartbeat():
        if tee.log is not None:
            faulthandler.dump_traceback_later(10, repeat=False, file=tee.log)
    _tick = QTimer()
    _tick.timeout.connect(heartbeat)
    _tick.start(300)

    def give_up(reason):
        QMessageBox.critical(None, "RadarForge – OpenGL problem", (
            "RadarForge could not get working OpenGL 3.3 graphics on this computer.\n\n"
            f"Tried {len(order)} setups. Last error: {reason}\n\n"
            "Updating the graphics driver usually fixes this: on Windows, install the latest driver "
            "from NVIDIA, AMD or Intel; on Linux, use your distribution's driver tool (for example "
            "the Nobara Driver Manager) and reboot.\n\n"
            f"Details are in the log file:\n{LOG_FILE}"))

    # ---- can this format be created here at all?
    simulated = [k for k in os.environ.get(gl_setup.ENV_SIMULATE_FAIL, "").split(",") if k]
    if key in simulated:
        ok, info = False, "simulated failure"
    else:
        ok, info = gl_setup.probe_format(kind)
    if not ok:
        if gl_setup.restart_with_next(order, attempt, info):
            return 0
        give_up(info)
        return 1
    print(f"RadarForge: OpenGL via {gl_setup.describe((key, kind))}: {info}", file=sys.stderr, flush=True)

    from .ui.mainwindow import MainWindow, apply_dark_theme
    if args.site:
        settings["site"] = args.site.upper()
    if args.layout:
        settings["layout"] = args.layout
    apply_dark_theme(app)
    tee.gl_failures = 0
    tee.counting = True              # only count problems from the real window onwards
    win = MainWindow(settings)
    win.show()
    if f"qtmsg:{key}:{kind}" in simulated:        # testing: pretend the driver complained
        from PySide6.QtCore import qWarning
        for ms in (300, 400, 500):
            QTimer.singleShot(ms, lambda: qWarning("QRhiGles2: Failed to make context current. "
                                                   "Expect bad things to happen."))

    # ---- confirm the real window can draw and show; otherwise move on to the next attempt
    checks = {"n": 0}

    def verify():
        checks["n"] += 1
        handle = win.windowHandle()
        exposed = handle is not None and handle.isExposed()
        broken = tee.gl_failures >= 2 or f"window:{key}" in simulated     # one stray warning is not enough
        valid = win.view.isValid() and bool(win.view.gl_info) and not broken
        if valid and checks["n"] >= 3:
            if gl_setup.is_software(win.view.gl_info) and not key.endswith("software"):
                win.show_gl_warning("Software OpenGL (llvmpipe) is in use, so drawing is slow. "
                                    "Check your graphics driver, then run: radarforge --gl-reset")
            # remember hardware setups; software rendering is never remembered, so a fixed
            # driver gets picked up again automatically
            remember = None if (key == "native" or key.endswith("software")) else key
            if settings["gl_platform"] != remember or settings["gl_format"] != kind:
                settings["gl_platform"] = remember
                settings["gl_format"] = kind
                settings.save()
            return
        if (broken or (exposed and not valid)) and checks["n"] >= 3:
            reason = tee.first_failure or "the window could not create its OpenGL context"
            win.prepare_restart()
            if gl_setup.restart_with_next(order, attempt, reason):
                return
            give_up(reason)
            return
        if checks["n"] < 30:
            QTimer.singleShot(800, verify)
    QTimer.singleShot(1200, verify)

    if args.files:
        QTimer.singleShot(200, lambda: win._open_paths([os.path.abspath(f) for f in args.files]))
    elif not args.no_live and settings["start_live"]:
        QTimer.singleShot(200, win.start_live)
    rc = app.exec()
    faulthandler.cancel_dump_traceback_later()
    return rc


if __name__ == "__main__":
    sys.exit(main())
