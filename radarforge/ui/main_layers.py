"""Map layer and overlay switches, storm report and chaser options."""
from __future__ import annotations




class LayersMixin:
    """Part of MainWindow: map layer and overlay switches, storm report and chaser options."""

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

    def _toggle_overlay(self, key, on):
        self.settings["overlays"][key] = on
        self.settings.save()
        if key in ("warnings", "watches", "reports") and on:
            self.warnings.refresh(force=True)
        if key == "chasers" and on:
            self.chasers.refresh(force=True)
        if key in ("spc_outlook", "spc_mcd") and on:
            self.spc.refresh(force=True)
        if on and key in ("chasers", "spc_outlook", "spc_mcd", "satellite", "surface_obs") and self.data.mode != "live":
            self._status_msg("Storm chasers, SPC products, satellite and surface observations are shown with live data")
        self._data_overlay_toggled(key, on)
        if key in ("warnings", "watches") and hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_filters()
        self._update_l3_needs()
        self.view.update()

    def set_report_hours(self, h):
        self.settings["report_hours"] = h
        self.settings.save()
        for a in self.rep_hours_group.actions():
            a.setChecked(a.text().startswith(f"{h} "))
        self.warnings.refresh(force=True)
        if hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_report_options()

    def set_report_type(self, group, on):
        types = dict(self.settings["report_types"] or {})
        types[group] = on
        self.settings["report_types"] = types
        self.settings.save()
        act = self.rep_type_acts.get(group)
        if act is not None and act.isChecked() != on:
            act.blockSignals(True)
            act.setChecked(on)
            act.blockSignals(False)
        self.view.update()
        if hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_report_options()

    def _toggle_sn_reports(self, on):
        self.settings["spotter_reports"] = on
        self.settings.save()
        self.warnings.refresh(force=True)

    def _set_chasers_active(self, active):
        self.settings["chasers_active_only"] = active
        self.settings.save()
        self.chasers.refresh(force=True)

    def _toggle_chaser_names(self, on):
        self.settings["chaser_names"] = on
        self.settings.save()
        self.view.update()
