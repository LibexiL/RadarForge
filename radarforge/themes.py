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
    "ui": {"window": "#26272d", "panel": "#1e1f24", "alt": "#2a2b31", "header": "#1f2025", "button": "#303138",
           "border": "#3a3c45", "text": "#e1e1e6", "dim": "#8d909b", "accent": "#3c6ec8", "accent_text": "#ffffff"},
    "map": {"map_bg": "#08080c", "map_gap": "#1f1f24", "states": "#e1e1e1", "countries": "#d7d7d7",
            "counties": "#696969e6", "roads": "#af4646eb", "roads2": "#78553cdc", "lakes": "#466eaac8",
            "city_text": "#e1e1e1", "city_dot": "#e6e6e6", "site_text": "#cde6d2", "site_88d": "#28965a",
            "site_tdwr": "#966ec8", "site_current": "#ffd700", "rings": "#c8c8d796", "label_bg": "#000000b9",
            "label_text": "#f0f0f5", "halo": "#000000dc", "panel_border": "#464650", "active_border": "#5a8cdc",
            "cursor": "#ffffffe6"},
    "widths": {"states": 1.8, "countries": 1.8, "counties": 1.0, "roads": 1.4, "roads2": 1.0, "lakes": 1.0},
    # family "" = the interface font
    "fonts": {"city": {"family": "", "size": 9, "bold": False}, "site": {"family": "", "size": 8, "bold": False},
              "title": {"family": "", "size": 9, "bold": True}},
}

_BUILTIN = [
    DEFAULT,
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
    app.setStyleSheet(stylesheet(t))


def stylesheet(t: dict) -> str:
    u = t["ui"]
    c = {k: css(v) for k, v in u.items()}
    acc = parse_color(u["accent"])
    c["acc_soft"] = f"rgba({acc[0]},{acc[1]},{acc[2]},70)"
    c["acc_hover"] = f"rgba({acc[0]},{acc[1]},{acc[2]},40)"
    tx = parse_color(u["text"])
    c["hover"] = f"rgba({tx[0]},{tx[1]},{tx[2]},20)"
    return """
QWidget { outline: 0; }
QToolTip { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px 6px; }
QMainWindow::separator, QSplitter::handle { background: %(window)s; }
QSplitter::handle:hover { background: %(acc_soft)s; }
QMenuBar { background: %(header)s; border-bottom: 1px solid %(border)s; padding: 1px 2px; }
QMenuBar::item { padding: 4px 9px; border-radius: 4px; background: transparent; }
QMenuBar::item:selected { background: %(hover)s; }
QMenuBar QToolButton { padding: 2px 8px; border-radius: 4px; border: 1px solid transparent; margin: 1px 4px; }
QMenuBar QToolButton:hover { background: %(hover)s; border-color: %(border)s; }
QMenuBar QToolButton:checked { background: %(acc_soft)s; border-color: %(accent)s; }
QMenu { background: %(panel)s; border: 1px solid %(border)s; padding: 4px; }
QMenu::item { padding: 5px 26px 5px 22px; border-radius: 4px; }
QMenu::item:selected { background: %(accent)s; color: %(accent_text)s; }
QMenu::item:disabled { color: %(dim)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 4px 8px; }
QMenu::section { color: %(dim)s; padding: 6px 10px 2px 10px; font-weight: bold; }
QToolBar { background: %(header)s; border: none; border-bottom: 1px solid %(border)s; spacing: 2px; padding: 3px 6px; }
QToolBar::separator { background: %(border)s; width: 1px; margin: 5px 7px; }
QToolBar QToolButton { padding: 3px 6px; border-radius: 5px; border: 1px solid transparent; }
QToolBar QToolButton:hover { background: %(hover)s; border-color: %(border)s; }
QToolBar QToolButton:checked { background: %(acc_soft)s; border-color: %(accent)s; }
QToolBar QToolButton:pressed { background: %(acc_soft)s; }
QToolBar QLabel[role="group"] { color: %(dim)s; padding: 0 3px 0 6px; }
QStatusBar { background: %(header)s; color: %(dim)s; border-top: 1px solid %(border)s; }
QStatusBar QLabel { color: %(dim)s; padding: 0 6px; }
QStatusBar::item { border: none; }
QGroupBox { border: 1px solid %(border)s; border-radius: 6px; margin-top: 16px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px;
                   color: %(dim)s; font-weight: bold; }
QPushButton { background: %(button)s; border: 1px solid %(border)s; border-radius: 5px; padding: 5px 12px; }
QPushButton:hover { border-color: %(accent)s; }
QPushButton:pressed, QPushButton:checked { background: %(acc_soft)s; border-color: %(accent)s; }
QPushButton:default { border-color: %(accent)s; }
QPushButton:disabled { color: %(dim)s; }
QToolButton { border-radius: 5px; }
QToolButton[role="chip"] { background: %(button)s; border: 1px solid %(border)s; padding: 4px 4px; }
QToolButton[role="chip"]:hover { border-color: %(accent)s; }
QToolButton[role="chip"]:checked { background: %(accent)s; color: %(accent_text)s; border-color: %(accent)s; }
QPushButton[role="qsection"] { color: %(dim)s; font-weight: bold; text-align: left; padding: 6px 2px 3px 2px;
    background: transparent; border: none; border-bottom: 1px solid %(border)s; border-radius: 0; }
QPushButton[role="qsection"]:hover { color: %(text)s; border-bottom-color: %(accent)s; }
QPushButton[role="qsection"]:pressed { background: transparent; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit, QTimeEdit {
    background: %(panel)s; border: 1px solid %(border)s; border-radius: 5px; padding: 3px 6px;
    selection-background-color: %(accent)s; selection-color: %(accent_text)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: %(accent)s; }
QComboBox QAbstractItemView { background: %(panel)s; border: 1px solid %(border)s;
    selection-background-color: %(accent)s; selection-color: %(accent_text)s; }
QTreeView, QTableView, QListView, QTextEdit, QPlainTextEdit {
    background: %(panel)s; alternate-background-color: %(alt)s; border: 1px solid %(border)s;
    border-radius: 5px; gridline-color: %(border)s;
    selection-background-color: %(accent)s; selection-color: %(accent_text)s; }
QHeaderView::section { background: %(header)s; color: %(dim)s; border: none; border-right: 1px solid %(border)s;
    border-bottom: 1px solid %(border)s; padding: 4px 6px; }
QTabWidget::pane { border: 1px solid %(border)s; border-radius: 5px; top: -1px; }
QTabBar::tab { background: transparent; color: %(dim)s; padding: 5px 12px; border: none;
    border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: %(text)s; border-bottom: 2px solid %(accent)s; }
QTabBar::tab:hover { color: %(text)s; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle { background: %(border)s; border-radius: 4px; margin: 2px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: %(dim)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSlider::groove:horizontal { height: 4px; background: %(border)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(text)s; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px; }
QProgressBar { background: %(panel)s; border: 1px solid %(border)s; border-radius: 5px; text-align: center;
    color: %(text)s; max-height: 16px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 4px; }
QLabel[role="hint"] { color: %(dim)s; }
QLabel[role="section"] { color: %(dim)s; font-weight: bold; padding-top: 4px; }
QLabel[role="card"] { background: %(panel)s; border: 1px solid %(border)s; border-radius: 6px; padding: 8px; }
QLabel[role="title"] { font-size: 15px; font-weight: bold; }
QListWidget[role="nav"] { background: %(header)s; border: none; border-right: 1px solid %(border)s;
    border-radius: 0; padding: 6px; }
QListWidget[role="nav"]::item { padding: 7px 10px; border-radius: 5px; margin: 1px 0; }
QListWidget[role="nav"]::item:selected { background: %(accent)s; color: %(accent_text)s; }
QListWidget[role="nav"]::item:hover:!selected { background: %(hover)s; }
QFrame[role="line"] { color: %(border)s; background: %(border)s; max-height: 1px; border: none; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
""" % c
