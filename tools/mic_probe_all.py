"""Capture from every input device at once and report which one hears you.

Used when the default microphone appears deaf: it opens each input device in
parallel for one window, so a single speaking test shows which device (if any)
is actually picking up the speaker. Nothing is written to disk.

  python tools/mic_probe_all.py 25
"""
import os
import sys
import threading
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config  # noqa: E402


def candidate_inputs():
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"][:52], d["max_input_channels"]))
    return out


def probe(idx, seconds, results):
    levels = []
    err = None

    def cb(indata, _f, _t, _s):
        levels.append(float(np.sqrt(np.mean(indata[:, 0].astype(np.float64) ** 2))))

    try:
        with sd.InputStream(device=idx, samplerate=config.SAMPLE_RATE, channels=1,
                            dtype="float32", blocksize=config.BLOCK, callback=cb):
            time.sleep(seconds)
    except Exception as exc:
        err = f"{type(exc).__name__}"
    results[idx] = (levels, err)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    devices = candidate_inputs()
    print(f"Opening {len(devices)} input devices for {seconds:g}s.")
    print("Speak continuously and a bit louder than normal: "
          "'Retro, what time is it, Retro, open notepad' over and over.\n")
    results = {}
    threads = [threading.Thread(target=probe, args=(i, seconds, results)) for i, _, _ in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"{'idx':>3}  {'median':>9} {'p90':>9} {'max':>9}  name")
    ranked = []
    for i, name, _ch in devices:
        levels, err = results.get(i, ([], "not started"))
        if err:
            print(f"{i:>3}  {'-':>9} {'-':>9} {'-':>9}  {name}  ({err})")
            continue
        a = np.array(levels) if levels else np.array([0.0])
        med, p90, mx = float(np.median(a)), float(np.percentile(a, 90)), float(a.max())
        ranked.append((mx, i, name))
        print(f"{i:>3}  {med:>9.5f} {p90:>9.5f} {mx:>9.5f}  {name}")
    print(f"\nSTART_RMS now = {config.START_RMS}")
    if ranked:
        ranked.sort(reverse=True)
        mx, i, name = ranked[0]
        print(f"loudest device: {i} ({name}) max={mx:.5f}")
        if mx < config.START_RMS:
            print("=> NO device reached START_RMS. The microphone path is not delivering audio.")


if __name__ == "__main__":
    main()
