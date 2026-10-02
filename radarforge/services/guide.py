"""A plain-language reading of the number under the cursor ("what does this value mean?"). No Qt here.

`hint(product_id, value)` takes the product id (REF, ZDR, CC...) and the value in the units the cursor shows
(dBZ, dB, m/s, deg/km...) and returns a short phrase, or None when there is nothing useful to say. The thresholds
are the rules of thumb forecasters use: they are a pointer, not a diagnosis.
"""
from __future__ import annotations

import math

KT = 1.0 / 0.514444                        # m/s -> knots


def _band(value: float, table) -> str | None:
    for upper, text in table:
        if value < upper:
            return text
    return None


_REF = ((5, "very light echo, or clutter and insects"), (20, "light precipitation or drizzle"),
        (35, "light to moderate rain"), (45, "moderate to heavy rain"),
        (55, "heavy rain; small hail possible"), (65, "very heavy rain, likely hail"),
        (200, "hail almost certain; check ZDR and CC for a hail core"))

_ZDR = ((-1.5, "negative: bad calibration, or a hail or graupel mix"), (0.0, "near zero or slightly negative: "
        "tumbling hail, graupel or snow"), (0.5, "round, tumbling targets: hail, graupel or dry snow"),
        (1.5, "small drops or mixed precipitation"), (3.0, "typical rain"),
        (5.0, "large drops: heavy rain, or size sorting"), (20, "very large oblate drops; a ZDR column if above "
        "the freezing level"))

_CC = ((0.2, "no meteorological echo"), (0.8, "non-meteorological: biological, clutter or a debris ball"),
       (0.9, "mixed targets: melting layer, large hail, wet snow or debris"),
       (0.97, "mixed hydrometeors: possible hail, melting layer or rain-snow mix"),
       (1.1, "uniform precipitation"))

_KDP = ((-0.3, "negative: noise"), (0.3, "little liquid water"), (1.0, "light to moderate rain"),
        (2.5, "heavy rain"), (6.0, "very heavy rain: flash-flood potential"), (100, "extreme rain rate"))

_SW = ((3, "smooth flow"), (5, "moderate turbulence"), (8, "strong shear or turbulence"),
       (100, "very high: strong turbulence, shear or rotation"))

_VIL = ((10, "light"), (25, "moderate"), (40, "heavy; thunderstorm"), (55, "very heavy; severe hail possible"),
        (500, "extreme; large hail likely"))

_ET = ((10, "shallow"), (25, "moderate"), (40, "deep convection"), (55, "very deep; strong updraft"),
       (200, "extreme; overshooting top"))

_MESH = ((0.5, "below severe"), (1.0, "marginal; up to quarter size"), (1.75, "severe: up to golf-ball size"),
         (2.75, "significant severe hail"), (20, "giant hail; baseball size or more"))


def _velocity(v_ms: float, srv: bool) -> str | None:
    kt = abs(v_ms) * KT
    if kt < 3:
        return "near zero: little motion along this beam, or the zero line"
    word = "away from" if v_ms > 0 else "toward"
    base = f"moving {word} the radar at {kt:.0f} kt"
    if srv and kt >= 40:
        return base + "; strong rotation if next to opposite-signed winds"
    return base


def _azsh(v: float) -> str | None:
    """v in 1e-3 /s: cyclonic positive."""
    if abs(v) < 6:
        return "weak shear"
    kind = "cyclonic" if v > 0 else "anticyclonic"
    if abs(v) < 12:
        return f"{kind} shear: broad rotation"
    if abs(v) < 20:
        return f"strong {kind} shear"
    return f"very strong {kind} shear" + ("; possible tornado-scale rotation" if v > 0 else "")


def hint(product: str, value) -> str | None:
    """A short reading of `value` for `product` (a product id), or None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    if math.isinf(v):
        return "range folded: the radar could not tell the true velocity here" if product in ("VEL", "SRV", "DVEL", "L3G", "L3S") else None
    if product in ("REF", "CREF", "L3B"):
        return _band(v, _REF)
    if product in ("VEL", "DVEL", "L3G"):
        return _velocity(v, False)
    if product in ("SRV", "L3S"):
        return _velocity(v, True)
    if product in ("ZDR", "L3X"):
        return _band(v, _ZDR)
    if product in ("CC", "L3C"):
        return _band(v, _CC)
    if product in ("KDP", "L3K"):
        return _band(v, _KDP)
    if product == "SW":
        return _band(v, _SW)
    if product in ("AZSH", "DIV"):
        if product == "DIV":
            return "divergence" if v > 6 else "convergence" if v < -6 else None
        return _azsh(v)
    if product in ("VIL", "L3DVL"):
        return _band(v, _VIL)
    if product in ("ET18", "ET30", "ET50", "L3EET"):
        return _band(v, _ET)
    if product == "MESH":
        return _band(v, _MESH)
    if product == "POSH":
        return "severe hail unlikely" if v < 20 else "severe hail possible" if v < 60 else "severe hail likely"
    return None


def _usable(x) -> bool:
    return x is not None and math.isfinite(x)


def combined(readings: dict) -> str | None:
    """Reasoning that needs more than one product, e.g. {'REF': 58, 'CC': 0.82, 'ZDR': 0.2}."""
    ref, cc, zdr = readings.get("REF"), readings.get("CC"), readings.get("ZDR")
    if _usable(ref) and _usable(cc) and _usable(zdr):
        if ref >= 50 and cc < 0.93 and zdr < 1.0:
            return "high reflectivity, low CC and low ZDR: a hail core"
        if ref >= 25 and cc < 0.8:
            return "echo with low CC: debris, if it sits in a hook with strong rotation"
    return None
