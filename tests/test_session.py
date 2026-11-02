"""The session, the UI event channel and the settings command, with no audio at all.

Nothing here loads Whisper, the model or a real voice: the session is given `None`
for each of them, which is exactly the state a machine without a microphone runs in.
"""
import time

from core import ui_hook
from core.runner import handle
from core.session import Session


def test_ui_hook_delivers_events():
    seen = []
    unsubscribe = ui_hook.subscribe(seen.append)
    ui_hook.emit("show_settings")
    assert seen == ["show_settings"]
    unsubscribe()
    ui_hook.emit("show_settings")
    assert seen == ["show_settings"], "an unsubscribed listener should stop hearing events"


def test_one_broken_listener_does_not_stop_the_others():
    def explode(_event):
        raise RuntimeError("boom")

    seen = []
    stop_bad = ui_hook.subscribe(explode)
    stop_good = ui_hook.subscribe(seen.append)
    ui_hook.emit("ping")
    stop_bad()
    stop_good()
    assert seen == ["ping"]


def test_open_settings_asks_the_window_to_show():
    seen = []
    unsubscribe = ui_hook.subscribe(seen.append)
    try:
        result = handle("Retro, open your settings")
    finally:
        unsubscribe()
    assert result.ok
    assert seen == ["show_settings"]


def test_a_refusal_reaches_the_user_and_runs_nothing():
    result = handle("laptop band karo")
    assert not result.ok
    assert "shut down" in result.message


def test_session_runs_a_typed_command_without_a_voice():
    states, replies = [], []
    session = Session(on_state=lambda state, detail="": states.append(state),
                      on_reply=lambda text, ok: replies.append((text, ok)))
    session.fallback = None
    session.ears = None
    session.speaker = None
    session.submit("what time is it")
    session.start_voice_loop()
    for _ in range(80):
        if replies:
            break
        time.sleep(0.05)
    session.stop()
    assert replies, "the typed command never ran"
    assert replies[0][1] is True
    assert replies[0][0].startswith("It is")
    assert "working" in states, "the display should have been told Retro was working"


class _Speaker:
    """Records what it was asked to say, without a sound card."""

    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)
        return True


def test_repeat_that_says_the_last_reply_again_word_for_word():
    """The line the owner missed already exists; re-planning it would change it."""
    session = Session()
    session.speaker = _Speaker()
    session._last_reply = "The battery is at 62 percent and charging."
    assert session.repeat_last() is True
    assert session.speaker.said == ["The battery is at 62 percent and charging."]


def test_repeat_that_works_even_when_the_line_was_just_said():
    """The duplicate guard exists for self-echo, not for a request from the owner."""
    session = Session()
    session.speaker = _Speaker()
    session._last_reply = "It is four o'clock."
    session._record_said("It is four o'clock.")
    assert session.repeat_last() is True
    assert session.speaker.said == ["It is four o'clock."]
    assert session.repeat_last() is True, "asking twice must work twice"
    assert len(session.speaker.said) == 2


def test_repeat_that_before_anything_was_said_says_so():
    session = Session()
    session.speaker = _Speaker()
    session._last_reply = ""
    session.repeat_last()
    assert session.speaker.said == ["I haven't said anything yet."]


def test_the_repeat_command_is_not_sent_to_the_model():
    """It is answered from the past, so no request runs and nothing is planned."""
    session = Session()
    session.speaker = _Speaker()
    session._last_reply = "The window is open."
    ran = []
    session.handle_text = lambda *args, **kwargs: ran.append(1)
    assert session._setting_command("repeat that") is True
    assert ran == []
    assert session.speaker.said == ["The window is open."]
