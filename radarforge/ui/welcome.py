"""Getting started: a one-page tour for new users (shown once on a fresh install, and from Help)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

STEPS = [
    ("Pick a radar",
     "Click a radar square on the map, or the radar button at the top left (Ctrl+R). Live data starts right "
     "away and keeps itself up to date. <b>Archive</b> loads any past storm."),
    ("Move around",
     "Scroll to zoom, drag to pan, double-click to centre. <b>← →</b> step through the loop, <b>Space</b> plays "
     "it, <b>↑ ↓</b> change the tilt, keys <b>1</b>–<b>6</b> show more panels."),
    ("Warnings",
     "Outlines are storm warnings: <span style='color:#ff3030'><b>red</b></span> tornado, "
     "<span style='color:#ff40ff'><b>magenta</b></span> confirmed / PDS tornado, "
     "<span style='color:#e0e000'><b>yellow</b></span> severe thunderstorm, "
     "<span style='color:#30d030'><b>green</b></span> flash flood. Shaded counties are winter and tropical alerts. "
     "Click any of them for the full text."),
    ("The drawer",
     "Everything else is in the drawer at the right. Its buttons pick what it shows: <b>Radar</b> (products and "
     "tilts), <b>Layers</b>, <b>Weather</b> (Severe, Winter and Tropical, each with a one-click setup and its "
     "model maps), <b>Alerts</b>, <b>Storms</b> and <b>Models</b>. Click the button that's on (or F9) to fold "
     "the drawer away and give the map the whole window. The tools (cross section, measure, storm track, 3-D) "
     "are down the left; every menu is behind <b>≡</b> at the top left (F10)."),
    ("Models",
     "The <b>Models</b> button (or F6) switches to forecast maps from the HRRR, NAM, RAP and GFS. The quickest way "
     "in: <b>Severe → Severe set</b> shows CAPE, helicity, shear and STP side by side. ← → step the forecast hours, "
     "Space plays them, right-click a panel to change its field."),
    ("Find a place",
     "Type a town, road or address in the search box at the top right (Ctrl+F). Right-click the map for "
     "soundings, storm tools and the nearest radar."),
]


class WelcomeDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Getting started with RadarForge")
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        title = QLabel("<span style='font-size:16pt; font-weight:600'>Welcome to RadarForge</span><br>"
                       "Six things to know, then you're set.")
        lay.addWidget(title)
        for i, (head, text) in enumerate(STEPS, 1):
            lab = QLabel(f"<b>{i}. {head}</b><br>{text}")
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.RichText)
            lay.addWidget(lab)
        self.again = QCheckBox("Show this when RadarForge starts")
        self.again.setChecked(bool(main.settings["show_welcome"]) and not getattr(main.settings, "fresh", False))
        lay.addWidget(self.again)
        row = QHBoxLayout()
        pick = QPushButton("Choose a radar…")
        pick.clicked.connect(lambda: (self.accept(), main.choose_site()))
        layers = QPushButton("Open the Severe panel")
        layers.clicked.connect(lambda: (self.accept(), main.show_panel("severe")))
        models = QPushButton("Show severe model maps")
        models.clicked.connect(lambda: (self.accept(), main.open_model_set(["cape_ml", "srh01", "shear06", "stp"],
                                                                           "hrrr")))
        close = QPushButton("Start")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(pick)
        row.addWidget(layers)
        row.addWidget(models)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        lay.addWidget(QLabel("<span style='color:gray'>Help → Getting started brings this back.</span>"))

    def done(self, r):
        s = self.main.settings
        s["show_welcome"] = self.again.isChecked()
        s.save()
        super().done(r)
