"""Frames (one point in time: a Level II volume plus Level III products)."""
from __future__ import annotations

import itertools
import threading
from collections import OrderedDict
from datetime import datetime

from .level2 import Level2Volume, read_level2_isolated

_ids = itertools.count(1)


class VolumeCache:
    """LRU of decoded Level II volumes keyed by file path."""

    def __init__(self, capacity: int = 4):
        self.capacity = capacity
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._loading: dict = {}

    def peek(self, path: str):
        """The decoded volume if it's already in memory, else None (never blocks)."""
        with self._lock:
            return self._d.get(path)

    def get(self, path: str, site_hint: str = "") -> Level2Volume:
        with self._lock:
            if path in self._d:
                self._d.move_to_end(path)
                return self._d[path]
            ev = self._loading.get(path)
            if ev is None:
                ev = threading.Event()
                self._loading[path] = ev
                owner = True
            else:
                owner = False
        if not owner:
            ev.wait(120)
            with self._lock:
                if path in self._d:
                    return self._d[path]
            return read_level2_isolated(path, site_hint)
        try:
            vol = read_level2_isolated(path, site_hint)
            with self._lock:
                self._d[path] = vol
                while len(self._d) > max(1, self.capacity):
                    self._d.popitem(last=False)
            return vol
        finally:
            with self._lock:
                self._loading.pop(path, None)
            ev.set()


VOLUMES = VolumeCache()


class Frame:
    def __init__(self, site: str, time: datetime, l2_path: str | None = None,
                 l2_volume: Level2Volume | None = None, l3: dict | None = None, live: bool = False,
                 label: str = ""):
        self.uid = next(_ids)
        self.revision = 0      # bumps on any change
        self.l2_rev = 0        # bumps when the Level II volume changes
        self.site = site
        self.time = time
        self.l2_path = l2_path
        self._l2 = l2_volume
        self.l3 = l3 or {}
        self.live = live
        self.label = label
        self.gaps = 0          # live volume with chunks that never arrived (replaced by the archive file later)

    @property
    def key(self):
        return (self.uid, self.revision)

    def has_level2(self) -> bool:
        return self._l2 is not None or self.l2_path is not None

    def level2(self) -> Level2Volume | None:
        if self._l2 is not None:
            return self._l2
        if self.l2_path:
            return VOLUMES.get(self.l2_path, self.site)
        return None

    def level2_if_ready(self) -> Level2Volume | None:
        """Like level2(), but returns None instead of waiting for a decode (safe on the UI thread)."""
        if self._l2 is not None:
            return self._l2
        return VOLUMES.peek(self.l2_path) if self.l2_path else None

    def set_level2(self, vol: Level2Volume):
        self._l2 = vol
        self.revision += 1
        self.l2_rev += 1

    def replace_with_file(self, path: str):
        """Swap a volume built from live chunks for the complete archive file."""
        self.l2_path = path
        self._l2 = None
        self.gaps = 0
        self.live = False
        self.revision += 1
        self.l2_rev += 1

    @property
    def from_chunks(self) -> bool:
        return self._l2 is not None and self.l2_path is None

    def pin_level2(self):
        """Keep the decoded volume in memory (used for live frames)."""
        if self._l2 is None and self.l2_path:
            self._l2 = VOLUMES.get(self.l2_path, self.site)

    def add_l3(self, prod):
        self.l3[prod.awips] = prod
        self.revision += 1

    def __repr__(self):
        return f"<Frame {self.site} {self.time:%Y-%m-%d %H:%M:%S} L2={'y' if self.has_level2() else 'n'} L3={sorted(self.l3)}>"
