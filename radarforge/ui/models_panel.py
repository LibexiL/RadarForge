"""The Models side panel: model, run, which panel, and the fields to show (grouped, with what each means)."""
from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..features import models


def _chip(text, tip=""):
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(True)
    b.setProperty("role", "chip")
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return b


class ModelsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        def caption(text):
            lab = QLabel(text[:1].upper() + text[1:].lower() if text.isupper() else text)
            lab.setProperty("role", "section")
            f = lab.font()
            f.setBold(True)
            lab.setFont(f)
            lay.addWidget(lab)

        caption("Model")
        row = QHBoxLayout()
        row.setSpacing(3)
        self.model_btns = QButtonGroup(self)
        self.model_btns.setExclusive(True)
        for m in models.MODELS.values():
            b = _chip(m.name, m.long)
            b.setProperty("model", m.id)
            self.model_btns.addButton(b)
            row.addWidget(b)
            b.clicked.connect(lambda _=False, mid=m.id: main.models.set_model(mid))
        lay.addLayout(row)
        self.run_lbl = QLabel("")
        self.run_lbl.setProperty("role", "hint")
        self.run_lbl.setWordWrap(True)
        lay.addWidget(self.run_lbl)

        caption("Panel")
        prow = QHBoxLayout()
        prow.setSpacing(3)
        self.panel_btns = QButtonGroup(self)
        self.panel_btns.setExclusive(True)
        self._pbtn = []
        for i in range(6):
            b = _chip(str(i + 1), f"Choose the field for panel {i + 1}")
            self.panel_btns.addButton(b, i)
            prow.addWidget(b)
            self._pbtn.append(b)
            b.clicked.connect(lambda _=False, i=i: main.set_active_panel(i))
        lay.addLayout(prow)
        self.panel_hint = QLabel("Pick a field for the highlighted panel. The layout button (or keys 1–6) shows more "
                                 "panels, each with its own field.")
        self.panel_hint.setProperty("role", "hint")
        self.panel_hint.setWordWrap(True)
        lay.addWidget(self.panel_hint)

        caption("Field")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemClicked.connect(self._picked)
        self.tree.currentItemChanged.connect(lambda cur, _old: self._describe(cur))
        self._items = {}
        for grp in models.GROUPS:
            top = QTreeWidgetItem([grp])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(top)
            for fd in models.FIELDS.values():
                if fd.group != grp:
                    continue
                it = QTreeWidgetItem([fd.name])
                it.setData(0, Qt.UserRole, fd.id)
                it.setToolTip(0, fd.desc)
                top.addChild(it)
                self._items[fd.id] = it
            top.setExpanded(True)
        lay.addWidget(self.tree, 1)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setProperty("role", "hint")
        self.desc.setMinimumHeight(54)
        self.desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(self.desc)

        orow = QHBoxLayout()
        lab = QLabel("Opacity")
        lab.setProperty("role", "hint")
        orow.addWidget(lab)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setValue(int(round(float(main.settings["model_opacity"] or 0.9) * 100)))
        self.opacity.valueChanged.connect(self._opacity)
        orow.addWidget(self.opacity, 1)
        self.refresh_btn = QPushButton("Newest run")
        self.refresh_btn.setToolTip("Look for a newer run now (it's checked every 5 minutes anyway)")
        self.refresh_btn.clicked.connect(lambda: (main.models.set_run(None), main.models.refresh_runs()))
        orow.addWidget(self.refresh_btn)
        lay.addLayout(orow)
        main.models.changed.connect(self.sync)
        main.models.runsChanged.connect(self.sync)
        main.stateChanged.connect(self.sync)
        self.sync()

    # ---------------------------------------------------------------- actions
    def _picked(self, item, _col):
        fid = item.data(0, Qt.UserRole)
        if fid:
            self.main.models.set_field(self.main.view.active_panel, fid)
            self.sync()

    def _opacity(self, v):
        self.main.settings["model_opacity"] = v / 100.0
        self.main.view.update()

    def _describe(self, item):
        fid = item.data(0, Qt.UserRole) if item is not None else None
        if not fid:
            return
        fd = models.FIELDS[fid]
        units = f" ({fd.units})" if fd.units else ""
        self.desc.setText(f"<b>{fd.name}</b>{units}<br>{fd.desc}")

    # ---------------------------------------------------------------- state
    def sync(self):
        ml = self.main.models
        for b in self.model_btns.buttons():
            b.setChecked(b.property("model") == ml.model_id)
        r, hrs = ml.run()
        if r is None:
            self.run_lbl.setText("Looking for the newest run…")
        else:
            newest = ml.run_list()[0][0] == r if ml.run_list() else True
            self.run_lbl.setText(f"Run {r:%HZ %a %d %b}{' (newest)' if newest else ''} · {len(hrs)} hours out so far, "
                                 f"to F{max(hrs):02d} ({(r + timedelta(hours=max(hrs))).astimezone():%a %-I %p})")
        n = len(self.main.view.panels)
        active = min(self.main.view.active_panel, n - 1)
        for i, b in enumerate(self._pbtn):
            b.setVisible(i < n)
            b.setChecked(i == active)
        fid = ml.fields()[active]
        for k, it in self._items.items():
            f = it.font(0)
            f.setBold(k == fid)
            it.setFont(0, f)
        it = self._items.get(fid)
        if it is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(it)
            self.tree.blockSignals(False)
            self._describe(it)
