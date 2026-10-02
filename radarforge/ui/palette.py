"""The command palette (Ctrl+K): type to find any menu command, radar, product, city, bookmark or workspace."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from ..services.commands import search


class CommandPalette(QDialog):
    def __init__(self, parent, commands: list):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setObjectName("palette")
        self.setModal(True)
        self.commands = commands
        self.chosen = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 8)
        lay.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a command, radar, product, city, bookmark…   (Esc to close)")
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self.list = QListWidget()
        self.list.setUniformItemSizes(False)
        self.list.itemActivated.connect(self._accept_item)
        self.list.itemClicked.connect(self._accept_item)
        lay.addWidget(self.list, 1)
        self.hint = QLabel("↑ ↓ to choose · Enter to run")
        self.hint.setProperty("role", "hint")
        lay.addWidget(self.hint)
        self.resize(min(640, max(420, parent.width() - 120)), 420)
        self._filter("")

    def showEvent(self, ev):
        super().showEvent(ev)
        p = self.parentWidget()
        if p is not None:                                  # near the top of the window, like a launcher
            g = p.geometry()
            self.move(p.mapToGlobal(g.topLeft()).x() + (g.width() - self.width()) // 2 - g.x(),
                      p.mapToGlobal(g.topLeft()).y() + 70 - g.y())
        self.edit.setFocus()

    def _filter(self, text):
        self.list.clear()
        for c in search(self.commands, text, 14):
            line = c.title + (f"    ({c.shortcut})" if c.shortcut else "")
            it = QListWidgetItem(f"{line}\n{c.detail or c.group}" if (c.detail or c.group) else line)
            it.setData(Qt.UserRole, c)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                step = 1 if key == Qt.Key_Down else -1
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + step)))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if self.list.currentItem() is not None:
                    self._accept_item(self.list.currentItem())
                return True
        return super().eventFilter(obj, ev)

    def _accept_item(self, item):
        self.chosen = item.data(Qt.UserRole)
        self.accept()
