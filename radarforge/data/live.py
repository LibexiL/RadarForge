"""Real-time Level II via the unidata-nexrad-level2-chunks bucket.

Each volume is a folder of numbered chunks (S = start, I = intermediate, E = end). The tracker follows the
newest volume chunk by chunk. It copes with the ways the feed misbehaves:

* a chunk that never shows up: after GAP_WAIT seconds it's skipped (the volume is marked as having gaps and
  the data manager swaps in the complete archive file when that appears);
* a volume that never gets its end chunk, or a radar that restarts its numbering: after STALL_S seconds
  without progress it looks for a newer volume and moves on;
* downloads that fail: chunks are fetched in parallel and retried on the next poll, in order.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import aws
from .level2 import SweepBuilder, add_chunks, run_isolated

BATCH = 3                # this many new chunks or more are decoded in a decoder process (catching up)

GAP_WAIT = 30.0          # seconds to wait for a missing chunk before skipping it
STALL_S = 90.0           # seconds without new chunks before checking whether the radar moved on
REFIND_S = 240.0         # seconds without new chunks before searching all volume numbers again


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
        self.missing: list = []          # chunk numbers skipped in the current volume
        self.gap_since: float | None = None
        self.cancelled = False
        self.busy = False
        self._next_check = 0.0

    def _start(self, vol: int):
        self.volume = vol
        self.stamp = None
        self.builder = SweepBuilder()
        self.next_chunk = 1
        self.complete = False
        self.missing = []
        self.gap_since = None
        self.last_progress = time.time()

    # ------------------------------------------------------------------ helpers
    def _event(self, complete, abandoned=False, vol=None):
        if vol is None:
            vol = self.builder.build(self.site, source=f"chunks {self.volume}")
        vol.complete = complete and not self.missing and not abandoned
        t = datetime.strptime(self.stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        return {"time": t, "volume": vol, "complete": complete,
                "missing": len(self.missing), "abandoned": abandoned, "number": self.volume}

    def _moved_on(self, now):
        """A newer volume number than the one we're waiting on, or None."""
        nxt = aws.next_volume_number(self.volume)
        ch = aws.list_chunks(self.site, nxt)
        ref = self.stamp or self.prev_stamp or ""
        if ch and ch[0].stamp > ref:
            return nxt
        if now - self.last_progress > REFIND_S:
            n = aws.find_latest_volume(self.site)
            if n is not None and n != self.volume:
                ch = aws.list_chunks(self.site, n)
                if ch and ch[0].stamp > ref:
                    return n
        return None

    # ------------------------------------------------------------------ polling
    def poll(self) -> list:
        """Events (dicts): time, volume (Level2Volume), complete, missing (chunks skipped), abandoned."""
        if self.cancelled:
            return []
        now = time.time()
        events = []
        if self.volume is None:
            n = aws.find_latest_volume(self.site)
            if n is None:
                return events
            self._start(n)
        elif now - self.last_progress > STALL_S and now >= self._next_check:
            moved = self._moved_on(now)
            if moved is not None:
                if self.stamp is not None and self.builder is not None and self.next_chunk > 1:
                    events.append(self._event(True, abandoned=True))       # it ended without us seeing the end
                self.prev_stamp = self.stamp or self.prev_stamp
                self._start(moved)
            else:
                self._next_check = now + 30.0
        if self.cancelled:
            return events
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
        # the chunks to add now: in order, skipping a gap only once it has lasted GAP_WAIT seconds
        take, expected, waiting = [], self.next_chunk, False
        for c in chunks:
            if c.number < expected:
                continue
            if c.number > expected:
                if self.gap_since is None:
                    self.gap_since = now
                if now - self.gap_since < GAP_WAIT:
                    waiting = True
                    break
                self.missing.extend(range(expected, c.number))
                self.gap_since = None
            take.append(c)
            expected = c.number + 1
        if not waiting:
            self.gap_since = None
        if not take:
            return events
        got = []
        for c, (_k, data) in zip(take, aws.fetch_many(aws.CHUNK_BUCKET, [c.key for c in take], cache=False,
                                                      timeout=30, cancelled=lambda: self.cancelled)):
            if isinstance(data, Exception):
                break                           # try again from this chunk next time
            got.append((c, data))
            if c.kind == "E":
                break
        if self.cancelled or not got:
            return events
        vol = None
        if len(got) >= BATCH:
            # catching up on a volume: parse in a decoder process, off the app's own threads
            self.builder, oks, vol = run_isolated(add_chunks, self.builder, [d for _c, d in got], self.site,
                                                  f"chunks {self.volume}")
        else:
            oks = []
            for _c, data in got:
                try:
                    self.builder.add_bytes(data)
                    oks.append(True)
                except Exception:               # a damaged chunk: treat it as missing
                    oks.append(False)
        for (c, _d), ok in zip(got, oks):
            if not ok:
                self.missing.append(c.number)
            self.next_chunk = c.number + 1
            if c.kind == "E":
                self.complete = True
        if self.cancelled:
            return events
        self.last_progress = time.time()
        events.append(self._event(self.complete, vol=vol))
        if self.complete:
            self.prev_stamp = self.stamp
            self._start(aws.next_volume_number(self.volume))
        return events

    def load_previous(self, newer_than=None):
        """The volume before the one being followed, from its chunks, if it's finished and newer than
        `newer_than` (datetime; e.g. the newest archive file). Fills the gap the archive is still behind on."""
        if self.volume is None or self.cancelled:
            return None
        n = self.volume - 1 if self.volume > 1 else 999
        chunks = aws.list_chunks(self.site, n)
        if not chunks or chunks[-1].kind != "E" or chunks[0].stamp >= (self.stamp or "99999999"):
            return None
        if newer_than is not None and chunks[0].time <= newer_than:
            return None
        missing = len(range(1, chunks[-1].number + 1)) - len(chunks)
        datas = []
        for _k, data in aws.fetch_many(aws.CHUNK_BUCKET, [c.key for c in chunks], cancelled=lambda: self.cancelled):
            if isinstance(data, Exception):
                missing += 1
                continue
            datas.append(data)
        if self.cancelled:
            return None
        _b, oks, vol = run_isolated(add_chunks, SweepBuilder(), datas, self.site, f"chunks {n}")   # (a decoder process)
        missing += oks.count(False)
        vol.complete = missing == 0
        return {"time": chunks[0].time, "volume": vol, "complete": True, "missing": missing, "abandoned": False,
                "number": n}
