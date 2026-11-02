"""Speak text through the default Windows voice.

Used to test Retro's ears (wake word and transcription) without a human
speaker, and to give tools/calibrate_voice.py a repeatable speaking level.

  python tools/tts_say.py "Retro, what time is it"

pyttsx3 must stay on the main thread: runAndWait() deadlocks when driven from a
worker thread, which is why this lives in its own process.
"""
import sys

import pyttsx3


def main():
    text = " ".join(sys.argv[1:]).strip() or "Retro, are you there"
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()


if __name__ == "__main__":
    main()
