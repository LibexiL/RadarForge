"""The command palette: every menu command plus radars, products, cities, bookmarks, workspaces and locations."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QTimer

from ...data.sites import all_sites
from ...products import catalog
from ...products.geometry import aeqd_forward
from ...services.commands import Command
from ..palette import CommandPalette

CITY_MIN_POP = 25000


class CommandsMixin:
    """The command palette (Ctrl+K)."""

    def open_palette(self):
        dlg = CommandPalette(self, self.command_list())
        if dlg.exec() and dlg.chosen is not None and dlg.chosen.run is not None:
            run = dlg.chosen.run
            QTimer.singleShot(0, run)                 # after the palette has closed, so dialogs open on top

    def command_list(self) -> list:
        cmds = self._menu_commands()
        n = len(cmds)
        for site in all_sites().values():
            if site.type == "tdwr":
                continue
            cmds.append(Command(f"Switch to radar {site.id} – {site.place}, {site.state}", "Radar",
                                lambda sid=site.id: self.switch_site(sid), keywords=f"{site.id} radar site nexrad",
                                order=n))
        for p in catalog.PRODUCTS:
            cmds.append(Command(f"Show {p.name} in the active panel", "Products",
                                lambda pid=p.id: self.set_panel_product(self.view.active_panel, pid),
                                keywords=f"{p.id} {p.short} product", order=n + 1))
        for name, ws in self.workspaces().items():
            cmds.append(Command(f"Workspace: {name}", "View › Workspaces", lambda w=name: self.apply_workspace(w),
                                detail=ws.get("note", ""), keywords="layout panels", order=n + 2))
        for bm in self._bookmarks():
            cmds.append(Command(f"Bookmark: {bm.get('name') or 'Bookmark'}", "File › Bookmarks",
                                lambda b=bm: self.open_bookmark(b), detail=bm.get("notes", "") or "", order=n + 2))
        for i, loc in enumerate(self.book.items):
            cmds.append(Command(f"Go to {loc.name}", "Locations" + (" (my location)" if i == 0 else ""),
                                lambda l=loc: self.go_to_location(l), order=n + 2))
        cmds += self._extra_commands(n + 2)
        cmds += self._city_commands(n + 3)
        return cmds

    def _extra_commands(self, order) -> list:
        """Hook for features that add their own palette entries (historic events, for one)."""
        return []

    def _menu_commands(self) -> list:
        out = []

        def walk(menu, path):
            for a in menu.actions():
                if a.isSeparator() or not a.text().strip():
                    continue
                text = a.text().replace("&", "").rstrip("…").strip()
                if a.menu() is not None:
                    walk(a.menu(), path + [text])
                    continue
                if not a.isEnabled():
                    continue
                out.append(Command(text, " › ".join(path), a.trigger, a.shortcut().toString(),
                                   "toggle on off show hide" if a.isCheckable() else "", order=0))
        for top in self.menuBar().actions():
            if top.menu() is not None:
                walk(top.menu(), [top.text().replace("&", "")])
        return out

    def _city_commands(self, order) -> list:
        raw = self.view.maps.raw
        if "city_name" not in raw:
            return []
        pop, names = raw["city_pop"], raw["city_name"]
        idx = np.nonzero(pop >= CITY_MIN_POP)[0]
        lat, lon = raw["city_lat"], raw["city_lon"]
        return [Command(f"Go to {names[i]}", f"City · population {int(pop[i]):,}",
                        lambda la=float(lat[i]), lo=float(lon[i]): self._go_to_latlon(la, lo), keywords="city town",
                        order=order) for i in idx]

    def _go_to_latlon(self, lat, lon):
        x, y = aeqd_forward(lat, lon, self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), max(self.view.scale, 3.0))
