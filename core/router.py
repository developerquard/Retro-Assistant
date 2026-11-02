"""Matches typed text to one action, with no AI.

Scoring: each action's longest matching keyword counts. Actions whose extractor
returns None are skipped. A tie between different actions is ambiguous and the
user is asked to rephrase. No match returns None (the Llama fallback comes in M4).
"""
from dataclasses import dataclass
from typing import List, Optional, Union

from .actions import ACTIONS
from .model import Action, has_kw, norm


@dataclass
class Match:
    action: Action
    args: dict


@dataclass
class Ambiguous:
    names: List[str]


def route(raw: str) -> Optional[Union[Match, Ambiguous]]:
    t = norm(raw)
    scored = []
    for action in ACTIONS:
        hits = [k for k in action.keywords if has_kw(t, k)]
        if not hits:
            continue
        args = action.extract(raw)
        if args is None:
            continue
        scored.append((max(len(k) for k in hits), action, args))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    best = scored[0][0]
    top = [s for s in scored if s[0] == best]
    if len(top) > 1:
        return Ambiguous([s[1].name for s in top])
    _, action, args = top[0]
    return Match(action, args)
