"""Small widgets and helpers shared by the side panels."""
from __future__ import annotations

import numpy as np

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QSizePolicy, QToolButton


def _swatch(rgb, size=12):
    pm = QPixmap(size, size)
    pm.fill(QColor(*rgb))
    return QIcon(pm)


def _section(text):
    lab = QLabel(text.upper())
    lab.setProperty("role", "section")
    return lab


def _hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("role", "hint")
    return lab


def _chip(text, tip="", checkable=True):
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setProperty("role", "chip")
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    b.setMinimumWidth(34)
    return b


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(widget)
    return sa


def _zoom_to(main, xs, ys, pad=1.5):
    view = main.view
    x0, x1, y0, y1 = float(np.min(xs)), float(np.max(xs)), float(np.min(ys)), float(np.max(ys))
    p = view.panels[min(view.active_panel, len(view.panels) - 1)]
    w = max(x1 - x0, 5.0) * pad
    h = max(y1 - y0, 5.0) * pad
    scale = min(p.rect.width() / w, p.rect.height() / h)
    view.set_view((x0 + x1) / 2, (y0 + y1) / 2, max(0.2, min(40.0, scale)))
