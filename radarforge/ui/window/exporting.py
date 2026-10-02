"""Saving and copying pictures of the map."""
from __future__ import annotations

import os

from PySide6.QtWidgets import QApplication, QFileDialog


class ExportMixin:
    """Saving and copying pictures of the map."""

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
        QApplication.clipboard().setImage(self.view.grabFramebuffer())
        self._status_msg("Map picture copied – paste it anywhere")
