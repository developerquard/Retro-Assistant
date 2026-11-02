"""One request in flight, and the handle the user can pull to stop or steer it.

Every long-running path - the agent, the model call, the planner's steps - carries
one of these. It is how "Retro, stop" reaches something that is already running,
and why a request that has been abandoned can never carry on acting: every step
checks `check()` first, and a cancelled task raises `Stopped` there.

It is also what stops the assistant latching: the watchdog in core/session.py
watches the task that is running and can cancel it, so a request that hangs can
never leave the display green and the microphone closed.
"""
import threading
import time


class Stopped(Exception):
    """Raised at the next checkpoint of a request the user (or the watchdog) stopped."""


class Task:
    def __init__(self, text, started=None):
        self.text = text
        self.started = started if started is not None else time.time()
        self.detail = ""              # what is happening now, for progress speech
        self.steps = 0                # how many steps have finished
        self.superseded = False       # a newer request took over; stay quiet
        self.replied = False          # a stop or the watchdog already answered for it
        self.progress_lines = 0       # how many "still working" lines it has had
        self.did_work = False         # a real step ran, not only a spoken answer
        self.note_at = 0.0            # when the "say Retro first" note was last played
        self.done = threading.Event()
        self.finished = None          # when it ended, so a duration can be reported
        self.failed = False           # ended badly, rather than being stopped
        self.reason = ""              # why it ended, for the log
        self.last_progress = self.started
        self._cancel = threading.Event()

    # ---------- what the running code asks ---------------------------------

    def alive(self):
        return not self._cancel.is_set() and not self.superseded

    def check(self):
        """Raises Stopped when this request should not continue."""
        if not self.alive():
            raise Stopped(self.reason or "cancelled")

    def touch(self, detail=""):
        """A step finished or is progressing: the watchdog uses this as a heartbeat."""
        self.last_progress = time.time()
        if detail:
            self.detail = detail

    # ---------- what the user (or the watchdog) does -----------------------

    def cancel(self, reason="user"):
        self.reason = reason
        self._cancel.set()
        return True

    def supersede(self):
        """Abandoned because the user asked for something else instead."""
        self.superseded = True
        self.done.set()

    def finish(self):
        if self.finished is None:
            self.finished = time.time()
        self.done.set()

    # ---------- what the task list and the tools report ----------------------

    def duration(self):
        """How long it ran, or has been running."""
        return (self.finished or time.time()) - self.started

    def status_label(self):
        """One word for where this task stands, for `list_tasks` and the log."""
        if self._cancel.is_set():
            return "cancelled"
        if self.superseded:
            return "replaced by a newer request"
        if not self.done.is_set():
            return "running"
        return "failed" if self.failed else "done"

    # ---------- numbers for the log and the progress lines ------------------

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def elapsed(self):
        return time.time() - self.started

    def quiet_for(self):
        """Seconds since anything last claimed progress."""
        return time.time() - self.last_progress
