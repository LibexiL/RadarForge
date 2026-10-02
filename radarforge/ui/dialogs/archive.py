"""Archive browser: pick a radar, a day and the volumes to open."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QDate, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout

from ...data import aws


class _Lister(QObject):
    done = Signal(object)

    def __init__(self, site, day):
        super().__init__()
        self.site, self.day = site, day

    def run(self):
        try:
            self.done.emit(aws.list_level2(self.site, self.day))
        except Exception as exc:
            self.done.emit(exc)


class ArchiveDialog(QDialog):
    """Pick a day, list volumes on AWS, select a range to load."""

    def __init__(self, site: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Archive – {site}")
        self.resize(520, 600)
        self.site = QLineEdit(site)
        self.site.setMaxLength(4)
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.list_btn = QPushButton("List volumes")
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.maxf = QSpinBox()
        self.maxf.setRange(1, 200)
        self.maxf.setValue(20)
        self.l3 = QCheckBox("Also fetch Level III products for panels/overlays (2020+)")
        self.l3.setChecked(True)
        self.info = QLabel("Dates are UTC. Select one or more volumes (Shift/Ctrl-click); "
                           "if more than the frame limit are selected they are evenly thinned.")
        self.info.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Radar", self.site)
        row = QHBoxLayout()
        row.addWidget(self.date)
        row.addWidget(self.list_btn)
        form.addRow("Day (UTC)", row)
        form.addRow("Max frames", self.maxf)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.l3)
        lay.addWidget(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Load")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.list_btn.clicked.connect(self.refresh)
        self.files = []
        self._thread = None

    def refresh(self):
        d = self.date.date()
        day = datetime(d.year(), d.month(), d.day(), tzinfo=timezone.utc).date()
        self.list.clear()
        self.list.addItem("Listing…")
        self._thread = QThread(self)
        self._worker = _Lister(self.site.text().strip().upper(), day)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._listed)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _listed(self, res):
        self.list.clear()
        if isinstance(res, Exception):
            self.list.addItem(f"Error: {res}")
            return
        self.files = res
        if not res:
            self.list.addItem("No volumes found for that day.")
            return
        for f in res:
            it = QListWidgetItem(f"{f.time:%H:%M:%S}Z   {f.name}   {f.size / 1e6:.1f} MB")
            it.setData(Qt.UserRole, f)
            self.list.addItem(it)

    def selected_files(self):
        items = [i.data(Qt.UserRole) for i in self.list.selectedItems() if i.data(Qt.UserRole)]
        items.sort(key=lambda f: f.time)
        n = self.maxf.value()
        if len(items) > n:
            step = len(items) / n
            items = [items[int(i * step)] for i in range(n - 1)] + [items[-1]]
        return items
