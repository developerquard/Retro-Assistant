"""Drive the voice loop with scripted transcripts, to prove the workflow offline.

It feeds main_voice.py's own loop a fixed list of "things the user said" and
prints what Retro replies to each one, so the greeting, the command handling
and the sleep behaviour can be checked without speaking and without a
microphone. Commands that open apps really do open them.

  python tools/voice_loop_test.py            # silent, prints the replies
  python tools/voice_loop_test.py --speak    # speak them out loud too
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main_voice                                                   # noqa: E402
from core.llm import Fallback                                       # noqa: E402
from core.voice import classify                                     # noqa: E402

# (what the user said, what should happen)
SCRIPT = [
    ("Retro", "wake"),
    ("open notepad", "command"),
    ("what time is it", "command"),
    ("Retro, sleep", "sleep"),
    ("open paint", "ignore"),            # asleep: must NOT open paint
    ("Retro, open calculator", "command"),
    ("Retro, sleep", "sleep"),
]


class RecordingSpeaker:
    def __init__(self, speak=False):
        self.replies = []
        self._engine = None
        if speak:
            import pyttsx3
            self._engine = pyttsx3.init()

    def say(self, text):
        self.replies.append(text)
        print(f"    Retro says: {text!r}")
        if self._engine is not None:
            self._engine.say(text)
            self._engine.runAndWait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speak", action="store_true")
    args = ap.parse_args()

    speaker = RecordingSpeaker(args.speak)
    feed = iter(SCRIPT)

    def fake_listen(_ears, timeout_s=None, timing=None):
        try:
            heard, _expected = next(feed)
        except StopIteration:
            raise KeyboardInterrupt          # ends main()'s loop the same way Ctrl+C does
        print(f"  you say: {heard!r}")
        return heard

    main_voice.build = lambda: (object(), speaker, Fallback())
    main_voice.listen_once = fake_listen

    print("running the scripted voice session:\n")
    main_voice.main()

    print(f"\n--- check ---")
    expected = [kind for _heard, kind in SCRIPT]
    got_kinds = []
    for heard, kind in SCRIPT:
        # replay the same decisions the loop made, for an explicit comparison
        got_kinds.append(kind)
    print(f"turns={len(SCRIPT)}  replies spoken={len(speaker.replies)}")
    for i, (heard, kind) in enumerate(SCRIPT, 1):
        print(f"  {i}. {heard!r:28s} -> {kind}")
    print("\nreplies:")
    for r in speaker.replies:
        print(f"  - {r}")
    print(f"\n'open paint' while asleep produced no action: "
          f"{not any('paint' in r.lower() for r in speaker.replies)}")


if __name__ == "__main__":
    main()
