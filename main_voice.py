"""Retro, voice mode.

Say "Retro" and it answers, then takes commands for as long as you keep talking.
Say "Retro, sleep" (or just "sleep") and it goes quiet, listening only for its
name again. While asleep nothing else you say is acted on.

Speech in is Whisper, speech out is the Windows voice, and anything the typed
rules can't read is handed to the local Llama model, which never runs anything
itself - it only proposes a command that core/validate.py checks.

Ctrl+C to stop.
"""
from core.llm import Fallback
from core.runner import handle
from core.voice import (GREETING, SLEEP_REPLY, Ears, Speaker, classify, listen_once)
from ui import hud


def say(speaker, text):
    hud.status("retro", text)
    try:
        speaker.say(text)
    except Exception as exc:                    # a broken voice must not end the session
        hud.status("error", f"could not speak ({type(exc).__name__})")


def build():
    """Loads the speech model and the voice. Kept separate so failures are clear."""
    hud.status("loading", "speech model (the first run downloads it)")
    ears = Ears()
    return ears, Speaker(), Fallback()


def main():
    try:
        ears, speaker, fallback = build()
    except Exception as exc:
        print(f"Retro could not start: {type(exc).__name__}: {exc}")
        print("Voice input needs the packages in requirements-voice.txt.")
        return

    hud.status("asleep", "say 'Retro'. Ctrl+C to quit.")
    awake = False
    while True:
        try:
            heard = listen_once(ears)
        except KeyboardInterrupt:
            print()
            break
        except Exception as exc:
            hud.status("error", f"microphone problem ({type(exc).__name__})")
            continue

        kind, payload = classify(awake, heard)
        if kind == "ignore":
            continue
        if kind == "sleep":
            say(speaker, SLEEP_REPLY)
            awake = False
            hud.status("asleep", "say 'Retro'")
            continue
        if kind == "wake":
            awake = True
            say(speaker, GREETING)
            hud.status("awake", "listening for a command")
            continue

        # A command means Retro is being used, so stay awake for follow-ups even if
        # the wake word and the command arrived in the same breath.
        awake = True
        result = handle(payload, fallback=fallback)
        say(speaker, result.message)
        hud.status("awake", "listening for a command")

    print("Retro stopped.")


if __name__ == "__main__":
    main()
