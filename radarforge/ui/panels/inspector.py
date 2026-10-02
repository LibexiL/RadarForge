"""Cursor inspector."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .common import _section


class InspectorPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.loc = QLabel("Move the mouse over the map.")
        self.loc.setTextFormat(Qt.RichText)
        self.loc.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Panel", "Value"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.setMinimumHeight(80)
        self.loc.setProperty("role", "card")
        self.under = QLabel()
        self.under.setWordWrap(True)
        self.under.setTextFormat(Qt.PlainText)
        self.under.setProperty("role", "card")
        self.under.hide()
        self.tree.setAlternatingRowColors(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.loc)
        lay.addWidget(_section("Values under the cursor"))
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.under)
        main.cursorInfo.connect(self.update_info)

    def update_info(self, info):
        if not self.isVisible():
            return
        if not info:
            return
        self.loc.setText(info["loc_html"])
        rows = info["values"]
        if self.tree.topLevelItemCount() != len(rows):
            self.tree.clear()
            for _ in rows:
                self.tree.addTopLevelItem(QTreeWidgetItem(["", ""]))
        for i, (name, val) in enumerate(rows):
            it = self.tree.topLevelItem(i)
            it.setText(0, name)
            it.setText(1, val)
        under = info.get("under")
        self.under.setVisible(bool(under))
        if under:
            self.under.setText(under)
