"""Panels: layout, products, image jobs, the right-click menu and the cursor readout."""
from __future__ import annotations

import math
import numpy as np

from PySide6.QtWidgets import QMenu

from ... import fmt
from ...data.sites import nearest_site
from ...products import catalog
from ...products.geometry import beam_height, slant_range
from ..dialogs import McdDialog
from ..panels import CELL_CODES
from .constants import L3_TILT_ELEVS
from .jobs import _Bg, _ImageJob


class RenderingMixin:
    """Panels: layout, products, image jobs, the right-click menu and the cursor readout."""

    def set_active_panel(self, i):
        self.view.active_panel = max(0, min(i, len(self.view.panels) - 1))
        self.view.update()
        self.stateChanged.emit()

    def set_layout(self, n):
        self.settings["layout"] = n
        for a in self.layout_group.actions():
            a.setChecked(a.data() == n)
        self.view.set_layout(n, list(self.settings["panels"]))
        self._update_l3_needs()
        self._show_frame()

    def set_panel_product(self, i, pid):
        panels = list(self.settings["panels"])
        while len(panels) < 6:
            panels.append("REF")
        panels[i] = pid
        self.settings["panels"] = panels
        self.view.panels[i].product = pid
        self.view.panels[i].image = None
        self._update_l3_needs()
        self._show_frame()
        self._prefetch()

    def _update_l3_needs(self):
        ti = int(np.argmin(np.abs(np.array(L3_TILT_ELEVS) - self.tilt_elev)))
        codes = set()
        for p in self.view.panels:
            pd = catalog.get(p.product)
            if pd.kind in ("l3", "l3tilt"):
                codes.update(pd.l3_candidates(ti)[:1])
                if pd.kind == "l3tilt" and ti != 0:
                    codes.update(pd.l3_candidates(0)[:1])
        ov = self.settings["overlays"]
        for key, (code, _label) in catalog.L3_OVERLAYS.items():
            if ov.get(key):
                codes.add(code)
        cells = getattr(self, "cells_panel", None)
        if cells is not None and cells.isVisible():
            codes.update(CELL_CODES)
        self.data.set_l3_needed(codes)

    def _show_frame(self):
        frame = self.current_frame()
        self._shown_frame = frame
        self.l3ov.frame = frame
        if frame is not None:
            self.placefiles.frame_time = frame.time
            self.warnings.frame_time = frame.time
            if self.data.mode in ("archive", "local"):
                self.warnings.set_archive_time(frame.time)
            n = len(self.data.frames)
            live = " LIVE" if frame.live else ""
            self.time_label.setText(f" {frame.time:%Y-%m-%d %H:%M:%S}Z  [{self.frame_index + 1}/{n}]{live} ")
        else:
            self.time_label.setText(" no data ")
        for p in self.view.panels:
            self._request_panel(p, frame)
        self.update_timed_layers()
        self._fill_tilt_combo(frame)
        if self.xs_win is not None and self.xs_win.isVisible():
            self.xs_win.refresh()
        self.view.update()
        self.stateChanged.emit()

    def _request_panel(self, p, frame):
        pid = p.product
        pd = catalog.get(pid)
        p.palette = self.palette_for(pid)
        p.storage_units = pd.units
        if frame is None:
            p.image = None
            p.header = f"{self.data.site_id}  {pd.name}"
            p.message = "Waiting for data…" if self.data.mode == "live" else "No data loaded"
            return
        req = (frame.uid, frame.revision, pid, round(self.tilt_elev, 2), self.engine._sig(pid), id(p.palette))
        if self._panel_req.get(p.index) == req and p.image is not None:
            self._panel_done[p.index] = req
            return
        self._panel_req[p.index] = req
        engine = self.engine
        frames = list(self.data.frames)

        def job():
            ti = self._tilt_index(frame) if pd.tilted else 0
            img = engine.image(frame, pid, ti)
            src_frame = frame
            if img is None and pd.tilted and frame.live and frame.has_level2():
                # newest volume hasn't reached this tilt yet: show the previous volume's
                idx = frames.index(frame) if frame in frames else -1
                if idx > 0:
                    prev = frames[idx - 1]
                    img = engine.image(prev, pid, self._tilt_index(prev))
                    src_frame = prev
            if img is not None:
                img.gpu_values()            # texture prep off the UI thread
            tilts = engine.tilts(frame) if frame.has_level2() else []
            return {"req": req, "img": img, "frame": src_frame, "ti": ti,
                    "tilt_label": tilts[ti].label if tilts and pd.tilted and ti < len(tilts) else ""}
        self.pool.start(_ImageJob(job, self.relay, p.index))

    def _image_ready(self, idx, res):
        if idx >= len(self.view.panels):
            return
        p = self.view.panels[idx]
        if "error" in res:
            p.message = "Error: " + res["error"][:80]
            self._panel_done[idx] = self._panel_req.get(idx)
            self.view.update()
            return
        if self._panel_req.get(idx) != res["req"]:
            return
        self._panel_done[idx] = res["req"]
        pd = catalog.get(p.product)
        img = res["img"]
        p.image = img
        frame = res["frame"]
        site = frame.site if frame else self.data.site_id
        if img is not None:
            t = img.time or frame.time
            tl = img.label if img.source == "L3" else (res["tilt_label"] if pd.tilted else "")
            stale = "  (prev vol)" if frame is not self.current_frame() else ""
            p.header = f"{site}  {pd.name}  {tl}  {t:%H:%M:%S}Z{stale}"
            p.message = ""
        else:
            p.header = f"{site}  {pd.name}"
            if pd.kind in ("l3", "l3tilt"):
                ti = res.get("ti", 0)
                p.message = f"No Level III {pd.l3_code(ti)} for this time"
            elif not frame.has_level2():
                p.message = "No Level II volume for this frame"
            else:
                p.message = "Not available at this tilt"
        self._fill_tilt_combo(self.current_frame())
        self.view.update()
        self.stateChanged.emit()

    def panels_idle(self) -> bool:
        """True when every panel has finished drawing the frame it was last asked for."""
        for p in self.view.panels:
            req = self._panel_req.get(p.index)
            if req is not None and self._panel_done.get(p.index) != req:
                return False
        return True

    def _frame_ready(self, frame):
        for p in self.view.panels:
            pd = catalog.get(p.product)
            ti = 0
            if pd.tilted:
                if frame.has_level2():
                    t = self.engine._tilt_cache.get((frame.uid, frame.l2_rev))
                    if t is None:
                        return False
                    els = np.array([x.elevation for x in t])
                    ti = int(np.argmin(np.abs(els - self.tilt_elev))) if len(els) else 0
            if self.engine.cached(frame, p.product, ti) is None and (frame.has_level2() or frame.l3):
                return False
        return True

    def _prefetch(self):
        frames = list(self.data.frames)
        cur = self.current_frame()
        pids = [p.product for p in self.view.panels]
        engine = self.engine
        self._prefetch_gen += 1
        gen = self._prefetch_gen

        def work():
            for f in reversed(frames):
                if f is cur:
                    continue
                if gen != self._prefetch_gen or self.data.loading:
                    return                      # superseded, or new data is still arriving
                if self.data.frames and f not in self.data.frames:
                    return
                for pid in pids:
                    pd = catalog.get(pid)
                    ti = self._tilt_index(f) if pd.tilted else 0
                    engine.image(f, pid, ti)
        self.bg_pool.clear()
        self.bg_pool.start(_Bg(work))

    def _panel_menu(self, idx, gpos):
        menu = QMenu(self)
        cur = self.view.panels[idx].product
        for cat in catalog.CATEGORIES:
            sub = menu.addMenu(cat)
            for p in catalog.PRODUCTS:
                if p.category != cat:
                    continue
                a = sub.addAction(p.name)
                a.setCheckable(True)
                a.setChecked(p.id == cur)
                a.triggered.connect(lambda _=False, pid=p.id: self.set_panel_product(idx, pid))
        menu.addSeparator()
        a = menu.addAction("Load colour table for this product…")
        a.triggered.connect(lambda: self._load_pal_for(cur))
        a = menu.addAction("Default colour table")
        a.triggered.connect(lambda: self._reset_pal_for(cur))
        menu.addSeparator()
        if self.view.cursor_world is not None:
            x, y = self.view.cursor_world
            lat, lon = self.view.world_to_latlon(x, y)
            ns = nearest_site(lat, lon)
            if ns is not None and ns.id != self.data.site_id:
                a = menu.addAction(f"Switch to nearest radar ({ns.id} – {ns.place})")
                a.triggered.connect(lambda: self.switch_site(ns.id, keep_view=True))
            a = menu.addAction("Centre here")
            a.triggered.connect(lambda: self.view.set_view(x, y, self.view.scale))
            a = menu.addAction("Model sounding here (HRRR)…")
            a.triggered.connect(lambda: self.open_sounding_at(lat, lon))
            mcd = self.spc.mcd_at(lat, lon)
            if mcd is not None:
                a = menu.addAction(f"Read SPC Mesoscale Discussion {mcd['number']}…")
                a.triggered.connect(lambda: McdDialog(mcd, self).show())
            menu.addSeparator()
            under = self.my_location.at(x, y, 10.0 / self.view.scale)
            if under is not None:
                a = menu.addAction(f"Edit “{under.name}”…")
                a.triggered.connect(lambda: self.edit_location(under))
                if under is not self.book.primary():
                    a = menu.addAction(f"Make “{under.name}” my location")
                    a.triggered.connect(lambda: (self.book.make_primary(under.id), self.locations_changed()))
                a = menu.addAction(f"Remove “{under.name}”")
                a.triggered.connect(lambda: (self.book.remove(under.id), self.locations_changed()))
            a = menu.addAction("Add a location here…")
            a.triggered.connect(lambda: self.add_location_dialog(lat, lon))
            a = menu.addAction("Set my location here")
            a.triggered.connect(lambda: self.set_my_location(lat, lon))
        if self.my_location.latlon() is not None:
            a = menu.addAction("Remove my location")
            a.triggered.connect(lambda: self.set_my_location(None, None))
        if self.view.track is not None:
            tm = menu.addMenu("Storm track")
            a = tm.addAction("Use for SRV storm motion")
            a.triggered.connect(self.use_track_for_srv)
            a = tm.addAction("Reset to the storm motion")
            a.triggered.connect(lambda: self.view.set_track(self.view.track["a"]))
            lm = tm.addMenu("Track length")
            for mins in (30, 60, 90, 120):
                a = lm.addAction(f"{mins} minutes")
                a.setCheckable(True)
                a.setChecked(self.view.track_minutes == mins)
                a.triggered.connect(lambda _=False, mm=mins: self.set_track_minutes(mm))
            a = tm.addAction("Clear the storm track")
            a.triggered.connect(self.view.clear_track)
        menu.exec(gpos)

    def _cursor(self, x, y, panel):
        if panel < 0 or math.isnan(x):
            self.readout.setText("")
            for p in self.view.panels:
                p.readout = ""
            return
        lat, lon, dist, az = self.view.describe_point(x, y)
        du = self.settings["distance_units"]
        s_km = dist * fmt.UNIT_KM[du]
        parts = [f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'} {abs(lon):.4f}°{'W' if lon < 0 else 'E'}",
                 f"{dist:.1f} {du} @ {az:03.0f}°"]
        for p in self.view.panels:
            p.readout = ""
            img = p.image
            if img is None or p.palette is None:
                continue
            v = img.sample(az, s_km)
            pd = catalog.get(p.product)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                txt = "—"
            elif math.isinf(v):
                txt = "RF"
            else:
                disp = v * p.palette.data_scale(pd.units) + p.palette.offset
                if pd.categorical and p.palette.labels:
                    txt = p.palette.labels.get(float(round(disp)), f"{disp:.0f}")
                else:
                    dec = pd.decimals if p.palette.units.upper() not in ("KTS", "KT", "MPH") else 0
                    txt = f"{disp:.{dec}f} {p.palette.units}".strip()
            p.readout = txt
            if p.index == panel and img.elevation and not img.ground_range:
                h_ft = (beam_height(slant_range(s_km, img.elevation), img.elevation)) * 3280.84
                parts.append(f"beam {h_ft:,.0f} ft ARL")
        cur = self.view.panels[panel].readout if panel < len(self.view.panels) else ""
        if cur:
            parts.append(f"{catalog.get(self.view.panels[panel].product).short}: {cur}")
        sky = self.satellite.readout(x, y, self.view)
        if sky:
            parts.append(sky)
        self.readout.setText("   |   ".join(parts))
        if self.inspector_panel.isVisible():
            beam = next((p for p in parts if p.startswith("beam")), "")
            loc = (f"<b>{parts[0]}</b><br>{parts[1]} from {self.data.site_id}" +
                   (f"<br>{beam}" if beam else ""))
            values = [(f"{p.index + 1}. {catalog.get(p.product).name}", p.readout or "—") for p in self.view.panels]
            under = None
            tol = 8.0 / self.view.scale
            for hp in (self.l3ov, self.warnings, self.placefiles):
                try:
                    under = hp.hover(x, y, tol)
                except Exception:
                    under = None
                if under:
                    break
            self.cursorInfo.emit({"loc_html": loc, "values": values, "under": under})
