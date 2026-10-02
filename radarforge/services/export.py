"""Turning the map into pictures and animations: annotated PNGs, GIF and MP4 loops.

Nothing here knows about the main window: it takes images in and writes files out, so it can be tested
(and reused) on its own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter

# ------------------------------------------------------------------------------------------ images
MAX_SIZES = (("Same as the window", 0), ("1920 px wide", 1920), ("1280 px wide", 1280), ("960 px wide", 960),
             ("640 px wide (small file)", 640))


def to_rgb(img: QImage) -> np.ndarray:
    """A QImage as an (height, width, 3) uint8 array."""
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=bpl * h).reshape(h, bpl)
    return buf[:, :w * 3].reshape(h, w, 3).copy()       # a copy: the QImage's memory goes away with it


def from_rgb(arr: np.ndarray) -> QImage:
    h, w, _c = arr.shape
    arr = np.ascontiguousarray(arr, np.uint8)
    return QImage(arr.data, w, h, w * 3, QImage.Format_RGB888).copy()


def scaled_to_width(img: QImage, width: int) -> QImage:
    """The picture scaled down to `width` pixels (never up). 0 keeps the size."""
    if width <= 0 or img.width() <= width:
        return img
    return img.scaledToWidth(width, Qt.SmoothTransformation)


def annotate(img: QImage, title: str, subtitle: str = "", footer: str = "", dark: bool = True) -> QImage:
    """The picture with a title band on top (and a thin footer line below, if given)."""
    scale = max(1.0, img.width() / 1100.0)
    big, small = QFont(), QFont()
    big.setPointSizeF(13 * scale)
    big.setBold(True)
    small.setPointSizeF(9.5 * scale)
    fb, fs = QFontMetricsF(big), QFontMetricsF(small)
    pad = 9 * scale
    top = pad * 2 + fb.height() + (fs.height() + pad * 0.4 if subtitle else 0)
    bottom = (fs.height() + pad * 1.4) if footer else 0
    out = QImage(img.width(), int(round(img.height() + top + bottom)), QImage.Format_RGB888)
    bg = QColor(18, 20, 26) if dark else QColor(244, 246, 249)
    fg = QColor(240, 243, 248) if dark else QColor(20, 24, 32)
    dim = QColor(150, 160, 175) if dark else QColor(90, 100, 115)
    out.fill(bg)
    p = QPainter(out)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.drawImage(QPointF(0, top), img)
    p.setFont(big)
    p.setPen(fg)
    p.drawText(QPointF(pad * 1.5, pad + fb.ascent()), title)
    if subtitle:
        p.setFont(small)
        p.setPen(dim)
        p.drawText(QPointF(pad * 1.5, pad * 1.4 + fb.height() + fs.ascent()), subtitle)
    if footer:
        p.setFont(small)
        p.setPen(dim)
        p.drawText(QRectF(pad * 1.5, top + img.height(), img.width() - pad * 3, bottom),
                   Qt.AlignVCenter | Qt.AlignRight, footer)
    p.end()
    return out


# ------------------------------------------------------------------------------------------ GIF
class GifWriter:
    """Collects frames (as 256-colour pictures, to keep memory small) and writes an animated GIF."""

    def __init__(self, path, fps: float = 6.0, dwell: float = 1.0):
        self.path = str(path)
        self.fps = max(0.5, float(fps))
        self.dwell = max(0.0, float(dwell))
        self.frames: list = []

    def add(self, rgb: np.ndarray):
        from PIL import Image
        self.frames.append(Image.fromarray(rgb).quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=0))

    def save(self) -> None:
        if not self.frames:
            raise ValueError("no frames to write")
        step = int(round(1000.0 / self.fps))
        durations = [step] * len(self.frames)
        durations[-1] += int(self.dwell * 1000)
        first, rest = self.frames[0], self.frames[1:]
        first.save(self.path, save_all=True, append_images=rest, duration=durations, loop=0, optimize=False,
                   disposal=1)


    def abort(self):
        self.frames.clear()


# ------------------------------------------------------------------------------------------ MP4
def ffmpeg_exe() -> str | None:
    """A usable ffmpeg: the one bundled with imageio-ffmpeg, else one on the PATH."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


class Mp4Writer:
    """Streams frames into ffmpeg (H.264, plays everywhere). Raises RuntimeError when ffmpeg is missing."""

    def __init__(self, path, fps: float = 6.0, dwell: float = 1.0):
        self.path = str(path)
        self.fps = max(0.5, float(fps))
        self.dwell = max(0.0, float(dwell))
        self.exe = ffmpeg_exe()
        if not self.exe:
            raise RuntimeError("MP4 export needs ffmpeg. Reinstall RadarForge (it installs imageio-ffmpeg) "
                               "or install ffmpeg and try again.")
        self.proc = None
        self.size = None
        self._last = None

    def _start(self, w, h):
        self.size = (w, h)
        cmd = [self.exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", f"{self.fps}", "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", self.path]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)

    def add(self, rgb: np.ndarray):
        h, w = rgb.shape[:2]
        rgb = rgb[:h - h % 2, :w - w % 2]               # H.264 wants even sizes
        h, w = rgb.shape[:2]
        if self.proc is None:
            self._start(w, h)
        elif (w, h) != self.size:
            raise ValueError("all frames must be the same size")
        self._write(rgb)
        self._last = rgb

    def _write(self, rgb):
        try:
            self.proc.stdin.write(np.ascontiguousarray(rgb).tobytes())
        except (BrokenPipeError, OSError):
            raise RuntimeError(self._error() or "ffmpeg stopped unexpectedly")

    def _error(self):
        try:
            return self.proc.stderr.read().decode(errors="replace").strip()[-400:]
        except Exception:
            return ""

    def save(self) -> None:
        if self.proc is None:
            raise ValueError("no frames to write")
        for _ in range(int(round(self.dwell * self.fps))):          # hold the last frame
            self._write(self._last)
        self.proc.stdin.close()
        rc = self.proc.wait()
        if rc != 0:
            raise RuntimeError(self._error() or f"ffmpeg failed ({rc})")

    def abort(self):
        """Stop and remove the half-written file."""
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            for f in (self.proc.stdin, self.proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass
        try:
            os.remove(self.path)
        except OSError:
            pass


# ------------------------------------------------------------------------------------------ names
def default_name(site: str, start, end, ext: str) -> str:
    """KTLX_20261001_2155-2243Z.gif"""
    if start is None:
        return f"{site}.{ext}"
    day = f"{start:%Y%m%d_%H%M}"
    return f"{site}_{day}-{end:%H%M}Z.{ext}" if end is not None and end != start else f"{site}_{day}Z.{ext}"


def pick_frames(count: int, last_n: int) -> list:
    """Indices of the frames to export: all of them, or the last `last_n` (0 = all)."""
    if last_n <= 0 or last_n >= count:
        return list(range(count))
    return list(range(count - last_n, count))


def writer_for(path, fps: float, dwell: float):
    ext = Path(path).suffix.lower()
    if ext == ".mp4":
        return Mp4Writer(path, fps, dwell)
    if ext == ".gif":
        return GifWriter(path, fps, dwell)
    raise ValueError(f"unsupported format {ext}")
