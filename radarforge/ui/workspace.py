"""Workspace: the map plus movable, snapping tool panels.

Qt's own dock widgets need pixel-precise aiming and pop out floating windows at
the slightest drag, which feels wonky. This is a small docking system of our own:

* The window is a tree of splitters. Leaves are the map ("center") and panel
  stacks (tabs on top, one panel shown at a time).
* Drag a tab (or the empty part of a tab bar to move the whole group). While
  dragging, every panel shows big drop zones: its top / bottom / left / right
  quarter splits it, the middle stacks as a tab. The map's outer quarter docks
  next to the map, and the window's outer edge docks along that whole edge.
  A blue preview shows exactly where the panel will land.
* Drop anywhere else (the middle of the map, or outside the window) to float it.
* Layout (including sizes and floating windows) is saved as plain JSON.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QMenu, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout,
                               QWidget)

HEADER_H = 27
GRIP_W = 12           # dotted handle at the left of a tab bar: drags the whole group
EDGE_PX = 22          # window-edge drop strip
ZONE = 0.30           # outer fraction of a panel that splits instead of tabbing
MAP_ZONE = 0.24       # outer fraction of the map that docks next to it


def _pal():
    return QApplication.palette()


def _accent():
    return _pal().color(QPalette.Highlight)


# --------------------------------------------------------------------------- #
# splitter + center host
# --------------------------------------------------------------------------- #
class Split(QSplitter):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(6)
        self.setOpaqueResize(True)


class CenterHost(QWidget):
    def __init__(self, center: QWidget):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(center)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)


# --------------------------------------------------------------------------- #
# panel stack (tab header + stacked panels)
# --------------------------------------------------------------------------- #
class StackHeader(QWidget):
    """Tab strip we draw ourselves so that dragging works the same everywhere."""

    def __init__(self, stack):
        super().__init__(stack)
        self.stack = stack
        self.setFixedHeight(HEADER_H)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self._press = None          # (pos, tab index or None)
        self._pressed_btn = None
        self._hover_btn = None
        self._hover_tab = None
        self.dragging = False
        self.setToolTip("Drag a tab to move that panel, or drag the empty bar to move the whole group.\n"
                        "Double-click a tab to float it. Middle-click closes it.")

    # geometry ---------------------------------------------------------------
    def _buttons(self):
        h = HEADER_H
        r = self.rect()
        close = QRect(r.right() - h + 3, 3, h - 6, h - 6)
        flt = QRect(close.left() - h + 4, 3, h - 6, h - 6)
        return {"close": close, "float": flt}

    def _tab_rects(self):
        keys = self.stack.keys
        if not keys:
            return []
        bold = QFont(self.font())
        bold.setBold(True)
        fm = QFontMetrics(bold)          # size for the bold (active) look so text never jumps
        avail = max(40, self._buttons()["float"].left() - 6 - GRIP_W)
        want = [fm.horizontalAdvance(self.stack.ws.title(k)) + 26 for k in keys]
        total = sum(want)
        if total > avail:
            f = avail / total
            want = [max(44, int(w * f)) for w in want]
        rects, x = [], GRIP_W
        for w in want:
            rects.append(QRect(x, 0, w, HEADER_H))
            x += w
        return rects

    def tab_at(self, pos):
        for i, r in enumerate(self._tab_rects()):
            if r.contains(pos):
                return i
        return None

    def btn_at(self, pos):
        for k, r in self._buttons().items():
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                return k
        return None

    # painting -----------------------------------------------------------------
    def paintEvent(self, _ev):
        pal = _pal()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        win = pal.color(QPalette.Window)
        base = pal.color(QPalette.Base)
        text = pal.color(QPalette.WindowText)
        dim = QColor(text)
        dim.setAlpha(150)
        acc = _accent()
        dark = win.lightness() < 128
        bar = win.darker(118) if dark else win.darker(106)
        p.fillRect(self.rect(), bar)
        p.setPen(QPen(pal.color(QPalette.Mid), 1))
        p.drawLine(0, HEADER_H - 1, self.width(), HEADER_H - 1)
        # grip dots
        p.setPen(Qt.NoPen)
        p.setBrush(dim)
        for gy in (9, 13, 17):
            for gx in (4, 8):
                p.drawEllipse(QPointF(gx, gy + 0.5), 1.1, 1.1)
        cur = self.stack.current()
        for i, (k, r) in enumerate(zip(self.stack.keys, self._tab_rects())):
            active = k == cur
            if active:
                p.fillRect(r.adjusted(0, 3, 0, 0), base)
                p.fillRect(QRect(r.left(), HEADER_H - 3, r.width(), 3), acc)
            elif i == self._hover_tab:
                hv = QColor(text)
                hv.setAlpha(22)
                p.fillRect(r.adjusted(0, 3, 0, -1), hv)
            f = QFont(self.font())
            f.setBold(active)
            p.setFont(f)
            fm = QFontMetrics(f)
            p.setPen(text if active else dim)
            p.drawText(r.adjusted(11, 1, -9, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(self.stack.ws.title(k), Qt.ElideRight, r.width() - 20))
        # buttons
        for name, r in self._buttons().items():
            hov = name == self._hover_btn
            if hov:
                hb = QColor(text)
                hb.setAlpha(35)
                if name == "close":
                    hb = QColor(200, 60, 60, 190)
                p.setPen(Qt.NoPen)
                p.setBrush(hb)
                p.drawRoundedRect(QRectF(r), 3, 3)
            p.setPen(QPen(text if hov else dim, 1.4))
            p.setBrush(Qt.NoBrush)
            c = QRectF(r).center()
            if name == "close":
                d = 4.0
                p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
                p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
            else:
                floating = self.stack.floating
                if floating:      # "dock back" arrow into a box
                    p.drawRect(QRectF(c.x() - 5, c.y() - 4, 10, 8))
                    p.drawLine(QPointF(c.x() - 5, c.y() - 1), QPointF(c.x() + 5, c.y() - 1))
                else:             # pop-out window
                    p.drawRect(QRectF(c.x() - 5, c.y() - 2, 8, 7))
                    p.drawLine(QPointF(c.x() - 2, c.y() - 5), QPointF(c.x() + 5, c.y() - 5))
                    p.drawLine(QPointF(c.x() + 5, c.y() - 5), QPointF(c.x() + 5, c.y() + 2))
        p.end()

    # mouse ----------------------------------------------------------------------
    def _set_hover(self, pos):
        b = self.btn_at(pos) if pos is not None else None
        t = self.tab_at(pos) if pos is not None and b is None else None
        if (b, t) != (self._hover_btn, self._hover_tab):
            self._hover_btn, self._hover_tab = b, t
            self.update()
        ws = self.stack.ws
        self.setCursor(Qt.ArrowCursor if (b or ws.locked) else Qt.OpenHandCursor)

    def leaveEvent(self, ev):
        self._set_hover(None)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        pos = ev.position().toPoint()
        b = self.btn_at(pos)
        if b is not None and ev.button() == Qt.LeftButton:
            self._pressed_btn = b
            return
        i = self.tab_at(pos)
        if ev.button() == Qt.MiddleButton and i is not None:
            self.stack.ws.close_panel(self.stack.keys[i])
            return
        if ev.button() == Qt.LeftButton:
            self._press = (pos, i)
            if i is not None:
                self.stack.set_current(self.stack.keys[i])

    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint()
        gpos = ev.globalPosition().toPoint()
        ws = self.stack.ws
        if self.dragging:
            ws.drag_move(gpos)
            return
        if self._press is not None and not ws.locked and \
                (pos - self._press[0]).manhattanLength() >= QApplication.startDragDistance():
            i = self._press[1]
            keys = [self.stack.keys[i]] if i is not None and i < len(self.stack.keys) else list(self.stack.keys)
            self._press = None
            if keys:
                self.dragging = True
                self.setCursor(Qt.ClosedHandCursor)
                ws.drag_begin(keys, self.stack, gpos, self)
            return
        if not (ev.buttons() & Qt.LeftButton):
            self._set_hover(pos)

    def mouseReleaseEvent(self, ev):
        pos = ev.position().toPoint()
        ws = self.stack.ws
        if self.dragging:
            self.dragging = False
            self.setCursor(Qt.OpenHandCursor)
            ws.drag_end(ev.globalPosition().toPoint())
            return
        b, self._pressed_btn = self._pressed_btn, None
        self._press = None
        if b is not None and self.btn_at(pos) == b:
            key = self.stack.current()
            if key is None:
                return
            if b == "close":
                ws.close_panel(key)
            elif self.stack.floating:
                ws.dock_panel(key)
            else:
                ws.float_panel(key)

    def mouseDoubleClickEvent(self, ev):
        i = self.tab_at(ev.position().toPoint())
        if i is not None:
            k = self.stack.keys[i]
            ws = self.stack.ws
            ws.dock_panel(k) if self.stack.floating else ws.float_panel(k)

    def contextMenuEvent(self, ev):
        i = self.tab_at(ev.pos())
        key = self.stack.keys[i] if i is not None else self.stack.current()
        if key is None:
            return
        ws = self.stack.ws
        m = QMenu(self)
        title = ws.title(key)
        if self.stack.floating:
            m.addAction(f"Dock “{title}”", lambda: ws.dock_panel(key))
        else:
            m.addAction(f"Float “{title}”", lambda: ws.float_panel(key))
            mv = m.addMenu("Move to")
            for where, label in (("left", "Left edge"), ("right", "Right edge"), ("bottom", "Bottom edge"),
                                 ("top", "Top edge")):
                mv.addAction(label, lambda w=where: ws.move_to_edge(key, w))
        m.addSeparator()
        m.addAction(f"Close “{title}”", lambda: ws.close_panel(key))
        if len(self.stack.keys) > 1:
            m.addAction("Close this group", lambda: [ws.close_panel(k) for k in list(self.stack.keys)])
        m.exec(ev.globalPos())


class PanelStack(QWidget):
    def __init__(self, ws, keys=(), floating=False, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.keys: list = []
        self.floating = floating
        self.header = StackHeader(self)
        self.body = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.body, 1)
        self.setMinimumSize(170, 110)
        for k in keys:
            self.add(k)

    def add(self, key, make_current=True):
        if key in self.keys:
            if make_current:
                self.set_current(key)
            return
        w = self.ws.widget(key)
        self.keys.append(key)
        self.body.addWidget(w)
        w.show()
        if make_current or len(self.keys) == 1:
            self.body.setCurrentWidget(w)
        self.header.update()

    def take(self, key):
        """Remove a panel from this stack (the widget stays alive)."""
        if key not in self.keys:
            return
        w = self.ws.widget(key)
        self.keys.remove(key)
        self.body.removeWidget(w)
        # park it on the workspace (same window) so OpenGL panels keep their context
        w.hide()
        w.setParent(self.ws)
        self.header.update()

    def current(self):
        w = self.body.currentWidget()
        for k in self.keys:
            if self.ws.widget(k) is w:
                return k
        return self.keys[0] if self.keys else None

    def set_current(self, key):
        if key in self.keys:
            self.body.setCurrentWidget(self.ws.widget(key))
            self.header.update()
            if self.floating:
                self.window().setWindowTitle(self.ws.title(key))

    def side_only(self):
        return bool(self.keys) and all(self.ws.group(k) == "side" for k in self.keys)


class FloatWindow(QWidget):
    def __init__(self, ws, stack):
        super().__init__(ws.window(), Qt.Tool)
        self.ws = ws
        self.stack = stack
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(stack)
        self.setWindowTitle(ws.title(stack.current()) if stack.current() else "RadarForge")

    def closeEvent(self, ev):
        self.ws._float_closed(self)
        super().closeEvent(ev)


# --------------------------------------------------------------------------- #
# drop-zone overlay
# --------------------------------------------------------------------------- #
class Zone:
    __slots__ = ("kind", "where", "target", "rect", "preview", "label")

    def __init__(self, kind, where=None, target=None, rect=QRect(), preview=QRect(), label=""):
        self.kind, self.where, self.target = kind, where, target      # kind: stack | center | edge | float
        self.rect, self.preview, self.label = rect, preview, label


class SnapOverlay(QWidget):
    def __init__(self, ws):
        super().__init__(ws)
        self.ws = ws
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.targets: list = []       # [(rect, is_center)]
        self.zone: Zone | None = None
        self.ghost = ("", QPoint())
        self.hide()

    def paintEvent(self, _ev):
        p = QPainter(self)
        self._paint(p)
        p.end()

    def render_region(self, rect):
        """The overlay as an image cropped to rect (for native windows it can't draw over)."""
        dpr = self.devicePixelRatioF()
        img = QImage(max(1, int(rect.width() * dpr)), max(1, int(rect.height() * dpr)),
                     QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        p = QPainter(img)
        p.translate(-rect.x(), -rect.y())
        self._paint(p)
        p.end()
        return img

    def _paint(self, p):
        p.setRenderHint(QPainter.Antialiasing, True)
        acc = _accent()
        p.fillRect(self.rect(), QColor(0, 0, 0, 45))
        # every place a panel can go
        pen = QPen(QColor(acc.red(), acc.green(), acc.blue(), 110), 1.2, Qt.DashLine)
        for r, _c in self.targets:
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(r).adjusted(3, 3, -3, -3), 5, 5)
        # edge strips of the window
        strip = QColor(acc)
        strip.setAlpha(40)
        r = self.rect()
        for er in (QRect(0, 0, EDGE_PX // 2, r.height()), QRect(r.width() - EDGE_PX // 2, 0, EDGE_PX // 2, r.height()),
                   QRect(0, r.height() - EDGE_PX // 2, r.width(), EDGE_PX // 2)):
            p.fillRect(er, strip)
        z = self.zone
        if z is not None and z.kind != "float":
            fill = QColor(acc)
            fill.setAlpha(85)
            p.setPen(QPen(acc.lighter(130), 2))
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(z.preview).adjusted(2, 2, -2, -2), 6, 6)
        for r, is_center in self.targets:
            if z is not None and z.rect == r:
                self._compass(p, r, z.where if z.kind != "float" else "float", is_center)
        if z is not None and z.label:
            self._label(p, z)
        text, pos = self.ghost
        if text:
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            fm = QFontMetrics(f)
            gr = QRect(pos.x() + 14, pos.y() + 12, fm.horizontalAdvance(text) + 18, fm.height() + 10)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 22, 28, 225))
            p.drawRoundedRect(QRectF(gr), 5, 5)
            p.setPen(QColor(240, 240, 245))
            p.drawText(gr, Qt.AlignCenter, text)

    def _compass(self, p, r, where, is_center):
        c = QRectF(r).center()
        s, g = 24.0, 30.0
        acc = _accent()
        spots = {"center": (0, 0), "left": (-g, 0), "right": (g, 0), "top": (0, -g), "bottom": (0, g)}
        for name, (dx, dy) in spots.items():
            box = QRectF(c.x() + dx - s / 2, c.y() + dy - s / 2, s, s)
            on = name == where or (is_center and name == "center" and where == "float")
            p.setPen(QPen(QColor(255, 255, 255, 200) if on else QColor(210, 215, 225, 150), 1.3,
                          Qt.DashLine if (is_center and name == "center") else Qt.SolidLine))
            p.setBrush(acc if on else QColor(25, 28, 36, 200))
            p.drawRoundedRect(box, 4, 4)
            # tiny glyph showing where the panel goes
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 230 if on else 150))
            inner = box.adjusted(5, 5, -5, -5)
            if name == "left":
                p.drawRect(QRectF(inner.left(), inner.top(), inner.width() / 2.4, inner.height()))
            elif name == "right":
                p.drawRect(QRectF(inner.right() - inner.width() / 2.4, inner.top(), inner.width() / 2.4, inner.height()))
            elif name == "top":
                p.drawRect(QRectF(inner.left(), inner.top(), inner.width(), inner.height() / 2.4))
            elif name == "bottom":
                p.drawRect(QRectF(inner.left(), inner.bottom() - inner.height() / 2.4, inner.width(), inner.height() / 2.4))
            elif not is_center:
                p.drawRect(QRectF(inner.left(), inner.top(), inner.width(), 3))
                p.drawRect(inner.adjusted(0, 5, 0, 0))

    def _label(self, p, z):
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        base = z.preview if z.kind != "float" else z.rect
        if base.isNull():
            base = self.rect()
        w = fm.horizontalAdvance(z.label) + 20
        lr = QRect(0, 0, w, fm.height() + 10)
        lr.moveCenter(QRectF(base).center().toPoint() + QPoint(0, 58 if z.kind in ("stack", "center") else 0))
        lr.moveLeft(max(4, min(lr.left(), self.width() - w - 4)))
        lr.moveTop(max(4, min(lr.top(), self.height() - lr.height() - 4)))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 17, 22, 230))
        p.drawRoundedRect(QRectF(lr), 5, 5)
        p.setPen(QColor(245, 245, 250))
        p.drawText(lr, Qt.AlignCenter, z.label)


class _EscFilter(QObject):
    def __init__(self, ws):
        super().__init__()
        self.ws = ws

    def eventFilter(self, obj, ev):
        # ShortcutOverride too: otherwise the window's own Esc shortcut swallows the key
        if ev.type() in (QEvent.ShortcutOverride, QEvent.KeyPress) and ev.key() == Qt.Key_Escape:
            if ev.type() == QEvent.KeyPress:
                self.ws.drag_cancel()
            ev.accept()
            return True
        return False


# --------------------------------------------------------------------------- #
# workspace
# --------------------------------------------------------------------------- #
class Workspace(QWidget):
    panelOpened = Signal(str)
    panelClosed = Signal(str)
    sideToggled = Signal(bool)
    layoutChanged = Signal()

    def __init__(self, center: QWidget, parent=None):
        super().__init__(parent)
        self.center = CenterHost(center)
        self._panels: dict = {}          # key -> dict(title, widget, group, action)
        self.floats: list = []
        self.closed: set = set()
        self.side_hidden = False
        self.locked = False
        self._saved_sizes: dict = {}
        self._drag = None
        self.center_overlay = None        # callable(QImage | None): lets a native map draw the drop zones
        self._esc = _EscFilter(self)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self.root = Split(Qt.Horizontal, self)
        self.root.addWidget(self.center)
        self._lay.addWidget(self.root)
        self.overlay = SnapOverlay(self)

    # ---------------------------------------------------------------- registry
    def register(self, key, title, widget, group="side"):
        act = QAction(title, self, checkable=True)
        act.triggered.connect(lambda on, k=key: self.show_panel(k) if on else self.close_panel(k))
        if widget.parentWidget() is None:
            widget.setParent(self)          # keep every panel inside this window, even while closed
        widget.hide()
        self._panels[key] = {"title": title, "widget": widget, "group": group, "action": act}
        widget.windowTitleChanged.connect(lambda t, k=key: self._title_changed(k, t))
        self.closed.add(key)

    def keys(self):
        return list(self._panels)

    def widget(self, key):
        return self._panels[key]["widget"]

    def title(self, key):
        return self._panels[key]["title"] if key in self._panels else key

    def group(self, key):
        return self._panels[key]["group"]

    def action(self, key):
        return self._panels[key]["action"]

    def _title_changed(self, key, text):
        if text:
            self._panels[key]["title"] = text
            st = self.stack_of(key)
            if st is not None:
                st.header.update()
                if st.floating and st.current() == key:
                    st.window().setWindowTitle(text)

    # ---------------------------------------------------------------- tree helpers
    def _walk(self, w=None):
        w = self.root if w is None else w
        yield w
        if isinstance(w, QSplitter):
            for i in range(w.count()):
                yield from self._walk(w.widget(i))

    def stacks(self, docked_only=False):
        out = [w for w in self._walk() if isinstance(w, PanelStack)]
        if not docked_only:
            out += [f.stack for f in self.floats]
        return out

    def stack_of(self, key):
        for st in self.stacks():
            if key in st.keys:
                return st
        return None

    def is_open(self, key):
        return self.stack_of(key) is not None

    def is_showing(self, key):
        return self.is_open(key) and self.widget(key).isVisible()

    def _sync_actions(self):
        for k, info in self._panels.items():
            a = info["action"]
            on = self.is_open(k)
            if a.isChecked() != on:
                a.blockSignals(True)
                a.setChecked(on)
                a.blockSignals(False)

    def _sync_visibility(self):
        def vis(w):
            if w is self.center:
                w.show()
                return True
            if isinstance(w, PanelStack):
                on = not (self.side_hidden and w.side_only())
                w.setVisible(on)
                return on
            if isinstance(w, QSplitter):
                any_on = False
                for i in range(w.count()):
                    any_on = vis(w.widget(i)) or any_on
                if w is not self.root:
                    w.setVisible(any_on)
                return any_on
            return w.isVisible()
        vis(self.root)

    def _new_stack(self, keys):
        return PanelStack(self, keys, parent=self)

    def _collapse(self, split):
        """Tidy a splitter after a child left it."""
        if split is self.root:
            if split.count() == 1 and isinstance(split.widget(0), QSplitter):
                child = split.widget(0)
                sizes = child.sizes()
                self._lay.replaceWidget(split, child)      # moves child straight onto the workspace
                self.root = child
                split.setParent(None)
                split.deleteLater()
                child.setSizes(sizes)
                self.overlay.raise_()
            return
        parent = split.parentWidget()
        if not isinstance(parent, QSplitter):
            return
        if split.count() == 0:
            split.setParent(None)
            split.deleteLater()
            self._collapse(parent)
            return
        if split.count() == 1:
            idx = parent.indexOf(split)
            sizes = parent.sizes()
            child = split.widget(0)
            parent.replaceWidget(idx, child)
            split.setParent(None)
            split.deleteLater()
            parent.setSizes(sizes)
            if isinstance(child, QSplitter) and child.orientation() == parent.orientation():
                self._flatten(parent, child)
            return
        if split.orientation() == parent.orientation():
            self._flatten(parent, split)

    def _flatten(self, parent, child):
        idx = parent.indexOf(child)
        psizes = parent.sizes()
        csizes = child.sizes()
        total = max(1, sum(csizes))
        span = psizes[idx]
        kids = [child.widget(i) for i in range(child.count())]
        new_sizes = psizes[:idx] + [max(40, int(span * s / total)) for s in csizes] + psizes[idx + 1:]
        for j, k in enumerate(kids):          # move the children first (never via a parentless widget)
            parent.insertWidget(idx + j, k)
        child.setParent(None)
        child.deleteLater()
        parent.setSizes(new_sizes)

    def _detach_stack(self, st):
        parent = st.parentWidget()
        st.setParent(None)
        st.deleteLater()
        if isinstance(parent, QSplitter):
            self._collapse(parent)

    def _take_key(self, key):
        """Remove a panel from wherever it is (widget stays alive)."""
        st = self.stack_of(key)
        if st is None:
            return
        st.take(key)
        if not st.keys:
            if st.floating:
                fw = st.window()
                if fw in self.floats:
                    self.floats.remove(fw)
                fw._closing = True
                fw.hide()
                fw.deleteLater()
            else:
                self._detach_stack(st)
        elif st.floating:
            st.window().setWindowTitle(self.title(st.current()))

    def _insert(self, keys, zone):
        """Place panels (already taken out of the layout) according to a drop zone."""
        if zone.kind == "stack" and zone.where == "center":
            for k in keys:
                zone.target.add(k)
            zone.target.set_current(keys[0])
            return zone.target
        new = self._new_stack(keys)
        horiz = zone.where in ("left", "right")
        orient = Qt.Horizontal if horiz else Qt.Vertical
        before = zone.where in ("left", "top")
        if zone.kind == "edge":
            root = self.root
            if root.orientation() == orient:
                total = sum(root.sizes()) or (self.width() if horiz else self.height())
                size = self._edge_size(total, horiz)
                sizes = root.sizes()
                scale = (total - size) / max(1, sum(sizes))
                sizes = [max(40, int(s * scale)) for s in sizes]
                root.insertWidget(0 if before else root.count(), new)
                sizes.insert(0 if before else len(sizes), size)
                root.setSizes(sizes)
            else:
                old = root
                total = (self.width() if horiz else self.height())
                self.root = Split(orient, self)
                self._lay.replaceWidget(old, self.root)
                size = self._edge_size(total, horiz)
                if before:
                    self.root.addWidget(new)
                    self.root.addWidget(old)
                    self.root.setSizes([size, total - size])
                else:
                    self.root.addWidget(old)
                    self.root.addWidget(new)
                    self.root.setSizes([total - size, size])
                self.overlay.raise_()
            return new
        target = zone.target
        parent = target.parentWidget()
        if not isinstance(parent, QSplitter):
            return None
        idx = parent.indexOf(target)
        sizes = parent.sizes()
        if parent.orientation() == orient:
            span = sizes[idx]
            share = int(span * (0.5 if zone.kind == "stack" else 0.3))
            sizes[idx] = span - share
            pos = idx if before else idx + 1
            parent.insertWidget(pos, new)
            sizes.insert(pos, share)
            parent.setSizes(sizes)
        else:
            ext = target.width() if horiz else target.height()
            split = Split(orient, self)
            parent.insertWidget(idx, split)
            split.addWidget(target)           # direct move: the map's OpenGL context survives
            share = int(ext * (0.5 if zone.kind == "stack" else 0.3))
            if before:
                split.insertWidget(0, new)
                split.setSizes([share, ext - share])
            else:
                split.addWidget(new)
                split.setSizes([ext - share, share])
            parent.setSizes(sizes)
        return new

    @staticmethod
    def _edge_size(total, horiz):
        return int(min(total * 0.22, 360)) if horiz else int(min(total * 0.3, 340))

    # ---------------------------------------------------------------- public ops
    def show_panel(self, key, focus=True):
        if key not in self._panels:
            return
        if self.side_hidden and self.group(key) == "side":
            self.set_side_hidden(False)
        st = self.stack_of(key)
        if st is None:
            self.closed.discard(key)
            st = self._place_home(key)
            self.panelOpened.emit(key)
        if st is not None:
            st.set_current(key)
            if st.floating:
                st.window().show()
                st.window().raise_()
        self._sync_visibility()
        self._sync_actions()
        self.layoutChanged.emit()

    def _home_of(self, key):
        """Remember where a panel is, so closing/floating and re-opening puts it back there."""
        st = self.stack_of(key)
        if st is None:
            return {}
        home = {"near": [k for k in st.keys if k != key], "floating": st.floating}
        parent = st.parentWidget()
        if not st.floating and isinstance(parent, QSplitter):
            i = parent.indexOf(st)
            horiz = parent.orientation() == Qt.Horizontal
            if parent is self.root and (i == 0 or i == parent.count() - 1):
                # docked along a window edge: reopen along the same edge
                home["edge"] = ("left" if horiz else "top") if i == 0 else ("right" if horiz else "bottom")
            for j, where in ((i + 1, "left" if horiz else "top"), (i - 1, "right" if horiz else "bottom")):
                if 0 <= j < parent.count():
                    sib = parent.widget(j)
                    if sib is self.center:
                        home["beside"] = ("#center", where)
                        break
                    if isinstance(sib, PanelStack) and sib.keys:
                        home["beside"] = (sib.current(), where)
                        break
                    if "edge" in home:
                        break
                    first = next((w for w in self._walk(sib) if isinstance(w, PanelStack) and w.keys), None)
                    if first is not None:
                        home["beside"] = (first.current(), where)
                        break
        return home

    def close_panel(self, key):
        if not self.is_open(key):
            return
        self._panels[key]["home"] = self._home_of(key)
        self._take_key(key)
        self.closed.add(key)
        self._sync_visibility()
        self._sync_actions()
        self.panelClosed.emit(key)
        self.layoutChanged.emit()

    def float_panel(self, key, gpos=None, size=None):
        if key not in self._panels:
            return
        w = self.widget(key)
        size = size or QSize(max(300, w.width()), max(260, w.height() + HEADER_H))
        was_open = self.is_open(key)
        if was_open:
            home = self._home_of(key)
            if not home.get("floating"):
                self._panels[key]["dock_home"] = home
        self._take_key(key)
        self.closed.discard(key)
        self._make_float([key], gpos, size)
        if not was_open:
            self.panelOpened.emit(key)
        self._sync_visibility()
        self._sync_actions()
        self.layoutChanged.emit()

    def _make_float(self, keys, gpos=None, size=QSize(320, 380), geom=None):
        st = PanelStack(self, keys, floating=True)
        fw = FloatWindow(self, st)
        fw._closing = False
        if geom:
            fw.setGeometry(QRect(*geom))
        else:
            fw.resize(size)
            if gpos is None:
                gpos = self.mapToGlobal(QPoint(self.width() // 2 - size.width() // 2, 80))
            fw.move(gpos - QPoint(40, 12))
        self.floats.append(fw)
        fw.show()
        return fw

    def _float_closed(self, fw):
        if getattr(fw, "_closing", False) or fw not in self.floats:
            return
        for k in list(fw.stack.keys):
            self._panels[k]["home"] = {"near": [], "floating": True}
            fw.stack.take(k)
            self.closed.add(k)
            self.panelClosed.emit(k)
        self.floats.remove(fw)
        fw.deleteLater()
        self._sync_actions()
        self.layoutChanged.emit()

    def dock_panel(self, key):
        """Put a floating panel back into the window (where it was before it floated)."""
        self._take_key(key)
        self._panels[key]["home"] = self._panels[key].pop("dock_home", {})
        st = self._place_home(key)
        if st is not None:
            st.set_current(key)
        self._sync_visibility()
        self._sync_actions()
        self.layoutChanged.emit()

    def move_to_edge(self, key, where):
        self._take_key(key)
        self._insert([key], Zone("edge", where))
        self._sync_visibility()
        self._sync_actions()
        self.layoutChanged.emit()

    def _place_home(self, key):
        home = self._panels[key].get("home") or {}
        if home.get("floating"):
            fw = self._make_float([key])
            return fw.stack
        for k in home.get("near", []):
            st = self.stack_of(k)
            if st is not None and not st.floating:
                st.add(key)
                return st
        beside = home.get("beside")
        if home.get("edge") and not beside:
            return self._insert([key], Zone("edge", home["edge"]))
        if beside:
            k, where = beside
            if k == "#center":
                return self._insert([key], Zone("center", where, self.center))
            st = self.stack_of(k)
            if st is not None and not st.floating:
                return self._insert([key], Zone("stack", where, st))
        group = self.group(key)
        docked = self.stacks(docked_only=True)
        if group == "side":
            side = [s for s in docked if s.side_only()]
            if side:
                side[-1].add(key)
                return side[-1]
            return self._insert([key], Zone("edge", "right"))
        tools = [s for s in docked if any(self.group(k) == group for k in s.keys)]
        if tools:
            tools[0].add(key)
            return tools[0]
        return self._insert([key], Zone("center", "bottom", self.center))

    def set_side_hidden(self, hidden):
        if hidden == self.side_hidden:
            return
        if hidden:
            self._saved_sizes = {id(w): (w, w.sizes()) for w in self._walk() if isinstance(w, QSplitter)}
        self.side_hidden = hidden
        self._sync_visibility()
        if not hidden:
            for w, sizes in self._saved_sizes.values():
                try:
                    if w.count() == len(sizes):
                        w.setSizes(sizes)
                except RuntimeError:
                    pass
            self._saved_sizes = {}
        self.sideToggled.emit(not hidden)
        self.layoutChanged.emit()

    def side_visible(self):
        return not self.side_hidden and any(s.side_only() for s in self.stacks(docked_only=True))

    def toggle_side(self):
        if not self.side_hidden and not any(s.side_only() for s in self.stacks(docked_only=True)):
            # nothing docked on the side: open the side panels
            for k in self._panels:
                if self.group(k) == "side" and not self.is_open(k):
                    self.show_panel(k, focus=False)
            return
        self.set_side_hidden(not self.side_hidden)

    # ---------------------------------------------------------------- dragging
    def drag_begin(self, keys, source, gpos, header):
        self._drag = {"keys": keys, "source": source, "header": header}
        self.overlay.setGeometry(self.rect())
        self.overlay.targets = self._drop_targets(keys, source)
        self.overlay.ghost = (" + ".join(self.title(k) for k in keys), self.mapFromGlobal(gpos))
        self.overlay.zone = None
        self.overlay.show()
        self.overlay.raise_()
        QApplication.instance().installEventFilter(self._esc)
        self.drag_move(gpos)

    def _drop_targets(self, keys, source):
        out = []
        for st in self.stacks(docked_only=True):
            if not st.isVisible():
                continue
            if st is source and set(st.keys) <= set(keys):
                continue          # dropping a whole group onto itself does nothing
            out.append((QRect(st.mapTo(self, QPoint(0, 0)), st.size()), False))
        out.append((QRect(self.center.mapTo(self, QPoint(0, 0)), self.center.size()), True))
        return out

    def _hit(self, gpos):
        p = self.mapFromGlobal(gpos)
        r = self.rect()
        if not r.contains(p):
            return Zone("float", label="Release to float as its own window")
        w, h = r.width(), r.height()
        edges = {"left": p.x(), "right": w - p.x(), "top": p.y(), "bottom": h - p.y()}
        side = min(edges, key=edges.get)
        if edges[side] < EDGE_PX:
            pv = {"left": QRect(0, 0, int(w * 0.24), h), "right": QRect(int(w * 0.76), 0, w - int(w * 0.76), h),
                  "top": QRect(0, 0, w, int(h * 0.3)), "bottom": QRect(0, int(h * 0.7), w, h - int(h * 0.7))}[side]
            return Zone("edge", side, None, r, pv, f"Dock along the {side} edge of the window")
        for rect, is_center in self.overlay.targets:
            if not rect.contains(p):
                continue
            fx = (p.x() - rect.x()) / max(1, rect.width())
            fy = (p.y() - rect.y()) / max(1, rect.height())
            d = {"left": fx, "right": 1 - fx, "top": fy, "bottom": 1 - fy}
            where = min(d, key=d.get)
            band = MAP_ZONE if is_center else ZONE
            if d[where] >= band:
                where = "center"
            if is_center:
                if where == "center":
                    return Zone("float", "float", self.center, rect, QRect(), "Release to float here")
                frac = 0.3
                target_name = "the map"
                kind, tgt = "center", self.center
            else:
                tgt = self._stack_at(rect)
                if tgt is None:
                    continue
                target_name = "“" + self.title(tgt.current()) + "”"
                kind = "stack"
                frac = 0.5
                if where == "center":
                    return Zone("stack", "center", tgt, rect, rect, f"Add as a tab next to {target_name}")
            pw, ph = int(rect.width() * frac), int(rect.height() * frac)
            pv = {"left": QRect(rect.x(), rect.y(), pw, rect.height()),
                  "right": QRect(rect.right() - pw + 1, rect.y(), pw, rect.height()),
                  "top": QRect(rect.x(), rect.y(), rect.width(), ph),
                  "bottom": QRect(rect.x(), rect.bottom() - ph + 1, rect.width(), ph)}[where]
            word = {"left": "left of", "right": "right of", "top": "above", "bottom": "below"}[where]
            return Zone(kind, where, tgt, rect, pv, f"Put it {word} {target_name}")
        return Zone("float", label="Release to float as its own window")

    def _stack_at(self, rect):
        for st in self.stacks(docked_only=True):
            if QRect(st.mapTo(self, QPoint(0, 0)), st.size()) == rect:
                return st
        return None

    def drag_move(self, gpos):
        if self._drag is None:
            return
        self.overlay.zone = self._hit(gpos)
        self.overlay.ghost = (self.overlay.ghost[0], self.mapFromGlobal(gpos))
        self.overlay.update()
        if self.center_overlay is not None:
            rect = QRect(self.center.mapTo(self, QPoint(0, 0)), self.center.size())
            self.center_overlay(self.overlay.render_region(rect))

    def drag_cancel(self):
        if self._drag is None:
            return
        hdr = self._drag.get("header")
        self._end_drag_ui()
        if hdr is not None:
            try:
                hdr.dragging = False
                hdr.setCursor(Qt.OpenHandCursor)
            except RuntimeError:
                pass

    def _end_drag_ui(self):
        QApplication.instance().removeEventFilter(self._esc)
        self.overlay.hide()
        self.overlay.zone = None
        self._drag = None
        if self.center_overlay is not None:
            self.center_overlay(None)

    def drag_end(self, gpos):
        if self._drag is None:
            return
        zone = self._hit(gpos)
        keys, source = self._drag["keys"], self._drag["source"]
        self._end_drag_ui()
        whole = set(source.keys) <= set(keys)
        if zone.kind == "float":
            if source.floating and whole:
                source.window().move(gpos - QPoint(40, 12))      # just move the floating window
                return
            size = QSize(max(280, source.width()), max(240, source.height()))
            for k in keys:
                self._take_key(k)
            self._make_float(keys, gpos, size)
        else:
            if zone.target is source and whole:
                return                                             # dropped onto itself
            if zone.target is source and zone.where == "center":
                return
            for k in keys:
                self._take_key(k)
            if zone.kind == "stack":
                try:
                    zone.target.isVisible()
                except RuntimeError:           # target vanished while tidying up
                    zone = Zone("edge", "right")
            self._insert(keys, zone)
        self._sync_visibility()
        self._sync_actions()
        self.layoutChanged.emit()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.overlay.isVisible():
            self.overlay.setGeometry(self.rect())

    # ---------------------------------------------------------------- state
    def _node_state(self, w):
        if w is self.center:
            return {"t": "center"}
        if isinstance(w, PanelStack):
            return {"t": "stack", "keys": list(w.keys), "cur": w.current()}
        if isinstance(w, QSplitter):
            sizes = w.sizes()
            saved = self._saved_sizes.get(id(w))
            if saved is not None and len(saved[1]) == w.count():
                sizes = saved[1]
            kids = [self._node_state(w.widget(i)) for i in range(w.count())]
            return {"t": "split", "o": "h" if w.orientation() == Qt.Horizontal else "v",
                    "sizes": list(sizes), "c": [k for k in kids if k]}
        return None

    def state(self) -> dict:
        return {"v": 1, "tree": self._node_state(self.root),
                "floating": [{"keys": list(f.stack.keys), "cur": f.stack.current(),
                              "geom": [f.x(), f.y(), f.width(), f.height()]} for f in self.floats],
                "closed": sorted(self.closed), "side_hidden": self.side_hidden, "locked": self.locked}

    def _clear(self):
        for f in list(self.floats):
            for k in list(f.stack.keys):
                f.stack.take(k)
            f._closing = True
            f.close()
            f.deleteLater()
        self.floats = []
        for st in self.stacks(docked_only=True):
            for k in list(st.keys):
                st.take(k)
        self._saved_sizes = {}

    def _build(self, node, used):
        t = node.get("t")
        if t == "center":
            if "center" in used:
                return None
            used.add("center")
            return self.center
        if t == "stack":
            keys = [k for k in node.get("keys", []) if k in self._panels and k not in used]
            if not keys:
                return None
            used.update(keys)
            st = self._new_stack(keys)
            if node.get("cur") in keys:
                st.set_current(node["cur"])
            return st
        if t == "split":
            sp = Split(Qt.Horizontal if node.get("o") == "h" else Qt.Vertical, self)
            sizes = []
            for child, size in zip(node.get("c", []), node.get("sizes", [])):
                w = self._build(child, used)
                if w is not None:
                    sp.addWidget(w)
                    sizes.append(int(size))
            if sp.count() == 0:
                sp.deleteLater()
                return None
            sp._pending_sizes = sizes
            return sp
        return None

    def _apply_sizes(self):
        for w in self._walk():
            if isinstance(w, QSplitter) and getattr(w, "_pending_sizes", None):
                if len(w._pending_sizes) == w.count():
                    w.setSizes(w._pending_sizes)
                w._pending_sizes = None

    def restore(self, state: dict) -> bool:
        try:
            if not state or state.get("v") != 1 or not state.get("tree"):
                return False
            self._clear()
            used = set()
            root = self._build(state["tree"], used)
            if root is None or "center" not in used:
                raise ValueError("layout without the map")
            if not isinstance(root, QSplitter):
                sp = Split(Qt.Horizontal, self)
                sp.addWidget(root)
                root = sp
            old = self.root
            self.root = root
            self._lay.replaceWidget(old, root)
            old.setParent(None)
            old.deleteLater()
            for fl in state.get("floating", []):
                keys = [k for k in fl.get("keys", []) if k in self._panels and k not in used]
                if keys:
                    used.update(keys)
                    fw = self._make_float(keys, geom=fl.get("geom"))
                    if fl.get("cur") in keys:
                        fw.stack.set_current(fl["cur"])
            self.closed = {k for k in self._panels if k not in used}
            # panels added in a newer version that the saved layout doesn't know about
            for k in self._panels:
                if k not in used and k not in state.get("closed", []) and self.group(k) == "side":
                    self.closed.discard(k)
                    self._place_home(k)
            self.locked = bool(state.get("locked", False))
            self.side_hidden = False
            self._sync_visibility()
            self._apply_sizes()
            if state.get("side_hidden"):
                self.set_side_hidden(True)
            self.overlay.raise_()
            self._sync_actions()
            self.layoutChanged.emit()
            return True
        except Exception as exc:
            print("layout restore failed:", exc)
            return False

    def apply_default(self, total_w=1500, total_h=900):
        side = [k for k in self._panels if self.group(k) == "side"]
        groups = [["products", "locations"], ["warnings", "cells", "inspector"], ["placefiles", "layers"]]
        groups = [[k for k in g if k in side] for g in groups]
        extra = [k for k in side if not any(k in g for g in groups)]
        if extra:
            groups[-1] += extra
        groups = [g for g in groups if g]
        col_w = 340
        state = {"v": 1, "tree": {"t": "split", "o": "h", "sizes": [max(400, total_w - col_w), col_w], "c": [
            {"t": "center"},
            {"t": "split", "o": "v", "sizes": [int(total_h * 0.5), int(total_h * 0.31), int(total_h * 0.19)][:len(groups)],
             "c": [{"t": "stack", "keys": g, "cur": g[0]} for g in groups]}]},
            "floating": [], "closed": [k for k in self._panels if self.group(k) != "side"]}
        for k in self._panels:
            self._panels[k].pop("home", None)
        self.restore(state)
