from radarforge.products import catalog
from radarforge.services import learn, views

PRODUCT_IDS = {p.id for p in catalog.PRODUCTS}


def test_every_event_is_complete_and_unique():
    ids = [e["id"] for e in learn.EVENTS]
    assert len(ids) == len(set(ids))
    for e in learn.EVENTS:
        assert e["kind"] in learn.KINDS and e["title"] and e["about"] and e["steps"]
        assert len(e["site"]) == 4


def test_every_step_is_a_valid_view():
    for e in learn.EVENTS:
        for i, step in enumerate(e["steps"]):
            assert step["title"] and step["text"]
            v = learn.step_view(e, i, PRODUCT_IDS)
            assert v["site"] == e["site"]
            assert v["time"] and "view" in v and v["panels"], (e["id"], i)
            assert len(v["panels"]) == len(step["panels"]), "unknown product id in " + e["id"]
            assert v["layout"] == len(v["panels"])


def test_steps_fall_within_a_short_window():
    for e in learn.EVENTS:
        first, last = learn.event_time_range(e)
        assert (last - first).total_seconds() <= 3 * 3600


def test_round_trips_through_sharing():
    v = learn.step_view(learn.find("moore-2013"), 1)
    assert views.decode(views.encode(v)) == v
    assert learn.find("nope") is None
