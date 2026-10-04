"""Camera window: the picture from one street camera (or a few at the same spot), refreshed every minute."""
from __future__ import annotations

import threading
from datetime import datetime

import requests
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from ..features.bglayer import UA

REFRESH_MS = 60_000


class _Relay(QObject):
    done = Signal(int, object, str)      # request number, QImage or None, message


class CameraViewer(QDialog):
    def __init__(self, main, cameras: list):
        super().__init__(main)
        self.main = main
        self.cameras = list(cameras)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.pick = QComboBox()
        for c in self.cameras:
            self.pick.addItem(c.name)
        self.pick.setVisible(len(self.cameras) > 1)
        self.pick.currentIndexChanged.connect(self._camera_changed)
        top.addWidget(self.pick, 1)
        self.view_pick = QComboBox()
        self.view_pick.currentIndexChanged.connect(lambda _i: self.load())
        top.addWidget(self.view_pick)
        lay.addLayout(top)
        self.image = QLabel("Loading…")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(320, 200)
        self.image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image.setStyleSheet("background:#000; color:#ccc;")
        lay.addWidget(self.image, 1)
        bottom = QHBoxLayout()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        bottom.addWidget(self.info, 1)
        self.web = QPushButton("Open in browser")
        self.web.clicked.connect(self._open_web)
        bottom.addWidget(self.web)
        again = QPushButton("Refresh")
        again.clicked.connect(self.load)
        bottom.addWidget(again)
        lay.addLayout(bottom)
        self._pix = None
        self._req = 0
        self.relay = _Relay()
        self.relay.done.connect(self._loaded)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)
        self.timer.start(REFRESH_MS)
        self._camera_changed(0)

    def cam(self):
        return self.cameras[max(0, self.pick.currentIndex())]

    def _camera_changed(self, _i):
        c = self.cam()
        self.setWindowTitle(f"Camera – {c.name}")
        self.view_pick.blockSignals(True)
        self.view_pick.clear()
        for label, _url in c.views:
            self.view_pick.addItem(label)
        self.view_pick.setVisible(len(c.views) > 1)
        self.view_pick.blockSignals(False)
        self.web.setVisible(bool(c.page))
        self.load()

    def load(self):
        c = self.cam()
        self._req += 1
        req = self._req
        vi = max(0, self.view_pick.currentIndex())
        cams_layer = getattr(self.main, "cameras", None)

        def work():
            try:
                cam = cams_layer.refresh_camera(c) if cams_layer is not None else c
                url = cam.views[min(vi, len(cam.views) - 1)][1]
                r = requests.get(url, headers=UA, timeout=25)
                r.raise_for_status()
                img = QImage.fromData(r.content)
                if img.isNull():
                    raise RuntimeError("the camera sent something that isn't a picture")
                self.relay.done.emit(req, img, "")
            except Exception as exc:
                self.relay.done.emit(req, None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, req, img, msg):
        if req != self._req:
            return
        c = self.cam()
        if img is None:
            self.image.setText(f"Couldn't load the picture:\n{msg}")
            self.info.setText(f"{c.source}")
            return
        self._pix = QPixmap.fromImage(img)
        self._fit()
        self.info.setText(f"{c.source} · picture loaded {datetime.now():%H:%M:%S} · refreshes every minute"
                          + (" · Webcams provided by windy.com" if c.source.startswith("Windy") else ""))

    def _fit(self):
        if self._pix is not None:
            self.image.setPixmap(self._pix.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _open_web(self):
        if self.cam().page:
            QDesktopServices.openUrl(QUrl(self.cam().page))
