"""What the microphone path really hears, end to end, with nobody in the loop.

    python tools/mic_e2e.py                                   # one line, out loud
    python tools/mic_e2e.py --phrase "open notepad" --phrases 3
    python tools/mic_e2e.py --device 3                        # another input
    python tools/mic_e2e.py --quiet                           # capture only, no playback

`tools/stt_probe.py` proves the recogniser: it hands the endpoint a wav rendered from
text and gets the words back. That leaves the half in front of it - the microphone, the
rate the device really accepts, the resample to 16 kHz, the loudness gate, the phrase
buffer - and that half cannot be checked with a wav, because a wav skips all of it.

So this plays a line out of the speakers and asks the same function the assistant
listens with (`core.voice.listen_once`, the same blocks, the same gate) what it heard.
A line that comes back as the words that were spoken is a working path. A line that
comes back empty, truncated, or as the recogniser's prompt is the failing one - and a
"the assistant cannot hear me" report is answered by which of those it is.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config, log  # noqa: E402
from core.voice import (BLOCK, SAMPLE_RATE, SPEECH_PEAK, START_RMS,  # noqa: E402
                        TARGET_PEAK, Speaker, build_ears, capture_rate,
                        listen_once, strip_wake)


def speak_later(line, delay, done):
    """Plays the line once, after the listener has had time to open the stream."""
    time.sleep(delay)
    try:
        speaker = Speaker()
        speaker.ensure_voices()
        played = speaker.say(line)
        done["played"] = bool(played)
        done["peak"] = float(getattr(speaker, "last_peak", 0.0) or 0.0)
    except Exception as exc:
        done["error"] = f"{type(exc).__name__}: {exc}"


def hear_once(ears, device, seconds=8.0, gate=None, phrases=None):
    """One listening pass. Returns (heard, timing, levels, phrase audio blobs)."""
    levels = []
    timing = {}
    blobs = [] if phrases is None else phrases

    def on_level(level):
        levels.append(level)

    heard = listen_once(ears, timeout_s=seconds, timing=timing, on_level=on_level,
                        gate=gate, device=device, on_phrase=blobs.append)
    return heard, timing, levels, blobs


def examined(ears, blobs):
    """What the recogniser makes of the phrase at its own level, and louder.

    A phrase that is refused is refused for one of two reasons: the decoder did not
    believe it, or its words were never sent because the audio was too quiet to
    normalise. Sending the same audio back at full scale tells the two apart, and
    that is the difference between a microphone that needs its gain fixed and a
    guard that is throwing real speech away.
    """
    import numpy as np

    rows = []
    for audio in blobs:
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        plain = ears.transcribe(audio)
        louder = ""
        if peak > 0.0:
            gain = min(20.0, TARGET_PEAK / peak)
            louder = ears.transcribe((audio * gain).astype("float32"))
        rows.append((len(audio) / float(SAMPLE_RATE), peak, plain, louder))
    return rows


def new_log_lines(mark):
    """Everything written to retro.log since `mark`, minus the noise of a start."""
    path = config.LOG_DIR / log.EVENT_LOG
    try:
        with open(path, encoding="utf-8") as handle:
            handle.seek(mark)
            return [line.strip() for line in handle if line.strip()]
    except OSError:
        return []


def log_mark():
    path = config.LOG_DIR / log.EVENT_LOG
    try:
        return path.stat().st_size
    except OSError:
        return 0


def describe(heard, phrase):
    """The one-word verdict: did the words that were spoken come back?"""
    if not heard:
        return "NOTHING - the phrase was never transcribed"
    wanted = [w for w in phrase.lower().replace(",", " ").replace(".", " ").split()
              if len(w) > 2]
    found = [w for w in wanted if w in heard.lower()]
    if not found:
        return "UNRELATED - none of the spoken words came back"
    if len(found) < len(wanted):
        return f"PARTIAL - {len(found)}/{len(wanted)} of the spoken words came back"
    return "OK - the words that were said came back"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phrase", default="Retro, what time is it?")
    parser.add_argument("--phrases", type=int, default=1)
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds after the stream opens before the line plays")
    parser.add_argument("--device", default=None, help="an input index (default: system)")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--quiet", action="store_true", help="capture only, play nothing")
    parser.add_argument("--gate", type=float, default=None,
                        help="a fixed gate, as the session builds while Retro speaks")
    parser.add_argument("--save", default=None,
                        help="write the captured phrase to this wav, to listen to")
    args = parser.parse_args()

    device = int(args.device) if args.device else None
    rate = capture_rate(device)
    print(f"input    : {device or 'system default'}  (capture rate {rate or 'refused'})")
    print(f"blocks   : {BLOCK} samples, gate floor {START_RMS} rms, ears at {SAMPLE_RATE} Hz")
    ears = build_ears()
    print(f"recogniser: {type(ears).__name__}"
          f"{' (' + ears.model + ')' if getattr(ears, 'model', None) else ''}\n")

    failures = 0
    for index in range(1, max(1, args.phrases) + 1):
        done = {}
        blobs = []
        mark = log_mark()
        if not args.quiet:
            threading.Thread(target=speak_later,
                             args=(args.phrase, args.delay, done), daemon=True).start()
        heard, timing, levels, blobs = hear_once(
            ears, device, args.seconds,
            gate=(lambda: args.gate) if args.gate is not None else None, phrases=blobs)
        # The line the speakers played is itself above the gate, so the levels say
        # whether the phrase was built from the playback, from a person, or from nothing.
        loud = [level for level in levels if level > START_RMS]
        peak = max(levels) if levels else 0.0
        verdict = describe(heard, args.phrase)
        failures += not verdict.startswith("OK")
        print(f"--- pass {index}: said {args.phrase!r}")
        if done:
            print(f"    playback : played={done.get('played')} "
                  f"rendered_peak={done.get('peak', 0.0):.3f}"
                  f"{' ' + done['error'] if done.get('error') else ''}")
        print(f"    captured : {len(levels)} blocks, {len(loud)} above the floor, "
              f"loudest {peak:.4f}, phrase {round(float(timing.get('duration') or 0.0), 2)}s "
              f"at peak {round(float(timing.get('peak') or 0.0), 3)}")
        print(f"    heard    : {heard!r}  (payload {strip_wake(heard)!r})")
        for seconds, peak, plain, louder in examined(ears, blobs):
            print(f"    phrase   : {seconds:.2f}s  peak {peak:.4f}"
                  f"  ({'normalised' if peak >= SPEECH_PEAK else 'left alone'})"
                  f"  -> as-is {plain!r}"
                  f"  -> at full scale {louder!r}")
        if args.save and blobs:
            import wave

            import numpy as np
            joined = np.concatenate(blobs)
            with wave.open(args.save, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes((np.clip(joined, -1, 1) * 32767).astype("<i2").tobytes())
            print(f"    audio    : {args.save}")
        for line in new_log_lines(mark):
            if '"stt' in line or '"listen' in line or '"phrase' in line:
                print(f"    log      : {line}")
        print(f"    verdict  : {verdict}\n")

    if failures:
        print(f"{failures} pass(es) failed - the microphone path above is where it breaks")
        return 1
    print("the microphone path carries the words that were spoken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
