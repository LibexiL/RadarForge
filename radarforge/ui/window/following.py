"""Following a storm: the map keeps a chosen storm centred as new frames arrive, and changes radar when it
moves out of range (live data)."""
from __future__ import annotations

import math

from ...data.sites import nearest_site
from ...products.geometry import aeqd_forward
from ...services import follow
from ..panels.cells import storm_cells

HAND_OVER_KM = 200.0                 # beyond this from the radar, a nearer radar sees the storm better
CELL_PICK_KM = 15.0


class FollowMixin:
    """Following a storm."""

    def _init_follow(self):
        self._follow: follow.Target | None = None

    # ---------------------------------------------------------------- starting and stopping
    def following(self) -> bool:
        return self._follow is not None

    def follow_storm_at(self, x, y):
        """Follow the storm at a map position: the Level III cell nearest to it, or the spot itself."""
        frame = self.current_frame()
        if frame is None:
            self._status_msg("Load some radar data before following a storm")
            return
        cells = storm_cells(frame)[1]
        cell = follow.nearest_cell(cells, x, y, CELL_PICK_KM)
        motion = (float(self.settings["storm_motion_dir"]), float(self.settings["storm_motion_kts"]))
        if cell is not None:
            target = follow.Target(cell["x"], cell["y"], frame.time, cell.get("motion") or motion, cell["id"], frame.site)
        else:
            target = follow.Target(x, y, frame.time, motion, None, frame.site)
        self._begin_following(target)

    def follow_cell(self, cell):
        frame = self.current_frame()
        if frame is None:
            return
        motion = cell.get("motion") or (float(self.settings["storm_motion_dir"]), float(self.settings["storm_motion_kts"]))
        self._begin_following(follow.Target(cell["x"], cell["y"], frame.time, motion, cell["id"], frame.site))

    def _begin_following(self, target):
        self._follow = target
        self.follow_act.blockSignals(True)
        self.follow_act.setChecked(True)
        self.follow_act.blockSignals(False)
        self.view.set_view(target.x, target.y, max(self.view.scale, 2.0))
        self._update_l3_needs()                                # the storm table is what tells us where it went
        self._announce_following()
        self._status_msg("Following a storm – Tools → Follow a storm (or click the yellow label) stops it")

    def toggle_follow(self, on):
        if not on:
            self.stop_following()
            return
        frame = self.current_frame()
        cells = storm_cells(frame)[1] if frame is not None else []
        if cells:
            self.follow_cell(cells[0])                          # the most threatening cell
        else:
            x, y = self.view.cx, self.view.cy
            self.follow_storm_at(x, y)
            if self._follow is None:
                self.follow_act.blockSignals(True)
                self.follow_act.setChecked(False)
                self.follow_act.blockSignals(False)

    def stop_following(self):
        if self._follow is None:
            return
        self._follow = None
        self.follow_act.blockSignals(True)
        self.follow_act.setChecked(False)
        self.follow_act.blockSignals(False)
        self.follow_lbl.setVisible(False)
        self._update_l3_needs()
        self._status_msg("Stopped following the storm")

    # ---------------------------------------------------------------- each new frame
    def _follow_step(self, frame):
        t = self._follow
        if t is None or frame is None or getattr(frame, "time", None) is None:
            return
        cells = storm_cells(frame)[1]
        new, matched = follow.update(t, cells, frame.time, frame.site)
        self._follow = new
        self.view.set_view(new.x, new.y, self.view.scale)
        self._announce_following(matched is None)
        if self.data.mode == "live" and math.hypot(new.x, new.y) > HAND_OVER_KM:
            lat, lon = self.view.world_to_latlon(new.x, new.y)
            near = nearest_site(lat, lon)
            if near is not None and near.id != self.data.site_id:
                self.switch_site(near.id, keep_view=True)       # the storm's position stays; its cell id is matched afresh
                x, y = aeqd_forward(lat, lon, self.view.lat0, self.view.lon0)
                self._follow = follow.Target(float(x), float(y), new.time, new.motion, None, near.id, new.label)
                self._status_msg(f"Following the storm: handed over to {near.id}")

    def _announce_following(self, estimated=False):
        t = self._follow
        if t is None:
            return
        self.follow_lbl.setText(f"Following {follow.label(t)}" + (" (estimated)" if estimated else ""))
        self.follow_lbl.setToolTip("Click to stop following this storm")
        self.follow_lbl.setVisible(True)
