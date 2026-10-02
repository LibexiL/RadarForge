"""Level III storm cell table."""
from __future__ import annotations

import math

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ... import fmt
from ...products.geometry import aeqd_forward
from .common import _hint

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
        follow_btn = QPushButton("Follow the selected cell")
        follow_btn.setToolTip("Keep this storm centred as new frames arrive (changes radar if it moves out of range)")
        follow_btn.clicked.connect(self._follow_selected)
        trend_btn = QPushButton("Show trends for the selected cell")
        trend_btn.setToolTip("Hail, rotation and speed of this cell over the loop")
        trend_btn.clicked.connect(self._trends_selected)
        row = QHBoxLayout()
        row.addWidget(follow_btn)
        row.addWidget(trend_btn)
        lay.addLayout(row)
        lay.addWidget(_hint("Sorted by threat. Double-click a cell to centre on it."))
        self._prod = None
        self._key = None
        main.stateChanged.connect(self.refresh)

    def _follow_selected(self):
        it = self.tree.currentItem()
        if it is None and self.tree.topLevelItemCount():
            it = self.tree.topLevelItem(0)
        if it is None:
            return
        x, y = it.data(0, Qt.UserRole)
        self.main.follow_storm_at(x, y)

    def _trends_selected(self):
        it = self.tree.currentItem()
        if it is None and self.tree.topLevelItemCount():
            it = self.tree.topLevelItem(0)
        self.main.open_trends(it.data(1, Qt.UserRole) if it is not None else None)

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
            rng = math.hypot(c["x"], c["y"]) / fmt.UNIT_KM[du]
            mot = f"{c['motion'][0]:03.0f}/{c['motion'][1]:.0f}" if c["motion"] else "—"
            posh = f"{c['posh']}%" if c["posh"] else "—"
            size = f"{c['size']:.2f}\"" if c["size"] else "—"
            meso = (f"{c['meso']}" + (" E" if c["meso_elev"] else "")) if c["meso"] is not None else "—"
            it = QTreeWidgetItem([c["id"], f"{az:03.0f}/{rng:.0f}", mot, posh, size, meso, c["tvs"] or "—"])
            it.setData(0, Qt.UserRole, (c["x"], c["y"]))
            it.setData(1, Qt.UserRole, c["id"])
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
