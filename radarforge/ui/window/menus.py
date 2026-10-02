"""The menu bar."""
from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QMessageBox

from ... import __version__
from ... import themes
from ...data import feeds
from ...data.sites import get_site


class MenusMixin:
    """The menu bar."""

    def _act(self, menu, text, fn, shortcut=None, checkable=False, checked=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
            a.toggled.connect(fn)
        else:
            a.triggered.connect(fn)
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        # File
        m = mb.addMenu("&File")
        self._act(m, "Open radar files…", self.open_files, "Ctrl+O")
        self._act(m, "Open archive from AWS…", self.open_archive, "Ctrl+A")
        m.addSeparator()
        self._act(m, "Save image…", self.save_image, "Ctrl+S")
        self._act(m, "Copy image", self.copy_image, "Ctrl+Shift+C")
        m.addSeparator()
        self._act(m, "Settings…", self.open_settings, "Ctrl+,")
        m.addSeparator()
        self._act(m, "Quit", self.close, "Ctrl+Q")

        # View
        m = mb.addMenu("&View")
        lay = m.addMenu("Panel layout")
        for a in self.layout_group.actions():
            lay.addAction(a)
        m.addSeparator()
        self.smooth_act = self._act(m, "Smoothing", self._toggle_smooth, "S", checkable=True,
                                    checked=bool(self.settings["gpu_smooth"]))
        vf = m.addMenu("Velocity noise filter")
        self.vf_group = QActionGroup(self)
        for lvl, label in ((0, "Off (raw data)"), (1, "Normal"), (2, "Aggressive")):
            a = QAction(label, self, checkable=True)
            a.setChecked(int(self.settings["velocity_filter"]) == lvl)
            a.triggered.connect(lambda _=False, lv=lvl: self.set_velocity_filter(lv))
            self.vf_group.addAction(a)
            vf.addAction(a)
        self.legend_act = self._act(m, "Colour bars", self._toggle_legend, None, checkable=True,
                                    checked=bool(self.settings["show_legend"]))
        self.link_act = self._act(m, "Linked cursor", self._toggle_link, None, checkable=True,
                                  checked=bool(self.settings["cursor_link"]))
        m.addSeparator()
        self.theme_menu = m.addMenu("Theme")
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        m.addSeparator()
        self._act(m, "Reset view", lambda: self.view.set_view(0, 0, max(0.3, self.view.panels[0].rect.width() / 500)),
                  "Home")
        self._act(m, "Full screen", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(),
                  "F11")

        # Radar
        m = mb.addMenu("&Radar")
        self._act(m, "Choose radar…", self.choose_site, "Ctrl+R")
        m.addAction(self.live_act)
        m.addAction(self.archive_act)
        m.addSeparator()
        self._act(m, "Previous frame", lambda: self.step_frame(-1), None)
        self._act(m, "Next frame", lambda: self.step_frame(1), None)
        self._act(m, "Play / pause loop", self.toggle_play, None)
        self._act(m, "Latest frame", lambda: self.goto_frame(len(self.data.frames) - 1), None)
        m.addSeparator()
        self._act(m, "Tilt up", lambda: self.step_tilt(1), None)
        self._act(m, "Tilt down", lambda: self.step_tilt(-1), None)
        m.addSeparator()
        self._act(m, "Storm motion…", self.edit_storm_motion, None)
        m.addSeparator()
        self.fav_menu = m.addMenu("Favourite radars")
        self.fav_menu.aboutToShow.connect(self._fill_fav_menu)
        self._act(m, "Add / remove this radar as a favourite", self.toggle_favorite, "Ctrl+D")
        m.addSeparator()
        self._act(m, "Go to my location", self.go_to_my_location, "Ctrl+L")
        self._act(m, "Set my location…", self.set_my_location_dialog, None)
        self.warn_loc_act = self._act(m, "Alert me when a warning covers my location", self._toggle_warn_loc, None,
                                      checkable=True, checked=bool(self.settings["warn_at_location"]))

        # Map (overlays + map layers + placefiles)
        m = mb.addMenu("&Map")
        self.overlay_acts = {}
        m.addSection("Warnings")
        for key, label in (("warnings", "NWS warnings"), ("watches", "Watches (live)"),
                           ("reports", "Local storm reports")):
            self.overlay_acts[key] = self._act(m, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                               None, checkable=True,
                                               checked=bool(self.settings["overlays"].get(key, False)))
        rep = m.addMenu("Storm report options")
        hours = rep.addMenu("Show the last")
        self.rep_hours_group = QActionGroup(self)
        for h in (1, 3, 6, 12, 24):
            a = QAction(f"{h} hour" + ("s" if h > 1 else ""), self, checkable=True)
            a.setChecked(int(self.settings["report_hours"] or 3) == h)
            a.triggered.connect(lambda _=False, h=h: self.set_report_hours(h))
            self.rep_hours_group.addAction(a)
            hours.addAction(a)
        rep.addSeparator()
        self.rep_type_acts = {}
        for g, label in feeds.REPORT_GROUPS:
            on = bool((self.settings["report_types"] or {}).get(g, g != "other"))
            self.rep_type_acts[g] = self._act(rep, label + (" (rain, snow…)" if g == "other" else ""),
                                              lambda checked, g=g: self.set_report_type(g, checked), None,
                                              checkable=True, checked=on)
        rep.addSeparator()
        self.sn_rep_act = self._act(rep, "Include Spotter Network reports", self._toggle_sn_reports, None,
                                    checkable=True, checked=bool(self.settings["spotter_reports"]))
        self._act(m, "Refresh warnings now", lambda: self.warnings.refresh(force=True), None)
        m.addSection("Storm chasers")
        self.overlay_acts["chasers"] = self._act(m, "Storm chasers (Spotter Network)",
                                                 lambda checked: self._toggle_overlay("chasers", checked), None,
                                                 checkable=True, checked=bool(self.settings["overlays"].get("chasers")))
        ch = m.addMenu("Storm chaser options")
        grp = QActionGroup(self)
        for active, label in ((False, "Everyone"), (True, "Active reporters only (5+ reports in a year)")):
            a = QAction(label, self, checkable=True)
            a.setChecked(bool(self.settings["chasers_active_only"]) == active)
            a.triggered.connect(lambda _=False, v=active: self._set_chasers_active(v))
            grp.addAction(a)
            ch.addAction(a)
        ch.addSeparator()
        self._act(ch, "Show names (zoomed in)", self._toggle_chaser_names, None, checkable=True,
                  checked=bool(self.settings["chaser_names"]))
        m.addSection("Storm Prediction Center")
        for key, label in (("spc_outlook", "Day 1 convective outlook"), ("spc_mcd", "Mesoscale discussions")):
            self.overlay_acts[key] = self._act(m, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                               None, checkable=True,
                                               checked=bool(self.settings["overlays"].get(key, False)))
        m.addSection("Level III")
        for key, label in (("storm_tracks", "Storm tracks (NST)"), ("meso", "Mesocyclones (NMD)"),
                           ("tvs", "TVS (NTV)"), ("hail", "Hail index (NHI)"), ("melting_layer", "Melting layer (N0M)")):
            self.overlay_acts[key] = self._act(m, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                               None, checkable=True,
                                               checked=bool(self.settings["overlays"].get(key, False)))
        m.addSection("Map")
        self.cities_act = self._act(m, "City labels", self._toggle_cities, None, checkable=True,
                                    checked=bool(self.settings["map_layers"].get("cities", True)))
        self.sites_act = self._act(m, "Radar sites", self._toggle_sites, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("radar_sites", True)))
        self.tdwr_act = self._act(m, "Include TDWR sites", self._toggle_tdwr, None, checkable=True,
                                  checked=bool(self.settings["map_layers"].get("tdwr_sites", False)))
        self.rings_act = self._act(m, "Range rings", self._toggle_rings, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("range_rings", False)))
        maps = m.addMenu("Map layers")
        self.layer_acts = {}
        from ...render.maps import LAYER_STYLE
        for name, (label, *_rest) in LAYER_STYLE.items():
            on = bool(self.settings["map_layers"].get(name, True))
            self.view.map_visible[name] = on
            self.layer_acts[name] = self._act(maps, label, lambda checked, n=name: self._toggle_layer(n, checked),
                                              None, checkable=True, checked=on)
        m.addSection("Placefiles")
        self._act(m, "Placefile manager", self.open_placefiles, "Ctrl+P")

        # Tools
        m = mb.addMenu("&Tools")
        for a in self.tool_group.actions():
            m.addAction(a)
        m.addSeparator()
        self._act(m, "Storm cell table", lambda: self.show_panel("cells"), None)
        self._act(m, "Level III storm table (text)", self.show_storm_table, None)

        # Panels
        self.panels_menu = mb.addMenu("&Panels")

        # Help
        m = mb.addMenu("&Help")
        self._act(m, "Keyboard shortcuts", self.show_shortcuts, "F1")
        self._act(m, "About RadarForge", self.show_about, None)

    def _fill_theme_menu(self):
        m = self.theme_menu
        m.clear()
        grp = QActionGroup(m)
        cur = self.settings["theme"]
        for t in themes.all_themes():
            a = m.addAction(t["name"])
            a.setCheckable(True)
            a.setChecked(t["name"] == cur)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, n=t["name"]: self.apply_theme(n))
        m.addSeparator()
        m.addAction("Manage themes…", lambda: self.open_settings("Themes"))

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

    def show_shortcuts(self):
        rows = [
            ("Frames", [("← / →", "previous / next frame"), ("Space", "play / pause the loop"),
                        ("End", "latest frame")]),
            ("Tilts & layout", [("↑ / ↓", "tilt up / down"), ("Alt+1 … Alt+6", "1–6 panels")]),
            ("Mouse tools", [("P", "pan / zoom"), ("X", "cross section (drag a line)"), ("M", "measure"),
                             ("T", "storm track (click a storm, drag the arrowhead)"),
                             ("B", "3-D (drag a box around a storm)"), ("Esc", "back to pan; again: clear measure / track"),
                             ("Shift+drag", "quick measure")]),
            ("Map", [("wheel / drag", "zoom / pan"), ("double-click", "centre here"), ("Home", "reset view"),
                     ("right-click", "product, colour table, nearest radar"), ("S", "smoothing on/off")]),
            ("Window", [("F9", "show / hide the side panel"), ("F11", "full screen"), ("F1", "this list")]),
            ("Files & data", [("Ctrl+O", "open files"), ("Ctrl+A", "archive"), ("Ctrl+R", "choose radar"),
                              ("Ctrl+D", "add / remove favourite radar"), ("Ctrl+L", "go to my location"),
                              ("Ctrl+P", "placefiles"), ("Ctrl+S", "save image"), ("Ctrl+Shift+C", "copy image"),
                              ("Ctrl+,", "settings")]),
        ]
        html = "<table cellspacing='0' cellpadding='3'>"
        for title, items in rows:
            html += f"<tr><td colspan='2'><br><b>{title}</b></td></tr>"
            html += "".join(f"<tr><td style='padding-right:18px'><code>{k}</code></td><td>{v}</td></tr>"
                            for k, v in items)
        html += "</table><p>Drag a panel's tab to move it; drop zones show where it will go.</p>"
        QMessageBox.information(self, "Keyboard shortcuts", html)

    def show_about(self):
        QMessageBox.about(self, "About RadarForge", (
            f"<b>RadarForge {__version__}</b> – NEXRAD Level II / III viewer<br><br>"
            "Data: NOAA NEXRAD on AWS (Unidata buckets), NWS API alerts, Iowa Environmental Mesonet archives, "
            "storm reports and SPC products; storm chasers and spotter reports from Spotter Network "
            "(non-commercial use).<br>"
            "Maps: US Census county boundaries, Natural Earth, GeoNames.<br>"
            "Level III decoding by MetPy.<br><br>"
            f"OpenGL: {self.view.gl_info}"))
