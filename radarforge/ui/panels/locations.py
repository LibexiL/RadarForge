"""Locations: what is happening at each saved place (warnings on it or near it, discussions, outlook, lightning)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from .common import _hint, _scroll

KIND_COLORS = {"inside": "#ff5a4d", "near": "#ffae42", "mcd": "#5aa0ff", "outlook": "#9aa4b5", "lightning": "#ffd84a"}


class LocationsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        inner = QWidget()
        self.cards = QVBoxLayout(inner)
        self.cards.setContentsMargins(8, 8, 8, 8)
        self.cards.setSpacing(8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 6)
        outer.addWidget(_scroll(inner), 1)
        row = QHBoxLayout()
        row.setContentsMargins(8, 0, 8, 0)
        add = QPushButton("Add…")
        add.clicked.connect(lambda: main.add_location_dialog())
        manage = QPushButton("Locations and alerts…")
        manage.clicked.connect(main.manage_locations)
        row.addWidget(add)
        row.addWidget(manage, 1)
        outer.addLayout(row)
        main.locationsUpdated.connect(self.refresh)
        self.refresh()

    def refresh(self):
        while self.cards.count():
            item = self.cards.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()                       # gone at once (deleteLater alone leaves it painted until the loop runs)
                w.setParent(None)
                w.deleteLater()
        book = self.main.book
        if not book.items:
            self.cards.addWidget(_hint("No locations yet. Right-click the map and choose “Add location here…”, or "
                                       "use Locations → Add location. You'll be told about warnings, discussions, "
                                       "the outlook and lightning at each of them."))
        for i, loc in enumerate(book.items):
            card = QFrame()
            card.setFrameShape(QFrame.StyledPanel)
            lay = QVBoxLayout(card)
            lay.setContentsMargins(8, 6, 8, 6)
            lay.setSpacing(3)
            head = QHBoxLayout()
            name = QLabel(f"<b>{'★ ' if i == 0 else ''}{loc.name}</b>")
            name.setTextFormat(Qt.RichText)
            head.addWidget(name, 1)
            go = QToolButton()
            go.setText("Go to")
            go.clicked.connect(lambda _=False, l=loc: self.main.go_to_location(l))
            edit = QToolButton()
            edit.setText("Edit")
            edit.clicked.connect(lambda _=False, l=loc: self.main.edit_location(l))
            head.addWidget(go)
            head.addWidget(edit)
            lay.addLayout(head)
            lines = self.main.location_status.get(loc.id)
            if lines is None:
                text = "<span style='color:#9aa4b5'>Waiting for data…</span>"
            elif not lines:
                text = "<span style='color:#6fcf8f'>All clear</span>"
            else:
                text = "<br>".join(f"<span style='color:{KIND_COLORS.get(k, '#ccc')}'>●</span> {t}"
                                   for _p, k, t in lines[:6])
            lab = QLabel(text)
            lab.setTextFormat(Qt.RichText)
            lab.setWordWrap(True)
            lay.addWidget(lab)
            lay.addWidget(_hint(f"Alerts: {loc.describe_rules()}"))
            self.cards.addWidget(card)
        self.cards.addStretch(1)
