"""The microphone switch: off means the capture is closed, not merely ignored.

No real device is opened here. The listening call itself is stubbed, which is the
level the switch lives at: if the loop does not call it, then no stream is opened,
no block is sent to a recogniser and no transcription request is paid for. The
tests also hold the two promises that make the switch safe to use - typed commands
keep working, and a request or a reply already under way is not interrupted.
"""
import threading
import time

from core import settings
from core import session as session_module
from core.control import Task
from core.session import Session


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_with_the_microphone_off_no_listening_call_is_made(monkeypatch):
    """Off is no capture at all - not a stream whose audio is thrown away."""
    calls = []
    monkeypatch.setattr(session_module, "listen_once",
                        lambda *args, **kwargs: calls.append(1) or "")
    session = Session()
    session.ears = object()             # the ears exist; they are simply not used
    session.mic_on = False
    thread = session.start_voice_loop()
    try:
        time.sleep(0.5)
        assert calls == [], "the microphone was opened while the switch was off"
    finally:
        session.stop()
        thread.join(timeout=2)


def test_turning_it_off_releases_a_capture_already_in_progress(monkeypatch):
    """The real listener asks `interrupt` on every block, so off takes effect now."""
    state = {"started": None, "returned": None}

    def listen(ears, **kwargs):
        interrupt = kwargs["interrupt"]
        state["started"] = time.time()
        while not interrupt():
            time.sleep(0.01)
        state["returned"] = time.time()
        return ""

    monkeypatch.setattr(session_module, "listen_once", listen)
    heard = []
    session = Session(on_mic=lambda on: heard.append(on))
    session.ears = object()
    thread = session.start_voice_loop()
    try:
        assert _wait_for(lambda: state["started"] is not None), "the loop never listened"
        started = time.time()
        session.set_mic(False)
        assert time.time() - started < 0.3, "turning the switch off must not block"
        assert _wait_for(lambda: state["returned"] is not None, 1.0), \
            "the capture did not close when the microphone was switched off"
        assert state["returned"] >= state["started"]
        assert heard == [False], "the window must be told the real state"
    finally:
        session.stop()
        thread.join(timeout=2)


def test_switching_it_back_on_restores_the_wake_word_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(session_module, "listen_once",
                        lambda *args, **kwargs: calls.append(1) or "")
    session = Session()
    session.ears = object()
    session.mic_on = False
    thread = session.start_voice_loop()
    try:
        time.sleep(0.3)
        assert calls == []
        session.set_mic(True)
        assert _wait_for(lambda: len(calls) >= 2, 2.0), \
            "the loop did not go back to listening after the switch was turned on"
    finally:
        session.stop()
        thread.join(timeout=2)


def test_the_switch_is_remembered_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_cache", None)
    first = Session()
    assert first.mic_on is True, "the default is listening"
    first.set_mic(False)
    assert settings.get("mic") is False
    monkeypatch.setattr(settings, "_cache", None)
    second = Session()
    assert second.mic_on is False, "a microphone turned off stays off after a restart"


def test_off_does_not_cancel_a_request_or_the_voice():
    """The switch is an input control, not a stop button."""
    session = Session()
    task = Task("write a long document")
    session.current_task = task
    stopped = []
    session._cancel_speech = lambda: stopped.append(1)
    session.set_mic(False, save=False)
    assert stopped == [], "turning the microphone off must not cut a reply off"
    assert session.current_task is task, "the running request must survive the switch"


def test_typed_commands_still_run_with_the_microphone_off(monkeypatch):
    monkeypatch.setattr(session_module, "listen_once",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("the microphone was opened")))
    replies = []
    session = Session(on_reply=lambda text, ok: replies.append((text, ok)))
    session.ears = object()
    session.speaker = None
    session.fallback = None
    session.mic_on = False
    thread = session.start_voice_loop()
    try:
        session.submit("what time is it")
        assert _wait_for(lambda: bool(replies), 2.0), "the typed command never ran"
        assert replies[0][1] is True
        assert replies[0][0].startswith("It is")
    finally:
        session.stop()
        thread.join(timeout=2)


def test_the_on_state_is_reported_after_it_is_set():
    seen = []
    session = Session(on_mic=seen.append)
    assert session.set_mic(False, save=False) is False
    assert session.set_mic(True, save=False) is True
    assert seen == [False, True]
    assert session.mic_on is True


def test_the_wake_word_still_needs_to_be_said_after_switching_back_on():
    """Turning it back on does not silently open a follow-up conversation."""
    session = Session()
    session.followup = True
    session._follow_until = time.time() + 999
    session.set_mic(False, save=False)
    assert session._follow_until == 0.0
    session.set_mic(True, save=False)
    assert not session._stay_awake()


def test_only_one_listening_loop_can_run():
    """A second start returns the running loop instead of opening a second stream."""
    session = Session()
    session.mic_on = False                 # the loop idles without a device
    first = session.start_voice_loop()
    second = session.start_voice_loop()
    try:
        assert first is second, "two loops would mean two microphone streams"
        assert first.is_alive()
    finally:
        session.stop()
        first.join(timeout=2)


def test_the_chosen_microphone_is_remembered_by_name(monkeypatch, tmp_path):
    """An index does not survive a headset being plugged in; a name does."""
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(Session, "inputs",
                        lambda self: [(1, "Headset Mic"), (3, "Webcam Mic")])
    monkeypatch.setattr(session_module, "input_usable", lambda index: True)
    session = Session()
    assert session.set_input(3) == 3
    assert settings.get("mic_device") == "Webcam Mic", \
        "the name is what has to survive, not the index"
    monkeypatch.setattr(settings, "_cache", None)
    again = Session()
    assert again._input_device() == 3, "the remembered microphone was not chosen again"


def test_a_microphone_that_is_not_there_falls_back_to_the_system_default(monkeypatch):
    """A saved headset must not leave the assistant listening to nothing."""
    session = Session()
    session._mic_device = "Headset Mic"
    session._mic_index_ready = False
    monkeypatch.setattr(Session, "inputs", lambda self: [(0, "Webcam Mic")])
    assert session._input_device() is None


def test_a_microphone_that_cannot_be_opened_falls_back_to_the_default(monkeypatch):
    """Listed is not the same as usable: a device with the wrong rate is not chosen."""
    session = Session()
    session._mic_device = "Headset Mic"
    session._mic_index_ready = False
    monkeypatch.setattr(Session, "inputs", lambda self: [(2, "Headset Mic")])
    monkeypatch.setattr(session_module, "input_usable", lambda index: False)
    assert session._input_device() is None


def test_the_system_default_is_the_default_with_no_saved_microphone(monkeypatch):
    session = Session()
    session._mic_device = None
    session._mic_index_ready = False
    monkeypatch.setattr(Session, "inputs",
                        lambda self: (_ for _ in ()).throw(
                            AssertionError("the device list was queried for the default")))
    assert session._input_device() is None


def test_the_listening_call_is_given_the_chosen_microphone(monkeypatch):
    """The picker is pointless if the listener keeps opening the default stream."""
    seen = {}

    def listen(ears, **kwargs):
        seen["device"] = kwargs.get("device")
        return ""

    monkeypatch.setattr(session_module, "listen_once", listen)
    session = Session()
    session.ears = object()
    session._input_device = lambda: 7
    session._set_phase = lambda *args, **kwargs: None
    session._ask_brief()
    assert seen["device"] == 7, "the chosen microphone was not the one opened"
