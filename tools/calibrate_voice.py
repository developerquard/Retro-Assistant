"""Measure the microphone so START_RMS is set from data instead of a guess.

Phase 1 records the room with nobody talking (background level). Phase 2 waits
until it actually hears you start speaking, then records for a fixed time, so
there is nothing to time by hand.

Only per-block loudness numbers are computed. No audio is written to disk and
no transcript is produced, so nothing private is stored.

  python tools/calibrate_voice.py                 # background, then you speak
  python tools/calibrate_voice.py --tts           # play a synthetic phrase instead
  python tools/calibrate_voice.py --background 6 --speech 8 --device 1

The last line prints the value to use for RETRO_START_RMS.
"""
import argparse
import math
import os
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config  # noqa: E402

SPOKEN_PHRASE = ("Retro, what time is it. Retro, open notepad. "
                 "Retro, set a timer for ten minutes. Retro, search the web.")
ONSET_TIMEOUT_S = 45.0


def _rms(indata):
    return float(np.sqrt(np.mean(indata[:, 0].astype(np.float64) ** 2)))


def record_fixed(seconds, device, label):
    """Records `seconds` and returns the RMS of every 30 ms block."""
    levels = []
    status = []

    def callback(indata, _frames, _time, st):
        if st:
            status.append(str(st))
        levels.append(_rms(indata))

    print(f"  recording {label} for {seconds:g}s ...", flush=True)
    with sd.InputStream(device=device, samplerate=config.SAMPLE_RATE, channels=1,
                        dtype="float32", blocksize=config.BLOCK, callback=callback):
        time.sleep(seconds)
    if status:
        print("  stream status:", "; ".join(sorted(set(status))[:3]))
    return np.array(levels)


def record_from_onset(seconds, device, onset, label):
    """Waits for speech to begin, then records `seconds`. Returns (levels, onset_index)."""
    levels = []
    onset_at = {"index": None}

    def callback(indata, _frames, _time, _st):
        level = _rms(indata)
        levels.append(level)
        if onset_at["index"] is None and level > onset:
            onset_at["index"] = len(levels) - 1

    print(f"  {label}", flush=True)
    with sd.InputStream(device=device, samplerate=config.SAMPLE_RATE, channels=1,
                        dtype="float32", blocksize=config.BLOCK, callback=callback):
        deadline = time.time() + ONSET_TIMEOUT_S
        while onset_at["index"] is None and time.time() < deadline:
            time.sleep(0.2)
        if onset_at["index"] is None:
            print("  heard nothing above the onset level, giving up")
            return np.array(levels), None
        print("  heard you start, keep going ...", flush=True)
        time.sleep(seconds)
    return np.array(levels), onset_at["index"]


def stats(name, a):
    if not len(a):
        print(f"  {name}: no audio captured")
        return None
    p = {q: float(np.percentile(a, q)) for q in (10, 20, 50, 90, 99)}
    print(f"  {name}: median={p[50]:.5f} p90={p[90]:.5f} p99={p[99]:.5f} max={float(a.max()):.5f}")
    return p


def suggest(background, active):
    """Above every background block, but below the quietest real speech."""
    bg_max = float(background.max())
    quiet_speech = float(np.percentile(active, 10))
    if quiet_speech <= bg_max:
        return None, bg_max, quiet_speech
    value = max(4.0 * bg_max, math.sqrt(bg_max * quiet_speech))
    value = min(value, quiet_speech / 2.0)
    return round(value, 5), bg_max, quiet_speech


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--background", type=float, default=6.0, help="seconds of silence")
    ap.add_argument("--speech", type=float, default=8.0, help="seconds of speaking")
    ap.add_argument("--device", default=None, help="input device index or name")
    ap.add_argument("--tts", action="store_true", help="play a synthetic phrase instead of a person")
    args = ap.parse_args()

    print(f"sample rate {config.SAMPLE_RATE} Hz, block {config.BLOCK} (30 ms), device {args.device or 'default'}")
    bg = record_fixed(args.background, args.device, "background (be quiet)")
    stats("background", bg)

    if args.tts:
        holder = {}

        def play():
            subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "tts_say.py"),
                            SPOKEN_PHRASE], check=False)

        print(f"  speaking a synthetic phrase through the speakers ...", flush=True)
        th = threading.Thread(target=play)
        th.start()
        speech = record_fixed(args.speech, args.device, "synthetic speech")
        th.join()
        onset, onset_level = 0, 0.0
    else:
        onset_level = max(float(np.percentile(bg, 99)) * 6.0, 0.004) if len(bg) else 0.004
        print(f"  waiting for speech (onset threshold {onset_level:.5f}).")
        print(f"  say this out loud, at your normal volume:\n    {SPOKEN_PHRASE}", flush=True)
        levels, onset = record_from_onset(args.speech, args.device, onset_level, "listening ...")
        speech = levels if onset is None else levels[onset:]

    stats("speech (whole window)", speech)
    active = speech[speech > onset_level] if len(speech) and onset_level else speech
    if not len(active):
        print("suggested START_RMS: none (no speech blocks above the onset level)")
        return
    stats("speech (active only)", active)

    print(f"\ncurrent START_RMS: {config.START_RMS}")
    value, bg_max, quiet_speech = suggest(bg, active)
    if value is None:
        print("suggested START_RMS: none (background reaches the speech level)")
        return
    print(f"background max={bg_max:.5f}, quietest speech p10={quiet_speech:.5f}")
    print(f"suggested START_RMS: {value}")
    print(f"  background above it: {(bg > value).mean() * 100:.1f}%   "
          f"active speech above it: {(active > value).mean() * 100:.1f}%")
    print(f"\nset it with:  set RETRO_START_RMS={value}")


if __name__ == "__main__":
    main()
