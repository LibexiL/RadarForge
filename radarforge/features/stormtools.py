"""Storm helpers: automatic flags (rotation, possible debris, hail), rotation history, storm
following and plain-language explanations of radar values ("learn mode").

These are rules of thumb meant to draw the eye, not warnings: every flag says why it was raised.
"""
from __future__ import annotations

import math

import numpy as np

from ..products.geometry import ground_range, slant_range

# thresholds (storage units: dBZ, m/s, 1e-3 /s, dB)
ROT_MIN = 8.0             # azimuthal shear for a rotation flag
ROT_STRONG = 15.0
TDS_CC = 0.80
TDS_REF = 35.0
HAIL_REF = 60.0
MAX_RANGE_KM = 150.0       # beyond this the lowest beam is too high and wide for these signatures
MIN_RANGE_KM = 15.0        # closer in, clutter and the cone of silence make rotation / debris flags unreliable
KIND_ORDER = ("tds", "rotation", "hail")
KTS = 1.943844
L3_ALIAS = {"L3B": "REF", "L3G": "VEL", "L3S": "SRV", "L3C": "CC", "L3X": "ZDR", "L3K": "KDP", "DVEL": "VEL"}


# --------------------------------------------------------------------------- geometry
def gate_ground_km(img) -> np.ndarray:
    """Ground distance (km) of each gate centre."""
    r = img.first_gate + img.gate_spacing * np.arange(img.values.shape[1])
    return r if img.ground_range else ground_range(r, img.elevation)


def gate_xy(img, rows=None, cols=None):
    """x, y (km) of gate centres (all, or the given row/column index arrays)."""
    s = gate_ground_km(img)
    if rows is None:
        a = np.radians(img.az)[:, None]
        return s[None, :] * np.sin(a), s[None, :] * np.cos(a)
    a = np.radians(img.az[rows])
    return s[cols] * np.sin(a), s[cols] * np.cos(a)


def sample_at(img, az_deg, s_km):
    """Nearest-gate values of an image at azimuths / ground ranges (arrays); NaN outside."""
    az_deg = np.asarray(az_deg, np.float64)
    s_km = np.asarray(s_km, np.float64)
    n = len(img.az)
    if n == 0:
        return np.full(az_deg.shape, np.nan, np.float32)
    i = np.searchsorted(img.az, az_deg) % n
    j = (i - 1) % n
    di = np.abs((img.az[i] - az_deg + 180) % 360 - 180)
    dj = np.abs((img.az[j] - az_deg + 180) % 360 - 180)
    row = np.where(di <= dj, i, j)
    r = s_km if img.ground_range else slant_range(s_km, img.elevation)
    g = np.floor((r - (img.first_gate - img.gate_spacing / 2)) / img.gate_spacing).astype(np.int64)
    ok = (g >= 0) & (g < img.values.shape[1])
    out = np.full(az_deg.shape, np.nan, np.float32)
    v = img.values[row[ok], g[ok]].astype(np.float32)
    v[np.isinf(v)] = np.nan
    out[ok] = v
    return out


def window(img, x, y, radius_km):
    """(values, gx, gy) of the gates within radius_km of x, y."""
    s0 = math.hypot(x, y)
    az0 = math.degrees(math.atan2(x, y)) % 360
    s = gate_ground_km(img)
    cols = np.where((s >= s0 - radius_km) & (s <= s0 + radius_km))[0]
    if len(cols) == 0 or len(img.az) == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    span = 180.0 if s0 < radius_km else math.degrees(math.asin(min(1.0, radius_km / max(s0, 1e-6)))) + 1.0
    d = np.abs((img.az - az0 + 180) % 360 - 180)
    rows = np.where(d <= span)[0]
    if len(rows) == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    gx, gy = gate_xy(img, rr.ravel(), cc.ravel())
    vals = img.values[rr.ravel(), cc.ravel()].astype(np.float32)
    keep = np.hypot(gx - x, gy - y) <= radius_km
    vals, gx, gy = vals[keep], gx[keep], gy[keep]
    vals[np.isinf(vals)] = np.nan
    return vals, gx, gy


def circle_stats(img, x, y, radius_km):
    """max, min and the location of the max of the finite values near x, y (None when there are none)."""
    vals, gx, gy = window(img, x, y, radius_km)
    ok = np.isfinite(vals)
    if not ok.any():
        return None
    v, gx, gy = vals[ok], gx[ok], gy[ok]
    i = int(np.argmax(v))
    return dict(max=float(v[i]), min=float(v.min()), x=float(gx[i]), y=float(gy[i]), n=int(len(v)))


# --------------------------------------------------------------------------- flags
def _regions(mask, min_gates):
    from scipy import ndimage
    lab, n = ndimage.label(mask)
    if n == 0:
        return lab, []
    sizes = np.bincount(lab.ravel())
    return lab, [k for k in range(1, n + 1) if sizes[k] >= min_gates]


def _merge(flags, km=5.0):
    out = []
    for f in sorted(flags, key=lambda f: -f["score"]):
        if any(o["kind"] == f["kind"] and math.hypot(o["x"] - f["x"], o["y"] - f["y"]) < km for o in out):
            continue
        out.append(f)
    return out


def _group(flags, km=4.0):
    """One marker per place: flags of different kinds within km are combined (most serious kind first)."""
    flags = sorted(flags, key=lambda f: (KIND_ORDER.index(f["kind"]), -f["score"]))
    out = []
    for f in flags:
        g = next((o for o in out if math.hypot(o["x"] - f["x"], o["y"] - f["y"]) < km), None)
        if g is None:
            out.append(dict(f, kinds=[f["kind"]]))
        elif f["kind"] not in g["kinds"]:
            g["kinds"].append(f["kind"])
            g["text"] += "\n\n" + f["text"]
            g["score"] = max(g["score"], f["score"])
    return out


def detect(ref, azsh=None, cc=None, zdr=None, max_range=MAX_RANGE_KM, min_range=MIN_RANGE_KM) -> list:
    """Flags from the lowest tilt: dicts with kind (the most serious: tds / rotation / hail), kinds (all
    found at that spot), x, y (km), score and text."""
    flags = []
    if ref is None:
        return flags
    s_ref = gate_ground_km(ref)
    near = s_ref <= max_range
    rv = np.asarray(ref.values, np.float32)
    # hail cores
    with np.errstate(invalid="ignore"):
        hmask = np.isfinite(rv) & (rv >= HAIL_REF) & near[None, :]
    lab, keep = _regions(hmask, 4)
    for k in keep:
        rows, cols = np.nonzero(lab == k)
        vals = rv[rows, cols]
        i = int(np.argmax(vals))
        x, y = gate_xy(ref, rows[i:i + 1], cols[i:i + 1])
        x, y = float(x[0]), float(y[0])
        why = f"reflectivity {vals[i]:.0f} dBZ"
        score = float(vals[i])
        conf = "Hail likely" if vals[i] >= 65 else "Possible hail"
        if zdr is not None:
            az = np.degrees(np.arctan2(x, y)) % 360
            z = sample_at(zdr, [az], [math.hypot(x, y)])[0]
            if np.isfinite(z):
                why += f", ZDR {z:.1f} dB"
                if z < 1.0:
                    conf, score = "Hail likely", score + 5
                elif z > 3.0:
                    why += " (could be big rain drops)"
        flags.append(dict(kind="hail", x=x, y=y, score=score, value=float(vals[i]),
                          text=f"{conf}: {why}. Hail cores show up as very high reflectivity (60+ dBZ), "
                               "often with ZDR near 0 because tumbling hailstones look round to the radar."))
    rot = []
    if azsh is not None:
        from scipy import ndimage
        av = np.nan_to_num(np.asarray(azsh.values, np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        av = ndimage.median_filter(av, size=3)                 # one bad gate doesn't make a flag
        s_az = gate_ground_km(azsh)
        az_grid = np.broadcast_to(azsh.az[:, None], av.shape)
        refs = sample_at(ref, az_grid, np.broadcast_to(s_az[None, :], av.shape))
        with np.errstate(invalid="ignore"):
            m = (av >= ROT_MIN) & ((s_az <= max_range) & (s_az >= min_range))[None, :] & \
                (np.nan_to_num(refs, nan=-99) >= 25)
        lab, keep = _regions(m, 3)
        for k in keep:
            rows, cols = np.nonzero(lab == k)
            vals = av[rows, cols]
            i = int(np.argmax(vals))
            x, y = gate_xy(azsh, rows[i:i + 1], cols[i:i + 1])
            x, y = float(x[0]), float(y[0])
            strong = vals[i] >= ROT_STRONG
            f = dict(kind="rotation", x=x, y=y, score=float(vals[i]), value=float(vals[i]),
                     text=(f"{'Strong rotation' if strong else 'Rotation'}: azimuthal shear "
                           f"{vals[i] / 1000:.3f} /s. Inbound and outbound velocities sit side by side here – "
                           "the radar's sign of a spinning updraft (mesocyclone). Check the velocity panel."))
            rot.append(f)
        flags += rot
    if cc is not None and rot:
        cv = np.asarray(cc.values, np.float32)
        s_cc = gate_ground_km(cc)
        az_grid = np.broadcast_to(cc.az[:, None], cv.shape)
        refs = sample_at(ref, az_grid, np.broadcast_to(s_cc[None, :], cv.shape))
        with np.errstate(invalid="ignore"):
            m = np.isfinite(cv) & (cv < TDS_CC) & (np.nan_to_num(refs, nan=-99) >= TDS_REF) & \
                ((s_cc <= min(max_range, 120.0)) & (s_cc >= min_range))[None, :]
        lab, keep = _regions(m, 3)
        for k in keep:
            rows, cols = np.nonzero(lab == k)
            gx, gy = gate_xy(cc, rows, cols)
            cx, cy = float(gx.mean()), float(gy.mean())
            best = min(rot, key=lambda r: math.hypot(r["x"] - cx, r["y"] - cy))
            if math.hypot(best["x"] - cx, best["y"] - cy) > 4.0 or best["value"] < 10.0:
                continue
            vals = cv[rows, cols]
            flags.append(dict(kind="tds", x=cx, y=cy, score=100.0 + best["value"], value=float(np.nanmin(vals)),
                              text=(f"Possible debris (TDS): correlation coefficient down to {np.nanmin(vals):.2f} "
                                    f"in {int(len(vals))} gates with reflectivity {TDS_REF:.0f}+ dBZ, right next to "
                                    "strong rotation. Rain and hail keep CC above about 0.85; lofted debris is "
                                    "jumbled and drops it. This often means a tornado is on the ground – confirm "
                                    "with warnings and reports.")))
    return _group(_merge(flags))


# --------------------------------------------------------------------------- rotation history
def rotation_point(azsh, vel, x, y, radius_km=5.0):
    """(max azimuthal shear 1e-3/s, rotational velocity kt, x, y of the shear max) near a point."""
    sh = circle_stats(azsh, x, y, radius_km) if azsh is not None else None
    vr = None
    if vel is not None:
        st = circle_stats(vel, x, y, radius_km)
        if st is not None and st["max"] > 0 > st["min"]:
            vr = (st["max"] - st["min"]) / 2.0 * KTS
    if sh is None:
        return None if vr is None else (None, vr, x, y)
    return sh["max"], vr, sh["x"], sh["y"]


def motion_xy(from_deg, kts):
    """(dx, dy) km per minute of a storm moving FROM from_deg at kts."""
    a = math.radians((from_deg + 180.0) % 360.0)
    d = kts * 1.852 / 60.0
    return math.sin(a) * d, math.cos(a) * d


# --------------------------------------------------------------------------- storm following
def follow_step(ref, x, y, search_km=12.0, min_dbz=40.0):
    """The reflectivity-weighted centre of the strong echo near x, y (or None)."""
    vals, gx, gy = window(ref, x, y, search_km)
    ok = np.isfinite(vals) & (vals >= min_dbz)
    if ok.sum() < 5:
        ok = np.isfinite(vals) & (vals >= min_dbz - 10)
        if ok.sum() < 5:
            return None
    w = 10 ** (vals[ok] / 10.0)
    return float((gx[ok] * w).sum() / w.sum()), float((gy[ok] * w).sum() / w.sum())


# --------------------------------------------------------------------------- learn mode
PRODUCT_HELP = {
    "REF": "Reflectivity (dBZ): how much energy comes back. Light rain 20–30, heavy rain 40–50, "
           "hail 60+. Shapes matter: hooks, bows and lines.",
    "VEL": "Velocity: motion toward (green) or away from (red) the radar along the beam. Bright green next to "
           "bright red means rotation (or a strong wind shift).",
    "SRV": "Storm-relative velocity: velocity with the storm's own motion removed, so rotation inside a moving "
           "storm stands out.",
    "ZDR": "Differential reflectivity: shape of the targets. Flat raindrops +1 to +4 dB, hail and snow near 0, "
           "insects and birds very high.",
    "CC": "Correlation coefficient: how alike the targets are. Rain/snow 0.97+, hail or melting 0.85–0.95, "
          "debris, birds, insects and ground clutter well below 0.8.",
    "KDP": "Specific differential phase: liquid water amount. High KDP (2+ °/km) means very heavy rain.",
    "AZSH": "Azimuthal shear: how fast the wind changes across the beam – the radar's rotation meter. "
            "0.010 /s and up is notable near the ground.",
    "DIV": "Radial divergence: winds spreading apart (positive) or converging (negative) along the beam.",
    "CREF": "Composite reflectivity: the highest reflectivity in the column – shows storm cores aloft.",
    "MESH": "Maximum expected size of hail (inches), estimated from reflectivity above the freezing level.",
    "ET18": "Echo tops: how tall the storm is. Taller usually means a stronger updraft.",
    "VIL": "Vertically integrated liquid: total water in the column; high values often mean hail.",
}


def explain(values: dict, beam_ft=None) -> list:
    """Plain-language notes about the values under the cursor. values: product id -> storage value."""
    v = {}
    for k, val in values.items():
        if val is None or (isinstance(val, float) and not math.isfinite(val)):
            continue
        v.setdefault(L3_ALIAS.get(k, k), float(val))
    notes = []
    ref, cc, zdr, kdp = v.get("REF"), v.get("CC"), v.get("ZDR"), v.get("KDP")
    vel = v.get("SRV", v.get("VEL"))
    if ref is not None:
        if ref < 15:
            notes.append(f"{ref:.0f} dBZ: very weak echo – drizzle, light snow, insects or clear-air returns.")
        elif ref < 30:
            notes.append(f"{ref:.0f} dBZ: light precipitation.")
        elif ref < 45:
            notes.append(f"{ref:.0f} dBZ: moderate to heavy rain.")
        elif ref < 55:
            notes.append(f"{ref:.0f} dBZ: very heavy rain; a thunderstorm core.")
        elif ref < 65:
            notes.append(f"{ref:.0f} dBZ: intense core – hail is possible.")
        else:
            notes.append(f"{ref:.0f} dBZ: extreme – large hail is likely.")
    if cc is not None:
        if cc >= 0.97:
            notes.append(f"CC {cc:.2f}: uniform targets – pure rain or snow.")
        elif cc >= 0.90:
            notes.append(f"CC {cc:.2f}: mixed sizes or phases – hail, melting snow or big drops.")
        elif cc >= 0.80:
            notes.append(f"CC {cc:.2f}: quite mixed – hail, the melting layer, or the edge of non-weather echo.")
        else:
            extra = (" With reflectivity this high, it can be tornado debris if there's rotation right here."
                     if ref is not None and ref >= TDS_REF else " Usually birds, insects, smoke or ground clutter.")
            notes.append(f"CC {cc:.2f}: non-weather targets.{extra}")
    if zdr is not None:
        if zdr < -0.5:
            notes.append(f"ZDR {zdr:.1f} dB: vertically oriented targets (ice crystals in an electrified cloud) "
                         "or a radar artefact.")
        elif zdr < 0.8:
            notes.append(f"ZDR {zdr:.1f} dB: round-looking targets – hail, graupel, snow or small drops.")
        elif zdr < 3.5:
            notes.append(f"ZDR {zdr:.1f} dB: flattened raindrops (bigger drops, higher ZDR).")
        else:
            notes.append(f"ZDR {zdr:.1f} dB: very large drops, or insects and birds.")
        if ref is not None and ref >= 55 and zdr < 1.0:
            notes.append("High reflectivity with low ZDR is a classic hail signature.")
    if kdp is not None and kdp >= 1.5:
        notes.append(f"KDP {kdp:.1f}°/km: lots of liquid water – very heavy rain.")
    if vel is not None:
        kt = abs(vel) * KTS
        toward = vel < 0
        word = "toward" if toward else "away from"
        notes.append(f"{kt:.0f} kt {word} the radar" + (" – damaging-wind strength if it reaches the ground."
                                                         if kt >= 50 else "."))
    az = v.get("AZSH")
    if az is not None and az >= ROT_MIN:
        notes.append(f"Azimuthal shear {az / 1000:.3f} /s: {'strong ' if az >= ROT_STRONG else ''}rotation here.")
    if beam_ft is not None and beam_ft > 10000:
        notes.append(f"The beam is {beam_ft:,.0f} ft up here, so low-level features can be missed.")
    return notes
