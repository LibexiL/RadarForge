"""The drawer: every side panel in one place, at the right of the map.

A column of big buttons down its right edge picks what it shows - Radar (products and tilts), Layers, Weather
(Severe, Winter and Tropical behind one switch), Alerts, Storms and Models. Clicking the button that's already on
folds the drawer away, giving the map the whole window; clicking any button opens it again. Only one thing shows
at a time: no stacks of tabs.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import icons


class _Slot(QStackedWidget):
    """Holds one panel - or, while the panel is out of the drawer (a window of its own, or docked by the map), a
    note saying so with a button to put it back."""

    def __init__(self, key, title, widget, put_back):
        super().__init__()
        self.key, self.panel = key, widget
        self.addWidget(widget)
        note = QWidget()
        v = QVBoxLayout(note)
        v.setContentsMargins(24, 40, 24, 24)
        v.setSpacing(12)
        lab = QLabel(f"{title} is out of the drawer: it's in its own window, or docked beside the map.")
        lab.setWordWrap(True)
        lab.setProperty("role", "hint")
        v.addWidget(lab)
        b = QPushButton("Put it back in the drawer")
        b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        b.clicked.connect(lambda: put_back(key))
        v.addWidget(b)
        v.addStretch(1)
        self.note = note
        self.addWidget(note)

    def is_out(self):
        return self.currentWidget() is self.note

    def take_out(self):
        if self.indexOf(self.panel) >= 0:
            self.removeWidget(self.panel)
        self.setCurrentWidget(self.note)

    def put_back(self, widget=None):
        w = widget or self.panel
        self.panel = w
        if self.indexOf(w) < 0:
            self.insertWidget(0, w)
        w.show()
        self.setCurrentWidget(w)


class _Head(QWidget):
    """The drawer's title line: drag it out of the drawer to give the panel a window of its own."""
    dragged_out = Signal(QPoint)

    def __init__(self):
        super().__init__()
        self.setObjectName("drawerHead")
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag this title out of the drawer to put the panel in a window of its own "
                        "(then dock it anywhere by its tab)")
        self._press = None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press is not None and (ev.globalPosition().toPoint() - self._press).manhattanLength() > 24:
            self._press = None
            self.dragged_out.emit(ev.globalPosition().toPoint())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._press = None
        super().mouseReleaseEvent(ev)


class _Switch(QWidget):
    """Several panels behind a segmented switch (Weather: Severe | Winter | Tropical)."""
    changed = Signal(str)

    def __init__(self, parts):
        super().__init__()
        self.keys = [k for k, _l, _w in parts]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("drawerSwitch")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 8, 12, 6)
        h.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for i, (key, label, _w) in enumerate(parts):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setProperty("role", "viewswitch")
            b.setProperty("side", "left" if i == 0 else "right" if i == len(parts) - 1 else "mid")
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b, i)
            self.buttons[key] = b
            h.addWidget(b)
        lay.addWidget(bar)
        self.stack = QStackedWidget()
        for _k, _l, w in parts:
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        self.group.idClicked.connect(self._picked)
        self.buttons[self.keys[0]].setChecked(True)

    def _picked(self, i):
        self.stack.setCurrentIndex(i)
        self.changed.emit(self.keys[i])

    def show_key(self, key):
        if key in self.buttons:
            i = self.keys.index(key)
            self.buttons[key].setChecked(True)
            self.stack.setCurrentIndex(i)

    def current(self):
        return self.keys[self.stack.currentIndex()]


class Drawer(QWidget):
    """[sections]: [(key, label, icon, tip, [(panel key, title, widget)])] - one panel shows it directly, several
    get a switch along the top."""
    toggled = Signal(bool)            # opened / folded away
    shown = Signal(str)               # the panel key now showing
    popOut = Signal(str, QPoint)      # take this panel out of the drawer (a window of its own, at this point)
    putBack = Signal(str)             # the panel out of the drawer should come back

    def __init__(self, sections, parent=None):
        super().__init__(parent)
        self.setObjectName("drawer")
        self._open = True
        self._sections = {}           # section key -> (index, widget, [panel keys])
        self._where = {}              # panel key -> section key
        self._titles = {}
        self._actions = {}
        self._slots = {}              # panel key -> _Slot
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # the content: a title line and the section
        self.body = QFrame()
        self.body.setObjectName("drawerBody")
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        head = _Head()
        head.dragged_out.connect(lambda gpos: self._pop_out_current(gpos))
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 10, 8, 4)
        self.title = QLabel()
        self.title.setProperty("role", "drawerTitle")
        hl.addWidget(self.title, 1)
        pop = QToolButton()
        pop.setIcon(icons.icon("popout"))
        pop.setIconSize(QSize(14, 14))
        pop.setAutoRaise(True)
        pop.setToolTip("Put this panel in a window of its own (dock it anywhere by its tab; close it to bring "
                       "it back to the drawer)")
        pop.clicked.connect(lambda: self._pop_out_current(None))
        self._pop_btn = pop
        hl.addWidget(pop)
        close = QToolButton()
        close.setIcon(icons.icon("close"))
        close.setIconSize(QSize(14, 14))
        close.setAutoRaise(True)
        close.setToolTip("Fold the drawer away (the map gets the room)")
        close.clicked.connect(lambda: self.set_open(False))
        self._close_btn = close
        hl.addWidget(close)
        bl.addWidget(head)
        self.stack = QStackedWidget()
        bl.addWidget(self.stack, 1)
        outer.addWidget(self.body, 1)

        # the buttons down the right edge
        self.nav = QFrame()
        self.nav.setObjectName("drawerNav")
        nl = QVBoxLayout(self.nav)
        nl.setContentsMargins(4, 8, 4, 8)
        nl.setSpacing(2)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = {}
        for key, label, icon, tip, parts in sections:
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setProperty("role", "drawerNav")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(20, 20))
            b.setProperty("iconName", icon)
            b.setIcon(icons.icon(icon))
            b.setFixedWidth(64)
            b.clicked.connect(lambda _=False, k=key: self._nav_clicked(k))
            self.nav_group.addButton(b)
            self.nav_btns[key] = b
            nl.addWidget(b)
            slots = [(pk, title, _Slot(pk, title, pw, self.putBack.emit)) for pk, title, pw in parts]
            for pk, _t, slot in slots:
                self._slots[pk] = slot
            if len(slots) == 1:
                w = slots[0][2]
            else:
                w = _Switch(slots)
                w.changed.connect(lambda k: (self._sync_title(), self.shown.emit(k)))
            idx = self.stack.addWidget(w)
            self._sections[key] = (idx, w, [p[0] for p in parts])
            for pk, title, _w in parts:
                self._where[pk] = key
                self._titles[pk] = title
            self._titles[key] = label
        nl.addStretch(1)
        outer.addWidget(self.nav)
        self._current = sections[0][0]
        self.nav_btns[self._current].setChecked(True)
        self._sync_title()

    # ------------------------------------------------------------------ state
    def keys(self):
        return list(self._where)

    def current_key(self):
        """The panel key showing (inside a switch: its current part)."""
        _i, w, parts = self._sections[self._current]
        return w.current() if isinstance(w, _Switch) else parts[0]

    def is_open(self, key=None):
        if not self._open:
            return False
        return key is None or self.current_key() == key

    def _sync_title(self):
        idx, w, parts = self._sections[self._current]
        self.title.setText(self._titles[self._current])
        for k, a in self._actions.items():
            a.blockSignals(True)
            a.setChecked(self.is_open(k))
            a.blockSignals(False)

    def set_open(self, on):
        on = bool(on)
        if on == self._open:
            return
        self._open = on
        self.body.setVisible(on)
        for b in self.nav_btns.values():               # (folded: no button looks "on")
            b.setProperty("folded", not on)
            b.style().unpolish(b)
            b.style().polish(b)
        self._sync_title()
        self.toggled.emit(on)

    def toggle(self):
        self.set_open(not self._open)

    def _nav_clicked(self, key):
        if key == self._current and self._open:
            self.set_open(False)                       # the one already showing: fold away
            return
        self.show_section(key)

    def show_section(self, key):
        idx, w, parts = self._sections[key]
        self._current = key
        self.stack.setCurrentIndex(idx)
        self.nav_btns[key].setChecked(True)
        self.set_open(True)
        self._sync_title()
        self.shown.emit(self.current_key())

    def show_key(self, panel_key):
        """Open the drawer at a panel (e.g. "warnings", or "winter" inside Weather)."""
        sec = self._where.get(panel_key)
        if sec is None:
            return False
        _i, w, _p = self._sections[sec]
        if isinstance(w, _Switch):
            w.show_key(panel_key)
        self.show_section(sec)
        return True

    def action(self, panel_key):
        """A checkable action showing / folding a panel (for menus and shortcuts)."""
        a = self._actions.get(panel_key)
        if a is None:
            a = QAction(self._titles.get(panel_key, panel_key), self)
            a.setCheckable(True)

            def flip(on, k=panel_key):
                if on:
                    self.show_key(k)
                elif self.is_open(k):
                    self.set_open(False)
            a.toggled.connect(flip)
            self._actions[panel_key] = a
            self._sync_title()
        return a

    # ------------------------------------------------------------------ panels out of the drawer
    def _pop_out_current(self, gpos):
        key = self.current_key()
        if not self._slots[key].is_out():
            self.popOut.emit(key, gpos if gpos is not None else self.mapToGlobal(QPoint(-420, 40)))

    def is_out(self, panel_key):
        s = self._slots.get(panel_key)
        return s is not None and s.is_out()

    def take_out(self, panel_key):
        """The panel's widget, taken out (its page shows a note meanwhile)."""
        s = self._slots[panel_key]
        w = s.panel
        s.take_out()
        return w

    def put_back(self, panel_key, widget=None):
        self._slots[panel_key].put_back(widget)

    def retint(self):
        """New theme: the buttons' icons in the new text colour."""
        for b in self.nav_btns.values():
            b.setIcon(icons.icon(b.property("iconName")))
        self._close_btn.setIcon(icons.icon("close"))
        self._pop_btn.setIcon(icons.icon("popout"))

    def state(self) -> dict:
        return {"open": self._open, "section": self._current, "part": self.current_key()}

    def restore(self, st):
        st = st or {}
        part = st.get("part")
        if part and part in self._where:
            self.show_key(part)
        elif st.get("section") in self._sections:
            self.show_section(st["section"])
        self.set_open(bool(st.get("open", True)))
