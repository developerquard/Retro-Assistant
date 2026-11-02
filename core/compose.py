"""Writing something long: the brief, the outline, then the sections.

This module exists because of one complaint, and it is worth writing down as it was
put: ask Retro for a documentary and it said nothing at all for a minute, then read
out an entire essay in one breath. No acknowledgement, no question, no sense that
anything was happening, and no way to say how long it should be. Writing is not a
command, and it should not behave like one.

So writing has three parts, in the order a person would do them:

* **the brief.** One line, written for the moment: what Retro is about to do, an
  honest hint that it will take a little while, and one question with two concrete
  lengths to choose from. The user answers it by voice if they want to; if they say
  nothing, the writing goes ahead at the length Retro proposed rather than hanging.
* **the outline.** Title, then three to six sections, each with a word count, so the
  document has a shape before a single word of prose exists.
* **the sections.** Written one at a time, each one streamed: every finished
  sentence is handed to the voice as it is written, and the growing text is handed to
  the window. That is what makes a long job visible from the first second instead of
  at the end - the writing is *watched*, not waited for.

The document is saved into Retro's workspace as markdown, and the spoken result says
the file's name and how many words are in it, so a document that was written can
also be found again.

Nothing here invents a length or claims a result it cannot check: the word count in
the reply is counted from the text that was actually written, and the path in the
reply is the path the file was really written to (core/files.py refuses anything
outside the workspace and that refusal is reported as it is).

The first version of this flow threw a stopped document away, which was its weakest
part: saying "stop" cost every word that had already been written. So there is now a
fourth part, invisible until it is needed - **the draft.** Every finished section is
written into a draft file the moment it exists, and "carry on writing" reads that
draft back and writes the remaining sections into the same document, without asking
for the length again. The finished document is still written once, at the end: a
stopped document is a draft, not a half-saved file.
"""
import json
import re

from . import files, log
from .control import Stopped

# The length Retro proposes when the user does not answer the brief. Long enough to
# be a real document, short enough to arrive in a few seconds.
DEFAULT_WORDS = 200
MIN_WORDS, MAX_WORDS = 60, 900
MAX_SECTIONS = 6
MIN_SECTION_WORDS = 40

# A request is a writing job when it names a writing verb *and* something whose whole
# point is length - a documentary, an essay, a story. Both halves are required, and
# naming an application to type into cancels it: "write a note in notepad" is the
# app_function workflow (a real file, opened in the editor), not a composition.
_VERB = re.compile(r"\b(?:write|compose|draft|author|pen|prepare|produce)\b", re.I)
_LONG_FORM = re.compile(
    r"\b(?:documentar(?:y|ies)|essay|article|blog(?: post)?|report|story|tale|script|"
    r"screenplay|poem|poetry|guide|tutorial|documentation|document|paper|thesis|speech|"
    r"letter|proposal|monograph|chapter|booklet|book|brochure|case study|summary|"
    r"review|comparison|whitepaper|white paper|walkthrough|overview)\b", re.I)
_APP_PLACE = re.compile(
    r"\b(?:notepad|word|wps|docs|google docs|vs ?code|chatgpt|gemini|claude|notion|"
    r"obsidian|editor|excel|email app|gmail|whatsapp)\b", re.I)

# What a spoken answer about length means. The words are the user's own; the number
# is only ever Retro's honest translation of them into a target.
_LENGTHS = (
    (re.compile(r"\b(?:very short|tiny|50|fifty)\b", re.I), 60, "very short"),
    (re.compile(r"\b(?:short|brief|concise|summary|100|a hundred)\b", re.I), 110, "short"),
    (re.compile(r"\b(?:medium|normal|standard|200|two hundred)\b", re.I), 220, "medium"),
    (re.compile(r"\b(?:long|detailed|detail|in[- ]depth|thorough|500|five hundred)\b", re.I), 500, "long"),
    (re.compile(r"\b(?:very long|full|complete|comprehensive|exhaustive|1000|"
                r"a thousand)\b", re.I), 800, "very long"),
)
_WORDS_NUMBER = re.compile(r"\b(\d{2,4})\s*(?:words?|word-length)\b", re.I)
_WORDS_HYPHEN = re.compile(r"\b(\d{2,4})[- ]word\b", re.I)
_PAGES = re.compile(r"\b(one|two|three|four|a|1|2|3|4)\s*pages?\b", re.I)
_PAGE_WORDS = {"one": 400, "a": 400, "1": 400, "two": 800, "2": 800,
               "three": 900, "3": 900, "four": 900, "4": 900}
_PARAGRAPH = re.compile(r"\b(one|two|three|a|1|2|3)\s*paragraphs?\b", re.I)
_PARAGRAPH_WORDS = {"one": 110, "a": 110, "1": 110, "two": 210, "2": 210,
                    "three": 320, "3": 320}

_MARK = re.compile(r"^\s*(?:[#>*\-•]|\d+[.)])\s*")
# A markdown heading of any depth, for reading a document out loud: "## Origins" is said
# as "Origins", not as "hash hash Origins".
_HEADING = re.compile(r"^#{1,6}\s*")
_PIPE = re.compile(r"^\s*(.+?)\s*(?:\||\u2014|--|:)\s*(\d{1,4})\s*(?:words?)?\s*$", re.I)


def looks_like_writing(text):
    """True when the request is to write something long rather than to type a note."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned.split()) < 3:
        return False
    if not _VERB.search(cleaned) or not _LONG_FORM.search(cleaned):
        return False
    return _APP_PLACE.search(cleaned) is None


def wants_document(text):
    """The session's own wording: the request is a composition job."""
    return looks_like_writing(text)


# Carrying on with a document that was stopped. Matched whole, the way the mode and
# follow-up commands are, so that "continue" in the middle of an ordinary sentence
# resumes nothing.
_RESUME = re.compile(
    r"^(?:retro[,\s]+)?(?:please\s+)?(?:can you\s+)?(?:"
    r"(?:carry|continue|resume|finish|complete|keep)\s+(?:on\s+)?"
    r"(?:with\s+)?(?:writing|the (?:document|draft|file|story|essay|article)|it|where you "
    r"left off|the rest)|continue (?:writing|where you left off)|carry on|resume)\b", re.I)

# Reading a finished document back. "Read my documents" is deliberately *not* this:
# that asks what exists, which is the list the planner answers.
_READBACK = re.compile(
    r"\bread\s+(?:it|that|this)\s*(?:back|out|aloud|again)?\b"
    r"|\bread\s+(?:me\s+)?(?:back\s+)?(?:the |your )?(?:last|latest|newest|finished)\s+"
    r"(?:document|one|file|story|essay|article)\b"
    r"|\bread\s+(?:me\s+)?(?:what|out)\s+(?:you\s+)?wrote\b"
    r"|\bread\s+(?:me\s+)?(?:the\s+)?(?:document|file|story|essay|article)\s+(?:back|out|aloud)\b",
    re.I)


def is_resume(text):
    """True when the request is to carry on with a document that was stopped."""
    cleaned = " ".join(str(text or "").split()).strip(" .,!?")
    return bool(_RESUME.match(cleaned)) if cleaned else False


def is_readback(text):
    """True when the request is to hear a document that was already written."""
    return bool(_READBACK.search(" ".join(str(text or "").split())))


def parse_length(answer, fallback=DEFAULT_WORDS):
    """The target length in words from a spoken answer, or `fallback`.

    Only the user's own scale is read - a number of words, a number of pages, a
    number of paragraphs, or one of the plain words for short and long. Anything
    else (silence, a shrug, a question of their own) falls back rather than
    guessing a number out of a sentence that did not name one.
    """
    cleaned = " ".join(str(answer or "").lower().split())
    if not cleaned:
        return clamp(fallback)
    match = _WORDS_NUMBER.search(cleaned) or _WORDS_HYPHEN.search(cleaned)
    if match:
        return clamp(int(match.group(1)))
    pages = _PAGES.search(cleaned)
    if pages:
        return clamp(_PAGE_WORDS.get(pages.group(1).lower(), fallback))
    paragraphs = _PARAGRAPH.search(cleaned)
    if paragraphs:
        return clamp(_PARAGRAPH_WORDS.get(paragraphs.group(1).lower(), fallback))
    for pattern, words, _label in _LENGTHS:
        if pattern.search(cleaned):
            return clamp(words)
    return clamp(fallback)


def clamp(words):
    """A target that is always inside a range Retro can actually deliver."""
    try:
        number = int(words)
    except (TypeError, ValueError):
        number = DEFAULT_WORDS
    return max(MIN_WORDS, min(MAX_WORDS, number))


def describe(words):
    """How a length is said out loud: 'short, about 110 words'."""
    for _pattern, size, label in _LENGTHS:
        if abs(size - words) <= max(20, size * 0.2):
            return f"{label}, about {words} words"
    return f"about {words} words"


def options_text(words):
    """The two concrete choices named in the brief's one question."""
    short = clamp(min(words, 110))
    long = clamp(max(words * 2, 400))
    return f"short, like {short} words, or longer, around {long}"


# ---------- the prompts ------------------------------------------------------

BRIEF_RULE = (
    "You are Retro, a voice assistant on your owner's Windows laptop, and they have "
    "just asked you to write something:\n{detail}\n"
    "Reply with one or two short sentences, spoken out loud, that: acknowledge you are "
    "starting; say honestly that writing it will take a little while (seconds to a "
    "minute); and ask them one question offering the two lengths '{options}'. Do not "
    "write any of the document. No markdown, no lists, no line breaks."
)

OUTLINE_RULE = (
    "You are planning a document for your owner, who asked for:\n{detail}\n"
    "Write the outline only. The first line is the title. Then one section per line, "
    "in the form 'heading | words'. Use {sections} sections whose word counts add up "
    "to about {words}. No prose, no numbering, no markdown, nothing else."
)

SECTION_RULE = (
    "You are writing a document titled '{title}', for this request:\n{detail}\n"
    "The outline is:\n{outline}\n"
    "Write ONLY the section '{heading}' now, in plain connected prose of about "
    "{words} words. No heading, no markdown, no bullet points, no title, no preamble. "
    "Write the prose itself and nothing else - it is being read out as it is written."
)


def brief_messages(request, words=DEFAULT_WORDS):
    """The prompt for the one line that opens a writing job."""
    rule = BRIEF_RULE.format(detail=request, options=options_text(words))
    return [{"role": "system", "content": rule},
            {"role": "user", "content": "Now"}]


def fallback_brief(words=DEFAULT_WORDS):
    """The same line with no model behind it: honest, short, and no invention."""
    return (f"On it - I'll write it, and this may take a few seconds to a minute. "
            f"Would you like it {options_text(words)}?")


def outline_messages(request, words=DEFAULT_WORDS, sections=4):
    return [{"role": "system", "content": OUTLINE_RULE.format(
        detail=request, words=words, sections=sections)},
        {"role": "user", "content": "Now"}]


def section_messages(request, title, heading, words, outline):
    listing = "\n".join(f"- {name} ({count} words)" for name, count in outline)
    return [{"role": "system", "content": SECTION_RULE.format(
        detail=request, title=title, heading=heading, words=words, outline=listing)},
        {"role": "user", "content": f"Write the '{heading}' section now."}]


# ---------- reading what the model wrote -------------------------------------

def clean_line(text, limit=70):
    """One tidy line out of a model reply: no markdown, no quotes, no label."""
    cleaned = str(text or "").split("\n")[0].strip()
    cleaned = _MARK.sub("", cleaned).strip().strip("*_`#").strip().strip('"').strip("'")
    cleaned = re.sub(r"^(?:title|heading|section)\s*[:\-]\s*", "", cleaned, flags=re.I)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip() if len(cleaned) > limit else cleaned


def parse_outline(reply, words=DEFAULT_WORDS):
    """(title, [(heading, words)]) from the model's outline, with honest defaults.

    A model that answers with a bad outline still gets one: a title from the request
    and an even split of the request's own length. That is a real document plan, not
    an invented claim, and it means a malformed outline costs shape rather than the
    whole request.
    """
    lines = [line.strip() for line in str(reply or "").splitlines() if line.strip()]
    title = clean_line(lines[0]) if lines else ""
    if not title or _PIPE.match(title):
        title = ""
    sections = []
    for raw in lines[1:] if title else lines:
        body = _MARK.sub("", raw).strip()
        match = _PIPE.match(body)
        if match:
            heading = clean_line(match.group(1), 60)
            count = int(match.group(2))
        else:
            heading = clean_line(body, 60)
            count = 0
        if not heading or heading.lower() in ("title", "outline"):
            continue
        sections.append((heading, max(MIN_SECTION_WORDS, min(400, count or MIN_SECTION_WORDS))))
        if len(sections) >= MAX_SECTIONS:
            break
    if not sections:
        sections = default_outline(words)
    return title or "Untitled document", sections


def default_outline(words=DEFAULT_WORDS):
    """Three sections, splitting the target evenly - the shape when nothing better."""
    count = max(2, min(4, words // 80 + 1))
    each = max(MIN_SECTION_WORDS, words // count)
    names = ["Opening", "The main picture", "What matters most", "Closing thoughts",
             "Where it goes next"]
    return [(names[index], each) for index in range(count)]


def safe_name(title, fallback="Document"):
    """A file name from a title: letters, digits, spaces and dashes only."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", str(title or "")).strip(" ._-")
    cleaned = " ".join(cleaned.split())
    if not cleaned or not files.NAME_OK.match(cleaned):
        cleaned = fallback
    return cleaned[:60]


# ---------- a document that survives being stopped ---------------------------

DRAFT_FOLDER = "Drafts"


def drafts_folder():
    """Where unfinished documents live, out of the way of the finished ones."""
    folder = files.workspace() / DRAFT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def draft_path(title):
    return drafts_folder() / (safe_name(title or "Document") + ".json")


def save_draft(state):
    """Writes the shape of a document that is not finished yet. Returns whether it landed.

    Called after every finished section, so the work exists on disk while it is being
    done rather than only at the end. A draft that cannot be written is not fatal:
    the writing carries on and only the resumability is lost.
    """
    try:
        payload = json.dumps({"request": str(state.get("request") or ""),
                              "title": str(state.get("title") or ""),
                              "words": int(state.get("words") or DEFAULT_WORDS),
                              "outline": [[str(name), int(count)] for name, count
                                          in (state.get("outline") or [])],
                              "sections": [[str(name), str(text)] for name, text
                                           in (state.get("sections") or [])],
                              "done": int(state.get("done") or 0),
                              "total": int(state.get("total") or 0)}, ensure_ascii=False)
        written = files.write_text(f"{DRAFT_FOLDER}/{draft_path(state.get('title')).name}",
                                   payload)
        return bool(getattr(written, "ok", False))
    except Exception as exc:                     # never let bookkeeping end a document
        log.event("draft_save_failed", error=type(exc).__name__)
        return False


def drafts():
    """Every unfinished document, newest first."""
    folder = files.workspace() / DRAFT_FOLDER
    if not folder.is_dir():
        return []
    found = []
    for entry in sorted(folder.glob("*.json")):
        try:
            state = json.loads(entry.read_text(encoding="utf-8"))
            when = entry.stat().st_mtime
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and state.get("title"):
            state["when"] = when
            state["path"] = str(entry)
            found.append(state)
    found.sort(key=lambda state: -state["when"])
    return found


def load_draft():
    """The unfinished document to carry on with: the newest one, or None."""
    found = drafts()
    return found[0] if found else None


def clear_draft(title):
    """Forgets a draft: the document it belonged to is finished."""
    folder = files.workspace() / DRAFT_FOLDER
    if not folder.is_dir():
        return
    keep = safe_name(title or "")
    for entry in folder.glob("*.json"):
        if keep and entry.stem != keep:
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def resume_line(draft):
    """What Retro says when it picks a stopped document back up."""
    title = str((draft or {}).get("title") or "your document")
    done = len((draft or {}).get("sections") or [])
    total = len((draft or {}).get("outline") or [])
    if not done:
        return f"Starting '{title}' from the beginning then."
    left = max(0, total - done)
    return (f"Carrying on with '{title}' - {done} of {total} sections are done, "
            f"so {left} to go.")


# ---------- the documents that exist -----------------------------------------

def documents(limit=8):
    """The documents Retro has written, newest first: name, words, when, path."""
    root = files.workspace()
    found = []
    for entry in root.glob("*.md"):
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
            when = entry.stat().st_mtime
        except OSError:
            continue
        found.append({"name": entry.name, "words": count_words(text), "when": when,
                      "path": str(entry)})
    found.sort(key=lambda row: -row["when"])
    return found[: max(1, int(limit))]


def latest_document():
    """The most recently written document, with its text - for reading it back."""
    found = documents(1)
    if not found:
        return None
    document = dict(found[0])
    try:
        document["text"] = open(document["path"], encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    document["title"] = first_heading(document["text"]) or document["name"]
    return document


def document_path(name):
    """The real path of one of Retro's documents, or None. Never leaves the workspace."""
    cleaned = " ".join(str(name or "").strip().strip('"').split())
    if not cleaned:
        return None
    try:
        target = files.inside_workspace(cleaned)
    except ValueError:
        return None
    if target.suffix.lower() not in (".md", ".txt") or not target.is_file():
        return None
    return str(target)


def first_heading(text):
    """The title line of a document, if it has one."""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        return _MARK.sub("", line).strip().strip("*_`# ").strip() or ""
    return ""


def readable(text):
    """A document as speech: no hash marks, and every heading its own sentence."""
    parts = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = line.startswith("#")
        line = _HEADING.sub("", line) if heading else _MARK.sub("", line)
        line = line.strip().strip("*_`").strip()
        line = " ".join(line.split())
        if not line:
            continue
        if heading and not re.search(r"[.!?]$", line):
            line += "."
        parts.append(line)
    return " ".join(parts)


def word_count_readable(text):
    """How many words a read-back would actually say."""
    return count_words(readable(text))


# ---------- writing it -------------------------------------------------------

def write_document(request, words=DEFAULT_WORDS, complete=None, stream=None,
                   on_sentence=None, on_partial=None, on_event=None, task=None,
                   resume=None):
    """Writes the whole document, section by section, and saves it.

    `complete(messages, max_tokens)` writes the outline; `stream(messages,
    max_tokens, on_delta)` writes a section and hands over each piece as it is
    written, so the voice can start on the first sentence while the last one is still
    being generated. `on_sentence(sentence)` receives each finished sentence, and
    `on_partial(text)`/`on_event(event)` feed the window. Returns a dict describing
    what was actually written - with `stopped` True and a saved draft when the user
    stopped it part way - and raises only if there is no model or no work at all.

    `resume` is a draft from an earlier sitting (see `load_draft`): the length and the
    outline are kept, the sections that are already written are kept, and only the
    rest of them are written, without asking the user anything again.
    """
    from .tts import SentenceStream          # imported here: no Qt, no audio needed

    if complete is None:
        raise RuntimeError("no model to write with")

    def event(kind, **fields):
        if on_event is not None:
            try:
                on_event({"kind": kind, **fields})
            except Exception:                # a window that cannot update is not fatal
                pass

    def check():
        if task is not None:
            task.check()

    check()
    carried = dict(resume) if isinstance(resume, dict) else None
    if carried:
        request = carried.get("request") or request
        words = clamp(carried.get("words") or words)
        title = str(carried.get("title") or "Untitled document")
        outline = [((name), int(count)) for name, count in (carried.get("outline") or [])]
        if not outline:
            outline = default_outline(words)
        sections = [[str(name), str(text)] for name, text in (carried.get("sections") or [])]
    else:
        raw_outline = complete(outline_messages(request, words), 400)
        title, outline = parse_outline(raw_outline, words)
        sections = []
    total = len(outline)
    log.event("compose_outline", title=title[:60], sections=total, words=words,
              resumed=len(sections))
    event("begin", title=title, total=total, words=words, resumed=len(sections))

    def draft_state(done):
        return {"request": request, "title": title, "words": words,
                "outline": [[name, count] for name, count in outline],
                "sections": sections, "done": done, "total": total}

    def paused():
        """What a stopped document leaves behind: the draft, and what to say about it."""
        done = len(sections)
        kept = count_words(" ".join(text for _name, text in sections))
        save_draft(draft_state(done))
        log.event("compose_paused", title=title[:60], done=done, total=total)
        event("paused", title=title, total=total, done=done, words=kept)
        if not done:
            message = ("Stopped - there wasn't a finished section yet, so there is nothing "
                       "to carry on with.")
        elif total and done >= total:
            # Stopped on the very last section: everything is written, only the file is
            # not. Saying "2 of 2 sections" there would read as if something were missing.
            message = (f"Stopped - '{title}' is fully written but not saved yet. Say 'carry "
                       f"on writing' and I'll save it as a file.")
        else:
            message = (f"Stopped - I've kept '{title}' as a draft with {done} of {total} "
                       f"sections and about {kept} words. Say 'carry on writing' and I'll "
                       f"finish it.")
        return {"title": title, "sections": sections, "text": "", "words": kept,
                "filename": "", "message": message, "stopped": True, "done": done,
                "total": total, "words_wanted": words, "event": event}

    # Everything that can be interrupted is inside one try: a stop can arrive at a
    # checkpoint, inside the model's own stream, or on the sentence being spoken - and
    # all three have to leave the same thing behind, which is the draft as it stands and
    # no document at all. That is what makes stopping a long job keep its words.
    try:
        for index, (heading, want) in enumerate(outline, start=1):
            if index <= len(sections):
                continue                      # already written in an earlier sitting
            check()
            event("section", index=index, total=total, heading=heading)
            splitter = SentenceStream()
            chunks = []
            heard = []

            def collect(piece, _splitter=splitter, _chunks=chunks, _heard=heard):
                _chunks.append(piece)
                text = "".join(_chunks)
                if on_partial is not None:
                    on_partial(text)
                for sentence in _splitter.feed(piece):
                    _heard.append(sentence)
                    if on_sentence is not None:
                        on_sentence(sentence)

            messages = section_messages(request, title, heading, want, outline)
            budget = max(120, int(want * 2.2) + 60)
            if stream is not None:
                body = stream(messages, budget, collect)
            else:
                body = complete(messages, budget)
                collect(body)
            for sentence in splitter.flush():
                heard.append(sentence)
                if on_sentence is not None:
                    on_sentence(sentence)
            section_text = " ".join(" ".join(str(section).split())
                                    for section in heard).strip()
            if not section_text:
                section_text = " ".join(str(body or "").split())
            sections.append([heading, section_text])
            # The draft is the whole answer to "stopping threw my document away": the
            # section that just finished is on disk *before* it is announced, so the
            # window's own count and the draft on disk can never disagree.
            save_draft(draft_state(len(sections)))
            event("section_done", index=index, total=total, heading=heading,
                  words=count_words(section_text))

        # The last checkpoint before anything is written down.
        check()
    except Stopped:
        return paused()
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections if text)
    document = f"# {title}\n\n{body}\n"
    total_words = count_words(document)
    filename = safe_name(title) + ".md"
    saved = files.write_text(filename, document)
    clear_draft(title)              # the document exists now, so the draft is done with
    log.event("compose_saved", file=filename, words=total_words,
              ok=bool(getattr(saved, "ok", False)))
    event("done", title=title, total=total, words=total_words, file=filename,
          saved=bool(getattr(saved, "ok", False)), detail=str(getattr(saved, "message", "")))
    verb = "finished" if carried else "written"
    message = (f"'{title}' is {verb} - about {total_words} words, saved as {filename} "
               f"in my workspace." if getattr(saved, "ok", True)
               else f"'{title}' is {verb} - about {total_words} words, but {saved.message}")
    return {"title": title, "sections": sections, "text": document, "words": total_words,
            "filename": filename, "message": message, "event": event, "stopped": False,
            "finished": bool(carried), "done": total, "total": total,
            "words_wanted": words}


def count_words(text):
    return len([word for word in str(text or "").split() if any(ch.isalnum() for ch in word)])
