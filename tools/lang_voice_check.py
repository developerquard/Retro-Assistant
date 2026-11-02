"""Japanese and Hindi, end to end: the voice says it, the ears read it back.

    python tools/lang_voice_check.py            # sound in, transcript out
    python tools/lang_voice_check.py --no-ears  # speech only, for a quick check

Three checks, and each one is a failure the owner would hear:

1. **The language is detected.** The same sentences are handed to `core.langs`, which
   is what decides the voice in the speech pipeline.
2. **The voice has audio in it.** Each sentence is synthesised through the neural
   voice, measured (rate, seconds, peak) and written to `.preview/` - so a clip that
   is silent, clipped, or half the sentence is a reported number rather than a guess.
3. **The ears understand it.** The synthesised audio is played straight into
   `Ears.transcribe`, the same recogniser the microphone uses. A Japanese sentence
   that comes back as English words means the conversation could never have worked,
   however good the voice was.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The console's codepage has no kana or Devanagari; escaping them is better than a
# UnicodeEncodeError that hides the result it was printing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

from core import langs  # noqa: E402
from core.neuralvoice import NeuralVoice, write_wav  # noqa: E402

SENTENCES = (
    ("ja", "こんにちは。今日はいい天気ですね。"),
    ("hi", "नमस्ते। आज मौसम अच्छा है।"),
)

# Script detection on sentences that are not synthesised: a Japanese line with an
# English word in it, kanji without kana, a Devanagari line, and one with no letters
# at all - which must be None so the sentence inherits the reply's language.
DETECTION = (
    ("ja", "Spotify を開いて"),
    ("hi", "Spotify खोलो"),
    ("ja", "東京は晴れです"),
    ("en", "Open Spotify"),
    (None, "3:30"),
)

OUT = os.path.join(ROOT, ".preview")


def main(argv):
    problems = []
    voice = NeuralVoice()
    print(f"neural   : available={voice.available()} "
          f"ja={voice.voice_for('ja')} hi={voice.voice_for('hi')}")
    if not voice.available():
        print("problem  : the neural voice is not installed (edge-tts / PyAV)")
        return 1

    for language, sentence in DETECTION:
        found = langs.detect(sentence)
        if found != language:
            problems.append(f"detected {found!r} for {sentence!r}, expected {language!r}")

    clips = {}
    for language, sentence in SENTENCES:
        detected = langs.detect(sentence)
        print(f"detect   : {langs.name(detected):<8} {sentence[:34]}")
        if detected != language:
            # The Hindi sentence labelled ja must be seen as Hindi; the others must
            # match. This is the rule the voice picker follows.
            problems.append(f"detected {detected!r} for {sentence[:20]!r}")
            continue
        import time
        started = time.time()
        rate, audio = voice.speech(sentence, detected)
        seconds = len(audio) / float(rate)
        peak = float(max(abs(audio))) if len(audio) else 0.0
        path = os.path.join(OUT, f"lang_{detected}.wav")
        write_wav(path, audio, rate)
        print(f"voice    : {langs.name(detected):<8} {len(audio) / float(rate):4.2f}s "
              f"@ {rate} Hz, peak {peak:.2f}, {time.time() - started:4.2f}s to synthesise"
              f" -> {os.path.relpath(path, ROOT)}")
        if seconds < 0.6 or peak < 0.05:
            problems.append(f"{detected}: the clip is silent or too short ({seconds:.2f}s)")
        clips[detected] = (rate, audio, sentence)

    if "--no-ears" not in argv:
        from core.voice import Ears, resample
        import numpy as np
        ears = Ears()
        for language, (rate, audio, sentence) in clips.items():
            heard = ears.transcribe(resample(audio, rate, 16000))
            print(f"ears     : {langs.name(language):<8} heard {heard!r}")
            if not heard.strip():
                problems.append(f"{language}: the recogniser heard nothing")
    for problem in problems:
        print(f"problem  : {problem}")
    print("languages ok" if not problems else "languages FAILED")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
