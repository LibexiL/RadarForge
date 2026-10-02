"""Draws a sounding: skew-T log-P with the parcel, a hodograph and wind barbs. Uses MetPy and matplotlib; takes a
matplotlib Figure so the window can show it and the tests can draw it without a screen."""
from __future__ import annotations

import numpy as np

from ..services.sounding import Parameters, Sounding

# colours for the hodograph by height above ground (km): 0-1, 1-3, 3-5, 5-8, 8-10
HODO_BOUNDS = (0, 1000, 3000, 5000, 8000, 10000)
HODO_COLORS = ("#ff4d4d", "#ffa631", "#f2e24a", "#52d36e", "#4db8ff")


def palette(dark: bool = True) -> dict:
    return ({"bg": "#14161c", "fg": "#d8dde6", "dim": "#7d8594", "grid": "#2c313c", "temp": "#ff5555", "dew": "#4fd66d",
             "parcel": "#f5f5f5", "cape": "#ff8a3d", "cin": "#4d9dff"} if dark else
            {"bg": "#ffffff", "fg": "#1b2030", "dim": "#6a7385", "grid": "#d5d9e2", "temp": "#d62020", "dew": "#1f9a3c",
             "parcel": "#222222", "cape": "#e86a10", "cin": "#1c64c4"})


def draw(fig, snd: Sounding, par: Parameters | None = None, dark: bool = True, title: str = ""):
    """(Re)draws the whole sounding on a Figure."""
    import metpy.calc as mc
    from metpy.plots import Hodograph, SkewT
    from metpy.units import units
    c = palette(dark)
    fig.clear()
    fig.patch.set_facecolor(c["bg"])
    p = snd.pressure * units.hPa
    t = snd.temp * units.degC
    td = snd.dewpoint * units.degC

    skew = SkewT(fig, rect=(0.075, 0.075, 0.57, 0.83), rotation=45)
    ax = skew.ax
    ax.set_facecolor(c["bg"])
    skew.plot_dry_adiabats(color="#7a5b3c", alpha=0.35, linewidth=0.7)
    skew.plot_moist_adiabats(color="#3c6e7a", alpha=0.35, linewidth=0.7)
    skew.plot_mixing_lines(color="#3c7a4b", alpha=0.3, linewidth=0.7)
    ax.set_ylim(1050, 100)
    ax.set_xlim(-40, 50)
    ax.axvline(0, color="#5aa0ff", linestyle="--", linewidth=0.8, alpha=0.6)         # freezing line
    skew.plot(p, t, color=c["temp"], linewidth=2.2)
    skew.plot(p, td, color=c["dew"], linewidth=2.2)
    try:                                                                               # the surface parcel
        parcel = mc.parcel_profile(p, t[0], td[0]).to("degC")
        skew.plot(p, parcel, color=c["parcel"], linewidth=1.4, linestyle="--")
        skew.shade_cape(p, t, parcel, facecolor=c["cape"], alpha=0.35)
        skew.shade_cin(p, t, parcel, td, facecolor=c["cin"], alpha=0.35)
    except Exception:
        pass
    keep, last = [], None
    for i, pr in enumerate(snd.pressure):                        # a barb about every 30 hPa (every 20 above 400)
        if pr >= 150 and (last is None or last - pr >= (30 if pr > 400 else 20)):
            keep.append(i)
            last = pr
    skew.plot_barbs(p[keep], snd.u[keep] * units.knots, snd.v[keep] * units.knots, xloc=1.0, length=6.5,
                    color=c["fg"], linewidth=0.8)
    ax.set_xlabel("Temperature (°C)", color=c["dim"])
    ax.set_ylabel("Pressure (hPa)", color=c["dim"])
    ax.tick_params(colors=c["dim"])
    for sp in ax.spines.values():
        sp.set_color(c["grid"])
    ax.grid(True, color=c["grid"], alpha=0.5, linewidth=0.5)
    ax.set_yticks([1000, 900, 800, 700, 600, 500, 400, 300, 200, 100])
    ax.set_yticklabels(["1000", "900", "800", "700", "600", "500", "400", "300", "200", "100"])
    # a few marked heights above ground on the right edge
    for km in (1, 3, 6, 9):
        if snd.agl[-1] >= km * 1000:
            pk = float(np.interp(km * 1000, snd.agl, snd.pressure))
            ax.text(1.012, pk, f"{km} km", color=c["dim"], fontsize=8, va="center", clip_on=False,
                    transform=ax.get_yaxis_transform())
    if par is not None:
        has_cape = (par.sbcape or 0) >= 25
        for name, h_m in (("LCL", par.lcl_m), ("LFC", par.lfc_m if has_cape else None), ("EL", par.el_m if has_cape else None)):
            if h_m is not None and h_m <= snd.agl[-1]:
                pk = float(np.interp(h_m, snd.agl, snd.pressure))
                ax.plot([-40, -33], [pk, pk], color=c["parcel"], linewidth=1.2)
                ax.text(-32.5, pk, name, color=c["parcel"], fontsize=8, va="center")

    # ---- hodograph
    hax = fig.add_axes([0.715, 0.585, 0.255, 0.33])
    hax.set_facecolor(c["bg"])
    top = min(float(snd.agl[-1]), 10000.0)
    lim = max(40.0, float(np.ceil(np.max(np.hypot(snd.u[snd.agl <= top], snd.v[snd.agl <= top])) / 20.0) * 20.0 + 10.0))
    hodo = Hodograph(hax, component_range=lim)
    hodo.add_grid(increment=20, color=c["grid"], linewidth=0.8)
    sel = snd.agl <= top
    if sel.sum() >= 2:
        heights = snd.agl[sel]
        from matplotlib.colors import BoundaryNorm, ListedColormap
        cmap = ListedColormap(HODO_COLORS)
        norm = BoundaryNorm(HODO_BOUNDS, cmap.N)
        hodo.plot_colormapped(snd.u[sel], snd.v[sel], heights, cmap=cmap, norm=norm, linewidth=2.6)
    if par is not None and par.right_mover is not None:
        for label, sm, marker in (("RM", par.right_mover, "o"), ("LM", par.left_mover, "s"), ("MW", par.mean_wind_6km, "x")):
            if sm is None:
                continue
            u_, v_ = -sm[1] * np.sin(np.radians(sm[0])), -sm[1] * np.cos(np.radians(sm[0]))
            hax.plot(u_, v_, marker, color=c["fg"], markersize=6, markerfacecolor="none" if marker != "x" else None)
            hax.annotate(label, (u_, v_), color=c["fg"], fontsize=8, xytext=(4, 4), textcoords="offset points")
    hax.tick_params(colors=c["dim"], labelsize=8)
    for sp in hax.spines.values():
        sp.set_color(c["grid"])
    hax.set_title("Hodograph (knots)", color=c["fg"], fontsize=9)
    # key to the hodograph colours
    for i, col in enumerate(HODO_COLORS):
        fig.text(0.70 + i * 0.056, 0.533, f"{HODO_BOUNDS[i] // 1000}-{HODO_BOUNDS[i + 1] // 1000}", color=col, fontsize=7.5)
    fig.text(0.70, 0.517, "kilometres above ground", color=c["dim"], fontsize=7)
    if par is not None:
        _table(fig, par, c)

    fig.text(0.075, 0.955, title or snd.source, color=c["fg"], fontsize=12, fontweight="bold")
    when = f"valid {snd.valid:%Y-%m-%d %H:%M}Z" if snd.valid is not None else ""
    fig.text(0.075, 0.925, "  ".join(x for x in (snd.place, when) if x), color=c["dim"], fontsize=9)
    return fig


def _table(fig, par: Parameters, c: dict):
    """The numbers (CAPE, levels, shear, helicity…) as two columns of text under the hodograph."""
    from ..services.sounding import table_rows
    sections = table_rows(par)
    columns = [sections[:2], sections[2:]]
    for ci, col in enumerate(columns):
        x = 0.668 + ci * 0.165
        y = 0.482
        for name, rows in col:
            fig.text(x, y, name.upper(), color=c["dim"], fontsize=7, fontweight="bold")
            y -= 0.0215
            for label, text in rows:
                fig.text(x, y, label, color=c["fg"], fontsize=7.5)
                fig.text(x + 0.078, y, text, color=c["fg"], fontsize=7.5, fontweight="bold")
                y -= 0.0205
            y -= 0.012
