"""Show how Retro chops speech into phrases, so segmentation problems are visible.

Captures continuously using the same gate as core.voice.listen_once. For every
phrase it decides to transcribe it prints the duration, a loudness envelope and
the transcript. This makes two different faults obvious: the phrase being cut
short (envelope stops while you are still talking) versus Whisper mishearing a
phrase that was captured correctly.

No audio is written to disk and transcripts are only printed here.

  python tools/voice_debug.py --phrases 5
"""
import argparse
import os
import queue
import sys
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config  # noqa: E402
from core.voice import (BLOCK, END_SILENCE_S, MAX_PHRASE_S, MIN_PHRASE_S,  # noqa: E402
                        SAMPLE_RATE, START_RMS, Ears)


def envelope(blocks, buckets=12):
    if not len(blocks):
        return ""
    arr = np.asarray(blocks, dtype=np.float64)
    idx = np.linspace(0, len(arr), buckets + 1).astype(int)
    out = []
    for i in range(buckets):
        part = arr[idx[i]:idx[i + 1]]
        out.append(float(np.sqrt(np.mean(part ** 2))) if len(part) else 0.0)
    return " ".join(f"{v:.3f}" for v in out)


def phrases(ears, count, device=None, quiet=False):
    """Yields (duration, envelope, transcript) for each captured phrase."""
    frames = queue.Queue()
    sd.InputStream(device=device, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                   blocksize=BLOCK, callback=lambda i, f, t, s: frames.put(i[:, 0].copy())).start()
    buf, in_phrase, last_loud = [], False, 0.0
    made = 0
    print(f"START_RMS={START_RMS}  END_SILENCE={END_SILENCE_S}s  "
          f"MAX_PHRASE={MAX_PHRASE_S}s  MIN_PHRASE={MIN_PHRASE_S}s", flush=True)
    while made < count:
        try:
            block = frames.get(timeout=0.5)
        except queue.Empty:
            continue
        now = time.time()
        level = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        if not quiet:
            print(f"\r  {level:.4f} " + ("#" * min(40, int(level * 400))), end="", flush=True)
        if level > START_RMS:
            if not in_phrase:
                in_phrase, buf = True, []
            last_loud = now
        if not in_phrase:
            continue
        buf.append(block)
        length = sum(len(b) for b in buf) / SAMPLE_RATE
        if (now - last_loud) < END_SILENCE_S and length < MAX_PHRASE_S:
            continue
        made += 1
        if length >= MIN_PHRASE_S:
            audio = np.concatenate(buf).astype(np.float32)
            t0 = time.time()
            text = ears.transcribe(audio)
            print(f"\n[{made}] duration={length:.2f}s transcribe={time.time()-t0:.2f}s")
            print(f"     envelope: {envelope(buf)}")
            print(f"     heard   : {text!r}")
        else:
            print(f"\n[{made}] dropped, too short ({length:.2f}s)")
        in_phrase, buf = False, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phrases", type=int, default=5)
    ap.add_argument("--device", default=None)
    ap.add_argument("--quiet", action="store_true", help="hide the live level bar")
    args = ap.parse_args()
    print("loading speech model ...", flush=True)
    ears = Ears()
    print(f"say {args.phrases} phrases, e.g. 'Retro, what time is it'.\n", flush=True)
    phrases(ears, args.phrases, args.device, args.quiet)


if __name__ == "__main__":
    main()
