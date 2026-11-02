"""Measure one stage of Retro's pipeline, or read the history it has written.

The window's diagnostics panel is the everyday view. This is the developer's view,
for the times a full voice interaction is the wrong tool: a WAV that transcribes
badly, a line of text that comes out late, a model that answers slowly.

    python tools/latency.py report                 # the persisted history
    python tools/latency.py report --file x.json   # a file written by export
    python tools/latency.py export --json out.json
    python tools/latency.py export --csv out.csv
    python tools/latency.py stt recording.wav
    python tools/latency.py tts "This is Retro checking the voice."
    python tools/latency.py llm "say hello in five words"

Every number printed is measured on this machine with the same clock the pipeline
uses (`time.perf_counter_ns`). Nothing here invents a stage, and a component that
cannot be loaded is reported as a failure rather than as a zero-millisecond pass.
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, diag  # noqa: E402


def _history(args):
    if getattr(args, "file", None):
        try:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"could not read {args.file}: {type(exc).__name__}")
            return []
        if isinstance(data, dict):
            return list(data.get("traces") or [])
        return list(data) if isinstance(data, list) else []
    return diag.history(limit=getattr(args, "limit", 20))


def _report(args):
    items = _history(args)
    print(diag.report(limit=getattr(args, "limit", 20), items=items))
    return 0 if items else 1


def _export(args):
    items = _history(args)
    if not items:
        print("no history to export")
        return 1
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "unavailable": diag.UNAVAILABLE,
                                      "traces": items}, indent=2, sort_keys=True),
                          encoding="utf-8")
        print(f"wrote {target}")
    if args.csv:
        import csv
        target = Path(args.csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        names = list(diag.STAGE_ORDER)
        fields = (["id", "at", "kind", "origin", "ok", "total_ms",
                   "time_to_first_text_ms", "time_to_first_audio_ms", "playback_ms",
                   "model", "streaming", "retries", "timeouts"]
                  + [f"{name}_ms" for name in names])
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in items:
                row = {key: item.get(key) for key in fields if key in item}
                for key in ("streaming",):
                    if row.get(key) is None:
                        row[key] = ""
                for name in names:
                    stage = (item.get("stages") or {}).get(name)
                    row[f"{name}_ms"] = "" if stage is None else stage.get("ms")
                writer.writerow(row)
        print(f"wrote {target}")
    return 0


def _stt(args):
    """Speech to text only: the same ears the session builds, on a file."""
    from core import voice

    rate, audio = voice.read_wav_mono(args.wav)
    if rate != voice.SAMPLE_RATE:
        audio = voice.resample(audio, rate, voice.SAMPLE_RATE)
    trace = diag.new(kind="probe", origin="stt", label="stt probe")
    ears = voice.build_ears()
    started = diag.now_ns()
    try:
        text = ears.transcribe(audio)
    except Exception as exc:
        if trace is not None:
            trace.error("stt", exc)
            trace.finish(ok=False)
            diag.record(trace)
        print(f"stt failed: {type(exc).__name__}: {exc}")
        return 1
    if trace is not None:
        trace.add("stt", diag.now_ns() - started)
        trace.meta["chars"] = len(text or "")
        trace.model = getattr(ears, "model", "whisper")
        trace.finish(ok=True)
        diag.record(trace)
    print(f"transcript ({len(text or '')} chars): {text!r}")
    print("")
    print(diag.report(limit=1, items=[trace.data()] if trace else []))
    return 0


def _tts(args):
    """Text to speech only: real synthesis, real playback, one trace."""
    from core.session import local_voice_only
    from core.tts import VoiceStream
    from core.voice import Speaker

    trace = diag.new(kind="probe", origin="tts", label="tts probe")
    speaker = Speaker()
    speaker.ensure_voices()
    stream = VoiceStream(speaker=speaker,
                         allow_cloud=config.cloud_available() and not local_voice_only(),
                         trace=trace)
    stream.feed(args.text)
    finished = stream.finish(timeout=args.timeout)
    if trace is not None:
        trace.mark("play_end")
        trace.finish(ok=bool(finished))
        diag.record(trace)
    print("")
    print(diag.report(limit=1, items=[trace.data()] if trace else []))
    if not finished:
        print(f"playback did not finish within {args.timeout}s")
        return 1
    return 0


def _llm(args):
    """The model only: time to first token and total, with the configured brain."""
    from core import settings
    from core.llm import build_brain

    choice = settings.get("model") or config.MODEL_CHOICE
    brain, cloud = build_brain(choice)
    messages = [{"role": "user", "content": args.prompt}]
    trace = diag.new(kind="probe", origin="llm", label="llm probe")
    if trace is not None:
        trace.model = getattr(brain, "model", None) or (
            getattr(getattr(brain, "model_path", None), "name", "local"))

    def on_delta(_piece):
        diag.mark("first_text", trace)

    started = diag.now_ns()
    try:
        if args.no_stream or not hasattr(brain, "stream"):
            text = brain.ask(messages, args.max_tokens)
            diag.mark("first_text", trace)
            if trace is not None:
                trace.streaming = False
        else:
            if trace is not None:
                trace.streaming = True
            text = brain.stream(messages, args.max_tokens, on_delta)
    except Exception as exc:
        if trace is not None:
            trace.error("llm", exc)
            trace.finish(ok=False)
            diag.record(trace)
        print(f"model failed: {type(exc).__name__}: {exc}")
        return 1
    if trace is not None:
        trace.add("llm", diag.now_ns() - started)
        trace.finish(ok=True)
        diag.record(trace)
    print(f"{'cloud' if cloud else 'local'} reply ({len(text or '')} chars): {text!r}")
    print("")
    print(diag.report(limit=1, items=[trace.data()] if trace else []))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="show the measured history")
    report.add_argument("--limit", type=int, default=20)
    report.add_argument("--file", help="a JSON file written by `export`")
    report.set_defaults(run=_report)

    export = sub.add_parser("export", help="write the history as JSON and/or CSV")
    export.add_argument("--json", help="path for the JSON export")
    export.add_argument("--csv", help="path for the CSV export")
    export.add_argument("--limit", type=int, default=200)
    export.add_argument("--file", help="export this JSON file instead of the history")
    export.set_defaults(run=_export)

    stt = sub.add_parser("stt", help="transcribe one WAV and report its timing")
    stt.add_argument("wav")
    stt.set_defaults(run=_stt)

    tts = sub.add_parser("tts", help="speak one line and report its timing")
    tts.add_argument("text")
    tts.add_argument("--timeout", type=float, default=40.0)
    tts.set_defaults(run=_tts)

    llm = sub.add_parser("llm", help="ask the configured model and report its timing")
    llm.add_argument("prompt")
    llm.add_argument("--max-tokens", type=int, default=220)
    llm.add_argument("--no-stream", action="store_true")
    llm.set_defaults(run=_llm)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
