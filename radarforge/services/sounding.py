"""Soundings: the profile data structure and the severe-weather numbers worked out from it (CAPE, shear, helicity,
storm motion…) with MetPy. No Qt here."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

MS_TO_KT = 1.943844


def dewpoint_c(temp_c, rh_percent):
    """Dewpoint (°C) from temperature (°C) and relative humidity (%) (Bolton)."""
    t = np.asarray(temp_c, float)
    es = 6.112 * np.exp(17.67 * t / (t + 243.5))
    e = np.clip(np.asarray(rh_percent, float), 0.5, 100.0) / 100.0 * es
    ln = np.log(e / 6.112)
    return 243.5 * ln / (17.67 - ln)


def wind_components(direction_deg, speed):
    """(u, v) from the direction the wind blows FROM (degrees) and its speed."""
    d = np.radians(np.asarray(direction_deg, float))
    s = np.asarray(speed, float)
    return -s * np.sin(d), -s * np.cos(d)


def wind_direction(u, v):
    """Direction the wind blows FROM (degrees) for components u, v."""
    return (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0


@dataclass
class Sounding:
    pressure: np.ndarray          # hPa, ground first (decreasing)
    height: np.ndarray            # m above sea level
    temp: np.ndarray              # °C
    dewpoint: np.ndarray          # °C
    u: np.ndarray                 # knots, towards the east
    v: np.ndarray                 # knots, towards the north
    source: str = ""              # "HRRR 21Z run, +1 h" / "Observed, Norman OK"
    place: str = ""
    lat: float = 0.0
    lon: float = 0.0
    valid: datetime | None = None
    notes: list = field(default_factory=list)

    @property
    def agl(self) -> np.ndarray:
        return self.height - self.height[0]

    @property
    def speed(self) -> np.ndarray:
        return np.hypot(self.u, self.v)

    @staticmethod
    def build(p, z, t, td, u, v, **meta) -> "Sounding":
        """A sounding from raw levels: levels with missing values are dropped, pressure is made to decrease,
        and duplicate levels are removed."""
        arr = [np.asarray(x, float) for x in (p, z, t, td, u, v)]
        n = min(len(a) for a in arr)
        arr = [a[:n] for a in arr]
        good = np.isfinite(arr[0]) & np.isfinite(arr[2]) & np.isfinite(arr[3]) & np.isfinite(arr[1])
        arr = [a[good] for a in arr]
        order = np.argsort(-arr[0], kind="stable")
        arr = [a[order] for a in arr]
        keep = np.concatenate(([True], np.diff(arr[0]) < -0.01))
        arr = [a[keep] for a in arr]
        # winds missing at some levels: fill by interpolation in height (a hodograph needs every level)
        u_, v_ = arr[4], arr[5]
        ok = np.isfinite(u_) & np.isfinite(v_)
        if ok.sum() >= 2 and not ok.all():
            u_ = np.interp(arr[1], arr[1][ok], u_[ok])
            v_ = np.interp(arr[1], arr[1][ok], v_[ok])
        elif ok.sum() < 2:
            u_, v_ = np.zeros_like(u_), np.zeros_like(v_)
        return Sounding(arr[0], arr[1], arr[2], np.minimum(arr[3], arr[2]), u_, v_, **meta)

    @staticmethod
    def from_hrrr(raw, place: str = "") -> "Sounding":
        """From data.hrrr.Raw: the surface (2 m / 10 m values) followed by the pressure levels above it."""
        from ..data import hrrr
        z = hrrr.heights(raw.p, raw.t, raw.rh, raw.psfc, raw.zsfc, raw.t2m, raw.rh2m)
        t_c = np.concatenate(([raw.t2m - 273.15], raw.t - 273.15))
        rh = np.concatenate(([raw.rh2m], raw.rh))
        return Sounding.build(
            np.concatenate(([raw.psfc], raw.p)), np.concatenate(([raw.zsfc], z)), t_c, dewpoint_c(t_c, rh),
            np.concatenate(([raw.u10], raw.u)) * MS_TO_KT, np.concatenate(([raw.v10], raw.v)) * MS_TO_KT,
            source=f"HRRR {raw.run:%H}Z {raw.run:%d %b} run, +{raw.fhr} h", place=place or f"{raw.lat:.2f}, {raw.lon:.2f}",
            lat=raw.lat, lon=raw.lon, valid=raw.valid)


# ------------------------------------------------------------------------------------------------ the numbers
@dataclass
class Parameters:
    """Severe-weather numbers (None where they couldn't be worked out). Heights are above ground level."""
    sbcape: float | None = None
    sbcin: float | None = None
    mlcape: float | None = None
    mlcin: float | None = None
    mucape: float | None = None
    mucin: float | None = None
    lcl_m: float | None = None
    lfc_m: float | None = None
    el_m: float | None = None
    shear_1km: float | None = None          # knots
    shear_3km: float | None = None
    shear_6km: float | None = None
    srh_1km: float | None = None            # m2/s2, relative to the Bunkers right mover
    srh_3km: float | None = None
    right_mover: tuple | None = None        # (direction FROM degrees, speed kt)
    left_mover: tuple | None = None
    mean_wind_6km: tuple | None = None
    pwat_in: float | None = None
    freezing_m: float | None = None
    minus20_m: float | None = None
    lapse_0_3km: float | None = None        # °C per km
    lapse_3_6km: float | None = None
    stp: float | None = None
    scp: float | None = None


def _q(f, unit):
    """Float of a pint quantity in a unit, or None for nan / failure."""
    try:
        v = float(np.asarray(f.to(unit).magnitude, float).ravel()[0])      # some MetPy results are 1-element arrays
    except Exception:
        return None
    return None if v != v else v


def level_height(snd: Sounding, temp_c: float) -> float | None:
    """Height AGL where the temperature first falls to temp_c (interpolated), or None."""
    t, h = snd.temp, snd.agl
    for i in range(len(t) - 1):
        if (t[i] - temp_c) * (t[i + 1] - temp_c) <= 0 and t[i] != t[i + 1]:
            return float(h[i] + (temp_c - t[i]) / (t[i + 1] - t[i]) * (h[i + 1] - h[i]))
    return None


def lapse_rate(snd: Sounding, z0: float, z1: float) -> float | None:
    """Temperature drop per km between two heights AGL (positive = temperature falls with height)."""
    h = snd.agl
    if h[-1] < z1 or z1 <= z0:
        return None
    t0, t1 = np.interp([z0, z1], h, snd.temp)
    return float((t0 - t1) / ((z1 - z0) / 1000.0))


def parameters(snd: Sounding) -> Parameters:
    """CAPE, shear, helicity, storm motion and friends for a sounding (needs at least a few levels)."""
    import metpy.calc as mc
    from metpy.units import units
    out = Parameters()
    if len(snd.pressure) < 4:
        return out
    p = snd.pressure * units.hPa
    t = snd.temp * units.degC
    td = snd.dewpoint * units.degC
    h = snd.agl * units.m
    u = snd.u * units.knots
    v = snd.v * units.knots

    def attempt(fn):
        try:
            return fn()
        except Exception:
            return None
    for name, fn in (("sb", mc.surface_based_cape_cin), ("ml", mc.mixed_layer_cape_cin), ("mu", mc.most_unstable_cape_cin)):
        res = attempt(lambda fn=fn: fn(p, t, td))
        if res is not None:
            setattr(out, f"{name}cape", _q(res[0], "J/kg"))
            setattr(out, f"{name}cin", _q(res[1], "J/kg"))
    lcl = attempt(lambda: mc.lcl(p[0], t[0], td[0]))
    if lcl is not None:
        out.lcl_m = _hgt(snd, _q(lcl[0], "hPa"))
    lfc = attempt(lambda: mc.lfc(p, t, td))
    if lfc is not None:
        out.lfc_m = _hgt(snd, _q(lfc[0], "hPa"))
    el = attempt(lambda: mc.el(p, t, td))
    if el is not None:
        out.el_m = _hgt(snd, _q(el[0], "hPa"))
    top = float(snd.agl[-1])
    for key, depth in (("shear_1km", 1000), ("shear_3km", 3000), ("shear_6km", 6000)):
        if top >= depth:
            res = attempt(lambda d=depth: mc.bulk_shear(p, u, v, height=h, depth=d * units.m))
            if res is not None:
                setattr(out, key, float(math.hypot(_q(res[0], "knots") or 0.0, _q(res[1], "knots") or 0.0)))
    storm = attempt(lambda: mc.bunkers_storm_motion(p, u, v, h))
    if storm is not None:
        rm, lm, mean = (tuple(_q(c, "knots") for c in pair) for pair in storm)
        if any(c is None for pair in (rm, lm, mean) for c in pair):
            storm = None                      # no shear at all (calm, or one wind all the way up): no storm motion
    if storm is not None:
        out.right_mover = (wind_direction(*rm), math.hypot(*rm))
        out.left_mover = (wind_direction(*lm), math.hypot(*lm))
        out.mean_wind_6km = (wind_direction(*mean), math.hypot(*mean))
        for key, depth in (("srh_1km", 1000), ("srh_3km", 3000)):
            if top >= depth:
                res = attempt(lambda d=depth: mc.storm_relative_helicity(h, u, v, depth=d * units.m,
                                                                         storm_u=rm[0] * units.knots, storm_v=rm[1] * units.knots))
                if res is not None:
                    setattr(out, key, _q(res[2], "m**2/s**2"))
    pw = attempt(lambda: mc.precipitable_water(p, td))
    if pw is not None:
        out.pwat_in = _q(pw, "inch")
    out.freezing_m = level_height(snd, 0.0)
    out.minus20_m = level_height(snd, -20.0)
    out.lapse_0_3km = lapse_rate(snd, 0, 3000)
    out.lapse_3_6km = lapse_rate(snd, 3000, 6000)
    shear6 = None if out.shear_6km is None else (out.shear_6km * units.knots).to("m/s")      # the indices want m/s
    if None not in (out.sbcape, out.lcl_m, out.srh_1km, out.shear_6km):
        stp = attempt(lambda: mc.significant_tornado(out.sbcape * units("J/kg"), out.lcl_m * units.m,
                                                     out.srh_1km * units("m**2/s**2"), shear6))
        out.stp = None if stp is None else _q(stp, "dimensionless")
    if None not in (out.mucape, out.srh_3km, out.shear_6km):
        scp = attempt(lambda: mc.supercell_composite(out.mucape * units("J/kg"), out.srh_3km * units("m**2/s**2"),
                                                     out.shear_6km * units.knots))
        out.scp = None if scp is None else _q(scp, "dimensionless")
    return out


def _hgt(snd: Sounding, pressure_hpa: float | None) -> float | None:
    """Height AGL at a pressure (interpolated in log pressure)."""
    if pressure_hpa is None:
        return None
    lp = np.log(snd.pressure)
    return float(np.interp(math.log(pressure_hpa), lp[::-1], snd.agl[::-1]))


def table_rows(par: Parameters) -> list:
    """[(section, [(label, text)])] for display."""
    def num(v, fmt="{:.0f}", unit=""):
        if v is None:
            return "–"
        text = fmt.format(v)
        if text.startswith("-") and not text.strip("-0."):          # "-0" and "-0.0" read as a mistake
            text = text[1:]
        return text + unit

    def feet(m):
        return "–" if m is None else f"{m:,.0f} m"

    def storm(sm):
        return "–" if sm is None else f"{sm[0]:03.0f}° / {sm[1]:.0f} kt"
    return [
        ("Instability", [("SBCAPE", num(par.sbcape, unit=" J/kg")), ("SBCIN", num(par.sbcin, unit=" J/kg")),
                         ("MLCAPE", num(par.mlcape, unit=" J/kg")), ("MLCIN", num(par.mlcin, unit=" J/kg")),
                         ("MUCAPE", num(par.mucape, unit=" J/kg")), ("MUCIN", num(par.mucin, unit=" J/kg"))]),
        ("Levels (above ground)", [("LCL", feet(par.lcl_m)), ("LFC", feet(par.lfc_m)), ("EL", feet(par.el_m)),
                                   ("Freezing level", feet(par.freezing_m)), ("−20 °C level", feet(par.minus20_m))]),
        ("Wind shear", [("0–1 km", num(par.shear_1km, unit=" kt")), ("0–3 km", num(par.shear_3km, unit=" kt")),
                        ("0–6 km", num(par.shear_6km, unit=" kt"))]),
        ("Helicity and storm motion", [("SRH 0–1 km", num(par.srh_1km, unit=" m²/s²")),
                                       ("SRH 0–3 km", num(par.srh_3km, unit=" m²/s²")),
                                       ("Right mover", storm(par.right_mover)), ("Left mover", storm(par.left_mover)),
                                       ("Mean wind 0–6", storm(par.mean_wind_6km))]),
        ("Other (fixed-layer indices)", [("Precip. water", num(par.pwat_in, "{:.2f}", " in")),
                   ("Lapse 0–3 km", num(par.lapse_0_3km, "{:.1f}", " °C/km")),
                   ("Lapse 3–6 km", num(par.lapse_3_6km, "{:.1f}", " °C/km")),
                   ("Sig. tornado", num(par.stp, "{:.1f}")),
                   ("Supercell comp.", num(par.scp, "{:.1f}"))]),
    ]
