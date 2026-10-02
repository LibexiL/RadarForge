"""How one storm cell has changed over the loop: hail, rotation and speed, frame by frame. No Qt here.

`history` is a list of (time, cells) pairs, one per radar frame, where cells are the dicts the Storm cells panel
builds (id, x, y, motion, posh, size, meso, tvs). A cell is followed by its SCIT id."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TVS_LEVEL = {"ETVS": 1, "TVS": 2}


@dataclass
class Series:
    cell_id: str
    times: list = field(default_factory=list)
    posh: list = field(default_factory=list)          # % (None when the radar gave no value)
    size: list = field(default_factory=list)          # inches
    meso: list = field(default_factory=list)          # MDA rank
    tvs: list = field(default_factory=list)           # 0 none, 1 elevated, 2 TVS
    speed: list = field(default_factory=list)         # knots
    range_km: list = field(default_factory=list)

    def __len__(self):
        return len(self.times)

    def latest(self, name: str):
        for v in reversed(getattr(self, name)):
            if v is not None:
                return v
        return None


def build(history: list, cell_id: str) -> Series:
    """The values of one cell in every frame that has it, oldest first."""
    s = Series(cell_id)
    for when, cells in sorted(history, key=lambda h: h[0]):
        c = next((c for c in cells or [] if c.get("id") == cell_id), None)
        if c is None:
            continue
        s.times.append(when)
        s.posh.append(c.get("posh"))
        s.size.append(c.get("size"))
        s.meso.append(c.get("meso"))
        s.tvs.append(TVS_LEVEL.get(c.get("tvs"), 0))
        m = c.get("motion")
        s.speed.append(m[1] if m else None)
        s.range_km.append(math.hypot(c["x"], c["y"]))
    return s


def trend_word(values: list, rising_by: float = 0.0) -> str:
    """'rising', 'falling' or 'steady' for the last few values (ignores gaps)."""
    v = [x for x in values if x is not None][-4:]
    if len(v) < 2:
        return "steady"
    d = v[-1] - v[0]
    return "rising" if d > rising_by + 1e-9 else "falling" if d < -(rising_by + 1e-9) else "steady"


def summary(s: Series) -> str:
    """One line: what the cell is doing now."""
    if not len(s):
        return f"Cell {s.cell_id}: no data in these frames"
    bits = [f"Cell {s.cell_id}"]
    posh, size, meso = s.latest("posh"), s.latest("size"), s.latest("meso")
    if posh:
        bits.append(f"POSH {posh}% ({trend_word(s.posh)})")
    if size:
        bits.append(f"hail {size:.2f}\" ({trend_word(s.size, 0.1)})")
    if meso is not None:
        bits.append(f"meso rank {meso} ({trend_word(s.meso)})")
    if s.tvs and s.tvs[-1]:
        bits.append("TVS" if s.tvs[-1] == 2 else "elevated TVS")
    return " · ".join(bits)
