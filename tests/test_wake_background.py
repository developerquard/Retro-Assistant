"""Grey and unloaded when asleep, three seconds to wake, and the room may be drawn.

Three things the owner asked for by name, each of which is a promise about *time* or
about *presence* rather than a detail:

* "Retro, sleep" leaves a still grey ball with no model held in memory;
* saying "Retro" again takes about three seconds of grey walking into blue, and only
  then is it ready - so the two copies of that number (the one the session sleeps for
  and the one the orb draws) have to be the same three seconds;
* "background voice hearing" lets the asleep orb come alive to whatever the machine
  is playing, without ever hearing a command.

Nothing here opens an audio device or loads a model: the sampler and the brain are
both replaced by stand-ins, because a test that captured the microphone would be a
test about the machine it happened to run on.
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import config, modes, settings, states  # noqa: E402
from core import session as session_module  # noqa: E402
from core.session import ASLEEP, Session  # noqa: E402


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    """Every setting written here lands in a temporary file, never the real one."""
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    settings.forget()
    yield
    settings.forget()


class FakeSampler:
    """Stands in for the WASAPI sampler: no test may open an audio device."""

    started = []

    def __init__(self, device=None, bands=12):
        self.device = device
        self.count = bands
        self.level = 0.4
        self.bands = [0.5] * bands
        self.available = True
        self.reason = "fake"

    def start(self):
        FakeSampler.started.append(self)
        return True

    def stop(self):
        return True


@pytest.fixture
def sampler(monkeypatch):
    FakeSampler.started = []
    monkeypatch.setattr("core.systemaudio.Sampler", FakeSampler)
    return FakeSampler


class StubSpeaker:
    """A speaker that says nothing itself and can be told it is mid-line."""

    def __init__(self, playing=False):
        self.playing = bool(playing)
        self.level = 0.5
        self.disabled = False
        self.device = None
        self.said = []

    def say(self, text, cap=None):
        self.said.append(text)
        return True

    def say_async(self, text):
        self.said.append(text)
        return True

    def stop(self):
        self.playing = False
        return True


class StubPlanner:
    def line(self, purpose, detail="", fallback=""):
        return f"Still on it: {detail}" if detail else fallback


def quiet_session(**kwargs):
    session = Session(**kwargs)
    session.fallback = None
    session.ears = None
    session.speaker = StubSpeaker()
    return session


# ---------- sleep --------------------------------------------------------------


def test_sleep_lets_go_of_the_brain_and_closes_the_conversation(sampler):
    """Sleep means unloaded - and, with background hearing on, still reacting.

    Asleep, the brain is let go of and the conversation is closed. What the owner
    turned background hearing on *for* is that the room keeps moving the picture while
    the assistant is quiet, so the sampler stays up with it: the setting is the switch,
    not the state. The other half of the rule is unchanged - the phase is `asleep` and
    it stays that way until it hears "Retro".

    It used to stop the sampler here, "because nothing may be drawn while asleep", and
    the effect was that turning background hearing on did nothing at all: the switch was
    on, the orb was still, and the only source of audio the visuals had was gone.
    """
    session = quiet_session()
    states_seen = []
    session._on_state = lambda state, detail="": states_seen.append((state, detail))
    session.loaded = True
    session.planner = None
    # The owner's rule: the room sampler is what the microphone-off state is *for*,
    # and the two captures cannot run together. So the switch is turned on here in the
    # mode it is meant for, rather than in the mode it is refused in.
    session.mic_on = False
    session.set_background(True, save=True)      # remembered; sampling, because awake
    session._follow_until = time.time() + 100
    assert session.audience is not None, \
        "background hearing samples the output while Retro is awake"

    session._sleep()

    assert session.loaded is False, "sleep means the model is not held in memory"
    assert session.planner is None, "and no conversation is kept either"
    assert session._follow_until == 0.0, "the conversation closes with it"
    assert session.phase == states.WAKE
    assert states.ui_state(session.phase) == "asleep", "the window must draw it asleep"
    assert session.audience is not None, \
        "background hearing is the one thing sleep does not switch off"
    assert session.sleeping is False, \
        "so the orb may answer the room - that is what the setting buys"
    assert states_seen and states_seen[-1][0] == "asleep"


def test_a_dozen_sleeps_and_wakes_still_leave_one_sampler(sampler):
    """Sleep and wake, over and over, must not accumulate streams.

    The switch now survives sleep, so every cycle starts and stops nothing: the same
    sampler is handed back rather than a second one being built, and the count of
    streams opened in the whole exercise is one. That matters because these are real
    WASAPI captures - a leak here is a leaked audio device, not a leaked object - and
    because the owner's report was of a second Retro appearing after a restart.
    """
    session = quiet_session()
    session.loaded = True
    session.mic_on = False                       # background hearing is mic-off work
    session.set_background(True, save=True)
    first = session.audience
    for _ in range(12):
        session._sleep()
        session.loaded = True                     # as the wake would leave it
        session._refresh_audience()
        assert session.audience is first, "each cycle must reuse the one sampler"
    assert len(sampler.started) == 1, \
        f"{len(sampler.started)} streams were opened for one session"
    session.set_background(False, save=True)
    assert session.audience is None, "and switching it off lets go of it"
    assert len(sampler.started) == 1


def test_sleep_without_background_hearing_leaves_the_room_alone(sampler):
    """The same sleep, with the switch off: quiet means quiet."""
    session = quiet_session()
    session.loaded = True
    session.background = False
    session._sleep()
    assert not sampler.started, "off means off: nothing reads the output"
    assert session.audience is None


def test_asleep_nothing_heard_in_the_room_may_move_the_orb(sampler):
    """The owner's rule: "it's UI just stays still UNTIL it hears retro".

    Asleep, the microphone is still being listened to for the wake word - there is
    nothing else to listen with - but the window must be told nothing about it. A
    sleeping circle that breathes with the room, or turns blue because a phase
    arrived late from a task that had already finished, looks like an assistant that
    is awake and ignoring you.
    """
    session = quiet_session()
    session.loaded = False
    session._state = ASLEEP
    assert session.sleeping is True

    seen, levels = [], []
    session._on_state = lambda state, detail="": seen.append(state)
    session._on_level = levels.append

    session._mic_level(0.9)                       # somebody talking in the room
    session._set_phase(states.SPEAKING, "speaking")     # a stale phase from a task
    session._set_phase(states.EXECUTING, "working")
    session._watch_once()

    assert levels == [], "nothing about the microphone may reach the window"
    assert seen == [], "and no colour may be drawn until it hears its name"


def test_the_wake_is_what_takes_it_out_of_standing_still(sampler):
    """The one way out of dormant, and it is the wake word - so the colour may walk."""
    session = quiet_session()
    session.loaded = False
    session._state = ASLEEP
    seen = []
    session._on_state = lambda state, detail="": seen.append(state)
    assert session._dormant() is True
    session._set_phase(states.LOADING, "waking up")
    assert session._dormant() is False, "a wake that has begun is not asleep"
    assert seen == ["loading"], "this is the three-second colour walk"


def test_a_typed_command_reaches_the_orb_even_while_asleep(sampler):
    """Standing still is a rule about the room, not about the owner.

    A line typed into the window needs no microphone and no wake word, so a sleeping
    assistant that swallowed its own answer's progress would look dead.
    """
    from core.control import Task

    session = quiet_session()
    session.loaded = False
    session._state = ASLEEP
    session.current_task = Task("typed")          # `_begin` has already run
    seen = []
    session._on_state = lambda state, detail="": seen.append(state)
    session._set_phase(states.EXECUTING, "working")
    assert seen == ["working"], "a request really in flight has the orb back"


def test_the_setting_that_was_switched_on_is_the_one_that_comes_back(sampler):
    session = quiet_session()
    session.set_background(True, save=True)
    assert settings.get("background") is True
    restarted = Session()                       # as if the app had been opened again
    assert restarted.background is True


# ---------- wake ----------------------------------------------------------------


def test_the_three_seconds_is_one_number_in_the_session_and_in_the_orb():
    agent_module = pytest.importorskip("ui.agent", reason="the orb needs PySide6")
    assert session_module.WAKE_LOAD_S == 3.0, "the owner asked for three seconds"
    assert agent_module.WAKE_LOAD_S == session_module.WAKE_LOAD_S, \
        "the session must wait for exactly the ramp the orb is drawing"


def test_waking_loads_the_brain_and_takes_the_whole_ramp(sampler, monkeypatch):
    session = quiet_session()
    seen = []
    session._on_state = lambda state, detail="": seen.append(state)
    session.loaded = False
    session.mic_on = False                      # the sampler runs with the mic off
    session.background = True
    session.audience = FakeSampler()            # what sleep left running
    loads = []

    def fake_load():
        loads.append(time.monotonic())
        session.loaded = True
        session.converse = False
        session.planner = None

    monkeypatch.setattr(session, "_load_brain", fake_load)
    monkeypatch.setattr(session_module, "WAKE_LOAD_S", 0.3)   # the real wait is slow

    started = time.monotonic()
    assert session._wake() is True
    took = time.monotonic() - started

    assert loads, "the brain was never loaded"
    # The load and the ramp run together and the slower of the two decides, so the
    # time the load itself took counts towards the three seconds rather than adding
    # to it - a margin for a clock that lands a hair short.
    assert took >= 0.3 - 0.05, f"the wake blinked in {took:.2f}s instead of walking its ramp"
    assert session.phase == states.LOADING
    assert states.ui_state(session.phase) == "loading", "this is the colour walk"
    # The sampler is kept running on purpose while Retro is awake: it is not only
    # the picture, it is also what gates the microphone - see the test below.
    assert session.audience is not None, \
        "the sampler is kept running while awake: it also gates the microphone"


def test_the_sampler_kept_running_is_what_ignores_what_the_machine_is_playing():
    """"Don't let it listen to media voices": while a video plays, a block has to be
    louder than the video to be transcribed at all.

    This only works while Retro is awake, which is why the sampler is no longer
    stopped by the wake: without it the recogniser was spent on a television's
    dialogue, and the word "Retro" was searched for in the middle of a song.
    """
    session = quiet_session()
    session.background = True
    session.speaker = None
    session.audience = FakeSampler()
    session.audience.level = 0.0                 # the machine is silent
    assert session._mic_gate() is None, "silence must not gate the microphone"
    session.audience.level = 0.4                 # something is playing, loudly
    gate = session._mic_gate()
    assert gate is not None and gate >= config.ROOM_GATE_FLOOR, \
        "a block has to beat the room to be transcribed"


def test_saying_retro_to_an_orb_that_is_already_awake_does_not_stall(monkeypatch):
    """Three seconds is a load, not a tax: already awake means ready now."""
    session = quiet_session()
    session.loaded = True
    monkeypatch.setattr(session_module, "WAKE_LOAD_S", 0.3)
    started = time.monotonic()
    assert session._wake() is True
    assert time.monotonic() - started < 0.25, "an awake orb must not be made to wait"


def test_one_retro_opens_the_conversation_for_three_minutes(sampler):
    session = quiet_session()
    session.set_followup(True, save=True)
    session.loaded = True
    assert config.CONVERSATION_WINDOW_S == 180.0, "the owner asked for three minutes"
    session._follow_until = 0.0                 # as if the window had closed itself
    assert session._stay_awake() is False
    session._wake()
    assert session._stay_awake() is True, "one 'Retro' is what opens it"
    left = session._follow_until - time.time()
    assert 170.0 <= left <= 181.0, f"the window is {left:.0f}s, not three minutes"


# ---------- background voice hearing -------------------------------------------


@pytest.mark.parametrize("phrase", [
    "background voice hearing", "background hearing on", "hear the background",
    "react to the music", "music visualiser", "visualiser on",
])
def test_the_background_phrases_are_understood(phrase):
    assert modes.parse_background(phrase) is True


@pytest.mark.parametrize("phrase", [
    "background voice hearing off", "stop background hearing",
    "music visualiser off", "stop the visualizer",
])
def test_the_background_phrases_are_understood_when_turning_off(phrase):
    assert modes.parse_background(phrase) is False


def test_a_sentence_that_merely_mentions_a_background_changes_nothing():
    assert modes.parse_background("open the background app") is None
    assert modes.parse_background("turn the music down") is None
    assert modes.parse_background("") is None


def test_the_switch_can_be_flipped_by_voice_and_is_remembered(sampler):
    session = quiet_session()
    assert session._setting_command("background voice hearing") is True
    assert session.background is True
    assert settings.get("background") is True
    assert session.speaker.said, "a setting that changes silently is a surprise"
    assert session._setting_command("stop background hearing") is True
    assert session.background is False
    assert settings.get("background") is False


def test_a_command_the_settings_do_not_know_is_not_swallowed(sampler):
    session = quiet_session()
    assert session._setting_command("open notepad") is False


# ---------- the five-second rule -----------------------------------------------


def test_a_status_line_waits_for_five_seconds_of_real_silence():
    session = quiet_session()
    assert config.FILLER_QUIET_S == 5.0, "the owner said five seconds"
    session._last_audible = time.monotonic()
    assert session.may_fill_silence() is False, "just answered: no line on top of it"
    session._last_audible = time.monotonic() - config.FILLER_QUIET_S + 0.5
    assert session.may_fill_silence() is False, "four and a half seconds is not five"
    session._last_audible = time.monotonic() - config.FILLER_QUIET_S - 0.5
    assert session.may_fill_silence() is True
    # ...but never in chat mode, where there is nothing being executed to report on.
    session.set_mode(modes.CHAT, save=False)
    assert session.may_fill_silence() is False, "chatting never needs a reminder"
    session.set_mode(modes.WORK, save=False)


def test_no_status_line_is_spoken_while_a_reply_is_still_coming_out():
    """The interruption itself: green, talking, and a line straight over the top."""
    from core.control import Task

    session = quiet_session()
    session.speaker = StubSpeaker(playing=True)
    session.converse = True
    session.planner = StubPlanner()
    task = Task("a long one")
    session.current_task = task
    task.detail = "opening spotify"
    task.did_work = True                        # a real step, not a spoken answer
    task.last_progress = 0.0                    # the step itself has been quiet a while

    session._watch_once()

    assert session.speaker.said == [], "never over a reply that is still being spoken"
    assert task.progress_lines == 0, "and not counted as one either"


def test_the_same_silence_does_get_covered_once_the_reply_has_ended():
    from core.control import Task

    session = quiet_session()
    session.converse = True
    session.planner = StubPlanner()
    task = Task("a long one")
    session.current_task = task
    task.detail = "opening spotify"
    task.did_work = True                        # a real step, not a spoken answer
    task.last_progress = 0.0
    session.speaker = StubSpeaker(playing=False)
    session._last_audible = time.monotonic() - config.FILLER_QUIET_S - 1.0

    session._watch_once()

    assert task.progress_lines == 1, "a five-second wait with a real detail is covered"
    assert session._last_progress_detail == "opening spotify"


# ---------- the window's side of it ---------------------------------------------


@pytest.fixture(scope="module")
def qt():
    pytest.importorskip("PySide6", reason="the window needs PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_panel_offers_the_switch_and_feeds_the_room_to_the_orb(qt, sampler):
    from ui.app import RetroWindow

    window = RetroWindow()
    try:
        assert window.bg_btn.isChecked() is False, "it is off until it is asked for"
        assert "BACKGROUND HEARING" in window.bg_btn.text()

        window.bg_btn.setChecked(True)          # the owner clicking it
        session = window.bridge.session
        assert session.background is True, "the button must reach the session"
        assert settings.get("background") is True, "and be remembered"
        assert window.bg_btn.text().endswith("ON")

        # Asleep, with the switch on: the frame keeps answering the room. That is what
        # the switch *means* - keep reacting while I am quiet - and it is the opposite of
        # what this test used to demand ("asleep is still: nothing is drawn"), because
        # tying it to the state is what made the switch do nothing at all.
        session._set_state("asleep", "say 'Retro'")
        window.agent.set_state("asleep")
        window._poll_room()
        assert window.agent.visual is True, \
            "background hearing is the switch, not the state: sleep does not stop it"
        assert window.agent._visual_level == pytest.approx(0.4), \
            "and the level on screen is still the machine's own audio"

        # The switch itself is the thing that stops it: off means off, asleep or awake.
        window.bg_btn.setChecked(False)
        assert session.background is False
        assert window.bg_btn.text().endswith("OFF")
        window._poll_room()
        assert window.agent.visual is False, "with the switch off, a sleeping orb is still"

        window.bg_btn.setChecked(True)          # and on again, awake, for the level check
        session._set_phase(states.IDLE, "listening for a command")
        window.agent.set_state("listening")
        window._poll_room()
        assert window.agent.visual is True, "awake and switched on: draw the room"
        assert window.agent._visual_level == pytest.approx(0.4)
    finally:
        window.close()


def test_the_window_syncs_a_switch_that_was_flipped_by_voice(qt, sampler):
    from ui.app import RetroWindow

    window = RetroWindow()
    try:
        window.bridge.session.set_background(True, save=True)
        window._sync_background_button()
        assert window.bg_btn.isChecked(), "a voice command must show on the panel"
        window.bridge.session.set_background(False, save=True)
        window._sync_background_button()
        assert not window.bg_btn.isChecked()
    finally:
        window.close()
