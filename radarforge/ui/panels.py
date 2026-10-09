"""Side-panel widgets (they live in the movable panels of ui/workspace.py)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QTabWidget, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..data.sites import get_site, nearest_site
from ..features import feeds
from ..features.warnings import FILTERS
from ..products import catalog
from ..products.geometry import aeqd_forward

UNIT_F = {"nm": 1.852, "km": 1.0, "mi": 1.609344}


def _swatch(rgb, size=12):
    pm = QPixmap(size, size)
    pm.fill(QColor(*rgb))
    return QIcon(pm)


def _section(text):
    lab = QLabel(text.upper())
    lab.setProperty("role", "section")
    return lab


def _hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("role", "hint")
    return lab


def _chip(text, tip="", checkable=True):
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setProperty("role", "chip")
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    b.setMinimumWidth(34)
    return b


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(widget)
    return sa


def _zoom_to(main, xs, ys, pad=1.5):
    view = main.view
    x0, x1, y0, y1 = float(np.min(xs)), float(np.max(xs)), float(np.min(ys)), float(np.max(ys))
    p = view.panels[min(view.active_panel, len(view.panels) - 1)]
    w = max(x1 - x0, 5.0) * pad
    h = max(y1 - y0, 5.0) * pad
    scale = min(p.rect.width() / w, p.rect.height() / h)
    view.set_view((x0 + x1) / 2, (y0 + y1) / 2, max(0.2, min(40.0, scale)))


# --------------------------------------------------------------------------- #
# Products / tilts / radar info
# --------------------------------------------------------------------------- #
class ProductsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        self.info = QLabel()
        self.info.setTextFormat(Qt.RichText)
        self.info.setWordWrap(True)
        self.info.setProperty("role", "card")
        lay.addWidget(self.info)

        # which panel we are choosing for
        lay.addSpacing(4)
        lay.addWidget(_section("Panel"))
        prow = QHBoxLayout()
        prow.setSpacing(3)
        self.panel_group = QButtonGroup(self)
        self.panel_group.setExclusive(True)
        self.panel_btns = []
        for i in range(6):
            b = _chip(str(i + 1))
            self.panel_group.addButton(b, i)
            self.panel_btns.append(b)
            prow.addWidget(b)
        self.panel_group.idClicked.connect(self._panel_clicked)
        lay.addLayout(prow)

        # products by category
        self.prod_btns = {}
        for cat in catalog.CATEGORIES:
            lay.addSpacing(4)
            lay.addWidget(_section(cat))
            grid = QGridLayout()
            grid.setSpacing(3)
            prods = [p for p in catalog.PRODUCTS if p.category == cat]
            for k, p in enumerate(prods):
                b = _chip(p.short, p.name + (f"\n{p.description}" if p.description else ""))
                b.clicked.connect(lambda _=False, pid=p.id: self._product_clicked(pid))
                grid.addWidget(b, k // 4, k % 4)
                self.prod_btns[p.id] = b
            lay.addLayout(grid)

        # tilts
        lay.addSpacing(4)
        self.tilt_title = _section("Tilt")
        lay.addWidget(self.tilt_title)
        self.tilt_grid = QGridLayout()
        self.tilt_grid.setSpacing(3)
        self.tilt_btns = []
        self._tilt_labels = None
        lay.addLayout(self.tilt_grid)

        # colour table + storm motion
        lay.addSpacing(4)
        lay.addWidget(_section("Colour table"))
        self.ct_label = QLabel()
        self.ct_label.setWordWrap(True)
        lay.addWidget(self.ct_label)
        row = QHBoxLayout()
        load = QPushButton("Load…")
        reset = QPushButton("Default")
        load.clicked.connect(lambda: self.main._load_pal_for(self._active_pid()))
        reset.clicked.connect(lambda: self.main._reset_pal_for(self._active_pid()))
        row.addWidget(load)
        row.addWidget(reset)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_hint("Tip: drag a .pal file onto a radar panel."))
        lay.addSpacing(4)
        lay.addWidget(_section("Storm motion"))
        self.sm_btn = QPushButton()
        self.sm_btn.setToolTip("Storm motion used for storm-relative velocity")
        self.sm_btn.clicked.connect(self.main.edit_storm_motion)
        lay.addWidget(self.sm_btn)
        lay.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self.refresh)
        main.stateChanged.connect(self._timer.start)

    def _active_pid(self):
        v = self.main.view
        return v.panels[min(v.active_panel, len(v.panels) - 1)].product

    def _panel_clicked(self, i):
        self.main.set_active_panel(i)

    def _product_clicked(self, pid):
        v = self.main.view
        self.main.set_panel_product(min(v.active_panel, len(v.panels) - 1), pid)

    def _tilt_clicked(self, i):
        els = self.main._tilt_elevs(self.main.current_frame())
        if 0 <= i < len(els):
            self.main._tilt_chosen(i)

    def refresh(self):
        m = self.main
        v = m.view
        n = len(v.panels)
        act = min(v.active_panel, n - 1)
        for i, b in enumerate(self.panel_btns):
            b.setVisible(i < n)
            if i < n:
                b.setToolTip(catalog.get(v.panels[i].product).name)
        self.panel_btns[act].setChecked(True)
        pid = v.panels[act].product
        for k, b in self.prod_btns.items():
            b.setChecked(k == pid)
        # radar info
        site = get_site(m.data.site_id)
        frame = m.current_frame()
        lines = [f"<b style='font-size:13px'>{m.data.site_id}</b>" +
                 (f" &nbsp;{site.place}, {site.state}" if site else "")]
        mode = {"live": "<span style='color:#46c86e'>● Live</span>", "archive": "Archive", "local": "Local files",
                "idle": "Idle"}.get(m.data.mode, m.data.mode)
        if frame is not None:
            vol = frame.level2_if_ready()        # never wait for a decode on the UI thread
            age = ""
            if m.data.mode == "live":
                mins = (datetime.now(timezone.utc) - frame.time).total_seconds() / 60
                age = f" &nbsp;({mins:.0f} min ago)"
            lines.append(f"Volume {frame.time:%Y-%m-%d %H:%M:%S}Z{age}")
            vcp = f"VCP {vol.vcp}" if vol is not None and vol.vcp else ""
            lines.append(" · ".join(x for x in (mode, vcp, f"frame {m.frame_index + 1}/{len(m.data.frames)}") if x))
        else:
            lines.append(mode + " · no data yet")
        img = v.panels[act].image
        if img is not None and img.nyquist:
            lines.append(f"Nyquist {img.nyquist * 1.943844:.0f} kt")
        self.info.setText("<br>".join(lines))
        # tilts
        labels = []
        if frame is not None:
            if frame.has_level2():
                t = m.engine.tilt_meta(frame)
                labels = [lab for _el, lab in t] if t else []
            else:
                labels = [f"{e:.1f}°" for e in (0.5, 0.9, 1.3, 1.8)]
        if labels != self._tilt_labels:
            self._tilt_labels = labels
            for b in self.tilt_btns:
                self.tilt_grid.removeWidget(b)
                b.deleteLater()
            self.tilt_btns = []
            for i, lab in enumerate(labels):
                b = _chip(lab.replace(" ×", "×"), "Show this elevation angle")
                b.clicked.connect(lambda _=False, i=i: self._tilt_clicked(i))
                self.tilt_grid.addWidget(b, i // 4, i % 4)
                self.tilt_btns.append(b)
        els = m._tilt_elevs(frame)
        cur = int(np.argmin(np.abs(np.array(els) - m.tilt_elev))) if els else -1
        for i, b in enumerate(self.tilt_btns):
            b.setChecked(i == cur)
        self.tilt_title.setText("TILT" + ("" if catalog.get(pid).tilted else "  (not used by this product)"))
        # colour table
        path = m.settings["palette_overrides"].get(pid)
        import os
        self.ct_label.setText(f"<b>{catalog.get(pid).name}</b>: " +
                              (os.path.basename(path) if path else f"default ({catalog.get(pid).palette})"))
        s = m.settings
        self.sm_btn.setText(f"Storm motion  {s['storm_motion_dir']:03.0f}° / {s['storm_motion_kts']:.0f} kt")


# --------------------------------------------------------------------------- #
# Warnings + storm reports
# --------------------------------------------------------------------------- #
def _left(t_end, now):
    if t_end is None:
        return ""
    s = (t_end - now).total_seconds()
    if s <= 0:
        return "expired"
    m = int(s // 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} m"


class WarningsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        # --- warnings tab
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.setContentsMargins(6, 6, 6, 6)
        frow = QHBoxLayout()
        frow.setSpacing(3)
        self.filters = {}
        for key, label, events in FILTERS:
            cb = _chip(label, "Show on the map and in this list: " + ", ".join(events))
            cb.setChecked(self._filter_on(key))
            cb.toggled.connect(lambda on, k=key: self._filter_changed(k, on))
            self.filters[key] = cb
            frow.addWidget(cb)
        wl.addLayout(frow)
        row2 = QHBoxLayout()
        self.in_view = QCheckBox("Only in view")
        self.in_view.setChecked(bool(main.settings["warnings_in_view"]))
        self.in_view.setToolTip("Only list warnings that overlap the area you're looking at")
        self.in_view.toggled.connect(self.refresh)
        self.in_view.toggled.connect(lambda on: (main.settings.__setitem__("warnings_in_view", on),
                                                 main.settings.save()))
        row2.addWidget(self.in_view)
        row2.addStretch(1)
        self.count = QLabel()
        row2.addWidget(self.count)
        ref = QToolButton()
        ref.setText("Refresh")
        ref.setToolTip("Download warnings now")
        ref.clicked.connect(lambda: main.warnings.refresh(force=True))
        row2.addWidget(ref)
        wl.addLayout(row2)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Warning", "Office", "Left", "Details"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.itemSelectionChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(self._zoom)
        wl.addWidget(self.tree, 1)
        wl.addWidget(_hint("Click a warning to highlight it, double-click to go to it (switches to the nearest radar)."))
        self.tabs.addTab(w, "Warnings")
        # --- reports tab
        r = QWidget()
        rl = QVBoxLayout(r)
        rl.setContentsMargins(6, 6, 6, 6)
        self.rstack = QStackedWidget()
        off = QWidget()
        ol = QVBoxLayout(off)
        ol.addWidget(_hint("Local storm reports are turned off."))
        onb = QPushButton("Show storm reports")
        onb.clicked.connect(lambda: main.overlay_acts["reports"].setChecked(True))
        ol.addWidget(onb)
        ol.addStretch(1)
        rep = QWidget()
        repl = QVBoxLayout(rep)
        repl.setContentsMargins(0, 0, 0, 0)
        orow = QHBoxLayout()
        orow.setSpacing(3)
        self.rhours = QComboBox()
        for h in (1, 3, 6, 12, 24):
            self.rhours.addItem(f"Last {h} h", h)
        self.rhours.setToolTip("How far back live storm reports go")
        self.rhours.activated.connect(lambda i: main.set_report_hours(self.rhours.itemData(i)))
        orow.addWidget(self.rhours)
        self.rtypes = {}
        for g, label in feeds.REPORT_GROUPS:
            cb = _chip(label, f"Show {label.lower()} reports" + (" (rain, snow, …)" if g == "other" else ""))
            cb.toggled.connect(lambda on, g=g: main.set_report_type(g, on))
            self.rtypes[g] = cb
            orow.addWidget(cb)
        repl.addLayout(orow)
        self.rtree = QTreeWidget()
        self.rtree.setHeaderLabels(["Time", "Report", "Where"])
        self.rtree.setRootIsDecorated(False)
        self.rtree.setAlternatingRowColors(True)
        self.rtree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.rtree.itemDoubleClicked.connect(self._zoom_report)
        repl.addWidget(self.rtree, 1)
        repl.addWidget(_hint("Double-click a report to go to it. T tornado, FC funnel cloud, WC wall cloud, H hail, "
                             "W wind damage, G wind gust, F flooding. Older reports are fainter."))
        self.rstack.addWidget(off)
        self.rstack.addWidget(rep)
        self.sync_report_options()
        rl.addWidget(self.rstack)
        self.tabs.addTab(r, "Reports")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)
        self._items = {}
        main.warnings.changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(30_000)
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(400)
        self._view_timer.timeout.connect(self.refresh)
        main.view.viewChanged.connect(lambda: self._view_timer.start() if self.in_view.isChecked() else None)
        main.stateChanged.connect(lambda: self._view_timer.start() if main.data.mode != "live" else None)

    def _now(self):
        m = self.main
        if m.data.mode == "live" or m.warnings.frame_time is None:
            return datetime.now(timezone.utc)
        return m.warnings.frame_time

    def _alert_xy(self, a):
        view = self.main.view
        return self.main.warnings._xy(a, view.lat0, view.lon0)

    # the buttons are the same switches the map uses (Watches = Map -> Watches)
    def _filter_on(self, key):
        s = self.main.settings
        if key == "WAT":
            return bool(s["overlays"].get("watches", True))
        return bool((s["warning_types"] or {}).get(key, True))

    def _filter_changed(self, key, on):
        m = self.main
        s = m.settings
        if key == "WAT":
            s["overlays"]["watches"] = on
            act = getattr(m, "overlay_acts", {}).get("watches")
            if act is not None:
                act.blockSignals(True)
                act.setChecked(on)
                act.blockSignals(False)
            if on:
                m.warnings.refresh(force=True)
        else:
            types = dict(s["warning_types"] or {})
            types[key] = on
            s["warning_types"] = types
        s.save()
        m.view.update()
        self.refresh()

    def sync_filters(self):
        """Match the buttons to the settings (after the Map menu changed them)."""
        for key, cb in self.filters.items():
            cb.blockSignals(True)
            cb.setChecked(self._filter_on(key))
            cb.blockSignals(False)
        self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        m = self.main
        alerts = [a for a in m.warnings.active_alerts() if m.warnings.visible(a)]
        if self.in_view.isChecked() and m.view.panels:
            vt = m.view.transform(m.view.panels[min(m.view.active_panel, len(m.view.panels) - 1)])
            x0, y0, x1, y1 = vt.world_bounds()
            keep = []
            for a in alerts:
                for xy in self._alert_xy(a):
                    if xy[:, 0].max() >= x0 and xy[:, 0].min() <= x1 and xy[:, 1].max() >= y0 and \
                            xy[:, 1].min() <= y1:
                        keep.append(a)
                        break
            alerts = keep
        now = self._now()
        alerts.sort(key=lambda a: (-a.style[3], a.expires or now))
        sel = m.warnings.selected_uid
        self.tree.blockSignals(True)
        self.tree.clear()
        self._items = {}
        for a in alerts:
            name = a.variant_label.replace("Severe Thunderstorm", "Severe T-storm").replace(" - ", " – ")
            it = QTreeWidgetItem([f"{name}  ({a.variant})", a.office, _left(a.expires, now),
                                  ", ".join(a.tags) or a.area[:80]])
            it.setIcon(0, _swatch(m.warnings.color(a)))
            it.setToolTip(0, a.hover)
            it.setToolTip(3, a.area)
            it.setData(0, Qt.UserRole, a.uid)
            if a.event.startswith("Tornado") and a.event.endswith(("Warning", "Emergency")):
                f = it.font(0)
                f.setBold(True)
                it.setFont(0, f)
            self.tree.addTopLevelItem(it)
            self._items[a.uid] = a
            if a.uid == sel:
                it.setSelected(True)
        self.tree.blockSignals(False)
        total = len(m.warnings.active_alerts())
        self.count.setText(f"{len(alerts)} shown / {total}")
        self.tabs.setTabText(0, f"Warnings ({len(alerts)})")
        self._refresh_reports()

    def sync_report_options(self):
        """Match the reports tab's controls to the settings (after the Map menu changed them)."""
        s = self.main.settings
        i = self.rhours.findData(int(s["report_hours"] or 3))
        if i >= 0:
            self.rhours.setCurrentIndex(i)
        for g, cb in self.rtypes.items():
            cb.blockSignals(True)
            cb.setChecked(bool((s["report_types"] or {}).get(g, g != "other")))
            cb.blockSignals(False)
        if self.isVisible():
            self._refresh_reports()

    def _refresh_reports(self):
        m = self.main
        on = bool(m.settings["overlays"].get("reports", False))
        self.rstack.setCurrentIndex(1 if on else 0)
        self.rtree.clear()
        if not on:
            self.tabs.setTabText(1, "Reports")
            return
        reps = sorted(m.warnings.visible_reports(), key=lambda r: r["time"] or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
        for r in reps:
            first = r["hover"].split("\n")
            if r.get("source") == "Spotter Network" and len(first) > 1:
                first = first[1:]                       # skip the "Spotter Network report" heading
            letter, rgb, label, _g = feeds.REPORT_KINDS.get(r.get("kind", "other"), feeds.REPORT_KINDS["other"])
            it = QTreeWidgetItem([f"{r['time']:%H:%MZ}" if r["time"] else "", f"{letter}  {first[0].strip()}",
                                  first[1] if len(first) > 1 else r.get("source", "")])
            it.setIcon(1, _swatch(rgb))
            it.setToolTip(1, r["hover"])
            it.setData(0, Qt.UserRole, (r["lat"], r["lon"]))
            self.rtree.addTopLevelItem(it)
        self.tabs.setTabText(1, f"Reports ({len(reps)})")

    def _selected(self):
        items = self.tree.selectedItems()
        self.main.warnings.selected_uid = items[0].data(0, Qt.UserRole) if items else None
        self.main.view.update()

    def _zoom(self, item, _col=0):
        a = self._items.get(item.data(0, Qt.UserRole))
        if a is None:
            return
        m = self.main
        if m.settings["go_to_nearest_radar"]:
            # switch to the radar nearest the warning first (Settings -> Warnings)
            lat, lon = a.centroid()
            near = nearest_site(lat, lon)
            if near is not None and near.id != m.data.site_id:
                m.switch_site(near.id)
        xy = np.concatenate(self._alert_xy(a))
        _zoom_to(m, xy[:, 0], xy[:, 1])
        m.warnings.selected_uid = a.uid
        m.view.update()

    def _zoom_report(self, item, _col=0):
        lat, lon = item.data(0, Qt.UserRole)
        view = self.main.view
        x, y = aeqd_forward(lat, lon, view.lat0, view.lon0)
        view.set_view(float(x), float(y), max(view.scale, 4.0))

    def showEvent(self, ev):
        super().showEvent(ev)
        self.refresh()


# --------------------------------------------------------------------------- #
# Storm cells (Level III SCIT / HDA / MDA / TVS)
# --------------------------------------------------------------------------- #
CELL_CODES = ("NST", "NHI", "NMD", "NTV")


def storm_cells(frame):
    """Rows describing storm cells in a frame's Level III products."""
    if frame is None:
        return None, []
    l3 = frame.l3
    nst = l3.get("NST")
    if nst is None:
        return None, []
    cells = []
    for g in nst.graphics:
        if g["kind"] != "storm":
            continue
        motion = None
        f = g.get("fcst")
        if f and len(f) > 1:
            mins = 15 * (len(f) - 1)
            dx, dy = f[-1][0] - f[0][0], f[-1][1] - f[0][1]
            spd = math.hypot(dx, dy) / (mins / 60) / 1.852
            motion = ((math.degrees(math.atan2(dx, dy)) + 180) % 360, spd)
        cells.append(dict(id=g.get("id") or "?", x=g["x"], y=g["y"], motion=motion, posh=None, poh=None,
                          size=None, meso=None, meso_elev=False, tvs=None))

    def nearest(x, y, maxd=8.0):
        best, bd = None, maxd
        for c in cells:
            d = math.hypot(c["x"] - x, c["y"] - y)
            if d < bd:
                best, bd = c, d
        return best
    hi = l3.get("NHI")
    if hi is not None:
        for g in hi.graphics:
            if g["kind"] == "hail":
                c = next((c for c in cells if c["id"] == g.get("id")), None) or nearest(g["x"], g["y"], 2.0)
                if c is not None:
                    c.update(posh=g.get("posh"), poh=g.get("poh"), size=g.get("size"))
    md = l3.get("NMD")
    if md is not None:
        for g in md.graphics:
            if g["kind"] == "meso":
                c = next((c for c in cells if g.get("storm") and c["id"] == g["storm"]), None) or \
                    nearest(g["x"], g["y"])
                rank = g.get("rank")
                if c is not None and rank is not None:
                    if c["meso"] is None or rank > c["meso"]:
                        c["meso"], c["meso_elev"] = rank, bool(g.get("elevated"))
    tv = l3.get("NTV")
    if tv is not None:
        for g in tv.graphics:
            if g["kind"] == "tvs":
                c = nearest(g["x"], g["y"])
                if c is not None:
                    c["tvs"] = "ETVS" if g.get("elevated") else "TVS"
    for c in cells:
        c["score"] = (3 if c["tvs"] == "TVS" else 2 if c["tvs"] else 0) * 100 + (c["meso"] or 0) * 5 + \
                     (c["posh"] or 0) / 10
    cells.sort(key=lambda c: -c["score"])
    return nst, cells


class CellsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.hint = _hint("")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Cell", "Az/Rng", "Motion", "POSH", "Hail", "Meso", "TVS"])
        hdr = self.tree.headerItem()
        for col, tip in enumerate(["SCIT storm ID", "Azimuth (°) / range from the radar",
                                   "Direction the cell is moving from (°) / speed (kt)",
                                   "Probability of severe hail", "Maximum expected hail size",
                                   "Mesocyclone strength rank (MDA); 'E' = elevated",
                                   "Tornado vortex signature (ETVS = elevated)"]):
            hdr.setToolTip(col, tip)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(False)
        for i in range(7):
            self.tree.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._zoom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.hint)
        lay.addWidget(self.tree, 1)
        lay.addWidget(_hint("Sorted by threat. Double-click a cell to centre on it."))
        self._prod = None
        self._key = None
        main.stateChanged.connect(self.refresh)

    def refresh(self):
        if not self.isVisible():
            return
        m = self.main
        frame = m.current_frame()
        key = (frame.uid, frame.revision) if frame is not None else None
        if key == self._key:
            return
        self._key = key
        nst, cells = storm_cells(frame)
        self._prod = nst
        self.tree.clear()
        if nst is None:
            self.hint.setText("No Level III storm-cell data for this frame yet. It downloads automatically while "
                              "this tab is open (live and archive modes, 2020 onwards).")
            self.hint.show()
            return
        self.hint.setText(f"{len(cells)} cells from {nst.site or m.data.site_id} "
                          f"at {(nst.vol_time or nst.time):%H:%M}Z")
        du = m.settings["distance_units"]
        self.tree.headerItem().setText(1, f"Az/Rng {du}")
        self.tree.headerItem().setText(2, "Motion kt")
        for c in cells:
            az = (math.degrees(math.atan2(c["x"], c["y"])) + 360) % 360
            rng = math.hypot(c["x"], c["y"]) / UNIT_F[du]
            mot = f"{c['motion'][0]:03.0f}/{c['motion'][1]:.0f}" if c["motion"] else "—"
            posh = f"{c['posh']}%" if c["posh"] else "—"
            size = f"{c['size']:.2f}\"" if c["size"] else "—"
            meso = (f"{c['meso']}" + (" E" if c["meso_elev"] else "")) if c["meso"] is not None else "—"
            it = QTreeWidgetItem([c["id"], f"{az:03.0f}/{rng:.0f}", mot, posh, size, meso, c["tvs"] or "—"])
            it.setData(0, Qt.UserRole, (c["x"], c["y"]))
            if c["tvs"] == "TVS":
                for col in range(7):
                    it.setForeground(col, QColor(255, 110, 110))
            elif c["meso"] and c["meso"] >= 5:
                it.setForeground(5, QColor(255, 170, 60))
            if c["posh"] and c["posh"] >= 50:
                it.setForeground(3, QColor(255, 220, 80))
            self.tree.addTopLevelItem(it)

    def _zoom(self, item, _col=0):
        if self._prod is None:
            return
        x, y = item.data(0, Qt.UserRole)
        view = self.main.view
        ox, oy = aeqd_forward(self._prod.lat, self._prod.lon, view.lat0, view.lon0)
        view.set_view(float(ox) + x, float(oy) + y, max(view.scale, 5.0))

    def showEvent(self, ev):
        super().showEvent(ev)
        self._key = None
        self.main._update_l3_needs()
        self.refresh()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        QTimer.singleShot(0, self.main._update_l3_needs)     # stop downloading cell products


# --------------------------------------------------------------------------- #
# Layers & overlays (mirrors the View / Overlays menu actions)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Cursor inspector
# --------------------------------------------------------------------------- #
class InspectorPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.loc = QLabel("Move the mouse over the map.")
        self.loc.setTextFormat(Qt.RichText)
        self.loc.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Panel", "Value"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.setMinimumHeight(80)
        self.loc.setProperty("role", "card")
        self.under = QLabel()
        self.under.setWordWrap(True)
        self.under.setTextFormat(Qt.PlainText)
        self.under.setProperty("role", "card")
        self.under.hide()
        self.tree.setAlternatingRowColors(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.loc)
        lay.addWidget(_section("Values under the cursor"))
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.under)
        self.learn = QLabel()
        self.learn.setWordWrap(True)
        self.learn.setTextFormat(Qt.PlainText)
        self.learn.setProperty("role", "card")
        self.learn.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.learn.hide()
        lay.addWidget(self.learn, 1)
        main.cursorInfo.connect(self.update_info)

    def update_info(self, info):
        if not self.isVisible():
            return
        if not info:
            return
        self.loc.setText(info["loc_html"])
        rows = info["values"]
        if self.tree.topLevelItemCount() != len(rows):
            self.tree.clear()
            for _ in rows:
                self.tree.addTopLevelItem(QTreeWidgetItem(["", ""]))
        for i, (name, val) in enumerate(rows):
            it = self.tree.topLevelItem(i)
            it.setText(0, name)
            it.setText(1, val)
        under = info.get("under")
        self.under.setVisible(bool(under))
        if under:
            self.under.setText(under)
        learn = info.get("learn")
        self.learn.setVisible(bool(learn))
        if learn:
            self.learn.setText("What this means\n" + learn)

