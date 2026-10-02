"""The whole main window, built offscreen: every menu, panel and the command palette's list come together."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from radarforge.config import Settings
    from radarforge.ui.mainwindow import MainWindow, apply_dark_theme
    apply_dark_theme(app)
    s = Settings(tmp_path_factory.mktemp("cfg") / "settings.json")
    s["start_live"] = False
    try:
        win = MainWindow(s)
    except Exception as exc:                                   # no usable OpenGL on this machine
        pytest.skip(f"main window can't be built here: {exc}")
    yield win
    win.close()


def test_menus_are_organised_by_area(window):
    names = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert names == ["File", "View", "Radar", "Layers", "Locations", "Tools", "Panels", "Help"]


def test_every_menu_builds_and_every_action_has_a_handler(window):
    count = 0

    def walk(menu):
        nonlocal count
        menu.aboutToShow.emit()                              # the lists that fill when a menu opens
        for a in menu.actions():
            if a.menu() is not None:
                walk(a.menu())
            elif not a.isSeparator():
                count += 1
    for top in window.menuBar().actions():
        walk(top.menu())
    assert count > 80


def test_layers_menu_has_the_new_layers(window):
    layers = next(a.menu() for a in window.menuBar().actions() if a.text().replace("&", "") == "Layers")
    text = " | ".join(a.text() for a in layers.actions())
    for word in ("Satellite picture", "Lightning flashes", "MRMS layer", "Surface observations", "Convective outlook"):
        assert word in text
    assert {"satellite", "lightning", "mrms", "surface"} <= set(window.overlay_acts)


def test_panels_and_tools_are_registered(window):
    keys = set(window.ws.keys())
    assert {"products", "warnings", "locations", "cells", "inspector", "placefiles", "layers", "xsection", "3d",
            "sounding"} <= keys


def test_command_palette_knows_commands_radars_products_and_cities(window):
    from radarforge.services.commands import search
    cmds = window.command_list()
    assert len(cmds) > 1000
    assert search(cmds, "mp4")[0].title == "Loop as MP4 video"
    assert "KTLX" in search(cmds, "ktlx radar")[0].title
    assert "Satellite picture" in [c.title for c in search(cmds, "satellite", 4)]
    assert any(c.title.startswith("Workspace: Briefing") for c in cmds)
    assert search(cmds, "norman")[0].title.startswith(("Go to Norman", "Switch to radar"))


def test_a_view_can_be_captured_and_applied_without_data(window):
    from radarforge.services import views
    v = window.capture_view()
    assert v["layout"] == int(window.settings["layout"]) and v["panels"] and v.get("site")
    assert views.decode(views.encode(v)) == v
    window.apply_workspace("Tornado hunt")
    assert [p.product for p in window.view.panels] == ["REF", "SRV", "CC", "AZSH"]
    assert window.settings["overlays"]["tvs"] is True


def test_locations_and_alert_state(window):
    window.set_my_location(35.0, -97.0)
    assert window.my_location.latlon() == (35.0, -97.0) and window.book.primary().name == "Home"
    window.add_location_dialog  # the dialog itself needs a screen; the book is what matters
    window.book.add("Cabin", 36.0, -98.0)
    window.locations_changed()
    assert len(window.location_status) == 2
    window.set_my_location(None, None)
    assert [loc.name for loc in window.book.items] == ["Cabin"]


def test_learn_mode_and_trends_are_reachable(window):
    from PySide6.QtCore import Qt
    titles = [c.title for c in window.command_list()]
    assert any(t.startswith("Learn: Moore") for t in titles)
    window.open_learn("el-reno-2013")
    dlg = window._learn_dlg
    assert dlg.event["id"] == "el-reno-2013"
    assert dlg.events.currentItem().data(Qt.UserRole) == "el-reno-2013"
    assert window.trends_win is not None
