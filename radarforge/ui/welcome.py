"""Getting started: a one-page tour for new users (shown once on a fresh install, and from Help)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

STEPS = [
    ("Pick a radar",
     "Click a radar square on the map, or <b>Choose radar</b> at the top left (Ctrl+R). Live data starts right "
     "away and keeps itself up to date."),
    ("Move around",
     "Scroll to zoom, drag to pan, double-click to centre. <b>← →</b> step through the loop, <b>Space</b> plays "
     "it, <b>↑ ↓</b> change the tilt."),
    ("Warnings",
     "Coloured outlines are NWS warnings: <span style='color:#ff3030'><b>red</b></span> tornado, "
     "<span style='color:#ff40ff'><b>magenta</b></span> tornado confirmed or PDS, "
     "<span style='color:#e0e000'><b>yellow</b></span> severe thunderstorm, "
     "<span style='color:#30d030'><b>green</b></span> flash flood. Rest the mouse on an outline for a summary; "
     "click it for the full warning. New warnings flash for a few seconds."),
    ("Panels and products",
     "Keys <b>1</b>–<b>6</b> change how many panels you see. Right-click a panel to pick what it shows "
     "(reflectivity, velocity, correlation coefficient…)."),
    ("Every switch in one place",
     "The <b>Quick</b> panel (F8) has the layers, warnings, map, satellite and storm tools as one-click "
     "buttons. Right-click the map for soundings, storm tools and the nearest radar."),
    ("Find a place, or go back in time",
     "Type a town, road or address in the search box at the top right (Ctrl+F). <b>Radar → Past data</b> "
     "loads any past storm; the warnings shown always match the scan's time."),
]


class WelcomeDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Getting started with RadarForge")
        self.setMinimumWidth(560)
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
        quick = QPushButton("Open the Quick panel")
        quick.clicked.connect(lambda: (self.accept(), main.show_panel("quick")))
        close = QPushButton("Start")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(pick)
        row.addWidget(quick)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        lay.addWidget(QLabel("<span style='color:gray'>Help → Getting started brings this back.</span>"))

    def done(self, r):
        s = self.main.settings
        s["show_welcome"] = self.again.isChecked()
        s.save()
        super().done(r)
