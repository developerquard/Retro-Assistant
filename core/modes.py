"""Chat mode and work mode - the switch that decides whether tools may run.

The user asked for two ways of talking to the same assistant, and this is the
honest way to build it: the mode is read by the executor, not drawn by the UI. In
chat mode the agent refuses every step that would change anything on the machine
and says so in words; in work mode it runs them. A mode the execution engine does
not consult is a label, and labels are what this is not.

Switching is understood in natural words ("switch to chat mode", "work mode",
"start working", "let's just talk") and as a tool, so the model can also switch
when the user asks for it in a phrasing this module does not know. The parsed
phrases are matched against the *whole* sentence (after an optional wake word),
never a substring: "open work mode" is not a phrase, and "start working on the
file" must not silently change how the assistant behaves.

What chat mode allows is deliberately narrow and never depends on what the request
says: read-only tools (reading the screen, listing a folder, the time) are fine,
because they change nothing and a question about the machine still deserves an
answer. Everything that opens, types, clicks, writes, sends or runs is refused.
"""

import re

CHAT, WORK = "chat", "work"
DEFAULT = WORK
ALL = (CHAT, WORK)

# Whole-sentence phrases. The wake word and punctuation are stripped first, so
# "Retro, switch to chat mode." lands on the same string as a typed command.
_CHAT_PHRASES = (
    "chat", "chat mode", "switch to chat", "switch to chat mode", "change to chat mode",
    "talk mode", "switch to talk mode", "conversation mode", "switch to conversation mode",
    "just talk", "let us talk", "lets talk", "let's talk", "let's just talk",
    "just chat with me", "chit chat mode",
    "sirf baat karo", "baat mode",
)
_WORK_PHRASES = (
    "work", "work mode", "switch to work", "switch to work mode", "change to work mode",
    "execution mode", "switch to execution mode", "task mode", "switch to task mode",
    "start working", "do work", "back to work", "tools mode", "switch to tools mode",
    "kaam mode", "kaam karo mode",
)
_STATUS = re.compile(r"\b(?:which|what)\b[^?]{0,20}\bmode\b|\bmode\b[^?]{0,15}\b(?:are you in|"
                     r"are we in|is on|is active)\b")

_MODE_WORD = re.compile(r"\b(chat|work|talk|execution|task|conversation|tools)\b")


# "Listen after Retro" - the setting the user asked for by name. Turning it on
# means a follow-up command does not need the wake word again; turning it off puts
# the wake word back in front of every request. Both are whole-sentence phrases for
# the same reason the modes are: "stop listening" must never be heard in the middle
# of a request about music.
_FOLLOWUP_ON = (
    "listen after retro", "listen after retro on", "follow up mode", "follow-up mode",
    "follow ups", "follow ups on", "keep listening", "listen for follow ups",
    "stay awake", "start listening after retro", "no wake word needed",
    "without the wake word",
)
_FOLLOWUP_OFF = (
    "stop listening after retro", "listen after retro off", "follow ups off",
    "stop follow ups", "no follow ups", "only the wake word", "wake word only",
    "require the wake word", "listen only when i say retro", "stop keeping listening",
    "back to the wake word",
)


def parse_followup(text):
    """True to keep listening for follow-ups, False to need the wake word, else None."""
    cleaned = _clean(text)
    if cleaned in _FOLLOWUP_ON:
        return True
    if cleaned in _FOLLOWUP_OFF:
        return False
    return None


# "Background voice hearing" - asleep, the orb comes alive to whatever the machine is
# playing. Same rule as everything else here: whole sentences only, so "turn the
# music down" can never be heard as "music visualiser on", and a sentence about a
# background app cannot turn the visualiser on.
_BACKGROUND_ON = (
    "background voice hearing", "background voice hearing on", "background hearing",
    "background hearing on", "hear the background", "listen to the background",
    "react to the music", "react to music", "music visualiser", "music visualiser on",
    "music visualizer on", "visualiser on", "visualizer on", "background mode on",
)
_BACKGROUND_OFF = (
    "background voice hearing off", "background hearing off", "stop background hearing",
    "stop hearing the background", "listen to the background off", "music visualiser off",
    "music visualizer off", "visualiser off", "visualizer off", "stop the visualiser",
    "stop the visualizer", "background mode off",
)


def parse_background(text):
    """True to switch background voice hearing on, False off, else None."""
    cleaned = _clean(text)
    if cleaned in _BACKGROUND_ON:
        return True
    if cleaned in _BACKGROUND_OFF:
        return False
    return None


# The microphone switch, by voice. Whole sentences for the same reason as everything
# else: "stop listening" said about a song must not close the microphone, and none of
# these phrases is long enough to be said by accident while a video plays.
_MIC_ON = (
    "microphone on", "turn the microphone on", "switch the microphone on",
    "start listening to me", "enable microphone", "enable the microphone",
    "unmute microphone", "unmute the microphone",
)
_MIC_OFF = (
    "microphone off", "turn the microphone off", "switch the microphone off",
    "stop listening to me", "disable microphone", "disable the microphone",
    "mute microphone", "mute the microphone", "stop listening",
)

# The developer report. Not a conversation topic: these exact sentences open the
# latency panel instead of being sent to a model, so a slow session can be measured
# without typing into a console.
_DIAGNOSTICS = (
    "diagnostics", "open diagnostics", "show diagnostics", "latency",
    "latency report", "timing report", "performance report", "why was that slow",
    "pipeline timings", "how slow are you",
)


def parse_mic(text):
    """True to listen, False to close the microphone, else None."""
    cleaned = _clean(text)
    if cleaned in _MIC_ON:
        return True
    if cleaned in _MIC_OFF:
        return False
    return None


# "Repeat that" - said when a line was missed, which is most often because the room
# was loud or the phone rang. The command cannot be treated as a request to the
# model: the answer is a line already spoken, and re-planning it would produce a
# *new* answer rather than the one the owner missed. Whole sentences only, so
# "repeat after me" in a document request is not this.  It is checked before the
# planner and after the settings, because it names a moment rather than a setting.
_REPEAT = (
    "repeat", "repeat that", "repeat that please", "say that again", "say it again",
    "say that once more", "what did you say", "what was that", "i didn't hear that",
    "i did not hear that", "i missed that", "come again", "pardon",
    "repeat the last thing", "repeat what you said",
)


def parse_repeat(text):
    """True for "repeat that" / "what did you say" - a request to re-say a line."""
    return _clean(text) in _REPEAT


def parse_diagnostics(text):
    """True for "diagnostics" / "why was that slow" - a report, not a request."""
    return _clean(text) in _DIAGNOSTICS


def _clean(text):
    """Lower case, wake word off the front, punctuation and spacing tidied."""
    cleaned = " ".join(str(text or "").lower().replace("’", "'").split())
    cleaned = re.sub(r"^(?:hey\s+|ok\s+|okay\s+)?(?:retro|retr|र+\S*)\s*[,!.:;]?\s*", "", cleaned)
    return cleaned.strip(" .!?,;:'\"")


# "Retro, switch to code" - the request to hold a conversation about a project before
# anything is built. It is not the chat/work mode: that decides whether tools may run,
# while this opens a different window and eventually a different program. Whole
# sentences only, like every command here, so "open the code folder" and "write some
# code for me" stay ordinary requests.
_CODE_SWITCH = (
    "switch to code", "switch to code mode", "switch to coding",
    "switch to coding mode", "switch to coding discussion", "change to code mode",
    "open code mode", "open coding mode", "open the coding discussion",
    "open coding discussion", "start coding mode", "coding", "code mode",
    "coding mode", "coding discussion", "coding discussion mode",
    "let us code", "lets code", "let's code", "talk about code",
    "talk about a project", "discuss a project", "discuss the project",
    "code mode please",
)


def parse_code_switch(text):
    """True when the sentence asks for the coding discussion, and nothing else.

    "Retro, switch to code" opens the chat; it must never be planned as a request,
    because a model asked to "switch to code" would answer in words and no window
    would open.
    """
    return _clean(text) in _CODE_SWITCH


def parse(text):
    """The mode the user asked to switch to, or None.

    Only a whole sentence counts. "open work mode" and "start working on that file"
    are not switches, and a mode that flipped on those words would be a demo, not a
    setting.
    """
    cleaned = _clean(text)
    if not cleaned:
        return None
    if cleaned in _CHAT_PHRASES:
        return CHAT
    if cleaned in _WORK_PHRASES:
        return WORK
    return None


def asks_status(text):
    """True for "what mode are you in" - a question the LLM answers itself."""
    return bool(_STATUS.search(_clean(text)))


def describe(mode):
    """One line about what a mode permits, for the hint under the switch."""
    return ("work mode - Retro can open, type, click, write files and run commands"
            if str(mode).lower() == WORK else
            "chat mode - Retro only talks; nothing on this machine is touched")


def allows_tools(mode):
    """True when tools may run at all in this mode."""
    return str(mode or DEFAULT).lower() != CHAT


# Tools chat mode allows even though one of them writes. Retro's memory is not the
# machine: it is a note about the person it is talking to, and the owner asked for
# facts to be kept wherever they were said - a preference told in chat mode is
# exactly the thing worth keeping. Everything else that opens, types, clicks or
# writes a real file is still refused, by tier, as before.
ALWAYS_OK = ("remember", "recall", "forget")


def allows_tool(mode, tool):
    """Whether one tool may run now.

    In work mode: everything. In chat mode: only tools that change nothing, which
    is decided by the tool's own tier in the registry - not by its name, and never
    by what the request claims it is - plus the two memory tools named above.
    """
    if allows_tools(mode):
        return True
    if str(getattr(tool, "name", "")) in ALWAYS_OK:
        return True
    tier = getattr(tool, "tier", "reversible")
    return str(tier) == "read"
