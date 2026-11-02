"""Times the first word of an answer from every model the key can call.

This is where config.MEASURED_FIRST_WORD_S comes from, and it is the check to run
after the numbers there are touched or after Groq changes what the key can see.

The measurement is deliberately the same shape as the real call in core/llm.py: a
streamed chat completion, a spoken-length question, and the time to the first piece
of `delta.content`. It is the only latency a voice turn shows - the total length
hardly matters, because Retro speaks the first sentence while the rest of the answer
is still being written. A thinking model is measured with config.reasoning_controls,
because a request sent without them is not the request Retro sends.

Usage:
    python -m tools.model_probe                  # every chat model the key can call
    python -m tools.model_probe qwen/qwen3.8-27b openai/gpt-oss-20b
    python -m tools.model_probe --runs 7 --json
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config                                       # noqa: E402

# The question a measurement uses. Short, answerable by every model on the key, and
# about the length of a spoken request rather than a written one.
QUESTION = "What is the capital of France?"
TOKENS = 80

# The console this is usually run from is cp1252, and a model's answer can contain
# anything at all. Replacing what cannot be printed beats dying after five runs with
# the measurement still unprinted.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def printable(text):
    """The start of the answer, safe to put in a console's own encoding."""
    return text[:38].encode("ascii", "replace").decode("ascii")


def first_word_s(model_id, timeout=None, question=QUESTION):
    """Seconds to the first word of the answer, or None when nothing was written.

    Raises nothing: a model that fails is data too, and it is reported as "no
    answer" rather than as a traceback in the middle of a table.
    """
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": question}],
               "max_completion_tokens": TOKENS,
               "temperature": 0.2,
               "stream": True}
    payload.update(config.reasoning_controls(model_id))
    request = urllib.request.Request(
        config.CLOUD_BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.cloud_key() or ''}",
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": config.USER_AGENT},
        method="POST")
    began = time.monotonic()
    first = None
    text = ""
    with urllib.request.urlopen(request, timeout=timeout or config.CLOUD_TIMEOUT_S) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                delta = json.loads(body)["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if piece and first is None:
                first = time.monotonic() - began
            text += piece
    return first, time.monotonic() - began, text.strip()


def measure(model_id, runs=5, warm=True, timeout=None):
    """(median first word, worst first word, median total, sample) for one model.

    The connection is opened once before the timed runs, because neither the machine
    nor the provider is warm on the first call and charging that to the model is how
    a 0.3 s model gets written down as a 6 s one.
    """
    if warm:
        try:
            first_word_s(model_id, timeout=timeout)
        except Exception:
            pass
    firsts, totals, sample, failure = [], [], "", None
    for _ in range(runs):
        try:
            first, total, text = first_word_s(model_id, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
            break
        totals.append(total)
        if first is not None:
            firsts.append(first)
        sample = text or sample
    if failure or not firsts:
        return None, None, (statistics.median(totals) if totals else None), failure or "no text"
    return statistics.median(firsts), max(firsts), statistics.median(totals), sample


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("models", nargs="*", help="model ids; default is every chat model")
    parser.add_argument("--runs", type=int, default=5, help="timed runs per model (5)")
    parser.add_argument("--timeout", type=float, default=None, help="per-request seconds")
    parser.add_argument("--json", action="store_true", help="print the table as JSON")
    args = parser.parse_args(argv)

    if not config.cloud_available():
        print("no API key: set GROQ_API_KEY, or paste one into the window")
        return 1
    listing, error = config.refresh_model_ids()
    if error:
        print(f"could not read the model listing: {error}")
    models = args.models or [m for m in listing if config.is_chat_model(m)]
    if not models:
        print("no chat models to measure")
        return 1

    rows = []
    for model_id in models:
        median, worst, total, sample = measure(model_id, runs=args.runs, timeout=args.timeout)
        rows.append({"model": model_id,
                     "first_word_s": None if median is None else round(median, 2),
                     "worst_s": None if worst is None else round(worst, 2),
                     "total_s": None if total is None else round(total, 2),
                     "answer": sample})
        if args.json:
            continue
        speed = "no answer" if median is None else f"{median:.2f} s"
        print(f"{model_id:34s} first word {speed:>10s}   total "
              f"{(f'{total:.2f} s' if total else '-'):>8s}   {printable(sample)!r}")

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print()
    print("config.MEASURED_FIRST_WORD_S from this run:")
    for row in sorted(rows, key=lambda r: (r["first_word_s"] is None, r["first_word_s"] or 0)):
        print(f"    {json.dumps(row['model'])}: {row['first_word_s']},")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
