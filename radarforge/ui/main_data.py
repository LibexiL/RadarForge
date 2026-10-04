"""Weather data layers (satellite, lightning, MRMS, surface obs), storm flags, storm following,
model soundings and learn mode."""
from __future__ import annotations

import math
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup

from ..features import alerts, mrms, satellite
from ..features import stormtools as st
from ..features.cameras import CamerasOverlay
from ..features.captions import LayerCaptions
from ..features.lightning import LightningOverlay
from ..features.mrms import MrmsOverlay
from ..features.obs import SurfaceObsOverlay
from ..features.satellite import SatelliteOverlay
from .notify import Notifier
from .storm_tools_ui import GuideDialog, RotationDialog, StormFlagsOverlay, flags_for_frame

LIVE_ONLY = ("satellite", "surface_obs", "chasers", "spc_outlook", "spc_mcd")


class _DataRelay(QObject):
    flags = Signal(object)        # (generation, flags)
    follow = Signal(object)       # (generation, frame time, x, y | None)


class DataLayersMixin:
    """Part of MainWindow: weather data layers, storm flags and following, soundings, learn mode."""

    # ---------------------------------------------------------------- set-up
    def _init_data_layers(self):
        s = self.settings
        is_live = lambda: self.data.mode == "live"          # noqa: E731
        self.satellite = SatelliteOverlay(s, self.view, is_live, self)
        self.mrms = MrmsOverlay(s, self.view, self._layer_time, self)
        self.ltg_density = MrmsOverlay(s, self.view, self._layer_time, self, key_setting="ltg_density_product",
                                       overlay_key="lightning_density")
        self.lightning = LightningOverlay(s, self.view, self._layer_time, self._lightning_needed, self)
        self.obs = SurfaceObsOverlay(s, self.view, is_live, self)
        self.storm_flags = StormFlagsOverlay(s)
        self.cameras = CamerasOverlay(s, self.view, self)
        self.captions = LayerCaptions([self.satellite, self.mrms, self.ltg_density, self.lightning,
                                       self.storm_flags, self.cameras])
        self.notifier = Notifier(s, self)
        self.notifier.clicked.connect(self._go_to_event)
        if alerts.migrate(s):
            s.save()
        self._data_relay = _DataRelay()
        self._data_relay.flags.connect(self._flags_ready)
        self._data_relay.follow.connect(self._follow_ready)
        self._flags_gen = 0
        self._follow = None                    # {"x", "y", "t", "dx", "dy"} km, km/min
        self._follow_gen = 0
        self.view.overlays = [self.mrms, self.ltg_density, self.spc, self.warnings, self.placefiles, self.l3ov,
                              self.obs, self.cameras, self.lightning, self.chasers, self.storm_flags,
                              self.my_location, self.captions]
        self.view.underlays = [self.satellite, self.placefiles]
        self.view.hover_providers = [self.l3ov, self.storm_flags, self.chasers, self.cameras, self.placefiles,
                                     self.warnings, self.my_location, self.obs, self.lightning, self.spc, self.mrms,
                                     self.ltg_density]
        self.view.mapClicked.connect(self._map_clicked)
        self._cam_timer = QTimer(self)
        self._cam_timer.setSingleShot(True)
        self._cam_timer.setInterval(1500)
        self._cam_timer.timeout.connect(lambda: self.cameras.refresh())
        self.view.viewChanged.connect(lambda: self._cam_timer.start() if self.cameras.enabled() else None)
        for ov in (self.satellite, self.mrms, self.ltg_density, self.lightning, self.obs, self.cameras):
            ov.changed.connect(self.view.update)
            ov.status.connect(self._status_msg)
        self.lightning.changed.connect(self._check_location_alerts)
        self._alert_timer = QTimer(self)
        self._alert_timer.timeout.connect(self._check_location_alerts)
        self._alert_timer.start(60_000)

    def _layer_time(self):
        """Time the archive-capable layers should show: None (= now) with live data, else the frame's."""
        if self.data.mode == "live":
            return None
        f = self.current_frame()
        return getattr(f, "time", None)

    def _refresh_data_layers(self, force=False):
        for ov in (self.satellite, self.mrms, self.ltg_density, self.lightning, self.obs):
            ov.refresh(force=force)

    def _layers_frame_changed(self, frame):
        """Called by _show_frame."""
        if self.data.mode != "live":
            for ov in (self.mrms, self.ltg_density, self.lightning):
                ov.refresh()
        self._update_storm_flags(frame)
        self._follow_frame(frame)

    def _data_overlay_toggled(self, key, on):
        """Extra work when one of the 1.9 layers is switched (called by _toggle_overlay)."""
        if key == "storm_flags":
            self.storm_flags.flags = []
            if on:
                self._update_storm_flags(self.current_frame(), force=True)
        elif key in ("satellite", "mrms", "lightning", "lightning_density", "surface_obs") and on:
            self._refresh_data_layers(force=True)
        elif key == "cameras" and on:
            self.cameras.refresh(force=True)
            if not self.cameras.all_cameras():
                self._status_msg("Street cameras: California works out of the box; other states need a free key – "
                                 "Layers → Street cameras → Camera sources")

    # ---------------------------------------------------------------- menus
    def _radio(self, menu, items, current, fn):
        grp = QActionGroup(self)
        acts = []
        for value, label in items:
            a = QAction(label, self, checkable=True)
            a.setChecked(value == current)
            a.triggered.connect(lambda _=False, v=value: fn(v))
            grp.addAction(a)
            menu.addAction(a)
            acts.append(a)
        return acts

    def _menu_data_layers(self, m):
        s = self.settings
        sat = m.addMenu("Satellite")
        self._overlay_act(sat, "satellite", "GOES satellite under the radar (live)")
        sat.addSeparator()
        self._radio(sat, [(k, v[1]) for k, v in satellite.CHANNELS.items()], s["satellite_channel"],
                    self.set_satellite_channel)
        sat.addSeparator()
        self._act(sat, "Colour-enhanced infrared / water vapour", self._toggle_sat_enhance, None, checkable=True,
                  checked=bool(s["satellite_enhance"]))
        op = sat.addMenu("Opacity")
        self._radio(op, [(v, f"{round(v * 100)}%") for v in (0.4, 0.6, 0.85, 1.0)],
                    round(float(s["satellite_opacity"] or 0.85), 2), lambda v: self._set_opacity("satellite", v))
        ltg = m.addMenu("Lightning")
        self._overlay_act(ltg, "lightning", "Lightning flashes (GOES GLM)")
        self._overlay_act(ltg, "lightning_density", "Lightning density map (NLDN, via MRMS)")
        ltg.addSeparator()
        win = ltg.addMenu("Flashes: show the last")
        self._radio(win, [(v, f"{v} minutes") for v in (5, 10, 15, 30)], int(s["lightning_minutes"] or 10),
                    self.set_lightning_minutes)
        mr = m.addMenu("MRMS swaths")
        self._overlay_act(mr, "mrms", "Show MRMS swath")
        mr.addSeparator()
        kinds = (("rot", "Rotation tracks"), ("mesh", "Hail size (MESH)"), ("qpe", "Rainfall"))
        cur = s["mrms_product"]
        grp = QActionGroup(self)
        for kind, title in kinds:
            sub = mr.addMenu(title)
            for key, (label, _p, k, _mins, _cad) in mrms.PRODUCTS.items():
                if k != kind:
                    continue
                a = QAction(label.split(" – ")[-1], self, checkable=True)
                a.setChecked(key == cur)
                a.triggered.connect(lambda _=False, key=key: self.set_mrms_product(key))
                grp.addAction(a)
                sub.addAction(a)
        op = mr.addMenu("Opacity")
        self._radio(op, [(v, f"{round(v * 100)}%") for v in (0.5, 0.65, 0.8, 1.0)],
                    round(float(s["mrms_opacity"] or 0.8), 2), lambda v: self._set_opacity("mrms", v))
        cam = m.addMenu("Street cameras")
        self._overlay_act(cam, "cameras", "Show street cameras (when zoomed in)")
        self._act(cam, "Camera sources && keys…", self.open_camera_sources, None)
        self._act(cam, "Refresh camera list", lambda: self.cameras.refresh(force=True), None)
        ob = m.addMenu("Surface observations")
        self._overlay_act(ob, "surface_obs", "Station plots: temperature, dew point, wind (live)")
        self._act(ob, "Refresh now", lambda: self.obs.refresh(force=True), None)

    def _menu_spc_days(self, spc):
        spc.addSeparator()
        self.spc_day_acts = self._radio(spc, [(1, "Outlook: day 1 (today)"), (2, "Outlook: day 2 (tomorrow)"),
                                              (3, "Outlook: day 3")], self.spc.day(), self.set_spc_day)

    def _menu_storm_tools(self, m):
        m.addSeparator()
        self._overlay_act(m, "storm_flags", "Automatic storm flags (rotation, debris, hail)")
        self.follow_act = self._act(m, "Follow storm", self._toggle_follow, None, checkable=True, checked=False)
        self.follow_act.setToolTip("Right-click a storm → Follow this storm. The map stays on it as frames change.")
        self._act(m, "Rotation history at the map centre…", lambda: self.open_rotation_history(), None)
        self._act(m, "Model sounding at the map centre…", lambda: self.open_sounding(), None)

    def _menu_help_extras(self, m):
        self._act(m, "Radar && dual-pol guide", lambda: GuideDialog(self).show(), None)
        self.learn_act = self._act(m, "Learn mode (explain values in the Inspector)", self._toggle_learn, None,
                                   checkable=True, checked=bool(self.settings["learn_mode"]))
        m.addSeparator()

    def _map_menu_extras(self, menu, lat, lon, x, y):
        a = menu.addAction("Model sounding here…")
        a.triggered.connect(lambda: self.open_sounding(lat, lon))
        if self.data.frames:
            a = menu.addAction("Rotation history for this storm…")
            a.triggered.connect(lambda: self.open_rotation_history(x, y))
            if self._follow is None:
                a = menu.addAction("Follow this storm")
                a.triggered.connect(lambda: self.start_follow(x, y))
        if self._follow is not None:
            a = menu.addAction("Stop following the storm")
            a.triggered.connect(self.stop_follow)
        a = menu.addAction("Save this location…")
        a.triggered.connect(lambda: self.save_location_here(lat, lon))

    # ---------------------------------------------------------------- settings changes
    def set_satellite_channel(self, k):
        self.settings["satellite_channel"] = k
        self.settings.save()
        self.satellite.refresh(force=True)

    def _toggle_sat_enhance(self, on):
        self.settings["satellite_enhance"] = on
        self.settings.save()
        self.satellite.refresh(force=True)

    def _set_opacity(self, which, v):
        self.settings[f"{which}_opacity"] = v
        self.settings.save()
        self.view.update()

    def set_lightning_minutes(self, v):
        self.settings["lightning_minutes"] = v
        self.settings.save()
        self.lightning.refresh(force=True)
        self.view.update()

    def set_mrms_product(self, key):
        self.settings["mrms_product"] = key
        self.settings.save()
        if not self.settings["overlays"].get("mrms"):
            self.overlay_acts["mrms"].setChecked(True)        # picking a product shows it
        self.mrms.refresh(force=True)

    def set_spc_day(self, day):
        self.spc.set_day(day)
        for a, d in zip(getattr(self, "spc_day_acts", []), (1, 2, 3)):
            a.setChecked(d == day)
        if not self.settings["overlays"].get("spc_outlook"):
            self.overlay_acts["spc_outlook"].setChecked(True)

    def _toggle_learn(self, on):
        self.settings["learn_mode"] = on
        self.settings.save()
        if on:
            self.show_panel("inspector")
            self._status_msg("Learn mode: move the mouse over the radar – the Inspector explains what you see")

    # ---------------------------------------------------------------- storm flags
    def _update_storm_flags(self, frame, force=False):
        if not self.settings["overlays"].get("storm_flags") or frame is None or not frame.has_level2():
            if self.storm_flags.flags:
                self.storm_flags.flags = []
                self.view.update()
            return
        key = (frame.uid, getattr(frame, "l2_rev", 0), self.view.lat0, self.view.lon0)
        if key == self.storm_flags.key and not force:
            return
        self._flags_gen += 1
        gen = self._flags_gen
        engine = self.engine

        def work():
            try:
                flags = flags_for_frame(engine, frame)
            except Exception as exc:
                print("storm flags:", exc)
                flags = []
            self._data_relay.flags.emit((gen, key, flags))
        threading.Thread(target=work, daemon=True).start()

    def _flags_ready(self, res):
        gen, key, flags = res
        if gen != self._flags_gen:
            return
        self.storm_flags.flags = flags
        self.storm_flags.key = key
        self.view.update()

    # ---------------------------------------------------------------- storm following
    def start_follow(self, x, y):
        f = self.current_frame()
        if f is None:
            return
        dx, dy = st.motion_xy(float(self.settings["storm_motion_dir"]), float(self.settings["storm_motion_kts"]))
        self._follow = {"x": x, "y": y, "t": f.time, "dx": dx, "dy": dy}
        self.storm_flags.follow_xy = (x, y)
        self.follow_act.blockSignals(True)
        self.follow_act.setChecked(True)
        self.follow_act.blockSignals(False)
        self.view.set_view(x, y, self.view.scale)
        self._status_msg("Following the storm – the map recentres on it each frame (Tools → Follow storm to stop)")

    def stop_follow(self):
        self._follow = None
        self.storm_flags.follow_xy = None
        self.follow_act.blockSignals(True)
        self.follow_act.setChecked(False)
        self.follow_act.blockSignals(False)
        self.view.update()

    def _toggle_follow(self, on):
        if on:
            if self._follow is None:
                self.start_follow(self.view.cx, self.view.cy)
        else:
            self.stop_follow()

    def _follow_frame(self, frame):
        fo = self._follow
        if fo is None or frame is None or getattr(frame, "time", None) is None:
            return
        mins = (frame.time - fo["t"]).total_seconds() / 60.0
        px, py = fo["x"] + fo["dx"] * mins, fo["y"] + fo["dy"] * mins
        self._follow_gen += 1
        gen = self._follow_gen
        engine = self.engine

        def work():
            pos = None
            try:
                ref = engine.image(frame, "REF", 0) if frame.has_level2() else None
                if ref is not None:
                    pos = st.follow_step(ref, px, py)
            except Exception:
                pos = None
            self._data_relay.follow.emit((gen, frame.time, px, py, pos))
        threading.Thread(target=work, daemon=True).start()

    def _follow_ready(self, res):
        gen, t, px, py, pos = res
        fo = self._follow
        if fo is None or gen != self._follow_gen:
            return
        x, y = pos if pos is not None else (px, py)
        if pos is not None and math.hypot(x - px, y - py) > 15:      # jumped to another storm: trust the motion
            x, y = px, py
        mins = (t - fo["t"]).total_seconds() / 60.0
        if pos is not None and 2.0 <= abs(mins) <= 20.0:
            vx, vy = (x - fo["x"]) / mins, (y - fo["y"]) / mins
            if math.hypot(vx, vy) < 3.7:                               # under ~120 kt
                fo["dx"] = 0.6 * fo["dx"] + 0.4 * vx
                fo["dy"] = 0.6 * fo["dy"] + 0.4 * vy
        if abs(mins) <= 20.0:
            fo.update(x=x, y=y, t=t)
        self.storm_flags.follow_xy = (x, y)
        self.view.set_view(x, y, self.view.scale)

    # ---------------------------------------------------------------- cameras
    def _map_clicked(self, x, y):
        if self.view.tool != "pan" or not self.cameras.enabled():
            return
        group = self.cameras.near(x, y, 8.0 / self.view.scale)
        if group:
            from .camera_viewer import CameraViewer
            CameraViewer(self, group).show()

    def open_camera_sources(self):
        from .camera_sources import CameraSourcesDialog
        if CameraSourcesDialog(self).exec():
            self.cameras.cams.clear()
            self.cameras._xy = None
            self.cameras.invalidate()
            if not self.settings["overlays"].get("cameras"):
                self.overlay_acts["cameras"].setChecked(True)
            self.cameras.refresh(force=True)

    # ---------------------------------------------------------------- windows
    def open_sounding(self, lat=None, lon=None):
        from .sounding_dialog import SoundingDialog
        if lat is None:
            lat, lon = self.view.world_to_latlon(self.view.cx, self.view.cy)
        SoundingDialog(self, float(lat), float(lon)).show()

    def open_rotation_history(self, x=None, y=None):
        if not self.data.frames:
            self._status_msg("Load some radar frames first (live or archive)")
            return
        if x is None:
            fo = self._follow
            x, y = (fo["x"], fo["y"]) if fo else (self.view.cx, self.view.cy)
        RotationDialog(self, x, y).show()

    # ---------------------------------------------------------------- learn mode
    def _learn_notes(self, raw, beam_ft, active_pid):
        if not self.settings["learn_mode"]:
            return None
        notes = st.explain(raw, beam_ft)
        help_txt = st.PRODUCT_HELP.get(st.L3_ALIAS.get(active_pid, active_pid))
        out = []
        if help_txt:
            out.append(help_txt)
        out += [f"• {n}" for n in notes]
        return "\n".join(out) if out else None
