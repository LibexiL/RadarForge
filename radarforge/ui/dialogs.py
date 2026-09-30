"""Dialogs: site picker, archive browser, placefile manager, storm motion."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QDate, QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDateEdit, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..data import aws
from ..data.sites import all_sites


# --------------------------------------------------------------------------- #
class SiteDialog(QDialog):
    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose radar")
        self.resize(460, 560)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search by ID, city or state (e.g. TLX, Denver, PA)")
        self.tdwr = QCheckBox("Include TDWR")
        self.list = QListWidget()
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.tdwr)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.filter.textChanged.connect(self._fill)
        self.tdwr.toggled.connect(self._fill)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        self.current = current
        self._fill()

    def _fill(self):
        q = self.filter.text().strip().lower()
        self.list.clear()
        for s in sorted(all_sites().values(), key=lambda s: (s.state, s.id)):
            if s.type == "tdwr" and not self.tdwr.isChecked():
                continue
            text = s.label
            if q and q not in text.lower() and q != s.state.lower():
                continue
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, s.id)
            self.list.addItem(it)
            if s.id == self.current:
                self.list.setCurrentItem(it)

    def selected(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None


# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
class PlacefilePanel(QWidget):
    """Placefile list with On / Below-radar checkboxes (used in the side panel and the dialog)."""

    def __init__(self, manager, parent=None, compact=False):
        super().__init__(parent)
        self.m = manager
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["On", "Below", "Title", "URL / file", "Status"])
        self.table.horizontalHeaderItem(0).setToolTip("Show this placefile")
        self.table.horizontalHeaderItem(1).setToolTip("Draw this placefile underneath the radar data "
                                                      "(e.g. background maps, shaded counties)")
        self.table.horizontalHeader().setSectionResizeMode(2 if compact else 3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        if compact:
            self.table.setColumnHidden(3, True)
        add_url = QPushButton("+ URL")
        add_file = QPushButton("+ File")
        rem = QPushButton("Remove")
        rel = QPushButton("Reload")
        for b, tip in ((add_url, "Add a placefile from a web address"), (add_file, "Add a placefile from disk"),
                       (rem, "Remove the selected placefile"), (rel, "Reload the selected (or all) placefiles")):
            b.setToolTip(tip)
        btns = QHBoxLayout()
        for b in (add_url, add_file, rem, rel):
            btns.addWidget(b)
        btns.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        if not compact:
            info = QLabel("GRLevelX-format placefiles (Lines, Polygons, Triangles, Text, Icons, Objects, "
                          "TimeRange). Enabled files refresh automatically. Tick <b>Below</b> to draw a "
                          "placefile underneath the radar data instead of on top.")
            info.setWordWrap(True)
            lay.addWidget(info)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)
        add_url.clicked.connect(self._add_url)
        add_file.clicked.connect(self._add_file)
        rem.clicked.connect(self._remove)
        rel.clicked.connect(self._reload)
        self.table.itemChanged.connect(self._changed)
        self.m.changed.connect(self._fill)
        self._filling = False
        self._fill()

    def _fill(self):
        self._filling = True
        entries = self.m.entries()
        self.table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if e.get("enabled", True) else Qt.Unchecked)
            chk.setData(Qt.UserRole, e["url"])
            self.table.setItem(r, 0, chk)
            below = QTableWidgetItem()
            below.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            below.setCheckState(Qt.Checked if e.get("below", False) else Qt.Unchecked)
            below.setData(Qt.UserRole, e["url"])
            below.setToolTip("Checked: drawn under the radar data. Unchecked: drawn on top.")
            self.table.setItem(r, 1, below)
            pf = self.m.files.get(e["url"])
            name = e["url"].rstrip("/").rsplit("/", 1)[-1] or e["url"]
            title = QTableWidgetItem(e.get("title") or (pf.title if pf and pf.title else name))
            title.setToolTip(e["url"])
            self.table.setItem(r, 2, title)
            self.table.setItem(r, 3, QTableWidgetItem(e["url"]))
            if pf is None:
                st = "loading…" if e.get("enabled", True) else "off"
            else:
                st = (f"error: {pf.error}" if pf.error and not pf.items
                      else f"{len(pf.items)} items" + (f" ({pf.error})" if pf.error else ""))
            self.table.setItem(r, 4, QTableWidgetItem(st))
            for c in (2, 3, 4):
                self.table.item(r, c).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self._filling = False

    def _changed(self, item):
        if self._filling:
            return
        if item.column() == 0:
            self.m.set_enabled(item.data(Qt.UserRole), item.checkState() == Qt.Checked)
        elif item.column() == 1:
            self.m.set_below(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def _add_url(self):
        url, ok = QInputDialog.getText(self, "Add placefile", "Placefile URL:")
        if ok and url.strip():
            self.m.add(url.strip())
            self._fill()

    def _add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add placefile", "", "Placefiles (*.txt *.pf *.plc);;All files (*)")
        if path:
            self.m.add(path)
            self._fill()

    def _sel_url(self):
        r = self.table.currentRow()
        if r < 0:
            return None
        return self.table.item(r, 0).data(Qt.UserRole)

    def _remove(self):
        u = self._sel_url()
        if u:
            self.m.remove(u)
            self._fill()

    def _reload(self):
        u = self._sel_url()
        if u:
            self.m.reload(u)
        else:
            self.m.reload_all()


class PlacefileDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Placefiles")
        self.resize(760, 380)
        self.panel = PlacefilePanel(manager, self)
        self.table = self.panel.table
        lay = QVBoxLayout(self)
        lay.addWidget(self.panel, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #


from . import settings_dialog as _settings_dialog  # noqa: E402  (moved; still importable from here)

SettingsDialog = _settings_dialog.SettingsDialog
