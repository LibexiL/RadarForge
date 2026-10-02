"""Searching a list of commands as you type (the Ctrl+K palette). No Qt here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Command:
    title: str                         # what it does: "Switch to radar KTLX"
    group: str = ""                    # where it lives: "Radar", "File › Export"
    run: Callable | None = None
    shortcut: str = ""
    keywords: str = ""                 # extra words that should find it
    detail: str = ""                   # a second line of explanation
    order: int = 0                     # lower = listed earlier when scores tie


def _token_score(token: str, hay: str) -> float | None:
    """How well one typed word matches the text: None = no match. Substrings beat scattered letters,
    and matches at the start of a word or of the text beat matches in the middle."""
    i = hay.find(token)
    if i >= 0:
        boundary = i == 0 or not hay[i - 1].isalnum()
        return 100.0 - min(i, 40) * 0.5 + (25.0 if boundary else 0.0) + (10.0 if len(token) == len(hay) else 0.0)
    if len(token) < 3:
        return None                                     # too short for scattered letters to mean anything
    pos, score, first, last = 0, 40.0, -1, -1
    for ch in token:                                    # letters in order, not necessarily together
        j = hay.find(ch, pos)
        if j < 0:
            return None
        if first < 0:
            first = j
        score -= (j - last - 1) * 0.6 if last >= 0 else j * 0.3
        score += 2.0 if (j == 0 or not hay[j - 1].isalnum()) else 0.0
        last, pos = j, j + 1
    if last - first + 1 > 2 * len(token) + 4:           # spread over too much text: a coincidence, not a match
        return None
    return max(score, 1.0)


def score(query: str, command: Command) -> float | None:
    """Total match score of a typed query (several words must all match), or None."""
    tokens = query.lower().split()
    if not tokens:
        return 0.0
    title = command.title.lower()
    hay = f"{title} {command.group.lower()} {command.keywords.lower()}"
    total = 0.0
    for t in tokens:
        s = _token_score(t, title)
        if s is None:
            s = _token_score(t, hay)
            s = None if s is None else s * 0.6              # matches outside the title count for less
        if s is None:
            return None
        total += s
    return total


def search(commands: list, query: str, limit: int = 12) -> list:
    """The best matches first (everything in listed order when nothing is typed)."""
    scored = []
    for c in commands:
        s = score(query, c)
        if s is not None:
            scored.append((-s, c.order, c.title.lower(), c))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]
