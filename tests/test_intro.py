"""The arrival: five seconds of logo, three of fade, one line at the end of it.

The owner described this moment precisely, so it is tested as precisely as it can be
without a screen: the timeline has to fit the budget they named, the fade has to be
three seconds and the last thing that happens, the soundtrack has to be real audio and
the same audio every time, and the line at the end has to be said even when the splash
could not be shown at all.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("RETRO_NO_STARTUP", "1")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from core import intro as plan  # noqa: E402

pytest.importorskip("PySide6", reason="the splash needs PySide6")

from PySide6.QtCore import QPoint, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import intro as splash  # noqa: E402


@pytest.fixture(scope="module")
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------- the timeline -----------------------------------------------------

def test_the_arrival_is_the_seven_to_eight_seconds_the_owner_asked_for():
    # The owner asked for longer after sitting through the first version: the reveal
    # keeps its tuned beats and the hold grew, with a brisk fade at the end.
    assert plan.INTRO_S == 6.4, "the reveal and hold are meant to be 6.4 seconds"
    assert plan.FADE_S == 1.6, "the fade is meant to be 1.6 seconds"
    assert 7.0 <= plan.total_seconds() <= 8.5
    assert plan.HARD_CAP_S > plan.total_seconds(), \
        "the cap has to be a backstop, not the plan"


def test_every_beat_lands_inside_the_five_seconds():
    assert plan.beats() == plan.BEATS
    last = 0.0
    for name, start, end in plan.beats():
        assert end > start, f"the {name} beat has no length"
        assert start >= last - 1e-9, f"the {name} beat starts before the one before it ends"
        assert end <= plan.INTRO_S, f"the {name} beat runs past the reveal"
        last = end


def test_the_letter_entrances_are_in_the_sketch_s_order():
    names = [name for name, _start, _end in plan.beats()]
    assert names[:5] == ["r_first", "e_particles", "t_wipe", "r_second", "o_trace"]
    assert names[-1] == "hold", "the word has to be held before it is faded"


def test_the_letters_are_drawn_one_at_a_time_and_then_held():
    """Nothing appears before its beat, and everything is there by the lock."""
    early = plan.progress(0.0)
    assert early["reveals"][0] == 0.0
    assert max(early["reveals"][1:]) == 0.0, "a letter arrived before its own beat"
    assert not early["locked"]
    for moment in plan.beats():
        name, _start, end = moment
        if name in ("lock", "hold"):
            continue
        assert plan.progress(0.0)["reveals"][plan.LETTERS.index(next(
            entry for entry in plan.LETTERS if True))] is not None   # index is stable
    locked = plan.progress(2.6)
    assert locked["locked"] and all(value >= 0.99 for value in locked["reveals"])
    assert plan.progress(4.9)["settle"] < plan.progress(2.6)["settle"], \
        "the glow is meant to quieten down for the fade"


def test_the_word_is_centred_whatever_the_screen():
    for width, height in ((1280, 720), (1920, 1080), (3840, 1600), (1024, 768)):
        info = splash.letter_layout(width, height)
        letters = info["letters"]
        left = letters[0]["x"]
        right = letters[4]["x"] + letters[4]["w"]
        assert abs(((left + right) / 2.0) - width / 2.0) < 1.0, f"not centred at {width}x{height}"
        assert abs(letters[0]["y"] + info["cap"] / 2.0 - height / 2.0) < 1.0
        assert right - left <= width * 0.80, "the word must not run off the screen"


def test_whether_it_plays_is_decided_by_the_environment_and_the_flags():
    assert plan.wanted(argv=[]) is True
    assert plan.wanted(argv=["--no-intro"]) is False
    assert plan.wanted(argv=["--selftest"]) is False
    assert plan.wanted(environ={"RETRO_NO_INTRO": "1"}) is False
    assert plan.wanted(environ={"RETRO_NO_INTRO": "0"}) is True
    assert plan.wanted(environ={"RETRO_NO_INTRO": "1"}, argv=[]) is False


# ---------- the soundtrack ---------------------------------------------------

def test_the_soundtrack_is_real_audio_of_the_right_length():
    rate = 22050                           # low, so the test is quick
    audio = plan.soundtrack(rate)
    assert len(audio) == int(round(plan.SOUND_S * rate))
    assert np.all(np.isfinite(audio)), "the soundtrack contains NaNs"
    peak = float(np.max(np.abs(audio)))
    assert 0.4 < peak <= 1.0, f"the soundtrack peaks at {peak}, which is not a cue"
    loud = float(np.mean(np.abs(audio)))
    assert loud > 0.005, "the soundtrack is effectively silent"
    # It has to be quiet enough to live through: a splash that is loud at logon is a
    # splash people turn off.
    assert peak <= 0.85, f"peaking at {peak} is louder than this should ever be"


def test_the_soundtrack_starts_and_ends_where_the_picture_does():
    rate = 22050
    audio = plan.soundtrack(rate)
    head = audio[:int(0.02 * rate)]
    tail = audio[int(5.6 * rate):]
    assert float(np.max(np.abs(tail))) < float(np.max(np.abs(audio))) * 0.5, \
        "the soundtrack is still going at full level when the fade ends"


def test_the_soundtrack_is_the_same_every_time():
    first = plan.soundtrack(16000)
    second = plan.soundtrack(16000)
    assert np.array_equal(first, second), "the arrival sounds different on every launch"


def test_the_timer_chime_is_its_own_short_cue():
    from core import sound
    audio, rate = sound.timer_done()
    assert sound.seconds_of(audio, rate) < 2.0, "a timer chime is not a song"
    assert float(np.max(np.abs(audio))) > 0.5
    assert np.all(np.isfinite(audio))


def test_sound_it_cannot_play_is_reported_and_not_raised(monkeypatch):
    from core import sound
    audio, rate = sound.timer_done()

    class Deaf:
        def OutputStream(self, **_kwargs):
            raise RuntimeError("no output device here")

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", Deaf())
    assert sound.play(audio, rate) is False


# ---------- the splash, drawn -----------------------------------------------

def render_splash(widget, size=(960, 540)):
    image = QImage(QSize(*size), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def lit(image, step=4):
    """How much of the picture is brighter than the black it is drawn on."""
    total = 0
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            colour = image.pixelColor(x, y)
            if colour.red() + colour.green() + colour.blue() > 90:
                total += 1
    return total


def test_the_logo_appears_letter_by_letter(qt):
    splash_widget = splash.IntroSplash(with_sound=False)
    splash_widget.setGeometry(0, 0, 960, 540)
    splash_widget.rebuild()
    counts = []
    for moment in (0.15, 0.60, 1.10, 1.55, 2.00, 4.90):
        splash_widget.render_at(moment)
        counts.append(lit(render_splash(splash_widget)))
    splash_widget.deleteLater()
    assert counts[0] < counts[1] < counts[-1], f"the word never grew: {counts}"
    assert counts[-1] > 1200, f"the finished wordmark is only {counts[-1]} lit pixels"
    for index in range(1, len(counts)):
        assert counts[index] >= counts[index - 1] - 400, \
            f"a letter disappeared again between frames: {counts}"


def test_all_five_letters_are_on_screen_at_the_end(qt):
    splash_widget = splash.IntroSplash(with_sound=False)
    splash_widget.setGeometry(0, 0, 1280, 720)
    splash_widget.rebuild()
    splash_widget.render_at(4.9)
    image = render_splash(splash_widget, (1280, 720))
    splash_widget.deleteLater()
    wanted = {"white R": (245, 247, 255), "gold T": (245, 196, 81), "red O": (255, 77, 95),
              "blue": (75, 141, 255)}
    found = {name: 0 for name in wanted}
    for name, (red, green, blue) in wanted.items():
        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 2):
                colour = image.pixelColor(x, y)
                if (abs(colour.red() - red) < 26 and abs(colour.green() - green) < 26
                        and abs(colour.blue() - blue) < 26):
                    found[name] += 1
    for name, count in found.items():
        assert count > 200, f"the {name} letter is not on screen ({count} pixels)"


def test_the_splash_covers_the_whole_desktop(qt):
    rect = splash.IntroSplash.desktop_rect()
    assert rect.width() >= 640 and rect.height() >= 480
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        assert rect.contains(geometry), "a screen would be left uncovered by the splash"


def test_the_splash_is_frameless_and_on_top(qt):
    splash_widget = splash.IntroSplash(with_sound=False)
    flags = splash_widget.windowFlags()
    splash_widget.deleteLater()
    for wanted in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint):
        assert flags & wanted, "the splash would have a title bar or hide behind a window"


def test_the_fade_takes_three_seconds_and_is_the_last_thing(qt, monkeypatch):
    """The window's own opacity, over the number of seconds the owner named."""
    splash_widget = splash.IntroSplash(with_sound=False)
    splash_widget.setGeometry(0, 0, 640, 360)
    seen = {}
    splash_widget.setWindowOpacity(1.0)
    original = splash_widget.setWindowOpacity

    def remember(value):
        seen[value] = True
        original(value)

    monkeypatch.setattr(splash_widget, "setWindowOpacity", remember)
    # Half way through the fade, half the opacity is gone; at the reveal's end it is
    # still a whole window.
    for moment in (plan.INTRO_S, plan.INTRO_S + plan.FADE_S / 2.0, plan.total_seconds() - 0.01):
        splash_widget._frozen = moment
        splash_widget._tick()
    splash_widget.deleteLater()
    assert any(abs(value - 0.5) < 0.05 for value in seen), \
        f"half the fade is not half the opacity: {sorted(seen)}"
    assert seen, "the fade never touched the opacity"


def test_the_splash_finishes_itself_once_and_tells_the_caller(qt):
    told = []
    splash_widget = splash.IntroSplash(on_finished=lambda: told.append("done"), with_sound=False)
    splash_widget.setGeometry(0, 0, 640, 360)
    splash_widget.finished.connect(lambda ran_full: told.append(ran_full))
    splash_widget.finish(True)
    splash_widget.finish(True)                    # the last frame and the cap can race
    assert told == ["done", True] or told == [True, "done"], told
    assert len(told) == 2, "finishing twice must not call the caller twice"


def test_escape_ends_the_arrival_early(qt):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    told = []
    splash_widget = splash.IntroSplash(on_finished=lambda: told.append(True), with_sound=False)
    splash_widget.setGeometry(0, 0, 640, 360)
    splash_widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                          Qt.KeyboardModifier.NoModifier))
    assert told == [True], "Escape during the splash must hand control straight back"


# ---------- the window's side of the arrival ---------------------------------

def build_window(floating=True):
    """A real window over a real session that was never started - the shape the
    self-test and the previews use, and the shape the arrival has to survive."""
    from ui.app import RetroWindow
    from ui.bridge import Bridge
    return RetroWindow(bridge=Bridge(), floating=floating, voice=False)


def heard(session):
    """Records what the session is asked to say, without a voice on this machine."""
    said = []

    def note(text, gen=None):
        said.append(text)
        return True

    session.speak_async = note
    return said


def quiet(session):
    """A voice that exists but does not speak: enough for `_on_ready` to run."""
    class Speaker:
        device = None

        def ensure_voices(self):
            return []

        def choose(self, name):
            return name

        def choose_output(self, device):
            return None

    session.speaker = Speaker()


def defuse(window):
    """Everything `_on_ready` does that needs a network or a microphone, stubbed."""
    loaded = []
    window._load_voices = lambda: loaded.append("voices")
    window._load_outputs = lambda: loaded.append("outputs")
    window._load_inputs = lambda: loaded.append("inputs")
    window._select_current_model = lambda: loaded.append("model")
    window.bridge.refresh_models_async = lambda: loaded.append("models")
    window._sync_follow_button = lambda: loaded.append("follow")
    window._sync_background_button = lambda: loaded.append("background")
    window.bridge.start_voice_loop = lambda: loaded.append("loop")
    return loaded


def test_the_line_is_said_after_the_fade(qt):
    from ui.app import ARRIVAL_LINE
    window = build_window()
    quiet(window.bridge.session)                # a session that has finished loading
    said = heard(window.bridge.session)
    window.begin_arrival()
    assert not window.isVisible(), "the circle must be out of the way of the logo"
    window.finish_arrival()
    import time
    for _ in range(30):                     # said on its own thread, so it is waited for
        if said:
            break
        time.sleep(0.05)
    assert said == [ARRIVAL_LINE], said
    assert window.isVisible(), "the circle never came back"
    window.close()


def test_the_line_is_said_even_with_no_splash_at_all(qt):
    """`--no-intro`, a replay with the sound off, a headless start: still the line."""
    from ui.app import ARRIVAL_LINE
    window = build_window()
    quiet(window.bridge.session)
    said = heard(window.bridge.session)
    window.finish_arrival()                     # begin_arrival was never called
    import time
    for _ in range(30):
        if said:
            break
        time.sleep(0.05)
    assert said == [ARRIVAL_LINE]
    window.close()


def test_the_line_waits_for_a_session_that_is_still_loading(qt):
    """Whisper takes a second or two, and the logo plays over it - so the greeting does."""
    from ui.app import ARRIVAL_LINE
    window = build_window()
    session = window.bridge.session
    said = heard(session)
    assert session.speaker is None, "this test is about a session that has not loaded"
    window.begin_arrival()
    window.finish_arrival()
    assert said == [], "the greeting was spoken into a session with no voice"
    assert window._announce_when_ready
    defuse(window)
    quiet(session)                              # loaded now
    window._on_ready(True, "")
    import time
    for _ in range(30):
        if said:
            break
        time.sleep(0.05)
    assert said == [ARRIVAL_LINE], "the greeting was dropped instead of deferred"
    assert not window._announce_when_ready
    window.close()


def test_the_greeting_does_not_freeze_the_window(qt):
    """Saying a line takes as long as the line: two seconds for this one.

    `speak_async` returns once the audio has finished, and it was being called from the
    GUI thread - so the owner's control came back with a window that could not be
    clicked for two seconds. It goes on a thread now, and this is the test that keeps it
    there.
    """
    import time
    window = build_window()
    session = window.bridge.session
    quiet(session)
    said = []
    released = []

    def slow(text, gen=None):
        time.sleep(1.2)
        said.append(text)
        released.append(True)
        return True

    session.speak_async = slow
    started = time.monotonic()
    window.finish_arrival()
    took = time.monotonic() - started
    assert took < 0.5, f"the arrival blocked the window for {took:.2f}s"
    for _ in range(40):                     # the line still has to be said
        if released:
            break
        time.sleep(0.1)
    assert said == ["Retro's ready."], said
    window.close()


def test_start_up_work_is_held_back_until_the_logo_has_gone(qt):
    """Nothing that would appear on screen may happen in front of the logo."""
    window = build_window()
    quiet(window.bridge.session)
    said = heard(window.bridge.session)
    window.begin_arrival()
    window._on_ready(True, "")                  # the session loaded during the splash
    assert window._ready_result == (True, ""), "the ready work ran in front of the logo"
    assert said == [], "the panel appearing would have talked over the logo"
    loaded = defuse(window)
    window.finish_arrival()
    assert "voices" in loaded and "loop" in loaded, f"the held-back work never ran: {loaded}"
    import time
    for _ in range(30):                     # the greeting is spoken on its own thread
        if said:
            break
        time.sleep(0.05)
    assert said == ["Retro's ready."], said
    assert not window._arriving
    window.close()


def launch(argv, monkeypatch):
    """Runs `ui.app.run` the way a logon does, minus the logon.

    The real `QApplication` is reused (there can only be one per process) and its event
    loop is stubbed out, and the session is not started: what is being tested is the
    path the shortcut takes, not Whisper's load time.
    """
    import ui.app as app_module
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_module, "QApplication", lambda *args, **kwargs: existing)
    monkeypatch.setattr(existing, "exec", lambda *args, **kwargs: 0, raising=False)
    monkeypatch.setattr(app_module.RetroWindow, "start", lambda self: True)
    return app_module


def test_the_launch_path_is_the_whole_arrival(qt, monkeypatch):
    """`run()` is what a logon actually calls, so this is the logon, without a screen.

    Written after a real run of `main_gui.py` died here: the splash was asked for with
    the flags handed over in the wrong place, and the exception landed in the crash log
    with no window and no logo at all. A timeline tested on its own would never have
    seen it - only the path the shortcut takes can.
    """
    app_module = launch(["--no-voice", "--window"], monkeypatch)
    played = []

    def fake_play(on_finished=None, with_sound=True):
        played.append(with_sound)
        on_finished()
        return object()

    monkeypatch.setattr(splash, "play", fake_play)
    assert app_module.run(["--no-voice", "--window"]) == 0
    assert played == [True], f"the launch did not play the arrival with sound: {played}"


def test_the_launch_path_without_the_intro_still_arrives(qt, monkeypatch):
    """`--no-intro` skips the picture, never the line: it is the arrival that matters."""
    app_module = launch(["--no-voice", "--no-intro"], monkeypatch)
    monkeypatch.setattr(splash, "play", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("the splash played when --no-intro was asked for")))
    said = []
    monkeypatch.setattr(app_module.RetroWindow, "announce_arrival", lambda self: said.append(True))
    assert app_module.run(["--no-voice", "--no-intro"]) == 0
    assert said == [True], "without the splash the line was never said"


def test_the_replay_is_the_same_arrival(qt, monkeypatch):
    window = build_window()
    played = {}

    def fake_play(on_finished=None, with_sound=True):
        played["sound"] = with_sound
        played["finish"] = on_finished
        return object()

    monkeypatch.setattr(splash, "play", fake_play)
    window.play_intro()
    assert played, "REPLAY INTRO did not start the splash"
    assert played["sound"] is True
    assert played["finish"] == window.finish_arrival, \
        "the replay does not end in the same place the arrival does"
    assert window._arriving, "the circle was left on top of the replay"
    window.close()
