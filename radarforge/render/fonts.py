"""Fonts for text drawn on the map: the system's UI font (Segoe UI on Windows, the desktop
font on Linux) at a given size, instead of a family name that only exists on some systems."""
from __future__ import annotations


def ui_font(point_size: float, bold: bool = False):
    from PySide6.QtGui import QFont, QGuiApplication
    f = QFont(QGuiApplication.font()) if QGuiApplication.instance() is not None else QFont()
    f.setStyleHint(QFont.SansSerif)
    f.setPointSizeF(point_size)
    f.setBold(bold)
    return f
