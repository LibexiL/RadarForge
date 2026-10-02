"""Automatic flags for signatures worth a closer look: strong rotation, debris and ZDR columns.

These are aids, not warnings: simple thresholds on radar fields resampled onto a plain grid, so they are
quick, easy to explain and easy to tune. Every flag says what it saw so you can judge it yourself. No Qt here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from ..products.geometry import beam_height, ground_range, slant_range

KM_PER_FT = 0.0003048

# thresholds (azimuthal shear in 10^-3 /s, reflectivity in dBZ, correlation coefficient, ZDR in dB)
ROT_MODERATE = 18.0
ROT_STRONG = 28.0
ROT_EXTREME = 40.0
ROT_REF_MIN = 30.0               # rotation counts only inside real echo
ROT_UPPER_FRACTION = 0.4         # and the next tilt up must show a good part of it
ROT_MIN_RANGE_KM = 12.0
ROT_MAX_FLAGS = 8
DEBRIS_REF_MIN = 35.0
DEBRIS_CC_MAX = 0.80
DEBRIS_ROT_MIN = 28.0
ZDR_COLUMN_MIN = 1.5
ZDR_COLUMN_MAX = 6.0               # more than this is usually noise, not drops
ZDR_COLUMN_REF_MIN = 40.0
ZDR_COLUMN_CC_MIN = 0.9
ZDR_COLUMN_MAX_RANGE_KM = 110.0


@dataclass
class Flag:
    kind: str                 # "ROT" | "DEBRIS" | "ZDRCOL"
    x: float                  # km east of the radar
    y: float                  # km north of the radar
    value: float              # the number that triggered it
    level: int                # 1 = worth a look, 2 = strong, 3 = extreme
    text: str                 # one line: what it is and the numbers
    detail: str = ""          # why it matters / what to check next


# ------------------------------------------------------------------------------------------------ polar -> grid
def grid_axes(half_km: float, step_km: float):
    n = int(round(2 * half_km / step_km))
    xs = -half_km + (np.arange(n) + 0.5) * step_km
    ys = half_km - (np.arange(n) + 0.5) * step_km
    return np.meshgrid(xs, ys)


def to_grid(img, half_km: float = 120.0, step_km: float = 1.0, reduce: str = "nearest") -> np.ndarray:
    """A sweep (products.engine.SweepImage) resampled onto a radar-centred grid, float32, NaN where there's no data.
    Row 0 is the north edge. reduce="nearest" takes the gate at each pixel's centre; "max" / "min" keep the largest /
    smallest value of all the gates that fall in a pixel, so a narrow shear maximum or a small hole in correlation
    coefficient isn't lost when many gates share one pixel."""
    if reduce in ("max", "min"):
        return _pooled(img, half_km, step_km, reduce)
    gx, gy = grid_axes(half_km, step_km)
    ground = np.hypot(gx, gy)
    az = (np.degrees(np.arctan2(gx, gy)) + 360.0) % 360.0
    r = ground if img.ground_range else slant_range(ground, img.elevation)
    nrad, ngates = img.values.shape
    if nrad == 0:
        return np.full(gx.shape, np.nan, np.float32)
    # nearest radial (azimuths ascend, so check the neighbours around the insertion point)
    i = np.searchsorted(img.az, az)
    hi, lo = i % nrad, (i - 1) % nrad
    d_hi = np.abs((img.az[hi] - az + 180.0) % 360.0 - 180.0)
    d_lo = np.abs((img.az[lo] - az + 180.0) % 360.0 - 180.0)
    radial = np.where(d_hi <= d_lo, hi, lo)
    gate = np.floor((r - (img.first_gate - img.gate_spacing / 2.0)) / img.gate_spacing).astype(np.int64)
    ok = (gate >= 0) & (gate < ngates)
    out = np.full(gx.shape, np.nan, np.float32)
    vals = img.values[radial[ok], gate[ok]].astype(np.float32)
    vals[np.isinf(vals)] = np.nan
    out[ok] = vals
    return out


def _pooled(img, half_km, step_km, reduce) -> np.ndarray:
    n = int(round(2 * half_km / step_km))
    out = np.full((n, n), np.nan, np.float32)
    nrad, ngates = img.values.shape
    if nrad == 0 or ngates == 0:
        return out
    slant = img.first_gate + img.gate_spacing * np.arange(ngates)
    ground = slant if img.ground_range else ground_range(slant, img.elevation)
    keep = ground <= half_km * 1.5
    if not keep.any():
        return out
    a = np.radians(img.az)
    gr = np.asarray(ground)[keep]
    x = gr[None, :] * np.sin(a)[:, None]
    y = gr[None, :] * np.cos(a)[:, None]
    ix = np.floor((x + half_km) / step_km).astype(np.int64)
    iy = np.floor((half_km - y) / step_km).astype(np.int64)
    vals = img.values[:, keep].astype(np.float32)
    ok = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n) & np.isfinite(vals)
    flat = (iy[ok] * n + ix[ok])
    buf = np.full(n * n, -np.inf if reduce == "max" else np.inf, np.float32)
    (np.maximum if reduce == "max" else np.minimum).at(buf, flat, vals[ok])
    buf[~np.isfinite(buf)] = np.nan
    return buf.reshape(n, n)


# ------------------------------------------------------------------------------------------------ rotation
def _clusters(mask: np.ndarray, min_pixels: int):
    """Connected areas of a boolean grid with at least min_pixels pixels: (labels, list of index arrays)."""
    labels, n = ndimage.label(mask)
    out = []
    for k in range(1, n + 1):
        idx = np.nonzero(labels == k)
        if len(idx[0]) >= min_pixels:
            out.append(idx)
    return out


def rotation_flags(azsh: np.ndarray, half_km: float, step_km: float, minimum: float = ROT_MODERATE,
                   min_area_km2: float = 2.0, min_range_km: float = ROT_MIN_RANGE_KM, ref: np.ndarray | None = None,
                   upper: np.ndarray | None = None, max_flags: int = ROT_MAX_FLAGS) -> list:
    """Areas of strong cyclonic azimuthal shear (the grid holds shear in 10^-3 /s, positive = cyclonic).

    To keep false alarms down the shear has to hold above the threshold across at least two touching pixels, away
    from the radar's noisy centre; sit in real echo (`ref`, if given); and, unless it is extreme, show up on the
    next tilt up too (`upper`, if given). Only the strongest of any group of neighbours is kept."""
    gx, gy = grid_axes(half_km, step_km)
    field = np.nan_to_num(azsh, nan=0.0)
    field = np.where(np.hypot(gx, gy) < min_range_km, 0.0, field)
    pair = np.maximum(ndimage.minimum_filter(field, footprint=[[1, 1]]), ndimage.minimum_filter(field, footprint=[[1], [1]]))
    found = []
    for idx in _clusters(pair >= minimum, max(1, int(round(min_area_km2 / (step_km * step_km))) - 1)):
        region = np.zeros(field.shape, bool)
        region[idx] = True
        region = ndimage.binary_dilation(region, iterations=1)
        vals = np.where(region, field, -np.inf)
        k = np.unravel_index(int(np.argmax(vals)), field.shape)
        peak = float(field[k])
        level = 3 if peak >= ROT_EXTREME else 2 if peak >= ROT_STRONG else 1
        near = np.hypot(gx - gx[k], gy - gy[k]) <= 3.0
        if ref is not None and not (np.nanmax(np.where(near, ref, np.nan), initial=-np.inf) >= ROT_REF_MIN):
            continue
        if upper is not None and level < 3 and np.nanmax(np.where(near, upper, np.nan), initial=-np.inf) < ROT_UPPER_FRACTION * peak:
            continue
        found.append((peak, level, float(gx[k]), float(gy[k])))
    found.sort(reverse=True)
    flags = []
    for peak, level, x, y in found:
        if any(np.hypot(x - f.x, y - f.y) < 6.0 for f in flags):
            continue                                                    # a weaker neighbour of one already flagged
        label = {1: "Rotation", 2: "Strong rotation", 3: "Extreme rotation"}[level]
        flags.append(Flag("ROT", x, y, peak, level, f"{label}: azimuthal shear {peak:.0f} ×10⁻³ /s",
                          "Check storm-relative velocity for a tight couplet, and the tilts above and below for "
                          "vertical continuity."))
    return flags[:max_flags]


def debris_flags(ref: np.ndarray, cc: np.ndarray, azsh: np.ndarray, half_km: float, step_km: float,
                 rotation: list) -> list:
    """Low correlation coefficient inside strong echo right at strong rotation: a possible tornado debris
    signature. Only looked for where rotation was already flagged (low CC alone is often non-meteorological)."""
    gx, gy = grid_axes(half_km, step_km)
    low = np.isfinite(ref) & np.isfinite(cc) & (ref >= DEBRIS_REF_MIN) & (cc <= DEBRIS_CC_MAX) & (cc > 0.2)
    flags = []
    for r in rotation:
        if r.value < DEBRIS_ROT_MIN:
            continue
        near = low & (np.hypot(gx - r.x, gy - r.y) <= 3.0)
        n = int(near.sum())
        if n < max(3, int(round(3.0 / (step_km * step_km)))):
            continue
        lowest = float(np.nanmin(cc[near]))
        flags.append(Flag("DEBRIS", float(gx[near].mean()), float(gy[near].mean()), lowest, 3 if lowest <= 0.75 else 2,
                          f"Possible debris signature: CC as low as {lowest:.2f} in {np.nanmax(ref[near]):.0f} dBZ echo",
                          "A drop in CC to under about 0.8 in strong reflectivity at the rotation can mean lofted "
                          "debris. Compare with ZDR (low, near 0) and the same spot on the next scan."))
    return flags


# ------------------------------------------------------------------------------------------------ ZDR columns
def zdr_column_flags(layers: list, half_km: float, step_km: float, freezing_km_arl: float,
                     min_height_above_km: float = 0.8, min_area_km2: float = 4.0, min_tilts: int = 2) -> list:
    """ZDR columns: high ZDR in strong, clean echo above the freezing level, a sign of water lofted by an updraft.
    `layers` is [(elevation_deg, zdr_grid, ref_grid, cc_grid or None)] for several tilts of one volume; heights are
    above the radar, like `freezing_km_arl`. It has to show on at least `min_tilts` tilts (a column, not a blip)."""
    gx, gy = grid_axes(half_km, step_km)
    ground = np.hypot(gx, gy)
    in_range = ground <= ZDR_COLUMN_MAX_RANGE_KM
    count = np.zeros(gx.shape, np.int32)
    top = np.zeros(gx.shape, np.float32)
    peak = np.full(gx.shape, -np.inf, np.float32)
    for elev, zdr, ref, cc in layers:
        h = beam_height(slant_range(ground, elev), elev)
        hit = np.isfinite(zdr) & np.isfinite(ref) & (zdr >= ZDR_COLUMN_MIN) & (zdr <= ZDR_COLUMN_MAX) & \
            (ref >= ZDR_COLUMN_REF_MIN) & (h >= freezing_km_arl + min_height_above_km) & in_range
        if cc is not None:
            hit &= np.isfinite(cc) & (cc >= ZDR_COLUMN_CC_MIN)
        count += hit
        top = np.where(hit, np.maximum(top, h), top)
        peak = np.where(hit, np.maximum(peak, np.nan_to_num(zdr, nan=-np.inf)), peak)
    flags = []
    for idx in _clusters(count >= min_tilts, max(1, int(round(min_area_km2 / (step_km * step_km))))):
        k = int(np.argmax(top[idx]))
        height = float(top[idx][k])
        flags.append(Flag("ZDRCOL", float(gx[idx].mean()), float(gy[idx].mean()), float(peak[idx].max()), 1,
                          f"Possible ZDR column: up to {peak[idx].max():.1f} dB, reaching {height / KM_PER_FT / 1000:.0f} kft",
                          "High ZDR above the freezing level in strong echo marks water carried up by a strong "
                          "updraft: a storm that is strengthening. Watch for the column growing and for rotation "
                          "developing."))
    return flags


def freezing_km_arl(freezing_ft_msl: float, radar_elev_ft: float = 0.0) -> float:
    """The freezing level (feet above sea level) as a height above the radar, in km."""
    return max(0.0, (freezing_ft_msl - radar_elev_ft) * KM_PER_FT)


def all_flags(azsh=None, ref=None, cc=None, zdr_layers=None, half_km=120.0, step_km=1.0, freezing_km=4.0,
              want=("ROT", "DEBRIS", "ZDRCOL"), azsh_upper=None) -> list:
    """Every flag for the fields given (any may be None), worst first. azsh_upper is the shear one tilt higher."""
    out = []
    rot = []
    if azsh is not None and ("ROT" in want or "DEBRIS" in want):
        rot = rotation_flags(azsh, half_km, step_km, ref=ref, upper=azsh_upper)
        if "ROT" in want:
            out += rot
    if azsh is not None and ref is not None and cc is not None and "DEBRIS" in want:
        out += debris_flags(ref, cc, azsh, half_km, step_km, [r for r in rot if r.level >= 2])
    if zdr_layers and "ZDRCOL" in want:
        out += zdr_column_flags(zdr_layers, half_km, step_km, freezing_km)
    out.sort(key=lambda f: (-f.level, -f.value))
    return out
