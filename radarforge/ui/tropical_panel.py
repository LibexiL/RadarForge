"""The Tropical side panel: active storms (details, arrival times, links), what's drawn, past storms and
historic tracks."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ..features import tropical as tr


def _caption(text):
    lab = QLabel(text.upper())
    lab.setProperty("role", "qsection")
    f = lab.font()
    f.setBold(True)
    lab.setFont(f)
    return lab


def _hint(text):
    lab = QLabel(text)
    lab.setProperty("role", "hint")
    lab.setWordWrap(True)
    return lab


class TropicalPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.ov = main.tropical
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        from .quick_panel import _setup_button
        lay.addWidget(_setup_button(main, "tropical", "Set up the map for tropical weather"))
        lay.addWidget(_hint("Turns on storms with their cones, wind fields, model tracks, the outlook, buoys and "
                            "hurricane / surge warnings."))
        lay.addWidget(_caption("Active storms"))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda cur, _o: self._select(cur))
        self.list.itemDoubleClicked.connect(lambda it: self.go_to())
        lay.addWidget(self.list)
        self.empty = _hint("No active storms right now. Turn on the outlook to see areas the National Hurricane "
                           "Center is watching.")
        lay.addWidget(self.empty)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setOpenExternalLinks(True)
        self.detail.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lay.addWidget(self.detail)
        row = QHBoxLayout()
        self.go_btn = QPushButton("Go to storm")
        self.go_btn.setToolTip("Centre the map on the storm and switch to the nearest radar if it's in range")
        self.go_btn.clicked.connect(self.go_to)
        row.addWidget(self.go_btn)
        self.sat_btn = QPushButton("Satellite on storm")
        self.sat_btn.setToolTip("GOES satellite under the radar, using the 1-minute 'mesoscale' view aimed at the "
                                "storm when there is one")
        self.sat_btn.clicked.connect(self.follow_satellite)
        row.addWidget(self.sat_btn)
        lay.addLayout(row)

        lay.addWidget(_caption("Show"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.checks = {}
        items = [("tropical", "Storms, cones && tracks"), ("tropical_wind", "Wind field"),
                 ("tropical_spaghetti", "Model tracks"), ("tropical_outlook", "Outlook (areas to watch)"),
                 ("buoys", "Buoys && stations"), ("TRO", "Hurricane / surge warnings"), ("qpe72", "Rainfall, 72 h")]
        for i, (key, label) in enumerate(items):
            cb = QCheckBox(label)
            cb.toggled.connect(lambda on, k=key: self._toggle(k, on))
            grid.addWidget(cb, i // 2, i % 2)
            self.checks[key] = cb
        lay.addLayout(grid)
        lay.addWidget(_hint("Model tracks: the official forecast (white) with GFS, HAFS, Canadian, UK and others. "
                            "Hurricane / surge warnings shade the counties under them; the coast is outlined in "
                            "NHC's colours when 'Storms' is on."))

        lay.addWidget(_caption("Tropical model maps"))
        mgrid = QGridLayout()
        mgrid.setHorizontalSpacing(4)
        mgrid.setVerticalSpacing(4)
        sets = [("Tropical set: 4 maps", ["mslp", "wind10", "pwat", "qpf"],
                 "GFS over the Atlantic tropics: radar and pressure, wind, precipitable water, total rain"),
                ("Pressure && radar", ["mslp"], "Where the low is heading, with the model's rain bands"),
                ("10 m wind", ["wind10"], "How strong the wind gets, with barbs"),
                ("Total rain", ["qpf"], "Rain from the start of the run: where flooding is possible"),
                ("Steering (500 mb)", ["h500"], "The mid-level pattern that steers the storm")]
        for i, (label, fields, tip) in enumerate(sets):
            b = QPushButton(label)
            b.setToolTip(tip + ". Opens the Models view (F6 switches back).")
            if i == 0:
                f = b.font()
                f.setBold(True)
                b.setFont(f)
            b.clicked.connect(lambda _=False, fl=fields: main.open_model_set(fl, "gfs", "Atlantic tropics"))
            if i == 0:
                mgrid.addWidget(b, 0, 0, 1, 2)
            else:
                mgrid.addWidget(b, 1 + (i - 1) // 2, (i - 1) % 2)
        lay.addLayout(mgrid)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        lay.addWidget(line)
        lay.addWidget(_caption("Past storms"))
        prow = QHBoxLayout()
        self.basin = QComboBox()
        self.basin.addItem("Atlantic", "AL")
        self.basin.addItem("East Pacific", "EP")
        self.basin.currentIndexChanged.connect(lambda _i: self.fill_past())
        prow.addWidget(self.basin)
        self.year = QSpinBox()
        this_year = datetime.now(timezone.utc).year
        self.year.setRange(1950, this_year)
        self.year.setValue(this_year - 1)
        self.year.valueChanged.connect(lambda _v: self.fill_past())
        prow.addWidget(self.year)
        lay.addLayout(prow)
        self.past = QComboBox()
        self.past.setToolTip("Storms of that season, strongest first")
        lay.addWidget(self.past)
        brow = QHBoxLayout()
        self.replay_btn = QPushButton("Replay landfall")
        self.replay_btn.setToolTip("Load the archive radar nearest its landfall (or its strongest point near land) "
                                   "with its best track and the NHC forecast of the time")
        self.replay_btn.clicked.connect(self.replay)
        brow.addWidget(self.replay_btn)
        self.track_btn = QPushButton("Track only")
        self.track_btn.setToolTip("Draw its whole best track without loading radar")
        self.track_btn.clicked.connect(self.track_only)
        brow.addWidget(self.track_btn)
        lay.addLayout(brow)
        self.past_status = _hint("")
        lay.addWidget(self.past_status)

        lay.addWidget(_caption("Historic tracks near a place"))
        hrow = QHBoxLayout()
        self.radius = QComboBox()
        for mi in (50, 100, 150, 250):
            self.radius.addItem(f"within {mi} mi", mi)
        self.radius.setCurrentIndex(1)
        hrow.addWidget(self.radius)
        self.hist_me = QPushButton("Near me")
        self.hist_me.setToolTip("Hurricanes and tropical storms since 1950 that passed near your location")
        self.hist_me.clicked.connect(lambda: self.history(True))
        hrow.addWidget(self.hist_me)
        self.hist_map = QPushButton("Map centre")
        self.hist_map.clicked.connect(lambda: self.history(False))
        hrow.addWidget(self.hist_map)
        lay.addLayout(hrow)
        self.hist_status = _hint("")
        lay.addWidget(self.hist_status)
        self.clear_btn = QPushButton("Clear past tracks")
        self.clear_btn.clicked.connect(self.clear_past)
        lay.addWidget(self.clear_btn)
        lay.addStretch(1)

        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sa)
        self._past_storms = []
        self.ov.changed.connect(self.sync)
        self.sync()

    # ---------------------------------------------------------------- active storms
    def sync(self):
        s = self.main.settings
        for key, cb in self.checks.items():
            if key == "TRO":
                on = bool((s["warning_types"] or {}).get("TRO", True)) and bool(s["overlays"].get("warnings", True))
            elif key == "qpe72":
                on = bool(s["overlays"].get("mrms")) and s["mrms_product"] == "qpe72"
            else:
                on = bool(s["overlays"].get(key, key == "tropical_wind"))
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        cur = self.ov.selected
        self.list.blockSignals(True)
        self.list.clear()
        for st in self.ov.storms:
            mv = f", moving {tr.compass(st.move_dir)} {st.move_kt:.0f} kt" if st.move_dir is not None and st.move_kt else ""
            it = QListWidgetItem(f"● {st.title} – {st.wind:.0f} kt" + (f", {st.pressure:.0f} mb" if st.pressure else "")
                                 + mv)
            from PySide6.QtGui import QColor
            it.setForeground(QColor(*tr.cat_color(st.wind)))
            it.setData(Qt.UserRole, st.id)
            self.list.addItem(it)
            if st.id == cur:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        has = bool(self.ov.storms)
        self.list.setFixedHeight(min(140, 8 + 24 * max(1, self.list.count())))
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if has and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        self._select(self.list.currentItem())

    def storm(self):
        sid = self.ov.selected
        return next((s for s in self.ov.storms if s.id == sid), None)

    def _select(self, item):
        sid = item.data(Qt.UserRole) if item is not None else None
        self.ov.selected = sid
        st = self.storm()
        self.go_btn.setEnabled(st is not None)
        self.sat_btn.setEnabled(st is not None)
        if st is None:
            self.detail.setText("")
            return
        lines = [f"<b>{st.title}</b> · {tr.cat_label(st.wind)}",
                 f"{st.wind:.0f} kt ({st.wind * 1.15078:.0f} mph)" + (f", {st.pressure:.0f} mb" if st.pressure else "")
                 + (f" · moving {tr.compass(st.move_dir)} at {st.move_kt:.0f} kt" if st.move_dir is not None and
                    st.move_kt else ""),
                 f"{abs(st.lat):.1f}°{'N' if st.lat >= 0 else 'S'} {abs(st.lon):.1f}°{'W' if st.lon < 0 else 'E'}"
                 + (f" · advisory {st.advisory}, {st.updated.astimezone():%a %-I:%M %p}" if st.updated else "")]
        loc = self.main.my_location.latlon() if hasattr(self.main, "my_location") else None
        if loc is not None and st.forecast:
            a = tr.arrival(st, loc[0], loc[1])
            if a is not None:
                mi = a["closest_km"] / 1.609344
                lines.append(f"<br><b>For your location</b>: closest about {a['closest_time'].astimezone():%a %-I %p}, "
                             f"{mi:.0f} mi {tr.compass(a['closest_dir'])} of you"
                             + (f" ({tr.cat_label(a['closest_wind']).lower()})" if a['closest_wind'] else ""))
                if a["ts_arrive"] is not None:
                    lines.append(f"Tropical-storm-force winds about {a['ts_arrive'].astimezone():%a %-I %p} to "
                                 f"{a['ts_leave'].astimezone():%a %-I %p}")
                if a["hu_arrive"] is not None:
                    lines.append(f"<span style='color:#ff6060'><b>Hurricane-force winds possible from "
                                 f"{a['hu_arrive'].astimezone():%a %-I %p}</b></span>")
                lines.append("<i>From the official forecast track and wind radii; follow NHC and local officials.</i>")
        elif loc is None:
            lines.append("<br><i>Set your location (Location menu) to see when it gets closest to you.</i>")
        links = " · ".join(f"<a href='{u}'>{k}</a>" for k, u in st.links.items())
        if links:
            lines.append("<br>" + links)
        self.detail.setText("<br>".join(lines))

    def go_to(self):
        st = self.storm()
        if st is not None:
            self.main.go_to_latlon(st.lat, st.lon, radar=True)

    def follow_satellite(self):
        st = self.storm()
        if st is None:
            return
        self.main.settings["satellite_sector"] = "storm"
        self.main.satellite.follow = (st.lat, st.lon)
        if not self.main.settings["overlays"].get("satellite"):
            self.main.overlay_acts["satellite"].setChecked(True)
        else:
            self.main.satellite.invalidate()
            self.main.satellite.refresh(force=True)
        self.main._status_msg(f"Satellite: following {st.name} (1-minute mesoscale view when one covers it)")

    def _toggle(self, key, on):
        m = self.main
        if key == "TRO":
            m.set_warning_type("TRO", on)
        elif key == "qpe72":
            if on:
                m.set_mrms_product("qpe72")
                if not m.settings["overlays"].get("mrms"):
                    m.overlay_acts["mrms"].setChecked(True)
            elif m.settings["mrms_product"] == "qpe72":
                m.overlay_acts["mrms"].setChecked(False)
        elif key in m.overlay_acts:
            m.overlay_acts[key].setChecked(on)
        else:
            m._toggle_overlay(key, on)

    # ---------------------------------------------------------------- past storms
    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._past_storms and self.past.count() == 0:
            self.fill_past()

    def fill_past(self):
        basin, year = self.basin.currentData(), self.year.value()
        self.past.clear()
        self.past_status.setText("Loading the storm list…")
        self.main.run_bg(lambda: self._season(basin, year), self._season_done)

    @staticmethod
    def _season(basin, year):
        this = datetime.now(timezone.utc).year
        storms = [s for s in tr.hurdat_storms(basin) if s.year == year]
        if not storms and year >= this - 1:
            storms = tr.recent_season(basin.lower(), year)
        return storms

    def _season_done(self, storms, err):
        if err is not None:
            self.past_status.setText(f"⚠ Couldn't get the storm list: {err}")
            return
        storms = sorted(storms, key=lambda s: -s.peak)
        self._past_storms = storms
        self.past.clear()
        for s in storms:
            self.past.addItem(s.title)
        self.past_status.setText(f"{len(storms)} storms that season." if storms else "No storms found for that season.")

    def _picked(self):
        i = self.past.currentIndex()
        return self._past_storms[i] if 0 <= i < len(self._past_storms) else None

    def replay(self):
        ps = self._picked()
        if ps is not None:
            self.main.replay_storm(ps)

    def track_only(self):
        ps = self._picked()
        if ps is not None:
            self.ov.set_history([(ps, 0.0)], None)
            t, lat, lon = tr.key_moment(ps)
            self.main.go_to_latlon(lat, lon, radar=False)
            self.main.view.update()

    def history(self, near_me):
        if near_me:
            loc = self.main.my_location.latlon()
            if loc is None:
                self.hist_status.setText("Set your location first (Location menu, or right-click the map).")
                return
        else:
            loc = self.main.view.world_to_latlon(self.main.view.cx, self.main.view.cy)
        km = float(self.radius.currentData()) * 1.609344
        basin = self.basin.currentData()
        self.hist_status.setText("Searching every storm since 1950…")

        def work():
            return tr.tracks_near(tr.hurdat_storms(basin), loc[0], loc[1], km)

        def done(items, err):
            if err is not None:
                self.hist_status.setText(f"⚠ {err}")
                return
            self.ov.set_history(items, loc)
            hurricanes = sum(1 for s, _d in items if s.peak >= 64)
            self.hist_status.setText(f"{len(items)} storms passed within {self.radius.currentData()} mi since 1950 "
                                     f"({hurricanes} were hurricanes at some point). Coloured by strength.")
            self.main.view.update()
        self.main.run_bg(work, done)

    def clear_past(self):
        self.ov.set_history([], None)
        self.main.replay_storm(None)
        self.hist_status.setText("")
        self.main.view.update()
