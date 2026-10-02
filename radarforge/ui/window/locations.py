"""Saved locations: going to them, editing them, and telling the user when something happens at one of them."""
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QSystemTrayIcon

from ...config import CACHE_DIR
from ...data.sites import get_site, nearest_site
from ...overlays.locations import LocationsOverlay
from ...products.geometry import aeqd_forward
from ...services import alerts as alert_engine
from ...services.locations import Location, LocationBook, normalize_rules
from ..dialogs.locations import LocationEditDialog, LocationsDialog


class LocationsMixin:
    """Saved locations: going to them, editing them, and telling the user when something happens at one."""

    def _init_locations(self):
        """Called once the warning, SPC and lightning layers exist."""
        self.book = LocationBook(self.settings)
        self.my_location = LocationsOverlay(self.book)       # the layer that draws them (and knows "my location")
        self.location_status: dict = {}                      # location id -> [(priority, kind, text)]
        self._notified = dict(self.settings["notified_warnings"] or {})
        self._sound = None
        self._tray = None
        self._boxes: list = []
        for sig in (self.warnings.changed, self.spc.changed, self.lightning.changed):
            sig.connect(self._check_location_alerts)
        self._alert_timer = QTimer(self)                      # also lets lightning cooldowns and expiries pass
        self._alert_timer.timeout.connect(self._check_location_alerts)
        self._alert_timer.start(60_000)

    # ---------------------------------------------------------------- my location / saved locations
    def set_my_location(self, lat, lon):
        """The right-click 'Set my location here' / 'Remove my location': changes the first saved location."""
        self.book.set_primary_position(lat, lon)
        self.locations_changed()
        if lat is not None:
            self._status_msg("My location set – Ctrl+L goes back to it")

    def locations_changed(self):
        """Something about the saved locations changed: redraw, re-check alerts, refresh the panel."""
        self._sync_alert_needs()
        self.view.update()
        self._track_changed()
        self._check_location_alerts()
        self.locationsUpdated.emit()

    def _map_centre(self):
        return self.view.world_to_latlon(self.view.cx, self.view.cy)

    def set_my_location_dialog(self):
        cur = self.my_location.latlon()
        text, ok = QInputDialog.getText(self, "My location", "Latitude, longitude (e.g. 35.22, -97.44).\n"
                                        "You can also right-click the map and choose \"Set my location here\".",
                                        text=f"{cur[0]:.4f}, {cur[1]:.4f}" if cur else "")
        if not ok:
            return
        try:
            lat, lon = (float(v) for v in text.replace(";", ",").split(",")[:2])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "My location", "Please type the latitude and longitude separated by a comma.")
            return
        self.set_my_location(lat, lon)

    def add_location_dialog(self, lat=None, lon=None, parent=None):
        """Add a saved location (at a map position, or the centre of the map)."""
        if lat is None:
            lat, lon = self._map_centre()
        loc = Location(uuid.uuid4().hex[:8], self.book.unique_name("Location"), round(lat, 4), round(lon, 4),
                       normalize_rules(None))
        dlg = LocationEditDialog(parent or self, loc, self._map_centre(), "Add location")
        if dlg.exec():
            self.book.items.append(loc)
            self.book.save()
            self.locations_changed()
            return loc
        return None

    def edit_location(self, loc, parent=None):
        dlg = LocationEditDialog(parent or self, loc, self._map_centre(), f"Location – {loc.name}")
        if dlg.exec():
            self.book.save()
            self.locations_changed()

    def manage_locations(self):
        LocationsDialog(self).exec()
        self.locations_changed()

    def go_to_location(self, loc):
        s = get_site(self.data.site_id)
        if s is not None and self.settings["go_to_nearest_radar"]:
            near = nearest_site(loc.lat, loc.lon)
            if near is not None and near.id != s.id:
                import math
                if math.hypot(*aeqd_forward(loc.lat, loc.lon, s.lat, s.lon)) > 230:
                    self.switch_site(near.id)
        x, y = aeqd_forward(loc.lat, loc.lon, self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), max(self.view.scale, 3.0))

    def go_to_my_location(self):
        loc = self.book.primary()
        if loc is None:
            self.set_my_location_dialog()
            loc = self.book.primary()
            if loc is None:
                return
        self.go_to_location(loc)

    def _fill_locations_menu(self):
        m = self.saved_menu
        m.clear()
        if not self.book.items:
            m.addAction("No saved locations yet").setEnabled(False)
        for i, loc in enumerate(self.book.items):
            a = m.addAction(f"{'★ ' if i == 0 else ''}{loc.name}")
            a.triggered.connect(lambda _=False, l=loc: self.go_to_location(l))

    # ---------------------------------------------------------------- alerts
    def _toggle_warn_loc(self, on):
        self.settings["warn_at_location"] = on
        self.settings.save()
        self._sync_alert_needs()
        if on:
            self._check_location_alerts()

    def _sync_alert_needs(self):
        """Tell the feeds what the alert rules need, so they're fetched even when their map layer is off."""
        rules = [loc.rules for loc in self.book.items] if self.settings["warn_at_location"] else []
        need = {"warn": any(r[k]["on"] for r in rules for k in ("tornado", "severe", "flood")) or
                        any(r["watch"]["on"] for r in rules),
                "mcd": any(r["mcd"]["on"] for r in rules), "outlook": any(r["outlook"]["min"] != "off" for r in rules),
                "lightning": any(r["lightning"]["miles"] for r in rules)}
        changed = (self.warnings.force_alerts != need["warn"] or self.spc.force["mcd"] != need["mcd"] or
                   self.spc.force["outlook"] != need["outlook"] or self.lightning.force != need["lightning"])
        self.warnings.force_alerts = need["warn"]
        self.spc.force = {"mcd": need["mcd"], "outlook": need["outlook"]}
        self.lightning.force = need["lightning"]
        if changed and self.data.mode == "live":
            self.warnings.refresh(force=True)
            self.spc.refresh(force=True)
            if need["lightning"]:
                self.update_timed_layers(prefetch=True)

    def _lightning_counter(self):
        return self.lightning.counts_near if (self.lightning.enabled() or self.lightning.force) else None

    def _update_location_status(self, now):
        live = self.data.mode == "live"
        if live:
            alerts = list(self.warnings.alerts)
        else:
            alerts = self.warnings.active_alerts()
            frame = self.current_frame()
            now = frame.time if frame is not None else now
        status = {}
        counter = self._lightning_counter()
        for loc in self.book.items:
            status[loc.id] = alert_engine.status_lines(loc, alerts, self.spc.mcds, self.spc.outlook, counter, now,
                                                       self.lightning.minutes())
        if status != self.location_status:
            self.location_status = status
            self.my_location.states = {
                lid: ("inside" if any(k == "inside" for _p, k, _t in lines) else
                      "near" if any(k == "near" for _p, k, _t in lines) else None)
                for lid, lines in status.items()}
            self.view.update()
            self.locationsUpdated.emit()

    def _check_location_alerts(self):
        """Works out what's new for each saved location: a window, a sound and a desktop notification (live data)."""
        now = datetime.now(timezone.utc)
        self._update_location_status(now)
        if not self.settings["warn_at_location"] or self.data.mode != "live" or not self.book.items:
            return
        events = alert_engine.evaluate(self.book.items, list(self.warnings.alerts), list(self.spc.mcds),
                                       list(self.spc.outlook), self._lightning_counter(), now, self.lightning.minutes())
        fresh = alert_engine.announce(events, self._notified, now)
        self.settings["notified_warnings"] = self._notified
        if fresh:
            self.settings.save()
            self._deliver(fresh)

    def _options(self):
        o = dict(self.settings["alert_options"] or {})
        return o.get("popup", True), o.get("sound", True), o.get("notify", True)

    def _deliver(self, events):
        """Show, sound and notify (as chosen in Locations → Locations and alerts…)."""
        popup, sound, notify = self._options()
        top = events[0]
        QApplication.alert(self, 0)
        self._status_msg("⚠ " + top.body)
        if sound:
            self.play_alert_sound(top.sound)
        else:
            QApplication.beep()
        if notify:
            for e in events[:3]:
                self._notify(e.title, e.body, e.priority >= 11)
        if popup:
            self._popup(events)

    def _popup(self, events):
        top = events[0]
        names = ", ".join(dict.fromkeys(e.location.name for e in events))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Alert – {names}")
        box.setText(f"<b>{top.title}</b>")
        box.setInformativeText("\n\n".join(e.body for e in events[:5]) +
                               (f"\n\n…and {len(events) - 5} more" if len(events) > 5 else ""))
        go = box.addButton("Show on map", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.setModal(False)
        go.clicked.connect(lambda: self._go_to_event(top))
        box.finished.connect(lambda _=0, b=box: self._boxes.remove(b) if b in self._boxes else None)
        self._boxes.append(box)
        box.show()

    def _go_to_event(self, e):
        ref = e.ref
        if ref is not None and hasattr(ref, "uid"):                     # a warning or watch
            self._go_to_alert(ref)
        elif isinstance(ref, dict) and ref.get("rings"):                # a mesoscale discussion
            from ..panels.common import _zoom_to
            import numpy as np
            pts = np.array([p for r in ref["rings"] for p in r])
            x, y = aeqd_forward(pts[:, 1], pts[:, 0], self.view.lat0, self.view.lon0)
            _zoom_to(self, x, y)
        else:
            self.go_to_location(e.location)

    def _go_to_alert(self, a):
        from PySide6.QtCore import Qt
        self.show_panel("warnings")
        p = self.warnings_panel
        p.refresh()
        it = next((p.tree.topLevelItem(i) for i in range(p.tree.topLevelItemCount())
                   if p.tree.topLevelItem(i).data(0, Qt.UserRole) == a.uid), None)
        if it is not None:
            p._zoom(it)
        else:
            self.go_to_my_location()

    # ---------------------------------------------------------------- sound and notifications
    def play_alert_sound(self, kind="info"):
        """The alert tone for tornado / severe / other alerts (the files are made once and kept)."""
        path = CACHE_DIR / "sounds" / f"alert_{kind}.wav"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(alert_engine.tone_wav(kind))
            from PySide6.QtMultimedia import QSoundEffect
            if self._sound is None:
                self._sound = QSoundEffect(self)
                self._sound.setVolume(0.9)
            self._sound.setSource(QUrl.fromLocalFile(str(path)))
            self._sound.play()
        except Exception as exc:
            print("alert sound:", exc, file=sys.stderr)
            QApplication.beep()

    def _notify(self, title, body, urgent=False):
        """A desktop notification: notify-send on Linux, the system tray's balloon elsewhere."""
        try:
            if sys.platform.startswith("linux") and shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "-a", "RadarForge", "-u", "critical" if urgent else "normal",
                                  "-i", "radarforge", title, body], stderr=subprocess.DEVNULL)
                return
            if QSystemTrayIcon.isSystemTrayAvailable():
                if self._tray is None:
                    self._tray = QSystemTrayIcon(QIcon(QApplication.windowIcon()), self)
                self._tray.show()
                self._tray.showMessage(title, body, QSystemTrayIcon.Warning if urgent else QSystemTrayIcon.Information,
                                       12000)
                QTimer.singleShot(14000, self._tray.hide)
        except Exception as exc:
            print("notification:", exc, file=sys.stderr)

    def test_alert(self, kind="tornado"):
        """Previews an alert exactly as it will look and sound (without recording it as announced)."""
        loc = self.book.primary() or Location("test", "Test location", *self._map_centre(), normalize_rules(None))
        title, body, prio = {
            "tornado": ("Tornado - PDS – " + loc.name, f"Tornado - PDS (TORP) covers {loc.name} · this is a test", 13),
            "severe": ("Severe Thunderstorm – " + loc.name, f"Severe Thunderstorm (SVR) covers {loc.name} · this is a test", 8),
            "info": ("Lightning near " + loc.name, f"3 lightning flashes within 10 mi of {loc.name} · this is a test", 4)}[kind]
        kind_name = "warning" if kind != "info" else "lightning"
        e = alert_engine.AlertEvent("test", kind_name, float(prio), title, body, loc, 0.0)
        popup, sound, notify = self._options()
        if sound:
            self.play_alert_sound(e.sound)
        if notify:
            self._notify(e.title, e.body, prio >= 11)
        if popup:
            self._popup([e])
