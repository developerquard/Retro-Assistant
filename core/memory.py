"""What Retro knows about its owner, kept on this machine's desktop and nowhere else.

The request was specific: if Retro finds out something important - a preference, a
person, a plan - it should save it, on the desktop, and recall it when it is needed
later. So:

* **One file, on the Desktop**: `<Desktop>/Retro memory.md`. Plain text, readable and
  editable by hand, with the date on every line so the owner can see what was learned
  when. Nothing is written anywhere else, and nothing leaves this file except the few
  lines pasted into the prompts Retro already sends to its own brain.
* **Newest first in the prompt, last on disk.** A memory that grows forever would make
  every reply slower, so `prompt()` shows the most recent `MAX_PROMPT_LINES` and the
  rest stays on disk where `recall()` can search it.
* **Nothing but the fact is stored.** The log never sees the text - only its length -
  for the same reason the rest of this codebase keeps note text and queries out of it.

Saving is deliberately unglamorous: one line appended, duplicates ignored, and a file
that can be deleted at any time without breaking anything. It is a note the assistant
keeps, not a database.
"""
import datetime as dt
import re
from pathlib import Path

from . import config, log

MAX_FILE_LINES = 400            # older lines stay in the file but stop being relevant
MAX_PROMPT_LINES = 14           # pasted into every prompt: keep it cheap and short
MAX_RECALL = 8
_LINE = re.compile(r"^\s*[-*]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*:?\s*(?P<text>.+)$")


def folder():
    """The owner's Desktop - the folder the memory was asked to live in.

    A machine with no Desktop still gets one: the workspace, so a note is never lost
    because of where the profile happens to be.
    """
    home = Path.home()
    for name in ("Desktop", "desktop"):
        candidate = home / name
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return Path(config.WORKSPACE) / "memory"


def path():
    """The memory file itself."""
    return folder() / "Retro memory.md"


def entries(limit=None):
    """Every fact, newest first: [{"date": ..., "text": ...}, ...]."""
    try:
        raw = path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found = []
    for line in raw:
        match = _LINE.match(line)
        if match:
            found.append({"date": match.group("date"),
                          "text": match.group("text").strip()})
    found.reverse()
    if limit:
        found = found[:max(1, int(limit))]
    return found


def _key(text):
    """What counts as 'the same fact' when deciding whether to save it again."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return " ".join(cleaned.split())


def remember(text):
    """Keeps one fact. Returns (saved, message) - the message is what is spoken.

    Duplicates are refused rather than stored twice: a memory file that repeats itself
    is a memory file nobody reads. The message is short on purpose - the reply to
    "remember this" is not the place for a paragraph.
    """
    fact = " ".join(str(text or "").split())
    if not fact:
        return False, "I didn't catch anything to remember."
    if len(fact) > 400:
        fact = fact[:397].rstrip() + "..."
    key = _key(fact)
    stamp = dt.date.today().isoformat()
    try:
        target = path()
        lines = [line for line in target.read_text(encoding="utf-8").splitlines()
                 if line.strip()] if target.exists() else []
        body = [line for line in lines if not line.startswith("#")]
        for line in body:
            match = _LINE.match(line)
            if match and _key(match.group("text")) == key:
                return True, "I already have that one."
        heading = (lines[0] if lines and lines[0].startswith("#")
                   else "# What Retro knows about you")
        target.parent.mkdir(parents=True, exist_ok=True)
        body.append(f"- {stamp}: {fact}")
        # Trimmed rather than appended forever: the oldest lines drop off, the file
        # stays something a person can read, and a duplicate never lands in it.
        target.write_text("\n".join([heading, *body[-MAX_FILE_LINES:]]) + "\n",
                          encoding="utf-8")
    except OSError as exc:
        log.event("memory_save_failed", error=type(exc).__name__, chars=len(fact))
        return False, "I couldn't write that to your desktop."
    log.event("memory_saved", chars=len(fact), count=len(entries()))
    return True, "Noted, on your desktop."


def recall(query=None, limit=MAX_RECALL):
    """The facts that match `query` (or just the newest ones): a list of strings.

    Matching is deliberately plain - the words of the question against the facts,
    newest first. This is called on the way to an answer, not instead of thinking.
    """
    found = entries()
    wanted = [_key(word) for word in str(query or "").split() if len(_key(word)) > 2]
    if not wanted:
        return [entry["text"] for entry in found[:max(1, int(limit))]]
    scored = []
    for entry in found:
        words = set(_key(entry["text"]).split())
        hits = sum(1 for word in wanted if word in words)
        if hits:
            scored.append((hits, entry["text"]))
    return [text for _hits, text in scored[:max(1, int(limit))]]


def _words(text):
    """The words that matter in a request to forget something.

    "forget that I like tea" has to match the fact "likes tea", so the scaffolding
    words - the ones every request has and no fact relies on - are dropped before
    matching. What is left must all be present: forgetting the wrong note is worse
    than forgetting nothing.
    """
    skip = {"the", "and", "that", "this", "about", "from", "what", "you", "know",
            "remember", "forget", "forgot", "please", "retro", "your", "our", "with",
            "have", "has", "had", "was", "were", "for", "its", "it's"}
    return [word for word in _key(text).split() if len(word) > 2 and word not in skip]


def forget(query):
    """Removes the facts that match `query`. Returns (removed_count, message).

    Every word of the request has to appear in the fact, which makes "forget my
    birthday" remove the birthday note and leave the rest alone. When nothing matches,
    the file is not rewritten at all - a failed forget must not be able to lose a line.
    """
    wanted = _words(query)
    if not wanted:
        return 0, "Tell me which note to forget, and I'll remove it."
    try:
        target = path()
        lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    except OSError as exc:
        log.event("memory_forget_failed", error=type(exc).__name__)
        return 0, "I couldn't open your notes file."
    kept, removed = [], 0
    for line in lines:
        match = _LINE.match(line)
        if match:
            words = set(_key(match.group("text")).split())
            if all(word in words for word in wanted):
                removed += 1
                continue
        kept.append(line)
    if not removed:
        return 0, "I don't have a note about that."
    try:
        target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError as exc:
        log.event("memory_forget_failed", error=type(exc).__name__)
        return 0, "I couldn't write your notes file, so nothing was removed."
    log.event("memory_forgotten", count=removed, left=len(entries()))
    return removed, ("Forgotten." if removed == 1 else f"Forgot {removed} notes.")


def prompt(max_lines=MAX_PROMPT_LINES):
    """The block pasted into every prompt, or '' when there is nothing to recall.

    Newest first, one line each, and nothing but the fact: this is read by the model
    on every single request, so it has to stay small enough to be free.
    """
    lines = entries(limit=max_lines)
    if not lines:
        return ""
    body = "\n".join(f"- {entry['date']}: {entry['text']}" for entry in lines)
    return ("\n\nWhat you remember about your owner (kept in your own notes file, "
            "recent first):\n" + body +
            "\nUse these when they are relevant and never repeat them back as a list.")


def forget_all():
    """For tests and for a clean start: the file is emptied, not archived."""
    try:
        path().unlink()
    except OSError:
        pass
    return True
