"""Immediate, ordered, cancellable speech - and the wake word that starts it all.

The user's complaint was latency and repetition: answers that arrived only once the
whole reply was written, one sentence spoken twice, and a wake word that either
never mattered or always mattered. These tests pin the pipeline down without a
sound card: a fake voice stands in for the hosted one, the playback itself is
replaced, and the sentences that reached the speaker are what get asserted.

Then there was the gap. "Extremely long pauses between full stops", and a circle
that stayed green for two or three seconds after the words had stopped. Both were
the same thing from the owner's side - something about the reply outliving the
speaking - so both are pinned here: the dead air each clip arrives with is trimmed,
the local voice is rendered before its turn comes instead of during it, and the
stream says out loud the moment it has stopped.
"""
import threading
import time

import numpy as np
import pytest

from core import config, modes, settings, tts, voice
from core.model import Result
from core.planner import Agent
from core.session import Session


@pytest.fixture(autouse=True)
def fresh_cooldown():
    """A rate limit in one test must not silence the hosted voice in the next.

    The cooldown is process-wide on purpose - the whole point is that a 429 answered
    by one reply tells the *next* reply not to ask - so every test starts from clear.
    """
    tts.reset_backoff()
    yield
    tts.reset_backoff()


class FakeVoice:
    """Stands in for GroqVoice: returns the text back as if it were audio."""

    def __init__(self, fail_first=False, delay=0.0, available=True, fail_times=0,
                 error="HTTP 500"):
        self.available = available
        self.calls = []
        self.fail_first = fail_first
        self.fail_times = int(fail_times) or (1 if fail_first else 0)
        self.error = error
        self.delay = delay

    def speech(self, text):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            self.fail_first = False
            raise tts.TtsError(self.error)
        return 16000, text


class StubSpeaker:
    """A local voice that records what it was asked to say."""

    def __init__(self, said=None):
        self.said = said if said is not None else []
        self.disabled = False
        self.playing = False
        self.level = 0.0
        self.device = None

    def say(self, text, cap=None):
        self.said.append(text)
        return True

    def say_async(self, text):
        self.said.append(text)
        return True

    def stop(self):
        return True


def make_stream(**kwargs):
    """A VoiceStream whose playback is recorded instead of played."""
    played = []
    stream = tts.VoiceStream(voice=FakeVoice(**kwargs), allow_cloud=True)
    stream._play = lambda rate, audio: played.append(audio)
    return stream, played


# ---------- splitting --------------------------------------------------------


def test_a_reply_is_split_into_sentences_in_order():
    assert tts.split_sentences("One thing. Then another! And a third?") == [
        "One thing.", "Then another!", "And a third?"]


def test_a_sentence_longer_than_the_api_allows_is_split_again():
    text = "word " * 60
    pieces = tts.split_sentences(text)
    assert pieces and all(len(piece) <= tts.MAX_CHARS for piece in pieces)
    assert " ".join(pieces).split() == text.split()


def test_a_stream_of_tokens_only_yields_whole_sentences():
    splitter = tts.SentenceStream()
    assert splitter.feed("Hello there") == []
    assert splitter.feed(". How are") == ["Hello there."]
    assert splitter.feed(" you?") == []          # still being written
    assert splitter.feed(" Yes") == ["How are you?"]
    assert splitter.flush() == ["Yes"]


def test_the_tail_is_not_lost_when_the_reply_ends():
    splitter = tts.SentenceStream()
    assert splitter.feed("no full stop here") == []
    assert splitter.flush() == ["no full stop here"]


def test_punctuation_is_never_a_sentence_of_its_own():
    """The complaint in the owner's words: "it reads out commas and full stops".

    A model reply arrives as a heading and a bulleted list often enough - whatever it
    has been told - and the piece handed to the voice used to be the marker itself. A
    piece with no letters or digits in it cannot be a sentence, so it is not one.
    """
    for piece in (".", "...", "-", "1.", "•", ",", "**", "> ", "##"):
        assert tts.split_sentences(piece) == [], f"{piece!r} was handed to the voice"
    assert tts.split_sentences("Commands.") == ["Commands."], \
        "a word with a full stop after it is still a word"


def test_a_list_is_spoken_as_its_words_and_never_as_its_markers():
    pieces = tts.split_sentences("1. Open apps\n2. Set reminders\n3. **Everything** else")
    assert pieces == ["Open apps Set reminders Everything else"], pieces
    assert tts.split_sentences("3.5 gigabytes is plenty.") == ["3.5 gigabytes is plenty."], \
        "a number followed by a stop is not a list marker"
    assert tts.speakable("---") == ""
    assert tts.speakable("```py\ncode()\n```") == ""


def test_the_other_paths_to_the_voice_strip_the_markers_too():
    """Not every line goes through the splitter: a plan's `say` step, a greeting, a
    written filler line and a document read back all arrive as whole lines, and a
    marker on a line of its own reaches SAPI as a symbol it will pronounce.
    """
    assert tts.speakable("- 2. Set reminders") == "Set reminders"
    assert tts.speakable("## Commands") == "Commands"
    assert tts.VoiceStream(speaker=object()).feed("...") is False


def test_a_sentence_glued_to_itself_is_said_once():
    """The live one, word for word: two sentences with no space between them.

    The model wrote "Just keeping the laptop humming.Just keeping the laptop humming."
    A sentence was only ever broken on a stop *followed by a space*, so those two were
    one piece for ever - and every repeat guard in the session compares whole lines, so
    none of them could see the repeat. What the owner heard was one sentence, twice,
    drawn out as a single utterance.
    """
    splitter = tts.SentenceStream()
    assert splitter.feed("Just keeping the laptop humming."
                         "Just keeping the laptop humming.") == [
        "Just keeping the laptop humming."]
    assert splitter.flush() == [], "the same sentence came round for a second reading"


def test_two_sentences_written_without_a_space_are_still_two_sentences():
    splitter = tts.SentenceStream()
    assert splitter.feed("Opening it now.Notepad is up.") == ["Opening it now."]
    assert splitter.flush() == ["Notepad is up."], "the second sentence went missing"


def test_the_same_sentence_twice_is_said_once_even_across_pieces():
    """A model asked the same empty question twice writes the same answer twice."""
    splitter = tts.SentenceStream()
    assert splitter.feed("Sure, what else?") == []          # still being written
    assert splitter.feed("Sure, what else?") == ["Sure, what else?"]
    assert splitter.flush() == [], "the same answer came round a second time"


def test_a_sentence_too_short_to_speak_alone_is_joined_on_not_dropped():
    """The short line at the top of a document used to be thrown away.

    A head under the splitter's minimum was skipped *after* being taken out of the
    buffer, so the words were gone rather than said with the sentence that followed:
    a title line, or a reply opening with "Yes.", simply went missing.
    """
    splitter = tts.SentenceStream()
    heard = splitter.feed("Cats. Cats sleep all day. They also decide when.")
    assert heard == ["Cats.", "Cats sleep all day."], heard
    assert splitter.flush() == ["They also decide when."]


def test_a_short_first_line_is_said_rather_than_lost_when_nothing_follows_it():
    splitter = tts.SentenceStream()
    assert splitter.feed("Cats. Cats sleep all day.") == []
    assert splitter.flush() == ["Cats.", "Cats sleep all day."]


# ---------- the ordered queue ------------------------------------------------


def test_sentences_are_spoken_in_the_order_they_arrived():
    stream, played = make_stream()
    stream.feed_many(["first one", "second one", "third one"])
    assert stream.finish(timeout=5)
    assert played == ["first one", "second one", "third one"]


def test_a_slow_first_sentence_does_not_reorder_the_rest():
    stream, played = make_stream(delay=0.2)
    stream.feed_many(["slow first", "second", "third"])
    assert stream.finish(timeout=6)
    assert played == ["slow first", "second", "third"]


def test_stopping_drops_what_was_queued_and_what_was_coming():
    stream, played = make_stream()
    stream._play = lambda rate, audio: (played.append(audio), stream.cancel())[1]
    stream.feed_many(["one", "two", "three", "four"])
    assert stream.finish(timeout=5) is False
    assert played == ["one"], "nothing queued after a stop may still be spoken"
    assert stream.feed("five") is False, "a stopped stream takes nothing more"


def test_the_next_sentence_is_synthesised_while_the_current_one_plays():
    """The gap *between* sentences is the latency that matters.

    Measured on this machine, one hosted sentence takes about a second to make and
    several seconds to say: synthesising after the previous sentence ends is a
    second of silence in the middle of an answer, so the round trip has to happen
    underneath the audio.
    """
    started = time.time()
    synth_started, played = [], []

    class SlowVoice:
        available = True

        def speech(self, text):
            synth_started.append(time.time())
            time.sleep(0.15)
            return 16000, text

    stream = tts.VoiceStream(voice=SlowVoice(), allow_cloud=True)

    def play(rate, audio):
        played.append((audio, time.time()))
        time.sleep(0.25)

    stream._play = play
    stream.feed_many(["one", "two", "three"])
    assert stream.finish(timeout=6)
    elapsed = time.time() - started
    assert [audio for audio, _at in played] == ["one", "two", "three"]
    assert len(synth_started) >= 2
    first_play_ends = played[0][1] + 0.25
    assert synth_started[1] < first_play_ends, \
        "the second sentence must be ready before the first one finishes"
    assert elapsed < 3 * (0.15 + 0.25), f"no overlap: {elapsed:.2f}s for three sentences"


def test_sentences_are_prepared_at_the_same_time_not_one_after_another():
    """Measured on this machine: 1.6 to 3.3 s to make a sentence, 0.8 to 1.7 s to say
    it. Laid end to end, a synthesiser that is that much slower than the voice falls
    further behind at every full stop - which is exactly what the owner heard. Three
    at once keeps the queue shorter than the answer.
    """
    guard = threading.Lock()
    live = [0]
    peak = [0]

    class SlowVoice:
        available = True

        def speech(self, text):
            with guard:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.25)
            with guard:
                live[0] -= 1
            return 16000, text

    stream = tts.VoiceStream(voice=SlowVoice(), allow_cloud=True)
    played = []
    stream._play = lambda rate, audio: played.append(audio)
    stream.feed_many(["one", "two", "three", "four"])
    assert stream.finish(timeout=8)
    assert peak[0] >= 2, f"only {peak[0]} sentence was being prepared at a time"
    assert played == ["one", "two", "three", "four"], "three at once, still in order"


def test_the_pause_between_two_sentences_is_a_few_milliseconds():
    """The complaint itself, as a number: "extremely long pauses between full stops".

    A hosted sentence takes longer to make than it takes to say, so the only way the
    gap can be the price of the handover is if several are being made at once. The
    bound is generous on purpose - what was measured before this was one to two
    seconds, and what is measured now is thousandths.
    """
    class SlowVoice:
        available = True

        def speech(self, text):
            time.sleep(0.3)
            return 16000, text

    stream = tts.VoiceStream(voice=SlowVoice(), allow_cloud=True)
    spans = []

    def play(rate, audio):
        start = time.monotonic()
        spans.append((start, start + 0.2))
        time.sleep(0.2)

    stream._play = play
    for number in range(6):
        stream.feed(f"sentence {number}")
        time.sleep(0.05)
    assert stream.finish(timeout=10)
    gaps = [spans[index][0] - spans[index - 1][1] for index in range(1, len(spans))]
    assert len(spans) == 6
    assert max(gaps) < 0.15, f"the longest pause between sentences was {max(gaps):.2f}s"


def test_a_piece_prepared_in_a_voice_the_reply_left_is_said_in_the_reply_s_voice(monkeypatch):
    """Two sentences are prepared at once, so the second can come back hosted while the
    first is still handing the reply to the local voice. Saying it as it is would put
    two voices in one answer - the "local and cloud" the owner heard - so it is said
    again, quietly, in the voice the reply settled on.
    """
    monkeypatch.setattr(tts, "VOICE_RETRY_S", 0.01)

    class MixedVoice:
        available = True

        def speech(self, text):
            if text == "the first line":
                raise tts.TtsError("HTTP 429: rate limit reached")
            time.sleep(0.12)                 # still in flight when the first line decides
            return 16000, "hosted:" + text

    class PreparingSpeaker(StubSpeaker):
        def __init__(self):
            super().__init__()
            self.prepared = []

        def prepare(self, text):
            self.prepared.append(text)
            return 16000, "local:" + text

    speaker = PreparingSpeaker()
    stream = tts.VoiceStream(speaker=speaker, voice=MixedVoice(), allow_cloud=True)
    played = []
    stream._play = lambda rate, audio: played.append(audio)
    stream.feed_many(["the first line", "the second line"])
    assert stream.finish(timeout=5)
    assert "the second line" in speaker.prepared, \
        "a hosted piece slipped into a reply that had gone local"
    assert played == ["local:the second line"]


def test_a_transient_failure_of_the_first_sentence_is_retried(monkeypatch):
    """One hiccup must not hand the whole reply to the other voice.

    The first sentence is the one that chooses the voice for the reply, so a rate limit
    or a 5xx gets one more try before the local voice takes over. Without it, a single
    429 turned a good answer into a local one.
    """
    monkeypatch.setattr(tts, "VOICE_RETRY_S", 0.01)
    voice = FakeVoice(fail_first=True)                 # one 500, then fine
    speaker = StubSpeaker()
    stream = tts.VoiceStream(speaker=speaker, voice=voice, allow_cloud=True)
    played = []
    stream._play = lambda rate, audio: played.append(audio)
    stream.feed_many(["the first line", "the second line"])
    assert stream.finish(timeout=5)
    # Two sentences are prepared at once, so the second line may be asked for before
    # the first has finished its retry - what matters is that the first is asked twice
    # and the reply is still played in one voice.
    assert voice.calls.count("the first line") == 2, voice.calls
    assert played == ["the first line", "the second line"], "the reply stayed in one voice"
    assert speaker.said == [], "nothing had to fall back to the local voice"


def test_one_reply_is_spoken_in_one_voice(monkeypatch):
    """A hosted voice that is really down speaks the whole reply locally, in order.

    It used to be decided sentence by sentence with the failure count reset after every
    success, so a reply that hit a rate limit alternated between the two voices - which
    is the "local and cloud overlap" the user heard in one answer.
    """
    monkeypatch.setattr(tts, "VOICE_RETRY_S", 0.01)
    voice = FakeVoice(fail_times=99, error="HTTP 429: rate limit reached")
    speaker = StubSpeaker()
    stream = tts.VoiceStream(speaker=speaker, voice=voice, allow_cloud=True)
    played = []
    stream._play = lambda rate, audio: played.append(audio)
    stream.feed_many(["the first line", "the second line", "the third line"])
    assert stream.finish(timeout=5)
    assert played == [], "nothing was synthesised once the hosted voice was out"
    assert speaker.said == ["the first line", "the second line", "the third line"]
    assert voice.calls.count("the first line") == 2, \
        "the first sentence is retried once, and then the voice is decided"
    assert len(voice.calls) <= 3, f"a dead voice was asked once too often: {voice.calls}"
    assert "429" in (stream.last_error or ""), "and the reason is kept"
    assert not stream.hosted


def test_a_refusal_that_will_not_pass_is_not_retried(monkeypatch):
    """A request the API will not take is not asked twice: that is a second of silence."""
    monkeypatch.setattr(tts, "VOICE_RETRY_S", 30.0)     # would be obvious if it slept
    voice = FakeVoice(fail_times=99, error="HTTP 400: invalid voice")
    stream = tts.VoiceStream(speaker=StubSpeaker(), voice=voice, allow_cloud=True)
    stream._play = lambda rate, audio: None
    started = time.time()
    stream.feed("the first line")
    assert stream.finish(timeout=5)
    assert voice.calls == ["the first line"]
    assert time.time() - started < 2.0


def test_a_rate_limit_tells_the_next_reply_not_to_ask_for_a_while():
    """The whole reason "very late responses" kept happening while the limit was on.

    The hosted voice is refused (429), the reply goes local - fine. What cost real time
    was that the *next* reply asked again, waited out another doomed round trip, and
    only then went local. One refusal now stops the asking for half a minute, so the
    first word comes back in a fraction of a second instead of after the refusal.
    """
    limited = FakeVoice(fail_times=99, error="HTTP 429: rate limit reached")
    first = tts.VoiceStream(speaker=StubSpeaker(), voice=limited, allow_cloud=True)
    first._play = lambda rate, audio: None
    first.feed("the first reply")
    assert first.finish(timeout=5)
    assert not first.hosted

    second_voice = FakeVoice()                       # a healthy, ready hosted voice
    second = tts.VoiceStream(speaker=StubSpeaker(), voice=second_voice, allow_cloud=True)
    second._play = lambda rate, audio: None
    assert not second.hosted, "a rate-limited hosted voice must not be asked again yet"
    second.feed("the second reply")
    assert second.finish(timeout=5)
    assert second_voice.calls == [], "it asked anyway, and paid the round trip"
    assert second.speaker.said == ["the second reply"], "...so the reply went local"

    tts.reset_backoff()                              # half a minute later
    third_voice = FakeVoice()
    third = tts.VoiceStream(speaker=StubSpeaker(), voice=third_voice, allow_cloud=True)
    third._play = lambda rate, audio: None
    assert third.hosted, "the hosted voice must come back on its own"
    third.feed("the third reply")
    assert third.finish(timeout=5)
    assert third_voice.calls == ["the third reply"], "the hosted voice is used again"


def test_the_circle_moves_for_the_local_voice_too():
    """Levels must reach the display whichever voice ended up speaking."""
    speaker = StubSpeaker()
    levels = []

    class LoudSpeaker(StubSpeaker):
        def __init__(self):
            super().__init__()
            self.on_level = self._report

        def _report(self, level):
            levels.append(level)

        def say(self, text, cap=None):
            self.said.append(text)
            self.on_level(0.7)                  # as the audio callback would
            return True

    stream = tts.VoiceStream(speaker=LoudSpeaker(), voice=None, on_level=levels.append,
                             allow_cloud=False)
    stream.feed("the local line")
    assert stream.finish(timeout=5)
    assert levels == [0.7], "the local voice's level never reached the stream's listener"
    assert stream.speaker.said == ["the local line"]


# ---------- the gap between two sentences ------------------------------------

def test_a_synthesiser_s_dead_air_is_trimmed_before_it_is_played():
    """The pause between two sentences was mostly the silence padding each clip arrives
    with - a few hundred milliseconds at 22 kHz, played in full, between every pair.
    """
    rate = 24000
    quiet = np.zeros(rate // 2, dtype="float32")          # 0.5 s of padding either side
    speech = np.zeros(rate, dtype="float32")
    speech[rate // 4:rate * 3 // 4] = 0.5                 # half a second of words
    out = tts.trim_silence(np.concatenate([quiet, speech, quiet]), rate)
    assert len(out) < len(speech) + rate * 0.2, \
        "the padding was played as a pause between two sentences"
    assert len(out) > rate * 0.4, "real speech was trimmed away with it"
    assert float(np.max(np.abs(out))) == pytest.approx(0.5), "the words were changed"


def test_a_clip_of_nothing_but_silence_is_not_trimmed_to_nothing():
    silence = np.zeros(1200, dtype="float32")
    assert len(tts.trim_silence(silence, 24000)) == len(silence)


def test_audio_that_cannot_be_read_is_handed_back_untouched():
    """A test's fake voice returns a string. A trim must never lose a sentence."""
    assert tts.trim_silence("not audio", 16000) == "not audio"
    assert tts.trim_silence(np.zeros((2, 2), dtype="float32"), 16000).shape == (2, 2)


def test_the_local_voice_is_rendered_before_its_turn_comes():
    """The local voice used to render each sentence *and* play it in one step, so the
    whole of SAPI's render time fell between two sentences that belong together.
    Rendering ahead is what makes them follow each other as they were written.
    """

    class PreparingSpeaker(StubSpeaker):
        def __init__(self):
            super().__init__()
            self.prepared = []

        def prepare(self, text):
            self.prepared.append(text)
            return 16000, "audio:" + text

    speaker = PreparingSpeaker()
    stream = tts.VoiceStream(speaker=speaker, voice=None, allow_cloud=False)
    played = []
    stream._play = lambda rate, audio: played.append(audio)
    stream.feed_many(["first line", "second line", "third line"])
    assert stream.finish(timeout=5)
    assert played == ["audio:first line", "audio:second line", "audio:third line"]
    assert speaker.prepared == ["first line", "second line", "third line"]
    assert speaker.said == [], "the local voice was rendered and played in one step again"


def test_a_local_voice_that_cannot_prepare_still_speaks_the_line():
    """The old path is the fallback, not a casualty: a speaker with no `prepare` is
    still asked to say the sentence."""
    speaker = StubSpeaker()
    stream = tts.VoiceStream(speaker=speaker, voice=None, allow_cloud=False)
    stream.feed("the local line")
    assert stream.finish(timeout=5)
    assert speaker.said == ["the local line"]


# ---------- telling the window the voice has stopped -------------------------

def test_the_voice_says_it_has_stopped_the_moment_it_has():
    """The orb stayed green for two or three seconds after the answer ended, because
    nothing told the session it was over: the watchdog looked twice a second and then
    waited for another 0.4 s of quiet. The stream says so itself now.
    """
    stopped = []
    stream = tts.VoiceStream(voice=FakeVoice(), allow_cloud=True,
                             on_idle=lambda: stopped.append(time.monotonic()))
    stream._play = lambda rate, audio: None
    started = time.monotonic()
    stream.feed("the only line")
    assert stream.finish(timeout=5)
    deadline = time.monotonic() + 2.0
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stopped, "nothing told the session the reply had ended"
    assert stopped[0] - started < 1.0, "the colour was told too late to be in time with it"


def test_the_colour_goes_back_the_moment_the_last_word_stops():
    """Green while sound is coming out, and not one second longer.

    The complaint was two or three seconds of green after the answer ended. The bound
    here is the grace itself plus the handover: the check is armed when the player finds
    nothing left, and it may not fire while the last clip is still playing.
    """
    ended, stopped = [], []
    stream = tts.VoiceStream(voice=FakeVoice(), allow_cloud=True,
                             on_idle=lambda: stopped.append(time.monotonic()))

    def play(rate, audio):
        time.sleep(0.2)
        ended.append(time.monotonic())

    stream._play = play
    stream.feed("the only line")
    assert stream.finish(timeout=5)
    deadline = time.monotonic() + 2.0
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stopped, "the circle was never told the voice had stopped"
    assert stopped[0] >= ended[0], "it went idle while the last word was still playing"
    assert stopped[0] - ended[0] < 0.45, \
        f"the colour stayed lit for {stopped[0] - ended[0]:.2f}s after the sound ended"


def test_the_gap_between_two_sentences_is_not_reported_as_the_end():
    """Armed between sentences, the check would flip the orb to its idle colour in the
    middle of an answer: a flicker, which is the same bug facing the other way."""
    events = []
    stream = tts.VoiceStream(voice=FakeVoice(delay=0.25), allow_cloud=True,
                             on_idle=lambda: events.append("idle"))
    stream._play = lambda rate, audio: None
    stream.feed_many(["first line", "second line", "third line"])
    assert stream.finish(timeout=8)
    time.sleep(0.4)
    assert events == ["idle"], f"the reply was reported over {len(events)} times"


# ---------- the session pipeline ---------------------------------------------


def fake_planner(*sentences):
    """An Agent whose brain streams a scripted reply, sentence by sentence."""

    def stream(messages, max_tokens=400, on_delta=None):
        text = " ".join(sentences)
        halfway = speakable_half(sentences)
        if on_delta is not None:
            on_delta(halfway)
            on_delta(text[len(halfway):])
        return text

    return Agent(complete=lambda messages, max_tokens=300: " ".join(sentences),
                 conversational=True, stream=stream)


def speakable_half(sentences):
    """The first sentence with its full stop, as a streaming model would send it."""
    return (sentences[0] + " ") if sentences else ""


def test_a_streamed_reply_is_spoken_piece_by_piece_and_never_twice():
    spoken = []
    session = Session()
    session.speaker = StubSpeaker(spoken)
    session.converse = True
    session.planner = fake_planner("Hello there.", "How are you today?")
    task = session._begin("hello there")
    result = session._work(task, "hello there")
    assert result.ok
    assert spoken == ["Hello there.", "How are you today?"], spoken
    assert len(spoken) == 2, "the reply was spoken once, not twice"


def test_the_reply_reported_to_the_window_is_the_whole_answer():
    session = Session()
    session.speaker = StubSpeaker()
    session.converse = True
    session.planner = fake_planner("Yes, that works.")
    shown = []
    session.on_state(lambda state, detail="": None)

    def on_reply(text, ok):
        shown.append((text, ok))

    session._on_reply = on_reply
    task = session._begin("does that work")
    session._work(task, "does that work")
    assert shown and shown[0][0] == "Yes, that works." and shown[0][1]


def test_an_answer_said_with_no_stream_is_not_said_again_at_the_end():
    """The repeated line, and the only way it could happen on this machine.

    A barge clears the request's speech stream - and on this machine the echo barges in
    on nearly every reply (183 barge_in events in one session). A sentence that then
    reaches the voice through `speak_async`, because there is no longer a stream to keep
    it in order, used to be spoken and left out of `_spoken`, which is the list the reply
    is checked against before it is said. So the whole answer came out a second time,
    in full, through the local voice, with nothing to compare it against.
    """
    spoken = []
    session = Session()
    session.speaker = StubSpeaker(spoken)
    session.converse = True
    session.planner = fake_planner("One thing.", "Then another.")
    task = session._begin("tell me two things")
    session._cancel_speech()                  # what a barge does to the stream
    result = session._work(task, "tell me two things")
    assert result.ok
    assert spoken == ["One thing.", "Then another."], spoken


def test_a_question_that_needs_a_tool_goes_to_the_agent_not_the_chat_path():
    """\"open notepad ...\" must never be answered with words instead of opened.

    The sentence has to be one the typed rules would match *and* one that describes a
    job: a single \"open notepad\" is answered by the rule that opens it, and a
    \"website\" is a project, which is the builder's route. The gap between those two
    is exactly this one - a job with several steps - and it has to reach the agent.
    """
    answered = []
    session = Session()
    session.speaker = StubSpeaker()
    session.converse = True

    class Planner:
        conversational = True
        available = True

        def respond(self, text, task=None, emit=None):
            answered.append(text)
            return "sure"

        def run(self, text, task=None, note=None, mode=None):
            return Result(True, "Opened Notepad.")

    session.planner = Planner()
    # The rules match the first verb, but the sentence describes a job - open, then
    # type - so it belongs to the agent and must not stop at the first match.
    task = session._begin("open notepad and type hello")
    result = session._work(task, "open notepad and type hello")
    assert answered == [], "an action request is not a conversation"
    assert result.ok and "Notepad" in result.message


def test_one_utterance_is_run_once(monkeypatch):
    session = Session()
    session.speaker = StubSpeaker()
    calls = []
    monkeypatch.setattr(session, "handle_text",
                        lambda text, task=None: calls.append(text) or Result(True, "ok"))
    task = session.dispatch("open notepad")
    assert task is not None
    deadline = time.time() + 3
    while time.time() < deadline and not calls:
        time.sleep(0.02)
    time.sleep(0.2)
    assert calls == ["open notepad"], "one utterance, one reply"


def test_cancelling_clears_the_speech_that_was_queued():
    session = Session()
    session.speaker = StubSpeaker()
    stream = session._begin_speech()
    assert stream is not None
    stream.feed("something that should not be finished")
    task = session._begin("a long one")
    ok, message = session.cancel_task()
    assert ok and "Stopped" in message
    assert session._voice_stream is None, "the stream is gone with the task"
    assert task.cancelled


# ---------- the wake word and the follow-up setting --------------------------


def test_the_wake_word_is_required_by_default():
    assert voice.classify(False, "open notepad") == ("ignore", None)
    assert voice.classify(False, "Retro, open notepad") == ("command", "open notepad")


def test_with_follow_ups_off_being_awake_does_not_help():
    assert voice.classify(True, "open notepad", followup=False) == ("ignore", None)


def test_with_follow_ups_on_a_command_needs_no_wake_word():
    assert voice.classify(True, "open notepad", followup=True) == ("command", "open notepad")


@pytest.mark.parametrize("heard", [".", ",", "…", "-", "...", " ?!", ". , ."])
def test_a_transcript_with_no_word_in_it_is_not_a_command(heard):
    """The "it reads out full stops" complaint, at the point the full stop is invented.

    Against the real endpoint, two seconds of a quiet room comes back as `'.'` -
    confidently: `no_speech_prob 0`, `avg_logprob -0.45`, `compression_ratio 0.2`. So no
    confidence threshold can refuse it, and while awake this used to fall through
    `classify` to "command" and be carried out.
    """
    assert voice.classify(True, heard) == ("ignore", None), "awake and no wake word"
    assert voice.classify(False, heard) == ("ignore", None), "asleep and not for us"
    assert voice.classify(True, "Retro " + heard) == ("wake", None), \
        "the wake word with a full stop after it is a wake - it is still not a command"
    # And the guard does not cost a real one: a sentence with words is a sentence.
    assert voice.classify(True, "open notepad") == ("command", "open notepad")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    settings.forget()
    yield
    settings.forget()


def test_a_spoken_mode_change_changes_the_real_mode(isolated_settings):
    session = Session()
    session.speaker = StubSpeaker()
    assert session.mode == modes.WORK
    assert session.dispatch("Retro, switch to chat mode") is None
    assert session.mode == modes.CHAT
    assert settings.get("mode") == "chat"
    assert session.dispatch("work mode") is None
    assert session.mode == modes.WORK


def test_listen_after_retro_is_a_setting_with_a_window(isolated_settings):
    session = Session()
    session.speaker = StubSpeaker()
    assert session.followup is False
    assert session._stay_awake() is False
    session.dispatch("listen after retro")
    assert session.followup is True and settings.get("followup") is True
    assert session._stay_awake() is True
    session.dispatch("stop listening after retro")
    assert session.followup is False and session._stay_awake() is False
    assert settings.get("followup") is False
