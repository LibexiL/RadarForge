"""Where street cameras come from, and the free keys some sources need."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QScrollArea,
                               QVBoxLayout, QWidget)

from ..features.cameras import IBI_STATES


class CameraSourcesDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        s = main.settings
        self.setWindowTitle("Street camera sources")
        self.resize(620, 620)
        lay = QVBoxLayout(self)
        intro = QLabel("Cameras show when you zoom in to about 250 miles across or less; click one to see its "
                       "picture. California's cameras need nothing. Other states' 511 systems and Windy (webcams "
                       "everywhere, including many traffic cameras) give a free key when you sign up as a "
                       "developer – paste it here. Keys stay on this computer.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        inner = QWidget()
        form = QFormLayout(inner)
        self.caltrans = QCheckBox("California (Caltrans) – no key needed")
        self.caltrans.setChecked(bool(s["camera_caltrans"]))
        form.addRow(self.caltrans)
        keys = dict(s["camera_keys"] or {})
        self.edits = {}
        e = QLineEdit(keys.get("windy", ""))
        e.setPlaceholderText("Windy Webcams API key")
        self.edits["windy"] = e
        lab = QLabel("<a href='https://api.windy.com/keys'>Windy Webcams</a> (worldwide)")
        lab.setOpenExternalLinks(True)
        form.addRow(lab, e)
        for st, (name, host, _box) in IBI_STATES.items():
            e = QLineEdit(keys.get(st, ""))
            e.setPlaceholderText(f"{host} developer key")
            self.edits[st] = e
            lab = QLabel(f"<a href='https://{host}/developers/doc'>{name} 511</a>")
            lab.setOpenExternalLinks(True)
            lab.setToolTip(f"Sign up at https://{host}/developers/doc for a free key")
            form.addRow(lab, e)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        lay.addWidget(sa, 1)
        note = QLabel("Pictures come straight from each agency or Windy and follow their terms of use. "
                      "Windy's free plan gives small pictures.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9ab;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

    def accept(self):
        s = self.main.settings
        s["camera_caltrans"] = self.caltrans.isChecked()
        s["camera_keys"] = {k: e.text().strip() for k, e in self.edits.items() if e.text().strip()}
        s.save()
        super().accept()
