"""Frames (one point in time: a Level II volume plus Level III products)."""
from __future__ import annotations

import itertools
import threading
from collections import OrderedDict
from datetime import datetime

from .level2 import DecodeCancelled, Level2Volume, read_level2_isolated

_ids = itertools.count(1)


class VolumeCache:
    """LRU of decoded Level II volumes keyed by file path. One decode per file: a second caller asking for a
    volume that is being decoded waits for that decode (and gets its error, if it fails)."""

    def __init__(self, capacity: int = 4):
        self.capacity = capacity
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._loading: dict = {}          # path -> [Event, volume or exception]

    def retain(self, paths):
        """Drops decoded volumes of files no frame uses any more (another radar, or older than the loop)."""
        keep = set(paths)
        with self._lock:
            for p in [p for p in self._d if p not in keep]:
                del self._d[p]

    def peek(self, path: str):
        """The decoded volume if it's already in memory, else None (never blocks)."""
        with self._lock:
            return self._d.get(path)

    def loading(self, path: str) -> bool:
        with self._lock:
            return path in self._loading

    def get(self, path: str, site_hint: str = "") -> Level2Volume:
        while True:
            with self._lock:
                if path in self._d:
                    self._d.move_to_end(path)
                    return self._d[path]
                slot = self._loading.get(path)
                if slot is None:
                    slot = [threading.Event(), None]
                    self._loading[path] = slot
                    break                                 # this thread decodes it
            slot[0].wait()
            res = slot[1]
            if isinstance(res, Level2Volume):
                return res
            if isinstance(res, Exception) and not isinstance(res, DecodeCancelled):
                raise res
            # the decoder gave up waiting (a read-ahead no longer wanted): decode it here after all
        try:
            vol = read_level2_isolated(path, site_hint)
            slot[1] = vol
            with self._lock:
                self._d[path] = vol
                while len(self._d) > max(1, self.capacity):
                    self._d.popitem(last=False)
            return vol
        except BaseException as exc:
            slot[1] = exc
            raise
        finally:
            with self._lock:
                self._loading.pop(path, None)
            slot[0].set()


VOLUMES = VolumeCache()


def total_ram_gb() -> float:
    """Installed memory in GB (0 when it can't be found)."""
    try:
        import os
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        import ctypes

        class _Mem(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                        ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
        m = _Mem()
        m.len = ctypes.sizeof(_Mem)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.total / 1e9
    except Exception:
        pass
    return 0.0


def volume_capacity(settings) -> int:
    """Decoded volumes kept in memory: at least the setting, and enough to hold the whole loop when the
    computer has the memory (a decoded volume is about 40-60 MB), so looping and changing products never
    waits on re-decoding."""
    want = int(settings["volume_cache"] or 4)
    loop = int(settings["loop_frames"] or 10) + 2
    ram = total_ram_gb()
    room = int(ram * 1.5) if ram else 8           # ~6% of memory for decoded volumes
    return max(want, min(loop, room))


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
        self.l3 = {**self.l3, prod.awips: prod}      # a new dict: other threads may be reading this one
        self.revision += 1

    def __repr__(self):
        return f"<Frame {self.site} {self.time:%Y-%m-%d %H:%M:%S} L2={'y' if self.has_level2() else 'n'} L3={sorted(self.l3)}>"
