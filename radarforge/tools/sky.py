"""Sun and sky for the realistic 3-D view (no Qt / OpenGL here).

* sun_position: where the sun was at the scan time, seen from the radar (good to a fraction of a degree).
* Sky: a small single-scattering sky (Rayleigh + haze) - the same formula as the shader's - giving the sunlight
  colour and the sky's ambient light for any sun height, so a low sun lights the storm orange and a high one white.
* noise_volume: a tileable 3-D noise texture that gives cloud edges their billows.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

# vertical optical depths: Rayleigh at roughly 680 / 550 / 440 nm, and the haze (Mie) part per visibility
TAU_R = np.array([0.043, 0.098, 0.24])
E0 = 18.0                     # sky brightness scale (the shader uses the same)
E_SUN = 3.2                   # sunlight above the atmosphere
NIGHT = np.array([0.0006, 0.0009, 0.0020])


def sun_position(when: datetime, lat: float, lon: float):
    """(azimuth from north, clockwise; elevation) in degrees for a UTC time and place."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    jd = when.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    ha = math.radians(gmst * 15.0 + lon) - ra
    la = math.radians(lat)
    el = math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(la) - math.sin(la) * math.cos(ha))
    el_deg = math.degrees(el)
    if el_deg > -1.0:                      # atmospheric refraction lifts a low sun a little
        el_deg += 1.02 / math.tan(math.radians(el_deg + 10.3 / (el_deg + 5.11))) / 60.0
    return math.degrees(az) % 360.0, el_deg


def sun_vector(az_deg: float, el_deg: float) -> np.ndarray:
    """Unit vector towards the sun: x east, y north, z up."""
    a, e = math.radians(az_deg), math.radians(el_deg)
    return np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])


def compass(az_deg: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int(((az_deg % 360) + 22.5) // 45) % 8]


def haze_tau(visibility_km: float) -> float:
    """Vertical haze optical depth for a horizontal visibility (Koschmieder, 1.2 km haze layer)."""
    return float(np.clip(3.912 / max(visibility_km, 1.0) * 1.2, 0.02, 2.0))


def airmass(sin_el):
    """Relative air mass for a sine of elevation (Kasten & Young, kept finite at and below the horizon)."""
    s = np.asarray(sin_el, np.float64)
    el = np.degrees(np.arcsin(np.clip(s, -1.0, 1.0)))
    el = np.maximum(el, 0.0)
    return 1.0 / (np.sin(np.radians(el)) + 0.50572 * (el + 6.07995) ** -1.6364)


def _hg(mu, g):
    return (1 - g * g) / (4 * math.pi * (1 + g * g - 2 * g * mu) ** 1.5)


def sky_radiance(rd, sd, tau_m: float):
    """Sky radiance (rgb) looking along unit vector(s) rd with the sun along sd - the shader's sky()."""
    rd = np.atleast_2d(np.asarray(rd, np.float64))
    sd = np.asarray(sd, np.float64)
    tt = TAU_R + tau_m
    mv = airmass(np.maximum(rd[:, 2], 0.0))[:, None]
    ms = float(airmass(max(sd[2], -0.1)))
    mu = rd @ sd
    pr = 3.0 / (16 * math.pi) * (1 + mu * mu)
    pm = _hg(mu, 0.76)
    scat = (TAU_R[None, :] * pr[:, None] + tau_m * pm[:, None]) / tt[None, :]
    day = _daylight(sd[2])
    L = E0 * scat * (1 - np.exp(-tt * mv)) * np.exp(-(TAU_R * 0.22 + tau_m * 0.5) * ms) * day
    return L + NIGHT


def _daylight(sin_el):
    """1 with the sun up, fading to 0 by 6 degrees below the horizon (twilight)."""
    x = np.clip((sin_el + 0.10) / 0.12, 0.0, 1.0)
    return x * x * (3 - 2 * x)


class Sky:
    """Light for a sun direction: sun colour, ambient sky light, horizon colour and a sensible exposure."""

    def __init__(self, sun_dir, visibility_km: float = 60.0):
        self.sun = np.asarray(sun_dir, np.float64) / max(np.linalg.norm(sun_dir), 1e-9)
        self.tau_m = haze_tau(visibility_km)
        s = self.sun[2]
        tt = TAU_R + self.tau_m
        ms = float(airmass(max(s, 0.0)))
        up = float(np.clip((s + 0.035) / 0.07, 0.0, 1.0))           # the disc sinking below the horizon
        self.sun_col = E_SUN * np.exp(-tt * ms) * up * up * (3 - 2 * up)
        # ambient: average radiance of the upper hemisphere (a coarse fixed set of directions)
        dirs = _HEMI
        w = dirs[:, 2:3]                           # (weighted towards overhead, like light falling on the ground)
        self.sky_amb = (sky_radiance(dirs, self.sun, self.tau_m) * w).sum(axis=0) / w.sum()
        h = np.array([self.sun[0], self.sun[1], 0.0])
        h = h / max(np.linalg.norm(h), 1e-9) if np.linalg.norm(h) > 1e-6 else np.array([1.0, 0, 0])
        self.horizon_sun = sky_radiance(np.array([[h[0] * 0.995, h[1] * 0.995, 0.1]]), self.sun, self.tau_m)[0]
        self.horizon_away = sky_radiance(np.array([[-h[0] * 0.995, -h[1] * 0.995, 0.1]]), self.sun,
                                         self.tau_m)[0]
        lum = float(np.dot(self.sky_amb, (0.2126, 0.7152, 0.0722)))
        lum_sun = float(np.dot(self.sun_col, (0.2126, 0.7152, 0.0722)))
        self.exposure = float(np.clip(0.45 / max(lum + 0.06 * lum_sun, 1e-4), 1.0, 9.0))
        self.night = s < -0.06

    @property
    def tau_r(self):
        return TAU_R


def _hemisphere(n=96):
    k = np.arange(n) + 0.5
    z = 1 - k / n                              # (uniform in solid angle over the upper half)
    phi = k * math.pi * (3 - math.sqrt(5))
    r = np.sqrt(1 - z * z)
    return np.c_[r * np.cos(phi), r * np.sin(phi), np.maximum(z, 0.02)]


_HEMI = _hemisphere()


def noise_volume(n: int = 64, seed: int = 11) -> np.ndarray:
    """uint8 (n, n, n) tileable fractal noise: big soft billows plus finer detail."""
    from scipy import ndimage
    rng = np.random.default_rng(seed)
    out = np.zeros((n, n, n), np.float32)
    for sigma, weight in ((5.0, 0.55), (2.4, 0.30), (1.1, 0.15)):
        a = ndimage.gaussian_filter(rng.random((n, n, n), np.float32), sigma, mode="wrap")
        a = (a - a.mean()) / (a.std() + 1e-9)
        out += weight * a
    lo, hi = np.percentile(out, (1.0, 99.0))
    return np.clip((out - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


SUN_PRESETS = {               # (label, azimuth, elevation)
    "scan": ("At scan time", None, None),
    "morning": ("Morning", 105.0, 18.0),
    "midday": ("Midday", 185.0, 62.0),
    "afternoon": ("Afternoon", 235.0, 35.0),
    "sunset": ("Sunset", 285.0, 4.0),
    "custom": ("Custom", None, None),
}
