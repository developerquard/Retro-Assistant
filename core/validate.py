"""Checks model-proposed arguments before anything runs.

Unknown keys are dropped. A bad value rejects the whole command. Nothing from
the model reaches a script without passing here.
"""
import re

from .actions import SITE_ALIASES, SITES, resolve


def _int_in(value, lo, hi):
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _text(value, max_len):
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    return s if 1 <= len(s) <= max_len else None


def validate(action, args):
    """Returns clean args for the action, or None if they're not acceptable."""
    if not isinstance(args, dict):
        return None
    if action == "set_volume":
        p = _int_in(args.get("percent"), 0, 100)
        return None if p is None else {"percent": p}
    if action == "open_app":
        # Any installed application may be named now, so this no longer refuses a
        # name just because it is not one of the seven built-in programs. The name
        # is only ever *matched* against what the system reported, never run as
        # given, so free text is safe here; a path-like name is still refused.
        n = str(args.get("name", "")).strip().lower()
        if not n or len(n) > 80 or re.search(r"[/\\]|\.\.", n):
            return None
        return {"name": " ".join(n.split())}
    if action == "open_site":
        n = resolve(SITES, SITE_ALIASES, args.get("name", ""))
        return {"name": n, "url": SITES[n]} if n else None
    if action == "search_web":
        q = _text(args.get("query"), 200)
        return {"query": q} if q else None
    if action == "timer_start":
        m = _int_in(args.get("minutes"), 1, 180)
        return None if m is None else {"minutes": m}
    if action == "create_note":
        t = _text(args.get("text"), 500)
        return {"text": t} if t else None
    if action == "find_file":
        q = _text(args.get("query"), 100)
        if q is None or re.search(r"[/\\]|\.\.", q):
            return None
        return {"query": q}
    return {}   # commands that take no arguments
