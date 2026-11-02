"""Listening: the hosted Whisper, and the local model it falls back to.

No network and no faster-whisper here: the transport is injected and the local
model is a stub, so the fallback itself is what gets tested.
"""
import numpy as np
import pytest

from core import config, voice
from core.voice import CloudEars, Ears, build_ears, multipart_body, read_wav_mono, wav_bytes


class StubEars:
    """Stands in for the local model, recording that it was asked."""

    def __init__(self, text="local transcript"):
        self.text = text
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text


def speech(seconds=0.2, rate=voice.SAMPLE_RATE):
    """A loud-enough tone, so normalise_peak treats it as speech."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    return (0.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)


def poster_returning(payload, calls=None):
    calls = [] if calls is None else calls

    def poster(url, body, boundary):
        calls.append({"url": url, "body": body, "boundary": boundary})
        return payload

    return poster, calls


def poster_failing(error, calls=None):
    calls = [] if calls is None else calls

    def poster(url, body, boundary):
        calls.append({"url": url, "body": body, "boundary": boundary})
        raise error

    return poster, calls


# ---------- the request ------------------------------------------------------

def test_the_wav_is_a_real_wav_that_reads_back():
    audio = speech()
    blob = wav_bytes(audio)
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"
    import io
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "x.wav"
        path.write_bytes(blob)
        rate, back = read_wav_mono(path)
    assert rate == voice.SAMPLE_RATE
    assert len(back) == len(audio)
    assert abs(float(np.max(np.abs(back))) - 0.4) < 0.02, "the audio must survive the trip"


def test_the_form_carries_the_model_the_prompt_and_the_audio():
    boundary = "----RetroTest"
    body = multipart_body({"model": "whisper-large-v3-turbo", "prompt": "Retro",
                           "response_format": "json"}, "speech.wav", b"RIFFxxxx", boundary)
    assert body.startswith(f"--{boundary}".encode())
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
    assert b'name="prompt"' in body
    assert b'name="file"; filename="speech.wav"' in body
    assert b"Content-Type: audio/wav" in body
    assert b"RIFFxxxx" in body


def test_it_posts_to_the_transcription_endpoint_with_the_wav_and_reads_the_text():
    poster, calls = poster_returning({"text": "open notepad"})
    ears = CloudEars(key="k", poster=poster)
    assert ears.transcribe(speech()) == "open notepad"
    assert calls[0]["url"] == config.CLOUD_BASE_URL + "/audio/transcriptions"
    assert b"RIFF" in calls[0]["body"]
    assert calls[0]["boundary"] in calls[0]["body"].decode("utf-8", "replace")


def test_the_audio_is_normalised_before_it_is_sent():
    """A quiet phrase is lifted to full scale first, or the model mis-hears it."""
    poster, calls = poster_returning({"text": "hi"})
    CloudEars(key="k", poster=poster).transcribe(speech() * 0.2)   # quieter than a mic
    body = calls[0]["body"]
    wav = body[body.index(b"RIFF"):]
    size = int.from_bytes(wav[40:44], "little")          # the data chunk header
    pcm = np.frombuffer(wav[44:44 + size], dtype="<i2")
    assert len(pcm) == len(speech())
    assert int(np.max(np.abs(pcm.astype(np.int32)))) > 25000, \
        "a quiet phrase should be lifted to full scale first"


# ---------- falling back to the local model ---------------------------------

def test_no_key_means_the_local_model_listens(monkeypatch):
    monkeypatch.setattr(config, "cloud_key", lambda: None)
    poster, calls = poster_returning({"text": "never"})
    local = StubEars()
    ears = CloudEars(poster=poster, local=local)
    assert not ears.available
    assert ears.transcribe(speech()) == "local transcript"
    assert local.calls == 1 and calls == [], "with no key nothing should be sent anywhere"


def test_an_empty_transcript_falls_back_to_the_local_model():
    poster, _ = poster_returning({"text": "   "})
    local = StubEars()
    ears = CloudEars(key="k", poster=poster, local=local)
    assert ears.transcribe(speech()) == "local transcript"
    assert local.calls == 1


def test_a_failed_request_falls_back_instead_of_losing_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    poster, _ = poster_failing(RuntimeError("connection reset"))
    local = StubEars()
    ears = CloudEars(key="k", poster=poster, local=local)
    assert ears.transcribe(speech()) == "local transcript"
    assert local.calls == 1
    assert "connection reset" in ears.last_error
    # The fallback must be visible in the log, not silent.
    assert "stt_fallback" in (tmp_path / "Logs" / "retro.log").read_text(encoding="utf-8")


def test_the_local_model_is_loaded_only_if_it_is_actually_needed():
    poster, _ = poster_returning({"text": "from the cloud"})
    ears = CloudEars(key="k", poster=poster)
    assert ears._local is None, "loading a second Whisper up front would waste RAM"
    ears.transcribe(speech())
    assert ears._local is None, "and a working cloud reply must never load it"
    assert ears.remote_calls == 1 and ears.local_calls == 0


# ---------- choosing ---------------------------------------------------------

def test_a_key_selects_the_hosted_whisper_and_no_key_selects_the_local_one(monkeypatch):
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    assert isinstance(build_ears(), CloudEars)
    monkeypatch.setattr(config, "cloud_available", lambda: False)
    ears = build_ears()
    assert isinstance(ears, Ears) and not isinstance(ears, CloudEars)


def test_the_local_ears_still_exist_and_still_work(monkeypatch):
    """Ears is what the tools and tests build; it must not have been removed."""
    assert issubclass(CloudEars, object)
    assert callable(Ears)
    assert not issubclass(CloudEars, Ears), "the cloud ears wrap, they do not inherit"


@pytest.mark.parametrize("bad", [{"text": None}, {}, {"text": 5}])
def test_a_reply_with_no_usable_text_falls_back(bad):
    poster, _ = poster_returning(bad)
    local = StubEars()
    ears = CloudEars(key="k", poster=poster, local=local)
    assert ears.transcribe(speech()) == "local transcript"
    assert local.calls == 1


# ---------- a transcript nobody said -----------------------------------------


def segment(text, **numbers):
    """One segment the way the hosted endpoint reports it."""
    return {"text": text, "avg_logprob": -0.1, "compression_ratio": 1.6,
            "no_speech_prob": 0.01, **numbers}


def test_the_hosted_reply_is_asked_for_with_the_decoders_own_numbers():
    """`verbose_json` is not cosmetic: it is where no_speech_prob comes from.

    Without it the transcript is the only thing that comes back, and a transcript of
    a fridge and a transcript of the owner look identical.
    """
    poster, calls = poster_returning({"text": "open notepad", "segments": [segment("open notepad")]})
    CloudEars(key="k", poster=poster).transcribe(speech())
    body = calls[0]["body"]
    assert b"verbose_json" in body, "the segments carry the fields the guard reads"
    assert b"temperature" in body


def test_a_segment_the_model_believes_is_silence_is_not_a_transcript():
    """"Thank you." is what Whisper says to a room with nobody in it."""
    poster, _ = poster_returning({"segments": [segment("Thank you.", no_speech_prob=0.94)]})
    local = StubEars()
    ears = CloudEars(key="k", poster=poster, local=local)
    assert ears.transcribe(speech()) == "", "silence is not a command"
    assert local.calls == 0, \
        "asking a second decoder to invent something for the same audio is not a fix"


def test_a_segment_read_with_no_confidence_is_not_a_transcript():
    poster, _ = poster_returning({"segments": [segment("Oh, finally.", avg_logprob=-1.8)]})
    ears = CloudEars(key="k", poster=poster)
    assert ears.transcribe(speech()) == ""


def test_a_repetitive_segment_is_not_a_transcript():
    """Repetition is the shape every hallucination takes, and Whisper says so."""
    poster, _ = poster_returning({"segments": [segment("I love you. I love you.",
                                                       compression_ratio=3.1)]})
    ears = CloudEars(key="k", poster=poster)
    assert ears.transcribe(speech()) == ""


def test_a_real_transcript_survives_the_guard():
    poster, _ = poster_returning({"segments": [segment(" Retro, open notepad.")]})
    ears = CloudEars(key="k", poster=poster)
    assert ears.transcribe(speech()) == "Retro, open notepad."


def test_one_bad_segment_does_not_throw_away_the_good_ones():
    """A phrase with a cough in it still holds the words that were said."""
    poster, _ = poster_returning({"segments": [
        segment(" Thank you.", no_speech_prob=0.9), segment(" open notepad.")]})
    ears = CloudEars(key="k", poster=poster)
    assert ears.transcribe(speech()) == "open notepad."


def test_an_endpoint_that_reports_no_numbers_is_still_believed():
    """A missing field is not a failure: the fields that are there are what decide."""
    from core.voice import understood
    assert understood([{"text": "open notepad"}]) == "open notepad"
    assert understood([{"text": "open notepad", "no_speech_prob": 0.99}]) == ""
    assert understood([]) == ""
    assert understood([{"text": ""}]) == ""


# ---------- the microphone that only speaks 48 kHz ---------------------------
#
# The WASAPI microphone array on this machine accepts 48 000 Hz and refuses 16 000
# with `Invalid sample rate` (measured, tools/acceptance_check.py --mic). Opening it
# at the rate the recogniser wants would therefore have made a device that never
# heard a word - the exact symptom the microphone picker was added to cure - so the
# device's rate is asked for first and the audio is brought down to 16 kHz.

def test_the_rate_a_microphone_accepts_is_asked_for_before_opening_it(monkeypatch):
    import sounddevice as sd
    voice.forget_rates()

    def check_input_settings(device=None, samplerate=None, channels=None, dtype=None):
        if samplerate != 48000:
            raise RuntimeError("Invalid sample rate")

    monkeypatch.setattr(sd, "query_devices", lambda device: {"default_samplerate": 48000.0})
    monkeypatch.setattr(sd, "check_input_settings", check_input_settings)
    assert voice.capture_rate(4) == 48000
    assert voice.input_usable(4) is True, "48 kHz-only is usable, it is not deaf"
    assert voice.capture_rate(None) == 48000, "the default device is asked the same way"


def test_a_microphone_that_accepts_no_rate_is_not_usable(monkeypatch):
    import sounddevice as sd
    voice.forget_rates()

    def refuse(**_kwargs):
        raise RuntimeError("no stream here")

    monkeypatch.setattr(sd, "query_devices", lambda device: {"default_samplerate": 0.0})
    monkeypatch.setattr(sd, "check_input_settings", refuse)
    assert voice.capture_rate(9) is None
    assert voice.input_usable(9) is False


def test_a_48_khz_microphone_is_listened_to_at_16_khz(monkeypatch):
    """The listener opens what the device takes and hands the ears 16 kHz audio."""
    import threading
    import time
    import sounddevice as sd
    voice.forget_rates()
    opened = {}

    class LoudInput:
        def __init__(self, **kwargs):
            opened.update(kwargs)
            self.callback = kwargs.get("callback")

        def __enter__(self):
            def push():
                loud = np.full((1024, 1), 0.4, dtype="float32")
                for _ in range(20):                  # ~0.43 s of speech at 48 kHz
                    time.sleep(0.02)
                    self.callback(loud, 1024, None, None)
                quiet = np.zeros((1024, 1), dtype="float32")
                for _ in range(40):                  # and the silence that ends it
                    time.sleep(0.02)
                    self.callback(quiet, 1024, None, None)

            threading.Thread(target=push, daemon=True).start()
            return self

        def __exit__(self, *exc):
            return False

    heard = {}

    class RecordingEars:
        def transcribe(self, audio):
            heard["samples"] = len(audio)
            return "retro what time is it"

    def only_48k(device=None, samplerate=None, channels=None, dtype=None):
        if samplerate != 48000:
            raise RuntimeError("Invalid sample rate")

    monkeypatch.setattr(sd, "InputStream", LoudInput)
    monkeypatch.setattr(sd, "query_devices", lambda device: {"default_samplerate": 48000.0})
    monkeypatch.setattr(sd, "check_input_settings", only_48k)
    text = voice.listen_once(RecordingEars(), timeout_s=5.0, device=3)
    assert opened.get("samplerate") == 48000, \
        f"the stream was opened at {opened.get('samplerate')}, not the device's own rate"
    assert text == "retro what time is it"
    # 20 loud blocks at 48 kHz are 0.43 s of speech, and the phrase keeps the trailing
    # silence up to END_SILENCE_S (0.6 s) before it ends - so about a second of 16 kHz
    # audio. Read as 16 kHz instead of 48 kHz the same push would be 0.34 s: the check
    # is that the conversion went the right way, three input samples to one output.
    seconds = heard["samples"] / float(voice.SAMPLE_RATE)
    assert 0.95 <= seconds <= 1.15, (
        f"the ears got {seconds:.2f}s of audio from 20 48 kHz blocks - 1.02s expected, "
        "0.34s if the blocks were mistaken for 16 kHz")


def test_the_seam_between_blocks_is_not_a_phase_restart():
    """Blocks are interpolated as one signal: a ramp stays a ramp, with no sawtooth.

    Restarting the resampler at every block is the cheap version, and it clicks 47
    times a second; a ramp makes that visible as a backwards jump in the middle of
    the stream. Continuity is asserted, and with it the pitch the recogniser sees.
    """
    down = voice.Down16(44100)
    out = [down.feed(np.arange(i, i + 1024, dtype="float32")) for i in range(0, 4096, 1024)]
    joined = np.concatenate([piece for piece in out if len(piece)])
    steps = np.diff(joined)
    assert steps.min() > 0, "the stream jumped backwards where two blocks met"
    assert steps.max() / steps.min() < 1.001, "the sample spacing is not constant"
    assert abs(len(joined) - 4096 * voice.SAMPLE_RATE / 44100.0) <= 1, \
        "the blockwise rate conversion lost or gained samples"
