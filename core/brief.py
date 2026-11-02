"""Turning a conversation into a task brief, without inventing the task.

The coding agent never sees the discussion. It gets one message and works from it, so
that message has to carry everything: what the person wants, what was already decided,
what was rejected, what the project is, and what "done" means.

Two ways to produce it, and the honest one always wins:

* the assistant's model reads the transcript and writes the brief, when the model is
  reachable - it can tell an accepted decision from a suggestion, which is the whole
  reason to ask it;
* otherwise the transcript is laid out as it was said, section by section. That is a
  worse brief, but it is the user's own words and it never contains a requirement
  nobody agreed to.

The rule both paths share: **no requirement that was not in the discussion**. A brief
with an invented feature is worse than a short one, because the agent will build it.
"""
import time

SYSTEM = """You are writing a single implementation brief for a coding agent. The agent will
not see the conversation it came from, so the brief must stand alone.

Rules:
- Use only what the conversation says. Do not add features, libraries or requirements
  that were not discussed.
- Treat something as a requirement only when the person asked for it or clearly agreed
  to it. A suggestion Retro made and the person did not answer is NOT a requirement.
- Put rejected ideas in their own section, so the agent does not build them.
- Keep the person's own words for anything specific: names, numbers, colours, text.
- Be concrete and short. No preamble, no closing offer to help.

Answer in exactly this markdown shape, filling each section from the conversation:

## Objective
One paragraph: what is being built and why.

## Requirements
- One line per functional requirement the person asked for.

## Decisions
- Technology, structure or behaviour that was agreed.

## Constraints
- Anything that must not be done, or must stay as it is.

## Rejected
- Ideas that were considered and turned down.

## Done means
- How the person will know it works.

## Open questions
- Anything left unresolved. Write "none" when there is nothing.

If the conversation contains no actual request, write "## Objective" followed by
"the discussion had no requests in it" and nothing else."""

HEADINGS = ("## Objective", "## Requirements", "## Decisions", "## Constraints",
            "## Rejected", "## Done means", "## Open questions")


def snapshot(messages, project=""):
    """A frozen copy of the discussion. The chat stays usable; this does not change.

    Messages arrive as {"role": "user"|"retro", "text": ...}. Anything else is dropped
    rather than guessed at, so a stray system line can never become a requirement.
    """
    kept = []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        text = str(message.get("text") or "").strip()
        if role not in ("user", "retro") or not text:
            continue
        kept.append({"role": role, "text": text})
    return {"messages": kept, "project": str(project or ""), "at": time.time()}


def transcript(snap, limit=12000):
    """The conversation as plain text for a model, newest kept when it is too long."""
    messages = list((snap or {}).get("messages") or [])
    blocks = []
    for message in messages:
        who = "Person" if message.get("role") == "user" else "Retro"
        blocks.append(f"{who}: {message.get('text')}")
    text = "\n\n".join(blocks)
    try:
        limit = max(500, int(limit))
    except (TypeError, ValueError):
        limit = 12000
    if len(text) <= limit:
        return text
    kept = []
    size = 0
    for block in reversed(blocks):
        if size + len(block) > limit:
            break
        kept.append(block)
        size += len(block) + 2
    if not kept:                       # one enormous turn: keep its tail
        return text[-limit:]
    return "[... earlier discussion shortened ...]\n\n" + "\n\n".join(reversed(kept))


def _project_line(snap):
    project = str((snap or {}).get("project") or "").strip()
    return project or "not chosen yet"


def fallback(snap):
    """The brief without a model: the person's own words, in order.

    This is deliberately plain. It never claims to have understood anything, and it
    cannot add a requirement, which is what makes it safe to hand to an agent that has
    to act on it.
    """
    messages = list((snap or {}).get("messages") or [])
    asked = [m["text"] for m in messages if m.get("role") == "user"]
    suggested = [m["text"] for m in messages if m.get("role") == "retro"]
    if not asked:
        objective = "(the discussion had no requests in it)"
    else:
        first = " ".join(asked[0].split())
        objective = first[:400]
    lines = ["## Objective", objective, "", "## Requirements"]
    if asked:
        lines += [f"- {text}" for text in asked]
    else:
        lines.append("- As described above.")
    lines += ["", "## Project", f"- Folder: {_project_line(snap)}", "", "## Constraints",
              "- Build only what is described above; nothing else was requested.", "",
              "## Done means", "- The project does what the requests above describe, and "
              "its own tests or build command pass (or the brief says why there is none)."]
    if suggested:
        lines += ["", "## Retro suggested"]
        lines += [f"- {text}" for text in suggested[:8]]
        lines += ["", "Treat the suggestions above as suggestions unless the person "
                      "clearly accepted them."]
    return "\n".join(lines)


def _clean_reply(text):
    """The model's brief, with a code fence or a preamble removed.

    Models wrap a long answer in a fence whether or not anybody asked, and often say
    "here you go" first. Both are packaging rather than content, and a brief that
    reaches the coding agent with ``` in it wastes a turn on nothing.
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if "```" in body:
        parts = body.split("```")
        if len(parts) >= 3:
            inner = parts[1]
            if "\n" in inner:
                first, rest = inner.split("\n", 1)
                if first.strip().lower() in ("markdown", "md", "text", "txt", ""):
                    inner = rest
            body = inner if "##" in inner or "#" in inner else parts[1]
    lines = body.splitlines()
    start = 0
    for index, line in enumerate(lines[:12]):
        if line.strip().startswith("#"):
            start = index
            break
    body = "\n".join(lines[start:]).strip()
    while body.startswith("```"):
        body = body.split("\n", 1)[1].strip() if "\n" in body else ""
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def usable(text):
    """Whether a model's brief is worth handing over."""
    body = str(text or "")
    if len(body) < 40:
        return False
    return "## Objective" in body or "## Requirements" in body


def build(snap, complete):
    """The brief, and where it came from. Returns (markdown, "model"|"transcript")."""
    project = _project_line(snap)
    prompt = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Where the project lives: {project}\n\n"
            f"--- CONVERSATION ---\n{transcript(snap)}\n--- END ---\n\n"
            f"Write the brief now.")},
    ]
    try:
        reply = complete(prompt)
    except Exception:
        reply = ""
    cleaned = _clean_reply(reply)
    if not usable(cleaned):
        return fallback(snap), "transcript"
    if "## Project" not in cleaned:
        cleaned += f"\n\n## Project\n- Folder: {project}"
    return cleaned, "model"
