"""Toolbar, timeline bar, menus, status bar and keyboard shortcuts."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QLabel, QMessageBox, QProgressBar, QSizePolicy, QSlider, QToolBar,
                               QToolButton)

from .. import __version__, themes
from ..features import feeds


class MenusMixin:
    """Part of MainWindow: toolbar, timeline bar, menus, status bar and keyboard shortcuts."""

    def _tb_button(self, tb, action, text_beside=True):
        tb.addAction(action)
        w = tb.widgetForAction(action)
        if isinstance(w, QToolButton):
            w.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text_beside else Qt.ToolButtonIconOnly)
        return w

    def _group_label(self, tb, text):
        lab = QLabel(text)
        lab.setProperty("role", "group")
        tb.addWidget(lab)

    def _build_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(tb)
        self.main_tb = tb
        self._icon_targets = []           # (action or button, icon name) re-tinted when the theme changes

        # radar + data source
        self.site_btn = QToolButton()
        self.site_btn.setText(self.settings["site"])
        self.site_btn.setToolTip("Choose radar (Ctrl+R) – or click a radar square on the map")
        self.site_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.site_btn.clicked.connect(self.choose_site)
        f = self.site_btn.font()
        f.setBold(True)
        self.site_btn.setFont(f)
        tb.addWidget(self.site_btn)
        self._icon_targets.append((self.site_btn, "radar"))
        tb.addSeparator()
        self.live_act = QAction("Live", self, checkable=True)
        self.live_act.setToolTip("Real-time data from AWS (Level II chunks + Level III)")
        self.live_act.toggled.connect(self._toggle_live)
        self._tb_button(tb, self.live_act)
        self._icon_targets.append((self.live_act, "live"))
        self.archive_act = QAction("Archive", self)
        self.archive_act.setToolTip("Load past data from the AWS archive (Ctrl+A)")
        self.archive_act.triggered.connect(self.open_archive)
        self._tb_button(tb, self.archive_act)
        self._icon_targets.append((self.archive_act, "archive"))
        self.open_act = QAction("Open", self)
        self.open_act.setToolTip("Open Level II / Level III files (Ctrl+O)")
        self.open_act.triggered.connect(self.open_files)
        self._tb_button(tb, self.open_act)
        self._icon_targets.append((self.open_act, "open"))
        tb.addSeparator()

        # layout
        self.layout_group = QActionGroup(self)
        for n in range(1, 7):
            act = QAction(str(n), self, checkable=True)
            act.setToolTip(f"{n}-panel layout (Alt+{n})")
            act.setData(n)
            act.setChecked(n == int(self.settings["layout"]))
            act.triggered.connect(lambda _=False, n=n: self.set_layout(n))
            self.layout_group.addAction(act)
            self._tb_button(tb, act, text_beside=False)
            self._icon_targets.append((act, f"layout{n}"))
        tb.addSeparator()

        # tilt
        down = QAction("Lower tilt", self)
        down.setToolTip("Lower tilt (Down)")
        down.triggered.connect(lambda: self.step_tilt(-1))
        self._tb_button(tb, down, text_beside=False)
        self._icon_targets.append((down, "down"))
        self.tilt_combo = QComboBox()
        self.tilt_combo.setMinimumContentsLength(7)
        self.tilt_combo.setToolTip("Elevation angle")
        self.tilt_combo.activated.connect(self._tilt_chosen)
        tb.addWidget(self.tilt_combo)
        up = QAction("Higher tilt", self)
        up.setToolTip("Higher tilt (Up)")
        up.triggered.connect(lambda: self.step_tilt(1))
        self._tb_button(tb, up, text_beside=False)
        self._icon_targets.append((up, "up"))
        tb.addSeparator()

        # mouse tools
        self.tool_group = QActionGroup(self)
        for tool, text, tip, ic in (("pan", "Pan", "Pan / zoom (P)", "pan"),
                                    ("xsection", "X-Section", "Drag a line to make a vertical cross section (X)",
                                     "xsection"),
                                    ("measure", "Measure", "Drag to measure distance / bearing (M)", "measure"),
                                    ("track", "Track", "Storm track: click a storm, then drag the yellow arrowhead to "
                                     "where it's going – shows when it reaches the towns ahead (T)", "track"),
                                    ("box3d", "3D", "Drag a box around a storm to see it in 3-D (B)", "box3d")):
            act = QAction(text, self, checkable=True)
            act.setToolTip(tip)
            act.setData(tool)
            act.setChecked(tool == "pan")
            act.triggered.connect(lambda _=False, t=tool: self.set_tool(t))
            self.tool_group.addAction(act)
            self._tb_button(tb, act)
            self._icon_targets.append((act, ic))
        tb.addSeparator()
        self.sm_act = QAction("", self)
        self.sm_act.triggered.connect(self.edit_storm_motion)
        self._tb_button(tb, self.sm_act)
        self._icon_targets.append((self.sm_act, "motion"))
        self._update_sm_label()

        # the side-panel switch lives in the menu bar's free right-hand corner, so it's never
        # pushed off a narrow window
        self.side_act = QAction("Side panel", self, checkable=True)
        self.side_act.setToolTip("Show / hide the side panel (F9)")
        self.side_act.setShortcut(QKeySequence("F9"))
        self.side_act.triggered.connect(self.toggle_side_panel)
        self.addAction(self.side_act)
        self.side_btn = QToolButton()
        self.side_btn.setDefaultAction(self.side_act)
        self.side_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.side_btn.setAutoRaise(True)
        self.side_btn.setIconSize(QSize(16, 16))
        self.menuBar().setCornerWidget(self.side_btn, Qt.TopRightCorner)
        self._icon_targets.append((self.side_act, "side"))

    def _build_timeline(self):
        """Frame / loop controls along the bottom, above the status bar."""
        tb = QToolBar("Timeline", self)
        tb.setObjectName("timeline_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(Qt.BottomToolBarArea, tb)
        self.timeline_tb = tb
        for name, tip, fn in (("first", "First frame", lambda: self.goto_frame(0)),
                              ("prev", "Previous frame (Left)", lambda: self.step_frame(-1))):
            act = QAction(tip, self)
            act.setToolTip(tip)
            act.triggered.connect(fn)
            self._tb_button(tb, act, text_beside=False)
            self._icon_targets.append((act, name))
        self.play_act = QAction("Play", self)
        self.play_act.setToolTip("Play / pause loop (Space)")
        self.play_act.triggered.connect(self.toggle_play)
        self._tb_button(tb, self.play_act, text_beside=False)
        self._icon_targets.append((self.play_act, "play"))
        for name, tip, fn in (("next", "Next frame (Right)", lambda: self.step_frame(1)),
                              ("last", "Latest frame (End)", lambda: self.goto_frame(len(self.data.frames) - 1))):
            act = QAction(tip, self)
            act.setToolTip(tip)
            act.triggered.connect(fn)
            self._tb_button(tb, act, text_beside=False)
            self._icon_targets.append((act, name))
        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setMinimumWidth(160)
        self.frame_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.frame_slider.setToolTip("Drag to scrub through the loaded frames")
        self.frame_slider.valueChanged.connect(self._slider_moved)
        tb.addWidget(self.frame_slider)
        self.time_label = QLabel(" --:--:--Z ")
        self.time_label.setMinimumWidth(230)
        self.time_label.setAlignment(Qt.AlignCenter)
        f = self.time_label.font()
        f.setBold(True)
        self.time_label.setFont(f)
        tb.addWidget(self.time_label)

    def _build_menus(self):
        """Menus, one home per topic: File (data in / pictures out), View, Radar, Layers (everything
        drawn on the map), Tools (mouse tools), Location, Panels, Help."""
        mb = self.menuBar()
        self.overlay_acts = {}
        self._menu_file(mb.addMenu("&File"))
        self._menu_view(mb.addMenu("&View"))
        self._menu_radar(mb.addMenu("&Radar"))
        self._menu_layers(mb.addMenu("&Layers"))
        self._menu_tools(mb.addMenu("&Tools"))
        self._menu_location(mb.addMenu("L&ocation"))
        self.panels_menu = mb.addMenu("&Panels")
        m = mb.addMenu("&Help")
        self._act(m, "Keyboard shortcuts", self.show_shortcuts, "F1")
        self._act(m, "About RadarForge", self.show_about, None)

    def _overlay_act(self, menu, key, label):
        self.overlay_acts[key] = self._act(menu, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                           None, checkable=True,
                                           checked=bool(self.settings["overlays"].get(key, False)))
        return self.overlay_acts[key]

    def _menu_file(self, m):
        self._act(m, "Open radar files…", self.open_files, "Ctrl+O")
        self._act(m, "Open archive from AWS…", self.open_archive, "Ctrl+A")
        m.addSeparator()
        ex = m.addMenu("Export")
        self._act(ex, "Save image…", self.save_image, "Ctrl+S")
        self._act(ex, "Save image with legend and details…", self.save_image_annotated, "Ctrl+Shift+S")
        self._act(ex, "Copy image", self.copy_image, "Ctrl+Shift+C")
        ex.addSeparator()
        self._act(ex, "Export loop (GIF / MP4)…", self.export_loop, "Ctrl+E")
        ex.addSeparator()
        self._act(ex, "Briefing view…", self.open_briefing, "Ctrl+B")
        m.addSeparator()
        self._act(m, "Settings…", self.open_settings, "Ctrl+,")
        m.addSeparator()
        self._act(m, "Quit", self.close, "Ctrl+Q")

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
        self.theme_menu = m.addMenu("Theme")
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        m.addSeparator()
        self._act(m, "Reset view", lambda: self.view.set_view(0, 0, max(0.3, self.view.panels[0].rect.width() / 500)),
                  "Home")
        self._act(m, "Full screen", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(),
                  "F11")

    def _menu_radar(self, m):
        self._act(m, "Choose radar…", self.choose_site, "Ctrl+R")
        self.fav_menu = m.addMenu("Favourite radars")
        self.fav_menu.aboutToShow.connect(self._fill_fav_menu)
        self._act(m, "Add / remove this radar as a favourite", self.toggle_favorite, "Ctrl+D")
        m.addSeparator()
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

    def _menu_layers(self, m):
        # warnings and reports
        w = m.addMenu("Warnings && reports")
        self._overlay_act(w, "warnings", "NWS warnings")
        self._overlay_act(w, "watches", "Watches (live)")
        self._overlay_act(w, "reports", "Local storm reports")
        w.addSeparator()
        hours = w.addMenu("Storm reports: show the last")
        self.rep_hours_group = QActionGroup(self)
        for h in (1, 3, 6, 12, 24):
            a = QAction(f"{h} hour" + ("s" if h > 1 else ""), self, checkable=True)
            a.setChecked(int(self.settings["report_hours"] or 3) == h)
            a.triggered.connect(lambda _=False, h=h: self.set_report_hours(h))
            self.rep_hours_group.addAction(a)
            hours.addAction(a)
        types = w.addMenu("Storm reports: types")
        self.rep_type_acts = {}
        for g, label in feeds.REPORT_GROUPS:
            on = bool((self.settings["report_types"] or {}).get(g, g != "other"))
            self.rep_type_acts[g] = self._act(types, label + (" (rain, snow…)" if g == "other" else ""),
                                              lambda checked, g=g: self.set_report_type(g, checked), None,
                                              checkable=True, checked=on)
        self.sn_rep_act = self._act(w, "Include Spotter Network reports", self._toggle_sn_reports, None,
                                    checkable=True, checked=bool(self.settings["spotter_reports"]))
        w.addSeparator()
        self._act(w, "Refresh warnings now", lambda: self.warnings.refresh(force=True), None)
        # storm chasers
        ch = m.addMenu("Storm chasers")
        self._overlay_act(ch, "chasers", "Show storm chasers (Spotter Network)")
        ch.addSeparator()
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
        # SPC
        spc = m.addMenu("Storm Prediction Center")
        self._overlay_act(spc, "spc_outlook", "Day 1 convective outlook")
        self._overlay_act(spc, "spc_mcd", "Mesoscale discussions")
        # Level III
        l3 = m.addMenu("Level III overlays")
        for key, label in (("storm_tracks", "Storm tracks (NST)"), ("meso", "Mesocyclones (NMD)"),
                           ("tvs", "TVS (NTV)"), ("hail", "Hail index (NHI)"), ("melting_layer", "Melting layer (N0M)")):
            self._overlay_act(l3, key, label)
        # base map
        mp = m.addMenu("Map")
        self.cities_act = self._act(mp, "City labels", self._toggle_cities, None, checkable=True,
                                    checked=bool(self.settings["map_layers"].get("cities", True)))
        self.sites_act = self._act(mp, "Radar sites", self._toggle_sites, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("radar_sites", True)))
        self.tdwr_act = self._act(mp, "Include TDWR sites", self._toggle_tdwr, None, checkable=True,
                                  checked=bool(self.settings["map_layers"].get("tdwr_sites", False)))
        self.rings_act = self._act(mp, "Range rings", self._toggle_rings, None, checkable=True,
                                   checked=bool(self.settings["map_layers"].get("range_rings", False)))
        mp.addSeparator()
        self.layer_acts = {}
        from ..render.maps import LAYER_STYLE
        for name, (label, *_rest) in LAYER_STYLE.items():
            on = bool(self.settings["map_layers"].get(name, True))
            self.view.map_visible[name] = on
            self.layer_acts[name] = self._act(mp, label, lambda checked, n=name: self._toggle_layer(n, checked),
                                              None, checkable=True, checked=on)
        m.addSeparator()
        self._act(m, "Placefiles…", self.open_placefiles, "Ctrl+P")
        self._act(m, "All layers in the side panel", lambda: self.show_panel("layers"), None)

    def _menu_tools(self, m):
        for a in self.tool_group.actions():
            m.addAction(a)
        m.addSeparator()
        self._act(m, "Storm cell table", lambda: self.show_panel("cells"), None)
        self._act(m, "Level III storm table (text)", self.show_storm_table, None)

    def _menu_location(self, m):
        self._act(m, "Go to my location", self.go_to_my_location, "Ctrl+L")
        self._act(m, "Set my location…", self.set_my_location_dialog, None)
        self._act(m, "Remove my location", lambda: self.set_my_location(None, None), None)
        m.addSeparator()
        self.warn_loc_act = self._act(m, "Alert me when a warning covers my location", self._toggle_warn_loc, None,
                                      checkable=True, checked=bool(self.settings["warn_at_location"]))

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

    def _build_status(self):
        sb = self.statusBar()
        self.readout = QLabel("")
        self.readout.setMinimumWidth(500)
        sb.addWidget(self.readout, 1)
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(140)
        self.prog.setVisible(False)
        sb.addPermanentWidget(self.prog)
        self.track_lbl = QLabel("")
        self.track_lbl.setStyleSheet("color:#ffd23c;font-weight:bold;")
        self.track_lbl.setVisible(False)
        sb.addPermanentWidget(self.track_lbl)
        self.status_lbl = QLabel("")
        sb.addPermanentWidget(self.status_lbl)
        self.gl_warn = QLabel("⚠ Software OpenGL")
        self.gl_warn.setStyleSheet("color:#ffb347;font-weight:bold;")
        self.gl_warn.setVisible(False)
        sb.addPermanentWidget(self.gl_warn)

    def _shortcuts(self):
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.WindowShortcut)
            s.activated.connect(fn)
        sc(Qt.Key_Left, lambda: self.step_frame(-1))
        sc(Qt.Key_Right, lambda: self.step_frame(1))
        sc(Qt.Key_Up, lambda: self.step_tilt(1))
        sc(Qt.Key_Down, lambda: self.step_tilt(-1))
        sc(Qt.Key_Space, self.toggle_play)
        sc(Qt.Key_End, lambda: self.goto_frame(len(self.data.frames) - 1))
        sc("P", lambda: self.set_tool("pan"))
        sc("X", lambda: self.set_tool("xsection"))
        sc("M", lambda: self.set_tool("measure"))
        sc("B", lambda: self.set_tool("box3d"))
        sc("T", lambda: self.set_tool("track"))
        sc(Qt.Key_Escape, self._escape)
        for n in range(1, 7):
            sc(f"Alt+{n}", lambda n=n: self.set_layout(n))

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
