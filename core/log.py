"""Two logs, one JSON line each.

`actions.log` records what Retro did, and never stores note text or search
queries - only their lengths. `retro.log` records how the assistant itself
behaved: states, timings, speech that timed out, tasks that had to be abandoned.
The packaged build has no console, so when something feels wrong this file is the
only evidence there is.

Both are capped. A line per event, for months of daily use, would otherwise leave a
file nobody can open and a disk that shrinks for no reason; when a log passes
`config.LOG_MAX_BYTES` it is rotated to `<name>.1`, keeping exactly one previous
generation. That is enough to answer "what happened just before this" after a
restart without becoming a log-management system, and the size is only asked for on
the first write of a run and then rarely, so nothing on the hot path pays for it.

Nothing here may raise: logging must never break a command.
"""
import datetime as dt
import json
import os
import time

from . import config

ACTION_LOG = "actions.log"
EVENT_LOG = "retro.log"
# How often the size is asked for, in lines. The first write of a run always checks -
# a file already over the cap at startup should not get 200 more lines first - and
# after that a stat every few hundred lines is far too cheap to matter and bounds how
# much a burst can overshoot to a few kilobytes.
CHECK_EVERY = 200
_writes = {}


def _rotate(target):
    """Keeps one previous generation: `retro.log` becomes `retro.log.1`."""
    try:
        os.replace(target, target.with_name(target.name + ".1"))
    except OSError:
        pass          # a log that cannot be rotated is still a log that can be written


def _write(name, entry):
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = config.LOG_DIR / name
        count = _writes.get(name, 0) + 1
        _writes[name] = count
        if count == 1 or count % CHECK_EVERY == 0:
            try:
                if target.stat().st_size > config.LOG_MAX_BYTES:
                    _rotate(target)
            except OSError:
                pass
        line = {"time": dt.datetime.now().isoformat(timespec="milliseconds"), **entry}
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def record(entry):
    """One action, as before."""
    _write(ACTION_LOG, entry)


def event(name, **fields):
    """One thing that happened to the assistant, with whatever numbers matter."""
    _write(EVENT_LOG, {"event": name, **fields})


def since(started):
    """Seconds since a `time.time()` stamp, rounded for the log."""
    return round(time.time() - started, 2)
