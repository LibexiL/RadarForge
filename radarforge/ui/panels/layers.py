"""Layers and overlays (mirrors the Layers menu)."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QRadioButton, QVBoxLayout, QWidget

from .common import _scroll, _section


def _mirror(action, text=None):
    cb = QCheckBox(text or action.text())
    cb.setChecked(action.isChecked())
    cb.toggled.connect(lambda on: action.setChecked(on) if action.isChecked() != on else None)
    action.toggled.connect(lambda on: cb.setChecked(on) if cb.isChecked() != on else None)
    return cb


class LayersPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        def group(title, actions):
            lay.addWidget(_section(title))
            g = QGridLayout()
            g.setContentsMargins(2, 0, 2, 6)
            g.setVerticalSpacing(4)
            for k, (a, text) in enumerate(actions):
                g.addWidget(_mirror(a, text), k // 2, k % 2)
            lay.addLayout(g)
        group("Overlays", [(main.overlay_acts[k], t) for k, t in (
            ("warnings", "Warnings"), ("watches", "Watches"), ("reports", "Storm reports"),
            ("chasers", "Storm chasers"), ("spc_outlook", "SPC outlook"), ("spc_mcd", "SPC discussions"),
            ("storm_tracks", "Storm tracks"), ("meso", "Mesocyclones"), ("tvs", "TVS"), ("hail", "Hail"),
            ("melting_layer", "Melting layer"))])
        group("Map", [(main.sites_act, "Radar sites"), (main.tdwr_act, "TDWR sites"),
                      (main.cities_act, "City labels"), (main.rings_act, "Range rings")] +
              [(a, a.text()) for a in main.layer_acts.values()])
        group("Display", [(main.smooth_act, "Smoothing"), (main.legend_act, "Colour bars"),
                          (main.link_act, "Linked cursor")])
        lay.addWidget(_section("Velocity noise filter"))
        vl = QHBoxLayout()
        vl.setContentsMargins(2, 0, 2, 0)
        for a, lv in zip(main.vf_group.actions(), (0, 1, 2)):
            rb = QRadioButton(("Off", "Normal", "Aggressive")[lv])
            rb.setChecked(a.isChecked())
            rb.clicked.connect(lambda _=False, lv=lv: main.set_velocity_filter(lv))
            a.toggled.connect(lambda on, rb=rb: rb.setChecked(on) if on else None)
            vl.addWidget(rb)
        vl.addStretch(1)
        lay.addLayout(vl)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
