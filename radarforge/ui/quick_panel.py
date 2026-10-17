"""Chip panels: Layers (general map layers) and the base for the Severe and Winter panels.

Every chip mirrors the same action as its menu item, so the two never disagree. Sections fold away
(remembered), and each header says how many of its switches are on.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..features import mrms

COLS = 3


class _Chip(QToolButton):
    """A chip that may be squeezed below its text width (Qt elides the text) instead of forcing the
    panel to scroll sideways; the panel reflows to fewer columns long before that happens."""

    def minimumSizeHint(self):
        h = super().minimumSizeHint()
        return QSize(min(h.width(), 28), h.height())


def _chip(text, tip=""):
    b = _Chip()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(True)
    b.setProperty("role", "chip")
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    b.setMinimumWidth(30)
    return b


def _button(text, fn, tip=""):
    b = _Chip()
    b.setText(text)
    b.setToolTip(tip)
    b.setProperty("role", "chip")
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    b.clicked.connect(lambda: fn())
    return b


class Section(QWidget):
    """A foldable group: header (arrow, title, "n on") and a grid of controls."""

    def __init__(self, panel, key, title):
        super().__init__()
        self.panel, self.key, self.title = panel, key, title
        self.chips = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(3)
        self.header = QPushButton()
        self.header.setFlat(True)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setProperty("role", "qsection")
        self.header.clicked.connect(self.toggle)
        lay.addWidget(self.header)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(2, 0, 2, 4)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(4)
        self.cols = COLS
        for c in range(COLS):
            self.grid.setColumnStretch(c, 1)
        lay.addWidget(self.body)
        self._row, self._col = 0, 0
        self._items = []                  # ("cell" | "row" | "break", widget) in order, for reflowing
        self.collapsed = key in (panel.main.settings["quick_collapsed"] or [])
        self.body.setVisible(not self.collapsed)
        self.update_header()

    def _place(self, w, record=True):
        if record:
            self._items.append(("cell", w))
        self.grid.addWidget(w, self._row, self._col)
        self._col += 1
        if self._col >= self.cols:
            self._row, self._col = self._row + 1, 0

    def newline(self, record=True):
        if record:
            self._items.append(("break", None))
        if self._col:
            self._row, self._col = self._row + 1, 0

    def _place_row(self, row, record=True):
        if record:
            self._items.append(("row", row))
        self.newline(record=False)
        self.grid.addWidget(row, self._row, 0, 1, self.cols)
        self._row += 1

    def reflow(self, cols):
        """Lay the same controls out again in a different number of columns."""
        if cols == self.cols:
            return
        for _kind, w in self._items:
            if w is not None:
                self.grid.removeWidget(w)
        for c in range(max(cols, self.cols)):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        self.cols = cols
        self._row, self._col = 0, 0
        for kind, w in self._items:
            if kind == "cell":
                self._place(w, record=False)
            elif kind == "row":
                self._place_row(w, record=False)
            else:
                self.newline(record=False)

    def chip(self, action, text, tip=None):
        """A chip mirroring a checkable action."""
        if action is None:
            return None
        b = _chip(text, tip or action.toolTip() or action.text().replace("&&", "&"))
        b.setChecked(action.isChecked())

        def clicked():
            action.trigger()
            b.setChecked(action.isChecked())
        b.clicked.connect(clicked)
        action.toggled.connect(lambda on: (b.setChecked(on), self.update_header()))
        self.chips.append((b, action))
        self._place(b)
        self.update_header()
        return b

    def button(self, text, fn, tip=""):
        b = _button(text, fn, tip)
        self._place(b)
        return b

    def label_row(self, text, widget):
        """A small caption on the left and a control filling the rest of the row."""
        self.newline()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lab = QLabel(text)
        lab.setProperty("role", "hint")
        lab.setMinimumWidth(58)
        h.addWidget(lab)
        h.addWidget(widget, 1)
        self._place_row(row)
        return widget

    def segment(self, text, actions, labels):
        """Mutually exclusive chips mirroring radio actions."""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        grp = QButtonGroup(box)
        grp.setExclusive(False)                      # the actions are the source of truth
        for a, lab in zip(actions, labels):
            b = _chip(lab, a.text().replace("&&", "&"))
            b.setChecked(a.isChecked())

            def clicked(_=False, a=a, b=b):
                a.trigger()
                b.setChecked(a.isChecked())
            b.clicked.connect(clicked)
            a.toggled.connect(b.setChecked)
            self.__dict__.setdefault("seg_chips", []).append((b, a))
            grp.addButton(b)
            h.addWidget(b)
        return self.label_row(text, box)

    def update_header(self):
        n = sum(1 for _b, a in self.chips if a.isChecked())
        arrow = "▸" if self.collapsed else "▾"
        title = self.title.upper().replace("&", "&&")
        self.header.setText(f"{arrow}  {title}" + (f"   ·  {n} on" if n else ""))
        self.header.setToolTip("Click to " + ("open" if self.collapsed else "fold away") + " this section")

    def toggle(self):
        self.collapsed = not self.collapsed
        self.body.setVisible(not self.collapsed)
        self.update_header()
        s = self.panel.main.settings
        cur = set(s["quick_collapsed"] or [])
        cur.symmetric_difference_update({self.key})
        s["quick_collapsed"] = sorted(cur)
        s.save()


def _setup_button(main, name, text):
    """A wide, bold button that applies a preset (checked while that preset is the current one)."""
    act = main.preset_acts.get(name)
    b = QPushButton(text)
    b.setCheckable(True)
    f = b.font()
    f.setBold(True)
    b.setFont(f)
    b.setMinimumHeight(32)
    b.setCursor(Qt.PointingHandCursor)
    if act is not None:
        b.setToolTip(act.toolTip() + ". Change any switch afterwards.")
        b.setChecked(act.isChecked())
        b.clicked.connect(lambda _=False: (act.trigger(), b.setChecked(act.isChecked())))
        act.toggled.connect(b.setChecked)
    return b


def _hint(text):
    lab = QLabel(text)
    lab.setProperty("role", "hint")
    lab.setWordWrap(True)
    return lab


class ChipPage(QWidget):
    """A side panel made of foldable sections of chips that reflow to the panel's width (Layers, Severe, Winter)."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        inner = QWidget()
        self._lay = lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(2)
        self.sections = []
        self.build(lay)
        lay.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)     # reflows to fit instead
        sa.setWidget(inner)
        self._scroll = sa
        sa.viewport().installEventFilter(self)
        self._chip_w = self._widest_chip()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sa)

    def build(self, lay):
        raise NotImplementedError

    def resync(self):
        """Match every chip to its action (for changes made with the action's signals blocked)."""
        for sec in self.sections:
            for b, a in list(sec.chips) + list(getattr(sec, "seg_chips", [])):
                if b.isChecked() != a.isChecked():
                    b.setChecked(a.isChecked())
                if b.isEnabled() != a.isEnabled():
                    b.setEnabled(a.isEnabled())
            sec.update_header()

    def section(self, key, title):
        sec = Section(self, key, title)
        self._lay.addWidget(sec)
        self.sections.append(sec)
        return sec

    def model_buttons(self, sec, sets, singles):
        """Buttons that open the Models view on a set of maps (several panels) or on one field."""
        from ..features import models as M
        for label, fields, model, region, tip in sets:
            b = sec.button(label, lambda f=fields, mo=model, r=region: self.main.open_model_set(f, mo, r), tip)
            bf = b.font()
            bf.setBold(True)
            b.setFont(bf)
        sec.newline()
        for fid, label, model, region in singles:
            fd = M.FIELDS[fid]
            sec.button(label, lambda f=fid, mo=model, r=region: self.main.open_model_set([f], mo, r),
                       f"{fd.name}: {fd.desc}")


    # ---------------------------------------------------------------- reflow to the panel's width
    def _widest_chip(self):
        fm = self.fontMetrics()
        texts = [b.text() for sec in self.sections for kind, b in sec._items
                 if kind == "cell" and isinstance(b, QToolButton)]
        return max((fm.horizontalAdvance(t) for t in texts), default=60) + 8       # padding + border

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and obj is self._scroll.viewport():
            self._fit_columns(ev.size().width())
        return False

    def _fit_columns(self, width):
        avail = width - 16 - 4                          # page margins, grid margins
        cols = max(1, min(COLS, (avail + 4) // (self._chip_w + 4)))
        for sec in self.sections:
            sec.reflow(cols)

    def changeEvent(self, ev):
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, "_scroll"):
            self._chip_w = self._widest_chip()
            self._fit_columns(self._scroll.viewport().width())
        super().changeEvent(ev)


class QuickPanel(ChipPage):
    """The Layers panel: the map's general layers. Severe, winter and tropical weather each have their own panel
    (the buttons at the top open them)."""

    def build(self, lay):
        m = self.main
        ov = m.overlay_acts
        cap = QLabel("WEATHER PANELS")
        cap.setProperty("role", "qsection")
        f = cap.font()
        f.setBold(True)
        cap.setFont(f)
        lay.addWidget(cap)
        prow = QGridLayout()
        prow.setHorizontalSpacing(4)
        prow.setVerticalSpacing(4)
        for i, (key, label, tip) in enumerate((
                ("severe", "Severe ›", "Warnings, storm reports, SPC outlooks, storm tools and severe model maps"),
                ("winter", "Winter ›", "Precipitation type, freezing line, snowfall, winter warnings and winter "
                                       "model maps"),
                ("tropical", "Tropical ›", "Hurricanes: cones, tracks, arrival times, past storms and tropical "
                                           "model maps"))):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setMinimumHeight(30)
            bf = b.font()
            bf.setBold(True)
            b.setFont(bf)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: m.show_panel(k))
            prow.addWidget(b, 0, i)
        clean = m.preset_acts.get("clean")
        if clean is not None:
            cb = _chip("Clean map", clean.toolTip())
            cb.setChecked(clean.isChecked())
            cb.clicked.connect(lambda _=False, a=clean, b=cb: (a.trigger(), b.setChecked(a.isChecked())))
            clean.toggled.connect(cb.setChecked)
            prow.addWidget(cb, 1, 0, 1, 3)
        lay.addLayout(prow)
        lay.addWidget(_hint("Each weather panel has a one-click setup and its own model maps. Below: layers for "
                            "any weather."))
        lay.addSpacing(4)

        s = self.section("warnings", "Warnings")
        s.chip(ov.get("warnings"), "Warnings", "NWS warnings on the map (rest the mouse on one for a summary, "
                                               "click it for the full text)")
        s.chip(ov.get("watches"), "Watches", "Tornado and severe thunderstorm watches")
        s.chip(m.warn_loc_act, "Alert me", "Pop up a new warning that covers your location (live)")
        s.button("Warnings panel…", lambda: m.show_panel("warnings"), "The list of warnings, with filters")

        s = self.section("overlays", "Radar overlays")
        s.chip(ov.get("melting_layer"), "Melting layer", "Level III melting layer rings (N0M)")
        s.chip(ov.get("mrms"), "MRMS swath", "Rotation tracks, hail size or rainfall over the last hours (MRMS)")
        self.mrms_combo = QComboBox()
        self._mrms_keys = list(mrms.PRODUCTS)
        for k in self._mrms_keys:
            self.mrms_combo.addItem(mrms.PRODUCTS[k][0])
        self.mrms_combo.currentIndexChanged.connect(self._mrms_picked)
        s.label_row("Swath", self.mrms_combo)
        self.mrms_slider = s.label_row("Opacity", self._slider("mrms"))
        self.sync_mrms()

        s = self.section("satellite", "Satellite & observations")
        s.chip(ov.get("satellite"), "Satellite", "GOES satellite under the radar (live)")
        s.chip(ov.get("lightning"), "Lightning", "GOES lightning flashes")
        s.chip(ov.get("lightning_density"), "Strike density", "Lightning strike density map (NLDN, via MRMS)")
        s.chip(ov.get("surface_obs"), "Surface obs", "Weather station plots: temperature, dew point, wind (live)")
        s.chip(ov.get("cameras"), "Cameras", "Street cameras (when zoomed in)")
        s.button("Camera keys…", m.open_camera_sources, "Street camera sources and free keys")
        s.segment("Channel", m.sat_channel_acts, ["IR", "Visible", "Water vap."])
        self.sat_slider = s.label_row("Opacity", self._slider("satellite"))
        s.segment("Flashes", m.ltg_minutes_acts, ["5 min", "10", "15", "30"])

        s = self.section("display", "Radar display")
        s.chip(m.smooth_act, "Smoothing")
        s.chip(m.dealias_act, "Dealiasing", "Unfold aliased velocities (fixes red / green flips in strong winds) (D)")
        s.chip(m.trail_act, "Σ Max trail")
        s.chip(m.legend_act, "Colour bars")
        s.chip(m.link_act, "Linked cursor", "One cursor shown in every panel")
        s.chip(m.hover_act, "Hover details", "Rest the mouse on warnings, reports, sites… for a summary box")
        s.segment("Velocity filter", m.vf_group.actions(), ["Off", "Normal", "Strong"])

        s = self.section("map", "Map")
        s.chip(m.cities_act, "Cities")
        s.chip(m.sites_act, "Radar sites")
        s.chip(m.rings_act, "Range rings")
        names = {"counties": "Counties", "roads": "Interstates", "roads2": "Highways", "states": "States",
                 "countries": "Countries", "lakes": "Lakes"}
        for key, label in names.items():
            if key in m.layer_acts:
                s.chip(m.layer_acts[key], label)
        s.chip(m.tdwr_act, "TDWR")
        if getattr(m, "maps_under_act", None) is not None:
            s.chip(m.maps_under_act, "Lines under radar", "Roads and borders beneath the radar, so storms cover them")
        s.button("Colours…", lambda: m.open_settings("Map style"), "Map colours, line widths and fonts")
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_opacity)
        self._pending = {}

    # ---------------------------------------------------------------- opacity sliders
    def _slider(self, which):
        sl = QSlider(Qt.Horizontal)
        sl.setRange(20, 100)
        sl.setSingleStep(5)
        sl.setPageStep(10)
        sl.setValue(int(round(float(self.main.settings[f"{which}_opacity"] or 0.85) * 100)))
        sl.setToolTip(f"{'Satellite' if which == 'satellite' else 'MRMS'} opacity")
        sl.valueChanged.connect(lambda v, w=which: self._opacity_moved(w, v))
        return sl

    def _opacity_moved(self, which, v):
        self.main.settings[f"{which}_opacity"] = v / 100.0       # drawn straight away
        self.main.view.update()
        self._pending[which] = v / 100.0
        self._save_timer.start()

    def _save_opacity(self):
        pending, self._pending = self._pending, {}
        for which, v in pending.items():
            self.main._set_opacity(which, v)                      # saves and ticks the menu presets

    def sync_opacity(self):
        for which, sl in (("satellite", self.sat_slider), ("mrms", self.mrms_slider)):
            v = int(round(float(self.main.settings[f"{which}_opacity"] or 0.8) * 100))
            if sl.value() != v:
                sl.blockSignals(True)
                sl.setValue(v)
                sl.blockSignals(False)

    # ---------------------------------------------------------------- MRMS product
    def _mrms_picked(self, i):
        if 0 <= i < len(self._mrms_keys) and self._mrms_keys[i] != self.main.settings["mrms_product"]:
            self.main.set_mrms_product(self._mrms_keys[i])

    def sync_mrms(self):
        key = self.main.settings["mrms_product"]
        if key in self._mrms_keys:
            self.mrms_combo.blockSignals(True)
            self.mrms_combo.setCurrentIndex(self._mrms_keys.index(key))
            self.mrms_combo.blockSignals(False)
