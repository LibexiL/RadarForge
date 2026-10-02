"""Settings window (categories on the left) and the theme editor."""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ... import themes
from ...overlays.warnings import BASE_CODE, CLASSIC_PRESET, LINE_KINDS, VARIANT, VARIANTS, default_line, hex_rgb
from ...products import catalog, colortable
from .. import icons


def _hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("role", "hint")
    return lab


def _title(text):
    lab = QLabel(text)
    lab.setProperty("role", "title")
    return lab


def _line():
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setProperty("role", "line")
    return f


def _page(title, subtitle=""):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(22, 18, 22, 18)
    lay.setSpacing(10)
    lay.addWidget(_title(title))
    if subtitle:
        lay.addWidget(_hint(subtitle))
    lay.addWidget(_line())
    return w, lay


def _form():
    f = QFormLayout()
    f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    f.setHorizontalSpacing(14)
    f.setVerticalSpacing(9)
    return f


def theme_preview_icon(t, w=72, h=40):
    """A little swatch picture of a theme: window, accent, map, lines."""
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    ui, mp = t["ui"], t["map"]
    p.setPen(themes.qcolor(ui["border"]))
    p.setBrush(themes.qcolor(ui["window"]))
    p.drawRoundedRect(0, 0, w - 1, h - 1, 5, 5)
    p.fillRect(1, 1, w - 2, 8, themes.qcolor(ui["header"]))
    p.fillRect(4, 3, 14, 4, themes.qcolor(ui["accent"]))
    p.fillRect(5, 12, w - 26, h - 16, themes.qcolor(mp["map_bg"]))
    p.setPen(themes.qcolor(mp["states"]))
    p.drawLine(8, h - 12, w - 26, 16)
    p.setPen(themes.qcolor(mp["counties"]))
    p.drawLine(10, 16, w - 30, h - 7)
    p.setPen(themes.qcolor(mp["roads"]))
    p.drawLine(5, h - 20, w - 24, h - 9)
    p.fillRect(w - 18, 12, 13, h - 16, themes.qcolor(ui["panel"]))
    p.fillRect(w - 16, 15, 9, 3, themes.qcolor(ui["text"]))
    p.fillRect(w - 16, 21, 7, 3, themes.qcolor(ui["dim"]))
    p.end()
    return QIcon(pm)


class SettingsDialog(QDialog):
    PAGES = ["General", "Display", "Loop & live", "Environment", "Colour tables", "Warnings", "Themes", "Performance"]

    def __init__(self, settings, parent=None, page=None):
        super().__init__(parent)
        self.s = settings
        self.main = parent
        self.setWindowTitle("Settings")
        self.resize(820, 580)
        self._theme_start = settings["theme"]
        self.nav = QListWidget()
        self.nav.setProperty("role", "nav")
        self.nav.setFixedWidth(170)
        self.nav.setIconSize(QSize(18, 18))
        self.pages = QStackedWidget()
        nav_icons = {"General": "settings", "Display": "layers", "Loop & live": "play", "Environment": "radar",
                     "Colour tables": "palette", "Warnings": "warning", "Themes": "theme", "Performance": "box3d"}
        for name in self.PAGES:
            it = QListWidgetItem(icons.icon(nav_icons.get(name, "settings")), name)
            it.setSizeHint(QSize(0, 34))
            self.nav.addItem(it)
            self.pages.addWidget(getattr(self, "_page_" + name.split()[0].lower())())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 10)
        lay.addLayout(body, 1)
        row = QHBoxLayout()
        row.setContentsMargins(12, 0, 12, 0)
        row.addWidget(bb)
        lay.addLayout(row)
        self.nav.setCurrentRow(self.PAGES.index(page) if page in self.PAGES else 0)

    # ------------------------------------------------------------------ pages
    def _page_general(self):
        w, lay = _page("General")
        f = _form()
        s = self.s
        self.units = QComboBox()
        self.units.addItems(["nm", "km", "mi"])
        self.units.setCurrentText(s["distance_units"])
        f.addRow("Distance units", self.units)
        self.temp_units = QComboBox()
        self.temp_units.addItems(["F", "C"])
        self.temp_units.setCurrentText(s["temp_units"])
        f.addRow("Temperature units", self.temp_units)
        self.start_live = QCheckBox("Start in live mode on the last radar used")
        self.start_live.setChecked(bool(s["start_live"]))
        f.addRow("On launch", self.start_live)
        self.invert = QCheckBox("Invert mouse-wheel zoom")
        self.invert.setChecked(bool(s["invert_scroll"]))
        f.addRow("Mouse", self.invert)
        self.hover = QCheckBox("Show pop-up text for warnings, storms and placefiles")
        self.hover.setChecked(bool(s["hover_text"]))
        f.addRow("", self.hover)
        self.learn = QCheckBox("Explain the value under the cursor in words (e.g. \"CC 0.85: mixed targets\")")
        self.learn.setChecked(bool(s["learn_hints"]))
        f.addRow("", self.learn)
        self.link = QCheckBox("Linked cursor across panels")
        self.link.setChecked(bool(s["cursor_link"]))
        f.addRow("", self.link)
        lay.addLayout(f)
        lay.addStretch(1)
        return w

    def _page_display(self):
        w, lay = _page("Display", "How radar data is drawn.")
        f = _form()
        s = self.s
        self.smooth = QCheckBox("Smooth radar data (like GR2Analyst)")
        self.smooth.setChecked(bool(s["gpu_smooth"]))
        f.addRow("Smoothing", self.smooth)
        self.vfilter = QComboBox()
        self.vfilter.addItems(["Off (raw data)", "Normal", "Aggressive"])
        self.vfilter.setCurrentIndex(int(s["velocity_filter"]))
        f.addRow("Velocity noise filter", self.vfilter)
        f.addRow("", _hint("Removes noisy velocity in weak echo. Strong storms and couplets are never filtered."))
        self.legend = QCheckBox("Show colour bars")
        self.legend.setChecked(bool(s["show_legend"]))
        f.addRow("Panels", self.legend)
        lay.addLayout(f)
        lay.addStretch(1)
        return w

    def _page_loop(self):
        w, lay = _page("Loop & live")
        f = _form()
        s = self.s
        self.frames = QSpinBox()
        self.frames.setRange(1, 60)
        self.frames.setValue(int(s["loop_frames"]))
        f.addRow("Frames to load (live)", self.frames)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(0.5, 30)
        self.fps.setSuffix(" frames/s")
        self.fps.setValue(float(s["loop_fps"]))
        f.addRow("Loop speed", self.fps)
        self.dwell = QDoubleSpinBox()
        self.dwell.setRange(0, 10)
        self.dwell.setSuffix(" s")
        self.dwell.setValue(float(s["loop_dwell"]))
        f.addRow("Pause on last frame", self.dwell)
        self.poll = QSpinBox()
        self.poll.setRange(5, 300)
        self.poll.setSuffix(" s")
        self.poll.setValue(int(s["live_poll_seconds"]))
        f.addRow("Check for new data every", self.poll)
        lay.addLayout(f)
        lay.addStretch(1)
        return w

    def _page_environment(self):
        w, lay = _page("Environment", "Used by MESH and POSH. Take them from a nearby sounding or model analysis.")
        f = _form()
        self.fz = QDoubleSpinBox()
        self.fz.setRange(0, 25000)
        self.fz.setSingleStep(500)
        self.fz.setSuffix(" ft MSL")
        self.fz.setValue(float(self.s["freezing_level_ft"]))
        self.m20 = QDoubleSpinBox()
        self.m20.setRange(0, 40000)
        self.m20.setSingleStep(500)
        self.m20.setSuffix(" ft MSL")
        self.m20.setValue(float(self.s["minus20_level_ft"]))
        f.addRow("0 °C height", self.fz)
        f.addRow("−20 °C height", self.m20)
        lay.addLayout(f)
        lay.addStretch(1)
        return w

    def _page_colour(self):
        w, lay = _page("Colour tables", "GR2Analyst / GRLevelX .pal files work directly. "
                                        "You can also drag a .pal file onto a radar panel.")
        self.ct = QTableWidget(0, 2)
        self.ct.setHorizontalHeaderLabels(["Product", "Colour table"])
        self.ct.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.ct.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ct.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ct.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ct.verticalHeader().setVisible(False)
        self.ct.setAlternatingRowColors(True)
        self.ct.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.ct, 1)
        hb = QHBoxLayout()
        load = QPushButton("Load .pal for selected…")
        reset = QPushButton("Use default")
        hb.addWidget(load)
        hb.addWidget(reset)
        hb.addStretch(1)
        lay.addLayout(hb)
        load.clicked.connect(self._load_pal)
        reset.clicked.connect(self._reset_pal)
        self.ct.doubleClicked.connect(lambda _i: self._load_pal())
        self.overrides = dict(self.s["palette_overrides"])
        self._fill_ct()
        return w

    def _page_warnings(self):
        w, lay = _page("Warnings", "Warning lines: every warning type and threat level has its own line. "
                                   "Click a line to change its colour, width and style. The defaults "
                                   "use the National Weather Service's colours; the NWS has no separate colours "
                                   "for threat levels, so those are told apart by the line style.")
        self.warn_lines = {}
        user = dict(self.s["warning_lines"] or {})
        legacy = dict(self.s["warning_colors"] or {})
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        for row, (code, ev, label, _d, _p) in enumerate(VARIANTS):
            color, width, kind = default_line(code)
            if BASE_CODE.get(ev) == code and legacy.get(ev):
                color = legacy[ev]
            o = user.get(code) or {}
            line = {"color": o.get("color", color), "width": float(o.get("width", width)),
                    "kind": o.get("kind", kind) if o.get("kind", kind) in LINE_KINDS else kind}
            self.warn_lines[code] = line
            btn = _LineButton(line)
            btn.setToolTip(f"Change the {label} line")
            btn.clicked.connect(lambda _c=False, c=code, b=btn: self._edit_line(c, b))
            grid.addWidget(btn, row, 0)
            cl = QLabel(code)
            cl.setStyleSheet("font-weight: bold;")
            grid.addWidget(cl, row, 1)
            grid.addWidget(QLabel(label.replace(" - ", " – ")), row, 2)
        grid.setColumnStretch(2, 1)
        self._line_buttons = {grid.itemAtPosition(r, 0).widget(): VARIANTS[r][0] for r in range(len(VARIANTS))}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setLayout(grid)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        hb = QHBoxLayout()
        nws = QPushButton("NWS colours")
        nws.setToolTip("Reset every line to the defaults (NWS colours)")
        classic = QPushButton("Classic colours")
        classic.setToolTip("Green flash flood, yellow severe thunderstorm, magenta reported / PDS / emergency tornado")
        nws.clicked.connect(lambda: self._line_preset({}))
        classic.clicked.connect(lambda: self._line_preset(CLASSIC_PRESET))
        hb.addWidget(nws)
        hb.addWidget(classic)
        hb.addStretch(1)
        lay.addLayout(hb)
        self.go_nearest = QCheckBox("Going to a warning switches to the radar nearest it")
        self.go_nearest.setChecked(bool(self.s["go_to_nearest_radar"]))
        lay.addWidget(self.go_nearest)
        return w

    def _edit_line(self, code, btn):
        d = WarningLineDialog(code, self.warn_lines[code], self)
        if d.exec():
            self.warn_lines[code] = d.line
            btn.set_line(d.line)

    def _line_preset(self, preset):
        for b, code in self._line_buttons.items():
            c, wdt, k = preset.get(code) or default_line(code)
            self.warn_lines[code] = {"color": c, "width": wdt, "kind": k}
            b.set_line(self.warn_lines[code])

    def _page_themes(self):
        w, lay = _page("Themes", "Pick a theme to try it right away. Themes are .rftheme files; drop one onto the "
                                 "RadarForge window, import it here, or make your own.")
        self.theme_list = QListWidget()
        self.theme_list.setIconSize(QSize(72, 40))
        self.theme_list.setSpacing(2)
        self.theme_list.currentItemChanged.connect(self._theme_selected)
        lay.addWidget(self.theme_list, 1)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        self.t_new = QPushButton("New from selected…")
        self.t_edit = QPushButton("Edit…")
        self.t_import = QPushButton("Import…")
        self.t_export = QPushButton("Export…")
        self.t_delete = QPushButton("Delete")
        self.t_folder = QPushButton("Open themes folder")
        for i, b in enumerate((self.t_new, self.t_edit, self.t_delete, self.t_import, self.t_export, self.t_folder)):
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        self.t_new.clicked.connect(self._theme_new)
        self.t_edit.clicked.connect(self._theme_edit)
        self.t_import.clicked.connect(self._theme_import)
        self.t_export.clicked.connect(self._theme_export)
        self.t_delete.clicked.connect(self._theme_delete)
        self.t_folder.clicked.connect(self._theme_folder)
        self._fill_themes(self.s["theme"])
        return w

    def _page_performance(self):
        w, lay = _page("Performance")
        f = _form()
        s = self.s
        self.vcache = QSpinBox()
        self.vcache.setRange(1, 30)
        self.vcache.setValue(int(s["volume_cache"]))
        f.addRow("Decoded volumes kept in RAM", self.vcache)
        self.icache = QSpinBox()
        self.icache.setRange(100, 8000)
        self.icache.setSuffix(" MB")
        self.icache.setValue(int(s["image_cache_mb"]))
        f.addRow("Rendered image cache", self.icache)
        self.scene_cache = QCheckBox("Reuse the drawn map while only the mouse moves (much faster hover)")
        self.scene_cache.setChecked(bool(s["scene_cache"]))
        f.addRow("Drawing", self.scene_cache)
        f.addRow("", _hint("Turn this off if the map or buttons stop updating with your graphics driver. "
                           "Takes effect after a restart."))
        lay.addLayout(f)
        lay.addWidget(_line())
        lay.addWidget(QLabel("<b>Graphics</b>"))
        info = getattr(getattr(self.main, "view", None), "gl_info", "") or "unknown"
        from ...gl_setup import is_software, label
        plat = s["gl_platform"]
        gl = QLabel(f"OpenGL: {info}<br>Display: {label(plat) if plat else 'automatic'}")
        gl.setWordWrap(True)
        gl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(gl)
        if is_software(info):
            lay.addWidget(_hint("⚠ This is software rendering, which is slow. Updating the graphics driver usually "
                                "fixes it."))
        redetect = QPushButton("Detect the best OpenGL setup again on next start")
        redetect.clicked.connect(self._gl_reset)
        log = QPushButton("Open log file")
        log.setToolTip("Everything RadarForge printed this session, including graphics errors")
        log.clicked.connect(self._open_log)
        row = QHBoxLayout()
        row.addWidget(redetect)
        row.addWidget(log)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------------ colour tables
    def _fill_ct(self):
        self.ct.setRowCount(len(catalog.PRODUCTS))
        for r, p in enumerate(catalog.PRODUCTS):
            a = QTableWidgetItem(p.name)
            a.setData(Qt.UserRole, p.id)
            self.ct.setItem(r, 0, a)
            path = self.overrides.get(p.id)
            b = QTableWidgetItem(os.path.basename(path) if path else f"default ({p.palette})")
            b.setToolTip(path or "Built-in table")
            if not path:
                b.setForeground(self.palette().placeholderText())
            self.ct.setItem(r, 1, b)

    def _load_pal(self):
        r = self.ct.currentRow()
        if r < 0:
            QMessageBox.information(self, "Colour table", "Select a product first.")
            return
        pid = self.ct.item(r, 0).data(Qt.UserRole)
        path, _ = QFileDialog.getOpenFileName(self, "Colour table", "", "Colour tables (*.pal *.txt);;All files (*)")
        if not path:
            return
        try:
            ct = colortable.load_pal(path)
            if not ct.entries:
                raise ValueError("no Color: entries found")
        except Exception as exc:
            QMessageBox.warning(self, "Colour table", f"Could not read {path}:\n{exc}")
            return
        fam, label = colortable.table_family(ct)
        if fam != catalog.get(pid).palette:
            what = colortable.FAMILY_NAMES.get(fam, fam) if fam else label
            if QMessageBox.question(self, "Colour table doesn't match",
                                    f"This table was made for {what}, not {catalog.get(pid).name}. Use it anyway?"
                                    ) != QMessageBox.Yes:
                return
        self.overrides[pid] = path
        self._fill_ct()
        self.ct.selectRow(r)

    def _reset_pal(self):
        r = self.ct.currentRow()
        if r >= 0:
            self.overrides.pop(self.ct.item(r, 0).data(Qt.UserRole), None)
            self._fill_ct()
            self.ct.selectRow(r)

    # ------------------------------------------------------------------ themes
    def _fill_themes(self, select=None):
        self.theme_list.blockSignals(True)
        self.theme_list.clear()
        self._themes = themes.all_themes()
        sel_row = 0
        for i, t in enumerate(self._themes):
            kind = "built-in" if t.get("_builtin") else "custom"
            it = QListWidgetItem(theme_preview_icon(t), f"{t['name']}\n{kind}" +
                                 (f" · by {t['author']}" if t.get("author") else ""))
            it.setData(Qt.UserRole, i)
            it.setSizeHint(QSize(0, 48))
            self.theme_list.addItem(it)
            if t["name"] == select:
                sel_row = i
        self.theme_list.setCurrentRow(sel_row)
        self.theme_list.blockSignals(False)
        self._update_theme_buttons()

    def _current_theme(self):
        it = self.theme_list.currentItem()
        return self._themes[it.data(Qt.UserRole)] if it is not None else None

    def _update_theme_buttons(self):
        t = self._current_theme()
        custom = t is not None and not t.get("_builtin")
        self.t_edit.setEnabled(custom)
        self.t_delete.setEnabled(custom)
        self.t_export.setEnabled(t is not None)

    def _theme_selected(self, *_):
        t = self._current_theme()
        self._update_theme_buttons()
        if t is not None and self.main is not None:
            self.main.apply_theme(t["name"], save=False)

    def _theme_new(self):
        base = self._current_theme() or themes.find(None)
        name, ok = QInputDialog.getText(self, "New theme", "Name for the new theme:",
                                        text=f"{base['name']} (my version)")
        if not ok or not name.strip():
            return
        t = copy.deepcopy(themes.clean(base))
        t["name"] = name.strip()
        ed = ThemeEditor(t, self.main, self)
        if ed.exec():
            themes.save_theme(ed.theme)
            self._fill_themes(ed.theme["name"])
            self._theme_selected()
        else:
            self._theme_selected()

    def _theme_edit(self):
        t = self._current_theme()
        if t is None or t.get("_builtin"):
            return
        ed = ThemeEditor(themes.clean(t), self.main, self)
        if ed.exec():
            old = t.get("_path")
            new_path = themes.save_theme(ed.theme)
            if old and os.path.abspath(old) != os.path.abspath(new_path):
                try:
                    os.remove(old)
                except OSError:
                    pass
            self._fill_themes(ed.theme["name"])
        self._theme_selected()

    def _theme_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import theme", os.path.expanduser("~"),
                                              "RadarForge themes (*.rftheme *.json);;All files (*)")
        if not path:
            return
        try:
            t = themes.import_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import theme", f"Couldn't import {os.path.basename(path)}:\n{exc}")
            return
        self._fill_themes(t["name"])
        self._theme_selected()

    def _theme_export(self):
        t = self._current_theme()
        if t is None:
            return
        default = os.path.join(os.path.expanduser("~"), themes._slug(t["name"]) + themes.EXT)
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", default, "RadarForge theme (*.rftheme)")
        if path:
            themes.save_theme(t, path)

    def _theme_delete(self):
        t = self._current_theme()
        if t is None or t.get("_builtin"):
            return
        if QMessageBox.question(self, "Delete theme", f"Delete the theme “{t['name']}”?") != QMessageBox.Yes:
            return
        themes.delete_theme(t)
        self._fill_themes(themes.DEFAULT["name"])
        self._theme_selected()

    def _theme_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        themes.THEME_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(themes.THEME_DIR)))

    # ------------------------------------------------------------------ misc
    def _open_log(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from ...config import LOG_FILE
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE)))

    def _gl_reset(self):
        self.s["gl_platform"] = None
        self.s["gl_format"] = None
        self.s.save()
        QMessageBox.information(self, "OpenGL", "RadarForge will test the graphics setup again next time it starts.")

    def reject(self):
        if self.main is not None:
            self.main.apply_theme(self._theme_start, save=False)      # undo any theme preview
        super().reject()

    def _ok(self):
        s = self.s
        s["distance_units"] = self.units.currentText()
        s["temp_units"] = self.temp_units.currentText()
        s["start_live"] = self.start_live.isChecked()
        s["invert_scroll"] = self.invert.isChecked()
        s["hover_text"] = self.hover.isChecked()
        s["learn_hints"] = self.learn.isChecked()
        s["cursor_link"] = self.link.isChecked()
        s["gpu_smooth"] = self.smooth.isChecked()
        s["velocity_filter"] = self.vfilter.currentIndex()
        s["show_legend"] = self.legend.isChecked()
        s["loop_frames"] = self.frames.value()
        s["loop_fps"] = self.fps.value()
        s["loop_dwell"] = self.dwell.value()
        s["live_poll_seconds"] = self.poll.value()
        s["freezing_level_ft"] = self.fz.value()
        s["minus20_level_ft"] = self.m20.value()
        s["palette_overrides"] = self.overrides
        lines = {}
        for code, line in self.warn_lines.items():
            c, wdt, k = default_line(code)
            if (line["color"].lower()[:7], round(line["width"], 2), line["kind"]) != (c, round(wdt, 2), k):
                lines[code] = {"color": line["color"].lower()[:7], "width": round(line["width"], 2), "kind": line["kind"]}
        s["warning_lines"] = lines
        s["warning_colors"] = {}           # 1.5.0 colours now live in warning_lines
        s["go_to_nearest_radar"] = self.go_nearest.isChecked()
        s["volume_cache"] = self.vcache.value()
        s["image_cache_mb"] = self.icache.value()
        s["scene_cache"] = self.scene_cache.isChecked()
        t = self._current_theme()
        if t is not None and self.main is not None:
            self.main.apply_theme(t["name"], save=False)
        s.save()
        self.accept()


# --------------------------------------------------------------------------- #
# theme editor
# --------------------------------------------------------------------------- #
class _ColorButton(QToolButton):
    def __init__(self, hex_color, on_change, alpha=True):
        super().__init__()
        self.on_change = on_change
        self.alpha = alpha
        self.setFixedSize(46, 24)
        self.set_hex(hex_color)
        self.clicked.connect(self._pick)

    def set_hex(self, hex_color):
        self.hex = themes.to_hex(themes.parse_color(hex_color))
        pm = QPixmap(38, 16)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        # checkerboard so transparency is visible
        for x in range(0, 38, 6):
            for y in range(0, 16, 6):
                p.fillRect(x, y, 6, 6, QColor(200, 200, 200) if (x + y) // 6 % 2 else QColor(140, 140, 140))
        p.fillRect(0, 0, 38, 16, themes.qcolor(self.hex))
        p.end()
        self.setIcon(QIcon(pm))
        self.setIconSize(QSize(38, 16))
        self.setToolTip(self.hex)

    def _pick(self):
        opts = QColorDialog.ShowAlphaChannel if self.alpha else QColorDialog.ColorDialogOption(0)
        c = QColorDialog.getColor(themes.qcolor(self.hex), self, "Choose colour", opts)
        if c.isValid():
            self.set_hex(themes.to_hex((c.red(), c.green(), c.blue(), c.alpha() if self.alpha else 255)))
            self.on_change()


def _paint_line(p, rect, line):
    """Draws a warning line sample (colour, width, style) across rect."""
    from PySide6.QtCore import QLineF, QPointF
    from ...overlays.warnings import draw_line
    p.fillRect(rect, QColor(16, 17, 22))
    y = rect.center().y()
    seg = QLineF(QPointF(rect.left() + 6, y), QPointF(rect.right() - 6, y))
    draw_line(p, (seg,), hex_rgb(line["color"]), float(line["width"]), line["kind"], halo=False)


class _LineButton(QToolButton):
    """A clickable warning line sample."""
    def __init__(self, line):
        super().__init__()
        self.setFixedSize(92, 28)
        self.set_line(line)

    def set_line(self, line):
        self.line = dict(line)
        pm = QPixmap(80, 18)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_line(p, pm.rect(), self.line)
        p.end()
        self.setIcon(QIcon(pm))
        self.setIconSize(QSize(80, 18))


class WarningLineDialog(QDialog):
    """Edit one warning line: colour, width and style, with a live sample."""
    KIND_LABELS = {"solid": "Solid", "center": "Black centre line", "double": "Double"}

    def __init__(self, code, line, parent=None):
        super().__init__(parent)
        _c, ev, label, _d, _p = VARIANT[code]
        self.code = code
        self.line = dict(line)
        self.setWindowTitle(f"{code} – {label.replace(' - ', ' – ')}")
        lay = QVBoxLayout(self)
        self.sample = QLabel()
        self.sample.setFixedHeight(40)
        self.sample.setMinimumWidth(320)
        lay.addWidget(self.sample)
        f = _form()
        self.color_btn = _ColorButton(self.line["color"], self._changed, alpha=False)
        f.addRow("Colour", self.color_btn)
        self.width = QDoubleSpinBox()
        self.width.setRange(0.5, 12.0)
        self.width.setSingleStep(0.5)
        self.width.setDecimals(1)
        self.width.setSuffix(" px")
        self.width.setValue(float(self.line["width"]))
        self.width.valueChanged.connect(self._changed)
        f.addRow("Width", self.width)
        self.kind = QComboBox()
        for k in LINE_KINDS:
            self.kind.addItem(self.KIND_LABELS[k], k)
        self.kind.setCurrentIndex(LINE_KINDS.index(self.line["kind"]) if self.line["kind"] in LINE_KINDS else 0)
        self.kind.currentIndexChanged.connect(self._changed)
        f.addRow("Style", self.kind)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.button(QDialogButtonBox.RestoreDefaults).setText("Default")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._default)
        lay.addWidget(bb)
        self._changed()

    def _default(self):
        c, w, k = default_line(self.code)
        self.color_btn.set_hex(c)
        self.width.setValue(w)
        self.kind.setCurrentIndex(LINE_KINDS.index(k))
        self._changed()

    def _changed(self, *_):
        self.line = {"color": self.color_btn.hex[:7], "width": self.width.value(), "kind": self.kind.currentData()}
        pm = QPixmap(max(320, self.sample.width()), 40)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_line(p, pm.rect(), self.line)
        p.end()
        self.sample.setPixmap(pm)


class ThemeEditor(QDialog):
    """Edit every colour of a theme with a live preview in the main window."""

    def __init__(self, theme, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.theme = themes.clean(theme)
        self.setWindowTitle(f"Edit theme – {self.theme['name']}")
        self.resize(560, 680)
        lay = QVBoxLayout(self)
        top = _form()
        self.name = QLineEdit(self.theme["name"])
        self.author = QLineEdit(self.theme.get("author", ""))
        self.author.setPlaceholderText("optional")
        self.dark = QCheckBox("Dark theme")
        self.dark.setChecked(self.theme["dark"])
        top.addRow("Name", self.name)
        top.addRow("Author", self.author)
        top.addRow("", self.dark)
        lay.addLayout(top)
        lay.addWidget(_hint("Changes show in the main window as you make them. Click a colour to change it; "
                            "colours can be partly transparent."))
        inner = QWidget()
        grid = QVBoxLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        self.buttons = {}
        for part, roles, heading in (("ui", themes.UI_ROLES, "Interface"), ("map", themes.MAP_ROLES, "Map")):
            lab = QLabel(heading)
            lab.setProperty("role", "section")
            grid.addWidget(lab)
            form = _form()
            for key, label in roles:
                b = _ColorButton(self.theme[part][key], self._changed)
                self.buttons[(part, key)] = b
                form.addRow(label, b)
            grid.addLayout(form)
        lab = QLabel("Line widths")
        lab.setProperty("role", "section")
        grid.addWidget(lab)
        form = _form()
        self.widths = {}
        for key, label in themes.WIDTH_ROLES:
            sp = QDoubleSpinBox()
            sp.setRange(0.3, 6.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(1)
            sp.setSuffix(" px")
            sp.setValue(float(self.theme["widths"][key]))
            sp.valueChanged.connect(self._changed)
            self.widths[key] = sp
            form.addRow(label, sp)
        grid.addLayout(form)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        lay.addWidget(sa, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._changed()

    def _collect(self):
        t = copy.deepcopy(self.theme)
        t["name"] = self.name.text().strip() or self.theme["name"]
        t["author"] = self.author.text().strip()
        t["dark"] = self.dark.isChecked()
        for (part, key), b in self.buttons.items():
            t[part][key] = b.hex
        for key, sp in self.widths.items():
            t["widths"][key] = sp.value()
        return t

    def _changed(self, *_):
        if self.main is not None:
            self.main.preview_theme(self._collect())

    def _save(self):
        t = self._collect()
        builtin = {x["name"] for x in themes.builtin_themes()}
        if t["name"] in builtin:
            QMessageBox.information(self, "Theme name", "That name belongs to a built-in theme. Pick another name.")
            return
        self.theme = t
        self.accept()
