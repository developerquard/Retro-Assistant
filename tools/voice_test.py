"""End-to-end voice test: wake word -> transcript -> action, with real numbers.

Say "Retro, <command>" in one breath. The harness prints what it heard so you
can judge accuracy; that transcript is shown on screen only and is never
written to the action log or to disk.

It reports, per sample: the transcript, the wake word split, the routed action,
and the latency from the end of your phrase to the start of the reply. At the
end it prints model load time and process RAM before/after loading.

  python tools/voice_test.py --samples 10
  python tools/voice_test.py --samples 10 --speak      # also speak the replies
"""
import argparse
import os
import sys
import time

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config  # noqa: E402
from core.llm import Fallback  # noqa: E402
from core.runner import handle  # noqa: E402
from core.router import route  # noqa: E402
from core.voice import END_SILENCE_S, Ears, listen_once, strip_wake  # noqa: E402


def rss_mb():
    return psutil.Process().memory_info().rss / 2 ** 20


def routed_name(rest):
    """The action the typed rules pick, or None when only the model could help."""
    m = route(rest)
    if m is None:
        return None
    if hasattr(m, "action"):
        return m.action.name
    return "ambiguous:" + ",".join(m.names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--speak", action="store_true", help="speak the reply (measures spoken latency)")
    ap.add_argument("--no-model", action="store_true", help="skip the Llama fallback")
    args = ap.parse_args()

    print(f"START_RMS={config.START_RMS}  whisper={config.WHISPER_MODEL}")
    before = rss_mb()
    print(f"process RAM before loading the speech model: {before:.0f} MB")

    t0 = time.time()
    print("loading the speech model (first run downloads it) ...", flush=True)
    ears = Ears()
    load_s = time.time() - t0
    after = rss_mb()
    print(f"model load time: {load_s:.1f}s")
    print(f"process RAM after loading: {after:.0f} MB  (+{after - before:.0f} MB)")

    speaker = None
    if args.speak:
        from core.voice import Speaker
        speaker = Speaker()

    fallback = None if args.no_model else Fallback()

    print(f"\nSay \"Retro, <command>\" in one breath. {args.samples} samples.\n")
    latencies, rows = [], []
    for i in range(1, args.samples + 1):
        print(f"[{i}/{args.samples}] listening ...", flush=True)
        timing = {}
        heard = listen_once(ears, timeout_s=20, timing=timing)
        t_return = time.time()
        # When last_loud is known, measure from the true end of the phrase, so the
        # 0.6 s silence wait and the transcription are both included.
        phrase_end = timing.get("phrase_end", t_return - END_SILENCE_S)
        rest = strip_wake(heard)
        if rest is None:
            print(f"    heard {heard!r} - NO wake word, ignored")
            rows.append((i, heard, None, None, None))
            continue
        res = handle(rest, fallback=fallback) if rest else None
        t_reply = time.time()          # the reply is ready: this is when speech starts
        if speaker is not None and res is not None:
            speaker.say(res.message)   # blocks until the sentence finishes
        latency = t_reply - phrase_end
        latencies.append(latency)
        print(f"    heard   {heard!r}")
        print(f"    command {rest!r}")
        print(f"    action  {routed_name(rest)} -> {res.message if res else 'greeting only'}")
        print(f"    latency {latency:.2f}s   (rss {rss_mb():.0f} MB)")
        rows.append((i, heard, rest, res, latency))

    woken = sum(1 for row in rows if row[2] is not None)
    handled = sum(1 for row in rows if row[3] is not None and row[3].ok)
    print(f"\n--- summary ---")
    print(f"samples={args.samples}  wake word detected={woken}  actions ok={handled}")
    if latencies:
        lat = sorted(latencies)
        med = lat[len(lat) // 2] if len(lat) % 2 else (lat[len(lat) // 2 - 1] + lat[len(lat) // 2]) / 2
        print(f"latency: median={med:.2f}s  min={min(lat):.2f}s  max={max(lat):.2f}s")
        print(f"  (target: median under 3s)")
    print(f"process RAM at end: {rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
