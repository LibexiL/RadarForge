"""Picture / loop export: annotated images, GIF and MP4 writers."""
from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from radarforge.services import export  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def frames(n=5, w=160, h=90):
    out = []
    for i in range(n):
        a = np.zeros((h, w, 3), np.uint8)
        a[:, :, 0] = 40 * i
        a[10:30, 10 + 20 * i:30 + 20 * i] = (255, 255, 0)
        out.append(a)
    return out


def test_image_roundtrip_and_scale(qapp):
    a = frames(1)[0]
    img = export.from_rgb(a)
    assert (img.width(), img.height()) == (160, 90)
    assert np.array_equal(export.to_rgb(img), a)
    small = export.scaled_to_width(img, 80)
    assert (small.width(), small.height()) == (80, 45)
    assert export.scaled_to_width(img, 0) is img and export.scaled_to_width(img, 999) is img


def test_to_rgb_does_not_point_into_the_image(qapp):
    """A (width x 3) multiple of 4 lets numpy view the QImage's memory; the copy must outlive it."""
    import gc
    from PySide6.QtGui import QImage
    img = QImage(100, 40, QImage.Format_RGB888)           # 300-byte rows: no padding, so a view would be possible
    img.fill(0x336699)
    arr = export.to_rgb(img)
    del img
    gc.collect()
    junk = [QImage(100, 40, QImage.Format_RGB888) for _ in range(20)]    # reuse the freed memory
    for j in junk:
        j.fill(0xffffff)
    assert arr[0, 0].tolist() == [0x33, 0x66, 0x99] and arr.flags.owndata


def test_annotate_adds_bands(qapp):
    img = export.from_rgb(frames(1)[0])
    out = export.annotate(img, "KTLX", "reflectivity", "RadarForge")
    assert out.width() == img.width() and out.height() > img.height()
    plain = export.annotate(img, "KTLX")
    assert img.height() < plain.height() < out.height()
    # the picture itself is untouched below the title band
    arr = export.to_rgb(out)
    top = out.height() - img.height() - (out.height() - plain.height())
    assert arr.shape[0] == out.height() and top > 0


def test_gif_has_every_frame_and_holds_the_last(qapp, tmp_path):
    from PIL import Image
    path = tmp_path / "loop.gif"
    w = export.writer_for(path, fps=5, dwell=2.0)
    for f in frames(4):
        w.add(f)
    w.save()
    im = Image.open(path)
    assert im.n_frames == 4 and im.is_animated
    durations = []
    for i in range(im.n_frames):
        im.seek(i)
        durations.append(im.info["duration"])
    assert durations[:3] == [200] * 3 and durations[3] == 2200


@pytest.mark.skipif(export.ffmpeg_exe() is None, reason="no ffmpeg available")
def test_mp4_is_written_and_odd_sizes_are_trimmed(qapp, tmp_path):
    path = tmp_path / "loop.mp4"
    w = export.writer_for(path, fps=5, dwell=1.0)
    for f in frames(4, w=161, h=91):                   # odd size: H.264 needs even
        w.add(f)
    w.save()
    assert w.size == (160, 90)
    assert path.stat().st_size > 500
    with open(path, "rb") as fh:
        assert b"ftyp" in fh.read(64)


def test_mp4_rejects_changing_size(qapp, tmp_path):
    if export.ffmpeg_exe() is None:
        pytest.skip("no ffmpeg available")
    w = export.Mp4Writer(tmp_path / "x.mp4")
    w.add(frames(1)[0])
    with pytest.raises(ValueError):
        w.add(frames(1, w=100, h=60)[0])
    w.abort()
    assert not (tmp_path / "x.mp4").exists()


def test_missing_ffmpeg_gives_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(export, "ffmpeg_exe", lambda: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        export.Mp4Writer(tmp_path / "x.mp4")


def test_names_and_frame_choice():
    from datetime import datetime, timezone
    a = datetime(2026, 10, 1, 21, 55, tzinfo=timezone.utc)
    b = datetime(2026, 10, 1, 22, 43, tzinfo=timezone.utc)
    assert export.default_name("KTLX", a, b, "gif") == "KTLX_20261001_2155-2243Z.gif"
    assert export.default_name("KTLX", a, a, "mp4") == "KTLX_20261001_2155Z.mp4"
    assert export.default_name("KTLX", None, None, "gif") == "KTLX.gif"
    assert export.pick_frames(10, 0) == list(range(10))
    assert export.pick_frames(10, 4) == [6, 7, 8, 9]
    assert export.pick_frames(3, 8) == [0, 1, 2]
    with pytest.raises(ValueError):
        export.writer_for("x.avi", 5, 1)
