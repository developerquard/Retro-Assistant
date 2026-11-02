"""Live microphone level meter, so you can see whether Retro hears you.

Prints a moving bar of the current loudness and, at the end, reports how loud
the room was versus how loud you were. Nothing is written to disk and no
transcript is produced.

  python tools/mic_check.py            # 30 seconds
  python tools/mic_check.py 60         # 60 seconds
  python tools/mic_check.py 60 2       # pick input device index 2
"""
import os
import sys
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config  # noqa: E402


def bar(level, width=28):
    filled = int(min(1.0, level / 0.05) * width)
    return "|" + "#" * filled + "." * (width - filled) + "|"


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    device = int(sys.argv[2]) if len(sys.argv) > 2 else None
    levels = []
    speech_blocks = [0]

    def callback(indata, _frames, _time, _status):
        rms = float(np.sqrt(np.mean(indata[:, 0].astype(np.float64) ** 2)))
        levels.append(rms)
        if rms > config.START_RMS:
            speech_blocks[0] += 1

    print(f"Listening for {seconds:g}s on device {device or 'default'} "
          f"at {config.SAMPLE_RATE} Hz.")
    print(f"A block counts as speech above START_RMS={config.START_RMS}.")
    print("Speak normally, about an arm's length from the laptop.\n")
    shown = 0
    with sd.InputStream(device=device, samplerate=config.SAMPLE_RATE, channels=1,
                        dtype="float32", blocksize=config.BLOCK, callback=callback):
        start = time.time()
        while time.time() - start < seconds:
            time.sleep(0.4)
            if len(levels) > shown:
                shown = len(levels)
                cur = max(levels[-5:])
                tag = "  <-- heard" if cur > config.START_RMS else ""
                print(f"  {bar(cur)} {cur:.5f}{tag}")
    a = np.array(levels) if levels else np.array([0.0])
    print(f"\nblocks={len(a)}")
    print(f"quietest 50%: median={np.median(a):.5f}")
    print(f"loudest 10% : p90={np.percentile(a, 90):.5f}   max={a.max():.5f}")
    print(f"blocks above START_RMS ({config.START_RMS}): {speech_blocks[0]} of {len(a)}")
    if a.max() < config.START_RMS:
        print("=> Retro would hear NOTHING at this threshold.")
    else:
        print("=> Retro can hear you at this threshold.")


if __name__ == "__main__":
    main()
