"""Checks the brain Retro is actually using, over the real network.

    python tools/brain_check.py                       # whatever the settings chose
    python tools/brain_check.py --local               # force the offline GGUF
    python tools/brain_check.py --ask "how are you"   # one question of your own

It asks something no typed rule can answer, and plans a request that needs six
steps, then prints how long each took and what came back. The second one is the
WhatsApp job from the log that the 0.5B model could not plan at all - so this is
the check that matters.

With no API key it says so and stops, rather than reporting a working assistant
that cannot think.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, settings                    # noqa: E402
from core.llm import CloudError, build_brain          # noqa: E402
from core.planner import Agent, PlanError             # noqa: E402

CHECKS = (
    "how are you doing today",
    "open whatsapp, search for the contact Developer and type Hi, is your software "
    "ready to publish?",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="force the local GGUF")
    parser.add_argument("--model", default=None, help="a model choice or id to try")
    parser.add_argument("--ask", action="append", default=None)
    parser.add_argument("--status", action="store_true",
                        help="report which brain is in use and stop, loading nothing")
    args = parser.parse_args()

    choice = "1b" if args.local else (args.model or settings.get("model") or config.MODEL_CHOICE)
    brain, is_cloud = build_brain(choice)
    where = f"cloud: {brain.model}" if is_cloud else f"local: {brain.model_path.name}"
    print(f"setting: {choice}")
    print(f"brain:   {'cloud' if is_cloud else 'local'}  ({where})")
    print(f"key:     {'present' if config.cloud_available() else 'not set'}")
    # Which ears, without building them: constructing the local one loads the whole
    # Whisper model, which --status exists to avoid.
    if config.cloud_available():
        print(f"ears:    cloud ({config.CLOUD_STT_MODEL}), local fallback {config.WHISPER_MODEL}")
    else:
        print(f"ears:    local ({config.WHISPER_MODEL})")
    if not is_cloud:
        if args.local:
            print("  (forced local: this is the offline path, and it is the slow, limited")
            print("   one - which is exactly what the comparison is here to show)")
        elif config.cloud_available():
            print("  a key is present, but the chosen model is a local one - the key is")
            print("  doing nothing. Pick CLOUD (or AUTO) to use the real model.")
        else:
            print("  no key is set, so the local model is answering. Set GROQ_API_KEY, or")
            print("  open the window and paste one into CLOUD KEY, to use the real model.")

    if args.status:
        return 0

    agent = Agent(complete=brain.ask, conversational=is_cloud,
                  speak=lambda text: print(f"    says: {text}"))
    checks = args.ask or list(CHECKS)

    planned, failed = 0, 0
    for request in checks:
        print(f"\n=== {request}")
        started = time.time()
        try:
            steps = agent.plan(request)
        except PlanError as exc:
            failed += 1
            print(f"    NO PLAN ({exc}) after {time.time() - started:.1f}s")
            continue
        except CloudError as exc:
            failed += 1
            print(f"    cloud refused: {exc}")
            continue
        except Exception as exc:
            failed += 1
            print(f"    brain failed: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.time() - started
        print(f"    planned in {elapsed:.1f}s: {len(steps)} step{'s' if len(steps) != 1 else ''}")
        for name, arguments in steps:
            shown = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 60 else v)
                     for k, v in arguments.items()}
            print(f"      {name} {shown}")
        planned += 1

    print(f"\n--- {planned} planned, {failed} without a plan ---")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
