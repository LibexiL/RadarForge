"""Base class for map layers whose data is downloaded in the background."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from ..data.aws import friendly_error

UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)"}


class BackgroundLayer(QObject):
    """Runs named download jobs on worker threads (one at a time per name), retries after a minute
    on errors and re-runs each job after its period. Subclasses implement jobs() and paint()."""
    changed = Signal()
    status = Signal(str)
    title = "Layer"

    def __init__(self, settings, parent=None, tick_ms=20_000):
        super().__init__(parent)
        self.settings = settings
        self._next: dict = {}
        self._busy: set = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(tick_ms)
        self.changed.connect(self._kick, Qt.QueuedConnection)     # a finished job may leave newer work

    def _kick(self):
        if not self._busy:
            self.refresh()

    def jobs(self):
        """[(name, fn, period_s)] to run now; fn() runs on a worker thread and returns nothing."""
        return []

    def refresh(self, force=False):
        now = time.time()
        for name, fn, period in self.jobs():
            if name in self._busy or (not force and now < self._next.get(name, 0.0)):
                continue
            self._busy.add(name)
            threading.Thread(target=self._run, args=(name, fn, period), daemon=True).start()

    def _run(self, name, fn, period):
        try:
            fn()
            self._next[name] = time.time() + period
        except Exception as exc:
            self._next[name] = time.time() + 60
            self.status.emit(f"{self.title}: {friendly_error(exc)}")
        finally:
            self._busy.discard(name)
            self.changed.emit()

    def invalidate(self, name=None):
        """Run a job (or all) at the next refresh."""
        if name is None:
            self._next.clear()
        else:
            self._next.pop(name, None)

    def busy(self):
        return bool(self._busy)
