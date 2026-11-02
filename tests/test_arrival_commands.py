"""What the arrival can be asked to do: replay itself, start with Windows, chime.

The three ways in are the same three the owner will use - a typed or spoken sentence,
the tray, and the panel - and they all end in the same places: `ui_hook`'s events for
anything the window owns, and `core.startup` for the registry entry. Tested by asking
for each of them, and by checking the one case that must not happen: an offscreen run
touching a real machine's startup list.
"""
import os

os.environ.setdefault("RETRO_NO_STARTUP", "1")

import pytest

from core import actions, intro as plan, runtime, settings, startup, ui_hook
from core.router import Ambiguous, Match, route


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    settings.forget()
    yield
    settings.forget()


# ---------- the words -------------------------------------------------------

@pytest.mark.parametrize("spoken", [
    "play your intro", "Retro, play your intro", "show your logo again",
    "play the logo", "logo reveal",
])
def test_the_intro_can_be_asked_for_out_loud(spoken):
    matched = route(spoken)
    assert isinstance(matched, Match) and not isinstance(matched, Ambiguous), \
        f"{spoken!r} was not understood as one thing: {matched}"
    assert matched.action.name == "play_intro"


@pytest.mark.parametrize("spoken", [
    "start with windows", "run at startup", "open at startup", "start when i log in",
])
def test_starting_with_windows_can_be_turned_on_out_loud(spoken):
    matched = route(spoken)
    assert isinstance(matched, Match), matched
    assert matched.action.name == "startup_on", f"{spoken!r} -> {matched.action.name}"


@pytest.mark.parametrize("spoken", [
    "don't start with windows", "do not start with windows", "remove from startup",
    "stop starting with windows",
])
def test_and_turned_off_without_being_read_as_turning_it_on(spoken):
    """`don't start with windows` *contains* `start with windows`, so the longer
    phrase has to win - the router scores by length for exactly this reason."""
    matched = route(spoken)
    assert isinstance(matched, Match), matched
    assert matched.action.name == "startup_off", f"{spoken!r} -> {matched.action.name}"


def test_a_sentence_that_merely_mentions_windows_starts_nothing():
    assert route("close all the windows on my screen") is None


def test_the_status_can_be_asked_for():
    matched = route("startup status")
    assert isinstance(matched, Match) and matched.action.name == "startup_status"


# ---------- what the words do ------------------------------------------------

def test_playing_the_intro_asks_the_window():
    seen = []
    unsubscribe = ui_hook.subscribe(seen.append)
    try:
        result = actions.run_play_intro({})
    finally:
        unsubscribe()
    assert "play_intro" in seen, f"the window was never asked: {seen}"
    assert result.ok and "intro" in result.message.lower()


def test_the_bridge_carries_that_ask_to_the_window():
    from ui.bridge import Bridge
    bridge = Bridge()
    seen = []
    bridge.introRequested.connect(lambda: seen.append(True))
    ui_hook.emit("play_intro")
    assert seen == [True], "the event did not reach the window's signal"
    bridge.stop()


def test_turning_startup_on_then_off_is_remembered(monkeypatch):
    calls = []
    monkeypatch.setattr(startup, "enable", lambda winreg=None: (calls.append("on"), (True, "ok"))[1])
    monkeypatch.setattr(startup, "disable", lambda winreg=None: (calls.append("off"), (True, "ok"))[1])
    assert actions.run_startup_on({}).ok
    assert settings.get("startup") is True
    assert actions.run_startup_off({}).ok
    assert settings.get("startup") is False
    assert calls == ["on", "off"]


def test_a_refused_startup_change_is_not_recorded_as_a_choice(monkeypatch):
    monkeypatch.setattr(startup, "enable", lambda winreg=None: (False, "could not add Retro to startup"))
    result = actions.run_startup_on({})
    assert not result.ok and "could not add" in result.message
    assert settings.get("startup") is None, "a failure was remembered as a decision"


def test_the_status_answers_from_the_registry(monkeypatch):
    monkeypatch.setattr(startup, "status", lambda winreg=None: ("opens with Windows", True))
    result = actions.run_startup_status({})
    assert result.ok and result.verified and "opens with Windows" in result.message

    monkeypatch.setattr(startup, "status",
                        lambda winreg=None: ("registered to a path that is not this one", False))
    result = actions.run_startup_status({})
    assert "not this one" in result.message, \
        "a stale entry has to be reported as stale, not as success"


# ---------- the launch path --------------------------------------------------

def test_an_offscreen_run_never_touches_the_startup_list(monkeypatch):
    """The self-test, the previews and the test suite all run offscreen."""
    from ui.app import apply_startup
    called = []
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv(startup.OPT_OUT, raising=False)
    monkeypatch.setattr(startup, "enable", lambda winreg=None: (called.append(True), (True, "ok"))[1])
    assert apply_startup([]) is None
    assert called == [], "an offscreen run registered itself in a real registry"
    assert settings.get("startup") is None


def test_the_opt_out_environment_variable_is_honoured(monkeypatch):
    from ui.app import apply_startup
    called = []
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setenv(startup.OPT_OUT, "1")
    monkeypatch.setattr(startup, "enable", lambda winreg=None: (called.append(True), (True, "ok"))[1])
    assert apply_startup([]) is None
    assert called == []


def test_the_first_real_run_registers_and_the_decision_sticks(monkeypatch):
    from ui.app import apply_startup
    called = []
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv(startup.OPT_OUT, raising=False)
    monkeypatch.setattr(startup, "ensure",
                        lambda choice, winreg=None: (called.append(choice), (True, "I'll open with Windows"))[1])
    assert apply_startup([]) is True
    assert called == [True], "the first run did not register"
    assert settings.get("startup") is True

    called.clear()
    apply_startup([])                      # second run: the decision is already made
    assert called == [True], "the second run asked again"


def test_no_startup_flag_takes_it_back_out(monkeypatch):
    from ui.app import apply_startup
    called = []
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv(startup.OPT_OUT, raising=False)
    monkeypatch.setattr(startup, "ensure",
                        lambda choice, winreg=None: (called.append(choice), (True, "gone"))[1])
    apply_startup(["--no-startup"])
    assert called == [False]
    assert settings.get("startup") is False


def test_the_arrival_can_be_switched_off_in_settings():
    """`--intro` still plays it: the flag is for showing somebody, not for a policy."""
    assert settings.get_bool("intro", True) is True
    settings.save(intro=False)
    assert settings.get_bool("intro", True) is False
    settings.save(intro_sound=False)
    assert settings.get_bool("intro_sound", True) is False
    settings.save(intro=True)
    assert settings.get_bool("intro", True) is True
    assert plan.wanted(argv=["--intro"]) is True


def test_a_switch_written_by_hand_is_read_as_what_it_says(tmp_path):
    import json
    settings.PATH.write_text(json.dumps({"intro": "off", "intro_sound": "ON"}), encoding="utf-8")
    settings.forget()
    assert settings.get_bool("intro", True) is False
    assert settings.get_bool("intro_sound", True) is True


# ---------- the timer --------------------------------------------------------

class Chime:
    def __init__(self):
        self.joined = []

    def join(self, timeout=None):
        self.joined.append(timeout)


def test_a_finished_timer_chimes_and_says_so(monkeypatch):
    """The timer used to print to a console the real app does not have."""
    from core import sound
    played = []
    said = []

    class Session:
        def say_async(self, text, gen=None):
            said.append(text)
            return True

    chime = Chime()
    monkeypatch.setattr(sound, "cue", lambda name: (played.append(name), chime)[1])
    monkeypatch.setattr(runtime, "session", lambda: Session())
    actions._timer_done(5)
    assert played == ["timer_done"], "no chime was played"
    assert chime.joined == [1.0], "the words were spoken over the chime"
    assert said and "5-minute timer is done" in said[0], said


def test_a_timer_still_works_with_no_assistant_running(monkeypatch, capsys):
    from core import sound
    monkeypatch.setattr(sound, "cue", lambda name: Chime())
    monkeypatch.setattr(runtime, "session", lambda: None)
    actions._timer_done(3)
    assert "3-minute timer is done" in capsys.readouterr().out


def test_a_broken_sound_device_does_not_lose_the_timer(monkeypatch):
    from core import sound

    def explode(_name):
        raise RuntimeError("no output device here")

    monkeypatch.setattr(sound, "cue", explode)
    monkeypatch.setattr(runtime, "session", lambda: None)
    actions._timer_done(1)                  # must not raise
