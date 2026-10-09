"""The details panel: what a click on the map lands on, and what the panel shows (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone

import numpy as np

from radarforge.features import warnings as W


def _alert(event, ring, variant=None, **kw):
    now = datetime.now(timezone.utc)
    a = W.Alert(event, [np.asarray(ring, float)], event.upper(), now - timedelta(minutes=5),
                now + timedelta(minutes=30), "Norman OK", "Cleveland, OK", [], event, variant, **kw)
    a.xy = [np.asarray(ring, float)]                 # already projected: km, as if painted
    return a


def _overlay(alerts):
    from radarforge.config import Settings
    o = W.WarningsOverlay.__new__(W.WarningsOverlay)
    o.settings = Settings.__new__(Settings)
    o.settings.data = {"overlays": {"warnings": True, "watches": True, "reports": True}, "warning_types": {},
                       "report_types": {}}
    o.alerts, o.reports, o.mode, o.frame_time = alerts, [], "live", None
    return o


def test_facts_and_motion():
    facts = W.live_facts({"maxHailSize": ["2.75"], "tornadoDetection": ["RADAR INDICATED"],
                          "maxWindGust": ["70 MPH"], "VTEC": ["x"]})
    assert ("Hail", "2.75 in") in facts and ("Tornado", "Radar indicated") in facts and ("Wind gusts", "70 mph") in facts
    assert W.storm_motion("2026-05-06T23:47:00-00:00...storm...229DEG...35KT...36.6,-97.31") == (229, 35)
    assert W.storm_motion("") is None
    js = {"report": {"text": "TORNADO WARNING..."}, "svs": [{"text": "SVS 1"}, {"text": "SVS 2"}]}
    txt = W.warning_text(js)
    assert txt.index("TORNADO WARNING") < txt.index("SVS 1") < txt.index("SVS 2")


def test_alerts_under_a_point():
    tor = _alert("Tornado Warning", [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], "TOR")
    watch = _alert("Tornado Watch", [(-50, -50), (50, -50), (50, 50), (-50, 50), (-50, -50)], "TOA")
    o = _overlay([watch, tor])
    assert o.alerts_at(5, 5, 0.5) == [tor, watch]                    # inside both: the warning first
    assert o.alerts_at(30, 30, 0.5) == [watch]
    assert o.alerts_at(5, 5, 0.5, inside=False) == []                # hover: only on an outline
    assert o.alerts_at(10.2, 5, 0.5, inside=False) == [tor]
    assert "click inside it" in o.hover(10.2, 5, 0.5)
    assert o.hover(5, 5, 0.5) is None
    o.settings.data["overlays"]["watches"] = False                   # hidden: not clickable either
    assert o.alerts_at(30, 30, 0.5) == []


def test_panel_titles_and_text():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from radarforge.ui import info_panel as IP
    a = _alert("Tornado Warning", [(0, 0), (1, 0), (1, 1), (0, 0)], "TORP",
               info=dict(description="A TORNADO...", instruction="TAKE COVER NOW!", headline="PDS",
                         facts=[("Tornado", "Observed")], motion=(250, 20)))
    assert IP.alert_title(a) == "Tornado Warning – PDS"
    assert IP.alert_title(_alert("Tornado Emergency", [(0, 0), (1, 0), (1, 1)], "TORE")) == "Tornado Emergency"
    assert IP.compass(250 + 180) == "ENE" and IP.compass(0) == "N"

    from PySide6.QtWidgets import QWidget
    main = QWidget()                                  # with just what the panel uses
    main.warnings = _overlay([a])
    main.warnings.selected_uid = None
    main.warnings.color = lambda al: (255, 0, 255)
    main.view = type("V", (), {"update": lambda self: None})()
    p = IP.InfoPanel(main)
    p.show_items([dict(kind="alert", obj=a), dict(kind="report", obj=dict(kind="hail", magnitude="1.75 INCH",
                                                                           where="Moore, OK", remark="Golf balls"))])
    body = p.body.toPlainText()
    assert "TAKE COVER NOW!" in body and "toward the ENE at 23 mph" in body and "Observed" in body
    assert p.list.isVisible() and main.warnings.selected_uid == a.uid       # the warning is highlighted
    p.list.setCurrentRow(1)
    assert "Golf balls" in p.body.toPlainText() and p.windowTitle() == "Hail report (1.75 INCH)"
    assert main.warnings.selected_uid is None
    p.close()
