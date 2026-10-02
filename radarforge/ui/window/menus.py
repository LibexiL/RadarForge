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
        self.overlay_acts = {}
        self._menu_file(mb.addMenu("&File"))
        self._menu_view(mb.addMenu("&View"))
        self._menu_radar(mb.addMenu("&Radar"))
        self._menu_layers(mb.addMenu("&Layers"))
        self._menu_locations(mb.addMenu("L&ocations"))
        self._menu_tools(mb.addMenu("&Tools"))
        self.panels_menu = mb.addMenu("&Panels")          # filled in by _build_panels
        self._menu_help(mb.addMenu("&Help"))

    def _overlay_act(self, menu, key, label):
        """A checkable Layers entry that switches the overlay `key` on and off."""
        self.overlay_acts[key] = self._act(menu, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                           None, checkable=True,
                                           checked=bool(self.settings["overlays"].get(key, False)))
        return self.overlay_acts[key]

    # ---------------------------------------------------------------- File: data in, pictures out
    def _menu_file(self, m):
        self._act(m, "Open radar files…", self.open_files, "Ctrl+O")
        self._act(m, "Open archive from AWS…", self.open_archive, "Ctrl+A")
        m.addSeparator()
        self.bookmarks_menu = m.addMenu("Bookmarks")
        self.bookmarks_menu.aboutToShow.connect(self._fill_bookmarks_menu)
        self._fill_bookmarks_menu()                         # so Ctrl+B works before the menu is opened
        share = m.addMenu("Share this view")
        self._act(share, "Save as a view file (.rfview)…", self.share_view_to_file, None)
        self._act(share, "Copy as text (to paste in a chat)", self.copy_view_text, None)
        self._act(m, "Open a shared view…", self.open_view_file, None)
        self._act(m, "Open a view from the clipboard", self.open_view_from_clipboard, None)
        m.addSeparator()
        ex = m.addMenu("Export")
        self._act(ex, "Image (PNG)…", self.save_image, "Ctrl+S")
        self._act(ex, "Image with title bar…", self.save_image_titled, "Ctrl+Shift+S")
        self._act(ex, "Briefing image (with a summary of what's in view)…", self.save_briefing_image, None)
        ex.addSeparator()
        self._act(ex, "Loop as animated GIF…", lambda: self.export_loop("gif"), None)
        self._act(ex, "Loop as MP4 video…", lambda: self.export_loop("mp4"), None)
        ex.addSeparator()
        self._act(ex, "Copy image to the clipboard", self.copy_image, "Ctrl+Shift+C")
        m.addSeparator()
        self._act(m, "Settings…", self.open_settings, "Ctrl+,")
        m.addSeparator()
        self._act(m, "Quit", self.close, "Ctrl+Q")

    # ---------------------------------------------------------------- View: how things look
    def _menu_view(self, m):
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
        self.workspace_menu = m.addMenu("Workspaces")
        self.workspace_menu.aboutToShow.connect(self._fill_workspaces_menu)
        self._act(m, "Briefing view", self.toggle_briefing, "F10")
        self.theme_menu = m.addMenu("Theme")
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        m.addSeparator()
        self._act(m, "Reset view", lambda: self.view.set_view(0, 0, max(0.3, self.view.panels[0].rect.width() / 500)),
                  "Home")
        self._act(m, "Full screen", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(),
                  "F11")

    # ---------------------------------------------------------------- Radar: which radar, which time
    def _menu_radar(self, m):
        self._act(m, "Choose radar…", self.choose_site, "Ctrl+R")
        m.addAction(self.live_act)
        m.addAction(self.archive_act)
        self.fav_menu = m.addMenu("Favourite radars")
        self.fav_menu.aboutToShow.connect(self._fill_fav_menu)
        self._act(m, "Add / remove this radar as a favourite", self.toggle_favorite, "Ctrl+D")
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

    # ---------------------------------------------------------------- Layers: what is drawn on the map
    def _menu_layers(self, m):
        m.addSection("Warnings and reports")
        for key, label in (("warnings", "NWS warnings"), ("watches", "Watches (live)"),
                           ("reports", "Local storm reports")):
            self._overlay_act(m, key, label)
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

        m.addSection("Storm Prediction Center")
        self._overlay_act(m, "spc_outlook", "Convective outlook")
        self.spc_day_group = self._radio_menu(m, "Outlook day", [("Day 1 (today)", 1), ("Day 2 (tomorrow)", 2), ("Day 3", 3)],
                                              self.settings["spc_day"] or 1, self.set_spc_day)
        self._overlay_act(m, "spc_mcd", "Mesoscale discussions")

        m.addSection("Level III")
        for key, label in (("storm_tracks", "Storm tracks (NST)"), ("meso", "Mesocyclones (NMD)"),
                           ("tvs", "TVS (NTV)"), ("hail", "Hail index (NHI)"), ("melting_layer", "Melting layer (N0M)")):
            self._overlay_act(m, key, label)

        m.addSection("Satellite and lightning (GOES)")
        self._menu_sky(m)

        m.addSection("Observations")
        self._overlay_act(m, "surface", "Surface observations (METAR station plots)")

        m.addSection("MRMS: radar-derived tracks and totals")
        self._menu_mrms(m)

        m.addSection("Storm chasers")
        self._overlay_act(m, "chasers", "Storm chasers (Spotter Network)")
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

        m.addSection("Base map")
        self.cities_act = self._act(m, "City labels", self._toggle_cities, None, checkable=True,
                                    checked=bool(self.settings["map_layers"].get("cities", True)))
        self.sites_act = self._act(m, "Radar sites", self._toggle_sites, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("radar_sites", True)))
        self.tdwr_act = self._act(m, "Include TDWR sites", self._toggle_tdwr, None, checkable=True,
                                  checked=bool(self.settings["map_layers"].get("tdwr_sites", False)))
        self.rings_act = self._act(m, "Range rings", self._toggle_rings, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("range_rings", False)))
        maps = m.addMenu("Map lines")
        self.layer_acts = {}
        from ...render.maps import LAYER_STYLE
        for name, (label, *_rest) in LAYER_STYLE.items():
            on = bool(self.settings["map_layers"].get(name, True))
            self.view.map_visible[name] = on
            self.layer_acts[name] = self._act(maps, label, lambda checked, n=name: self._toggle_layer(n, checked),
                                              None, checkable=True, checked=on)

        m.addSection("Placefiles")
        self._act(m, "Placefile manager", self.open_placefiles, "Ctrl+P")

    def _radio_menu(self, menu, title, choices, current, handler):
        """A submenu of exclusive choices [(label, value)]; returns its action group."""
        sub = menu.addMenu(title)
        group = QActionGroup(self)
        for label, value in choices:
            a = QAction(label, self, checkable=True)
            a.setData(value)
            a.setChecked(value == current)
            a.triggered.connect(lambda _=False, v=value: handler(v))
            group.addAction(a)
            sub.addAction(a)
        return group

    def _menu_sky(self, m):
        from ...data import goes
        s = self.settings
        self._overlay_act(m, "satellite", "Satellite picture")
        self.sat_channel_group = self._radio_menu(
            m, "Satellite channel", [(v["label"], k) for k, v in goes.CHANNELS.items()], s["satellite_channel"],
            self.set_satellite_channel)
        self.sat_source_group = self._radio_menu(
            m, "Satellite", [("Automatic (by location)", "auto"), ("GOES-East", "east"), ("GOES-West", "west")],
            s["satellite_sat"], self.set_satellite_source)
        self.sat_opacity_group = self._radio_menu(
            m, "Satellite opacity", [(f"{int(v * 100)}%", v) for v in (1.0, 0.8, 0.6, 0.4, 0.2)],
            min((1.0, 0.8, 0.6, 0.4, 0.2), key=lambda v: abs(v - float(s["satellite_opacity"] or 0.8))),
            self.set_satellite_opacity)
        self._overlay_act(m, "lightning", "Lightning flashes (GLM)")
        self.lightning_window_group = self._radio_menu(
            m, "Lightning window", [(f"Last {v} minutes", v) for v in (5, 10, 15, 30)], int(s["lightning_minutes"] or 10),
            self.set_lightning_minutes)

    def _menu_mrms(self, m):
        from ...data import mrms
        self._overlay_act(m, "mrms", "MRMS layer")
        self.mrms_product_group = self._radio_menu(
            m, "MRMS product", [(v["label"], k) for k, v in mrms.PRODUCTS.items()], self.settings["mrms_product"],
            self.set_mrms_product)
        win = m.addMenu("MRMS window")

        def fill():
            win.clear()
            product = self.mrms.product()
            group = QActionGroup(win)
            for w in mrms.PRODUCTS[product]["windows"]:
                a = QAction(f"Last {mrms.window_label(product, w)}", win, checkable=True)
                a.setChecked(w == self.mrms.window())
                a.triggered.connect(lambda _=False, w=w: self.set_mrms_window(w))
                group.addAction(a)
                win.addAction(a)
        win.aboutToShow.connect(fill)

    # ---------------------------------------------------------------- Locations: where I am and what to tell me
    def _menu_locations(self, m):
        self._act(m, "Go to my location", self.go_to_my_location, "Ctrl+L")
        self.saved_menu = m.addMenu("Go to a saved location")
        self.saved_menu.aboutToShow.connect(self._fill_locations_menu)
        m.addSeparator()
        self._act(m, "Add a location…", lambda: self.add_location_dialog(), "Ctrl+Shift+L")
        self._act(m, "Set my location…", self.set_my_location_dialog, None)
        self._act(m, "Locations and alerts…", self.manage_locations, None)
        self._act(m, "Locations panel", lambda: self.show_panel("locations"), None)
        m.addSeparator()
        self.warn_loc_act = self._act(m, "Alert me about my locations (live data)", self._toggle_warn_loc, None,
                                      checkable=True, checked=bool(self.settings["warn_at_location"]))
        test = m.addMenu("Test an alert")
        for kind, label in (("tornado", "Tornado warning"), ("severe", "Severe thunderstorm warning"),
                            ("info", "Lightning / other")):
            self._act(test, label, lambda k=kind: self.test_alert(k), None)

    # ---------------------------------------------------------------- Tools: things you use on a storm
    def _menu_tools(self, m):
        for a in self.tool_group.actions():
            m.addAction(a)
        m.addSeparator()
        self._act(m, "Command palette…", self.open_palette, "Ctrl+K")
        m.addSeparator()
        self.follow_act = self._act(m, "Follow a storm (keeps it centred)", self.toggle_follow, None, checkable=True)
        self._overlay_act(m, "signatures", "Flag signatures automatically (rotation, debris, ZDR columns)")
        self._act(m, "Sounding (model or balloon)…", self.open_sounding, None)
        self._act(m, "Storm cell table", lambda: self.show_panel("cells"), None)
        self._act(m, "Level III storm table (text)", self.show_storm_table, None)

    # ---------------------------------------------------------------- Help
    def _menu_help(self, m):
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
