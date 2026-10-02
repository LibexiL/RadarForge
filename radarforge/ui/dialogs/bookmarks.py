"""Bookmarks: a name and notes for a saved view, and the list to open, edit, share and delete them."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from ...services import views


class BookmarkDialog(QDialog):
    def __init__(self, parent, name: str, notes: str, title="Bookmark"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name)
        self.name.selectAll()
        form.addRow("Name", self.name)
        self.notes = QPlainTextEdit(notes)
        self.notes.setPlaceholderText("What to look for (optional): hook echo at the southwest edge, velocity couplet…")
        self.notes.setFixedHeight(90)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        lay.addWidget(QLabel("A bookmark keeps the radar, the time, the panels, the layers and the zoom."))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _ok(self):
        if self.name.text().strip():
            self.accept()


class BookmarksDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Bookmarks")
        self.setMinimumSize(600, 420)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self.open())
        row.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.btns = {}
        for key, text, fn in (("open", "Open", self.open), ("edit", "Rename / notes…", self.edit),
                              ("share", "Save as file…", self.export), ("copy", "Copy as text", self.copy),
                              ("delete", "Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            side.addWidget(b)
            self.btns[key] = b
        side.addStretch(1)
        imp = QPushButton("Add from file…")
        imp.clicked.connect(self.add_from_file)
        side.addWidget(imp)
        row.addLayout(side)
        lay.addLayout(row, 1)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setProperty("role", "hint")
        lay.addWidget(self.detail)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        lay.addWidget(buttons)
        self.list.currentRowChanged.connect(self._show_detail)
        self.fill()

    def _items(self) -> list:
        return self.main._bookmarks()

    def fill(self, select=None):
        self.list.clear()
        for bm in self._items():
            when = views.parse_time(bm.get("time"))
            self.list.addItem(QListWidgetItem(f"{bm.get('name') or 'Bookmark'}    "
                                              f"{bm.get('site', '')} {when:%Y-%m-%d %H:%MZ}" if when else
                                              f"{bm.get('name') or 'Bookmark'}    {bm.get('site', '')} live"))
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        elif self.list.count():
            self.list.setCurrentRow(0)
        for b in self.btns.values():
            b.setEnabled(self.list.count() > 0)
        self._show_detail()

    def _show_detail(self, *_):
        i = self.list.currentRow()
        items = self._items()
        self.detail.setText(items[i].get("notes") or "No notes." if 0 <= i < len(items) else
                            "No bookmarks yet. Use File → Bookmarks → Save a bookmark… while you look at something.")

    def _current(self):
        i = self.list.currentRow()
        items = self._items()
        return (i, items[i]) if 0 <= i < len(items) else (None, None)

    def _store(self, items):
        self.main.settings["bookmarks"] = items
        self.main.settings.save()

    def open(self):
        _i, bm = self._current()
        if bm is not None:
            self.accept()
            self.main.open_bookmark(bm)

    def edit(self):
        i, bm = self._current()
        if bm is None:
            return
        dlg = BookmarkDialog(self, bm.get("name", ""), bm.get("notes", ""), "Rename bookmark")
        if dlg.exec():
            items = self._items()
            items[i] = {**bm, "name": dlg.name.text().strip()[:80], "notes": dlg.notes.toPlainText().strip()}
            self._store(items)
            self.fill(i)

    def delete(self):
        i, bm = self._current()
        if bm is not None and QMessageBox.question(self, "Delete bookmark", f"Delete “{bm.get('name')}”?") == QMessageBox.Yes:
            items = self._items()
            del items[i]
            self._store(items)
            self.fill(min(i, len(items) - 1))

    def export(self):
        _i, bm = self._current()
        if bm is None:
            return
        name = (bm.get("name") or "view").replace(" ", "_").replace(":", "") + views.EXTENSION
        path, _ = QFileDialog.getSaveFileName(self, "Save bookmark", os.path.join(os.path.expanduser("~"), name),
                                              f"RadarForge view (*{views.EXTENSION})")
        if path:
            from ... import __version__
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(views.to_file_text(bm, __version__))

    def copy(self):
        _i, bm = self._current()
        if bm is not None:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(views.encode(views.clean_view(bm)))

    def add_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add a bookmark from a file", os.path.expanduser("~"),
                                              f"RadarForge view (*{views.EXTENSION});;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                v = views.decode(fh.read())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Bookmarks", f"Couldn't read {os.path.basename(path)}:\n{exc}")
            return
        bm = views.new_bookmark(v, v.get("name") or views.default_bookmark_name(v), v.get("notes", ""))
        self._store(self._items() + [bm])
        self.fill(self.list.count())
