"""Toolbar, timeline bar and status bar."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import QComboBox, QLabel, QProgressBar, QSizePolicy, QSlider, QToolBar, QToolButton


class ToolbarsMixin:
    """Toolbar, timeline bar and status bar."""

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

    def show_gl_warning(self, text):
        self.gl_warn.setToolTip(text)
        self.gl_warn.setVisible(True)
        self._status_msg(text)

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

    def _status_msg(self, msg):
        self.status_lbl.setText(msg[:160])

    def _progress(self, done, total):
        busy = 0 < total and done < total
        if self.prog.isVisible() != busy:
            self.prog.setVisible(busy)
        if busy:
            if self.prog.maximum() != total:
                self.prog.setRange(0, total)
            self.prog.setValue(done)
            self.prog.setFormat(f"{done}/{total}")
