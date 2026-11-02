"""M3: list the installed voices, show Retro's choice, and check for Hindi.

Prints every voice pyttsx3 can see, marks the one config selects, and says
whether a Hindi voice is installed (Windows ships none by default).

  python tools/list_voices.py                 # list and report
  python tools/list_voices.py --say           # also speak the greeting out loud
  python tools/list_voices.py --save out.wav  # write it to a WAV and check the audio

Change the choice with RETRO_VOICE (a substring of the voice name or id), e.g.
  set RETRO_VOICE=Hazel
"""
import argparse
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config                                            # noqa: E402
from core.voice import GREETING, Speaker, find_hindi_voice, pick_voice  # noqa: E402

SAMPLE = GREETING + " Okay, going to sleep. Say Retro when you need me."


def describe(voice):
    langs = ", ".join(getattr(voice, "languages", None) or []) or "?"
    return f"{voice.name}  [{langs}]  ...{voice.id[-32:]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--say", action="store_true", help="speak the sample out loud")
    ap.add_argument("--save", metavar="PATH", help="write the sample to a WAV file")
    ap.add_argument("--rate", type=int, default=None)
    args = ap.parse_args()

    import pyttsx3
    engine = pyttsx3.init()
    voices = list(engine.getProperty("voices") or [])

    print(f"installed voices: {len(voices)}\n")
    chosen = pick_voice(voices)
    for i, v in enumerate(voices, 1):
        mark = "  <-- Retro uses this" if chosen is not None and v.id == chosen.id else ""
        print(f"  {i}. {describe(v)}{mark}")

    print(f"\nconfigured choice : RETRO_VOICE={config.VOICE_NAME!r}")
    print(f"selected voice    : {getattr(chosen, 'name', None) or '(system default)'}")
    print(f"rate / volume     : {config.VOICE_RATE} wpm / {config.VOICE_VOLUME}")

    hindi = find_hindi_voice(voices)
    if hindi is None:
        # Since M16 the answer is not "no Hindi": Japanese and Hindi are spoken by
        # Microsoft's neural voices (core/neuralvoice.py), which need no Windows
        # language pack at all. A SAPI voice would only make them work offline.
        from core.neuralvoice import NeuralVoice
        neural = NeuralVoice()
        print("\nHindi voice       : no Windows voice - the neural one is used instead"
              f" ({neural.voice_for('hi')}, "
              f"{'ready' if neural.available() else 'MISSING edge-tts'})")
        print("Japanese voice    : "
              f"{neural.voice_for('ja')} ({'ready' if neural.available() else 'unavailable'})")
        print("  Install a Windows voice only if you want them offline: Settings > "
              "Time & language > Speech.")
    else:
        print(f"\nHindi voice       : {hindi.name} (Windows)\n"
              "Japanese voice    : the neural one, since Windows has none here")

    speaker = Speaker(rate=args.rate)
    print(f"\nRetro will speak with: {speaker.voice_name}")

    if args.save:
        speaker.save_to_file(SAMPLE, args.save)
        with wave.open(args.save) as w:
            frames, rate, width = w.getnframes(), w.getframerate(), w.getsampwidth()
            raw = w.readframes(frames)
        seconds = frames / rate
        peak = 0
        if width == 2:
            import array
            samples = array.array("h")
            samples.frombytes(raw)
            peak = max(abs(s) for s in samples) if samples else 0
        print(f"wrote {args.save}: {seconds:.2f}s, {rate} Hz, peak sample {peak}")
        print("audio present:", "YES" if peak > 500 and seconds > 0.5 else "NO - silent output")

    if args.say:
        print("\nspeaking the sample now ...")
        speaker.say(SAMPLE)
        print("done.")

    if not args.save and not args.say:
        print("\n(pass --say to hear it, or --save out.wav to check the audio)")


if __name__ == "__main__":
    main()
