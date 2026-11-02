"""Measures the planner against real requests, with the real model.

Plans are printed, and by default nothing is executed - which is the point: a plan
can be judged without typing into whatever window happens to be open. Add --run to
execute them as well.

    python tools/agent_check.py                          # plan a built-in set
    python tools/agent_check.py --run                    # and carry them out
    python tools/agent_check.py "open ffmpeg and do x"   # your own request

It prints the plan, whether it parsed, how long the model took, and (with --run)
what every step observed - which is the only way to know whether the agent can
actually see the result of what it did.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm import Fallback                      # noqa: E402
from core.planner import Agent, PlanError                # noqa: E402
from core import tools                             # noqa: E402

DEFAULT_REQUESTS = (
    "open the downloads folder",
    "open notepad and type hello there",
    "open chatgpt and type this is retro talking to me and send it",
    "open spotify, search the song sunflower and play it",
    "what is on my screen right now",
    "open file explorer, go to the downloads folder",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("requests", nargs="*", default=list(DEFAULT_REQUESTS))
    parser.add_argument("--run", action="store_true", help="carry the plans out")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    fallback = Fallback(model_path=args.model) if args.model else Fallback()
    agent = Agent(complete=fallback.ask, speak=lambda text: print(f"    says: {text}"))
    planned, failed = 0, 0

    for request in args.requests:
        print(f"\n=== {request}")
        started = time.time()
        try:
            steps = agent.plan(request)
        except PlanError as exc:
            failed += 1
            print(f"    NO PLAN ({exc}) after {time.time() - started:.1f}s")
            continue
        except Exception as exc:
            failed += 1
            print(f"    model failed: {type(exc).__name__}: {exc}")
            continue
        print(f"    planned in {time.time() - started:.1f}s: "
              f"{len(steps)} step{'s' if len(steps) != 1 else ''}")
        for name, arguments in steps:
            print(f"      {name} {arguments}")
        planned += 1
        if args.run and steps:
            print("    running:")
            for name, arguments in steps:
                step_started = time.time()
                result = tools.run(name, arguments)
                print(f"      {name}: {'ok' if result.ok else 'FAILED'} "
                      f"({time.time() - step_started:.1f}s) {result.message[:160]}")

    print(f"\n--- {planned} planned, {failed} without a plan ---")


if __name__ == "__main__":
    main()
