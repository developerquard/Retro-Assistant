"""Voice in and out. Audio and speech packages are imported inside the functions,
so the typed core and its tests don't need them installed.

Only audio that passes the loudness gate is transcribed. Transcripts that don't
contain the wake word are dropped and never written to disk.
"""
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections import namedtuple
from contextlib import contextmanager

from . import config, log

SAMPLE_RATE = config.SAMPLE_RATE
BLOCK = config.BLOCK
START_RMS = config.START_RMS   # measured by tools/calibrate_voice.py
MIN_PEAK = config.MIN_PEAK     # a phrase quieter than this is noise, so it isn't transcribed

# Biases the decoder towards the words Retro actually hears. Measured: the wake word
# was recognised 0/10 times without it and 4/10 with it, on the same clips.
#
# It is one word, and that is the whole point. Whisper treats `prompt` as text to
# *continue*: given a phrase it cannot read - the tail of Retro's own answer, a door,
# a cough, a room noise - it writes the prompt back out rather than admitting it heard
# nothing. The old prompt here was a sentence, "Retro. Commands: what time is it,
# today's date, battery, CPU and RAM, ...", and the live log is exactly that sentence
# coming back: "Commands, you're free to go.", "Commands, I'm going to go to the",
# "Commands, what time is it, it's time to " - at peaks of 0.44 to 0.70, loud enough
# to beat every level gate there is - and each one was dispatched as a request and
# answered out loud. So a prompt may not contain a single word that could be carried
# out. "Retro." still biases the wake word, and the worst a recitation of it can
# produce is "Retro", which asks for attention and does nothing.
SPEECH_PROMPT = "Retro."

# Whisper's own three defaults for deciding a segment is not speech it understood.
# They are used twice: as the decoder's thresholds, and - because the hosted API's
# numbers come back in the response rather than through a decoder we control - as a
# filter over the segments of whatever the decoder produced.
NO_SPEECH_PROB = 0.6        # a segment the model believes is silence
MIN_LOGPROB = -1.0          # read with too little confidence to be a command
MAX_COMPRESSION = 2.4       # repetitive output: the shape every hallucination takes
# One floor, two uses: `listen_once` refuses a phrase quieter than this, and
# `normalize_peak` lifts anything at or above it to TARGET_PEAK. They are the same
# number because they are the same question - is this speech, or is this the room -
# and having two of them is how a phrase came to be accepted as speech and then handed
# to the recogniser at four per cent of full scale, which the decoder answers with its
# own prompt or with silence.
SPEECH_PEAK = MIN_PEAK
TARGET_PEAK = 0.9              # the level the recogniser is handed, always
END_SILENCE_S = 0.6         # quiet this long ends a phrase
BARGE_GAIN = 1.6            # louder than the echo threshold by this much to cut in
BARGE_S = 0.12              # and held for this long (two audio blocks at 16 kHz)
MAX_PHRASE_S = 4.0
MIN_PHRASE_S = 0.3
# A phrase in progress with no block for this long means the stream itself stopped -
# the device was unplugged, re-configured, or its callback died. Blocks arrive every
# 30 ms, so a second of nothing is thirty-three blocks missing, not a quiet room.
STALL_S = 1.0
GREETING = "Hey, I'm listening."
SLEEP_REPLY = "Okay, going to sleep. Say Retro when you need me."
WAKE = re.compile(r"\b(re|ri)t+r?o+\b|\bretr|रेट्रो|रिट्रो", re.I)

# "Retro, sleep" ends the awake session, so Retro only listens for its name again.
SLEEP_WORDS = ("sleep", "go to sleep", "stop listening", "deep sleep", "standby", "stand by",
               "so jao", "so ja", "so jaa", "बंद हो जाओ", "सो जाओ", "सो जा")


def strip_wake(text):
    """Words after the wake word. '' if only the wake word was said. None if it isn't there."""
    m = WAKE.search(text)
    if not m:
        return None
    return " ".join(text[m.end():].split()).strip(" ,.!?:;।-")


def is_sleep(text):
    """True for "sleep", "Retro, go to sleep", "so jao" and friends."""
    if not text:
        return False
    t = " ".join(text.lower().replace("’", "'").split())
    return any(w in t for w in SLEEP_WORDS)


def classify(awake, heard, followup=True, control=None):
    """Decide what one transcript means, with no audio involved.

    Returns (kind, text):
      "ignore"  - not for Retro, or nothing to do
      "wake"    - speak the greeting and stay awake
      "sleep"   - speak the sleep reply and go back to sleep
      "command" - run `text`

    Asleep, anything without the wake word is dropped, so ordinary conversation is
    never acted on. Awake and listening for follow-ups, the wake word is optional -
    and `followup` False overrides even that: the setting is the policy, and it is
    enforced here, in the one function that decides what a transcript means, so
there is no second place where "is it awake" is decided.

    `control` is the one exception to the wake word, and it only exists while a
    request is running: a callable that says whether a phrase is one of the words
    that steer a job already in flight - stop, carry on, what are you doing. Saying
    "stop" to something that is visibly churning away is not small talk, and being
    told to say "Retro" first is exactly what made the assistant look like it was
    ignoring the user. Ordinary sentences still need the wake word, so a
    conversation in the room cannot hijack a running request.
    """
    if not followup:
        awake = False
    if not heard or not heard.strip():
        return "ignore", None
    if not words_only(heard):
        # A full stop, a comma, an ellipsis. The recogniser invents these out of a
        # quiet room and asserts them - see `words_only` - and while awake this used to
        # fall through to "command", so Retro was asked to carry out a punctuation
        # mark and answered something. There is no request here; there is no word.
        return "ignore", None
    plain = " ".join(str(heard).lower().replace("’", "'").split())
    rest = strip_wake(heard)
    if rest is not None and not words_only(rest):
        # "Retro …", "Retro ," - the wake word with nothing but an invented mark after
        # it. That is the owner saying the wake word, not asking for an ellipsis to be
        # carried out, so it is treated as the bare wake word it is meant to be.
        rest = ""
    if not awake:
        if rest is None:
            if control is not None and control(plain):
                return "command", plain   # a control word during a running request
            return "ignore", None          # no wake word while asleep: not for us
        if is_sleep(rest):
            return "ignore", None          # "Retro, sleep" when already asleep
        if rest == "":
            return "wake", None
        return "command", rest             # "Retro, open notepad" in one breath
    if is_sleep(heard) or is_sleep(rest or ""):
        return "sleep", None
    if rest == "":
        return "wake", None                # just "Retro" again: greet and keep listening
    return "command", rest or heard


def _field(segment, name, default=None):
    """One field of a segment, from either shape the two decoders hand back.

    The hosted Whisper answers with JSON - plain dicts - and faster-whisper with its
    own Segment objects. The same numbers are behind both, so the honesty check below
    is written once and reads either.
    """
    if isinstance(segment, dict):
        return segment.get(name, default)
    return getattr(segment, name, default)


def _unbelievable(segment):
    """True when the decoder's own numbers say this segment is not speech it heard.

    Three tests, and all three are Whisper's own defaults rather than numbers invented
    here: the segment is probably silence (`no_speech_prob`), it was read with almost
    no confidence (`avg_logprob`), or it is repetitive, which is the shape a
    hallucination always takes (`compression_ratio`).

    A missing number is not a failure: an endpoint that does not report one is tested
    on the ones it does.
    """
    no_speech = _field(segment, "no_speech_prob")
    if isinstance(no_speech, (int, float)) and no_speech >= NO_SPEECH_PROB:
        return True
    logprob = _field(segment, "avg_logprob")
    if isinstance(logprob, (int, float)) and logprob <= MIN_LOGPROB:
        return True
    ratio = _field(segment, "compression_ratio")
    if isinstance(ratio, (int, float)) and ratio >= MAX_COMPRESSION:
        return True
    return False


def words_only(text):
    """True when a transcript actually contains a word.

    A transcript with no letter and no digit in it is not speech, whatever the model
    thought it was doing: it is a full stop, a comma, a stray dash, an ellipsis. This
    is the one rule that catches the "it reads out full stops" complaint at the point
    where the full stop is invented.

    Measured on this machine, against the real endpoint, with two seconds of a quiet
    room: the reply was `'.'`, and the decoder's own numbers were confident -
    `no_speech_prob 0`, `avg_logprob -0.45`, `compression_ratio 0.2`. Whisper does not
    merely guess a punctuation mark from silence, it asserts one, so no confidence
    threshold can refuse it and no word-matching guard downstream can match it. The
    only honest test is the obvious one: a command is made of words.
    """
    return any(ch.isalnum() for ch in str(text or ""))


def understood(segments):
    """The words the decoder can stand behind - the rest is dropped, not guessed.

    This is where a hallucination is stopped at the source rather than argued with
    afterwards, and it is the answer to the owner's question of *why* the model produced
    the phrase in the first place. Two things were wrong, and both are decided here:

    * the recogniser was handed a sentence of commands as its `prompt` and wrote it
      back out (see SPEECH_PROMPT), and
    * on audio that held no speech at all, it invented a phrase or a lone punctuation
      mark - and asserted it, with the confidence of a sentence it had really heard.

    Every dropped segment is written to the log with the numbers that dropped it, so
    the next reader can see which rule decided and why, rather than being told the
    assistant heard nothing.
    """
    kept = []
    for segment in segments or ():
        text = str(_field(segment, "text", "") or "").strip()
        if not text:
            continue
        if not words_only(text):
            log.event("stt_dropped", why="no words", text=text[:24],
                      no_speech_prob=_field(segment, "no_speech_prob"),
                      avg_logprob=_field(segment, "avg_logprob"),
                      compression_ratio=_field(segment, "compression_ratio"))
            continue
        if _unbelievable(segment):
            log.event("stt_dropped", why="not speech", text=text[:40],
                      no_speech_prob=_field(segment, "no_speech_prob"),
                      avg_logprob=_field(segment, "avg_logprob"),
                      compression_ratio=_field(segment, "compression_ratio"))
            continue
        kept.append(text)
    return " ".join(kept).strip()


def normalize_peak(audio):
    """Even out loud and quiet speech so the decoder sees a consistent level.

    Near-silence is deliberately left alone: amplifying noise to full scale is what
    makes Whisper invent phrases out of nothing.

    The floor this turns on used to be 0.05, and that is why a working microphone
    stopped being heard. A microphone across a room, or a laptop array running its own
    gain control, hands over real words at a peak of 0.03 or so - and audio below the
    floor was passed to the recogniser at four per cent of full scale, where the first
    thing it does is answer with its own prompt or with nothing at all. The floor is
    now the same number `listen_once` uses to decide a phrase is speech, so anything
    that gets as far as the recogniser is also given a level it can read.
    """
    import numpy as np
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak >= SPEECH_PEAK:
        return audio * (TARGET_PEAK / peak)
    return audio


class Ears:
    """Speech to text on this machine, with faster-whisper."""

    def __init__(self, model_size=None):
        # The model is loaded on first use, not here. Building the ears is what a
        # caller does when it wants to know *whether* it can listen at all, and a
        # machine without faster-whisper must still be able to answer that question
        # instead of failing at the constructor.
        self._model = None
        self._model_size = model_size or config.WHISPER_MODEL

    def _loaded(self):
        """The Whisper model, loaded the first time it is really needed."""
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self._model_size, device="cpu",
                                       compute_type="int8")
        return self._model

    def transcribe(self, audio):
        """One phrase in, one honest transcript out - "" when there was no speech.

        `condition_on_previous_text` is off deliberately: on, the model conditions on
        its own earlier output, so one invented sentence breeds the next. The three
        thresholds are Whisper's own, and the segments are filtered here as well as
        inside the decoder because a segment it kept can still be one it does not
        believe.
        """
        segments, _info = self._loaded().transcribe(
            normalize_peak(audio), beam_size=1, initial_prompt=SPEECH_PROMPT,
            condition_on_previous_text=False, temperature=0.0,
            no_speech_threshold=NO_SPEECH_PROB, log_prob_threshold=MIN_LOGPROB,
            compression_ratio_threshold=MAX_COMPRESSION)
        return understood(segments)


def wav_bytes(audio, rate=SAMPLE_RATE):
    """A mono 16-bit PCM WAV in memory - what the transcription API wants.

    Built with the standard library only. The microphone gives float32 at 16 kHz;
    a WAV container is four small fields and a header, which is not worth a new
    dependency for.
    """
    import io
    import wave

    import numpy as np
    clipped = np.clip(np.asarray(audio, dtype="float32"), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm)
    return buffer.getvalue()


def multipart_body(fields, filename, blob, boundary):
    """A multipart/form-data body, assembled by hand.

    The standard library has no multipart encoder, and this is the one shape the
    transcription endpoint accepts: a `file` part carrying the audio, plus ordinary
    text fields for the model and the prompt.
    """
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f'\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'.encode("utf-8"))
    parts.append(blob)
    parts.append(f'\r\n--{boundary}--\r\n'.encode("utf-8"))
    return b"".join(parts)


class CloudEars:
    """Speech to text through hosted Whisper, falling back to the local model.

    Same `transcribe(audio)` as Ears, so nothing above it changes. Why it exists:
    the local `base` model on this CPU is slow and mis-hears commands, and a
    mis-heard command is the difference between the assistant working and the
    assistant looking stupid.

    The local model is not removed, only demoted. If there is no key, or the
    request fails, the same audio is transcribed on this machine - so a dropped
    connection costs accuracy, never the ability to listen. Which path was taken is
    in the log, because a silent fall back to the less accurate one would look like
    the assistant had gone deaf for no reason.

    `audio` is mono float32 at 16 kHz, which is exactly what `listen_once`
    produces; the WAV it is wrapped in is labelled with that rate, so nothing is
    resampled and nothing is mislabelled.
    """

    def __init__(self, key=None, model=None, timeout=None, poster=None, local=None):
        self.key = (key or config.cloud_key() or "").strip()
        self.model = model or config.CLOUD_STT_MODEL
        self.base_url = config.CLOUD_BASE_URL
        self.timeout = config.CLOUD_STT_TIMEOUT_S if timeout is None else timeout
        self._poster = poster or self._post
        self._local = local               # an Ears, built on first fallback
        self.last_error = None
        self.remote_calls = 0
        self.local_calls = 0

    @property
    def available(self):
        return bool(self.key)

    def transcribe(self, audio):
        if not self.available:
            return self._local_transcribe(audio)
        try:
            return self._remote(normalize_peak(audio))
        except Exception as exc:              # never cost the user their command
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.event("stt_fallback", error=type(exc).__name__)
            return self._local_transcribe(audio)

    def _remote(self, audio):
        """One phrase in, one honest transcript out. "" when it held no speech.

        `verbose_json` is asked for instead of `json` for one reason: it carries the
        decoder's own per-segment `no_speech_prob`, `avg_logprob` and
        `compression_ratio`, and without them the caller has no way to tell a phrase
        from the room. That is the whole difference between an assistant that answers
        what was said and one that answers "Thank you." to a fridge.
        """
        import uuid
        boundary = "----Retro" + uuid.uuid4().hex
        body = multipart_body({"model": self.model, "prompt": SPEECH_PROMPT,
                               "response_format": "verbose_json", "temperature": "0"},
                              "speech.wav", wav_bytes(audio), boundary)
        data = self._poster(self.base_url + "/audio/transcriptions", body, boundary)
        if not isinstance(data, dict):
            raise RuntimeError("the reply was not a transcript")
        segments = data.get("segments")
        if isinstance(segments, list) and segments:
            text = understood(segments)
            if text:
                self.remote_calls += 1
                return text
            # The endpoint reported segments and not one of them is speech. Answering
            # with "nothing was said" is the point; falling through to the local model
            # would only ask a second decoder to invent something for the same audio.
            self.remote_calls += 1
            return ""
        raw = data.get("text")
        # Only a string is a transcript. Coercing whatever came back would turn a
        # number or an error object into a command, and it would then be carried out.
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            raise RuntimeError("empty transcript")
        self.remote_calls += 1
        return text

    def _post(self, url, body, boundary):
        """One POST of a form body. Raises, so the caller can fall back."""
        import json as _json
        import urllib.request
        from .llm import USER_AGENT
        request = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Accept": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return _json.loads(response.read().decode("utf-8", "replace"))

    def _local_transcribe(self, audio):
        if self._local is None:
            self._local = Ears()          # loads the local model only if it is needed
        self.local_calls += 1
        return self._local.transcribe(audio)


def build_ears(cloud=None):
    """The best ears available: hosted Whisper with a key, the local model without."""
    wanted = config.cloud_available() if cloud is None else cloud
    return CloudEars() if wanted else Ears()


def listen_once(ears, timeout_s=None, timing=None, interrupt=None, on_level=None,
                gate=None, on_barge=None, trace=None, device=None, on_phrase=None):
    """Waits for one spoken phrase, returns its transcript, then closes the microphone.

    Returns '' if nothing was said within timeout_s. The stream closes between
    calls, so Retro doesn't hear its own voice while speaking.

    Pass a dict as `timing` to get back when the phrase actually ended
    ("phrase_end") and how long it was ("duration"), for latency measurement.

    `interrupt` is called about 30 times a second and returns True to give up
    waiting; the UI uses it to run a typed command without stopping the loop.
    `on_level` receives the loudness of every block, which drives the display.

    `gate` is called for every block and returns the loudness a block must beat to
    count while Retro is playing its own voice on the speakers. Without it the
    microphone hears Retro, Whisper transcribes it, and the assistant answers
    itself in a loop; with it, a user speaking over the voice still gets through
    because they are louder at the microphone than the echo of the output is.

    `on_barge` is called the moment that really happens - a block well above the
    echo threshold, held for a couple of blocks - so Retro stops talking *now* instead
    of finishing its sentence and answering words the user said a minute ago. Called
    at most once per listening call: after the first time there is nothing left to
    interrupt, and the rest of the phrase is just the user talking.

    `trace` (core/diag.py) receives two measured stages and is moved forward to the
    instant the phrase ended, so the latency that follows is the silence the user
    actually sits in - not the time they spent talking.

    `device` is a sounddevice input index, or None for the system default. It is
    resolved by the session from a saved name, so unplugging the chosen headset does
    not leave the assistant silently listening to nothing.

    `on_phrase` receives the raw phrase exactly as the ears are handed it, before any
    of the checks that can refuse it - too quiet to send, not believed by the
    decoder - so a diagnostic can measure what the recogniser was really given. It
    is the only place that sees the audio, and every reason a phrase is dropped is
    invisible without it.
    """
    def give_up():
        return interrupt is not None and interrupt()
    import numpy as np
    import sounddevice as sd

    frames = queue.Queue()
    # The device decides the rate it will accept; everything after this point works in
    # the ears' own 16 kHz, so the gate, the phrase buffer and the recogniser never
    # learn that the microphone was 48 kHz.
    rate = capture_rate(device) or SAMPLE_RATE
    down = Down16(rate)

    def on_audio(indata, _frames, _time, _status):
        block = down.feed(indata[:, 0])
        if len(block):
            frames.put(block)

    started = time.time()
    last_block = started
    phrase_started_ns = None
    buf, in_phrase, last_loud = [], False, 0.0
    barge_since = None
    with sd.InputStream(device=device, samplerate=rate, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=on_audio):
        while True:
            if give_up():
                return ""
            now = time.time()
            # The deadline is asked here, not only where a block fails to arrive. It
            # used to live in the `queue.Empty` branch, which a healthy microphone
            # never reaches: a stream delivering its usual thirty-three blocks a second
            # into a silent room waited for a phrase that might never come, and every
            # caller that passes a timeout - `ask_user`, `_ask_brief`, the probe tools -
            # was left hanging on it. A phrase already being recorded is still never cut
            # short by the clock; only the wait for one to start is bounded.
            if not in_phrase and timeout_s and now - started > timeout_s:
                return ""
            # A stream that has stopped delivering blocks is not a quiet room. A device
            # can be unplugged or re-configured underneath an open stream, and when that
            # happens the callback simply stops being called: the pass waited forever on
            # an empty queue, the listening loop spun there silently, and the log said
            # nothing at all. That is the shape of "it stopped hearing me and never came
            # back", so it is written down and the pass is handed back.
            if in_phrase and now - last_block > STALL_S:
                log.event("listen_stalled", seconds=round(now - last_block, 2),
                          blocks=len(buf), device=device)
                return ""
            try:
                block = frames.get(timeout=0.2)
            except queue.Empty:
                continue
            now = time.time()
            last_block = now
            level = float(np.sqrt(np.mean(block ** 2)))
            if on_level is not None:
                on_level(level)
            blocked_under = gate() if gate is not None else None
            if blocked_under is not None and level <= blocked_under:
                barge_since = None
                continue          # this is Retro's own voice coming back in
            if blocked_under is not None and on_barge is not None:
                # Loud enough to beat the echo, *and* held for a couple of blocks: the
                # owner is talking over Retro rather than the microphone hearing itself.
                # One block above the gate is not enough on its own - a single peak of
                # the echo would cut an answer off every time.
                if level > blocked_under * BARGE_GAIN:
                    if barge_since is None:
                        barge_since = now
                    if (now - barge_since) >= BARGE_S:
                        fire, on_barge = on_barge, None     # once: there is nothing
                        try:                                # left to interrupt after
                            fire()                          # the audio is stopped
                        except Exception:
                            log.event("barge_failed")
                        blocked_under = None
                else:
                    barge_since = None
            if level > START_RMS:
                if not in_phrase:
                    in_phrase, buf = True, []
                    phrase_started_ns = time.perf_counter_ns()
                last_loud = now
            if not in_phrase:
                continue
            buf.append(block)
            length = sum(len(b) for b in buf) / SAMPLE_RATE
            if (now - last_loud) < END_SILENCE_S and length < MAX_PHRASE_S:
                continue
            audio = np.concatenate(buf).astype(np.float32)
            if on_phrase is not None:
                try:
                    on_phrase(audio)
                except Exception:
                    log.event("phrase_hook_failed")
            if length >= MIN_PHRASE_S:
                if timing is not None:
                    timing["phrase_end"] = last_loud
                    timing["duration"] = length
                    timing["peak"] = float(np.max(np.abs(audio)))
                if trace is not None:
                    # The origin moves to the end of the phrase: everything the tool
                    # reports from here is the wait the user feels, and capture is
                    # kept beside it rather than folded into it.
                    ended_ns = time.perf_counter_ns()
                    trace.restart(ended_ns)
                    trace.mark("phrase_end", ended_ns)
                    if phrase_started_ns is not None:
                        trace.add("capture", ended_ns - phrase_started_ns)
                # Room noise crosses the loudness gate, but it isn't speech: transcribing
                # it wastes CPU and is what makes Whisper hallucinate phrases. Written
                # down, because a phrase dropped here is a phrase the owner spoke into a
                # microphone that was not loud enough for it, and the silence that
                # followed is what "it cannot hear me" actually looked like.
                if float(np.max(np.abs(audio))) < MIN_PEAK:
                    log.event("phrase_too_quiet", seconds=round(length, 2),
                              peak=round(float(np.max(np.abs(audio))), 4),
                              floor=MIN_PEAK, device=device)
                    in_phrase, buf = False, []
                    continue
                transcribe_started = time.perf_counter_ns()
                try:
                    text = ears.transcribe(audio)
                except Exception as exc:
                    if trace is not None:
                        trace.error("stt", exc)
                    raise
                finally:
                    if trace is not None:
                        trace.add("stt", time.perf_counter_ns() - transcribe_started)
                if trace is not None:
                    trace.mark("stt_done")
                if not text:
                    # A phrase was captured and the recogniser returned nothing. That is
                    # a different failure from hearing no phrase at all, and the log has
                    # to say which one happened.
                    log.event("stt_empty", seconds=round(length, 2),
                              peak=round(float(np.max(np.abs(audio))), 3), device=device)
                return text
            in_phrase, buf = False, []


# The voice SAPI reports, plus the token needed to select it. `.id`, `.name` and
# `.languages` are what pick_voice() reads, so this replaces what pyttsx3 exposed.
Voice = namedtuple("Voice", "id name languages token")
SSF_CREATE_FOR_WRITE = 3          # SpeechStreamFileMode for SpFileStream.Open
RATE_WPM_BASE = 200               # SAPI's own default speaking rate, in words per minute


def sapi_rate(wpm):
    """A words-per-minute figure as SAPI's -10..10 rate."""
    try:
        return max(-10, min(10, int(round((float(wpm) - RATE_WPM_BASE) / 25.0))))
    except (TypeError, ValueError):
        return 0


def sapi_voices(sapi):
    """Every installed voice, as Voice records."""
    tokens = sapi.GetVoices()
    found = []
    for index in range(tokens.Count):
        token = tokens.Item(index)
        try:
            name = str(token.GetDescription())
        except Exception:
            name = f"voice {index}"
        try:
            languages = [str(code) for code in token.GetAttribute("Language")]
        except Exception:
            languages = []
        try:
            identifier = str(token.Id)
        except Exception:
            identifier = name
        found.append(Voice(identifier, name, languages, token))
    return found


# Voices that would speak Hindi, if the owner ever installs one. Windows ships
# none of these by default, so replies stay English until one is added.
HINDI_VOICE_HINTS = ("hindi", "हिन्दी", "हिंदी", "hi-in", "heera", "kalpana", "hemant", "swara")


def voice_blob(voice):
    languages = " ".join(getattr(voice, "languages", None) or [])
    return f"{getattr(voice, 'id', '')} {getattr(voice, 'name', '')} {languages}".lower()


def pick_voice(voices, preferred=None):
    """The configured voice if it is installed, else the first English one."""
    if not voices:
        return None
    wanted = (config.VOICE_NAME if preferred is None else preferred) or ""
    wanted = wanted.strip().lower()
    if wanted:
        for v in voices:
            if wanted in voice_blob(v):
                return v
    for v in voices:
        if "en" in voice_blob(v):
            return v
    return voices[0]


def find_hindi_voice(voices):
    """Returns an installed Hindi voice, or None when there isn't one."""
    for v in voices:
        blob = voice_blob(v)
        if any(h in blob for h in HINDI_VOICE_HINTS):
            return v
    return None


def read_wav_mono(path):
    """Reads a WAV into float32 mono, scaled to -1..1, with its own sample rate."""
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    dtype = {1: np.int8, 2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:
        raise ValueError(f"unsupported sample width {width}")
    audio = np.frombuffer(raw, dtype=dtype).astype(np.float32) / float(2 ** (8 * width - 1))
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return rate, audio


def resample(audio, src_rate, dst_rate):
    """Audio at one sample rate, as audio at another.

    The two halves of this voice disagree about rates, and it cost the user every
    spoken word. SAPI renders `Microsoft Zira` at 22 050 Hz, and the devices this app
    offers are WASAPI devices, which accept only their own mix rate - 48 000 Hz here.
    Handing 22 050 to one of those is not "slightly off": the stream is refused
    outright, and the line was lost in silence while the app reported that it spoke.

    Linear interpolation is enough for speech at these rates and needs nothing new:
    in the direction that matters here it only ever upsamples, which cannot alias.
    """
    import numpy as np
    source = np.asarray(audio, dtype="float32")
    if not src_rate or not dst_rate or int(src_rate) == int(dst_rate) or len(source) == 0:
        return source
    count = int(round(len(source) * float(dst_rate) / float(src_rate)))
    if count <= 1:
        return source
    grid = np.linspace(0.0, len(source) - 1.0, count, dtype="float32")
    return np.interp(grid, np.arange(len(source), dtype="float32"), source).astype("float32")


PLAY_RATES = (48000, 44100, 32000, 24000, 22050, 16000)
FIRST_BLOCK_S = 2.0               # how long the first audio block may take to arrive
_RATE_CACHE = {}


def playable_rate(device, preferred=None):
    """The rate this output will really accept, or None when it accepts none of them.

    Asked in order of preference: the rate the audio is already in (so nothing is
    resampled when the device happens to agree), the device's own mix rate, then the
    usual rates. Cached, because it is a device query and the answer only changes when
    the machine's audio devices do.
    """
    import sounddevice as sd
    key = (device, int(preferred or 0))
    if key in _RATE_CACHE:
        return _RATE_CACHE[key]
    wanted = []
    if preferred:
        wanted.append(int(preferred))
    try:
        info = sd.query_devices(device)
        default = int(round(float(info.get("default_samplerate") or 0)))
    except Exception:
        default = 0
    if default:
        wanted.append(default)
    wanted.extend(rate for rate in PLAY_RATES if rate not in wanted)
    chosen = None
    for rate in wanted:
        try:
            sd.check_output_settings(device=device, samplerate=rate, channels=1,
                                     dtype="float32")
            chosen = rate
            break
        except Exception:
            continue
    _RATE_CACHE[key] = chosen
    return chosen


def device_usable(device):
    """True when this output can be opened at some rate we can produce.

    Silent and instant - nothing is played, so nothing is heard at startup. It is
    what lets the session notice that the device it remembered is gone *before* the
    first reply is written into it, instead of after three silent lines.
    """
    return playable_rate(device, 22050) is not None


INPUT_RATES = PLAY_RATES


def capture_rate(device=None):
    """The rate this microphone will really accept, or None when it accepts none.

    Asked in the same order as the output's: the rate the ears need first, because
    resampling nothing is free; then the device's own mix rate; then the usual rates.

    This machine is why it exists. Its WASAPI microphone array accepts *only* 48 000 Hz
    and refuses 16 000 outright - `Invalid sample rate` - so a microphone offered in the
    picker and then opened at 16 kHz would have been a device that never heard a word,
    with the same silent symptom the picker was added to cure.
    """
    import sounddevice as sd
    key = ("in", device, SAMPLE_RATE)
    if key in _RATE_CACHE:
        return _RATE_CACHE[key]
    wanted = [SAMPLE_RATE]
    try:
        default = int(round(float(sd.query_devices(device).get("default_samplerate") or 0)))
    except Exception:
        default = 0
    if default and default not in wanted:
        wanted.append(default)
    wanted.extend(rate for rate in INPUT_RATES if rate not in wanted)
    chosen = None
    for rate in wanted:
        try:
            sd.check_input_settings(device=device, samplerate=rate, channels=1,
                                    dtype="float32")
            chosen = rate
            break
        except Exception:
            continue
    _RATE_CACHE[key] = chosen
    if chosen and chosen != SAMPLE_RATE:
        # Once per device: a microphone that only offers its own mix rate is worth the
        # log line, because it is also the device someone will later call "deaf".
        log.event("capture_rate", device=device, rate=chosen)
    return chosen


class Down16:
    """Blocks arriving at `rate`, as 16 kHz float32 blocks, with no seam at the edge.

    Linear interpolation, continued across blocks rather than restarted at each one:
    a device that only offers 48 000 Hz must sound like a device giving 16 000. One
    sample of phase is carried over, so the 21 ms joins are not re-phased and the
    stutter they cause is not left in the transcript.
    """

    def __init__(self, rate):
        self.rate = float(rate or SAMPLE_RATE)
        self._tail = 0.0
        self._next = 0.0

    def feed(self, block):
        import numpy as np
        samples = np.asarray(block, dtype="float32").reshape(-1)
        if self.rate == float(SAMPLE_RATE):
            # A *copy*, and this is the whole of "Retro cannot hear me".
            #
            # PortAudio hands the callback one buffer and reuses it for every block.
            # At the ears' own rate there is nothing to resample, so this used to
            # return a view of that buffer - and `listen_once` queued the view and kept
            # the blocks of a phrase in a list. By the time the phrase ended every one
            # of them pointed at the same few milliseconds of memory, holding whatever
            # the microphone had delivered last: usually the silence after the speaker
            # stopped. The loudness gate had already seen the real speech (it measures
            # each block the instant it arrives), so a phrase was started, collected as
            # near-silence, refused as `phrase_too_quiet` at a peak of 0.0002, and the
            # recogniser was never given the words. A 16 kHz device - which is what
            # this laptop's array opens at - therefore could not transcribe anything at
            # all, while a 48 kHz one worked because the interpolation path allocates.
            return np.array(samples, dtype="float32", copy=True)
        extended = np.empty(len(samples) + 1, dtype="float32")
        extended[0] = self._tail
        extended[1:] = samples
        step = self.rate / float(SAMPLE_RATE)
        # Only positions with a sample on both sides are emitted. The very last step of
        # a block has nothing after it to interpolate towards, so it is *deferred* to
        # the next block - which begins with this block's final sample - instead of
        # being clamped to it. Clamping an edge is a sample of error at every block
        # boundary, which is a periodic 47 Hz wobble on the audio the ears receive.
        positions, position = [], self._next
        while position <= len(samples) - 1:
            positions.append(position + 1.0)      # the sample before the block sits at 0
            position += step
        self._next = position - len(samples)
        if len(samples):
            self._tail = float(samples[-1])
        if not positions:
            return np.empty(0, dtype="float32")
        grid = np.arange(len(samples) + 1, dtype="float32")
        return np.interp(np.asarray(positions, dtype="float32"), grid, extended).astype("float32")


def forget_rates():
    """Forgets the cached device answers - after a device changes, and in tests."""
    _RATE_CACHE.clear()


def seconds_for(samples, rate):
    """How long a block of samples lasts, so a playback can be waited for honestly.

    Playback is bounded by the audio's own length rather than by a guess made from the
    text: a line that stalls is given up on in the time it should have taken to speak,
    which is what turns a twelve-second silent stall into an immediate fallback.
    """
    return (float(samples) / float(rate or 1)) if rate else 1.0


def process_name():
    """The name the Windows mixer knows this process by, without its extension."""
    return os.path.splitext(os.path.basename(sys.executable))[0]


def mixer_peak_while(work, settle=0.0):
    """Runs `work` and reports the loudest the Windows mixer saw this process make.

    Returns (what `work` returned, mixer peak or None). None means the mixer could not
    be read at all, and that is reported as *unknown*, never as silence: the one thing
    this must not do is claim proof it does not have.

    It exists because this app twice told the user it had spoken when nothing was
    audible. The peak it used to quote came out of the buffer it had just filled - a
    number that is identical whether the audio reached the speakers, a switched-off
    Bluetooth headset, or nothing at all. The mixer's own meter is the difference
    between "we handed samples to a device" and "sound left this process".
    """
    from . import mixer
    stop = threading.Event()
    seen = []

    def watch():
        name = process_name()
        while not stop.is_set():
            try:
                peak = mixer.peak_of(name)
            except Exception:
                peak = None
            if peak is not None:
                seen.append(peak)
            stop.wait(0.08)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        result = work()
    finally:
        stop.set()
        watcher.join(timeout=1.5)
    if settle:
        time.sleep(settle)
    return result, (max(seen) if seen else None)


WASAPI = "wasapi"


def _devices(channel_key, prefer_hostapi=WASAPI):
    """(index, name) for devices with at least one channel of `channel_key`.

    sounddevice enumerates every host API, so the same speakers or microphone show
    up three or four times - once each for MME, DirectSound and WASAPI. A picker only
    needs one of each, and WASAPI is preferred because it is the one Windows' own
    volume mixer and privacy controls describe; if the host API list is unavailable,
    everything is returned rather than nothing.
    """
    import sounddevice as sd
    preferred = None
    try:
        for index, api in enumerate(sd.query_hostapis()):
            if prefer_hostapi and prefer_hostapi in str(api.get("name", "")).lower():
                preferred = index
    except Exception:
        preferred = None
    found, seen = [], set()
    for index, info in enumerate(sd.query_devices()):
        if info.get(channel_key, 0) <= 0:
            continue
        if preferred is not None and info.get("hostapi") != preferred:
            continue
        name = " ".join(str(info["name"]).split())
        if not name or name in seen:
            continue
        seen.add(name)
        found.append((index, name))
    return found


def output_devices(prefer_hostapi=WASAPI):
    """(index, name) for devices that can play sound, one entry per name."""
    return _devices("max_output_channels", prefer_hostapi)


def input_devices(prefer_hostapi=WASAPI):
    """(index, name) for devices that can record, one entry per name.

    A laptop usually has one microphone, but a headset, a webcam and a virtual
    cable add up quickly - and Windows' *default* input is not always the one the
    owner is speaking into. Without a picker, the only symptom is "Retro can't hear
    me", which is indistinguishable from a broken recogniser.
    """
    return _devices("max_input_channels", prefer_hostapi)


def default_output_name():
    """The name of the device the system would use, or '' if there is none."""
    try:
        import sounddevice as sd
        return " ".join(str(sd.query_devices(kind="output")["name"]).split())
    except Exception:
        return ""


def default_input_name():
    """The microphone Windows would use, or '' if there is none."""
    try:
        import sounddevice as sd
        return " ".join(str(sd.query_devices(kind="input")["name"]).split())
    except Exception:
        return ""


def input_usable(index):
    """True when this input can really be opened, at some rate the ears can use.

    A device in the list is not a device that accepts 16 kHz mono float32 - the WASAPI
    microphone array on this machine takes only 48 000 Hz. Such a microphone is still
    perfectly usable, because `listen_once` opens it at the rate it likes and
    `Down16` brings the audio to 16 kHz; what is not usable is a device that accepts
    no rate at all, and that is what this asks.
    """
    return capture_rate(index) is not None


def measure_input(device=None, seconds=1.5):
    """The loudest sample a microphone hears in `seconds`, for TEST MIC.

    It records and measures - it does not transcribe, so pressing the button costs
    nothing at a provider and sends nothing anywhere. Raises if the device cannot be
    opened; the caller reports that as a failure rather than as silence, because
    "0.00" would look like a working microphone in a quiet room.
    """
    import numpy as np
    import sounddevice as sd

    peak = [0.0]
    rate = capture_rate(device) or SAMPLE_RATE

    def callback(indata, _frames, _time, _status):
        block = indata[:, 0]
        if len(block):
            peak[0] = max(peak[0], float(np.max(np.abs(block))))

    with sd.InputStream(device=device, samplerate=rate, channels=1,
                        dtype="float32", blocksize=BLOCK, callback=callback):
        time.sleep(max(0.2, float(seconds)))
    return peak[0]


BLOCK_OUT = 1024                  # playback block: about 46 ms at 22 kHz
# The blocks are small on purpose: about 21 ms at 48 kHz, which is what the small
# pause between spoken sentences is made of.
LEVEL_GAIN = 6.0                  # RMS to display range
SPEECH_FLOOR_S = 3.0              # shortest a line may take before we call it wedged
SPEECH_PER_CHAR_S = 0.12          # and how much longer each character allows
PLAY_MARGIN_S = 4.0               # slack on top of the length of the audio itself
MAX_WEDGES = 3                    # after this many stuck lines, stop trying to speak
HARD_CAP_S = 90.0                 # a line can never hold the caller longer than this


def speech_cap(text):
    """How long `text` may take to render and play before it is called wedged.

    Generous on purpose: this is only meant to catch a call that will never
    return, not to cut off a slow sentence.
    """
    return SPEECH_FLOOR_S + SPEECH_PER_CHAR_S * len(text or "")


class SpeechError(RuntimeError):
    """The audio stack refused to render or play a line."""


# One voice at a time, whoever is talking.
#
# There are two playback paths in this app: the hosted voice in core/tts.py, which
# plays a synthesised WAV through its own output stream, and the local SAPI voice,
# which plays a rendered WAV here. They are two different streams through the same
# speakers, and nothing used to stop them running at once - so a reply that started
# on the hosted voice and fell back to the local one, or a `say` line handed to the
# speaker while a streamed sentence was still playing, came out as two voices
# talking over each other. That is the overlap the user heard.
#
# The fix is one lock around the act of playing, taken by both paths. A line can
# only start when the previous one has really finished or been stopped. The lock is
# re-entrant, so a playback that stops the other voice from inside itself cannot
# deadlock against its own thread.
PLAYBACK_LOCK = threading.RLock()


@contextmanager
def one_voice_at_a_time():
    """Held for the whole of a playback, by every path that makes sound.

    A context manager rather than a raw lock so the two playback paths cannot
    disagree about what "playing" means: the hosted voice in core/tts.py and the
    local SAPI voice both take this, and nothing else needs to know the lock exists.
    """
    with PLAYBACK_LOCK:
        yield


class Speaker:
    """Reads replies aloud - on a thread of its own, with a deadline on every line.

    Three failures shaped this design, all of them seen on this machine:

    * SAPI picked its own output device and sent the voice where nobody was
      listening. Speech is rendered to a WAV and played by us instead, so the
      device is a choice and the level of every block is known.
    * **The second line in a row never came back.** pyttsx3 wraps SAPI but cannot
      render twice through one engine: its second `runAndWait()` blocks for ever,
      reproduced in tools/tts_probe.py. That was the "green for ever" bug - the
      ring animated, nothing was audible, and no command could run again until the
      app was restarted, because speech held the voice loop's thread. Speech now
      goes to SAPI directly and renders synchronously into a file stream, and even
      so every line runs on this class's own worker thread and is waited for with a
      stall detector. A line that really does stick is reported and abandoned, the
      audio is silenced, and a fresh worker takes over so the next line still runs.
    * Playback in blocking mode could stall. The stream is driven by its own
      callback here, so there is nothing to block on, and `stop()` cuts a line off
      mid-sentence - which is what makes saying "Retro, stop" work.

    Everything SAPI touches happens on this class's worker thread, because the COM
    objects are not safe to share with the window or the voice loop.
    """

    def __init__(self, voice=None, rate=None, volume=None, device=None, on_level=None):
        self.rate = int(config.VOICE_RATE if rate is None else rate)
        self.volume = float(config.VOICE_VOLUME if volume is None else volume)
        self.device = device                          # sounddevice index, None = system default
        self.on_level = on_level
        self.last_peak = 0.0                          # of the last reply, for tests and the log
        # True when the last line started playing and was stopped part way through -
        # a barge, a stop, or the next line taking over. Different from "nothing came
        # out": a cut line is not evidence of a broken output device, and reporting it
        # as one sends the owner to the OUTPUT picker over a line they did hear.
        self.last_cut = False
        self.last_error = None
        self.voice = None
        self.voice_name = "(system default)"
        self.voices = []                              # filled by ensure_voices()
        self.wedges = 0
        self.disabled = False                         # set once speech has wedged MAX_WEDGES times
        self.last_played = None                       # did the last line really come out?
        self._wanted_voice = voice
        self._sapi = None                             # ISpeechVoice, built in the worker
        self._stream = None                           # ISpeechFileStream, same thread
        self._abort = threading.Event()
        self._speaking = False
        self._level = 0.0
        self._heartbeat = time.time()                 # the worker, showing signs of life
        self._jobs = queue.Queue()
        self._start_worker()

    # ---------- the worker --------------------------------------------------

    def _start_worker(self):
        """A fresh thread with a fresh SAPI voice - the only cure for a wedged one."""
        self._sapi = None
        self._abort.clear()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        # COM belongs to the thread that uses it, and this thread is the one that
        # talks to SAPI. pyttsx3 used to do this for us; going around it, we do.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._beat()
            try:
                job["result"] = self._do(job, job["result"])
            except BaseException as exc:              # a broken line must not kill the worker
                job["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                job["done"].set()

    def _beat(self):
        """Says "the worker is alive and moving" - which is not the same as "my job
        is running". A second line queued behind the first used to time out waiting
        for its turn and be written off as a wedged engine."""
        self._heartbeat = time.time()

    def _do(self, job, _result):
        kind = job["kind"]
        if kind == "voices":
            self._sapi_now()
            return list(self.voices)
        if kind == "choose":
            self._sapi_now()
            return self._apply_voice(job["name"])
        if kind == "save":
            self._sapi_now()
            return self._render(job["text"], job["path"])
        if kind == "say":
            from .tts import speakable
            # The one choke point every spoken line passes through, sync or async, so
            # a markdown marker or a lone full stop cannot be read out as a word. The
            # sync path cleans its text in `say()`; a line handed to `say_async` - the
            # greeting, a written "still working on it", the panel's voice test - went
            # straight to the renderer, which is exactly the sort of line a model
            # writes with a heading or a bullet in it.
            text = speakable(job["text"])
            if not text:
                return True
            self._sapi_now()
            path = self._render(text)
            try:
                return self._play(path, job["cap"])
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        raise ValueError(f"unknown speech job {kind!r}")

    def _sapi_now(self):
        """Builds the SAPI voice on first use, in the worker thread.

        `pyttsx3` is deliberately not used here any more. It wraps this same SAPI
        object, but it cannot render two lines in a row: the second `runAndWait()`
        never returns. That hang is what left Retro green and deaf until it was
        restarted, and it is reproduced in tools/tts_probe.py. Speaking straight to
        SAPI also renders a line in about a tenth of a second instead of two.
        """
        if self._sapi is not None:
            return self._sapi
        import comtypes.client as comtypes_client
        sapi = comtypes_client.CreateObject("SAPI.SpVoice")
        self._sapi = sapi
        self._stream = comtypes_client.CreateObject("SAPI.SpFileStream")
        sapi.Rate = sapi_rate(self.rate)
        sapi.Volume = 100                 # the gain is applied when we play the WAV
        self.voices = sapi_voices(sapi)
        self._apply_voice(self._wanted_voice)
        self._beat()
        return sapi

    def _apply_voice(self, voice):
        chosen = pick_voice(self.voices, voice)
        if chosen is None:
            return None
        self._sapi.Voice = chosen.token
        self.voice = chosen
        self.voice_name = chosen.name or str(voice)
        return self.voice_name

    # ---------- the jobs the rest of the app asks for -----------------------

    def _enqueue(self, kind, stall_s, **fields):
        """Runs one job on the worker and waits for its answer.

        The wait is for a *stall*, not for a fixed number of seconds: as long as the
        worker keeps rendering or playing, the wait is extended. That is the
        difference between "this line is long" and "this call will never return".
        """
        job = {"kind": kind, "done": threading.Event(), "result": None, "error": None, **fields}
        self._jobs.put(job)
        started = time.time()
        deadline = started + stall_s
        seen = self._heartbeat
        while not job["done"].wait(0.25):
            now = time.time()
            if self._heartbeat > seen:
                seen = self._heartbeat        # not `now`: comparing a stamp with a clock
                deadline = now + stall_s      # stops the wait ever being extended twice
            if now > deadline or now - started > HARD_CAP_S:
                return job, False
        return job, True

    def say(self, text, cap=None):
        """Speaks `text` and returns once it has played.

        False means no sound came out of this line: the engine did not come back, or
        no output device would have it. A False is no longer the same thing as a wedge
        - the caller can say so, and the next line goes to the system default instead
        of failing the same way for the rest of the session. Raises SpeechError when
        the audio stack refused the line outright, so a caller can report why.
        """
        if not text:
            return True
        from .tts import speakable
        # The last gate before the local voice: SAPI reads punctuation and markdown
        # markers out loud - a line that is nothing but markers would be spoken as a
        # symbol - and this is the one path that reaches it without the splitter.
        text = speakable(text)
        if not text:
            return True
        if self.disabled:
            return False
        cap = speech_cap(text) if cap is None else cap
        job, answered = self._enqueue("say", cap + PLAY_MARGIN_S, text=text, cap=cap)
        if job.get("stopped"):
            # A line the owner cut off (or the next request took over from) never played.
            # That is a cut, and it is not evidence about the output device: counting it
            # as a wedge is what disabled the voice after three ordinary barges.
            self.last_cut = True
            log.event("speech_cut", chars=len(text), why="stopped before it played")
            self.last_error = "the line was stopped before it played"
            return False
        if not answered:
            self._wedge(text, cap)
            return False
        if job["error"]:
            self.last_error = job["error"]
            raise SpeechError(job["error"])
        self.last_played = bool(job["result"])
        if not self.last_played:
            if self.last_cut:
                # It did come out - it was stopped. Saying "unheard" here is what sent
                # the owner to the OUTPUT picker over a line they had just listened to.
                log.event("speech_cut", chars=len(text), device=self.device)
                self.last_error = "the line was stopped before it finished"
                return False
            # Heard by nobody. Written down, because this is the bug the user reported
            # and a log line is what turns "it speaks and I hear nothing" into evidence.
            log.event("speech_unheard", chars=len(text), device=self.device)
            self.last_error = "no output device would play the line"
        return self.last_played

    def audible(self, text="This is Retro, checking the speakers."):
        """Speaks a line and reports whether sound really left this process.

        Returns (played, mixer peak). A peak of None means the mixer could not be
        read, so audibility is *unknown* and is reported as unknown - this is the
        honest answer the old "peak 0.68" claimed to be and was not.
        """
        return mixer_peak_while(lambda: self.say(text))

    def say_async(self, text):
        """Hands a line to the speech thread and returns at once.

        Used for the thinking noises that cover a slow model call: the line must
        not delay the work it is covering for.
        """
        if not text or self.disabled:
            return False
        job = {"kind": "say", "done": threading.Event(), "result": None, "error": None,
               "text": text, "cap": speech_cap(text)}
        self._jobs.put(job)
        return True

    def queue_depth(self):
        """How many lines are waiting. Reported in the log when a line is written off."""
        return self._jobs.qsize()

    def stop(self):
        """Cuts off the line being played and drops anything waiting behind it.

        A dropped job is *answered*, not abandoned. It used to be pulled off the queue
        and forgotten, so the caller waiting on it timed out and reported a broken
        engine - the live log has 440 of those, and after three in a row the voice
        switched itself off for the rest of the session. Saying "you stopped me"
        instead is the difference between a barge and a machine that has gone deaf.
        """
        self._abort.set()
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            job["stopped"] = True
            job["error"] = None
            job["result"] = False
            job["done"].set()
        return True

    @property
    def playing(self):
        """True while audio is actually coming out, so the microphone can be gated."""
        return self._speaking

    @property
    def level(self):
        """Loudness of the block being played, for the microphone gate."""
        return self._level

    def _wedge(self, text, cap):
        """A line that never came back: report it, silence the audio, start over."""
        self.wedges += 1
        log.event("speech_wedged", cap=cap, chars=len(text), wedges=self.wedges,
                  queued=self.queue_depth(), device=self.device)
        self.stop()
        if self.device is not None:
            # Whatever that device was, the next line goes to the system default rather
            # than failing the same way for the rest of the session. This is what kept
            # the voice dead after a stale Bluetooth index was restored.
            log.event("output_device_abandoned", device=self.device)
            self.device = None
            forget_rates()
        if self.wedges >= MAX_WEDGES:
            self.disabled = True
            log.event("speech_disabled", wedges=self.wedges)
            return
        self._start_worker()

    def ensure_voices(self, cap=10.0):
        """The installed voices, asked for once. Empty if SAPI won't answer.

        Builds the SAPI voice, so this is also where a machine with no voices or no
        SAPI finds out - it raises rather than pretending there is a voice.
        """
        if self.voices or self.disabled:
            return self.voices
        job, answered = self._enqueue("voices", cap)
        if not answered:
            self._wedge("", cap)
            return []
        if job["error"]:
            self.last_error = job["error"]
            raise SpeechError(job["error"])
        if job["result"]:
            self.voices = list(job["result"])
        return self.voices

    def choose(self, voice=None):
        """Re-picks the spoken voice, by name substring. Returns its name or None."""
        self._wanted_voice = voice
        if self.disabled:
            return None
        job, answered = self._enqueue("choose", 10.0, name=voice)
        if not answered or not job["result"]:
            return None
        self.voice_name = job["result"]
        return self.voice_name

    def choose_output(self, device):
        """Sets the playback device: a sounddevice index, a name, or None for default.

        A name is resolved against the devices that exist *now*, exactly first and then
        by substring, and None comes back when it cannot be found - so the caller can
        tell the difference between "chosen" and "that device is not here any more"
        instead of writing audio into an index that belonged to it last week. An index
        is accepted too, because that is what old settings files hold.

        Choosing an output is also the way back from a voice that gave up: the speaker
        is un-disabled and its wedges are forgotten, so the window's advice ("nothing
        came out - pick another output device") is actually recoverable.
        """
        if device is not None and not isinstance(device, int):
            wanted = " ".join(str(device).split()).lower()
            if not wanted:
                device = None
            else:
                found = None
                for exact in (True, False):
                    for index, name in output_devices():
                        match = (name.lower() == wanted) if exact else (wanted in name.lower())
                        if match:
                            found = index
                            break
                    if found is not None:
                        break
                if found is None:
                    return None
                device = found
        self.device = device
        self.disabled = False
        self.wedges = 0
        forget_rates()
        return device

    def save_to_file(self, text, path):
        """Used by the voice checks: writes the reply to a WAV instead of speaking it."""
        job, answered = self._enqueue("save", speech_cap(text) + PLAY_MARGIN_S,
                                      text=text, path=str(path))
        if job.get("stopped"):
            raise SpeechError("the line was stopped before it was rendered")
        if not answered:
            self._wedge(text, speech_cap(text))
            raise SpeechError("rendering the line never finished")
        if job["error"]:
            raise SpeechError(job["error"])
        return path

    def prepare(self, text):
        """Renders a line to audio *without playing it*, and hands the audio back.

        This is the difference between a sentence and the one before it being spoken
        as one answer, and the same two being spoken as two answers with a gap of
        silence between them. Rendering happens on this class's worker thread - the
        only thread that may touch the COM voice - and it is silent, so a sentence can
        be rendered while the previous one is still coming out of the speakers.

        Returns (rate, float32 samples), or None when the line cannot be rendered;
        the caller falls back to `say`, which renders and plays in one go.
        """
        from .tts import speakable
        text = speakable(text)
        if not text or self.disabled:
            return None
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="retro-speech-")
        os.close(handle)
        try:
            self.save_to_file(text, path)
            rate, audio = read_wav_mono(path)
            if rate and len(audio):
                return rate, audio
            return None
        except Exception as exc:                 # a broken voice must not end a reply
            log.event("speech_prepare_failed", error=type(exc).__name__, chars=len(text))
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------- rendering and playback, in the worker thread ---------------

    def _render(self, text, path=None):
        """Renders the line to a WAV and returns its path.

        SAPI speaks into a file stream synchronously, so when this returns the file
        is complete. There is no loop to wait on and nothing that can fail to return.
        """
        if path is None:
            handle, path = tempfile.mkstemp(suffix=".wav", prefix="retro-speech-")
            os.close(handle)
        self._stream.Open(str(path), SSF_CREATE_FOR_WRITE, False)
        try:
            self._sapi.AudioOutputStream = self._stream
            self._sapi.Speak(str(text))
        finally:
            try:
                self._stream.Close()
            except Exception:
                pass
        self._beat()
        return path

    def _report(self, chunk):
        import numpy as np
        self._beat()                      # audio is coming out: the worker is alive
        level = float(np.sqrt(np.mean(chunk ** 2))) * LEVEL_GAIN
        self._level = min(1.0, level)
        if self.on_level is not None:
            self.on_level(self._level)

    def _play(self, path, cap):
        """Plays a rendered WAV, on the chosen device or on the system default.

        Two bugs lived here, and both of them were silent - which is exactly what the
        user reported: "it speaks and I can't hear it".

        * **The stream was opened at the WAV's own rate.** SAPI renders at 22 050 Hz;
          every device this app offers is WASAPI, which takes only its own 48 000 Hz
          mix rate. So *choosing an output device in the window was guaranteed to make
          the voice fail*: PortAudio refused the stream, the line was never played, and
          192 of these were written off as "wedged" before speech disabled itself
          altogether. The samples are resampled to a rate the device will accept now.
        * **A refused device was never doubted and the fallback never helped.** The old
          code set `self.device = None` only in one branch, and returned `played` from
          inside the loop, so a device that could not play was tried again for every
          single line. The chosen device and then the system default are both tried,
          and whichever one works becomes the device that is used.
        """
        import numpy as np
        rate, audio = read_wav_mono(path)
        if len(audio) == 0:
            return False
        self.last_peak = float(np.max(np.abs(audio)))
        self._abort.clear()
        self._level = 0.0
        self.last_cut = False
        # Held for the whole of a playback: the same speakers, and the same lock the
        # hosted voice takes, so the two can never be heard at once.
        with one_voice_at_a_time():
            for round_index in range(2):
                if round_index:
                    # One retry, and only after every device in the first round failed.
                    # "The stream opened and then never asked for a block" is this
                    # machine's signature for a transient WASAPI hiccup - the shared
                    # array is busy for a moment when a capture starts - and the line
                    # that pays for it is the one the owner never hears. 150 ms and a
                    # second attempt is the whole fix for "sometimes it doesn't speak".
                    log.event("speech_retry", device=self.device, seconds=len(audio) / float(rate))
                    time.sleep(0.15)
                tries = [self.device] if self.device is None else [self.device, None]
                for attempt in tries:
                    played, refused, why, cut = self._play_on(attempt, audio, rate, cap)
                    if played:
                        self.wedges = 0
                        if attempt != self.device:
                            log.event("output_device_changed", device=attempt, reason=why)
                        self.device = attempt
                        return True
                    if cut:
                        # Stopped on purpose. Not a device failure, and not "unheard":
                        # the audio did come out. Moving on to the fallback device here
                        # is what re-spoke a barged answer in full, in the clear, where
                        # the microphone could hear it a second time.
                        self.last_cut = True
                        return False
                    if refused and attempt is not None:
                        # A device that will not open at any rate we can produce is not
                        # a voice failing - it is a device to stop using. Try the default.
                        log.event("output_device_failed", device=attempt, error=why)
                        forget_rates()
                        continue
        return False

    def _play_on(self, device, audio, rate, cap):
        """One attempt on one device. Returns (played, refused, why, cut).

        `refused` means this device would not take the audio at all, and `cut` means the
        line was deliberately stopped part way through - both are different from a line
        that simply never came out, and the caller treats all three differently: a
        refusal moves on to the next device, a cut does not, and neither of them is
        anything to tell the owner about their output device.
        """
        import numpy as np
        import sounddevice as sd

        target = playable_rate(device, rate)
        if target is None:
            return False, True, "no sample rate this device accepts", False
        samples = np.clip(resample(audio, rate, target) * self.volume, -1.0, 1.0)
        cursor = [0]
        began = threading.Event()
        finished = threading.Event()
        # A previous attempt must not silence this one: each attempt sets `_abort` when
        # it is done, and the next attempt's callback would abort on its first block.
        self._abort.clear()

        def callback(outdata, frames, _time, _status):
            if self._abort.is_set():
                raise sd.CallbackAbort
            began.set()
            chunk = samples[cursor[0]:cursor[0] + frames]
            cursor[0] += len(chunk)
            outdata[:len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):] = 0.0
                self._report(chunk if len(chunk) else np.zeros(1, dtype="float32"))
                finished.set()
                raise sd.CallbackStop
            self._report(chunk)

        try:
            stream = sd.OutputStream(samplerate=target, channels=1, dtype="float32",
                                     device=device, blocksize=BLOCK_OUT, callback=callback)
        except Exception as exc:
            # The message, not only the class: "PortAudioError" on its own is what kept
            # the real cause ("Invalid sample rate") out of the log for a month.
            log.event("output_open_failed", device=device, rate=target,
                      error=f"{type(exc).__name__}: {exc}"[:160])
            return False, True, f"{type(exc).__name__}: {exc}"[:80], False
        played, cut_off = False, False
        try:
            with stream:
                self._speaking = True
                if not began.wait(FIRST_BLOCK_S):
                    # The stream opened and then never asked for a block, so nothing is
                    # coming out of this device. Giving up here is what turns a silent
                    # twelve-second stall into an immediate, visible fallback.
                    return False, True, "no audio block ever arrived", False
                if not finished.wait(seconds_for(len(samples), target) + PLAY_MARGIN_S):
                    cut_off = True
                played = finished.is_set() and not self._abort.is_set()
        except Exception as exc:
            log.event("output_play_failed", device=device, rate=target,
                      error=f"{type(exc).__name__}: {exc}"[:160])
            return False, True, f"{type(exc).__name__}: {exc}"[:80], False
        finally:
            self._speaking = False
            self._level = 0.0
            self._abort.set()          # make sure the stream really is done
        if cut_off:
            log.event("playback_cut", device=device, cap=cap, samples=len(samples))
        # A stop or a barge sets `_abort` and the final block never lands, so `played`
        # is False for a line that was audible for all but its last few milliseconds.
        return played, False, "", bool(cut_off or self._abort.is_set())
