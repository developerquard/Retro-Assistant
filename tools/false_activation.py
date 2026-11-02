"""Count false activations: listen for a while and count wake-word hits only.

Runs the same path main_voice.py uses (capture, then check for the wake word).
It never stores or prints a transcript and never writes audio: only counters and
timestamps. Talk, watch videos, play music - whatever you would normally do.

  python tools/false_activation.py --minutes 30

Progress is printed every 60 seconds so the run can be watched from a log.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.voice import Ears, listen_once, strip_wake  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    print(f"false-activation run: {args.minutes:g} minutes. No transcripts are stored.",
          flush=True)
    ears = Ears()
    started = time.time()
    deadline = started + args.minutes * 60
    phrases = 0
    wakes = 0
    woken_at = []
    last_report = started

    while time.time() < deadline:
        try:
            heard = listen_once(ears, timeout_s=15)
        except Exception as exc:                      # never let one bad block end the run
            print(f"  [warn] {type(exc).__name__}", flush=True)
            time.sleep(1)
            continue
        if heard.strip():
            phrases += 1
            if strip_wake(heard) is not None:
                wakes += 1
                woken_at.append(round(time.time() - started, 1))
                print(f"  WAKE at t+{woken_at[-1]:.0f}s  (count so far {wakes})", flush=True)
        now = time.time()
        if now - last_report >= 60:
            mins = (now - started) / 60
            print(f"  t+{mins:4.1f} min  phrases captured={phrases}  false wakes={wakes}",
                  flush=True)
            last_report = now

    total = (time.time() - started) / 60
    print(f"\n--- false-activation summary ---")
    print(f"duration       {total:.1f} minutes")
    print(f"phrases heard  {phrases}")
    print(f"false wakes    {wakes}   (target: 0)")
    print(f"wake times     {woken_at}")


if __name__ == "__main__":
    main()
