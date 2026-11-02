"""Fallback for requests the typed rules can't match.

The model only picks one command from the registry and fills its arguments.
It never runs anything itself, and its output is checked before use. The GGUF
model is loaded on first use and unloaded again after a few idle minutes, so
Retro isn't holding a gigabyte of RAM while nothing is happening.
"""
import gc
import json
import re
import threading
import time
from pathlib import Path

from . import config, diag
from .actions import ACTIONS
from .router import Match
from .validate import validate

ARG_HINTS = {
    "set_volume": '{"percent": 0-100}',
    "open_app": '{"name": "the application the user named, as they said it"}',
    "open_site": '{"name": "youtube|google|github"}',
    "search_web": '{"query": "text to search"}',
    "timer_start": '{"minutes": 1-180}',
    "create_note": '{"text": "note text"}',
    "find_file": '{"query": "part of a file name"}',
}

SYSTEM_PROMPT = (
    "You are Retro, a voice assistant running locally on a Windows laptop. You never talk "
    "to the user: your only job is to choose exactly one command from the list below and "
    "fill in its arguments.\n"
    "Rules:\n"
    "- Reply with a single JSON object and nothing else. No prose, no markdown, no code fences.\n"
    "- You never answer the user's question, not even when you know the answer. Asking for\n"
    "  the time, the date, the weather or a fact is answered by naming the command, never by\n"
    "  giving the value.\n"
    "- Use a command name exactly as written in the list. Never invent a name. There is no\n"
    "  'play' command: music is pause, resume, next_track or prev_track.\n"
    "- Websites (youtube, google, github) use open_site. Installed applications use\n"
    "  open_app, naming the application the way the user said it. Any installed\n"
    "  application may be opened, so never answer that one does not exist.\n"
    "- If no command fits what the user asked, reply {\"action\": null, \"args\": {}}.\n"
    "- Give the command name in English even when the user spoke Hindi or Hinglish.\n"
    "- Most sentences are not commands. Greetings, small talk, questions about you, and\n"
    "  anything the list cannot do must be null. Never force a command onto a sentence\n"
    "  that does not clearly ask for one, and never answer a question with an unrelated\n"
    "  command.\n"
    "- Requests like shut down, restart, sleep, delete, install, send, call or message are\n"
    "  not on the list. Reply null for them.\n"
    "- The user speaks English, Hinglish and Hindi. Understand all three.\n"
    "Commands:\n"
)


def build_messages(text):
    lines = [f"- {a.name}: {a.description or a.name}. args: {ARG_HINTS.get(a.name, '{}')}"
             for a in ACTIONS]
    # A single "time" example here made the model answer small talk with the time
    # command. Show several shapes instead, including the null the rules now demand.
    examples = ('\nExamples of well-formed replies, nothing else:\n'
                '{"action": "time", "args": {}}\n'
                '{"action": "open_app", "args": {"name": "notepad"}}\n'
                '{"action": "set_volume", "args": {"percent": 40}}\n'
                'a greeting or small talk -> {"action": null, "args": {}}\n'
                'Reply now with one JSON object only.')
    system = SYSTEM_PROMPT + "\n".join(lines) + examples
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def parse_reply(reply):
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("reply is not an object")
    return data.get("action"), data.get("args") or {}


def propose_detailed(text, complete):
    """Like propose(), but also reports whether the reply was valid JSON.

    Returns (Match or None, reason, json_ok). The reason is a fixed label, never
    user text. tools/eval_intent.py needs the JSON flag to score validity.
    """
    try:
        reply = complete(build_messages(text))
    except Exception:
        return None, "model_error", False
    try:
        name, args = parse_reply(reply)
    except ValueError:
        return None, "invalid_output", False
    if name is None:
        return None, "no_match", True
    by_name = {a.name: a for a in ACTIONS}
    if name not in by_name:
        return None, "unknown_action", True
    clean = validate(name, args)
    if clean is None:
        return None, "bad_args", True
    return Match(by_name[name], clean), "ok", True


def choose_from_names(text, names, complete):
    """Picks one of `names` for `text`, or None.

    Used when a spoken application name fits several installed ones. The model can
    only ever return a name from the list it was given, so it cannot invent an
    application - it only decides which of the real ones was meant.
    """
    if not names:
        return None
    listing = "\n".join(f"- {name}" for name in names)
    messages = [
        {"role": "system", "content":
            "You are Retro. The user asked for an application. These installed applications "
            "could be the one they meant:\n" + listing +
            "\nReply with exactly one name from that list and nothing else, or null if none "
            "of them is what the user meant."},
        {"role": "user", "content": text},
    ]
    try:
        reply = complete(messages)
    except Exception:
        return None
    cleaned = str(reply).strip().strip('"').strip("'.").strip()
    if not cleaned or cleaned.lower() in ("null", "none"):
        return None
    for name in names:
        if name.lower() == cleaned.lower():
            return name
    for name in names:                     # tolerate a name wrapped in a sentence
        if name.lower() in cleaned.lower():
            return name
    return None


def propose(text, complete):
    """Returns (Match or None, reason). The reason is a fixed label, never user text."""
    match, reason, _json_ok = propose_detailed(text, complete)
    return match, reason


class Fallback:
    """Loads the GGUF model on first use, so startup stays fast and idle memory stays low."""

    def __init__(self, model_path=None, complete=None, idle_unload_s=None):
        self.model_path = Path(model_path or config.MODEL_PATH)
        self._complete = complete
        self._injected = complete is not None
        self.idle_unload_s = config.MODEL_IDLE_UNLOAD_S if idle_unload_s is None else idle_unload_s
        self._lock = threading.Lock()
        self._timer = None
        self.load_time_s = None
        self.loads = 0            # how many times the model had to be loaded

    @property
    def loaded(self):
        return self._complete is not None

    def _get_complete(self):
        with self._lock:
            if self._complete is not None:
                return self._complete
            if not self.model_path.exists():
                raise FileNotFoundError("model file missing")
            from llama_cpp import Llama
            started = time.time()
            llm = Llama(model_path=str(self.model_path), n_ctx=config.MODEL_CTX,
                        n_threads=config.MODEL_THREADS, verbose=False)
            self.load_time_s = time.time() - started
            self.loads += 1

            def complete(messages, max_tokens=160):
                r = llm.create_chat_completion(messages=messages, temperature=0.0,
                                               max_tokens=max_tokens)
                return r["choices"][0]["message"]["content"]

            self._complete = complete
            return complete

    def unload(self):
        """Drop the model so idle RAM goes back to the baseline."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._complete = None
        gc.collect()

    def _arm_unload(self):
        if self._injected:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.idle_unload_s, self.unload)
            self._timer.daemon = True
            self._timer.start()

    def ask(self, messages, max_tokens=220):
        """Raw text from the model for a prompt the caller wrote itself.

        The planner brings its own prompt, because choosing one command from a list
        and planning a sequence of them are different questions.
        """
        try:
            complete = self._get_complete()
        except Exception:
            raise
        try:
            reply = complete(messages, max_tokens)
        except TypeError:                       # a completion that takes messages only
            reply = complete(messages)
        self._arm_unload()
        return reply

    def stream(self, messages, max_tokens=220, on_delta=None):
        """The local model cannot stream: it answers in one piece, then it is sent.

        Same shape as Cloud.stream, so the caller does not have to care which brain
        it is talking to - a machine with no key simply gets its sentence a moment
        later instead of not at all.
        """
        text = self.ask(messages, max_tokens)
        if on_delta is not None and text:
            on_delta(text)
        return text

    def choose(self, text, names):
        """Which of `names` the user meant, or None if the model isn't available."""
        try:
            complete = self._get_complete()
        except Exception:
            return None
        chosen = choose_from_names(text, names, complete)
        self._arm_unload()
        return chosen

    def __call__(self, text):
        try:
            complete = self._get_complete()
        except Exception:
            return None, "model_unavailable"
        result = propose(text, complete)
        self._arm_unload()                       # restart the idle countdown after every use
        return result


def build_brain(choice, idle_unload_s=None):
    """The brain for a UI choice. Returns (brain, is_cloud).

    "groq" and "groq-smart" are the hosted models. "auto" means the best one that
    is actually available here: the cloud when a key is configured, and otherwise
    the 1B GGUF on disk, so a machine with no key and no network still runs. Asking
    for the cloud without a key falls back the same way rather than leaving an
    assistant that cannot answer at all - the reason is logged, once, by the caller.
    """
    choice = str(choice or "auto").lower()
    chosen = config.cloud_model_for(choice)
    wanted_cloud = chosen is not None
    if wanted_cloud and config.cloud_available():
        model = chosen
        # The other chat models ride along as a way out of a rate limit, which on
        # this provider is counted per model rather than per key.
        return Cloud(model=model, alternates=config.alternates_for(model)), True
    local = choice if choice in config.MODEL_FILES else "1b"
    return (Fallback(model_path=config.model_path_for(local), idle_unload_s=idle_unload_s),
            False)


# Cloudflare fronts this API, and it answers a bare urllib request with
# "HTTP 403, error code: 1010" - a blocked user-agent signature, not a rejection by
# the provider. That is why this is set explicitly rather than left to the default
# Python-urllib string, which looks like a scraper. The value lives in config because
# the model listing is fetched from there, and both calls have to send the same string.
USER_AGENT = config.USER_AGENT


class CloudError(RuntimeError):
    """The hosted model refused, timed out or answered with nothing usable."""


class Cloud:
    """A hosted chat model, reached over HTTPS with the standard library only.

    The same shape as Fallback - `ask`, `complete`, `choose`, `__call__` - so the
    planner and the router take it without knowing where the words came from. The
    difference it makes is the whole point of M7: a 1B GGUF on a laptop CPU cannot
    hold a six-step plan, cannot notice that a step came back wrong, and cannot
    answer "what can you do" without being told which command to name. This can.

    Why the standard library rather than the provider's SDK: the project already
    carries Qt, ctranslate2, llama.cpp and uiautomation, and a voice assistant one
    `pip install` away from a broken venv is a voice assistant that stops talking.
    An HTTP POST and a JSON body are not worth a new dependency.
    """

    # The window this many seconds, so a wedged socket cannot hold the microphone.
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, api_key=None, model=None, base_url=None, timeout=None,
                 opener=None, retry_sleep=1.5, attempts=None, alternates=None):
        self.api_key = (api_key or config.cloud_key() or "").strip()
        self.model = str(model or config.CLOUD_DEFAULT)
        self.base_url = str(base_url or config.CLOUD_BASE_URL).rstrip("/")
        self.timeout = config.CLOUD_TIMEOUT_S if timeout is None else timeout
        self.attempts = config.CLOUD_ATTEMPTS if attempts is None else max(1, attempts)
        self._opener = opener or self._post
        self._retry_sleep = retry_sleep
        # The other models this key can call, quickest first - see
        # config.alternates_for. A rate limit on this provider is counted per model,
        # so the next model down the list is a way out that waiting is not.
        self.alternates = [str(name) for name in (alternates or [])]
        self.answered_by = self.model         # the model that wrote the last reply
        self.switches = 0                     # how many times a limit moved it
        self.last_error = None
        self.calls = 0

    @property
    def available(self):
        return bool(self.api_key)

    # ---------- the transport -------------------------------------------------

    def _post(self, url, payload):
        """One POST, returning the decoded body. Raises CloudError with the reason."""
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with diag.span("http"):
                response = urllib.request.urlopen(request, timeout=self.timeout)
            diag.mark("headers")
            with response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # The body carries the provider's own explanation, which is the useful
            # part. It never echoes the key back, but it is still only logged by
            # class name below - never spoken, never written into actions.log.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise CloudError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"network: {exc.reason}") from exc

    def _status_of(self, error):
        """The HTTP status inside a CloudError message, or 0."""
        match = re.match(r"HTTP (\d+)", str(error))
        return int(match.group(1)) if match else 0

    # ---------- talking to the model ------------------------------------------

    def complete(self, messages, max_tokens=400):
        """Raw text for a prompt the caller wrote. Raises CloudError."""
        if not self.available:
            raise CloudError("no API key")
        payload = {
            "model": self.model,
            "messages": messages,
            # `max_tokens` is deprecated on this API; the completion limit is the
            # one that still works, and the old name is now a 400 waiting to happen.
            "max_completion_tokens": max(1, int(max_tokens)),
            "temperature": 0.2,
        }
        # A thinking model has to be told to think briefly, or it spends the whole
        # completion budget doing it and answers with nothing. See
        # config.reasoning_controls - the families that want the fields, the ones that
        # reject them, and the measurement behind it.
        payload.update(config.reasoning_controls(self.model))
        data = self._call(payload)
        return self._text_of(data)

    def _call(self, payload):
        """Sends the payload, retrying while the failure is one worth retrying.

        Two kinds are. A 429 is ordinary free-tier traffic, and the limits are per
        minute, so waiting helps. And a status of 0 means there was no HTTP status at
        all - a dropped connection or a TLS handshake that stalled, both of which
        happen on an ordinary home network. This was written after watching the
        handshake time out twice in a row from the machine it runs on, so one retry
        was not enough; the wait doubles between attempts instead.

        A 401 or a 400 is not retried: a rejected key and a malformed request will
        both fail identically however many times they are sent.
        """
        url = self.base_url + "/chat/completions"
        delay = self._retry_sleep
        failure = None
        model = self.model
        for attempt in range(self.attempts):
            try:
                data = self._opener(url, dict(payload, model=model))
            except CloudError as exc:
                failure = exc
                status = self._status_of(exc)
                if status == 429 and self.alternates:
                    # Measured from this machine, waiting the limit out cost 12-14 s
                    # and the next model down the list answered in about 0.3 s.
                    model = self._step_aside()
                    continue
                retryable = status == 0 or status in self.RETRY_STATUS
                self._note_failure(exc, status, retry=bool(retryable and attempt + 1 < self.attempts))
                if not retryable or attempt == self.attempts - 1:
                    break
                time.sleep(delay)
                delay *= 2
                continue
            self.calls += 1
            self.answered_by = model
            return data
        self.last_error = str(failure)[:200]
        raise failure

    def _note_failure(self, error, status, retry=False):
        """Tells the diagnostics recorder that a call failed, and whether it was retried.

        Only the status and the exception's class reach the record: a provider error
        body can echo a request, and none of that belongs in a latency file.
        """
        trace = diag.current()
        if trace is not None:
            trace.error("llm", error, status=status, retry=retry,
                        timed_out=(status == 0 and "timed out" in str(error).lower()))

    def _step_aside(self):
        """The next model to ask after a rate limit. The user's own choice is only
        stepped aside for the one request - the next turn asks for it again, which is
        how a limit that lasts a minute stops mattering."""
        model = self.alternates.pop(0)
        self.switches += 1
        return model

    @staticmethod
    def _text_of(data):
        """The assistant's text from a chat response: `content`, and only `content`.

        A reasoning model answers with two fields. `content` is the reply; `reasoning`
        is its private working - "Need to say hello in three words. Probably \"Hello,
        world, friend!\"". Reading that out loud is worse than saying nothing, so it
        is never used here. When the reasoning eats the whole token budget `content`
        comes back empty, and an empty answer is reported as an error for the caller
        to retry or cover - which is the honest outcome, not a guess at what the
        model was thinking.
        """
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise CloudError("no choices in the reply")
        text = message.get("content") if isinstance(message, dict) else None
        if isinstance(text, str) and text.strip():
            return text
        raise CloudError("the reply carried no text")

    def _stream_lines(self, url, payload):
        """Yields text pieces from a streaming chat completion (server-sent events).

        The deltas arrive as `data: {...}` lines ending with `data: [DONE]`. Only
        `delta.content` is used: the reasoning deltas of a model that thinks before
        it speaks are not the answer, and reading them aloud was one of the ways the
        voice became nonsense.
        """
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "User-Agent": USER_AGENT},
            method="POST")
        try:
            with diag.span("http"):
                response = urllib.request.urlopen(request, timeout=self.timeout)
            diag.mark("headers")
            with response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        data = json.loads(body)
                    except ValueError:
                        continue
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            if diag.current() is not None:
                                diag.mark("first_token")
                            yield piece
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise CloudError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"network: {exc.reason}") from exc

    def stream(self, messages, max_tokens=400, on_delta=None):
        """Calls `on_delta(piece)` as the reply is written; returns the whole text.

        This is what makes the voice immediate: the caller can split the growing
        text into sentences and speak the first one while the rest is still being
        generated. A failure before anything arrived is retried once - a dropped
        connection at the start is ordinary - while a failure mid-reply is raised,
        because speaking half an answer twice is worse than admitting the loss.
        """
        if not self.available:
            raise CloudError("no API key")
        model = self.model
        payload = {"model": model, "messages": messages,
                   "max_completion_tokens": max(1, int(max_tokens)),
                   "temperature": 0.4, "stream": True}
        payload.update(config.reasoning_controls(model))
        url = self.base_url + "/chat/completions"
        for attempt in range(max(1, min(2, self.attempts))):
            collected = []
            try:
                for piece in self._stream_lines(url, payload):
                    collected.append(piece)
                    if on_delta is not None:
                        on_delta(piece)
            except CloudError as exc:
                trace = diag.current()
                if collected or attempt == 1:
                    if trace is not None:
                        trace.error("llm", exc, status=self._status_of(exc))
                    raise
                if trace is not None:
                    # It is about to be asked again: a retry is a fact about this
                    # request and is counted where it is decided.
                    trace.error("llm", exc, status=self._status_of(exc), retry=True)
                if self._status_of(exc) == 429 and self.alternates:
                    # Same reasoning as in _call: a spoken answer that arrives late is
                    # worth less than one from the next model, and the reply that was
                    # cut off before a single piece arrived says nothing yet.
                    model = self._step_aside()
                    payload = dict(payload, model=model)
                    payload.update(config.reasoning_controls(model))
                    continue
                time.sleep(self._retry_sleep)
                continue
            text = "".join(collected).strip()
            if not text:
                raise CloudError("the streamed reply carried no text")
            self.calls += 1
            self.answered_by = model
            return text
        raise CloudError("the stream never started")

    def ask(self, messages, max_tokens=400):
        """The planner's window onto the model. Named as Fallback names it."""
        return self.complete(messages, max_tokens)

    def choose(self, text, names):
        """Which of `names` the user meant. Works like Fallback.choose."""
        if not self.available:
            return None
        try:
            return choose_from_names(text, names, self.complete)
        except CloudError:
            return None

    def __call__(self, text):
        """The router's window onto the model: one command, or nothing."""
        if not self.available:
            return None, "cloud_unavailable"
        try:
            return propose(text, self.complete)
        except CloudError:
            return None, "cloud_error"

    def unload(self):
        """Nothing to free: the weights live on the provider's hardware."""
        return None
