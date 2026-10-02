"""Storm track maths: which towns a storm reaches, and when. No Qt here."""
from __future__ import annotations

import math


def track_etas(a, b, minutes: float, city_xy, city_pop, city_name, half_width_km: float = 10.0,
               min_pop: int = 2000, limit: int = 12) -> list:
    """Towns within half_width_km of the track from a to b (km), reached within `minutes`:
    [(name, minutes, x, y)] soonest first, each name once."""
    if city_xy is None or len(city_xy) == 0:
        return []
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length = math.hypot(dx, dy)
    if length < 0.5 or minutes <= 0:
        return []
    import numpy as np
    ux, uy = dx / length, dy / length
    px = city_xy[:, 0] - ax
    py = city_xy[:, 1] - ay
    along = px * ux + py * uy
    across = -px * uy + py * ux
    ok = (along >= 0) & (along <= length) & (np.abs(across) <= half_width_km) & (np.asarray(city_pop) >= min_pop)
    idx = np.nonzero(ok)[0]
    rows = sorted(((float(along[i]) / length * minutes, int(i)) for i in idx))
    out, seen = [], set()
    for m, i in rows:
        name = str(city_name[i])
        if name in seen:
            continue
        seen.add(name)
        out.append((name, m, float(city_xy[i, 0]), float(city_xy[i, 1])))
        if len(out) >= limit:
            break
    return out


def eta_at(a, b, minutes: float, p, half_width_km: float = 10.0):
    """Minutes until the track reaches point p (km), or None when it's off the corridor (or past 3x the arrow)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 0.5:
        return None
    ux, uy = dx / length, dy / length
    qx, qy = p[0] - a[0], p[1] - a[1]
    along = qx * ux + qy * uy
    across = -qx * uy + qy * ux
    if along < 0 or along > 3 * length or abs(across) > half_width_km:
        return None
    return along / length * minutes


def tick_minutes(minutes: int) -> int:
    return 10 if minutes <= 30 else 15 if minutes <= 60 else 30
