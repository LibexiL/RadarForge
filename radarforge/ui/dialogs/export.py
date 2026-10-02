"""Export a loop (GIF or MP4) of the frames that are loaded."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QSpinBox,
                               QVBoxLayout)

from ...services import export


class LoopExportDialog(QDialog):
    """Asks how to export the loop; `options()` has the answers once it is accepted."""

    def __init__(self, parent, settings, frame_count: int, default_name: str, fmt: str = "gif"):
        super().__init__(parent)
        self.setWindowTitle("Export loop")
        self.setMinimumWidth(460)
        self.settings = settings
        self.count = frame_count
        saved = dict(settings["export"] or {})
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        self.format = QComboBox()
        self.format.addItem("Animated GIF (.gif) – plays anywhere, big files", "gif")
        self.format.addItem("MP4 video (.mp4) – small files, sharp", "mp4")
        self.format.setCurrentIndex(1 if (fmt or saved.get("format")) == "mp4" else 0)
        self.format.currentIndexChanged.connect(self._format_changed)
        form.addRow("Format", self.format)

        self.all = QRadioButton(f"All {frame_count} frames")
        self.last = QRadioButton("Only the last")
        self.last_n = QSpinBox()
        self.last_n.setRange(2, max(2, frame_count))
        self.last_n.setValue(min(max(2, int(saved.get("last_n", 12))), max(2, frame_count)))
        self.last_n.setSuffix(" frames")
        row = QHBoxLayout()
        row.addWidget(self.last)
        row.addWidget(self.last_n)
        row.addStretch(1)
        box = QVBoxLayout()
        box.addWidget(self.all)
        box.addLayout(row)
        form.addRow("Frames", box)
        (self.last if saved.get("last") else self.all).setChecked(True)
        self.last_n.setEnabled(self.last.isChecked())
        self.last.toggled.connect(self.last_n.setEnabled)

        self.fps = QDoubleSpinBox()
        self.fps.setRange(0.5, 30.0)
        self.fps.setDecimals(1)
        self.fps.setSuffix(" frames/s")
        self.fps.setValue(float(saved.get("fps", settings["loop_fps"] or 6.0)))
        form.addRow("Speed", self.fps)
        self.dwell = QDoubleSpinBox()
        self.dwell.setRange(0.0, 10.0)
        self.dwell.setDecimals(1)
        self.dwell.setSuffix(" s")
        self.dwell.setValue(float(saved.get("dwell", settings["loop_dwell"] or 1.5)))
        form.addRow("Hold the last frame", self.dwell)

        self.size = QComboBox()
        for label, width in export.MAX_SIZES:
            self.size.addItem(label, width)
        i = self.size.findData(int(saved.get("width", 1280)))
        self.size.setCurrentIndex(i if i >= 0 else 2)
        form.addRow("Size", self.size)
        self.title = QCheckBox("Add a title bar with the radar, time and products")
        self.title.setChecked(bool(saved.get("title", True)))
        form.addRow("", self.title)

        self.path = QLineEdit(os.path.join(saved.get("folder") or os.path.expanduser("~"), default_name))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("Save as", row)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setProperty("role", "hint")
        lay.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._format_changed()

    def _format_changed(self):
        ext = self.format.currentData()
        base, _old = os.path.splitext(self.path.text())
        self.path.setText(f"{base}.{ext}")
        if ext == "mp4" and export.ffmpeg_exe() is None:
            self.hint.setText("MP4 needs ffmpeg, which isn't installed – choose GIF, or reinstall RadarForge.")
        else:
            self.hint.setText("Each frame is drawn exactly as it looks in the window, so arrange the panels, "
                              "zoom and layers first." if ext == "gif" else
                              "Each frame is drawn exactly as it looks in the window.")

    def _browse(self):
        ext = self.format.currentData()
        path, _ = QFileDialog.getSaveFileName(self, "Export loop", self.path.text(),
                                              "GIF (*.gif)" if ext == "gif" else "MP4 video (*.mp4)")
        if path:
            self.path.setText(path)

    def _ok(self):
        path = self.path.text().strip()
        if not path:
            return
        folder = os.path.dirname(path) or "."
        if not os.path.isdir(folder):
            self.hint.setText(f"The folder {folder} doesn't exist.")
            return
        o = self.options()
        self.settings["export"] = {"format": o["format"], "last": o["last_n"] > 0, "last_n": self.last_n.value(),
                                   "fps": o["fps"], "dwell": o["dwell"], "width": o["width"], "title": o["title"],
                                   "folder": folder}
        self.settings.save()
        self.accept()

    def options(self) -> dict:
        ext = self.format.currentData()
        path = self.path.text().strip()
        if not path.lower().endswith("." + ext):
            path = f"{os.path.splitext(path)[0]}.{ext}"
        return {"path": path, "format": ext, "last_n": self.last_n.value() if self.last.isChecked() else 0,
                "fps": self.fps.value(), "dwell": self.dwell.value(), "width": self.size.currentData(),
                "title": self.title.isChecked()}
