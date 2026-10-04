"""Product catalog: every product a panel can display."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductDef:
    id: str
    name: str
    short: str
    category: str
    kind: str            # 'moment' | 'derived' | 'volume' | 'l3' | 'l3tilt'
    units: str           # storage units
    palette: str
    moment: str | None = None     # Level II moment used
    l3: str | None = None         # Level III AWIPS code or family letter
    tilted: bool = True
    smooth: bool = True
    categorical: bool = False
    decimals: int = 1
    description: str = ""

    def l3_code(self, tilt_index: int) -> str | None:
        if self.kind == "l3":
            return self.l3
        if self.kind == "l3tilt":
            return f"N{min(max(tilt_index, 0), 3)}{self.l3}"
        return None

    def l3_candidates(self, tilt_index: int) -> list:
        """Primary code plus legacy equivalents (N0Q for N0B, N0U for N0G)."""
        code = self.l3_code(tilt_index)
        if code is None:
            return []
        out = [code]
        if self.kind == "l3tilt":
            for alt in _LEGACY.get(self.l3, ()):
                out.append(code[:2] + alt)
        return out


_LEGACY = {"B": ("Q", "R"), "G": ("U", "V")}

P = ProductDef
PRODUCTS = [
    # --- Level II base moments -------------------------------------------------
    P("REF", "Base Reflectivity", "BR", "Base", "moment", "dBZ", "REF", "REF"),
    P("VEL", "Base Velocity", "BV", "Base", "moment", "m/s", "VEL", "VEL"),
    P("SW", "Spectrum Width", "SW", "Base", "moment", "m/s", "SW", "SW"),
    P("ZDR", "Differential Reflectivity", "ZDR", "Dual-Pol", "moment", "dB", "ZDR", "ZDR", decimals=2),
    P("CC", "Correlation Coefficient", "CC", "Dual-Pol", "moment", "", "CC", "RHO", decimals=3),
    P("PHI", "Differential Phase", "PHI", "Dual-Pol", "moment", "deg", "PHI", "PHI"),
    P("CFP", "Clutter Filter Power Removed", "CFP", "Base", "moment", "dB", "CFP", "CFP"),
    # --- per-tilt derived --------------------------------------------------------
    P("SRV", "Storm Relative Velocity", "SRV", "Derived", "derived", "m/s", "VEL", "VEL",
      description="Velocity minus the storm-motion component along each radial."),
    P("DVEL", "Dealiased Velocity", "DV", "Derived", "derived", "m/s", "VEL", "VEL",
      description="Region-based velocity unfolding."),
    P("KDP", "Specific Differential Phase", "KDP", "Dual-Pol", "derived", "deg/km", "KDP", "PHI", decimals=2,
      description="Range derivative of filtered differential phase."),
    P("AZSH", "Azimuthal Shear", "AzSh", "Derived", "derived", "/s x1e-3", "SHEAR", "VEL", decimals=1,
      description="Rotation: along-arc derivative of velocity (cyclonic positive)."),
    P("DIV", "Radial Divergence", "Div", "Derived", "derived", "/s x1e-3", "SHEAR", "VEL", decimals=1,
      description="Along-radial derivative of velocity (positive = divergence)."),
    # --- volume products ---------------------------------------------------------
    P("CREF", "Composite Reflectivity", "CR", "Volume", "volume", "dBZ", "REF", tilted=False),
    P("ET18", "Echo Tops (18 dBZ)", "ET", "Volume", "volume", "kft", "ET", tilted=False),
    P("ET30", "Echo Tops (30 dBZ)", "ET30", "Volume", "volume", "kft", "ET", tilted=False),
    P("ET50", "Echo Tops (50 dBZ)", "ET50", "Volume", "volume", "kft", "ET", tilted=False),
    P("VIL", "Vertically Integrated Liquid", "VIL", "Volume", "volume", "kg/m2", "VIL", tilted=False),
    P("VILD", "VIL Density", "VILD", "Volume", "volume", "g/m3", "VILD", tilted=False, decimals=2),
    P("MESH", "Max Expected Size of Hail", "MESH", "Volume", "volume", "in", "MESH", tilted=False,
      decimals=2),
    P("POSH", "Probability of Severe Hail", "POSH", "Volume", "volume", "%", "POSH", tilted=False,
      decimals=0),
    # --- Level III, per tilt -----------------------------------------------------
    P("L3B", "L3 Super-Res Reflectivity", "N0B", "Level III", "l3tilt", "dBZ", "REF", l3="B"),
    P("L3G", "L3 Super-Res Velocity", "N0G", "Level III", "l3tilt", "m/s", "VEL", l3="G"),
    P("L3S", "L3 Storm Relative Velocity", "N0S", "Level III", "l3tilt", "m/s", "VEL", l3="S",
      smooth=False),
    P("L3C", "L3 Correlation Coefficient", "N0C", "Level III", "l3tilt", "", "CC", l3="C", decimals=3),
    P("L3X", "L3 Differential Reflectivity", "N0X", "Level III", "l3tilt", "dB", "ZDR", l3="X",
      decimals=2),
    P("L3K", "L3 Specific Diff. Phase", "N0K", "Level III", "l3tilt", "deg/km", "KDP", l3="K", decimals=2),
    P("L3H", "L3 Hydrometeor Classification", "N0H", "Level III", "l3tilt", "class", "HCA", l3="H",
      smooth=False, categorical=True, decimals=0),
    # --- Level III, single -------------------------------------------------------
    P("L3DVL", "L3 Digital VIL", "DVL", "Level III", "l3", "kg/m2", "VIL", l3="DVL", tilted=False),
    P("L3EET", "L3 Enhanced Echo Tops", "EET", "Level III", "l3", "kft", "ET", l3="EET", tilted=False),
    P("L3HHC", "L3 Hybrid Hydrometeor Class", "HHC", "Level III", "l3", "class", "HCA", l3="HHC",
      tilted=False, smooth=False, categorical=True, decimals=0),
    P("L3DPR", "L3 Precipitation Rate", "DPR", "Level III", "l3", "in/hr", "PRATE", l3="DPR",
      tilted=False, decimals=2),
    P("L3DAA", "L3 One-Hour Precip", "DAA", "Level III", "l3", "in", "PACC", l3="DAA", tilted=False,
      decimals=2),
    P("L3DU3", "L3 Three-Hour Precip", "DU3", "Level III", "l3", "in", "PACC", l3="DU3", tilted=False,
      decimals=2),
    P("L3DTA", "L3 Storm Total Precip", "DTA", "Level III", "l3", "in", "PACC", l3="DTA", tilted=False,
      decimals=2),
    P("L3DSD", "L3 Storm Total Difference", "DSD", "Level III", "l3", "in", "PDIFF", l3="DSD",
      tilted=False, decimals=2),
    P("L3DOD", "L3 One-Hour Difference", "DOD", "Level III", "l3", "in", "PDIFF", l3="DOD",
      tilted=False, decimals=2),
]

BY_ID = {p.id: p for p in PRODUCTS}

# Level III graphic overlays (drawn on every panel when enabled)
L3_OVERLAYS = {
    "storm_tracks": ("NST", "Storm tracks (SCIT)"),
    "hail": ("NHI", "Hail index"),
    "melting_layer": ("N0M", "Melting layer"),
}

CATEGORIES = ["Base", "Dual-Pol", "Derived", "Volume", "Level III"]


def l3_fallbacks(code: str) -> list:
    """Older codes carrying the same data, tried when a radar/date has no `code` (N0B -> N0Q, N0R)."""
    if code and len(code) == 3 and code[0] == "N" and code[2] in _LEGACY:
        return [code[:2] + alt for alt in _LEGACY[code[2]]]
    return []


def get(pid: str) -> ProductDef:
    return BY_ID.get(pid) or BY_ID["REF"]


def l3_codes_for(pids, tilt_index: int) -> set:
    out = set()
    for pid in pids:
        p = BY_ID.get(pid)
        if p and p.kind in ("l3", "l3tilt"):
            out.add(p.l3_code(tilt_index))
    return out
