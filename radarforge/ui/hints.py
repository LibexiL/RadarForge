"""A small one-time hint bubble (first visit to the Models view, the Layers presets…)."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class HintBubble(QFrame):
    def __init__(self, parent, text):
        super().__init__(parent)
        self.setObjectName("hintBubble")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("#hintBubble { background: palette(highlight); border-radius: 8px; }"
                           "#hintBubble QLabel { color: palette(highlighted-text); }"
                           "#hintBubble QPushButton { color: palette(highlighted-text); border: none; "
                           "font-weight: bold; padding: 2px 8px; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 6, 8)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setMaximumWidth(520)
        h.addWidget(lab, 1)
        ok = QPushButton("Got it")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.close)
        h.addWidget(ok)
        self.adjustSize()
        QTimer.singleShot(16000, self.close)


def show_hint(window, text):
    """Shows [text] near the top of the map for a few seconds (once per hint: the caller remembers)."""
    b = HintBubble(window, text)
    b.adjustSize()
    w = window.width()
    top = window.menuBar().height() + window.main_tb.height() + 10
    b.move(QPoint(max(8, (w - b.width()) // 2 - 120), top))
    b.raise_()
    b.show()
    return b
