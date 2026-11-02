"""What Retro is doing right now, in one vocabulary, for every part of the app.

The window, the voice loop and the agent used to keep their own idea of the state,
and they disagreed: the circle sat there looking idle while a task was running, or
stayed green with nothing behind it. This module is the single list, and the single
mapping from a fine phase (recording, transcribing, executing a tool) onto the five
coarse states the display has always drawn.

The coarse names are kept exactly as they were - offline, asleep, listening,
working, speaking - because everything from the tray icon to the smoke test reads
them, and a state system that renames what it already had is a second state system.
The detail is that the phase is finer, and it is what the window shows in words and
what the log records.

`phase_for` is how a caller that only knows "I am running a tool" gets a phase
without inventing one; [ACTIVE] is the set of phases that mean work is in flight,
which is what stops the display claiming to be idle mid-task.
"""

IDLE = "idle"
LOADING = "loading"            # waking up: the brain is being loaded again
WAKE = "wake"                  # listening for the wake word
RECORDING = "recording"        # a phrase is being captured
TRANSCRIBING = "transcribing"  # speech to text in flight
GENERATING = "generating"      # the model is writing the reply
SPEAKING = "speaking"          # audio is coming out
PLANNING = "planning"          # the model is writing a plan
EXECUTING = "executing"        # a tool is running
WAITING_TOOL = "waiting_tool"  # a tool was started and its result is due
BACKGROUND = "background"      # work continues while the user is elsewhere
WAITING_USER = "waiting_user"  # Retro asked something and is listening for it
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
OFFLINE = "offline"

ALL = (IDLE, LOADING, WAKE, RECORDING, TRANSCRIBING, GENERATING, SPEAKING, PLANNING,
       EXECUTING, WAITING_TOOL, BACKGROUND, WAITING_USER, COMPLETED, FAILED,
       CANCELLED, OFFLINE)

# The states the window knows how to colour. `loading` is the wake-up: the orb walks
# from the grey it slept in to the blue it will be, over three seconds.
UI_STATES = ("offline", "asleep", "loading", "listening", "working", "speaking")

_UI = {
    OFFLINE: "offline",
    LOADING: "loading",
    WAKE: "asleep",
    IDLE: "listening",
    RECORDING: "listening",
    TRANSCRIBING: "listening",
    GENERATING: "working",
    PLANNING: "working",
    EXECUTING: "working",
    WAITING_TOOL: "working",
    BACKGROUND: "working",
    WAITING_USER: "listening",
    SPEAKING: "speaking",
    COMPLETED: "listening",
    FAILED: "listening",
    CANCELLED: "listening",
}

# Phases during which a request is genuinely in flight - and `loading`, because a
# machine that is waking up is not idle either.
ACTIVE = (GENERATING, PLANNING, EXECUTING, WAITING_TOOL, BACKGROUND, LOADING)

LABELS = {
    IDLE: "listening for a command",
    LOADING: "waking up",
    WAKE: "say 'Retro'",
    RECORDING: "listening to you",
    TRANSCRIBING: "writing down what you said",
    GENERATING: "thinking",
    PLANNING: "planning",
    EXECUTING: "working",
    WAITING_TOOL: "waiting for the machine",
    BACKGROUND: "working in the background",
    WAITING_USER: "waiting for your answer",
    SPEAKING: "speaking",
    COMPLETED: "done",
    FAILED: "that did not work",
    CANCELLED: "stopped",
    OFFLINE: "voice off",
}


def ui_state(phase):
    """The coarse state the display draws for a fine phase."""
    return _UI.get(str(phase or "").lower(), "listening")


def label(phase):
    """A short human phrase for a phase, for the panel's hint line."""
    return LABELS.get(str(phase or "").lower(), "")


def is_active(phase):
    """True while a request is in flight, so nothing may claim to be idle."""
    return str(phase or "").lower() in ACTIVE
