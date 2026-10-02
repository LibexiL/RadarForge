"""Background jobs the main window hands to its thread pools."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal


class _Relay(QObject):
    imageReady = Signal(int, object)      # panel index, result dict
    call = Signal(object)                 # a function to run on the UI thread (see MainWindow.run_bg)


class _ImageJob(QRunnable):
    def __init__(self, fn, relay, panel):
        super().__init__()
        self.fn, self.relay, self.panel = fn, relay, panel
        self.setAutoDelete(True)

    def run(self):
        try:
            res = self.fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            res = {"error": str(exc)}
        self.relay.imageReady.emit(self.panel, res)


class _Bg(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.fn()
        except Exception:
            pass
