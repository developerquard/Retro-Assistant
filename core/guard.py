"""Answers for requests Retro must refuse, checked where the typed rules find nothing.

This runs only when `route()` returns None, so it can never shadow a real command.
It exists for two reasons:

* A small model sometimes forces a command onto a sentence that asked for none.
  Measured on the M4 set, "laptop band karo" became "pause the music" - it would
  have stopped Spotify for a shutdown request. The device words below tell that
  request apart from "gaana band karo", which really is the media command.
* Some requests (shut down, delete, install, send a message, make a call) are off
  the list on purpose. Refusing them in words is better than a silent wrong action.

Every rule returns a reply; `check()` returns None when the text should still go
to the model.
"""
import re

from .actions import ACTIONS
from .model import has_kw, norm

# Power. English phrases are unambiguous on their own.
_POWER_EN = re.compile(
    r"\b(?:shut ?down|shutdown|restart|re-?start|reboot|power off|log ?off|logoff|"
    r"sign out|signout|hibernate|sleep mode|standby)\b"
)
# Devanagari power words. "बंद करो" also means "close/stop", so it needs a device
# word and no media word (see check()).
_POWER_HI = re.compile(r"(?:शटडाउन|रीस्टार्ट|सुलाओ|बंद कर दो|बंद करो|बंद कर)")
_BAND_HINGLISH = re.compile(r"\bband kar(?:o|do|na|de)?\b")
_DEVICE = re.compile(r"\b(?:laptop|computer|pc|system|windows|machine)\b|(?:लैपटॉप|कंप्यूटर|सिस्टम|मशीन)")
_MEDIA = re.compile(r"\b(?:music|song|gaana|video|track|playlist|player|audio|movie)\b"
                    r"|(?:गाना|गाने|म्यूजिक|संगीत|गीत|वीडियो)")

_DELETE = re.compile(r"\b(?:delete|erase|remove|wipe|format|uninstall)\b|(?:डिलीट|मिटा|हटा)")
_INSTALL = re.compile(r"\b(?:install|download)\b|(?:इंस्टॉल)")
_SEND = re.compile(r"\b(?:send|message|whatsapp|email|mail|call|dial|text)\b"
                   r"|(?:भेजो|भेज दो|संदेश|मैसेज|कॉल)")
# Sending *to someone* is still refused: Retro must never pick a recipient. Typing
# into a window the user is pointing at is not the same thing, so a request that
# says how to do it - type, click, press - is allowed through to the planner.
_SEND_TO = re.compile(r"\b(?:whatsapp|email|e-mail|gmail|mail|sms|telegram|instagram|"
                      r"messenger|signal|call|dial|dm)\b|(?:भेजो|संदेश|मैसेज|कॉल)")
_TYPE_UI = re.compile(r"\b(?:type|typed|typing|click|clicking|press|pressing|enter|"
                      r"likho|likh do|tap)\b")

_GREET = re.compile(r"^(?:hi|hey|hello|yo|hiya|namaste|namaskar|नमस्ते|नमस्कार"
                    r"|good (?:morning|afternoon|evening|night))\b")
_THANKS = re.compile(r"\b(?:thank you|thanks|thank u|shukriya)\b|(?:धन्यवाद|शुक्रिया)")
_JOKE = re.compile(r"\b(?:joke|chutkula|story|kahani|poem|kavita)\b|(?:चुटकुला|कहानी|कविता)")
_WEATHER = re.compile(r"\b(?:weather|forecast|mausam|rains?|raining)\b|(?:मौसम|बारिश|ठंड|गर्मी)")
_NEWS = re.compile(r"\b(?:news|headlines|khabar)\b|(?:समाचार|खबर)")
_HOWRU = re.compile(r"\b(?:how are you|how's it going|how are things|what's up|wassup|sup|"
                    r"kaise ho|kaise hain|kya haal)\b|(?:कैसे हो|कैसे हैं|क्या हाल)")
_IDENTITY = re.compile(r"\b(?:who are you|what are you|your name|what can you do|what do you do|"
                       r"what are your commands|help)\b|(?:तुम कौन हो|आप कौन हैं|तुम्हारा नाम|मदद)")
# Fact questions the assistant cannot answer. Listed last, after every real command
# ("what time is it", "how much battery") has already been matched by the rules.
_FACT = re.compile(r"^(?:who|what|when|where|why|which|whose)\b(?:\s+\w+){0,4}\s+"
                   r"(?:is|are|was|were|does|do|did|can|will)\b"
                   r"|^(?:how (?:much|many|far|long|old|tall|big))\b"
                   r"|(?:कौन है|कौन था|क्या है|कब है|कहाँ है|कहां है)"
                   r"|\b(?:kaun hai|kya hai|kab hai|kahan hai)\b")

POWER = "I can't shut down, restart or sleep your laptop. I could close a program instead."
DELETE = "I can't delete or format anything."
INSTALL = "I can't install software."
SEND = ("I can't send messages or make calls. I can type into a window you have open "
        "if you tell me what to type and when to press send.")
# Short, warm and, above all, free of a list of features: the owner complained that
# the assistant kept telling them what it could do. Answering *when asked* is fine;
# volunteering a menu of capabilities is the thing that made it sound like a demo.
GREET = "Hey, I'm listening."
THANKS = "Any time."
JOKE = "That one's beyond me, I'm afraid."
WEATHER = "I can't check the sky - I can search for it if you like."
NEWS = "I can't read the news, but I can search for it."
HOWRU = "Good, thanks. What's on your mind?"
FACT = "I don't know that one - want me to search for it?"
IDENTITY = ("I'm Retro - I live on this laptop and I'm listening. Tell me what you need "
            "and we'll get it done.")


def _mentions_command(t):
    """True when the text carries an action keyword the router could not use.

    Such a sentence may be a real command in a phrasing the rules miss, so the
    soft rules below step aside and let the model try it.
    """
    return any(has_kw(t, k) for action in ACTIONS for k in action.keywords)


def check(text, only_hard=False):
    """Returns a reply when Retro must decline, or None to let the model try.

    Hard rules (power, delete, install, send) come first and always stand. The
    rest are soft: they only apply when the sentence has no action keyword at
    all, so "what time is it" still reaches the time command.

    `only_hard` stops after the refusals that protect the machine, and hands
    everything else to the model. That is what a cloud brain needs: the canned
    lines below were written to stop a 1B model from inventing an action for a
    greeting, but a real model *should* answer "how are you" in its own words,
    and "what is the capital of France" is a question it can simply answer.
    Nothing that risks the machine is skipped - only the small talk.
    """
    t = norm(text)
    if _POWER_EN.search(t):
        return POWER
    if _POWER_HI.search(t) and not _MEDIA.search(t):
        return POWER
    if _BAND_HINGLISH.search(t) and _DEVICE.search(t) and not _MEDIA.search(t):
        return POWER
    if _DELETE.search(t):
        return DELETE
    if _INSTALL.search(t):
        return INSTALL
    # A sentence that names a real command keyword is not a send/request even
    # when it also says "message" or "whatsapp" - "open discord and check my"
    # should reach the router or model, not be refused here. The hard rules above
    # (power, delete, install) still stand because they name no command keyword.
    if _mentions_command(t):
        return None
    if _SEND.search(t) and not (_TYPE_UI.search(t) and not _SEND_TO.search(t)):
        return SEND
    if only_hard:
        return None            # a real brain answers the rest itself
    if _WEATHER.search(t):
        return WEATHER
    if _NEWS.search(t):
        return NEWS
    if _JOKE.search(t):
        return JOKE
    if _GREET.search(t):
        return GREET
    if _THANKS.search(t):
        return THANKS
    if _HOWRU.search(t):
        return HOWRU
    if _IDENTITY.search(t):
        return IDENTITY
    if _FACT.search(t):
        return FACT
    return None
