"""Learn mode: famous storms from the public radar archive, each a few steps that say what to look at and why.

An event is a plain dict so it can be checked, tested and shown without Qt. Every step is a *view* (see
services.views): the radar, the time, the panels and the zoom to go to. The archive is Level II only for the older
events, so the steps use base moments and the products RadarForge derives from them.
"""
from __future__ import annotations

from . import views

EVENTS: list[dict] = [
    {
        "id": "moore-2013", "title": "Moore, Oklahoma tornado", "date": "20 May 2013", "site": "KTLX",
        "kind": "Tornado", "level": "Beginner",
        "about": "A violent tornado crossed Moore in the afternoon. The radar shows the whole life cycle of a "
                 "classic supercell: the hook echo, the rotation and the debris the tornado threw into the air.",
        "steps": [
            {"title": "The supercell", "time": "2013-05-20T19:45:00Z", "layout": 1, "panels": ["REF"],
             "view": {"lat": 35.25, "lon": -97.55, "km_across": 120},
             "text": "Reflectivity shows where the rain and hail are. Look for the isolated, intense storm west of "
                     "Moore with a bulge on its south-west side: the start of a hook."},
            {"title": "Rotation in velocity", "time": "2013-05-20T19:56:00Z", "layout": 2, "panels": ["REF", "SRV"],
             "view": {"lat": 35.32, "lon": -97.45, "km_across": 30},
             "text": "Storm relative velocity: green is toward the radar, red away. A bright pair side by side is a "
                     "couplet, air turning around a point. This one is tight and strong."},
            {"title": "Debris ball", "time": "2013-05-20T20:07:00Z", "layout": 4, "panels": ["REF", "SRV", "CC", "AZSH"],
             "view": {"lat": 35.33, "lon": -97.45, "km_across": 20},
             "text": "Correlation coefficient drops where the radar sees things that are not rain: here pieces of "
                     "buildings. A low-CC ball at the tip of the hook, under the couplet, is a tornado debris "
                     "signature, a confirmation that the tornado is on the ground."},
        ],
    },
    {
        "id": "el-reno-2013", "title": "El Reno, Oklahoma: the widest tornado", "date": "31 May 2013", "site": "KTLX",
        "kind": "Tornado", "level": "Intermediate",
        "about": "The widest tornado on record. It grew very quickly and changed direction, which is why it caught "
                 "experienced storm chasers off guard. A good case for watching the trend, not just a snapshot.",
        "steps": [
            {"title": "A fast-growing hook", "time": "2013-05-31T23:15:00Z", "layout": 2, "panels": ["REF", "SRV"],
             "view": {"lat": 35.55, "lon": -98.05, "km_across": 50},
             "text": "Compare the two panels a few volumes apart (use the right arrow). The hook gets bigger and the "
                     "rotation wider within minutes."},
            {"title": "Strong rotation, wide circulation", "time": "2013-05-31T23:28:00Z", "layout": 4,
             "panels": ["REF", "SRV", "CC", "AZSH"],
             "view": {"lat": 35.6, "lon": -97.95, "km_across": 35},
             "text": "The rotation signature is broad. The wider it is, the wider the damage path can be, so a "
                     "large couplet deserves respect even when it is not very tight."},
        ],
    },
    {
        "id": "joplin-2011", "title": "Joplin, Missouri tornado", "date": "22 May 2011", "site": "KSGF",
        "kind": "Tornado", "level": "Intermediate",
        "about": "A violent tornado hit Joplin in the evening. The radar is far away (about 100 km), which "
                 "shows the limit of what a single radar can resolve at a distance.",
        "steps": [
            {"title": "A hook near the limit", "time": "2011-05-22T22:40:00Z", "layout": 2, "panels": ["REF", "SRV"],
             "view": {"lat": 37.1, "lon": -94.5, "km_across": 60},
             "text": "The beam is high above the ground this far from the radar, so it sees the storm above the "
                     "tornado. The hook and the couplet are still there, just coarser than near a radar."},
            {"title": "Debris", "time": "2011-05-22T22:45:00Z", "layout": 2, "panels": ["REF", "CC"],
             "view": {"lat": 37.08, "lon": -94.45, "km_across": 25},
             "text": "Low correlation coefficient over the city: debris lofted to the height of the beam."},
        ],
    },
    {
        "id": "tuscaloosa-2011", "title": "Tuscaloosa to Birmingham, Alabama", "date": "27 April 2011", "site": "KBMX",
        "kind": "Tornado", "level": "Intermediate",
        "about": "One of the largest tornado outbreaks on record. A long-track tornado stayed on the ground for "
                 "about 80 miles. Scroll the loop and follow the storm across the state.",
        "steps": [
            {"title": "A long-lived supercell", "time": "2011-04-27T22:10:00Z", "layout": 2, "panels": ["REF", "SRV"],
             "view": {"lat": 33.3, "lon": -87.3, "km_across": 90},
             "text": "A supercell that keeps its shape for hours has plenty of fuel. Watch the hook persist as it "
                     "moves north-east."},
            {"title": "Follow it", "time": "2011-04-27T22:30:00Z", "layout": 1, "panels": ["REF"],
             "view": {"lat": 33.5, "lon": -87.0, "km_across": 60},
             "text": "Try Tools → Follow a storm in live mode to keep a cell centred automatically."},
        ],
    },
    {
        "id": "greensburg-2007", "title": "Greensburg, Kansas tornado", "date": "4 May 2007", "site": "KDDC",
        "kind": "Tornado", "level": "Beginner",
        "about": "A night-time tornado: nobody could see it, so the warning came entirely from the radar. "
                 "A reminder of why radar matters after dark.",
        "steps": [
            {"title": "The hook at night", "time": "2007-05-05T02:40:00Z", "layout": 2, "panels": ["REF", "SRV"],
             "view": {"lat": 37.6, "lon": -99.3, "km_across": 50},
             "text": "A well-defined hook echo and a strong couplet to the south-west of Greensburg."},
        ],
    },
    {
        "id": "mayfield-2021", "title": "Mayfield, Kentucky: a December tornado", "date": "10 December 2021",
        "site": "KPAH", "kind": "Tornado", "level": "Intermediate",
        "about": "A long-track tornado at night in December, when severe weather is unusual. "
                 "The environment was warm and very windy, and the storm moved fast.",
        "steps": [
            {"title": "Fast and strong", "time": "2021-12-11T03:15:00Z", "layout": 4, "panels": ["REF", "SRV", "CC", "AZSH"],
             "view": {"lat": 36.75, "lon": -88.65, "km_across": 40},
             "text": "A fast-moving supercell: use the loop to see how far it travels between volumes. "
                     "Rotation and a debris signature appear together."},
        ],
    },
    {
        "id": "rolling-fork-2023", "title": "Rolling Fork, Mississippi tornado", "date": "24 March 2023",
        "site": "KDGX", "kind": "Tornado", "level": "Intermediate",
        "about": "A strong tornado after dark in the Delta. Another example of a long-lived, high-contrast "
                 "supercell and a debris signature far from the radar.",
        "steps": [
            {"title": "Debris at long range", "time": "2023-03-25T01:15:00Z", "layout": 4,
             "panels": ["REF", "SRV", "CC", "AZSH"],
             "view": {"lat": 32.9, "lon": -90.9, "km_across": 40},
             "text": "The tornado is some 80 km from the radar. Compare the four panels: the hook, the couplet "
                     "and the low-correlation patch line up."},
        ],
    },
    {
        "id": "derecho-2020", "title": "The Iowa derecho", "date": "10 August 2020", "site": "KDVN",
        "kind": "Wind", "level": "Beginner",
        "about": "A line of storms that produced hurricane-force winds across Iowa for hundreds of miles. "
                 "There is no hook to look for here: you look for the bow shape and for fast winds in velocity.",
        "steps": [
            {"title": "A bow echo", "time": "2020-08-10T17:05:00Z", "layout": 2, "panels": ["REF", "VEL"],
             "view": {"lat": 41.9, "lon": -91.7, "km_across": 220},
             "text": "The line bows out ahead of its ends. Straight-line winds are strongest at the apex. In the "
                     "velocity panel look for a long, bright streak of inbound winds just behind the leading edge."},
        ],
    },
    {
        "id": "harvey-2017", "title": "Hurricane Harvey landfall", "date": "25 August 2017", "site": "KCRP",
        "kind": "Hurricane", "level": "Beginner",
        "about": "A major hurricane came ashore on the Texas coast. A hurricane is one of the few things radar sees "
                 "as a whole: the eye, the eyewall and the spiral rain bands.",
        "steps": [
            {"title": "The eye and the eyewall", "time": "2017-08-26T03:00:00Z", "layout": 2, "panels": ["REF", "VEL"],
             "view": {"lat": 28.0, "lon": -97.1, "km_across": 250},
             "text": "The eyewall is the ring of the strongest echoes. In velocity, inbound and outbound winds sit "
                     "on either side of the eye: the stronger the pair, the stronger the wind."},
        ],
    },
]

KINDS = ("Tornado", "Wind", "Hurricane")


def find(event_id: str) -> dict | None:
    return next((e for e in EVENTS if e["id"] == event_id), None)


def step_view(event: dict, index: int, known_products=None) -> dict:
    """The (checked) view for one step of an event."""
    step = event["steps"][index]
    raw = {"site": event["site"], **{k: v for k, v in step.items() if k in ("time", "layout", "panels", "view")}}
    return views.clean_view(raw, known_products)


def event_time_range(event: dict):
    """(first, last) times used by an event's steps, as datetimes."""
    times = sorted(views.parse_time(s["time"]) for s in event["steps"])
    return times[0], times[-1]
