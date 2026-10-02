"""Side panels and the movable-panel workspace."""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...tools.sounding import SoundingWindow
from ...tools.volume3d import Volume3DWindow
from ...tools.xsection import CrossSectionWindow
from ..dialogs import PlacefilePanel
from ..panels import CellsPanel, InspectorPanel, LayersPanel, LocationsPanel, ProductsPanel, WarningsPanel


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


class DockingMixin:
    """Side panels and the movable-panel workspace."""

    SIDE_PANELS = ("products", "warnings", "locations", "cells", "inspector", "placefiles", "layers")

    def _build_panels(self):
        ws = self.ws
        self.products_panel = ProductsPanel(self)
        self.warnings_panel = WarningsPanel(self)
        self.cells_panel = CellsPanel(self)
        self.inspector_panel = InspectorPanel(self)
        self.placefile_panel = PlacefilePanel(self.placefiles, compact=True)
        self.layers_panel = LayersPanel(self)
        self.locations_panel = LocationsPanel(self)
        for key, title, w in (("products", "Products", self.products_panel),
                              ("warnings", "Warnings", self.warnings_panel),
                              ("locations", "Locations", self.locations_panel),
                              ("cells", "Storm cells", self.cells_panel),
                              ("inspector", "Inspector", self.inspector_panel),
                              ("placefiles", "Placefiles", self.placefile_panel),
                              ("layers", "Layers", self.layers_panel)):
            ws.register(key, title, w, "side")
        self.xs_win = CrossSectionWindow(self)
        self.xs_win.closed.connect(self._xsection_closed)
        ws.register("xsection", "Cross Section", self.xs_win, "tool")
        self.sounding_win = SoundingWindow(self)               # the plot library loads when the first sounding does
        ws.register("sounding", "Sounding", self.sounding_win, "tool")
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
        for key in ("xsection", "3d", "sounding"):
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
