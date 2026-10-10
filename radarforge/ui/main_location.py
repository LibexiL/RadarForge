"""Favourite radars, my location and warning alerts for it."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ..data.sites import get_site, nearest_site
from ..features import alerts
from ..features.warnings import EVENT_GROUP, report_on
from ..products.geometry import aeqd_forward


class LocationMixin:
    """Part of MainWindow: favourite radars, my location and warning alerts for it."""

    def _favorites(self):
        return [s for s in (self.settings["favorite_sites"] or []) if get_site(s)]

    def _fill_fav_menu(self):
        m = self.fav_menu
        m.clear()
        favs = self._favorites()
        if not favs:
            a = m.addAction("No favourites yet – Ctrl+D adds the current radar")
            a.setEnabled(False)
        for sid in favs:
            s = get_site(sid)
            a = m.addAction(f"{sid}  {s.place}, {s.state}")
            a.setCheckable(True)
            a.setChecked(sid == self.data.site_id)
            a.triggered.connect(lambda _=False, sid=sid: self.switch_site(sid))

    def toggle_favorite(self, sid=None):
        sid = sid if isinstance(sid, str) else self.data.site_id
        favs = list(self.settings["favorite_sites"] or [])
        if sid in favs:
            favs.remove(sid)
            self._status_msg(f"{sid} removed from favourites")
        else:
            favs.append(sid)
            self._status_msg(f"{sid} added to favourites (Radar → Favourite radars)")
        self.settings["favorite_sites"] = favs
        self.settings.save()

    def set_my_location(self, lat, lon):
        self.settings["my_location"] = None if lat is None else [round(float(lat), 5), round(float(lon), 5)]
        self.settings.save()
        self.view.update()
        self._track_changed()
        if lat is not None:
            self._status_msg("My location set – Ctrl+L goes back to it")
            self._check_location_alerts()

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

    def go_to_my_location(self):
        ll = self.my_location.latlon()
        if ll is None:
            self.set_my_location_dialog()
            ll = self.my_location.latlon()
            if ll is None:
                return
        self.go_to_latlon(*ll)

    # ------------------------------------------------------------------ place search
    def _build_search(self):
        from ..features.places import LocalPlaces, SearchMarker
        from .place_search import PlaceSearch
        self.search_marker = SearchMarker()
        ovs = list(self.view.overlays)
        at = ovs.index(self.captions) if getattr(self, "captions", None) in ovs else len(ovs)
        ovs.insert(at, self.search_marker)
        self.view.overlays = ovs
        self.place_search = PlaceSearch(LocalPlaces(self.view.maps.raw),
                                        lambda: self.view.world_to_latlon(self.view.cx, self.view.cy), self)
        self.place_search.placeChosen.connect(self.go_to_place)
        self.place_search.cleared.connect(self.clear_search_marker)
        self.corner.layout().insertWidget(0, self.place_search)

    def focus_search(self):
        ps = getattr(self, "place_search", None)
        if ps is not None:
            ps.setFocus(Qt.ShortcutFocusReason)
            ps.selectAll()

    def go_to_place(self, p):
        """Zoom to a place from the search box (switching radar if it's far away) and mark it."""
        from .panels import _zoom_to
        self.go_to_latlon(p["lat"], p["lon"])
        v = self.view
        if p.get("bbox"):
            s, n, w, e = p["bbox"]
            xs, ys = aeqd_forward(np.array([s, s, n, n]), np.array([w, e, w, e]), v.lat0, v.lon0)
        else:
            half = 10.0 if p["source"] == "coords" else 8 + 10 * math.log10(max(p.get("pop") or 0, 1000) / 1000)
            x, y = aeqd_forward(p["lat"], p["lon"], v.lat0, v.lon0)
            xs, ys = np.array([x - half, x + half]), np.array([y - half, y + half])
        _zoom_to(self, xs, ys, pad=1.4)
        self.search_marker.set(p)
        v.update()
        src = " – search results © OpenStreetMap contributors" if p["source"] == "osm" else ""
        self._status_msg(f"{p['label']} ({p['kind']}){src} · Esc clears the marker")
        if v.host is not None:
            v.host.setFocus(Qt.OtherFocusReason)

    def clear_search_marker(self):
        m = getattr(self, "search_marker", None)
        if m is not None and m.place is not None:
            m.clear()
            self.view.update()

    def go_to_latlon(self, lat, lon, zoom=3.0):
        """Centre the map on a point (switching to its nearest radar if it's far from this one)."""
        s = get_site(self.data.site_id)
        if s is not None and self.settings["go_to_nearest_radar"]:
            near = nearest_site(lat, lon)
            if near is not None and near.id != s.id:
                d = math.hypot(*aeqd_forward(lat, lon, s.lat, s.lon))
                if d > 230:
                    self.switch_site(near.id)
        x, y = aeqd_forward(lat, lon, self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), max(self.view.scale, zoom))

    def _mine_entry(self):
        return next((loc for loc in (self.settings["saved_locations"] or []) if loc.get("mine")), None)

    def _toggle_warn_loc(self, on):
        self.settings["warn_at_location"] = on
        mine = self._mine_entry()
        if mine is not None:
            mine["enabled"] = bool(on)
        self.settings.save()
        if on:
            self._check_location_alerts()

    def open_locations(self, select_id=None):
        from .locations_dialog import LocationsDialog
        d = LocationsDialog(self, select_id)
        if d.exec():
            mine = self._mine_entry()
            if mine is not None and hasattr(self, "warn_loc_act"):
                self.warn_loc_act.blockSignals(True)
                self.warn_loc_act.setChecked(bool(mine.get("enabled", True)))
                self.warn_loc_act.blockSignals(False)
            self.view.update()
            self.lightning.refresh()
            self._check_location_alerts()

    def save_location_here(self, lat, lon):
        name, ok = QInputDialog.getText(self, "Save this location", "Name for this place:",
                                        text=f"Place {len(self.settings['saved_locations'] or [])}")
        if not ok:
            return
        loc = alerts.new_location(name.strip() or "Saved place", lat, lon)
        self.settings["saved_locations"] = list(self.settings["saved_locations"] or []) + [loc]
        self.settings.save()
        self.view.update()
        self._status_msg(f"Saved \"{loc['name']}\" – set its alerts in Location → Saved locations & alerts")
        self.open_locations(loc["id"])

    def _lightning_needed(self):
        return any(loc.get("enabled", True) and loc.get("lightning")
                   for loc in (self.settings["saved_locations"] or []))

    def _check_location_alerts(self):
        """Alerts for saved locations (live data): warnings / watches covering them, storm reports and lightning
        nearby. Each warning alerts once, and again only when it's upgraded (e.g. to PDS or an emergency)."""
        if self.data.mode != "live":
            return
        locs = [alerts.normalise(loc) for loc in (self.settings["saved_locations"] or [])]
        if not locs:
            return
        now = datetime.now(timezone.utc)
        ltg = self.lightning if any(loc.get("lightning") for loc in locs) else None
        events = alerts.evaluate(locs, self.my_location.latlon(), list(self.warnings.alerts),
                                 list(self.warnings.reports), ltg.flashes_near if ltg is not None else None, now,
                                 self._notified, EVENT_GROUP.get, lambda r: report_on(self.settings, r))
        if not events:
            return
        t_now = time.time()
        self._notified = {k: v for k, v in self._notified.items() if v[1] > t_now - 3600}
        self.settings["notified_warnings"] = self._notified
        self.settings.save()
        shown = []
        for i, ev in enumerate(events[:4]):
            shown.append(self.notifier.notify(ev, sound=i == 0))       # one sound for a burst of alerts
            self._status_msg(f"⚠ {ev['text']}")
        top = events[0]
        loc = top["loc"]
        if loc.get("popup", True) or (loc.get("desktop", True) and not shown[0]):    # no desktop notifications here
            self._alert_popup(top)

    def _alert_popup(self, ev):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("RadarForge alert")
        box.setText(f"<b>{ev['title']}</b><br>{ev['text']}")
        if ev.get("detail"):
            box.setInformativeText(ev["detail"])
        go = box.addButton("Show on map", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.setModal(False)
        go.clicked.connect(lambda: self._go_to_event(ev))
        box.show()

    def _go_to_event(self, ev):
        t = ev.get("target")
        if isinstance(t, tuple):
            self.go_to_latlon(*t)
        elif t is not None:
            self._go_to_alert(t)

    def _go_to_alert(self, a):
        self.show_panel("warnings")
        p = self.warnings_panel
        p.refresh()
        it = next((p.tree.topLevelItem(i) for i in range(p.tree.topLevelItemCount())
                   if p.tree.topLevelItem(i).data(0, Qt.UserRole) == a.uid), None)
        if it is not None:
            p._zoom(it)
        else:
            self.go_to_my_location()
