"""Model soundings (RAP / HRRR / NAM BUFKIT profiles from the Iowa Environmental Mesonet) and the
usual severe-weather numbers: CAPE, shear, helicity, storm motion.

  https://mesonet.agron.iastate.edu/api/1/nws/bufkit.json?lat=35.2&lon=-97.4&model=RAP
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import requests

BUFKIT_URL = "https://mesonet.agron.iastate.edu/api/1/nws/bufkit.json"
MODELS = ("RAP", "HRRR", "RRFS", "NAM", "NAM4KM", "GFS")
UA = {"User-Agent": "RadarForge (NEXRAD viewer; github.com/LibexiL/RadarForge)"}


@dataclass
class Profile:
    p: np.ndarray            # hPa, surface first
    t: np.ndarray            # °C
    td: np.ndarray           # °C
    z: np.ndarray            # m above sea level
    drct: np.ndarray         # deg
    sknt: np.ndarray         # kt
    time: datetime | None = None
    fhour: int = 0
    station: str = ""
    model: str = ""
    run: str = ""
    params: dict = field(default_factory=dict)

    @property
    def hagl(self):
        return self.z - self.z[0]

    def uv(self):
        """Wind components (kt), u east, v north."""
        a = np.radians(self.drct)
        return -self.sknt * np.sin(a), -self.sknt * np.cos(a)


def _t(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_bufkit(js: dict, model="") -> list:
    """All forecast-hour profiles in an IEM bufkit.json answer (levels with missing values dropped)."""
    out = []
    src = js.get("source") or {}
    station = str(src.get("station") or src.get("sid") or "") if isinstance(src, dict) else str(src)
    run = str(src.get("run_time") or src.get("runtime") or "") if isinstance(src, dict) else ""
    rt = _t(run)
    if rt is not None:
        run = f"{rt:%d %b %H}Z"
    for pr in js.get("profiles", []) or []:
        rows = []
        for lv in pr.get("levels", []) or []:
            try:
                row = [float(lv[k]) for k in ("PRES", "TMPC", "DWPC", "HGHT", "DRCT", "SKNT")]
            except (KeyError, TypeError, ValueError):
                continue
            if all(math.isfinite(v) and v > -9000 for v in row):
                rows.append(row)
        if len(rows) < 5:
            continue
        a = np.array(rows, float)
        a = a[np.argsort(-a[:, 0])]                         # surface (highest pressure) first
        keep = np.concatenate([[True], np.diff(a[:, 0]) < 0])   # strictly decreasing pressure
        a = a[keep]
        try:
            fh = int(pr.get("forecast_hour") or 0)
        except (TypeError, ValueError):
            fh = 0
        out.append(Profile(p=a[:, 0], t=a[:, 1], td=np.minimum(a[:, 2], a[:, 1]), z=a[:, 3], drct=a[:, 4],
                           sknt=a[:, 5], time=_t(pr.get("time")), fhour=fh, station=station, model=model, run=run,
                           params=dict(pr.get("parameters") or {})))
    return out


def fetch(lat, lon, model="RAP", when=None) -> list:
    """Profiles of the newest run (every forecast hour), or – for a past time `when` – the archived profile
    valid then."""
    params = {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "model": model}
    if when is not None:
        params["time"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    else:
        params["fall"] = "true"
    r = requests.get(BUFKIT_URL, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    profs = parse_bufkit(r.json(), model)
    if not profs:
        raise RuntimeError(f"no {model} sounding near this point")
    return profs


def interp_height(prof: Profile, values, h_agl):
    """Linear interpolation of a profile quantity to heights above ground (m)."""
    return np.interp(h_agl, prof.hagl, values)


def wind_at(prof: Profile, h_agl):
    u, v = prof.uv()
    return float(interp_height(prof, u, h_agl)), float(interp_height(prof, v, h_agl))


def bulk_shear(prof: Profile, top_m, bottom_m=0.0):
    u0, v0 = wind_at(prof, bottom_m)
    u1, v1 = wind_at(prof, top_m)
    return math.hypot(u1 - u0, v1 - v0)


def bunkers(prof: Profile):
    """Bunkers right- and left-mover storm motions (u, v kt) and the 0-6 km mean wind."""
    h = np.arange(0, 6001, 250.0)
    u, v = prof.uv()
    uu, vv = interp_height(prof, u, h), interp_height(prof, v, h)
    mean = (float(uu.mean()), float(vv.mean()))
    su, sv = wind_at(prof, 5750.0)
    bu, bv = wind_at(prof, 250.0)
    shu, shv = su - bu, sv - bv
    mag = math.hypot(shu, shv) or 1.0
    d = 7.5 * 1.943844                          # 7.5 m/s in kt
    right = (mean[0] + d * shv / mag, mean[1] - d * shu / mag)
    left = (mean[0] - d * shv / mag, mean[1] + d * shu / mag)
    return right, left, mean


def srh(prof: Profile, top_m, storm):
    """Storm-relative helicity (m²/s²) from the ground to top_m for a storm motion (u, v kt)."""
    h = np.arange(0, top_m + 1, 100.0)
    u, v = prof.uv()
    k = 0.514444                                 # kt -> m/s
    uu = (interp_height(prof, u, h) - storm[0]) * k
    vv = (interp_height(prof, v, h) - storm[1]) * k
    return float(np.sum(uu[1:] * vv[:-1] - uu[:-1] * vv[1:]))


def lapse_rate(prof: Profile, p_bot=700.0, p_top=500.0):
    lp = np.log(prof.p[::-1])
    t = np.interp([math.log(p_bot), math.log(p_top)], lp, prof.t[::-1])
    z = np.interp([math.log(p_bot), math.log(p_top)], lp, prof.z[::-1])
    return float((t[0] - t[1]) / ((z[1] - z[0]) / 1000.0))


def parcel_numbers(prof: Profile):
    """Surface-based, mixed-layer and most-unstable CAPE / CIN, LCL height, the surface parcel's
    temperature path and precipitable water, via MetPy (each number None if it can't be worked out)."""
    out = dict(sbcape=None, sbcin=None, mlcape=None, mlcin=None, mucape=None, lcl_m=None, pwat_in=None,
               parcel=None, freezing_m=None)
    try:
        import metpy.calc as mpcalc
        from metpy.units import units
    except Exception:
        return out
    p = prof.p * units.hPa
    t = prof.t * units.degC
    td = prof.td * units.degC
    try:
        par = mpcalc.parcel_profile(p, t[0], td[0]).to("degC")
        out["parcel"] = np.asarray(par.m, float)
        cape, cin = mpcalc.cape_cin(p, t, td, par)
        out["sbcape"], out["sbcin"] = float(cape.m), float(cin.m)
    except Exception:
        pass
    try:
        cape, cin = mpcalc.mixed_layer_cape_cin(p, t, td, depth=100 * units.hPa)
        out["mlcape"], out["mlcin"] = float(cape.m), float(cin.m)
    except Exception:
        pass
    try:
        cape, _cin = mpcalc.most_unstable_cape_cin(p, t, td)
        out["mucape"] = float(cape.m)
    except Exception:
        pass
    try:
        lp, _lt = mpcalc.lcl(p[0], t[0], td[0])
        out["lcl_m"] = float(np.interp(math.log(lp.m), np.log(prof.p[::-1]), prof.hagl[::-1]))
    except Exception:
        pass
    try:
        out["pwat_in"] = float(mpcalc.precipitable_water(p, td).to("inch").m)
    except Exception:
        pass
    above = np.where(prof.t <= 0)[0]
    if len(above):
        i = int(above[0])
        if i > 0:
            f = prof.t[i - 1] / (prof.t[i - 1] - prof.t[i])
            out["freezing_m"] = float(prof.hagl[i - 1] + f * (prof.hagl[i] - prof.hagl[i - 1]))
        else:
            out["freezing_m"] = 0.0
    for k in ("sbcape", "mlcape", "mucape", "sbcin", "mlcin"):
        if out[k] is not None and not math.isfinite(out[k]):
            out[k] = None
    return out


def indices(prof: Profile) -> dict:
    right, left, mean = bunkers(prof)
    res = parcel_numbers(prof)
    res.update(shear01=bulk_shear(prof, 1000), shear03=bulk_shear(prof, 3000), shear06=bulk_shear(prof, 6000),
               srh01=srh(prof, 1000, right), srh03=srh(prof, 3000, right), right=right, left=left, mean=mean)
    try:
        res["lr75"] = lapse_rate(prof)
    except Exception:
        res["lr75"] = None
    # significant tornado parameter (fixed layer), a rough guide only
    try:
        cape = res["mlcape"] or 0.0
        lcl = res["lcl_m"] if res["lcl_m"] is not None else 2000.0
        cin = res["mlcin"] or 0.0
        sh = res["shear06"] * 0.514444
        lcl_t = 1.0 if lcl < 1000 else 0.0 if lcl > 2000 else (2000 - lcl) / 1000
        cin_t = 1.0 if cin > -50 else 0.0 if cin < -200 else (200 + cin) / 150
        sh_t = 0.0 if sh < 12.5 else min(sh, 30.0) / 20.0
        res["stp"] = max(0.0, cape / 1500 * lcl_t * res["srh01"] / 150 * sh_t * cin_t)
    except Exception:
        res["stp"] = None
    return res


def motion_from(u, v):
    """(direction FROM in degrees, speed kt) of a motion vector."""
    spd = math.hypot(u, v)
    return (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0, spd
