"""Which input, at which rate, actually hears the speakers - one row per attempt.

    python tools/mic_scan.py                # every input Retro could open, 16k and its own
    python tools/mic_scan.py --device 15    # one device, every rate it accepts

A microphone that opens without an error and then delivers near-silence is the worst
failure this app has, because every symptom of it looks like something else: the loop
waits, nothing is transcribed, no line is written to the log, and the machine looks as
though it is simply ignoring the owner. `tools/mic_check.py` measures one device at the
rate the help says, `tools/mic_probe_all.py` measures all of them at one rate - this
walks the matrix and plays a line itself, so a row that stays at 0.0000 while the
speakers are at 0.7 is a combination that cannot hear anybody.

The rate half matters on this machine specifically: its microphone array runs at
48 000 Hz, and the Windows sound emulation, asked for 16 000, can accept the request
and then hand back a stream that never carries a sample of the room.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from core.voice import BLOCK, SAMPLE_RATE, START_RMS, Speaker  # noqa: E402

LINE = "Retro, what time is it?"


def inputs():
    """Every device sounddevice offers as an input, in its own order."""
    found = []
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) > 0:
            found.append((index, info["name"], float(info.get("default_samplerate") or 0)))
    return found


def rates_for(index, native):
    """The rates to try: the app's own 16 kHz first, then the device's own."""
    wanted = [SAMPLE_RATE]
    if native and int(native) not in wanted:
        wanted.append(int(native))
    accepted = []
    for rate in wanted:
        try:
            sd.check_input_settings(device=index, samplerate=rate, channels=1,
                                    dtype="float32")
            accepted.append(rate)
        except Exception as exc:
            accepted.append((rate, type(exc).__name__))
    return accepted


def measure(index, rate, seconds=4.0):
    """Captures for a window while a line plays. Returns (loudest block, statuses)."""
    levels, statuses = [], set()

    def callback(indata, _frames, _time, status):
        if str(status):
            statuses.add(str(status))
        levels.append(float(np.max(np.abs(indata[:, 0]))))

    played = {}
    thread = threading.Thread(target=lambda: played.update(play(1.2)), daemon=True)
    thread.start()
    try:
        with sd.InputStream(device=index, samplerate=rate, channels=1, dtype="float32",
                            blocksize=BLOCK, callback=callback):
            time.sleep(seconds)
    except Exception as exc:
        return None, {f"{type(exc).__name__}: {exc}"[:60]}
    thread.join(timeout=6.0)
    loud = max(levels) if levels else 0.0
    return loud, statuses | ({f"played={played.get('played')} "
                             f"peak={played.get('peak', 0.0):.3f}"} if played else set())


def play(delay):
    """One line out of the speakers, reported so a row can be trusted as a test."""
    time.sleep(delay)
    try:
        speaker = Speaker()
        speaker.ensure_voices()
        played = speaker.say(LINE)
        return {"played": bool(played), "peak": float(speaker.last_peak or 0.0)}
    except Exception as exc:
        return {"played": f"{type(exc).__name__}", "peak": 0.0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=None, help="one input index only")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--rate", type=int, default=None, help="one rate only")
    args = parser.parse_args()

    default = sd.default.device[0]
    rows = []
    for index, name, native in inputs():
        if args.device is not None and index != args.device:
            continue
        for entry in rates_for(index, native):
            if isinstance(entry, tuple):
                rate, why = entry
                rows.append((index, name, rate, None, {f"refused: {why}"}))
                continue
            if args.rate is not None and entry != args.rate:
                continue
            loud, notes = measure(index, entry, args.seconds)
            rows.append((index, name, entry, loud, notes))

    print(f"default input is device {default}; the line plays at ~0.69 rendered peak\n")
    print(f"{'dev':>3} {'rate':>6} {'native':>6} {'loudest':>9}  notes")
    for index, name, rate, loud, notes in rows:
        native = next((n for i, _n, n in inputs() if i == index), 0)
        mark = "  <-- HEARS" if (loud or 0.0) > START_RMS * 3 else ""
        text = "no stream" if loud is None else f"{loud:.4f}"
        print(f"{index:>3} {rate:>6} {int(native):>6} {text:>9}  "
              f"{name[:28]} {' '.join(sorted(notes))[:44]}{mark}")

    heard = [row for row in rows if (row[3] or 0.0) > START_RMS * 3]
    print()
    if not heard:
        print("not one combination captured the line - check the microphone's own")
        print("Windows privacy setting and its level, then run this again")
        return 1
    print("combinations that captured the line:")
    for index, name, rate, loud, _notes in heard:
        print(f"  device {index} at {rate} Hz  ({loud:.4f})  {name[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
