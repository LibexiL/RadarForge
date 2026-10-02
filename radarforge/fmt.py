"""Small text formatting helpers shared by the map, the status bar and the dialogs."""
from __future__ import annotations

from datetime import datetime


def compass(deg: float) -> str:
    dirs = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return dirs[int(((deg % 360) / 22.5) + 0.5) % 16]


def local_hm(t: datetime) -> str:
    """'4:52 PM' in the computer's time zone."""
    lt = t.astimezone()
    return f"{lt:%I:%M %p}".lstrip("0")
