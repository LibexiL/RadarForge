"""Saved locations and their alert rules."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ..features import alerts


class LocationsDialog(QDialog):
    def __init__(self, main, select_id=None):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Saved locations & alerts")
        self.resize(860, 660)
        s = main.settings
        self.locs = [alerts.normalise(loc) for loc in (s["saved_locations"] or [])]
        self._cur = -1
        lay = QVBoxLayout(self)
        intro = QLabel("Places you care about. With live data RadarForge watches each one and alerts you when a "
                       "warning covers it, a storm report comes in nearby or lightning gets close. "
                       "\"My location\" follows the blue dot (Location → Set my location).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Add map centre", self._add_center), ("Add…", self._add_typed), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        left.addLayout(row)
        go = QPushButton("Show on map")
        go.clicked.connect(self._go)
        left.addWidget(go)
        body.addLayout(left, 2)

        form_w = QWidget()
        form = QVBoxLayout(form_w)
        form.setContentsMargins(0, 0, 0, 0)
        place = QGroupBox("Place")
        pf = QFormLayout(place)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._edited)
        pf.addRow("Name", self.name)
        self.lat = QDoubleSpinBox()
        self.lat.setRange(-90, 90)
        self.lat.setDecimals(4)
        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(4)
        for sp in (self.lat, self.lon):
            sp.valueChanged.connect(self._edited)
        pf.addRow("Latitude", self.lat)
        pf.addRow("Longitude", self.lon)
        form.addWidget(place)

        rules = QGroupBox("Alert me when…")
        rf = QVBoxLayout(rules)
        self.enabled = QCheckBox("Alerts on for this place")
        rf.addWidget(self.enabled)
        wrow = QHBoxLayout()
        wrow.addWidget(QLabel("a warning covers it:"))
        self.warn = {}
        for k, label in (("TOR", "Tornado"), ("SVR", "Severe"), ("FFW", "Flash flood"), ("OTH", "Other")):
            cb = QCheckBox(label)
            self.warn[k] = cb
            wrow.addWidget(cb)
        wrow.addStretch(1)
        rf.addLayout(wrow)
        self.watches = QCheckBox("a tornado or severe thunderstorm watch covers it")
        rf.addWidget(self.watches)
        rrow = QHBoxLayout()
        self.reports = QCheckBox("a storm report comes in within")
        self.report_mi = QSpinBox()
        self.report_mi.setRange(1, 100)
        self.report_mi.setSuffix(" mi")
        rrow.addWidget(self.reports)
        rrow.addWidget(self.report_mi)
        rrow.addStretch(1)
        rf.addLayout(rrow)
        lrow = QHBoxLayout()
        self.lightning = QCheckBox("lightning strikes within")
        self.ltg_mi = QSpinBox()
        self.ltg_mi.setRange(1, 60)
        self.ltg_mi.setSuffix(" mi")
        lrow.addWidget(self.lightning)
        lrow.addWidget(self.ltg_mi)
        lrow.addWidget(QLabel("(GOES lightning mapper)"))
        lrow.addStretch(1)
        rf.addLayout(lrow)
        form.addWidget(rules)

        how = QGroupBox("How")
        hf = QHBoxLayout(how)
        self.sound = QCheckBox("Sound")
        self.desktop = QCheckBox("Desktop notification")
        self.popup = QCheckBox("Pop-up window")
        for cb in (self.sound, self.desktop, self.popup):
            hf.addWidget(cb)
        hf.addStretch(1)
        form.addWidget(how)
        for cb in [self.enabled, self.watches, self.reports, self.lightning, self.sound, self.desktop, self.popup,
                   *self.warn.values()]:
            cb.toggled.connect(self._edited)
        for sp in (self.report_mi, self.ltg_mi):
            sp.valueChanged.connect(self._edited)

        snd = QGroupBox("Alert sound (all places)")
        sf = QHBoxLayout(snd)
        self.sound_name = QComboBox()
        for k, label in alerts.SOUNDS.items():
            self.sound_name.addItem(label, k)
        self.sound_name.setCurrentIndex(max(0, self.sound_name.findData(s["alert_sound"] or "chime")))
        sf.addWidget(self.sound_name)
        sf.addWidget(QLabel("Volume"))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(round(float(s["alert_volume"] or 0.8) * 100)))
        sf.addWidget(self.volume)
        test = QPushButton("Test")
        test.clicked.connect(self._test)
        sf.addWidget(test)
        form.addWidget(snd)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#9ab;")
        form.addWidget(self.summary)
        form.addStretch(1)
        body.addWidget(form_w, 3)
        lay.addLayout(body, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._loading = False
        self._fill()
        idx = next((i for i, loc in enumerate(self.locs) if loc.get("id") == select_id), 0)
        if self.locs:
            self.list.setCurrentRow(idx)

    # ---------------------------------------------------------------- list
    def _label(self, loc):
        ll = alerts.coords(loc, self.main.my_location.latlon())
        where = f"{ll[0]:.3f}, {ll[1]:.3f}" if ll else "not set"
        return f"{'🔔' if loc.get('enabled', True) else '🔕'}  {loc['name']}   ({where})"

    def _fill(self):
        self.list.blockSignals(True)
        self.list.clear()
        for loc in self.locs:
            QListWidgetItem(self._label(loc), self.list)
        self.list.blockSignals(False)

    def _select(self, row):
        self._cur = row
        self._loading = True
        ok = 0 <= row < len(self.locs)
        loc = self.locs[row] if ok else alerts.normalise({"name": ""})
        mine = bool(loc.get("mine"))
        self.name.setText(loc.get("name", ""))
        ll = alerts.coords(loc, self.main.my_location.latlon()) or (0.0, 0.0)
        self.lat.setValue(ll[0])
        self.lon.setValue(ll[1])
        for w in (self.lat, self.lon):
            w.setEnabled(ok and not mine)
            w.setToolTip("Set with Location → Set my location" if mine else "")
        self.enabled.setChecked(loc.get("enabled", True))
        for k, cb in self.warn.items():
            cb.setChecked(bool(loc["warn"].get(k)))
        self.watches.setChecked(bool(loc.get("watches")))
        self.reports.setChecked(bool(loc.get("reports")))
        self.report_mi.setValue(int(loc.get("report_miles") or 10))
        self.lightning.setChecked(bool(loc.get("lightning")))
        self.ltg_mi.setValue(int(loc.get("lightning_miles") or 10))
        self.sound.setChecked(bool(loc.get("sound", True)))
        self.desktop.setChecked(bool(loc.get("desktop", True)))
        self.popup.setChecked(bool(loc.get("popup", True)))
        self.summary.setText("Alerts: " + alerts.describe_rules(loc) if ok else "")
        self._loading = False

    def _edited(self, *_):
        if self._loading or not (0 <= self._cur < len(self.locs)):
            return
        loc = self.locs[self._cur]
        loc["name"] = self.name.text().strip() or "Unnamed place"
        if not loc.get("mine"):
            loc["lat"], loc["lon"] = round(self.lat.value(), 5), round(self.lon.value(), 5)
        loc["enabled"] = self.enabled.isChecked()
        loc["warn"] = {k: cb.isChecked() for k, cb in self.warn.items()}
        loc["watches"] = self.watches.isChecked()
        loc["reports"] = self.reports.isChecked()
        loc["report_miles"] = self.report_mi.value()
        loc["lightning"] = self.lightning.isChecked()
        loc["lightning_miles"] = self.ltg_mi.value()
        loc["sound"] = self.sound.isChecked()
        loc["desktop"] = self.desktop.isChecked()
        loc["popup"] = self.popup.isChecked()
        self.summary.setText("Alerts: " + alerts.describe_rules(loc))
        it = self.list.item(self._cur)
        if it is not None:
            it.setText(self._label(loc))

    def add(self, name, lat, lon):
        loc = alerts.new_location(name, lat, lon)
        self.locs.append(loc)
        self._fill()
        self.list.setCurrentRow(len(self.locs) - 1)

    def _add_center(self):
        v = self.main.view
        lat, lon = v.world_to_latlon(v.cx, v.cy)
        self.add(f"Place {len(self.locs) + 1}", lat, lon)
        self.name.setFocus()
        self.name.selectAll()

    def _add_typed(self):
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Add a place", "Name, latitude, longitude\n(e.g.  Grandma's, 35.22, -97.44)")
        if not ok or not text.strip():
            return
        parts = [p.strip() for p in text.split(",")]
        try:
            lat, lon = float(parts[-2]), float(parts[-1])
            name = ", ".join(parts[:-2]) or f"Place {len(self.locs) + 1}"
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (ValueError, IndexError):
            QMessageBox.warning(self, "Add a place", "Please type a name, then the latitude and longitude, "
                                "separated by commas.")
            return
        self.add(name, lat, lon)

    def _remove(self):
        r = self._cur
        if not (0 <= r < len(self.locs)):
            return
        if self.locs[r].get("mine"):
            QMessageBox.information(self, "Saved locations", "\"My location\" can't be removed – untick "
                                    "\"Alerts on for this place\" to silence it, or use Location → Remove my location.")
            return
        del self.locs[r]
        self._fill()
        self.list.setCurrentRow(min(r, len(self.locs) - 1))

    def _go(self):
        if 0 <= self._cur < len(self.locs):
            ll = alerts.coords(self.locs[self._cur], self.main.my_location.latlon())
            if ll:
                self.main.go_to_latlon(*ll)

    def _test(self):
        self.main.settings["alert_volume"] = self.volume.value() / 100.0
        self.main.notifier.play(self.sound_name.currentData())

    def result_locations(self):
        return copy.deepcopy(self.locs)

    def accept(self):
        s = self.main.settings
        s["saved_locations"] = self.result_locations()
        s["alert_sound"] = self.sound_name.currentData()
        s["alert_volume"] = self.volume.value() / 100.0
        mine = next((loc for loc in self.locs if loc.get("mine")), None)
        if mine is not None:
            s["warn_at_location"] = bool(mine.get("enabled", True))
        s.save()
        super().accept()
