"""The cloud brain: the request it sends, the reply it reads, and how it fails.

Nothing here touches the network. The transport is injected - that is the reason
`Cloud` takes an `opener` at all - so the request shape and every failure path can
be checked on a machine with no API key and no internet.
"""
import json
import time

import pytest

from core import config, guard
from core.llm import Cloud, CloudError, Fallback, build_brain
from core.planner import Agent, first_sentence
from core.runner import handle
from core.session import Session


# ---------- helpers ---------------------------------------------------------

def body(text, key="content"):
    """A chat-completions body carrying `text` in the named message field."""
    return {"choices": [{"message": {key: text}}]}


def opener_returning(value, calls=None):
    """An injected transport that records each call and returns `value`."""
    calls = [] if calls is None else calls

    def opener(url, payload):
        calls.append({"url": url, "payload": payload})
        return value if isinstance(value, dict) else body(value)

    return opener, calls


def opener_failing(errors, calls=None):
    """An injected transport that raises each error in turn, then succeeds."""
    calls = [] if calls is None else calls
    queue = list(errors)

    def opener(url, payload):
        calls.append({"url": url, "payload": payload})
        if queue:
            raise queue.pop(0)
        return body("recovered")

    return opener, calls


@pytest.fixture
def no_key(monkeypatch):
    """No key in the environment and no key file, whatever this machine has."""
    monkeypatch.setattr(config, "cloud_key", lambda: None)
    monkeypatch.setattr(config, "cloud_available", lambda: False)


# ---------- the request -----------------------------------------------------

def test_the_request_uses_the_shape_this_api_documented():
    opener, calls = opener_returning("done")
    cloud = Cloud(api_key="k", model="openai/gpt-oss-20b", opener=opener)
    cloud.complete([{"role": "user", "content": "hi"}], max_tokens=123)
    sent = calls[0]["payload"]
    assert sent["model"] == "openai/gpt-oss-20b"
    assert sent["max_completion_tokens"] == 123
    # `max_tokens` was deprecated and now 400s; sending both would be a bug.
    assert "max_tokens" not in sent
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert calls[0]["url"].endswith("/chat/completions")


def test_a_reasoning_model_is_asked_for_the_short_answer():
    opener, calls = opener_returning("done")
    Cloud(api_key="k", model="openai/gpt-oss-120b", opener=opener).complete([], max_tokens=10)
    assert calls[0]["payload"]["reasoning_effort"] == "low"


def test_the_short_answer_is_asked_for_hidden_as_well_as_low():
    """The thinking pass is charged to the same budget as the answer.

    With a tight budget and the provider's defaults, gpt-oss spent all of it thinking
    and answered with nothing - which reached the voice as "the reply carried no
    text". `reasoning_effort: "low"` is what ends the wait in about half a second,
    and `reasoning_format: "hidden"` keeps the working out out of the reply the voice
    reads from, so the two travel together.
    """
    opener, calls = opener_returning("done")
    Cloud(api_key="k", model="openai/gpt-oss-20b", opener=opener).complete([], max_tokens=10)
    assert calls[0]["payload"]["reasoning_effort"] == "low"
    assert calls[0]["payload"]["reasoning_format"] == "hidden"


def test_another_model_is_not_sent_the_reasoning_fields():
    opener, calls = opener_returning("done")
    Cloud(api_key="k", model="qwen/qwen3.8-27b", opener=opener).complete([], max_tokens=10)
    assert "reasoning_effort" not in calls[0]["payload"]
    assert "reasoning_format" not in calls[0]["payload"]


def test_a_family_that_rejects_the_reasoning_fields_never_receives_them():
    # allam-2-7b answers the pair with HTTP 400, so a blanket setting is a bug.
    assert config.reasoning_controls("allam-2-7b") == {}
    assert config.reasoning_controls("qwen/qwen3.8-27b") == {}
    assert config.reasoning_controls("openai/gpt-oss-120b") == {
        "reasoning_effort": "low", "reasoning_format": "hidden"}


def test_the_streamed_reply_asks_for_the_same_short_hidden_answer(monkeypatch):
    """The path the voice actually uses: one streamed request, read piece by piece."""
    import urllib.request
    seen = {}

    class Response:
        LINES = iter([b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n',
                      b'data: {"choices":[{"delta":{"content":"Paris"}}]}\n',
                      b'data: {"choices":[{"delta":{"content":"."}}]}\n',
                      b"data: [DONE]\n"])

        def __iter__(self):
            return self.LINES

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["payload"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cloud = Cloud(api_key="k", model="openai/gpt-oss-20b")
    pieces = []
    text = cloud.stream([{"role": "user", "content": "hi"}], on_delta=pieces.append)
    assert text == "Paris." and pieces == ["Paris", "."]
    assert seen["payload"]["stream"] is True
    assert seen["payload"]["reasoning_effort"] == "low"
    assert seen["payload"]["reasoning_format"] == "hidden"
    assert "thinking" not in "".join(pieces), "reasoning is never spoken"


def test_the_key_travels_as_a_bearer_header_and_nowhere_else(monkeypatch):
    import urllib.request
    seen = {}

    class Response:
        def read(self):
            return json.dumps(body("hi")).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["method"] = request.get_method()
        seen["agent"] = request.get_header("User-agent")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert Cloud(api_key="secret-value").complete([{"role": "user", "content": "hi"}]) == "hi"
    assert seen["auth"] == "Bearer secret-value"
    assert seen["method"] == "POST"
    assert seen["url"] == config.CLOUD_BASE_URL + "/chat/completions"
    # A bare urllib request is answered 403 "error code: 1010" by the CDN in front
    # of this API, so the agent string is load-bearing, not decoration.
    assert seen["agent"] and "Retro" in seen["agent"]


# ---------- reading the reply ----------------------------------------------

def test_the_answer_comes_from_the_message_content():
    opener, _ = opener_returning("open_app | notepad")
    assert Cloud(api_key="k", opener=opener).complete([]) == "open_app | notepad"


def test_the_models_private_reasoning_is_never_returned_as_an_answer():
    """`reasoning` is the model thinking out loud, and it must never be spoken.

    Seen on the real API: content empty, reasoning reading "Need to say hello in
    three words. Probably \"Hello, world, friend!\"". Say that aloud and it sounds
    broken, so an empty answer is reported as an error instead.
    """
    opener, _ = opener_returning({"choices": [{"message": {
        "content": "",
        "reasoning": "Need to say hello in three words. Probably \"Hello, world!\""}}]})
    with pytest.raises(CloudError):
        Cloud(api_key="k", opener=opener).complete([])


def test_a_line_falls_back_to_the_written_one_when_the_answer_is_empty():
    opener, _ = opener_returning({"choices": [{"message": {"content": "   ",
                                                          "reasoning": "thinking..."}}]})
    agent = Agent(complete=Cloud(api_key="k", opener=opener, attempts=1).ask,
                  conversational=True)
    assert agent.line("greeting", fallback="Yes?") == "Yes?"


@pytest.mark.parametrize("bad", [
    {"choices": []},
    {},
    {"choices": [{"message": {"content": "   "}}]},
])
def test_a_reply_with_no_text_is_an_error(bad):
    opener, _ = opener_returning(bad)
    with pytest.raises(CloudError):
        Cloud(api_key="k", opener=opener).complete([])


# ---------- failing ---------------------------------------------------------

def test_no_key_means_unavailable_and_a_clear_error(no_key):
    cloud = Cloud()
    assert not cloud.available
    with pytest.raises(CloudError):
        cloud.complete([])
    assert cloud([{"role": "user", "content": "hi"}]) == (None, "cloud_unavailable")


def test_a_busy_provider_is_retried_once_then_succeeds():
    opener, calls = opener_failing([CloudError("HTTP 429: rate limited")])
    cloud = Cloud(api_key="k", opener=opener, retry_sleep=0)
    assert cloud.complete([]) == "recovered"
    assert len(calls) == 2, "a 429 is normal free-tier traffic, not a failure"


def test_a_rejected_key_is_not_retried():
    opener, calls = opener_failing([CloudError("HTTP 401: invalid api key")])
    cloud = Cloud(api_key="k", opener=opener, retry_sleep=0)
    with pytest.raises(CloudError):
        cloud.complete([])
    assert len(calls) == 1, "a bad key will not fix itself"


def test_a_dropped_connection_is_retried():
    """No HTTP status at all means a network blip, which is worth trying again."""
    opener, calls = opener_failing([CloudError("network: [WinError 10061] refused")])
    cloud = Cloud(api_key="k", opener=opener, retry_sleep=0)
    assert cloud.complete([]) == "recovered"
    assert len(calls) == 2


def test_a_stalled_handshake_is_survived_when_the_third_attempt_works():
    """The exact failure seen on this machine: the TLS handshake times out twice."""
    stall = CloudError("network: _ssl.c:993: The handshake operation timed out")
    opener, calls = opener_failing([stall, stall])
    cloud = Cloud(api_key="k", opener=opener, retry_sleep=0)
    assert cloud.complete([]) == "recovered"
    assert len(calls) == 3


def test_it_gives_up_after_the_last_attempt_and_reports_why():
    stall = CloudError("network: _ssl.c:993: The handshake operation timed out")
    opener, calls = opener_failing([stall, stall, stall, stall])
    cloud = Cloud(api_key="k", opener=opener, retry_sleep=0)
    with pytest.raises(CloudError):
        cloud.complete([])
    assert len(calls) == 3, "it must stop rather than retry for ever"
    assert "handshake" in cloud.last_error


def test_the_failure_is_remembered_for_the_log_without_the_key():
    opener, _ = opener_failing([CloudError("HTTP 401: invalid api key")])
    cloud = Cloud(api_key="secret-value", opener=opener, retry_sleep=0)
    with pytest.raises(CloudError):
        cloud.complete([])
    assert "401" in cloud.last_error
    assert "secret-value" not in cloud.last_error


# ---------- choosing a brain ------------------------------------------------

def test_a_key_selects_the_cloud_and_no_key_selects_the_local_model(monkeypatch):
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    brain, is_cloud = build_brain("groq")
    assert is_cloud and isinstance(brain, Cloud)
    assert brain.model == config.CLOUD_MODELS["groq"]

    monkeypatch.setattr(config, "cloud_available", lambda: False)
    brain, is_cloud = build_brain("groq")
    assert not is_cloud and isinstance(brain, Fallback), "no key must not mean no assistant"


def test_an_exact_model_id_that_could_not_be_used_says_so_in_the_log(tmp_path, monkeypatch):
    """The picker carries real model ids now, so the reason has to reach the log for
    those too - not only for the nicknames an older build wrote into settings.json."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(config, "cloud_available", lambda: False)
    session = Session()
    session.model_choice = "qwen/qwen3.8-27b"
    session._load_brain()
    assert session.converse is False, "no key must not mean no assistant"
    lines = (tmp_path / "Logs" / "retro.log").read_text(encoding="utf-8").splitlines()
    assert any("cloud_unavailable" in line and "qwen/qwen3.8-27b" in line
               for line in lines)


def test_a_local_choice_does_not_blame_a_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(config, "cloud_available", lambda: False)
    session = Session()
    session.model_choice = "1b"
    session._load_brain()
    assert session.converse is False
    written = tmp_path / "Logs" / "retro.log"
    assert not written.exists() or "cloud_unavailable" not in written.read_text(
        encoding="utf-8")


def test_auto_prefers_the_cloud_but_still_runs_without_one(monkeypatch):
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    assert build_brain("auto")[1] is True
    monkeypatch.setattr(config, "cloud_available", lambda: False)
    assert build_brain("auto")[1] is False


def test_the_local_choices_never_reach_for_the_network(monkeypatch):
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    for choice in ("1b", "500m"):
        brain, is_cloud = build_brain(choice)
        assert not is_cloud and isinstance(brain, Fallback)


# ---------- what a real brain is allowed to do ------------------------------

def test_a_real_brain_answers_small_talk_instead_of_a_canned_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")

    def complete(messages, max_tokens=200):
        return "answer | Hi! What can I do for you?"

    planner = Agent(complete=complete, conversational=True)
    result = handle("hello", confirm=lambda q: False, planner=planner, converse=True)
    assert result.ok and "Hi!" in result.message


def test_the_local_brain_keeps_the_written_replies(tmp_path, monkeypatch, no_key):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")

    def complete(messages, max_tokens=200):
        pytest.fail("a 1B model must not be asked to answer small talk")

    planner = Agent(complete=complete, conversational=False)
    result = handle("hello", confirm=lambda q: False, planner=planner, converse=False)
    from core.voice import GREETING
    assert result.message == GREETING, "the offline brain gets the written line, not a model call"


def test_the_machine_protecting_refusals_survive_a_real_brain(no_key):
    assert "shut down" in guard.check("shutdown my laptop", only_hard=True)
    assert "delete" in guard.check("delete my downloads", only_hard=True)
    assert "install" in guard.check("install chrome", only_hard=True)
    assert "send messages" in guard.check("send an email to my boss", only_hard=True)


def test_small_talk_is_left_to_the_model_when_only_hard_rules_apply(no_key):
    assert guard.check("hello", only_hard=True) is None
    assert guard.check("what is the capital of France", only_hard=True) is None
    # ...and the written reply is still there for the local brain.
    assert guard.check("hello") is not None


def test_a_typed_rule_still_answers_with_no_model_call_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")

    def complete(messages, max_tokens=200):
        pytest.fail("the twenty typed rules must stay ahead of the network")

    planner = Agent(complete=complete, conversational=True)
    result = handle("what time is it", confirm=lambda q: False, planner=planner, converse=True)
    assert result.ok and result.message.startswith("It is")


# ---------- memory ----------------------------------------------------------

def test_the_agent_remembers_the_last_turns_and_drops_the_old_ones():
    seen = []

    def complete(messages, max_tokens=200):
        seen.append([m["content"] for m in messages])
        return "answer | ok"

    agent = Agent(complete=complete, conversational=True, history_turns=2)
    for word in ("one", "two", "three", "four"):
        agent.run(word)
    last = seen[-1]
    assert "one" not in last, "the oldest turn should have fallen off"
    assert "two" in last and "three" in last and "four" in last


def test_a_local_brain_is_never_given_history():
    agent = Agent(complete=lambda messages, max_tokens=200: "answer | ok",
                  conversational=False)
    agent.run("hello")
    assert agent.history == []


def test_forgetting_starts_a_new_conversation():
    agent = Agent(complete=lambda messages, max_tokens=200: "answer | ok",
                  conversational=True)
    agent.run("hello")
    assert agent.history
    agent.forget()
    assert agent.history == []


# ---------- lines it writes about itself ------------------------------------
# These are the lines that used to come off a fixed list - "Let me think about
# that", "Still on it", "Yes, what can I help you with?" - so the assistant read
# the same seven sentences for ever. With a real brain they are written now.

def test_a_line_is_written_for_the_moment_when_a_real_brain_is_present():
    seen = []

    def complete(messages, max_tokens=80):
        seen.append(messages)
        return "Righto - still picking at WhatsApp."

    agent = Agent(complete=complete, conversational=True)
    got = agent.line("working", detail="typing into WhatsApp", fallback="Still on it.")
    assert got == "Righto - still picking at WhatsApp."
    assert "typing into WhatsApp" in seen[0][0]["content"], \
        "the line must be about what is actually happening"


def test_the_written_line_is_used_when_there_is_no_real_brain():
    agent = Agent(complete=lambda *a, **k: pytest.fail("no model call expected"),
                  conversational=False)
    assert agent.line("greeting", fallback="Yes, what can I help you with?") == \
        "Yes, what can I help you with?"


def test_a_line_that_cannot_be_written_falls_back_instead_of_raising():
    def complete(messages, max_tokens=80):
        raise RuntimeError("network gone")

    agent = Agent(complete=complete, conversational=True)
    assert agent.line("greeting", fallback="Yes?") == "Yes?"


@pytest.mark.parametrize("raw,expected", [
    ('"Hello there!"', "Hello there!"),
    ("Retro: Hello there.", "Hello there."),
    ("- Hello there.", "Hello there."),
    ("1. Hello there.", "Hello there."),
    ("Hello there.\nSecond line.", "Hello there."),
    ("   ", ""),
    (None, ""),
])
def test_a_spoken_line_is_cleaned_of_what_models_wrap_it_in(raw, expected):
    assert first_sentence(raw) == expected


class StubSpeaker:
    """Records what would have been said, without a sound card.

    It has to answer the whole interface the session speaks through - `say` as well as
    `say_async`, and the two readings the microphone gate takes - because a line with
    no request behind it is now said through `say`, on the calling thread: a line
    handed to a thread nobody owns could be neither ordered nor stopped, and two of
    them were heard together.
    """

    def __init__(self, spoken):
        self.spoken = spoken
        self.disabled = False
        self.playing = False
        self.level = 0.0

    def say(self, text, cap=None):
        self.spoken.append(text)
        return True

    def say_async(self, text):
        return self.say(text)

    def stop(self):
        return True


def test_a_real_brain_says_nothing_while_it_thinks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    spoken = []
    session = Session()
    session.speaker = StubSpeaker(spoken)
    session.planner = Agent(complete=lambda messages, max_tokens=80: "On it.",
                            conversational=True)
    session.converse = True
    session._thinking()
    assert spoken == [], "a cloud answer lands in a moment; there is no wait to cover"
    session._speak_written("working", detail="opening notepad", fallback="Still on it.")
    for _ in range(200):
        if spoken:
            break
        time.sleep(0.02)
    assert spoken == ["On it."]


def test_without_a_real_brain_nothing_is_read_off_a_list(tmp_path, monkeypatch):
    """Starting a request used to speak "Let me think about that."

    That is the scripted assistant the user asked to be rid of: with no brain to
    write a line there is no spoken cover at all, and the written pools only remain
    for the moments that genuinely have no model behind them - a greeting after the
    wake word, the sleep reply - where the fallback is passed in explicitly.
    """
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    spoken = []
    session = Session()
    session.speaker = StubSpeaker(spoken)
    session._thinking()
    assert spoken == [], "the thinking noise must not come back"
    session._speak_written("greeting", fallback="Yes, what can I help you with?")
    assert spoken[-1] == "Yes, what can I help you with?"


# ---------- the listing the MODEL picker is built from ------------------------
#
# The picker is the user's own request: list what this key can call, and let them
# choose. So the listing is checked for what it does with a key, without one, with a
# network that fails, and for the order the rows come out in.

class FakeResponse:
    """Just enough of the urlopen context manager to carry a JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A listing cache in a temporary file, never this machine's real one."""
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", tmp_path / "groq.models.json")
    monkeypatch.setattr(config, "_listing", None)
    return config.MODEL_LIST_CACHE


def test_the_listing_is_fetched_once_and_read_back_from_the_cache(cache, monkeypatch):
    monkeypatch.setattr(config, "cloud_key", lambda: "k")
    monkeypatch.setattr(config, "urlopen", lambda *a, **k: FakeResponse(
        {"data": [{"id": "whisper-large-v3"}, {"id": "openai/gpt-oss-20b"}]}))
    ids, error = config.refresh_model_ids()
    assert error is None and ids == ["whisper-large-v3", "openai/gpt-oss-20b"]
    assert cache.exists(), "the next window must not need the network"
    monkeypatch.setattr(config, "_listing", None)          # as if the app had restarted

    def never(*args, **kwargs):
        raise AssertionError("a cached listing must not be re-fetched")

    monkeypatch.setattr(config, "urlopen", never)
    assert config.model_ids() == ["whisper-large-v3", "openai/gpt-oss-20b"]
    assert config.known_model_ids() == ["whisper-large-v3", "openai/gpt-oss-20b"]


def test_a_listing_that_cannot_be_fetched_is_not_a_crash(cache, monkeypatch):
    """The failure that broke the window: a network call that raises during startup."""
    monkeypatch.setattr(config, "cloud_key", lambda: "k")

    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(config, "urlopen", boom)
    ids, error = config.refresh_model_ids()
    assert ids == [] and "no network" in error
    assert not cache.exists()
    rows = config.model_choices()                           # must not raise
    assert rows and rows[0]["value"] == "auto"
    assert [r["value"] for r in rows if r["kind"] == "cloud"]


def test_a_broken_cache_file_is_simply_no_cache(cache, monkeypatch):
    cache.write_text("{ not json at all", encoding="utf-8")
    monkeypatch.setattr(config, "_listing", None)
    assert config.model_ids() == []
    assert config.model_choices()[0]["value"] == "auto"


def test_without_a_key_not_even_the_listing_is_requested(cache, monkeypatch):
    monkeypatch.setattr(config, "cloud_key", lambda: None)

    def never(*args, **kwargs):
        raise AssertionError("no key means no call")

    monkeypatch.setattr(config, "urlopen", never)
    ids, error = config.fetch_model_ids()
    assert ids == [] and error == "no API key"
    assert config.model_ids() == []


def test_every_model_the_key_can_call_is_offered_quickest_first(cache, monkeypatch):
    monkeypatch.setattr(config, "_listing", ["whisper-large-v3", "openai/gpt-oss-20b",
                                             "qwen/qwen3.8-27b",
                                             "canopylabs/orpheus-v1-english",
                                             "allam-2-7b"])
    rows = config.model_choices()
    assert [row["value"] for row in rows] == [
        "auto", "qwen/qwen3.8-27b", "allam-2-7b", "openai/gpt-oss-20b",
        "canopylabs/orpheus-v1-english", "whisper-large-v3", "1b", "500m"]
    # A model that transcribes or speaks is listed and marked unusable, rather than
    # hidden: the list is meant to be the truth about the key.
    assert [row["kind"] for row in rows] == ["auto", "cloud", "cloud", "cloud",
                                             "nonchat", "nonchat", "local", "local"]


def test_a_model_with_no_measurement_sorts_after_the_ones_that_have_one(cache, monkeypatch):
    monkeypatch.setattr(config, "_listing", ["vendor/brand-new-model", "allam-2-7b"])
    assert [row["value"] for row in config.model_choices()] == [
        "auto", "allam-2-7b", "vendor/brand-new-model", "1b", "500m"]


def test_every_built_in_row_has_a_measurement():
    # The ordering *is* the measurement, so a fallback row with no number in the table
    # would be sorted as though it were the slowest model on the key.
    for model_id in config.FALLBACK_MODELS:
        assert model_id in config.MEASURED_FIRST_WORD_S


def test_a_picker_value_resolves_to_the_model_that_is_called(cache, monkeypatch):
    monkeypatch.setattr(config, "_listing", ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"])
    # The literal id the row carries, in whatever case it was saved in.
    assert config.cloud_model_for("openai/gpt-oss-20b") == "openai/gpt-oss-20b"
    assert config.cloud_model_for("QWEN/QWEN3.8-27B") == "qwen/qwen3.8-27b"
    # The nicknames a build before this one wrote into settings.json.
    assert config.cloud_model_for("groq") == config.CLOUD_MODELS["groq"]
    assert config.cloud_model_for("groq-smart") == config.CLOUD_MODELS["groq-smart"]
    # AUTO means the quickest chat model on offer, and is not a model of its own.
    assert config.cloud_model_for("auto") == config.fastest_model() == "qwen/qwen3.8-27b"
    # The local choices are not cloud choices, so the caller falls through to the GGUF.
    assert config.cloud_model_for("1b") is None
    assert config.cloud_model_for("500m") is None
    assert config.cloud_model_for("vendor/nonsense") is None


def test_a_tie_inside_the_measured_spread_goes_to_the_preferred_model(cache, monkeypatch):
    """60 ms apart is one measurement, not one product apart.

    Sorting on the number alone made whichever model the listing happened to name
    first the assistant's default, which is how a 7B model could end up ahead of the
    27B one this project is developed against.
    """
    monkeypatch.setattr(config, "_listing", ["allam-2-7b", "qwen/qwen3.8-27b"])
    assert config.first_word_s("allam-2-7b") < config.first_word_s("qwen/qwen3.8-27b")
    assert config.first_word_s("qwen/qwen3.8-27b") - config.first_word_s("allam-2-7b") \
        <= config.TIE_WINDOW_S
    assert config.fastest_model() == "qwen/qwen3.8-27b"
    assert [row["value"] for row in config.model_choices()][:3] == [
        "auto", "qwen/qwen3.8-27b", "allam-2-7b"]


def test_a_model_outside_the_spread_never_wins_the_tie(cache, monkeypatch):
    monkeypatch.setattr(config, "_listing", ["vendor/brand-new-model", "allam-2-7b"])
    assert config.fastest_model() == "allam-2-7b"


# ---------- a rate limit is counted per model, so another model is the way out ---

def test_a_rate_limit_moves_to_the_next_model_instead_of_waiting():
    """Seen from this machine: waiting the limit out cost 12-14 s of a spoken turn."""
    opener, calls = opener_failing([CloudError("HTTP 429: rate limited")])
    cloud = Cloud(api_key="k", model="qwen/qwen3.8-27b", opener=opener, retry_sleep=0,
                  alternates=["openai/gpt-oss-20b", "allam-2-7b"])
    assert cloud.complete([]) == "recovered"
    assert calls[0]["payload"]["model"] == "qwen/qwen3.8-27b"
    assert calls[1]["payload"]["model"] == "openai/gpt-oss-20b"
    assert cloud.answered_by == "openai/gpt-oss-20b" and cloud.switches == 1
    # Only the one request steps aside: the next turn asks for the chosen model again.
    assert cloud.model == "qwen/qwen3.8-27b"
    assert cloud.alternates == ["allam-2-7b"]


def test_a_rate_limit_does_not_move_the_model_when_there_is_nowhere_to_move():
    opener, calls = opener_failing([CloudError("HTTP 429: rate limited")])
    cloud = Cloud(api_key="k", model="qwen/qwen3.8-27b", opener=opener, retry_sleep=0)
    assert cloud.complete([]) == "recovered"
    assert [call["payload"]["model"] for call in calls] == ["qwen/qwen3.8-27b"] * 2
    assert cloud.switches == 0 and cloud.answered_by == "qwen/qwen3.8-27b"


def test_a_bad_request_still_stops_rather_than_moving_to_another_model():
    # Only a rate limit steps aside. A 400 will fail identically on every model, and
    # moving would hide a real problem behind four more failures.
    opener, calls = opener_failing([CloudError("HTTP 400: bad model")])
    cloud = Cloud(api_key="k", model="qwen/qwen3.8-27b", opener=opener, retry_sleep=0,
                  alternates=["openai/gpt-oss-20b"])
    with pytest.raises(CloudError):
        cloud.complete([])
    assert len(calls) == 1 and cloud.switches == 0


def test_the_alternates_are_the_other_chat_models_quickest_first(cache, monkeypatch):
    monkeypatch.setattr(config, "_listing", ["whisper-large-v3", "openai/gpt-oss-20b",
                                             "qwen/qwen3.8-27b", "allam-2-7b"])
    assert config.alternates_for("qwen/qwen3.8-27b") == ["allam-2-7b",
                                                         "openai/gpt-oss-20b"]


def test_the_cloud_brain_carries_the_other_models_as_its_way_out(cache, monkeypatch):
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    monkeypatch.setattr(config, "_listing", ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"])
    brain, is_cloud = build_brain("auto")
    assert is_cloud and brain.model == "qwen/qwen3.8-27b"
    assert brain.alternates == ["openai/gpt-oss-20b"]


def test_auto_with_no_listing_still_means_a_real_model(cache, monkeypatch):
    # An empty cache on a first launch: AUTO has to resolve to something the key can
    # actually be asked for, which is what FALLBACK_MODELS is for.
    assert config.model_ids() == []
    chosen = config.cloud_model_for("auto")
    assert chosen in config.FALLBACK_MODELS
    assert config.is_chat_model(chosen)
