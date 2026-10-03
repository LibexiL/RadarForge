"""Storm tools on screen: automatic flags overlay, rotation history window and the radar guide."""
from __future__ import annotations

import math
import threading

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..features import stormtools as st
from ..products.geometry import beam_height, slant_range

FLAG_STYLE = {"rotation": (QColor(255, 60, 60), "ROT"), "tds": (QColor(255, 60, 255), "TDS?"),
              "hail": (QColor(80, 230, 255), "HAIL")}


class StormFlagsOverlay:
    """Flags raised by stormtools.detect() for the frame shown."""

    def __init__(self, settings):
        self.settings = settings
        self.flags: list = []
        self.key = None              # (frame uid, centre) the flags belong to
        self.follow_xy = None        # storm being followed (Tools → Follow storm)

    def enabled(self):
        return bool(self.settings["overlays"].get("storm_flags", False))

    def paint(self, painter, vt, panel, view):
        if self.follow_xy is not None:
            sx, sy = vt.to_screen(*self.follow_xy)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 180), 4))
            painter.drawEllipse(QPointF(sx, sy), 26, 26)
            painter.setPen(QPen(QColor(255, 210, 60), 2, Qt.DashLine))
            painter.drawEllipse(QPointF(sx, sy), 26, 26)
        if not self.enabled() or not self.flags:
            return
        x0, y0, x1, y1 = vt.world_bounds(pad=5)
        font = QFont(painter.font())
        font.setPixelSize(10)
        font.setBold(True)
        for f in sorted(self.flags, key=lambda f: f["score"]):          # most important drawn last (on top)
            if not (x0 <= f["x"] <= x1 and y0 <= f["y"] <= y1):
                continue
            kinds = f.get("kinds") or [f["kind"]]
            col = FLAG_STYLE.get(kinds[0], (QColor(255, 255, 255), "?"))[0]
            sx, sy = vt.to_screen(f["x"], f["y"])
            painter.setPen(QPen(QColor(0, 0, 0, 220), 3))
            painter.drawLine(QPointF(sx, sy), QPointF(sx, sy - 22))
            painter.setPen(QPen(QColor(240, 240, 240), 1.5))
            painter.drawLine(QPointF(sx, sy), QPointF(sx, sy - 22))
            tri = QPolygonF([QPointF(sx, sy - 22), QPointF(sx + 13, sy - 17.5), QPointF(sx, sy - 13)])
            painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
            painter.setBrush(col)
            painter.drawPolygon(tri)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(sx, sy), 3, 3)
            lx = sx + 15
            fm = QFontMetricsF(font)
            for k in kinds:
                kc, label = FLAG_STYLE.get(k, (QColor(255, 255, 255), "?"))
                view._halo_text(painter, lx, sy - 13, label, kc, font)
                lx += fm.horizontalAdvance(label) + 6

    def hover(self, x, y, tol):
        if not self.enabled():
            return None
        for f in self.flags:
            if math.hypot(f["x"] - x, f["y"] - y) < max(tol * 1.5, 1.5):
                return f["text"] + "\n(automatic flag – a hint to look closer, not a warning)"
        return None

    def caption(self):
        if not self.enabled():
            return None
        n = len(self.flags)
        return f"Storm flags: {n}" if n else None


class _Relay(QObject):
    row = Signal(object)
    done = Signal()


class RotationChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []           # (time, shear 1e-3/s | None, vrot kt | None, dist km)
        self.setMinimumHeight(260)

    def set_rows(self, rows):
        self.rows = sorted(rows, key=lambda r: r[0])
        self.update()

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.fillRect(self.rect(), QColor(18, 20, 24))
        r = QRectF(56, 14, self.width() - 56 - 56, self.height() - 14 - 34)
        qp.setPen(QPen(QColor(120, 120, 130), 1))
        qp.drawRect(r)
        f = QFont(self.font())
        f.setPointSizeF(8)
        qp.setFont(f)
        rows = self.rows
        if not rows:
            qp.drawText(r, Qt.AlignCenter, "Working through the loaded frames…")
            return
        t0, t1 = rows[0][0].timestamp(), rows[-1][0].timestamp()
        if t1 - t0 < 60:
            t0, t1 = t0 - 300, t1 + 300
        sh_max = max([20.0] + [x[1] * 1.15 for x in rows if x[1] is not None])
        vr_max = max([60.0] + [x[2] * 1.15 for x in rows if x[2] is not None])

        def px(t):
            return r.left() + (t.timestamp() - t0) / (t1 - t0) * r.width()

        def py(v, top):
            return r.bottom() - v / top * r.height()
        # guides
        y = py(10.0, sh_max)                                      # 0.010 /s: notable low-level rotation
        qp.setPen(QPen(QColor(255, 120, 120, 120), 1, Qt.DashLine))
        qp.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        for v in (30.0, 50.0):
            y = py(v, vr_max)
            qp.setPen(QPen(QColor(120, 200, 255, 110), 1, Qt.DotLine))
            qp.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            qp.drawText(QRectF(r.right() + 4, y - 7, 50, 14), Qt.AlignLeft | Qt.AlignVCenter, f"{v:.0f} kt")
        # axes labels
        qp.setPen(QColor(255, 120, 120))
        for k in range(0, 5):
            v = sh_max * k / 4
            qp.drawText(QRectF(0, py(v, sh_max) - 7, 52, 14), Qt.AlignRight | Qt.AlignVCenter, f"{v / 1000:.3f}")
        qp.setPen(QColor(150, 200, 255))
        qp.drawText(QRectF(r.right() - 160, r.top() + 2, 156, 14), Qt.AlignRight, "rotational velocity (kt)")
        qp.setPen(QColor(255, 140, 140))
        qp.drawText(QRectF(r.left() + 4, r.top() + 2, 200, 14), Qt.AlignLeft, "azimuthal shear (/s)")
        qp.setPen(QColor(170, 170, 180))
        n = min(6, len(rows))
        for k in range(n):
            t = rows[round(k * (len(rows) - 1) / max(n - 1, 1))][0]
            x = px(t)
            qp.drawText(QRectF(x - 30, r.bottom() + 4, 60, 14), Qt.AlignCenter, f"{t:%H:%MZ}")
        for idx, col, top in ((1, QColor(255, 90, 90), sh_max), (2, QColor(110, 180, 255), vr_max)):
            pts = [QPointF(px(row[0]), py(row[idx], top)) for row in rows if row[idx] is not None]
            if not pts:
                continue
            path = QPainterPath(pts[0])
            for p in pts[1:]:
                path.lineTo(p)
            qp.setPen(QPen(col, 2.2))
            qp.setBrush(Qt.NoBrush)
            qp.drawPath(path)
            qp.setBrush(col)
            for p in pts:
                qp.drawEllipse(p, 3, 3)


class RotationDialog(QDialog):
    """Rotation strength of one storm through the loaded frames (lowest tilt). The storm is followed with
    the storm motion and the strongest shear within 6 km is used each frame."""

    def __init__(self, main, x, y):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Rotation history")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.chart = RotationChart()
        lay.addWidget(self.chart, 2)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Time", "Az. shear (/s)", "Rot. velocity (kt)", "Range (km)", "Beam height (ft)"])
        self.table.setRootIsDecorated(False)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        copy = QPushButton("Copy as CSV")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.rows = []
        self._stop = False
        self.relay = _Relay()
        self.relay.row.connect(self._add_row)
        self.relay.done.connect(self._done)
        frames = list(main.data.frames)
        cur = main.current_frame()
        t_ref = getattr(cur, "time", None)
        s = main.settings
        dx, dy = st.motion_xy(float(s["storm_motion_dir"]), float(s["storm_motion_kts"]))
        self.info.setText(f"Following the storm with the storm motion ({float(s['storm_motion_dir']):03.0f}° / "
                          f"{float(s['storm_motion_kts']):.0f} kt) through {len(frames)} frame{'s' if len(frames) != 1 else ''}, "
                          "lowest tilt. "
                          "Rotational velocity is half the difference between the strongest outbound and inbound "
                          "velocity within 6 km. Values are approximate – range and beam height matter.")
        engine = main.engine

        def work():
            for f in frames:
                if self._stop:
                    break
                ft = getattr(f, "time", None)
                if ft is None or t_ref is None:
                    continue
                mins = (ft - t_ref).total_seconds() / 60.0
                px, py = x + dx * mins, y + dy * mins
                try:
                    azsh = engine.image(f, "AZSH", 0)
                except Exception:
                    azsh = None
                try:
                    vel = engine.image(f, "VEL", 0)
                except Exception:
                    vel = None
                res = st.rotation_point(azsh, vel, px, py, 6.0)
                if res is None:
                    continue
                sh, vr, rx, ry = res
                src = azsh if azsh is not None else vel
                elev = src.elevation if src is not None else 0.5
                s_km = math.hypot(rx, ry)
                h_ft = float(beam_height(slant_range(s_km, elev), elev)) * 3280.84
                self.relay.row.emit((ft, sh, vr, s_km, h_ft))
            self.relay.done.emit()
        threading.Thread(target=work, daemon=True).start()

    def _add_row(self, r):
        self.rows.append(r)
        self.chart.set_rows([(a, b, c, d) for a, b, c, d, _e in self.rows])
        t, sh, vr, d, h = r
        QTreeWidgetItem(self.table, [f"{t:%H:%M:%SZ}", "–" if sh is None else f"{sh / 1000:.4f}",
                                     "–" if vr is None else f"{vr:.0f}", f"{d:.0f}", f"{h:,.0f}"])
        self.table.sortItems(0, Qt.AscendingOrder)

    def _done(self):
        if not self.rows:
            self.chart.set_rows([])
            self.info.setText(self.info.text() + "\n\nNo velocity data found near that point in the loaded frames.")

    def _copy(self):
        lines = ["time_utc,azimuthal_shear_per_s,rotational_velocity_kt,range_km,beam_height_ft"]
        for t, sh, vr, d, h in sorted(self.rows, key=lambda r: r[0]):
            lines.append(f"{t:%Y-%m-%dT%H:%M:%SZ},{'' if sh is None else f'{sh / 1000:.5f}'},"
                         f"{'' if vr is None else f'{vr:.1f}'},{d:.1f},{h:.0f}")
        QGuiApplication.clipboard().setText("\n".join(lines))

    def done(self, r):
        self._stop = True
        super().done(r)


GUIDE_HTML = """
<h2>Reading radar: a quick guide</h2>
<p>Turn on <b>Learn mode</b> (Help menu) and the Inspector panel explains the values under your mouse in plain
words. This page is the cheat sheet.</p>
<h3>The base products</h3>
<table cellpadding='4' cellspacing='0' border='1' style='border-color:#445'>
<tr><th align='left'>Product</th><th align='left'>What it measures</th><th align='left'>Typical values</th></tr>
<tr><td><b>Reflectivity</b> (dBZ)</td><td>How much energy comes back – size and number of targets</td>
<td>20–30 light rain · 40–50 heavy rain · 55–65 hail possible · 65+ large hail</td></tr>
<tr><td><b>Velocity</b> (kt)</td><td>Motion toward (green) or away from (red) the radar, along the beam only</td>
<td>Bright green beside bright red = rotation or a sharp wind shift. Velocity can "fold": a sudden jump from
max green to max red in a smooth area is aliasing, not rotation</td></tr>
<tr><td><b>Storm-relative velocity</b></td><td>Velocity minus the storm's motion</td>
<td>Makes rotation inside a fast-moving storm easier to see. Set the storm motion first (toolbar "SM", the
track tool, or a model sounding)</td></tr>
</table>
<h3>Dual-polarisation products</h3>
<table cellpadding='4' cellspacing='0' border='1' style='border-color:#445'>
<tr><th align='left'>Product</th><th align='left'>What it tells you</th><th align='left'>Rain</th>
<th align='left'>Hail</th><th align='left'>Snow</th><th align='left'>Debris / biology / clutter</th></tr>
<tr><td><b>ZDR</b> – differential reflectivity (dB)</td><td>Shape: wide-and-flat is positive, round is near 0</td>
<td>+1 to +4 (bigger drops, higher)</td><td>−0.5 to +1</td><td>0 to +1 (wet snow higher)</td>
<td>Birds and insects very high (+4 to +8); debris around 0, noisy</td></tr>
<tr><td><b>CC</b> – correlation coefficient</td><td>How alike the targets are</td><td>0.97–1.00</td>
<td>0.85–0.95</td><td>0.95–1.00 (melting layer 0.85–0.95)</td><td>Below 0.80 – a low-CC blob in a hook with
strong rotation is a <b>tornado debris signature (TDS)</b></td></tr>
<tr><td><b>KDP</b> – specific differential phase (°/km)</td><td>Amount of liquid water, not fooled by hail</td>
<td>0.5–3+ (higher = heavier rain)</td><td>Near 0 for dry hail; high with melting hail</td><td>~0</td><td>Noisy</td></tr>
</table>
<h3>Storm signatures to look for</h3>
<ul>
<li><b>Hook echo</b> on reflectivity – a curl on the storm's back side where a mesocyclone wraps rain around.</li>
<li><b>Velocity couplet</b> – tight inbound/outbound pair. Rotational velocity (half the difference) of 30+ kt
is notable, 50+ kt strong; closer to the radar it means more.</li>
<li><b>TDS</b> – CC under about 0.8 with reflectivity 35+ dBZ right on a velocity couplet: debris in the air,
usually a tornado on the ground.</li>
<li><b>Hail core</b> – 60+ dBZ with ZDR near 0 and CC 0.85–0.95. A "hail spike" (a radial streak of echo
behind the core) means very large hail.</li>
<li><b>ZDR arc</b> – high ZDR along the storm's inflow edge: big drops sorted by strong low-level shear,
often seen in tornadic supercells.</li>
<li><b>Bow echo / rear-inflow notch</b> – a line bulging forward with a weak-echo notch behind it: damaging winds.</li>
</ul>
<h3>Things that fool people</h3>
<ul><li>The beam rises with distance (roughly 12,000 ft up at 120 miles on the lowest tilt) – far storms are seen
high up.</li>
<li>Ground clutter and wind farms: stationary, low CC, odd velocities.</li>
<li>Birds at sunrise make expanding rings; insects make clear-air echo with very high ZDR.</li>
<li>The melting layer makes a ring of lower CC and higher reflectivity ("bright band").</li></ul>
<p><b>Automatic storm flags</b> (Tools menu) mark rotation (ROT), possible debris (TDS?) and hail cores (HAIL) on
the lowest tilt using these same rules. They are hints to look closer – always check warnings, the velocity
couplet and reports.</p>
"""


class GuideDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Radar & dual-pol guide")
        self.resize(820, 680)
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(GUIDE_HTML)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def flags_for_frame(engine, frame):
    """Run the flag rules on a frame's lowest tilt (background thread)."""
    def img(pid):
        try:
            return engine.image(frame, pid, 0)
        except Exception:
            return None
    ref = img("REF")
    if ref is None:
        return []
    return st.detect(ref, img("AZSH"), img("CC"), img("ZDR"))

