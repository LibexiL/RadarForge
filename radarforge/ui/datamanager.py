"""Loads frames for live, archive and local-file modes (all network/decoding off the UI thread)."""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from ..data import aws
from ..data.frames import VOLUMES, Frame, volume_capacity
from ..data.level2 import DecodeCancelled, decode_slot, decoder_count
from ..data.level3 import read_level3_isolated as read_level3
from ..data.live import ChunkTracker
from ..data.sites import get_site
from ..products.catalog import l3_fallbacks


class DataManager(QObject):
    framesChanged = Signal()
    frameUpdated = Signal(object)
    status = Signal(str)
    progress = Signal(int, int)
    error = Signal(str)
    loadingChanged = Signal(bool)
    frameDecoded = Signal()          # a volume was decoded ahead of time (its images can be made now)
    _pollSoon = Signal()             # chunks are flowing: look again in a few seconds

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.site_id = settings["site"]
        self.mode = "idle"
        self.frames: list = []
        self.l3_needed: set = set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rf-data")
        # live chunk polling gets its own thread so archive downloads can never hold it up
        self._live_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rf-live")
        self._dl_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rf-download")   # volume files
        # Level III files (small) get their own downloads, so they never wait behind volume files
        self._l3_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rf-l3")
        # downloaded volumes are decoded straight away (newest first) instead of when the loop first plays them
        self._decode_pool = ThreadPoolExecutor(max_workers=decoder_count(), thread_name_prefix="rf-decode")
        self._sync_gen = -1              # generation an archive sync is running for (-1: none)
        self._last_sync = 0.0
        self._loading = 0
        self._gen = 0                   # bumps when mode/site changes -> stale jobs drop results
        self._lock = threading.RLock()
        self._tracker: ChunkTracker | None = None
        self._poller = None              # live Level II from a polling server (data/polling.py) instead of AWS
        self.source_name = "NOAA on AWS"
        self._l3_seen: set = set()
        self._l3_alt: dict = {}          # requested code -> older code this radar actually has (N0B -> N0Q)
        self._pending_l3: list = []       # archive Level III waiting for its Level II volumes to arrive
        self._l2_expected = False
        self._l3_gen = -1                 # generation a Level III poll is running for (-1: none)
        self._last_l3_poll = 0.0
        self._l3_last: dict = {}          # code -> newest key listed (live: later polls list only what's newer)
        self._l3_fail: dict = {}          # key -> failed downloads/decodes (tried again, up to 3 times)
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._pollSoon.connect(lambda: QTimer.singleShot(4000, self._poll_live_now))
        VOLUMES.capacity = volume_capacity(settings)
        # keep the download cache from filling the disk (old files first, then down to 3 GB)
        threading.Thread(target=aws.prune_cache, daemon=True, name="rf-prune").start()

    # ------------------------------------------------------------------ helpers
    @property
    def site(self):
        return get_site(self.site_id)

    def _emit_frames(self):
        self.framesChanged.emit()

    def _submit(self, fn, *args, pool=None):
        gen = self._gen

        def wrap():
            try:
                fn(gen, *args)
            except Exception as exc:
                traceback.print_exc()
                if gen == self._gen:
                    self.error.emit(f"{type(exc).__name__}: {exc}")
        return (pool or self._pool).submit(wrap)

    def _submit_live(self, fn, *args):
        return self._submit(fn, *args, pool=self._live_pool)

    def _find_frame(self, t: datetime, tol=150.0):
        best, bd = None, tol
        for f in self.frames:
            d = abs((f.time - t).total_seconds())
            if d <= bd:
                best, bd = f, d
        return best

    def _insert_frame(self, frame):
        # a new list, never one changed in place: the window reads self.frames from the UI thread without the
        # lock, and a list being sorted looks empty to other threads until the sort ends
        with self._lock:
            self.frames = sorted(self.frames + [frame], key=lambda f: f.time)
            self._inherit_l3(frame)

    L3_CARRY_S = 12 * 60     # a Level III product stays on later frames for this long (until a newer one comes)

    def _inherit_l3(self, frame):
        """A new frame starts with the Level III products of the frame before it (up to 12 minutes old) until
        its own arrive: a new live volume has no N0G / N0B for its first minutes, which left the panel empty
        for much of every volume."""
        i = self.frames.index(frame)
        if i == 0:
            return
        prev = self.frames[i - 1]
        add = {}
        for code, prod in prev.l3.items():
            if code in frame.l3:
                continue
            t = prod.vol_time or prod.time
            if t is not None and 0 <= (frame.time - t).total_seconds() <= self.L3_CARRY_S:
                add[code] = prod
        if add:
            frame.l3 = {**frame.l3, **add}       # (a new dict: the UI thread may be reading the old one)

    def _trim(self):
        n = int(self.settings["loop_frames"])
        with self._lock:
            if self.mode == "live" and len(self.frames) > n:
                self.frames = self.frames[-n:]

    def _decode_ahead(self, gen, frame):
        """Decode a frame's downloaded volume now, so the loop is ready about when its last file arrives
        (rather than decoding one volume at a time once everything is in). Newest first, and always behind
        anything on screen."""
        path = frame.l2_path
        if not path or frame._l2 is not None or VOLUMES.peek(path) is not None:
            return

        def stale():
            return gen != self._gen or frame.l2_path != path

        def job():
            if stale():
                return
            with self._lock:
                filed = [f for f in self.frames if f.l2_path and f._l2 is None]
            if frame not in filed:
                return
            age = len(filed) - 1 - filed.index(frame)            # 0 = the newest volume file
            # (a loop longer than the volume cache holds is decoded too: each frame's images are made as its
            # volume comes in, so the volume can go again)
            try:
                with decode_slot(10 + age, cancelled=stale):     # (waits its turn before claiming the volume)
                    if stale() or VOLUMES.peek(path) is not None:
                        return
                    VOLUMES.get(path, frame.site)
            except DecodeCancelled:
                return
            except Exception:
                return                       # the panel reports it if the frame is shown
            if not stale():
                self.frameDecoded.emit()
        self._decode_pool.submit(job)

    def _frame_for_l3(self, t):
        """The Level II frame a Level III product belongs to: the volume that started at or just before its
        volume time (products of a SAILS cut or late tilt carry the volume's start time, give or take)."""
        best = None
        for f in self.frames:
            if not f.has_level2():
                continue
            d = (t - f.time).total_seconds()
            if -90 <= d <= 15 * 60 and (best is None or f.time > best.time):
                best = f
        return best

    def attach_l3(self, prod, gen=None) -> bool:
        """Attach a Level III product to its volume's frame, carrying it forward to later frames. [gen]: the
        generation it was fetched for (checked under the lock: a product for a radar just left never lands).
        Returns False when it was older than the live loop reaches (so older ones needn't be fetched)."""
        t = prod.vol_time or prod.time
        with self._lock:
            if gen is not None and gen != self._gen:
                return False
            if self._l2_expected:
                self._pending_l3.append(prod)          # archive: wait until the Level II volumes are all in
                return True
            has_l2 = any(f.has_level2() for f in self.frames)
            loop = int(self.settings["loop_frames"]) if self.mode == "live" else 0

            def room():                                # (live) a frame for it would still be in the loop
                return not loop or sum(1 for f in self.frames if (f.time - t).total_seconds() > 60) < loop
            target = self._frame_for_l3(t) if has_l2 else self._find_frame(t, 60)
            if has_l2 and self._loading > 0 and (target is None or abs((t - target.time).total_seconds()) > 90):
                # the loop is still downloading and this product's own volume isn't in yet: give it a frame of
                # its own (the volume joins that frame when it arrives) rather than an older volume's frame
                own = self._find_frame(t, 60)
                if own is None and room():
                    own = Frame(self.site_id, t, label="L3")
                    self._insert_frame(own)
                if own is not None:
                    target = own
            if target is None:
                if not has_l2:
                    if not room():
                        return False
                    # Level III only: every product time becomes its own frame
                    target = Frame(self.site_id, t, label="L3")
                    self._insert_frame(target)
                # otherwise it is only carried forward to later frames (never shown before it existed)
            touched = []
            if target is not None:
                cur = target.l3.get(prod.awips)
                cur_t = (cur.vol_time or cur.time) if cur is not None else None
                if cur is None or cur_t is None or cur_t <= t:
                    target.add_l3(prod)
                    touched.append(target)
            for f in self.frames:
                if f.time <= (target.time if target else t):
                    continue
                if (f.time - t).total_seconds() > self.L3_CARRY_S:
                    break
                cur = f.l3.get(prod.awips)
                cur_t = (cur.vol_time or cur.time) if cur is not None else None
                if cur is None or (cur_t is not None and cur_t < t):
                    f.l3 = {**f.l3, prod.awips: prod}      # (a new dict: the UI may be reading the old one)
                    f.revision += 1
                    touched.append(f)
            too_old = target is None and not room()
        for f in touched:
            self.frameUpdated.emit(f)
        return not too_old

    def _flush_pending_l3(self):
        with self._lock:
            pending, self._pending_l3 = self._pending_l3, []
            self._l2_expected = False
        for prod in sorted(pending, key=lambda p: p.vol_time or p.time):
            self.attach_l3(prod)
        if pending:
            self._emit_frames()

    # ------------------------------------------------------------------ site / modes
    def set_site(self, site_id: str):
        site_id = site_id.upper()
        if get_site(site_id) is None:
            self.error.emit(f"Unknown radar site {site_id}")
            return
        self.site_id = site_id
        self.settings["site"] = site_id
        self.settings.save()
        mode = self.mode
        window = (self.frames[0].time, self.frames[-1].time) if self.frames else None
        self.stop_live()
        self._reset()
        if mode == "live":
            self.start_live()
        elif mode == "archive" and window is not None:
            # same period, new radar
            self.mode = "archive"
            t0, t1 = window
            self.status.emit(f"Archive: finding {site_id} volumes for {t0:%Y-%m-%d %H:%M}–{t1:%H:%M}Z…")

            self._l2_expected = True

            def work(gen):
                files = aws.list_level2_range(site_id, t0 - timedelta(minutes=3), t1 + timedelta(minutes=3))
                if gen != self._gen:
                    return
                if not files:
                    self.status.emit(f"No {site_id} volumes in that period")
                    self._flush_pending_l3()
                    return
                self._load_l2_files(gen, list(reversed(files)))     # newest first: the view follows it
                if gen == self._gen:
                    self._flush_pending_l3()
                    self.status.emit(f"Archive: {len(self.frames)} {site_id} frames loaded")
            self._submit(work)
            if self.l3_needed:          # Level III downloads alongside (held until the volumes are in)
                self._submit(self._archive_l3, t0 - timedelta(minutes=12), t1 + timedelta(minutes=8),
                             set(self.l3_needed))

    def _reset(self):
        with self._lock:                 # (work for the old generation checks it under this lock before it writes)
            self._gen += 1
            self.frames = []
            self._pending_l3 = []
            self._l2_expected = False
            self._l3_seen.clear()
            self._l3_alt.clear()
            self._l3_last.clear()
            self._l3_fail.clear()
        self._emit_frames()

    def set_l3_needed(self, codes: set):
        new = set(c for c in codes if c) - self.l3_needed
        self.l3_needed = set(c for c in codes if c)
        if new and self.mode == "live":
            self._last_l3_poll = 0
            self._poll_l3()
        elif new and self.mode == "archive" and self.frames:
            self._submit(self._archive_l3, self.frames[0].time - timedelta(minutes=12),
                         self.frames[-1].time + timedelta(minutes=2), set(new))

    # ------------------------------------------------------------------ live
    def start_live(self):
        """Newest volume first (from the live chunks), then the loop is filled in from the archive,
        newest first; Level III starts at the same time."""
        self.stop_live()
        self._reset()
        self.mode = "live"
        self._last_sync = time.time()
        src = self.settings["l2_source"] or "aws"
        if src != "aws":
            from ..data.polling import PollingClient, server_label
            self._poller = PollingClient(src, self.site_id)
            self.source_name = server_label(src, self.settings["polling_servers"])
            self.status.emit(f"Live: loading {self.site_id} from {self.source_name}…")
            self._submit_live(self._polled_first)
        else:
            self._start_aws_live()
        self._poll_l3(force=True)
        self._live_timer.start(max(5, int(self.settings["live_poll_seconds"])) * 1000)

    def _start_aws_live(self, gen=None):
        """Live Level II from NOAA's buckets: the newest volume from the chunks, the loop from the archive."""
        self._poller = None
        self.source_name = "NOAA on AWS"
        self._tracker = ChunkTracker(self.site_id)
        self.status.emit(f"Live: loading the newest {self.site_id} volume…")
        if gen is None:
            self._submit_live(self._live_first)
            self._submit(self._live_backfill)
        else:                          # (already on the live thread: a polling server failed)
            self._submit(self._live_backfill)
            self._live_first(gen)

    # ---- polling server
    def _polled_first(self, gen):
        pc = self._poller
        if pc is None or gen != self._gen:
            return
        from ..data.polling import safe_url
        try:
            files = pc.list()
            if not files:
                raise FileNotFoundError(f"{safe_url(pc.base)} lists no {self.site_id} volumes")
        except Exception as exc:
            if gen == self._gen:
                self.status.emit(f"Polling server: {aws.friendly_error(exc)} – using NOAA on AWS instead")
                self._start_aws_live(gen)
            return
        n = max(1, int(self.settings["loop_frames"]))
        files = files[-n:]

        def fetch(pf):
            if gen != self._gen or pc.cancelled:
                return None
            return pc.fetch(pf)
        # the earlier volumes download while the newest one does (it shows first)
        futs = [(pf, self._dl_pool.submit(fetch, pf)) for pf in reversed(files[:-1])]
        self._polled_apply(gen, files[-1], live=True)
        with self._lock:
            self._loading += 1
            first = self._loading == 1
        if first:
            self.loadingChanged.emit(True)
        try:
            for i, (pf, fut) in enumerate(futs):
                if gen != self._gen:
                    for _pf, f in futs:
                        f.cancel()
                    return
                self.progress.emit(i, len(futs))
                try:
                    path = fut.result()
                except Exception as exc:
                    self.status.emit(f"Polling server: {pf.name} – {aws.friendly_error(exc)}")
                    continue
                if gen != self._gen:
                    return
                if path:
                    self._polled_frame(pf, path, live=False, gen=gen)
            self.progress.emit(len(futs), len(futs))
        finally:
            with self._lock:
                self._loading -= 1
                last = self._loading == 0
            if last:
                self.loadingChanged.emit(False)
        self._trim()
        self._emit_frames()

    def _polled_apply(self, gen, pf, live):
        pc = self._poller
        try:
            path = pc.fetch(pf)
            pc.failures = 0
        except Exception as exc:
            pc.failures += 1
            self.status.emit(f"Live ({self.source_name}): {pf.name} – {aws.friendly_error(exc)} – retrying")
            return False
        if path and gen == self._gen:
            self._polled_frame(pf, path, live, gen=gen)
        return bool(path)

    def _polled_frame(self, pf, path, live, gen=None):
        with self._lock:
            if gen is not None and gen != self._gen:
                return
            f = self._find_frame(pf.time, 60)
            if f is None:
                f = Frame(self.site_id, pf.time, l2_path=path, live=live, label=pf.name)
                self._insert_frame(f)
                new = True
            else:
                f.l2_path = path
                f._l2 = None
                f.l2_rev += 1
                f.revision += 1
                f.live = live
                new = False
        self._trim()
        if new:
            self._emit_frames()
        else:
            self.frameUpdated.emit(f)
        if not live and gen is not None:
            self._decode_ahead(gen, f)
        if live:
            self.status.emit(f"Live {self.site_id} ({self.source_name}): volume {pf.time:%H:%M:%S}Z "
                             f"— updated {datetime.now(timezone.utc):%H:%M:%S}Z")

    def _polled_tick(self, gen):
        pc = self._poller
        if pc is None or pc.busy or gen != self._gen:
            return
        pc.busy = True
        try:
            try:
                files = pc.list()
                pc.failures = 0
            except Exception as exc:
                pc.failures += 1
                if gen == self._gen:
                    self.status.emit(f"Live ({self.source_name}): {aws.friendly_error(exc)} – retrying")
                return
            if not files:
                return
            with self._lock:
                newest_known = max((f.time for f in self.frames if f.has_level2()), default=None)
            n = max(1, int(self.settings["loop_frames"]))
            for pf in files[-n:]:
                if gen != self._gen or pc.cancelled:
                    return
                is_newest = pf is files[-1]
                had = pc.known_size(pf.name)
                if had and pf.size is not None and pf.size <= had and not is_newest:
                    with self._lock:
                        f = self._find_frame(pf.time, 60)
                    if f is not None and f.live:            # finished: no longer the volume being scanned
                        f.live = False
                        self.frameUpdated.emit(f)
                    continue
                if not had and newest_known is not None and pf.time < newest_known and \
                        self._find_frame(pf.time, 60) is not None:
                    continue
                self._polled_apply(gen, pf, live=is_newest)
            pc.forget_older({pf.name for pf in files[-n:]})
        finally:
            pc.busy = False

    def loop_length_changed(self):
        """The loop length setting changed (live): drop the oldest frames, or load the extra ones straight away."""
        self._trim()
        self._emit_frames()
        if self.mode != "live":
            return
        if self._poller is not None:
            if not self._poller.busy:
                self._submit_live(self._polled_tick)
        elif self._sync_gen != self._gen:
            self._last_sync = time.time()
            self._submit(self._archive_sync)
        else:
            self._last_sync = 0.0               # a sync is running: another one at the next tick

    def reload_live(self):
        """Start the live feed for this radar again from scratch (Radar → Reload live data)."""
        if self.mode == "live":
            self.start_live()

    def stop_live(self):
        self._live_timer.stop()
        if self._poller is not None:
            self._poller.cancelled = True
        self._poller = None
        if self._tracker is not None:
            self._tracker.cancelled = True          # an in-flight poll for the old radar stops early
        self._tracker = None
        if self.mode == "live":
            self.mode = "idle"

    def _live_first(self, gen):
        tr = self._tracker                  # (this radar's: another may have been chosen by the time it's done)
        self._live_poll_once(gen, tr)
        if tr is None or gen != self._gen or tr.cancelled:
            return
        # the volume between the newest archive file and the live one: the archive is a few minutes behind
        try:
            newest = aws.latest_level2(tr.site, 1)
            ev = tr.load_previous(newest[-1].time + timedelta(seconds=60) if newest else None)
        except Exception:
            ev = None
        if ev is not None and gen == self._gen and not tr.cancelled:
            self._apply_live_event(ev, gen)

    def _live_backfill(self, gen):
        n = max(1, int(self.settings["loop_frames"]) - 1)
        try:
            files = aws.latest_level2(self.site_id, n)
        except Exception as exc:
            self.status.emit(f"Live: couldn't list earlier volumes ({aws.friendly_error(exc)}) – retrying shortly")
            self._last_sync = 0.0
            return
        if gen != self._gen:
            return
        self._load_l2_files(gen, list(reversed(files)))       # newest first

    @property
    def loading(self) -> bool:
        return self._loading > 0

    def _load_l2_files(self, gen, files):
        with self._lock:
            self._loading += 1
            first = self._loading == 1
        if first:
            self.loadingChanged.emit(True)
        try:
            self._load_l2_files_inner(gen, files)
        finally:
            with self._lock:
                self._loading -= 1
                last = self._loading == 0
            if last:
                self.loadingChanged.emit(False)

    def _load_l2_files_inner(self, gen, files):
        """Downloads [files] (several at once, in the order given) and adds a frame for each as it arrives; each
        is decoded straight away. Choosing another radar stops what's left, mid-download too."""
        total = len(files)
        done = 0
        self.progress.emit(0, total)

        def stale():
            return gen != self._gen

        def one(f):
            return f, aws.fetch_file(aws.L2_BUCKET, f.key, cancelled=stale)
        # the first file on its own (it's what shows first), then the rest several at once
        futures = [self._dl_pool.submit(one, f) for f in files[:1]]
        if futures:
            wait(futures, timeout=4)
        if not stale():
            futures += [self._dl_pool.submit(one, f) for f in files[1:]]
        failed = 0
        try:
            for fut in futures:
                if stale():
                    return
                try:
                    f, path = fut.result()
                except aws.Cancelled:
                    return
                except Exception as exc:
                    failed += 1
                    if not stale():
                        self.status.emit(f"Download failed – {aws.friendly_error(exc)}")
                    done += 1
                    continue
                with self._lock:
                    if stale():                  # (checked under the lock: _reset clears the list under it)
                        return
                    frame = self._find_frame(f.time, 60)
                    if frame is None:
                        frame = Frame(self.site_id, f.time, l2_path=str(path), label=f.name)
                        self._insert_frame(frame)
                    elif not frame.has_level2():
                        frame.l2_path = str(path)
                        frame.l2_rev += 1
                        frame.revision += 1
                    elif frame.from_chunks and frame.gaps and not frame.live:
                        frame.replace_with_file(str(path))
                    else:
                        frame = None
                done += 1
                self.progress.emit(done, total)
                self._emit_frames()
                if frame is not None:
                    self._decode_ahead(gen, frame)
        finally:
            if stale():
                for fut in futures:
                    fut.cancel()
        if failed and self.mode == "live":
            self._last_sync = 0.0                  # try the missing ones again at the next tick
        self._trim()
        self._emit_frames()

    def _live_tick(self):
        if self.mode != "live":
            return
        if self._poller is not None:
            if not self._poller.busy:
                self._submit_live(self._polled_tick)
            if time.time() - self._last_l3_poll > self.L3_POLL_S:
                self._poll_l3()
            return
        tr = self._tracker
        if tr is not None and not tr.busy:
            self._submit_live(self._live_poll_once)
        now = time.time()
        if now - self._last_l3_poll > self.L3_POLL_S:
            self._poll_l3()
        if now - self._last_sync > 90 and self._sync_gen != self._gen:
            self._last_sync = now
            self._submit(self._archive_sync)

    def _poll_live_now(self):
        tr = self._tracker
        if self.mode == "live" and tr is not None and not tr.busy:
            self._submit_live(self._live_poll_once)

    def _live_poll_once(self, gen, tr=None):
        tr = tr or self._tracker
        if tr is None or tr.busy or gen != self._gen:
            return
        tr.busy = True
        try:
            events = tr.poll()
            for ev in events:
                if gen != self._gen or tr.cancelled:
                    return
                self._apply_live_event(ev, gen)
            if events and not events[-1]["complete"] or tr.gap_since is not None:
                self._pollSoon.emit()       # mid-volume (or waiting on a late chunk): check again soon
        except Exception as exc:
            if gen == self._gen:
                self.status.emit(f"Live update failed ({aws.friendly_error(exc)}) – retrying")
        finally:
            tr.busy = False

    def _apply_live_event(self, ev, gen=None):
        t, vol, complete = ev["time"], ev["volume"], ev["complete"]
        with self._lock:
            if gen is not None and gen != self._gen:
                return                        # (another radar was chosen meanwhile)
            f = self._find_frame(t, 60)
            if f is None:
                f = Frame(self.site_id, t, l2_volume=vol, live=not complete, label="live")
                self._insert_frame(f)
                new = True
            else:
                if f.l2_path:
                    return                    # the complete archive copy is already there
                new = False
                f.set_level2(vol)
                f.live = not complete
            f.gaps = int(ev.get("missing") or 0) + (1 if ev.get("abandoned") else 0)
        self._trim()
        if new:
            self._emit_frames()
        else:
            self.frameUpdated.emit(f)
        ns = len(vol.sweeps)
        msg = f"Live {self.site_id}: volume {t:%H:%M:%S}Z, {ns} sweeps" + (" (complete)" if complete else
                                                                           " (scanning)")
        if f.gaps:
            msg += f" – {ev.get('missing') or 0} chunk(s) never arrived; the full file will replace it"
        self.status.emit(msg + f" — updated {datetime.now(timezone.utc):%H:%M:%S}Z")

    def _archive_sync(self, gen):
        """Every 90 s (live): fill holes in the loop from the archive, and swap volumes that were built from
        incomplete chunks for the complete archive files once those are published."""
        if self._sync_gen == gen or gen != self._gen:
            return
        self._sync_gen = gen
        try:
            n = int(self.settings["loop_frames"])
            try:
                files = aws.latest_level2(self.site_id, n)
            except Exception:
                self._last_sync = time.time() - 60          # try again in about 30 s
                return
            if gen != self._gen:
                return
            with self._lock:
                frames = list(self.frames)
            oldest = frames[0].time if frames else None
            todo = []
            for lf in reversed(files):                      # newest first
                with self._lock:
                    f = self._find_frame(lf.time, 60)
                if f is None:
                    if oldest is None or len(frames) < n or lf.time >= oldest:
                        todo.append((lf, None))
                elif f.from_chunks and f.gaps and not f.live:
                    todo.append((lf, f))
            def stale():
                return gen != self._gen
            futs = [(lf, f, self._dl_pool.submit(aws.fetch_file, aws.L2_BUCKET, lf.key, 60, stale))
                    for lf, f in todo]
            for lf, f, fut in futs:
                if stale():
                    for _a, _b, x in futs:
                        x.cancel()
                    return
                try:
                    path = str(fut.result())
                except Exception:
                    continue
                if stale():
                    return
                if not os.path.exists(path):
                    continue
                with self._lock:
                    if stale():
                        return
                    if f is None:
                        if self._find_frame(lf.time, 60) is not None:
                            continue
                        frame = Frame(self.site_id, lf.time, l2_path=path, label=lf.name)
                        self._insert_frame(frame)
                    else:
                        f.replace_with_file(path)
                        frame = f
                if f is None:
                    self._trim()
                    self._emit_frames()
                else:
                    self.frameUpdated.emit(f)
                    self.status.emit(f"Live {self.site_id}: volume {lf.time:%H:%M:%S}Z replaced with the "
                                     "complete archive file")
                self._decode_ahead(gen, frame)
        finally:
            if self._sync_gen == gen:
                self._sync_gen = -1

    L3_POLL_S = 20           # live: how often Level III is checked (a cheap request: only newer files are listed)

    def _l3_mark(self, key, ok, gen=None):
        """Remember a Level III file as done once it decoded; a failed one is tried again (up to 3 times)."""
        with self._lock:
            if gen is None or gen == self._gen:
                self._l3_mark_locked(key, ok)

    def _l3_mark_locked(self, key, ok):
        if ok:
            self._l3_seen.add(key)
            self._l3_fail.pop(key, None)
        else:
            n = self._l3_fail.get(key, 0) + 1
            self._l3_fail[key] = n
            if n >= 3:
                self._l3_seen.add(key)

    # SAILS repeats the lowest tilt (and its products) up to 4 times a volume: list enough files to reach back
    # to the oldest frame of the loop, not just the last few minutes
    L3_PER_VOLUME = 4

    def _poll_l3(self, force=False):
        if self._l3_gen == self._gen or not self.l3_needed or self.site is None:
            return                  # (one poll per radar at a time; one still running for another is dropped)
        gen = self._gen
        self._l3_gen = gen
        self._last_l3_poll = time.time()
        codes = set(self.l3_needed)
        site, site_id = self.site, self.site_id

        def work(gen):
            try:
                n = max(1, int(self.settings["loop_frames"])) * self.L3_PER_VOLUME
                for want in sorted(codes):
                    if gen != self._gen:
                        return
                    code = self._l3_alt.get(want, want)
                    try:
                        last = self._l3_last.get(code)
                        if force or last is None:
                            files = aws.latest_level3(site.l3_id, code, count=n)
                            if not files and want not in self._l3_alt:
                                for alt in l3_fallbacks(want):
                                    files = aws.latest_level3(site.l3_id, alt, count=n)
                                    if files:
                                        code = alt
                                        with self._lock:
                                            if gen == self._gen:
                                                self._l3_alt[want] = alt
                                        break
                        else:
                            files = aws.level3_after(site.l3_id, code, last)[-n:]
                    except Exception as exc:
                        if gen == self._gen:
                            self.status.emit(f"Level III {code}: {aws.friendly_error(exc)}")
                        continue
                    with self._lock:
                        if gen != self._gen:
                            return
                        if files:
                            self._l3_last[code] = max(self._l3_last.get(code, ""), files[-1].key)
                        # newest first, so the frame on screen gets its product first
                        keys = [lf.key for lf in reversed(files) if lf.key not in self._l3_seen]
                        # and the ones that failed before, once more
                        keys += [k for k in self._l3_fail if k.split("_")[1] == code and k not in self._l3_seen
                                 and k not in keys]
                    self._load_l3_keys(gen, keys, code, site_id)
            finally:
                if self._l3_gen == gen:
                    self._l3_gen = -1
        self._submit(work)

    def _load_l3_keys(self, gen, keys, code, site_id):
        """Downloads Level III files (several at once) and attaches each product as it decodes, in order."""
        def get(key):
            if gen != self._gen:
                raise aws.Cancelled(key)
            return aws.fetch(aws.L3_BUCKET, key)
        futs = [(key, self._l3_pool.submit(get, key)) for key in keys]
        try:
            for key, fut in futs:
                if gen != self._gen:
                    return
                try:
                    prod = read_level3(fut.result(), awips_hint=code, site_hint=site_id)
                except aws.Cancelled:
                    return
                except Exception:
                    try:                       # once more: a dropped download shouldn't lose the frame's product
                        if gen != self._gen:
                            return
                        prod = read_level3(aws.fetch(aws.L3_BUCKET, key), awips_hint=code, site_hint=site_id)
                    except Exception:
                        self._l3_mark(key, False, gen)
                        continue
                if gen != self._gen:
                    return
                self._l3_mark(key, True, gen)
                if not self.attach_l3(prod, gen) and gen == self._gen:
                    return                     # older than the loop: so are the rest (newest come first)
        finally:
            if gen != self._gen:
                for _k, fut in futs:
                    fut.cancel()

    # ------------------------------------------------------------------ archive
    def load_archive(self, files: list, l3: bool = True):
        """files: list of aws.L2File to load as frames."""
        self.stop_live()
        self._reset()
        self.mode = "archive"
        if not files:
            return
        self.status.emit(f"Archive: downloading {len(files)} volumes…")
        start = files[0].time - timedelta(minutes=12)
        end = files[-1].time + timedelta(minutes=8)
        self._l2_expected = True

        def work(gen):
            self._load_l2_files(gen, files)
            if gen == self._gen:
                self._flush_pending_l3()
                self.status.emit(f"Archive: {len(self.frames)} frames loaded "
                                 f"({files[0].time:%Y-%m-%d %H:%M}–{files[-1].time:%H:%M}Z)")
        self._submit(work)
        if l3 and self.l3_needed:       # Level III downloads alongside (held until the volumes are in)
            self._submit(self._archive_l3, start, end, set(self.l3_needed))

    def _archive_l3(self, gen, start, end, codes):
        site, site_id = self.site, self.site_id
        if site is None:
            return
        for want in sorted(codes):
            if gen != self._gen:
                return
            code = self._l3_alt.get(want, want)
            try:
                files = aws.list_level3_range(site.l3_id, code, start, end)
                if not files:                      # older cases: the same data under its older code
                    for alt in l3_fallbacks(want):
                        files = aws.list_level3_range(site.l3_id, alt, start, end)
                        if files:
                            code = alt
                            with self._lock:
                                if gen == self._gen:
                                    self._l3_alt[want] = alt
                            break
            except Exception as exc:
                if gen == self._gen:
                    self.status.emit(f"Level III {code} listing failed – {aws.friendly_error(exc)}")
                continue
            if gen != self._gen:
                return
            if not files:
                self.status.emit(f"No Level III {want} for {site.id} in this period")
                continue
            with self._lock:
                keys = [lf.key for lf in files if lf.key not in self._l3_seen]
            self._load_l3_keys(gen, keys, code, site_id)

    # ------------------------------------------------------------------ local files
    def open_local(self, paths: list):
        self.stop_live()
        self._reset()
        self.mode = "local"

        def work(gen):
            total = len(paths)
            for i, p in enumerate(sorted(paths)):
                if gen != self._gen:
                    return
                self.progress.emit(i, total)
                try:
                    vol = VOLUMES.get(p)          # decoded once: the frame uses this same decode
                    ok = len(vol.sweeps) > 0
                except Exception:
                    ok = False
                if ok:
                    if vol.site and get_site(vol.site) and vol.site != self.site_id:
                        self.site_id = vol.site
                    with self._lock:
                        f = self._find_frame(vol.start_time, 60)
                        if f is None:
                            self._insert_frame(Frame(vol.site or self.site_id, vol.start_time, l2_path=p,
                                                     label=os.path.basename(p)))
                        else:
                            f.l2_path = p
                            f.l2_rev += 1
                    self._emit_frames()
                    continue
                try:
                    prod = read_level3(p, awips_hint=_awips_from_name(p))
                    if prod.site and get_site("K" + prod.site[-3:]) and not self.frames:
                        pass
                    self.attach_l3(prod, gen)
                    self._emit_frames()
                except Exception as exc:
                    self.status.emit(f"Could not read {os.path.basename(p)}: {exc}")
            self.progress.emit(total, total)
            self.status.emit(f"Opened {total} file(s): {len(self.frames)} frames")
        self._submit(work)


def _awips_from_name(path: str) -> str:
    """Guess the Level III product code from common file names."""
    import re
    name = os.path.basename(path).upper()
    m = re.match(r"^[A-Z0-9]{3}_([A-Z0-9]{3})_\d{4}_", name)          # AWS: SSS_PPP_YYYY_...
    if m:
        return m.group(1)
    m = re.search(r"SDUS\d\d_([A-Z0-9]{3})[A-Z]{3}", name)           # NOAAPORT: ..._SDUS54_N0QTLX_...
    if m:
        return m.group(1)
    m = re.search(r"LEVEL3_[A-Z]{3}_([A-Z0-9]{3})_", name)
    if m:
        return m.group(1)
    m = re.search(r"_([A-Z0-9]{3})_\d{8}_\d{4}", name)                 # KDDC_N0Q_20200817_0501
    if m:
        return m.group(1)
    return ""
