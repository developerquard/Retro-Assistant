#!/usr/bin/env python3
"""Handing a discussion over to the coding terminal - and surviving it.

The order here is the feature, and every step can fail in its own way, so every step
has a name:

    launch   start the terminal process
    connect  wait for it to open the link
    auth     check the token it sent
    ready    wait for it to prove it can talk to its model
    brief    write the brief (only now - a brief written for a terminal that never
             became ready is a brief nobody read)
    accept   wait for the terminal to take the task
    work     relay progress until `done`

A failure is emitted with the stage it happened at, so the window can say "the
terminal exited before it connected (exit 1)" instead of a generic timeout. The link
is a plain TCP socket over loopback with a per-run token; nothing else on the machine
can claim the task.
"""
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

CONNECT_S = 90          # how long a terminal has to open the link
READY_S = 180           # how long it has to prove it can reach its model
ACK_S = 60              # how long it has to accept the task
FINISH_S = 3600         # a task may take an hour; the link is watched, not timed out
STALE_S = 120           # a state file older than this is not live
BRIEF_S = 45            # how long the model may take to write the brief, at most

STARTING_STATES = ("starting", "launched", "connected", "ready", "working")

# ---------------------------------------------------------------------------
# Events: one subscriber list, so the orb and the window see the same truth
# ---------------------------------------------------------------------------

_listeners = []


def subscribe(fn):
    """Register a listener. Returns a function that removes it."""
    _listeners.append(fn)

    def unsubscribe():
        if fn in _listeners:
            _listeners.remove(fn)

    return unsubscribe


def emit(event):
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The seed: a phrase typed to Retro that opens the discussion window
# ---------------------------------------------------------------------------

_seed = ""


def offer(text=""):
    """Open the discussion window with `text` already in the box.

    Two channels, on purpose: the handoff's own listeners carry the seed, and the
    core's ui_hook carries the bare event to whichever window is running. A console
    run has no window, and both are no-ops there.
    """
    global _seed
    _seed = str(text or "").strip()
    emit({"kind": "open_code_chat", "seed": _seed})
    try:
        from core import ui_hook
        ui_hook.emit("open_code_chat")
    except Exception:
        pass


def take_seed():
    """The seed, once. Asking twice returns "" so it is never submitted twice."""
    global _seed
    text = _seed
    _seed = ""
    return text


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def launcher_command(project, address, token):
    """The command that starts a terminal for this handoff.

    From a source checkout that is ``python -m retrocli``. A frozen build ships a
    console executable beside the window (RetroCLI.exe) and that is what is started:
    it is smaller, it starts faster, and it has a real stdout. If that file is not
    there, Retro.exe re-enters itself with ``--cli`` instead - the module flag means
    nothing to a packaged executable.
    """
    tail = ["--project", str(project), "--ipc", str(address), "--token", str(token)]
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).with_name("RetroCLI.exe")
        if beside.exists():
            return [str(beside), *tail]
        return [sys.executable, "--cli", *tail]
    root = str(Path(__file__).resolve().parent.parent)
    return [sys.executable, "-m", "retrocli", *tail]


def launch(project, address, token):
    """Start the terminal in its own console and return the process.

    Nothing is redirected, and that is the whole point. ``stdin=subprocess.PIPE``
    made the child read its keyboard from a pipe the window never wrote to: the
    terminal reached its prompt and then died on a lost standard input, which is
    exactly the console that vanished a second after it opened. With every stream
    left alone, CREATE_NEW_CONSOLE gives the child the new console's own handles.
    """
    command = launcher_command(project, address, token)
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = (env.get("PYTHONPATH") or "").strip()
    separator = ";" if sys.platform == "win32" else ":"
    env["PYTHONPATH"] = root + (separator + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    setup = dict(cwd=str(project or "."), env=env)
    if sys.platform == "win32":
        return subprocess.Popen(
            command,
            creationflags=0x00000010,                 # CREATE_NEW_CONSOLE
            close_fds=True,
            **setup,
        )
    return subprocess.Popen(command, start_new_session=True, **setup)


# ---------------------------------------------------------------------------
# Published state: what the terminal last said it was doing
# ---------------------------------------------------------------------------

def configured():
    """Whether the coding model is set up, so a big request can be offered instead."""
    try:
        from retrocli import config
        return bool(config.configured())
    except Exception:
        return False


def published_state():
    """The terminal's last published state, or {} when it is not live."""
    try:
        from retrocli import config, state
        data = state.read()
        if not isinstance(data, dict) or not data:
            return {}
        updated = float(data.get("updated") or 0)
        if not updated or (time.time() - updated) > STALE_S:
            return {}
        return data
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# The handoff
# ---------------------------------------------------------------------------

class Handoff:
    """One discussion, one terminal, one task - supervised to the end."""

    def __init__(self, spawn=None, generator=None):
        self._spawn = spawn or launch
        self._generator = generator
        self._state = "idle"
        self._task = None
        self._brief = ""
        self._snapshot = None
        self._project = ""
        self._listener = None
        self._link = None
        self._process = None
        self._thread = None
        self._cancel = threading.Event()
        self._started = 0.0
        self._done_payload = None
        self._address = ""
        self._token = ""

    # ---- reading the state ---------------------------------------------

    @property
    def state(self):
        return self._state

    def busy(self):
        """Whether a handoff is running (the "one at a time" rule)."""
        return self._state in STARTING_STATES

    @property
    def task(self):
        return self._task

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def brief(self):
        return self._brief

    @property
    def address(self):
        return self._address

    @property
    def token(self):
        return self._token

    # ---- the run -------------------------------------------------------

    def _fail(self, stage, message):
        self._state = "failed"
        emit({"kind": "error", "stage": stage, "message": str(message)})
        emit({"kind": "state", "state": "failed", "detail": str(message)[:160],
              "id": self._task})
        return False, str(message)

    def start(self, project, messages):
        """Hand `project` and the discussion over. Returns ``(ok, message)``."""
        if self.busy():
            return False, "already coding - one task at a time"
        project = str(project or "").strip()
        messages = list(messages or [])
        self._project = project
        self._snapshot = {"project": project, "messages": messages}
        self._state = "starting"
        self._brief = ""
        self._done_payload = None
        self._cancel.clear()
        if not project:
            return self._fail("launch", "no project folder - choose one first")
        if not messages:
            return self._fail("launch", "the discussion is empty - say what to build")
        # The folder itself is checked by the terminal when it starts: it is the side
        # that has to work in it, and its refusal is a better message than ours.
        try:
            from retrocli import config
            problem = config.missing()
        except Exception as exc:
            problem = f"the coding model could not be read ({exc})"
        if problem:
            return self._fail("launch", f"the coding model is not set up: {problem}")

        self._task = f"task-{int(time.time() * 1000) % 10_000_000}"
        self._started = time.time()
        emit({"kind": "state", "state": "starting",
              "detail": "starting the coding terminal", "id": self._task})
        # Listen and launch now, not in the worker: starting twice in the same instant
        # must see the first terminal, and the port has to exist before the window
        # can say it is starting.
        try:
            self._open_and_launch()
        except Exception as exc:
            return self._fail("launch", f"the coding terminal could not start ({exc})")
        self._thread = threading.Thread(target=self._supervise, name="retro-handoff",
                                        daemon=True)
        self._thread.start()
        return True, ""

    def _wait(self, timeout=FINISH_S):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _run(self):
        """Start and supervise in one call (the older entry point)."""
        self._open_and_launch()
        self._supervise()

    def wait(self, timeout=8.0):
        """Wait for the handoff to finish; True when it has."""
        return self._wait(timeout)

    def cancel(self):
        """Stop waiting and tell the terminal to stop between steps."""
        self._cancel.set()
        self._state = "cancelled"
        try:
            if self._link is not None:
                self._link.send({"t": "cancel"})
        except Exception:
            pass
        try:
            if self._process is not None and self._process.poll() is None:
                if sys.platform == "win32":
                    self._process.terminate()
                else:
                    self._process.send_signal(signal.SIGTERM)
        except Exception:
            pass
        emit({"kind": "state", "state": "cancelled", "detail": "coding cancelled",
              "id": self._task})

    # ---- the supervisor ------------------------------------------------

    def _open_and_launch(self):
        """Open the listening socket and start the terminal process."""
        from retrocli import ipc
        try:
            listener, port = ipc.listener()
        except Exception as exc:
            raise RuntimeError(f"no loopback port could be opened ({exc})") from exc
        self._listener = listener
        self._address = f"127.0.0.1:{port}"
        self._token = secrets.token_hex(16)
        self._process = self._spawn(self._project, self._address, self._token)
        self._state = "launched"
        emit({"kind": "state", "state": "launched",
              "detail": "coding terminal launched", "id": self._task})

    def _supervise(self):
        from retrocli import ipc
        listener = self._listener
        link = None
        try:
            # ---- connect, watching the process while we wait
            deadline = time.time() + CONNECT_S
            while True:
                try:
                    link = ipc.Link.accept(listener, 0.25)
                    break
                except ipc.LinkError:
                    pass
                if self._cancel.is_set():
                    return
                code = self._poll()
                if code is not None:
                    self._fail("launch",
                               f"the coding terminal exited (exit {code}) before it "
                               f"connected - its console shows why")
                    return
                if time.time() > deadline:
                    self._fail("connect",
                               f"no terminal connected within {CONNECT_S}s")
                    return
            self._link = link
            self._state = "connected"
            emit({"kind": "state", "state": "connected",
                  "detail": "terminal connected", "id": self._task})

            # ---- auth
            hello = link.recv(READY_S)
            if not hello:
                self._fail("auth", "the terminal connected but sent no hello")
                return
            if str(hello.get("token") or "") != self._token:
                self._fail("auth", "the terminal sent the wrong token")
                return
            if hello.get("t") == "error":
                self._fail("ready", str(hello.get("message") or "the terminal failed"))
                return

            # ---- ready
            message = hello if hello.get("t") == "ready" else link.recv(READY_S)
            if not message:
                self._fail("ready", f"the terminal did not report ready within {READY_S}s")
                return
            if message.get("t") == "error":
                self._fail("ready", str(message.get("message") or "the terminal said no"))
                return
            if message.get("t") != "ready":
                self._fail("ready", f"unexpected message {message.get('t')!r} before ready")
                return
            self._state = "ready"
            emit({"kind": "ready", "id": self._task,
                  "model": str(message.get("model") or ""),
                  "detail": str(message.get("detail") or ""),
                  "project": str(message.get("project") or self._project)})

            # ---- brief, then the task
            brief = self._write_brief()
            if brief is None:
                return
            emit({"kind": "brief", "chars": len(brief), "source": "transcript",
                  "id": self._task})
            link.send({"t": "task", "id": self._task, "brief": brief,
                       "project": self._project})

            # ---- accept
            accepted = link.recv(ACK_S)
            while accepted is not None and accepted.get("t") == "state":
                accepted = link.recv(ACK_S)
            if accepted is None:
                self._fail("accept", f"the terminal did not take the task within {ACK_S}s")
                return
            if accepted.get("t") == "error":
                self._fail("accept", str(accepted.get("message") or "the terminal refused"))
                return
            if accepted.get("t") != "accepted":
                self._fail("accept",
                           f"unexpected message {accepted.get('t')!r} instead of accepted")
                return
            self._state = "working"
            emit({"kind": "ack", "id": self._task})
            emit({"kind": "state", "state": "working",
                  "detail": "the coding agent is working", "id": self._task})

            # ---- work
            self._relay(link)
        finally:
            for item in (link, listener):
                try:
                    if item is not None:
                        item.close()
                except Exception:
                    pass

    def _poll(self):
        """The process exit code, or None while it is still running."""
        if self._process is None:
            return None
        try:
            return self._process.poll()
        except Exception:
            return None

    def _write_brief(self):
        """The brief, from the snapshot - and on disk, because it is the record."""
        try:
            generator = self._generator
            if generator is None:
                from core import brief as brief_module
                text, source = brief_module.build(self._snapshot, _complete_brief)
            else:
                text, source = generator(self._snapshot)
        except Exception as exc:
            self._fail("brief", f"the brief could not be written ({exc})")
            return None
        text = str(text or "").strip()
        if not text:
            self._fail("brief", "the discussion produced no usable brief")
            return None
        self._brief = text
        try:
            from retrocli import config
            Path(config.LAST_BRIEF).parent.mkdir(parents=True, exist_ok=True)
            Path(config.LAST_BRIEF).write_text(text, encoding="utf-8")
        except Exception:
            pass
        return text

    def _relay(self, link):
        """Relay progress until the terminal reports done."""
        deadline = time.time() + FINISH_S
        while time.time() < deadline:
            if self._cancel.is_set():
                return
            try:
                message = link.recv(1.0)
            except Exception:
                break
            if message is None:
                if link.closed:
                    break
                code = self._poll()
                if code is not None:
                    break
                continue
            kind = message.get("t")
            if kind == "state":
                current = str(message.get("state") or "working")
                self._state = current if current in ("working", "done", "failed") else self._state
                emit({"kind": "state", "state": current,
                      "detail": str(message.get("detail") or ""), "id": self._task})
            elif kind == "tool":
                emit({"kind": "tool", "tool": str(message.get("tool") or ""),
                      "id": self._task})
            elif kind == "done":
                self._finish(message)
                return
            elif kind == "error":
                self._fail("work", str(message.get("message") or "the task failed"))
                return
        # The link went quiet without a done: say so rather than hang.
        if not self._cancel.is_set():
            self._finish({"t": "done", "ok": False, "cancelled": False,
                          "summary": "the terminal closed without a report",
                          "error": "the terminal closed without a report",
                          "files": [], "commands": []})

    def _finish(self, message):
        self._done_payload = dict(message or {})
        self._state = "done" if message.get("ok") else (
            "cancelled" if message.get("cancelled") else "failed")
        emit({
            "kind": "done",
            "id": self._task,
            "ok": bool(message.get("ok")),
            "cancelled": bool(message.get("cancelled")),
            "awaiting_plan": bool(message.get("awaiting_plan")),
            "summary": str(message.get("summary") or ""),
            "error": str(message.get("error") or ""),
            "files": list(message.get("files") or []),
            "commands": list(message.get("commands") or []),
            "seconds": float(message.get("seconds") or (time.time() - self._started)),
        })
        emit({"kind": "state", "state": self._state,
              "detail": str(message.get("summary") or "")[:160], "id": self._task})

    # ---- compatibility --------------------------------------------------

    def set_brief(self, brief):
        """Kept for callers that write the brief themselves."""
        self._brief = str(brief or "")

    def send_task(self, brief):
        """Send a task on an already-ready link (used by the window's retry path)."""
        if self._link is None:
            return {"ok": False, "error": "no link"}
        self._brief = str(brief or "")
        self._link.send({"t": "task", "id": self._task, "brief": self._brief,
                         "project": self._project})
        self._state = "working"
        emit({"kind": "state", "state": "working",
              "detail": "the coding agent is working", "id": self._task})
        return {"ok": True}


# ---------------------------------------------------------------------------
# One handoff per process: the window asks for this one
# ---------------------------------------------------------------------------

_handoff = None
_lock = threading.Lock()


def get_handoff():
    global _handoff
    with _lock:
        if _handoff is None:
            _handoff = Handoff()
        return _handoff


def _brief_completion(messages, max_tokens=800):
    """One completion from Retro's own model, used only to write a brief.

    The cloud is used when a key is set. Without one this raises, and the brief is
    built from the transcript instead: loading a local GGUF to summarise a handful of
    lines would cost seconds of silence and hundreds of megabytes, and the transcript
    brief is deterministic - the same discussion always produces the same brief.
    """
    from core import config as core_config
    from core import llm, settings
    if not core_config.cloud_available():
        raise RuntimeError("no cloud key - the transcript brief is used")
    brain, _is_cloud = llm.build_brain(str(settings.get("model") or "auto"))
    try:
        return brain.ask(messages, max_tokens)
    finally:
        try:
            brain.unload()
        except Exception:
            pass


def _complete_brief(messages, max_tokens=800):
    """`_brief_completion` with a deadline, so a wedged request cannot stall a handoff."""
    result = {}

    def work():
        try:
            result["text"] = _brief_completion(messages, max_tokens)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=work, name="retro-brief", daemon=True)
    thread.start()
    thread.join(BRIEF_S)
    if thread.is_alive():
        raise TimeoutError(f"the brief did not arrive within {BRIEF_S}s")
    if "error" in result:
        raise result["error"]
    return result.get("text") or ""


def brief_generator(snap):
    """The default brief: what was discussed, and the project it belongs to."""
    from core import brief as brief_module
    return brief_module.build(snap, _complete_brief)


def kinds(events):
    return [event.get("kind") for event in events if isinstance(event, dict)]
