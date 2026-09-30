"""Real-time Level II via the unidata-nexrad-level2-chunks bucket."""
from __future__ import annotations

import time

from . import aws
from .level2 import SweepBuilder


class ChunkTracker:
    """Follows the newest volume for one site, chunk by chunk."""

    def __init__(self, site: str):
        self.site = site.upper()
        self.volume: int | None = None
        self.stamp: str | None = None
        self.prev_stamp: str = ""
        self.builder: SweepBuilder | None = None
        self.next_chunk = 1
        self.last_progress = time.time()
        self.complete = False

    def _start(self, vol: int):
        self.volume = vol
        self.stamp = None
        self.builder = SweepBuilder()
        self.next_chunk = 1
        self.complete = False
        self.last_progress = time.time()

    def poll(self) -> list:
        """Returns events: ('update', stamp_time, Level2Volume, complete_bool)."""
        events = []
        if self.volume is None or time.time() - self.last_progress > 900:
            n = aws.find_latest_volume(self.site)
            if n is None:
                return events
            self._start(n)
        chunks = aws.list_chunks(self.site, self.volume)
        if chunks and chunks[0].stamp <= self.prev_stamp:
            chunks = []          # leftovers from the previous cycle of this volume number
        if not chunks:
            return events
        if self.stamp is None:
            self.stamp = chunks[0].stamp
        elif chunks[0].stamp != self.stamp:
            self._start(self.volume)
            self.stamp = chunks[0].stamp
        added = False
        for c in chunks:
            if c.number < self.next_chunk:
                continue
            if c.number != self.next_chunk:
                break                      # wait for the missing chunk
            data = aws.fetch(aws.CHUNK_BUCKET, c.key, cache=False, timeout=30)
            self.builder.add_bytes(data)
            self.next_chunk += 1
            added = True
            if c.kind == "E":
                self.complete = True
        if added:
            self.last_progress = time.time()
            vol = self.builder.build(self.site, source=f"chunks {self.volume}")
            vol.complete = self.complete
            events.append(("update", chunks[0].time, vol, self.complete))
        if self.complete:
            self.prev_stamp = self.stamp
            self._start(aws.next_volume_number(self.volume))
        return events
