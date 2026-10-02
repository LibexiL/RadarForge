"""Saving, copying and exporting pictures of the map: PNGs, GIF and MP4 loops."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog

from ... import __version__
from ...data import feeds
from ...data.sites import get_site
from ...products import catalog
from ...services import briefing, export
from ..dialogs import LoopExportDialog


class ExportMixin:
    """Saving, copying and exporting pictures of the map."""

    # ---------------------------------------------------------------- one picture
    def save_image(self):
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M%S}.png" if f else "radar.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save image", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            self.view.grab_png(path)
            self._status_msg(f"Saved {path}")

    def save_image_titled(self):
        """A PNG with a title band: radar, time and the products on show."""
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M%S}_titled.png" if f else "radar.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save image with title", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            self.titled_image(self.view.grabFramebuffer(), f).save(path)
            self._status_msg(f"Saved {path}")

    def copy_image(self):
        """The map as a picture on the clipboard (paste it into a chat, e-mail or document)."""
        QApplication.clipboard().setImage(self.view.grabFramebuffer())
        self._status_msg("Map picture copied – paste it anywhere")

    def export_title(self, frame):
        """(title, subtitle, footer) describing what the map shows."""
        site = self.data.site_id
        s = get_site(site)
        place = f" {s.place}" if s is not None else ""
        when = f" · {frame.time:%Y-%m-%d %H:%M:%S}Z" if frame is not None else ""
        names = " · ".join(catalog.get(p.product).name for p in self.view.panels)
        return f"{site}{place}{when}", names, f"RadarForge {__version__} · data: NOAA NEXRAD"

    def titled_image(self, img, frame):
        title, sub, foot = self.export_title(frame)
        return export.annotate(img, title, sub, foot, dark=self.view.bg.lightness() < 128)

    def briefing_lines(self) -> list:
        """What is happening in the area on screen, as a few lines for a briefing picture."""
        panel = self.view.panels[min(self.view.active_panel, len(self.view.panels) - 1)]
        x0, y0, x1, y1 = self.view.transform(panel).world_bounds()
        alerts = []
        for a in self.warnings.active_alerts():
            if not self.warnings.visible(a):
                continue
            if any(xy[:, 0].max() >= x0 and xy[:, 0].min() <= x1 and xy[:, 1].max() >= y0 and xy[:, 1].min() <= y1
                   for xy in self.warnings._xy(a, self.view.lat0, self.view.lon0)):
                alerts.append(a)
        mcds = [m for m in self.spc.mcds if self.spc._on("spc_mcd")]
        outlook = None
        me = self.my_location.latlon()
        if me is not None and self.spc.outlook:
            o = feeds.outlook_at(self.spc.outlook, *me)
            if o is not None:
                chances = " · ".join(f"{k} {v * 100:.0f}%" for k in ("tornado", "wind", "hail") if (v := o.get(k)) is not None)
                outlook = f"SPC outlook at {self.book.primary().name}: {feeds.CAT_NAME[o['cat']]}" + (f" ({chances})" if chances else "")
        flashes = None
        if self.lightning.enabled() and self.lightning.has_data:
            lat, lon = self._map_centre()
            flashes = self.lightning.counts_near(lat, lon, (x1 - x0) / 2.0)
        reports = None
        if self.settings["overlays"].get("reports"):
            reports = {}
            for r in self.warnings.visible_reports():
                if "xy" in r and x0 <= r["xy"][0] <= x1 and y0 <= r["xy"][1] <= y1:
                    reports[r["kind"]] = reports.get(r["kind"], 0) + 1
        return briefing.summary_lines(alerts, mcds, outlook, flashes, reports)

    def save_briefing_image(self):
        """The map with a summary of the warnings, discussions, outlook, lightning and reports in view."""
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M}_briefing.png" if f else "briefing.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save briefing image", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if not path:
            return
        title, _names, foot = self.export_title(f)
        lines = self.briefing_lines()
        export.annotate(self.view.grabFramebuffer(), title, "\n".join(lines), foot,
                        dark=self.view.bg.lightness() < 128).save(path)
        self._status_msg(f"Saved {path}")

    # ---------------------------------------------------------------- loops
    def export_loop(self, fmt="gif"):
        """Export the loaded frames as an animated GIF or an MP4 video."""
        frames = list(self.data.frames)
        if len(frames) < 2:
            QMessageBox.information(self, "Export loop",
                                    "There is only one frame loaded. Load some more (live data collects them as "
                                    "it arrives, or open an archive) and try again.")
            return
        name = export.default_name(self.data.site_id, frames[0].time, frames[-1].time, fmt)
        dlg = LoopExportDialog(self, self.settings, len(frames), name, fmt)
        if not dlg.exec():
            return
        opt = dlg.options()
        try:
            writer = export.writer_for(opt["path"], opt["fps"], opt["dwell"])
        except Exception as exc:
            QMessageBox.warning(self, "Export loop", str(exc))
            return
        picks = export.pick_frames(len(frames), opt["last_n"])
        was = (self.frame_index, self.follow_latest, self.playing)
        if self.playing:
            self.toggle_play()
        prog = QProgressDialog("Drawing frames…", "Cancel", 0, len(picks), self)
        prog.setWindowTitle("Export loop")
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.show()
        error = None
        try:
            for k, i in enumerate(picks):
                prog.setValue(k)
                prog.setLabelText(f"Drawing frame {k + 1} of {len(picks)}…")
                QApplication.processEvents()
                if prog.wasCanceled():
                    raise InterruptedError
                self.goto_frame(i)
                if not self._wait_for_panels():
                    raise TimeoutError("a frame took too long to draw")
                img = self.view.grabFramebuffer()
                if opt["title"]:
                    img = self.titled_image(img, frames[i])
                writer.add(export.to_rgb(export.scaled_to_width(img, opt["width"])))
            prog.setLabelText("Writing the file…")
            QApplication.processEvents()
            writer.save()
        except InterruptedError:
            writer.abort()
            error = ""
        except Exception as exc:
            writer.abort()
            error = str(exc) or type(exc).__name__
        finally:
            prog.close()
            self.goto_frame(was[0])
            self.follow_latest = was[1]
            if was[2] and not self.playing:
                self.toggle_play()
        if error:
            QMessageBox.warning(self, "Export loop", f"The loop could not be exported:\n{error}")
        elif error is None:
            size = os.path.getsize(opt["path"]) / 1e6
            self._status_msg(f"Saved {opt['path']} ({len(picks)} frames, {size:.1f} MB)")

    def _wait_for_panels(self, timeout=40.0) -> bool:
        """Lets the event loop run until every panel has drawn its frame (False on timeout)."""
        end = time.monotonic() + timeout
        while not self.panels_idle():
            if time.monotonic() > end:
                return False
            QApplication.processEvents(QEventLoop.AllEvents, 30)
            time.sleep(0.01)
        self.view.update()
        QApplication.processEvents()
        return True
