"""Following a storm: where is it in the next radar frame? Pure maths, no Qt.

A target remembers where the storm was and how it was moving. For each new frame we predict where it should be,
look for the nearest storm cell (by its SCIT id if the radar is the same, else by position) and take its position;
if the radar has no cell there (the data hasn't arrived, or the storm isn't tracked) we carry on with the motion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

KT_TO_KM_PER_MIN = 1.852 / 60.0


@dataclass
class Target:
    x: float                          # km east of the radar the map is centred on
    y: float                          # km north
    time: datetime                    # when the storm was there
    motion: tuple | None = None       # (direction it moves FROM in degrees, knots)
    cell_id: str | None = None        # SCIT id of the cell being followed
    site: str = ""                    # radar the id belongs to
    label: str = ""

    def predicted(self, when: datetime) -> tuple:
        """Where the storm should be at `when`, carrying on with its motion."""
        if not self.motion:
            return self.x, self.y
        minutes = (when - self.time).total_seconds() / 60.0
        heading = math.radians((self.motion[0] + 180.0) % 360.0)
        d = self.motion[1] * KT_TO_KM_PER_MIN * minutes
        return self.x + math.sin(heading) * d, self.y + math.cos(heading) * d


def nearest_cell(cells: list, x: float, y: float, max_km: float):
    """The cell (dict with x, y) closest to a point, within max_km, or None."""
    best, bd = None, max_km
    for c in cells:
        d = math.hypot(c["x"] - x, c["y"] - y)
        if d < bd:
            best, bd = c, d
    return best


def update(target: Target, cells: list, when: datetime, site: str, min_jump_km: float = 10.0):
    """The target moved to `when`. Returns (new target, the cell it matched or None).

    Prefers the cell with the same id on the same radar; otherwise the nearest cell to where the storm should be,
    allowing for a storm that is faster or slower than expected."""
    px, py = target.predicted(when)
    minutes = abs((when - target.time).total_seconds()) / 60.0
    reach = max(min_jump_km, 1.6 * (target.motion[1] if target.motion else 30.0) * KT_TO_KM_PER_MIN * minutes + 6.0)
    cell = None
    if target.cell_id and site == target.site:
        cell = next((c for c in cells if c["id"] == target.cell_id and math.hypot(c["x"] - px, c["y"] - py) <= reach * 1.5), None)
    if cell is None:
        cell = nearest_cell(cells, px, py, reach)
    if cell is None:
        return Target(px, py, when, target.motion, target.cell_id, target.site, target.label), None
    motion = target.motion
    if minutes >= 1.0 and when > target.time:
        dx, dy = cell["x"] - target.x, cell["y"] - target.y
        kts = math.hypot(dx, dy) / minutes / KT_TO_KM_PER_MIN
        frm = (math.degrees(math.atan2(dx, dy)) + 180.0) % 360.0
        if kts >= 3:
            if motion:                                       # blend with what we had, so one odd fix doesn't swing it
                kts = 0.6 * motion[1] + 0.4 * kts
                dirs = [math.radians(motion[0]), math.radians(frm)]
                frm = (math.degrees(math.atan2(0.6 * math.sin(dirs[0]) + 0.4 * math.sin(dirs[1]),
                                               0.6 * math.cos(dirs[0]) + 0.4 * math.cos(dirs[1]))) + 360.0) % 360.0
            motion = (frm, kts)
    elif cell.get("motion"):
        motion = cell["motion"]
    return Target(cell["x"], cell["y"], when, motion, cell["id"], site, target.label), cell


def label(target: Target) -> str:
    """'B7 · 245° / 32 kt'"""
    name = target.cell_id or "storm"
    if target.motion:
        return f"{name} · {target.motion[0]:03.0f}° / {target.motion[1]:.0f} kt"
    return name
