"""Radar beam geometry (4/3 effective earth radius model)."""
from __future__ import annotations

import numpy as np

EARTH_R = 6371.0
AE = EARTH_R * 4.0 / 3.0          # effective earth radius (km)


def beam_height(r_km, elev_deg):
    """Height of beam centre above the antenna (km)."""
    r = np.asarray(r_km, np.float64)
    th = np.radians(elev_deg)
    return np.sqrt(r * r + AE * AE + 2.0 * r * AE * np.sin(th)) - AE


def ground_range(r_km, elev_deg):
    r = np.asarray(r_km, np.float64)
    th = np.radians(elev_deg)
    h = beam_height(r, elev_deg)
    return AE * np.arcsin(np.clip(r * np.cos(th) / (AE + h), -1, 1))


def slant_range(s_km, elev_deg):
    """Slant range of the beam at ground distance s (km)."""
    phi = np.asarray(s_km, np.float64) / AE
    th = np.radians(elev_deg)
    return AE * np.sin(phi) / np.cos(th + phi)


def point_to_beam(s_km, z_km):
    """(elevation deg, slant range km) of a point at ground distance s, height z above antenna."""
    phi = np.asarray(s_km, np.float64) / AE
    rad = AE + np.asarray(z_km, np.float64)
    x = rad * np.sin(phi)
    y = rad * np.cos(phi) - AE
    return np.degrees(np.arctan2(y, x)), np.hypot(x, y)


def fold(d, nyq):
    """Wrap velocity differences into [-nyq, nyq)."""
    iv = 2.0 * nyq
    return d - iv * np.round(d / iv)


# --------------------------------------------------------------------------- #
# Azimuthal equidistant projection centred on the radar (km)
# --------------------------------------------------------------------------- #
def aeqd_forward(lat, lon, lat0, lon0):
    lat = np.radians(np.asarray(lat, np.float64))
    lon = np.radians(np.asarray(lon, np.float64))
    p0, l0 = np.radians(lat0), np.radians(lon0)
    dl = lon - l0
    cosc = np.sin(p0) * np.sin(lat) + np.cos(p0) * np.cos(lat) * np.cos(dl)
    cosc = np.clip(cosc, -1.0, 1.0)
    c = np.arccos(cosc)
    with np.errstate(invalid="ignore", divide="ignore"):
        k = np.where(c > 1e-12, c / np.sin(c), 1.0)
    x = EARTH_R * k * np.cos(lat) * np.sin(dl)
    y = EARTH_R * k * (np.cos(p0) * np.sin(lat) - np.sin(p0) * np.cos(lat) * np.cos(dl))
    return x, y


def aeqd_inverse(x, y, lat0, lon0):
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    p0, l0 = np.radians(lat0), np.radians(lon0)
    rho = np.hypot(x, y)
    c = rho / EARTH_R
    with np.errstate(invalid="ignore", divide="ignore"):
        lat = np.arcsin(np.clip(np.cos(c) * np.sin(p0) + np.where(rho > 0, y * np.sin(c) * np.cos(p0) / rho, 0),
                                -1, 1))
        lon = l0 + np.arctan2(x * np.sin(c), rho * np.cos(p0) * np.cos(c) - y * np.sin(p0) * np.sin(c))
    return np.degrees(lat), (np.degrees(lon) + 540.0) % 360.0 - 180.0


def az_range(x, y):
    """Azimuth (deg clockwise from north) and ground distance (km) from radar."""
    return (np.degrees(np.arctan2(x, y)) + 360.0) % 360.0, np.hypot(x, y)
