"""NEXRAD Level III decoding (thin layer over MetPy's Level3File).

Produces either a radial image (az x gates of physical values) or a list of
graphic overlay items (storm tracks, mesocyclones, TVS, hail, melting layer).
All positions are returned in km east/north of the radar.
"""
from __future__ import annotations

import io
import itertools
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

KT_TO_MS = 0.514444
_uids = itertools.count(1)

# Product-code (numeric) -> post-processing of MetPy's map_data output so that
# every product ends up in RadarForge's canonical storage units.
#   velocities: m/s; precipitation accumulations: inches; rates: in/hr;
#   echo tops: kft; VIL: kg/m^2; HCA: class index (code/10)
_SCALE_BY_CODE = {
    56: KT_TO_MS,      # N?S storm relative mean radial velocity (kt)
    25: KT_TO_MS,      # legacy base velocity 16 level (kt)? kept for safety
    27: KT_TO_MS,
    30: KT_TO_MS,
    170: 0.01,         # DAA digital one-hour accumulation (0.01 in)
    172: 0.01,         # DTA digital storm total accumulation
    173: 0.01,         # DU3/DU6 user selectable accumulation
    174: 0.01,         # DOD one-hour difference
    175: 0.01,         # DSD storm total difference
}


@dataclass
class L3Radial:
    azimuths: np.ndarray      # centre azimuth of each radial (deg)
    widths: np.ndarray        # width of each radial (deg)
    values: np.ndarray        # float32 (nrad, ngates), NaN = no data
    first_gate: float         # km, centre of first gate
    gate_spacing: float       # km
    elevation: float          # deg (0 for volume / ground-range products)
    ground_range: bool = False


@dataclass
class Level3Product:
    awips: str                 # e.g. 'N0B'
    code: int                  # numeric product code
    name: str
    site: str
    time: datetime
    vol_time: datetime | None
    lat: float
    lon: float
    height_m: float
    elevation: float | None
    radial: L3Radial | None = None
    graphics: list = field(default_factory=list)
    text_pages: list = field(default_factory=list)
    source: str = ""
    uid: int = field(default_factory=lambda: next(_uids))


def _utc(dt):
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ground_range_code(code: int) -> bool:
    # Volume-derived products whose bins are on ground range
    return code in (134, 135, 169, 170, 171, 172, 173, 174, 175, 176, 177, 32, 37, 38, 41, 57)


def _radial_from_components(comp) -> L3Radial | None:
    if isinstance(comp, (list, tuple)):
        comp = next((c for c in comp if getattr(c, "radials", None)), None)
    rads = getattr(comp, "radials", None)
    if not rads:
        return None
    nb = max(r.num_bins for r in rads)
    vals = np.full((len(rads), nb), np.nan, np.float32)
    scale = 1.0
    attrs = (rads[0].attributes or "").lower()
    if "inches/hour" in attrs or "ushort" in attrs:
        scale = 0.001
    for i, r in enumerate(rads):
        d = np.asarray(r.data, dtype=np.float32)
        vals[i, :len(d)] = d * scale
    vals[vals <= 0] = np.nan
    az = np.array([r.azimuth for r in rads], np.float32)
    w = np.array([r.width for r in rads], np.float32)
    return L3Radial(az + w / 2.0, w, vals, comp.first_gate / 1000.0, comp.gate_width / 1000.0,
                    0.0, True)


def _graphics(sym_block) -> list:
    """Normalise MetPy symbology packets to simple dicts."""
    out = []
    color = None
    pending_track_owner = None
    for layer in sym_block or []:
        for p in layer:
            if not isinstance(p, dict):
                continue
            if "components" in p or "data" in p:
                continue
            if set(p.keys()) == {"color"}:
                color = p["color"]
                continue
            if "vectors" in p:
                out.append(dict(kind="line", points=list(p["vectors"]), color=p.get("color", color)))
                continue
            t = p.get("type")
            if "current storm position" in p:
                x, y = p["current storm position"]
                pending_track_owner = dict(kind="storm", x=x, y=y, id=None, past=[], fcst=[])
                out.append(pending_track_owner)
                continue
            if t == "Storm ID":
                # attach id to the storm/meso/tvs at the same position if possible
                for g in reversed(out[-6:]):
                    if g.get("x") == p["x"] and g.get("y") == p["y"] and g.get("id") is None:
                        g["id"] = p["id"]
                        break
                else:
                    out.append(dict(kind="label", x=p["x"], y=p["y"], text=p["id"]))
                continue
            if "track" in p and "markers" in p:
                markers = p["markers"]
                past = any("past" in str(m) for m in markers) if markers else False
                owner = None
                for g in reversed(out):
                    if g["kind"] in ("storm", "meso"):
                        owner = g
                        break
                pts = [tuple(xy) for xy in p["track"]]
                if owner is not None:
                    owner["past" if past else "fcst"] = pts
                else:
                    out.append(dict(kind="track", points=pts, past=past))
                continue
            if t in ("MDA", "MDA (Elev.)", "Mesocyclone", "3D Correlated Shear", "Uncorrelated Shear"):
                out.append(dict(kind="meso", x=p["x"], y=p["y"], radius=p.get("radius", 2.0),
                                elevated=("Elev" in t), id=None, strength=None, past=[], fcst=[]))
                continue
            if t in ("TVS", "ETVS"):
                out.append(dict(kind="tvs", x=p["x"], y=p["y"], elevated=(t == "ETVS"), id=None))
                continue
            if t == "HDA":
                out.append(dict(kind="hail", x=p["x"], y=p["y"], poh=p.get("POH"),
                                posh=p.get("POSH"), size=p.get("Max Size"), id=None))
                continue
            if "text" in p and "x" in p:
                # MDA strength rank labels sit on the circle centre
                for g in reversed(out[-4:]):
                    if g["kind"] == "meso" and g["x"] == p["x"] and g["y"] == p["y"]:
                        g["strength"] = p["text"].strip()
                        break
                else:
                    out.append(dict(kind="label", x=p["x"], y=p["y"], text=p["text"].strip()))
                continue
    return out


def read_level3(data: bytes | str, awips_hint: str = "", site_hint: str = "") -> Level3Product:
    from metpy.io import Level3File
    source = ""
    if isinstance(data, str):
        source = data
        with open(data, "rb") as fh:
            data = fh.read()
    f = Level3File(io.BytesIO(data))
    code = int(f.prod_desc.prod_code)
    md = f.metadata
    awips = awips_hint or ""
    if not awips:
        try:
            awips = f.header.product.decode()[:3] if hasattr(f, "header") else ""
        except Exception:
            awips = ""
    name = getattr(f, "product_name", "") or str(code)
    site = site_hint or getattr(f, "siteID", "") or ""
    el = md.get("el_angle")
    try:
        el = float(el) if el is not None else None
        if el is not None and (el < -1 or el > 90 or abs(el) < 1e-20):
            el = None
    except (TypeError, ValueError):
        el = None
    prod = Level3Product(awips=awips.upper(), code=code, name=name, site=site,
                         time=_utc(md.get("prod_time") or md.get("vol_time")),
                         vol_time=_utc(md.get("vol_time")),
                         lat=float(f.lat), lon=float(f.lon), height_m=float(f.height) * 0.3048,
                         elevation=el, source=source)

    radial = None
    sym = getattr(f, "sym_block", None)
    if sym:
        first = sym[0][0] if sym[0] else None
        if isinstance(first, dict) and "start_az" in first and "data" in first:
            raw = first["data"]
            nb = max(len(r) for r in raw)
            arr = np.zeros((len(raw), nb), np.int32)
            for i, r in enumerate(raw):
                arr[i, :len(r)] = np.frombuffer(bytes(r), np.uint8) if isinstance(r, (bytes, bytearray)) \
                    else np.asarray(r)
            mapped = f.map_data(arr)
            if isinstance(mapped, tuple):          # e.g. EET returns (values, topped flag)
                mapped = mapped[0]
            vals = mapped
            if np.ma.isMaskedArray(vals):
                vals = vals.filled(np.nan)
            vals = np.array(vals, np.float32)
            vals *= _SCALE_BY_CODE.get(code, 1.0)
            if code in (165, 177):     # hydrometeor classification -> class index
                vals = np.round(vals).astype(np.float32)
                vals[vals <= 0] = np.nan
            sa = np.asarray(first["start_az"], np.float32)
            ea = np.asarray(first["end_az"], np.float32)
            w = np.mod(ea - sa, 360.0)
            w[(w <= 0) | (w > 5)] = np.median(w[(w > 0) & (w <= 5)]) if np.any((w > 0) & (w <= 5)) else 1.0
            spacing = float(f.max_range) / nb
            radial = L3Radial(np.mod(sa + w / 2.0, 360.0), w, vals, spacing / 2.0, spacing,
                              el or 0.0, _ground_range_code(code))
        elif isinstance(first, dict) and "components" in first:
            radial = _radial_from_components(first["components"])
        prod.graphics = _graphics(sym)
    prod.radial = radial
    if hasattr(f, "tab_pages") and f.tab_pages:
        prod.text_pages = ["\n".join(pg) if isinstance(pg, list) else str(pg) for pg in f.tab_pages]
    if code == 141 and prod.text_pages:
        _attach_mda_table(prod)
    return prod


_MDA_ROW = re.compile(r"^\s*(\d+)\s+(\d+)/\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\d+)")


def _attach_mda_table(prod):
    """MDA graphics only carry the circulation ID; the strength rank lives in the text table."""
    rows = {}
    for page in prod.text_pages:
        for line in page.splitlines():
            m = _MDA_ROW.match(line)
            if m:
                circ, _az, _rng, sr, stm, llrv, _dv = m.groups()
                rows[circ] = dict(rank=int(sr), storm=stm, llrv=int(llrv),
                                  tvs=" Y " in line[line.find(stm) + len(stm):])
    for g in prod.graphics:
        if g.get("kind") != "meso":
            continue
        circ = str(g.get("strength") or "").strip()
        info = rows.get(circ)
        g["circ_id"] = circ
        if info:
            g.update(rank=info["rank"], storm=info["storm"], llrv=info["llrv"])
        else:
            g["rank"] = None


def read_level3_isolated(data: bytes | str, awips_hint: str = "", site_hint: str = "") -> Level3Product:
    """read_level3 in a decoder process (MetPy is slow to import and parse; keep it off the UI's GIL)."""
    from .level2 import run_isolated
    try:
        prod = run_isolated(read_level3, data, awips_hint, site_hint, timeout=90, level3=True)
    except Exception:
        prod = read_level3(data, awips_hint, site_hint)
    prod.uid = next(_uids)          # ids from the worker process aren't unique here
    return prod
