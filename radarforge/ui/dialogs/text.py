"""A window that shows a piece of text (a warning, a watch) to read and copy."""
from __future__ import annotations

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPlainTextEdit, QPushButton, QVBoxLayout


class TextDialog(QDialog):
    def __init__(self, parent, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit(text)
        self.view.setReadOnly(True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(self.view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = QPushButton("Copy text")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.addButton(copy, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.accept)
        lay.addWidget(buttons)
