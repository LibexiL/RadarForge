"""Saved locations: add / edit a place and choose what to be alerted about, and the alert delivery options."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout)

from ...services.locations import (LIGHTNING_CHOICES, OUTLOOK_LEVELS, OUTLOOK_NAMES, RADIUS_CHOICES, Location,
                                   normalize_rules)


def _radius_combo(value: int) -> QComboBox:
    cb = QComboBox()
    for miles in RADIUS_CHOICES:
        cb.addItem("covers the location" if miles == 0 else f"covers it or is within {miles} miles", miles)
    cb.setCurrentIndex(max(0, cb.findData(min(RADIUS_CHOICES, key=lambda m: abs(m - int(value))))))
    return cb


class LocationEditDialog(QDialog):
    """One place: its name, position and what it should be alerted about."""

    def __init__(self, parent, loc: Location, centre=None, title="Location"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.loc = loc
        rules = normalize_rules(loc.rules)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(loc.name)
        form.addRow("Name", self.name)
        self.lat = QDoubleSpinBox()
        self.lat.setRange(-90, 90)
        self.lat.setDecimals(4)
        self.lat.setValue(loc.lat)
        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(4)
        self.lon.setValue(loc.lon)
        row = QHBoxLayout()
        row.addWidget(QLabel("Latitude"))
        row.addWidget(self.lat)
        row.addWidget(QLabel("Longitude"))
        row.addWidget(self.lon)
        if centre is not None:
            here = QPushButton("Use the map centre")
            here.clicked.connect(lambda: (self.lat.setValue(centre[0]), self.lon.setValue(centre[1])))
            row.addWidget(here)
        form.addRow("Position", row)
        lay.addLayout(form)

        box = QGroupBox("Warn me about")
        bl = QFormLayout(box)
        self.rule_on, self.rule_miles = {}, {}
        for key, label in (("tornado", "Tornado warnings"), ("severe", "Severe thunderstorm warnings"),
                           ("flood", "Flash flood warnings")):
            on = QCheckBox(label)
            on.setChecked(rules[key]["on"])
            miles = _radius_combo(rules[key]["miles"])
            miles.setEnabled(on.isChecked())
            on.toggled.connect(miles.setEnabled)
            self.rule_on[key], self.rule_miles[key] = on, miles
            bl.addRow(on, miles)
        self.significant = QCheckBox("Only the serious ones (PDS, considerable, destructive, emergency, observed tornado)")
        self.significant.setChecked(rules["significant_only"])
        bl.addRow(self.significant)
        self.watch = QCheckBox("Tornado and severe thunderstorm watches that cover it")
        self.watch.setChecked(rules["watch"]["on"])
        bl.addRow(self.watch)
        self.mcd = QCheckBox("SPC mesoscale discussions that cover it")
        self.mcd.setChecked(rules["mcd"]["on"])
        bl.addRow(self.mcd)
        self.outlook = QComboBox()
        for lv in OUTLOOK_LEVELS:
            self.outlook.addItem(OUTLOOK_NAMES[lv], lv)
        self.outlook.setCurrentIndex(self.outlook.findData(rules["outlook"]["min"]))
        bl.addRow("SPC day 1 outlook", self.outlook)
        self.lightning = QComboBox()
        for miles in LIGHTNING_CHOICES:
            self.lightning.addItem("Don't tell me" if miles == 0 else f"Flashes within {miles} miles", miles)
        self.lightning.setCurrentIndex(max(0, self.lightning.findData(rules["lightning"]["miles"])))
        bl.addRow("Lightning (GOES GLM)", self.lightning)
        lay.addWidget(box)
        hint = QLabel("Alerts come with live data. Warnings, discussions, the outlook and lightning are fetched "
                      "for this even when their map layers are switched off.")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        lay.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _ok(self):
        if not self.name.text().strip():
            self.name.setFocus()
            return
        self.loc.name = self.name.text().strip()[:40]
        self.loc.lat, self.loc.lon = self.lat.value(), self.lon.value()
        r = {k: {"on": self.rule_on[k].isChecked(), "miles": self.rule_miles[k].currentData()}
             for k in ("tornado", "severe", "flood")}
        r["significant_only"] = self.significant.isChecked()
        r["watch"] = {"on": self.watch.isChecked()}
        r["mcd"] = {"on": self.mcd.isChecked()}
        r["outlook"] = {"min": self.outlook.currentData()}
        r["lightning"] = {"miles": self.lightning.currentData()}
        self.loc.rules = normalize_rules(r)
        self.accept()


class LocationsDialog(QDialog):
    """All saved locations, with how alerts are delivered."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.book = main.book
        self.setWindowTitle("Locations and alerts")
        self.setMinimumSize(620, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(self._hint("The first location is “my location” (Ctrl+L goes to it, and the storm track tool "
                                 "tells you when a storm reaches it). Each location has its own alert rules."))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self.edit())
        row.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.btns = {}
        for key, text, fn in (("add", "Add…", self.add), ("edit", "Edit…", self.edit),
                              ("main", "Make my location", self.make_main), ("go", "Go to", self.go),
                              ("remove", "Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            side.addWidget(b)
            self.btns[key] = b
        side.addStretch(1)
        row.addLayout(side)
        lay.addLayout(row, 1)

        opt = QGroupBox("When an alert comes")
        ol = QVBoxLayout(opt)
        o = dict(main.settings["alert_options"] or {})
        self.popup = QCheckBox("Show a window (with a Show on map button)")
        self.popup.setChecked(o.get("popup", True))
        self.sound = QCheckBox("Play a sound (a different one for tornado, severe and other alerts)")
        self.sound.setChecked(o.get("sound", True))
        self.notify = QCheckBox("Send a desktop notification")
        self.notify.setChecked(o.get("notify", True))
        for w in (self.popup, self.sound, self.notify):
            ol.addWidget(w)
        test = QHBoxLayout()
        test.addWidget(QLabel("Try it:"))
        for kind, text in (("tornado", "Tornado"), ("severe", "Severe"), ("info", "Other")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, k=kind: main.test_alert(k))
            test.addWidget(b)
        test.addStretch(1)
        ol.addLayout(test)
        lay.addWidget(opt)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)
        self.fill()

    @staticmethod
    def _hint(text):
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setProperty("role", "hint")
        return lab

    def fill(self, select=None):
        self.list.clear()
        for i, loc in enumerate(self.book.items):
            it = QListWidgetItem(f"{'★ ' if i == 0 else ''}{loc.name}    {loc.lat:.3f}, {loc.lon:.3f}\n"
                                 f"     Alerts: {loc.describe_rules()}")
            it.setData(256, loc.id)
            self.list.addItem(it)
            if loc.id == select:
                self.list.setCurrentItem(it)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        has = self.list.count() > 0
        for k in ("edit", "main", "go", "remove"):
            self.btns[k].setEnabled(has)

    def _current(self) -> Location | None:
        it = self.list.currentItem()
        return self.book.get(it.data(256)) if it is not None else None

    def add(self):
        self.main.add_location_dialog(parent=self)
        self.fill()

    def edit(self):
        loc = self._current()
        if loc is not None:
            self.main.edit_location(loc, parent=self)
            self.fill(loc.id)

    def make_main(self):
        loc = self._current()
        if loc is not None:
            self.book.make_primary(loc.id)
            self.main.locations_changed()
            self.fill(loc.id)

    def go(self):
        loc = self._current()
        if loc is not None:
            self.main.go_to_location(loc)

    def remove(self):
        loc = self._current()
        if loc is not None and QMessageBox.question(self, "Remove location", f"Remove “{loc.name}”?") == QMessageBox.Yes:
            self.book.remove(loc.id)
            self.main.locations_changed()
            self.fill()

    def done(self, result):
        s = self.main.settings
        s["alert_options"] = {"popup": self.popup.isChecked(), "sound": self.sound.isChecked(),
                              "notify": self.notify.isChecked()}
        s.save()
        super().done(result)
