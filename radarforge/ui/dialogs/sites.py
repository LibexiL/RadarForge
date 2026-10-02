"""Radar site picker."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from ...data.sites import all_sites


class SiteDialog(QDialog):
    def __init__(self, current: str, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("Choose radar")
        self.resize(460, 560)
        self.settings = settings
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search by ID, city or state (e.g. TLX, Denver, PA)")
        self.tdwr = QCheckBox("Include TDWR")
        self.list = QListWidget()
        self.fav_btn = QPushButton("☆ Favourite")
        self.fav_btn.setToolTip("Keep the selected radar at the top of this list (and in Radar → Favourite radars)")
        self.fav_btn.clicked.connect(self._toggle_fav)
        self.fav_btn.setVisible(settings is not None)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.tdwr)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.fav_btn)
        bottom.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        lay.addLayout(bottom)
        self.filter.textChanged.connect(self._fill)
        self.tdwr.toggled.connect(self._fill)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        self.list.currentItemChanged.connect(lambda *_: self._sync_fav())
        self.current = current
        self._fill()

    def _favs(self):
        return list(self.settings["favorite_sites"] or []) if self.settings is not None else []

    def _fill(self):
        q = self.filter.text().strip().lower()
        keep = self.selected() or self.current
        self.list.clear()
        favs = self._favs()
        sites = sorted(all_sites().values(), key=lambda s: (s.id not in favs, favs.index(s.id) if s.id in favs else 0,
                                                            s.state, s.id))
        for s in sites:
            if s.type == "tdwr" and not self.tdwr.isChecked() and s.id not in favs:
                continue
            text = s.label
            if q and q not in text.lower() and q != s.state.lower():
                continue
            it = QListWidgetItem(("★  " if s.id in favs else "") + text)
            it.setData(Qt.UserRole, s.id)
            self.list.addItem(it)
            if s.id == keep:
                self.list.setCurrentItem(it)
        self._sync_fav()

    def _sync_fav(self):
        sid = self.selected()
        on = sid in self._favs()
        self.fav_btn.setText("★ Remove favourite" if on else "☆ Add favourite")
        self.fav_btn.setEnabled(sid is not None)

    def _toggle_fav(self):
        sid = self.selected()
        if sid is None or self.settings is None:
            return
        favs = self._favs()
        if sid in favs:
            favs.remove(sid)
        else:
            favs.append(sid)
        self.settings["favorite_sites"] = favs
        self.settings.save()
        self._fill()

    def selected(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None
