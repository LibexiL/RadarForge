"""Learn mode: pick a famous storm, then step through what to look at. Stays open beside the map."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QTextBrowser, QVBoxLayout)

from ...services import learn


class LearnDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Learn: historic storms")
        self.setWindowFlag(Qt.Tool, True)                     # a small window that stays over the map, not modal
        self.resize(760, 460)
        self.event: dict | None = None
        self.step = 0
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.events = QListWidget()
        self.events.setMaximumWidth(250)
        for e in learn.EVENTS:
            it = QListWidgetItem(f"{e['title']}\n{e['date']} · {e['kind']} · {e['level']}")
            it.setData(Qt.UserRole, e["id"])
            self.events.addItem(it)
        self.events.currentRowChanged.connect(self._chosen)
        row.addWidget(self.events)
        right = QVBoxLayout()
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        right.addWidget(self.title)
        right.addWidget(self.text, 1)
        self.progress = QLabel("")
        self.progress.setProperty("role", "hint")
        right.addWidget(self.progress)
        nav = QHBoxLayout()
        self.prev = QPushButton("◀ Previous")
        self.show_btn = QPushButton("Take me there")
        self.next = QPushButton("Next ▶")
        self.prev.clicked.connect(lambda: self._go(self.step - 1))
        self.show_btn.clicked.connect(lambda: self._go(self.step))
        self.next.clicked.connect(lambda: self._go(self.step + 1))
        for b in (self.prev, self.show_btn, self.next):
            nav.addWidget(b)
        right.addLayout(nav)
        row.addLayout(right, 1)
        lay.addLayout(row, 1)
        self.hints = QCheckBox("Explain the value under the cursor in words (for example: \"CC 0.85: mixed targets\")")
        self.hints.setChecked(bool(main.settings["learn_hints"]))
        self.hints.toggled.connect(self._hints_toggled)
        lay.addWidget(self.hints)
        self.events.setCurrentRow(0)

    def _hints_toggled(self, on):
        self.main.settings["learn_hints"] = bool(on)
        self.main.settings.save()

    def select(self, event_id: str):
        for i in range(self.events.count()):
            if self.events.item(i).data(Qt.UserRole) == event_id:
                self.events.setCurrentRow(i)
                return

    def _chosen(self, row):
        if row < 0:
            return
        self.event = learn.find(self.events.item(row).data(Qt.UserRole))
        self.step = 0
        self._show()

    def _show(self):
        e = self.event
        if e is None:
            return
        n = len(e["steps"])
        step = e["steps"][self.step]
        self.title.setText(f"{e['title']} – {e['date']}")
        intro = f"<p>{e['about']}</p><hr>" if self.step == 0 else ""
        self.text.setHtml(f"{intro}<h3>{step['title']}</h3><p>{step['text']}</p>")
        self.progress.setText(f"Step {self.step + 1} of {n} · radar {e['site']}, "
                              f"{step['time'].replace('T', ' ').replace(':00Z', 'Z')}")
        self.prev.setEnabled(self.step > 0)
        self.next.setEnabled(self.step < n - 1)

    def _go(self, index):
        e = self.event
        if e is None or not 0 <= index < len(e["steps"]):
            return
        self.step = index
        self._show()
        self.main.learn_step(e, index)
