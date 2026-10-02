"""The Sounding tool panel: a model sounding (HRRR) at any point, or the latest balloon launch from a station,
drawn as a skew-T with a hodograph and the severe-weather numbers."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from ..data import hrrr, raob
from ..services import geo
from ..services.sounding import Parameters, Sounding, parameters

SOURCES = (("Model forecast (HRRR)", "model"), ("Balloon launch (observed)", "observed"))


class SoundingWindow(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.canvas = None
        self.fig = None
        self.snd: Sounding | None = None
        self.par: Parameters | None = None
        self.point = None                      # (lat, lon) for the model sounding
        self._token = 0
        self._stations: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)

        row = QHBoxLayout()
        self.source = QComboBox()
        for label, key in SOURCES:
            self.source.addItem(label, key)
        self.source.currentIndexChanged.connect(self._source_changed)
        row.addWidget(self.source)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(self.stack.sizePolicy().horizontalPolicy(), self.stack.sizePolicy().verticalPolicy())
        # model controls
        mw = QWidget()
        ml = QHBoxLayout(mw)
        ml.setContentsMargins(0, 0, 0, 0)
        self.point_label = QLabel("Right-click the map → Model sounding here, or use the buttons")
        ml.addWidget(self.point_label, 1)
        for text, tip, fn in (("Map centre", "Use the centre of the map", self._use_centre),
                              ("My location", "Use my location", self._use_mine)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, f=fn: f())
            ml.addWidget(b)
        ml.addWidget(QLabel("Forecast hour +"))
        self.fhr = QSpinBox()
        self.fhr.setRange(0, hrrr.MAX_FORECAST_HOUR)
        self.fhr.setToolTip("0 is the model's analysis (the best picture of now); later hours are forecasts")
        self.fhr.setValue(0)
        ml.addWidget(self.fhr)
        self.stack.addWidget(mw)
        # balloon controls
        ow = QWidget()
        ol = QHBoxLayout(ow)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(QLabel("Station"))
        self.station = QComboBox()
        self.station.setMinimumWidth(240)
        ol.addWidget(self.station, 1)
        ol.addWidget(QLabel("Launch"))
        self.launch = QComboBox()
        ol.addWidget(self.launch)
        self.stack.addWidget(ow)
        row.addWidget(self.stack, 1)
        self.go = QPushButton("Load")
        self.go.clicked.connect(self.load)
        row.addWidget(self.go)
        lay.addLayout(row)

        self.canvas_host = QVBoxLayout()
        lay.addLayout(self.canvas_host, 1)
        self.message = QLabel("Choose a point or a station, then Load.")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setProperty("role", "hint")
        self.canvas_host.addWidget(self.message, 1)

        bottom = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(160)
        self.bar.setVisible(False)
        bottom.addWidget(self.bar)
        self.status = QLabel("")
        bottom.addWidget(self.status, 1)
        self.srv_btn = QPushButton("Use right mover for SRV")
        self.srv_btn.setToolTip("Set the storm motion used by Storm Relative Velocity to the Bunkers right mover")
        self.srv_btn.clicked.connect(self.use_for_srv)
        self.srv_btn.setEnabled(False)
        copy = QPushButton("Copy picture")
        copy.clicked.connect(self.copy_picture)
        save = QPushButton("Save picture…")
        save.clicked.connect(self.save_picture)
        for b in (self.srv_btn, copy, save):
            bottom.addWidget(b)
        lay.addLayout(bottom)
        self._fill_launches()

    # ---------------------------------------------------------------- choosing what to load
    def _source_changed(self, i):
        self.stack.setCurrentIndex(i)
        if self.source.currentData() == "observed" and not self._stations:
            self._load_stations()

    def _fill_launches(self):
        self.launch.clear()
        t = raob.latest_launch()
        for k in range(4):
            self.launch.addItem(f"{t:%d %b %H}Z", t)
            t = raob.previous_launch(t)

    def _load_stations(self):
        centre = self.main.my_location.latlon() or self.main._map_centre()
        self._stations = sorted(raob.stations(), key=lambda s: geo.distance_km(centre[0], centre[1], s[2], s[3]))
        self.station.clear()
        for sid, name, lat, lon in self._stations:
            km = geo.distance_km(centre[0], centre[1], lat, lon)
            self.station.addItem(f"{sid[-3:]}  {name}  ({km * 0.621371:.0f} mi)", (sid, name, lat, lon))

    def set_point(self, lat, lon):
        self.point = (float(lat), float(lon))
        self.point_label.setText(f"{lat:.3f}, {lon:.3f}  –  {self.main.place_name(lat, lon)}")

    def _use_centre(self):
        self.set_point(*self.main._map_centre())

    def _use_mine(self):
        me = self.main.my_location.latlon()
        if me is None:
            self.main.set_my_location_dialog()
            me = self.main.my_location.latlon()
        if me is not None:
            self.set_point(*me)

    def model_here(self, lat, lon):
        """From the map's right-click menu."""
        self.source.setCurrentIndex(0)
        self.set_point(lat, lon)
        self.load()

    # ---------------------------------------------------------------- loading
    def load(self):
        self._token += 1
        token = self._token
        self.go.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.status.setText("Starting…")
        relay = self.main.relay
        if self.source.currentData() == "model":
            if self.point is None:
                self._use_centre()
            lat, lon = self.point
            fhr = self.fhr.value()
            place = self.main.place_name(lat, lon)

            def work():
                run = hrrr.latest_run(fhr=fhr)
                if run is None:
                    raise OSError("no recent HRRR run was found")
                relay.call.emit(lambda: self._set_status(f"HRRR {run:%H}Z run: downloading…"))

                def progress(done, total):
                    relay.call.emit(lambda: self._progress(token, done, total))
                raw = hrrr.fetch_profile(lat, lon, run, fhr, progress, cancelled=lambda: token != self._token)
                snd = Sounding.from_hrrr(raw, place)
                return snd, parameters(snd)
        else:
            data = self.station.currentData()
            if data is None:
                self._load_stations()
                data = self.station.currentData()
            sid, name, lat, lon = data
            when = self.launch.currentData()

            def work():
                snd = raob.fetch(sid, when, name, lat, lon)
                return snd, parameters(snd)
        self.main.run_bg(work, lambda res: self._loaded(token, *res), lambda exc: self._failed(token, exc))

    def _set_status(self, text):
        self.status.setText(text)

    def _progress(self, token, done, total):
        if token == self._token:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.status.setText(f"Downloading model data… {done}/{total}")

    def _done_loading(self):
        self.go.setEnabled(True)
        self.bar.setVisible(False)

    def _failed(self, token, exc):
        if token != self._token:
            return
        self._done_loading()
        text = "Cancelled" if isinstance(exc, InterruptedError) else f"Couldn't load the sounding: {exc}"
        self.status.setText(text)

    def _loaded(self, token, snd, par):
        if token != self._token:
            return
        self._done_loading()
        self.snd, self.par = snd, par
        self.srv_btn.setEnabled(par.right_mover is not None)
        self.status.setText(f"{snd.source} · {len(snd.pressure)} levels")
        self.redraw()

    # ---------------------------------------------------------------- drawing
    def _ensure_canvas(self):
        if self.canvas is not None:
            return
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(11, 7.2), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setMinimumSize(720, 440)
        self.canvas_host.removeWidget(self.message)
        self.message.hide()
        self.canvas_host.addWidget(self.canvas, 1)

    def is_dark(self) -> bool:
        return QApplication.palette().color(QPalette.Window).lightness() < 128

    def redraw(self):
        if self.snd is None:
            return
        self._ensure_canvas()
        from . import skewt
        skewt.draw(self.fig, self.snd, self.par, dark=self.is_dark())
        self.canvas.draw_idle()

    # ---------------------------------------------------------------- actions
    def use_for_srv(self):
        if self.par is None or self.par.right_mover is None:
            return
        d, kt = self.par.right_mover
        self.main.set_storm_motion(round(d), round(kt))
        self.status.setText(f"SRV storm motion set to {d:03.0f}° / {kt:.0f} kt")

    def copy_picture(self):
        if self.canvas is not None:
            QApplication.clipboard().setPixmap(self.canvas.grab())
            self.status.setText("Picture copied")

    def save_picture(self):
        if self.fig is None:
            return
        name = f"sounding_{datetime.now(timezone.utc):%Y%m%d_%H%M}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save sounding", name, "PNG (*.png)")
        if path:
            self.fig.savefig(path, facecolor=self.fig.get_facecolor(), dpi=150)
            self.status.setText(f"Saved {path}")
