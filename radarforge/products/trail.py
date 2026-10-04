"""Max-value trail (the Σ toolbar button): each panel shows the most extreme value seen at every gate
across the loaded frames up to the one shown – a hail swath from reflectivity or MESH, a rotation track
from azimuthal shear, a debris trail from CC (its minimum), the strongest winds from velocity."""
from __future__ import annotations

import dataclasses
import threading
from collections import OrderedDict

import numpy as np

from . import catalog
from .geometry import ground_range, slant_range

MIN_IDS = {"CC", "L3C"}                                           # low values are the interesting ones
ABS_IDS = {"VEL", "SRV", "DVEL", "L3G", "L3S", "DIV"}             # strongest either way (keeps the sign)


def rule_for(pid: str):
    """'max', 'min', 'absmax', or None where a trail makes no sense (categories like HCA)."""
    p = catalog.get(pid)
    if p.categorical:
        return None
    if pid in MIN_IDS:
        return "min"
    if pid in ABS_IDS:
        return "absmax"
    return "max"


def resample(src, target) -> np.ndarray:
    """src's values at target's gates (nearest radial and gate), NaN where src has no data."""
    nt, gt = target.values.shape
    if len(src.az) == 0 or src.values.size == 0:
        return np.full((nt, gt), np.nan, np.float32)
    i = np.searchsorted(src.az, target.az) % len(src.az)
    j = (i - 1) % len(src.az)
    di = np.abs((src.az[i] - target.az + 180) % 360 - 180)
    dj = np.abs((src.az[j] - target.az + 180) % 360 - 180)
    rows = np.where(di <= dj, i, j)
    width = np.maximum(np.abs((src.az_hi - src.az_lo + 180) % 360 - 180), 0.5)
    ok_r = np.minimum(di, dj) <= width[rows]                         # no radial there (sector scans, gaps)
    r = target.first_gate + target.gate_spacing * np.arange(gt)
    if target.ground_range != src.ground_range:                      # convert target range to src's kind
        r = ground_range(r, target.elevation) if src.ground_range else slant_range(r, src.elevation)
    g = np.floor((r - (src.first_gate - src.gate_spacing / 2)) / src.gate_spacing).astype(np.int64)
    ok_g = (g >= 0) & (g < src.values.shape[1])
    out = np.full((nt, gt), np.nan, np.float32)
    vals = src.values[rows[ok_r]][:, g[ok_g]].astype(np.float32)
    out[np.ix_(np.where(ok_r)[0], np.where(ok_g)[0])] = vals
    out[np.isinf(out)] = np.nan
    return out


def combine(target, sources: list, rule: str):
    """A copy of `target` whose values are the trail over target + sources (sources: older images)."""
    base = np.asarray(target.values, np.float32)
    rf = np.isinf(base)
    out = np.where(rf, np.nan, base)
    for src in sources:
        v = resample(src, target)
        if rule == "max":
            out = np.fmax(out, v)
        elif rule == "min":
            out = np.fmin(out, v)
        else:
            take = np.isnan(out) | (np.abs(np.nan_to_num(v, nan=0.0)) > np.abs(np.nan_to_num(out, nan=0.0)))
            take &= ~np.isnan(v)
            out = np.where(take, v, out)
    out[np.isnan(out) & rf] = np.inf                                 # range folding only where nothing better
    extra = {k: v for k, v in target.extra.items() if not k.startswith("_")}
    extra["trail"] = len(sources) + 1
    return dataclasses.replace(target, key=("TRAIL",) + tuple(target.key if isinstance(target.key, tuple)
                                                                  else (target.key,)),
                               values=out.astype(np.float16), extra=extra)


class TrailCache:
    def __init__(self, size=24):
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.size = size

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()
