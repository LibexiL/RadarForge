"""GRLevelX-compatible color tables (.pal).

Supported directives: Product, Units, Scale, Offset, Step, Decimals, RF,
Color, Color4, SolidColor, SolidColor4, plus a RadarForge extension
`Label: value text` for categorical legends. Lines starting with ';' or '#'
are comments.

Semantics (matching GR2Analyst / GRLevel3):
  * values below the first entry are transparent
  * `Color: v r g b [r2 g2 b2]` blends from (r,g,b) at v to either the
    optional second colour or the next entry's colour at the next value
  * `SolidColor` holds a constant colour until the next entry
  * values at/above the last entry use the last entry's colour
  * data values are converted with  display = data * Scale + Offset
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PALETTE_DIR = Path(__file__).with_name("palettes")

# storage unit -> {table unit: factor}
_UNIT_FACTORS = {
    "m/s": {"KTS": 1.943844, "KT": 1.943844, "KNOTS": 1.943844, "MPH": 2.236936,
            "KMH": 3.6, "KM/H": 3.6, "KPH": 3.6, "M/S": 1.0, "MPS": 1.0},
    "kft": {"KFT": 1.0, "KM": 0.3048, "FT": 1000.0, "KFEET": 1.0},
    "in": {"IN": 1.0, "INCHES": 1.0, "MM": 25.4, "CM": 2.54},
    "in/hr": {"IN/HR": 1.0, "MM/HR": 25.4},
}


@dataclass
class Entry:
    value: float
    c1: tuple
    c2: tuple | None
    solid: bool


@dataclass
class ColorTable:
    name: str = ""
    product: str = ""
    units: str = ""
    scale: float | None = None
    offset: float = 0.0
    step: float | None = None
    decimals: int | None = None
    rf: tuple | None = (119, 0, 125, 255)
    entries: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    path: str = ""

    # ------------------------------------------------------------------ #
    def data_scale(self, storage_units: str) -> float:
        """Factor applied to stored data before lookup."""
        if self.scale is not None:
            return self.scale
        f = _UNIT_FACTORS.get(storage_units, {}).get(self.units.upper().replace(" ", ""))
        return f if f is not None else 1.0

    @property
    def vmin(self) -> float:
        return self.entries[0].value if self.entries else 0.0

    @property
    def vmax(self) -> float:
        if not self.entries:
            return 1.0
        if len(self.entries) == 1:
            return self.entries[0].value + 1.0
        last = self.entries[-1].value
        prev = self.entries[-2].value
        return last + max(last - prev, 1e-6)

    def color_at(self, v: np.ndarray) -> np.ndarray:
        """Vectorised lookup in display units -> RGBA uint8 (N,4)."""
        v = np.asarray(v, np.float64)
        out = np.zeros(v.shape + (4,), np.float64)
        if not self.entries:
            return out.astype(np.uint8)
        vals = np.array([e.value for e in self.entries])
        idx = np.searchsorted(vals, v, side="right") - 1
        for i, e in enumerate(self.entries):
            m = idx == i
            if not np.any(m):
                continue
            c1 = np.array(e.c1, np.float64)
            if e.solid or (i == len(self.entries) - 1 and e.c2 is None):
                out[m] = c1
                continue
            if e.c2 is not None:
                c2 = np.array(e.c2, np.float64)
            elif i + 1 < len(self.entries):
                c2 = np.array(self.entries[i + 1].c1, np.float64)
            else:
                c2 = c1
            v1 = e.value
            v2 = self.entries[i + 1].value if i + 1 < len(self.entries) else self.vmax
            t = np.clip((v[m] - v1) / max(v2 - v1, 1e-12), 0, 1)[:, None]
            out[m] = c1 + (c2 - c1) * t
        out[idx < 0] = 0
        out[np.isnan(v)] = 0
        return np.clip(np.round(out), 0, 255).astype(np.uint8)

    def lut(self, n: int = 1024) -> tuple[np.ndarray, float, float]:
        lo, hi = self.vmin, self.vmax
        x = lo + (np.arange(n) + 0.5) / n * (hi - lo)
        return self.color_at(x), lo, hi

    def legend_stops(self, count: int = 12):
        """(value, rgba) pairs for drawing a legend."""
        if self.labels:
            return [(v, tuple(self.color_at(np.array([v]))[0]), t) for v, t in sorted(self.labels.items())]
        lo, hi = self.vmin, self.vmax
        step = self.step
        if not step or (hi - lo) / step > 40:
            step = _nice_step((hi - lo) / count)
        start = np.ceil(lo / step) * step
        vals = np.arange(start, hi + 1e-9, step)
        return [(float(v), tuple(self.color_at(np.array([v]))[0]), None) for v in vals]


def _nice_step(x: float) -> float:
    if x <= 0:
        return 1.0
    p = 10 ** np.floor(np.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= x:
            return float(m * p)
    return float(10 * p)


_num = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def parse_pal(text: str, name: str = "") -> ColorTable:
    ct = ColorTable(name=name)
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip() if not raw.strip().lower().startswith("label:") else raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key = key.strip().lower()
        rest = rest.strip()
        nums = [float(x) for x in re.findall(_num, rest)]
        if key == "product":
            ct.product = rest
        elif key == "units":
            ct.units = rest
        elif key == "scale" and nums:
            ct.scale = nums[0]
        elif key == "offset" and nums:
            ct.offset = nums[0]
        elif key == "step" and nums:
            ct.step = nums[0]
        elif key == "decimals" and nums:
            ct.decimals = int(nums[0])
        elif key == "rf" and len(nums) >= 3:
            ct.rf = (int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3]) if len(nums) > 3 else 255)
        elif key in ("color", "solidcolor") and len(nums) >= 4:
            c1 = (nums[1], nums[2], nums[3], 255)
            c2 = (nums[4], nums[5], nums[6], 255) if len(nums) >= 7 and key == "color" else None
            ct.entries.append(Entry(nums[0], c1, c2, key == "solidcolor"))
        elif key in ("color4", "solidcolor4") and len(nums) >= 5:
            c1 = (nums[1], nums[2], nums[3], nums[4])
            c2 = (nums[5], nums[6], nums[7], nums[8]) if len(nums) >= 9 and key == "color4" else None
            ct.entries.append(Entry(nums[0], c1, c2, key == "solidcolor4"))
        elif key == "label":
            m = re.match(rf"\s*({_num})\s+(.*)$", rest)
            if m:
                ct.labels[float(m.group(1))] = m.group(2).strip()
    ct.entries.sort(key=lambda e: e.value)
    return ct


def load_pal(path: str | Path) -> ColorTable:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    ct = parse_pal(text, p.stem)
    ct.path = str(p)
    return ct


_builtin_cache: dict = {}


def builtin(name: str) -> ColorTable:
    if name not in _builtin_cache:
        _builtin_cache[name] = load_pal(PALETTE_DIR / f"{name}.pal")
    return _builtin_cache[name]


def list_builtin() -> list:
    return sorted(p.stem for p in PALETTE_DIR.glob("*.pal"))


# --------------------------------------------------------------------------- #
# matching a colour table to products
# --------------------------------------------------------------------------- #
# GRLevelX / GR2Analyst "Product:" names -> RadarForge palette family
_PRODUCT_FAMILY = {
    "BR": "REF", "REF": "REF", "BREF": "REF", "CR": "REF", "CREF": "REF", "Z": "REF", "DBZ": "REF",
    "N0Q": "REF", "N0B": "REF", "N0R": "REF",
    "BV": "VEL", "VEL": "VEL", "V": "VEL", "SRV": "VEL", "SRM": "VEL", "DV": "VEL", "N0U": "VEL",
    "N0G": "VEL", "N0S": "VEL",
    "SW": "SW", "ZDR": "ZDR", "DR": "ZDR", "N0X": "ZDR", "CC": "CC", "RHO": "CC", "RHOHV": "CC",
    "N0C": "CC", "KDP": "KDP", "N0K": "KDP", "PHI": "PHI", "PHIDP": "PHI", "DP": "PHI", "CFP": "CFP",
    "VIL": "VIL", "DVL": "VIL", "ET": "ET", "EET": "ET", "HC": "HCA", "HCA": "HCA", "HHC": "HCA",
    "N0H": "HCA", "MESH": "MESH", "MEHS": "MESH", "POSH": "POSH", "VILD": "VILD", "DPR": "PRATE",
    "DAA": "PACC", "DTA": "PACC", "DU3": "PACC", "OHA": "PACC", "STP": "PACC", "DSD": "PDIFF",
    "DOD": "PDIFF", "AZSHEAR": "SHEAR", "AZSH": "SHEAR", "SHEAR": "SHEAR", "DIV": "SHEAR",
}
_UNIT_FAMILY = {"DBZ": "REF", "KDP": "KDP", "DEG/KM": "KDP", "KG/M2": "VIL", "KFT": "ET"}

FAMILY_NAMES = {"REF": "reflectivity", "VEL": "velocity", "SW": "spectrum width", "ZDR": "ZDR",
                "CC": "correlation coefficient", "KDP": "KDP", "PHI": "differential phase", "CFP": "CFP",
                "VIL": "VIL", "ET": "echo tops", "HCA": "hydrometeor class", "MESH": "MESH", "POSH": "POSH",
                "VILD": "VIL density", "PRATE": "precip rate", "PACC": "precip accumulation",
                "PDIFF": "precip difference", "SHEAR": "shear"}


def table_family(ct: ColorTable):
    """(family or None, label) – which kind of product a colour table was made for."""
    raw = (ct.product or "").strip()
    key = raw.upper().replace(" ", "")
    if key in _PRODUCT_FAMILY:
        return _PRODUCT_FAMILY[key], raw
    ukey = (ct.units or "").strip().upper().replace(" ", "")
    if not key and ukey in _UNIT_FAMILY:
        return _UNIT_FAMILY[ukey], ct.units
    return None, raw or "(no Product: line)"


def looks_like_color_table(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() == ".pal":
        return True
    if p.suffix.lower() not in (".txt", ".ct", ""):
        return False
    try:
        head = p.read_bytes()[:4096]
        if b"\x00" in head:
            return False
        text = head.decode("utf-8", errors="ignore").lower()
        return ("color:" in text or "solidcolor:" in text or "color4:" in text) and "title:" not in text
    except OSError:
        return False
