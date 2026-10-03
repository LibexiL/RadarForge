"""Exports: annotated pictures, loop animations (GIF / MP4) and the briefing view."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QProgressDialog, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ..features import feeds
from ..products import catalog


# --------------------------------------------------------------------------- encoding (no Qt widgets)
def qimage_to_rgb(img: QImage) -> np.ndarray:
    """H x W x 3 uint8 copy of a QImage."""
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    arr = np.frombuffer(img.constBits(), np.uint8, count=bpl * h).reshape(h, bpl)
    return arr[:, : w * 3].reshape(h, w, 3).copy()


def encode_gif(frames: list, durations_ms: list, path: str):
    """Animated GIF (loops forever). frames: H x W x 3 arrays; one duration per frame."""
    from PIL import Image
    imgs = [Image.fromarray(f).quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            for f in frames]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=[int(d) for d in durations_ms], loop=0,
                 optimize=False, disposal=1)


def mp4_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("imageio_ffmpeg") is not None


def encode_mp4(frames: list, durations_ms: list, path: str, fps: float):
    """H.264 MP4. Frame durations are made by repeating frames at `fps`."""
    import imageio.v2 as imageio
    h, w = frames[0].shape[:2]
    h2, w2 = h - h % 2, w - w % 2                 # H.264 needs even sizes
    writer = imageio.get_writer(path, fps=fps, codec="libx264", quality=8, macro_block_size=1,
                                pixelformat="yuv420p")
    try:
        for f, d in zip(frames, durations_ms):
            for _ in range(max(1, round(d / 1000.0 * fps))):
                writer.append_data(f[:h2, :w2])
    finally:
        writer.close()


def durations(n: int, fps: float, dwell_s: float) -> list:
    """Milliseconds per frame: 1/fps each, plus the dwell on the last one."""
    step = 1000.0 / max(0.5, fps)
    out = [step] * n
    if n:
        out[-1] += dwell_s * 1000.0
    return out


# --------------------------------------------------------------------------- annotated picture
def _local(t: datetime) -> str:
    lt = t.astimezone()
    return f"{lt:%a %b %d, %I:%M %p %Z}".replace(" 0", " ")


def annotate(main, img: QImage) -> QImage:
    """The map picture with a title bar (radar, time) and a details bar (products, storm motion, warnings)."""
    from ..data.sites import get_site
    dpr = img.devicePixelRatio() or 1.0
    w = img.width()
    f_title = QFont(main.font())
    f_title.setPixelSize(int(17 * dpr))
    f_title.setBold(True)
    f_small = QFont(main.font())
    f_small.setPixelSize(int(13 * dpr))
    head_h, foot_h = int(40 * dpr), int(46 * dpr)
    out = QImage(w, img.height() + head_h + foot_h, QImage.Format_RGB32)
    out.fill(QColor(24, 25, 30))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    frame = main.current_frame()
    s = get_site(main.data.site_id)
    title = f"{s.id}  {s.place}, {s.state}" if s else main.data.site_id
    when = ""
    if frame is not None and frame.time is not None:
        when = f"{frame.time:%Y-%m-%d %H:%M:%S}Z  ·  {_local(frame.time)}"
    pad = 12 * dpr
    p.setPen(QColor(235, 236, 240))
    p.setFont(f_title)
    p.drawText(QRectF(pad, 0, w / 2, head_h), Qt.AlignVCenter | Qt.AlignLeft, title)
    p.setFont(f_small)
    p.setPen(QColor(200, 202, 210))
    p.drawText(QRectF(w / 2, 0, w / 2 - pad, head_h), Qt.AlignVCenter | Qt.AlignRight, when)
    p.drawImage(0, head_h, img)
    # details
    prods = "   ".join(f"{i + 1}. {catalog.get(pn.product).name}" for i, pn in enumerate(main.view.panels))
    sm = f"Storm motion {main.settings['storm_motion_dir']:03.0f}° / {main.settings['storm_motion_kts']:.0f} kt"
    warns = [a for a in main.warnings.active_alerts() if main.warnings.visible(a) and not a.event.endswith("Watch")]
    tor = sum(1 for a in warns if a.event.startswith("Tornado"))
    wtxt = f"{len(warns)} warnings shown" + (f" ({tor} tornado)" if tor else "") if main.settings["overlays"].get(
        "warnings") else ""
    y0 = head_h + img.height()
    p.setPen(QColor(205, 207, 215))
    p.drawText(QRectF(pad, y0 + 3 * dpr, w - 2 * pad, foot_h / 2), Qt.AlignVCenter | Qt.AlignLeft, prods)
    p.setPen(QColor(150, 153, 163))
    p.drawText(QRectF(pad, y0 + foot_h / 2 - 2 * dpr, w - 2 * pad, foot_h / 2), Qt.AlignVCenter | Qt.AlignLeft,
               "   ·   ".join(x for x in (sm, wtxt) if x))
    p.drawText(QRectF(pad, y0 + foot_h / 2 - 2 * dpr, w - 2 * pad, foot_h / 2), Qt.AlignVCenter | Qt.AlignRight,
               "RadarForge  ·  not an official warning source")
    p.end()
    return out


def grab_map(main, legend=True) -> QImage:
    """The map as drawn now, with colour bars forced on when legend=True."""
    view = main.view
    old = view.show_legend
    if legend and not old:
        view.show_legend = True
        view.update()
    try:
        return view.grab_fresh()
    finally:
        if legend and not old:
            view.show_legend = old
            view.update()


# --------------------------------------------------------------------------- loop export
class LoopExportDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle("Export loop")
        n = len(main.data.frames)
        self.fmt = QComboBox()
        self.fmt.addItem("Animated GIF (plays anywhere, bigger file)", "gif")
        self.fmt.addItem("MP4 video (small, best for sharing)" if mp4_available() else
                         "MP4 video – needs the imageio-ffmpeg package", "mp4")
        if not mp4_available():
            self.fmt.model().item(1).setEnabled(False)
        self.first = QSpinBox()
        self.first.setRange(1, max(1, n))
        self.first.setValue(1)
        self.last = QSpinBox()
        self.last.setRange(1, max(1, n))
        self.last.setValue(n)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(0.5, 30)
        self.fps.setValue(float(main.settings["loop_fps"]))
        self.fps.setSuffix(" frames / s")
        self.dwell = QDoubleSpinBox()
        self.dwell.setRange(0, 10)
        self.dwell.setValue(float(main.settings["loop_dwell"]))
        self.dwell.setSuffix(" s on the last frame")
        self.scale = QComboBox()
        for pct in (100, 75, 50):
            self.scale.addItem(f"{pct}%", pct)
        self.scale.setCurrentIndex(1)
        self.annot = QCheckBox("Add the title and details bars")
        self.annot.setChecked(True)
        self.legend = QCheckBox("Show the colour bars")
        self.legend.setChecked(True)
        form = QFormLayout()
        form.addRow("Format", self.fmt)
        rng = QHBoxLayout()
        rng.addWidget(self.first)
        rng.addWidget(QLabel("to"))
        rng.addWidget(self.last)
        rng.addWidget(QLabel(f"of {n}"))
        rng.addStretch(1)
        form.addRow("Frames", rng)
        form.addRow("Speed", self.fps)
        form.addRow("Pause", self.dwell)
        form.addRow("Size", self.scale)
        form.addRow("", self.annot)
        form.addRow("", self.legend)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Every panel and overlay is recorded as you see it now (layout, zoom, layers)."))
        lay.addLayout(form)
        lay.addWidget(bb)


class LoopRecorder:
    """Steps through the frames, waits until every panel has drawn, grabs each, then encodes in a thread."""

    def __init__(self, main, opts: dict, path: str):
        self.main, self.opts, self.path = main, opts, path
        self.indices = list(range(opts["first"], opts["last"] + 1))
        self.images: list = []
        self.k = 0
        self.wait_ms = 0
        self.late = 0
        self.paint_target = None
        self.cancelled = False
        self.prog = QProgressDialog("Recording frames…", "Cancel", 0, len(self.indices) + 1, main)
        self.prog.setWindowTitle("Export loop")
        self.prog.setMinimumDuration(0)
        self.prog.canceled.connect(self._cancel)
        self.start_index = main.frame_index
        self.was_playing = main.playing
        if main.playing:
            main.toggle_play()
        self.timer = QTimer(main)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._tick)

    def start(self):
        self.main.goto_frame(self.indices[0])
        self.timer.start()

    def _cancel(self):
        self.cancelled = True

    def _finish_ui(self):
        self.timer.stop()
        self.main.goto_frame(self.start_index)

    def _tick(self):
        m = self.main
        if self.cancelled:
            self._finish_ui()
            m._status_msg("Loop export cancelled")
            return
        self.wait_ms += self.timer.interval()
        if not m.panels_ready() and self.wait_ms < 120_000:
            if self.wait_ms > 1500:
                self.prog.setLabelText(f"Drawing frame {self.k + 1} of {len(self.indices)}… (decoding radar data)")
            return
        if not m.panels_ready():
            self.late += 1                    # gave up waiting: this frame may show the previous image
        # the window's last paint can predate the newest panel image: ask for a paint and grab after it
        if self.paint_target is None:
            self.paint_target = m.view.paint_serial
            m.view.update()
            return
        if m.view.paint_serial <= self.paint_target and self.wait_ms < 123_000:
            return
        self.paint_target = None
        self.wait_ms = 0
        self.prog.setLabelText("Recording frames…")
        img = grab_map(m, self.opts["legend"])
        if self.opts["annotate"]:
            img = annotate(m, img)
        pct = self.opts["scale"]
        if pct != 100:
            img = img.scaledToWidth(int(img.width() * pct / 100), Qt.SmoothTransformation)
        self.images.append(qimage_to_rgb(img))
        self.k += 1
        self.prog.setValue(self.k)
        if self.k < len(self.indices):
            m.goto_frame(self.indices[self.k])
            return
        self._finish_ui()
        self.prog.setLabelText("Encoding…")
        self.prog.setCancelButton(None)
        self._encode()

    def _encode(self):
        o = self.opts
        durs = durations(len(self.images), o["fps"], o["dwell"])
        result = {}

        def work():
            try:
                if o["format"] == "mp4":
                    encode_mp4(self.images, durs, self.path, max(o["fps"], 10.0))
                else:
                    encode_gif(self.images, durs, self.path)
                result["ok"] = True
            except Exception as exc:
                result["err"] = str(exc)
        th = threading.Thread(target=work, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                QTimer.singleShot(150, poll)
                return
            self.prog.setValue(self.prog.maximum())
            self.prog.close()
            if "err" in result:
                self.main._status_msg(f"Loop export failed: {result['err']}")
            else:
                note = f" ({self.late} frame(s) took too long to draw and may repeat)" if self.late else ""
                self.main._status_msg(f"Saved {self.path}{note}")
        poll()


# --------------------------------------------------------------------------- briefing view
class BriefingDialog(QDialog):
    """A clean picture-and-summary page: the map, plus warnings, reports, SPC and storm motion beside it.
    It follows the frame you're on; save it as a PNG for a briefing or a post."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Briefing view")
        self.resize(1400, 860)
        self.map = QLabel()
        self.map.setAlignment(Qt.AlignCenter)
        self.map.setMinimumSize(640, 480)
        self.map.setStyleSheet("background:#101116;")
        self.side = QLabel()
        self.side.setTextFormat(Qt.RichText)
        self.side.setWordWrap(True)
        self.side.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.side.setFixedWidth(360)
        self.side.setStyleSheet("padding:12px; background:#1b1c21; color:#e1e1e6;")
        self.page = QWidget()
        pl = QHBoxLayout(self.page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.map, 1)
        pl.addWidget(self.side)
        save = QPushButton("Save as PNG…")
        save.clicked.connect(self.save)
        copy = QPushButton("Copy")
        copy.clicked.connect(self.copy)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(QLabel("Updates with the frame shown in the main window."))
        row.addStretch(1)
        for b in (ref, copy, save):
            row.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(self.page, 1)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        main.stateChanged.connect(self._timer.start)
        main.warnings.changed.connect(self._timer.start)
        QTimer.singleShot(50, self.refresh)

    def _summary(self) -> str:
        m = self.main
        from ..data.sites import get_site
        s = get_site(m.data.site_id)
        frame = m.current_frame()
        now = datetime.now(timezone.utc)
        h = [f"<div style='font-size:20px;font-weight:bold'>{s.id if s else ''} – {s.place + ', ' + s.state if s else ''}</div>"]
        if frame is not None and frame.time is not None:
            h.append(f"<div style='color:#b9bcc6'>{frame.time:%Y-%m-%d %H:%M}Z · {_local(frame.time)}</div>")
        # warnings in view
        vt = m.view.transform(m.view.panels[0])
        x0, y0, x1, y1 = vt.world_bounds()
        warns = []
        for a in m.warnings.active_alerts():
            if not m.warnings.visible(a):
                continue
            xy = m.warnings._xy(a, m.view.lat0, m.view.lon0)
            if any(r[:, 0].max() >= x0 and r[:, 0].min() <= x1 and r[:, 1].max() >= y0 and r[:, 1].min() <= y1
                   for r in xy):
                warns.append(a)
        warns.sort(key=lambda a: -a.style[3])
        h.append("<h3 style='margin-bottom:4px'>Warnings in view</h3>")
        if not m.settings["overlays"].get("warnings"):
            h.append("<div style='color:#8d909b'>Warnings are switched off.</div>")
        elif not warns:
            h.append("<div style='color:#8d909b'>None.</div>")
        for a in warns[:12]:
            rgb = m.warnings.color(a)
            left = ""
            if a.expires is not None and m.data.mode == "live":
                mins = int((a.expires - now).total_seconds() // 60)
                left = f" · {mins} min left" if mins >= 0 else ""
            tags = (" · " + ", ".join(a.tags[:2])) if a.tags else ""
            h.append(f"<div style='margin:2px 0'><span style='color:rgb{rgb}'>■</span> <b>{a.variant_label}</b>"
                     f" <span style='color:#b9bcc6'>{a.office}{left}{tags}</span></div>")
        if len(warns) > 12:
            h.append(f"<div style='color:#8d909b'>…and {len(warns) - 12} more</div>")
        # storm reports
        if m.settings["overlays"].get("reports"):
            reps = m.warnings.visible_reports()
            counts = {}
            for r in reps:
                lab = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])[2]
                counts[lab] = counts.get(lab, 0) + 1
            h.append("<h3 style='margin-bottom:4px'>Storm reports</h3>")
            h.append("<div>" + (", ".join(f"{v} {k.lower()}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
                                or "<span style='color:#8d909b'>None.</span>") + "</div>")
        # SPC at the view centre
        lat, lon = m.view.world_to_latlon(m.view.cx, m.view.cy)
        if m.settings["overlays"].get("spc_outlook"):
            o = m.spc.outlook_at(lat, lon)
            h.append("<h3 style='margin-bottom:4px'>SPC day 1 outlook (map centre)</h3>")
            if o is None:
                h.append("<div style='color:#8d909b'>No thunderstorm risk.</div>")
            else:
                rgb = feeds.CAT_RGB[o["cat"]]
                h.append(f"<div><span style='color:rgb{rgb}'>■</span> <b>{feeds.CAT_NAME[o['cat']]}</b></div>")
        mcd = m.spc.mcd_at(lat, lon)
        if mcd is not None:
            h.append(f"<div style='margin-top:6px'>Mesoscale discussion {mcd['number']}"
                     + (f" – watch {mcd['watch']}%" if mcd["watch"] is not None else "") + "</div>")
        # motion and products
        h.append("<h3 style='margin-bottom:4px'>Radar</h3>")
        h.append("<div>" + "<br>".join(f"{i + 1}. {catalog.get(p.product).name}" for i, p in enumerate(m.view.panels))
                 + "</div>")
        h.append(f"<div style='color:#b9bcc6;margin-top:4px'>Storm motion {m.settings['storm_motion_dir']:03.0f}° / "
                 f"{m.settings['storm_motion_kts']:.0f} kt</div>")
        if m.view.track is not None and m.view.track["etas"]:
            h.append("<h3 style='margin-bottom:4px'>Storm track arrivals</h3>")
            from datetime import timedelta
            for name, mins, _x, _y in m.view.track["etas"][:6]:
                h.append(f"<div>{name}: {feeds.local_hm(m.view.track['start'] + timedelta(minutes=mins))}</div>")
        h.append("<div style='color:#6f7280;margin-top:14px;font-size:11px'>RadarForge – not an official warning "
                 "source. Follow the National Weather Service.</div>")
        return "".join(h)

    def refresh(self):
        if not self.isVisible():
            return
        img = grab_map(self.main, legend=True)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(1.0)
        self.map.setPixmap(pm.scaled(self.map.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.side.setText(self._summary())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._timer.start()

    def save(self):
        f = self.main.current_frame()
        name = f"briefing_{self.main.data.site_id}_{f.time:%Y%m%d_%H%M}.png" if f else "briefing.png"
        import os
        path, _ = QFileDialog.getSaveFileName(self, "Save briefing", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            self.page.grab().save(path)
            self.main._status_msg(f"Saved {path}")

    def copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setPixmap(self.page.grab())
        self.main._status_msg("Briefing copied – paste it anywhere")

    def done(self, r):
        try:
            self.main.stateChanged.disconnect(self._timer.start)
            self.main.warnings.changed.disconnect(self._timer.start)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
