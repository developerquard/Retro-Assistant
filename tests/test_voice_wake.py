import numpy as np
import pytest

from core.voice import (SPEECH_PEAK, SPEECH_PROMPT, TARGET_PEAK, find_hindi_voice,
                        normalize_peak, pick_voice, strip_wake)


@pytest.mark.parametrize("heard,expected", [
    ("Retro open notepad", "open notepad"),
    ("Retro", ""),
    ("retro.", ""),
    ("Hey Retro, what time is it", "what time is it"),
    ("रेट्रो खोलो", "खोलो"),
    ("What time is it", None),
    ("metro station", None),
    ("return home", None),
])
def test_strip_wake(heard, expected):
    assert strip_wake(heard) == expected


def test_normalize_peak_lifts_real_speech_to_the_target():
    audio = np.full(1600, 0.1, dtype="float32")
    out = normalize_peak(audio)
    assert abs(float(np.max(np.abs(out))) - TARGET_PEAK) < 1e-5


def test_a_phrase_this_microphone_really_delivers_reaches_the_decoder():
    """The regression, at the level it happened, in the units it happened in.

    0.03 is what this machine's microphone array hands over for speech across the
    room - measured with `tools/mic_e2e.py`, against a line the speakers put out at
    0.69. It is above the speech floor, so it is speech, and it has to arrive at the
    decoder as speech: a fixed 0.9 peak. Sent unchanged it is thirty times quieter
    than that, and the endpoint answers with its own prompt or with nothing.
    """
    for peak in (SPEECH_PEAK, 0.03, 0.09):
        audio = np.full(1600, peak, dtype="float32")
        out = float(np.max(np.abs(normalize_peak(audio))))
        assert out == pytest.approx(TARGET_PEAK, abs=1e-4), \
            f"a phrase at {peak} was handed over at {out:.3f}"


def test_normalize_peak_leaves_near_silence_untouched():
    """And the guard stays: the room is never amplified to look like speech."""
    quiet = np.full(1600, SPEECH_PEAK / 10, dtype="float32")
    assert np.array_equal(normalize_peak(quiet), quiet)


def test_speech_prompt_biases_the_wake_word_and_holds_no_command():
    """The prompt may not contain a word that could be carried out.

    This test used to assert the opposite - that the prompt listed notepad, timer,
    battery, volume and internet - and that is the bug it was protecting. Whisper
    treats `prompt` as text to continue, so on a phrase it cannot read it writes the
    prompt back out. The live log is that sentence arriving as commands: "Commands,
    you're free to go.", "Commands, I'm going to go to the", "Commands, what time is
    it, it's time to " - at peaks of 0.44 to 0.70, loud enough to beat every level
    gate - and every one of them was dispatched as a request and answered out loud.
    A prompt is now the wake word and nothing else, so the worst a recitation can
    produce is "Retro".
    """
    text = SPEECH_PROMPT.lower()
    assert text.startswith("retro"), "the wake word is what it is for"
    for word in ("notepad", "timer", "battery", "volume", "internet", "commands",
                 "search", "volume", "mute", "pause", "file"):
        assert word not in text, f"'{word}' could be recited and then carried out"
    assert len(SPEECH_PROMPT.split()) <= 2, "a sentence is a thing to continue"


class FakeVoice:
    def __init__(self, id, name, languages):
        self.id, self.name, self.languages = id, name, languages


ENGLISH = [FakeVoice("TTS_MS_EN-GB_HAZEL_11.0", "Microsoft Hazel Desktop", ["en-GB"]),
           FakeVoice("TTS_MS_EN-US_ZIRA_11.0", "Microsoft Zira Desktop", ["en-US"])]
HINDI = FakeVoice("TTS_MS_HI-IN_HEERA_11.0", "Microsoft Heera", ["hi-IN"])


def test_pick_voice_honours_the_configured_name():
    assert pick_voice(ENGLISH, "zira").name == "Microsoft Zira Desktop"
    assert pick_voice(ENGLISH, "Hazel").name == "Microsoft Hazel Desktop"


def test_pick_voice_falls_back_to_an_english_voice():
    assert pick_voice(ENGLISH, "NotInstalled").name == "Microsoft Hazel Desktop"


def test_pick_voice_survives_having_no_voices():
    assert pick_voice([], "Zira") is None


def test_find_hindi_voice_reports_none_by_default():
    assert find_hindi_voice(ENGLISH) is None
    assert find_hindi_voice(ENGLISH + [HINDI]) is HINDI
