"""A one-way channel from the core to a running window.

Actions stay free of any UI dependency: an action that needs the window (opening
the settings, for example) emits a named event here, and whichever window is
running subscribes. With no window attached, `emit` simply does nothing and the
action's spoken reply is all the user gets - which is exactly what happens in the
console and test runs.
"""
import threading

_listeners = []
_lock = threading.Lock()


def subscribe(fn):
    """Register a callback for events. Returns a function that unsubscribes."""
    with _lock:
        _listeners.append(fn)

    def unsubscribe():
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return unsubscribe


def emit(event):
    """Send an event name to every subscriber. Never raises into the caller."""
    with _lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(event)
        except Exception:      # a broken listener must not break an action
            pass
