"""The Severe and Winter side panels: one place for everything about that kind of weather – a one-click setup,
the radar layers it needs, and buttons that open its model maps."""
from __future__ import annotations

from .quick_panel import ChipPage, _chip, _setup_button


def _caption(lay, text):
    from PySide6.QtWidgets import QLabel
    cap = QLabel(text)
    cap.setProperty("role", "section")
    f = cap.font()
    f.setBold(True)
    cap.setFont(f)
    lay.addWidget(cap)


class SeverePanel(ChipPage):
    """Severe weather: warnings and watches, storm reports and chasers, the SPC, storm tools and severe models."""

    def build(self, lay):
        m = self.main
        ov = m.overlay_acts
        b = _setup_button(m, "severe", "Set up the map for severe weather")
        b.setToolTip("Turns on warnings, watches, storm reports, the SPC outlook and discussions, and storm tracks. "
                     "Change any switch below afterwards.")
        lay.addWidget(b)
        lay.addSpacing(2)

        s = self.section("sev_warn", "Warnings & watches")
        s.chip(ov.get("warnings"), "Warnings", "NWS warnings on the map")
        s.chip(ov.get("watches"), "Watches", "Tornado and severe thunderstorm watches")
        for key, label, tip in (("TOR", "Tornado", "Tornado warnings and emergencies"),
                                ("SVR", "Severe t-storm", "Severe thunderstorm warnings"),
                                ("FFW", "Flash flood", "Flash flood warnings and emergencies"),
                                ("OTH", "Other", "Extreme wind, marine, snow squall, dust storm, special statements")):
            s.chip(m.wtype_acts.get(key), label, tip)
        s.chip(m.warn_loc_act, "Alert me", "Pop up a new warning that covers your location (live)")

        s = self.section("sev_reports", "Storm reports & chasers")
        s.chip(ov.get("reports"), "Storm reports", "Tornado, hail, wind and flood reports")
        s.chip(ov.get("chasers"), "Storm chasers", "Spotter Network chasers (click one for their details)")
        s.chip(m.sn_rep_act, "Spotter reports", "Add Spotter Network reports to the NWS ones (live)")
        s.segment("Last", m.rep_hours_group.actions(), ["1h", "3h", "6h", "12h", "24h"])

        s = self.section("sev_spc", "Storm Prediction Center")
        s.chip(ov.get("spc_outlook"), "Severe outlook", "The convective outlook (marginal … high risk)")
        s.chip(ov.get("spc_mcd"), "Discussions", "Mesoscale discussions (they often come before a watch)")
        s.segment("Outlook", m.spc_day_acts, ["Day 1", "Day 2", "Day 3"])
        self._spc_box = s.segment("Product", m.spc_product_acts, ["Risk", "Tornado", "Wind", "Hail", "Any"])
        self.sync_spc()

        s = self.section("sev_tools", "Storm tools on the radar")
        s.chip(ov.get("storm_tracks"), "Storm tracks", "Level III storm cell tracks and forecast positions (NST)")
        s.chip(ov.get("hail"), "Hail markers", "Level III hail index (NHI)")
        s.chip(ov.get("storm_flags"), "Storm flags", "Automatic flags for rotation, possible debris and hail")
        # MRMS swaths: on / off chips (one MRMS product is shown at a time; Layers has the full choice)
        self._mrms_chips = {}
        for key, label, tip in (("rot60", "Rotation tracks", "MRMS rotation tracks over the last hour (a strong, "
                                                             "long track is a likely tornado path)"),
                                ("mesh60", "Hail swath", "MRMS maximum hail size (MESH) over the last hour")):
            b = _chip(label, tip)
            b.clicked.connect(lambda _=False, k=key: self._mrms_clicked(k))
            s._place(b)
            self._mrms_chips[key] = b
        act = m.overlay_acts.get("mrms")
        if act is not None:
            act.toggled.connect(lambda _on: self.sync_mrms())
        self.sync_mrms()

        s = self.section("sev_models", "Severe model maps")
        self.model_buttons(s, [
            ("Severe set: 4 maps", ["cape_ml", "srh01", "shear06", "stp"], "hrrr", None,
             "HRRR mixed-layer CAPE, 0–1 km helicity, 0–6 km shear and the significant tornado parameter, side by side"),
            ("Storm-scale set", ["refc", "uh25", "td2m", "scp"], "hrrr", None,
             "HRRR simulated radar, updraft helicity, dewpoint and the supercell composite"),
        ], [
            ("refc", "Sim. radar", "hrrr", None), ("cape_ml", "MLCAPE", None, None), ("cape_sb", "SBCAPE", None, None),
            ("stp", "STP", None, None), ("scp", "SCP", None, None), ("srh01", "0–1 km SRH", None, None),
            ("srh03", "0–3 km SRH", None, None), ("shear06", "0–6 km shear", None, None),
            ("uh25", "Updraft hel.", "hrrr", None), ("lapse75", "Lapse rates", None, None),
            ("td2m", "Dewpoint", None, None), ("cin_sb", "CIN", None, None),
        ])
        s.header.setToolTip("The buttons open the Models view (F6 switches back). The sets use several panels; "
                            "single maps use the model you last picked.")

    def sync_spc(self):
        """Grey out the outlook products the shown day doesn't have (day 3: risk and any severe only)."""
        from PySide6.QtWidgets import QToolButton
        box = getattr(self, "_spc_box", None)
        if box is None:
            return
        for b, a in zip(box.findChildren(QToolButton), self.main.spc_product_acts):
            b.setEnabled(a.isEnabled())
            b.setChecked(a.isChecked())

    # ---------------------------------------------------------------- MRMS chips
    def _mrms_clicked(self, key):
        m = self.main
        on = bool(m.settings["overlays"].get("mrms")) and m.settings["mrms_product"] == key
        if on:
            m.overlay_acts["mrms"].setChecked(False)          # a second click hides it
        else:
            m.set_mrms_product(key)
            m._status_msg(f"Loading MRMS {'rotation tracks' if key.startswith('rot') else 'hail swath'} for the "
                          "last hour…")
        self.sync_mrms()

    def sync_mrms(self):
        m = self.main
        on = bool(m.settings["overlays"].get("mrms"))
        cur = m.settings["mrms_product"]
        for key, b in getattr(self, "_mrms_chips", {}).items():
            b.setChecked(on and cur == key)


class WinterPanel(ChipPage):
    """Winter weather: precipitation type, the freezing line, snowfall, winter warnings and winter models."""

    def build(self, lay):
        m = self.main
        ov = m.overlay_acts
        b = _setup_button(m, "winter", "Set up the map for winter weather")
        b.setToolTip("Turns on precipitation type in panel 1, the freezing line, winter warnings and snow and ice "
                     "reports. Change any switch below afterwards.")
        lay.addWidget(b)
        lay.addSpacing(2)

        s = self.section("win_radar", "On the radar")
        s.chip(m.ptype_act, "Precip type", "Panel 1 shows reflectivity coloured by rain, snow, sleet and freezing "
                                           "rain (HRRR analysis for each scan's hour)")
        s.chip(m.winter_pal_act, "Winter colours", "A reflectivity colour table that spreads light snow over more "
                                                   "colours")
        s.chip(ov.get("melting_layer"), "Melting layer", "Where snow melts into rain aloft (Level III N0M)")

        s = self.section("win_snow", "Snow & ice")
        s.chip(ov.get("freezing_line"), "Freezing line", "The 32 °F line at the surface (HRRR)")
        s.chip(ov.get("snowfall"), "Snowfall totals", "NOAA's snowfall analysis (NOHRSC)")
        s.segment("Snowfall", m.snow_hours_acts, ["24 h", "48 h", "72 h"])

        s = self.section("win_alerts", "Warnings & reports")
        s.chip(m.wtype_acts.get("WIN"), "Winter warnings", "Blizzard, winter storm, ice storm, lake effect, cold and "
                                                           "frost alerts (shaded counties)")
        s.chip(m.rep_type_acts.get("winter"), "Snow reports", "Snow, sleet and ice reports, with snow totals")
        s.chip(ov.get("spc_mcd"), "Discussions", "SPC mesoscale discussions cover heavy snow and freezing rain too")

        s = self.section("win_models", "Winter model maps")
        self.model_buttons(s, [
            ("Winter set: 4 maps", ["ptype", "snow10", "frzr", "t2m"], None, None,
             "Precipitation type, snowfall (10:1), freezing rain and 2 m temperature, side by side"),
            ("Snow set", ["snow10", "asnow", "snod", "t850"], None, None,
             "Snowfall at 10:1, the model's own snowfall, snow depth and 850 mb temperature"),
        ], [
            ("ptype", "P-type", None, None), ("snow10", "Snow 10:1", None, None), ("asnow", "Model snow", None, None),
            ("frzr", "Freezing rain", None, None), ("t2m", "2 m temp", None, None), ("t850", "850 mb temp", None, None),
            ("snod", "Snow depth", None, None), ("qpf", "Total precip", None, None), ("mslp", "Radar + MSLP", None, None),
        ])
        s.header.setToolTip("The buttons open the Models view (F6 switches back). The NAM goes out 60 hours and "
                            "the GFS 16 days for storms further ahead (pick the model in the Models panel).")
