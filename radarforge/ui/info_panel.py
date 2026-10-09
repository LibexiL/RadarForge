"""The details panel: what a click on the map landed on (warnings, watches, SPC discussions, storm reports).

One panel is reused: clicking something else shows that instead. When several things overlap at the spot
(a warning inside a watch inside a discussion), a list at the top picks between them.
"""
from __future__ import annotations

import html
import threading
from datetime import datetime, timezone

import numpy as np
from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPalette
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QTextBrowser, QVBoxLayout)

from ..features import feeds
from ..features import warnings as W

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def when(t) -> str:
    """'4:52 PM EDT', with the date when it isn't today."""
    if t is None:
        return ""
    lt = t.astimezone()
    s = f"{feeds.local_hm(t)} {lt.tzname() or ''}".strip()
    if lt.date() != datetime.now().astimezone().date():
        s = f"{lt:%b} {lt.day}, {lt.year}, {s}" if lt.year != datetime.now().year else f"{lt:%b} {lt.day}, {s}"
    return s


def left(t_end, now) -> str:
    s = (t_end - now).total_seconds()
    if s <= 0:
        return "expired"
    m = int(s // 60)
    return f"{m} min left" if m < 60 else f"{m // 60} h {m % 60:02d} min left"


def alert_title(a) -> str:
    """'Tornado Warning – PDS', 'Severe Thunderstorm Warning – Considerable', 'Tornado Watch'…"""
    label = W.VARIANT[a.variant][2]
    if " - " in label:
        extra = label.split(" - ", 1)[1]
        if extra.lower() not in a.event.lower():
            return f"{a.event} – {extra}"
    return a.event


def item_title(it) -> str:
    k, o = it["kind"], it["obj"]
    if k == "alert":
        return alert_title(o)
    if k == "mcd":
        return f"SPC Mesoscale Discussion {o['number']}"
    if k == "report":
        name = feeds.REPORT_KINDS.get(o.get("kind", "other"), feeds.REPORT_KINDS["other"])[2]
        mag = o.get("magnitude") or ""
        return f"{name} report" + (f" ({mag})" if mag else "")
    return o["title"]


def item_rgb(it, main) -> tuple:
    k, o = it["kind"], it["obj"]
    if k == "alert":
        return tuple(main.warnings.color(o))
    if k == "mcd":
        return feeds.MCD_RGB
    if k == "report":
        return feeds.REPORT_KINDS.get(o.get("kind", "other"), feeds.REPORT_KINDS["other"])[1]
    return feeds.CAT_RGB.get(o.get("cat"), (200, 200, 200))


def _get_text(url: str) -> str:
    import requests
    r = requests.get(url, timeout=20, headers={"User-Agent": "RadarForge (NEXRAD viewer)"})
    r.raise_for_status()
    return r.text.strip()


class _Relay(QObject):
    done = Signal(str, str, bool)          # key, text, ok


class InfoPanel(QDialog):
    _texts: dict = {}                      # downloaded texts, by key (kept for the session)

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Details")
        self.setObjectName("InfoPanel")
        self.resize(580, 640)
        self.items: list = []
        self.cur = None
        self._selected = None              # the warning this panel highlighted on the map
        self._loading: set = set()
        self._failed: dict = {}
        self._placed = False
        self._relay = _Relay(self)
        self._relay.done.connect(self._loaded)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.RichText)
        self.head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.go = QPushButton("Go to it")
        self.go.setToolTip("Zoom the map to it")
        self.go.clicked.connect(self._go)
        self.web = QPushButton("Open on the SPC website")
        self.web.clicked.connect(self._open_web)
        self.copy = QPushButton("Copy text")
        self.copy.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.go)
        row.addWidget(self.web)
        row.addWidget(self.copy)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addWidget(self.head)
        lay.addWidget(self.body, 1)
        lay.addLayout(row)

    # ------------------------------------------------------------------ showing
    def show_items(self, items: list, near: QPoint | None = None, start: int = 0):
        """Show [items] ({"kind": "alert"|"mcd"|"report"|"outlook", "obj": …}), [start] selected."""
        from .panels import _swatch
        self.items = list(items)
        self.list.blockSignals(True)
        self.list.clear()
        for it in self.items:
            self.list.addItem(QListWidgetItem(_swatch(item_rgb(it, self.main)), item_title(it)))
        start = max(0, min(start, len(self.items) - 1))
        self.list.setCurrentRow(start)
        self.list.blockSignals(False)
        self.list.setVisible(len(self.items) > 1)
        if len(self.items) > 1:                 # just tall enough for its rows (up to five)
            rh = max(16, self.list.sizeHintForRow(0))
            self.list.setFixedHeight(rh * min(5, len(self.items)) + 2 * self.list.frameWidth() + 2)
        self._show(start)
        if not self.isVisible() and near is not None and not self._placed:
            self._place(near)
        self.show()
        self.raise_()
        self.activateWindow()

    def _place(self, near: QPoint):
        """First time: beside the click, kept on the screen (afterwards it stays where it was put)."""
        scr = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = near.x() + 24
        if x + self.width() > g.right():
            x = near.x() - 24 - self.width()
        y = min(max(g.top(), near.y() - self.height() // 3), g.bottom() - self.height())
        self.move(max(g.left(), x), max(g.top(), y))
        self._placed = True

    def _show(self, row):
        if not (0 <= row < len(self.items)):
            return
        it = self.cur = self.items[row]
        k, o = it["kind"], it["obj"]
        rows, area, text, note = [], "", None, ""
        sub = ""
        self.web.setVisible(k == "mcd")
        self.go.setVisible(k != "outlook")
        if k == "alert":
            rows, area, text, sub, note = self._alert(o)
            self._highlight(o.uid)
        else:
            self._highlight(None)
            if k == "mcd":
                rows, text, note = self._mcd(o)
            elif k == "report":
                rows, text = self._report(o)
            else:
                rows = [tuple(line.split(": ", 1)) if ": " in line else ("", line) for line in o["lines"]]
        self.setWindowTitle(item_title(it))
        rgb = item_rgb(it, self.main)
        head = (f"<span style='color:rgb{tuple(rgb)}'>■</span>&nbsp;<b style='font-size:13pt'>"
                f"{html.escape(item_title(it))}</b>")
        if sub:
            head += f"<br><b>{html.escape(sub)}</b>"
        self.head.setText(head)
        dim = self.palette().color(QPalette.PlaceholderText).name()
        h = ["<table cellspacing='0' cellpadding='2'>"]
        for lab, val in rows:
            h.append(f"<tr><td style='padding-right:10px; color:{dim}'>{html.escape(lab)}</td>"
                     f"<td>{html.escape(str(val))}</td></tr>")
        h.append("</table>")
        if area:
            h.append(f"<p><b>Areas:</b> {html.escape(area)}</p>")
        if note:
            h.append(f"<p><i>{html.escape(note)}</i></p>")
        if text:
            h.append("<pre style='white-space:pre-wrap; font-family:monospace'>" + html.escape(text) + "</pre>")
        self.body.setHtml("".join(h))
        self._text = "\n".join([item_title(it)] + ([sub] if sub else []) +
                               [f"{a}: {b}" if a else str(b) for a, b in rows] +
                               ([f"Areas: {area}"] if area else []) + ([text] if text else []))

    def _now(self):
        wo = self.main.warnings
        if wo.mode == "live" or wo.frame_time is None:
            return datetime.now(timezone.utc)
        return wo.frame_time

    def _alert(self, a):
        info = a.info or {}
        rows = []
        if a.office:
            rows.append(("From", ("NWS " + a.office) if not a.office.startswith("NWS") else a.office))
        if a.issued:
            rows.append(("Issued", when(a.issued)))
        if a.expires:
            rows.append(("Until", f"{when(a.expires)}  ({left(a.expires, self._now())})"))
        rows += list(info.get("facts") or [])
        mot = info.get("motion")
        if mot:
            frm, kt = mot
            rows.append(("Storm motion", f"toward the {compass(frm + 180)} at {round(kt * 1.15078)} mph "
                                         f"({kt} kt, from {frm}°)"))
        area = a.area if a.area and not a.area.startswith("#") else ""
        if a.area.startswith("#"):
            rows.append(("Event", a.area))
        text, note = None, ""
        if info.get("description"):
            text = info["description"]
            if info.get("instruction"):
                text += "\n\nPRECAUTIONARY/PREPAREDNESS ACTIONS...\n\n" + info["instruction"]
        elif info.get("iem"):
            iem = info["iem"]
            key = "iem:" + ".".join(str(iem[k]) for k in ("year", "wfo", "phenomena", "significance", "etn"))
            text, note = self._text_for(key, lambda: W.fetch_warning_text(iem), "the warning text")
        return rows, area, text, info.get("headline") or "", note

    def _mcd(self, m):
        rows = []
        if m.get("concerning"):
            rows.append(("Concerning", m["concerning"].capitalize()))
        if m.get("issue"):
            rows.append(("Issued", when(m["issue"])))
        if m.get("expire"):
            rows.append(("Until", f"{when(m['expire'])}  ({left(m['expire'], datetime.now(timezone.utc))})"))
        if m.get("watch") is not None:
            rows.append(("Chance of a watch", f"{m['watch']}%"))
        pid = m.get("product_id") or ""
        if not pid:
            return rows, None, "No text available – open it on the SPC website."
        text, note = self._text_for("mcd:" + pid, lambda: _get_text(feeds.mcd_text_url(pid)), "the discussion")
        return rows, text, note

    def _report(self, r):
        rows = []
        if r.get("where"):
            rows.append(("Where", r["where"]))
        if r.get("time") is not None:
            rows.append(("Time", when(r["time"])))
        if r.get("reporter"):
            rows.append(("Reported by", r["reporter"].title() if r["reporter"].isupper() else r["reporter"]))
        src = r.get("source") or ""
        if src == "NWS" and r.get("office"):
            src = f"NWS {r['office']}"
        if src:
            rows.append(("Source", src))
        text = r.get("remark") or ""
        if not text and r.get("hover"):
            text = r["hover"]
        return rows, text

    def _text_for(self, key, fn, what):
        """(text, note) for a downloaded text: the text once it's in, or a note while it loads / if it failed."""
        if key in self._texts:
            return self._texts[key], ""
        if key in self._failed and key not in self._loading:
            msg = self._failed.pop(key)            # shown once; asking again tries again
            return None, msg
        if key not in self._loading:
            self._loading.add(key)

            def work():
                try:
                    txt, ok = fn(), True
                except Exception as exc:
                    from ..data.aws import friendly_error
                    txt, ok = f"Couldn't load {what} ({friendly_error(exc)}).", False
                self._relay.done.emit(key, txt, ok)
            threading.Thread(target=work, daemon=True).start()
        return None, f"Loading {what}…"

    def _loaded(self, key, text, ok):
        self._loading.discard(key)
        if ok:
            self._texts[key] = text
        else:
            self._failed[key] = text
        if self.isVisible() and self.cur is not None:
            self._show(max(0, self.list.currentRow()))

    # ------------------------------------------------------------------ actions
    def _highlight(self, uid):
        wo = self.main.warnings
        if uid is None:
            if self._selected is not None and wo.selected_uid == self._selected:
                wo.selected_uid = None
            self._selected = None
        else:
            wo.selected_uid = self._selected = uid
        self.main.view.update()

    def _go(self):
        from .panels import _zoom_to
        it = self.cur
        if it is None:
            return
        k, o = it["kind"], it["obj"]
        view = self.main.view
        if k == "alert":
            xy = np.concatenate(self.main.warnings._xy(o, view.lat0, view.lon0))
            _zoom_to(self.main, xy[:, 0], xy[:, 1])
        elif k == "mcd" and o.get("xy"):
            xy = np.concatenate(o["xy"])
            _zoom_to(self.main, xy[:, 0], xy[:, 1])
        elif k == "report" and "xy" in o:
            view.set_view(float(o["xy"][0]), float(o["xy"][1]), max(view.scale, 4.0))

    def _open_web(self):
        it = self.cur
        if it is not None and it["kind"] == "mcd":
            m = it["obj"]
            year = (m.get("issue") or datetime.now(timezone.utc)).year
            QDesktopServices.openUrl(QUrl(feeds.mcd_page(m["number"], year)))

    def _copy(self):
        QGuiApplication.clipboard().setText(getattr(self, "_text", ""))

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._highlight(None)
