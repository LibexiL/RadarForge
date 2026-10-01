"""RadarForge main window."""
from __future__ import annotations

import math
import os
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QLabel, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QSizePolicy, QSlider, QToolBar, QToolButton, QVBoxLayout,
                               QWidget)

from .. import __version__, themes
from ..config import APP_NAME
from ..data.sites import get_site, nearest_site
from ..features.l3overlay import Level3Overlay
from ..features.placefile import PlacefileManager
from ..features.warnings import WarningsOverlay
from ..products import catalog, colortable
from ..products.engine import ProductEngine
from ..products.geometry import aeqd_forward, beam_height, slant_range
from ..render.glview import RadarView
from ..tools.volume3d import Volume3DWindow
from ..tools.xsection import CrossSectionWindow
from .datamanager import DataManager
from .dialogs import ArchiveDialog, PlacefilePanel, SiteDialog, StormMotionDialog
from .settings_dialog import SettingsDialog
from . import icons
from .panels import CELL_CODES, CellsPanel, InspectorPanel, LayersPanel, ProductsPanel, WarningsPanel
from .workspace import Workspace

L3_TILT_ELEVS = [0.5, 0.9, 1.3, 1.8]
UNIT_F = {"nm": 1.852, "km": 1.0, "mi": 1.609344}


class _Relay(QObject):
    imageReady = Signal(int, object)      # panel index, result dict


class _ImageJob(QRunnable):
    def __init__(self, fn, relay, panel):
        super().__init__()
        self.fn, self.relay, self.panel = fn, relay, panel
        self.setAutoDelete(True)

    def run(self):
        try:
            res = self.fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            res = {"error": str(exc)}
        self.relay.imageReady.emit(self.panel, res)


class _Bg(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.fn()
        except Exception:
            pass


class _Lazy3D(QWidget):
    """Stand-in for the 3-D panel; the OpenGL view inside is created on first show."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.win = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

    def ensure(self):
        if self.win is None:
            self.win = Volume3DWindow(self.main)
            self.win.closed.connect(lambda: self.main.view.set_box(None))
            self.layout().addWidget(self.win)
        return self.win

    def showEvent(self, ev):
        self.ensure()
        super().showEvent(ev)


class MainWindow(QMainWindow):
    stateChanged = Signal()          # frame / panel / tilt / product changed (side panels refresh)
    cursorInfo = Signal(object)      # dict for the cursor inspector

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 950)
        self.engine = ProductEngine(settings)
        self.data = DataManager(settings, self)
        self.view = RadarView()
        self.ws = Workspace(self.view.make_container(), self)
        self.ws.center_overlay = self.view.set_overlay_image     # drop zones over the (native) map
        self.view.drop_handler = self._view_drag
        self.setCentralWidget(self.ws)
        self.frame_index = -1
        self._shown_frame = None
        self.follow_latest = True
        self.tilt_elev = 0.5
        self.playing = False
        self._palettes: dict = {}
        self._panel_req: dict = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.bg_pool = QThreadPool(self)
        self.bg_pool.setMaxThreadCount(1)
        self._prefetch_gen = 0
        self._shown_frame = None
        # coalesce bursts of data events into one UI refresh
        self._frames_timer = QTimer(self)
        self._frames_timer.setSingleShot(True)
        self._frames_timer.setInterval(200)
        self._frames_timer.timeout.connect(self._apply_frames_changed)
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(150)
        self._update_timer.timeout.connect(self._show_frame)
        self.relay = _Relay()
        self.relay.imageReady.connect(self._image_ready)
        self.xs_win = None
        self.v3d_host = None

        # overlays
        self.warnings = WarningsOverlay(settings, self.view.maps.county_polygons, self)
        self.placefiles = PlacefileManager(settings, self)
        self.l3ov = Level3Overlay(settings)
        self.view.overlays = [self.warnings, self.placefiles, self.l3ov]
        self.view.underlays = [self.placefiles]
        self.view.hover_providers = [self.l3ov, self.placefiles, self.warnings]
        for sig in (self.warnings.changed, self.placefiles.changed):
            sig.connect(self.view.update)
        self.warnings.status.connect(self._status_msg)
        self.placefiles.status.connect(self._status_msg)

        self._build_toolbar()
        self._build_timeline()
        self._build_menus()
        self._build_status()
        self._build_panels()
        self._shortcuts()
        self.setAcceptDrops(True)

        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._play_step)

        # data signals
        self.data.framesChanged.connect(self._frames_changed)
        self.data.frameUpdated.connect(self._frame_updated)
        self.data.loadingChanged.connect(self._loading_changed)
        self.data.status.connect(self._status_msg)
        self.data.error.connect(lambda m: self._status_msg("⚠ " + m))
        self.data.progress.connect(self._progress)

        self.view.cursorMoved.connect(self._cursor)
        self.view.panelMenuRequested.connect(self._panel_menu)
        self.view.lineDrawn.connect(self._line_drawn)
        self.view.boxDrawn.connect(self._box_drawn)
        self.view.siteClicked.connect(lambda sid: self.switch_site(sid, keep_view=True))
        self.view.panelActivated.connect(lambda i: self.stateChanged.emit())

        # initial state
        self._apply_view_settings()
        self.view.set_layout(int(settings["layout"]), list(settings["panels"]))
        self._set_site_projection(self.data.site_id)
        v = settings["view"]
        if v:
            self.view.set_view(v.get("cx", 0), v.get("cy", 0), v.get("scale", 1.5))
        else:
            self.view.set_view(0, 0, 1.4)
        self._update_l3_needs()
        self.placefiles.reload_all()
        g = settings["window_geometry"]
        if g:
            try:
                self.restoreGeometry(bytes.fromhex(g))
            except Exception:
                pass
        if not self.ws.restore(settings["workspace"] or {}):
            self.ws.apply_default(self.width(), self.height())
        self.lock_act.setChecked(self.ws.locked)
        self._sync_side_act()
        self.apply_theme(settings["theme"], save=False)
        from ..data.level2 import warm_up
        QTimer.singleShot(1500, warm_up)          # start decoder processes before they're needed

    @property
    def v3d_win(self):
        return self.v3d_host.win if self.v3d_host is not None else None

    # ================================================================== UI build
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
        mb = self.menuBar()
        # File
        m = mb.addMenu("&File")
        self._act(m, "Open radar files…", self.open_files, "Ctrl+O")
        self._act(m, "Open archive from AWS…", self.open_archive, "Ctrl+A")
        m.addSeparator()
        self._act(m, "Save image…", self.save_image, "Ctrl+S")
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

        # Map (overlays + map layers + placefiles)
        m = mb.addMenu("&Map")
        self.overlay_acts = {}
        m.addSection("Warnings")
        for key, label in (("warnings", "NWS warnings"), ("watches", "Watches (live)"),
                           ("reports", "Local storm reports")):
            self.overlay_acts[key] = self._act(m, label, lambda checked, k=key: self._toggle_overlay(k, checked),
                                               None, checkable=True,
                                               checked=bool(self.settings["overlays"].get(key, False)))
        self._act(m, "Refresh warnings now", lambda: self.warnings.refresh(force=True), None)
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
        from ..render.maps import LAYER_STYLE
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

    # ================================================================== side panel / workspace
    SIDE_PANELS = ("products", "warnings", "cells", "inspector", "placefiles", "layers")

    def _build_panels(self):
        ws = self.ws
        self.products_panel = ProductsPanel(self)
        self.warnings_panel = WarningsPanel(self)
        self.cells_panel = CellsPanel(self)
        self.inspector_panel = InspectorPanel(self)
        self.placefile_panel = PlacefilePanel(self.placefiles, compact=True)
        self.layers_panel = LayersPanel(self)
        for key, title, w in (("products", "Products", self.products_panel),
                              ("warnings", "Warnings", self.warnings_panel),
                              ("cells", "Storm cells", self.cells_panel),
                              ("inspector", "Inspector", self.inspector_panel),
                              ("placefiles", "Placefiles", self.placefile_panel),
                              ("layers", "Layers", self.layers_panel)):
            ws.register(key, title, w, "side")
        self.xs_win = CrossSectionWindow(self)
        self.xs_win.closed.connect(self._xsection_closed)
        ws.register("xsection", "Cross Section", self.xs_win, "tool")
        # the 3-D view has its own OpenGL widget: only create it when the panel is first opened
        self.v3d_host = _Lazy3D(self)
        ws.register("3d", "3D Volume", self.v3d_host, "tool")
        ws.panelClosed.connect(self._panel_closed)
        ws.sideToggled.connect(lambda _on: self._sync_side_act())
        ws.layoutChanged.connect(self._sync_side_act)
        ws.layoutChanged.connect(self._update_l3_needs)
        # Panels menu
        pm = self.panels_menu
        pm.addAction(self.side_act)
        pm.addSection("Side panels")
        for key in self.SIDE_PANELS:
            pm.addAction(ws.action(key))
        pm.addSection("Tools")
        for key in ("xsection", "3d"):
            pm.addAction(ws.action(key))
        pm.addSeparator()
        self.lock_act = self._act(pm, "Lock panel layout", self._toggle_lock, None, checkable=True)
        self._act(pm, "Reset panel layout", self.reset_panel_layout, None)
        pm.addSeparator()
        tip = pm.addAction("Tip: drag a panel's tab – drop zones show where it will go")
        tip.setEnabled(False)

    def _panel_closed(self, key):
        if key == "xsection":
            self.xs_win.on_closed()
        elif key == "3d" and self.v3d_win is not None:
            self.v3d_win.on_closed()

    def show_panel(self, key):
        self.ws.show_panel(key)

    def show_dock(self, key):          # older name, kept for scripts
        self.show_panel(key)

    def toggle_side_panel(self):
        self.ws.toggle_side()
        self._sync_side_act()

    def _sync_side_act(self):
        if hasattr(self, "side_act") and hasattr(self, "ws"):
            self.side_act.setChecked(self.ws.side_visible())

    def _toggle_lock(self, on):
        self.ws.locked = on
        self._status_msg("Panel layout locked" if on else "Panel layout unlocked: drag a panel's tab to move it")

    def reset_panel_layout(self):
        self.ws.apply_default(self.width(), self.height())
        self.lock_act.setChecked(False)

    reset_dock_layout = reset_panel_layout

    # ================================================================== themes
    def apply_theme(self, name, save=True):
        theme = self.preview_theme(themes.find(name))
        self.settings["theme"] = theme["name"]
        if save:
            self.settings.save()
        return theme

    def preview_theme(self, theme):
        """Apply a theme (dict) to the interface and the map without remembering it."""
        theme = themes.normalize(theme)
        app = QApplication.instance()
        themes.apply_ui(app, theme)
        colors, layers = themes.map_style(theme)
        self.view.set_colors(colors, layers)
        icons.clear_cache()
        for target, ic in self._icon_targets:
            target.setIcon(icons.icon(ic))
        self.play_act.setIcon(icons.icon("pause" if self.playing else "play"))
        for w in (self.ws, self.xs_win, self.v3d_host):
            if w is not None:
                w.update()
        return theme

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
        sc(Qt.Key_Escape, lambda: self.set_tool("pan"))
        for n in range(1, 7):
            sc(f"Alt+{n}", lambda n=n: self.set_layout(n))

    def _apply_view_settings(self):
        s = self.settings
        self.view.smooth = bool(s["gpu_smooth"])
        self.view.show_cities = bool(s["map_layers"].get("cities", True))
        self.view.show_range_rings = bool(s["map_layers"].get("range_rings", False))
        self.view.show_sites = bool(s["map_layers"].get("radar_sites", True))
        self.view.show_tdwr = bool(s["map_layers"].get("tdwr_sites", False))
        self.view.link_cursor = bool(s["cursor_link"])
        self.view.distance_units = s["distance_units"]
        self.view.show_legend = bool(s["show_legend"])
        self.view.invert_wheel = bool(s["invert_scroll"])
        self.view.hover_text = bool(s["hover_text"])
        self.view._city_cache.clear()
        self.view.update()

    # ================================================================== palettes
    def palette_for(self, pid):
        p = catalog.get(pid)
        path = self.settings["palette_overrides"].get(pid)
        key = (pid, path)
        ct = self._palettes.get(key)
        if ct is None:
            try:
                ct = colortable.load_pal(path) if path else colortable.builtin(p.palette)
            except Exception as exc:
                self._status_msg(f"Colour table {path}: {exc}")
                ct = colortable.builtin(p.palette)
            self._palettes[key] = ct
        return ct

    # ================================================================== site / modes
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
        d = SiteDialog(self.data.site_id, self)
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

    def import_theme(self, path):
        try:
            t = themes.import_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "Theme", f"Couldn't use {os.path.basename(path)} as a theme:\n{exc}")
            return
        self.apply_theme(t["name"])
        self._status_msg(f"Theme “{t['name']}” added and applied (View → Theme to switch back)")

    def apply_color_table(self, path, pid=None):
        """Use a .pal for product *pid* (None = every product it was made for). Returns True if applied."""
        name = os.path.basename(path)
        try:
            ct = colortable.load_pal(path)
            if not ct.entries:
                raise ValueError("no Color: lines found")
        except Exception as exc:
            QMessageBox.warning(self, "Colour table", f"Could not read {name}:\n{exc}")
            return False
        fam, label = colortable.table_family(ct)
        fam_pids = [p.id for p in catalog.PRODUCTS if p.palette == fam] if fam else []
        targets = []
        if pid is not None:
            panel_fam = catalog.get(pid).palette
            if fam == panel_fam:
                targets = [pid]
            else:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("Colour table doesn't match")
                what = f"a <b>{colortable.FAMILY_NAMES.get(fam, fam)}</b> table" if fam else \
                    f"made for <b>{label}</b>, which RadarForge doesn't recognise"
                box.setText(f"<b>{name}</b> is {what}.<br>You dropped it on "
                            f"<b>{catalog.get(pid).name}</b>.")
                here = box.addButton(f"Use for {catalog.get(pid).name} anyway", QMessageBox.AcceptRole)
                instead = box.addButton(f"Use for {colortable.FAMILY_NAMES.get(fam, fam)} products",
                                        QMessageBox.ActionRole) if fam_pids else None
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if box.clickedButton() is here:
                    targets = [pid]
                elif instead is not None and box.clickedButton() is instead:
                    targets = fam_pids
                else:
                    return False
        else:
            if not fam_pids:
                QMessageBox.information(self, "Colour table",
                                        f"{name} is for “{label}”, which RadarForge doesn't have. Drop it "
                                        f"directly onto a panel to use it there anyway.")
                return False
            targets = fam_pids
        for t in targets:
            self.settings["palette_overrides"][t] = path
        self.settings.save()
        self._palettes.clear()
        self._panel_req.clear()
        self._show_frame()
        names = ", ".join(catalog.get(t).short for t in targets)
        self._status_msg(f"Colour table {name} → {names}")
        return True

    # ================================================================== frames
    def current_frame(self):
        fr = self.data.frames
        if not fr:
            return None
        i = self.frame_index if 0 <= self.frame_index < len(fr) else len(fr) - 1
        return fr[i]

    def _frames_changed(self):
        if not self._frames_timer.isActive():
            self._frames_timer.start()

    def _apply_frames_changed(self):
        frames = self.data.frames
        n = len(frames)
        if n and self.data.site_id and get_site(self.data.site_id) and \
                self.view.site_id != self.data.site_id:
            self._set_site_projection(self.data.site_id)
        if self.follow_latest or self._shown_frame is None or self._shown_frame not in frames:
            self.frame_index = n - 1 if (self.follow_latest or self._shown_frame is None) else \
                max(0, min(self.frame_index, n - 1))
        else:
            # keep showing the same volume while others are still arriving
            self.frame_index = frames.index(self._shown_frame)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, max(0, n - 1))
        self.frame_slider.setValue(max(0, self.frame_index))
        self.frame_slider.blockSignals(False)
        if self.current_frame() is not self._shown_frame:
            self._show_frame()
        else:
            self._update_time_label()
        if not self.data.loading:
            self._prefetch()

    def _frame_updated(self, frame):
        if frame is self.current_frame() and not self._update_timer.isActive():
            self._update_timer.start()

    def _loading_changed(self, busy):
        if not busy:
            self._prefetch()

    def _update_time_label(self):
        frame = self.current_frame()
        if frame is None:
            self.time_label.setText(" no data ")
            return
        n = len(self.data.frames)
        live = " LIVE" if frame.live else ""
        self.time_label.setText(f" {frame.time:%Y-%m-%d %H:%M:%S}Z  [{self.frame_index + 1}/{n}]{live} ")

    def goto_frame(self, i):
        n = len(self.data.frames)
        if n == 0:
            return
        i = max(0, min(n - 1, i))
        self.frame_index = i
        self.follow_latest = (i == n - 1) and self.data.mode == "live"
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(i)
        self.frame_slider.blockSignals(False)
        self._show_frame()

    def step_frame(self, d):
        n = len(self.data.frames)
        if n:
            self.goto_frame((self.frame_index + d) % n)

    def _slider_moved(self, v):
        self.goto_frame(v)

    def toggle_play(self):
        self.playing = not self.playing
        self.play_act.setIcon(icons.icon("pause" if self.playing else "play"))
        self.play_act.setToolTip("Pause loop (Space)" if self.playing else "Play loop (Space)")
        if self.playing:
            self._prefetch()
            self.play_timer.start(int(1000 / max(0.5, float(self.settings["loop_fps"]))))
        else:
            self.play_timer.stop()

    def _play_step(self):
        n = len(self.data.frames)
        if n < 2:
            return
        nxt = (self.frame_index + 1) % n
        if not self._frame_ready(self.data.frames[nxt]) and getattr(self, "_wait", 0) < 8:
            self._wait = getattr(self, "_wait", 0) + 1
            return
        self._wait = 0
        self.goto_frame(nxt)
        if nxt == n - 1:
            self.play_timer.setInterval(int(1000 * (1.0 / float(self.settings["loop_fps"]) +
                                                    float(self.settings["loop_dwell"]))))
        else:
            self.play_timer.setInterval(int(1000 / max(0.5, float(self.settings["loop_fps"]))))

    # ================================================================== tilts
    def _tilt_index(self, frame):
        if frame is None:
            return 0
        if frame.has_level2():
            tilts = self.engine.tilts(frame)
            if tilts:
                els = np.array([t.elevation for t in tilts])
                return int(np.argmin(np.abs(els - self.tilt_elev)))
        return int(np.argmin(np.abs(np.array(L3_TILT_ELEVS) - self.tilt_elev)))

    def step_tilt(self, d):
        frame = self.current_frame()
        els = self._tilt_elevs(frame)
        if not els:
            return
        i = int(np.argmin(np.abs(np.array(els) - self.tilt_elev)))
        i = max(0, min(len(els) - 1, i + d))
        self.tilt_elev = els[i]
        self._show_frame()
        self._prefetch()

    def _tilt_elevs(self, frame):
        if frame is None:
            return []
        if frame.has_level2():
            k = (frame.uid, frame.l2_rev)
            t = self.engine._tilt_cache.get(k)
            if t is not None:
                return [x.elevation for x in t]
            return []
        return L3_TILT_ELEVS

    def _tilt_chosen(self, idx):
        els = self._tilt_elevs(self.current_frame())
        if 0 <= idx < len(els):
            self.tilt_elev = els[idx]
            self._show_frame()
            self._prefetch()

    def _fill_tilt_combo(self, frame):
        frame = frame or self.current_frame()
        labels = []
        if frame is not None and frame.has_level2():
            t = self.engine._tilt_cache.get((frame.uid, frame.l2_rev))
            if t:
                labels = [x.label for x in t]
        elif frame is not None:
            labels = [f"{e:.1f}° (L3)" for e in L3_TILT_ELEVS]
        self.tilt_combo.blockSignals(True)
        self.tilt_combo.clear()
        self.tilt_combo.addItems(labels)
        els = self._tilt_elevs(frame)
        if els:
            self.tilt_combo.setCurrentIndex(int(np.argmin(np.abs(np.array(els) - self.tilt_elev))))
        self.tilt_combo.blockSignals(False)

    # ================================================================== panels
    def set_active_panel(self, i):
        self.view.active_panel = max(0, min(i, len(self.view.panels) - 1))
        self.view.update()
        self.stateChanged.emit()

    def set_layout(self, n):
        self.settings["layout"] = n
        for a in self.layout_group.actions():
            a.setChecked(a.data() == n)
        self.view.set_layout(n, list(self.settings["panels"]))
        self._update_l3_needs()
        self._show_frame()

    def set_panel_product(self, i, pid):
        panels = list(self.settings["panels"])
        while len(panels) < 6:
            panels.append("REF")
        panels[i] = pid
        self.settings["panels"] = panels
        self.view.panels[i].product = pid
        self.view.panels[i].image = None
        self._update_l3_needs()
        self._show_frame()
        self._prefetch()

    def _update_l3_needs(self):
        ti = int(np.argmin(np.abs(np.array(L3_TILT_ELEVS) - self.tilt_elev)))
        codes = set()
        for p in self.view.panels:
            pd = catalog.get(p.product)
            if pd.kind in ("l3", "l3tilt"):
                codes.update(pd.l3_candidates(ti)[:1])
                if pd.kind == "l3tilt" and ti != 0:
                    codes.update(pd.l3_candidates(0)[:1])
        ov = self.settings["overlays"]
        for key, (code, _label) in catalog.L3_OVERLAYS.items():
            if ov.get(key):
                codes.add(code)
        cells = getattr(self, "cells_panel", None)
        if cells is not None and cells.isVisible():
            codes.update(CELL_CODES)
        self.data.set_l3_needed(codes)

    def _show_frame(self):
        frame = self.current_frame()
        self._shown_frame = frame
        self.l3ov.frame = frame
        if frame is not None:
            self.placefiles.frame_time = frame.time
            self.warnings.frame_time = frame.time
            if self.data.mode in ("archive", "local"):
                self.warnings.set_archive_time(frame.time)
            n = len(self.data.frames)
            live = " LIVE" if frame.live else ""
            self.time_label.setText(f" {frame.time:%Y-%m-%d %H:%M:%S}Z  [{self.frame_index + 1}/{n}]{live} ")
        else:
            self.time_label.setText(" no data ")
        for p in self.view.panels:
            self._request_panel(p, frame)
        self._fill_tilt_combo(frame)
        if self.xs_win is not None and self.xs_win.isVisible():
            self.xs_win.refresh()
        self.view.update()
        self.stateChanged.emit()

    def _request_panel(self, p, frame):
        pid = p.product
        pd = catalog.get(pid)
        p.palette = self.palette_for(pid)
        p.storage_units = pd.units
        if frame is None:
            p.image = None
            p.header = f"{self.data.site_id}  {pd.name}"
            p.message = "Waiting for data…" if self.data.mode == "live" else "No data loaded"
            return
        req = (frame.uid, frame.revision, pid, round(self.tilt_elev, 2), self.engine._sig(pid), id(p.palette))
        if self._panel_req.get(p.index) == req and p.image is not None:
            return
        self._panel_req[p.index] = req
        engine = self.engine
        frames = list(self.data.frames)

        def job():
            ti = self._tilt_index(frame) if pd.tilted else 0
            img = engine.image(frame, pid, ti)
            src_frame = frame
            if img is None and pd.tilted and frame.live and frame.has_level2():
                # newest volume hasn't reached this tilt yet: show the previous volume's
                idx = frames.index(frame) if frame in frames else -1
                if idx > 0:
                    prev = frames[idx - 1]
                    img = engine.image(prev, pid, self._tilt_index(prev))
                    src_frame = prev
            if img is not None:
                img.gpu_values()            # texture prep off the UI thread
            tilts = engine.tilts(frame) if frame.has_level2() else []
            return {"req": req, "img": img, "frame": src_frame, "ti": ti,
                    "tilt_label": tilts[ti].label if tilts and pd.tilted and ti < len(tilts) else ""}
        self.pool.start(_ImageJob(job, self.relay, p.index))

    def _image_ready(self, idx, res):
        if idx >= len(self.view.panels):
            return
        p = self.view.panels[idx]
        if "error" in res:
            p.message = "Error: " + res["error"][:80]
            self.view.update()
            return
        if self._panel_req.get(idx) != res["req"]:
            return
        pd = catalog.get(p.product)
        img = res["img"]
        p.image = img
        frame = res["frame"]
        site = frame.site if frame else self.data.site_id
        if img is not None:
            t = img.time or frame.time
            tl = img.label if img.source == "L3" else (res["tilt_label"] if pd.tilted else "")
            stale = "  (prev vol)" if frame is not self.current_frame() else ""
            p.header = f"{site}  {pd.name}  {tl}  {t:%H:%M:%S}Z{stale}"
            p.message = ""
        else:
            p.header = f"{site}  {pd.name}"
            if pd.kind in ("l3", "l3tilt"):
                ti = res.get("ti", 0)
                p.message = f"No Level III {pd.l3_code(ti)} for this time"
            elif not frame.has_level2():
                p.message = "No Level II volume for this frame"
            else:
                p.message = "Not available at this tilt"
        self._fill_tilt_combo(self.current_frame())
        self.view.update()
        self.stateChanged.emit()

    def _frame_ready(self, frame):
        for p in self.view.panels:
            pd = catalog.get(p.product)
            ti = 0
            if pd.tilted:
                if frame.has_level2():
                    t = self.engine._tilt_cache.get((frame.uid, frame.l2_rev))
                    if t is None:
                        return False
                    els = np.array([x.elevation for x in t])
                    ti = int(np.argmin(np.abs(els - self.tilt_elev))) if len(els) else 0
            if self.engine.cached(frame, p.product, ti) is None and (frame.has_level2() or frame.l3):
                return False
        return True

    def _prefetch(self):
        frames = list(self.data.frames)
        cur = self.current_frame()
        pids = [p.product for p in self.view.panels]
        engine = self.engine
        self._prefetch_gen += 1
        gen = self._prefetch_gen

        def work():
            for f in reversed(frames):
                if f is cur:
                    continue
                if gen != self._prefetch_gen or self.data.loading:
                    return                      # superseded, or new data is still arriving
                if self.data.frames and f not in self.data.frames:
                    return
                for pid in pids:
                    pd = catalog.get(pid)
                    ti = self._tilt_index(f) if pd.tilted else 0
                    engine.image(f, pid, ti)
        self.bg_pool.clear()
        self.bg_pool.start(_Bg(work))

    # ================================================================== panel menu
    def _panel_menu(self, idx, gpos):
        menu = QMenu(self)
        cur = self.view.panels[idx].product
        for cat in catalog.CATEGORIES:
            sub = menu.addMenu(cat)
            for p in catalog.PRODUCTS:
                if p.category != cat:
                    continue
                a = sub.addAction(p.name)
                a.setCheckable(True)
                a.setChecked(p.id == cur)
                a.triggered.connect(lambda _=False, pid=p.id: self.set_panel_product(idx, pid))
        menu.addSeparator()
        a = menu.addAction("Load colour table for this product…")
        a.triggered.connect(lambda: self._load_pal_for(cur))
        a = menu.addAction("Default colour table")
        a.triggered.connect(lambda: self._reset_pal_for(cur))
        menu.addSeparator()
        if self.view.cursor_world is not None:
            x, y = self.view.cursor_world
            lat, lon = self.view.world_to_latlon(x, y)
            ns = nearest_site(lat, lon)
            if ns is not None and ns.id != self.data.site_id:
                a = menu.addAction(f"Switch to nearest radar ({ns.id} – {ns.place})")
                a.triggered.connect(lambda: self.switch_site(ns.id, keep_view=True))
            a = menu.addAction("Centre here")
            a.triggered.connect(lambda: self.view.set_view(x, y, self.view.scale))
        menu.exec(gpos)

    def _load_pal_for(self, pid):
        path, _ = QFileDialog.getOpenFileName(self, "Colour table", "", "Colour tables (*.pal *.txt);;All (*)")
        if path:
            self.apply_color_table(path, pid)

    def _reset_pal_for(self, pid):
        self.settings["palette_overrides"].pop(pid, None)
        self.settings.save()
        self._panel_req.clear()
        self._show_frame()

    # ================================================================== cursor / readout
    def _cursor(self, x, y, panel):
        if panel < 0 or math.isnan(x):
            self.readout.setText("")
            for p in self.view.panels:
                p.readout = ""
            return
        lat, lon, dist, az = self.view.describe_point(x, y)
        du = self.settings["distance_units"]
        s_km = dist * UNIT_F[du]
        parts = [f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'} {abs(lon):.4f}°{'W' if lon < 0 else 'E'}",
                 f"{dist:.1f} {du} @ {az:03.0f}°"]
        for p in self.view.panels:
            p.readout = ""
            img = p.image
            if img is None or p.palette is None:
                continue
            v = img.sample(az, s_km)
            pd = catalog.get(p.product)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                txt = "—"
            elif math.isinf(v):
                txt = "RF"
            else:
                disp = v * p.palette.data_scale(pd.units) + p.palette.offset
                if pd.categorical and p.palette.labels:
                    txt = p.palette.labels.get(float(round(disp)), f"{disp:.0f}")
                else:
                    dec = pd.decimals if p.palette.units.upper() not in ("KTS", "KT", "MPH") else 0
                    txt = f"{disp:.{dec}f} {p.palette.units}".strip()
            p.readout = txt
            if p.index == panel and img.elevation and not img.ground_range:
                h_ft = (beam_height(slant_range(s_km, img.elevation), img.elevation)) * 3280.84
                parts.append(f"beam {h_ft:,.0f} ft ARL")
        cur = self.view.panels[panel].readout if panel < len(self.view.panels) else ""
        if cur:
            parts.append(f"{catalog.get(self.view.panels[panel].product).short}: {cur}")
        self.readout.setText("   |   ".join(parts))
        if self.inspector_panel.isVisible():
            beam = next((p for p in parts if p.startswith("beam")), "")
            loc = (f"<b>{parts[0]}</b><br>{parts[1]} from {self.data.site_id}" +
                   (f"<br>{beam}" if beam else ""))
            values = [(f"{p.index + 1}. {catalog.get(p.product).name}", p.readout or "—") for p in self.view.panels]
            under = None
            tol = 8.0 / self.view.scale
            for hp in (self.l3ov, self.warnings, self.placefiles):
                try:
                    under = hp.hover(x, y, tol)
                except Exception:
                    under = None
                if under:
                    break
            self.cursorInfo.emit({"loc_html": loc, "values": values, "under": under})

    # ================================================================== tools
    def set_tool(self, tool):
        self.view.set_tool(tool)
        for a in self.tool_group.actions():
            a.setChecked(a.data() == tool)
        if tool == "xsection":
            self._status_msg("Cross-section: drag a line across a storm (A → B)")
        elif tool == "box3d":
            self._status_msg("3-D: drag a box around the storm you want to render (Esc to cancel)")

    def _box_drawn(self, x0, y0, x1, y1):
        self.set_tool("pan")
        self.open_3d()
        self.v3d_host.ensure().set_box(x0, y0, x1, y1)

    def _line_drawn(self, tool, x0, y0, x1, y1):
        if tool == "xsection":
            self.open_xsection()
            self.xs_win.set_line(x0, y0, x1, y1)

    def open_xsection(self):
        self.show_panel("xsection")

    def _xsection_closed(self):
        self.view.clear_lines("xsection")
        if self.view.tool == "xsection":
            self.set_tool("pan")

    def open_3d(self):
        self.show_panel("3d")

    def edit_storm_motion(self):
        d = StormMotionDialog(self.settings, self.l3ov.mean_storm_motion, self)
        if d.exec():
            self._update_sm_label()
            self._show_frame()
            self.stateChanged.emit()

    def _update_sm_label(self):
        self.sm_act.setText(f"SM {self.settings['storm_motion_dir']:03.0f}°/{self.settings['storm_motion_kts']:.0f}kt")
        self.sm_act.setToolTip("Storm motion used for SRV (click to edit)")

    def show_storm_table(self):
        f = self.current_frame()
        prod = f.l3.get("NST") if f else None
        if prod is None or not prod.text_pages:
            QMessageBox.information(self, "Storm table", "No Level III storm-structure table (NST) for this frame. "
                                    "Enable Overlays → Storm tracks to download it.")
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"NST {prod.time:%H:%M:%S}Z")
        box.setText("<pre>" + "\n\n".join(prod.text_pages[:4]) + "</pre>")
        box.exec()

    def open_placefiles(self):
        self.show_panel("placefiles")

    def open_settings(self, page=None):
        d = SettingsDialog(self.settings, self, page if isinstance(page, str) else None)
        if d.exec():
            self._palettes.clear()
            self._apply_view_settings()
            self.legend_act.setChecked(bool(self.settings["show_legend"]))
            self.link_act.setChecked(bool(self.settings["cursor_link"]))
            self.data._live_timer.setInterval(max(5, int(self.settings["live_poll_seconds"])) * 1000)
            from ..data.frames import VOLUMES
            VOLUMES.capacity = int(self.settings["volume_cache"])
            self.smooth_act.blockSignals(True)
            self.smooth_act.setChecked(bool(self.settings["gpu_smooth"]))
            self.smooth_act.blockSignals(False)
            for a, lv in zip(self.vf_group.actions(), (0, 1, 2)):
                a.setChecked(lv == int(self.settings["velocity_filter"]))
            self._panel_req.clear()
            self._show_frame()
            self.warnings_panel.refresh()          # warning colours may have changed
            self.view.update()

    def save_image(self):
        f = self.current_frame()
        name = f"{self.data.site_id}_{f.time:%Y%m%d_%H%M%S}.png" if f else "radar.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save image", os.path.join(os.path.expanduser("~"), name),
                                              "PNG (*.png)")
        if path:
            self.view.grab_png(path)
            self._status_msg(f"Saved {path}")

    # ================================================================== toggles
    def _toggle_smooth(self, on):
        self.settings["gpu_smooth"] = on
        self.view.smooth = on
        self.view.update()

    def _toggle_legend(self, on):
        self.settings["show_legend"] = on
        self.view.show_legend = on
        self.view.update()

    def _toggle_cities(self, on):
        self.settings["map_layers"]["cities"] = on
        self.view.show_cities = on
        self.view.update()

    def _toggle_sites(self, on):
        self.settings["map_layers"]["radar_sites"] = on
        self.view.show_sites = on
        self.view.update()

    def _toggle_tdwr(self, on):
        self.settings["map_layers"]["tdwr_sites"] = on
        self.view.show_tdwr = on
        self.view.update()

    def _toggle_rings(self, on):
        self.settings["map_layers"]["range_rings"] = on
        self.view.show_range_rings = on
        self.view.update()

    def set_velocity_filter(self, level):
        self.settings["velocity_filter"] = level
        self.settings.save()
        for a, lv in zip(self.vf_group.actions(), (0, 1, 2)):
            a.setChecked(lv == level)
        self._panel_req.clear()
        self._show_frame()
        self._prefetch()

    def _toggle_link(self, on):
        self.settings["cursor_link"] = on
        self.view.link_cursor = on

    def _toggle_layer(self, name, on):
        self.settings["map_layers"][name] = on
        self.view.map_visible[name] = on
        self.view.update()

    def _toggle_overlay(self, key, on):
        self.settings["overlays"][key] = on
        self.settings.save()
        if key in ("warnings", "watches", "reports") and on:
            self.warnings.refresh(force=True)
        if key in ("warnings", "watches") and hasattr(self, "warnings_panel"):
            self.warnings_panel.sync_filters()
        self._update_l3_needs()
        self.view.update()

    # ================================================================== misc
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

    def show_shortcuts(self):
        rows = [
            ("Frames", [("← / →", "previous / next frame"), ("Space", "play / pause the loop"),
                        ("End", "latest frame")]),
            ("Tilts & layout", [("↑ / ↓", "tilt up / down"), ("Alt+1 … Alt+6", "1–6 panels")]),
            ("Mouse tools", [("P", "pan / zoom"), ("X", "cross section (drag a line)"), ("M", "measure"),
                             ("B", "3-D (drag a box around a storm)"), ("Esc", "back to pan / cancel"),
                             ("Shift+drag", "quick measure")]),
            ("Map", [("wheel / drag", "zoom / pan"), ("double-click", "centre here"), ("Home", "reset view"),
                     ("right-click", "product, colour table, nearest radar"), ("S", "smoothing on/off")]),
            ("Window", [("F9", "show / hide the side panel"), ("F11", "full screen"), ("F1", "this list")]),
            ("Files & data", [("Ctrl+O", "open files"), ("Ctrl+A", "archive"), ("Ctrl+R", "choose radar"),
                              ("Ctrl+P", "placefiles"), ("Ctrl+S", "save image"), ("Ctrl+,", "settings")]),
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
            "Data: NOAA NEXRAD on AWS (Unidata buckets), NWS API alerts, Iowa Environmental Mesonet archives.<br>"
            "Maps: US Census county boundaries, Natural Earth, GeoNames.<br>"
            "Level III decoding by MetPy.<br><br>"
            f"OpenGL: {self.view.gl_info}"))

    def _save_state(self):
        s = self.settings
        s["workspace"] = self.ws.state()
        s["view"] = {"cx": self.view.cx, "cy": self.view.cy, "scale": self.view.scale}
        s["window_geometry"] = bytes(self.saveGeometry()).hex()
        s["panels"] = [p.product for p in self.view.panels] + list(s["panels"])[len(self.view.panels):]
        s.save()

    def prepare_restart(self):
        """Save everything and stop background work before the app re-executes itself."""
        try:
            self._save_state()
            self.data.stop_live()
            from ..data.level2 import shutdown_pool
            shutdown_pool()
        except Exception as exc:
            print("restart cleanup:", exc)

    def closeEvent(self, ev):
        self._save_state()
        for f in list(self.ws.floats):          # floating panels go away with the main window
            f._closing = True
            f.hide()
        self.data.stop_live()
        from ..data.level2 import shutdown_pool
        shutdown_pool()                          # don't wait for decoder processes to finish
        super().closeEvent(ev)


def apply_dark_theme(app: QApplication):
    """Default theme before the main window exists (kept for older launch scripts)."""
    themes.apply_ui(app, themes.find(None))
