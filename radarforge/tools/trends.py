"""The Storm trends panel: how one cell's hail, rotation and speed changed over the loop."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from ..services import trends
from ..ui.panels.cells import storm_cells


class TrendsWindow(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.canvas = None
        self.fig = None
        self.cell_id: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Cell"))
        self.picker = QComboBox()
        self.picker.setMinimumWidth(120)
        self.picker.setToolTip("Storm cells in the current frame, most threatening first")
        self.picker.activated.connect(self._picked)
        row.addWidget(self.picker)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        save = QPushButton("Save picture…")
        save.clicked.connect(self.save_picture)
        row.addWidget(save)
        lay.addLayout(row)
        self.host = QVBoxLayout()
        lay.addLayout(self.host, 1)
        self.message = QLabel("Pick a cell in the Storm cells panel (right-click → Show trends), or choose one above.\n"
                              "Trends need Level III storm data, which downloads while this panel is open.")
        self.message.setWordWrap(True)
        self.message.setProperty("role", "hint")
        self.host.addWidget(self.message, 1)
        main.stateChanged.connect(self.refresh)

    # ---------------------------------------------------------------- choosing the cell
    def show_cell(self, cell_id: str):
        self.cell_id = cell_id
        self.refresh(force=True)

    def _picked(self, _i):
        self.cell_id = self.picker.currentData()
        self.refresh(force=True)

    def _history(self):
        out = []
        for f in list(self.main.data.frames):
            if f.l3.get("NST") is not None:
                out.append((f.time, storm_cells(f)[1]))
        return out

    def refresh(self, force=False):
        if not self.isVisible():
            return
        frame = self.main.current_frame()
        cells = storm_cells(frame)[1] if frame is not None else []
        cur = self.cell_id
        self.picker.blockSignals(True)
        self.picker.clear()
        for c in cells:
            self.picker.addItem(c["id"], c["id"])
        if cur is not None:
            i = self.picker.findData(cur)
            if i >= 0:
                self.picker.setCurrentIndex(i)
            else:
                self.picker.insertItem(0, f"{cur} (not in this frame)", cur)
                self.picker.setCurrentIndex(0)
        self.picker.blockSignals(False)
        if cur is None and cells:
            self.cell_id = cur = cells[0]["id"]
            self.picker.setCurrentIndex(0)
        if cur is None:
            self.summary.setText("")
            return
        series = trends.build(self._history(), cur)
        self.summary.setText(trends.summary(series))
        if len(series) < 2:
            self._plot(None)
            self.message.setText(f"Cell {cur} appears in {len(series)} frame{'s' if len(series) != 1 else ''} so far. "
                                 "Load a longer loop (Settings → Loop frames) or wait for more volumes.")
            self.message.show()
            return
        self._plot(series, frame.time if frame is not None else None)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.main._update_l3_needs()
        self.refresh()

    # ---------------------------------------------------------------- drawing
    def is_dark(self) -> bool:
        return QApplication.palette().color(QPalette.Window).lightness() < 128

    def _ensure_canvas(self):
        if self.canvas is not None:
            return
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(8, 6), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setMinimumSize(520, 360)
        self.host.addWidget(self.canvas, 1)

    def _plot(self, s, now=None):
        if s is None:
            if self.canvas is not None:
                self.canvas.hide()
            return
        self._ensure_canvas()
        self.message.hide()
        self.canvas.show()
        draw(self.fig, s, now, dark=self.is_dark())
        self.canvas.draw_idle()

    def save_picture(self):
        if self.fig is None:
            return
        name = f"trends_{self.cell_id}_{datetime.now(timezone.utc):%Y%m%d_%H%M}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save storm trends", name, "PNG (*.png)")
        if path:
            self.fig.savefig(path, facecolor=self.fig.get_facecolor(), dpi=150)


def draw(fig, s: trends.Series, now=None, dark=True):
    """Three stacked charts on one time axis: hail, rotation, speed and distance."""
    import matplotlib.dates as mdates
    bg, fg, grid = ("#1b1d22", "#dcdfe4", "#3a3e46") if dark else ("#ffffff", "#20232a", "#d0d4da")
    fig.clear()
    fig.set_facecolor(bg)
    axes = fig.subplots(3, 1, sharex=True)
    t = list(s.times)

    def style(ax, title):
        ax.set_facecolor(bg)
        ax.set_title(title, loc="left", fontsize=9, color=fg)
        ax.grid(True, color=grid, lw=0.6)
        ax.tick_params(colors=fg, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(grid)
        if now is not None and t[0] <= now <= t[-1]:
            ax.axvline(now, color="#ffb347", lw=1.0, ls="--")

    def series(ax, y, color, label, marker="o"):
        pts = [(a, b) for a, b in zip(t, y) if b is not None]
        if pts:
            ax.plot(*zip(*pts), color=color, marker=marker, ms=3.5, lw=1.4, label=label)

    a = axes[0]
    style(a, f"Cell {s.cell_id} – hail")
    series(a, s.posh, "#6fb7ff", "POSH %")
    a.set_ylim(0, 100)
    a.set_ylabel("POSH %", color="#6fb7ff", fontsize=8)
    b = a.twinx()
    b.tick_params(colors=fg, labelsize=8)
    series(b, s.size, "#ffd35c", 'Hail size (")', "s")
    b.set_ylabel('hail size (")', color="#ffd35c", fontsize=8)
    b.set_ylim(0, max(2.0, max([v for v in s.size if v is not None] or [0]) * 1.15))
    for sp in b.spines.values():
        sp.set_color(grid)

    a = axes[1]
    style(a, "Rotation")
    series(a, s.meso, "#ff9f4a", "Meso rank")
    a.set_ylim(-0.3, max(8, max([v for v in s.meso if v is not None] or [0]) + 1))
    a.set_ylabel("MDA rank", fontsize=8, color=fg)
    for when, lvl in zip(t, s.tvs):
        if lvl:
            a.axvline(when, color="#ff5e5e" if lvl == 2 else "#ff9f9f", lw=3, alpha=0.35)
    if any(s.tvs):
        a.text(0.01, 0.9, "red bars: TVS (pale: elevated)", transform=a.transAxes, fontsize=8, color="#ff8080")

    a = axes[2]
    style(a, "Motion and distance")
    series(a, s.speed, "#7be0a0", "Speed (kt)")
    a.set_ylabel("speed kt", color="#7be0a0", fontsize=8)
    a.set_ylim(bottom=0)
    r = a.twinx()
    r.tick_params(colors=fg, labelsize=8)
    r.plot(t, [x * 0.539957 for x in s.range_km], color="#b9a0ff", lw=1.2, ls=":")
    r.set_ylabel("range nm", color="#b9a0ff", fontsize=8)
    for sp in r.spines.values():
        sp.set_color(grid)
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%H:%MZ"))
    fig.tight_layout()
