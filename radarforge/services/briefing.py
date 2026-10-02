"""The few lines of text for a briefing picture: what is happening in the area on screen. No Qt here."""
from __future__ import annotations

from datetime import datetime

from .alerts import EVENT_RULE

_SIGNIFICANT = {"TORP": "PDS", "TORE": "emergency", "TORR": "observed", "SVRC": "considerable", "SVRD": "destructive",
                "FFWC": "considerable", "FFWE": "emergency"}


def warning_counts(alerts) -> dict:
    """{'tornado': (n, ['1 PDS']), 'severe': …, 'flood': …, 'watch': n} for the warnings given."""
    out = {"tornado": [0, {}], "severe": [0, {}], "flood": [0, {}], "watch": [0, {}]}
    for a in alerts:
        rule = EVENT_RULE.get(a.event)
        if rule is None or a.action in ("CAN", "EXP"):
            continue
        out[rule][0] += 1
        tag = _SIGNIFICANT.get(a.variant)
        if tag:
            out[rule][1][tag] = out[rule][1].get(tag, 0) + 1
    return {k: (n, [f"{c} {t}" for t, c in tags.items()]) for k, (n, tags) in out.items()}


def summary_lines(alerts, mcds=(), outlook_text: str | None = None, flashes: int | None = None, reports=None,
                  where: str = "in view") -> list:
    """Lines such as 'Warnings in view: 2 tornado (1 PDS) · 3 severe · 0 flash flood'."""
    c = warning_counts(alerts)

    def part(key, label):
        n, tags = c[key]
        return f"{n} {label}" + (f" ({', '.join(tags)})" if tags else "")
    lines = [f"Warnings {where}: {part('tornado', 'tornado')} · {part('severe', 'severe')} · {part('flood', 'flash flood')}"]
    if c["watch"][0]:
        lines.append(f"Watches {where}: {c['watch'][0]}")
    if mcds:
        lines.append("Mesoscale discussions: " + ", ".join(f"MD {m['number']}" for m in mcds))
    if outlook_text:
        lines.append(outlook_text)
    if flashes is not None:
        lines.append(f"Lightning {where}: {flashes} flashes")
    if reports:
        order = ("tornado", "funnel", "hail", "wind_damage", "wind_gust", "flood")
        bits = [f"{reports[k]} {k.replace('_', ' ')}" for k in order if reports.get(k)]
        if bits:
            lines.append("Storm reports: " + " · ".join(bits))
    return lines


def stamp(when: datetime | None, site: str) -> str:
    return f"{site} · {when:%Y-%m-%d %H:%M}Z" if when is not None else site
