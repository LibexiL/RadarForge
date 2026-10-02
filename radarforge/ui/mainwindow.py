"""RadarForge main window."""
from __future__ import annotations

from PySide6.QtCore import QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMainWindow

from .. import themes
from ..config import APP_NAME
from ..overlays.chasers import ChasersOverlay
from ..overlays.level3 import Level3Overlay
from ..overlays.lightning import LightningOverlay
from ..overlays.placefile import PlacefileManager
from ..overlays.satellite import SatelliteLayer
from ..overlays.spc import SpcOverlay
from ..overlays.warnings import WarningsOverlay
from ..products.engine import ProductEngine
from ..render.glview import RadarView
from .datamanager import DataManager
from .window.jobs import _Relay
from .window.toolbars import ToolbarsMixin
from .window.menus import MenusMixin
from .window.docking import DockingMixin
from .window.appearance import AppearanceMixin
from .window.sources import SourcesMixin
from .window.timeline import TimelineMixin
from .window.rendering import RenderingMixin
from .window.tools import ToolsMixin
from .window.locations import LocationsMixin
from .window.layers import LayersMixin
from .window.exporting import ExportMixin
from .window.session import SessionMixin
from .workspace import Workspace


class MainWindow(ToolbarsMixin, MenusMixin, DockingMixin, AppearanceMixin, SourcesMixin, TimelineMixin,
                 RenderingMixin, ToolsMixin, LocationsMixin, LayersMixin, ExportMixin, SessionMixin, QMainWindow):
    """RadarForge main window.

    Each area of the interface lives in its own mixin under ui/window/; this class builds the window and
    owns the state they share."""

    stateChanged = Signal()          # frame / panel / tilt / product changed (side panels refresh)
    cursorInfo = Signal(object)      # dict for the cursor inspector
    locationsUpdated = Signal()      # saved locations or what's happening at them changed

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
        self._panel_done: dict = {}          # panel index -> the request it has finished drawing
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
        is_live = lambda: self.data.mode == "live"          # noqa: E731
        self.chasers = ChasersOverlay(settings, is_live, self)
        self.spc = SpcOverlay(settings, is_live, self)
        self.spc._view = self.view
        self.satellite = SatelliteLayer(settings, self)
        self.lightning = LightningOverlay(settings, self)
        self._init_locations()
        self.view.overlays = [self.satellite, self.lightning, self.spc, self.warnings, self.placefiles, self.l3ov,
                              self.chasers, self.my_location]
        self.view.underlays = [self.satellite, self.placefiles]
        self.view.hover_providers = [self.l3ov, self.chasers, self.placefiles, self.warnings, self.my_location, self.spc]
        for sig in (self.warnings.changed, self.placefiles.changed, self.chasers.changed, self.spc.changed,
                    self.satellite.changed, self.lightning.changed):
            sig.connect(self.view.update)
        for ov in (self.warnings, self.placefiles, self.chasers, self.spc, self.satellite, self.lightning):
            ov.status.connect(self._status_msg)
        # storm track tool
        self.view.track_minutes = int(settings["track_minutes"] or 60)
        self.view.track_time_fn = self._track_start_time
        self.view.track_default_fn = self._track_default

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


def apply_dark_theme(app: QApplication):
    """Default theme before the main window exists (kept for older launch scripts)."""
    themes.apply_ui(app, themes.find(None))
