"""Mouse tools: measure, storm track, cross section, 3-D and storm motion."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from PySide6.QtWidgets import QMessageBox

from ... import fmt
from ...tools import track
from ..dialogs import StormMotionDialog
from ..settings_dialog import SettingsDialog


class ToolsMixin:
    """Mouse tools: measure, storm track, cross section, 3-D and storm motion."""

    def set_tool(self, tool):
        self.view.set_tool(tool)
        for a in self.tool_group.actions():
            a.setChecked(a.data() == tool)
        if tool == "xsection":
            self._status_msg("Cross-section: drag a line across a storm (A → B)")
        elif tool == "box3d":
            self._status_msg("3-D: drag a box around the storm you want to render (Esc to cancel)")
        elif tool == "track":
            self._status_msg("Storm track: click a storm, then drag the yellow arrowhead to where it's going "
                             "(right-click for options, Esc twice to clear)")

    def _escape(self):
        """Esc: back to pan; pressed again, clears the measurement and the storm track."""
        if self.view.tool != "pan":
            self.set_tool("pan")
        else:
            self.view.clear_lines("measure")
            self.view.clear_track()

    def _track_start_time(self):
        f = self.current_frame()
        return f.time if f is not None and getattr(f, "time", None) else datetime.now(timezone.utc)

    def _track_default(self, x, y, minutes):
        """Where the storm motion (Storm motion…) takes a storm at x, y in `minutes`."""
        heading = math.radians((float(self.settings["storm_motion_dir"]) + 180.0) % 360.0)
        kts = float(self.settings["storm_motion_kts"]) or 30.0
        d = kts * 1.852 * minutes / 60.0
        return x + math.sin(heading) * d, y + math.cos(heading) * d

    def _track_changed(self):
        t = self.view.track
        if t is None:
            self.track_lbl.setVisible(False)
            return
        m = self.view.track_motion()
        if m is None:
            self.track_lbl.setVisible(False)
            return
        kmh, heading = m
        now = datetime.now(timezone.utc)
        from datetime import timedelta

        def when(mins):
            at = t["start"] + timedelta(minutes=mins)
            left = round((at - now).total_seconds() / 60)
            return f"{fmt.local_hm(at)} (" + (f"in {left} min" if left > 0 else "now" if left == 0 else "passed") + ")"
        parts = [f"Track {fmt.compass(heading)} {kmh / 1.852:.0f} kt"]
        loc = self.my_location.xy(self.view)
        if loc is not None:
            e = track.eta_at(t["a"], t["b"], self.view.track_minutes, loc, self.view.track_half_width)
            parts.append("You: " + (when(e) if e is not None else "not in its path"))
        parts += [f"{n} {when(mins)}" for n, mins, _x, _y in t["etas"][:3]]
        if not t["etas"]:
            parts.append(f"no towns in the next {self.view.track_minutes} min")
        self.track_lbl.setText("  ·  ".join(parts))
        self.track_lbl.setToolTip("Towns the storm reaches (within 6 miles of the track):\n" +
                                  ("\n".join(f"{n}: {when(mins)}" for n, mins, _x, _y in t["etas"]) or "none"))
        self.track_lbl.setVisible(True)

    def use_track_for_srv(self):
        m = self.view.track_motion()
        if m is None:
            return
        kmh, heading = m
        self.settings["storm_motion_dir"] = float(round((heading + 180) % 360))
        self.settings["storm_motion_kts"] = float(round(kmh / 1.852))
        self.settings.save()
        self._update_sm_label()
        self._panel_req.clear()
        self._show_frame()
        self.stateChanged.emit()
        self._status_msg(f"SRV storm motion set from the track: {self.settings['storm_motion_dir']:03.0f}° / "
                         f"{self.settings['storm_motion_kts']:.0f} kt")

    def set_track_minutes(self, m):
        self.settings["track_minutes"] = m
        self.view.set_track_minutes(m)

    def _box_drawn(self, x0, y0, x1, y1):
        self.set_tool("pan")
        self.open_3d()
        self.v3d_host.ensure().set_box(x0, y0, x1, y1)

    def _line_drawn(self, tool, x0, y0, x1, y1):
        if tool == "xsection":
            self.open_xsection()
            self.xs_win.set_line(x0, y0, x1, y1)

    def open_xsection(self):
        self.show_panel("xsection")

    def _xsection_closed(self):
        self.view.clear_lines("xsection")
        if self.view.tool == "xsection":
            self.set_tool("pan")

    def open_3d(self):
        self.show_panel("3d")

    def edit_storm_motion(self):
        d = StormMotionDialog(self.settings, self.l3ov.mean_storm_motion, self)
        if d.exec():
            self._update_sm_label()
            self._show_frame()
            self.stateChanged.emit()

    def _update_sm_label(self):
        self.sm_act.setText(f"SM {self.settings['storm_motion_dir']:03.0f}°/{self.settings['storm_motion_kts']:.0f}kt")
        self.sm_act.setToolTip("Storm motion used for SRV (click to edit)")

    def show_storm_table(self):
        f = self.current_frame()
        prod = f.l3.get("NST") if f else None
        if prod is None or not prod.text_pages:
            QMessageBox.information(self, "Storm table", "No Level III storm-structure table (NST) for this frame. "
                                    "Enable Overlays → Storm tracks to download it.")
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"NST {prod.time:%H:%M:%S}Z")
        box.setText("<pre>" + "\n\n".join(prod.text_pages[:4]) + "</pre>")
        box.exec()

    def open_placefiles(self):
        self.show_panel("placefiles")

    def open_settings(self, page=None):
        d = SettingsDialog(self.settings, self, page if isinstance(page, str) else None)
        if d.exec():
            self._palettes.clear()
            self._apply_view_settings()
            self.legend_act.setChecked(bool(self.settings["show_legend"]))
            self.link_act.setChecked(bool(self.settings["cursor_link"]))
            self.data._live_timer.setInterval(max(5, int(self.settings["live_poll_seconds"])) * 1000)
            from ...data.frames import VOLUMES
            VOLUMES.capacity = int(self.settings["volume_cache"])
            self.smooth_act.blockSignals(True)
            self.smooth_act.setChecked(bool(self.settings["gpu_smooth"]))
            self.smooth_act.blockSignals(False)
            for a, lv in zip(self.vf_group.actions(), (0, 1, 2)):
                a.setChecked(lv == int(self.settings["velocity_filter"]))
            self._panel_req.clear()
            self._show_frame()
            self.warnings_panel.refresh()          # warning colours may have changed
            self.view.update()
