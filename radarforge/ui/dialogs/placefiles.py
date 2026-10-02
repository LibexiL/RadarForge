"""Placefile manager (panel and dialog)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget


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
