"""Shows every application Retro can open, and how it reads a spoken name.

    python tools/list_apps.py                      # count and a sample
    python tools/list_apps.py --all                # every name
    python tools/list_apps.py --match "legacy launcher"
    python tools/list_apps.py --refresh            # rebuild the index
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from core import apps  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="print every discovered app")
    parser.add_argument("--match", action="append", default=[], help="try a spoken name")
    parser.add_argument("--refresh", action="store_true", help="rebuild the index first")
    args = parser.parse_args()

    started = time.time()
    found = apps.load(refresh=args.refresh)
    print(f"{len(found)} applications found in {time.time() - started:.1f}s "
          f"(cached at {apps.CACHE_PATH})")
    by_source = {}
    for item in found:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    print("  " + ", ".join(f"{source}: {count}" for source, count in sorted(by_source.items())))

    if args.all:
        for name in sorted(a.name for a in found):
            print(f"  {name}")
    else:
        sample = sorted(a.name for a in found)[:15]
        print("  " + ", ".join(sample) + (" ..." if len(found) > len(sample) else ""))

    for query in args.match:
        app, others = apps.best(query)
        if app is not None:
            print(f"\n{query!r} -> {app.name!r}  [{app.kind}]  score {apps.score(query, app.name):.2f}")
            print(f"    {app.target}")
        elif others:
            print(f"\n{query!r} -> more than one match:")
            for candidate in others:
                print(f"    {candidate.name}  [{candidate.kind}]  score {apps.score(query, candidate.name):.2f}")
        else:
            print(f"\n{query!r} -> nothing close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
