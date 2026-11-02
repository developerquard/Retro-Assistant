"""Voice accuracy harness: say each prompted phrase once, get real numbers.

For each phrase it prints the line to say, records a fixed window (so speaking
early or late cannot break it), keeps the loudest speech segment from that
window, then runs it through the production recogniser (core.voice.Ears) and
scores the wake word and the routed action.

It reports accuracy overall and per language, and the latency from the end of
the phrase to the reply being ready. Test clips are saved to a temporary folder
so results can be re-checked later - delete that folder when you are done.

  python tools/voice_eval.py --set all
  python tools/voice_eval.py --set all --window 7 --no-keep
"""
import argparse
import os
import queue
import sys
import tempfile
import time
import wave

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Hindi prompts need UTF-8 even when stdout is a pipe (Windows defaults to cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass
from core import config  # noqa: E402
from core.router import route  # noqa: E402
from core.voice import (BLOCK, END_SILENCE_S, MAX_PHRASE_S, MIN_PHRASE_S,  # noqa: E402
                        SAMPLE_RATE, START_RMS, Ears, strip_wake)

# All 20 actions, one utterance each: 10 Hinglish then 10 Hindi.
PHRASES = [
    ("hinglish", "Retro, kitne baje hain", "time"),
    ("hinglish", "Retro, aaj ki tarikh kya hai", "date"),
    ("hinglish", "Retro, battery kitni bachi hai", "battery"),
    ("hinglish", "Retro, CPU aur RAM batao", "cpu_ram"),
    ("hinglish", "Retro, internet connect hai", "internet"),
    ("hinglish", "Retro, awaaz badhao", "volume_up"),
    ("hinglish", "Retro, awaaz kam karo", "volume_down"),
    ("hinglish", "Retro, notepad kholo", "open_app"),
    ("hinglish", "Retro, youtube kholo", "open_site"),
    ("hinglish", "Retro, agla gaana chalao", "next_track"),
    ("hindi", "Retro, वॉल्यूम 30 प्रतिशत करो", "set_volume"),
    ("hindi", "Retro, म्यूट कर दो", "toggle_mute"),
    ("hindi", "Retro, वेब पर फैब्रिक सर्च करो", "search_web"),
    ("hindi", "Retro, म्यूजिक रोक दो", "pause"),
    ("hindi", "Retro, म्यूजिक फिर से चलाओ", "resume"),
    ("hindi", "Retro, पिछला गाना चलाओ", "prev_track"),
    ("hindi", "Retro, 2 मिनट का टाइमर लगाओ", "timer_start"),
    ("hindi", "Retro, टाइमर रद्द करो", "timer_cancel"),
    ("hindi", "Retro, नोट बनाओ: कल मिलना है", "create_note"),
    ("hindi", "Retro, रोडमैप फाइल ढूंढो", "find_file"),
]


def capture_window(seconds, device=None):
    """Records a fixed window and returns (loudest_segment, its_end_time, peak)."""
    frames = queue.Queue()
    stream = sd.InputStream(device=device, samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32", blocksize=BLOCK,
                            callback=lambda i, f, t, s: frames.put(i[:, 0].copy()))
    stream.start()
    t_start = time.time()
    time.sleep(seconds)
    stream.stop()
    stream.close()
    blocks = []
    while True:
        try:
            blocks.append(frames.get_nowait())
        except queue.Empty:
            break
    if not blocks:
        return None, None, 0.0

    # Same gating rules as core.voice.listen_once, applied to the whole window.
    segs, buf, in_phrase, last_loud = [], [], False, 0.0
    for i, b in enumerate(blocks):
        t = t_start + i * BLOCK / SAMPLE_RATE
        if float(np.sqrt(np.mean(b.astype(np.float64) ** 2))) > START_RMS:
            if not in_phrase:
                in_phrase, buf = True, []
            last_loud = t
        if not in_phrase:
            continue
        buf.append(b)
        length = len(buf) * BLOCK / SAMPLE_RATE
        if (t - last_loud) < END_SILENCE_S and length < MAX_PHRASE_S:
            continue
        if length >= MIN_PHRASE_S:
            segs.append((np.concatenate(buf).astype(np.float32), last_loud))
        in_phrase, buf = False, []
    if in_phrase and len(buf) * BLOCK / SAMPLE_RATE >= MIN_PHRASE_S:
        segs.append((np.concatenate(buf).astype(np.float32), last_loud))
    if not segs:
        return None, None, 0.0
    audio, end = max(segs, key=lambda s: float(np.mean(s[0].astype(np.float64) ** 2)))
    return audio, end, float(np.max(np.abs(audio)))


def save_wav(path, audio):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=7.0, help="seconds recorded per phrase")
    ap.add_argument("--no-keep", action="store_true", help="do not save the test clips")
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    outdir = os.path.join(tempfile.gettempdir(), "retro_voice_eval")
    os.makedirs(outdir, exist_ok=True)
    print("Say each line exactly as printed. Speak the moment you see it.\n")
    for i, (_lang, say, expected) in enumerate(PHRASES, 1):
        print(f"  {i:2d}. {say}   -> {expected}")
    print(f"\nRecording {args.window:g}s after each prompt. Clips kept in {outdir}\n")

    ears = Ears()
    rows = []
    for i, (lang, say, expected) in enumerate(PHRASES, 1):
        print(f">>> SAY {i}/20:  {say}", flush=True)
        time.sleep(0.4)
        audio, end, peak = capture_window(args.window, args.device)
        if audio is None:
            print("    nothing captured")
            rows.append((lang, say, expected, "", False, None, None))
            continue
        if not args.no_keep:
            save_wav(os.path.join(outdir, f"{i:02d}_{expected}.wav"), audio)
        t0 = time.time()
        text = ears.transcribe(audio)
        transcribe_s = time.time() - t0
        rest = strip_wake(text)
        woke = rest is not None
        got = None
        if rest:
            m = route(rest)
            a = getattr(m, "action", None)
            got = getattr(a, "name", None)
        # The recorded window keeps running after the phrase ends, so timing straight
        # from wall clock would count that idle tail. In production listen_once waits
        # END_SILENCE_S after the last loud block and then transcribes, so the reply
        # is ready after that plus the transcription. TTS start is not included here;
        # tools/voice_test.py measures the full path through listen_once.
        latency = END_SILENCE_S + transcribe_s
        ok = woke and got == expected
        print(f"    heard={text!r}")
        print(f"    wake={woke} action={got} expected={expected} "
              f"{'OK' if ok else 'MISS'}  transcribe={transcribe_s:.2f}s "
              f"peak={peak:.2f} latency={latency:.2f}s")
        rows.append((lang, say, expected, text, woke, got, latency))

    print("\n--- summary ---")
    for lang in ("hinglish", "hindi"):
        part = [r for r in rows if r[0] == lang]
        good = sum(1 for r in part if r[3] and r[4] and r[5] == r[2])
        print(f"  {lang:9s} {good}/{len(part)} handled correctly ({good/len(part)*100:.0f}%)")
    good = sum(1 for r in rows if r[3] and r[4] and r[5] == r[2])
    print(f"  overall   {good}/{len(rows)} ({good/len(rows)*100:.0f}%)   target 90%")
    lats = sorted(r[6] for r in rows if r[6])
    if lats:
        med = lats[len(lats) // 2] if len(lats) % 2 else (lats[len(lats)//2 - 1] + lats[len(lats)//2]) / 2
        print(f"  latency   median={med:.2f}s  min={lats[0]:.2f}s  max={lats[-1]:.2f}s   target <3s")
    print("\n  misses:")
    for r in rows:
        if not (r[3] and r[4] and r[5] == r[2]):
            print(f"    {r[1][:40]:42s} expected={r[2]:12s} got={r[5]} heard={r[3]!r}")


if __name__ == "__main__":
    main()
