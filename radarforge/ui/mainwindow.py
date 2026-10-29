"""RadarForge main window."""
from __future__ import annotations

import math
import os
import numpy as np

from PySide6.QtCore import QEvent, QObject, QPointF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import themes
from ..config import APP_NAME
from ..data.sites import get_site, nearest_site
from ..features.chasers import ChasersOverlay
from ..features.l3overlay import Level3Overlay
from ..features.location import MyLocation
from ..features.placefile import PlacefileManager
from ..features.spc import SpcOverlay
from ..features.warnings import WarningsOverlay
from ..products import catalog, colortable
from ..products import trail as trail_mod
from ..products.engine import FULL_SWEEP, ProductEngine, fill_partial, image_coverage
from ..products.geometry import aeqd_forward, beam_height, slant_range
from ..render.glview import RadarView
from ..tools.volume3d import Volume3DWindow
from ..tools.xsection import CrossSectionWindow
from .datamanager import DataManager
from .dialogs import ArchiveDialog, PlacefilePanel, SiteDialog
from .settings_dialog import SettingsDialog
from . import icons
from .panels import CELL_CODES, CellsPanel, InspectorPanel, ProductsPanel, WarningsPanel
from .quick_panel import QuickPanel
from .workspace import Workspace
from .main_data import DataLayersMixin
from .main_export import ExportMixin
from .main_layers import LayersMixin
from .main_location import LocationMixin
from .main_menus import MenusMixin
from .main_models import ModelsMixin
from .main_tropical import PresetsMixin, WinterTropicalMixin
from .main_storm import StormToolsMixin
from .updates import UpdatesMixin

L3_TILT_ELEVS = list(catalog.L3_SLICE_ELEVS)
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
        finally:
            # PySide can hold on to a finished runnable for a while: don't let it keep the job's frames (and
            # with them whole volumes) alive
            self.fn = None
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
        finally:
            self.fn = None              # see _ImageJob.run


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


class MainWindow(MenusMixin, ModelsMixin, WinterTropicalMixin, PresetsMixin, LayersMixin, StormToolsMixin, LocationMixin,
                 ExportMixin, DataLayersMixin, UpdatesMixin, QMainWindow):
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
        self._panel_done: dict = {}          # panel -> request whose image is shown (loop export waits on it)
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
        self._prefetch_timer = QTimer(self)       # volumes decoded / Level III arriving: images made in a batch
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(300)
        self._prefetch_timer.timeout.connect(lambda: self._prefetch())
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(150)
        self._update_timer.timeout.connect(self._show_frame)
        self.relay = _Relay()
        self.relay.imageReady.connect(self._image_ready)
        self.xs_win = None
        self.v3d_host = None
        self._trails = trail_mod.TrailCache()

        # overlays
        self.warnings = WarningsOverlay(settings, self.view.maps.county_polygons, self)
        self.placefiles = PlacefileManager(settings, self)
        self.l3ov = Level3Overlay(settings)
        is_live = lambda: self.data.mode == "live"          # noqa: E731
        self.chasers = ChasersOverlay(settings, is_live, self)
        self.spc = SpcOverlay(settings, is_live, self)
        self.spc._view = self.view
        self.my_location = MyLocation(settings)
        self._notified = dict(settings["notified_warnings"] or {})
        self._init_data_layers()          # satellite, lightning, MRMS, obs, storm flags; sets the overlay lists
        self._init_models()               # the Models view (its toolbar pickers are built with the toolbar)
        for sig in (self.warnings.changed, self.placefiles.changed, self.chasers.changed, self.spc.changed):
            sig.connect(self.view.update)
        for ov in (self.warnings, self.placefiles, self.chasers, self.spc):
            ov.status.connect(self._status_msg)
        self.warnings.changed.connect(self._check_location_alerts)
        # storm track tool
        self.view.track_minutes = int(settings["track_minutes"] or 60)
        self.view.track_time_fn = self._track_start_time
        self.view.track_default_fn = self._track_default

        self._build_toolbar()
        self._build_timeline()
        self._build_menus()
        self._build_status()
        self._init_updates()             # after the status bar: the "Update to …" button lives there
        self._build_panels()
        self._build_warning_bar()        # the warning type toggles, right end of the toolbar
        self._build_search()             # the place search box, left of Quick in the menu bar's corner
        self._shortcuts()
        self._show_menu_checks()
        self.setAcceptDrops(True)

        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._play_step)
        self._age_timer = QTimer(self)
        self._age_timer.timeout.connect(self._update_data_age)
        self._age_timer.start(1000)

        # data signals
        self.data.framesChanged.connect(self._frames_changed)
        self.data.frameUpdated.connect(self._frame_updated)
        self.data.loadingChanged.connect(self._loading_changed)
        self.data.frameDecoded.connect(self._prefetch_soon)
        self.data.status.connect(self._status_msg)
        self.data.error.connect(lambda m: self._status_msg("⚠ " + m))
        self.data.progress.connect(self._progress)

        self.view.cursorMoved.connect(self._cursor)
        self.view.panelMenuRequested.connect(self._panel_menu)
        self.view.lineDrawn.connect(self._line_drawn)
        self.view.boxDrawn.connect(self._box_drawn)
        self.view.siteClicked.connect(lambda sid: self.switch_site(sid, keep_view=True))
        self.view.panelActivated.connect(lambda i: self.stateChanged.emit())
        self.view.trackChanged.connect(self._track_changed)

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
        WS_VERSION = 4          # 1.10: Quick panel; 1.20: Layers + Models, then Severe / Winter panels; one-time switch to the new default arrangement
        if int(settings["workspace_version"] or 0) < WS_VERSION or not self.ws.restore(settings["workspace"] or {}):
            self.ws.apply_default(self.width(), self.height())
            settings["workspace_version"] = WS_VERSION
            settings["ws_roomy"] = True
        elif not settings.get("ws_roomy"):
            # (1.25, once) the side panels got roomier: a side column left at the old 360 px default widens
            settings["ws_roomy"] = True
            QTimer.singleShot(0, self.ws.widen_side_column)
        self.lock_act.setChecked(self.ws.locked)
        self._sync_side_act()
        self.apply_theme(settings["theme"], save=False)
        from ..data.level2 import release_memory, warm_up
        QTimer.singleShot(1500, warm_up)          # start decoder processes before they're needed
        self._trim_timer = QTimer(self)           # hand freed memory back to the system now and then (Linux;
        self._trim_timer.timeout.connect(lambda: self.playing or release_memory())   # not mid-loop: can take ~50 ms)
        self._trim_timer.start(60_000)

    @property
    def v3d_win(self):
        return self.v3d_host.win if self.v3d_host is not None else None

    # ================================================================== UI build
    # ================================================================== side panel / workspace
    SIDE_PANELS = ("quick", "severe", "winter", "tropical", "products", "models", "warnings", "cells", "inspector",
                   "placefiles")

    def _build_panels(self):
        ws = self.ws
        self.products_panel = ProductsPanel(self)
        self.warnings_panel = WarningsPanel(self)
        self.cells_panel = CellsPanel(self)
        self.inspector_panel = InspectorPanel(self)
        self.placefile_panel = PlacefilePanel(self.placefiles, compact=True)
        self.quick_panel = QuickPanel(self)
        from .models_panel import ModelsPanel
        from .tropical_panel import TropicalPanel
        from .weather_panels import SeverePanel, WinterPanel
        self.models_panel = ModelsPanel(self)
        self.tropical_panel = TropicalPanel(self)
        self.severe_panel = SeverePanel(self)
        self.winter_panel = WinterPanel(self)
        for key, title, w in (("quick", "Layers", self.quick_panel),
                              ("severe", "Severe", self.severe_panel),
                              ("winter", "Winter", self.winter_panel),
                              ("products", "Products", self.products_panel),
                              ("models", "Models", self.models_panel),
                              ("tropical", "Tropical", self.tropical_panel),
                              ("warnings", "Warnings", self.warnings_panel),
                              ("cells", "Storm cells", self.cells_panel),
                              ("inspector", "Inspector", self.inspector_panel),
                              ("placefiles", "Placefiles", self.placefile_panel)):
            ws.register(key, title, w, "side")
        self.xs_win = CrossSectionWindow(self)
        self.xs_win.closed.connect(self._xsection_closed)
        ws.register("xsection", "Cross Section", self.xs_win, "tool")
        # the 3-D view has its own OpenGL widget: only create it when the panel is first opened
        self.v3d_host = _Lazy3D(self)
        ws.register("3d", "3D Volume", self.v3d_host, "tool", prefer=("right", 0.48))   # beside the map, big
        ws.panelClosed.connect(self._panel_closed)
        ws.sideToggled.connect(lambda _on: self._sync_side_act())
        ws.layoutChanged.connect(self._sync_side_act)
        ws.layoutChanged.connect(self._update_l3_needs)
        # the Quick panel's switch sits beside the side-panel switch in the menu bar corner (F8)
        qa = ws.action("quick")
        qa.setShortcut(QKeySequence("F8"))
        qa.setToolTip("Layers panel: the map's layers, and the Severe, Winter and Tropical panels (F8)")
        self.addAction(qa)
        self._iconize(qa, "quick")
        qb = QToolButton()
        qb.setDefaultAction(qa)
        qb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        qb.setAutoRaise(True)
        qb.setIconSize(QSize(16, 16))
        self.corner.layout().insertWidget(0, qb)
        self.quick_btn = qb
        # Panels menu
        pm = self.panels_menu
        pm.addAction(self.side_act)
        self._header(pm, "Side panels")
        for key in self.SIDE_PANELS:
            pm.addAction(ws.action(key))
        self._header(pm, "Tool panels")
        for key in ("xsection", "3d"):
            pm.addAction(ws.action(key))
        pm.addSeparator()
        self.lock_act = self._act(pm, "Lock panel layout", self._toggle_lock, None, checkable=True)
        self._act(pm, "Reset panel layout", self.reset_panel_layout, None)
        pm.addSeparator()
        tip = pm.addAction("Tip: drag a panel's tab – drop zones show where it will go")
        tip.setEnabled(False)

    # the warning-type toggles live at the right end of the main toolbar (they were a block in the Warnings panel)
    WBAR = (("TOR", "Tornado", "TOR", "TOR"), ("SVR", "Severe", "SVR", "SVR"), ("FFW", "Flood", "FFW", "FFW"),
            ("WIN", "Winter", "WIN", "WSW"), ("TRO", "Tropical", "TRO", "HUW"), ("OTH", "Other", "OTH", "SPS"),
            ("WAT", "Watches", "WAT", "TOA"))

    def _build_warning_bar(self):
        """One "Warnings" button at the right end of the main toolbar: its icon shows a dot per warning type
        that's on (in that type's line colour), its menu turns each type on or off. (The same switches are in
        the Warnings panel.)"""
        tb = self.main_tb
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        spacer.setMinimumWidth(8)
        self._wbar_spacer_act = tb.addWidget(spacer)
        self._wbar_chips = []                 # (none any more: _fit_toolbar has nothing to shorten here)
        self._wbar_filters = []
        btn = QToolButton()
        btn.setText("Warnings")
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setToolTip("Which warnings are drawn on the map and listed in the Warnings panel")
        menu = QMenu(btn)
        filters = getattr(self.warnings_panel, "filters", {})
        for key, full, _short, code in self.WBAR:
            b = filters.get(key)
            if b is None:
                continue
            act = menu.addAction(full)
            act.setCheckable(True)
            act.setChecked(b.isChecked())
            act.toggled.connect(lambda on, b=b: b.setChecked(on) if b.isChecked() != on else None)
            b.toggled.connect(lambda on, a=act: (a.setChecked(on) if a.isChecked() != on else None,
                                                 self._wbar_colours()))
            self._wbar_filters.append((key, full, code, b, act))
        menu.addSeparator()
        menu.addAction("All on", lambda: [b.setChecked(True) for _k, _f, _c, b, _a in self._wbar_filters])
        menu.addAction("Warnings panel…", lambda: self.show_panel("warnings"))
        btn.setMenu(menu)
        self._wbar_btn = btn
        self._wbar_dots = QLabel()
        self._wbar_dots.setToolTip(btn.toolTip())
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 2, 0)
        h.setSpacing(2)
        h.addWidget(self._wbar_dots)
        h.addWidget(btn)
        self._wbar_act = tb.addWidget(box)
        self._wbar = box
        self._wbar_colours()

    def _wbar_colours(self):
        """The Warnings button's icon: a dot per warning type, in its line colour when it's on, hollow when
        off; each menu item gets its colour dot too (Settings → Warnings can change the colours)."""
        from PySide6.QtGui import QPixmap, QPainter as _P, QIcon, QColor as _C, QPen as _Pen
        from ..features.warnings import line_style
        items = getattr(self, "_wbar_filters", [])
        if not items:
            return
        dpr = self.devicePixelRatioF()
        n = len(items)
        d, gap = 7, 3
        pm = QPixmap(int((n * (d + gap) - gap + 2) * dpr), int((d + 2) * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = _P(pm)
        p.setRenderHint(_P.Antialiasing)
        for i, (_key, _full, code, b, act) in enumerate(items):
            try:
                rgb = line_style(self.settings, code)[0]
            except Exception:
                rgb = (200, 200, 200)
            col = _C(*rgb[:3])
            x = 1 + i * (d + gap)
            if b.isChecked():
                p.setPen(Qt.NoPen)
                p.setBrush(col)
            else:
                p.setPen(_Pen(_C(150, 158, 175, 170), 1.2))
                p.setBrush(Qt.NoBrush)
            p.drawEllipse(x, 1, d, d)
            dot = QPixmap(int(12 * dpr), int(12 * dpr))
            dot.setDevicePixelRatio(dpr)
            dot.fill(Qt.transparent)
            q = _P(dot)
            q.setRenderHint(_P.Antialiasing)
            q.setPen(Qt.NoPen)
            q.setBrush(col)
            q.drawEllipse(2, 2, 8, 8)
            q.end()
            act.setIcon(QIcon(dot))
        p.end()
        btn = getattr(self, "_wbar_btn", None)
        if btn is not None:
            self._wbar_dots.setPixmap(pm)
            off = [full for _k, full, _c, b, _a in items if not b.isChecked()]
            btn.setText("Warnings" if not off else f"Warnings ({len(items) - len(off)}/{len(items)})")

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
        self.view.set_fonts(themes.map_fonts(theme))
        icons.clear_cache()
        for target, ic in self._icon_targets:
            target.setIcon(icons.icon(ic))
        self._update_layout_btn()
        self.play_act.setIcon(icons.icon("pause" if self.playing else "play"))
        for w in (self.ws, self.xs_win, self.v3d_host):
            if w is not None:
                w.update()
        return theme

    def show_gl_warning(self, text):
        self.gl_warn.setToolTip(text)
        self.gl_warn.setVisible(True)
        self._status_msg(text)

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
        self.view.maps_under_radar = bool(s["maps_under_radar"])
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
        self._site_label = (s.id, f"{s.id}  {s.place}")
        self.site_btn.setText(self._site_label[0] if getattr(self, "_tb_level", 0) >= 2 else self._site_label[1])
        self._fit_toolbar()
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
            self.tropical.set_replay(None)
            self.follow_latest = True
            self.warnings.set_live()
            self.data.start_live()
            self.chasers.refresh(force=True)
            self.spc.refresh(force=True)
            self._refresh_data_layers(force=True)
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
        self.tropical.set_replay(None)
        self.start_archive(d.site.text().strip().upper(), files, l3=d.l3.isChecked())

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
        self.engine.forget_frames(list(frames))
        from ..data.frames import VOLUMES
        VOLUMES.retain(f.l2_path for f in frames if f.l2_path)     # decoded volumes of frames that are gone
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
        cur = self.current_frame()
        if cur is not self._shown_frame:
            self._show_frame()
        elif cur is not None and not cur.has_level2():
            self._show_frame()          # Level III only so far: its Level II panels show the newest volume there is
        else:
            self._update_time_label()
        self._prefetch_soon()

    def _prefetch_soon(self):
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()

    def _frame_updated(self, frame):
        self.engine.forget_frames(list(self.data.frames))     # the volume's earlier revision isn't shown again
        if frame is self.current_frame() and not self._update_timer.isActive():
            self._update_timer.start()
        if not frame.live:
            self._prefetch_soon()                   # (e.g. its Level III product came in: make that image too)

    def _loading_changed(self, busy):
        if not busy:
            self._prefetch()
            from ..data.level2 import release_memory
            QTimer.singleShot(3000, release_memory)        # parsing a loop's worth of volumes frees a lot

    def _update_time_label(self):
        frame = self.current_frame()
        if frame is None:
            self.time_label.setText(" no data ")
            return
        n = len(self.data.frames)
        live = " LIVE" if frame.live else ""
        self.time_label.setText(f" {frame.time:%Y-%m-%d %H:%M:%S}Z  [{self.frame_index + 1}/{n}]{live} ")

    def goto_frame(self, i):
        if getattr(self, "view_mode", "radar") == "models":
            self.models_goto_index(i)
            return
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
        if getattr(self, "view_mode", "radar") == "models":
            self.models_step(d)
            return
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
            if self.view_mode != "models":
                self._prefetch()
            self.play_timer.start(int(1000 / max(0.5, float(self.settings["loop_fps"]))))
        else:
            self.play_timer.stop()

    def _play_step(self):
        if self.view_mode == "models":
            if self.models.busy_count() == 0 or getattr(self, "_wait", 0) >= 10:
                self._wait = 0
                self.models_step(1)
            else:
                self._wait = getattr(self, "_wait", 0) + 1
            return
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
            # the elevations once known (they outlive the decoded volume: a long loop plays from its images
            # without decoding its volumes again), else from the volume
            meta = self.engine.tilt_meta(frame)
            els = [el for el, _lab in meta] if meta is not None else [t.elevation for t in self.engine.tilts(frame)]
            if els:
                return int(np.argmin(np.abs(np.array(els) - self.tilt_elev)))
        return int(np.argmin(np.abs(np.array(L3_TILT_ELEVS) - self.tilt_elev)))

    def _l3_tilt_index(self, frame, pd) -> int:
        """Which Level III slice (see catalog.L3_SLICES) a tilted Level III panel shows on [frame]: of that
        product's slices the frame has, the one whose own elevation is nearest the chosen tilt (its header
        says which); before any has come in, the slice whose usual elevation is nearest."""
        best = None
        for k in pd.l3_slices():
            prod = ProductEngine.l3_product(frame, pd, k)
            r = getattr(prod, "radial", None)
            if r is None or r.ground_range:
                continue
            d = abs(float(r.elevation) - self.tilt_elev)
            if best is None or d < best[0] - 0.05:
                best = (d, k)
        return best[1] if best is not None else pd.l3_slice_for(self.tilt_elev)

    def _panel_ti(self, frame, pd) -> int:
        """The tilt index a panel's product uses on [frame]."""
        if not pd.tilted:
            return 0
        if pd.kind == "l3tilt":
            return self._l3_tilt_index(frame, pd)
        return self._tilt_index(frame)

    def set_tilt_elev(self, elev):
        """Show the tilt nearest elev (degrees); 0 = the lowest."""
        els = self._tilt_elevs(self.current_frame()) or [0.5]
        self.tilt_elev = min(els, key=lambda e: abs(e - elev))
        self._show_frame()
        self._prefetch()

    def reset_view(self):
        self.view.set_view(0, 0, max(0.3, self.view.panels[0].rect.width() / 500))

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

    def _tilt_list(self, frame):
        """[(elevation, label)] to choose from on [frame]. A live volume still being scanned has only its
        lower tilts so far: its list is the newest finished volume's (the radar scans those again), so any
        tilt can be chosen at any time (until the volume gets there, panels show it from the volume before)."""
        if frame is None:
            return []
        if not frame.has_level2():
            return [(e, f"{e:.1f}° (L3)") for e in L3_TILT_ELEVS]
        own = self.engine.tilt_meta(frame) or []
        if frame.live:
            frames = list(self.data.frames)
            if frame in frames:
                for f in reversed(frames[:frames.index(frame)]):
                    if f.has_level2() and not f.live:
                        done = self.engine.tilt_meta(f)
                        if done and len(done) >= len(own):
                            return done
                        break
        return own

    def _tilt_elevs(self, frame):
        return [el for el, _lab in self._tilt_list(frame)]

    def _tilt_reached(self, frame) -> bool:
        """Whether a live volume still being scanned has reached the chosen tilt (finished volumes: always)."""
        if frame is None or not frame.live or not frame.has_level2():
            return True
        meta = self.engine.tilt_meta(frame)
        els = [el for el, _lab in meta] if meta is not None else [t.elevation for t in self.engine.tilts(frame)]
        return bool(els) and min(abs(e - self.tilt_elev) for e in els) <= 0.25

    def _tilt_chosen(self, idx):
        els = self._tilt_elevs(self.current_frame())
        if 0 <= idx < len(els):
            self.tilt_elev = els[idx]
            self._show_frame()
            self._prefetch()

    def _fill_tilt_combo(self, frame):
        frame = frame or self.current_frame()
        lst = self._tilt_list(frame)
        labels = [lab for _el, lab in lst]
        if [self.tilt_combo.itemText(i) for i in range(self.tilt_combo.count())] == labels and labels:
            els = [el for el, _lab in lst]
            i = int(np.argmin(np.abs(np.array(els) - self.tilt_elev)))
            if self.tilt_combo.currentIndex() != i:
                self.tilt_combo.blockSignals(True)
                self.tilt_combo.setCurrentIndex(i)
                self.tilt_combo.blockSignals(False)
            return                                   # (unchanged: don't rebuild it on every live update)
        self.tilt_combo.blockSignals(True)
        self.tilt_combo.clear()
        self.tilt_combo.addItems(labels)
        els = [el for el, _lab in lst]
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
        self._update_layout_btn()
        self.view.set_layout(n, list(self.settings["panels"]))
        self._update_l3_needs()
        if getattr(self, "view_mode", "radar") == "models":
            self.models.pictures.clear()
            self.models.request_soon(0)
        self._show_frame()

    def set_panel_product(self, i, pid):
        panels = list(self.settings["panels"])
        while len(panels) < 6:
            panels.append("REF")
        panels[i] = pid
        self.settings["panels"] = panels
        self._sync_ptype_act()
        self.view.panels[i].product = pid
        self.view.panels[i].image = None
        self._update_l3_needs()
        self._show_frame()
        self._prefetch()

    def _update_l3_needs(self):
        # (runs whenever a frame is shown: the chosen tilt, the radar or its scan pattern may have changed)
        codes = set()
        for p in self.view.panels:
            pd = catalog.get(p.product)
            if pd.kind == "l3":
                codes.update(pd.l3_candidates(0)[:1])
            elif pd.kind == "l3tilt":
                for k in {pd.l3_slice_for(self.tilt_elev), 0}:     # the lowest too (most used, never missing)
                    codes.update(pd.l3_candidates(k)[:1])
        ov = self.settings["overlays"]
        for key, (code, _label) in catalog.L3_OVERLAYS.items():
            if ov.get(key):
                codes.add(code)
        cells = getattr(self, "cells_panel", None)
        if cells is not None and cells.isVisible():
            codes.update(CELL_CODES)
        self.data.set_l3_needed(codes)

    def _show_frame(self):
        if getattr(self, "view_mode", "radar") == "models":
            self._models_changed()
            return
        frame = self.current_frame()
        self._shown_frame = frame
        self._update_l3_needs()
        self.l3ov.frame = frame
        if frame is not None:
            self.placefiles.frame_time = frame.time
            self.warnings.frame_time = frame.time
            if self.data.mode in ("archive", "local"):
                self.warnings.set_archive_time(frame.time)       # (first: the mode decides the span fetched)
            frames = self.data.frames
            if frames:
                # the warnings of the loop's whole span: each scan then shows those of its own time
                self.warnings.set_span(frames[0].time, frames[-1].time, frames[-1].time)
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
        self._layers_frame_changed(frame)
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
        frames = list(self.data.frames)
        rule = trail_mod.rule_for(pid) if self.settings["trail_mode"] else None
        window = ()
        if rule is not None and frame in frames:
            window = tuple(frames[:frames.index(frame) + 1])      # Σ: every loaded frame up to this one
        req = (frame.uid, frame.revision, pid, round(self.tilt_elev, 2), self.engine._sig(pid), id(p.palette),
               tuple((f.uid, f.revision) for f in window))
        if self._panel_req.get(p.index) == req and p.image is not None:
            return
        self._panel_req[p.index] = req
        engine = self.engine
        trails = self._trails

        def job():
            ti = self._panel_ti(frame, pd)
            img = engine.image(frame, pid, ti)
            src_frame = frame
            if img is None and pd.kind in ("l3", "l3tilt") and frame in frames:
                # this volume's product hasn't come in yet (they follow each scan by a few minutes): show the
                # newest earlier one, marked "(earlier volume)", rather than an empty panel
                idx = frames.index(frame)
                for prev in reversed(frames[:idx]):
                    if (frame.time - prev.time).total_seconds() > 20 * 60:
                        break
                    pti = self._panel_ti(prev, pd)
                    if ProductEngine.l3_product(prev, pd, pti) is not None:
                        img = engine.image(prev, pid, pti)
                        if img is not None:
                            src_frame = prev
                            break
            if pd.tilted and pd.kind != "l3tilt" and frame.live and frame.has_level2() and \
                    (img is None or not self._tilt_reached(frame)):
                # the volume being scanned hasn't reached this tilt (or this moment) yet: show the previous
                # volume's, rather than an empty panel or a different tilt
                idx = frames.index(frame) if frame in frames else -1
                prev = next((f for f in reversed(frames[:idx]) if f.has_level2()), None) if idx > 0 else None
                if prev is not None:
                    pimg = engine.image(prev, pid, self._tilt_index(prev))
                    if pimg is not None or img is None:
                        img, src_frame = pimg, prev
            elif img is None and pd.kind not in ("l3", "l3tilt") and not frame.has_level2() and frame in frames:
                # a Level III product came in before its volume (a radar just chosen, its live volume still on
                # the way): show the newest volume there is until it arrives
                idx = frames.index(frame)
                prev = next((f for f in reversed(frames[:idx]) if f.has_level2()), None)
                if prev is not None and (frame.time - prev.time).total_seconds() <= 20 * 60:
                    img = engine.image(prev, pid, self._panel_ti(prev, pd))
                    src_frame = prev
            if img is not None and src_frame is frame and frame.live and pd.tilted and pd.kind != "l3tilt" \
                    and image_coverage(img) < FULL_SWEEP:
                # the sweep is still coming in: draw it over the same tilt of the volume before, so the picture
                # fills in like the beam going round instead of growing out of an empty panel
                idx = frames.index(frame) if frame in frames else -1
                prev = next((f for f in reversed(frames[:idx]) if f.has_level2()), None) if idx > 0 else None
                if prev is not None:
                    try:
                        img = fill_partial(img, engine.image(prev, pid, self._tilt_index(prev)))
                    except Exception:
                        pass
            if img is not None and len(window) > 1:
                tkey = (pid, round(self.tilt_elev, 2), engine._sig(pid), req[-1])
                cached = trails.get(tkey)
                if cached is None:
                    older = []
                    for f in window[:-1]:
                        try:
                            o = engine.image(f, pid, self._panel_ti(f, pd))
                        except Exception:
                            o = None
                        if o is not None:
                            older.append(o)
                    cached = trail_mod.combine(img, older, rule)
                    trails.put(tkey, cached)
                img = cached
            if img is not None:
                img.gpu_values()            # texture prep off the UI thread
            sti = self._panel_ti(src_frame, pd) if src_frame is not frame else ti
            labels = []
            if src_frame.has_level2() and pd.tilted:
                meta = engine.tilt_meta(src_frame)     # (never decodes a volume just for its tilt's name)
                labels = [lab for _el, lab in meta] if meta is not None else \
                    [t.label for t in engine.tilts(src_frame)]
            return {"req": req, "img": img, "frame": src_frame, "ti": ti,
                    "tilt_label": labels[sti] if sti < len(labels) else ""}
        self.pool.start(_ImageJob(job, self.relay, p.index))

    def _image_ready(self, idx, res):
        if idx >= len(self.view.panels):
            return
        p = self.view.panels[idx]
        if "error" in res:
            self._panel_done[idx] = self._panel_req.get(idx)
            p.message = "Error: " + res["error"][:80]
            self.view.update()
            return
        img = res["img"]
        if self._panel_req.get(idx) != res["req"]:
            if img is not None and img is not p.image:
                img.extra.pop("_gpu", None)          # never shown: don't keep its texture copy in the cache
            return
        self._panel_done[idx] = res["req"]
        pd = catalog.get(p.product)
        old = p.image
        if old is not None and old is not img:
            old.extra.pop("_gpu", None)              # replaced before it was drawn (fast loop): same
        p.image = img
        frame = res["frame"]
        site = frame.site if frame else self.data.site_id
        if img is not None:
            t = img.time or frame.time
            tl = img.label if img.source == "L3" else (res["tilt_label"] if pd.tilted else "")
            cur = self.current_frame()
            stale = "  (prev vol)" if frame is not cur and img.source != "L3" else ""
            tags = ""
            ref = cur if cur is not None else frame
            if img.source == "L3" and ref is not None and img.time is not None and \
                    (ref.time - img.time).total_seconds() > 150:
                tags += "  (earlier volume)"          # this volume's own product hasn't come in yet
            if img.extra.get("filled"):
                tags += "  (scanning)"                # new radials over the volume before
            if img.extra.get("dealiased") and p.product in ("VEL", "L3G", "L3S"):
                tags += "  dealiased"
            if img.extra.get("ptype"):
                tags += "  · " + img.extra['ptype'].replace(" analysis", "")
            if img.extra.get("trail"):
                tags += f"  Σ {'min' if trail_mod.rule_for(p.product) == 'min' else 'max'} of {img.extra['trail']} frames"
            p.header = f"{site}  {pd.name}  {tl}  {t:%H:%M:%S}Z{stale}{tags}"
            p.message = ""
        else:
            p.header = f"{site}  {pd.name}"
            if pd.kind in ("l3", "l3tilt"):
                code = pd.l3_code(res.get("ti", 0))
                p.message = (f"Waiting for Level III {code} (it follows each scan by a few minutes)"
                             if self.data.mode == "live" else f"No Level III {code} for this time")
            elif not frame.has_level2():
                p.message = "No Level II volume for this frame"
            else:
                p.message = "Not available at this tilt"
        self._fill_tilt_combo(self.current_frame())
        self.view.update()
        self.stateChanged.emit()

    def _frame_ready(self, frame):
        """Every panel's image for [frame] is made (or known not to exist): the loop can show it at once."""
        for p in self.view.panels:
            pd = catalog.get(p.product)
            ti = 0
            if pd.kind == "l3tilt":
                ti = self._l3_tilt_index(frame, pd)
            elif pd.tilted:
                if frame.has_level2():
                    t = self.engine.tilt_meta(frame)
                    if t is None:
                        return False
                    els = np.array([el for el, _lab in t])
                    ti = int(np.argmin(np.abs(els - self.tilt_elev))) if len(els) else 0
            if (frame.has_level2() or frame.l3) and not self.engine.known(frame, p.product, ti):
                return False
        return True

    def _prefetch(self):
        """Makes every panel's image for the other frames of the loop in the background (newest first), so the
        loop plays without waiting. While volumes are still downloading only the ones already decoded are done
        (they are decoded as they arrive; frameDecoded brings this back for the next)."""
        frames = list(self.data.frames)
        cur = self.current_frame()
        pids = [p.product for p in self.view.panels]
        engine = self.engine
        self._prefetch_gen += 1
        gen = self._prefetch_gen
        loading = self.data.loading

        def superseded():
            return gen != self._prefetch_gen

        def work():
            from ..data.level2 import DecodeCancelled, decode_priority
            with decode_priority(5, cancelled=superseded):     # behind what's on screen, before read-ahead
                for f in reversed(frames):
                    if f is cur:
                        continue
                    if superseded():
                        return
                    if self.data.frames and f not in self.data.frames:
                        return
                    if loading and f.has_level2() and f.level2_if_ready() is None:
                        continue                # not decoded yet: comes back when it is
                    for pid in pids:
                        pd = catalog.get(pid)
                        try:
                            engine.image(f, pid, self._panel_ti(f, pd))
                        except DecodeCancelled:
                            return
                        except Exception:
                            break
        self.bg_pool.clear()
        self.bg_pool.start(_Bg(work))

    # ================================================================== panel menu
    def _panel_menu(self, idx, gpos):
        """Right-click on a panel: its product and colour table, then things to do at that spot."""
        if self.view_mode == "models":
            self._model_panel_menu(idx, gpos)
            return
        menu = QMenu(self)
        cur = self.view.panels[idx].product

        def act(m, text, fn, icon=None):
            a = m.addAction(text)
            if icon:
                a.setIcon(icons.icon(icon))
            a.triggered.connect(fn)
            return a
        self._header(menu, f"Panel {idx + 1}: {catalog.get(cur).name}")
        for cat in catalog.CATEGORIES:
            sub = menu.addMenu(cat)
            for p in catalog.PRODUCTS:
                if p.category != cat:
                    continue
                a = sub.addAction(p.name)
                a.setCheckable(True)
                a.setChecked(p.id == cur)
                a.triggered.connect(lambda _=False, pid=p.id: self.set_panel_product(idx, pid))
        ct = menu.addMenu("Colour table")
        ct.setIcon(icons.icon("palette"))
        act(ct, "Load colour table for this product…", lambda: self._load_pal_for(cur))
        act(ct, "Default colour table", lambda: self._reset_pal_for(cur))
        if self.view.cursor_world is not None:
            x, y = self.view.cursor_world
            lat, lon = self.view.world_to_latlon(x, y)
            self._header(menu, f"Here: {abs(lat):.3f}°{'N' if lat >= 0 else 'S'} {abs(lon):.3f}°{'W' if lon < 0 else 'E'}")
            act(menu, "Model sounding here…", lambda: self.open_sounding(lat, lon), "sounding")
            if self.data.frames:
                act(menu, "Rotation history for this storm…", lambda: self.open_rotation_history(x, y), "chart")
                if self._follow is None:
                    act(menu, "Follow this storm", lambda: self.start_follow(x, y), "target")
            if self._follow is not None:
                act(menu, "Stop following the storm", self.stop_follow, "target")
            from .info_panel import item_title
            here = self.info_items_at(x, y, inside=True)
            for i, it in enumerate(here[:6]):
                act(menu, f"Details: {item_title(it)}…", lambda _=False, i=i: self.show_info(here, gpos, i), "flag")
            act(menu, "Centre here", lambda: self.view.set_view(x, y, self.view.scale))
            ns = nearest_site(lat, lon)
            if ns is not None and ns.id != self.data.site_id:
                act(menu, f"Switch to nearest radar ({ns.id} – {ns.place})",
                    lambda: self.switch_site(ns.id, keep_view=True), "radar")
            self._header(menu, "Location")
            act(menu, "Set my location here", lambda: self.set_my_location(lat, lon), "pin")
            act(menu, "Save this location…", lambda: self.save_location_here(lat, lon), "star")
        if self.my_location.latlon() is not None:
            act(menu, "Remove my location", lambda: self.set_my_location(None, None))
        if self.view.track is not None:
            menu.addSeparator()
            tm = menu.addMenu("Storm track")
            tm.setIcon(icons.icon("track"))
            act(tm, "Use for SRV storm motion", self.use_track_for_srv)
            act(tm, "Reset to the storm motion", lambda: self.view.set_track(self.view.track["a"]))
            lm = tm.addMenu("Track length")
            for mins in (30, 60, 90, 120):
                a = lm.addAction(f"{mins} minutes")
                a.setCheckable(True)
                a.setChecked(self.view.track_minutes == mins)
                a.triggered.connect(lambda _=False, mm=mins: self.set_track_minutes(mm))
            act(tm, "Clear the storm track", self.view.clear_track)
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
        if self.view_mode == "models":
            lat, lon = self.view.world_to_latlon(x, y)
            parts = [f"{abs(lat):.3f}°{'N' if lat >= 0 else 'S'} {abs(lon):.3f}°{'W' if lon < 0 else 'E'}"]
            for p in self.view.panels:
                p.readout = self.models.value_text(p.index, x, y)
            cur = self.view.panels[panel]
            if cur.readout:
                parts.append(f"{self.models.header(panel).split('  F')[0]}: {cur.readout}")
            self.readout.setText("   |   ".join(parts))
            return
        lat, lon, dist, az = self.view.describe_point(x, y)
        du = self.settings["distance_units"]
        s_km = dist * UNIT_F[du]
        parts = [f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'} {abs(lon):.4f}°{'W' if lon < 0 else 'E'}",
                 f"{dist:.1f} {du} @ {az:03.0f}°"]
        raw, beam_ft = {}, None
        for p in self.view.panels:
            p.readout = ""
            img = p.image
            if img is None or p.palette is None:
                continue
            v = img.sample(az, s_km)
            if v is not None and p.product not in raw:
                raw[p.product] = v
            pd = catalog.get(p.product)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                txt = "—"
            elif math.isinf(v):
                txt = "RF"
            else:
                disp = v * p.palette.data_scale(pd.units) + p.palette.offset
                if p.product == "PTYPE":
                    from ..features.winter import TYPE_NAMES
                    txt = f"{disp % 100:.0f} dBZ {TYPE_NAMES[int(disp // 100) % 4]}"
                elif pd.categorical and p.palette.labels:
                    txt = p.palette.labels.get(float(round(disp)), f"{disp:.0f}")
                else:
                    dec = pd.decimals if p.palette.units.upper() not in ("KTS", "KT", "MPH") else 0
                    txt = f"{disp:.{dec}f} {p.palette.units}".strip()
            p.readout = txt
            if p.index == panel and img.elevation and not img.ground_range:
                h_ft = (beam_height(slant_range(s_km, img.elevation), img.elevation)) * 3280.84
                beam_ft = float(h_ft)
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
            active = self.view.panels[panel].product if panel < len(self.view.panels) else ""
            self.cursorInfo.emit({"loc_html": loc, "values": values, "under": under,
                                  "learn": self._learn_notes(raw, beam_ft, active)})

    # ================================================================== tools
    # ---------------------------------------------------------------- storm track
    def open_placefiles(self):
        self.show_panel("placefiles")

    def open_settings(self, page=None):
        s = self.settings
        keys = ("dealias_velocity", "trail_mode", "satellite_channel", "satellite_enhance", "satellite_opacity",
                "lightning_minutes", "mrms_product", "mrms_opacity", "warn_at_location", "loop_frames")
        before = {k: s[k] for k in keys}
        d = SettingsDialog(self.settings, self, page if isinstance(page, str) else None)
        if d.exec():
            changed = {k for k in keys if s[k] != before[k]}
            for act, key in ((self.dealias_act, "dealias_velocity"), (self.trail_act, "trail_mode"),
                             (self.sat_enhance_act, "satellite_enhance"), (self.warn_loc_act, "warn_at_location")):
                if key in changed:
                    s[key] = before[key]
                    act.setChecked(not before[key])            # runs the usual toggle (saves, redraws)
            if "satellite_channel" in changed:
                self.set_satellite_channel(s["satellite_channel"])
            if "lightning_minutes" in changed:
                self.set_lightning_minutes(s["lightning_minutes"])
            if "mrms_product" in changed:
                self.set_mrms_product(s["mrms_product"])
            for which in ("satellite", "mrms"):
                if f"{which}_opacity" in changed:
                    self._set_opacity(which, s[f"{which}_opacity"])
            self._palettes.clear()
            self._wbar_colours()
            self._apply_view_settings()
            self.legend_act.setChecked(bool(self.settings["show_legend"]))
            self.link_act.setChecked(bool(self.settings["cursor_link"]))
            self.data._live_timer.setInterval(max(5, int(self.settings["live_poll_seconds"])) * 1000)
            from ..data.frames import VOLUMES, volume_capacity
            VOLUMES.capacity = volume_capacity(self.settings)
            if "loop_frames" in changed and self.data.mode == "live":
                self.data.loop_length_changed()
            self.smooth_act.blockSignals(True)
            self.smooth_act.setChecked(bool(self.settings["gpu_smooth"]))
            self.smooth_act.blockSignals(False)
            for a, lv in zip(self.vf_group.actions(), (0, 1, 2)):
                a.setChecked(lv == int(self.settings["velocity_filter"]))
            self.hover_act.blockSignals(True)
            self.hover_act.setChecked(bool(self.settings["hover_text"]))
            self.hover_act.blockSignals(False)
            self.maps_under_act.blockSignals(True)
            self.maps_under_act.setChecked(bool(self.settings["maps_under_radar"]))
            self.maps_under_act.blockSignals(False)
            self._update_loop_buttons()
            self._panel_req.clear()
            self._show_frame()
            self.warnings_panel.refresh()          # warning colours may have changed
            self.view.update()

    # ---------------------------------------------------------------- favourites
    # ---------------------------------------------------------------- my location
    # ---------------------------------------------------------------- report / chaser options
    # ================================================================== toggles
    # ================================================================== misc
    def _update_data_age(self):
        """Status bar badge: how fresh the data on screen is."""
        from datetime import datetime, timezone
        mode = self.data.mode
        frames = self.data.frames
        if mode == "live" and frames:
            newest = frames[-1]
            age = (datetime.now(timezone.utc) - newest.time).total_seconds()
            if newest.live:
                txt, col = "● LIVE  scanning now", "#46d27a"
            else:
                mins = age / 60.0
                when = f"{int(age)} s" if age < 90 else f"{mins:.0f} min"
                col = "#46d27a" if mins < 10 else "#ffb347" if mins < 20 else "#ff5c5c"
                txt = f"● LIVE  newest {newest.time:%H:%MZ} · {when} ago"
        elif mode == "live":
            txt, col = "● LIVE  connecting…", "#ffb347"
        elif mode == "archive" and frames:
            txt, col = f"ARCHIVE  {frames[0].time:%Y-%m-%d}", "#8d909b"
        elif mode == "local" and frames:
            txt, col = "FILES", "#8d909b"
        else:
            txt, col = "", "#8d909b"
        if mode == "live" and txt and getattr(self.data, "_poller", None) is not None:
            txt += f"  ({self.data.source_name})"          # from a polling server, not AWS
        if self.data_lbl.text() != txt:
            self.data_lbl.setText(txt)
            self.data_lbl.setStyleSheet(f"color:{col}; font-weight:bold; padding: 0 6px;")
        site = get_site(self.data.site_id)
        where = f"{site.id} {site.place}, {site.state}" if site else self.data.site_id
        kind = {"live": "Live", "archive": "Archive", "local": "Files"}.get(mode, "")
        title = f"{APP_NAME} – {where}" + (f" · {kind}" if kind else "")
        if self.windowTitle() != title:
            self.setWindowTitle(title)

    def _status_msg(self, msg):
        self.status_lbl.setText(msg[:400])

    def _progress(self, done, total):
        busy = 0 < total and done < total
        if self.prog.isVisible() != busy:
            self.prog.setVisible(busy)
        if busy:
            if self.prog.maximum() != total:
                self.prog.setRange(0, total)
            self.prog.setValue(done)
            self.prog.setFormat(f"{done}/{total}")

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
        if self._update_blocks_close():          # install.sh still running: the user chose to stay
            ev.ignore()
            return
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
