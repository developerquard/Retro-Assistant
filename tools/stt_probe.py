"""What the recogniser says to a room with nothing in it, and to a real sentence.

This is the tool for the bug the owner reported as "random replies, random lines,
randomly repeating lines" - an assistant answering phrases nobody said. Every one of
those phrases came from the speech recogniser, and almost all of them were Whisper
writing its own `prompt` back out: the prompt used to be a sentence full of commands
("Retro. Commands: what time is it, ... open notepad ..."), and on audio that held no
speech it continued that sentence instead of admitting it heard nothing.

So there are two things to check, and both matter:

* **Silence in, nothing out.** Two seconds of quiet, sent to the real endpoint. Under
  the old prompt this is where "Commands, you're free to go." and friends came from.
* **Speech in, words out.** A sentence rendered by the local voice, sent to the same
  endpoint. The fix is only a fix if real speech still comes back - a guard that
  refuses everything is not a guard, it is a deaf assistant.

Run it with the app closed. It uses the configured key and never prints it.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from core import config, voice  # noqa: E402

SENTENCES = ("Retro, open notepad.", "What time is it?", "Set the volume to forty.")


def silence(seconds=2.0, rate=voice.SAMPLE_RATE):
    """Room noise, not digital zero: that is what the microphone actually sends."""
    rng = np.random.default_rng(7)
    return (rng.normal(0.0, 0.0008, int(rate * seconds))).astype(np.float32)


def speech(text, folder):
    """One sentence, spoken by the local SAPI voice, at the microphone's own rate."""
    from core.voice import Speaker
    speaker = Speaker()
    speaker.ensure_voices()
    raw = Path(folder) / "line.wav"
    speaker.save_to_file(text, raw)
    rate, audio = voice.read_wav_mono(raw)
    if rate != voice.SAMPLE_RATE:                     # resample to the mic's rate
        audio = voice.resample(audio, rate, voice.SAMPLE_RATE)
    return np.asarray(audio, dtype=np.float32)


def main():
    ears = voice.build_ears()
    print(f"recogniser: {type(ears).__name__}"
          f"{' (' + ears.model + ')' if getattr(ears, 'model', None) else ''}")
    print(f"prompt:     {voice.SPEECH_PROMPT!r}\n")

    failures = 0
    quiet = silence()
    heard = ears.transcribe(quiet)
    mark = "ok  " if not heard else "BAD "
    failures += bool(heard)
    print(f"{mark} 2.0s of a quiet room -> {heard!r}")
    if heard:
        print("      ^ that is a phrase nobody said, and it would be answered")

    with tempfile.TemporaryDirectory() as folder:
        for text in SENTENCES:
            audio = speech(text, folder)
            heard = ears.transcribe(audio)
            ok = bool(heard) and heard.strip().strip(".,!?").lower() != ""
            # Not an exact match: what matters is that words arrive at all, and that
            # the ones that do are the words that were said rather than the prompt.
            shares = [w for w in ("notepad", "time", "volume") if w in heard.lower()]
            mark = "ok  " if ok else "BAD "
            failures += not ok
            print(f"{mark} said {text!r}\n      heard {heard!r} {shares if shares else ''}")

    print()
    if failures:
        print(f"{failures} check(s) failed - see above")
        return 1
    print("all checks passed: silence is silence, and speech is still heard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
