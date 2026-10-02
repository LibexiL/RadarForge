"""Themes, colour tables and the map display switches."""
from __future__ import annotations

import os

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .. import icons
from ... import themes
from ...products import catalog, colortable


class AppearanceMixin:
    """Themes, colour tables and the map display switches."""

    def apply_theme(self, name, save=True):
        theme = self.preview_theme(themes.find(name))
        self.settings["theme"] = theme["name"]
        if save:
            self.settings.save()
        return theme

    def preview_theme(self, theme):
        """Apply a theme (dict) to the interface and the map without remembering it."""
        theme = themes.normalize(theme)
        app = QApplication.instance()
        themes.apply_ui(app, theme)
        colors, layers = themes.map_style(theme)
        self.view.set_colors(colors, layers)
        icons.clear_cache()
        for target, ic in self._icon_targets:
            target.setIcon(icons.icon(ic))
        self.play_act.setIcon(icons.icon("pause" if self.playing else "play"))
        for w in (self.ws, self.xs_win, self.v3d_host):
            if w is not None:
                w.update()
        return theme

    def import_theme(self, path):
        try:
            t = themes.import_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "Theme", f"Couldn't use {os.path.basename(path)} as a theme:\n{exc}")
            return
        self.apply_theme(t["name"])
        self._status_msg(f"Theme “{t['name']}” added and applied (View → Theme to switch back)")

    def _apply_view_settings(self):
        s = self.settings
        self.view.smooth = bool(s["gpu_smooth"])
        self.view.show_cities = bool(s["map_layers"].get("cities", True))
        self.view.show_range_rings = bool(s["map_layers"].get("range_rings", False))
        self.view.show_sites = bool(s["map_layers"].get("radar_sites", True))
        self.view.show_tdwr = bool(s["map_layers"].get("tdwr_sites", False))
        self.view.link_cursor = bool(s["cursor_link"])
        self.view.distance_units = s["distance_units"]
        self.view.show_legend = bool(s["show_legend"])
        self.view.invert_wheel = bool(s["invert_scroll"])
        self.view.hover_text = bool(s["hover_text"])
        self.view._city_cache.clear()
        self.view.update()

    def palette_for(self, pid):
        p = catalog.get(pid)
        path = self.settings["palette_overrides"].get(pid)
        key = (pid, path)
        ct = self._palettes.get(key)
        if ct is None:
            try:
                ct = colortable.load_pal(path) if path else colortable.builtin(p.palette)
            except Exception as exc:
                self._status_msg(f"Colour table {path}: {exc}")
                ct = colortable.builtin(p.palette)
            self._palettes[key] = ct
        return ct

    def apply_color_table(self, path, pid=None):
        """Use a .pal for product *pid* (None = every product it was made for). Returns True if applied."""
        name = os.path.basename(path)
        try:
            ct = colortable.load_pal(path)
            if not ct.entries:
                raise ValueError("no Color: lines found")
        except Exception as exc:
            QMessageBox.warning(self, "Colour table", f"Could not read {name}:\n{exc}")
            return False
        fam, label = colortable.table_family(ct)
        fam_pids = [p.id for p in catalog.PRODUCTS if p.palette == fam] if fam else []
        targets = []
        if pid is not None:
            panel_fam = catalog.get(pid).palette
            if fam == panel_fam:
                targets = [pid]
            else:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("Colour table doesn't match")
                what = f"a <b>{colortable.FAMILY_NAMES.get(fam, fam)}</b> table" if fam else \
                    f"made for <b>{label}</b>, which RadarForge doesn't recognise"
                box.setText(f"<b>{name}</b> is {what}.<br>You dropped it on "
                            f"<b>{catalog.get(pid).name}</b>.")
                here = box.addButton(f"Use for {catalog.get(pid).name} anyway", QMessageBox.AcceptRole)
                instead = box.addButton(f"Use for {colortable.FAMILY_NAMES.get(fam, fam)} products",
                                        QMessageBox.ActionRole) if fam_pids else None
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if box.clickedButton() is here:
                    targets = [pid]
                elif instead is not None and box.clickedButton() is instead:
                    targets = fam_pids
                else:
                    return False
        else:
            if not fam_pids:
                QMessageBox.information(self, "Colour table",
                                        f"{name} is for “{label}”, which RadarForge doesn't have. Drop it "
                                        f"directly onto a panel to use it there anyway.")
                return False
            targets = fam_pids
        for t in targets:
            self.settings["palette_overrides"][t] = path
        self.settings.save()
        self._palettes.clear()
        self._panel_req.clear()
        self._show_frame()
        names = ", ".join(catalog.get(t).short for t in targets)
        self._status_msg(f"Colour table {name} → {names}")
        return True

    def _load_pal_for(self, pid):
        path, _ = QFileDialog.getOpenFileName(self, "Colour table", "", "Colour tables (*.pal *.txt);;All (*)")
        if path:
            self.apply_color_table(path, pid)

    def _reset_pal_for(self, pid):
        self.settings["palette_overrides"].pop(pid, None)
        self.settings.save()
        self._panel_req.clear()
        self._show_frame()

    def _toggle_smooth(self, on):
        self.settings["gpu_smooth"] = on
        self.view.smooth = on
        self.view.update()

    def _toggle_legend(self, on):
        self.settings["show_legend"] = on
        self.view.show_legend = on
        self.view.update()

    def _toggle_cities(self, on):
        self.settings["map_layers"]["cities"] = on
        self.view.show_cities = on
        self.view.update()

    def _toggle_sites(self, on):
        self.settings["map_layers"]["radar_sites"] = on
        self.view.show_sites = on
        self.view.update()

    def _toggle_tdwr(self, on):
        self.settings["map_layers"]["tdwr_sites"] = on
        self.view.show_tdwr = on
        self.view.update()

    def _toggle_rings(self, on):
        self.settings["map_layers"]["range_rings"] = on
        self.view.show_range_rings = on
        self.view.update()

    def set_velocity_filter(self, level):
        self.settings["velocity_filter"] = level
        self.settings.save()
        for a, lv in zip(self.vf_group.actions(), (0, 1, 2)):
            a.setChecked(lv == level)
        self._panel_req.clear()
        self._show_frame()
        self._prefetch()

    def _toggle_link(self, on):
        self.settings["cursor_link"] = on
        self.view.link_cursor = on

    def _toggle_layer(self, name, on):
        self.settings["map_layers"][name] = on
        self.view.map_visible[name] = on
        self.view.update()
