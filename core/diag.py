"""What took the time, measured rather than guessed.

Every slow answer has one stage to blame, and until this module existed the only
way to find it was to sit with a stopwatch. The recorder here is deliberately
small: a ring buffer of the last few dozen requests, a dictionary of stage
durations per request, and a text report. No framework, no background thread, no
sampling - a request costs a few dozen `perf_counter_ns` calls and a handful of
numbers.

**Nothing private is in a record.** No transcript, no reply, no prompt, no header
and no key. A trace carries a random id, the kind of interaction (voice or typed),
durations, counts, the model's *name*, whether the reply streamed, and - when
something failed - the exception's class name and an HTTP status code. Lengths
stand in for the words: `chars`, `steps`, `calls`.

**Absent is not zero.** Several stages a generic latency tool would report do not
exist here as separate steps - there is no standalone voice-activity detector (the
energy gate runs inside capture) and Python's `urllib` does not expose the TCP
connect separately from the request. Those are listed in `UNAVAILABLE` with the
reason, and never reported as a 0 ms measurement.

The clock is `time.perf_counter_ns`, monotonic and nanosecond-resolution, so a
duration cannot be moved by a clock adjustment and a 0.2 ms stage is still
readable. Only the wall-clock timestamp beside a trace uses `time.time`, because a
person reading history needs to know *when*, not only how long.
"""

import contextlib
import csv
import json
import os
import threading
import time
import uuid
from collections import deque

from . import config

# The ring the panel and the report read. Forty is enough to see a pattern and
# small enough that the memory cannot matter.
MAX_TRACES = 40

# The order stages are listed in, which is pipeline order rather than alphabetical.
STAGE_ORDER = ("capture", "stt", "route", "llm", "http", "tts", "buffering", "playback")

STAGE_LABELS = {
    "capture": "microphone capture",
    "stt": "speech to text",
    "route": "intent classification",
    "llm": "language model",
    "http": "HTTP request",
    "tts": "text to speech (first audio)",
    "buffering": "audio buffering",
    "playback": "playback",
}

# Metrics this app genuinely cannot produce, and why. Reported as unknown -
# never as a zero-millisecond stage, which would read as "instant".
UNAVAILABLE = {
    "connect": "urllib does not expose TCP/TLS connect separately from the request",
    "vad": "no separate voice-activity detector: the energy gate runs inside capture",
    "api_prep": "payload assembly is part of the llm/http stage, not timed apart",
}

_lock = threading.Lock()
_traces = deque(maxlen=MAX_TRACES)
# Configurable, and free when off: `RETRO_DIAG=0` in the environment, or the
# `diagnostics` setting in settings.json, turns recording off without touching code.
_enabled = os.environ.get("RETRO_DIAG", "1").strip().lower() not in ("0", "false", "no", "off")
_local = threading.local()


# ---------- the clock ---------------------------------------------------------

def now_ns():
    """A monotonic, nanosecond clock. Durations only; wall time is `time.time`."""
    return time.perf_counter_ns()


def _ms(ns):
    return None if ns is None else round(ns / 1_000_000.0, 1)


# ---------- one request -------------------------------------------------------

class Trace:
    """The timings of one interaction. Created by whoever starts it.

    A trace may be created before the user has finished speaking - the wake-word
    listener owns one per listening pass - so `restart()` moves its origin forward
    to the moment the phrase ended. Everything derived (total, time to first text,
    time to first audio) is measured from that origin, which is the number a person
    actually feels: the silence after they stop talking.
    """

    def __init__(self, kind="voice", origin="voice", started_ns=None, label=""):
        self.id = uuid.uuid4().hex[:10]
        self.kind = str(kind)
        self.origin = str(origin)
        self.started_ns = now_ns() if started_ns is None else int(started_ns)
        self.started_at = time.time()
        self.finished_ns = None
        self.ok = None
        self.cancelled = False
        self.stages = {}          # name -> [total ns, calls]
        self.marks = {}           # name -> ns
        self.errors = []          # [{stage, type, status, timed_out, retry}]
        self.retries = 0
        self.timeouts = 0
        self.model = None
        self.streaming = None
        self.label = str(label)[:40]
        self.unavailable = dict(UNAVAILABLE)
        self.meta = {}            # counts only: chars, steps, pieces

    # ---------- writing -----------------------------------------------------

    def restart(self, started_ns=None):
        """Moves the origin, for a trace created before the phrase really ended."""
        self.started_ns = now_ns() if started_ns is None else int(started_ns)
        self.started_at = time.time()
        return self

    def mark(self, name, at_ns=None):
        """Remembers one instant - the first token, first audio, playback start."""
        self.marks[str(name)] = now_ns() if at_ns is None else int(at_ns)

    def add(self, name, elapsed_ns):
        """Adds one measured interval to a stage, counting repeated calls."""
        if elapsed_ns is None:
            return
        entry = self.stages.setdefault(str(name), [0, 0])
        entry[0] += max(0, int(elapsed_ns))
        entry[1] += 1

    @contextlib.contextmanager
    def span(self, name):
        """Times a `with` block into a stage, even when it raises."""
        started = now_ns()
        try:
            yield self
        finally:
            self.add(name, now_ns() - started)

    def error(self, stage, exc=None, status=0, timed_out=False, retry=False):
        """Records a failure as a class name and a status - never as a message.

        A provider's error body can echo the request, so only the exception type
        and the numeric HTTP status are kept. A retry is counted where it happens,
        so "it was slow" and "it was asked three times" stay different facts.
        """
        if retry:
            self.retries += 1
            return
        if timed_out:
            self.timeouts += 1
        kind = type(exc).__name__ if exc is not None else ""
        text = str(exc or "")
        if not status:
            for code in (400, 401, 403, 404, 413, 429, 500, 502, 503, 504):
                if f"HTTP {code}" in text:
                    status = code
                    break
            if not status and ("timed out" in text.lower() or "timeout" in text.lower()):
                timed_out = True
        self.errors.append({"stage": str(stage), "type": kind, "status": int(status),
                            "timed_out": bool(timed_out)})

    def finish(self, ok=True, cancelled=False):
        if self.finished_ns is None:
            self.finished_ns = now_ns()
        self.ok = bool(ok) and not cancelled
        self.cancelled = bool(cancelled)
        return self

    # ---------- reading -----------------------------------------------------

    @property
    def running(self):
        return self.finished_ns is None

    def elapsed_ns(self):
        return (self.finished_ns or now_ns()) - self.started_ns

    def mark_ms(self, name):
        at = self.marks.get(name)
        return None if at is None else _ms(at - self.started_ns)

    def stage_ms(self, name):
        entry = self.stages.get(name)
        return None if entry is None else _ms(entry[0])

    def data(self):
        """A plain dictionary - what the panel draws and the export writes."""
        total_ns = self.elapsed_ns()
        stages = {}
        slowest = None
        for name in STAGE_ORDER:
            entry = self.stages.get(name)
            if entry is None:
                continue
            value = _ms(entry[0])
            stages[name] = {"ms": value, "calls": entry[1]}
            if slowest is None or value > stages[slowest]["ms"]:
                slowest = name
        for name, entry in self.stages.items():
            if name in stages:
                continue
            stages[name] = {"ms": _ms(entry[0]), "calls": entry[1]}
            if slowest is None or stages[name]["ms"] > stages[slowest]["ms"]:
                slowest = name
        first_text = self.mark_ms("first_text")
        first_audio = self.mark_ms("first_audio")
        play_start = self.mark_ms("play_start")
        play_end = self.mark_ms("play_end")
        playback = None
        if play_start is not None and play_end is not None and play_end >= play_start:
            playback = round(play_end - play_start, 1)
        # Two stages are derived from the marks rather than measured by a span: the
        # wait between the first audio being ready and the first sound leaving the
        # speaker (buffering), and how long the reply was audible (playback). They
        # belong in the same breakdown as everything else - the panel and the CSV
        # promise a row per stage, and a blank there would read as "not measured".
        if first_audio is not None and play_start is not None:
            wait = max(0.0, round(play_start - first_audio, 1))
            stages["buffering"] = {"ms": wait, "calls": 1}
            if slowest is None or wait > stages[slowest]["ms"]:
                slowest = "buffering"
        if playback is not None:
            # Playback is an output duration, not a bottleneck: it is listed and
            # exported, but never wins "slowest stage" against the work that decides
            # how long the user waits for an answer.
            stages["playback"] = {"ms": playback, "calls": 1}
        return {
            "id": self.id,
            "kind": self.kind,
            "origin": self.origin,
            "label": self.label,
            "at": time.strftime("%H:%M:%S", time.localtime(self.started_at)),
            "ok": self.ok,
            "cancelled": self.cancelled,
            "running": self.running,
            "total_ms": _ms(total_ns),
            "stages": stages,
            "slowest": slowest,
            "time_to_first_text_ms": first_text,
            "time_to_first_audio_ms": first_audio,
            "playback_ms": playback,
            "model": self.model,
            "streaming": self.streaming,
            "errors": list(self.errors),
            "retries": self.retries,
            "timeouts": self.timeouts,
            "meta": dict(self.meta),
            "unavailable": dict(self.unavailable),
        }

    def __repr__(self):                      # pragma: no cover - debugging aid
        return f"<Trace {self.id} {self.kind} {_ms(self.elapsed_ns())}ms>"


# ---------- the current request on this thread --------------------------------

def activate(trace):
    """Makes `trace` the one `mark()` and `span()` write to on this thread.

    The session runs a request on its own thread, so a thread-local is enough and
    it means the model client and the router can report their own timings without
    every call growing a `trace=` argument. Returns whatever was current before,
    which is what `using` restores.
    """
    previous = getattr(_local, "trace", None)
    _local.trace = trace
    return previous


def deactivate():
    previous = getattr(_local, "trace", None)
    _local.trace = None
    return previous


def current():
    return getattr(_local, "trace", None)


@contextlib.contextmanager
def using(trace):
    previous = activate(trace)
    try:
        yield trace
    finally:
        _local.trace = previous


# ---------- module helpers used by the pipeline ------------------------------

def enabled():
    """Detailed recording is on by default; `set_enabled(False)` makes it free."""
    return _enabled


def set_enabled(on):
    global _enabled
    _enabled = bool(on)
    if not _enabled:
        with _lock:
            _traces.clear()
    return _enabled


def new(kind="voice", origin="voice", started_ns=None, label=""):
    """A fresh trace, or None when recording is off."""
    if not _enabled:
        return None
    return Trace(kind=kind, origin=origin, started_ns=started_ns, label=label)


@contextlib.contextmanager
def span(name, trace=None):
    """Times a block into `name` on the given trace, or the thread's current one."""
    target = trace if trace is not None else current()
    if target is None:
        yield None
        return
    with target.span(name) as active:
        yield active


def mark(name, trace=None):
    target = trace if trace is not None else current()
    if target is not None:
        target.mark(name)
    return target


def _history_path():
    return _export_dir() / "recent.jsonl"


def record(trace):
    """Files a finished trace in memory and appends it to the small history file.

    The file is what lets a developer read the history from a terminal
    (`tools/latency.py report`) after the window has been closed, and what makes an
    export possible when the app is not running. It is one JSON line per request -
    no text, no headers - and it is rotated by line count rather than by a thread or
    a watcher.
    """
    if trace is None or not _enabled:
        return None
    if trace.running:
        trace.finish(ok=False)
    with _lock:
        _traces.append(trace)
        try:
            path = _history_path()
            os.makedirs(path.parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace.data(), sort_keys=True) + "\n")
            if path.stat().st_size > 240_000:
                lines = path.read_text(encoding="utf-8").splitlines()[-MAX_TRACES:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            pass                     # recording a timing is never worth a failure
    return trace


def history(limit=None):
    """The persisted history, newest last, as dictionaries.

    Reads the file the running app appends to, so a terminal report works with the
    window closed. A missing or damaged file is simply no history.
    """
    items = []
    try:
        with open(_history_path(), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
    except OSError:
        return []
    if limit is not None:
        items = items[-max(1, int(limit)):]
    return items


def clear_history():
    """Removes the persisted history as well as the in-memory ring."""
    reset()
    try:
        _history_path().unlink()
    except OSError:
        pass


def reset():
    """Drops the history (tests, and the panel's Clear button)."""
    with _lock:
        _traces.clear()


# ---------- reading the history ----------------------------------------------

def recent(limit=None):
    """The recorded traces, newest last."""
    with _lock:
        items = list(_traces)
    if limit is not None:
        items = items[-max(1, int(limit)):]
    return items


def latest():
    items = recent(1)
    return items[0].data() if items else None


def _mean(values):
    return round(sum(values) / len(values), 1) if values else None


def _percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * share))))
    return ordered[index]


def summary(items=None):
    """Averages, the slowest stage, and the failure count for the recent history.

    Only finished traces count. A request still running has a total that is not a
    latency yet, and including it would make the average move while the panel is
    being read. Pass `items` (already `data()` dictionaries, from memory or from the
    history file) to summarise an explicit set.
    """
    if items is None:
        items = [trace.data() for trace in recent() if not trace.running]
    else:
        items = [item for item in items if not item.get("running")]
    totals = [item["total_ms"] for item in items if item["total_ms"] is not None]
    by_stage = {}
    for item in items:
        for name, stage in item["stages"].items():
            by_stage.setdefault(name, []).append(stage["ms"])
    slowest_stage, slowest_ms = None, None
    for name, values in by_stage.items():
        average = _mean(values)
        if average is not None and (slowest_ms is None or average > slowest_ms):
            slowest_stage, slowest_ms = name, average
    failures = sum(1 for item in items if not item["ok"])
    return {
        "count": len(items),
        "average_ms": _mean(totals),
        "median_ms": _percentile(totals, 0.5),
        "p95_ms": _percentile(totals, 0.95),
        "slowest_stage": slowest_stage,
        "slowest_stage_ms": slowest_ms,
        "stages": {name: _mean(values) for name, values in by_stage.items()},
        "first_text_ms": _mean([item["time_to_first_text_ms"] for item in items
                                if item["time_to_first_text_ms"] is not None]),
        "first_audio_ms": _mean([item["time_to_first_audio_ms"] for item in items
                                 if item["time_to_first_audio_ms"] is not None]),
        "failures": failures,
        "retries": sum(item["retries"] for item in items),
        "timeouts": sum(item["timeouts"] for item in items),
        "models": sorted({item["model"] for item in items if item["model"]}),
        "streaming": (None if not any(item["streaming"] is not None for item in items)
                      else any(item["streaming"] for item in items)),
        "latest": items[-1] if items else None,
    }


def spoken_summary():
    """One short line for the voice - the panel holds the detail."""
    stats = summary()
    latest_item = stats.get("latest")
    if not latest_item:
        return "Nothing measured yet. Ask me something first."
    line = f"Last request took {latest_item['total_ms']:.0f} milliseconds"
    if latest_item["slowest"]:
        stage = latest_item["stages"][latest_item["slowest"]]
        line += f", and the slowest stage was {STAGE_LABELS.get(latest_item['slowest'], latest_item['slowest'])} at {stage['ms']:.0f}"
    line += f". The average is {stats['average_ms']:.0f}."
    return line


def _stage_line(item):
    """`capture 900 / stt 640 / llm 1300` - only stages that were measured."""
    parts = []
    for name in STAGE_ORDER:
        stage = item["stages"].get(name)
        if stage:
            calls = f"x{stage['calls']}" if stage["calls"] > 1 else ""
            parts.append(f"{name} {stage['ms']:.0f}{calls}")
    for name, stage in item["stages"].items():
        if name not in STAGE_ORDER:
            parts.append(f"{name} {stage['ms']:.0f}")
    return "  ".join(parts)


def report(limit=5, items=None):
    """The developer-facing text: latest breakdown, history, and what is missing."""
    if items is None:
        items = [trace.data() for trace in recent(limit)]
    else:
        items = list(items)[-max(1, int(limit)):]
    if not items:
        return ("no requests recorded yet - say something or type a command, then ask "
                "for the diagnostics again")
    stats = summary(items)
    lines = [f"last {stats['count']} request(s): average {stats['average_ms']} ms, "
             f"median {stats['median_ms']} ms, p95 {stats['p95_ms']} ms, "
             f"{stats['failures']} failed, {stats['retries']} retried, "
             f"{stats['timeouts']} timed out"]
    if stats["slowest_stage"]:
        lines.append(f"slowest stage on average: {stats['slowest_stage']} "
                     f"({stats['slowest_stage_ms']} ms)")
    latest_item = stats.get("latest")
    if latest_item:
        streaming = ("streamed" if latest_item["streaming"] else
                     "not streamed" if latest_item["streaming"] is False else "streaming unknown")
        lines.append("")
        lines.append(f"latest  {latest_item['id']}  {latest_item['origin']}  "
                     f"{'ok' if latest_item['ok'] else 'failed'}  "
                     f"total {latest_item['total_ms']} ms  {streaming}"
                     + (f"  model {latest_item['model']}" if latest_item["model"] else ""))
        if _stage_line(latest_item):
            lines.append("        " + _stage_line(latest_item))
        if latest_item["slowest"]:
            stage = latest_item["stages"][latest_item["slowest"]]
            lines.append(f"        slowest: {latest_item['slowest']} "
                         f"({stage['ms']} ms, {stage['calls']} call(s))")
        for key, label in (("time_to_first_text_ms", "first text"),
                           ("time_to_first_audio_ms", "first audio"),
                           ("playback_ms", "playback")):
            if latest_item[key] is not None:
                lines.append(f"        {label}: {latest_item[key]} ms")
        if latest_item["errors"]:
            lines.append("        errors: " + ", ".join(
                f"{item['stage']} {item['type'] or 'failure'}"
                + (f" HTTP {item['status']}" if item["status"] else "")
                for item in latest_item["errors"]))
    if len(items) > 1:
        lines.append("")
        lines.append("history (newest first):")
        for item in reversed(items):
            lines.append(f"  {item['at']}  {item['id']}  {item['total_ms']:>7.0f} ms  "
                         f"{'ok' if item['ok'] else 'failed'}  {_stage_line(item)}")
    if items and items[0]["unavailable"]:
        lines.append("")
        lines.append("not measured (never reported as zero):")
        for name, reason in sorted(items[0]["unavailable"].items()):
            lines.append(f"  {name}: {reason}")
    return "\n".join(lines)


# ---------- export ------------------------------------------------------------

def _export_dir():
    return config.LOG_DIR / "latency"


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def export_json(path=None):
    """Writes the recorded traces as JSON. Returns the path, or None on failure."""
    target = path or (_export_dir() / f"latency-{_stamp()}.json")
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unavailable": dict(UNAVAILABLE),
        "summary": summary(),
        "traces": [trace.data() for trace in recent()],
    }
    try:
        os.makedirs(os.path.dirname(str(target)), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except OSError:
        return None
    return target


def export_csv(path=None):
    """One row per request, one column per stage. Returns the path, or None."""
    target = path or (_export_dir() / f"latency-{_stamp()}.csv")
    names = list(STAGE_ORDER)
    for trace in recent():
        for name in trace.stages:
            if name not in names:
                names.append(name)
    fields = (["id", "at", "kind", "origin", "ok", "cancelled", "total_ms",
               "time_to_first_text_ms", "time_to_first_audio_ms", "playback_ms",
               "model", "streaming", "retries", "timeouts", "error_count"]
              + [f"{name}_ms" for name in names])
    try:
        os.makedirs(os.path.dirname(str(target)), exist_ok=True)
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for trace in recent():
                item = trace.data()
                row = {key: item.get(key) for key in fields if key in item}
                row["error_count"] = len(item["errors"])
                row["streaming"] = "" if item["streaming"] is None else item["streaming"]
                for name in names:
                    stage = item["stages"].get(name)
                    row[f"{name}_ms"] = "" if stage is None else stage["ms"]
                writer.writerow(row)
    except OSError:
        return None
    return target
