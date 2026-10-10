"""The place search box (top right, Ctrl+F): towns as you type, OpenStreetMap search on Enter."""
from __future__ import annotations

import threading

from PySide6.QtCore import QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QCompleter, QLineEdit

from ..features import places

ROLE_PLACE = Qt.UserRole + 1          # the place dict ("online" row: {"online": True})
ROLE_INSERT = Qt.UserRole + 2         # what goes into the box when a row is chosen


class _Relay(QObject):
    found = Signal(int, object, str)  # request number, results, error message


class PlaceSearch(QLineEdit):
    placeChosen = Signal(object)      # a place dict (see features.places)
    cleared = Signal()

    def __init__(self, local: places.LocalPlaces, near_fn=None, parent=None):
        super().__init__(parent)
        self.local = local
        self.near_fn = near_fn        # () -> (lat, lon) the map is showing (online results near it first)
        self.setPlaceholderText("Search places  (Ctrl+F)")
        self.setToolTip("Find a town, road, address or landmark.\nTowns appear as you type; press Enter to search "
                        "OpenStreetMap for anything else.\nYou can also type coordinates (35.22, -97.44).")
        self.setClearButtonEnabled(True)
        self.setFixedWidth(250)
        self.model = QStandardItemModel(self)
        self.comp = QCompleter(self.model, self)
        self.comp.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.comp.setCompletionRole(ROLE_INSERT)
        self.comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.comp.setMaxVisibleItems(12)
        self.comp.setWidget(self)
        self.comp.activated[QModelIndex].connect(self._activated)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._suggest)
        self._relay = _Relay(self)
        self._relay.found.connect(self._found)
        self._req = 0
        self._just_chosen = False
        self.textEdited.connect(lambda _t: self._timer.start())
        self.textChanged.connect(self._text_changed)
        # (Qt hands Enter to the box before it reports the list row chosen with it: wait one turn of the
        # event loop, so a chosen row wins over "search online")
        self.returnPressed.connect(lambda: QTimer.singleShot(0, self._enter))

    # ------------------------------------------------------------------ rows
    def _fill(self, rows):
        self.model.clear()
        for text, place, insert, enabled in rows:
            it = QStandardItem(text)
            it.setData(place, ROLE_PLACE)
            it.setData(insert, ROLE_INSERT)
            it.setEditable(False)
            if not enabled:
                it.setFlags(Qt.NoItemFlags)
            self.model.appendRow(it)
        if rows:
            self.comp.setCompletionPrefix("")
            self.comp.complete()
        else:
            self.comp.popup().hide()

    @staticmethod
    def _row_text(p) -> str:
        extra = p["kind"]
        if p.get("pop"):
            extra += f" · {p['pop']:,}"
        return f"{p['label']}    ({extra})"

    def _suggest(self):
        text = self.text().strip()
        if len(text) < 2:
            self.comp.popup().hide()
            return
        rows = []
        ll = places.parse_coords(text)
        if ll is not None:
            p = dict(name=f"{ll[0]:.4f}, {ll[1]:.4f}", label=f"{ll[0]:.4f}, {ll[1]:.4f}", kind="Coordinates",
                     lat=ll[0], lon=ll[1], pop=0, bbox=None, geom=[], source="coords")
            rows.append((f"Go to {p['label']}", p, text, True))
        else:
            for p in self.local.search(text):
                rows.append((self._row_text(p), p, p["name"], True))
        rows.append((f"Search online for “{text}” – towns, roads, addresses  (Enter)", {"online": True}, text, True))
        self._fill(rows)

    # ------------------------------------------------------------------ choosing
    def _activated(self, index):
        place = index.data(ROLE_PLACE)
        if not place:
            return
        self._just_chosen = True
        QTimer.singleShot(0, lambda: setattr(self, "_just_chosen", False))
        if place.get("online"):
            self.search_online(self.text().strip())
        else:
            QTimer.singleShot(0, lambda: (self.setText(place["label"]), self.setCursorPosition(0)))  # (after the
            # completer's own insert; the start of a long name shows)
            self.placeChosen.emit(place)

    def _enter(self):
        if self._just_chosen:                  # (Enter on a row in the list already handled it)
            return
        text = self.text().strip()
        ll = places.parse_coords(text)
        if ll is not None:
            self._timer.stop()
            self.placeChosen.emit(dict(name=text, label=text, kind="Coordinates", lat=ll[0], lon=ll[1], pop=0,
                                       bbox=None, geom=[], source="coords"))
            return
        if len(text) >= 2:
            self.search_online(text)

    def search_online(self, text):
        self._timer.stop()
        self._req += 1
        req = self._req
        self._fill([(f"Searching OpenStreetMap for “{text}”…", None, text, False)])
        near = None
        try:
            near = self.near_fn() if self.near_fn else None
        except Exception:
            near = None

        def work():
            try:
                res, err = places.search_osm(text, near), ""
            except Exception as exc:
                from ..data.aws import friendly_error
                res, err = [], friendly_error(exc)
            self._relay.found.emit(req, res, err)
        threading.Thread(target=work, daemon=True).start()

    def _found(self, req, results, err):
        if req != self._req or not self.hasFocus() and not self.text():
            return
        text = self.text().strip()
        if err:
            rows = [(f"Search failed ({err})", None, text, False)]
        elif not results:
            rows = [(f"Nothing found for “{text}”", None, text, False)]
        else:
            rows = [(self._row_text(p), p, p["name"], True) for p in results]
        rows.append((places.ATTRIBUTION, None, text, False))
        self._fill(rows)
        self.setFocus()

    def _text_changed(self, text):
        if not text:
            self._req += 1                     # a search still running no longer shows up
            self.comp.popup().hide()
            self.cleared.emit()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and not self.comp.popup().isVisible():
            self.clearFocus()                  # back to the map
            return
        super().keyPressEvent(ev)
