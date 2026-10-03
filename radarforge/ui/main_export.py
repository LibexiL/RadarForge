"""Pictures and animations of the map."""
from __future__ import annotations

import os

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .export import BriefingDialog, LoopExportDialog, LoopRecorder, annotate, grab_map


class ExportMixin:
    """Part of MainWindow: pictures and animations of the map."""

    def save_image(self):
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M%S}.png" if f else "radar.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save image", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            self.view.grab_png(path)
            self._status_msg(f"Saved {path}")

    def copy_image(self):
        """The map as a picture on the clipboard (paste it into a chat, e-mail or document)."""
        QApplication.clipboard().setImage(self.view.grab_fresh())
        self._status_msg("Map picture copied – paste it anywhere")

    def save_image_annotated(self):
        """PNG with colour bars, a title bar (radar, time) and a details bar (products, warnings)."""
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M%S}_annotated.png" if f else "radar.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save image with legend", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            annotate(self, grab_map(self, legend=True)).save(path)
            self._status_msg(f"Saved {path}")

    def panels_ready(self) -> bool:
        """True once every panel shows the image for the current frame (used while recording a loop)."""
        return all(self._panel_done.get(p.index) == self._panel_req.get(p.index) for p in self.view.panels)

    def export_loop(self):
        if len(self.data.frames) < 2:
            QMessageBox.information(self, "Export loop", "Load at least two frames first (live data, or Archive).")
            return
        d = LoopExportDialog(self)
        if not d.exec():
            return
        fmt = d.fmt.currentData()
        opts = dict(format=fmt, first=d.first.value() - 1, last=max(d.first.value(), d.last.value()) - 1,
                    fps=d.fps.value(), dwell=d.dwell.value(), scale=d.scale.currentData(),
                    annotate=d.annot.isChecked(), legend=d.legend.isChecked())
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M}_loop.{fmt}" if f else f"radar_loop.{fmt}"
        path, _ = QFileDialog.getSaveFileName(self, "Export loop", os.path.join(os.path.expanduser("~"), name),
                                              "MP4 video (*.mp4)" if fmt == "mp4" else "Animated GIF (*.gif)")
        if not path:
            return
        if not path.lower().endswith("." + fmt):
            path += "." + fmt
        self._loop_rec = LoopRecorder(self, opts, path)
        self._loop_rec.start()

    def open_briefing(self):
        dlg = getattr(self, "_briefing", None)
        if dlg is None or not dlg.isVisible():
            self._briefing = dlg = BriefingDialog(self)
        dlg.show()
        dlg.raise_()
