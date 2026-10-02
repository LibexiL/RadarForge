"""Bookmarks / shared views / workspaces, the command search and the briefing summary (no window needed)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from radarforge.products import catalog
from radarforge.services import briefing, views
from radarforge.services.commands import Command, score, search

PRODUCTS = {p.id for p in catalog.PRODUCTS}


def sample_view():
    return {"site": "ktlx", "time": "2013-05-20T19:46:55Z", "layout": 4, "panels": ["REF", "SRV", "CC", "AZSH"], "tilt": 0.5,
            "view": {"lat": 35.3, "lon": -97.3, "km_across": 120.0}, "overlays": {"warnings": True, "lightning": False},
            "storm_motion": [240, 30], "name": "Moore", "notes": "Hook echo"}


def test_clean_view_keeps_good_data_and_drops_the_rest():
    v = views.clean_view(sample_view(), PRODUCTS)
    assert v["site"] == "KTLX" and v["layout"] == 4 and v["panels"] == ["REF", "SRV", "CC", "AZSH"]
    assert v["time"] == "2013-05-20T19:46:55Z" and v["view"] == {"lat": 35.3, "lon": -97.3, "km_across": 120.0}
    bad = views.clean_view({"site": "<script>", "layout": 99, "panels": ["REF", "NOPE", 3], "tilt": "x",
                            "view": {"lat": 999, "lon": 0, "km_across": 5}, "overlays": {"evil": True, "satellite": 1},
                            "time": "not a time", "storm_motion": ["a", "b"]}, PRODUCTS)
    assert "site" not in bad and bad["layout"] == 6 and bad["panels"] == ["REF"] and "tilt" not in bad
    assert "view" not in bad and bad["overlays"] == {"satellite": True} and bad["time"] is None
    assert "storm_motion" not in bad
    with pytest.raises(ValueError):
        views.clean_view([1, 2])


def test_times_are_utc_and_none_means_live():
    assert views.parse_time("2026-10-02T17:00:00-05:00") == datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)
    assert views.parse_time(None) is None and views.parse_time("junk") is None
    assert views.default_bookmark_name({"site": "KTLX", "time": None}) == "KTLX (live)"
    assert views.default_bookmark_name({"site": "KTLX", "time": "2013-05-20T19:46:55Z"}) == "KTLX 2013-05-20 19:46Z"


def test_share_text_roundtrips_and_stays_short():
    v = views.clean_view(sample_view(), PRODUCTS)
    text = views.encode(v)
    assert text.startswith("RFV1:") and "\n" not in text and len(text) < 600
    assert views.decode(text) == v
    assert views.decode("  " + text + "\n") == v                                  # pasted with spaces around it
    assert views.decode(json.dumps(v)) == v                                       # plain JSON works too
    file_text = views.to_file_text(v, "1.8.0")
    assert views.decode(file_text) == v and json.loads(file_text)["radarforge_view"] == "1.8.0"
    for junk in ("", "hello", "RFV1:!!!!", "RFV1:" + "QUJD", "[1,2,3]", '{"layout": "x"}x'):
        with pytest.raises(ValueError):
            views.decode(junk)


def test_bookmark():
    bm = views.new_bookmark(views.clean_view(sample_view()), "  Moore 2013  ", "notes")
    assert bm["name"] == "Moore 2013" and bm["notes"] == "notes" and bm["created"].endswith("Z")


def test_builtin_workspaces_only_use_real_products_and_panels():
    side = {"products", "warnings", "locations", "cells", "inspector", "placefiles", "layers"}
    assert {"Briefing", "Tornado hunt", "Hail", "Flood"} <= set(views.BUILTIN_WORKSPACES)
    for name, ws in views.BUILTIN_WORKSPACES.items():
        clean = views.clean_workspace(ws, PRODUCTS)
        assert clean["panels"] == ws["panels"], name                              # nothing was dropped as unknown
        assert len(ws["panels"]) >= ws["layout"] and ws["layout"] in range(1, 7), name
        assert set(ws["side"]) <= side, name
        assert set(ws["overlays"]) <= set(views.OVERLAY_KEYS), name
        assert "site" not in clean and "time" not in clean and ws["note"], name


# ------------------------------------------------------------------------------------------------ command search
def cmds():
    return [Command("Switch to radar KTLX Oklahoma City", "Radar", order=1), Command("Export loop as MP4 video", "File › Export"),
            Command("Open archive from AWS", "File", shortcut="Ctrl+A"), Command("Storm track", "Tools", keywords="arrow eta"),
            Command("Satellite picture", "Layers"), Command("Lightning flashes (GLM)", "Layers", keywords="strikes")]


def test_search_ranks_substrings_and_word_starts_first():
    c = cmds()
    assert search(c, "mp4")[0].title.startswith("Export loop")
    assert search(c, "sat")[0].title == "Satellite picture"
    assert [x.title for x in search(c, "ktlx")] == ["Switch to radar KTLX Oklahoma City"]
    assert search(c, "strikes")[0].title.startswith("Lightning")                   # found through a keyword
    assert search(c, "zzzz") == []
    assert len(search(c, "", limit=3)) == 3


def test_search_handles_several_words_and_typos_in_order():
    c = cmds()
    assert search(c, "export mp4")[0].title.startswith("Export loop")
    assert search(c, "exp mp")[0].title.startswith("Export loop")
    assert search(c, "arch aws")[0].title == "Open archive from AWS"
    assert search(c, "stmtrk")[0].title == "Storm track"                           # letters in order
    assert score("mp4 zzz", c[1]) is None                                          # every word has to match
    far = Command("Go to Minot", "City · population 49,450")
    assert score("mp4", far) is None and score("mnt", far) is not None             # scattered letters must stay close


# ------------------------------------------------------------------------------------------------ briefing
def alert(event, variant, action="NEW"):
    return SimpleNamespace(event=event, variant=variant, action=action)


def test_briefing_lines():
    al = [alert("Tornado Warning", "TORP"), alert("Tornado Warning", "TOR"), alert("Severe Thunderstorm Warning", "SVR"),
          alert("Severe Thunderstorm Warning", "SVRD"), alert("Tornado Watch", "TOA"),
          alert("Flash Flood Warning", "FFW", "CAN")]                                   # cancelled: not counted
    lines = briefing.summary_lines(al, [{"number": 1234}, {"number": 1240}], "SPC outlook: Enhanced risk", 41,
                                   {"tornado": 2, "hail": 9, "funnel": 0})
    assert lines[0] == "Warnings in view: 2 tornado (1 PDS) · 2 severe (1 destructive) · 0 flash flood"
    assert lines[1] == "Watches in view: 1" and lines[2] == "Mesoscale discussions: MD 1234, MD 1240"
    assert lines[3:] == ["SPC outlook: Enhanced risk", "Lightning in view: 41 flashes", "Storm reports: 2 tornado · 9 hail"]
    quiet = briefing.summary_lines([])
    assert quiet == ["Warnings in view: 0 tornado · 0 severe · 0 flash flood"]
    assert briefing.stamp(datetime(2026, 10, 2, 22, 5, tzinfo=timezone.utc), "KTLX") == "KTLX · 2026-10-02 22:05Z"
