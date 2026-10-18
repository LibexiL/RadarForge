"""The Models view (Pivotal-Weather style model maps) and switching between it and the Radar view."""
from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QComboBox, QLabel, QMenu, QToolButton, QWidget, QHBoxLayout

from ..features import models
from ..features.model_layer import ModelLayer


class ModelsMixin:
    """Part of MainWindow: the Radar / Models switch and everything the Models view needs."""

    # ---------------------------------------------------------------- set-up
    def _init_models(self):
        self.view_mode = "radar"
        self.models = ModelLayer(self.settings, self.view, self)
        models.PALETTE_RESOLVER = self._model_palette
        self.models.changed.connect(self._models_changed)
        self.models.runsChanged.connect(self._fill_run_combo)
        self.models.status.connect(self._status_msg)
        self.models.busyChanged.connect(lambda b: self._models_busy(b))
        self.view.model_layer = self.models
        self.view.viewChanged.connect(lambda: self.models.request_soon() if self.view_mode == "models" else None)
        self._radar_view_state = None

    def _model_palette(self, name):
        pid = {"REF": "REF"}.get(name, name)
        try:
            return self.palette_for(pid)
        except Exception:
            from ..products import colortable
            return colortable.builtin(name)

    # ---------------------------------------------------------------- toolbar
    def _build_view_switch(self, tb):
        """[Radar] [Models] at the left of the toolbar: the two views of the app."""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(2, 0, 6, 0)
        h.setSpacing(0)
        self.view_group = QActionGroup(self)
        self.view_btns = {}
        for mode, text, tip, key in (("radar", "Radar", "Radar view: live and past radar data (F6 switches)", "F6"),
                                     ("models", "Models", "Models view: HRRR, NAM, RAP and GFS forecast maps – "
                                                          "severe, winter and upper-air fields (F6 switches)", None)):
            a = QAction(text, self, checkable=True)
            a.setToolTip(tip)
            a.setChecked(mode == "radar")
            a.triggered.connect(lambda _=False, m=mode: self.set_view_mode(m))
            self.view_group.addAction(a)
            b = QToolButton()
            b.setDefaultAction(a)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setProperty("role", "viewswitch")
            b.setProperty("side", "left" if mode == "radar" else "right")
            b.setMinimumWidth(0)
            h.addWidget(b)
            self.view_btns[mode] = (a, b)
            self._icon_targets.append((a, "radar" if mode == "radar" else "models"))
        sw = QAction("Switch Radar / Models", self)
        sw.setShortcut(QKeySequence("F6"))
        sw.triggered.connect(lambda: self.set_view_mode("models" if self.view_mode == "radar" else "radar"))
        self.addAction(sw)
        self._view_switch_act = sw
        tb.addWidget(box)
        tb.addSeparator()

    def _build_models_toolbar(self, tb):
        """Model, run and region pickers: shown in the Models view in place of the radar controls."""
        self._model_tb_actions = []

        def add(w):
            a = tb.addWidget(w)
            a.setVisible(False)
            self._model_tb_actions.append(a)
            return w
        lab = QLabel("  Model ")
        lab.setProperty("role", "hint")
        add(lab)
        self.model_combo = QComboBox()
        for m in models.MODELS.values():
            self.model_combo.addItem(m.name, m.id)
            self.model_combo.setItemData(self.model_combo.count() - 1, m.long, Qt.ToolTipRole)
        self.model_combo.setToolTip("Which model")
        self.model_combo.activated.connect(lambda i: self.models.set_model(self.model_combo.itemData(i)))
        add(self.model_combo)
        lab = QLabel("  Run ")
        lab.setProperty("role", "hint")
        add(lab)
        self.run_combo = QComboBox()
        self.run_combo.setMinimumContentsLength(22)
        self.run_combo.setToolTip("Which run (the newest is picked automatically)")
        self.run_combo.activated.connect(self._run_chosen)
        add(self.run_combo)
        lab = QLabel("  Area ")
        lab.setProperty("role", "hint")
        add(lab)
        self.region_combo = QComboBox()
        self.region_combo.addItem("Around the radar", "radar")
        for name in models.REGIONS:
            self.region_combo.addItem(name, name)
        self.region_combo.setToolTip("Jump to a region (you can still pan and zoom)")
        self.region_combo.activated.connect(lambda i: self.set_model_region(self.region_combo.itemData(i)))
        add(self.region_combo)
        self._sync_model_combos()

    def _sync_model_combos(self):
        if not hasattr(self, "model_combo"):
            return
        i = self.model_combo.findData(self.models.model_id)
        if i >= 0:
            self.model_combo.setCurrentIndex(i)
        j = self.region_combo.findData(self.settings["model_region"] or "CONUS")
        if j >= 0:
            self.region_combo.setCurrentIndex(j)

    def _fill_run_combo(self):
        c = self.run_combo
        c.blockSignals(True)
        c.clear()
        rl = self.models.run_list()
        cur, _h = self.models.run()
        for k, (r, hrs) in enumerate(rl):
            newest = "  (newest)" if k == 0 else ""
            c.addItem(f"{r:%HZ %a %d %b}{newest}", r)
            c.setItemData(c.count() - 1, f"{len(hrs)} forecast hours out so far (to F{max(hrs):02d})", Qt.ToolTipRole)
        if not rl:
            c.addItem("finding runs…", None)
        elif cur is not None:
            c.setCurrentIndex(max(0, [r for r, _ in rl].index(cur)))
        c.blockSignals(False)
        self._sync_model_combos()

    def _run_chosen(self, i):
        r = self.run_combo.itemData(i)
        if r is not None:
            self.models.set_run(None if i == 0 else r)

    # ---------------------------------------------------------------- the two views
    RADAR_ONLY = ("live_act", "archive_act", "open_act", "tilt_down_act", "tilt_up_act", "sm_act", "smooth_act",
                  "dealias_act", "trail_act")

    def set_view_mode(self, mode):
        if mode not in ("radar", "models"):
            return
        for m, (a, _b) in self.view_btns.items():
            a.setChecked(m == mode)
        if mode == self.view_mode:
            return
        self.view_mode = mode
        self.view.mode = mode
        models_on = mode == "models"
        if self.playing:
            self.toggle_play()
        # the toolbar: radar controls or model pickers
        tb = self.main_tb
        for name in self.RADAR_ONLY:
            a = getattr(self, name, None)
            if a is not None:
                a.setVisible(not models_on)
        self._site_btn_act.setVisible(not models_on)
        self._tilt_combo_act.setVisible(not models_on)
        for a in self.tool_group.actions():
            a.setVisible(not models_on or a.data() in ("pan", "measure"))
        for a in self._model_tb_actions:
            a.setVisible(models_on)
        self._tidy_separators(tb)
        self._frames_btn_act.setVisible(not models_on)
        if hasattr(self, "_wbar_act"):
            self._wbar_act.setVisible(not models_on)      # no warnings on the model maps
        if models_on:
            self.set_tool("pan") if self.view.tool not in ("pan", "measure") else None
            self._radar_view_state = (self.view.cx, self.view.cy, self.view.scale)
            self.models.active = True
            self._fill_run_combo()
            self.set_model_region(self.settings["model_region"] or "CONUS", save=False)
            if not self.models.run_list():
                self.models.refresh_runs()
            self.ws.show_panel("models")
            self._first_time_hint("models", "Models view: pick a field in the Models panel (or right-click a panel). "
                                            "← → step through the forecast hours, Space plays them. F6 goes back "
                                            "to the radar.")
        else:
            self.models.active = False
            self._set_site_projection(self.data.site_id)
            if self._radar_view_state:
                self.view.set_view(*self._radar_view_state)
            if self.ws.is_open("models"):
                self.ws.show_panel("products", focus=False) if self.ws.is_open("products") else None
        self._fit_toolbar()
        self._models_changed()
        if not models_on:
            self.frame_slider.blockSignals(True)
            self.frame_slider.setRange(0, max(0, len(self.data.frames) - 1))
            self.frame_slider.setValue(max(0, self.frame_index))
            self.frame_slider.blockSignals(False)
            self._show_frame()
        self.stateChanged.emit()
        self.view.update()

    def open_model_set(self, fields, model=None, region=None):
        """The Models view with these fields, one per panel (1 field: the panel layout stays as it is)."""
        if model and model in models.MODELS and model != self.models.model_id:
            self.models.set_model(model)
        if region and region in models.REGIONS:
            self.settings["model_region"] = region
            self.settings.save()
        elif not region and self.settings["model_region"] == "Atlantic tropics" and len(fields) > 1:
            self.settings["model_region"] = "CONUS"           # a severe / winter set: back over land
            self.settings.save()
        if len(fields) > 1:
            f = self.models.fields()
            for i, fid in enumerate(fields[:6]):
                f[i] = fid
            self.settings["model_fields"] = f
            self.settings.save()
        if self.view_mode != "models":
            self.set_view_mode("models")
        elif region:
            self.set_model_region(region)
        if len(fields) > 1:
            if int(self.settings["layout"] or 1) != len(fields):
                self.set_layout(len(fields))
        else:
            self.models.set_field(min(self.view.active_panel, len(self.view.panels) - 1), fields[0])
        self.models.pictures.clear()
        self.models.request_soon(0)
        self._sync_model_combos()
        if hasattr(self, "models_panel"):
            self.models_panel.sync()
        self._models_changed()

    @staticmethod
    def _tidy_separators(tb):
        """No separator at the ends, or two in a row, once some toolbar items are hidden."""
        acts = tb.actions()
        prev_sep = True
        for i, a in enumerate(acts):
            if a.isSeparator():
                later = any(b.isVisible() and not b.isSeparator() for b in acts[i + 1:])
                a.setVisible(not prev_sep and later)
                if a.isVisible():
                    prev_sep = True
            elif a.isVisible():
                prev_sep = False

    def set_model_region(self, name, save=True):
        """Centre the projection on a region (or the radar) and fit it in the panels."""
        if save:
            self.settings["model_region"] = name
            self.settings.save()
        if name == "radar" or name not in models.REGIONS:
            s = __import__("radarforge.data.sites", fromlist=["get_site"]).get_site(self.data.site_id)
            lat, lon, width = (s.lat, s.lon, 900) if s else (38.5, -97.0, 4900)
        else:
            lat, lon, width = models.REGIONS[name]
        self.view.set_projection(lat, lon, self.data.site_id)
        w = min((p.rect.width() for p in self.view.panels), default=self.view.width()) or 800
        self.view.set_view(0.0, 0.0, max(0.05, w / width))
        self._sync_model_combos()
        self.models.pictures.clear()
        self.models.request_soon(0)

    # ---------------------------------------------------------------- refresh
    def _models_changed(self):
        if getattr(self, "view_mode", "radar") != "models":
            return
        ml = self.models
        for p in self.view.panels:
            p.image = None
            p.header = ml.header(p.index)
            p.message = ml.message(p.index)
            try:
                p.palette = ml.colortable(p.index)
            except Exception:
                p.palette = None
            p.storage_units = ""
        hrs = ml.hours()
        fh = ml.fh()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, max(0, len(hrs) - 1))
        self.frame_slider.setValue(hrs.index(fh) if fh in hrs else 0)
        self.frame_slider.blockSignals(False)
        r, _h = ml.run()
        if r is None:
            self.time_label.setText(" finding the newest run… ")
        else:
            v = r + timedelta(hours=fh)
            self.time_label.setText(f" F{fh:02d}  {v:%a %d %b %HZ}  ({v.astimezone():%-I %p} local) ")
        self.view.update()
        self.stateChanged.emit()

    def _models_busy(self, busy):
        if self.view_mode == "models":
            self.prog.setVisible(busy)
            if busy:
                self.prog.setRange(0, 0)

    # ---------------------------------------------------------------- timeline in the Models view
    def models_step(self, d):
        self.models.step(d)
        hrs = self.models.hours()
        if self.playing and hrs:
            fh = self.models.fh()
            i = hrs.index(fh) if fh in hrs else 0
            self.models.prefetch(hrs[(i + 1) % len(hrs)])

    def models_goto_index(self, i):
        hrs = self.models.hours()
        if hrs:
            self.models.set_fh(hrs[max(0, min(i, len(hrs) - 1))])

    # ---------------------------------------------------------------- panel menu in the Models view
    def _model_panel_menu(self, idx, gpos):
        menu = QMenu(self)
        cur = self.models.fields()[idx]
        self._header(menu, f"Panel {idx + 1}: {models.FIELDS[cur].name}")
        for grp in models.GROUPS:
            sub = menu.addMenu(grp)
            for fd in models.FIELDS.values():
                if fd.group != grp:
                    continue
                a = sub.addAction(fd.name)
                a.setCheckable(True)
                a.setChecked(fd.id == cur)
                a.setToolTip(fd.desc)
                a.triggered.connect(lambda _=False, f=fd.id: self.models.set_field(idx, f))
            sub.setToolTipsVisible(True)
        if self.view.cursor_world is not None:
            x, y = self.view.cursor_world
            lat, lon = self.view.world_to_latlon(x, y)
            self._header(menu, f"Here: {abs(lat):.2f}°{'N' if lat >= 0 else 'S'} {abs(lon):.2f}°{'W' if lon < 0 else 'E'}")
            mname = {"hrrr": "HRRR", "nam": "NAM4KM", "rap": "RAP", "gfs": "GFS"}[self.models.model_id]
            a = menu.addAction(f"{self.models.model.name} sounding here (F{self.models.fh():02d})…")
            a.triggered.connect(lambda: self.open_sounding(lat, lon, model=mname, valid=self.models.valid_time()))
            a = menu.addAction("Centre here")
            a.triggered.connect(lambda: self.view.set_view(x, y, self.view.scale))
        menu.exec(gpos)

    # ---------------------------------------------------------------- first-time hints
    def _first_time_hint(self, key, text):
        seen = set(self.settings["hints_seen"] or [])
        if key in seen:
            return
        seen.add(key)
        self.settings["hints_seen"] = sorted(seen)
        self.settings.save()
        from .hints import show_hint
        show_hint(self, text)
