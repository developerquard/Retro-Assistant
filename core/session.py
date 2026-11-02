"""One assistant session, shared by the console and the window.

Owns the ears, the voice, the fallback model and the agent, and turns one spoken
or typed line into one reply. It is plain Python with callbacks, so the same object
can be driven from a console loop, from a test, or from a GUI thread - the window
only subscribes to the callbacks and never touches audio itself.

States reported through `on_state`, and the colours the display gives them:

    offline    grey   - the voice could not start, or the session has stopped
    asleep     blue   - listening only for the wake word
    listening  blue   - awake and waiting for a command
    working    green  - running a request
    speaking   green  - reading a reply aloud

Three rules keep it honest, and each one of them was a bug in the version before:

* **Every request runs on its own worker thread.** The listening loop only listens
  and dispatches, so it is always free to hear "Retro, stop" - even while a request
  is stuck inside a model call or a window that will not open.
* **A request can be steered.** Saying "Retro" during one is heard: stop, carry on,
  what are you doing, or start a different request instead. Anything said without
  the wake word while a request is running is ignored, so ordinary conversation
  nearby cannot hijack it.
* **Nothing can latch.** A watchdog clears a state that has been held with no
  request behind it and cancels a request that has run past its limit, so the
  assistant cannot end up green, deaf and only fixable by restarting.
"""
import math
import os
import queue
import threading
import time
import traceback
from collections import deque

from . import builder, compose, handoff, brief
from . import config as _config
from . import diag, log, modes, progress, runtime, settings, states, ui_hook
from .control import Stopped, Task
from .llm import build_brain
from .model import Result
from .planner import Agent, SPEECH_TOOLS, pick_line, THINKING, WORKING
from .runner import ask_yes_no, describe_work, handle
from .tts import VoiceStream
from .voice import (SLEEP_REPLY, Speaker, GREETING, build_ears, classify, device_usable,
                    input_usable, listen_once, strip_wake)

OFFLINE, ASLEEP, LISTENING, WORKING, SPEAKING = "offline", "asleep", "listening", "working", "speaking"
MIC_LEVEL_GAIN = 14.0     # raw microphone RMS to the 0..1 the display expects
# A restatement, rather than an exact repeat. The guard on what has been said compares
# whole lines, which catches a sentence said twice word for word - and misses the one
# the owner actually hears next: the same sentence with one word changed, or the same
# idea said again in a document's next section. Five words is the floor (nothing
# shorter is a sentence worth suppressing by shape), and four fifths of the wording
# must match before a line is treated as the one just said.
RESTATED_WORDS = 5        # a line shorter than this is never suppressed by shape
RESTATED_OVERLAP = 0.8    # and this much of it has to be the same words


def _restated(left, right):
    """True when two lines are the same sentence in almost the same words.

    Word-for-word is exact equality and is checked separately. This is the other half:
    a small model rewriting its own sentence - "Just keeping the laptop humming." and
    "Just keeping the laptop running." - which is the same words to a listener and a
    different string to a computer.

    Two sentences of *different length* are two different sentences. That is the whole
    of the shape rule, and it is here because a first attempt at it was too greedy: a
    document's line and the line that grows out of it - "About Origins, there is this."
    then "And about Origins, there is that too." - are a superset, not a restatement,
    and dropping the second of those is dropping half of what was written. So the
    lengths must match within a word, and the overlap is measured against the *longer*
    line, which also stops a short line from hiding inside a long one.
    """
    first, second = left.split(), right.split()
    if len(first) < RESTATED_WORDS or len(second) < RESTATED_WORDS:
        return False
    if abs(len(first) - len(second)) > max(1, min(len(first), len(second)) // 6):
        return False
    shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
    pool = list(longer)
    hits = 0
    for word in shorter:
        if word in pool:
            pool.remove(word)         # multiset: two "the"s need two "the"s
            hits += 1
    return hits / float(len(longer)) >= RESTATED_OVERLAP
# The echo of Retro's own voice, and the gate that keeps it out of the recogniser.
#
# The gate used to be `played_level * 1.5` read at the instant of the block - and the
# played level reads *zero* while a sentence is being synthesised and in every gap
# between two sentences. So for most of a reply the threshold fell to START_RMS*2 and
# anything in the room barged in, cut Retro off mid-sentence, and had its fragment
# transcribed and handed back as a command. The log showed the whole loop, repeated
# for minutes out of one wake word:
#
#     barge_in (speaking) -> barge_failed -> playback_cut -> speech_unheard
#     -> dispatch woke:false chars 3-10 -> task_end -> barge_in -> ...
#
# Nothing exotic fixes that: the last level the player actually reported is remembered
# for PLAY_TAIL_S after it stops, so the threshold cannot collapse in a gap. What the
# microphone reads is deliberately *not* used to build the gate - a block much louder
# than the output is a person, and teaching the gate their voice is how a gate starts
# refusing the one speaker it exists to let through.
#
# The second half of the same bug is *units*, and it is what made the first fix look
# like it worked while the echo still came through. A player reports its loudness as
# `rms * PLAY_LEVEL_GAIN`, clamped to 1.0 (`core/tts.py` and `core/voice.py` both use
# 6.0); the microphone reports raw rms, where a person talking at a laptop is 0.01 to
# 0.05 and the speech floor is START_RMS. Multiplying one by the other is meaningless:
# a reply at 0.5 on the display scale is 0.083 at the monitor, and "1.5x the played
# level" was therefore 0.125 - five times louder than the loudest thing a person says
# into this microphone. No command spoken over a reply could ever pass, while the
# collapse above let everything through in the gaps. `_echo_threshold` converts first,
# and the two numbers it produces live in the range a person can actually beat.
PLAY_LEVEL_GAIN = 6.0       # the gain the players report their level through
PLAY_GATE = 0.5             # a block must beat this share of what Retro is playing
PLAY_GATE_MAX = _config.START_RMS * 20.0   # never gate above a raised voice
PLAY_TAIL_S = 0.6           # the played level is remembered this long after it stops
PLAY_FLOOR = _config.START_RMS * 2.0   # and the gate is never nothing while Retro talks
# The echo test, in words rather than in levels.
#
# A level gate cannot be the whole answer on a machine like this one, and the live log
# is the proof: the microphone picks the speakers up nearly as loudly as it picks the
# owner up - a reply echoed back at a peak of 0.907 against the owner's own 0.849 - so
# "louder than the echo" is not a test anything reliably passes. The words are what
# separate the two. They have to be matched *roughly* rather than exactly, because the
# recogniser hears the speakers a syllable behind and duplicates across the seam: the
# greeting came back as "Commands, the app, and the app, and the" while what was said
# was "Commands, the app, and t...", the substring test said no, and the fragment was
# dispatched as a request and answered - again on every loop, out of a single "Retro".
ECHO_MATCH = 0.6            # this share of a transcript's words, in order, are Retro's
ECHO_RUN = 3                # ...or this many of them, verbatim and in a row
ECHO_SHORT_WORD = 5         # a one-word transcript shorter than this is a word, not a line
# How loud a phrase has to be to count as the owner, as a share of the owner's own
# voice measured at this microphone. Two floors, because the two cases differ in how
# much evidence they carry: a phrase that says "Retro" is deliberate, and a phrase
# that arrives on the follow-up window alone has to be at the volume of somebody
# talking to the laptop.
#
# Both were re-measured on the newest live session, and both had to go up. What got
# through the old pair (0.35 / 0.15) was not junk at the floor of the room but phrases
# at half the owner's own volume - "I love you." at 0.406, "Retro. Commands. Retro.
# Commands." at 0.443, "Commands, I'm going to go to the table," at 0.557 - against
# the owner's real "Retro. Sleep." at 0.977 and "Retro. Sleep." at 0.994. Every one
# of those was the recogniser reading its own prompt back (see SPEECH_PROMPT in
# core/voice.py), which is fixed where it happens; these floors are the second line,
# and they now sit in the gap the log actually shows: the loudest junk that was ever
# answered, 0.557, is no longer above the bar for a command nobody asked for by name.
ECHO_OF_VOICE = 0.5         # a phrase on the follow-up window, with no wake word
# A phrase that carries the wake word is measured as the owner's voice even when a
# greeting is playing over it, provided it is at least this loud. The first "Retro"
# of a session almost always lands over the greeting, and refusing it left the
# yardstick unset - after which every quiet fragment was "unknown, never quiet" and
# became a command that answered itself.
WAKE_LEVEL = 0.30
WAKE_OF_VOICE = 0.35        # ...and one that says "Retro", which is nobody's accident
# How quickly the measurement of the owner's voice forgets an outlier. It used to be a
# maximum that could only ever go up, so one loud phrase - a cough, a door, the laptop
# lid, "Retro" said up close once - raised the bar for the rest of the session and every
# normal sentence after it was refused as "too quiet to be a person". A measurement
# that cannot come back down is not a measurement of a person; it is a ratchet, and the
# owner's report of a deaf assistant is what it sounds like. Ninety seconds is long
# enough to cover a conversation and short enough that the next one starts clean.
CLEAN_DECAY_S = 90.0
# How long one "Retro" keeps the conversation open when the wake word is *not*
# required every time. The owner asked for three minutes, with the conversation
# itself remembered across the gap.
CONVERSATION_WINDOW_S = _config.CONVERSATION_WINDOW_S
# The older name for the same window, still written by `_barged` and `_ask_brief`.
# It was never defined in this module, so *both* of those raised a NameError at run
# time - and `_barged` raised it immediately after cutting the audio, which is exactly
# the log signature of the storm: `barge_in`, then `barge_failed`, then the reply it
# had just cut being transcribed and answered. A real interruption was therefore also
# failing to re-open the window it needed to hear the answer in.
FOLLOWUP_WINDOW_S = CONVERSATION_WINDOW_S
# How long a microphone that is switched on may carry nothing at all before the owner
# is told. Twenty-five seconds is not a pause in a conversation; it is a diagnosis.
MIC_SILENT_S = 25.0
# Programs that hold the microphone in a way that leaves every other capture stream
# silent. This is not a guess: the live machine's array delivered a phrase at 0.02 and
# then, with ChatGPT Classic's voice session open, at 0.0001 through every host API at
# once - the exact shape of "Retro cannot hear me" that no threshold and no recogniser
# can fix from inside this process. Naming the program on the hint line is the only
# useful thing the app can do about it, and it is far more useful than "check mute".
MIC_HOLDERS = (("chatgpt", "ChatGPT"), ("discord", "Discord"), ("teams", "Teams"),
               ("zoom", "Zoom"), ("obs", "OBS"), ("audacity", "Audacity"),
               ("whatsapp", "WhatsApp"), ("slack", "Slack"))
# How long Retro must have said nothing before a "still working on it" line may be
# spoken. The owner's rule: while it is answering, none of those; only when the wait
# has gone quiet for this long does one play.
SILENCE_BEFORE_FILLER_S = _config.FILLER_QUIET_S
# How long a wake takes, at least. The orb walks from grey to blue over this and the
# brain loads underneath it; ui/agent.py draws the same number, and the two are the
# same three seconds the owner asked for.
WAKE_LOAD_S = 3.0

# Said while a request is running, when the user says something without the wake
# word. Ignoring it silently would look like being ignored; saying it explains both.
BUSY_NOTE = "Say Retro first and I'll stop or change what I'm doing."
# Words that stop a request. While a request is running these are heard *without* the
# wake word (see `_control_word`), because "stop" said to something that is visibly
# working is not small talk and being asked for the wake word first is what made the
# stop feel ignored. Prefix matching covers "stop it", "stop now", "cancel please".
STOP_WORDS = ("stop", "stop it", "stopp", "cancel", "cancel that", "abort", "quit it",
              "stop that", "stop this", "halt", "enough", "that's enough", "thats enough",
              "cut it out", "leave it", "stand down", "never mind",
              "ruko", "ruk jao", "ruk", "band karo", "chhodo", "chhod do",
              "nevermind", "forget it", "bas karo", "bas kar do",
              "रुको", "बंद करो", "छोड़ो", "रद्द")
CONTINUE_WORDS = ("continue", "carry on", "go on", "keep going", "resume", "jaari rakho",
                  "jari rakho", "chalu rakho", "जारी रखो", "चालू रखो")
STATUS_WORDS = ("what are you doing", "what's happening", "whats happening", "status",
                "where are you", "kya kar rahe ho", "kahan tak", "क्या कर रहे हो")
YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "affirmative", "correct",
             "haan", "han", "ha")
NO_WORDS = ("no", "nope", "nah", "dont", "stop", "cancel", "never", "nahi", "mat")


def _nonnegative(value):
    """A level as a float, or 0.0 for anything that is not a usable number.

    Both playback paths report their level through objects that can hold None while
    they are starting or stopping, and `max(None, 0.0)` is a TypeError that used to
    take the whole microphone loop down with it.
    """
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _mic_holders():
    """The names of running programs known to take the microphone, newest concern first.

    Only used to explain a silent capture, never to change it: closing another app is
    the owner's decision. Best-effort by design - psutil may be missing, permissions
    may hide a process, and an empty list simply means "not one of the usual suspects".
    """
    try:
        import psutil
    except Exception:
        return []
    found = []
    try:
        for process in psutil.process_iter(["name"]):
            name = str((process.info or {}).get("name") or "").lower()
            if not name:
                continue
            for needle, label in MIC_HOLDERS:
                if needle in name and label not in found:
                    found.append(label)
    except Exception:
        return found
    return found


def _words(text):
    """A line as bare lowercase words, punctuation and spacing gone.

    The echo test compares words rather than characters, so a comma - or the
    punctuation a recogniser invents - cannot make two identical phrases look
    different.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " "
                      for ch in str(text or "").lower())
    return cleaned.split()


def _echo_overlap(words, said):
    """How much of `words` is Retro's own line: (share in order, verbatim run).

    `share` is the largest number of the transcript's words that also appear inside
    `said`, in the same order, as a fraction of the transcript - 1.0 means every word
    heard is a word Retro itself had just said. `run` is the longest stretch of them
    that appears verbatim, which is what catches the seam: a transcript that is mostly
    Retro's words with an invented one in the middle still contains a run of three or
    four of them in the right order.

    Both are computed here, in one place, because the two rules that read them disagree
    about which case they are for - a long mangled echo is caught by the share and a
    short clean one by the run - and a caller that had to pick one would be wrong about
    half the fragments in the log.
    """
    said = _words(said)          # both sides through the same normalisation
    if not words or not said:
        return 0.0, 0
    best = 0
    for start in range(len(said)):
        index, matched = start, 0
        for word in words:
            if index < len(said) and said[index] == word:
                matched += 1
                index += 1
        if matched > best:
            best = matched
    longest = 0
    for a in range(len(words)):
        for b in range(len(said)):
            run = 0
            while (a + run < len(words) and b + run < len(said)
                   and words[a + run] == said[b + run]):
                run += 1
            if run > longest:
                longest = run
    return best / len(words), longest


def is_stop(text):
    t = " ".join(str(text).lower().split())
    return bool(t) and (t in STOP_WORDS or any(t.startswith(word + " ") for word in STOP_WORDS))


def is_continue(text):
    t = " ".join(str(text).lower().split())
    return bool(t) and (t in CONTINUE_WORDS or any(t.startswith(word + " ") for word in CONTINUE_WORDS))


def is_status(text):
    t = " ".join(str(text).lower().split())
    return any(word in t for word in STATUS_WORDS)


def is_control(text):
    """True for a phrase that steers a request already running, not a new one."""
    return is_stop(text) or is_continue(text) or is_status(text)


def local_voice_only():
    """True when only the local Windows voice speaks - which is the shipped default.

    The hosted voice sounded nicer and was slower to the first word (1.6 s against
    0.13 s), it was rate limited on this machine 126 times, and a reply that lost the
    hosted voice halfway through changed voice mid-answer. The owner asked for the
    local one only. `local_voice` in settings.json (or `RETRO_LOCAL_VOICE=0`) brings
    the hosted voice back.
    """
    value = settings.get("local_voice")
    if value is None:
        return _config.LOCAL_VOICE
    return bool(value)


def says_yes(text):
    """True for a spoken agreement. A refusal anywhere in it wins."""
    t = " ".join(str(text or "").lower().replace("'", " ").replace("’", " ").split())
    if any(mark in t for mark in ("नहीं", "मत ")) or any(word in NO_WORDS for word in t.split()):
        return False
    if any(mark in t for mark in ("हाँ", "हां")):
        return True
    return any(word in YES_WORDS for word in t.split())


class Session:
    """Drives the assistant. Nothing here starts until `start()` is called."""

    def __init__(self, on_state=None, on_reply=None, on_level=None, on_log=None,
                 confirm=None, model_choice=None, idle_unload_s=None,
                 task_timeout_s=None, progress_every_s=None, state_stuck_s=None,
                 on_progress=None, on_mode=None, on_writing=None, on_mic=None):
        self.ears = None
        self.speaker = None
        self.fallback = None          # the brain: the cloud model, or the local GGUF
        self.planner = None
        # Chat or work, and whether a follow-up needs the wake word. Both are read
        # from settings at startup and both can be changed by voice or by a tool.
        self.mode = str(settings.get("mode") or modes.DEFAULT).lower()
        if self.mode not in modes.ALL:
            self.mode = modes.DEFAULT
        # "Say 'Retro' every time" or "say it once, then talk" - the setting the owner
        # calls "listen after Retro". Off means the wake word for *every* request, on
        # means one "Retro" opens the conversation for a few minutes.
        self.followup = bool(settings.get("followup"))
        # Background voice hearing: asleep, the orb reacts to whatever the machine is
        # playing - and still hears no commands at all.
        self.background = bool(settings.get("background"))
        # The microphone switch. Off means no stream, no transcription, no STT call -
        # not "listening and throwing it away". It is remembered between runs because
        # a user who turned the microphone off did not mean "until the next launch".
        self.mic_on = settings.get("mic")
        self.mic_on = True if self.mic_on is None else bool(self.mic_on)
        # Which microphone, by *name*. A saved index is meaningless after a USB
        # headset is plugged in or the machine reboots, so the name is what survives
        # and `_input_device` is what resolves it - once, not on every pass.
        self._mic_device = settings.get("mic_device") or None
        self._mic_index = None
        self._mic_index_ready = False
        self._voice_wanted = True         # whether this session was started with a voice
        # What the window has been told, and whether a capture is open right now. A
        # microphone switched off during a phrase is closed at the listener's next
        # block, so "off" is reported after that block - never while the device is
        # still being read.
        self._mic_reported = self.mic_on
        self._capturing = False
        # When the microphone last carried anything above the speech floor, and whether
        # the silence since has been reported. A stream that opens, delivers its blocks,
        # and carries only the room is indistinguishable from a user who is not talking
        # - so it is timed here and said out loud, once.
        self._mic_heard_at = time.monotonic()
        self._mic_silent_reported = False
        self.loaded = False               # False while asleep: no brain in memory
        self._follow_until = 0.0
        # When anything was last really said. Nothing has been said yet, so a session
        # starts out quieter than the filler threshold: the first job's acknowledgement
        # is allowed the moment the app opens, and only a reply that has just ended is
        # allowed to suppress one.
        self._last_audible = time.monotonic() - _config.FILLER_QUIET_S - 1.0
        self.audience = None              # the system-audio sampler, while asleep
        # True when the brain is a real model that can hold a conversation, not only
        # name a command. Set by `start()`/`set_model()` from what actually loaded.
        self.converse = False
        self.error = None
        self.current_task = None
        self._on_state = on_state or (lambda state, detail="": None)
        self._on_reply = on_reply or (lambda text, ok: None)
        self._on_level = on_level or (lambda level: None)
        self._on_log = on_log or (lambda text: None)
        self._on_progress = on_progress or (lambda text: None)
        self._on_mode = on_mode or (lambda mode: None)
        self._on_writing = on_writing or (lambda event: None)
        self._on_mic = on_mic or (lambda on: None)
        self.phase = states.IDLE
        self.confirm = confirm or ask_yes_no
        self.model_choice = (model_choice or settings.get("model")
                             or _config.MODEL_CHOICE).lower()
        self._idle_unload_s = idle_unload_s
        self.task_timeout_s = _config.TASK_TIMEOUT_S if task_timeout_s is None else task_timeout_s
        self.progress_every_s = _config.PROGRESS_EVERY_S if progress_every_s is None else progress_every_s
        self._last_progress_detail = ""
        self.state_stuck_s = _config.STATE_STUCK_S if state_stuck_s is None else state_stuck_s
        self.tasks = deque(maxlen=20)     # this session's tasks, newest last
        self._typed = queue.Queue()
        self._stop = threading.Event()
        self._task_lock = threading.Lock()
        self._thread = None
        self._watcher = None
        self._state = OFFLINE
        self._state_since = time.time()
        self._voice_stream = None         # one ordered speech stream per request
        # The trace of the listening pass that is about to become a request. It is
        # handed over here rather than as an argument so the voice loop's call to
        # `dispatch` keeps the same two-argument shape every test and caller knows.
        self._next_trace = None
        self._spoken = []                 # what was actually said for this request
        # Which moment the voice belongs to. Every line that is about to be spoken
        # names the generation it was written for, and a line whose generation has
        # passed is dropped rather than played. Without it a sentence composed for one
        # moment - the waking greeting, a line about work that has since been
        # superseded - is spoken into whatever is running when the model finally
        # answers, and the owner hears a second answer arriving late on top of the
        # first one.
        self._speech_gen = 0
        # One line in the air at a time when no request is running to keep order.
        self._speech_lock = threading.Lock()
        # The last few things Retro said, and when it was last heard to stop saying
        # them. Read by the echo guard: the microphone hears these speakers, and its
        # own words come back as commands (see `_is_echo`).
        self._said_recently = deque(maxlen=16)
        # The same line must not come out twice, and the same request must not be run
        # twice. Two separate ledgers, because they answer two different questions: one
        # is "has Retro already said this?" and the other is "has Retro already been
        # asked this?". Both are read and written under `_speech_lock`-free plain code on
        # purpose: they are lists of tuples and every access is atomic in CPython, and a
        # dropped duplicate is a cheap mistake while a lock here could deadlock the voice.
        self._recent_lines = deque(maxlen=40)     # (monotonic, normalised line)
        self._recent_asks = deque(maxlen=8)       # (monotonic, normalised request)
        self._output_ended_at = 0.0
        self._output_was_playing = False
        # The loudness the voice last reported, and how long after it stops that still
        # describes the room. Read by the microphone gate: the player reports zero
        # between sentences, and a gate that believed it opened the microphone to the
        # room for the whole of every reply.
        self._played_held = 0.0
        self._played_until = 0.0
        # Set by the microphone callback while Retro's own voice is on the speakers:
        # anything the recogniser produces from those blocks is Retro hearing itself,
        # and is dropped however well it matches a command.
        self._heard_under_voice = False
        # The loudest phrase last heard with nothing playing, and the match that decided
        # the last echo. Together they are what `_too_quiet_to_be_a_person` measures
        # against, and what the log line says when a fragment is dropped.
        self._clean_peak = 0.0
        self._clean_at = time.monotonic()
        self._echo_match = (0.0, 0)
        # The document being written right now, if there is one. It is here because a
        # stop that arrives mid-document has to be able to say what was kept.
        self._document = None
        # The last reply, as it was shown and (where it was not already spoken in
        # pieces) said. "Repeat that" is the one request that is answered from the
        # past instead of the model: the line the owner missed already exists, and
        # asking a model for it again would produce a different line.
        self._last_reply = ""

    # ---------- lifecycle ---------------------------------------------------

    def start(self, voice=True):
        """Loads the speech model and the voice. Returns True when it worked.

        A failure is reported through `on_state('offline', reason)` instead of
        raising, so a window can still open and take typed commands.

        With the microphone switched off, the ears are not loaded either: the local
        Whisper weights are several hundred megabytes that nothing can reach until the
        switch is turned back on, and loading them to ignore them is the opposite of
        turning the microphone off. Typed commands never needed them.
        """
        self._voice_wanted = bool(voice)
        # Detailed timing is on unless the user turned it off. It costs a ring buffer
        # of a few dozen small dictionaries, not a monitoring framework.
        saved = settings.get("diagnostics")
        diag.set_enabled(True if saved is None else bool(saved))
        try:
            self._load_brain()
            # The speaker reports the level of every block it plays, so the orb
            # moves with the real voice rather than an imitation of one.
            self.speaker = Speaker(on_level=self._on_level)
            self.speaker.ensure_voices()             # builds the engine: fails loudly here
            saved_voice = settings.get("voice")      # remembered from last time
            if saved_voice:
                self.speaker.choose(saved_voice)
            self._restore_output(settings.get("output"))
            if voice and self.mic_on:
                # Hosted Whisper when there is a key, the local model otherwise; it
                # falls back on its own if the network drops mid-session.
                self.ears = build_ears()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.event("start_failed", error=self.error)
            self._set_state(OFFLINE, self.error)
            return False
        self._start_watcher()
        self._refresh_audience()            # the gate, and the picture: only while awake
        runtime.attach(self)              # so the tools can reach this session
        self._report_mic(self.mic_on, force=True)   # the real switch, not a guess
        self._set_state(ASLEEP, "say 'Retro'" if self.mic_on else "microphone off")
        self._on_mode(self.mode)
        log.event("input_ready", device=self._input_device(),
                  input_name=(self._mic_device or "system default"),
                  was=str(settings.get("mic_device") or "")[:40])
        log.event("session_ready", model=self.model_choice,
                  brain=("cloud" if self.converse else "local"),
                  mode=self.mode, followup=self.followup,
                  background=self.background, mic=self.mic_on,
                  brain_model=(getattr(self.fallback, "model", None) if self.converse else None))
        return True

    def _load_brain(self):
        """Builds the brain and the planner. Startup and every wake come through here."""
        self.fallback, self.converse = build_brain(self.model_choice,
                                                   idle_unload_s=self._idle_unload_s)
        if not self.converse and _config.cloud_model_for(self.model_choice) is not None:
            # The cloud was asked for and could not be used - by a nickname from an
            # older build, by "auto", or by the exact model id the picker carries.
            # Say so once in the log; the local model still runs, so the assistant
            # still answers.
            log.event("cloud_unavailable", requested=self.model_choice, reason="no key")
            self._log("no cloud API key set - using the local model")
        self.planner = self._new_planner()
        self.loaded = True
        return True

    def _release_brain(self):
        """Lets go of the model. Sleep means the machine is not holding it."""
        brain, self.fallback = self.fallback, None
        self.planner = None
        self.loaded = False
        if brain is not None:
            try:
                brain.unload()
            except Exception as exc:              # a model that will not let go is
                log.event("unload_failed", error=type(exc).__name__)   # not fatal

    def _sleep(self):
        """Sleep: grey, still, and unloaded - and it stays that way until "Retro".

        Everything the owner asked sleep to mean happens here: the drawing stops and
        goes grey (the window does that from the phase), the model is let go of, the
        conversation is closed, and the only thing left running is the wake word.

        Nothing samples the output while it sleeps, and nothing the microphone or the
        speakers do may move the orb. The owner asked for exactly that, in as many
        words: asleep is still until it hears "Retro" - no colour, no reacting to a
        video in the next tab, no level on screen. This used to start the sampler
        here and leave it running "for the picture", so a sleeping Retro sat there
        dancing to whatever the laptop was playing, which reads as an assistant that
        is awake and ignoring you.
        """
        self._cancel_speech()
        self._follow_until = 0.0
        if self.planner is not None:
            self.planner.forget()          # waking up starts a fresh conversation
        self._release_brain()
        # The phase goes first, because it is what "still" is read from: a sampler
        # refreshed before the session admits it is asleep would be started again.
        self._set_phase(states.WAKE, "say 'Retro'")
        self._refresh_audience()           # asleep: nothing reads the output at all
        log.event("session_sleep", background=self.background, still=self._dormant())

    def _dormant(self):
        """True while Retro is asleep: grey, still, and reporting nothing.

        This is the whole of "asleep means still". Three things read it - the state
        and phase it refuses, the microphone level it refuses to draw, and the output
        sampler it refuses to run - so there is one answer to "may the UI move right
        now" rather than four places that each have to remember the rule.

        Two conditions have to be false as well, and both are deliberate.

        `not self.loaded`: the moment between "asleep" and "waking up" is exactly when
        the colour is supposed to walk, and a wake that has begun is not dormant.

        `self.current_task is None`: a request that is really running - most importantly
        one **typed** into the window, which needs no microphone and no wake word - has
        the orb back. What sleep is still against is the room, not the owner's own
        deliberate command.

        And `not self.background`: background voice hearing is the one switch that says
        the picture may keep moving while the assistant sleeps. With it on, sleep is
        still unloaded and still unable to act on anything the room says - but the orb
        is no longer inert, so the audio-reactive visuals do not stop the moment Retro
        goes quiet, which is what the owner asked for by name.
        """
        # ...and the background visualiser only counts when the microphone is off. The
        # owner's rule, and this machine's hardware: a sampler and a microphone open at
        # the same time leave both with nothing, so the look runs in mic-off time only.
        # With the microphone on, a sleeping orb is still and grey even if the switch is
        # on; turning the microphone off brings the visualiser straight back.
        return (self._state == ASLEEP and not self.loaded and self.current_task is None
                and not (self.background and not self.mic_on))

    @property
    def sleeping(self):
        """True while Retro is asleep. The window reads this to leave the orb still."""
        return self._dormant()

    def _refresh_audience(self):
        """Runs the output sampler for exactly as long as it was asked for.

        Two jobs, one of them impossible while asleep. It gates the microphone against
        whatever the machine is playing, which only matters when a command might be
        coming, and it feeds the audio-reactive visuals.

        It is tied to the *setting* now, not to the state. It used to stop whenever the
        session slept, which took the visuals' only source of audio with it: with
        background hearing switched on, going quiet turned the orb still, and the
        switch the owner had just turned on did nothing at all. The microphone is
        untouched either way - this samples the machine's own output, so it is not the
        microphone being left open, and it still stops the moment the setting is off.
        """
        if self.background and not self.mic_on:
            self._start_audience()
        else:
            self._stop_audience()

    def _wake(self):
        """Wakes slowly, on purpose: load, then three seconds of colour, then ready.

        The owner asked for the load to be visible - grey turning blue over about
        three seconds, and *then* the greeting. So the ramp and the load run together
        and the slower of the two decides: a cloud brain comes back instantly and
        still gets its three seconds, because blinking awake and taking a breath are
        different things to sit next to and the one asked for is the breath.
        """
        started = time.monotonic()
        if self.followup:
            # One "Retro" opens the conversation: with listen-after-Retro on, this wake
            # starts the window, so the next sentence needs no wake word even when the
            # previous window had long since closed itself.
            self._follow_until = time.time() + CONVERSATION_WINDOW_S
        # The sampler starts here and not at boot, because sleep does not run it: while
        # the machine is playing media a block has to beat that room level to be
        # transcribed at all, so a video is never spent searching for the word "Retro".
        self._refresh_audience()
        if self.loaded:
            # Already awake - saying "Retro" again is just asking for attention, and
            # making the owner wait three seconds for that would be a stall, not a
            # load. The slow wake is only for a machine that was really let go of.
            log.event("session_wake", seconds=0.0, loaded=True)
            return True
        self._set_phase(states.LOADING, "waking up")
        try:
            self._load_brain()
        except Exception as exc:
            log.event("wake_failed", error=type(exc).__name__)
            self._set_state(OFFLINE, f"could not load ({type(exc).__name__})")
            return False
        left = WAKE_LOAD_S - (time.monotonic() - started)
        if left > 0:
            time.sleep(left)               # the ramp is the point, not a side effect
        log.event("session_wake", seconds=round(time.monotonic() - started, 2),
                  loaded=False)
        return True

    def _start_audience(self):
        """Starts the system-audio sampler, in microphone-off time only.

        It draws the visualiser. It used to gate the microphone as well, and that job
        is gone with the policy: sampling the output and capturing the microphone at
        the same time is not possible on this machine - both go quiet - so the two are
        mutually exclusive, and the microphone wins whenever it is on. The sampler
        therefore runs exactly when the owner has turned background hearing on *and*
        the microphone off, which is also the only time the visualiser is shown.
        """
        if not self.background or self.mic_on:
            return None
        if self.audience is not None:
            return self.audience
        from .systemaudio import Sampler
        device = getattr(self.speaker, "device", None)
        self.audience = Sampler(device=device)
        self.audience.start()
        log.event("visualiser_started", reason=self.audience.reason)
        return self.audience

    def _stop_audience(self):
        """Stops it, so nothing is reading the output."""
        sampler, self.audience = self.audience, None
        if sampler is not None:
            sampler.stop()
            log.event("visualiser_stopped")
        return sampler

    def audience_level(self):
        """(level, bands) from the room, or (0, None) when nothing is sampling."""
        sampler = self.audience
        if sampler is None:
            return 0.0, None
        return sampler.level, list(sampler.bands)

    def audience_vocal(self):
        """How much of the room's audio is a voice, 0..1 - or None if unknown.

        Measured by the sampler on the raw spectrum (200-3500 Hz against the rest of
        the mix), which is not something the window can recover from twelve bars that
        have already been normalised against the loudest one.
        """
        sampler = self.audience
        if sampler is None:
            return None
        return float(getattr(sampler, "vocal", 0.0) or 0.0)

    def quiet_for(self):
        """Seconds since anything was actually said, or None if it is talking now."""
        stream = self._voice_stream
        if stream is not None and stream.playing:
            return 0.0
        if self.speaker is not None and self.speaker.playing:
            return 0.0
        return time.monotonic() - self._last_audible

    def may_fill_silence(self):
        """Whether a "still working on it" line is allowed right now.

        The owner's rule, and the reason this exists at all: while Retro is answering
        - or has just answered - nothing of that kind may be said. Only a wait that
        has gone quiet for SILENCE_BEFORE_FILLER_S may be covered by one.

        Chat mode is conversation, so it is never covered by one: there is nothing
        being executed to report on, and a reminder that it is "still working" only
        interrupts the answer the owner is waiting for. The window may still show a
        label - words on screen interrupt nothing - but nothing is spoken.
        """
        if self.mode == modes.CHAT:
            return False
        quiet = self.quiet_for()
        return quiet is not None and quiet >= SILENCE_BEFORE_FILLER_S

    def _restore_output(self, saved):
        """Puts the voice back on the device the user chose - if it is still there.

        A saved *index* is not a saved device. The device list shifts when headphones
        are plugged in, a Bluetooth headset goes to sleep or the machine reboots, and
        the index that meant "the speakers" last week means something nobody is
        listening to today. This app offered only WASAPI devices and then refused to
        open them, so a remembered choice was not just stale, it was fatal: every line
        was written into a device that would not take it, three of them were counted as
        wedged, and the voice switched itself off for the rest of the session. That is
        the whole of "it speaks and I can't hear anything".

        So: resolve by name, keep an index only if it is one of the devices offered
        right now, check that the device can actually be opened, and otherwise use the
        system default and say so. A stale index is migrated to a name on the way past.
        """
        chosen, offered = None, dict(self.outputs())
        if isinstance(saved, int):
            if saved in offered:
                chosen = saved
            else:
                log.event("output_device_missing", saved=saved)
        elif saved:
            chosen = self.speaker.choose_output(saved)
            if chosen is None:
                log.event("output_device_missing", saved=str(saved)[:60])
        if chosen is not None and not device_usable(chosen):
            log.event("output_device_unusable", device=chosen)
            chosen = None
        if chosen is None and saved:
            self._log(f"'{str(saved)[:40]}' isn't available - using the system default")
        self.speaker.choose_output(chosen)
        if chosen is not None and offered.get(chosen) and settings.get("output") != offered[chosen]:
            settings.save(output=offered[chosen])       # an index becomes a name
        # `output_name`, not `name`: the first argument of log.event is the event name.
        log.event("output_ready", device=chosen, output_name=offered.get(chosen, "system default"),
                  was=str(saved)[:40])

    def start_voice_loop(self):
        """Runs the listening loop on its own thread - exactly one of them.

        Called twice (a window rebuilt around an existing session, say), a second
        loop would open a second microphone stream and run every request twice. The
        guard is here rather than at the call site so every caller gets the same one
        loop, and a stopped session does not block a later start.
        """
        if self._thread is not None and self._thread.is_alive():
            log.event("voice_loop_again", why="already running")
            return self._thread
        self._thread = threading.Thread(target=self._voice_loop_guarded, name="retro-voice",
                                        daemon=True)
        self._thread.start()
        return self._thread

    def _voice_loop_guarded(self):
        """The loop under a net, so a death is written down instead of being silent.

        `voice_loop` catches the failures it knows about - a microphone that will not
        open, a transcription that threw - and those are logged where they happen. An
        exception from anywhere else (the window's callbacks, an import) would end the
        thread, and a dead listening thread is indistinguishable from a deaf one: no
        microphone error, no state change, and an orb that looks like it is waiting.
        """
        try:
            self.voice_loop()
        except Exception as exc:
            log.event("voice_loop_crashed", error=f"{type(exc).__name__}: {exc}"[:160],
                      trace=traceback.format_exc()[-400:])
        log.event("voice_loop_ended", stopping=self._stop.is_set())

    def stop(self):
        self._stop.set()
        self._cancel_speech()
        self._stop_audience()
        runtime.detach(self)

    @property
    def state(self):
        return self._state

    def _new_planner(self):
        """The agent, wired to this session: its model, its voice and its own answers.

        Every part the agent may reach back for is this session's: the model that
        streams, the streamed voice, the confirmation, the one question before a
        document is written, and the window's view of that document. It is built in
        one place so a session that is started twice - or switched to another brain -
        cannot end up holding an agent wired to the session it used to be.
        """
        return Agent(complete=self._model_ask, speak=self.say_step, confirm=self.confirm,
                     conversational=self.converse, stream=self._model_stream,
                     brief=self._ask_brief, on_writing=self._writing_event)

    def _model_ask(self, messages, max_tokens=220):
        """The planner's window onto the language model."""
        if self.fallback is None:
            raise RuntimeError("no model")
        trace = diag.current()
        started = diag.now_ns()
        try:
            return self.fallback.ask(messages, max_tokens)
        except Exception as exc:
            if trace is not None and not any(item["stage"] == "llm" for item in trace.errors):
                # The cloud client records its own failures with the status and the
                # retry flag; only a failure it could not see is recorded here.
                trace.error("llm", exc)
            raise
        finally:
            if trace is not None:
                trace.add("llm", diag.now_ns() - started)
                if trace.streaming is None:
                    trace.streaming = False
                if "first_text" not in trace.marks:
                    # A one-shot answer has no first token to wait for: the whole
                    # text arrives at once, and that instant is its first text.
                    trace.mark("first_text")

    def _model_stream(self, messages, max_tokens=400, on_delta=None):
        """The same model, streamed - so an answer can be spoken as it is written.

        Every piece of the reply passes a cancellation checkpoint on its way through.
        A socket call cannot be interrupted once it is in flight, but this is the
        next best thing: saying "stop" while the model is still writing stops the
        words within one token instead of at the end of the whole answer, which is
        the difference between a stop that works and one that looks ignored.
        """
        if self.fallback is None:
            raise RuntimeError("no model")
        task = self.current_task
        trace = diag.current()
        started = diag.now_ns()
        if trace is not None:
            trace.streaming = True

        def checked(piece):
            if task is not None and not task.alive():
                raise Stopped(task.reason or "user")
            if trace is not None and "first_text" not in trace.marks:
                trace.mark("first_text")
            if on_delta is not None:
                on_delta(piece)

        try:
            return self.fallback.stream(messages, max_tokens, checked)
        except Exception as exc:
            if trace is not None and not any(item["stage"] == "llm" for item in trace.errors):
                trace.error("llm", exc)
            raise
        finally:
            if trace is not None:
                trace.add("llm", diag.now_ns() - started)

    # ---------- callbacks ---------------------------------------------------

    def _set_state(self, state, detail="", phase=None):
        # Asleep is still. Nothing heard in the room, no progress line, no leftover
        # phase may paint the orb while it sleeps - the only two ways out are a real
        # failure and the wake itself. Without this the loop's own "say 'Retro'" and a
        # stray phase from a task that had not finished yet kept the window repainting
        # a sleeping assistant, which is the opposite of what sleep is for.
        if self._dormant() and state not in (OFFLINE, ASLEEP) and phase not in (states.LOADING,
                                                                                 states.IDLE):
            return
        if state != self._state:
            self._state_since = time.time()
        if phase is not None:
            self.phase = phase
        self._state = state
        self._on_state(state, detail)

    def _set_phase(self, phase, detail=None):
        """Reports a fine phase and lets it decide the coarse state.

        One mapping, in core/states.py, so the microphone, the model and the
        execution engine can never each hold a different idea of what is going on -
        which is how the circle could look idle through a running task.
        """
        self._set_state(states.ui_state(phase), states.label(phase) if detail is None else detail,
                        phase=phase)

    def on_state(self, fn):
        self._on_state = fn

    def _log(self, text):
        self._on_log(text)

    def _progress(self, text):
        """One short label about a real event, for the text beside the circle."""
        if text:
            self._on_progress(str(text)[:44])

    # ---------- speaking, in order -----------------------------------------

    @staticmethod
    def _normalise(text):
        """What two lines are compared as: case, punctuation and spacing are not words.

        "Opening my settings." and "opening my settings" are the same line said twice,
        and that is exactly the pair the owner heard three times in a row. Everything
        that compares what was said goes through this, so the comparison cannot be
        defeated by a full stop.

        What is stripped first is the *markers* - `**bold**`, a bullet, a step number,
        a fence - because a line is cleaned on its way to the voice and compared raw on
        its way through here, and the two spellings of one sentence did not match. That
        mismatch is a real repeat: a reply streamed as `**Done.** Setting the volume.`
        was spoken once as one thing and again as the other, twelve seconds apart.
        What two things are compared as is what they would sound like.
        """
        from .tts import speakable
        return " ".join(speakable(text).lower().split()).strip(" .,!?;:-\"'")

    def _recently_said(self, text, window_s=None):
        """True when this exact line has already been spoken inside the window.

        This is the guard against the repetition the owner reported, and it is deliberately
        blind to *who* said it: a plan's `say` step, the sentences of a streamed answer, the
        reply written back at the end of a request, and the greeting the model writes all
        reach the voice by different routes, and any two of them can carry the same words.
        Checking the words here - at the moment of speaking - catches all of them at once,
        where a check on any single route would only catch one of them.
        """
        want = self._normalise(text)
        if not want:
            return False
        window = _config.REPEAT_SUPPRESS_S if window_s is None else window_s
        now = time.monotonic()
        for when, line in reversed(self._recent_lines):
            if now - when > window:
                break
            if line == want:
                return True
            if _restated(line, want):
                # The same sentence in almost the same words. The owner's complaint was
                # that it repeated itself "randomly", and this is the shape that got
                # past an exact comparison: a document's next section restating the
                # point it had just made, or a small model rewriting one sentence
                # twice. Same words to a listener, different string to a machine.
                return True
        return False

    def _note_line(self, text):
        """Writes a line down as spoken, so the next one can be compared against it."""
        want = self._normalise(text)
        if want:
            self._recent_lines.append((time.monotonic(), want))   # maxlen caps it

    def _duplicate_ask(self, text):
        """True when this request has already been run very recently, and is dropped.

        One question came back as three and four answers, and the second and third of
        them were the *same request* run again: the recogniser returns the same words
        twice when a block is split, and the microphone hears Retro's own answer and
        hands it back as a command. Both are the same failure from the user's side - a
        question answered several times - so both are stopped here, at the door, before
        a task is created for them.
        """
        want = self._normalise(text)
        if not want:
            return False
        now = time.monotonic()
        for when, line in reversed(self._recent_asks):
            if now - when > _config.DUPLICATE_ASK_S:
                break
            if line == want:
                log.event("duplicate_ask_dropped", chars=len(want))
                return True
        self._recent_asks.append((now, want))
        return False

    def _begin_speech(self, trace=None):
        """Starts one ordered speech stream for the request that is about to run.

        The hosted voice is used when this session really has the app's speaker and a
        key is configured - unless the owner has asked for the local voice only, which
        is the shipped default now: it is faster to the first word (0.13 s against 1.6 s)
        and it cannot alternate with a second voice inside one answer.

        Anything still queued by the *previous* request is dropped first. One task at a
        time includes its voice: the old request's queued sentences used to sit in the
        stream the new request adopted, so a new command started by finishing the old
        answer out loud - two jobs heard as one.
        """
        if self.speaker is None:
            return None
        self._speech_gen += 1
        stale, self._voice_stream = self._voice_stream, None
        if stale is not None and not stale.cancelled:
            log.event("speech_superseded", spoken=len(self._spoken))
            stale.cancel()
        hosted = (isinstance(self.speaker, Speaker) and _config.cloud_available()
                  and not local_voice_only())
        self._voice_stream = VoiceStream(speaker=self.speaker, on_level=self._on_level,
                                         device=getattr(self.speaker, "device", None),
                                         allow_cloud=hosted, on_idle=self._on_speech_idle,
                                         trace=trace)
        self._spoken = []
        return self._voice_stream

    def _on_speech_idle(self):
        """The voice has really stopped - so the colour stops with it, at once.

        Called from the speech stream's own thread the moment its last sentence has
        finished playing. Until this existed the only way back from green was the
        watchdog, which looks twice a second and then waits for a further 0.4 s of
        quiet: the answer ended and the orb stayed lit for two or three seconds with
        nothing behind it, which is exactly what the owner described. Nothing running
        means the microphone is ours again; a request still in flight goes back to
        working, not to idle.
        """
        task = self.current_task
        running = task is not None and not task.done.is_set() and not task.superseded
        if self._state not in (SPEAKING, WORKING):
            return                      # already idle, or asleep/offline: nothing to undo
        log.event("speech_idle", running=running, held=round(time.time() - self._state_since, 1))
        if running:
            self._set_state(WORKING, phase=states.EXECUTING)
        else:
            self._set_state(LISTENING, "listening for a command", phase=states.IDLE)

    def _cancel_speech(self):
        """Stops everything spoken for the running request and drops what was queued.

        The generation moves on with it, so a line still being composed somewhere -
        the greeting, a "still working on it" written for a job that is already over -
        cannot arrive afterwards and be spoken into the next request.
        """
        self._speech_gen += 1
        stream, self._voice_stream = self._voice_stream, None
        if stream is not None:
            stream.cancel()
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception:
                pass
        self._spoken = []

    def _finish_speech(self, timeout=None):
        """Waits for this request's speech, and never leaves a stream playing.

        If the stream is still talking when the wait runs out, it is *cancelled* and
        not merely dropped. A dropped stream keeps playing on its own thread while the
        next request's first line goes to the SAPI queue, which is two voices at once -
        the hosted one and the local one, overlapping. That is the overlap the user
        heard, and it can only happen if a stream is allowed to outlive the session's
        reference to it.
        """
        stream, self._voice_stream = self._voice_stream, None
        if stream is None:
            return
        if stream.cancelled:
            return                      # a stop already owns this stream: nothing to cut
        if not stream.finish(timeout=timeout):
            log.event("speech_tail_cut", timeout=timeout, spoken=len(self._spoken))
            stream.cancel()

    def say_step(self, text):
        """A `say` step from a plan. Same route as every other spoken line: one
        stream per request, so segments keep their order and never overlap.

        `gen` is read at the moment the step runs, not when the plan was written: a
        step that belongs to a request that has since been replaced is dropped instead
        of being spoken into the one that replaced it.
        """
        return self._stream_or_speaker(text, gen=self._speech_gen)

    def _stream_or_speaker(self, text, gen=None):
        """Speaks one line, and writes it down as said.

        The list is what the reply is checked against before it is spoken at the end,
        so it has to hold everything that reached the voice - a plan's `say` line, the
        sentences of a streamed answer, and an `answer` step read out in pieces - not
        only the sentences the chat path handled.

        `gen` is the moment the line was written for. A line from an older one is
        dropped: it belongs to a request that is over, and speaking it into the one
        that replaced it is how a single question came out as several answers.
        """
        if not text:
            return False
        if gen is not None and gen != self._speech_gen:
            log.event("speech_dropped", chars=len(str(text)))
            return False
        stream = self._voice_stream
        if stream is None or stream.cancelled:
            # No request stream to keep this line in order with, so `speak_async` owns
            # it entirely - including the repeat check below, which it does not do twice.
            #
            # What it is not allowed to skip is writing it down. `_spoken` is what the
            # reply is checked against before it is spoken at the end of the request, and
            # a line routed this way used to be said and left out of the list - so an
            # answer whose *streamed* sentences arrived after a barge had already cleared
            # the stream was spoken a second time, in full, by the local voice. That is
            # the repeated line the owner hears, and it only ever showed up on this
            # machine because the echo barges in and clears that stream on every reply.
            if self.speak_async(text, gen=gen):
                self._spoken.append(str(text))
                return True
            return False
        if self._recently_said(text):
            log.event("speech_repeat_dropped", chars=len(self._normalise(text)))
            return False
        self._note_line(text)
        self._spoken.append(str(text))
        self._record_said(text)
        return stream.feed(text)

    def _record_said(self, text):
        """Remembers a line that reached the voice, with when it did.

        Only the echo guard reads this. `_spoken` cannot serve: it is cleared at the
        start of every request, and the echo of an answer arrives after the request
        that said it has finished.
        """
        line = " ".join(str(text or "").split())
        if line:
            self._said_recently.append((time.monotonic(), line))

    # ---------- one request -------------------------------------------------

    def handle_text(self, text, task=None):
        """Runs one line and returns its Result. Never raises."""
        return handle(text, confirm=self.confirm, fallback=self.fallback,
                      planner=self.planner, task=task, on_slow=self._thinking,
                      converse=self.converse, mode=self.mode,
                      on_sentence=self._speak_piece, on_work=self._work_started)

    def _writing_event(self, event):
        """One document event, on its way to the window. Never raises into a write."""
        payload = dict(event) if isinstance(event, dict) else {}
        self._note_document(payload)
        try:
            self._on_writing(payload)
        except Exception:
            pass

    def _note_document(self, event):
        """Remembers the document that is in flight, so a stop can keep its words.

        Nothing else in the session knows a document exists: the writing happens inside
        the agent, one section at a time. This is the one place that fact is kept, which
        is what lets "stop" answer with what was kept rather than with a bare word, and
        what lets the window's shelf know when to come out.
        """
        if not isinstance(event, dict):
            return
        kind = str(event.get("kind") or "")
        if kind in ("brief", "begin", "resume"):
            self._document = {"title": str(event.get("title") or ""),
                              "total": int(event.get("total") or 0),
                              "done": int(event.get("resumed") or event.get("done") or 0),
                              "words": int(event.get("words") or 0),
                              "paused": kind == "resume"}
        elif kind == "section_done" and isinstance(self._document, dict):
            self._document["done"] = int(event.get("index") or self._document["done"])
        elif kind == "paused":
            self._document = {"title": str(event.get("title") or ""),
                              "total": int(event.get("total") or 0),
                              "done": int(event.get("done") or 0),
                              "words": int(event.get("words") or 0), "paused": True}
        elif kind == "done":
            self._document = None

    def _stop_reply(self):
        """What to say when a request is stopped - and what of it was kept.

        A stopped document used to be thrown away, and the stop line said nothing about
        it. Now the finished sections are in a draft, so the line names it: that is the
        difference between "your work is gone" and "your work is here, say 'carry on
        writing'".
        """
        document = self._document if isinstance(self._document, dict) else {}
        done, total = int(document.get("done") or 0), int(document.get("total") or 0)
        title = str(document.get("title") or "").strip()
        if done and total and title:
            return (f"Stopped - I've kept '{title}' with {done} of {total} sections. "
                    f"Say 'carry on writing' and I'll finish it.")
        return "Stopped."

    def _speak_piece(self, sentence):
        """One finished sentence of a streamed reply: queued at once, in order.

        This is the whole point of the streaming pipeline: the first sentence is
        audible while the model is still writing the second.
        """
        if not sentence:
            return
        self._set_phase(states.SPEAKING)
        self._last_audible = time.monotonic()      # quieter than this is 'said nothing'
        # `gen` is read now, not when the sentence was written. A streamed answer is
        # spoken sentence by sentence, and a request that is superseded mid-answer used
        # to keep pouring sentences into the *new* request's voice - which is how one
        # question came out as two answers, the second of them unrelated.
        self._stream_or_speaker(sentence, gen=self._speech_gen)

    def _control_word(self, text):
        """True when a line steers the request that is running, without the wake word.

        Deliberately narrow, and deliberately only while a request is in flight: the
        three things a person says to something already working - stop it, carry on,
        what are you doing - plus nothing else. A sentence that is merely *said* in
        the room is still ignored.
        """
        task = self.current_task
        if task is None or task.done.is_set():
            return False
        return is_control(text)

    def _work_started(self, text):
        """One short line the moment a job starts.

        The complaint was a five-to-eight second silence followed by an answer: the
        model writes its plan, and nothing at all is heard until it lands. A job that
        is going to take work - a multi-step request, a UI flow, a document - now gets
        an immediate acknowledgement, while a one-step command keeps its silence and
        its instant result. The words are written down rather than composed, because
        the whole point of this line is that it costs nothing and arrives at once.
        """
        if not describe_work(text):
            return
        line = progress.acknowledgement()
        self._progress(line)
        # The label always goes to the window; the voice only speaks when nothing has
        # been said for a while. An acknowledgement that lands on top of a reply is
        # the interruption the owner asked to be rid of.
        if self.may_fill_silence():
            self.speak_async(line, gen=self._speech_gen)

    def _thinking(self):
        """Called as the request starts: the phase, and the label by the circle.

        Nothing is *said* here any more. The written "Let me think about that" was
        there because a 1B model took half a minute to plan; with a real brain the
        answer is already being written and spoken, and a line in front of it is
        filler of exactly the kind the user asked to be rid of. What replaces it is
        a visible label - words on screen, not in the voice - and, if the wait turns
        out to be real, one line the model writes about what it is actually doing.
        """
        self._set_phase(states.PLANNING)
        self._progress(progress.planning_label([]))
        self._log("thinking ...")

    def _speak_written(self, purpose, detail="", fallback=""):
        """Speaks a line the brain writes for the moment, off the calling thread.

        Writing the line is a model round trip, so it happens on a worker: the voice
        loop must never sit and wait for a sentence *about* waiting. Without a real
        brain, or when the call fails, the caller's written line is the fallback.
        """
        if not self.converse or self.planner is None:
            if fallback:
                self.speak_async(fallback, gen=self._speech_gen)
            return

        # The moment this line is being written for, taken before the model round trip
        # rather than after it. The greeting is the reason: it is asked for the instant
        # the wake word lands and takes about a second to write, and if the owner is
        # already talking by then, the line belongs to a moment that has passed - it is
        # dropped instead of arriving as a second answer, three seconds late.
        gen = self._speech_gen

        def work():
            text = self.planner.line(purpose, detail=detail, fallback=fallback)
            if text:
                self.speak_async(text, gen=gen)

        threading.Thread(target=work, daemon=True).start()

    def ask(self, text):
        """Typed command for a caller that wants the reply back (console, tests)."""
        trace = diag.new(kind="typed", origin="typed", label="typed command")
        task = self._begin(text, trace)
        if trace is not None:
            diag.activate(trace)
        try:
            return self._work(task, text)
        finally:
            self._end(task)
            if trace is not None:
                diag.deactivate()

    def submit(self, text):
        """Queues a typed command for the listening loop to run next."""
        self._typed.put(text)

    def _setting_command(self, text):
        """Modes, listen-after-Retro and background hearing, by voice or typed.

        Handled here instead of being left to the planner, because all three change how
        the assistant itself behaves: a request to stop listening for follow-ups has
        to work even when the model is unreachable, and it has to take effect before
        the next thing that is heard. The phrases are matched whole, in core/modes.py,
        so ordinary sentences that merely contain the words change nothing.
        """
        # "Retro, switch to code" opens the coding discussion, and nothing else:
        # no model turn, no plan, no build. The handoff module carries the event to
        # whichever window is running (a console run simply has none).
        if modes.parse_code_switch(text):
            log.event("code_switch_command", source="voice")
            handoff.offer("")
            # The window opens on its own, but the person is waiting for an answer:
            # this is a setting, so it gets the same one-line acknowledgement as every
            # other setting here instead of opening in silence.
            if self.converse and self.planner is not None:
                self._speak_written("setting", detail="the coding discussion is open",
                                    fallback="Opening the coding discussion.")
            else:
                self.speak_async("Opening the coding discussion.")
            return True
        wanted = modes.parse(text)
        if wanted is not None:
            self.set_mode(wanted, save=True)
            log.event("mode_command", mode=wanted, source="voice")
            if self.converse and self.planner is not None:
                self._speak_written("setting", detail=f"the mode is now {wanted}")
            else:
                self.speak_async(modes.describe(wanted))
            return True
        state = modes.parse_followup(text)
        if state is not None:
            self.set_followup(state, save=True)
            log.event("followup_command", on=state, source="voice")
            line = ("I'll keep listening for follow-ups without the wake word." if state
                    else "I'll wait for you to say Retro each time.")
            if self.converse and self.planner is not None:
                self._speak_written("setting", detail=f"follow-up listening is {state}")
            else:
                self.speak_async(line)
            return True
        mic = modes.parse_mic(text)
        if mic is not None:
            self.set_mic(mic, save=True)
            log.event("microphone_command", on=mic, source="voice")
            line = ("I'm listening again." if mic
                    else "Microphone off. Type if you need me.")
            if self.converse and self.planner is not None:
                self._speak_written("setting", detail=f"the microphone is {'on' if mic else 'off'}")
            else:
                self.speak_async(line)
            return True
        if modes.parse_repeat(text):
            log.event("repeat_command", chars=len(self._last_reply))
            self.repeat_last()
            return True
        if modes.parse_diagnostics(text):
            # The report is the panel's job; the voice gets the one line that answers
            # "why was that slow", and the window is asked to put the rest on screen.
            log.event("diagnostics_command", traces=len(diag.recent()))
            self._on_progress("latency report")
            self.speak_async(diag.spoken_summary())
            ui_hook.emit("show_diagnostics")
            return True
        visual = modes.parse_background(text)
        if visual is not None:
            self.set_background(visual, save=True)
            log.event("background_command", on=visual, source="voice")
            line = ("I'll come alive to whatever this machine plays while I'm asleep."
                    if visual else "I'll sit still and grey while I'm asleep.")
            if self.converse and self.planner is not None:
                self._speak_written("setting", detail=f"background hearing is {visual}")
            else:
                self.speak_async(line)
            return True
        return False

    def dispatch(self, text, woke=False, trace=None):
        """Runs a request on its own thread, or steers the one already running.

        `trace` is the diagnostics record for this interaction. A spoken command
        arrives with one already carrying its capture and transcription timings; a
        typed one gets a fresh record here, so both kinds appear in the same history.
        """
        text = " ".join(str(text or "").split())
        if not text:
            return None
        if trace is None:
            trace, self._next_trace = self._next_trace, None
        if trace is None:
            trace = diag.new(kind="typed", origin="voice" if woke else "typed",
                             label="typed command" if not woke else "command")
        route_started = diag.now_ns()
        if self._duplicate_ask(text):
            # The same request, again, inside a few seconds: a split block from the
            # recogniser, or our own answer coming back round the speakers. Nothing is
            # run, and - more to the point - nothing is said twice.
            self._progress("already on it")
            self._record_trace(trace, ok=True, label="duplicate")          
            return None
        if self._setting_command(text):
            if trace is not None:
                trace.label = "setting"
            self._record_trace(trace, ok=True)
            return None
        if trace is not None:
            trace.add("route", diag.now_ns() - route_started)
        running = self.current_task
        if running is not None and not running.done.is_set() and not running.superseded:
            if trace is not None:
                trace.label = "steer"
                self._record_trace(trace, ok=True)
            return self.steer(running, text, woke=woke)
        task = self._begin(text, trace)
        log.event("dispatch", chars=len(text), woke=woke, text=text[:40])
        threading.Thread(target=self._serve, args=(task, text), daemon=True).start()
        return task

    def _record_trace(self, trace, ok=True, label=""):
        """Files a trace that never became a request, so it is not simply lost."""
        if trace is None:
            return None
        if label:
            trace.label = label
        trace.finish(ok=ok)
        return diag.record(trace)

    def steer(self, task, text, woke=False):
        """Something was said while a request was running: stop, carry on, or switch.

        Without the wake word only the control words count, so the assistant does
        not act on a conversation it happens to overhear.
        """
        if is_stop(text):
            log.event("task_stopped", by="user", seconds=round(task.elapsed(), 1))
            task.cancel("user")
            # Everything goes: the task, the sentence being said, and the queue of
            # sentences behind it. `_speaker_stop` alone only stopped the local voice -
            # the hosted stream kept talking - which is why "stop" seemed to do nothing
            # until the whole answer had played out.
            self._cancel_speech()
            # And Retro is listening for what comes next: the follow-up window opens
            # instead of closing, so the next sentence does not need the wake word.
            self._follow_until = time.time() + CONVERSATION_WINDOW_S
            # One word, said. The fuller sentence goes to the window only - a stop that
            # answers with a paragraph is the opposite of what a stop is for.
            self._reply_and_say(task, self._stop_reply(), False, speak="Got it.")
            return task
        if self._setting_command(text):
            return task
        if is_continue(text):
            self._log("carrying on")
            self.speak_async("Carrying on.")
            task.touch()
            return task
        if is_status(text):
            self._say(task.detail or "Still working on it.")
            return task
        if not woke and not self._stay_awake():
            if time.time() - task.note_at > 20:      # once is enough, not every sentence
                task.note_at = time.time()
                self._log(BUSY_NOTE)
                self.speak_async(BUSY_NOTE)
            return task
        # The owner is talking to Retro while it works, so this request replaces the
        # one running: one task at a time, cancelled rather than left to finish in the
        # background. Nothing is said about the switch - the new work starting is the
        # only reply that matters.
        log.event("task_pivot", seconds=round(task.elapsed(), 1), text=task.text[:40])
        task.supersede()
        self._cancel_speech()                # the old answer stops mid-sentence
        return self.dispatch(text, woke=True)

    def _begin(self, text, trace=None):
        task = Task(text)
        # The task carries its own diagnostics record, so every path that answers for
        # this request - the reply, a stop, the watchdog - finishes the same trace.
        task.trace = trace if trace is not None else diag.new(
            kind="typed", origin="typed", label="typed command")
        with self._task_lock:
            previous = self.current_task
            if previous is not None and not previous.done.is_set():
                previous.supersede()
            self.current_task = task
            self.tasks.append(task)
        self._last_progress_detail = ""
        return task

    def step_progress(self, name, args):
        """One line per real step: the label beside the circle, and nothing spoken."""
        label = progress.label_for(name, args)
        self._progress(label)
        task = self.current_task
        if task is not None:
            task.last_progress = time.time()
            task.detail = f"{name} {args.get('name') or args.get('app') or ''}".strip()
            # Only a step that changes something on the machine is "working". A
            # step that merely speaks the answer is conversation, and a reminder
            # about conversation has nothing to report.
            if str(name) not in SPEECH_TOOLS:
                task.did_work = True

    def _serve(self, task, text):
        trace = getattr(task, "trace", None)
        if trace is not None:
            # The model client and the router report through the thread's current
            # trace, so a request's own thread is what makes them meet.
            diag.activate(trace)
        try:
            self._work(task, text)
        finally:
            self._end(task)
            if trace is not None:
                diag.deactivate()

    def _work(self, task, text):
        trace = getattr(task, "trace", None)
        self._set_phase(states.GENERATING, "working")
        self._begin_speech(trace)           # one ordered voice for this request
        self._document = None               # no stale document from the last request
        started = time.time()
        try:
            draft = compose.load_draft() if compose.is_resume(text) else None
            if compose.is_readback(text) and self.converse and self.planner is not None:
                # Hearing back something already written is not a tool step either: the
                # words are on disk, so there is nothing to plan - it is read out, in
                # order, a sentence at a time, from the same speech stream as everything
                # else.
                log.event("readback_route", chars=len(text))
                result = self._read_back(task=task)
            elif draft is not None and self.converse and self.planner is not None:
                # Carrying on with a document that was stopped: the length and the shape
                # are already settled, so it is picked up where it stopped instead of
                # being asked about again from the beginning.
                log.event("compose_resume", done=len(draft.get("sections") or []))
                result = self.planner.compose(draft.get("request") or text, task=task,
                                              resume=draft)
            elif (builder.wants_project(text) and self.converse
                  and self.planner is not None and builder.looks_big(text)
                  and handoff.configured()):
                # Too big for one silent pass of files - a backend, a framework, or a
                # change to code that already exists. It is not refused and it is not
                # guessed at: the discussion opens with the request already in it, so
                # the decisions are made before anything is written.
                log.event("coding_discussion_offered", chars=len(text))
                handoff.offer(text)
                result = Result(True, "This one is worth talking through first. I've "
                                      "opened a coding discussion with your request in "
                                      "it - settle the details there, then type "
                                      "Retro Code and I'll build it.")
            elif builder.wants_project(text) and self.converse and self.planner is not None:
                # A coding request is its own path, and it is silent on purpose: the
                # folder and the files are made first, and the editor is opened on the
                # finished project - never in the middle of it. See core/builder.py.
                log.event("build_route", chars=len(text))
                result = self.planner.build(text, task=task, on_step=self.step_progress)
            elif compose.wants_document(text) and self.converse and self.planner is not None:
                # Writing something long is not a tool step that returns a wall of
                # text at the end. It gets its own path: a brief with the user, then
                # the document written section by section, spoken as it is written.
                log.event("compose_route", chars=len(text))
                result = self.planner.compose(text, task=task, ask=self._ask_brief)
            else:
                result = self.handle_text(text, task=task)
        except Stopped:
            result = Result(False, "Stopped.")
        except Exception as exc:            # a bug must never end the session
            log.event("task_crashed", error=f"{type(exc).__name__}: {exc}")
            self._log(f"something went wrong ({type(exc).__name__})")
            result = Result(False, "Something went wrong inside me. I'm still listening.")
        log.event("task_end", seconds=log.since(started), steps=task.steps,
                  ok=result.ok, cancelled=task.cancelled)
        if trace is not None:
            trace.model = self._model_name()
            trace.meta["chars"] = len(text)
        if task.superseded or task.replied:
            self._finish_speech()           # a newer request, or a stop, owns the reply
            return result
        self._reply_and_say(task, result.message, result.ok)
        return result

    def _mark_play_end(self, trace=None):
        """Closes the audible part of the trace, if any sound was played for it."""
        target = trace if trace is not None else diag.current()
        if target is not None and "play_start" in target.marks:
            target.mark("play_end")

    def _model_name(self):
        """What wrote the last reply, for the diagnostics record - a name, no text."""
        if not self.converse:
            return "local"
        return (getattr(self.fallback, "answered_by", None)
                or getattr(self.fallback, "model", None) or "cloud")

    def _reply_and_say(self, task, message, ok, speak=None):
        """The reply for one request, spoken exactly once.

        When the answer was written by the model it has already been spoken sentence
        by sentence while it was still being written; saying the whole thing again
        afterwards is precisely the duplication the user heard, so the text is shown
        and not repeated. A plan's result - a tool's own summary, say - has not been
        spoken yet, and that is what `_say` handles.

        `speak` overrides *what is said* while `message` is what the window shows. A
        stop says "Got it." and still reports the longer sentence in text, because a
        spoken paragraph after "stop" is the bug this whole function exists to avoid.
        """
        task.replied = True
        task.failed = not ok
        if str(message or "").strip():
            self._last_reply = str(message)
        self._on_reply(message, ok)
        self._finish_speech(timeout=_config.SPEECH_QUEUE_MAX_S)
        self._mark_play_end()
        # The conversation stays open after a reply, for as long as the owner asked:
        # this is the window `_stay_awake` reads and the loop re-reads every pass.
        self._follow_until = time.time() + CONVERSATION_WINDOW_S
        if speak is None and self._already_spoken(message):
            self._set_phase(states.COMPLETED if ok else states.FAILED)
            return
        self._say(speak if speak is not None else message)
        self._mark_play_end()
        self._set_phase(states.COMPLETED if ok else states.FAILED)

    def _already_spoken(self, message):
        """True when this reply has already been said through the speech stream.

        Asked from both ends, because the words reach the voice by two routes: a
        streamed answer is spoken as it is written and then handed back here as the
        reply, and a plan's last step speaks its own sentence before the summary
        arrives. Either way the same words must not come out twice - which is what
        the user heard as the assistant repeating itself.

        Compared as *speech*, not as text: both sides go through `_normalise`, which
        strips the markdown and the punctuation the voice never says. Comparing the
        raw strings is what let the live log play a whole answer twice - the streamed
        copy had already been cleaned on its way to the voice, so the uncleaned reply
        arriving behind it matched nothing here, was not recognised as already spoken,
        and was read out again by the local voice.
        """
        want = self._normalise(message)
        if not want:
            return False
        for line in self._spoken:
            if self._normalise(line) == want:
                return True
        said = self._normalise(" ".join(self._spoken))
        if not said:
            return False
        return said in want or want in said

    def repeat_last(self):
        """Says the last reply again, word for word.

        It goes straight to the speaker rather than through `_say` or `speak_async`,
        because both of those refuse a line that was said a moment ago - which is
        precisely the line being asked for. Asking twice has to work twice, so this
        does not add to `_said_recently` either; it is a reply to the owner, not
        Retro hearing itself.
        """
        text = str(self._last_reply or "").strip()
        if not text:
            self.speak_async("I haven't said anything yet.")
            return False
        if self.speaker is None:
            return False
        log.event("repeat_reply", chars=len(text))
        self._set_state(SPEAKING, phase=states.SPEAKING)
        self._last_audible = time.monotonic()
        try:
            with self._speech_lock:
                self.speaker.say(text)
        except Exception as exc:                  # a broken voice must not end the session
            log.event("speech_failed", error=type(exc).__name__)
            self._log(f"could not speak ({type(exc).__name__})")
        finally:
            if self._state == SPEAKING:
                self._set_state(WORKING if self.current_task is not None else LISTENING,
                                "listening for a command")
        return True

    def _speaker_stop(self):
        if self.speaker is not None:
            self.speaker.stop()

    def _end(self, task):
        task.finish()
        trace = getattr(task, "trace", None)
        if trace is not None and trace.running:
            trace.finish(ok=not task.cancelled and not task.failed, cancelled=task.cancelled)
            diag.record(trace)
        with self._task_lock:
            if self.current_task is task:
                self.current_task = None
                idle = True
            else:
                idle = False
        if idle and not task.superseded and self._state != OFFLINE:
            self._set_phase(states.IDLE, "listening for a command")

    # ---------- speaking ----------------------------------------------------

    def _say(self, text):
        """Speaks one line. Never blocks longer than the line itself can last."""
        if self.speaker is None or not text:
            return
        if self._recently_said(text):
            # The same line, spoken again within the window: dropped. This is what
            # turned "opening my settings" three times into once, whatever route the
            # second and third copies arrived by.
            log.event("speech_repeat_dropped", chars=len(self._normalise(text)))
            return
        self._note_line(text)
        self._spoken.append(str(text))
        self._record_said(text)
        self._set_state(SPEAKING, phase=states.SPEAKING)
        self._last_audible = time.monotonic()      # this is what "quiet" is measured from
        try:
            stream = self._voice_stream
            if stream is not None and not stream.cancelled:
                stream.feed(text)
                stream.finish(timeout=_config.SPEECH_QUEUE_MAX_S)
            else:
                # No request is running, so there is no stream to hold this line in
                # order. The session's speech lock does it instead - one line in the
                # air at a time - and the line is spoken here, never handed to a
                # thread nobody owns. That thread was a second voice, and it could be
                # overtaken by the next request's answer, which is what "the voices
                # still overlap" was.
                with self._speech_lock:
                    heard = self.speaker.say(text)
                cut = bool(getattr(self.speaker, "last_cut", False))
                if not heard and not cut:
                    # Nothing came out. Saying so is the difference between "it spoke and
                    # I heard nothing" and the user being told where to fix it; the
                    # speaker has already moved the next line to the system default.
                    # A line that was *stopped* is not this: the live log had a reply
                    # cut off by a barge reported as `reply_unheard` with the advice to
                    # check the OUTPUT picker, about a line the owner had just heard.
                    log.event("reply_unheard", chars=len(text),
                              device=getattr(self.speaker, "device", None))
                    self._progress("no sound came out - check OUTPUT")
                    self._log("nothing came out of the output device - check OUTPUT")
        except Exception as exc:                  # a broken voice must not end the session
            log.event("speech_failed", error=type(exc).__name__)
            self._log(f"could not speak ({type(exc).__name__})")
        finally:
            if self.speaker is not None and self.speaker.disabled:
                self._log("speech stopped working - restart Retro to get the voice back")
            if self._state == SPEAKING:
                self._set_state(WORKING if self.current_task is not None else LISTENING,
                                "listening for a command")

    def speak_async(self, text, gen=None):
        """Speaks without waiting, so it can cover a slow model call.

        While a request is running this goes through that request's speech stream,
        which is what guarantees the order of what is said - a line handed to the
        SAPI queue while the streamed reply is playing would be a second voice
        talking over the first.

        With no request running there is no stream to keep a line in order, so it is
        spoken here and now under the session's speech lock. One line at a time, and
        never on an unowned thread: that thread could not be stopped, could not be
        waited for, and could be heard underneath the next answer.

        `gen` is the moment the line was written for; a stale one is dropped.
        """
        if self.speaker is None or not text:
            return False
        if gen is not None and gen != self._speech_gen:
            log.event("speech_dropped", chars=len(str(text)))
            return False
        if self._recently_said(text):
            log.event("speech_repeat_dropped", chars=len(self._normalise(text)))
            return False
        self._last_audible = time.monotonic()
        self._record_said(text)
        # The orb goes green for anything that is really being said, whether it was
        # handed to a request's stream or spoken here. It used to be set by `_say`
        # alone, so the greeting and every streamed sentence were spoken with the
        # circle still blue - which is the whole of "it is speaking but it never went
        # green". The watchdog puts it back when the audio has stopped.
        self._set_phase(states.SPEAKING)
        stream = self._voice_stream
        if stream is not None and not stream.cancelled:
            self._note_line(text)          # committed: it is going into the stream now
            return stream.feed(text)
        with self._speech_lock:
            if gen is not None and gen != self._speech_gen:
                return False               # a request started while we waited for the lock
            self._note_line(text)
            try:
                return bool(self.speaker.say(text))
            except Exception as exc:       # a broken voice must not end the session
                log.event("speech_failed", error=type(exc).__name__)
                return False

    def test_voice(self, text="Yes, this is Retro speaking on the output you chose."):
        """Speaks a check line and reports what *really* happened, not what was hoped.

        Returns (peak, mixer peak). The first number is the level of the line as it was
        rendered - the number this used to quote on its own, which is identical whether
        the audio reached the speakers or a switched-off Bluetooth headset. The second
        is the loudest the Windows mixer saw this process make while the line played,
        which is the only available answer to "did anybody hear that". None means the
        mixer could not be read, so audibility is unknown - and unknown is reported as
        unknown, never as success.
        """
        if self.speaker is None:
            return None, None
        try:
            played, mixer_peak = self.speaker.audible(text)
        except Exception as exc:
            log.event("voice_test_failed", error=type(exc).__name__)
            return None, None
        if not played:
            log.event("voice_test_failed", device=getattr(self.speaker, "device", None))
        log.event("voice_test", peak=round(float(self.speaker.last_peak), 3),
                  mixer=None if mixer_peak is None else round(mixer_peak, 4), played=played)
        return self.speaker.last_peak, mixer_peak

    def _ask_brief(self, question="", detail="", timeout_s=9.0):
        """Listens for one short answer about the shape of a document.

        The question itself is already spoken by the time this is called - it was
        written for the moment and handed to the same voice stream as everything else
        - so this only waits, and only briefly. No microphone, no answer, or a
        mis-heard one all end the same way: the writing goes ahead with the length
        Retro proposed, because a request that hangs waiting for an answer that is
        never coming is worse than a document of the wrong length.
        """
        if self.ears is None:
            log.event("brief_no_microphone")
            return ""
        self._set_phase(states.WAITING_USER, "waiting for your answer")
        self._follow_until = time.time() + FOLLOWUP_WINDOW_S
        try:
            heard = listen_once(self.ears, timeout_s=timeout_s, gate=self._mic_gate,
                                on_barge=self._barged, device=self._input_device())
        except Exception as exc:
            log.event("brief_failed", error=type(exc).__name__)
            return ""
        log.event("brief_heard", chars=len(heard or ""))
        return heard or ""

    def _read_back(self, task=None):
        """Reads the last document Retro wrote back out loud, sentence by sentence.

        Nothing is planned and nothing is decided: the text is on disk, so this says it.
        It goes through the ordered speech stream one finished sentence at a time - the
        same route a reply takes - and passes a cancellation checkpoint between
        sentences, so "stop" three sentences into a long document actually stops it.
        """
        from .tts import SentenceStream
        document = compose.latest_document()
        if document is None:
            return Result(True, "I haven't written anything yet - ask me to write something "
                                "and then I can read it back to you.")
        prose = compose.readable(document["text"])
        words = compose.count_words(prose)
        log.event("read_back", file=document["name"], words=words)
        self._progress(f"reading {document['name']}")
        splitter = SentenceStream()
        for sentence in splitter.feed(prose) + splitter.flush():
            if task is not None:
                task.check()                     # a stop is heard between sentences
            if sentence:
                self._speak_piece(sentence)
        return Result(True, f"That was '{document['title']}', about {words} words "
                            f"({document['name']}).")

    def ask_user(self, question, timeout_s=8.0):
        """Asks out loud and listens for a yes or a no.

        The windowed build has no console, so every sensitive action would otherwise
        be answered by a `input()` that cannot read anything and always refuses.
        """
        self._say(question + " Say yes or no.")
        if self.ears is None:
            return False
        try:
            heard = listen_once(self.ears, timeout_s=timeout_s, gate=self._mic_gate,
                                on_barge=self._barged, device=self._input_device())
        except Exception as exc:
            log.event("confirm_failed", error=type(exc).__name__)
            return False
        log.event("confirm_heard", answer="yes" if says_yes(heard) else "no")
        return says_yes(heard)

    # ---------- the watchdog ------------------------------------------------

    def _start_watcher(self):
        if self._watcher is not None:
            return
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self):
        """Keep the display honest, and talk while a long request runs.

        Both jobs come from the same afternoon of testing. The ring stayed green and
        nothing could be asked again until the app was restarted; and a request that
        took a while produced no sound at all, so it read as frozen rather than busy.
        """
        while not self._stop.is_set():
            # Four looks a second, not two. The stream itself reports the end of a
            # reply now (`_on_speech_idle`), so this is the backstop for the paths that
            # have no stream - a line spoken with no request behind it - and a backstop
            # that is half a second late is a backstop that looks like the bug.
            time.sleep(0.25)
            try:
                self._watch_once()
            except Exception as exc:                 # the watchdog must never die
                log.event("watchdog_error", error=type(exc).__name__)

    def _report_microphone_silence(self):
        """Says so, once, when the microphone is on and carrying nothing.

        The failure this exists for has no symptom of its own. The stream opens, the
        blocks arrive on time, every one of them is the room at a level far below
        speech - and the assistant never hears anything again. That looks exactly like
        a user who is not talking, so nothing was logged and nothing was shown, and a
        microphone that Windows had muted, or that a driver had left with no gain, went
        on being waited for indefinitely. Half a minute of that is not a pause in a
        conversation, it is a diagnosis: it is written to the log with the device it is
        about, and it is put on the hint line where the owner will see it. The first
        block above the floor withdraws it.
        """
        if not self.mic_on or not self._capturing or self._mic_silent_reported:
            return
        waited = time.monotonic() - self._mic_heard_at
        if waited < MIC_SILENT_S:
            return
        self._mic_silent_reported = True
        holders = _mic_holders()
        log.event("microphone_silent", seconds=round(waited, 1),
                  device=self._input_device(), voice=self._voice_wanted,
                  apps=holders)
        if holders:
            who = holders[0] + (f" and {len(holders) - 1} more" if len(holders) > 1 else "")
            # Short enough for the hint line, which cuts at 44 characters.
            self._on_progress(f"{who} is using the mic - close it")
        else:
            self._on_progress("the microphone is hearing nothing - check it is not muted")

    def _watch_once(self):
        self._report_microphone_silence()
        if self._dormant():
            return                # asleep is inert: there is no state to recover
        task = self.current_task
        if task is None or task.done.is_set():
            held = time.time() - self._state_since
            # Green with nothing coming out of the speakers is not speaking: a line
            # handed to `speak_async` has no request behind it and no caller waiting,
            # so this is the only thing that puts the circle back. Half a second of
            # silence after the last word, and it is listening again - rather than the
            # twelve seconds it used to sit there green for. A streamed reply does not
            # wait for this at all: it says so itself, the moment the audio stops.
            finished = (self._state == SPEAKING and not self._playing()
                        and self.quiet_for() is not None and self.quiet_for() > 0.25)
            if finished or (self._state in (WORKING, SPEAKING) and held > self.state_stuck_s):
                # No request is running, so a held colour can only be a line that
                # never came back. Hand the microphone back. `playing` and `quiet` are
                # here because "held 2.8" says the colour stuck and nothing about why:
                # a player that still claims to be playing and a silence nobody counted
                # from look the same from the outside, and the live log had both.
                log.event("state_recovered", state=self._state, held=round(held, 1),
                          playing=self._playing(), quiet=self.quiet_for(),
                          finished=finished)
                self._speaker_stop()
                self._set_state(LISTENING, "listening for a command")
            return
        detail = (task.detail or "").strip()
        # Three conditions, all about not being a nuisance: only twice per task, only
        # when what is happening has actually changed, and only when a real step has
        # run. The old version spoke on a timer, which is how "I am still working on
        # it" came out three times in a row about nothing new; and a question being
        # answered - an `answer` step, or the chat route - is not execution, so it is
        # never covered by a reminder either. An offline brain says nothing here.
        interesting = detail and detail != self._last_progress_detail
        quiet_enough = self.may_fill_silence()
        if (task.did_work and task.quiet_for() > self.progress_every_s
                and task.progress_lines < 2 and interesting and quiet_enough):
            task.progress_lines += 1
            self._last_progress_detail = detail
            log.event("progress_line", seconds=round(task.elapsed(), 1),
                      detail=detail[:40])
            self._speak_written("working", detail=detail, fallback="")
        elif interesting and task.did_work and not quiet_enough:
            # It has said something within the last few seconds, so the label goes to
            # the window and nothing at all is spoken: the owner's rule, in as many
            # words - no "still working on it" on top of an answer.
            log.event("progress_line_held", seconds=round(task.elapsed(), 1),
                      quiet=None if self.quiet_for() is None else round(self.quiet_for(), 1))
        if task.elapsed() > self.task_timeout_s:
            log.event("task_timeout", seconds=round(task.elapsed(), 1), steps=task.steps)
            task.cancel("timeout")
            self._follow_until = 0.0
            self._speaker_stop()
            task.replied = True
            self._on_reply("That took too long, so I stopped. Ask me again.", False)
            self.speak_async("That took too long, so I stopped.")
            with self._task_lock:
                self.current_task = None
            self._set_state(LISTENING, "listening for a command")

    # ---------- the microphone ----------------------------------------------

    def _mic_level(self, rms):
        """Scales raw microphone loudness into the display's 0..1 range.

        The two phases the microphone can prove are reported from here: a phrase is
        being recorded while the input is loud, and transcription is what follows
        when it goes quiet again. Both are read off the audio itself rather than
        assumed from the loop's position.

        It is also where "that block arrived while Retro was talking" is written down.
        Every block reaches here whether or not it passed the gate, so this is the one
        place that can tell a transcript of Retro's own voice from a person's - which
        is what `_heard_ourselves` reads to stop the loop in the log.
        """
        level = float(rms)
        if level > _config.START_RMS:
            # Heard, whatever happens next. This is the number the silence report is
            # measured against, so it is taken before anything may refuse to draw.
            self._mic_heard_at = time.monotonic()
            self._mic_silent_reported = False
        if self._dormant():
            # Asleep with background hearing off: the microphone is still being
            # listened to for the wake word, but the window is told nothing about it. A
            # sleeping orb that breathes with the room looks awake, and the owner asked
            # for it to sit still until it hears its name. The audibility bookkeeping
            # above still runs, because whether the microphone works is worth knowing
            # whether or not the picture is allowed to move.
            return
        if self._playing():
            self._heard_under_voice = True
        self._on_level(min(1.0, level * MIC_LEVEL_GAIN))
        loud = level > _config.START_RMS
        if loud and self.phase in (states.WAKE, states.IDLE):
            self._set_phase(states.RECORDING)
        elif not loud and self.phase == states.RECORDING:
            self._set_phase(states.TRANSCRIBING)

    def _stay_awake(self):
        """Whether the loop keeps taking commands without the wake word.

        Only when the user asked for it, and only for a while after the last reply.
        That window is what stops a television in the next room from keeping the
        microphone open all evening, and it is why an error or a stop closes the
        door immediately: a session policy that survives a failure is a session
        policy that listens when nobody meant it to.
        """
        return self.followup and time.time() < self._follow_until

    def _barged(self):
        """The owner started talking over Retro: cut the reply off, right there.

        This is the difference between a conversation and a broadcast. Without it the
        reply kept playing to the end while the microphone kept recording, so the
        transcript of the *user's* sentence only arrived afterwards - Retro finished
        its own sentence first and then answered words said half a minute earlier.
        Cancelled at the first block the mic could not attribute to the echo instead.
        """
        log.event("barge_in", state=self._state, phase=self.phase, playing=self._playing())
        # `_cancel_speech` only when there is really something to cancel. The gate is
        # also up for PLAY_TAIL_S after the last sentence has stopped, and a barge fired
        # in that tail cancelled the *next* line before it had started - the live log has
        # one at `state: listening, phase: transcribing`.
        if self._playing():
            self._cancel_speech()
            self._progress("listening")
        self._follow_until = time.time() + FOLLOWUP_WINDOW_S
        # The "heard under our own voice" flag is deliberately *not* cleared here any
        # more. It used to be, on the reasoning that a barge means a person at the
        # microphone - and that is not true on a machine with a loud echo, where the
        # echo itself barges. Keeping it keeps `_too_quiet_to_be_a_person` armed for the
        # rest of that phrase, which is the check that catches the fragment the barge
        # was really caused by.

    def _playing(self):
        """True while audio is really coming out of either voice."""
        stream = self._voice_stream
        if stream is not None and stream.playing:
            return True
        return self.speaker is not None and self.speaker.playing

    def _room_level(self):
        """The machine's own playback as a raw RMS, or None when nothing samples it.

        None and zero are different answers: no sampler means the room is unknown, and
        an unknown room must not gate the microphone at all - a gate that cannot be
        measured would either shut the assistant out or let everything through.
        """
        sampler = self.audience
        if sampler is None:
            return None
        try:
            level = float(sampler.level)
        except (TypeError, ValueError):
            return None
        return level / max(0.01, _config.AUDIENCE_GAIN)

    def _mic_gate(self):
        """How loud a block must be to count, as speech rather than as the room.

        Two things are excluded here, and both were being heard as commands.

        Retro's own voice: without this the microphone hears Retro, Whisper
        transcribes it, and the assistant starts answering itself. A user speaking
        over the voice is louder at the microphone than the echo is, so they still
        get through. Both playback paths are asked, the streamed one and the SAPI one
        it falls back to. The threshold itself is built in `_echo_threshold`, in the
        microphone's own units, and the level it is built from is the last one the
        player reported rather than the one it reports now - a player reads zero in
        every gap between two sentences, which is where the echo used to get in.
        Past the last sentence the same threshold holds for PLAY_TAIL_S, because the
        echo of a line outlives the line. The moment the output stops is remembered as
        well, because that is when the echo of the last answer arrives - see `_is_echo`.

        The machine's own playback is deliberately *not* part of this any more. The
        room sampler runs only while the microphone is off (see `_start_audience`), so
        with the microphone open there is no measured room to gate against - and the
        visualiser the owner wants is a microphone-off activity. The echo half above is
        all that is left, and it is the half that was ever about the microphone.
        """
        floor = _config.START_RMS * 2.0
        now = time.monotonic()
        if self._playing():
            self._output_was_playing = True
            played = self._played_level()
            if played > 0.0:
                # Remembered, not read at this instant: the player reports zero between
                # sentences, and that is the whole bug. Whatever it last reported still
                # describes the room it is filling.
                self._played_held = played
                self._played_until = now + PLAY_TAIL_S
            return self._echo_threshold()
        if self._output_was_playing:
            self._output_was_playing = False
            self._output_ended_at = now
        if now < self._played_until:
            # The tail: the last sentence has stopped playing but is still in the air.
            # This is the moment every bad transcript came from - a fragment of Retro's
            # own voice, transcribed and handed back as a three-word command.
            return self._echo_threshold()
        room = self._room_level()
        if room is not None and room >= _config.ROOM_GATE_FLOOR:
            return max(floor, room * _config.ROOM_GATE_FACTOR)
        return None

    def _echo_threshold(self):
        """How loud a block must be for a person rather than for Retro's own voice.

        The played level is a display number and the microphone is raw rms, so the
        conversion comes first: `played / PLAY_LEVEL_GAIN` is what the speakers are
        actually putting out, and PLAY_GATE of that is what a room like this one sends
        back to the microphone. For a reply at 0.6 on the display scale that is a gate
        of 0.05 - above the echo, below the 0.1 or so a person reaches by talking.

        Capped, because the coupling between speakers and microphone is not known and
        a wrong constant must fail in the direction that still lets the user speak: no
        command said over a reply may ever be refused, and the words themselves are
        checked separately by `_is_echo`.
        """
        now = time.monotonic()
        played = _nonnegative(self._played_held) / PLAY_LEVEL_GAIN
        gate = max(PLAY_FLOOR, min(PLAY_GATE_MAX, played * PLAY_GATE))
        # ...and the gate can never be higher than the owner's own voice, measured at
        # this microphone. PLAY_GATE_MAX is an absolute number, which is the wrong kind
        # of number for a device whose gain is not known: a quiet array hands over real
        # words at 0.05, this machine's echo arrives at 0.024, and a gate capped at
        # START_RMS * 20 = 0.06 sat above everything the owner could say - so every
        # command spoken while Retro was talking was refused, silently, and the mic
        # looked deaf. A gate at four fifths of a measured voice still refuses the echo
        # and still lets the person through, which is the only property it needs.
        heard = self._clean_level(now)
        if heard > 0.0:
            gate = min(gate, heard * 0.8)
        return gate

    def _played_level(self):
        """The loudness of the block the voice is playing right now, or 0.0.

        Both paths are asked: the streamed hosted voice, and the local SAPI one it
        falls back to. They report through different objects, and asking only one of
        them is how the gate came to be zero for half of every reply.
        """
        stream = self._voice_stream
        if stream is not None and stream.playing:
            return _nonnegative(getattr(stream, "level", 0.0))
        if self.speaker is not None and self.speaker.playing:
            return _nonnegative(getattr(self.speaker, "level", 0.0))
        return 0.0

    def _heard_ourselves(self, heard):
        """True when a transcript was recorded entirely over Retro's own voice.

        Kept for the log, not as a veto. The microphone gate is what refuses the echo
        - a block has to beat the level Retro is playing by PLAY_GATE to be recorded
        at all - and a veto on top of it would also refuse the command a person says
        straight after "Retro" while the greeting is still going, which is the common
        case and the one that has to work. What this buys is the next diagnosis: when
        a transcript that should not exist arrives anyway, the log says whether Retro
        was talking at the time.
        """
        return bool(self._heard_under_voice and strip_wake(heard) is None)

    def _is_echo(self, text, peak=0.0):
        """True when what was just heard is Retro's own voice coming back.

        The microphone hears these speakers, and the follow-up window takes a command
        without the wake word, so the two together make a loop. The log has the whole
        of it: the assistant said "Opening my settings", the recogniser returned
        "settings", and the same action ran again from a line nobody had spoken - one
        identical action twice, ten seconds apart. The owner heard one answer per
        repetition.

        A line is Retro's own voice when it arrives while the output is playing (or
        within ECHO_WINDOW_S of it stopping) and its words are Retro's own. Both halves
        matter: the window alone would swallow the user repeating a command back, and
        the words alone would swallow it whenever they did.

        The words are matched *roughly* now, and that is the whole change. The old test
        asked whether the transcript was a substring of something Retro had said, which
        is true of a clean echo and false of every echo this microphone actually
        produces: the recogniser hears the speakers a syllable behind and duplicates
        across the seam, so "Sure, I got it." came back as "Sure, I got it." (matched)
        but the greeting's "Commands, the app, and t..." came back as "Commands, the
        app, and the app, and the" - four words it never said, in the middle - and that
        fragment was dispatched as a request and answered. `_echo_overlap` asks what
        share of the transcript's words are Retro's, in order, and how long a verbatim
        run it contains, and either answer settles it.

        Two cases are deliberately left to the caller's level test rather than to the
        words, because words alone would refuse something real: a transcript that is
        *only* the wake word, and a single short word like "stop" or "yes".
        """
        words = _words(text)
        if not words:
            return False
        now = time.monotonic()
        if not self._playing() and now - self._output_ended_at > _config.ECHO_WINDOW_S:
            return False
        payload = strip_wake(text)
        if payload == "":
            # Nothing but the wake word. A person has to be able to say "Retro" while
            # Retro is still talking, so the words cannot settle this one and the level
            # has to: the owner's own "Retro" arrives as loudly as the rest of their
            # speech, and the echo of one arrives at the level of the echo.
            return self._too_quiet_to_be_a_person(peak, wake=text)
        if payload:
            # Match only what came *after* the wake word. An echo can contain it - the
            # live log had "Retro saw what you're doing..." come back off the speakers -
            # and while the wake word is in the transcript it cannot be a word of Retro's
            # own sentence being matched, because Retro does not address itself by name.
            words, text = _words(payload), payload
        if len(words) == 1 and len(words[0]) < ECHO_SHORT_WORD:
            # "stop", "yes", "now": a single short word is a name for something, and
            # matched loosely against 45 seconds of everything Retro has said it would
            # be swallowed every time. Short words are left to the level test and to
            # the duplicate guard.
            return False
        for when, line in reversed(self._said_recently):
            if now - when > _config.ECHO_KEEP_S:
                break
            share, run = _echo_overlap(words, line)
            # A run of three of Retro's own words in a row is not something two people
            # say by accident; so is a transcript that is mostly made of them.
            if run >= ECHO_RUN or (share >= ECHO_MATCH
                                   and (len(words) >= 2 or len(words[0]) >= ECHO_SHORT_WORD)):
                self._echo_match = (round(share, 2), run)
                return True
        return False

    def _take_yardstick(self, heard, peak):
        """Measures the owner's own voice, from the one phrase that can be trusted.

        Not every phrase heard with nothing playing, which is what this was and what
        made it useless: the live session measured 0.83 on "Retro.", then the
        recogniser invented "Thank you." at 0.031 and "Cennance." at 0.158 out of an
        empty room, and each of those *replaced* the measurement. The yardstick fell to
        0.031, so it stopped refusing anything at all, and every quiet fragment after
        it was transcribed, dispatched and answered out loud.

        So: only a phrase that carried the wake word, only with nothing playing, and
        only ever upwards. "Retro" is deliberate - the owner said it to this machine -
        and it is loud, so the junk the recogniser invents cannot move it.
        """
        if not heard or peak <= 0.0:
            return
        if strip_wake(heard) is None:
            return
        # "Under our voice" is not disqualifying: the owner says "Retro" over the
        # greeting all the time, and that phrase is the best measurement there is.
        # An echo of the greeting cannot carry the wake word at this level.
        if self._heard_under_voice and peak < WAKE_LEVEL:
            return
        now = time.monotonic()
        # Two decimals: the measurement is a level the microphone gave us, and the
        # float32 it arrived as (0.8299999...) is not a different voice from 0.83.
        self._clean_peak = round(max(self._clean_level(now), float(peak)), 2)
        self._clean_at = now

    def _clean_level(self, now=None):
        """The owner's voice as measured *now*: the last peak, older every second.

        See `CLEAN_DECAY_S`. Kept as one function so the gate and the quiet test
        cannot disagree about how loud the owner is - the same number refused a
        command in one place and let the echo through in the other when they were
        two copies of the arithmetic.
        """
        if self._clean_peak <= 0.0:
            return 0.0
        now = time.monotonic() if now is None else now
        age = max(0.0, now - getattr(self, "_clean_at", now))
        return self._clean_peak * math.exp(-age / CLEAN_DECAY_S)

    def _too_quiet_to_be_a_person(self, peak, wake=None):
        """True when a phrase arrived at a volume only the room or the echo reaches.

        `_mic_gate` reasons from what the *player* reports and from an assumed coupling
        between the speakers and the microphone; this reasons from what this owner's own
        voice actually measures, which needs no assumption at all. `_clean_peak` is that
        measurement, and it is taken only from a phrase that carried the wake word - the
        one utterance a hallucination or an echo essentially never produces at volume.

        It is what catches the fragments no word test can reach. The live session had
        the recogniser invent "ありがとうございました" out of a sentence in English and hand
        it back as a command, with no words in common to match on (peak 0.079 against
        the owner's 0.849), and it had a run of quiet junk transcribed as real words -
        "Thank you." at 0.031, "Cennance." at 0.158, "Commands." at 0.113 - each of
        which became a request, and each of which was answered, out loud, so its answer
        was heard and answered in turn. That is the loop the owner asked to be rid of,
        and it is the same loop as the echo: all of it is a transcript nobody said.

        Unknown is never quiet. With no wake word heard yet in this session there is
        nothing to measure against, and nothing is refused.
        """
        if self._clean_peak <= 0.0 or peak <= 0.0:
            return False
        floor = ECHO_OF_VOICE if wake is None else WAKE_OF_VOICE
        return peak < self._clean_level() * floor

    def _echo_reason(self, text, peak=0.0):
        """Why this transcript is Retro's own voice, or "" when it is not.

        A label rather than a flag because the log is the point: the next time a
        fragment is dropped - or one that should have been dropped is not - the line
        says which rule decided, and with which numbers.
        """
        payload = strip_wake(text)
        if payload == "":
            # Nothing but the wake word: the words cannot decide it, so it is the level.
            return "echo level" if self._too_quiet_to_be_a_person(peak, wake=text) else ""
        if self._is_echo(text, peak):
            return "own words"
        # A phrase that says "Retro" is deliberate, so it is held to the lower floor: the
        # owner must be able to say the wake word at a distance and be heard.
        if self._too_quiet_to_be_a_person(peak, wake=text if payload else None):
            return "too quiet"
        return ""

    def _report_mic(self, on, force=False):
        """Tells the window the microphone state - once, and only when it is true.

        Off is deferred while a capture is open (see `set_mic`), which is what stops
        the display saying "microphone off" over a stream that is still being read.
        """
        on = bool(on)
        if not force and on == self._mic_reported:
            return
        self._mic_reported = on
        try:
            self._on_mic(on)
        except Exception:                 # the window must not break the session
            pass

    def _interrupt(self):
        """Tells the listener to stop waiting: a typed command is due, or we're closing.

        The microphone switch is checked here too, and that is what makes turning it
        off take effect *now* rather than when the phrase in progress ends: the
        listener asks this on every block, so the capture loop closes within one
        block and the device is released instead of held for another sentence.
        """
        return self._stop.is_set() or not self.mic_on or not self._typed.empty()

    def voice_loop(self):
        """Wake word first, then a request, then back to the wake word.

        By default every command has to start with "Retro", so ordinary speech
        nearby is never a request. With the listen-after-Retro setting on, a
        follow-up is accepted without repeating the wake word - until the window
        after the last reply closes, the user turns it off, or something goes wrong.
        """
        awake = False
        # One line at the top, because "it does not hear me" arrived as a bug report
        # with no way to tell *which* of two things was true: the loop never started
        # (the window held it back, or the thread died) or the loop was running and
        # heard nothing. An open and a close, and the reason when it leaves, is what
        # answers that from a log on somebody else's machine.
        log.event("voice_loop_started", mic=self.mic_on, voice=self._voice_wanted)
        while not self._stop.is_set():
            if not self._typed.empty():
                # A typed line is always acted on: it was meant, not overheard.
                self.dispatch(self._typed.get(), woke=True)
                awake = self._stay_awake()
                continue
            if not self.mic_on:
                # Switched off: no stream is opened and no audio leaves this machine.
                # The loop stays alive only to drain typed commands, and it re-reads the
                # switch every pass - so a worker cannot quietly reopen the microphone
                # after the user closed it.
                if self._state != ASLEEP and self._state != OFFLINE and self.current_task is None:
                    self._set_state(ASLEEP, "microphone off")
                awake = False
                time.sleep(0.12)
                continue
            if self.ears is None:
                if not self._voice_wanted:
                    # Started with --no-voice: the loop survives purely for typing.
                    time.sleep(0.05)
                    continue
                # The microphone was turned on after a start with it off, so the ears
                # are built here, on this thread, rather than at launch.
                try:
                    self.ears = build_ears()
                    log.event("ears_ready", device=self._input_device())
                except Exception as exc:
                    log.event("ears_failed", error=type(exc).__name__)
                    self._set_state(OFFLINE, f"microphone unavailable ({type(exc).__name__})")
                    time.sleep(1.0)
                    continue
            if not awake:
                self._set_phase(states.WAKE, "say 'Retro'")
            self._heard_under_voice = False         # whatever this pass hears is clean
            phrase = {}
            # One trace per listening pass. It is moved to the end of the phrase by
            # `listen_once`, so everything it measures after that is the silence the
            # user is sitting in - the number that actually feels like latency.
            trace = diag.new(kind="voice", origin="voice", label="voice command")
            self._capturing = True
            try:
                heard = listen_once(self.ears, interrupt=self._interrupt,
                                    on_level=self._mic_level, gate=self._mic_gate,
                                    on_barge=self._barged, timing=phrase, trace=trace,
                                    device=self._input_device())
            except Exception as exc:
                log.event("microphone_problem", error=type(exc).__name__)
                if trace is not None:
                    trace.error("capture", exc)
                    trace.finish(ok=False)
                    diag.record(trace)
                self._log(f"microphone problem ({type(exc).__name__})")
                awake = False
                self._follow_until = 0.0
                self._set_state(OFFLINE, f"microphone problem ({type(exc).__name__})")
                time.sleep(1.0)
                continue
            finally:
                self._capturing = False
                if not self.mic_on:
                    # The device is closed now, so the switch may finally say off.
                    self._report_mic(False)
            if self._stop.is_set():
                break
            if not self._typed.empty():
                continue                          # interrupted for a typed command
            # Re-read every time round. The window is opened by the *reply* to the
            # last request, which happens on another thread - so deciding it once, at
            # the end of the previous pass, is exactly why "listen after Retro"
            # looked broken: the flag was read before the reply existed, Retro went
            # back to needing the wake word, and the window it had opened was never
            # used. It is open or it is closed; this loop asks, every time.
            if self._stay_awake():
                awake = True
            if self._heard_ourselves(heard):
                # Retro was on the speakers for every block of that phrase. It is kept
                # as evidence rather than as a veto: a veto here would also swallow the
                # command a person says straight after "Retro", over the greeting -
                # which is the common case, and the one that must never break. What
                # decides is the words (`_is_echo`) and, for the fragments no words can
                # match, the level (`_too_quiet_to_be_a_person`).
                log.event("heard_under_our_voice", chars=len(heard or ""),
                          text=str(heard or "")[:24],
                          ours=str(self._said_recently[-1][1])[:40] if self._said_recently else "")
            peak = 0.0
            if heard:
                # What was heard, and what it was heard *in*: the length of the phrase,
                # its loudest sample and whether Retro was talking over it. A transcript
                # nobody can explain is the whole difficulty of a bug like the storm -
                # "dispatch chars: 5" says nothing about where the five characters came
                # from, while 0.4 s at a peak of 0.06 under the voice says it in one
                # line. The text is here for the same reason the pivot text already is.
                peak = float(phrase.get("peak") or 0.0)
                log.event("heard", text=str(heard)[:40], chars=len(str(heard)),
                          seconds=round(float(phrase.get("duration") or 0.0), 2),
                          peak=round(peak, 3), clean_peak=round(self._clean_level(), 3),
                          under_voice=self._heard_under_voice)
            self._take_yardstick(heard, peak)
            why = self._echo_reason(heard, peak)
            if why:
                # Its own voice, off the speakers and back through the microphone. The
                # window it was said into stays exactly as it was; nothing is answered.
                log.event("heard_itself", chars=len(heard), text=str(heard)[:32], why=why,
                          peak=round(peak, 3), match=self._echo_match,
                          clean_peak=round(self._clean_level(), 3))
                awake = self._stay_awake()
                continue
            kind, payload = classify(awake, heard, followup=self.followup,
                                     control=self._control_word)
            if kind == "ignore":
                awake = self._stay_awake()
                if not awake:
                    self._set_phase(states.WAKE, "say 'Retro'")
                continue
            if kind == "sleep":
                # The goodbye is said *before* the brain is let go, or there is
                # nothing left to render a line with. `_say` waits for it to play.
                self._say(SLEEP_REPLY)
                awake = False
                self._sleep()
                continue
            if kind == "wake":
                woke_at = diag.now_ns()
                loaded = self._wake()
                if trace is not None:
                    trace.kind = "wake"
                    trace.label = "wake"
                    trace.add("wake", diag.now_ns() - woke_at)
                    trace.model = getattr(self.fallback, "model", None) if self.converse else "local"
                    trace.finish(ok=loaded)
                    diag.record(trace)
                awake = loaded
                if not loaded:
                    self._set_phase(states.WAKE, "say 'Retro' again")
                    continue
                if self.current_task is None:
                    self._set_phase(states.IDLE, "listening for a command")
                # The greeting comes *after* the load, which is the whole point of
                # a visible three-second wake: it is ready when it speaks. It is also
                # said *here*, from one short line, rather than written by the model
                # on a worker thread: that round trip is what the owner heard as "it
                # says hello, turns green, plans, and then says hello again" - a
                # second of stall for a sentence no better than this one. The model
                # still writes the lines that carry information; a greeting is not one.
                self.speak_async(GREETING, gen=self._speech_gen)
                continue
            if not self.loaded and not self._wake():
                # "Retro, open notepad" in one breath while asleep: it still has to
                # load before anything can be asked of it.
                self._set_phase(states.WAKE, "say 'Retro' again")
                continue
            self._next_trace = trace
            self.dispatch(payload, woke=strip_wake(heard) is not None)
            self._next_trace = None
            awake = self._stay_awake()
            if not awake:
                self._set_phase(states.WAKE, "say 'Retro'")

    # ---------- modes, listening policy, and the task list -------------------

    def set_mode(self, mode, save=True):
        """Switches chat/work. Returns the mode now in force, or None.

        The mode is read by the executor (core/planner.py, core/tools.py) as well as
        shown in the window, which is the difference between a mode and a label.
        """
        wanted = str(mode or "").strip().lower()
        if wanted not in modes.ALL:
            return None
        if wanted != self.mode:
            log.event("mode_set", mode=wanted, was=self.mode)
        self.mode = wanted
        self._on_mode(wanted)
        if save:
            settings.save(mode=wanted)
        return wanted

    def set_followup(self, on, save=True):
        """The wake word for every request, or one "Retro" then talk.

        On ("listen after Retro"): a follow-up needs no wake word again, and turning it
        on opens the conversation at once. Off: every request starts with "Retro" and
        the window closes. Either way, the window a "Retro" opens stays open - without
        repeating the wake word - until CONVERSATION_WINDOW_S passes with nothing said:
        the three-minute reset. That window is chosen here and enforced in
        `_stay_awake` and `voice.classify`, so there is one place that decides whether
        a transcript is for Retro.
        """
        state = bool(on)
        self.followup = state
        self._follow_until = (time.time() + CONVERSATION_WINDOW_S) if state else 0.0
        log.event("followup_set", on=state)
        self._on_progress("listening for follow-ups without the wake word" if state
                          else "say 'Retro' every time")
        if save:
            settings.save(followup=state)
        return state

    def set_mic(self, on, save=True):
        """The microphone switch: off means capture really stops.

        The listening loop reads `mic_on` before it opens a stream and on every block
        inside one, so off means no device is held, no audio is recognised and no
        transcription request is made - not that the audio is heard and discarded. The
        decision lives here rather than in the window for the same reason the mode and
        the follow-up switch do: a setting the assistant obeys has one owner, and a
        worker that reopens the microphone on its own would be a second one.

        Typed commands, a reply that is already being spoken, and a request that is
        already running are untouched by it. The microphone is an input switch, not a
        stop button.
        """
        state = bool(on)
        changed = state != self.mic_on
        self.mic_on = state
        if not state:
            self._follow_until = 0.0
        log.event("microphone_set", on=state)
        if not state and self._capturing:
            # The listener checks `interrupt` on its next block and closes the stream;
            # the callback is sent from that point (in `voice_loop`), not from here.
            pass
        else:
            self._report_mic(state)
        self._on_progress("microphone on" if state else "microphone off - type a command")
        if save:
            settings.save(mic=state)
        closing = bool(not state and self._capturing)
        if state:
            # A microphone just switched on is given the benefit of the doubt: the
            # silence it will be judged on starts from this click, not from whenever the
            # machine last made a noise.
            self._mic_heard_at = time.monotonic()
            self._mic_silent_reported = False
        # The visualiser and the microphone are mutually exclusive: turning the
        # microphone off is what lets background hearing start, and turning it on takes
        # the sampler away again before any capture opens. This is the owner's rule and
        # the hardware's: both streams at once leave the microphone deaf.
        self._refresh_audience()
        if changed and not closing and self._state != OFFLINE and self.current_task is None:
            if state:
                self._set_phase(states.WAKE, "say 'Retro'")
            else:
                self._set_state(ASLEEP, "microphone off")
        return state

    def set_background(self, on, save=True):
        """Background voice hearing: the orb reacts to the machine's own audio.

        It only ever *draws*. Sleep still means the assistant is off, unloaded, and
        unable to act on anything - turning this on while awake changes nothing, and
        going to sleep with it on changes the picture, not the listening policy.
        """
        state = bool(on)
        self.background = state
        log.event("background_set", on=state, mic=self.mic_on)
        if state and self.mic_on:
            self._on_progress("background hearing waits for the mic to be off")
        else:
            self._on_progress("background voice hearing on" if state
                              else "background voice hearing off")
        if save:
            settings.save(background=state)
        # The sampler is not only the picture: while it is running it is also what
        # keeps the microphone from spending the recogniser on a television. It runs
        # while Retro is awake and stops when it sleeps, so switching this on with the
        # assistant asleep changes the setting and nothing else - sleep stays still.
        self._refresh_audience()
        return state

    def task_list(self, limit=10):
        """This session's tasks, newest first, as plain dictionaries."""
        snapshot = list(self.tasks)[-max(1, int(limit)):]
        return [{"text": task.text,
                 "status": task.status_label(),
                 "seconds": round(task.duration(), 1),
                 "steps": task.steps} for task in reversed(snapshot)]

    def cancel_task(self, which=""):
        """Stops the running task, or a named/numbered one from the list.

        Cancelling means all three of the things the user asked for: the running
        code stops at its next checkpoint, anything it had planned to do next is
        dropped, and the speech it had queued is invalidated instead of finishing
        whatever it was saying.
        """
        wanted = " ".join(str(which or "").lower().split())
        current = self.current_task
        if not wanted or wanted in ("current", "that", "it", "the running one", "last"):
            if current is None or current.done.is_set():
                return False, "Nothing is running to stop."
            return self._stop_task(current)
        recent = [task for task in reversed(list(self.tasks))]
        for index, task in enumerate(recent, start=1):
            if wanted == str(index) or (len(wanted) > 2 and wanted in task.text.lower()):
                if task.done.is_set():
                    return False, (f"Task {index} ('{task.text[:40]}') has already "
                                   f"{task.status_label()}.")
                return self._stop_task(task)
        return False, "I couldn't tell which task you meant."

    def _stop_task(self, task):
        task.cancel("user")
        self._follow_until = 0.0
        self._cancel_speech()
        reply = self._stop_reply()
        if task is self.current_task:
            task.replied = True
            self._on_reply(reply, False)
        log.event("task_stopped", by="tool", seconds=round(task.elapsed(), 1))
        return True, reply

    # ---------- the documents -------------------------------------------------

    def documents(self, limit=8):
        """The documents Retro has written, newest first - what the window's shelf shows."""
        return compose.documents(limit)

    def open_document(self, name):
        """Opens one of Retro's documents in whatever this machine reads it with.

        Returns the path it opened, or None when there is no such document - the
        workspace check in core/compose.py is the only thing that decides, so no name
        from the window can walk out of the workspace.
        """
        path = compose.document_path(name)
        if path is None or not hasattr(os, "startfile"):
            return None
        try:
            os.startfile(path)
        except OSError as exc:
            log.event("document_open_failed", error=type(exc).__name__)
            return None
        log.event("document_opened", name=os.path.basename(path))
        return path

    # ---------- settings ----------------------------------------------------

    def set_model(self, choice):
        """Switches the brain - cloud or local - and unloads the old one first."""
        choice = str(choice).lower()
        if choice == self.model_choice:
            return
        if self.fallback is not None:
            self.fallback.unload()
        self.fallback, self.converse = build_brain(choice, idle_unload_s=self._idle_unload_s)
        if self.planner is not None:
            # The planner holds the model call and the conversation; both have to
            # follow the switch, and the history must not outlive the brain that
            # wrote it.
            self.planner.complete = self._model_ask
            self.planner.conversational = self.converse
            self.planner.forget()
        self.model_choice = choice
        settings.save(model=choice)
        self._log(f"model set to {choice}")

    def voices(self):
        """Names of the installed voices, best first. Empty if the voice failed."""
        if self.speaker is None:
            return []
        return [getattr(v, "name", "voice") for v in self.speaker.ensure_voices()]

    def set_voice(self, name):
        """Re-picks the spoken voice by name substring. Returns the name chosen."""
        if self.speaker is None:
            return None
        chosen = self.speaker.choose(name)
        if chosen:
            settings.save(voice=name)
            self._log(f"voice set to {chosen}")
        return chosen

    def outputs(self):
        """(index, name) for the playback devices this machine offers."""
        from .voice import output_devices
        return output_devices()

    def inputs(self):
        """(index, name) for the recording devices this machine offers."""
        from .voice import input_devices
        return input_devices()

    def _input_device(self):
        """The sounddevice index for the remembered microphone, or None.

        Resolved once and cached: the device list is stable within a run, and asking
        the OS for it on every listening pass would add a query to the path whose
        speed is the whole point. A remembered microphone that is gone, or that will
        not accept the rate the recogniser needs, falls back to the system default
        and says so in the log - it does not leave a dead choice quietly in place.
        """
        if self._mic_index_ready:
            return self._mic_index
        self._mic_index_ready = True
        wanted = self._mic_device
        if not wanted:
            return None
        for index, label in self.inputs():
            if label != wanted:
                continue
            if input_usable(index):
                self._mic_index = index
            else:
                log.event("input_device_unusable", device=index, input_name=label)
                self._log(f"'{label[:40]}' can't be opened - using the system microphone")
            return self._mic_index
        log.event("input_device_missing", input_name=str(wanted)[:60])
        self._log(f"'{str(wanted)[:40]}' isn't available - using the system microphone")
        return None

    def set_input(self, device):
        """Chooses the microphone. Returns its index, or None for the default.

        The name is remembered for the same reason the output's is: indices move when
        hardware comes and goes, and a microphone chosen once should still be the one
        heard after a reboot.
        """
        offered = dict(self.inputs())
        index = device if device in offered else None
        name = offered.get(index)
        self._mic_device = name
        self._mic_index = index
        self._mic_index_ready = True
        settings.save(mic_device=name)
        log.event("input_set", device=index, input_name=name or "system default")
        self._log(f"microphone set to {name or 'system default'}")
        return index

    def test_microphone(self, seconds=1.5):
        """Records briefly and returns the loudest thing it heard. No transcription.

        This is what makes "Retro can't hear me" answerable before an API key, a
        network or a wake word is involved: a number near zero on the chosen device
        means the room or the mute button, and an exception means the device itself.
        """
        from .voice import measure_input
        level = measure_input(self._input_device(), seconds)
        log.event("mic_test", device=self._mic_index, peak=round(level, 4))
        return level

    def set_output(self, device):
        """Chooses the playback device. Returns its index, or None for the default.

        The *name* is what is remembered, never the index: names survive a device list
        that reorders itself, indices do not. Choosing an output also revives a voice
        that had given up, so "nothing came out - pick another output device" is a real
        way back rather than advice the assistant itself cannot follow.
        """
        if self.speaker is None:
            return None
        index = self.speaker.choose_output(device)
        name = dict(self.outputs()).get(index)
        settings.save(output=name if index is not None else None)
        log.event("output_set", device=index, output_name=name or "system default")
        self._log(f"output set to {name or 'system default'}")
        return index
