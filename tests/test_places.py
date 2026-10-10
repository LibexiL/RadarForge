"""Place search: the built-in town list, typed coordinates and OpenStreetMap answers (no network)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import numpy as np

from radarforge.features import places

MAPS = Path(__file__).resolve().parent.parent / "radarforge" / "assets" / "maps.npz"


def test_typed_text():
    assert places.parse_coords("35.22, -97.44") == (35.22, -97.44)
    assert places.parse_coords("35.2 -97.4") == (35.2, -97.4)
    assert places.parse_coords("moore") is None and places.parse_coords("95, 10") is None
    assert places.split_state("Springfield, MO") == ("springfield", "MO")
    assert places.split_state("springfield missouri") == ("springfield", "MO")
    assert places.split_state("Norman OK") == ("norman", "OK")
    assert places.split_state("New York") == ("new york", None)


def test_town_search():
    lp = places.LocalPlaces(dict(np.load(MAPS)))
    r = lp.search("moore")
    assert r[0]["label"] == "Moore, OK" and r[0]["kind"] == "Town"
    spr = [p["label"] for p in lp.search("springfield")]
    assert {"Springfield, MO", "Springfield, IL"} <= set(spr)
    assert [p["label"] for p in lp.search("Springfield, MO")] == ["Springfield, MO"]
    assert lp.state_at(35.47, -97.52) == "OK" and lp.state_at(39.74, -104.99) == "CO"
    assert lp.state_at(48.0, -40.0) == ""                       # the ocean
    assert lp.search("x") == []


def test_osm_answers():
    js = [{"lat": "35.3395", "lon": "-97.4512", "category": "highway", "type": "motorway", "addresstype": "road",
           "name": "I 35", "display_name": "I 35, Moore, Cleveland County, Oklahoma, 73160, United States",
           "boundingbox": ["35.30", "35.38", "-97.46", "-97.44"],
           "geojson": {"type": "MultiLineString", "coordinates": [[[-97.45, 35.30], [-97.45, 35.34]],
                                                                  [[-97.45, 35.34], [-97.46, 35.38]]]}},
          {"lat": "36.15", "lon": "-95.99", "class": "place", "type": "city", "name": "Tulsa",
           "display_name": "Tulsa, Tulsa County, Oklahoma, United States", "boundingbox": ["35.9", "36.3", "-96.1", "-95.7"],
           "geojson": {"type": "Polygon", "coordinates": [[[-96.1, 35.9], [-95.7, 35.9], [-95.7, 36.3], [-96.1, 35.9]]]}},
          {"lat": "bad"}]
    r = places.parse_nominatim(js)
    assert len(r) == 2
    assert r[0]["label"] == "I 35, Moore, Cleveland County, Oklahoma" and r[0]["kind"] == "Interstate / freeway"
    assert len(r[0]["geom"]) == 2 and r[0]["bbox"] == (35.30, 35.38, -97.46, -97.44)
    assert r[1]["kind"] == "City" and len(r[1]["geom"]) == 1
    assert places.parse_nominatim({"error": "x"}) == []
