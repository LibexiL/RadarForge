"""Storm motion for SRV."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QPushButton


class StormMotionDialog(QDialog):
    def __init__(self, settings, auto_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Storm motion")
        self.s = settings
        self.dir = QDoubleSpinBox()
        self.dir.setRange(0, 359.9)
        self.dir.setSuffix(" ° (from)")
        self.dir.setValue(float(settings["storm_motion_dir"]))
        self.spd = QDoubleSpinBox()
        self.spd.setRange(0, 120)
        self.spd.setSuffix(" kt")
        self.spd.setValue(float(settings["storm_motion_kts"]))
        self.dealias = QCheckBox("Use dealiased velocity for SRV")
        self.dealias.setChecked(bool(settings["srv_use_dealiased"]))
        form = QFormLayout(self)
        form.addRow("Direction", self.dir)
        form.addRow("Speed", self.spd)
        form.addRow(self.dealias)
        if auto_fn is not None:
            b = QPushButton("Use mean motion from Level III storm tracks (NST)")
            lab = QLabel("")

            def auto():
                r = auto_fn()
                if r is None:
                    lab.setText("No storm-track (NST) product in the current frame.")
                else:
                    self.dir.setValue(round(r[0], 0))
                    self.spd.setValue(round(r[1], 0))
                    lab.setText(f"Mean cell motion: {r[0]:.0f}° at {r[1]:.0f} kt")
            b.clicked.connect(auto)
            form.addRow(b)
            form.addRow(lab)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _ok(self):
        self.s["storm_motion_dir"] = self.dir.value()
        self.s["storm_motion_kts"] = self.spd.value()
        self.s["srv_use_dealiased"] = self.dealias.isChecked()
        self.s.save()
        self.accept()
