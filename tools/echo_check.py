"""How loud is Retro's own voice at the microphone, and how loud is the room?

    python tools/echo_check.py               # silence, then one spoken line, then you
    python tools/echo_check.py --seconds 8   # a longer line to measure the echo over

Prints raw microphone RMS for three states, on the same microphone path the app
listens on (16 kHz, 1024-sample blocks):

    room   - nobody talking, nothing playing
    echo   - the speakers playing a line of Retro's own voice
    voice  - the room with the line off, so you can say something and see it

The echo number is the one that matters. `core/session.py` refuses any microphone
block quieter than the gate while Retro is talking, so the gate has to sit *above*
the echo and *below* a person. If those two numbers are not separated on this
machine, no threshold can tell them apart, and the honest answer is to close the
microphone while the voice plays - so this prints that verdict too, in numbers.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                                   # noqa: E402

from core import config                                              # noqa: E402
from core.voice import BLOCK, SAMPLE_RATE, Speaker                   # noqa: E402

LINE = ("This is Retro speaking on the voice you chose. If you can hear this line, "
        "the echo is what the microphone is hearing right now.")


def _stats(values):
    if not values:
        return {}
    array = np.array(values, dtype="float64")
    return {"p50": float(np.percentile(array, 50)),
            "p90": float(np.percentile(array, 90)),
            "p99": float(np.percentile(array, 99)),
            "max": float(array.max())}


def record(seconds, on_block=None, stop=None):
    """Records the microphone for `seconds` and returns the raw RMS of every block.

    The same blocksize and rate `listen_once` uses, and the same default input, so a
    number from here is the number the app sees. `on_block` is called with each RMS.
    """
    import sounddevice as sd

    levels = []
    done = threading.Event()

    def on_audio(indata, _frames, _time, _status):
        level = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
        levels.append(level)
        if on_block is not None:
            on_block(level)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=on_audio):
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.02)
            if stop is not None and stop.is_set():
                break
    done.set()
    return levels


def measure_echo(seconds, speaker, line):
    """Plays `line` and records what the microphone hears while it plays."""
    played = []
    finished = threading.Event()

    def play():
        try:
            speaker.say(line * max(1, int(round(seconds / 8.0))))
        finally:
            finished.set()

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    # Wait for the voice to actually start before believing a quiet block.
    started = time.time()
    while time.time() - started < 3.0 and not getattr(speaker, "playing", False):
        time.sleep(0.01)
    echo = record(seconds, on_block=lambda _l: played.append(float(getattr(speaker, "level", 0.0))))
    finished.wait(timeout=30)
    return echo, played


def describe(stats):
    if not stats:
        return "no blocks"
    return (f"p50 {stats['p50']:.5f}  p90 {stats['p90']:.5f}  "
            f"p99 {stats['p99']:.5f}  max {stats['max']:.5f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=6.0,
                        help="how long the spoken line runs (and so how long the echo is measured)")
    parser.add_argument("--no-voice", action="store_true", help="skip the spoken line")
    args = parser.parse_args()

    speaker = Speaker()
    try:
        speaker.ensure_voices()
    except Exception as exc:
        print(f"no voice available ({type(exc).__name__}: {exc})")

    print(f"microphone path : {SAMPLE_RATE} Hz, block {BLOCK}, START_RMS {config.START_RMS}")
    print(f"settings        : output={_setting('output')!r} voice={_setting('voice')!r}")

    print("\n--- room: say nothing for 2 s")
    room = record(2.0)
    print(f"    {describe(_stats(room))}")

    echo_stats = played_stats = {}
    if not args.no_voice:
        print(f"\n--- echo: playing a line for {args.seconds:.0f} s - stay quiet")
        echo, played = measure_echo(args.seconds, speaker, LINE)
        echo_stats, played_stats = _stats(echo), _stats(played)
        print(f"    mic    raw  {describe(echo_stats)}")
        if played_stats:
            print(f"    played 0..1 {describe(played_stats)}")

    print("\n--- voice: the line is over - say \"Retro, open notepad\" now (4 s)")
    voice = record(4.0)
    voice_stats = _stats(voice)
    print(f"    {describe(voice_stats)}")

    print("\n--- verdict")
    if not echo_stats:
        print("    no echo measured (no spoken line), so nothing to compare")
        return 0
    echo_high = echo_stats["p99"]
    voice_low = voice_stats.get("p50", 0.0) if voice_stats else 0.0
    print(f"    echo p99 {echo_high:.5f} against START_RMS {config.START_RMS:.5f} "
          f"({echo_high / max(1e-9, config.START_RMS):.1f}x)")
    if voice_stats:
        print(f"    your own speech p50 {voice_low:.5f} "
              f"({voice_low / max(1e-9, config.START_RMS):.1f}x START_RMS)")
        if voice_low > echo_high * 1.2:
            gate = echo_high * 1.5
            print(f"    separated: a gate at {gate:.5f} refuses the echo and still hears you "
                  f"(RETRO_PLAY_GATE={gate / max(1e-9, echo_high):.2f} x echo)")
        else:
            print("    NOT separated: your voice and Retro's echo are within 20% of each "
                  "other at this microphone, so no loudness threshold can tell them apart.")
            print("    The microphone has to be closed while the voice plays - see "
                  "PLAY_GATE / `_mic_gate` in core/session.py.")
    else:
        print("    nothing was said after the line, so the voice side was not measured")
    return 0


def _setting(name):
    try:
        from core import settings
        return settings.get(name)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
