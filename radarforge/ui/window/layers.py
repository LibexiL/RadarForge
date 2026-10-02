"""Map overlays: switches and options for warnings, reports, chasers, SPC, satellite and lightning."""
from __future__ import annotations

from datetime import datetime, timezone


class LayersMixin:
    """Map overlays: switches and options for warnings, reports, chasers, SPC, satellite and lightning."""

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
        if key in ("satellite", "mrms", "lightning") and on:
            self.update_timed_layers(prefetch=True)
        if on and key in ("chasers", "spc_outlook", "spc_mcd") and self.data.mode != "live":
            self._status_msg("Storm chasers and SPC products are shown with live data")
        if key in ("warnings", "watches") and hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_filters()
        self._update_l3_needs()
        self.view.update()

    # ---------------------------------------------------------------- satellite and lightning
    def update_timed_layers(self, prefetch=False):
        """Point the layers that follow the radar frame's time (satellite, MRMS, lightning) at the frame on screen,
        and at the whole loop when asked, so it plays smoothly."""
        frame = self.current_frame()
        t = frame.time if frame is not None else datetime.now(timezone.utc)
        lat0, lon0 = self.view.lat0, self.view.lon0
        layers = (self.satellite, self.mrms, self.lightning)
        for layer in layers:
            layer.set_target(t, lat0, lon0)
        if prefetch:
            times = [f.time for f in self.data.frames]
            for layer in layers:
                layer.prefetch(times, lat0, lon0)

    def _choose(self, key, value, group=None):
        self.settings[key] = value
        self.settings.save()
        if group is not None:
            for a in group.actions():
                a.setChecked(a.data() == value)

    def set_satellite_channel(self, channel):
        self._choose("satellite_channel", channel, getattr(self, "sat_channel_group", None))
        self.satellite.refresh()
        self.update_timed_layers(prefetch=True)

    def set_satellite_source(self, source):
        self._choose("satellite_sat", source, getattr(self, "sat_source_group", None))
        self.satellite.refresh(force=True)
        self.lightning.refresh(force=True)
        self.update_timed_layers(prefetch=True)

    def set_satellite_opacity(self, value):
        self._choose("satellite_opacity", value, getattr(self, "sat_opacity_group", None))
        self.view.update()

    def set_mrms_product(self, product):
        self._choose("mrms_product", product, getattr(self, "mrms_product_group", None))
        self.mrms.refresh()
        self.update_timed_layers(prefetch=True)

    def set_mrms_window(self, window):
        windows = dict(self.settings["mrms_window"] or {})
        windows[self.settings["mrms_product"]] = window
        self.settings["mrms_window"] = windows
        self.settings.save()
        self.mrms.refresh()
        self.update_timed_layers(prefetch=True)

    def set_lightning_minutes(self, minutes):
        self._choose("lightning_minutes", minutes, getattr(self, "lightning_window_group", None))
        self.update_timed_layers(prefetch=True)
