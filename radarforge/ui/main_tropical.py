"""Winter and tropical layers in the main window: freezing line, snowfall, precipitation type; hurricanes, replays
of past storms and historic tracks."""
from __future__ import annotations

import threading
from datetime import timedelta

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QActionGroup

from ..features import tropical as tr
from ..features.tropical import TropicalOverlay
from ..features.winter import FreezingLineOverlay, SnowfallOverlay
from ..products.geometry import aeqd_forward


class _BgRelay(QObject):
    done = Signal(object, object, object)       # callback, result, error


class WinterTropicalMixin:
    """Part of MainWindow."""

    def _init_winter_tropical(self):
        s = self.settings
        is_live = lambda: self.data.mode == "live"          # noqa: E731
        self.freezing = FreezingLineOverlay(s, self.view, self._layer_time, self)
        self.snowfall = SnowfallOverlay(s, self.view, self._layer_time, self)
        self.tropical = TropicalOverlay(s, self.view, is_live, self._layer_time, self)
        for ov in (self.freezing, self.snowfall, self.tropical):
            ov.changed.connect(self.view.update)
            ov.status.connect(self._status_msg)
        self._bg_relay = _BgRelay()
        self._bg_relay.done.connect(lambda cb, res, err: cb(res, err))

    def run_bg(self, fn, done):
        """fn() on a worker thread, then done(result, error) back on the UI thread."""
        relay = self._bg_relay

        def work():
            try:
                res, err = fn(), None
            except Exception as exc:
                from ..data.aws import friendly_error
                res, err = None, friendly_error(exc)
            relay.done.emit(done, res, err)
        threading.Thread(target=work, daemon=True).start()

    def set_warning_type(self, key, on):
        types = dict(self.settings["warning_types"] or {})
        types[key] = bool(on)
        self.settings["warning_types"] = types
        if on and not self.settings["overlays"].get("warnings", True):
            self.overlay_acts["warnings"].setChecked(True)
        self.settings.save()
        act = getattr(self, "wtype_acts", {}).get(key)
        if act is not None and act.isChecked() != bool(on):
            act.blockSignals(True)
            act.setChecked(bool(on))
            act.blockSignals(False)
        if hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_filters()
        if hasattr(self, "tropical_panel"):
            self.tropical_panel.sync()
        self.view.update()

    def go_to_latlon(self, lat, lon, radar=True):
        """Centre the map on a place; [radar]: switch to the nearest radar first when one covers it."""
        from ..data.sites import haversine_km, nearest_site
        if radar and getattr(self, "view_mode", "radar") == "radar":
            ns = nearest_site(lat, lon)
            if ns is not None and haversine_km(lat, lon, ns.lat, ns.lon) < 400 and ns.id != self.data.site_id:
                self.switch_site(ns.id)
        x, y = aeqd_forward(lat, lon, self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), min(self.view.scale, 1.2) if radar else 0.35)

    # ---------------------------------------------------------------- replays of past storms
    def replay_storm(self, ps):
        """Archive radar nearest a past storm's landfall, with its best track and the forecast of the time."""
        self.tropical.set_replay(ps)
        if ps is None:
            self.view.update()
            return
        t, lat, lon = tr.key_moment(ps)
        from ..data.sites import haversine_km, nearest_site
        site = nearest_site(lat, lon)
        if site is None or haversine_km(lat, lon, site.lat, site.lon) > 460:
            self._status_msg(f"{ps.name.title()} never came within radar range – showing its track")
            self.tropical_panel.track_only()
            return
        if getattr(self, "view_mode", "radar") == "models":
            self.set_view_mode("radar")
        n = max(10, int(self.settings["loop_frames"] or 10))
        start, end = t - timedelta(hours=2), t + timedelta(minutes=20)
        self.tropical_panel.past_status.setText(f"Finding {site.id} radar files around {t:%H:%MZ %d %b %Y}…")

        def work():
            from ..data import aws
            files = aws.list_level2_range(site.id, start, end)
            if len(files) > n:                       # spread the loop over the window
                step = len(files) / n
                files = [files[int(i * step)] for i in range(n - 1)] + [files[-1]]
            return files

        def done(files, err):
            if err is not None or not files:
                self.tropical_panel.past_status.setText(
                    f"⚠ No {site.id} radar files for {t:%d %b %Y}" + (f" ({err})" if err else "") +
                    " – showing the track only")
                self.tropical_panel.track_only()
                return
            self.tropical_panel.past_status.setText(f"Replaying {ps.name.title()}: {site.id}, {len(files)} scans to "
                                                    f"{t:%H:%MZ %d %b %Y}. Use ◀ ▶ or Space to step through.")
            self.start_archive(site.id, files, l3=False)
            x, y = aeqd_forward(lat, lon, self.view.lat0, self.view.lon0)
            self.view.set_view(float(x), float(y), max(self.view.scale, 0.8))
        self.run_bg(work, done)

    def start_archive(self, site, files, l3=True):
        """Load archive files for a radar (the Archive dialog's last step)."""
        from ..data.sites import get_site
        self.live_act.blockSignals(True)
        self.live_act.setChecked(False)
        self.live_act.blockSignals(False)
        if site != self.data.site_id and get_site(site):
            self.data.site_id = site
            self.settings["site"] = site
            self._set_site_projection(site)
        self.engine.clear()
        self.frame_index = -1
        self._shown_frame = None
        self.follow_latest = False
        self._update_l3_needs()
        self.data.load_archive(files, l3=l3)


# --------------------------------------------------------------------------- presets, and the winter / tropical menus
PRESETS = {
    "severe": dict(on=("warnings", "watches", "reports", "spc_outlook", "spc_mcd", "storm_tracks"),
                   off=("freezing_line", "snowfall", "tropical", "tropical_spaghetti", "tropical_outlook", "buoys"),
                   ptype=False, label="Severe", tip="Warnings, watches, storm reports, the SPC outlook and discussions, "
                                                     "storm tracks"),
    "winter": dict(on=("warnings", "watches", "reports", "spc_mcd", "freezing_line"),
                   off=("spc_outlook", "tropical", "tropical_spaghetti", "tropical_outlook", "buoys", "storm_tracks"),
                   ptype=True, label="Winter", tip="Precipitation type on the radar, the freezing line, winter "
                                                    "storm warnings and advisories, snow and ice reports"),
    "tropical": dict(on=("warnings", "watches", "reports", "tropical", "tropical_wind", "tropical_outlook",
                         "tropical_spaghetti", "buoys"),
                     off=("spc_outlook", "spc_mcd", "freezing_line", "snowfall"),
                     ptype=False, label="Tropical", tip="Hurricanes: cones, tracks, wind fields, model tracks, the "
                                                         "outlook, buoys, hurricane and storm surge warnings"),
    "clean": dict(on=("warnings",),
                  off=("watches", "reports", "spc_outlook", "spc_mcd", "chasers", "storm_tracks", "hail",
                       "melting_layer", "storm_flags", "satellite", "mrms", "lightning", "lightning_density",
                       "surface_obs", "cameras", "freezing_line", "snowfall", "tropical", "tropical_spaghetti",
                       "tropical_outlook", "buoys"),
                  ptype=False, label="Clean", tip="Just the radar, the map and warnings"),
}


class PresetsMixin:
    """Part of MainWindow: the Layers presets and the winter / tropical menu items."""

    def apply_preset(self, name):
        p = PRESETS.get(name)
        if p is None:
            return
        self._applying_preset = True
        try:
            for key in p["on"]:
                a = self.overlay_acts.get(key)
                if a is not None and not a.isChecked():
                    a.setChecked(True)
            for key in p["off"]:
                a = self.overlay_acts.get(key)
                if a is not None and a.isChecked():
                    a.setChecked(False)
            if name == "winter":
                self.set_warning_type("WIN", True)
                types = dict(self.settings["report_types"] or {})
                if not types.get("winter", True):
                    self.rep_type_acts["winter"].setChecked(True)
            if name == "tropical":
                self.set_warning_type("TRO", True)
            if self.ptype_act.isChecked() != p["ptype"]:
                self.ptype_act.setChecked(p["ptype"])
        finally:
            self._applying_preset = False
        self.settings["layer_preset"] = name
        self.settings.save()
        if name in ("severe", "winter", "tropical"):
            self.show_panel(name)
        self._sync_preset_buttons()
        self._status_msg(f"{p['label']} preset: {p['tip'].lower()}. Change any switch you like.")

    def _layer_switched(self, *_):
        """A layer changed by hand: no preset is current any more."""
        if getattr(self, "_applying_preset", False):
            return
        if self.settings["layer_preset"]:
            self.settings["layer_preset"] = ""
            self._sync_preset_buttons()

    def _sync_preset_buttons(self):
        cur = self.settings["layer_preset"] or ""
        for name, a in getattr(self, "preset_acts", {}).items():
            a.blockSignals(True)
            a.setChecked(name == cur)
            a.blockSignals(False)

    # ---------------------------------------------------------------- precipitation type on the radar
    def toggle_ptype(self, on):
        """Panel 1 shows precipitation-type reflectivity (what it showed before comes back when this is off)."""
        panels = list(self.settings["panels"])
        if on:
            if panels[0] != "PTYPE":
                self.settings["ptype_prev"] = panels[0]
                self.set_panel_product(0, "PTYPE")
                if getattr(self, "view_mode", "radar") == "radar":
                    self._status_msg("Panel 1: precipitation type – green rain, blue snow, orange sleet, pink "
                                     "freezing rain (HRRR analysis for each scan's hour)")
        elif panels[0] == "PTYPE":
            self.set_panel_product(0, self.settings["ptype_prev"] or "REF")
        self._layer_switched()

    def _sync_ptype_act(self):
        a = getattr(self, "ptype_act", None)
        if a is not None:
            on = (list(self.settings["panels"]) or ["REF"])[0] == "PTYPE"
            if a.isChecked() != on:
                a.blockSignals(True)
                a.setChecked(on)
                a.blockSignals(False)

    def toggle_winter_colours(self, on):
        from ..products.colortable import PALETTE_DIR
        path = str(PALETTE_DIR / "extra" / "REF_Winter.pal")
        ov = dict(self.settings["palette_overrides"] or {})
        if on:
            ov["REF"] = path
        elif ov.get("REF") == path:
            ov.pop("REF")
        self.settings["palette_overrides"] = ov
        self.settings.save()
        self._palettes.clear()
        self._show_frame()

    def set_snowfall_hours(self, h):
        self.settings["snowfall_hours"] = int(h)
        self.settings.save()
        self._sync_radio(self.snow_hours_acts, int(h))
        self.snowfall.refresh(force=True)

    # ---------------------------------------------------------------- menus
    def _menu_presets(self, m):
        self.preset_acts = {}
        grp = QActionGroup(self)
        grp.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        for name, p in PRESETS.items():
            a = QAction(p["label"], self, checkable=True)
            a.setToolTip(p["tip"])
            a.setStatusTip(p["tip"])
            a.setChecked(self.settings["layer_preset"] == name)
            a.triggered.connect(lambda _=False, n=name: self.apply_preset(n))
            grp.addAction(a)
            m.addAction(a)
            self.preset_acts[name] = a

    def _menu_winter(self, m):
        self.ptype_act = self._act(m, "Precipitation type on the radar (panel 1)", self.toggle_ptype, None,
                                   checkable=True, checked=(list(self.settings["panels"]) or ["REF"])[0] == "PTYPE",
                                   tip="Reflectivity coloured by rain, snow, sleet and freezing rain (HRRR)")
        self._overlay_act(m, "freezing_line", "Freezing line (32 °F)",
                          tip="The HRRR's 32 °F line at the surface for each scan's hour")
        self._overlay_act(m, "snowfall", "Snowfall totals (NOHRSC)",
                          tip="NOAA's gridded snowfall analysis: the last 24, 48 or 72 hours")
        hrs = m.addMenu("Snowfall: the last")
        self.snow_hours_acts = self._radio(hrs, [(24, "24 hours"), (48, "48 hours"), (72, "72 hours")],
                                           int(self.settings["snowfall_hours"] or 24), self.set_snowfall_hours)
        self.wtype_acts = getattr(self, "wtype_acts", {})
        self.wtype_acts["WIN"] = self._act(m, "Winter storm warnings && advisories",
                                           lambda on: self.set_warning_type("WIN", on), None, checkable=True,
                                           checked=bool((self.settings["warning_types"] or {}).get("WIN", True)),
                                           tip="Blizzard, winter storm, ice storm, lake effect, cold and frost alerts "
                                               "(shaded counties)")
        from ..products.colortable import PALETTE_DIR
        winter_pal = str(PALETTE_DIR / "extra" / "REF_Winter.pal")
        self.winter_pal_act = self._act(m, "Winter reflectivity colours", self.toggle_winter_colours, None,
                                        checkable=True,
                                        checked=(self.settings["palette_overrides"] or {}).get("REF") == winter_pal,
                                        tip="A reflectivity colour table that spreads light snow (5–25 dBZ) over "
                                            "more colours")

    def _menu_tropical(self, m):
        self._overlay_act(m, "tropical", "Hurricanes: storms, cones && tracks",
                          tip="Active storms from the National Hurricane Center, with the forecast cone and track")
        self._overlay_act(m, "tropical_wind", "Wind field (tropical-storm / hurricane force)")
        self._overlay_act(m, "tropical_spaghetti", "Model tracks (spaghetti)")
        self._overlay_act(m, "tropical_outlook", "Tropical outlook: areas to watch")
        self._overlay_act(m, "buoys", "Buoys && coastal stations")
        self.wtype_acts = getattr(self, "wtype_acts", {})
        self.wtype_acts["TRO"] = self._act(m, "Hurricane, tropical storm && storm surge warnings",
                                           lambda on: self.set_warning_type("TRO", on), None, checkable=True,
                                           checked=bool((self.settings["warning_types"] or {}).get("TRO", True)))
        m.addSeparator()
        self._act(m, "Tropical panel (storms, arrival times, past storms)…", lambda: self.show_panel("tropical"),
                  None, icon="target")
