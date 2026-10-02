"""Radar sites, live / archive / local data and dropped files."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtWidgets import QFileDialog

from ... import themes
from ...data.sites import get_site
from ...products import colortable
from ...products.geometry import aeqd_forward
from ..dialogs import ArchiveDialog, SiteDialog


class SourcesMixin:
    """Radar sites, live / archive / local data and dropped files."""

    def _set_site_projection(self, site_id):
        s = get_site(site_id)
        if s is None:
            return
        self.view.set_projection(s.lat, s.lon, s.id)
        self.site_btn.setText(f"{s.id}  {s.place}")
        self.site_btn.setToolTip(f"{s.id} – {s.place}, {s.state}\nClick to choose another radar (Ctrl+R), "
                                 f"or click a radar square on the map")
        self.warnings.center = (s.lat, s.lon)

    def choose_site(self):
        d = SiteDialog(self.data.site_id, self, self.settings)
        if d.exec() and d.selected():
            self.switch_site(d.selected())

    def switch_site(self, sid, keep_view=False):
        if sid == self.data.site_id and self.data.frames:
            return
        center = self.view.world_to_latlon(self.view.cx, self.view.cy) if keep_view else None
        self.engine.clear()
        self.frame_index = -1
        self._shown_frame = None
        self.follow_latest = True
        self.view.clear_lines()
        self.view.clear_track()
        self.view.set_box(None)
        self._set_site_projection(sid)
        if center is not None:
            # same map location stays under the view after re-centring the projection on the new radar
            x, y = aeqd_forward(center[0], center[1], self.view.lat0, self.view.lon0)
            self.view.set_view(float(x), float(y), self.view.scale)
        else:
            self.view.set_view(0, 0, self.view.scale)
        was = self.data.mode
        self.data.set_site(sid)
        self._update_l3_needs()
        if was in ("idle", "local"):
            self.start_live()           # nothing to reload for the new radar: show its live data
        s = get_site(sid)
        self._status_msg(f"Radar {sid} – {s.place}, {s.state}" + (" (loading live data…)" if self.data.mode == "live"
                                                                    else ""))

    def _toggle_live(self, on):
        if on:
            self.follow_latest = True
            self.warnings.set_live()
            self.data.start_live()
            self.chasers.refresh(force=True)
            self.spc.refresh(force=True)
        else:
            self.data.stop_live()

    def start_live(self):
        if self.live_act.isChecked():
            self.data.start_live()
        else:
            self.live_act.setChecked(True)

    def open_archive(self):
        d = ArchiveDialog(self.data.site_id, self)
        if not d.exec():
            return
        files = d.selected_files()
        if not files:
            return
        site = d.site.text().strip().upper()
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
        self.data.load_archive(files, l3=d.l3.isChecked())

    def open_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Open Level II / Level III files", os.path.expanduser("~"),
                                                "Radar files (*)")
        if paths:
            self._open_paths(paths)

    def _open_paths(self, paths):
        self.live_act.blockSignals(True)
        self.live_act.setChecked(False)
        self.live_act.blockSignals(False)
        self.engine.clear()
        self.frame_index = -1
        self._shown_frame = None
        self.follow_latest = False
        self.data.open_local(paths)

    def _drop_paths(self, ev):
        return [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]

    def _panel_under(self, ev):
        host = self.view.host
        if host is None:
            return -1
        pos = host.mapFrom(self, ev.position().toPoint())
        return self.view.panel_at(QPointF(pos))

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        self._drag_hover(ev, self._panel_under(ev))

    def dragLeaveEvent(self, ev):
        self.view.drop_panel = -1
        self.view.update()

    def dropEvent(self, ev):
        self._drop(ev, self._panel_under(ev))

    def _view_drag(self, kind, ev):
        """Files dragged over the map itself (the map is its own native window)."""
        if kind in (QEvent.DragEnter, QEvent.DragMove):
            if ev.mimeData().hasUrls():
                self._drag_hover(ev, self.view.panel_at(ev.position()))
        elif kind == QEvent.DragLeave:
            self.dragLeaveEvent(ev)
        elif kind == QEvent.Drop:
            self._drop(ev, self.view.panel_at(ev.position()))
            ev.acceptProposedAction()

    def _drag_hover(self, ev, panel):
        paths = self._drop_paths(ev)
        pals = [p for p in paths if colortable.looks_like_color_table(p)]
        target = panel if len(pals) == 1 else -1
        if self.view.drop_panel != target:
            self.view.drop_panel = target
            self.view.update()
        ev.acceptProposedAction()

    def _drop(self, ev, panel):
        self.view.drop_panel = -1
        paths = self._drop_paths(ev)
        theme_files = [p for p in paths if themes.looks_like_theme(p)]
        for p in theme_files:
            self.import_theme(p)
        paths = [p for p in paths if p not in theme_files]
        pals = [p for p in paths if colortable.looks_like_color_table(p)]
        radar = [p for p in paths if p not in pals]
        if pals:
            if len(pals) == 1 and panel >= 0:
                self.apply_color_table(pals[0], self.view.panels[panel].product)
            else:
                for p in pals:
                    self.apply_color_table(p, None)
        if radar:
            self._open_paths(radar)
        self.view.update()

    def _favorites(self):
        return [s for s in (self.settings["favorite_sites"] or []) if get_site(s)]

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
