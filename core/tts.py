"""Speaking at the speed of thinking: hosted text to speech, in order, cancellable.

The old voice rendered a whole reply through SAPI and only then played it - so the
first sound arrived only after the sentence was finished being written, which is
exactly what the user complained about. Three things changed here:

* **Groq's Orpheus model** (`canopylabs/orpheus-v1-english`) synthesises each short
  sentence in one HTTPS call and returns a WAV, in a fraction of a second. It is
  the model Groq documents for the audio/speech endpoint today; the `playai-tts`
  family it replaces was shut down at the end of 2025.
* **Sentence at a time.** A reply is split as it is written, and each finished
  sentence goes into a queue. The first sentence is spoken while the rest of the
  answer is still being generated, which is the only way a hosted round trip can
  feel immediate.
* **One worker, in order.** A single thread takes sentences off the queue in the
  order they arrived, so segments can never overlap, arrive out of order, or be
  spoken twice. Stopping cancels the sentence being played and drops the rest -
  and `cancel()` also invalidates what was already queued, so a stopped request
  cannot keep talking.

The SAPI voice is not removed: when there is no key, or the network call fails, the
same sentence is spoken locally, so the assistant keeps its voice offline. The
endpoint takes at most 200 characters of input, so a long sentence is split again
at a comma or a space; nothing is ever silently truncated.
"""
import io
import re
import threading
import time
from collections import deque

from . import config, diag, langs, log
from .neuralvoice import NeuralVoice

# The documented Orpheus model and its voices. Verified against Groq's TTS guide:
# the only models on the audio/speech endpoint now, and WAV is the format they take.
MODEL = "canopylabs/orpheus-v1-english"
VOICES = ("autumn", "diana", "hannah", "austin", "daniel", "troy")
DEFAULT_VOICE = "autumn"
MAX_CHARS = 190                  # the API's limit is 200; leave room for punctuation
MIN_FIRST_S = 0.0                # nothing waits for a bigger first chunk
VOICE_RETRY_S = 0.6              # one pause before retrying the sentence that picks the voice
RATE_LIMIT_S = 30.0              # after a rate limit, don't ask the hosted voice again yet
# After the neural voice (Japanese/Hindi) refuses something, this long before it is
# asked again. One failure is usually the network, and asking forty times for one
# answer is how a reply goes from slow to stuck.
NEURAL_COOLDOWN_S = 6.0
# The longest piece the neural voice is asked to speak in one request. Speech time
# grows with characters, and so does the silence before the first word of the reply;
# a paragraph is split at its own clause marks before it is synthesised.
NEURAL_CHUNK = 90
# How long "nothing is queued, nothing is being prepared, nothing is playing" has to
# hold before it is the end rather than the breath before the next sentence. A quarter
# of a second: long enough that a model pausing between two sentences does not make the
# circle blink blue and green again, short enough that the green ends with the voice.
IDLE_GRACE_S = 0.22
_rate_limited_at = 0.0           # monotonic stamp of the last rate limit

# A synthesiser pads the end of what it renders - the hosted model by a few hundred
# milliseconds, SAPI by more - and every one of those milliseconds was played as
# silence between two sentences. The owner's complaint was exactly that: "extremely
# long pauses between full stops". Each clip is trimmed to its own audio before it is
# played, keeping a little air at each end so nothing sounds clipped, and a clip that
# is *all* silence is left alone rather than trimmed to nothing.
SILENCE_FLOOR = 0.004            # below this, a sample is the noise floor, not a word
EDGE_KEEP_S = 0.035              # the air kept at each end of a trimmed clip


def trim_silence(audio, rate, keep=EDGE_KEEP_S):
    """The audio without the dead air the synthesiser left at each end of it.

    Answers with what it was given when the samples cannot be read (a test's fake
    voice hands back a string) or when the whole clip is silence: a trim must never
    be the reason a sentence went missing.
    """
    try:
        import numpy as np
        samples = np.asarray(audio, dtype="float32")
    except Exception:
        return audio
    if samples.ndim != 1 or len(samples) < 2:
        return audio
    loud = np.flatnonzero(np.abs(samples) >= SILENCE_FLOOR)
    if len(loud) == 0:
        return samples
    pad = max(1, int(max(1.0, float(rate or 1)) * keep))
    start = max(0, int(loud[0]) - pad)
    stop = min(len(samples), int(loud[-1]) + 1 + pad)
    if start == 0 and stop == len(samples):
        return samples
    return samples[start:stop]


def reset_backoff():
    """Forgets the rate-limit cooldown - tests, and a fresh session."""
    global _rate_limited_at
    _rate_limited_at = 0.0


def cooling_down():
    """True for a while after a rate limit.

    This machine's log has 126 hosted failures, almost all 429s: the hosted voice is
    refused, and without this every single reply started by paying for a doomed
    request (about 1.6 s of the wait before the first word), because the voice for a
    reply is chosen once per reply. Remembering the refusal for half a minute costs
    nothing when the limit is gone and saves that whole round trip while it is not.
    """
    return (time.monotonic() - _rate_limited_at) < RATE_LIMIT_S


def worth_retrying(exc):
    """True for a refusal that is worth asking again for.

    A rate limit or a server error is ordinary on a free tier and passes; a request
    the API simply will not take will not start working in half a second, and waiting
    for it twice is a second of silence for nothing.
    """
    text = str(exc)
    return "HTTP 429" in text or "HTTP 5" in text or text.startswith("network:")

# A sentence ends at a full stop, a question mark, an exclamation mark, an ellipsis
# or a Devanagari danda - and then either whitespace, or the next sentence starting
# with a capital. The second case is not hypothetical: the model wrote
# "Just keeping the laptop humming.Just keeping the laptop humming." with no space
# after the stop, so the two sentences were never separated, the repeat guards (which
# compare whole lines) never saw a repeat, and the owner heard one sentence, twice,
# drawn out as a single utterance. A word of at least two letters before the stop
# keeps "U.S.A." and "e.g." in one piece.
# Japanese and Hindi end a sentence with their own marks - `。！？` and `।` - and a
# model writing in either language uses them, never a full stop. Without them here, a
# whole Japanese answer arrived at the voice as ONE piece: nothing was spoken until the
# last character had been generated, and the neural request then read a paragraph in one
# go. That was the latency in the two new languages.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[a-z]{2}[.!?])(?=[A-Z\"'(])|(?<=[।。！？])\s*")
_CLAUSE_END = re.compile(r"(?<=[,;:—-])\s+")
# Markdown and list furniture, none of which is a thing anybody says out loud. The
# model is told not to use any of it, and does anyway - so the voice has to cope:
# reading a bullet aloud is how an assistant ends up saying "dash" and "asterisk".
_CODE_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_EMPHASIS = (
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)"), r"\1"),   # a link is its label
)
# A leading bullet or step number, which is structure and not words. `\d{1,3}[.)]`
# needs whitespace after it so that "3.5 gigabytes" keeps its 3.
_BULLET = re.compile(r"^\s*(?:[-*•·‣–—>]+\s*|\d{1,3}[.)]\s+)")
# Characters that are never spoken as part of a sentence. Quotes and apostrophes stay:
# they are punctuation *inside* words and sentences, not furniture around them.
_UNSPOKEN = re.compile(r"[*_`#~^|<>\[\]{}\\]")


def speakable(text):
    """The part of a line that can actually be said out loud.

    Everything here is a shape a model reply really takes on this machine, not a
    hypothetical: a heading on its own line ("Commands."), a bulleted or numbered
    list, `**emphasis**`, a link, a fenced block of code. None of it is speech. A
    piece that is left with no letters or digits at all - a lone ".", a "-", a "1." -
    is dropped rather than handed to the voice, because that is what makes an
    assistant read punctuation out loud.

    The words are kept in order and joined with spaces; a list becomes one sentence,
    which is how a person would read it.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    raw = _CODE_FENCE.sub(" ", raw)
    lines = []
    for line in raw.replace("\r", "\n").split("\n"):
        # Twice, because a nested marker is two markers: "- 2. Set reminders" loses its
        # bullet and is still left holding the step number.
        for _ in range(2):
            line = _BULLET.sub("", line)
        for pattern, replacement in _EMPHASIS:
            line = pattern.sub(replacement, line)
        line = _UNSPOKEN.sub(" ", line)
        line = " ".join(line.split())
        # A line of pure punctuation or digits - ".", "...", "!?", "-", "1." - is not a
        # sentence and is not spoken. A *letter* is what makes it one, so a stray "1."
        # that is a marker on its own cannot be read out as a word, while
        # "3.5 gigabytes is plenty." still can.
        if line and any(ch.isalpha() for ch in line):
            lines.append(line)
    return " ".join(lines).strip()


def split_sentences(text, limit=MAX_CHARS):
    """Splits a reply into speakable pieces, in order, none of them too long.

    Full stops first, then commas for anything still over the limit - a single long
    sentence is spoken as two pieces rather than refused or cut off mid-word.

    Two things are removed on the way past, and both were audible complaints: the
    markers in `speakable`, and a sentence that repeats the one immediately before it.
    A model that has just been asked the same empty question twice writes the same
    answer twice, and the owner should hear it once.
    """
    pieces = []
    for chunk in _SENTENCE_END.split(speakable(text)):
        chunk = " ".join(chunk.split())
        if not chunk or not any(ch.isalnum() for ch in chunk):
            continue
        while len(chunk) > limit:
            cut = chunk.rfind(" ", 0, limit)
            if cut < limit * 0.5:
                # Japanese and Hindi prose has no spaces to break on, so a long piece
                # is cut at a clause mark near the limit; failing that, at the limit.
                for mark in ("、", "，", "。", "।", "،"):
                    at = chunk.rfind(mark, 0, limit)
                    if at >= limit * 0.5:
                        cut = at + len(mark)
                        break
                else:
                    cut = limit
            pieces.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if not chunk:
            continue
        if pieces and len(chunk) > 8 and chunk.lower() == pieces[-1].lower():
            continue                 # the same sentence twice in a row: said once
        pieces.append(chunk)
    return pieces


class SentenceStream:
    """Turns a growing reply into whole sentences, as the words arrive.

    The model streams tokens, not sentences, so this holds the incomplete tail back
    until it is a sentence - speaking a fragment and then the rest of it is how a
    voice ends up stuttering. `flush()` releases whatever is left when the stream
    ends, so the last sentence is never lost.
    """

    def __init__(self, min_chars=12):
        self.min_chars = max(1, int(min_chars))
        self._buffer = ""
        # The last sentence handed out, so a repeat is caught even when it arrives in a
        # later piece or in `flush()`. A model asked the same empty question twice in a
        # row writes the same answer twice, and the live log has one sentence *glued* to
        # itself ("...humming.Just keeping the laptop humming.") which no whole-line
        # guard could see. The owner hears it once.
        self._last = ""

    def _emit(self, piece):
        """Keeps `piece` unless it is the sentence that was just handed out."""
        if not piece:
            return False
        if len(piece) > 8 and piece.lower() == self._last.lower():
            return False
        self._last = piece
        return True

    def feed(self, text):
        """Adds text; returns the sentences that are now complete.

        A sentence too short to stand on its own waits for the next one and is spoken
        with it. It used to be *dropped* on the floor, which is the kind of bug that only
        shows up in the words a user hears: a document whose first line is a title, or a
        reply that starts "Yes.", lost that sentence entirely.

        The break between sentences is `_SENTENCE_END`, the same one `split_sentences`
        uses, so a sentence that starts immediately after a full stop ("humming.Just")
        ends one piece and begins the next. Budgeting with a different rule here is how
        two sentences arrived at the voice as one.
        """
        self._buffer += str(text or "")
        pieces = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if not match:
                break
            # A boundary is either "full stop, then space" - which consumes the space -
            # or "full stop, then a capital", which is zero-width and must leave the new
            # sentence in the buffer, growing.
            end = match.end() if match.end() > match.start() else match.start()
            if end <= 0:
                break
            head = self._buffer[:end].strip()
            if not head:
                self._buffer = self._buffer[end:]
                continue
            if len(head) < self.min_chars:
                extra = _SENTENCE_END.search(self._buffer[end:])
                if not extra:
                    break                     # too short alone: wait, then say it joined up
                grown = extra.end() if extra.end() > extra.start() else extra.start()
                if not grown:
                    break                     # nothing more to join it to yet
                end += grown
                head = self._buffer[:end].strip()
            self._buffer = self._buffer[end:]
            for piece in split_sentences(head):
                if self._emit(piece):
                    pieces.append(piece)
        return pieces

    def flush(self):
        """Whatever is left, when the reply has finished."""
        rest, self._buffer = self._buffer, ""
        return [piece for piece in split_sentences(rest) if self._emit(piece)]


class TtsError(RuntimeError):
    """The hosted voice refused, timed out or returned something that is not audio."""


def _wav_to_audio(blob):
    """A WAV in memory as (rate, float32 mono numpy array)."""
    import wave

    import numpy as np
    with wave.open(io.BytesIO(blob), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    dtype = {1: np.int8, 2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:
        raise TtsError(f"unsupported sample width {width}")
    audio = np.frombuffer(raw, dtype=dtype).astype(np.float32) / float(2 ** (8 * width - 1))
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return rate, audio


class GroqVoice:
    """One sentence in, one WAV out, over the standard library.

    The key never reaches a log line and never leaves this object: only its length
    is ever recorded, and a failure is written down by class name.
    """

    def __init__(self, api_key=None, model=None, voice=None, base_url=None, timeout=None,
                 opener=None):
        self.api_key = (api_key if api_key is not None else config.cloud_key() or "").strip()
        self.model = str(model or config.CLOUD_TTS_MODEL)
        self.voice = str(voice or config.CLOUD_TTS_VOICE)
        self.base_url = str(base_url or config.CLOUD_BASE_URL).rstrip("/")
        self.timeout = config.CLOUD_TTS_TIMEOUT_S if timeout is None else timeout
        self._opener = opener or self._post
        self.calls = 0
        self.last_error = None

    @property
    def available(self):
        return bool(self.api_key)

    def _post(self, url, payload):
        """One POST that returns raw audio bytes. Raises TtsError with the reason."""
        import json
        import urllib.error
        import urllib.request

        from .llm import USER_AGENT
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "audio/wav",
                     "User-Agent": USER_AGENT},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise TtsError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TtsError(f"network: {exc.reason}") from exc

    def speech(self, text):
        """The spoken audio for one short piece of text: (rate, float32 array)."""
        if not self.available:
            raise TtsError("no API key")
        payload = {"model": self.model, "input": str(text)[:MAX_CHARS],
                   "voice": self.voice, "response_format": "wav"}
        blob = self._opener(self.base_url + "/audio/speech", payload)
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 44:
            raise TtsError("the reply was not audio")
        self.calls += 1
        return _wav_to_audio(bytes(blob))


class VoiceStream:
    """Speaks sentences in the order they arrive, and can be stopped dead.

    Two threads, and the split is the whole trick. One **synthesises**: it takes the
    next sentence while the current one is still playing, so a hosted round trip
    (measured at about a second on this machine) happens *underneath* the audio
    instead of in a silence between two sentences. The other **plays**: it takes
    prepared pieces in order and speaks them, always exactly one at a time.

    Order is guaranteed by construction - a single producer, a single consumer, one
    FIFO queue between them - so sentences cannot overlap, arrive out of order or be
    spoken twice even though generation and playback run at the same time.

    `cancel()` empties both queues, aborts the playback in progress and marks the
    stream closed, so anything still holding a reference to it cannot push one more
    sentence out after the user has said stop.

    **One reply is spoken in one voice.** The hosted voice and the local one are two
    different people reading the same answer, so the voice is chosen once, by the first
    sentence, and kept for the rest of the stream: a sentence that cannot be synthesised
    turns the reply over to the local voice rather than alternating between the two.
    That alternation was real - the user's log has 126 hosted failures, mostly rate
    limits - and hearing two voices in one answer is what "the voices overlap" meant.
    """

    PREFETCH = 4                      # pieces prepared ahead of the voice
    SYNTH_THREADS = 3                 # and how many of them are prepared at once

    def __init__(self, speaker=None, voice=None, device=None, on_level=None, volume=None,
                 allow_cloud=True, on_idle=None, trace=None):
        self.speaker = speaker                    # the SAPI fallback and its device
        # The request's diagnostics record (core/diag.py). Marks are taken here rather
        # than in the session because synthesis and playback happen on these workers,
        # and the two numbers that matter - time to first audio, time until the first
        # sound is audible - are only knowable from here.
        self.trace = trace
        self._audio_prepared = False
        self._play_marked = False
        # Called the moment the last sentence has really finished playing, so the
        # circle can go back to its idle colour *then* rather than when a watchdog
        # next looks. See `_announce_idle`.
        self.on_idle = on_idle
        # `allow_cloud` False keeps a session (or a test) off the network entirely:
        # the same sentences are spoken by the local voice, in the same order.
        self.voice = (voice if voice is not None else GroqVoice()) if allow_cloud else None
        self._hosted_voice = bool(self.voice is not None
                                  and getattr(self.voice, "available", False))
        self._voice_choice = None                 # None until the first sentence decides
        # The second voice: Japanese and Hindi, which the SAPI voice cannot say.
        # Independent of `allow_cloud` - the local path is still the default for
        # English, and this only ever takes the sentences SAPI has no voice for.
        self.neural = NeuralVoice() if config.NEURAL_TTS else None
        self.lang = None                          # the last language spoken
        self._neural_off_until = 0.0
        self._neural_retried = False              # the first neural sentence gets two tries
        self._neural_pieces = set()               # seq numbers prepared neurally
        self.device = device if device is not None else getattr(speaker, "device", None)
        self.on_level = on_level or getattr(speaker, "on_level", None)
        self.volume = float(config.VOICE_VOLUME if volume is None else volume)
        self.spoken = []                          # every piece actually played
        self.failures = 0                         # consecutive hosted failures
        self._level = 0.0                         # loudness of the block playing now
        self._playing = False
        self.last_error = None
        self._queue = deque()             # sentences waiting to be synthesised
        self._ready = deque()             # (text, audio or None) waiting to be played
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._ready_event = threading.Event()
        self._abort = threading.Event()
        self._closed = False
        # Two flags, not one: a single event shared by two threads can be cleared by
        # whichever finishes first, and then `finish()` would believe the voice was
        # idle with a sentence still in it.
        self._busy_play = threading.Event()
        self._busy_synth = threading.Event()
        self._idle_seq = 0                # invalidates an armed "we have stopped" check
        # Prepared out of order, played in order. The hosted voice takes one to three
        # seconds per sentence, which is longer than the sentence itself lasts, so a
        # single synthesiser fell behind the voice no matter how far ahead it was
        # allowed to run: every full stop was a wait. Three sentences are prepared at
        # once and the player takes them by number, so the order is still exact.
        self._pending = {}                # seq -> (text, audio), finished early
        self._next_seq = 0                # the next piece handed to a synthesiser
        self._next_out = 0                # the next piece the voice may have
        self._in_flight = 0               # being synthesised right now
        self._more = threading.Condition(self._lock)
        self._thread = threading.Thread(target=self._play_worker, daemon=True)
        self._speaker_thread = threading.Thread(target=self._synth_workers, daemon=True)
        self._thread.start()
        self._speaker_thread.start()

    # ---------- what the caller does ----------------------------------------

    def feed(self, text):
        """Queues one piece to speak. Returns False once the stream is stopped.

        The piece is stripped of markers here as well as in the splitter, because not
        every caller splits first: a plan's `say` step, a greeting, a written "still
        working on it" line and the read-back of a document all arrive as whole lines,
        and any of them can carry the markdown a model likes to write.
        """
        text = speakable(text)
        if not text or self._closed:
            return False
        # A long Japanese or Hindi sentence is spoken in pieces, not as one paragraph.
        # The neural voice takes time in proportion to the characters it is given, and
        # one 200-character request means the first sound arrives after all of it has
        # been synthesised. Two 80-character requests, prepared in parallel by the
        # synthesisers, have the first sound out while the second is still being made.
        pieces = [text]
        if len(text) > NEURAL_CHUNK and langs.spoken(langs.detect(text)):
            pieces = langs.split_neural(text, NEURAL_CHUNK)
        with self._lock:
            self._queue.extend(pieces)
            self._idle_seq += 1           # more is coming: any armed idle check is void
            self._more.notify_all()
        self._wake.set()
        return True

    def feed_many(self, pieces):
        return [self.feed(piece) for piece in pieces]

    def finish(self, timeout=None):
        """No more pieces are coming. Waits until every one of them has played."""
        deadline = None if timeout is None else time.time() + float(timeout)
        while not self._closed:
            with self._lock:
                empty = not self._queue and not self._ready and not self._pending
            if empty and not self.busy:
                return True
            if deadline is not None and time.time() > deadline:
                return False
            time.sleep(0.05)
        return False

    def cancel(self):
        """Stop now: drop what is queued and cut off what is playing."""
        self._closed = True
        self._abort.set()
        with self._lock:
            self._queue.clear()
            self._ready.clear()
            self._pending.clear()
            self._more.notify_all()
        self._wake.set()
        self._ready_event.set()
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception:
                pass
        return True

    @property
    def cancelled(self):
        return self._closed


    @property
    def playing(self):
        """True while audio is coming out, so the microphone can be gated."""
        return self._playing

    @property
    def level(self):
        """Loudness of the block being played, for that gate."""
        return self._level

    @property
    def hosted(self):
        """True while the hosted voice is still the one this reply is being spoken in.

        Sticky on purpose. It used to be recomputed from the number of consecutive
        failures, and any success reset that count - so a reply that hit a rate limit
        came out hosted, local, hosted, local, sentence by sentence. One reply, one
        voice: the first sentence picks, and a failure hands the rest of the reply to
        the local voice. The next request starts with the hosted voice again.
        """
        return (self._hosted_voice
                and self._voice_choice != "local"
                and not cooling_down())

    @property
    def busy(self):
        return self._busy_play.is_set() or self._busy_synth.is_set()

    def wait(self, timeout=0.5):
        return self._thread.join(timeout=timeout)

    # ---------- the two workers ---------------------------------------------

    def _synth_workers(self):
        """Starts the small pool that turns queued sentences into audio.

        This is the latency work, and it is why several threads exist. Measured on
        this machine, one hosted sentence takes 1.6 to 3.3 seconds to come back and
        lasts 0.8 to 1.7 seconds once its silence is trimmed - so one synthesiser,
        however far ahead it is allowed to run, falls a second further behind with
        every full stop. That is exactly what the owner heard as "extremely long
        pauses between full stops". Three at once costs the same number of requests
        and turns a queue that drains slower than it fills into one that is ready
        before its turn.
        """
        for _ in range(self.SYNTH_THREADS):
            threading.Thread(target=self._synth_loop, daemon=True).start()

    def _synth_loop(self):
        """One synthesiser: take the next unattributed piece, prepare it, file it.

        The pieces are numbered as they are taken and the results land in a dict, so
        the player is handed them in the order they were queued no matter which
        thread finished first. A piece that cannot be synthesised is stored without
        audio - the player still speaks it, with the local voice - so a network
        failure costs quality and never the sentence itself.
        """
        while not self._closed:
            with self._more:
                while not self._closed and (not self._queue
                                            or len(self._ready) + self._in_flight >= self.PREFETCH
                                            or (self._voice_choice is None
                                                and self._in_flight >= 2)):
                    # Either there is nothing to do, the voice is already well stocked,
                    # or the first two sentences are in flight and neither has chosen
                    # the voice yet. Two rather than one, because waiting for a whole
                    # hosted round trip before starting the second sentence is a
                    # second of silence in the middle of the first reply - the very
                    # gap this pool exists to remove - and `_drain` fixes the one thing
                    # that can go wrong: a piece prepared in a voice the reply then
                    # changed its mind about.
                    self._more.wait(0.2)
                if self._closed:
                    return
                piece = self._queue.popleft()
                seq = self._next_seq
                self._next_seq += 1
                self._in_flight += 1
                self._busy_synth.set()
            audio = None
            # Every piece is timed, not only the first: `tts` is the total time spent
            # generating this reply's audio, while the *first* audio instant is marked
            # separately because that is what the user waits for.
            started_ns = time.perf_counter_ns() if self.trace is not None else None
            try:
                audio = self._synthesise(piece, first=(seq == 0), seq=seq)
            except Exception as exc:              # a broken voice must not end a task
                log.event("tts_failed", error=type(exc).__name__, chars=len(piece))
                if self.trace is not None:
                    self.trace.error("tts", exc)
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._voice_choice is None:
                    self._voice_choice = "local"  # and never leave the others waiting
            finally:
                if started_ns is not None:
                    self.trace.add("tts", time.perf_counter_ns() - started_ns)
                if self.trace is not None and audio is not None and not self._audio_prepared:
                    self._audio_prepared = True
                    self.trace.mark("first_audio")
                with self._more:
                    self._pending[seq] = (piece, audio)
                    self._in_flight -= 1
                    self._drain()
                    if self._in_flight == 0:
                        self._busy_synth.clear()
                    self._more.notify_all()
                self._ready_event.set()

    def _drain(self):
        """Moves every piece that is now contiguous into the play queue.

        Called with the lock held. Out-of-order results wait in `_pending` until the
        one before them arrives, which is what keeps the voice in order without ever
        making a faster synthesiser wait.
        """
        while self._next_out in self._pending:
            seq = self._next_out
            piece, audio = self._pending.pop(seq)
            self._next_out += 1
            if self._closed:
                continue
            if (audio is not None and self._hosted_voice
                    and self._voice_choice == "local"
                    and seq not in self._neural_pieces):
                # (A Japanese or Hindi sentence is not re-voiced into English: its
                # audio was made by the only voice that can say it.)
                # Prepared in the hosted voice while the sentence in front of it was
                # still deciding, and that sentence handed the reply to the local
                # voice. Two voices reading one answer is what the owner heard as
                # "the voices overlap, local and cloud", so this piece is rendered
                # again - quietly, before its turn - in the voice the reply settled on.
                log.event("tts_revoice", chars=len(piece))
                audio = self._prepare_local(piece)
            self._ready.append((piece, audio))

    def _synthesise(self, text, first=False, seq=None):
        """The audio for one piece, or None when the local voice will speak it.

        The language is decided first, because it decides which voice may speak at
        all: English has SAPI (and the hosted voice when it is on), Japanese and
        Hindi only have the neural one. A sentence with no letters inherits the
        language of the sentence before it, so "3:30" is not read aloud in English
        in the middle of a Hindi answer.

        `first` marks the piece that opens the reply - the only one that retries a
        refusal, because it is the only one whose answer decides the voice for the
        whole reply. With several pieces being prepared at once, retrying each of
        them would mean four doomed requests during one rate limit.

        First a cooldown check: a rate limit from any earlier reply stops this one from
        asking at all, which is the difference between a 0.15 s first word and a 1.6 s
        one while the hosted voice is refused. Then the sentence that opens a reply gets
        one retry if the refusal looks temporary (a 5xx, a dropped connection), because
        that is the sentence that chooses the voice for the whole reply.

        The local voice is prepared here too, and for the same reason. Rendering a
        sentence and playing it in one step put SAPI's whole render time into the gap
        between two sentences of one answer; rendering it *now* and playing it later
        removes that gap entirely.
        """
        global _rate_limited_at
        lang = langs.detect(text) or self.lang or "en"
        if langs.spoken(lang):
            self.lang = lang
            neural = self._prepare_neural(text, lang, seq)
            if neural is not None:
                return neural
            # The neural voice is gone (offline, rate limited, uninstalled): the
            # sentence is still spoken, in the local voice, with the wrong accent.
            # Wrong accent beats silence, and the log says which one happened.
        if not self.hosted:
            if self._voice_choice is None:
                self._voice_choice = "local"     # this reply is the local voice's
            return self._prepare_local(text)
        tries = 2 if first and self._voice_choice is None else 1
        failure = None
        for attempt in range(tries):
            try:
                rate, audio = self.voice.speech(text)
            except TtsError as exc:
                failure = exc
                if attempt + 1 < tries and worth_retrying(exc):
                    time.sleep(VOICE_RETRY_S)
                    continue
                break
            self.failures = 0
            if self._voice_choice is None:
                self._voice_choice = "hosted"
            return (rate, trim_silence(audio, rate))
        self.failures += 1
        self._voice_choice = "local"              # and the rest of this reply with it
        if "HTTP 429" in str(failure):
            _rate_limited_at = time.monotonic()    # everyone else, stop asking for a while
        self.last_error = str(failure)[:200]
        log.event("tts_fallback", error=type(failure).__name__, failures=self.failures,
                  detail=str(failure)[:120])
        return None

    def _prepare_local(self, text):
        """The local voice's audio for one piece, rendered before its turn comes.

        The speaker renders on its own worker thread - the only thread allowed to touch
        the COM voice - and this is silent, so it happens underneath the sentence that
        is playing. A speaker that cannot pre-render (a test's fake) answers None and
        the piece is spoken the old way, in one step.
        """
        prepare = getattr(self.speaker, "prepare", None)
        if prepare is None:
            return None
        try:
            ready = prepare(text)
        except Exception as exc:
            log.event("speech_prepare_failed", error=type(exc).__name__)
            return None
        if not ready:
            return None
        try:
            rate, audio = ready
        except (TypeError, ValueError):
            return None
        return (rate, trim_silence(audio, rate))

    def _prepare_neural(self, text, lang, seq=None):
        """Japanese or Hindi, through the neural voice. None means "let SAPI try".

        A failure is recorded and cooled down rather than retried per sentence: the
        usual causes are a dropped connection and a rate limit, and both answer the
        same way forty times in a row. The sentence itself is never lost - the caller
        falls back to the local voice - so this only decides which voice it uses.
        """
        if self.neural is None or not self.neural.available():
            return None
        if time.monotonic() < self._neural_off_until:
            return None
        started = time.perf_counter_ns() if self.trace is not None else None
        # The first Japanese or Hindi sentence of a reply is asked for twice. The
        # endpoint drops connections now and then, and the price of that was the whole
        # reply: one failure put the language on a cooldown, and the local English
        # voice then read kanji - which is to say nothing was heard. Two attempts and
        # a sixth of a second is cheap next to that. Later sentences get one, so a
        # broken network cannot multiply the wait.
        attempts = 1 if self._neural_retried else 2
        self._neural_retried = True
        audio = None
        for attempt in range(attempts):
            try:
                rate, audio = self.neural.speech(text, lang)
            except Exception as exc:
                audio = None
                if attempt + 1 < attempts:
                    log.event("neural_tts_retry", lang=lang, error=str(exc)[:60])
                    time.sleep(0.15)
                    continue
                self._neural_off_until = time.monotonic() + NEURAL_COOLDOWN_S
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.event("neural_tts_failed", lang=lang, error=str(exc)[:80])
                if self.trace is not None:
                    self.trace.error("tts_neural", exc)
            if audio is not None:
                break
        if started is not None:
            self.trace.add("tts_neural", time.perf_counter_ns() - started)
        if audio is None:
            return None
        if not len(audio):
            log.event("neural_tts_empty", lang=lang)
            return None
        if seq is not None:
            self._neural_pieces.add(seq)
        if self._voice_choice is None:
            self._voice_choice = "neural"
        self.lang = lang
        log.event("neural_tts", lang=lang, chars=len(text), rate=rate)
        return (rate, trim_silence(audio, rate))

    def _announce_idle(self):
        """Arms a one-shot check for "the voice has really stopped".

        The colour of the circle is what the owner watches, and it is put back by
        something looking at the voice rather than by the voice itself: the watchdog
        looks twice a second and then waits for another 0.4 s of quiet, which is the
        two or three seconds the orb stayed lit after the answer had ended. This is
        the stream saying so on its own, a hair late so that the gap between two
        sentences of one reply is not mistaken for its end.

        Nothing blocks the player to do it: the check runs on a one-shot timer, and it
        only believes a queue that is still empty when it fires, so a sentence fed
        while it was armed cancels it.
        """
        if self.on_idle is None or self._closed:
            return
        with self._lock:
            self._idle_seq += 1
            seq = self._idle_seq
        timer = threading.Timer(IDLE_GRACE_S, self._idle_now, args=(seq,))
        timer.daemon = True
        timer.start()

    def _idle_now(self, seq):
        if self._closed or seq != self._idle_seq:
            return                        # more arrived while this was armed
        with self._lock:
            if self._queue or self._ready or self._pending or self._in_flight:
                return
        if self._busy_synth.is_set() or self._busy_play.is_set():
            return
        try:
            self.on_idle()
        except Exception as exc:                 # a broken listener must not kill a reply
            log.event("speech_idle_failed", error=type(exc).__name__)

    def _play_worker(self):
        """Speaks prepared pieces, in order, one at a time."""
        while not self._closed:
            if not self._ready_event.wait(0.2):
                continue
            self._ready_event.clear()
            while not self._closed:
                with self._lock:
                    item = self._ready.popleft() if self._ready else None
                if item is None:
                    # Nothing left to play and nothing ready to play: the voice has
                    # stopped. Say so now - the display hears it from here rather than
                    # from the watchdog's next look, half a second and 0.4 s of quiet
                    # later, which is the whole of "still green after it finishes".
                    self._announce_idle()
                    break
                # Room in the buffer again: a synthesiser may have stopped because the
                # voice was already well stocked, and it is waiting for this to be said.
                with self._more:
                    self._more.notify_all()
                text, audio = item
                self._busy_play.set()
                self.spoken.append(text)
                if self.trace is not None and not self._play_marked:
                    self._play_marked = True
                    self.trace.mark("play_start")
                    if not self._audio_prepared:
                        # The local voice renders at play time, so its first audio
                        # and its playback start are the same instant by definition.
                        self.trace.mark("first_audio")
                try:
                    if audio is None:
                        self._speak_local(text)
                    else:
                        try:
                            self._play(audio[0], audio[1])
                        except Exception as exc:
                            # The sentence is made but the speakers would not take it.
                            # Dropping it here is how "sometimes it speaks and sometimes
                            # it doesn't" happened: the words were synthesised, and
                            # then a refused stream meant they were never heard. The
                            # local voice still has them, so the sentence is spoken
                            # instead of lost.
                            log.event("tts_play_failed", error=type(exc).__name__,
                                      chars=len(text))
                            if self.trace is not None:
                                self.trace.error("playback", exc)
                            self.last_error = f"{type(exc).__name__}: {exc}"
                            self._speak_local(text)
                except Exception as exc:
                    log.event("tts_play_failed", error=type(exc).__name__, chars=len(text))
                    if self.trace is not None:
                        self.trace.error("playback", exc)
                    self.last_error = f"{type(exc).__name__}: {exc}"
                finally:
                    self._busy_play.clear()
        self._busy_play.clear()

    def _speak_local(self, text):
        """The SAPI path, used when the hosted voice is unavailable or refused.

        The level is routed to this stream's listener while it speaks. The hosted path
        reports its own levels, and the local one reported them to whoever built the
        Speaker - so a reply spoken locally could be audible with a circle that stayed
        still, which is the opposite of the same complaint.
        """
        if self.speaker is None:
            return
        self._voice_choice = "local"    # a stream that has spoken locally stays local
        if self.trace is not None and not self._audio_prepared:
            self._audio_prepared = True
            self.trace.mark("first_audio")
        previous = getattr(self.speaker, "on_level", None)
        if self.on_level is not None:
            try:
                self.speaker.on_level = self.on_level
            except Exception:
                previous = None
        try:
            self.speaker.say(text)
        except Exception as exc:
            log.event("tts_local_failed", error=type(exc).__name__)
        finally:
            if self.on_level is not None:
                try:
                    self.speaker.on_level = previous
                except Exception:
                    pass

    def _play(self, rate, audio):
        """Plays one synthesised piece, reporting its real level to the display."""
        import numpy as np
        import sounddevice as sd

        samples = np.clip(np.asarray(audio, dtype="float32") * self.volume, -1.0, 1.0)
        if self._closed or len(samples) == 0:
            return
        self._abort.clear()
        cursor = [0]
        finished = threading.Event()

        def report(chunk):
            level = float(np.sqrt(np.mean(chunk ** 2))) * 6.0 if len(chunk) else 0.0
            self._level = min(1.0, level)
            if self.on_level is not None:
                self.on_level(self._level)

        def callback(outdata, frames, _time, _status):
            if self._abort.is_set() or self._closed:
                raise sd.CallbackAbort
            chunk = samples[cursor[0]:cursor[0] + frames]
            cursor[0] += len(chunk)
            outdata[:len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):] = 0.0
                report(chunk)
                finished.set()
                raise sd.CallbackStop
            report(chunk)

        tries = [self.device] if self.device is None else [self.device, None]
        # The hosted voice and the local SAPI voice are two playback paths onto the
        # same speakers. This is the one lock both of them take (core/voice.py), so a
        # sentence from either can only start once the other has finished - without
        # it, a fallback line and a streamed line were audible at the same time.
        from .voice import one_voice_at_a_time
        with one_voice_at_a_time():
            for attempt in tries:
                try:
                    stream = sd.OutputStream(samplerate=int(rate), channels=1, dtype="float32",
                                             device=attempt, blocksize=1024, callback=callback)
                    with stream:
                        self._playing = True
                        finished.wait(_seconds_for(len(samples), rate) + 4.0)
                    if self._abort.is_set() or self._closed:
                        return
                    return
                except Exception as exc:
                    if attempt is not None:
                        log.event("tts_device_failed", device=attempt, error=type(exc).__name__)
                        self.device = None
                        continue
                    raise TtsError(f"{type(exc).__name__}: {exc}") from exc
                finally:
                    self._playing = False
                    self._level = 0.0


def _seconds_for(samples, rate):
    return (float(samples) / float(rate or 1)) if rate else 1.0
