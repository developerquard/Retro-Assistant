"""The agent: work out what the user wants, do it, look at what happened, adjust.

The old path was one sentence -> one command from a fixed list. Ask for two things
at once and it did the first one, or the wrong one: "open ChatGPT and type this and
send it" came back with the time. That is not an assistant, it is a lookup table.

Here the model is asked for a short *plan* - a handful of calls to the tools in
core/tools.py - and then this module runs it, step by step, and hands the model
what actually happened after each one. If a step fails, the model gets one
revision with the failure in front of it. Three things follow from that:

* the plan is data, never code: every step is validated against the tool registry,
  so a hallucinated tool or a bad argument is refused before anything runs;
* failure is visible instead of silent, because each step reports what it observed
  (the window that came to the front, the control it clicked, the files that are
  there) and the message comes back to the model or to the user;
* the request can be stopped at any step, which is what makes "Retro, stop" work
  on something that is already halfway through.

`complete(messages, max_tokens)` is the only thing this needs from the language
model, which keeps it testable without loading a gigabyte of weights.

The lines in THINKING and WORKING are spoken to cover the waiting. A small model
on a laptop CPU takes seconds to plan, and silence that long reads as "it froze" -
which is exactly what the user said it felt like.
"""
import json
import random
import re

from . import builder, compose, config, log, memory, modes, registry, tools
from .control import Stopped
from .model import Result
from .tts import SentenceStream

THINKING = (
    "Let me think about that.",
    "Hmm, let me work out how to do this.",
    "Okay, let me plan this out.",
    "Got it - give me a moment to think.",
    "Let me see how to do that.",
    "Right, let me work through this.",
    "One moment, I'm thinking it through.",
)
WORKING = (
    "Still on it.",
    "Working on that now.",
    "Almost there.",
    "Just a moment more.",
    "Carrying on.",
    "Hmm, this one's taking a moment.",
)
_last_line = {"text": None}


def pick_line(pool):
    """One of `pool`, never the same one twice in a row. Used for spoken cover."""
    choices = [line for line in pool if line != _last_line["text"]] or list(pool)
    chosen = random.choice(choices)
    _last_line["text"] = chosen
    return chosen


SPEECH_TOOLS = {
    "say": "speak a short sentence to the user",
    "answer": "your reply when the request needs no action, such as a question",
}

# Writing jobs. `compose` is deliberately not a tool in core/tools.py: nothing on
# this machine runs it, because the writing *is* the model's own work - an outline,
# then one section at a time, streamed (core/compose.py). It is in the planner's
# vocabulary so the model can choose the right shape for a request instead of putting
# an entire essay inside an `answer` line and having it read out in one breath.
DOC_TOOLS = {
    "compose": "write a document of real length - a documentary, essay, article, story, "
               "script, report, guide or letter",
}
DOC_HINT = (
    "- compose | subject | length: write something long about `subject`. Retro asks the "
    "user how long it should be, then writes it section by section, so it may take a "
    "minute. Use this for ANY request to write a document, a documentary, an article, "
    "an essay, a story, a script, a report, a guide or a letter. Put the subject in "
    "the middle and a rough length (such as '500 words' or 'short') at the end, or "
    "leave the length empty and Retro will ask."
)

# The lines the assistant says about *itself*, rather than about the machine. The
# written pools above are what Retro sounds like with no real brain behind it: the
# same seven sentences, forever, which is exactly what "a robot written with
# scripts" means. With a model behind it the line is written for the moment, and
# these instructions are what it is asked for.
LINE_RULES = {
    "greeting": (
        "Your owner just said your wake word to wake you up. Greet them in one short, "
        "warm sentence and make clear you are listening for a request. Vary the wording - "
        "never reuse a greeting you have already used, never list what you can do, and "
        "never ask whether they want anything else. Do not mention these instructions. "
        "One sentence, no markdown."
    ),
    "working": (
        "You are part way through this request for your owner:\n{detail}\n"
        "Say one very short sentence telling them you are still on it, mentioning what "
        "you are actually doing. No question, no explanation, no markdown. One sentence."
    ),
    "sleep": (
        "Your owner told you to sleep. Say one short sentence in your own words "
        "acknowledging it and going quiet. One sentence, no markdown."
    ),
    "cannot": (
        "Your owner asked:\n{detail}\nYou have no tool that can do this. Say one short "
        "sentence, in your own words, saying you cannot do that and what you could do "
        "instead. Never list everything you can do - just the one thing you could do "
        "instead. Be matter of fact, do not grovel. One sentence, no markdown."
    ),
    "chat": (
        "You are in chat mode, so you cannot touch the machine. Your owner asked:\n"
        "{detail}\nSay one short sentence in your own words that this needs work mode, "
        "and offer to switch. One sentence, no markdown."
    ),
    "setting": (
        "You just changed one of your own settings ({detail}). Say one short sentence in "
        "your own words confirming it, without inventing anything else. One sentence."
    ),
    "brief": (
        "Your owner has just asked you to write something long:\n{detail}\n"
        "Say one short line, out loud, that acknowledges you are starting, admits "
        "honestly that writing it will take a little while, and asks them how long it "
        "should be with two concrete choices in the question. Do not write any of the "
        "document, do not plan it out loud, no markdown, no lists, no line breaks."
    ),
}

# The conversation prompt, used when the request needs no action: the reply is
# streamed, split into sentences and spoken while it is still being written, so it
# has to read well out loud. This is the whole of the assistant's small talk - there
# is no list of greetings behind it and no canned acknowledgement in front of it.
# One string, not a list: it is pasted straight into the system message together with
# the owner's notes below, so it has to be a string already.
CHAT_SYSTEM = "".join((
    "You are Retro, a friendly voice assistant on your owner's Windows laptop, talking "
    "to them out loud. Be a person, not a manual - a sharp friend who happens to live "
    "in their laptop.",
    "\n\nHow you answer:\n"
    "- One or two short sentences for a simple answer, three when it genuinely needs "
    "them: the answer, anything truly useful beside it, then stop. Medium - not two "
    "clipped words, and never a lecture. Everything is spoken aloud.",
    "- Never list what you can do, never advertise a feature, never suggest other "
    "things they might ask, and never end with 'do you want anything else', 'anything "
    "else I can help with', 'let me know if' or any offer to do more. When the answer "
    "is finished, you are finished.",
    "- Warm and plain: no 'Certainly', no 'Sure thing', no apologising, no repeating "
    "their question back, no filler before the answer. No markdown, no lists, no "
    "emoji, no code - it is spoken, not read.",
    "- You remember this conversation: what was said earlier is still in play, so use "
    "it instead of asking again, and refer to it plainly ('the one you mentioned "
    "earlier'). If you learn something about them worth keeping - a preference, a "
    "person, a date, a plan - save it by ending your reply with "
    "<remember>the fact in one short line</remember>. It is removed before you are "
    "heard, so it never counts as part of your answer, and do it only when there is "
    "really something to keep.",
    "- The notes block below is what you already know about them: use it when it is "
    "relevant, never recite it as a list, never open with it.\n\n"
    "If something needs a tool rather than an answer, say briefly what you would do in "
    "work mode instead of pretending it is done."
))


def _same_words(text):
    """A line flattened to bare lowercase words, for comparing two turns of a conversation.

    Punctuation goes too, so that "Commands." and "commands" are the same question: the
    recogniser is the thing adding the full stops, and the model is copying a question
    it has been asked and answered already.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " "
                      for ch in str(text or "").lower())
    return " ".join(cleaned.split())


def build_chat_messages(text, history=()):
    """The prompt for a conversational reply: the rules, the past, the request.

    Three things are in front of the model every time: the tone above, what the owner
    said earlier in this conversation, and what Retro already knows about them. A
    memory that is not in the prompt cannot be used, which is the whole reason recall
    lives here rather than in a separate lookup the model would have to remember to do.

    A question that has already been asked is left out of the past, and that is not
    tidiness. Given its own earlier answer to the same question, a small model *copies
    it* rather than answering: the live log has the owner's session answering "Commands."
    with "Open apps, set reminders, adjust volume." - the very answer it had already
    given to the previous identical request, because that pair was in its history. The
    owner's complaint was in as many words: it repeats lines.
    """
    messages = [{"role": "system", "content": CHAT_SYSTEM + memory.prompt()}]
    asked = {_same_words(text)}
    for question, answer in history:
        key = _same_words(question)
        if not key or key in asked:
            continue                  # the same question twice is why the model repeats
        asked.add(key)
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": text})
    return messages


def build_line_messages(purpose, detail=""):
    """One prompt for one short spoken sentence the model writes itself."""
    rule = LINE_RULES.get(purpose, LINE_RULES["working"]).format(detail=detail or "a request")
    return [{"role": "system", "content": rule},
            {"role": "user", "content": "Now" if not detail else f"Now. (what I am doing: {detail})"}]


def first_sentence(text, limit=180):
    """One clean spoken sentence out of a model reply, or ''.

    Models answer in bullets, in quotes, or with a label in front. All of that is
    stripped here, because everything downstream is spoken aloud.
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.split("\n")[0].strip().strip('"').strip("'").strip("`").strip()
    cleaned = re.sub(r"^(?:retro|assistant)\s*[:\-]\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:[-*\u2022]|\d+[.)])\s*", "", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for stop in (". ", "! ", "? "):
        at = cut.rfind(stop)
        if at > 40:
            return cut[:at + 1]
    return cut.rstrip() + "..."

_REMEMBER_MARK = re.compile(r"<remember>\s*(.*?)\s*</remember>", re.S | re.I)


def keep_fact(fact):
    """Writes one fact to the owner's notes file. Never raises into a reply.

    A note that cannot be written costs the answer nothing: it is logged by length and
    forgotten, because a memory failing to save must not stop the sentence being
    finished or spoken.
    """
    try:
        saved, _message = memory.remember(fact)
        log.event("memory_from_chat", saved=saved, chars=len(str(fact or "")))
        return saved
    except Exception as exc:
        log.event("memory_from_chat", saved=False, error=type(exc).__name__)
        return False


class _RememberTagger:
    """Takes `<remember>fact</remember>` out of a reply while it is still streaming.

    The conversation path has no tools - a question is answered, never planned - so
    the only way to keep a fact the owner mentions mid-chat is for the model to mark it
    in its own answer. The mark must disappear before a sentence reaches the voice, and
    streamed pieces do not respect tag boundaries: `<remem` in one piece and
    `ber>my sister is Ria</remember>` in the next is the normal case, not a corner.
    """

    OPEN = "<remember"
    CLOSE = "</remember>"
    MAX_HOLD = 600                    # past this, no closing tag is coming

    def __init__(self):
        self._held = ""
        self.facts = []

    def feed(self, piece):
        """One streamed piece in, the text that may be spoken now, out."""
        text = self._held + str(piece or "")
        self._held = ""
        out, cursor = [], 0
        low = text.lower()
        while True:
            at = low.find(self.OPEN, cursor)
            if at < 0:
                break
            close = low.find(self.CLOSE, at + len(self.OPEN))
            if close < 0:
                break                      # it has not finished arriving
            out.append(text[cursor:at])
            opening = text.find(">", at)
            body = text[opening + 1:close] if 0 <= opening < close else text[at:close]
            fact = " ".join(body.split())
            if fact:
                self.facts.append(fact[:300])
            cursor = close + len(self.CLOSE)
        rest = text[cursor:]
        at = rest.lower().find(self.OPEN)
        if at >= 0:
            if len(rest) - at <= self.MAX_HOLD:
                # The opening has arrived and the closing has not: hold the tail back
                # and try again with the next piece.
                out.append(rest[:at])
                self._held = rest[at:]
            else:
                # No closing tag is coming. The words are still the answer - only the
                # marker is dropped, so a malformed mark truncates nothing.
                opening = rest.find(">", at)
                out.append(rest[:at] + (rest[opening + 1:] if opening >= 0
                                        else rest[at + len(self.OPEN):]))
            return "".join(out)
        # No mark in sight: hold back whatever could be the start of one - `<r`,
        # `<remember`, `<remember>my sis` - because the next piece completes it.
        hold = 0
        for size in range(min(len(self.OPEN) - 1, len(rest)), 0, -1):
            if rest[-size:].lower() == self.OPEN[:size]:
                hold = size
                break
        out.append(rest[:len(rest) - hold] if hold else rest)
        if hold:
            self._held = rest[len(rest) - hold:]
        return "".join(out)

    def flush(self):
        """Whatever is held when the reply ends - minus an unfinished mark."""
        held, self._held = self._held, ""
        # Only an unfinished mark is ever held, so dropping from the last '<' can only
        # lose the marker - and reading "<remember" out loud would be worse.
        at = held.rfind("<")
        return held[:at] if at >= 0 else held


# The plan is one line per step: the tool name, then its arguments after a pipe.
# A JSON plan was tried first and abandoned: a 1B model on a laptop CPU ran out of
# tokens in the middle of a six-step plan and produced a malformed object, and it
# dropped half of "open notepad and type hello" even when it did parse. Lines cost
# a few tokens instead of dozens, which is also why they are ten seconds faster.
RULES = """You are Retro, a voice assistant that controls a Windows laptop for its owner.
Write the steps that carry the request out, one step per line, in this exact form:

tool_name | argument

Tools:
{tools}
- say | words: speak a short sentence to the user
- answer | words: your reply when no action is needed, in two or three sentences
- compose | subject | length: write something long - a documentary, essay, article,
  story, script, report, guide or letter

{apps}

Rules:
- One to eight steps. Every step names one tool from the list above.
- open_app only opens a program. Everything done *inside* an application is
  app_function, and it is almost always one step instead of six: music and video
  (spotify, youtube, crunchyroll) use search_play; a message uses send_message with
  the person in target and the words in detail; text in a document uses write_text;
  a game uses launch_game.
- A request to BUILD or CODE something - a website, an app, a game, a script - never
  comes here: Retro writes those files itself, in silence, and opens the editor when
  they are finished. Never open VS Code, an editor or a browser to do one.
- Use the application the request names, and the default where it does not: "the
  music app" is the one marked as the default music app, "minecraft" is the launcher
  in the list, never the website. Do not open a download page when the app is
  already installed.
- Every part of the request must appear as a step. "open X, type Y and send" is
  three or more steps and none of them may be left out.
- Always focus_window the window you are about to work in, and always before
  type_text, press_keys or click_text. Nothing is typed into a window that has not
  been named, so a step without it is refused. open_app opens a program but does
  not put it in front, so a step that types or clicks always needs focus_window
  first, even when the request says "open X and type Y".
- click_text clicks a real control by the label it shows: a button, a link, a box.
  The label must be something you have seen in read_screen, or a control you are
  sure every window has, such as "Search". Never click a word merely because the
  user said it: for "play the song sunflower" there is no control called
  "song" - read_screen first, then click the search box, then type the name.
- read_screen shows the labels in the front window. Use it before clicking when you
  are unsure of the label on the button you need.
- When a window shows no labels at all (a canvas, a browser page, a picture), the
  screen itself is readable: `screenshot` saves a picture, `find_on_screen` finds a
  picture from the workspace on it, and `click_at` presses that point with Retro's
  own pointer - which never moves the user's mouse. The window must be aimed with
  focus_window first, or named in click_at's `window` argument, and a point over any
  other window is refused. Prefer read_screen and click_text while labels exist; the
  pointer is for when they do not.
- A question, a greeting, small talk or a request for your opinion needs no action
  on the machine: reply with `answer | your reply`. This is how you talk to the
  user - answer in your own words, in the language they used, and never refuse a
  question you can answer. Use `say | words` only to narrate a long job.
- A request to WRITE something of real length - a documentary, article, essay,
  story, script, report, guide, letter, plan - is ONE compose step. Never put a
  document inside `answer`: it is read out loud, so an essay there is a minute of
  speech arriving all at once, and it will be refused before it runs.
- `answer` holds a short reply: one or two sentences, three at most, no lists and
  no headings. It is your voice, not a page - medium length, then stop.
- Answer like a friend, not a manual: warm, plain, natural. Never list what you can
  do, never advertise a feature, never suggest other things the owner could ask, and
  never end a reply with "do you want anything else", "anything else" or an offer to
  do more. When the answer is done, you are done. No "Certainly", no apology, no
  repeating their words back, no filler before the answer.
- The notes block (if there is one) is what you already know about your owner: use it
  when it matters and never recite it as a list. When they tell you something about
  themselves worth keeping - a preference, a person, a plan, a date - add a
  `remember | the fact in one short line` step after the answer.
- Use open_website for a site the user names by name or address, and open_app for a
  program installed on the laptop.
- If the request cannot be done with these tools, reply with one `answer | ...`
  line explaining that, rather than writing nothing.
- Write the steps only. No prose, no numbering, no explanation, no markdown.
"""

# Worked examples are real chat turns, not a block of text in the system prompt. The
# block version was copied: the model planned the request and then carried on
# reading the examples out as if they were part of its own answer, so a plan for
# "open notepad" arrived with the answers to five other requests stapled to it.
FEW_SHOT = (
    ("open notepad and type hello there",
     "open_app | notepad\nfocus_window | notepad\ntype_text | hello there"),
    ("find the song sunflower in spotify and play it",
     "app_function | spotify | search_play | sunflower"),
    ("message JyotiBoruah that hey maam, I am your son's personal assistant",
     "app_function | whatsapp | send_message | hey maam, I am your son's personal "
     "assistant | JyotiBoruah"),
    ("write me a note about black holes in notepad",
     "app_function | notepad | write_text | Black holes are regions where gravity is so "
     "strong nothing escapes. | black holes"),
    ("open vs code", "open_app | vs code"),
    ("open minecraft",
     "app_function | legacy_launcher | launch_game"),
    ("open chatgpt and type this is retro talking to me and send it",
     "open_website | chatgpt.com\nwait | 5\nfocus_window | chatgpt\nread_screen | 30\n"
     "click_text | message\ntype_text | this is retro talking to me\npress_keys | enter"),
    ("what is on my screen right now", "read_screen | 30"),
    ("click at 900, 600 in notepad",
     "focus_window | notepad\nclick_at | 900 | 600 | notepad"),
    ("what is the time right now",
     'answer | I read the clock with a tool, so ask me "what time is it".'),
)


class PlanError(ValueError):
    """The model did not produce a plan that can be trusted."""


def build_messages(text, extra=None, history=()):
    """The planner's prompt: the rules, the real tool list, the apps, the request.

    `history` is what was said earlier in this conversation. Without it "and type
    it" arrives with no idea what "it" is, which is exactly how a request that
    refers back to the last one used to become a brand new guess.

    The applications are part of the prompt for the same reason the tools are: the
    catalogue is the assistant's knowledge of what each program is for and what it
    can do, so a request for music is a choice from a known list rather than a
    workflow invented from scratch every time. This is what the user asked for when
    they said to reduce the reasoning around routine commands - not to remove it.
    """
    guide = (tools.catalogue() + "\n"
             + "\n".join(f"- {name} (text: text): {what}"
                         for name, what in SPEECH_TOOLS.items())
             + "\n" + DOC_HINT)
    # The owner's notes go in front of the plan as well as the chat: "open it" after
    # "my bank is ICICI" only works if the model was told the fact before it chose a
    # step, and a recall the model has to remember to ask for is a recall it never does.
    messages = [{"role": "system",
                 "content": RULES.format(tools=guide, apps=registry.catalogue())
                            + memory.prompt()}]
    for question, answer in FEW_SHOT:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    for question, answer in history:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    if extra:
        messages.append({"role": "user", "content": extra})
    messages.append({"role": "user", "content": text})
    return messages


# Twelve, not eight. "open whatsapp, find Developer, type this" is already nine
# steps once read_screen and the two focus_window calls are in it, and the plan used
# to be truncated at ten - which silently dropped the press_keys that actually sent
# the message. A plan that stops just short of finishing is worse than a long one.
MAX_STEPS = 12
_LINE = re.compile(r"^([a-z_]+)\s*(?:\|\s*(.*))?$", re.I)
_MARKER = re.compile(r"^(?:\d+[.)]|[-*\u2022])\s*")


def known_step(name):
    """True for a step the model is allowed to write: a real tool, or a speech step."""
    return name in tools.TOOLS or name in SPEECH_TOOLS or name in DOC_TOOLS


def _args_from_line(name, rest):
    """The arguments on a step line, in the order the tool declares them."""
    if rest.startswith("|"):
        rest = rest[1:]
    if name in DOC_TOOLS:
        pieces = [piece.strip() for piece in rest.split("|")]
        subject = pieces[0] if pieces else ""
        if not subject:
            raise PlanError(f"{name} needs something to write about")
        return {"subject": subject[:200], "length": (pieces[1] if len(pieces) > 1 else "")[:60]}
    if name in SPEECH_TOOLS:
        words = rest.strip()
        if not words:
            raise PlanError(f"{name} needs something to say")
        return {"text": words[:400]}
    tool = tools.TOOLS[name]
    keys = list(tool.args)
    pieces = rest.strip().split("|", maxsplit=max(0, len(keys) - 1)) if rest.strip() else []
    given = {}
    for index, key in enumerate(keys):
        piece = pieces[index].strip() if index < len(pieces) else ""
        if piece:
            given[key] = piece                  # empty means "use the default"
    try:
        return tools.validate_args(tool, given)
    except ValueError as exc:
        raise PlanError(f"{name}: {exc}")


def _steps_from_json(reply):
    """The JSON shape, kept as a fallback because a model may insist on writing it."""
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
        return None
    steps = []
    for item in payload["steps"]:
        if not isinstance(item, dict):
            return None
        name = str(item.get("tool") or "").strip().lower()
        args = item.get("args") or {}
        if not known_step(name):
            return None
        if name in DOC_TOOLS:
            body = args if isinstance(args, dict) else {}
            subject = str(body.get("subject") or "").strip()
            if not subject:
                return None
            steps.append((name, {"subject": subject[:200],
                                 "length": str(body.get("length") or "")[:60]}))
            continue
        if name in SPEECH_TOOLS:
            text = args.get("text") if isinstance(args, dict) else None
            if not isinstance(text, str) or not text.strip():
                return None
            steps.append((name, {"text": text.strip()[:400]}))
            continue
        try:
            steps.append((name, tools.validate_args(tools.TOOLS[name], args)))
        except ValueError:
            return None
    return steps


def parse_steps(reply):
    """Turns the model's reply into [(tool, args)] - or raises PlanError.

    Only tools that exist survive, and only with arguments their spec allows, so a
    made-up step cannot reach the machine.
    """
    if not reply or not isinstance(reply, str):
        raise PlanError("empty reply")
    steps = []
    for raw in reply.splitlines():
        line = _MARKER.sub("", raw.strip().strip("`").strip()).strip()
        if not line or line.startswith(("#", "//", "{")):
            continue
        match = _LINE.match(line)
        if match:
            name, rest = match.group(1).lower(), match.group(2) or ""
        else:
            parts = line.split(None, 1)         # tolerate "open_app spotify"
            name, rest = parts[0].lower(), parts[1] if len(parts) > 1 else ""
        if not known_step(name):
            continue          # a tool that does not exist is dropped, never run
        try:
            steps.append((name, _args_from_line(name, rest)))
        except PlanError:
            continue          # so is a step whose arguments do not add up
        if len(steps) > MAX_STEPS:
            return steps[:MAX_STEPS]
    if not steps:
        if not reply.strip():
            return []            # the documented way to say "this cannot be done"
        from_json = _steps_from_json(reply)
        if from_json is None:
            raise PlanError("no steps in the reply")
        return from_json[:MAX_STEPS]
    return steps


class Agent:
    """Runs one request as a plan of tool calls, with eyes on the result.

    `complete` is the language model: it takes messages and a token budget and
    returns raw text. `speak` is how a `say` step reaches the user's ears. Both are
    injected so this class can be tested without either.
    """

    def __init__(self, complete=None, speak=None, confirm=None, plan_tries=2,
                 history_turns=None, conversational=False, stream=None,
                 brief=None, on_writing=None):
        self.complete = complete
        # Asked before a document is written: the user's answer about its length, from
        # the session's ears. None means nobody can be asked, and the writing goes
        # ahead at the length Retro proposed.
        self.brief = brief
        # The window's own view of a document being written, one event at a time.
        self.on_writing = on_writing
        # A streaming completion, when the brain has one. This is what lets an answer
        # be spoken sentence by sentence while it is still being written; without it
        # the reply arrives in one piece and is spoken after it lands.
        self.stream = stream
        self.speak = speak or (lambda text: None)
        self.confirm = confirm
        self.plan_tries = plan_tries            # one retry when the reply isn't a usable plan
        self.history_turns = (config.CLOUD_HISTORY_TURNS if history_turns is None
                              else history_turns)
        # True when the model behind `complete` is good enough to hold a
        # conversation rather than only name a command. A 1B local model is not:
        # asked to "answer" it produces confident nonsense, so it is kept to tools.
        self.conversational = conversational
        self.history = []                       # [(the request, the plan written for it)]

    @property
    def available(self):
        return self.complete is not None

    # ---------- what was said before ----------------------------------------

    def _past(self):
        if not self.conversational or self.history_turns <= 0:
            return []
        return self.history[-self.history_turns:]

    def _remember(self, text, reply):
        """Keep the last few exchanges, so a follow-up can refer back to them."""
        if not self.conversational or self.history_turns <= 0:
            return
        self.history.append((text, reply))
        del self.history[:-self.history_turns]

    def forget(self):
        """Starts a new conversation - the wake word after a sleep, or a reset."""
        self.history.clear()

    def line(self, purpose, detail="", fallback=""):
        """One short spoken sentence about itself, written for this moment.

        With a real brain the sentence is composed now; without one the caller's
        written line is used, so a machine with no key still says something. Never
        raises: a line that cannot be written must not cost the request its reply.
        """
        if not self.conversational or self.complete is None:
            return fallback
        try:
            raw = self._ask_model(build_line_messages(purpose, detail), max_tokens=160)
        except Exception:
            return fallback
        return first_sentence(raw) or fallback

    def explain_failure(self, request):
        """Why a request could not be carried out, in the model's own words."""
        return self.line("cannot", detail=request, fallback="")

    # ---------- planning ----------------------------------------------------

    # A reasoning model spends part of this budget thinking before it writes a
    # single step, so the allowance is the plan plus room for the thinking. At 200
    # the shorter plans came back fine but the longer ones were at risk of returning
    # an empty `content`, which is an error rather than a plan.
    def _ask_model(self, messages, max_tokens=320):
        complete = self.complete
        try:
            return complete(messages, max_tokens)
        except TypeError:
            return complete(messages)           # a plain callable, as in tests

    def plan(self, text, task=None, history=None):
        """The steps for `text`, or [] when nothing can be done."""
        if not self.available:
            raise PlanError("no model")
        complaint = ""
        past = self._past() if history is None else history
        for _ in range(max(1, self.plan_tries)):
            if task is not None:
                task.check()
            reply = self._ask_model(build_messages(text, complaint, past))
            try:
                steps = parse_steps(reply)
                self._remember(text, reply)
                return steps
            except PlanError as exc:
                complaint = (f"That could not be used ({exc}). Write only the steps for this "
                             f"request, one per line, as tool_name | argument. Nothing else.")
                log.event("plan_refused", reason=str(exc))
        raise PlanError(complaint or "no usable plan")

    def replan(self, text, done, failure, task=None):
        """What to do next, told what has already happened and what went wrong."""
        if not self.available:
            return []
        story = ("Already done:\n" + "\n".join(f"- {line}" for line in done)
                 if done else "Nothing has been done yet.")
        ask = (f"{story}\nThat step failed: {failure}\n"
               f"Reply with the remaining steps for the request as a JSON object with a "
               f"'steps' list, or {{\"steps\": []}} if it cannot be done.")
        try:
            reply = self._ask_model(build_messages(text, ask, self._past()))
            return parse_steps(reply)
        except PlanError:
            return []

    # ---------- writing something long ---------------------------------------

    def compose(self, subject, length="", task=None, ask=None, resume=None):
        """Writes a document: the brief, then the sections, out loud as they are made.

        The order is the whole point. One line first - what Retro is about to do, how
        long it may take, and one question about the length - so the first sound
        arrives immediately instead of a minute later. The answer, if there is one,
        sets the target; silence is allowed and means the length Retro proposed, and
        when nobody answers it now says which length it is going with instead of
        leaving the question hanging.

        Then the document is written section by section, with every finished sentence
        spoken as it is written and the growing text handed to the window, so a long
        job is visibly happening rather than silently pending.

        `resume` is a document that was stopped part way. The length and the outline
        were settled in the first sitting, so there is no brief and no question: one
        line saying what is left, then the remaining sections, and a stopped job
        keeps its words instead of losing them.
        """
        draft = dict(resume) if isinstance(resume, dict) else None
        subject = " ".join(str(subject or (draft or {}).get("request") or "").split())
        if not subject:
            return Result(False, "I need to know what to write about.")
        if not self.available or self.complete is None:
            return Result(False, "I need my language model to write that, and it isn't loaded.")
        if draft:
            seen = len(draft.get("sections") or [])
            total = len(draft.get("outline") or [])
            self.speak(compose.resume_line(draft))
            self._writing({"kind": "resume", "title": draft.get("title") or subject[:70],
                           "done": seen, "total": total,
                           "words": compose.clamp(draft.get("words"))})
        words = compose.clamp((draft or {}).get("words") or compose.DEFAULT_WORDS)
        if not draft:
            asked = ask if ask is not None else self.brief
            opening = self.line("brief", detail=subject, fallback=compose.fallback_brief())
            if opening:
                self.speak(opening)
            answer = ""
            if asked is not None:
                try:
                    answer = asked(opening, subject) or ""
                except Stopped:
                    return Result(False, "Stopped.")
                except Exception as exc:         # a failed question must not lose the job
                    log.event("brief_failed", error=type(exc).__name__)
            words = compose.parse_length(answer or length, compose.DEFAULT_WORDS)
            if asked is not None and not str(answer).strip():
                # Silence is allowed, but it is not left mysterious: the user is told
                # which length the writing is going ahead at, right after the question
                # they did not answer.
                self.speak(f"No answer, so I'll write it {compose.describe(words)}.")
            self._writing({"kind": "brief", "title": subject[:70], "words": words})
        sent = {"chars": 0}

        def partial(text):
            """The growing section, to the window - but only when it has really grown."""
            if len(text) - sent["chars"] < 24:
                return
            sent["chars"] = len(text)
            self._writing({"kind": "writing", "text": text})

        try:
            document = compose.write_document(
                request=subject, words=words, complete=self._ask_model, stream=self.stream,
                on_sentence=self.speak, on_partial=partial, on_event=self._writing, task=task,
                resume=draft)
        except Stopped:
            return Result(False, "Stopped.")
        except Exception as exc:
            log.event("compose_failed", error=type(exc).__name__)
            return Result(False, "My writing failed there - the language model didn't answer.")
        if task is not None:
            task.touch(f"writing {document['filename']}" if document.get("filename")
                       else f"paused: {document.get('title') or subject}")
            task.steps += 1
        if document.get("stopped"):
            return Result(False, document["message"])
        return Result(True, document["message"], verified=True)

    def build(self, request, task=None, on_step=None):
        """A coding project: its folder, its files, its report - and the editor last.

        Not a plan. `app_function | vscode | create_project` would put the editor in
        front of the owner while the project is still half written; the whole job is
        done here, quietly, and VS Code is opened on the finished folder afterwards.
        The steps are still reported one at a time through `on_step`, so the labels
        beside the circle name the file being written rather than inventing activity.
        """
        if not self.available or self.complete is None:
            return Result(False, "I need my language model to write code, and it isn't loaded.")

        def progress(event):
            detail = str(event.get("detail") or "")
            if on_step is None or not detail.startswith("writing "):
                return
            try:
                on_step("write_file", {"path": detail[len("writing "):]})
            except Exception:               # a label must never break a build
                pass

        try:
            result = builder.build(request, self._ask_model, task=task, on_event=progress)
        except Stopped:
            return Result(False, "Stopped.")
        except Exception as exc:
            log.event("build_failed", error=f"{type(exc).__name__}: {exc}"[:160])
            return Result(False, "My coding failed there - the language model didn't answer.")
        if task is not None:
            task.touch("built " + (result.message.split(" - ")[0][:40] if result.ok else ""))
        return result

    def _writing(self, event):
        """One document event for the window, if there is one. Never raises."""
        if self.on_writing is None or not isinstance(event, dict):
            return
        try:
            self.on_writing(dict(event))
        except Exception:                        # a window that cannot update is not fatal
            pass

    def _speak_pieces(self, text):
        """A written reply, handed to the voice one sentence at a time.

        A short answer is one piece and arrives as before. A longer one is heard as
        it is delivered rather than in a single breath at the end, which is the same
        reason the sections of a document are streamed.
        """
        stream = SentenceStream()
        for sentence in stream.feed(text) + stream.flush():
            if sentence:
                self.speak(sentence)

    # ---------- doing ------------------------------------------------------

    def respond(self, text, task=None, emit=None):
        """A conversational reply, spoken in pieces as it is written.

        `emit(sentence)` is called for each finished sentence, in order, which is how
        the first words are heard while the rest of the answer is still being
        generated. Deliberately not a plan: nothing here can touch the machine, so a
        question cannot accidentally become an action. Returns the whole reply.

        A `<remember>...</remember>` mark in the model's answer is taken out on the way
        past and written to the owner's notes file: this path has no tools, so it is
        the only way a fact mentioned in conversation can be kept. The mark is removed
        *before* a sentence is handed to the voice, even when it is split across two
        streamed pieces, so nothing unspoken-looking is ever read aloud.
        """
        if not self.available:
            raise PlanError("no model")
        splitter = SentenceStream()
        tagger = _RememberTagger()
        spoken = []
        messages = build_chat_messages(text, self._past())

        def collect(piece):
            for sentence in splitter.feed(tagger.feed(piece)):
                spoken.append(sentence)
                if emit is not None:
                    emit(sentence)

        if self.stream is not None:
            full = self.stream(messages, 400, collect)
        else:
            full = self._ask_model(messages, 400)
            collect(full)
        for sentence in splitter.feed(tagger.flush()) + splitter.flush():
            spoken.append(sentence)
            if emit is not None:
                emit(sentence)
        for fact in tagger.facts:
            keep_fact(fact)
        if task is not None:
            task.touch("answering")
        reply = " ".join(spoken).strip() or first_sentence(_REMEMBER_MARK.sub("", str(full)))
        self._remember(text, reply)
        return reply

    def run(self, text, task=None, note=None, mode=None):
        """Plans and runs one request. Returns the Result the user hears."""
        note = note or (lambda *_args: None)
        try:
            plan = self.plan(text, task)
        except PlanError:
            if not self.available:
                return Result(False, "I need my language model for that, and it isn't loaded.")
            # A real brain says why in its own words instead of reading a line off a
            # list. The written ones stay as the fallback for the offline model.
            spoken = self.explain_failure(text) if self.conversational else ""
            return Result(False, spoken or "I couldn't work out how to do that.")
        except Stopped:
            return Result(False, "Stopped.")
        except Exception as exc:
            log.event("plan_failed", error=type(exc).__name__)
            return Result(False, "My thinking failed there - the language model didn't load.")
        if not plan:
            return Result(False, "I don't know how to do that yet.")
        return self._execute(text, plan, task, note, mode=mode)

    def _execute(self, text, plan, task, note, mode=None):
        done, spoken, revisions, spoken_lines = [], [], 0, []
        while plan:
            if task is not None:
                try:
                    task.check()
                except Stopped:
                    return Result(False, "Stopped.")
            name, args = plan.pop(0)
            tool = tools.TOOLS.get(name)
            if mode is not None and tool is not None and not modes.allows_tool(mode, tool):
                # Chat mode, and the plan reached for something that would change the
                # machine. The step is refused here, where it would have run - not
                # filtered out of the plan, so the user is told why and offered the
                # switch instead of watching half a task disappear.
                log.event("mode_blocked", tool=name, mode=mode)
                said = self.line("chat", detail=text) if self.conversational else ""
                return Result(False, said or f"I'm in chat mode, so I didn't {name}. Say "
                                              f"'work mode' and I'll do it.")
            if name in DOC_TOOLS:
                if mode is not None and not modes.allows_tools(mode):
                    # Writing a document is a change to this machine: it lands as a
                    # file in the workspace, so chat mode refuses it like anything else.
                    log.event("mode_blocked", tool=name, mode=mode)
                    said = self.line("chat", detail=text) if self.conversational else ""
                    return Result(False, said or "I'm in chat mode, so I didn't write it. "
                                                  "Say 'work mode' and I'll do it.")
                return self.compose(args.get("subject", ""), args.get("length", ""), task=task)
            if name in SPEECH_TOOLS:
                word = args["text"]
                if name == "answer":
                    # Spoken here, sentence by sentence, instead of being handed back
                    # as one block to read out at the end: a longer reply is heard as
                    # it is delivered, and the same words are never said twice.
                    self._speak_pieces(word)
                    return Result(True, word)
                self.speak(word)
                spoken_lines.append(word)
                task and task.touch("I said something")
                note("say", args)
                continue
            note(name, args)
            # `mode` is only passed when there is one, so a caller (or a test) that
            # stands in for the registry is not handed an argument it never had.
            extra = {"mode": mode} if mode is not None else {}
            result = tools.run(name, args, task=task, confirm=self.confirm, **extra)
            observation = f"{name} -> {result.message}"
            done.append(observation)
            if task is not None:
                task.touch(result.message[:60])
                task.steps += 1
            if not result.ok:
                if revisions >= 1:
                    return self._failed(result, done)
                revisions += 1
                plan = self.replan(text, done, observation, task)
                if not plan:
                    return self._failed(result, done)
                continue
            spoken.append(result.message)
        if spoken_lines:
            return Result(True, spoken_lines[-1])
        return Result(True, self._summary(spoken))

    @staticmethod
    def _summary(spoken):
        """A short spoken reply from what the last steps reported."""
        for line in reversed(spoken):
            if line and len(line) <= 120:
                return line
        return "Done."

    @staticmethod
    def _failed(result, done):
        return Result(False, f"I couldn't finish that. {result.message}")
