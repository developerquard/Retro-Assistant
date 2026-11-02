"""Replays the owner's live transcripts through the real voice loop.

Every phrase below is from the log, in order, with the peak it arrived at and whether
Retro was talking over it - which is exactly the three fields `heard` writes down. The
question this answers is the one the owner asked: one "Retro", and then nothing but
the recogniser's own invention, must run no request at all.

Three sessions are in here, because they are three rounds of the same bug. The 10:45
one is the echo: Retro's own greeting and reply coming back through the microphone,
mangled. The 11:13 one is what was left after that was fixed - quiet junk from an
empty room ("Thank you." at 0.031, "Cennance." at 0.158, "Commands." at 0.113) that
the recogniser invented words for and dispatched as requests, each answered out loud,
each answer heard and answered again. The 22:04 one is the loud half of the same
family: phrases at 0.17 to 0.70 that were the recogniser reading its own `prompt`
back ("Commands, I'm going to go to the table,", "Commands, what time is it, it's
time to ", "Retro. Commands. Retro. Commands."), and a phantom "Retro." at 0.168 that
woke the assistant up.

What this can and cannot show. It replays *transcripts*, so it can only exercise the
second line of defence - the words and the level. The first line is the recogniser
itself (see tools/stt_probe.py, which asks the real endpoint), and a phrase that is
stopped there never reaches this file at all. A phrase that this replay still lets
through at the owner's own volume is therefore not a hole in the level gate; it is a
phrase that only the recogniser could have prevented, and that is where it is fixed.

The microphone and the speaker are simulated, and both are driven the way the real
ones are: the speaker reports itself playing while a line is out (that is what the
gate reads), and the microphone calls the gate for each block it produces (that is
what opens and closes the echo window). A simulation that skips either of those is
testing a session where Retro never spoke, and there every echo is rightly allowed
through.

    .venv/Scripts/python.exe tools/replay_live_transcripts.py
"""
import sys
import time

sys.path.insert(0, ".")

from core import session as S      # noqa: E402  (after the path fix)

try:                               # the console is cp1252 on this machine by default
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# What Retro itself said, in the order it said it: the greeting, the capability line
# the log's fragment proves it said ("Commands, the app, and t..."), the reply sentence
# that came back verbatim, the line whose echo carried the wake word back in, and then
# the 22:04 session's own spoken lines - the greeting, a hint, and a reply.
SAID_BY_RETRO = [
    "Hey, I'm listening.",
    "Commands, the app, and the tools all by voice, and I can write things down.",
    "Sure, I got it.",
    "Is there anything on your mind?",
    "Just keeping the laptop humming.",
    "Try saying \u201cOpen Chrome\u201d, \u201cSearch for reminders\u201d, or \u201cWhat time is it\u201d.",
    "I'm here for you.",
    "Say \u201cOpen table\u201d to bring up the table view.",
]

# (transcript, peak, was Retro's own voice playing for every block of it)
PHRASES = [
    # 10:45 - the echo, mangled and at the owner's own volume
    ("Retro.", 0.849, False),                                    # the owner, waking it
    ("How are you today?", 0.849, False),                        # the owner, a real question
    ("Commands, the app, and the app, and the ", 0.106, True),   # the greeting, echoed
    ("Sure, I got it.", 0.907, True),                            # a reply, echoed
    ("ありがとうございました。", 0.079, True),                      # invented out of English
    # 11:13 - the quiet junk from an empty room, every one of them dispatched live
    ("Thank you.", 0.031, False),
    ("And,", 0.051, True),
    ("Cennance.", 0.158, False),
    ("Commands.", 0.113, True),
    ("Commands.", 0.128, True),
    ("Right.", 0.051, True),
    ("So you play yourself.", 0.090, True),
    ("Bye.", 0.051, True),
    ("Retro, anything's on your mind?", 0.240, True),            # an echo with the wake word in it
    ("Commands.", 0.166, True),
    # and the one thing in all of it that was really said to Retro
    ("open notepad", 0.700, True),
    # 22:04 - the loud half: prompt recitations at half the owner's volume, and a
    # phantom "Retro" at 0.168 that woke the assistant and made it greet the room
    ("Retro. Sleep.", 0.994, False),                             # the owner, twice
    ("Retro. Sleep.", 0.977, False),
    ("It's true.", 0.240, True),
    ("Commands, you're free to go.", 0.093, False),
    ("I love you.", 0.406, False),
    ("Thank you.", 0.158, True),
    ("I love it.", 0.138, False),
    ("Retro.", 0.168, False),                                    # a phantom, and it woke it
    ("Commands, I'm going to go to the table, ", 0.557, False),
    ("Commands, I'm a fan of the", 0.233, True),
    ("Oh, finally.", 0.174, False),
    ("H\u00e5n er sann, h\u00e5n er \u00e5 vans a\u00e5.", 0.346, False),
    ("Thank you, everyone.", 0.160, False),
    ("Thank you.", 0.042, False),
    ("Retro. Commands. Retro. Commands.", 0.443, False),         # woke it a second time
    ("Oh my god.", 0.096, True),
    ("Commands, what time is it, it's time to ", 0.699, False),  # the prompt, near verbatim
]


class LiveSpeaker:
    """The real speaker's contract: `playing` and `level` while a line is out."""

    def __init__(self):
        self.playing = False
        self.level = 0.0
        self.disabled = False
        self.last_peak = 0.6
        self.last_cut = False

    def say(self, text, cap=None):
        self.playing, self.level = True, 0.6      # out loud, as the gate sees it
        time.sleep(0.01)
        self.playing, self.level = False, 0.0
        return True

    def stop(self):
        self.playing, self.level = False, 0.0
        return True


def main():
    session = S.Session()
    session.fallback = None
    session.ears = object()               # a microphone that is never opened
    session.speaker = LiveSpeaker()
    session.followup = True               # "listen after Retro", as the settings have it
    session._follow_until = time.time() + 999
    session.loaded = True
    for line in SAID_BY_RETRO:
        session._record_said(line)

    dispatched, woken = [], []
    # Recorded against the phrase that caused it, not against the words. The same
    # words arrive twice at different volumes ("Thank you." at 0.158 and at 0.042,
    # "Retro" at 0.849 and at 0.168) and one of each pair is refused, so matching on
    # the text afterwards reports the wrong row's outcome - which is worse than no
    # report at all, because it looks like the guard missed something it caught.
    outcomes, whys = {}, {}

    def at_now():
        return state["current"]

    def took_it(text, woke=False):
        dispatched.append(text)
        outcomes.setdefault(at_now(), ("dispatched", text))
        return None

    session.dispatch = took_it
    session._wake = lambda: (woken.append(state["current"]),
                             outcomes.setdefault(at_now(), ("woke it", "")))[-1]

    def watched_reason(text, peak=0.0):
        why = session.__class__._echo_reason(session, text, peak)
        whys[at_now()] = why                   # one call per phrase, in order
        return why

    session._echo_reason = watched_reason

    state = {"index": 0, "current": 0}

    def listen_once(ears, **kwargs):
        index = state["index"]
        if index >= len(PHRASES):
            session._stop.set()
            return ""
        text, peak, under = PHRASES[index]
        state["index"] += 1
        state["current"] = index
        timing = kwargs.get("timing")
        if timing is not None:
            # The real `listen_once` fills these in for the phrase it returns, and the
            # loop reads `peak` out of them: a simulation that leaves them empty is a
            # session whose microphone never reported a level, where the yardstick is
            # never written and no fragment is ever too quiet to be a person.
            timing["duration"] = 1.0
            timing["peak"] = peak
            timing["phrase_end"] = time.time()
        if under:
            # The line is still in the air: the gate is consulted for each block, as
            # `listen_once` does, and it is what opens the echo window.
            session.speaker.playing, session.speaker.level = True, 0.6
            session._mic_gate()
            session._heard_under_voice = True       # `_mic_level` sets this per block
        else:
            session._mic_gate()                     # nothing playing: the room's gate
        session._mic_level(0.02)
        if under:
            session.speaker.playing, session.speaker.level = False, 0.0
            session._mic_gate()                     # the output stopped: start the window
        return text

    S.listen_once = listen_once                     # the name the session imported
    session.voice_loop()

    print(f"{'transcript':<44} {'peak':>6}  {'under':<6} {'rule':<12} outcome")
    print("-" * 92)
    for index, (text, peak, under) in enumerate(PHRASES):
        why = whys.get(index, "")
        kind, detail = outcomes.get(index, ("dropped", ""))
        outcome = {"dropped": f"dropped ({why})" if why else "dropped"}.get(kind, kind)
        print(f"{text[:42]:<44} {peak:>6}  {str(under):<6} {why or '-':<12} {outcome}")
    print("-" * 92)
    print(f"requests run: {len(dispatched)} -> {dispatched}")
    print(f"times it was woken up: {len(woken)}")
    print("  at " + ", ".join(f"{PHRASES[i][0]!r} peak {PHRASES[i][1]}" for i in woken))


if __name__ == "__main__":
    main()
