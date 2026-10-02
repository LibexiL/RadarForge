"""Products, tilts and radar information."""
from __future__ import annotations

import numpy as np
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...data.sites import get_site
from ...products import catalog
from .common import _chip, _hint, _scroll, _section


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
                t = m.engine._tilt_cache.get((frame.uid, frame.l2_rev))
                labels = [x.label for x in t] if t else []
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
