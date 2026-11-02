"""Japanese and Hindi, in a voice this machine does not have installed.

Windows ships English SAPI voices here - Hazel and Zira - and nothing for Japanese or
Hindi. `core.voice.Speaker` can only use what Windows has, so asking it for those
languages reads kanji and Devanagari in an English accent, or skips them entirely.
That is why there is a second voice at all.

Microsoft's Edge read-aloud endpoint has neural voices for both languages and needs
no API key, so it is the one that fits: nothing to configure, nothing to pay for, and
the same voice families Windows itself uses.

* each language has one voice, overridable by environment variable
  (`RETRO_TTS_JA_VOICE`, `RETRO_TTS_HI_VOICE`);
* audio comes back as MP3 at 24 kHz, decoded with PyAV - which faster-whisper already
  depends on, so the speech *input* path pays for the speech *output* decoder and
  this module adds one dependency, not two;
* a failure is raised, never hidden. The caller falls back to the local voice so a
  sentence is still heard (wrong accent, right words), and `log.event` records which
  language lost its neural voice so the next report is answerable.

The endpoint is undocumented and unofficial. That is a real risk, which is why it is
one module with one job behind a try/except: when it goes away, the app still speaks.
"""

import io
import threading

from . import config, log

# One event loop for the whole process, on its own thread.
#
# `asyncio.run` per sentence builds and tears down a loop for every piece of every
# reply - and the synthesisers work in parallel, so four of them did that at once. The
# loop is not the expensive part, but it is not free either: measured on this machine
# it was 15-40 ms per sentence before a byte left the machine, paid exactly when the
# first word of a reply is late. One loop, many requests, no churn.
_LOOP = {"loop": None, "lock": threading.Lock()}


def _loop():
    """The loop every neural request runs on, started once."""
    import asyncio

    with _LOOP["lock"]:
        if _LOOP["loop"] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name="neural-voice").start()
            _LOOP["loop"] = loop
    return _LOOP["loop"]

# The voice for each language. Nanami and Swara are the default female voices for
# ja-JP and hi-IN on this endpoint; Keita and Madhur are the male ones.
DEFAULT_VOICES = {
    "ja": "ja-JP-NanamiNeural",
    "hi": "hi-IN-SwaraNeural",
}


def decode_mp3(blob):
    """(rate, mono float32 audio) from an MP3 in memory.

    PyAV rather than a new decoder: `av` is already in the bundle for faster-whisper,
    and it reads this format without a plugin. A clip whose samples overshoot (the
    codec can, at full scale) is scaled back instead of clipping to a buzz.
    """
    import av
    import numpy as np
    container = av.open(io.BytesIO(blob))
    try:
        stream = container.streams.audio[0]
        rate = int(stream.rate or 24000)
        frames = [frame.to_ndarray() for frame in container.decode(stream)]
    finally:
        container.close()
    if not frames:
        return rate, np.zeros(0, dtype="float32")
    audio = np.concatenate(frames, axis=1).mean(axis=0).astype("float32")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak
    return rate, audio


# One warm-up per process, and it is not a nicety: the endpoint pays for a TLS
# handshake and a voice session on the first request, and that first request is the
# first sentence of the first Japanese or Hindi answer the owner hears. Measured on
# this machine, the same 47-character sentence took 4.1 s cold and 1.2-1.9 s warm, so
# a silent five-character request at startup is bought back on the first reply.
_WARM = {"started": False, "lock": None}
WARM_TEXT = "こんにちは"


def warm_async(voice=None):
    """Starts the connection in the background, once per process. Never raises."""
    import threading
    if _WARM["lock"] is None:
        _WARM["lock"] = threading.Lock()
    with _WARM["lock"]:
        if _WARM["started"]:
            return False
        _WARM["started"] = True
    engine = voice or NeuralVoice.__new__(NeuralVoice)
    if voice is None:
        NeuralVoice.__init__(engine)

    def once():
        try:
            if engine.available():
                engine.speech(WARM_TEXT, "ja")
                log.event("neural_tts_warm")
        except Exception as exc:
            log.event("neural_tts_warm_failed", error=type(exc).__name__)

    threading.Thread(target=once, daemon=True).start()
    return True


class NeuralVoice:
    """One sentence in, one decoded clip out - for the languages SAPI cannot say."""

    def __init__(self, voices=None, timeout=None):
        self.voices = dict(DEFAULT_VOICES)
        self.voices.update({str(k).lower(): str(v) for k, v in (voices or {}).items()})
        self.voices.update({str(k).lower(): str(v) for k, v in
                            getattr(config, "NEURAL_VOICES", {}).items()})
        self.timeout = float(config.NEURAL_TIMEOUT_S if timeout is None else timeout)
        self._available = None
        # The first stream of the session should not be the one that pays for the
        # connection. Once per process, in the background, and silent.
        warm_async(self)

    def available(self):
        """True when the package and the decoder are both importable.

        Asked once and remembered: this is called for every sentence, and an import
        attempt per sentence is a lock and a stat per sentence.
        """
        if self._available is None:
            try:
                import av           # noqa: F401  (the decoder)
                import edge_tts     # noqa: F401  (the voice)
                self._available = True
            except Exception as exc:
                log.event("neural_tts_missing", error=type(exc).__name__)
                self._available = False
        return self._available

    def voice_for(self, lang):
        return self.voices.get(str(lang or "").lower(), "")

    def speech(self, text, lang):
        """(rate, audio) for one sentence. Raises when the voice cannot deliver.

        Raising rather than returning None is deliberate: the caller logs the failure
        and hands the sentence to the local voice, and a silent None here would make
        that a mystery instead of a decision.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("nothing to say")
        voice = self.voice_for(lang)
        if not voice:
            raise ValueError(f"no neural voice for {lang!r}")
        if not self.available():
            raise RuntimeError("edge-tts or PyAV is not installed")
        blob = self._download(text, voice)
        rate, audio = decode_mp3(blob)
        if not len(audio):
            raise RuntimeError("the neural voice returned no audio")
        return rate, audio

    def _download(self, text, voice):
        """The MP3 for one sentence. Bounded by the configured timeout."""
        import asyncio
        import concurrent.futures

        import edge_tts

        async def once():
            communicate = edge_tts.Communicate(text, voice)
            out = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    out.extend(chunk["data"])
            return bytes(out)

        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(once(), _loop())
            blob = future.result(self.timeout)
        except concurrent.futures.TimeoutError:
            try:
                future.cancel()
            except Exception:
                pass
            raise RuntimeError(f"the neural voice took longer than {self.timeout:.0f} s")
        except Exception as exc:
            raise RuntimeError(f"{type(exc).__name__}: {exc}")
        if not blob:
            raise RuntimeError("the neural voice returned nothing")
        return blob

    def say(self, text, lang, save=None):
        """Speaks one sentence through the speakers - for the checking tools."""
        from . import sound
        rate, audio = self.speech(text, lang)
        if save:
            write_wav(save, audio, rate)
        return sound.play(audio, rate, volume=float(config.VOICE_VOLUME))


def write_wav(path, audio, rate=24000):
    """Writes mono audio to a 16-bit WAV, so a clip can be checked with ears."""
    import wave

    import numpy as np
    clipped = np.clip(np.asarray(audio, dtype="float32"), -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes((clipped * 32767.0).astype("<i2").tobytes())
    return path
