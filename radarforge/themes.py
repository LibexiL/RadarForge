"""Themes: colours for the interface and the map.

A theme is a small JSON file (``.rftheme``)::

    {
      "format": "radarforge-theme", "version": 1,
      "name": "My theme", "dark": true,
      "ui":  {"window": "#26272d", "accent": "#3c6ec8", ...},
      "map": {"map_bg": "#08080c", "states": "#e1e1e1", "counties": "#69696980", ...},
      "widths": {"states": 1.8, "counties": 1.0},
      "fonts": {"city": {"family": "", "size": 9, "bold": false}, "site": {...}, "title": {...}}
    }

Colours are ``#rrggbb`` or ``#rrggbbaa``. Anything a file leaves out falls back to
the default theme, so a theme can be as short as a couple of colours.
User themes live in the ``themes`` folder inside the settings folder (see config.py).
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .config import CONFIG_DIR

THEME_DIR = CONFIG_DIR / "themes"
FORMAT = "radarforge-theme"
EXT = ".rftheme"

# role -> label, grouped for the editor
UI_ROLES = [
    ("window", "Window background"), ("panel", "Lists, fields and panels"), ("alt", "Alternate list rows"),
    ("header", "Toolbar, tab bars and status bar"), ("button", "Buttons"), ("border", "Borders and dividers"),
    ("text", "Text"), ("dim", "Secondary text"), ("accent", "Accent (selection, active items)"),
    ("accent_text", "Text on accent"),
]
MAP_ROLES = [
    ("map_bg", "Map background"), ("map_gap", "Gap between panels"), ("states", "State / province lines"),
    ("countries", "Country and coast lines"), ("counties", "County lines"), ("roads", "Interstates"),
    ("roads2", "Highways"), ("lakes", "Lakes"), ("city_text", "City labels"), ("city_dot", "City dots"),
    ("site_text", "Radar site labels"), ("site_88d", "WSR-88D site markers"), ("site_tdwr", "TDWR site markers"),
    ("site_current", "Current radar marker"), ("rings", "Range rings"), ("label_bg", "Panel title and colour bar background"),
    ("label_text", "Panel title and colour bar text"), ("halo", "Text outline"), ("panel_border", "Panel borders"),
    ("active_border", "Active panel border"), ("cursor", "Linked cursor"),
]
FONT_ROLES = [("city", "City labels"), ("site", "Radar site labels"), ("title", "Panel titles")]
WIDTH_ROLES = [("states", "State lines"), ("countries", "Country lines"), ("counties", "County lines"),
               ("roads", "Interstates"), ("roads2", "Highways"), ("lakes", "Lakes")]

DEFAULT = {
    "format": FORMAT, "version": 1, "name": "RadarForge Dark", "dark": True,
    "ui": {"window": "#16191e", "panel": "#1b1f25", "alt": "#1f242b", "header": "#121418", "button": "#22272e",
           "border": "#2a3038", "text": "#e3e7ed", "dim": "#8a94a3", "accent": "#5b9cff", "accent_text": "#0b1220"},
    "map": {"map_bg": "#08080c", "map_gap": "#121418", "states": "#e1e1e1", "countries": "#d7d7d7",
            "counties": "#696969e6", "roads": "#af4646eb", "roads2": "#78553cdc", "lakes": "#466eaac8",
            "city_text": "#e1e1e1", "city_dot": "#e6e6e6", "site_text": "#cde6d2", "site_88d": "#28965a",
            "site_tdwr": "#966ec8", "site_current": "#ffd700", "rings": "#c8c8d796", "label_bg": "#000000b9",
            "label_text": "#f0f0f5", "halo": "#000000dc", "panel_border": "#2a3038", "active_border": "#5b9cff",
            "cursor": "#ffffffe6"},
    "widths": {"states": 1.8, "countries": 1.8, "counties": 1.0, "roads": 1.4, "roads2": 1.0, "lakes": 1.0},
    # family "" = the interface font
    "fonts": {"city": {"family": "", "size": 9, "bold": False}, "site": {"family": "", "size": 8, "bold": False},
              "title": {"family": "", "size": 9, "bold": True}},
}

_BUILTIN = [
    DEFAULT,
    {"name": "RadarForge Classic", "dark": True,                  # the look before 1.22
     "ui": {"window": "#26272d", "panel": "#1e1f24", "alt": "#2a2b31", "header": "#1f2025", "button": "#303138",
            "border": "#3a3c45", "text": "#e1e1e6", "dim": "#8d909b", "accent": "#3c6ec8", "accent_text": "#ffffff"},
     "map": {"map_gap": "#1f1f24", "panel_border": "#464650", "active_border": "#5a8cdc"}},
    {"name": "Midnight Blue", "dark": True,
     "ui": {"window": "#1a2030", "panel": "#131826", "alt": "#1a2132", "header": "#121724", "button": "#232c40",
            "border": "#2d3850", "text": "#dde5f3", "dim": "#8793ab", "accent": "#4c8dff", "accent_text": "#ffffff"},
     "map": {"map_bg": "#05070d", "map_gap": "#121724", "counties": "#5a6478dc", "states": "#d8e0f0",
             "countries": "#c8d2e6", "roads": "#b4505ae6", "label_bg": "#0a0f1cc8", "active_border": "#4c8dff",
             "panel_border": "#2d3850", "site_88d": "#2f8f9d"}},
    {"name": "GR Classic", "dark": True,
     "ui": {"window": "#2d2d2d", "panel": "#232323", "alt": "#2b2b2b", "header": "#262626", "button": "#3a3a3a",
            "border": "#474747", "text": "#e6e6e6", "dim": "#9a9a9a", "accent": "#3a78c8", "accent_text": "#ffffff"},
     "map": {"map_bg": "#000000", "map_gap": "#262626", "states": "#ffffff", "countries": "#ffffff",
             "counties": "#808080d2", "roads": "#c04040", "roads2": "#8a6446", "lakes": "#3c64a0",
             "city_text": "#ffffff", "label_bg": "#000000c8", "panel_border": "#5a5a5a", "active_border": "#3a78c8"},
     "widths": {"states": 2.0, "countries": 2.0, "counties": 1.0}},
    {"name": "Nord", "dark": True,
     "ui": {"window": "#2e3440", "panel": "#272c36", "alt": "#323845", "header": "#242933", "button": "#3b4252",
            "border": "#434c5e", "text": "#eceff4", "dim": "#9aa3b5", "accent": "#5e81ac", "accent_text": "#eceff4"},
     "map": {"map_bg": "#1b1f27", "map_gap": "#242933", "states": "#d8dee9", "countries": "#e5e9f0",
             "counties": "#4c566ae6", "roads": "#bf616a", "roads2": "#d0876f96", "lakes": "#5e81acc8",
             "city_text": "#e5e9f0", "site_88d": "#8fbcbb", "site_current": "#ebcb8b", "rings": "#88c0d096",
             "label_bg": "#1b1f27c8", "active_border": "#88c0d0", "panel_border": "#434c5e"}},
    {"name": "High Contrast", "dark": True,
     "ui": {"window": "#000000", "panel": "#000000", "alt": "#111111", "header": "#000000", "button": "#1a1a1a",
            "border": "#9a9a9a", "text": "#ffffff", "dim": "#d0d0d0", "accent": "#ffd400", "accent_text": "#000000"},
     "map": {"map_bg": "#000000", "map_gap": "#333333", "states": "#ffffff", "countries": "#ffffff",
             "counties": "#b4b4b4", "roads": "#ff4040", "roads2": "#c08040", "lakes": "#4080ff",
             "city_text": "#ffffff", "city_dot": "#ffffff", "site_text": "#ffffff", "label_bg": "#000000e6",
             "label_text": "#ffffff", "halo": "#000000", "panel_border": "#9a9a9a", "active_border": "#ffd400",
             "cursor": "#ffff00"},
     "widths": {"states": 2.4, "countries": 2.4, "counties": 1.3, "roads": 1.8}},
    {"name": "Daylight", "dark": False,
     "ui": {"window": "#eceef2", "panel": "#ffffff", "alt": "#f4f5f8", "header": "#e2e5eb", "button": "#f7f8fa",
            "border": "#c4c9d4", "text": "#1d2129", "dim": "#667085", "accent": "#2f6fdb", "accent_text": "#ffffff"},
     "map": {"map_bg": "#e8e6e1", "map_gap": "#c9ccd3", "states": "#2d2d2d", "countries": "#1e1e1e",
             "counties": "#8c8c8cc8", "roads": "#b43c3c", "roads2": "#a0785a", "lakes": "#7fa8d8",
             "city_text": "#1e1e1e", "city_dot": "#333333", "site_text": "#12301e", "site_88d": "#2a9a5c",
             "rings": "#50506496", "label_bg": "#ffffffd2", "label_text": "#141414", "halo": "#ffffffdc",
             "panel_border": "#a5aab5", "active_border": "#2f6fdb", "cursor": "#000000e6"}},
]


# --------------------------------------------------------------------------- #
# colours
# --------------------------------------------------------------------------- #
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")


def parse_color(text: str):
    """'#rrggbb' / '#rrggbbaa' -> (r, g, b, a)."""
    m = _HEX.match(str(text).strip())
    if not m:
        raise ValueError(f"not a colour: {text!r}")
    rgb = m.group(1)
    a = int(m.group(2), 16) if m.group(2) else 255
    return int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16), a


def to_hex(rgba) -> str:
    r, g, b = rgba[:3]
    a = rgba[3] if len(rgba) > 3 else 255
    return f"#{r:02x}{g:02x}{b:02x}" + (f"{a:02x}" if a != 255 else "")


def qcolor(text):
    from PySide6.QtGui import QColor
    return QColor(*parse_color(text))


def css(text) -> str:
    r, g, b, a = parse_color(text)
    return f"rgba({r},{g},{b},{a})"


# --------------------------------------------------------------------------- #
# theme objects
# --------------------------------------------------------------------------- #
def normalize(theme: dict) -> dict:
    """Fill a (possibly partial) theme from the default and check every value."""
    if not isinstance(theme, dict):
        raise ValueError("a theme must be a JSON object")
    out = copy.deepcopy(DEFAULT)
    name = str(theme.get("name") or "").strip()
    if not name:
        raise ValueError("the theme has no name")
    out["name"] = name[:60]
    out["dark"] = bool(theme.get("dark", _is_dark(theme.get("ui", {}).get("window", DEFAULT["ui"]["window"]))))
    for part in ("ui", "map"):
        for k, v in (theme.get(part) or {}).items():
            if k in out[part]:
                parse_color(v)
                out[part][k] = to_hex(parse_color(v))
    for k, v in (theme.get("widths") or {}).items():
        if k in out["widths"]:
            out["widths"][k] = max(0.3, min(6.0, float(v)))
    for k, v in (theme.get("fonts") or {}).items():
        if k in out["fonts"] and isinstance(v, dict):
            f = out["fonts"][k]
            f["family"] = str(v.get("family") or "")[:80]
            try:
                f["size"] = max(6, min(24, int(round(float(v.get("size", f["size"]))))))
            except (TypeError, ValueError):
                pass
            f["bold"] = bool(v.get("bold", f["bold"]))
    if theme.get("author"):
        out["author"] = str(theme["author"])[:80]
    return out


def _is_dark(hex_window) -> bool:
    try:
        r, g, b, _a = parse_color(hex_window)
    except ValueError:
        return True
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def builtin_themes() -> list:
    out = []
    for t in _BUILTIN:
        n = normalize(t)
        n["_builtin"] = True
        out.append(n)
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return s or "theme"


def user_themes() -> list:
    out = []
    if THEME_DIR.is_dir():
        for p in sorted(THEME_DIR.iterdir()):
            if p.suffix.lower() not in (EXT, ".json"):
                continue
            try:
                t = load_file(p)
            except Exception as exc:
                print("theme", p.name, "skipped:", exc)
                continue
            t["_path"] = str(p)
            out.append(t)
    return out


def all_themes() -> list:
    themes = builtin_themes()
    names = {t["name"] for t in themes}
    for t in user_themes():
        if t["name"] in names:
            t["name"] = f"{t['name']} (custom)"
        names.add(t["name"])
        themes.append(t)
    return themes


def find(name: str | None) -> dict:
    for t in all_themes():
        if t["name"] == name:
            return t
    return builtin_themes()[0]


def load_file(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and data.get("format") not in (None, FORMAT):
        raise ValueError("not a RadarForge theme file")
    return normalize(data)


def looks_like_theme(path) -> bool:
    p = Path(path)
    if p.suffix.lower() == EXT:
        return True
    if p.suffix.lower() != ".json":
        return False
    try:
        with open(p, encoding="utf-8") as fh:
            head = fh.read(4096)
        return FORMAT in head
    except OSError:
        return False


def clean(theme: dict) -> dict:
    """Theme as saved to disk (no internal keys)."""
    t = normalize(theme)
    return {"format": FORMAT, "version": 1, "name": t["name"], "dark": t["dark"],
            **({"author": t["author"]} if t.get("author") else {}),
            "ui": t["ui"], "map": t["map"], "widths": t["widths"], "fonts": t["fonts"]}


def save_theme(theme: dict, path=None) -> str:
    THEME_DIR.mkdir(parents=True, exist_ok=True)
    data = clean(theme)
    if path is None:
        path = THEME_DIR / (_slug(data["name"]) + EXT)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return str(path)


def import_file(path) -> dict:
    """Validate a theme file and copy it into the theme folder. Returns the theme."""
    t = load_file(path)
    names = {x["name"] for x in builtin_themes()}
    if t["name"] in names:
        t["name"] += " (imported)"
    dest = save_theme(t)
    t["_path"] = dest
    return t


def delete_theme(theme: dict) -> bool:
    p = theme.get("_path")
    if not p or theme.get("_builtin"):
        return False
    try:
        Path(p).unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
def map_style(theme: dict):
    """(role -> QColor, layer_style) for RadarView.set_colors()."""
    from .render.maps import LAYER_STYLE
    t = normalize(theme)
    colors = {k: qcolor(v) for k, v in t["map"].items()}
    layers = {}
    for name, (label, rgba, width, min_scale) in LAYER_STYLE.items():
        c = t["map"].get(name)
        layers[name] = (label, parse_color(c) if c else rgba, float(t["widths"].get(name, width)), min_scale)
    return colors, layers


def map_fonts(theme: dict) -> dict:
    """role -> QFont for RadarView.set_fonts()."""
    from .render.fonts import ui_font
    t = normalize(theme)
    out = {}
    for role, f in t["fonts"].items():
        q = ui_font(f["size"], f["bold"])
        if f["family"]:
            q.setFamily(f["family"])
        out[role] = q
    return out


def apply_ui(app, theme: dict):
    from PySide6.QtGui import QColor, QPalette
    t = normalize(theme)
    u = t["ui"]
    q = {k: qcolor(v) for k, v in u.items()}
    app.setStyle("Fusion")
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive):
        pal.setColor(group, QPalette.Window, q["window"])
        pal.setColor(group, QPalette.WindowText, q["text"])
        pal.setColor(group, QPalette.Base, q["panel"])
        pal.setColor(group, QPalette.AlternateBase, q["alt"])
        pal.setColor(group, QPalette.ToolTipBase, q["header"])
        pal.setColor(group, QPalette.ToolTipText, q["text"])
        pal.setColor(group, QPalette.PlaceholderText, q["dim"])
        pal.setColor(group, QPalette.Text, q["text"])
        pal.setColor(group, QPalette.Button, q["button"])
        pal.setColor(group, QPalette.ButtonText, q["text"])
        pal.setColor(group, QPalette.BrightText, QColor(255, 80, 80))
        pal.setColor(group, QPalette.Highlight, q["accent"])
        pal.setColor(group, QPalette.HighlightedText, q["accent_text"])
        pal.setColor(group, QPalette.Link, q["accent"].lighter(130) if t["dark"] else q["accent"])
        pal.setColor(group, QPalette.Mid, q["border"])
        pal.setColor(group, QPalette.Dark, q["border"].darker(130))
        pal.setColor(group, QPalette.Light, q["button"].lighter(120))
        pal.setColor(group, QPalette.Midlight, q["button"].lighter(110))
        pal.setColor(group, QPalette.Shadow, QColor(0, 0, 0))
    dis = QColor(q["dim"])
    dis.setAlpha(150)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, dis)
    pal.setColor(QPalette.Disabled, QPalette.Base, q["window"])
    pal.setColor(QPalette.Disabled, QPalette.Button, q["window"])
    pal.setColor(QPalette.Disabled, QPalette.Highlight, q["border"])
    app.setPalette(pal)
    f = app.font()
    if f.pointSizeF() > 10.0:
        f.setPointSizeF(10.0)
        app.setFont(f)
    elif f.pointSizeF() <= 0 and f.pixelSize() > 13:      # (desktops that give the font size in pixels)
        f.setPixelSize(13)
        app.setFont(f)
    app.setStyleSheet(stylesheet(t))


def _mix(a, b, f):
    """Colour between a and b ((r, g, b, ...) tuples), f = 0..1 towards b."""
    return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))


def _rgba(c, a):
    return f"rgba({c[0]},{c[1]},{c[2]},{int(a)})"


_ASSETS = {}


def _assets(t: dict) -> dict:
    """Small PNG pictures the stylesheet needs (tick, dot, arrow), drawn for this theme's colours and kept in
    a temporary folder. (PNG rather than SVG: no image plugin needed, also in the packaged builds.)"""
    import hashlib
    import tempfile
    u = t["ui"]
    key = hashlib.sha1(f"{u['accent_text']}{u['text']}{u['dim']}".encode()).hexdigest()[:10]
    if key in _ASSETS:
        return _ASSETS[key]
    out = {}
    try:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
        folder = Path(tempfile.gettempdir()) / f"radarforge-ui-{key}"
        folder.mkdir(parents=True, exist_ok=True)

        def draw(name, size, fn):
            img = QImage(size, size, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            fn(p, size)
            p.end()
            path = folder / f"{name}.png"
            img.save(str(path))
            out[name] = path.as_posix()

        def tick(p, n):
            p.setPen(QPen(QColor(*parse_color(u["accent_text"])[:3]), n * 0.13, Qt.SolidLine, Qt.RoundCap,
                          Qt.RoundJoin))
            path = QPainterPath(QPointF(n * 0.24, n * 0.52))
            path.lineTo(QPointF(n * 0.42, n * 0.70))
            path.lineTo(QPointF(n * 0.76, n * 0.32))
            p.drawPath(path)

        def arrow(colour):
            def fn(p, n):
                p.setPen(QPen(QColor(*parse_color(colour)[:3]), n * 0.14, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                path = QPainterPath(QPointF(n * 0.22, n * 0.38))
                path.lineTo(QPointF(n * 0.5, n * 0.64))
                path.lineTo(QPointF(n * 0.78, n * 0.38))
                p.drawPath(path)
            return fn

        def dash(p, n):
            p.setPen(QPen(QColor(*parse_color(u["accent_text"])[:3]), n * 0.13, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(n * 0.28, n * 0.5), QPointF(n * 0.72, n * 0.5))
        draw("tick", 32, tick)
        draw("dash", 32, dash)
        draw("arrow", 32, arrow(u["dim"]))
        draw("arrow_on", 32, arrow(u["text"]))
    except Exception as exc:              # (no pictures: Qt's own drawing is used)
        print("theme: couldn't draw the interface pictures:", exc)
    _ASSETS[key] = out
    return out


def stylesheet(t: dict) -> str:
    """The interface's look: roomy but not chunky, flat, hairline borders; on / selected things are a soft tint of the accent
    with accent-coloured text, rather than solid blocks of colour."""
    u = t["ui"]
    dark = bool(t.get("dark", True))
    c = {k: css(v) for k, v in u.items()}
    acc = parse_color(u["accent"])
    tx = parse_color(u["text"])
    btn = parse_color(u["button"])
    c["acc_soft"] = _rgba(acc, 44 if dark else 36)          # on chips, selections
    c["acc_soft2"] = _rgba(acc, 64 if dark else 52)         # ... hovered
    c["acc_line"] = _rgba(acc, 140)
    c["acc_fg"] = "rgb(%d,%d,%d)" % (_mix(acc, tx, 0.38) if dark else _mix(acc, (0, 0, 0), 0.18))
    c["acc_hi"] = "rgb(%d,%d,%d)" % _mix(acc, (255, 255, 255), 0.18)
    c["hover"] = _rgba(tx, 14)
    c["hover2"] = _rgba(tx, 22)
    c["chip"] = _rgba(tx, 10 if dark else 14)              # a switch that's off: a faint tile
    c["press"] = _rgba(tx, 24)
    c["edge"] = _rgba(tx, 52)                                # hovered borders
    c["btn_hi"] = "rgb(%d,%d,%d)" % _mix(btn, tx, 0.06)
    c["track"] = _rgba(tx, 34)
    c["thumb"] = _rgba(tx, 48)
    c["thumb_hi"] = _rgba(tx, 90)
    c["dim_soft"] = _rgba(parse_color(u["dim"]), 150)
    a = _assets(t)
    c["tick"] = 'image: url("%s");' % a["tick"] if a.get("tick") else ""
    c["dash"] = 'image: url("%s");' % a["dash"] if a.get("dash") else ""
    arrow = (f"QComboBox::drop-down {{ border: none; width: 20px; subcontrol-origin: padding; "
             f"subcontrol-position: center right; }}\n"
             f"QComboBox::down-arrow {{ image: url(\"{a['arrow']}\"); width: 10px; height: 10px; }}\n"
             f"QComboBox::down-arrow:on, QComboBox::down-arrow:hover {{ image: url(\"{a['arrow_on']}\"); }}\n"
             f"QToolButton::menu-indicator {{ image: url(\"{a['arrow']}\"); width: 9px; height: 9px; "
             f"subcontrol-origin: padding; subcontrol-position: center right; right: 3px; }}\n"
             f"QToolButton[popupMode=\"2\"] {{ padding-right: 15px; }}\n") if a.get("arrow") else ""
    return """
QWidget { outline: 0; }
QToolTip { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 6px;
    padding: 5px 8px; }
QMainWindow::separator, QSplitter::handle { background: %(window)s; }
QSplitter::handle:hover { background: %(acc_line)s; }

/* ---- bars along the top and bottom */
QMenuBar { background: %(header)s; border: none; border-bottom: 1px solid %(border)s; padding: 0 4px; }
QMenuBar::item { padding: 4px 10px; margin: 2px 0; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: %(hover)s; }
QMenuBar::item:pressed { background: %(press)s; }
QMenuBar QToolButton { padding: 1px 8px; border-radius: 6px; border: 1px solid transparent; margin: 1px 2px; }
QMenuBar QToolButton:hover { background: %(hover)s; border-color: %(border)s; }
QMenuBar QToolButton:checked { background: %(acc_soft)s; border-color: %(acc_line)s; color: %(acc_fg)s; }
QMenuBar QLineEdit { padding: 1px 8px; margin: 2px 6px; border-radius: 7px; }
QToolBar { background: %(header)s; border: none; border-bottom: 1px solid %(border)s; spacing: 4px;
    padding: 4px 10px; }
QToolBar::separator { background: %(border)s; width: 1px; margin: 6px 5px; }
QToolBar#main_toolbar::separator { background: transparent; width: 18px; margin: 0; }
QToolBar QToolButton { padding: 4px 6px; border-radius: 6px; border: 1px solid transparent; }
QToolBar QToolButton:hover { background: %(hover)s; }
QToolBar QToolButton:checked { background: %(acc_soft)s; color: %(acc_fg)s; }
QToolBar QToolButton:pressed { background: %(press)s; }
QToolBar QLabel[role="group"] { color: %(dim)s; padding: 0 2px 0 6px; }
QToolBar QComboBox { padding: 3px 8px; }
QStatusBar { background: %(header)s; color: %(dim)s; border-top: 1px solid %(border)s; }
QStatusBar QLabel { color: %(dim)s; padding: 0 6px; }
QStatusBar::item { border: none; }
QStatusBar QToolBar { background: transparent; border: none; padding: 2px 0; spacing: 3px; }
QStatusBar QToolBar::separator { margin: 6px 5px; }

/* ---- the Radar / Models switch: one segmented control */
QToolButton[role="viewswitch"] { padding: 4px 12px; border: 1px solid %(border)s; border-radius: 0;
    background: transparent; color: %(dim)s; }
QToolButton[role="viewswitch"][side="left"] { border-top-left-radius: 7px; border-bottom-left-radius: 7px; }
QToolButton[role="viewswitch"][side="right"] { border-left: none; border-top-right-radius: 7px;
    border-bottom-right-radius: 7px; }
QToolButton[role="viewswitch"]:hover { background: %(hover)s; color: %(text)s; }
QToolButton[role="viewswitch"]:checked { background: %(acc_soft)s; color: %(acc_fg)s; }

/* ---- menus */
QMenu { background: %(panel)s; border: 1px solid %(border)s; border-radius: 9px; padding: 6px; }
QMenu::item { padding: 6px 28px 6px 24px; border-radius: 6px; }
QMenu::item:selected { background: %(acc_soft)s; color: %(text)s; }
QMenu::item:disabled { color: %(dim_soft)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 5px 8px; }
QMenu::section { color: %(dim)s; padding: 6px 10px 2px 10px; font-weight: 600; }

/* ---- buttons */
QPushButton { background: %(button)s; border: 1px solid %(border)s; border-radius: 6px; padding: 5px 14px; }
QPushButton:hover { background: %(btn_hi)s; border-color: %(edge)s; }
QPushButton:pressed { background: %(press)s; }
QPushButton:checked { background: %(acc_soft)s; border-color: transparent; color: %(acc_fg)s; }
QPushButton:default { background: %(accent)s; color: %(accent_text)s; border-color: %(accent)s; font-weight: 600; }
QPushButton:default:hover { background: %(acc_hi)s; border-color: %(acc_hi)s; }
QPushButton:disabled { color: %(dim_soft)s; background: transparent; }
QToolButton { border-radius: 6px; }
QToolButton[role="chip"] { background: %(chip)s; border: 1px solid transparent; border-radius: 6px;
    padding: 5px 10px; color: %(text)s; }
QToolButton[role="chip"]:hover { background: %(hover2)s; border-color: %(edge)s; }
QToolButton[role="chip"]:pressed { background: %(press)s; }
QToolButton[role="chip"]:checked { background: %(acc_soft)s; color: %(acc_fg)s; }
QToolButton[role="chip"]:checked:hover { background: %(acc_soft2)s; }
QToolButton[role="chip"]:disabled { color: %(dim_soft)s; background: transparent; border-color: %(chip)s; }
QToolBar QToolButton[role="chip"] { border-radius: 12px; padding: 3px 10px 3px 8px; }
QToolButton[role="update"] { background: %(accent)s; color: %(accent_text)s; border: none; border-radius: 9px;
    padding: 1px 10px; margin: 1px 4px; font-weight: 600; }
QToolButton[role="update"]:hover { background: %(acc_hi)s; }
QPushButton[role="qsection"] { color: %(text)s; font-weight: 600; text-align: left; padding: 10px 2px 6px 2px;
    background: transparent; border: none; border-bottom: 1px solid %(border)s; border-radius: 0; }
QPushButton[role="qsection"]:hover { color: %(acc_fg)s; background: transparent; }
QPushButton[role="qsection"]:pressed { background: transparent; }

/* ---- fields */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit, QTimeEdit {
    background: %(panel)s; border: 1px solid %(border)s; border-radius: 6px; padding: 4px 8px;
    selection-background-color: %(acc_soft2)s; selection-color: %(text)s; }
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QDateTimeEdit:hover { border-color: %(edge)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus,
QComboBox:on { border-color: %(accent)s; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: %(dim_soft)s; background: transparent; }
QComboBox QAbstractItemView { background: %(panel)s; border: 1px solid %(border)s; border-radius: 6px; padding: 3px;
    selection-background-color: %(acc_soft)s; selection-color: %(text)s; outline: 0; }
%(arrows)s
QCheckBox, QRadioButton { spacing: 8px; padding: 2px 0; }
QCheckBox::indicator, QGroupBox::indicator { width: 14px; height: 14px; border: 1px solid %(edge)s; border-radius: 4px;
    background: %(panel)s; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: %(accent)s; }
QCheckBox::indicator:checked, QGroupBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; %(tick)s }
QCheckBox::indicator:indeterminate { background: %(accent)s; border-color: %(accent)s; %(dash)s }
QCheckBox::indicator:disabled { background: transparent; border-color: %(border)s; }
QCheckBox::indicator:checked:disabled { background: %(border)s; }
QRadioButton::indicator { width: 13px; height: 13px; border: 1px solid %(edge)s; border-radius: 8px;
    background: %(panel)s; }
QRadioButton::indicator:checked { border: 4px solid %(accent)s; background: %(accent_text)s; width: 7px; height: 7px; }

/* ---- lists and tables */
QTreeView, QTableView, QListView, QTextEdit, QPlainTextEdit, QTextBrowser {
    background: %(panel)s; alternate-background-color: %(alt)s; border: 1px solid %(border)s;
    border-radius: 8px; gridline-color: %(border)s;
    selection-background-color: %(acc_soft2)s; selection-color: %(text)s; }
QTreeView::item:hover, QListView::item:hover, QTableView::item:hover { background: %(hover)s; }
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected { background: %(acc_soft2)s;
    color: %(text)s; }
QHeaderView { background: transparent; }
QHeaderView::section { background: %(header)s; color: %(dim)s; border: none; border-right: 1px solid %(border)s;
    border-bottom: 1px solid %(border)s; padding: 5px 9px; }
QTableCornerButton::section { background: %(header)s; border: none; }
QGroupBox { border: 1px solid %(border)s; border-radius: 9px; margin-top: 18px; padding: 14px 12px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px;
    color: %(dim)s; font-weight: 600; }
QTabWidget::pane { border: 1px solid %(border)s; border-radius: 8px; top: -1px; }
QTabBar::tab { background: transparent; color: %(dim)s; padding: 7px 12px; border: none;
    border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: %(text)s; border-bottom: 2px solid %(accent)s; }
QTabBar::tab:hover:!selected { color: %(text)s; }

/* ---- scroll bars, sliders, progress */
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }
QScrollBar::handle { background: %(thumb)s; border-radius: 3px; margin: 2px; min-height: 28px; min-width: 28px; }
QScrollBar::handle:hover { background: %(thumb_hi)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSlider { min-height: 16px; }
QSlider::groove:horizontal { height: 4px; background: %(track)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(text)s; border: 2px solid %(accent)s; width: 8px; height: 8px;
    margin: -4px 0; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: %(acc_hi)s; }
QSlider::handle:horizontal:disabled { background: %(dim)s; border-color: %(border)s; }
QSlider::sub-page:horizontal:disabled { background: %(border)s; }
QProgressBar { background: %(track)s; border: none; border-radius: 7px; text-align: center; color: %(text)s;
    max-height: 14px; font-size: 8pt; }
QProgressBar::chunk { background: %(accent)s; border-radius: 7px; }

/* ---- text roles */
QLabel[role="hint"] { color: %(dim)s; }
QLabel[role="section"] { color: %(dim)s; font-weight: 600; padding-top: 2px; padding-bottom: 1px; }
QLabel[role="card"] { background: %(panel)s; border: 1px solid %(border)s; border-radius: 9px; padding: 10px 12px; }
QLabel[role="title"] { font-size: 15px; font-weight: 600; }
QListWidget[role="nav"] { background: %(header)s; border: none; border-right: 1px solid %(border)s;
    border-radius: 0; padding: 8px 6px; }
QListWidget[role="nav"]::item { padding: 7px 10px; border-radius: 6px; margin: 2px 0; }
QListWidget[role="nav"]::item:selected { background: %(acc_soft)s; color: %(acc_fg)s; }
QListWidget[role="nav"]::item:hover:!selected { background: %(hover)s; }
QFrame[role="line"] { color: %(border)s; background: %(border)s; max-height: 1px; border: none; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QDialogButtonBox QPushButton { min-width: 76px; }
""" % dict(c, arrows=arrow)
