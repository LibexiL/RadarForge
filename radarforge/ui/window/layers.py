"""Map overlays: switches and options for warnings, reports, chasers and SPC."""
from __future__ import annotations




class LayersMixin:
    """Map overlays: switches and options for warnings, reports, chasers and SPC."""

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

    def _toggle_overlay(self, key, on):
        self.settings["overlays"][key] = on
        self.settings.save()
        if key in ("warnings", "watches", "reports") and on:
            self.warnings.refresh(force=True)
        if key == "chasers" and on:
            self.chasers.refresh(force=True)
        if key in ("spc_outlook", "spc_mcd") and on:
            self.spc.refresh(force=True)
        if on and key in ("chasers", "spc_outlook", "spc_mcd") and self.data.mode != "live":
            self._status_msg("Storm chasers and SPC products are shown with live data")
        if key in ("warnings", "watches") and hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_filters()
        self._update_l3_needs()
        self.view.update()
