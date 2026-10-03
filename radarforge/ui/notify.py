"""Alert sound and desktop notifications (system tray balloon / notification centre)."""
from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ..features import alerts


class Notifier(QObject):
    clicked = Signal(object)          # the event whose desktop notification was clicked

    def __init__(self, settings, window):
        super().__init__(window)
        self.settings = settings
        self.window = window
        self._tray = None
        self._effects = {}
        self._last = None

    # ---------------------------------------------------------------- sound
    def play(self, name=None):
        name = name or self.settings["alert_sound"] or "chime"
        if name == "none":
            return
        try:
            from PySide6.QtMultimedia import QSoundEffect
            eff = self._effects.get(name)
            if eff is None:
                path = alerts.sound_file(name)
                eff = QSoundEffect(self)
                eff.setSource(QUrl.fromLocalFile(str(path)))
                self._effects[name] = eff
            eff.setVolume(max(0.0, min(1.0, float(self.settings["alert_volume"] or 0.8))))
            if eff.status() == QSoundEffect.Error:
                raise RuntimeError("sound device")
            eff.play()
        except Exception:
            QApplication.beep()

    # ---------------------------------------------------------------- desktop
    def tray(self):
        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self.window.windowIcon() or QApplication.windowIcon(), self.window)
            self._tray.setToolTip("RadarForge alerts")
            self._tray.messageClicked.connect(lambda: self._last is not None and self.clicked.emit(self._last))
            self._tray.activated.connect(lambda _r: self._raise())
            self._tray.show()
        return self._tray

    def _raise(self):
        w = self.window
        if w.isMinimized():
            w.showNormal()
        w.raise_()
        w.activateWindow()

    def desktop(self, ev):
        tr = self.tray()
        if tr is None:
            return False
        self._last = ev
        icon = QSystemTrayIcon.Critical if ev["priority"] >= 8 else QSystemTrayIcon.Warning \
            if ev["priority"] >= 3 else QSystemTrayIcon.Information
        tr.showMessage(ev["title"], ev["text"], icon, 15000)
        return True

    def notify(self, ev, sound=True):
        """Sound and desktop notification for an alert. Returns True if a desktop notification was shown."""
        loc = ev["loc"]
        if sound and loc.get("sound", True):
            self.play()
        shown = bool(loc.get("desktop", True)) and self.desktop(ev)
        QApplication.alert(self.window, 0)
        return shown
