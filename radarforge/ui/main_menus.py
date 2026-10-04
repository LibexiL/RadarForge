"""Toolbar, timeline bar, menus, status bar and keyboard shortcuts.

Where things live (one home per topic):
  File      data in (files, archive) and pictures out (export), settings
  View      how the radar is drawn: layout, smoothing, dealiasing, Σ trail, colour bars, theme, panels
  Radar     which radar and when: radars, favourites, live / archive, frames, tilts, storm motion
  Layers    everything drawn on the map, grouped: warnings & SPC, weather data, Level III, map
  Tools     mouse tools and storm analysis
  Location  my location, saved places and their alerts
  Panels    side and tool panels
  Help      shortcuts, the radar guide, learn mode, component check, about
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMenu, QMessageBox, QProgressBar, QSizePolicy,
                               QSlider, QToolBar, QToolButton, QWidget)

from .. import __version__, themes
from ..features import feeds

LAYOUT_NAMES = {1: "1 panel", 2: "2 panels side by side", 3: "3 panels side by side", 4: "4 panels (2 × 2)",
                5: "5 panels (3 over 2)", 6: "6 panels (3 × 2)"}
LOOP_FPS = (2, 4, 6, 8, 10, 15)
LOOP_FRAMES = (6, 12, 18, 24, 36, 48)


class MenusMixin:
    """Part of MainWindow: toolbar, timeline bar, menus, status bar and keyboard shortcuts."""

    # ------------------------------------------------------------------ helpers
    def _tb_button(self, tb, action, text_beside=True):
        tb.addAction(action)
        w = tb.widgetForAction(action)
        if isinstance(w, QToolButton):
            w.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text_beside else Qt.ToolButtonIconOnly)
        return w

    def _iconize(self, target, name):
        """Give an action / button a themed icon (re-tinted when the theme changes)."""
        from . import icons
        target.setIcon(icons.icon(name))
        self._icon_targets.append((target, name))
        return target

    def _act(self, menu, text, fn, shortcut=None, checkable=False, checked=False, icon=None, tip=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
            a.toggled.connect(fn)
        else:
            a.triggered.connect(fn)
        if icon:
            self._iconize(a, icon)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        menu.addAction(a)
        return a

    @staticmethod
    def _header(menu, text):
        """A small bold caption between menu groups (Fusion draws addSection() as a bare line)."""
        if menu.actions():
            menu.addSeparator()
        a = menu.addAction(text.replace("&", "&&"))      # a caption, not a mnemonic
        f = a.font()
        f.setBold(True)
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.9))
        a.setFont(f)
        a.setEnabled(False)
        return a

    def _submenu(self, menu, title, icon=None):
        sub = menu.addMenu(title)
        if icon:
            self._iconize(sub.menuAction(), icon)
        return sub

    def _radio(self, menu, items, current, fn, optional=False):
        """Radio items [(value, label)] calling fn(value); returns the actions (value in .data())."""
        grp = QActionGroup(self)
        if optional:
            grp.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        acts = []
        for value, label in items:
            a = QAction(label, self, checkable=True)
            a.setData(value)
            a.setChecked(value == current)
            a.triggered.connect(lambda _=False, v=value: fn(v))
            grp.addAction(a)
            if menu is not None:
                menu.addAction(a)
            acts.append(a)
        return acts

    def _show_menu_checks(self):
        """Fusion draws an on/off item's icon *instead of* its tick, so its state can't be seen in a menu.
        Those items keep their icon on the toolbar and show the tick in menus."""
        seen = {}                        # id -> action: holding the wrapper keeps its id from being reused

        def walk(menu):
            for a in menu.actions():
                if id(a) in seen:
                    continue
                seen[id(a)] = a
                if a.menu() is not None:
                    walk(a.menu())
                elif a.isCheckable():                # (toolbar icons are only set once the theme applies)
                    a.setIconVisibleInMenu(False)
        for a in self.menuBar().actions():
            if a.menu() is not None:
                walk(a.menu())

    @staticmethod
    def _sync_radio(acts, value):
        """Tick the radio item whose value matches (none when nothing matches, for optional groups)."""
        for a in acts:
            on = a.data() == value
            if a.isChecked() != on:
                a.blockSignals(True)
                a.setChecked(on)
                a.blockSignals(False)

    def _overlay_act(self, menu, key, label, icon=None, tip=None):
        self.overlay_acts[key] = self._act(menu, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                           None, checkable=True,
                                           checked=bool(self.settings["overlays"].get(key, False)), icon=icon,
                                           tip=tip)
        return self.overlay_acts[key]

    # ------------------------------------------------------------------ toolbar
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
        self.live_act.setToolTip("Real-time data from AWS (Level II chunks + Level III). F5 reloads it.")
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

        # layout: one button with a menu (the six layouts used to take six buttons)
        self.layout_group = QActionGroup(self)
        cur = int(self.settings["layout"])
        layout_menu = QMenu(self)
        for n in range(1, 7):
            act = QAction(LAYOUT_NAMES[n], self, checkable=True)
            act.setToolTip(f"{LAYOUT_NAMES[n]} (Alt+{n})")
            act.setData(n)
            act.setChecked(n == cur)
            act.triggered.connect(lambda _=False, n=n: self.set_layout(n))
            self.layout_group.addAction(act)
            layout_menu.addAction(act)
            self._icon_targets.append((act, f"layout{n}"))
        self.layout_btn = QToolButton()
        self.layout_btn.setMenu(layout_menu)
        self.layout_btn.setPopupMode(QToolButton.InstantPopup)
        self.layout_btn.setToolTip("Panel layout (Alt+1 … Alt+6)")
        tb.addWidget(self.layout_btn)
        self._update_layout_btn()
        tb.addSeparator()

        # tilt
        down = QAction("Lower tilt", self)
        down.setToolTip("Lower tilt (Down)")
        down.triggered.connect(lambda: self.step_tilt(-1))
        self._tb_button(tb, down, text_beside=False)
        self._icon_targets.append((down, "down"))
        self.tilt_combo = QComboBox()
        self.tilt_combo.setMinimumContentsLength(7)
        self.tilt_combo.setToolTip("Elevation angle (↑ / ↓)")
        self.tilt_combo.activated.connect(self._tilt_chosen)
        tb.addWidget(self.tilt_combo)
        up = QAction("Higher tilt", self)
        up.setToolTip("Higher tilt (Up)")
        up.triggered.connect(lambda: self.step_tilt(1))
        self._tb_button(tb, up, text_beside=False)
        self._icon_targets.append((up, "up"))
        self.tilt_down_act, self.tilt_up_act = down, up
        tb.addSeparator()

        # mouse tools
        self.tool_group = QActionGroup(self)
        for tool, text, tip, ic in (("pan", "Pan", "Pan / zoom (P)", "pan"),
                                    ("xsection", "X-Section", "Cross section: drag a line across a storm (X)",
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
        tb.addSeparator()

        # display switches (GR2Analyst-style): smoothing, velocity dealiasing, max-value trail
        self.smooth_act = QAction("Smoothing", self, checkable=True)
        self.smooth_act.setChecked(bool(self.settings["gpu_smooth"]))
        self.smooth_act.setShortcut(QKeySequence("S"))
        self.smooth_act.setToolTip("Smoothing on / off (S)")
        self.smooth_act.toggled.connect(self._toggle_smooth)
        self.dealias_act = QAction("Dealias velocity", self, checkable=True)
        self.dealias_act.setChecked(bool(self.settings["dealias_velocity"]))
        self.dealias_act.setShortcut(QKeySequence("D"))
        self.dealias_act.setToolTip("Dealias velocity (D): unfold aliased velocities in the velocity and "
                                    "storm-relative panels (Level II and Level III)")
        self.dealias_act.toggled.connect(self._toggle_dealias)
        self.trail_act = QAction("Max value trail", self, checkable=True)
        self.trail_act.setChecked(bool(self.settings["trail_mode"]))
        self.trail_act.setShortcut(QKeySequence("Ctrl+T"))
        self.trail_act.setToolTip("Σ Max value trail (Ctrl+T): every panel shows the highest value seen at each "
                                  "spot over the loop up to the frame shown – hail swaths, rotation tracks, "
                                  "strongest winds (CC shows its lowest: debris trails)")
        self.trail_act.toggled.connect(self._toggle_trail)
        for a, ic in ((self.smooth_act, "smooth"), (self.dealias_act, "dealias"), (self.trail_act, "trail")):
            self._tb_button(tb, a, text_beside=False)
            self._icon_targets.append((a, ic))

        # panel switches live in the menu bar's free right-hand corner, so they're never pushed off a
        # narrow window: [Quick] [Side panel] (Quick is added once the panels exist)
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
        self.corner = QWidget()
        cl = QHBoxLayout(self.corner)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(2)
        cl.addWidget(self.side_btn)
        self.menuBar().setCornerWidget(self.corner, Qt.TopRightCorner)
        self._icon_targets.append((self.side_act, "side"))

    def _update_layout_btn(self):
        from . import icons
        n = int(self.settings["layout"])
        if getattr(self, "layout_btn", None) is not None:
            self.layout_btn.setIcon(icons.icon(f"layout{n}"))
            self.layout_btn.setToolTip(f"Panel layout: {LAYOUT_NAMES.get(n, n)} (Alt+1 … Alt+6)")

    def _fit_toolbar(self):
        """Narrow windows: first drop the words beside the toolbar icons, then shorten the radar and storm
        motion buttons (tooltips still say what everything is), so every button stays visible instead of
        disappearing into the overflow arrow."""
        tb = getattr(self, "main_tb", None)
        if tb is None:
            return
        acts = [self.live_act, self.archive_act, self.open_act] + list(self.tool_group.actions())
        wids = [w for w in (tb.widgetForAction(a) for a in acts) if isinstance(w, QToolButton)]
        sm = tb.widgetForAction(self.sm_act)
        labels = getattr(self, "_site_label", (self.site_btn.text(), self.site_btn.text()))

        def apply(level):
            for w in wids:
                w.setToolButtonStyle(Qt.ToolButtonIconOnly if level >= 1 else Qt.ToolButtonTextBesideIcon)
            if isinstance(sm, QToolButton):
                sm.setToolButtonStyle(Qt.ToolButtonIconOnly if level >= 2 else Qt.ToolButtonTextBesideIcon)
            self.site_btn.setText(labels[0] if level >= 2 else labels[1])

        def need():
            items = [w for w in (tb.widgetForAction(a) for a in tb.actions()) if w is not None]
            m = tb.contentsMargins()
            sp = tb.layout().spacing() if tb.layout() is not None else 4
            return sum(w.sizeHint().width() for w in items) + sp * len(items) + m.left() + m.right() + 16

        level = 2
        for lv in (0, 1, 2):
            apply(lv)
            if need() <= tb.width():
                level = lv
                break
        if level != getattr(self, "_tb_level", None):
            self._tb_level = level
            tb.layout().invalidate()
            tb.updateGeometry()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_toolbar()

    # ------------------------------------------------------------------ timeline (bottom)
    def _build_timeline(self):
        """Frame / loop controls along the bottom, above the status bar."""
        tb = QToolBar("Timeline", self)
        tb.setObjectName("timeline_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(Qt.BottomToolBarArea, tb)
        self.timeline_tb = tb
        for name, tip, fn in (("first", "First frame (Home)", lambda: self.goto_frame(0)),
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
        tb.addSeparator()
        # loop speed and length, one click away (GR's animation settings)
        self.fps_btn = QToolButton()
        self.fps_btn.setPopupMode(QToolButton.InstantPopup)
        self.fps_btn.setToolTip("Loop speed")
        menu = QMenu(self)
        self.fps_acts = self._radio(menu, [(v, f"{v} frames per second") for v in LOOP_FPS],
                                    int(round(float(self.settings["loop_fps"] or 6))), self.set_loop_fps, optional=True)
        self.fps_btn.setMenu(menu)
        tb.addWidget(self.fps_btn)
        self.frames_btn = QToolButton()
        self.frames_btn.setPopupMode(QToolButton.InstantPopup)
        self.frames_btn.setToolTip("How many frames the loop holds (live) – archive loads what you pick")
        menu = QMenu(self)
        self.frames_acts = self._radio(menu, [(v, f"{v} frames") for v in LOOP_FRAMES],
                                       int(self.settings["loop_frames"] or 12), self.set_loop_frames, optional=True)
        self.frames_btn.setMenu(menu)
        tb.addWidget(self.frames_btn)
        self._update_loop_buttons()

    def _update_loop_buttons(self):
        fps = float(self.settings["loop_fps"] or 6)
        self.fps_btn.setText(f"{fps:g} fps")
        self.frames_btn.setText(f"{int(self.settings['loop_frames'] or 12)} frames")
        self._sync_radio(self.fps_acts, int(round(fps)))
        self._sync_radio(self.frames_acts, int(self.settings["loop_frames"] or 12))

    def set_loop_fps(self, fps):
        self.settings["loop_fps"] = float(fps)
        self.settings.save()
        if self.playing:
            self.play_timer.setInterval(int(1000 / max(0.5, float(fps))))
        self._update_loop_buttons()

    def set_loop_frames(self, n):
        self.settings["loop_frames"] = int(n)
        self.settings.save()
        self._update_loop_buttons()
        from ..data.frames import VOLUMES, volume_capacity
        VOLUMES.capacity = volume_capacity(self.settings)
        if self.data.mode == "live":
            self.data._trim()
            self.data._last_sync = 0.0          # a longer loop is filled from the archive at the next tick
            self.data._emit_frames()
        self._status_msg(f"Loop length: {n} frames" + (" (live)" if self.data.mode == "live" else ""))

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        mb = self.menuBar()
        self.overlay_acts = {}
        self._menu_file(mb.addMenu("&File"))
        self._menu_view(mb.addMenu("&View"))
        self._menu_radar(mb.addMenu("&Radar"))
        self._menu_layers(mb.addMenu("&Layers"))
        self._menu_tools(mb.addMenu("&Tools"))
        self._menu_location(mb.addMenu("L&ocation"))
        self.panels_menu = mb.addMenu("&Panels")
        self._menu_help(mb.addMenu("&Help"))

    def _menu_file(self, m):
        m.addAction(self.open_act)
        self.open_act.setText("Open radar files…")
        self.open_act.setIconText("Open")              # the toolbar keeps the short words
        self.open_act.setShortcut(QKeySequence("Ctrl+O"))
        m.addAction(self.archive_act)
        self.archive_act.setText("Open archive…")
        self.archive_act.setIconText("Archive")
        self.archive_act.setShortcut(QKeySequence("Ctrl+A"))
        m.addSeparator()
        ex = self._submenu(m, "Export", "image")
        self._act(ex, "Save image…", self.save_image, "Ctrl+S", icon="save")
        self._act(ex, "Save image with legend and details…", self.save_image_annotated, "Ctrl+Shift+S")
        self._act(ex, "Copy image", self.copy_image, "Ctrl+Shift+C")
        ex.addSeparator()
        self._act(ex, "Export loop (GIF / MP4)…", self.export_loop, "Ctrl+E", icon="film")
        ex.addSeparator()
        self._act(ex, "Briefing view…", self.open_briefing, "Ctrl+B")
        m.addSeparator()
        self._act(m, "Settings…", self.open_settings, "Ctrl+,", icon="settings")
        m.addSeparator()
        self._act(m, "Quit", self.close, "Ctrl+Q")

    def _menu_view(self, m):
        lay = self._submenu(m, "Panel layout", "layout4")
        for a in self.layout_group.actions():
            lay.addAction(a)
        m.addSeparator()
        m.addAction(self.smooth_act)
        m.addAction(self.dealias_act)
        m.addAction(self.trail_act)
        vf = m.addMenu("Velocity noise filter")
        self.vf_group = QActionGroup(self)
        for lvl, label in ((0, "Off (raw data)"), (1, "Normal"), (2, "Aggressive")):
            a = QAction(label, self, checkable=True)
            a.setData(lvl)
            a.setChecked(int(self.settings["velocity_filter"]) == lvl)
            a.triggered.connect(lambda _=False, lv=lvl: self.set_velocity_filter(lv))
            self.vf_group.addAction(a)
            vf.addAction(a)
        m.addSeparator()
        self.legend_act = self._act(m, "Colour bars", self._toggle_legend, None, checkable=True,
                                    checked=bool(self.settings["show_legend"]))
        self.link_act = self._act(m, "Linked cursor", self._toggle_link, None, checkable=True,
                                  checked=bool(self.settings["cursor_link"]))
        self.hover_act = self._act(m, "Pop-up details on hover", self._toggle_hover_text, None, checkable=True,
                                   checked=bool(self.settings["hover_text"]),
                                   tip="Show warning, storm and layer details when the mouse rests on them")
        m.addSeparator()
        self.theme_menu = self._submenu(m, "Theme", "theme")
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        self._act(m, "Map colours && fonts…", lambda: self.open_settings("Map style"), None, icon="palette")
        m.addSeparator()
        self._act(m, "Reset view", self.reset_view, "Home")
        self._act(m, "Full screen", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(),
                  "F11")

    def _menu_radar(self, m):
        self._act(m, "Choose radar…", self.choose_site, "Ctrl+R", icon="radar")
        self.fav_menu = self._submenu(m, "Favourite radars", "star")
        self.fav_menu.aboutToShow.connect(self._fill_fav_menu)
        self._act(m, "Add / remove this radar as a favourite", self.toggle_favorite, "Ctrl+D")
        m.addSeparator()
        m.addAction(self.live_act)
        self.live_act.setText("Live data")
        self.live_act.setIconText("Live")
        self._act(m, "Reload live data", self.reload_live, "F5", icon="refresh",
                  tip="Start the live feed for this radar again")
        m.addAction(self.archive_act)
        m.addSeparator()
        fr = self._submenu(m, "Frames", "play")
        self._act(fr, "Play / pause loop", self.toggle_play, None).setToolTip("Space")
        self._act(fr, "Previous frame", lambda: self.step_frame(-1), None)
        self._act(fr, "Next frame", lambda: self.step_frame(1), None)
        self._act(fr, "First frame", lambda: self.goto_frame(0), None)
        self._act(fr, "Latest frame", lambda: self.goto_frame(len(self.data.frames) - 1), None)
        tl = self._submenu(m, "Tilt", "up")
        self._act(tl, "Higher tilt", lambda: self.step_tilt(1), None)
        self._act(tl, "Lower tilt", lambda: self.step_tilt(-1), None)
        self._act(tl, "Lowest tilt", lambda: self.set_tilt_elev(0.0), None)
        m.addSeparator()
        self._act(m, "Storm motion…", self.edit_storm_motion, None, icon="motion")

    def _menu_layers(self, m):
        self._header(m, "Warnings & outlooks")
        w = self._submenu(m, "Warnings && reports", "warning")
        self._overlay_act(w, "warnings", "NWS warnings")
        self._overlay_act(w, "watches", "Watches (live)")
        self._overlay_act(w, "reports", "Local storm reports")
        w.addSeparator()
        hours = w.addMenu("Storm reports: show the last")
        self.rep_hours_group = QActionGroup(self)
        for h in (1, 3, 6, 12, 24):
            a = QAction(f"{h} hour" + ("s" if h > 1 else ""), self, checkable=True)
            a.setData(h)
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
        self._act(w, "Refresh warnings now", lambda: self.warnings.refresh(force=True), None, icon="refresh")
        spc = self._submenu(m, "Storm Prediction Center", "flag")
        self._overlay_act(spc, "spc_outlook", "Convective outlook")
        self._overlay_act(spc, "spc_mcd", "Mesoscale discussions")
        self._menu_spc_days(spc)
        ch = self._submenu(m, "Storm chasers", "target")
        self._overlay_act(ch, "chasers", "Show storm chasers (Spotter Network)")
        ch.addSeparator()
        self.chaser_acts = self._radio(ch, [(False, "Everyone"), (True, "Active reporters only (5+ reports in a year)")],
                                       bool(self.settings["chasers_active_only"]), self._set_chasers_active)
        ch.addSeparator()
        self.chaser_names_act = self._act(ch, "Show names (zoomed in)", self._toggle_chaser_names, None,
                                          checkable=True, checked=bool(self.settings["chaser_names"]))
        self._header(m, "Weather data")
        self._menu_data_layers(m)
        self._header(m, "Radar & map")
        l3 = self._submenu(m, "Level III overlays", "radar")
        for key, label in (("storm_tracks", "Storm tracks (NST)"), ("hail", "Hail index (NHI)"),
                           ("melting_layer", "Melting layer (N0M)")):
            self._overlay_act(l3, key, label)
        mp = self._submenu(m, "Map", "layers")
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
        mp.addSeparator()
        self._act(mp, "Map colours && fonts…", lambda: self.open_settings("Map style"), None)
        self._act(m, "Placefiles…", self.open_placefiles, "Ctrl+P")
        m.addSeparator()
        self._act(m, "Quick panel (every switch in one place)", lambda: self.show_panel("quick"), None, icon="quick")

    def _menu_tools(self, m):
        self._header(m, "Mouse tools")
        for a in self.tool_group.actions():
            m.addAction(a)
        self._header(m, "Storm analysis")
        self._menu_storm_tools(m)
        m.addSeparator()
        self._act(m, "Storm cell table", lambda: self.show_panel("cells"), None)
        self._act(m, "Level III storm table (text)", self.show_storm_table, None)

    def _menu_location(self, m):
        self._act(m, "Go to my location", self.go_to_my_location, "Ctrl+L", icon="pin")
        self._act(m, "Set my location…", self.set_my_location_dialog, None)
        self._act(m, "Remove my location", lambda: self.set_my_location(None, None), None)
        m.addSeparator()
        self.warn_loc_act = self._act(m, "Alert me when a warning covers my location", self._toggle_warn_loc, None,
                                      checkable=True, checked=bool(self.settings["warn_at_location"]))
        m.addSeparator()
        self._act(m, "Saved locations && alerts…", lambda: self.open_locations(), "Ctrl+Shift+L", icon="star")
        self._act(m, "Save the map centre as a location…",
                  lambda: self.save_location_here(*self.view.world_to_latlon(self.view.cx, self.view.cy)), None)
        self._act(m, "Test the alert sound", lambda: self.notifier.play(), None)

    def _menu_help(self, m):
        self._act(m, "Keyboard shortcuts", self.show_shortcuts, "F1", icon="keyboard")
        self._menu_help_extras(m)
        self._act(m, "Check optional components…", self.show_component_check, None,
                  tip="Lightning files, MRMS decoding, alert sounds, MP4 export and soundings")
        m.addSeparator()
        self._act(m, "About RadarForge", self.show_about, None, icon="info")

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

    # ------------------------------------------------------------------ status bar
    def _build_status(self):
        sb = self.statusBar()
        self.readout = QLabel("")
        self.readout.setMinimumWidth(300)
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
        self.status_lbl.setMaximumWidth(620)
        sb.addPermanentWidget(self.status_lbl)
        self.data_lbl = QLabel("")             # LIVE · 25 s ago / ARCHIVE / FILES
        self.data_lbl.setToolTip("How fresh the data on screen is")
        sb.addPermanentWidget(self.data_lbl)
        self.gl_warn = QLabel("⚠ Software OpenGL")
        self.gl_warn.setStyleSheet("color:#ffb347;font-weight:bold;")
        self.gl_warn.setVisible(False)
        sb.addPermanentWidget(self.gl_warn)

    # ------------------------------------------------------------------ keys
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
            ("Display", [("S", "smoothing on / off"), ("D", "dealias velocity on / off"),
                         ("Ctrl+T", "Σ max value trail on / off")]),
            ("Map", [("wheel / drag", "zoom / pan"), ("double-click", "centre here"), ("Home", "reset view"),
                     ("right-click", "product, colour table, sounding, storm tools, nearest radar")]),
            ("Window", [("F8", "Quick panel"), ("F9", "show / hide the side panel"), ("F11", "full screen"),
                        ("F1", "this list")]),
            ("Data & files", [("F5", "reload live data"), ("Ctrl+O", "open files"), ("Ctrl+A", "archive"),
                              ("Ctrl+R", "choose radar"), ("Ctrl+D", "add / remove favourite radar"),
                              ("Ctrl+L", "go to my location"), ("Ctrl+Shift+L", "saved locations & alerts"),
                              ("Ctrl+P", "placefiles"), ("Ctrl+S", "save image"), ("Ctrl+Shift+C", "copy image"),
                              ("Ctrl+E", "export loop"), ("Ctrl+B", "briefing view"), ("Ctrl+,", "settings")]),
        ]
        html = "<table cellspacing='0' cellpadding='3'>"
        for title, items in rows:
            html += f"<tr><td colspan='2'><br><b>{title}</b></td></tr>"
            html += "".join(f"<tr><td style='padding-right:18px'><code>{k}</code></td><td>{v}</td></tr>"
                            for k, v in items)
        html += "</table><p>Drag a panel's tab to move it; drop zones show where it will go.</p>"
        QMessageBox.information(self, "Keyboard shortcuts", html)

    def show_component_check(self):
        from ..app import check_components
        rows = check_components()
        html = "<table cellspacing='0' cellpadding='4'>" + "".join(
            f"<tr><td>{'✅' if ok else '❌'}</td><td><b>{name}</b></td><td>{msg}</td></tr>" for name, ok, msg in rows)
        html += "</table>"
        if not all(ok for _n, ok, _m in rows):
            html += "<p>Running the installer again usually fixes a missing part.</p>"
        QMessageBox.information(self, "Optional components", html)

    def show_about(self):
        QMessageBox.about(self, "About RadarForge", (
            f"<b>RadarForge {__version__}</b> – NEXRAD Level II / III viewer<br><br>"
            "Data: NOAA NEXRAD on AWS (Unidata buckets), NWS API alerts, Iowa Environmental Mesonet archives, "
            "storm reports, SPC products, GOES satellite pictures, surface observations and model soundings; "
            "NOAA MRMS and GOES GLM lightning on AWS; storm chasers and spotter reports from Spotter Network "
            "(non-commercial use); street cameras from Caltrans, state 511 systems and Windy.<br>"
            "Maps: US Census county boundaries, Natural Earth, GeoNames.<br>"
            "Level III decoding and sounding maths by MetPy.<br><br>"
            f"OpenGL: {self.view.gl_info}"))
