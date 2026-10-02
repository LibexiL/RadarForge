"""Bookmarks, shared views and named workspaces: saving what you are looking at, and coming back to it."""
from __future__ import annotations

import os
from datetime import timedelta

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from ... import __version__
from ...data import aws
from ...data.sites import get_site
from ...products import catalog
from ...products.geometry import aeqd_forward
from ...services import views
from ..dialogs.bookmarks import BookmarkDialog, BookmarksDialog

PRODUCT_IDS = {p.id for p in catalog.PRODUCTS}
ARCHIVE_BEFORE, ARCHIVE_AFTER = timedelta(minutes=20), timedelta(minutes=25)


class ViewsMixin:
    """Bookmarks, shared views and named workspaces."""

    # ---------------------------------------------------------------- capturing and applying a view
    def capture_view(self) -> dict:
        """Everything about what is on screen that a bookmark or a shared file keeps."""
        frame = self.current_frame()
        live = self.data.mode == "live" and (frame is None or self.follow_latest)
        lat, lon = self._map_centre()
        width = self.view.panels[0].rect.width() or 600.0
        return views.clean_view({
            "site": self.data.site_id,
            "time": None if (live or frame is None) else frame.time.isoformat(),
            "layout": int(self.settings["layout"]),
            "panels": [p.product for p in self.view.panels],
            "tilt": self.tilt_elev,
            "view": {"lat": lat, "lon": lon, "km_across": max(1.0, width / self.view.scale)},
            "overlays": {k: bool(self.settings["overlays"].get(k)) for k in views.OVERLAY_KEYS},
            "satellite": {"channel": self.settings["satellite_channel"]},
            "storm_motion": [self.settings["storm_motion_dir"], self.settings["storm_motion_kts"]],
            "side": [k for k in self.SIDE_PANELS if self.ws.is_open(k)],
        }, PRODUCT_IDS)

    def apply_view(self, v: dict):
        """Goes to a view: radar, time (loads the archive for it), panels, layers and zoom."""
        v = views.clean_view(v, PRODUCT_IDS)
        self.apply_layout_part(v)
        if "storm_motion" in v:
            self.settings["storm_motion_dir"], self.settings["storm_motion_kts"] = v["storm_motion"]
            self._update_sm_label()
        if "satellite" in v:
            self.set_satellite_channel(v["satellite"]["channel"])
        if "tilt" in v:
            self.tilt_elev = v["tilt"]
        site = v.get("site") or self.data.site_id
        when = views.parse_time(v.get("time"))
        if when is None:
            if site != self.data.site_id:
                self.switch_site(site)
            elif self.data.mode != "live":
                self.start_live()
            self._apply_zoom(v)
        else:
            self._status_msg(f"Loading {site} for {when:%Y-%m-%d %H:%M}Z…")
            self.run_bg(lambda: aws.list_level2_range(site, when - ARCHIVE_BEFORE, when + ARCHIVE_AFTER),
                        lambda files: self._archive_listed(site, files, when, v))
        self.stateChanged.emit()

    def _archive_listed(self, site, files, when, v):
        if not files:
            QMessageBox.information(self, "Open view", f"There is no archived Level II data for {site} around "
                                    f"{when:%Y-%m-%d %H:%M}Z.")
            return
        self.load_archive_files(site, files, l3=True)
        self._apply_zoom(v)
        self.goto_time_when_loaded(when)

    def _apply_zoom(self, v):
        z = v.get("view")
        if z is None or not self.view.panels:
            return
        x, y = aeqd_forward(z["lat"], z["lon"], self.view.lat0, self.view.lon0)
        self.view.set_view(float(x), float(y), self.view.panels[0].rect.width() / z["km_across"])

    def apply_layout_part(self, v: dict):
        """Panels, layers and open side panels from a view or workspace (the radar, time and zoom stay)."""
        panels = v.get("panels")
        if panels:
            old = list(self.settings["panels"])
            self.settings["panels"] = panels + old[len(panels):]
        if "layout" in v:
            self.set_layout(v["layout"])
        elif panels:
            self.set_layout(int(self.settings["layout"]))
        for key, on in (v.get("overlays") or {}).items():
            act = self.overlay_acts.get(key)
            if act is not None and act.isChecked() != on:
                act.setChecked(on)
        for key in v.get("side") or []:
            if key in self.SIDE_PANELS:
                self.show_panel(key)

    # ---------------------------------------------------------------- bookmarks
    def _bookmarks(self) -> list:
        return [b for b in (self.settings["bookmarks"] or []) if isinstance(b, dict)]

    def save_bookmark(self):
        view = self.capture_view()
        dlg = BookmarkDialog(self, views.default_bookmark_name(view), "", "Save bookmark")
        if dlg.exec():
            bm = views.new_bookmark(view, dlg.name.text(), dlg.notes.toPlainText().strip())
            self.settings["bookmarks"] = self._bookmarks() + [bm]
            self.settings.save()
            self._status_msg(f"Bookmark “{bm['name']}” saved (File → Bookmarks)")

    def open_bookmark(self, bm):
        try:
            self.apply_view(bm)
        except ValueError as exc:
            QMessageBox.warning(self, "Bookmark", str(exc))

    def manage_bookmarks(self):
        BookmarksDialog(self).exec()

    def _fill_bookmarks_menu(self):
        m = self.bookmarks_menu
        m.clear()
        m.addAction("Save a bookmark…", self.save_bookmark).setShortcut("Ctrl+B")
        m.addAction("Manage bookmarks…", self.manage_bookmarks)
        items = self._bookmarks()
        if items:
            m.addSeparator()
        for bm in items:
            a = m.addAction(bm.get("name") or "Bookmark")
            a.setToolTip(bm.get("notes", ""))
            a.triggered.connect(lambda _=False, b=bm: self.open_bookmark(b))

    # ---------------------------------------------------------------- sharing a view
    def share_view_to_file(self):
        v = self.capture_view()
        name = views.default_bookmark_name(v).replace(" ", "_").replace(":", "") + views.EXTENSION
        path, _ = QFileDialog.getSaveFileName(self, "Save this view", os.path.join(os.path.expanduser("~"), name),
                                              f"RadarForge view (*{views.EXTENSION})")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(views.to_file_text(v, __version__))
            self._status_msg(f"Saved {path} – send it to anyone: they can open it (or drop it on RadarForge)")

    def copy_view_text(self):
        QApplication.clipboard().setText(views.encode(self.capture_view()))
        self._status_msg("View copied as text – paste it into a chat; in RadarForge use File → Open view from the clipboard")

    def open_view_file(self, path=None):
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(self, "Open a shared view", os.path.expanduser("~"),
                                                  f"RadarForge view (*{views.EXTENSION});;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                self.apply_view(views.decode(fh.read()))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open view", f"Couldn't open {os.path.basename(path)}:\n{exc}")

    def open_view_from_clipboard(self):
        try:
            self.apply_view(views.decode(QApplication.clipboard().text()))
        except ValueError as exc:
            QMessageBox.information(self, "Open view", f"The clipboard doesn't hold a RadarForge view.\n({exc})")

    # ---------------------------------------------------------------- workspaces
    def workspaces(self) -> dict:
        """Built-in workspaces first, then the user's own (which can replace a built-in one by name)."""
        out = {k: dict(v) for k, v in views.BUILTIN_WORKSPACES.items()}
        for k, v in (self.settings["named_workspaces"] or {}).items():
            if isinstance(v, dict):
                out[k] = dict(v)
        return out

    def apply_workspace(self, name):
        ws = self.workspaces().get(name)
        if ws is None:
            return
        self.apply_layout_part(views.clean_workspace(ws, PRODUCT_IDS))
        if ws.get("dock"):
            self.ws.restore(ws["dock"])
        self._status_msg(f"Workspace “{name}”: {ws.get('note', '')}"[:160])

    def save_workspace(self):
        name, ok = QInputDialog.getText(self, "Save workspace", "Name for this layout of panels, layers and "
                                        "side panels:")
        name = name.strip()[:40]
        if not ok or not name:
            return
        v = self.capture_view()
        ws = views.clean_workspace(v, PRODUCT_IDS)
        ws["note"] = "Your own workspace"
        ws["dock"] = self.ws.state()
        saved = dict(self.settings["named_workspaces"] or {})
        saved[name] = ws
        self.settings["named_workspaces"] = saved
        self.settings.save()
        self._status_msg(f"Workspace “{name}” saved (View → Workspaces)")

    def delete_workspace(self, name):
        saved = dict(self.settings["named_workspaces"] or {})
        if saved.pop(name, None) is not None:
            self.settings["named_workspaces"] = saved
            self.settings.save()

    def _fill_workspaces_menu(self):
        m = self.workspace_menu
        m.clear()
        saved = self.settings["named_workspaces"] or {}
        for name, ws in self.workspaces().items():
            a = m.addAction(name)
            a.setToolTip(ws.get("note", ""))
            a.triggered.connect(lambda _=False, n=name: self.apply_workspace(n))
        m.addSeparator()
        m.addAction("Save the current layout as a workspace…", self.save_workspace)
        if saved:
            rm = m.addMenu("Delete a workspace")
            for name in saved:
                rm.addAction(name, lambda n=name: self.delete_workspace(n))

    def toggle_briefing(self):
        self.apply_workspace("Briefing")

    def site_label(self):
        s = get_site(self.data.site_id)
        return f"{self.data.site_id} {s.place}" if s else self.data.site_id
