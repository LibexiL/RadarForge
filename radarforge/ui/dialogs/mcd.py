"""SPC mesoscale discussion reader."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout


class _TextFetch(QObject):
    done = Signal(str, bool)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        import requests
        try:
            r = requests.get(self.url, timeout=20, headers={"User-Agent": "RadarForge (NEXRAD viewer)"})
            r.raise_for_status()
            self.done.emit(r.text.strip(), True)
        except Exception as exc:
            self.done.emit(f"Couldn't load the discussion ({exc}).\nOpen it on the SPC website instead.", False)


class McdDialog(QDialog):
    """An SPC mesoscale discussion: summary, and its full text loaded in the background."""
    _cache: dict = {}

    def __init__(self, mcd: dict, parent=None):
        super().__init__(parent)
        from ... import fmt
        from ...data import feeds
        self.setWindowTitle(f"SPC Mesoscale Discussion {mcd['number']}")
        self.resize(640, 640)
        head = f"<b>Mesoscale Discussion {mcd['number']}</b>"
        if mcd.get("concerning"):
            head += f"<br>Concerning: {mcd['concerning'].capitalize()}"
        bits = []
        if mcd.get("issue"):
            bits.append(f"issued {fmt.local_hm(mcd['issue'])}")
        if mcd.get("expire"):
            bits.append(f"until {fmt.local_hm(mcd['expire'])}")
        if mcd.get("watch") is not None:
            bits.append(f"chance of a watch {mcd['watch']}%")
        if bits:
            head += "<br>" + " · ".join(bits)
        lab = QLabel(head)
        lab.setWordWrap(True)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        year = (mcd.get("issue") or datetime.now(timezone.utc)).year
        web = QPushButton("Open on the SPC website")
        web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(feeds.mcd_page(mcd["number"], year))))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(web)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(lab)
        lay.addWidget(self.text, 1)
        lay.addLayout(row)
        pid = mcd.get("product_id") or ""
        if pid in self._cache:
            self.text.setPlainText(self._cache[pid])
        elif pid:
            self.text.setPlainText("Loading the discussion…")
            self._thread = QThread(self)
            self._job = _TextFetch(feeds.mcd_text_url(pid))
            self._job.moveToThread(self._thread)
            self._thread.started.connect(self._job.run)
            self._pid = pid
            self._job.done.connect(self._loaded)          # a bound method: runs on the UI thread
            self._job.done.connect(self._thread.quit)
            self._thread.start()
        else:
            self.text.setPlainText("No text available – open it on the SPC website.")

    def _loaded(self, text, ok):
        if ok:
            self._cache[self._pid] = text
        self.text.setPlainText(text)
