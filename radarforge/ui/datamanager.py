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


class DataManager(QObject):
    framesChanged = Signal()
    frameUpdated = Signal(object)
    status = Signal(str)
    progress = Signal(int, int)
    error = Signal(str)
    loadingChanged = Signal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.site_id = settings["site"]
        self.mode = "idle"
        self.frames: list = []
        self.l3_needed: set = set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rf-data")
        self._loading = 0
        self._gen = 0                   # bumps when mode/site changes -> stale jobs drop results
        self._lock = threading.RLock()
        self._tracker: ChunkTracker | None = None
        self._live_busy = False
        self._l3_seen: set = set()
        self._l3_busy = False
        self._last_l3_poll = 0.0
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        VOLUMES.capacity = int(settings["volume_cache"])

    # ------------------------------------------------------------------ helpers
    @property
    def site(self):
        return get_site(self.site_id)

    def _emit_frames(self):
        self.framesChanged.emit()

    def _submit(self, fn, *args):
        gen = self._gen

        def wrap():
            try:
                fn(gen, *args)
            except Exception as exc:
                traceback.print_exc()
                if gen == self._gen:
                    self.error.emit(f"{type(exc).__name__}: {exc}")
        return self._pool.submit(wrap)

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

    def attach_l3(self, prod):
        """Attach a Level III product to its volume's frame, carrying it forward to later frames."""
        t = prod.vol_time or prod.time
        with self._lock:
            if not self.frames:
                f = Frame(self.site_id, t, label="L3")
                self.frames.append(f)
            target = self._find_frame(t, 60.0)
            if target is None:
                if not any(f.has_level2() for f in self.frames):
                    # Level III only: every product time becomes its own frame
                    target = Frame(self.site_id, t, label="L3")
                    self._insert_frame(target)
                # otherwise it is only carried forward to later frames (never shown before it existed)
            touched = []
            if target is not None:
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

            def work(gen):
                files = aws.list_level2_range(site_id, t0 - timedelta(minutes=3), t1 + timedelta(minutes=3))
                if gen != self._gen:
                    return
                if not files:
                    self.status.emit(f"No {site_id} volumes in that period")
                    return
                self._load_l2_files(gen, files)
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
        self._l3_seen.clear()
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
        self.stop_live()
        self._reset()
        self.mode = "live"
        self._tracker = ChunkTracker(self.site_id)
        self.status.emit(f"Live: loading recent {self.site_id} volumes…")
        self._submit(self._live_seed)
        self._live_timer.start(max(5, int(self.settings["live_poll_seconds"])) * 1000)

    def stop_live(self):
        self._live_timer.stop()
        self._tracker = None
        if self.mode == "live":
            self.mode = "idle"

    def _live_seed(self, gen):
        n = max(1, int(self.settings["loop_frames"]) - 1)
        files = aws.latest_level2(self.site_id, n)
        self._load_l2_files(gen, files)
        if gen != self._gen:
            return
        self._live_poll_once(gen)
        self._poll_l3(force=True)

    @property
    def loading(self) -> bool:
        return self._loading > 0

    def _load_l2_files(self, gen, files):
        self._loading += 1
        if self._loading == 1:
            self.loadingChanged.emit(True)
        try:
            self._load_l2_files_inner(gen, files)
        finally:
            self._loading -= 1
            if self._loading == 0:
                self.loadingChanged.emit(False)

    def _load_l2_files_inner(self, gen, files):
        total = len(files)
        done = 0
        self.progress.emit(0, total)

        def one(f):
            aws.fetch(aws.L2_BUCKET, f.key)
            return f, aws.cached_path(aws.L2_BUCKET, f.key)
        futures = [self._pool.submit(one, f) for f in files]
        for fut in futures:
            try:
                f, path = fut.result()
            except Exception as exc:
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
            done += 1
            self.progress.emit(done, total)
            self._emit_frames()
        self._trim()
        self._emit_frames()

    def _live_tick(self):
        if self.mode != "live" or self._live_busy:
            return
        self._submit(self._live_poll_once)
        if time.time() - self._last_l3_poll > 60:
            self._poll_l3()

    def _live_poll_once(self, gen):
        tr = self._tracker
        if tr is None or self._live_busy:
            return
        self._live_busy = True
        try:
            for kind, t, vol, complete in tr.poll():
                if gen != self._gen:
                    return
                with self._lock:
                    f = self._find_frame(t, 60)
                    if f is None:
                        f = Frame(self.site_id, t, l2_volume=vol, live=True, label="live")
                        self._insert_frame(f)
                        new = True
                    else:
                        new = False
                        if f.l2_path and not f.live:
                            continue          # archive copy already complete
                        f.set_level2(vol)
                        f.live = not complete
                self._trim()
                if new:
                    self._emit_frames()
                else:
                    self.frameUpdated.emit(f)
                ns = len(vol.sweeps)
                self.status.emit(f"Live {self.site_id}: volume {t:%H:%M:%S}Z, {ns} sweeps"
                                 + (" (complete)" if complete else " (scanning)")
                                 + f" — updated {datetime.now(timezone.utc):%H:%M:%S}Z")
        except Exception as exc:
            self.status.emit(f"Live update failed: {exc}")
        finally:
            self._live_busy = False

    def _poll_l3(self, force=False):
        if self._l3_busy or not self.l3_needed or self.site is None:
            return
        self._l3_busy = True
        self._last_l3_poll = time.time()
        codes = set(self.l3_needed)

        def work(gen):
            try:
                n = int(self.settings["loop_frames"])
                for code in sorted(codes):
                    try:
                        files = aws.latest_level3(self.site.l3_id, code, count=n if force else 2)
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

        def work(gen):
            self._load_l2_files(gen, files)
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
        for code in sorted(codes):
            try:
                files = aws.list_level3_range(site.l3_id, code, start, end)
            except Exception as exc:
                self.status.emit(f"Level III {code} listing failed: {exc}")
                continue
            futs = []
            for lf in files:
                if lf.key in self._l3_seen:
                    continue
                self._l3_seen.add(lf.key)
                futs.append(self._pool.submit(lambda k=lf.key: aws.fetch(aws.L3_BUCKET, k)))
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
