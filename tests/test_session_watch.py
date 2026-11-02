"""The assistant must never latch: green and deaf until the app is restarted.

That was the real bug. Speech ran on the voice loop's thread, so one wedged
`runAndWait()` - or an output device that stopped consuming - froze the whole
session: the ring stayed green, the microphone never came back, and the only way
out was to restart Retro. These tests pin down both halves of the fix: a line that
never comes back is abandoned, and a state held with no request behind it is
cleared by the watchdog.
"""
import threading
import time

import pytest

from core import config, voice
from core.control import Stopped, Task
from core.model import Result
from core.session import LISTENING, SPEAKING, WORKING, Session


class CountingSpeaker:
    """A speaker that finishes instantly and can be told to wedge."""

    def __init__(self, wedge=False):
        self.wedged = bool(wedge)
        self.said = []
        self.disabled = False
        self.playing = False
        self.level = 0.0
        self.last_peak = 0.5

    def say(self, text, cap=None):
        if self.wedged:
            time.sleep(60)                    # what runAndWait() does when it hangs
        self.said.append(text)
        return True

    def say_async(self, text):
        return self.say(text)

    def stop(self):
        self.wedged = False
        return True


class HangingSpeaker(CountingSpeaker):
    def say(self, text, cap=None):
        raise AssertionError("the session must not call an unbounded say() and wait")


def wait_for(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def bare_session(**kwargs):
    session = Session(**kwargs)
    session.fallback = None
    session.ears = None
    return session


def test_a_line_that_never_finishes_is_abandoned_and_the_next_one_still_plays(monkeypatch):
    """The cap, on the real Speaker: a wedged line must not be waited for for ever."""
    monkeypatch.setattr(voice, "SPEECH_FLOOR_S", 0.4)
    monkeypatch.setattr(voice, "SPEECH_PER_CHAR_S", 0.0)
    speaker = voice.Speaker()
    monkeypatch.setattr(speaker, "_do", lambda job, result: time.sleep(60))
    started = time.time()
    assert speaker.say("hello") is False
    assert time.time() - started < 5.0, "say() waited for a line that will never finish"
    assert speaker.wedges == 1
    assert not speaker.disabled, "one stuck line should not silence the voice for good"


def test_speech_giving_up_three_times_stops_trying(monkeypatch):
    monkeypatch.setattr(voice, "SPEECH_FLOOR_S", 0.2)
    monkeypatch.setattr(voice, "SPEECH_PER_CHAR_S", 0.0)
    speaker = voice.Speaker()
    monkeypatch.setattr(speaker, "_do", lambda job, result: time.sleep(60))
    for _ in range(3):
        speaker.say("hello")
    assert speaker.disabled, "after repeated wedges, stop pretending there is a voice"
    assert speaker.say("hello") is False


def test_two_lines_in_a_row_both_play(monkeypatch):
    """The bug that started all of this: the second spoken line never returned.

    pyttsx3's second `runAndWait()` on one engine blocks for ever - reproduced in
    tools/tts_probe.py - and that is what left Retro green and deaf. Speech goes to
    SAPI directly now, and this is the test that says so.
    """
    pytest.importorskip("comtypes")
    speaker = voice.Speaker()
    try:
        if not speaker.ensure_voices():
            pytest.skip("no SAPI voice installed here")
    except Exception as exc:
        pytest.skip(f"no speech engine here ({type(exc).__name__})")
    peaks = []
    for line in ("Opened notepad.", "Stopped.", "Let me think about that."):
        assert speaker.say(line, cap=20.0) is True, f"{line!r} did not play"
        peaks.append(speaker.last_peak)
    assert speaker.wedges == 0, "a run of ordinary lines must not be written off as wedged"
    assert min(peaks) > 0.01, f"one of the lines was silent: {peaks}"


def test_rendering_a_line_writes_real_audio(tmp_path):
    """Rendered straight to a file, with no speakers involved."""
    pytest.importorskip("comtypes")
    speaker = voice.Speaker()
    path = tmp_path / "line.wav"
    try:
        speaker.save_to_file("Retro is speaking into a file.", path)
    except Exception as exc:
        pytest.skip(f"no speech engine here ({type(exc).__name__})")
    assert path.exists() and path.stat().st_size > 1000, "the WAV has no audio in it"


def test_stop_cuts_a_line_off_mid_sentence():
    speaker = CountingSpeaker()
    speaker.wedged = True
    threading.Thread(target=lambda: speaker.say("a long line"), daemon=True).start()
    time.sleep(0.1)
    assert speaker.stop() is True
    assert speaker.said == [], "a stopped line must not be reported as spoken"


def test_a_wedged_reply_leaves_the_session_listening():
    """The regression: speaking used to hold the voice loop for ever."""
    states, replies = [], []
    session = bare_session(on_state=lambda s, d="": states.append(s),
                           on_reply=lambda t, ok: replies.append((t, ok)))
    session.speaker = HangingSpeaker(wedge=False)
    session.submit("what time is it")
    session.start_voice_loop()
    assert wait_for(lambda: replies), "the typed command never ran"
    assert wait_for(lambda: session.state == LISTENING), \
        f"the session stayed in {session.state!r} instead of listening again"
    session.stop()
    assert states[-1] == LISTENING


def test_the_watchdog_clears_a_state_that_was_held_with_no_request_behind_it():
    session = bare_session(state_stuck_s=0.3)
    session.speaker = CountingSpeaker()
    session._start_watcher()
    session._set_state(WORKING, "working")
    assert wait_for(lambda: session.state == LISTENING, timeout=4), \
        "a stuck state must be recovered without restarting the app"
    session.stop()


def test_the_watchdog_cancels_a_request_that_runs_past_its_limit():
    """A request that hangs is cancelled, and the session takes commands again."""
    session = bare_session(task_timeout_s=0.6, progress_every_s=30)
    session.speaker = CountingSpeaker()
    session.handle_text = lambda text, task=None: (time.sleep(30), Result(True, "done"))[1]
    session._start_watcher()
    task = session.dispatch("open spotify and play sunflower")
    assert task is not None
    assert wait_for(lambda: task.cancelled, timeout=4), "the runaway request was not cancelled"
    assert wait_for(lambda: session.current_task is None and session.state == LISTENING, timeout=4)
    session.stop()


class StubPlanner:
    """A brain that writes lines, so the watchdog's own rule can be tested."""

    def line(self, purpose, detail="", fallback=""):
        return f"Still on it: {detail}" if detail else fallback


def test_the_watchdog_says_nothing_when_there_is_nothing_new():
    """No timer, no rotation, no "I am still working on it" - twice.

    The line that made the user angry was said on a clock. Now: with no brain to
    write one there is nothing spoken at all, and even with a brain the same step
    reported twice is not news.
    """
    session = bare_session(progress_every_s=0.3, task_timeout_s=30)
    said = []
    session.speaker = CountingSpeaker()
    session.speaker.say_async = said.append
    task = Task("a long one")
    session.current_task = task
    session._watch_once()
    assert said == [], "an offline brain must not read a filler line"
    assert task.progress_lines == 0


def test_the_watchdog_speaks_once_per_real_change():
    """A long step still gets cover - written for the step, and only when it changed.

    Asserted on what the speaker was asked to *say* rather than on which of its
    methods was used: the line no longer goes out through `say_async`, because a line
    handed to a thread nobody owns cannot be ordered or stopped, and two of them were
    heard together.
    """
    session = bare_session(progress_every_s=0.3, task_timeout_s=30)
    speaker = CountingSpeaker()
    session.speaker = speaker
    session.converse = True
    session.planner = StubPlanner()
    session._start_watcher()
    task = Task("a long one")
    session.current_task = task
    task.detail = "opening spotify"
    task.did_work = True              # a real step ran: that is what may be covered
    assert wait_for(lambda: speaker.said, timeout=4), \
        "no line was spoken while the request was quiet"
    assert task.progress_lines == 1
    task.last_progress = 0.0          # quiet again, with the same detail
    session._watch_once()
    assert task.progress_lines == 1, "the same detail is not news twice"
    task.finish()
    session.stop()


def test_a_question_being_answered_is_never_covered_by_a_reminder():
    """The owner's correction: a big answer is not execution.

    A request answered by a `answer` step (or by the chat route) has nothing running
    on the machine to report, so a "still working on your request" line in the middle
    of it is an interruption with no information in it. A real step still earns the
    cover - the other tests hold that half.
    """
    session = bare_session(progress_every_s=0.3, task_timeout_s=30)
    speaker = CountingSpeaker()
    session.speaker = speaker
    session.converse = True
    session.planner = StubPlanner()
    task = Task("what is quantum tunnelling")
    session.current_task = task
    task.detail = "answer quantum tunnelling"   # a real step ran, but only spoke
    task.last_progress = 0.0
    session._last_audible = time.monotonic() - 30.0
    session._watch_once()
    assert speaker.said == [], "an answer is not work to report on"
    assert task.progress_lines == 0
    task.did_work = True                        # the same wait, after a real step
    session._watch_once()
    assert task.progress_lines == 1, "a real step still gets its cover"


def test_chat_mode_never_speaks_a_reminder():
    """Chat mode is conversation: the line has no use there, in as many words."""
    from core import modes

    session = bare_session(progress_every_s=0.3, task_timeout_s=30)
    speaker = CountingSpeaker()
    session.speaker = speaker
    session.converse = True
    session.planner = StubPlanner()
    session.set_mode(modes.CHAT, save=False)
    task = Task("open notepad")
    session.current_task = task
    task.detail = "open notepad"
    task.did_work = True
    task.last_progress = 0.0
    session._last_audible = time.monotonic() - 30.0
    assert session.may_fill_silence() is False
    session._watch_once()
    assert speaker.said == [], "chat mode may not remind anyone of anything"
    assert task.progress_lines == 0


def test_a_worker_keeps_the_loop_free_to_listen():
    """dispatch() must return at once, even while the request is still running."""
    session = bare_session()
    session.speaker = CountingSpeaker()
    started = threading.Event()
    release = threading.Event()

    def slow(text, task=None):
        started.set()
        release.wait(5)
        return Result(True, "done")

    session.handle_text = slow
    began = time.time()
    task = session.dispatch("open notepad")
    assert time.time() - began < 0.5, "dispatch blocked the listening loop"
    assert started.wait(2), "the request never started"
    assert not task.done.is_set()
    assert session.current_task is task
    release.set()
    assert wait_for(lambda: session.current_task is None)
    session.stop()


def test_a_task_stops_at_its_next_checkpoint():
    task = Task("something")
    task.cancel("user")
    with pytest.raises(Stopped):
        task.check()
    assert not task.alive()


def test_ask_user_treats_anything_unclear_as_no():
    session = bare_session()
    session.speaker = CountingSpeaker()
    session.ears = None
    assert session.ask_user("Delete it?") is False, "no microphone means no consent"


# ---------- the output device, and a line nobody can hear ---------------------


def test_a_device_that_cannot_play_falls_back_to_the_system_default(monkeypatch):
    """The bug: a chosen device was refused, and every line was written into it.

    `_play` used to return from inside its first attempt, so a device that could not
    open was tried again for every sentence for the rest of the session - three of
    which were counted as wedges, after which the voice switched itself off. A refusal
    must move on to the system default, and the device that worked must be the one
    kept.
    """
    import numpy as np
    speaker = voice.Speaker()
    monkeypatch.setattr(voice, "read_wav_mono",
                        lambda path: (22050, np.zeros(2205, dtype="float32")))
    attempts = []

    def play_on(device, audio, rate, cap):
        attempts.append(device)
        if device == 99:
            return False, True, "Invalid sample rate", False
        return True, False, "", False

    monkeypatch.setattr(speaker, "_play_on", play_on)
    speaker.device = 99
    assert speaker._play("line.wav", 5.0) is True, "the default device should have played it"
    assert attempts == [99, None], "the refused device must not be tried twice"
    assert speaker.device is None, "the device that worked is the one to keep"


def test_a_line_stopped_part_way_is_not_played_again_on_the_fallback(monkeypatch):
    """The tail of the storm: a barged answer re-spoken in full on the default device.

    `_play` reads "did not play" as "try the next device", and a line stopped by a
    barge arrives there for exactly that reason. So the sentence the owner had just
    interrupted was started again from its first word on the system default - a second
    hearing of a line nobody asked to hear, with the microphone open for it.
    """
    import numpy as np
    speaker = voice.Speaker()
    monkeypatch.setattr(voice, "read_wav_mono",
                        lambda path: (22050, np.zeros(2205, dtype="float32")))
    attempts = []

    def play_on(device, audio, rate, cap):
        attempts.append(device)
        return False, False, "", True          # stopped part way through

    monkeypatch.setattr(speaker, "_play_on", play_on)
    speaker.device = 7
    assert speaker._play("line.wav", 5.0) is False
    assert attempts == [7], "the stopped line was re-spoken on the fallback device"
    assert speaker.last_cut is True, "and a stopped line is a cut, not a broken device"


def test_a_fresh_attempt_does_not_inherit_the_last_one_abort(monkeypatch):
    """Each attempt sets `_abort` when it finishes; the next must clear it.

    Without the clear, the second device's callback aborted on its first block and a
    line that could have been spoken on the default was reported as unplayable.
    """
    import numpy as np
    import sounddevice as sd
    speaker = voice.Speaker()
    monkeypatch.setattr(voice, "playable_rate", lambda device, preferred: 48000)
    monkeypatch.setattr(voice, "resample", lambda audio, src, dst: audio)
    speaker._abort.set()
    seen = {}

    def refuse(**kwargs):
        seen["aborted"] = speaker._abort.is_set()
        raise RuntimeError("the open is not what is under test")

    monkeypatch.setattr(sd, "OutputStream", refuse)
    played, refused, why, cut = speaker._play_on(18, np.zeros(8, dtype="float32"), 22050, 1.0)
    assert seen["aborted"] is False, "the new attempt started silenced"
    assert not played and refused, "a device that will not open is a refusal, not a stall"
    assert not cut, "a device that never opened was not cut off part way through"


def test_a_line_that_never_played_is_reported_rather_than_claimed(monkeypatch):
    """"I can't hear it even if it speaks" has to leave a mark the next reader can find."""
    speaker = voice.Speaker()
    monkeypatch.setattr(speaker, "_do", lambda job, result: False)
    assert speaker.say("hello") is False
    assert speaker.last_played is False
    assert "no output device" in (speaker.last_error or "")
    assert speaker.wedges == 0, "a device refusing is not the engine wedging"


def test_the_voice_check_reports_what_the_mixer_heard():
    """The panel's contract: the rendered peak, and the one that means something."""
    session = bare_session()

    class HeardSpeaker:
        last_peak = 0.5
        device = 18

        def audible(self, text):
            return True, 0.42

    session.speaker = HeardSpeaker()
    assert session.test_voice() == (0.5, 0.42), "the mixer's peak reaches the window"


def test_a_voice_that_was_not_heard_is_not_reported_as_a_voice():
    session = bare_session()

    class SilentSpeaker:
        last_peak = 0.5
        device = 18

        def audible(self, text):
            return False, 0.0

    session.speaker = SilentSpeaker()
    peak, mixer_peak = session.test_voice()
    assert peak == 0.5, "the line rendered fine, and that is still reported"
    assert mixer_peak == 0.0, "...but nobody heard it, and that is what the window needs"


def test_a_stream_still_talking_is_cut_off_rather_than_left_playing():
    """The local and hosted voices overlapping came from a stream that outlived us.

    A stream that has not finished must be *cancelled*, not dropped: dropped, it keeps
    playing on its own thread while the next request's line goes to the SAPI queue, and
    two voices are heard at once.
    """
    session = bare_session()
    cuts = []

    class StillTalking:
        cancelled = False            # as a real stream is until cancel() is called

        def finish(self, timeout=None):
            return False

        def cancel(self):
            cuts.append(True)
            self.cancelled = True
            return True

    session._voice_stream = StillTalking()
    session._finish_speech(timeout=0.1)
    assert cuts == [True], "a stream that was still talking was dropped, not stopped"
    assert session._voice_stream is None


# ---------- stop, barge-in, one task at a time ------------------------------


class QueuedStream:
    """A speech stream with words in it, as one has between two sentences."""

    def __init__(self):
        self.cancelled = False
        self.fed = []

    def feed(self, text):
        self.fed.append(text)
        return not self.cancelled

    def finish(self, timeout=None):
        return True

    def cancel(self):
        self.cancelled = True
        return True

    @property
    def playing(self):
        return not self.cancelled

    level = 0.0


def test_stop_cuts_the_queued_words_says_one_word_and_keeps_listening():
    """The owner's rule: stop means *everything* stops, and "Got it" is the answer.

    `steer` used to stop only the local voice, so a queued reply kept playing while
    the session reported that it had stopped - and it answered with a sentence about
    what was stopped, which is the opposite of being quiet.
    """
    replies = []
    session = bare_session(on_reply=lambda text, ok: replies.append(text))
    speaker = CountingSpeaker()
    session.speaker = speaker
    stream = QueuedStream()
    session._voice_stream = stream
    session._spoken = ["I was halfway through a very long answer."]
    task = session._begin("open notepad and type hello")
    session.current_task = task

    session.steer(task, "stop")

    assert not task.alive() and task.cancelled, "the task kept running after a stop"
    assert stream.cancelled, "the words already queued kept talking"
    assert session._voice_stream is None
    assert speaker.said == ["Got it."], f"a stop is one word: {speaker.said}"
    assert replies and "Got it" not in replies[-1], "the window gets the detail"
    assert session._follow_until > time.time(), "it should be listening for what comes next"


def test_a_new_request_leaves_none_of_the_old_one_in_its_mouth():
    """One task at a time includes its voice: a new command started by finishing the old
    answer, because it adopted the stream that answer was still talking through."""
    session = bare_session()
    session.speaker = CountingSpeaker()
    stale = QueuedStream()
    session._voice_stream = stale
    session._spoken = ["the previous reply"]
    try:
        stream = session._begin_speech()
        assert stale.cancelled, "the old answer's queued sentences were adopted"
        assert stream is not stale
        assert session._spoken == [], "the new request started mid-remembering"
    finally:
        session._cancel_speech()


def test_saying_something_else_while_it_works_cancels_the_job_silently():
    """The owner asked for one task at a time, with nothing said about the switch."""
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session.followup = True
    session._follow_until = time.time() + 30      # they are still talking to it
    session.handle_text = lambda text, task=None: Result(True, "ok")
    old = session._begin("open spotify and play something long")
    session.current_task = old
    session._voice_stream = QueuedStream()

    session.steer(old, "open notepad", woke=False)   # no wake word: the window is open

    assert not old.alive(), "one task at a time - the first one must be cancelled"
    assert session._voice_stream is None or session._voice_stream.cancelled
    assert wait_for(lambda: session.current_task is not old and session.current_task is not None)
    time.sleep(0.3)
    assert not any("switch" in line.lower() for line in speaker.said), \
        "it should not narrate the change, the work starting is the reply"
    session.stop()


def test_a_local_voice_only_default_and_a_way_back(tmp_path, monkeypatch):
    """The owner liked the local voice: no cloud voice, no rate limits, no mid-answer
    voice change - and the hosted one still available for anyone who wants it."""
    from core import config, settings
    from core.session import local_voice_only
    assert config.LOCAL_VOICE is True, "the local voice is the shipped default"
    assert local_voice_only() is True
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    settings.forget()
    try:
        settings.save(local_voice=False)
        assert local_voice_only() is False, "the hosted voice must still be reachable"
        settings.save(local_voice=True)
        assert local_voice_only() is True
    finally:
        settings.forget()


def test_the_speech_stream_is_built_for_the_local_voice():
    session = bare_session()
    session.speaker = voice.Speaker()
    try:
        stream = session._begin_speech()
        assert stream.voice is None, "the hosted voice is off unless it is asked for"
        assert not stream.hosted, "nothing here should reach for the network"
    finally:
        session._cancel_speech()


def test_talking_over_the_reply_stops_it_at_the_first_loud_block(monkeypatch):
    """"When I cut its sentence it should stop and listen" - which needs the audio
    to stop on the mic's terms rather than at the end of a sentence.

    The stream is fake but the timing is real: blocks arrive 70 ms apart, above the
    echo gate, and the second one that lands 120 ms after the first must stop it.
    """
    import numpy as np
    import sounddevice as sd

    fired = []

    class FakeInput:
        def __init__(self, *args, **kwargs):
            self.callback = kwargs.get("callback")

        def __enter__(self):
            def push():
                for _ in range(8):
                    time.sleep(0.07)
                    self.callback(np.full((1024, 1), 0.4, dtype="float32"), 1024, None, None)

            threading.Thread(target=push, daemon=True).start()
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sd, "InputStream", FakeInput)
    ears = type("E", (), {"transcribe": lambda self, audio: "stop it"})()
    deadline = time.time() + 0.8
    heard = voice.listen_once(ears, interrupt=lambda: time.time() > deadline,
                              gate=lambda: 0.1, on_barge=lambda: fired.append(time.time()))
    assert fired, "talking over the reply did not stop it"
    assert len(fired) == 1, "it must interrupt once, not once per audio block"
    assert heard in ("stop it", ""), f"the microphone returned {heard!r}"


def test_the_owners_own_voice_is_never_treated_as_talking_over_it(monkeypatch):
    """A block that merely clears the gate is the echo, not an interruption."""
    import numpy as np
    import sounddevice as sd

    fired = []

    class FakeInput:
        def __init__(self, *args, **kwargs):
            self.callback = kwargs.get("callback")

        def __enter__(self):
            def push():
                # Just above the gate, below the gain: the reply keeps playing.
                for _ in range(8):
                    time.sleep(0.07)
                    self.callback(np.full((1024, 1), 0.12, dtype="float32"), 1024, None, None)

            threading.Thread(target=push, daemon=True).start()
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sd, "InputStream", FakeInput)
    ears = type("E", (), {"transcribe": lambda self, audio: "something"})()
    deadline = time.time() + 0.7
    voice.listen_once(ears, interrupt=lambda: time.time() > deadline,
                      gate=lambda: 0.1, on_barge=lambda: fired.append(1))
    assert fired == [], "the echo cut the reply off on its own"


# ---------- one answer per question ------------------------------------------
#
# The owner's report: "it gives 3-4 answers of every question", and an action that
# ran twice from a nine-character transcript nobody had spoken. Five separate ways a
# line could reach the voice belong to a moment that had already passed are pinned
# down here, one by one.

class WatchingSpeaker(CountingSpeaker):
    """A speaker that also records whether it was spoken to on someone else's thread."""

    def __init__(self):
        super().__init__()
        self.async_calls = 0

    def say_async(self, text):
        self.async_calls += 1
        return self.say(text)


class RoomSampler:
    """Just the two reads the session takes from the system-audio sampler."""

    def __init__(self, level=0.0):
        self.level = level
        self.bands = [0.0] * 12


class WaitingPlanner:
    """A planner whose written line waits, so a request can start underneath it."""

    def __init__(self, text="Hello, I'm all ears."):
        self.text = text
        self.called = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()

    def line(self, purpose, detail="", fallback=""):
        self.called.set()
        self.release.wait(5)
        self.done.set()
        return self.text


def test_a_greeting_that_took_too_long_is_not_spoken_into_the_next_request():
    """"It says hello, then three seconds later says hello again."

    The greeting is asked for the instant the wake word lands and takes a model round
    trip to write. By the time it comes back the owner may already be talking - and the
    line used to be spoken into whatever request was running by then, which is the
    second hello, arriving after the answer had started.
    """
    session = bare_session()
    speaker = WatchingSpeaker()
    session.speaker = speaker
    session.converse = True
    planner = WaitingPlanner()
    session.planner = planner
    session._speak_written("greeting", fallback="Hello.")
    assert planner.called.wait(2.0), "the greeting was never written"
    session._begin_speech()             # the owner starts talking before it lands
    planner.release.set()
    assert planner.done.wait(2.0)
    assert not wait_for(lambda: bool(speaker.said), timeout=0.5), \
        "a greeting written for the wake was spoken over the request that replaced it"


def test_a_line_from_a_superseded_request_is_dropped_rather_than_spoken_later():
    session = bare_session()
    session.speaker = WatchingSpeaker()
    stale = session._speech_gen
    session._begin_speech()             # a newer request owns the voice now
    assert session._stream_or_speaker("Opening my settings", gen=stale) is False
    assert session.speaker.said == []
    assert session._spoken == [], "nothing spoken means nothing written down as spoken"


def test_a_stale_line_is_dropped_by_speak_async_too():
    session = bare_session()
    session.speaker = WatchingSpeaker()
    stale = session._speech_gen
    session._begin_speech()
    assert session.speak_async("Still working on it.", gen=stale) is False
    assert session.speaker.said == []


def test_a_line_with_no_request_behind_it_is_spoken_here_not_on_a_stray_thread():
    """`say_async` was a second voice: nothing could stop it, wait for it, or order it."""
    session = bare_session()
    speaker = WatchingSpeaker()
    session.speaker = speaker
    assert session.speak_async("Setting the mode.") is True
    assert speaker.said == ["Setting the mode."]
    assert speaker.async_calls == 0, "no line may be handed to a thread nobody owns"


def test_retro_does_not_answer_its_own_voice():
    """The log, exactly: one action, twice, ten seconds apart.

    It said "Opening my settings", the recogniser returned "settings", and the
    follow-up window - which takes a command without the wake word - ran the same
    action again.
    """
    session = bare_session()
    session._record_said("Opening my settings")
    session._output_ended_at = time.monotonic()
    assert session._is_echo("settings") is True
    assert session._is_echo("Opening my settings") is True
    assert session._is_echo("open notepad") is False, "a real command is not an echo"


def test_the_echo_of_an_answer_stops_counting_after_a_pause():
    session = bare_session()
    session._record_said("Opening my settings")
    session._output_ended_at = time.monotonic() - (config.ECHO_WINDOW_S + 1.0)
    assert session._is_echo("settings") is False, \
        "after a pause, saying the same command again has to work"


def test_the_wake_word_is_never_an_echo():
    session = bare_session()
    session._record_said("I am Retro and I am listening")
    session._output_ended_at = time.monotonic()
    assert session._is_echo("Retro") is False
    assert session._is_echo("retro") is False


def test_the_microphone_is_gated_while_retro_is_playing_and_then_timed_out():
    from core.session import PLAY_GATE_MAX

    session = bare_session()
    speaker = CountingSpeaker()
    speaker.playing = True
    speaker.level = 0.2                 # a normal reply, as the player reports it
    session.speaker = speaker
    gate = session._mic_gate()
    # The gate is a raw microphone level, not the player's display level: the echo of
    # this reply is under it, and a person talking over the reply - 0.01 to 0.05 at
    # this microphone - is over it.
    assert gate is not None and gate > voice.START_RMS, "the echo of Retro's own voice got through"
    assert gate < 0.2, "the gate was built in the player's display units, not the microphone's"
    assert gate <= PLAY_GATE_MAX, "and a person always has to be able to beat it"
    speaker.playing = False
    session._mic_gate()                 # the first block after the output stops
    assert session._output_ended_at > 0.0, "the echo window is measured from here"


def test_the_room_gate_makes_the_recogniser_ignore_what_the_machine_is_playing():
    """"Don't let it listen to media voices": a television is not a command."""
    session = bare_session()
    session.speaker = CountingSpeaker()
    session.audience = RoomSampler(0.0)
    assert session._mic_gate() is None, "a silent room must not gate anything"
    session.audience.level = 0.34       # amplified by the sampler: about 0.1 raw
    gate = session._mic_gate()
    assert gate is not None and gate >= config.ROOM_GATE_FLOOR
    assert gate < 0.2, "a person talking over the music still has to get through"


def test_no_sampler_means_the_room_is_unknown_and_never_gates():
    session = bare_session()
    session.speaker = CountingSpeaker()
    assert session.audience is None
    assert session._mic_gate() is None, "an unmeasurable room must not shut Retro out"


# ---------- one question, one answer -----------------------------------------


def test_the_same_line_is_not_spoken_twice():
    """The complaint, in one test: "Opening my settings" said three times.

    Three routes really can carry the same words - a plan's `say` step, the reply
    written back at the end of the request, and an acknowledgement the watchdog
    wrote - and each of them used to speak it again. The check is on the words, at
    the moment of speaking, so all of them are caught by one guard.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._say("Opening my settings.")
    session._say("Opening my settings")
    session.speak_async("Opening my settings.")
    session._stream_or_speaker("Opening my settings")
    assert speaker.said == ["Opening my settings."], f"the line was said {len(speaker.said)} times"


def test_a_different_line_is_still_spoken():
    """The guard has to be about repetition, not about silence."""
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._say("Opening my settings.")
    session._say("Opening Spotify instead.")
    session._say("Stopped.")
    assert speaker.said == ["Opening my settings.", "Opening Spotify instead.", "Stopped."]


def test_a_repeated_line_again_later_is_allowed():
    """A person who asks again twelve seconds later is asking again, not stuttering."""
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._say("Opening my settings.")
    session._recent_lines[-1] = (time.monotonic() - config.REPEAT_SUPPRESS_S - 1,
                                 session._normalise("Opening my settings."))
    session._say("Opening my settings.")
    assert len(speaker.said) == 2, "the window closed, so this is a new line"


def test_a_sentence_restated_in_almost_the_same_words_is_not_said_again():
    """The repeat the exact check cannot see, and the one the owner called "random".

    A small model rewriting its own sentence - one word changed, or the same point made
    again in a document's next section - is a different string and the same sentence to
    a listener. The window is longer now too, because a document's repeat arrives a
    section later rather than a sentence later.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._say("Just keeping the laptop humming along quietly.")
    session.speak_async("Just keeping the laptop humming along quietly!")
    session._say("Just keeping the laptop running along quietly.")
    assert speaker.said == ["Just keeping the laptop humming along quietly."], speaker.said


def test_an_ordinary_next_sentence_is_still_spoken():
    """The guard has to be about restating one line, not about talking."""
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    lines = ["Opening my settings for you now.",
             "Opening Spotify instead, since you asked.",
             "Your battery is at 82 percent and charging.",
             "Notepad is open with your notes in it."]
    for line in lines:
        session._say(line)
    assert speaker.said == lines, speaker.said


def test_the_same_request_is_not_run_twice():
    """A split block, or Retro's own answer coming back round the speakers.

    Either way the second copy is the *same request*, and running it twice is what
    made one question produce several answers.
    """
    session = bare_session()
    session.speaker = CountingSpeaker()
    runs = []

    def run(text, task=None):
        runs.append(text)
        return Result(True, "Opening my settings.")

    session.handle_text = run
    first = session.dispatch("open my settings")
    second = session.dispatch("open my settings")
    assert first is not None, "the first copy must still run"
    assert second is None, "the same request was run twice"
    assert wait_for(lambda: session.current_task is None)
    assert runs == ["open my settings"], f"the request ran {len(runs)} times"
    session.stop()


def test_the_colour_goes_back_the_moment_the_voice_stops():
    """The stream reports its own end, so the green does not have to be timed out.

    The watchdog used to be the only way back from it: two looks a second and another
    0.4 s of quiet on top, which is the two or three seconds the owner watched the orb
    stay lit with nothing behind it.
    """
    session = bare_session()
    session._set_state(SPEAKING)
    session._on_speech_idle()
    assert session._state == LISTENING, "the orb was left speaking after the voice stopped"


def test_a_reply_still_being_worked_on_goes_back_to_working_not_to_idle():
    """Green either way, but the difference is real: the microphone is not ours yet."""
    session = bare_session()
    session.current_task = Task("typed")
    session._set_state(SPEAKING)
    session._on_speech_idle()
    assert session._state == WORKING


def test_the_idle_report_never_wakes_something_asleep_or_offline():
    session = bare_session()
    for state in ("asleep", "offline"):
        session._set_state(state)
        session._on_speech_idle()
        assert session._state == state, f"a stray report woke the {state} assistant"


def test_a_different_request_is_not_dropped():
    session = bare_session()
    session.speaker = CountingSpeaker()
    session.handle_text = lambda text, task=None: Result(True, "ok")
    assert session.dispatch("open my settings") is not None
    assert session.dispatch("open spotify") is not None
    assert wait_for(lambda: session.current_task is None)
    session.stop()



# ---------- Retro must not hear itself ---------------------------------------


def test_the_echo_gate_survives_the_gap_between_two_sentences():
    """The bug behind "sure thing / hey there / hi" out of one wake word.

    The gate was `played_level * 1.5` read at the instant of the block, and the player
    reports *zero* while a sentence is being synthesised and in every gap between two
    sentences - which is most of a reply. For those gaps the threshold fell to
    START_RMS*2, anything in the room barged in, Retro cut its own line off, and the
    fragment it had been saying was transcribed and handed back as a command. The
    level the player last reported is now remembered for the tail, so the threshold
    cannot collapse with it.
    """
    from core.session import PLAY_FLOOR, PLAY_GATE_MAX, PLAY_TAIL_S

    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    speaker.playing, speaker.level = True, 0.2
    gated = session._mic_gate()
    assert gated is not None and gated >= PLAY_FLOOR, "the echo itself was not gated"
    assert gated <= PLAY_GATE_MAX, "and it must stay inside what a person can beat"
    speaker.playing, speaker.level = False, 0.0         # the gap before the next sentence
    for _ in range(int(PLAY_TAIL_S / 0.03)):
        assert session._mic_gate() is not None, "the gate collapsed in the gap"
        assert session._mic_gate() >= PLAY_FLOOR, "the gate fell back to the floor in the gap"
    assert session._output_ended_at > 0.0, "the echo window is measured from here"


def test_the_gate_is_in_the_microphones_own_units():
    """A gate of "1.5x the played level" is five times louder than a person.

    The players report `rms * 6` clamped to 1.0; the microphone reports raw rms. Read
    as if they were the same number, a reply at 0.5 on the display scale produced a
    gate of 0.75 - nothing anybody says into a laptop microphone reaches that, so no
    command said over a reply could ever be heard, and only the collapse in the gaps
    let anything through at all. Both ends are asserted here: the gate is above the
    speech floor, and it is below a raised voice.
    """
    from core.session import PLAY_FLOOR, PLAY_GATE_MAX

    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    for played in (0.2, 0.5, 1.0):                      # quiet reply, normal, loud
        speaker.playing, speaker.level = True, played
        gate = session._mic_gate()
        assert PLAY_FLOOR <= gate <= PLAY_GATE_MAX, \
            f"gate {gate} at played {played}: above the floor, inside what a person beats"


def test_the_gate_does_not_chase_the_microphone():
    """A person talking over Retro must never raise the gate that lets them through.

    The obvious way to build this gate - track what the microphone hears while Retro
    speaks, and gate above it - absorbs the *user's* voice into the echo and then
    refuses them with their own loudness. The gate is built from what is being played,
    which a shout at the microphone cannot change.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    speaker.playing, speaker.level = True, 0.2
    before = session._mic_gate()
    for _ in range(6):
        session._mic_level(0.6)                         # somebody shouts over it
    assert session._mic_gate() == before, "the microphone raised its own gate"


def test_the_tail_expires_so_a_quiet_retro_still_hears_a_person():
    """The other half: once the echo has gone, so has the gate."""
    from core.session import PLAY_TAIL_S

    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    speaker.playing, speaker.level = True, 0.2
    assert session._mic_gate() is not None
    speaker.playing = False
    session._played_until = time.monotonic() - 0.1      # the tail has passed
    assert session._mic_gate() is None, \
        "with nothing playing and a quiet room, the microphone is open"


def test_a_phrase_said_straight_after_retro_is_still_a_command():
    """The case that must never break: "Retro", then the command over the greeting.

    A blanket "this arrived under our own voice, so drop it" veto would refuse exactly
    that sentence - the common one - and it is not what stops the echo anyway. The
    gate is: a block has to beat what Retro is playing by PLAY_GATE to be recorded at
    all, which the echo of laptop speakers never does and a person always does.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    speaker.playing, speaker.level = True, 0.2
    session._mic_level(0.06)                       # Retro's own voice at the mic
    assert session._heard_under_voice is True
    assert session._heard_ourselves("open my settings") is True, \
        "this is evidence, not a veto - the method only reports what happened"
    assert session._mic_gate() > voice.START_RMS, \
        "and the echo itself is still refused by the gate, which is what matters"
    assert session._heard_ourselves("Retro, stop") is False


def test_a_fragment_of_retros_own_words_is_never_a_command():
    """The storm, in one call: the microphone heard the speakers mid-sentence.

    The echo guard used to be measured from the moment the output *stopped*, so a
    fragment transcribed while the voice was still playing was not Retro's own voice
    as far as the loop was concerned - it was a command. A real `heard_itself` in the
    log (11 characters) is exactly that case, and the lines it came from look like
    this: "Commands.", "If you don't.", "What would you like to do today?" - each one
    a piece of something Retro had said a moment earlier.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._record_said("I can open apps, run commands and write things down.")
    session._record_said("If you don't need anything else, just say so.")
    speaker.playing = True                        # still talking: the output has not stopped
    session._output_ended_at = 0.0                # and the old window is measured from zero
    for fragment in ("Commands.", "commands", "If you don't."):
        assert session._is_echo(fragment) is True, f"{fragment!r} was taken for a command"
    assert session._is_echo("open notepad") is False, \
        "a command said over a reply is not a fragment of the reply"
    assert session._is_echo("Retro, open notepad") is False, \
        "the wake word is never an echo, whatever is being said at the time"


def test_the_echo_of_a_line_comes_back_mangled_and_is_still_an_echo():
    """The live session, in one call: the greeting, duplicated across the seam.

    What Retro said was "Commands, the app, and t..."; what the recogniser returned
    was "Commands, the app, and the app, and the" - four words it never said, in the
    middle of it. The guard asked whether the transcript was a *substring* of a line
    Retro had said, and the answer was no, so the fragment was dispatched as a
    request, the model answered it, that answer was echoed back in turn, and the owner
    heard a reply per loop out of one "Retro". A substring test cannot be the answer
    to this: the recogniser is hearing the speakers a syllable behind and duplicating
    across the seam, and every fragment it produces that way fails it.
    """
    session = bare_session()
    speaker = CountingSpeaker()
    session.speaker = speaker
    session._record_said("Commands, the app, and the tools, all by voice.")
    speaker.playing = True                  # still talking: the output has not stopped
    session._output_ended_at = 0.0
    assert session._is_echo("Commands, the app, and the app, and the") is True, \
        "the mangled echo of Retro's own sentence was taken for a command"
    assert session._is_echo("Sure, I got it.") is False, \
        "a line Retro never said is not an echo of one"
    session._record_said("Sure, I got it.")
    assert session._is_echo("Sure I got it") is True
    assert session._is_echo("the app") is True, "two of Retro's own words in a row"


def test_the_owners_own_session_now_runs_two_requests_instead_of_five(monkeypatch):
    """The 10:45 session, replayed through the real voice loop.

    Seven transcripts, in the order they arrived, at the peak each one arrived at, and
    with Retro's own voice playing under the ones that arrived over it - every number
    from the owner's log. Five of them became requests that afternoon, one after
    another, each answered and then echoed back in turn. What is asserted is the thing
    the owner asked for: the only requests are the two things they actually said, and
    the fragments are dropped with the rule that dropped them.
    """
    from core import session as session_module

    # (transcript, peak, was Retro's own voice playing for every block of it)
    phrases = [
        ("Retro.", 0.849, False),                                    # the owner
        ("How are you today?", 0.849, False),                        # the owner
        ("Commands, the app, and the app, and the ", 0.106, True),   # the greeting, echoed
        ("Sure, I got it.", 0.907, True),                            # a reply, echoed
        ("ありがとうございました。", 0.079, True),                      # invented out of English
        ("open notepad", 0.700, True),                               # the owner, over it
    ]

    class PlayingSpeaker(CountingSpeaker):
        """Reports itself playing while a line is out, which is what the gate reads."""

        def say(self, text, cap=None):
            self.said.append(text)
            self.playing, self.level = True, 0.6
            self.playing, self.level = False, 0.0
            return True

    session = bare_session()
    session.speaker = PlayingSpeaker()
    session.ears = object()
    session.followup = True
    session._follow_until = time.time() + 999
    session.loaded = True
    for line in ("Hey, I'm listening.",
                 "Commands, the app, and the tools all by voice.",
                 "Sure, I got it."):
        session._record_said(line)

    dispatched, woken, why = [], [], {}
    session.dispatch = lambda text, woke=False: dispatched.append(text) or None
    session._wake = lambda: woken.append("retro") or True
    real_reason = Session._echo_reason

    def watched(self, text, peak=0.0):
        reason = real_reason(self, text, peak)
        why[text] = reason
        return reason

    monkeypatch.setattr(Session, "_echo_reason", watched)

    state = {"index": 0}

    def listen_once(ears, **kwargs):
        index = state["index"]
        if index >= len(phrases):
            session._stop.set()
            return ""
        text, peak, under = phrases[index]
        state["index"] += 1
        timing = kwargs.get("timing")
        if timing is not None:                     # what the real listener fills in
            timing["duration"], timing["peak"] = 1.0, peak
            timing["phrase_end"] = time.time()
        if under:                                  # the microphone callback, per block
            session.speaker.playing, session.speaker.level = True, 0.6
            session._mic_gate()
            session._heard_under_voice = True
        else:
            session._mic_gate()
        session._mic_level(0.02)
        if under:
            session.speaker.playing, session.speaker.level = False, 0.0
            session._mic_gate()                    # the output stopped: open the window
        return text

    monkeypatch.setattr(session_module, "listen_once", listen_once)
    session.voice_loop()

    assert dispatched == ["How are you today?", "open notepad"], \
        f"one 'Retro' turned into these requests: {dispatched}"
    assert woken == ["retro"], "the owner's own 'Retro' still wakes it"
    assert why["Commands, the app, and the app, and the "] == "own words"
    assert why["Sure, I got it."] == "own words"
    assert why["ありがとうございました。"] == "too quiet"
    assert why["open notepad"] == "", "said over a reply, and it is not an echo"


def test_a_short_word_is_left_to_the_level_test_and_not_swallowed_by_the_words():
    """"stop", "yes", "so": one short word matched against everything Retro has said.

    A loose word test that took these would make the assistant deaf to exactly the
    words a person says to interrupt it, so a single short word is not the word
    test's business - it is the level test's, and the duplicate guard's.
    """
    session = bare_session()
    session._record_said("So I can open that for you now.")
    session._output_ended_at = time.monotonic()
    session._record_said("Opening my settings for you.")
    assert session._is_echo("So,") is False
    assert session._is_echo("stop") is False
    assert session._is_echo("settings") is True, "a long, distinctive word still is one"


def test_a_garbled_fragment_over_the_voice_is_dropped_by_level_alone():
    """The live case no word test can reach.

    The recogniser invented "ありがとうございました" out of a sentence in English and the
    loop dispatched it as a request - there are no words in common with anything Retro
    said, so the word test has nothing to match on. The phrase arrived at a peak of
    0.079 while the owner's own voice measured 0.849 at the same microphone, and that
    is what this test is: the yardstick is the owner's own voice, measured, rather
    than a constant assumed about how the speakers couple to the microphone.
    """
    session = bare_session()
    session.speaker = CountingSpeaker()
    session._clean_peak = 0.849          # the last phrase heard with nothing playing
    session._heard_under_voice = True    # every block of this one arrived over the voice
    assert session._is_echo("ありがとうございました。") is False, \
        "with no words in common there is nothing for the word test to find"
    assert session._echo_reason("ありがとうございました。", 0.079) == "too quiet"
    assert session._echo_reason("How are you today?", 0.849) == "", \
        "the owner talking at their own volume is never refused"


def test_the_yardstick_is_the_owners_own_voice_and_it_never_falls():
    """The live session's own arithmetic, which is what collapsed the gate.

    "Retro." arrived at 0.83 and measured the owner's own voice at this microphone.
    Then the recogniser invented "Thank you." at 0.031 and "Cennance." at 0.158 out of
    a quiet room, and under the old rule each of them *replaced* that measurement: the
    yardstick fell to 0.031, the gate fell with it, and every quiet fragment after that
    was louder than the gate and became a request. Every one of them was answered, out
    loud, and the answer was heard in turn.
    """
    session = bare_session()
    assert session._clean_peak == 0.0
    session._take_yardstick("Retro.", 0.83)
    assert session._clean_peak == 0.83
    session._take_yardstick("Thank you.", 0.031)      # no wake word: not a measurement
    session._take_yardstick("Cennance.", 0.158)
    assert session._clean_peak == 0.83, "junk from an empty room became the yardstick"
    session._take_yardstick("Retro.", 0.31)           # quieter than the first: no fall
    assert session._clean_peak == 0.83
    session._take_yardstick("Retro.", 0.97)           # louder: it may rise
    assert session._clean_peak == 0.97


def test_the_first_retro_over_the_greeting_still_measures_the_owner():
    """The first "Retro" of a session lands over the greeting, and it counts.

    Refusing it while Retro was speaking left the yardstick at zero, and a zero
    yardstick means "unknown, never quiet": every fragment of the reply that came
    back through the microphone was then answered as a new command. That is the
    double answer the owner heard.
    """
    session = bare_session()
    session._heard_under_voice = True
    session._take_yardstick("Retro.", 0.83)
    assert session._clean_peak == 0.83
    assert session._too_quiet_to_be_a_person(0.05) is True

    echo = bare_session()
    echo._heard_under_voice = True
    echo._take_yardstick("Retro.", 0.05)      # the greeting, back off the speakers
    assert echo._clean_peak == 0.0


def test_the_quiet_junk_of_the_live_session_is_refused_by_that_yardstick():
    """Every level below is from the 11:13 session, in order."""
    session = bare_session()
    session._clean_peak = 0.83
    for text, peak in (("Thank you.", 0.031), ("Cennance.", 0.158), ("And,", 0.051),
                       ("Commands.", 0.113), ("Commands.", 0.128),
                       ("Commands.", 0.166), ("Bye.", 0.051)):
        assert session._echo_reason(text, peak) == "too quiet", \
            f"{text!r} at {peak} was taken for a request"
    assert session._echo_reason("open notepad", 0.70) == "", \
        "the owner, talking to their laptop, is never refused"
    assert session._echo_reason("Retro, open notepad", 0.45) == "", \
        "a deliberate wake word is held to a lower floor than a follow-up"
    assert session._echo_reason("Retro.", 0.05) == "echo level", \
        "the echo of the wake word is not the owner saying it"


def test_the_half_volume_junk_of_the_newest_session_is_refused():
    """The 22:04 session, and why both floors had to go up.

    What got through the old pair (0.35 for a follow-up, 0.15 for a wake word) was not
    junk at the floor of the room but phrases at half the owner's own volume, each was
    dispatched as a request and answered out loud, and the owner's real phrases in the
    same session measure 0.699 to 0.994. So the bar sits in the gap the log shows:

      "I love you." 0.406, "Retro." 0.168 - which woke it - and
      "Retro. Commands. Retro. Commands." 0.443, which woke it again.

    One that is deliberately **not** here: "Commands, I'm going to go to the table,"
    arrived at 0.557, and no honest level gate may refuse that - it is a fifth below
    the owner's own voice on a signal that clips at 0.994, and a rule that tried would
    start refusing the owner. Every one of those phrases was the recogniser reading its
    own prompt back, and that is fixed where it happens, in SPEECH_PROMPT and in the
    decoder's own confidence numbers (tests/test_stt.py). A level gate is the second
    line of defence, not a way of getting out of fixing the first.
    """
    session = bare_session()
    session._clean_peak = 0.994                    # the owner's own "Retro. Sleep."
    for text, peak in (("I love you.", 0.406), ("Oh, finally.", 0.174),
                       ("Thank you, everyone.", 0.16), ("Thank you.", 0.042),
                       ("H?n er sann, h?n er ? vans a?.", 0.346)):
        assert session._echo_reason(text, peak) == "too quiet", \
            f"{text!r} at {peak} was answered out loud"
    assert session._echo_reason("Retro.", 0.168) == "echo level", \
        "the phantom 'Retro' at 0.168 woke the assistant on 2026-10-16"
    for text, peak in (("Retro. Sleep.", 0.977), ("Retro. Sleep.", 0.994),
                       ("Commands, what time is it,", 0.699), ("Retro, open notepad", 0.45)):
        assert session._echo_reason(text, peak) == "", \
            f"{text!r} at {peak} is the owner talking and must still be heard"

    # And the wake-word floor is deliberately *not* raised to catch the one recitation
    # that arrived at 0.443 ("Retro. Commands. Retro. Commands.", which woke it). Any
    # floor high enough to refuse a phrase that says "Retro" at 44% of the owner's
    # voice is a floor that refuses a quiet "Retro", and being ignored is the
    # complaint the wake word exists to answer. That text cannot be produced at all any
    # more - it was the prompt being read back - so it is stopped there, not here.
    assert session._echo_reason("Retro. Commands. Retro. Commands.", 0.443) == "", \
        "the level bar may not be raised into a quiet 'Retro' to catch a recitation"


def test_an_echo_that_contains_the_wake_word_is_still_an_echo():
    """Live: "Retro saw what you're doing, I didn't to..." was dispatched.

    The wake word used to be an absolute exemption: a transcript containing "retro"
    could not be Retro's own voice, whatever else was in it. But Retro says its own
    name out loud, and the microphone hears the speakers - so the echo of a reply
    arrived with the wake word inside it and was accepted as a fresh request, every
    time. Only the words *after* the wake word are Retro's own; the wake word itself is
    what the owner adds.
    """
    session = bare_session()
    session._record_said("Is there anything on your mind?")
    session._output_ended_at = time.monotonic()
    assert session._is_echo("Retro, anything's on your mind?") is True
    assert session._is_echo("Retro, open notepad") is False


def test_the_owners_own_voice_over_a_reply_is_never_refused():
    """The case that must keep working: a real command said over the greeting."""
    session = bare_session()
    session._clean_peak = 0.849
    session._heard_under_voice = True
    assert session._echo_reason("open notepad", 0.7) == ""
    assert session._echo_reason("Retro, open notepad", 0.7) == ""
    assert session._too_quiet_to_be_a_person(0.25) is True, \
        "a quarter of the owner's own voice is the echo, not the owner"


def test_with_no_clean_phrase_to_compare_against_nothing_is_refused():
    """Unknown is not quiet: a session that has heard nobody may refuse nobody."""
    session = bare_session()
    session._heard_under_voice = True
    assert session._clean_peak == 0.0
    assert session._too_quiet_to_be_a_person(0.01) is False
    assert session._echo_reason("open notepad", 0.01) == ""


def test_a_barge_in_the_echo_tail_does_not_cancel_the_line_behind_it():
    """A barge fired at `state: listening` in the live log, and cancelled nothing.

    The gate is up for PLAY_TAIL_S after the last sentence has stopped, so the echo
    tail can barge at a moment when no audio is playing at all - and `_cancel_speech`
    there kills whatever line is about to start. Only a barge with something to
    interrupt may reach it.
    """
    session = bare_session()
    cancels = []
    session._cancel_speech = lambda: cancels.append(True)
    speaker = CountingSpeaker()
    session.speaker = speaker
    speaker.playing = False
    session._barged()
    assert cancels == [], "nothing was playing, so there was nothing to cut"
    assert session._follow_until > time.time(), "but the window still opens"
    speaker.playing, speaker.level = True, 0.2
    session._barged()
    assert cancels == [True], "a barge while it is really talking still cuts it off"


def test_a_reply_cut_off_by_a_barge_is_never_reported_as_unheard():
    """The live log's `reply_unheard` was about a line the owner was listening to.

    A barge stopped the reply mid-sentence, so the player reported "did not finish" -
    and the session turned that into "nothing came out of the output device - check
    OUTPUT", sending the owner to the OUTPUT picker over audio they could hear. A cut
    line is not evidence of a broken device and must not be reported as one.
    """
    class CutSpeaker(CountingSpeaker):
        last_cut = True

        def say(self, text, cap=None):
            self.said.append(text)
            return False

    notes, said = [], []
    session = bare_session(on_progress=notes.append, on_log=said.append)
    session.speaker = CutSpeaker()
    session._say("That answer was stopped part way through.")
    assert session.speaker.said, "the line was handed to the voice"
    assert not any("OUTPUT" in note for note in notes + said), \
        "a stopped line must not be reported as a broken output device"


def test_the_barge_in_cuts_a_real_person_through():
    """Barge-in still has to fire when somebody actually talks over Retro.

    A person at conversational distance is several times louder at the microphone
    than the echo of laptop speakers, so the case the feature exists for still works -
    and it clears the "heard ourselves" flag, or the interruption itself would be
    dropped as Retro hearing its own speakers.
    """
    from core import voice

    session = bare_session()
    fired = []
    speaker = CountingSpeaker()
    session.speaker = speaker
    real_barge = session._barged

    def watched():
        fired.append(True)
        real_barge()

    session._barged = watched
    speaker.playing, speaker.level = True, 0.2
    person = session._mic_gate() * voice.BARGE_GAIN * 1.2
    for _ in range(6):
        session._mic_level(person)
        if person > session._mic_gate() * voice.BARGE_GAIN:
            session._barged()
    assert fired, "a person louder than the echo must still be able to cut Retro off"
