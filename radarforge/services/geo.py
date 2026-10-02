"""Small geography helpers for the alert logic: distances from a point to warning polygons, and bearings."""
from __future__ import annotations

import math

import numpy as np

KM_PER_DEG = 111.195
KM_PER_MILE = 1.609344


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def ring_distance_km(lat: float, lon: float, ring) -> float:
    """Distance (km) from a point to the outline of one ring of (lon, lat) pairs; 0 is not special-cased
    for 'inside' (see rings_distance_km)."""
    pts = np.asarray(ring, float)
    if len(pts) < 2:
        return float("inf")
    cos = math.cos(math.radians(lat))
    x = (pts[:, 0] - lon) * cos * KM_PER_DEG
    y = (pts[:, 1] - lat) * KM_PER_DEG
    a = np.stack([x, y], 1)
    b = np.roll(a, -1, axis=0)
    ab = b - a
    den = (ab * ab).sum(1)
    t = np.clip(-(a * ab).sum(1) / np.where(den > 0, den, 1.0), 0.0, 1.0)
    d = a + ab * t[:, None]
    return float(np.sqrt((d * d).sum(1)).min())


def rings_distance_km(lat: float, lon: float, rings) -> float:
    """0 when the point is inside any ring (even-odd), else the distance to the nearest outline (km)."""
    from ..data.feeds import rings_contain
    pairs = [[(float(p[0]), float(p[1])) for p in r] for r in rings]
    if rings_contain(pairs, lat, lon):
        return 0.0
    return min((ring_distance_km(lat, lon, r) for r in rings), default=float("inf"))
