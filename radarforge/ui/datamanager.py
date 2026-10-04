"""Loads frames for live, archive and local-file modes (all network/decoding off the UI thread)."""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from ..data import aws
from ..data.frames import VOLUMES, Frame
from ..data.level2 import read_level2
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
        self._sync_busy = False
        self._last_sync = 0.0
        self._loading = 0
        self._gen = 0                   # bumps when mode/site changes -> stale jobs drop results
        self._lock = threading.RLock()
        self._tracker: ChunkTracker | None = None
        self._l3_seen: set = set()
        self._l3_alt: dict = {}          # requested code -> older code this radar actually has (N0B -> N0Q)
        self._pending_l3: list = []       # archive Level III waiting for its Level II volumes to arrive
        self._l2_expected = False
        self._l3_busy = False
        self._last_l3_poll = 0.0
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._pollSoon.connect(lambda: QTimer.singleShot(4000, self._poll_live_now))
        VOLUMES.capacity = int(settings["volume_cache"])
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
        with self._lock:
            self.frames.append(frame)
            self.frames.sort(key=lambda f: f.time)

    def _trim(self):
        n = int(self.settings["loop_frames"])
        with self._lock:
            if self.mode == "live" and len(self.frames) > n:
                self.frames = self.frames[-n:]

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

    def attach_l3(self, prod):
        """Attach a Level III product to its volume's frame, carrying it forward to later frames."""
        t = prod.vol_time or prod.time
        with self._lock:
            has_l2 = any(f.has_level2() for f in self.frames)
            if not has_l2 and self._l2_expected:
                self._pending_l3.append(prod)          # archive: wait for the Level II volumes
                return
            target = self._frame_for_l3(t) if has_l2 else self._find_frame(t, 60)
            if target is None:
                if not has_l2:
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
                if (f.time - t).total_seconds() > 12 * 60:
                    break
                cur = f.l3.get(prod.awips)
                cur_t = (cur.vol_time or cur.time) if cur is not None else None
                if cur is None or (cur_t is not None and cur_t < t):
                    f.l3[prod.awips] = prod
                    f.revision += 1
                    touched.append(f)
        for f in touched:
            self.frameUpdated.emit(f)

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
                self._load_l2_files(gen, files)
                if gen == self._gen:
                    self._flush_pending_l3()
                if self.l3_needed and gen == self._gen:
                    self._archive_l3(gen, t0 - timedelta(minutes=12), t1 + timedelta(minutes=8),
                                     set(self.l3_needed))
                if gen == self._gen:
                    self.status.emit(f"Archive: {len(self.frames)} {site_id} frames loaded")
            self._submit(work)

    def _reset(self):
        self._gen += 1
        with self._lock:
            self.frames = []
            self._pending_l3 = []
            self._l2_expected = False
        self._l3_seen.clear()
        self._l3_alt.clear()
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
        self._tracker = ChunkTracker(self.site_id)
        self._last_sync = time.time()
        self.status.emit(f"Live: loading the newest {self.site_id} volume…")
        self._submit_live(self._live_first)
        self._submit(self._live_backfill)
        self._poll_l3(force=True)
        self._live_timer.start(max(5, int(self.settings["live_poll_seconds"])) * 1000)

    def reload_live(self):
        """Start the live feed for this radar again from scratch (Radar → Reload live data)."""
        if self.mode == "live":
            self.start_live()

    def stop_live(self):
        self._live_timer.stop()
        if self._tracker is not None:
            self._tracker.cancelled = True          # an in-flight poll for the old radar stops early
        self._tracker = None
        if self.mode == "live":
            self.mode = "idle"

    def _live_first(self, gen):
        self._live_poll_once(gen)
        tr = self._tracker
        if tr is None or gen != self._gen:
            return
        # the volume between the newest archive file and the live one: the archive is a few minutes behind
        try:
            newest = aws.latest_level2(self.site_id, 1)
            ev = tr.load_previous(newest[-1].time + timedelta(seconds=60) if newest else None)
        except Exception:
            ev = None
        if ev is not None and gen == self._gen and not tr.cancelled:
            self._apply_live_event(ev)

    def _live_backfill(self, gen):
        n = max(1, int(self.settings["loop_frames"]) - 1)
        try:
            files = aws.latest_level2(self.site_id, n)
        except Exception as exc:
            self.status.emit(f"Live: couldn't list earlier volumes ({exc}) – retrying shortly")
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
        total = len(files)
        done = 0
        self.progress.emit(0, total)

        def one(f):
            aws.fetch(aws.L2_BUCKET, f.key)
            return f, aws.cached_path(aws.L2_BUCKET, f.key)
        futures = [self._dl_pool.submit(one, f) for f in files]
        failed = 0
        for fut in futures:
            try:
                f, path = fut.result()
            except Exception as exc:
                failed += 1
                self.status.emit(f"Download failed: {exc}")
                done += 1
                continue
            if gen != self._gen:
                return
            with self._lock:
                existing = self._find_frame(f.time, 60)
                if existing is None:
                    self._insert_frame(Frame(self.site_id, f.time, l2_path=str(path), label=f.name))
                elif not existing.has_level2():
                    existing.l2_path = str(path)
                    existing.l2_rev += 1
                elif existing.from_chunks and existing.gaps and not existing.live:
                    existing.replace_with_file(str(path))
            done += 1
            self.progress.emit(done, total)
            self._emit_frames()
        if failed and self.mode == "live":
            self._last_sync = 0.0                  # try the missing ones again at the next tick
        self._trim()
        self._emit_frames()

    def _live_tick(self):
        if self.mode != "live":
            return
        tr = self._tracker
        if tr is not None and not tr.busy:
            self._submit_live(self._live_poll_once)
        now = time.time()
        if now - self._last_l3_poll > 60:
            self._poll_l3()
        if now - self._last_sync > 90 and not self._sync_busy:
            self._last_sync = now
            self._submit(self._archive_sync)

    def _poll_live_now(self):
        tr = self._tracker
        if self.mode == "live" and tr is not None and not tr.busy:
            self._submit_live(self._live_poll_once)

    def _live_poll_once(self, gen):
        tr = self._tracker
        if tr is None or tr.busy or gen != self._gen:
            return
        tr.busy = True
        try:
            events = tr.poll()
            for ev in events:
                if gen != self._gen or tr.cancelled:
                    return
                self._apply_live_event(ev)
            if events and not events[-1]["complete"] or tr.gap_since is not None:
                self._pollSoon.emit()       # mid-volume (or waiting on a late chunk): check again soon
        except Exception as exc:
            if gen == self._gen:
                self.status.emit(f"Live update failed ({type(exc).__name__}: {exc}) – retrying")
        finally:
            tr.busy = False

    def _apply_live_event(self, ev):
        t, vol, complete = ev["time"], ev["volume"], ev["complete"]
        with self._lock:
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
        if self._sync_busy:
            return
        self._sync_busy = True
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
            for lf, f in todo:
                try:
                    aws.fetch(aws.L2_BUCKET, lf.key)
                except Exception:
                    continue
                if gen != self._gen:
                    return
                path = str(aws.cached_path(aws.L2_BUCKET, lf.key))
                with self._lock:
                    if f is None:
                        if self._find_frame(lf.time, 60) is not None:
                            continue
                        self._insert_frame(Frame(self.site_id, lf.time, l2_path=path, label=lf.name))
                    else:
                        f.replace_with_file(path)
                if f is None:
                    self._trim()
                    self._emit_frames()
                else:
                    self.frameUpdated.emit(f)
                    self.status.emit(f"Live {self.site_id}: volume {lf.time:%H:%M:%S}Z replaced with the "
                                     "complete archive file")
        finally:
            self._sync_busy = False

    def _poll_l3(self, force=False):
        if self._l3_busy or not self.l3_needed or self.site is None:
            return
        self._l3_busy = True
        self._last_l3_poll = time.time()
        codes = set(self.l3_needed)

        def work(gen):
            try:
                n = int(self.settings["loop_frames"])
                for want in sorted(codes):
                    code = self._l3_alt.get(want, want)
                    try:
                        files = aws.latest_level3(self.site.l3_id, code, count=n if force else 2)
                        if not files and want not in self._l3_alt:
                            for alt in l3_fallbacks(want):
                                files = aws.latest_level3(self.site.l3_id, alt, count=n if force else 2)
                                if files:
                                    code = self._l3_alt[want] = alt
                                    break
                    except Exception as exc:
                        self.status.emit(f"Level III {code}: {exc}")
                        continue
                    for lf in files:
                        if lf.key in self._l3_seen:
                            continue
                        self._l3_seen.add(lf.key)
                        try:
                            data = aws.fetch(aws.L3_BUCKET, lf.key)
                            prod = read_level3(data, awips_hint=code, site_hint=self.site_id)
                        except Exception as exc:
                            self.status.emit(f"Level III {code} decode failed: {exc}")
                            continue
                        if gen != self._gen:
                            return
                        self.attach_l3(prod)
            finally:
                self._l3_busy = False
        self._submit(work)

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
            if l3 and self.l3_needed and gen == self._gen:
                self._archive_l3(gen, start, end, set(self.l3_needed))
            if gen == self._gen:
                self.status.emit(f"Archive: {len(self.frames)} frames loaded "
                                 f"({files[0].time:%Y-%m-%d %H:%M}–{files[-1].time:%H:%M}Z)")
        self._submit(work)

    def _archive_l3(self, gen, start, end, codes):
        site = self.site
        if site is None:
            return
        for want in sorted(codes):
            code = self._l3_alt.get(want, want)
            try:
                files = aws.list_level3_range(site.l3_id, code, start, end)
                if not files:                      # older cases: the same data under its older code
                    for alt in l3_fallbacks(want):
                        files = aws.list_level3_range(site.l3_id, alt, start, end)
                        if files:
                            code = self._l3_alt[want] = alt
                            break
            except Exception as exc:
                self.status.emit(f"Level III {code} listing failed: {exc}")
                continue
            if not files:
                self.status.emit(f"No Level III {want} for {site.id} in this period")
                continue
            futs = []
            for lf in files:
                if lf.key in self._l3_seen:
                    continue
                self._l3_seen.add(lf.key)
                futs.append(self._dl_pool.submit(lambda k=lf.key: aws.fetch(aws.L3_BUCKET, k)))
            for fut in futs:
                try:
                    prod = read_level3(fut.result(), awips_hint=code, site_hint=self.site_id)
                except Exception:
                    continue
                if gen != self._gen:
                    return
                self.attach_l3(prod)

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
                    vol = read_level2(p)
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
                    self.attach_l3(prod)
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
