"""Warnings and storm reports list."""
from __future__ import annotations

import numpy as np
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QStackedWidget, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...data import feeds
from ...data.sites import nearest_site
from ...overlays.warnings import FILTERS
from ...products.geometry import aeqd_forward
from .common import _chip, _hint, _swatch, _zoom_to


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
        self.in_view.setChecked(True)
        self.in_view.setToolTip("Only list warnings that overlap the area you're looking at")
        self.in_view.toggled.connect(self.refresh)
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
