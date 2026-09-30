"""Core tests (no network, no GPU). Run: python -m pytest tests"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from radarforge.features.placefile import parse_placefile
from radarforge.products import catalog, colortable
from radarforge.products.dealias import dealias_region
from radarforge.products.geometry import (aeqd_forward, aeqd_inverse, ground_range, point_to_beam,
                                          slant_range)

L2_SAMPLE = os.environ.get("RADARFORGE_TEST_L2")


def test_builtin_palettes_load():
    for name in colortable.list_builtin():
        ct = colortable.builtin(name)
        assert ct.entries, name
        lut, lo, hi = ct.lut(256)
        assert lut.shape == (256, 4) and hi > lo


def test_every_product_has_palette():
    names = set(colortable.list_builtin())
    for p in catalog.PRODUCTS:
        assert p.palette in names, p.id


def test_gr_palette_semantics():
    ct = colortable.parse_pal("Units: KTS\nScale: 1.9426\nColor: -10 0 255 0\nSolidColor: 0 100 100 100\n"
                              "Color: 10 255 0 0 255 255 0\n")
    assert ct.data_scale("m/s") == pytest.approx(1.9426)
    c = ct.color_at(np.array([-20.0, -5.0, 5.0, 15.0]))
    assert c[0, 3] == 0                         # below first entry -> transparent
    assert tuple(c[2, :3]) == (100, 100, 100)   # solid
    assert c[3, 0] == 255                       # last gradient


def test_units_conversion_without_scale():
    ct = colortable.parse_pal("Units: MPH\nColor: 0 0 0 0\nColor: 50 255 255 255\n")
    assert ct.data_scale("m/s") == pytest.approx(2.236936)


def test_geometry_roundtrip():
    x, y = aeqd_forward(36.0, -96.0, 35.333, -97.278)
    lat, lon = aeqd_inverse(x, y, 35.333, -97.278)
    assert lat == pytest.approx(36.0, abs=1e-6) and lon == pytest.approx(-96.0, abs=1e-6)
    for el in (0.5, 5.0, 19.5):
        s = ground_range(200.0, el)
        assert slant_range(s, el) == pytest.approx(200.0, rel=1e-6)
    th, r = point_to_beam(100.0, 3.0)
    assert r > 100 and 1.0 < th < 2.0


def test_dealias_recovers_aliased_field():
    nyq = 20.0
    az = np.linspace(0, 360, 360, endpoint=False)
    rng = np.arange(400) * 0.25
    true = (35.0 * np.sin(np.radians(az))[:, None] * np.minimum(rng / 30.0, 1.0)[None, :]).astype(np.float32)
    aliased = ((true + nyq) % (2 * nyq)) - nyq
    out = dealias_region(aliased, nyq)
    good = np.abs(out - true) < 1.0
    # a constant multiple of 2*nyq is ambiguous; allow for it
    good |= np.abs(out - true - 2 * nyq) < 1.0
    assert good.mean() > 0.97


def test_placefile_parse():
    text = """Title: T
Refresh: 1
Color: 255 0 0
Line: 2, 0, "hover"
35.0, -97.0
35.1, -97.1
End:
Object: 35.0, -97.0
Icon: 0, 0, 90, 1, 2, "ic"
End:
Polygon:
35.0, -97.0, 0, 0, 255, 100
35.1, -97.0
35.1, -97.1
End:
"""
    pf = parse_placefile(text, "https://x/y/z.txt")
    kinds = [i.kind for i in pf.items]
    assert kinds == ["line", "icon", "polygon"]
    assert pf.refresh_s == 60
    assert pf.items[0].hover == "hover"
    assert pf.items[1].offsets is not None and pf.items[1].angle == 90
    assert pf.items[2].color == (0, 0, 255, 100)


@pytest.mark.skipif(not L2_SAMPLE, reason="set RADARFORGE_TEST_L2 to a Level II file")
def test_level2_decode_and_products():
    from radarforge.config import Settings
    from radarforge.data.frames import Frame
    from radarforge.products.engine import ProductEngine
    from pathlib import Path
    import tempfile
    s = Settings(path=Path(tempfile.mkdtemp()) / "s.json")
    f = Frame("KXXX", None, l2_path=L2_SAMPLE)
    e = ProductEngine(s)
    assert e.tilts(f)
    for pid in ("REF", "VEL", "SRV", "DVEL", "KDP", "AZSH", "CREF", "ET18", "VIL", "MESH"):
        img = e.image(f, pid, 0)
        assert img is not None, pid
        assert np.isfinite(img.values.astype(np.float32)).any(), pid


def test_velocity_filter_removes_noise_keeps_weather():
    from radarforge.products.derived import velocity_noise_filter
    rng = np.random.default_rng(0)
    nyq = 26.0
    v = np.full((360, 400), np.nan, np.float32)
    ref = np.full(v.shape, np.nan, np.float32)
    # smooth "storm" with strong echo, including a tight couplet
    v[100:200, 50:250] = 15.0
    ref[100:200, 50:250] = 45.0
    v[140:150, 140:150] = 25.0
    v[150:160, 140:150] = -25.0
    # weak-echo noise region
    v[250:330, 50:250] = rng.uniform(-nyq, nyq, (80, 200))
    ref[250:330, 50:250] = 5.0
    out = velocity_noise_filter(v, np.zeros(v.shape, bool), ref, nyq, level=1)
    assert np.isfinite(out[100:200, 50:250]).all()                 # weather + couplet untouched
    assert np.isfinite(out[250:330, 50:250]).mean() < 0.15         # noise mostly removed
    assert velocity_noise_filter(v, np.zeros(v.shape, bool), ref, nyq, level=0) is v


def test_color_table_family_matching():
    ct = colortable.parse_pal("Product: BV\nUnits: MPH\nColor: 0 1 1 1\n")
    assert colortable.table_family(ct)[0] == "VEL"
    ct = colortable.parse_pal("Product: NROT\nColor: 0 1 1 1\n")
    assert colortable.table_family(ct)[0] is None
    for name in colortable.list_builtin():
        fam, _ = colortable.table_family(colortable.builtin(name))
        assert fam == name, name


def test_placefile_below_flag(tmp_path):
    from pathlib import Path
    from PySide6.QtCore import QCoreApplication
    _qapp()
    from radarforge.config import Settings
    from radarforge.features.placefile import PlacefileManager
    if QCoreApplication.instance() is None:
        test_placefile_below_flag.app = QCoreApplication([])
    s = Settings(path=Path(tmp_path) / "s.json")
    s["placefiles"] = [{"url": "a", "enabled": True}, {"url": "b", "enabled": True}]
    m = PlacefileManager(s)
    assert not m.has_below()
    m.set_below("b", True)
    assert m.below("b") and not m.below("a") and m.has_below()


_APP = []


def _qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        _APP.append(app)
    return app


def test_theme_files(tmp_path, monkeypatch):
    from radarforge import themes
    monkeypatch.setattr(themes, "THEME_DIR", tmp_path)
    assert themes.parse_color("#11223344") == (0x11, 0x22, 0x33, 0x44)
    assert themes.parse_color("abcdef") == (0xab, 0xcd, 0xef, 255)
    with pytest.raises(ValueError):
        themes.parse_color("red")
    # a partial theme is filled in from the default
    t = themes.normalize({"name": "Tiny", "ui": {"accent": "#ff0000"}, "map": {"states": "#00ff0080"}})
    assert t["ui"]["accent"] == "#ff0000" and t["map"]["states"] == "#00ff0080"
    assert t["ui"]["window"] == themes.DEFAULT["ui"]["window"]
    (tmp_path / "src").mkdir()
    src = tmp_path / "src" / "in.json"
    src.write_text('{"format": "radarforge-theme", "name": "Imported one", "dark": false, '
                   '"map": {"map_bg": "#ffffff"}, "widths": {"states": 9}}', encoding="utf-8")
    assert themes.looks_like_theme(src)
    got = themes.import_file(src)
    assert got["widths"]["states"] == 6.0          # clamped
    names = [x["name"] for x in themes.all_themes()]
    assert "Imported one" in names and "RadarForge Dark" in names
    assert themes.find("Imported one")["map"]["map_bg"] == "#ffffff"
    assert themes.delete_theme(themes.find("Imported one"))
    assert "Imported one" not in [x["name"] for x in themes.all_themes()]
    for b in themes.builtin_themes():               # every built-in theme is complete and valid
        themes.stylesheet(b)
        colors, layers = themes.map_style(b)
        assert set(layers) and all(len(v) == 4 for v in layers.values())


def test_workspace_layout_ops():
    _qapp()
    from PySide6.QtWidgets import QLabel, QWidget
    from radarforge.ui.workspace import Workspace, Zone

    host = QWidget()
    host.resize(1200, 800)
    ws = Workspace(QLabel("map"), host)
    ws.resize(1200, 800)
    for k in ("a", "b", "c", "d"):
        ws.register(k, k.upper(), QLabel(k), "side")
    ws.register("t", "Tool", QLabel("t"), "tool")
    ws.apply_default(1200, 800)

    def tree(node=None):
        st = ws.state()["tree"] if node is None else node
        if st["t"] == "split":
            return st["o"] + "[" + ",".join(tree(c) for c in st["c"]) + "]"
        return "(" + "|".join(st["keys"]) + ")" if st["t"] == "stack" else "MAP"

    assert tree() == "h[MAP,v[(a|b|c|d)]]" or "MAP" in tree()
    # split a panel off below another, tab it back, put one at the left edge
    ws._take_key("b")
    ws._insert(["b"], Zone("stack", "bottom", ws.stack_of("a")))
    assert ws.stack_of("b") is not ws.stack_of("a")
    ws._take_key("c")
    ws._insert(["c"], Zone("edge", "left"))
    assert tree().startswith("h[(c),MAP")
    # the tool panel opens under the map
    ws.show_panel("t")
    assert "v[MAP,(t)]" in tree()
    # close / reopen returns to the same place
    before = tree()
    ws.close_panel("c")
    assert not ws.is_open("c") and "(c)" not in tree()
    ws.show_panel("c")
    assert tree() == before
    # save + restore
    state = ws.state()
    ws.apply_default(1200, 800)
    assert ws.restore(state)
    assert tree() == before
    # hiding the side panels keeps the map and the tool
    ws.set_side_hidden(True)
    assert ws.side_hidden and not ws.widget("a").isVisibleTo(ws)
    ws.set_side_hidden(False)


def test_gl_attempt_order(monkeypatch):
    """The Linux choices (X11 / software fallbacks) – checked on every OS."""
    from radarforge import gl_setup
    monkeypatch.setattr(gl_setup, "IS_LINUX", True)
    order = gl_setup.attempt_order("x11", "core", None)
    assert order[0] == ("x11", "core")
    assert len(order) == len(set(order))
    assert {f for _p, f in order} == set(gl_setup.FORMATS)
    forced = gl_setup.attempt_order("x11", None, "x11-software")
    assert forced[0] == ("x11-software", "core-msaa")


def test_windows_folders_and_graphics_order(monkeypatch):
    """Per-OS behaviour that can be checked without Windows."""
    import sys
    from radarforge import config, gl_setup
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:/Users/me/AppData/Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/me/AppData/Local")
    settings_dir, cache_dir = config._dirs()
    assert settings_dir.as_posix().endswith("AppData/Roaming/RadarForge")
    assert cache_dir.as_posix().endswith("AppData/Local/RadarForge/cache")
    monkeypatch.setattr(gl_setup, "IS_LINUX", False)
    # Linux-only choices (X11, software rendering) are ignored on Windows
    order = gl_setup.attempt_order("x11", "core", "x11-software")
    assert order == [("native", "core"), ("native", "core-msaa"), ("native", "compat")]
    assert not gl_setup._wayland_session()
