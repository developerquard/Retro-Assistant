"""The running assistant, for the tools that have to talk back to it.

Most tools do something to the machine and report what they saw. A few have to ask
the assistant itself: switch to chat mode, turn follow-up listening on or off, list
the tasks that are running, cancel one. Those are properties of the session, not of
Windows, so the session attaches itself here while it runs and the tools reach it
through this module.

With no session attached - the console, a test, a tool check - every function here
returns a plain refusal instead of raising, so a tool never crashes for the lack of
an assistant to talk to.
"""
import threading

_lock = threading.Lock()
_session = None


def attach(session):
    """Called by Session.start: makes the tools able to reach it."""
    global _session
    with _lock:
        _session = session
    return session


def detach(session=None):
    """Called by Session.stop, and by tests. Only detaches the session it was given."""
    global _session
    with _lock:
        if session is None or _session is session:
            _session = None


def session():
    with _lock:
        return _session


def connected():
    return session() is not None


def set_mode(mode, save=True):
    """Switches chat/work from a tool. Returns (ok, message)."""
    active = session()
    if active is None:
        return False, "no assistant is running to switch modes"
    chosen = active.set_mode(mode, save=save)
    if chosen is None:
        return False, f"'{mode}' is not a mode - say chat or work"
    return True, f"switched to {chosen} mode"


def set_followup(on, save=True):
    """Turns listen-after-Retro on or off from a tool."""
    active = session()
    if active is None:
        return False, "no assistant is running to change that"
    state = active.set_followup(on, save=save)
    return True, ("listening for follow-ups without the wake word"
                  if state else "back to needing 'Retro' every time")


def followup():
    active = session()
    return bool(getattr(active, "followup", False))


def mode():
    active = session()
    return getattr(active, "mode", "work")


def tasks(limit=10):
    """The tasks this session has run, newest first, as plain dictionaries."""
    active = session()
    if active is None:
        return []
    return active.task_list(limit=limit)


def cancel_task(which=""):
    """Cancels the running task, or one from the recent list. Returns a message."""
    active = session()
    if active is None:
        return False, "no assistant is running"
    return active.cancel_task(which)
