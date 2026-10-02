"""Frames, the loop and tilts."""
from __future__ import annotations

import numpy as np

from .. import icons
from ...data.sites import get_site
from .constants import L3_TILT_ELEVS


class TimelineMixin:
    """Frames, the loop and tilts."""

    def current_frame(self):
        fr = self.data.frames
        if not fr:
            return None
        i = self.frame_index if 0 <= self.frame_index < len(fr) else len(fr) - 1
        return fr[i]

    def _frames_changed(self):
        if not self._frames_timer.isActive():
            self._frames_timer.start()

    def _apply_frames_changed(self):
        frames = self.data.frames
        n = len(frames)
        if n and self.data.site_id and get_site(self.data.site_id) and \
                self.view.site_id != self.data.site_id:
            self._set_site_projection(self.data.site_id)
        if self.follow_latest or self._shown_frame is None or self._shown_frame not in frames:
            self.frame_index = n - 1 if (self.follow_latest or self._shown_frame is None) else \
                max(0, min(self.frame_index, n - 1))
        else:
            # keep showing the same volume while others are still arriving
            self.frame_index = frames.index(self._shown_frame)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, max(0, n - 1))
        self.frame_slider.setValue(max(0, self.frame_index))
        self.frame_slider.blockSignals(False)
        if self.current_frame() is not self._shown_frame:
            self._show_frame()
        else:
            self._update_time_label()
        self._apply_pending_goto()
        if not self.data.loading:
            self._prefetch()
            self.update_sky_layers(prefetch=True)

    def goto_time_when_loaded(self, t):
        """Show the frame closest to t as soon as frames have loaded (a bookmark that opens an archive time)."""
        self._goto_time = t
        self._apply_pending_goto()

    def _apply_pending_goto(self):
        t = getattr(self, "_goto_time", None)
        frames = self.data.frames
        if t is None or not frames:
            return
        best = min(range(len(frames)), key=lambda i: abs((frames[i].time - t).total_seconds()))
        if best != self.frame_index:
            self.goto_frame(best)
        if not self.data.loading:
            self._goto_time = None

    def _frame_updated(self, frame):
        if frame is self.current_frame() and not self._update_timer.isActive():
            self._update_timer.start()

    def _loading_changed(self, busy):
        if not busy:
            self._prefetch()

    def _update_time_label(self):
        frame = self.current_frame()
        if frame is None:
            self.time_label.setText(" no data ")
            return
        n = len(self.data.frames)
        live = " LIVE" if frame.live else ""
        self.time_label.setText(f" {frame.time:%Y-%m-%d %H:%M:%S}Z  [{self.frame_index + 1}/{n}]{live} ")

    def goto_frame(self, i):
        n = len(self.data.frames)
        if n == 0:
            return
        i = max(0, min(n - 1, i))
        self.frame_index = i
        self.follow_latest = (i == n - 1) and self.data.mode == "live"
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(i)
        self.frame_slider.blockSignals(False)
        self._show_frame()

    def step_frame(self, d):
        n = len(self.data.frames)
        if n:
            self.goto_frame((self.frame_index + d) % n)

    def _slider_moved(self, v):
        self.goto_frame(v)

    def toggle_play(self):
        self.playing = not self.playing
        self.play_act.setIcon(icons.icon("pause" if self.playing else "play"))
        self.play_act.setToolTip("Pause loop (Space)" if self.playing else "Play loop (Space)")
        if self.playing:
            self._prefetch()
            self.play_timer.start(int(1000 / max(0.5, float(self.settings["loop_fps"]))))
        else:
            self.play_timer.stop()

    def _play_step(self):
        n = len(self.data.frames)
        if n < 2:
            return
        nxt = (self.frame_index + 1) % n
        if not self._frame_ready(self.data.frames[nxt]) and getattr(self, "_wait", 0) < 8:
            self._wait = getattr(self, "_wait", 0) + 1
            return
        self._wait = 0
        self.goto_frame(nxt)
        if nxt == n - 1:
            self.play_timer.setInterval(int(1000 * (1.0 / float(self.settings["loop_fps"]) +
                                                    float(self.settings["loop_dwell"]))))
        else:
            self.play_timer.setInterval(int(1000 / max(0.5, float(self.settings["loop_fps"]))))

    def _tilt_index(self, frame):
        if frame is None:
            return 0
        if frame.has_level2():
            tilts = self.engine.tilts(frame)
            if tilts:
                els = np.array([t.elevation for t in tilts])
                return int(np.argmin(np.abs(els - self.tilt_elev)))
        return int(np.argmin(np.abs(np.array(L3_TILT_ELEVS) - self.tilt_elev)))

    def step_tilt(self, d):
        frame = self.current_frame()
        els = self._tilt_elevs(frame)
        if not els:
            return
        i = int(np.argmin(np.abs(np.array(els) - self.tilt_elev)))
        i = max(0, min(len(els) - 1, i + d))
        self.tilt_elev = els[i]
        self._show_frame()
        self._prefetch()

    def _tilt_elevs(self, frame):
        if frame is None:
            return []
        if frame.has_level2():
            k = (frame.uid, frame.l2_rev)
            t = self.engine._tilt_cache.get(k)
            if t is not None:
                return [x.elevation for x in t]
            return []
        return L3_TILT_ELEVS

    def _tilt_chosen(self, idx):
        els = self._tilt_elevs(self.current_frame())
        if 0 <= idx < len(els):
            self.tilt_elev = els[idx]
            self._show_frame()
            self._prefetch()

    def _fill_tilt_combo(self, frame):
        frame = frame or self.current_frame()
        labels = []
        if frame is not None and frame.has_level2():
            t = self.engine._tilt_cache.get((frame.uid, frame.l2_rev))
            if t:
                labels = [x.label for x in t]
        elif frame is not None:
            labels = [f"{e:.1f}° (L3)" for e in L3_TILT_ELEVS]
        self.tilt_combo.blockSignals(True)
        self.tilt_combo.clear()
        self.tilt_combo.addItems(labels)
        els = self._tilt_elevs(frame)
        if els:
            self.tilt_combo.setCurrentIndex(int(np.argmin(np.abs(np.array(els) - self.tilt_elev))))
        self.tilt_combo.blockSignals(False)
