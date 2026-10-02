"""My location and the warning alert for it."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from ...data import feeds
from ...data.sites import get_site, nearest_site
from ...overlays.warnings import EVENT_GROUP
from ...products.geometry import aeqd_forward


class LocationsMixin:
    """My location and the warning alert for it."""

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
        s = get_site(self.data.site_id)
        if s is not None and self.settings["go_to_nearest_radar"]:
            near = nearest_site(*ll)
            if near is not None and near.id != s.id:
                d = math.hypot(*aeqd_forward(ll[0], ll[1], s.lat, s.lon))
                if d > 230:
                    self.switch_site(near.id)
        x, y = aeqd_forward(ll[0], ll[1], self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), max(self.view.scale, 3.0))

    def _toggle_warn_loc(self, on):
        self.settings["warn_at_location"] = on
        self.settings.save()
        if on:
            self._check_location_alerts()

    def _check_location_alerts(self):
        """Pops up a new tornado / severe / flash flood warning that covers my location (live data):
        once per warning, and again only when it's upgraded (e.g. to PDS or an emergency)."""
        if not self.settings["warn_at_location"] or self.data.mode != "live":
            return
        ll = self.my_location.latlon()
        if ll is None:
            return
        lat, lon = ll
        now = datetime.now(timezone.utc)
        hits = []
        for a in list(self.warnings.alerts):
            if EVENT_GROUP.get(a.event) not in ("TOR", "SVR", "FFW") or a.action in ("CAN", "EXP"):
                continue
            if a.expires is not None and a.expires < now:
                continue
            if any(r[:, 1].min() <= lat <= r[:, 1].max() and r[:, 0].min() <= lon <= r[:, 0].max() for r in a.rings) \
                    and feeds.rings_contain(a.rings, lat, lon):
                hits.append(a)
        fresh = [a for a in hits if a.key not in self._notified or a.style[3] > self._notified[a.key][0]]
        if not fresh:
            return
        t_now = time.time()
        self._notified = {k: v for k, v in self._notified.items() if v[1] > t_now - 3600}
        for a in hits:
            old = self._notified.get(a.key, [0, 0])[0]
            self._notified[a.key] = [max(a.style[3], old), a.expires.timestamp() if a.expires else t_now + 7200]
        self.settings["notified_warnings"] = self._notified
        self.settings.save()
        top = max(fresh, key=lambda a: a.style[3])
        QApplication.alert(self, 0)
        QApplication.beep()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Warning for your location")
        box.setText(f"<b>{top.variant_label} ({top.variant})</b><br>covers your location.")
        box.setInformativeText(top.hover)
        go = box.addButton("Show on map", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.setModal(False)
        go.clicked.connect(lambda: self._go_to_alert(top))
        box.show()
        self._status_msg(f"⚠ {top.variant_label} covers your location")

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
