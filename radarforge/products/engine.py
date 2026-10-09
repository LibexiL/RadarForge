"""Turns frames into renderable polar images, with caching.

Every panel ultimately draws a SweepImage: a (radials x gates) array of values
in storage units plus the geometry needed to place each gate on the map.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from . import catalog
from .dealias import dealias_region
from .derived import (GRID_AZ, GRID_DR, VolumeGrid, azimuthal_shear, despeckle, kdp_from_phi,
                      radial_divergence, resample_to_grid, storm_relative, velocity_noise_filter)
from .geometry import slant_range

RF = np.float32(np.inf)   # range-folded marker


@dataclass
class SweepImage:
    key: tuple
    product: str
    values: np.ndarray            # float16/32 (nrad, ngates) storage units; NaN none; +inf RF
    az: np.ndarray                # centre azimuths, ascending
    az_lo: np.ndarray
    az_hi: np.ndarray
    first_gate: float             # km (gate centre)
    gate_spacing: float           # km
    elevation: float
    ground_range: bool
    time: datetime | None
    label: str = ""
    nyquist: float | None = None
    source: str = "L2"
    extra: dict = field(default_factory=dict)

    @property
    def nbytes(self):
        """Memory this image holds (the texture copy made for the graphics card is short-lived and not counted)."""
        n = self.values.nbytes + self.az.nbytes * 3
        for k, v in self.extra.items():
            if k != "_gpu" and isinstance(v, np.ndarray):
                n += v.nbytes
        return n

    @property
    def max_range(self):
        return self.first_gate + self.gate_spacing * (self.values.shape[1] - 0.5)

    def gpu_values(self):
        """float16 texture data with sentinels (NaN -> -60000, RF -> 60000). Made off the UI thread and kept
        until the texture is on the graphics card (the map then drops it, see RadarView._gpu_image)."""
        g = self.extra.get("_gpu")
        if g is None:
            v = self.values.astype(np.float32)
            g = np.where(np.isnan(v), -60000.0, np.where(np.isinf(v), 60000.0, v)).astype(np.float16)
            self.extra["_gpu"] = np.ascontiguousarray(g)
        return g

    def sample(self, az_deg: float, s_km: float):
        """Value at azimuth / ground distance (None when outside)."""
        n = len(self.az)
        if n == 0:
            return None
        i = int(np.searchsorted(self.az, az_deg)) % n
        for j in (i, (i - 1) % n):
            lo, hi = self.az_lo[j], self.az_hi[j]
            d = (az_deg - lo) % 360.0
            if d <= (hi - lo) % 360.0 + 1e-6:
                r = s_km if self.ground_range else float(slant_range(s_km, self.elevation))
                g = int(np.floor((r - (self.first_gate - self.gate_spacing / 2)) / self.gate_spacing))
                if 0 <= g < self.values.shape[1]:
                    return float(self.values[j, g])
                return None
        return None


def _bounds(az_sorted: np.ndarray, nominal: float):
    n = len(az_sorted)
    if n == 0:
        return az_sorted, az_sorted
    nxt = np.roll(az_sorted, -1)
    prv = np.roll(az_sorted, 1)
    gn = (nxt - az_sorted) % 360.0
    gp = (az_sorted - prv) % 360.0
    cap = nominal * 0.75
    hi = az_sorted + np.minimum(gn / 2.0, cap)
    lo = az_sorted - np.minimum(gp / 2.0, cap)
    return lo.astype(np.float32), hi.astype(np.float32)


# --------------------------------------------------------------------------- #
# tilts
# --------------------------------------------------------------------------- #
@dataclass
class Scan:
    elevation: float
    time: datetime
    sweeps: dict             # moment -> Sweep


@dataclass
class Tilt:
    elevation: float
    scans: list

    @property
    def label(self):
        n = len(self.scans)
        return f"{self.elevation:.1f}°" + (f" ×{n}" if n > 1 else "")

    def sweep(self, moment: str):
        for sc in reversed(self.scans):          # latest scan (e.g. last SAILS cut) first
            if moment in sc.sweeps:
                return sc.sweeps[moment]
        return None

    def moments(self):
        s = set()
        for sc in self.scans:
            s.update(sc.sweeps)
        return s


def build_tilts(vol) -> list:
    scans: list = []
    for sw in vol.sweeps:
        if scans:
            last = scans[-1]
            if (abs(last.elevation - sw.elevation) < 0.3 and getattr(last, "_n", 1) == 1
                    and "VEL" not in last.sweeps and "VEL" in sw.moments):
                for m in sw.moments:
                    if m not in last.sweeps or m in ("VEL", "SW"):
                        last.sweeps[m] = sw
                last._n = 2
                continue
        sc = Scan(sw.elevation, sw.start_time, {m: sw for m in sw.moments})
        sc._n = 1
        scans.append(sc)
    tilts: list = []
    for sc in sorted(scans, key=lambda s: (round(s.elevation, 1), s.time)):
        if tilts and abs(tilts[-1].elevation - sc.elevation) < 0.25:
            tilts[-1].scans.append(sc)
        else:
            tilts.append(Tilt(round(float(sc.elevation), 2), [sc]))
    return tilts


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class ProductEngine:
    def __init__(self, settings):
        self.settings = settings
        self._cache: OrderedDict = OrderedDict()
        self._sizes: dict = {}
        self._bytes = 0
        self._lock = threading.RLock()
        self._tilt_cache: OrderedDict = OrderedDict()
        self._grid_cache: OrderedDict = OrderedDict()
        self._inflight: dict = {}
        self._missing: OrderedDict = OrderedDict()   # keys known to have no image (product not in that volume)
        self._tilt_meta: OrderedDict = OrderedDict()  # (uid, l2_rev) -> [(elevation, label)] (holds no data)

    # ---------------------------------------------------------------- cache
    def _get(self, key):
        with self._lock:
            v = self._cache.get(key)
            if v is not None:
                self._cache.move_to_end(key)
            return v

    def _put(self, key, img):
        if img is None:
            # remembered so a loop doesn't wait on (or keep remaking) a product this frame doesn't have
            with self._lock:
                self._missing[key] = True
                while len(self._missing) > 4000:
                    self._missing.popitem(last=False)
            return
        # the setting, but never less than the loop on screen needs (frames x panels, ~3 MB an image)
        s = self.settings
        budget = max(float(s["image_cache_mb"]), int(s["loop_frames"] or 10) * int(s["layout"] or 1) * 3.2) * 1e6
        n = img.nbytes
        with self._lock:
            old = self._sizes.pop(key, None)
            if self._cache.pop(key, None) is not None and old is not None:
                self._bytes -= old
            self._cache[key] = img
            self._sizes[key] = n                # what was counted in, so exactly that is counted out
            self._bytes += n
            while self._bytes > budget and len(self._cache) > 8:
                k, _ev = self._cache.popitem(last=False)
                self._bytes -= self._sizes.pop(k, 0)

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._sizes.clear()
            self._bytes = 0
            self._grid_cache.clear()
            self._missing.clear()

    def forget_frames(self, frames):
        """Drops cached images and tilt lists of frames that are gone (left the loop, another radar) and of
        earlier revisions of the ones still here (a live volume that has grown since)."""
        cur = {f.uid: f.l2_rev for f in frames}
        with self._lock:
            for k in [k for k in self._cache if isinstance(k, tuple) and len(k) > 1 and isinstance(k[0], int)
                      and cur.get(k[0]) != k[1]]:
                self._cache.pop(k, None)
                self._bytes -= self._sizes.pop(k, 0)
            for k in [k for k in self._tilt_cache if cur.get(k[0]) != k[1]]:
                self._tilt_cache.pop(k, None)
            for k in [k for k in self._tilt_meta if cur.get(k[0]) != k[1]]:
                self._tilt_meta.pop(k, None)
            revs = {f.uid: getattr(f, "revision", 0) for f in frames}
            for k in [k for k in self._missing if (k[0] == "L3none" and revs.get(k[1]) != k[2]) or
                      (isinstance(k[0], int) and cur.get(k[0]) != k[1])]:
                self._missing.pop(k, None)

    def cached(self, frame, pid, tilt_index):
        return self._get(self._key(frame, pid, tilt_index))

    def known(self, frame, pid, tilt_index) -> bool:
        """True once the image has been made, or found not to exist (never computes)."""
        key = self._key(frame, pid, tilt_index)
        with self._lock:
            return key in self._cache or key in self._missing

    # ---------------------------------------------------------------- tilts
    def tilts(self, frame) -> list:
        k = (frame.uid, frame.l2_rev)
        with self._lock:
            t = self._tilt_cache.get(k)
        if t is not None:
            return t
        vol = frame.level2()
        t = build_tilts(vol) if vol is not None else []
        with self._lock:
            # earlier revisions of this frame (a live volume growing chunk by chunk) are never shown again,
            # and their tilts would keep those whole volumes alive
            for old in [o for o in self._tilt_cache if o[0] == frame.uid and o != k]:
                self._tilt_cache.pop(old, None)
            self._tilt_cache[k] = t
            # tilts hold their volume's sweeps: keep about as many as the volume cache, so a long loop doesn't
            # keep every volume it has ever decoded (the elevations stay in _tilt_meta)
            from ..data.frames import VOLUMES
            while len(self._tilt_cache) > max(8, VOLUMES.capacity + 2):
                self._tilt_cache.popitem(last=False)
            self._tilt_meta[k] = [(x.elevation, x.label) for x in t]
            while len(self._tilt_meta) > 400:
                self._tilt_meta.popitem(last=False)
        return t

    def tilt_meta(self, frame):
        """[(elevation, label)] of a frame's tilts once they are known, else None (never decodes)."""
        with self._lock:
            return self._tilt_meta.get((frame.uid, frame.l2_rev))

    def tilt_labels(self, frame):
        return [t.label for t in self.tilts(frame)]

    # ---------------------------------------------------------------- keys
    def _sig(self, pid):
        s = self.settings
        if pid == "SRV":
            return (s["storm_motion_dir"], s["storm_motion_kts"], s["srv_use_dealiased"], s["velocity_filter"],
                    bool(s["dealias_velocity"]))
        if pid == "VEL":
            return (s["velocity_filter"], bool(s["dealias_velocity"]))
        if pid in ("SW", "DVEL", "AZSH", "DIV"):
            return (s["velocity_filter"],)
        if pid in ("MESH", "POSH"):
            return (s["freezing_level_ft"], s["minus20_level_ft"])
        return ()

    def _key(self, frame, pid, tilt_index):
        p = catalog.get(pid)
        if p.kind in ("l3", "l3tilt"):
            prod = self.l3_product(frame, p, tilt_index)
            dl = bool(self.settings["dealias_velocity"]) and pid in ("L3G", "L3S")
            return ("L3", prod.uid, pid, dl) if prod is not None else ("L3none", frame.uid, frame.revision, pid,
                                                                        tilt_index)
        ti = tilt_index if p.tilted else -1
        return (frame.uid, frame.l2_rev, pid, ti, self._sig(pid))

    # ---------------------------------------------------------------- main
    def image(self, frame, pid: str, tilt_index: int = 0, cache: bool = True):
        """The image of one tilt. [cache]=False: made without keeping it (the 3-D view reads every tilt, and
        would otherwise push the panels' images out of the cache)."""
        key = self._key(frame, pid, tilt_index)
        img = self._get(key)
        if img is not None:
            return img
        with self._lock:
            if key in self._missing:
                return None
        if not cache:
            return self._compute(frame, pid, tilt_index, key)
        # single-flight so parallel requests don't duplicate heavy work
        with self._lock:
            ev = self._inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._inflight[key] = ev
                owner = True
            else:
                owner = False
        if not owner:
            ev.wait(180)
            return self._get(key)
        try:
            img = self._compute(frame, pid, tilt_index, key)
            self._put(key, img)
            return img
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            ev.set()

    def _compute(self, frame, pid, tilt_index, key):
        p = catalog.get(pid)
        if p.kind in ("l3", "l3tilt"):
            return self._l3_image(frame, p, tilt_index, key)
        vol = frame.level2()
        if vol is None:
            return None
        if p.kind == "volume":
            return self._volume_image(frame, vol, p, key)
        tilts = self.tilts(frame)
        if not tilts:
            return None
        tilt = tilts[max(0, min(tilt_index, len(tilts) - 1))]
        return self._tilt_image(vol, tilt, p, key)

    # ---------------------------------------------------------------- level II
    @staticmethod
    def _sorted_sweep(sw):
        o = getattr(sw, "_rf_sorted", None)
        if o is None:
            order = np.argsort(sw.azimuths, kind="stable")
            az = sw.azimuths[order]
            lo, hi = _bounds(az, sw.az_res)
            o = (order, az, lo, hi)
            sw._rf_sorted = o
        return o

    def moment_values(self, sw, moment, rf=True):
        mf = sw.moments[moment]
        order, _az, _lo, _hi = self._sorted_sweep(sw)
        v = mf.values()[order]
        if moment in ("VEL", "SW"):
            v = self._velocity_qc(sw, moment, v, mf.raw[order] == 1)
        if rf and moment in ("VEL", "SW"):
            v[mf.raw[order] == 1] = RF
        return v

    def _velocity_qc(self, sw, moment, v, rf_mask):
        level = int(self.settings["velocity_filter"])
        if level <= 0:
            return v
        ref = None
        if "REF" in sw.moments:
            mr = sw.moments["REF"]
            mv = sw.moments[moment]
            if abs(mr.gate_spacing - mv.gate_spacing) < 1e-6 and abs(mr.first_gate - mv.first_gate) < 1e-6:
                order = self._sorted_sweep(sw)[0]
                r = mr.values()[order]
                n = v.shape[1]
                ref = r[:, :n] if r.shape[1] >= n else np.pad(r, ((0, 0), (0, n - r.shape[1])),
                                                             constant_values=np.nan)
        if moment == "VEL":
            return velocity_noise_filter(v, rf_mask, ref, sw.nyquist, level)
        return despeckle(v, rf_mask)

    def _make(self, key, pid, sw, values, first_gate, spacing, label, source="L2", **extra):
        order, az, lo, hi = self._sorted_sweep(sw)
        return SweepImage(key, pid, values.astype(np.float16), az, lo, hi, first_gate, spacing,
                          float(sw.elevation), False, sw.start_time, label, sw.nyquist, source, extra)

    def dealiased(self, sw):
        cache = getattr(sw, "_rf_dealiased", None)
        level = int(self.settings["velocity_filter"])
        dv = cache[1] if cache is not None and cache[0] == level else None
        if dv is None:
            v = self.moment_values(sw, "VEL", rf=False)
            dv = dealias_region(v, sw.nyquist or 0.0)
            m = sw.moments["VEL"]
            order = self._sorted_sweep(sw)[0]
            dv[m.raw[order] == 1] = RF
            dv = dv.astype(np.float16)              # kept with the sweep: half the memory, plenty of precision
            sw._rf_dealiased = (level, dv)
        return dv.astype(np.float32)

    def _tilt_image(self, vol, tilt, p, key):
        pid = p.id
        label = tilt.label
        if p.kind == "moment":
            sw = tilt.sweep(p.moment)
            if sw is None:
                return None
            m = sw.moments[p.moment]
            if pid == "VEL" and self.settings["dealias_velocity"]:
                return self._make(key, pid, sw, self.dealiased(sw), m.first_gate, m.gate_spacing, label,
                                  dealiased=True)
            return self._make(key, pid, sw, self.moment_values(sw, p.moment), m.first_gate,
                              m.gate_spacing, label)
        if pid in ("SRV", "DVEL", "AZSH", "DIV"):
            sw = tilt.sweep("VEL")
            if sw is None:
                return None
            m = sw.moments["VEL"]
            order, az, _lo, _hi = self._sorted_sweep(sw)
            use_d = pid in ("DVEL", "AZSH", "DIV") or (pid == "SRV" and (self.settings["srv_use_dealiased"] or
                                                                        self.settings["dealias_velocity"]))
            v = self.dealiased(sw) if use_d else self.moment_values(sw, "VEL")
            rf = np.isinf(v)
            vf = np.where(rf, np.nan, v)
            extra = {"dealiased": bool(use_d)}
            if pid == "SRV":
                out = storm_relative(vf, az, float(self.settings["storm_motion_dir"]),
                                     float(self.settings["storm_motion_kts"]))
                out[rf] = RF
            elif pid == "DVEL":
                out = v
            else:
                rng = m.first_gate + np.arange(v.shape[1]) * m.gate_spacing
                if pid == "AZSH":
                    out = azimuthal_shear(vf, az, rng, sw.nyquist, dealiased=use_d)
                else:
                    out = radial_divergence(vf, m.gate_spacing, sw.nyquist, dealiased=use_d)
                # suppress clear-air / weak-echo noise using the same sweep's reflectivity
                if "REF" in sw.moments:
                    ref = self.moment_values(sw, "REF", rf=False)
                    mref = sw.moments["REF"]
                    if abs(mref.gate_spacing - m.gate_spacing) < 1e-6 and abs(mref.first_gate - m.first_gate) < 1e-6:
                        n = out.shape[1]
                        ref = ref[:, :n] if ref.shape[1] >= n else np.pad(
                            ref, ((0, 0), (0, n - ref.shape[1])), constant_values=np.nan)
                        out[~(np.nan_to_num(ref, nan=-99.0) >= 15.0)] = np.nan
            return self._make(key, pid, sw, out, m.first_gate, m.gate_spacing, label, **extra)
        if pid == "KDP":
            sw = tilt.sweep("PHI")
            if sw is None:
                return None
            m = sw.moments["PHI"]
            phi = self.moment_values(sw, "PHI")
            n = phi.shape[1]

            def fit(mom):
                if mom not in sw.moments:
                    return None
                a = self.moment_values(sw, mom, rf=False)
                if a.shape[1] >= n:
                    return a[:, :n]
                return np.pad(a, ((0, 0), (0, n - a.shape[1])), constant_values=np.nan)
            k = kdp_from_phi(phi, fit("RHO"), fit("REF"), m.gate_spacing)
            return self._make(key, pid, sw, k, m.first_gate, m.gate_spacing, label)
        return None

    # ---------------------------------------------------------------- volume
    def volume_grid(self, frame, vol):
        k = (frame.uid, frame.l2_rev)
        with self._lock:
            g = self._grid_cache.get(k)
        if g is not None:
            return g
        vals, els = [], []
        for t in self.tilts(frame):
            sw = t.sweep("REF")
            if sw is None:
                continue
            m = sw.moments["REF"]
            order, az, _lo, _hi = self._sorted_sweep(sw)
            v = m.values()[order]
            vals.append(resample_to_grid(v, az, m.first_gate, m.gate_spacing, sw.elevation))
            els.append(sw.elevation)
        g = VolumeGrid(vals, els, (vol.height_m or 0.0) / 1000.0)
        with self._lock:
            self._grid_cache[k] = g
            while len(self._grid_cache) > 3:
                self._grid_cache.popitem(last=False)
        return g

    def _volume_image(self, frame, vol, p, key):
        g = self.volume_grid(frame, vol)
        if g.h is None:
            return None
        s = self.settings
        h0 = float(s["freezing_level_ft"]) / 3280.84
        hm20 = float(s["minus20_level_ft"]) / 3280.84
        pid = p.id
        if pid == "CREF":
            v = g.composite()
        elif pid.startswith("ET"):
            v = g.echo_top(float(pid[2:]))
        elif pid == "VIL":
            v = g.vil()
        elif pid == "VILD":
            vil = g.vil()
            et = g.echo_top(18.0)
            with np.errstate(all="ignore"):
                v = vil / ((et / 3.28084 - g.h0) * 1000.0) * 1000.0
            v[~np.isfinite(v)] = np.nan
        elif pid == "MESH":
            v = g.mesh(h0, hm20)
        elif pid == "POSH":
            v = g.posh(h0, hm20)
        else:
            return None
        if v is None:
            return None
        az = ((np.arange(GRID_AZ) + 0.5) * (360.0 / GRID_AZ)).astype(np.float32)
        w = 360.0 / GRID_AZ / 2.0
        return SweepImage(key, pid, v.astype(np.float16), az, az - w, az + w, GRID_DR / 2.0, GRID_DR,
                          0.0, True, vol.start_time, "Volume", None, "L2")

    # ---------------------------------------------------------------- level III
    @staticmethod
    def l3_product(frame, p, tilt_index):
        for code in p.l3_candidates(tilt_index):
            prod = frame.l3.get(code)
            if prod is not None:
                return prod
        return None

    def _l3_image(self, frame, p, tilt_index, key):
        prod = self.l3_product(frame, p, tilt_index)
        code = prod.awips if prod is not None else ""
        if prod is None or prod.radial is None:
            return None
        r = prod.radial
        order = np.argsort(r.azimuths)
        az = r.azimuths[order].astype(np.float32)
        w = r.widths[order].astype(np.float32)
        lo = (az - w / 2.0).astype(np.float32)
        hi = (az + w / 2.0).astype(np.float32)
        vals = r.values[order]
        dealiased = False
        if p.id in ("L3G", "L3S") and self.settings["dealias_velocity"]:
            v = vals.astype(np.float32)
            rf = np.isinf(v)
            w = np.where(rf, np.nan, v)
            nyq = float(np.nanmax(np.abs(w))) if np.isfinite(w).any() else 0.0   # L3 velocity tops out at Nyquist
            if nyq > 10.0:
                d = dealias_region(w, nyq)
                d[rf] = np.inf
                vals, dealiased = d, True
        el = r.elevation if not r.ground_range else 0.0
        label = f"{code} {el:.1f}°" if not r.ground_range else code
        return SweepImage(key, p.id, vals.astype(np.float16), az, lo, hi, r.first_gate, r.gate_spacing,
                          float(el), bool(r.ground_range), prod.time, label, None, "L3",
                          {"dealiased": True} if dealiased else {})

    # ---------------------------------------------------------------- sampling
    def tilt_sweeps(self, frame, pid, cache: bool = True):
        """[(elevation, SweepImage)] for every tilt (for cross sections / 3D)."""
        out = []
        for i, t in enumerate(self.tilts(frame)):
            img = self.image(frame, pid, i, cache=cache)
            if img is not None:
                out.append((t.elevation, img))
        return out
