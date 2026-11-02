"""Proves the voice can speak, line after line, and records why it could not before.

    python tools/tts_probe.py             # five lines through the real Speaker
    python tools/tts_probe.py --pyttsx3   # the old path, to see the hang for yourself
    python tools/tts_probe.py --lines 10 --render-only

## Why this exists

The original "it stays green for ever and I can't hear anything" bug was here:

    pyttsx3.init(); save_to_file(line); runAndWait()      # line 1: fine
    save_to_file(line2); runAndWait()                     # line 2: never returns

The second render through one pyttsx3 engine blocks for ever. Speech used to run
on the voice loop's thread, so a stuck render froze the whole assistant: the ring
kept animating from the last level it had seen, nothing was audible, and no
command - spoken or typed - could run again until the app was restarted.

Speech goes to SAPI through comtypes now, which renders synchronously into a file
stream: 0.1 s per line instead of 2 s, and no call that fails to return. The
worker thread, the stall detector and the watchdog in core/session.py are all still
there, because a hang of some other kind must never be able to do that again.

`--pyttsx3` is kept deliberately: it is the evidence. Run it and it will hang on
line 2, which is exactly what the user saw.
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINES = (
    "Opened notepad.",
    "Stopped.",
    "Let me think about that.",
    "I found three files in your downloads folder.",
    "Still on it.",
    "Done.",
    "One moment, I am typing that for you.",
    "Sorry, that window would not come to the front.",
)


def speaker_probe(count, render_only):
    from core.voice import Speaker
    speaker = Speaker()
    voices = speaker.ensure_voices()
    print(f"voice    : {speaker.voice_name} ({len(voices)} installed)")
    worst = 0.0
    for index in range(count):
        line = LINES[index % len(LINES)]
        started = time.time()
        if render_only:
            path = os.path.join(tempfile.gettempdir(), f"retro-probe-{index}.wav")
            speaker.save_to_file(line, path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            note = f"{size} bytes"
        else:
            ok = speaker.say(line)
            size = speaker.last_peak
            note = f"played={ok} peak={size:.2f}"
        spent = time.time() - started
        worst = max(worst, spent)
        print(f"  line {index + 1}: {spent:5.2f}s  {note}")
        if (not render_only and size <= 0.01) or (render_only and size <= 44):
            print("  problem : the line produced no audio")
            return 1
    print(f"  {count} lines, {worst:.2f}s worst, wedges={speaker.wedges}")
    if speaker.wedges:
        print("  problem : a line was written off as wedged")
        return 1
    return 0


def pyttsx3_probe(count):
    """The old path, kept as the demonstration of the bug."""
    import pyttsx3
    print("pyttsx3: the second runAndWait() through one engine does not return.")
    engine = pyttsx3.init()
    for index in range(count):
        path = os.path.join(tempfile.gettempdir(), f"retro-pyttsx3-{index}.wav")
        started = time.time()
        engine.save_to_file(f"Line number {index}.", path)
        engine.runAndWait()
        size = os.path.getsize(path) if os.path.exists(path) else 0
        print(f"  line {index + 1}: {time.time() - started:5.1f}s  {size} bytes", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=5)
    parser.add_argument("--render-only", action="store_true",
                        help="write WAVs instead of playing them (no speakers needed)")
    parser.add_argument("--pyttsx3", action="store_true",
                        help="run the old path, which hangs on the second line")
    args = parser.parse_args()
    if args.pyttsx3:
        return pyttsx3_probe(args.lines)
    return speaker_probe(args.lines, args.render_only)


if __name__ == "__main__":
    sys.exit(main())
