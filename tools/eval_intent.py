"""M4: score the intent set through the rules first, then the local model.

Every utterance goes through route() first, exactly as the live assistant does.
Only the ones the rules can't read reach the model, so the model is measured on
the work it is actually asked to do.

It reports what the gate asks for: JSON validity, intent accuracy overall and
per language, rejected bad arguments, wrong sensitive actions, model load time,
RAM before and after loading, and average response time.

Nothing here executes a command: it only decides what Retro would run.

Two passes are reported. The hybrid pass is what the assistant actually does
(rules first, model only for the rest). Because the rules read most lines, that
pass calls the model only a handful of times - too small a sample to judge JSON
validity - so --all also sends every utterance through the model alone.

  python tools/eval_intent.py            # hybrid pass
  python tools/eval_intent.py --all      # + model-only pass over all rows
  python tools/eval_intent.py --limit 20 # quick pass
  python tools/eval_intent.py --no-model # rules only, no model load
"""
import argparse
import csv
import json
import os
import sys
import time

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from core.actions import ACTIONS                                    # noqa: E402
from core import guard                                              # noqa: E402
from core.llm import Fallback, propose_detailed                     # noqa: E402
from core.router import Match, route                                # noqa: E402

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "intent_set.csv")
TIERS = {a.name: a.tier for a in ACTIONS}


def rss_mb():
    return psutil.Process().memory_info().rss / 2 ** 20


def args_match(expected, got):
    """Exact match for named args; *_contains for free text like a query or note."""
    for key, want in expected.items():
        if key.endswith("_contains"):
            produced = str(got.get(key[: -len("_contains")], "")).lower()
            if str(want).lower() not in produced:
                return False
        elif got.get(key) != want:
            return False
    return True


def load_rows(path, limit=None):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


def is_correct(row, action, args):
    expected = row["action"]
    if expected == "":
        return action is None
    return action == expected and args_match(json.loads(row["args"] or "{}"), args)


class Recorder:
    """Wraps the model callable so the raw reply can be shown for every miss."""

    def __init__(self, complete):
        self.complete = complete
        self.last = None

    def __call__(self, messages):
        self.last = self.complete(messages)
        return self.last


def evaluate(rows, complete, use_model=True, model_on_all=False):
    complete = Recorder(complete) if (use_model and complete is not None) else complete
    results = []
    for row in rows:
        started = time.time()
        decision = route(row["text"])
        source, json_ok, reason = "rules", None, "rules"
        action, args, raw = None, {}, None
        if isinstance(decision, Match):
            action, args = decision.action.name, decision.args
        elif decision is not None:                       # Ambiguous: the live loop asks again
            source, reason = "rules", "ambiguous"
        elif guard.check(row["text"]) is not None:       # refused before the model, as in handle()
            source, reason = "refused", "refused"
        elif use_model:
            source = "model"
            match, reason, json_ok = propose_detailed(row["text"], complete)
            raw = complete.last if isinstance(complete, Recorder) else None
            if match is not None:
                action, args = match.action.name, match.args
        else:
            source, reason = "skipped", "no_model"
        hybrid_seconds = time.time() - started

        # The model-only arm: same questions, no rules in front of it.
        model = None
        if model_on_all and use_model and source != "model":
            t0 = time.time()
            m, m_reason, m_json = propose_detailed(row["text"], complete)
            model = {"action": m.action.name if m else None,
                     "args": m.args if m else {}, "reason": m_reason,
                     "json_ok": m_json, "seconds": time.time() - t0,
                     "raw": complete.last if isinstance(complete, Recorder) else None,
                     "correct": is_correct(row, m.action.name if m else None,
                                           m.args if m else {})}
        elif source == "model":
            model = {"action": action, "args": args, "reason": reason,
                     "json_ok": json_ok, "seconds": hybrid_seconds, "raw": raw,
                     "correct": is_correct(row, action, args)}

        results.append({
            "id": row["id"], "lang": row["lang"], "text": row["text"],
            "expected": row["action"], "action": action, "args": args,
            "source": source, "reason": reason, "json_ok": json_ok,
            "correct": is_correct(row, action, args), "seconds": hybrid_seconds,
            "raw": raw if source == "model" else None,
            "model": model,
        })
    return results


def per_language(rows, label):
    print(f"  {label}")
    for lang in ("en", "hinglish", "hindi"):
        part = [r for r in rows if r["lang"] == lang]
        if not part:
            continue
        ok = sum(1 for r in part if r["correct"])
        print(f"    {lang:9s} {ok}/{len(part)} ({ok / len(part) * 100:.1f}%)")


def report(results, load_time, rss_before, rss_after):
    print(f"\n=== hybrid pass (rules first, model for the rest) - {len(results)} utterances ===")
    by_rules = sum(1 for r in results if r["source"] == "rules")
    refused = sum(1 for r in results if r["source"] == "refused")
    model_calls = [r for r in results if r["source"] == "model"]
    print(f"answered by rules : {by_rules}")
    print(f"refused (no action): {refused}")
    print(f"answered by model : {len(model_calls)}")
    total_ok = sum(1 for r in results if r["correct"])
    print(f"intent accuracy   : {total_ok}/{len(results)} ({total_ok / len(results) * 100:.1f}%)")
    per_language(results, "per language:")

    if model_calls:
        valid = sum(1 for r in model_calls if r["json_ok"])
        print(f"JSON validity     : {valid}/{len(model_calls)}"
              f" ({valid / len(model_calls) * 100:.1f}%)  target 95%  [small sample]")

    acted = [r for r in results if r["expected"] == "" and r["action"]]
    print(f"acted when none expected: {len(acted)}")
    for r in acted:
        print(f"    {r['text'][:44]!r:48s} -> {r['action']}")

    scored = []
    if any(r["model"] for r in results):
        scored = [r["model"] for r in results if r["model"]]
        valid = sum(1 for m in scored if m["json_ok"])
        ok = sum(1 for m in scored if m["correct"])
        print(f"\n=== model-only pass (no rules in front) - {len(scored)} utterances ===")
        print(f"JSON validity     : {valid}/{len(scored)} ({valid / len(scored) * 100:.1f}%)  target 95%")
        print(f"intent accuracy   : {ok}/{len(scored)} ({ok / len(scored) * 100:.1f}%)  target 90%")
        for lang in ("en", "hinglish", "hindi"):
            part = [r["model"] for r in results if r["lang"] == lang and r["model"]]
            if not part:
                continue
            good = sum(1 for m in part if m["correct"])
            print(f"    {lang:9s} {good}/{len(part)} ({good / len(part) * 100:.1f}%)")

    rejected = sum(1 for r in results if r["reason"] == "bad_args")
    invalid = sum(1 for r in results if r["reason"] == "invalid_output")
    print(f"\nrejected bad args : {rejected}")
    print(f"invalid JSON      : {invalid}")
    sensitive = [a for a, tier in TIERS.items() if tier == "sensitive"]
    # A sensitive action chosen for a request that wasn't asking for it is the
    # failure this metric exists to catch, in either pass.
    chosen = [r["action"] for r in results] + [m["action"] for m in scored]
    wrong = sum(1 for a in chosen if a and TIERS.get(a) == "sensitive")
    print(f"wrong sensitive   : {wrong}   (target 0; registry has {len(sensitive)} sensitive actions)")

    print(f"model load time   : {load_time:.2f}s" if load_time else "model load time   : not loaded")
    print(f"RAM before load   : {rss_before:.0f} MB")
    print(f"RAM after load    : {rss_after:.0f} MB  (+{rss_after - rss_before:.0f} MB)")
    avg = sum(r["seconds"] for r in results) / len(results)
    print(f"avg response time : {avg:.2f}s per utterance (hybrid)")
    if model_calls:
        print(f"  model-answered  : {sum(r['seconds'] for r in model_calls) / len(model_calls):.2f}s")
    if scored:
        print(f"  model-only      : {sum(m['seconds'] for m in scored) / len(scored):.2f}s")

    fails = [r for r in results if not r["correct"]]
    if fails:
        print(f"\nhybrid misses ({len(fails)}):")
        for r in fails:
            print(f"  [{r['lang']:8s}] {r['text'][:46]!r:48s} expected={r['expected'] or '(none)':12s}"
                  f" got={r['action'] or '(none)'} via {r['source']}/{r['reason']}")
            if r["raw"]:
                print(f"      raw reply: {r['raw'].strip()[:150]!r}")
    if scored:
        bad = [r for r in results if r["model"] and not r["model"]["correct"]]
        if bad:
            print(f"\nmodel-only misses ({len(bad)}):")
            for r in bad:
                print(f"  [{r['lang']:8s}] {r['text'][:46]!r:48s} expected={r['expected'] or '(none)':12s}"
                      f" got={r['model']['action'] or '(none)'} ({r['model']['reason']})")
                if r["model"].get("raw"):
                    print(f"      raw reply: {r['model']['raw'].strip()[:150]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="also send every utterance through the model alone")
    args = ap.parse_args()

    rows = load_rows(CSV_PATH, args.limit)
    print(f"intent set: {CSV_PATH}")
    print(f"utterances: {len(rows)}")

    rss_before = rss_mb()
    fb = Fallback()
    complete = None
    if not args.no_model:
        started = time.time()
        complete = fb._get_complete()          # loads the model once and times it
        print(f"model: {fb.model_path.name}  loaded in {time.time() - started:.2f}s")
        print(f"RSS before: {rss_before:.0f} MB")
    results = evaluate(rows, complete, use_model=not args.no_model, model_on_all=args.all)
    report(results, fb.load_time_s or 0.0, rss_before, rss_mb())


if __name__ == "__main__":
    main()
