"""Making a coding project: files first, the editor last.

The owner's rule, in their words: *it creates a folder inside Coding Tasks, makes the
individual files, and after all work is done it simply reports that the work is done.
It must not open VS Code - it should silently execute in the background. After it's
done, then only it opens VS Code to show me.*

That ordering is the whole design of this module, and it is why coding does not go
through the planner. A plan step like `app_function | vscode | create_project` opens
the editor as *part* of the work: the editor comes to the front, steals the keyboard
and shows a half-built folder while the rest is still being written. Here the work is:

1. a folder under `RetroWorkspace/Coding Tasks/<project>`;
2. a file list, written by the model for *this* request rather than a fixed starter;
3. each file's contents, written to disk and then read back to prove it is there;
4. the report - the files by name, and where the folder is;
5. only then the editor, opened on the finished folder.

Nothing here types into a window or presses a key. Every write goes through
`core/files.py`, so a path that tries to leave the workspace is refused instead of
followed, and the count in the reply is counted from files that are really on disk.

The model is asked for one thing at a time - the list, then one file - because a small
model asked for "a website" in one answer produces a single half-page of prose instead
of a project. It is also told, for every file, that the result must run as written:
a placeholder is worse than a missing file, because a missing file is reported and a
placeholder looks finished.
"""
import os
import re
import subprocess

from . import files, log, registry
from .control import Stopped
from .model import Result

# A whole project is bounded on purpose: eight files (or folders) is a real site or
# script, and every one of them is another model call. Beyond that the job stops being
# a request and starts being a build system.
MAX_FILES = 8
MAX_TOKENS = 1600
MAX_CHARS = 40000

# Safe file names: a plain relative path with a normal extension, at most two folders
# deep. Anything else is refused rather than cleaned up into something surprising.
_PATH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")
_EXT_OK = re.compile(r"^[A-Za-z0-9]{1,8}$")
_MAX_DEPTH = 2

# ---------- is this a coding request? ---------------------------------------

# What is being made. Deliberately concrete: these are things that are built out of
# files, and "make it louder" or "make a note" must not match any of them.
_TARGET = re.compile(
    r"\b(?:website|web ?site|webpage|web page|web app|site|app|application|game|clone|"
    r"portfolio|landing page|dashboard|interface|\bui\b|api|server|backend|frontend|"
    r"script|program|tool|calculator|component|widget|extension|editor|blog|store|shop|"
    r"chat ?bot|cli|parser|scraper|automation|3d scene|scene)\b", re.I)
# What is being done to it. "open", "show" and "launch" are absent on purpose: those
# ask to see something that already exists, which is not a build.
_MAKE = re.compile(
    r"\b(?:make|build|create|code|develop|design|implement|write|generate|program|"
    r"scaffold|construct|set up|put together)\b", re.I)
# Language and framework words that make a request a coding job even without a target:
# "use react and tailwind" is a build, and so is "write it in python".
_TOOLS = re.compile(
    r"\b(?:html|css|javascript|js|typescript|ts|python|react|next\.?js|vue|svelte|node|"
    r"express|flask|django|fastapi|three\.?js|tailwind|bootstrap|vite|webgl|canvas|"
    r"java|c\+\+|c#|rust|golang|go lang|php|laravel|sql|electron|tkinter|pygame)\b", re.I)
# The editor, named in the request. "open VS Code, make a 3D website" is a build with
# a preamble, not a request to open the editor: the clause is removed and what is left
# decides. "open VS Code" alone leaves nothing to make, and falls through to the
# planner, which opens it.
_EDITOR = r"(?:vs ?code|vscode|visual studio code|code editor)"
_EDITOR_LEAD = re.compile(
    r"^(?:please\s+|retro[,\s]+)?(?:(?:open|launch|start|show|bring up|focus)\s+)?"
    r"" + _EDITOR + r"\s*[,;:-]?\s*", re.I)
_EDITOR_TAIL = re.compile(r"\s+in\s+" + _EDITOR + r"\b.*$", re.I)
# A document is `core/compose.py`'s job, not a project: "write an essay about the web".
_DOCUMENT = re.compile(
    r"\b(?:essay|article|blog post|report|story|poem|letter|documentary|guide|summary|"
    r"chapter|book|review|speech|thesis|document)\b", re.I)


def wants_project(text):
    """True when the request is to build a piece of software, not to describe one.

    Both halves are required - something being made, and something made of code - so
    "make a note" and "open my website" stay where they belong. Naming a language or a
    framework counts as the second half, which is what catches "use react and tailwind
    to make a landing page" and "write it in python".
    """
    cleaned = " ".join(str(text or "").split())
    if len(cleaned.split()) < 2:
        return False
    cleaned = _EDITOR_TAIL.sub("", _EDITOR_LEAD.sub("", cleaned)).strip()
    if len(cleaned.split()) < 2:
        return False
    if _DOCUMENT.search(cleaned) and not _TOOLS.search(cleaned):
        return False
    if not _MAKE.search(cleaned):
        return False
    return bool(_TARGET.search(cleaned) or _TOOLS.search(cleaned))


# A request that one pass of files cannot honour: a framework, a backend, accounts,
# payments, or a change to code that already exists. Those are the ones worth talking
# through, and `looks_big` is the question the session asks before deciding whether to
# build silently or offer the coding discussion instead.
_BIG_FRAMEWORK = re.compile(
    r"\b(?:react|next\.?js|vue|angular|svelte|nuxt|django|flask|fastapi|express|"
    r"laravel|rails|spring|asp\.?net|electron|graphql|three\.?js)\b", re.I)
_BIG_SCOPE = re.compile(
    r"\b(?:backend|back-end|frontend|front-end|full ?stack|database|postgres|"
    r"postgresql|mysql|mongodb|sqlite|firebase|supabase|authentication|auth|login|"
    r"sign ?up|user accounts?|accounts? system|payments?|stripe|checkout|multiplayer|"
    r"websockets?|real ?time|rest api|api server|docker|deploy|admin panel)\b", re.I)
_BIG_CHANGE = re.compile(
    r"\b(?:add|integrate|refactor|migrate|upgrade|extend|convert|rewrite)\b.*"
    r"\b(?:my|our|existing|current|this)\b", re.I)


def looks_big(text):
    """True for a coding request that deserves a discussion before files are written.

    The builder writes at most eight files from a fixed shape, which is exactly right
    for a landing page or a calculator and exactly wrong for "add authentication to my
    project": it cannot see the project, and eight files cannot be a backend. Those
    requests go to the coding discussion instead - not refused, offered.
    """
    cleaned = " ".join(str(text or "").split())
    if len(cleaned.split()) < 3:
        return False
    if _BIG_CHANGE.search(cleaned):
        return True
    return bool(_BIG_FRAMEWORK.search(cleaned) or _BIG_SCOPE.search(cleaned))


# ---------- the folder ------------------------------------------------------

_LEAD = re.compile(
    r"^(?:(?:retro[,\s]+)?(?:please|can you|could you|would you|will you|i want you to|"
    r"i need you to|i want|i need)\s+)*(?:(?:make|build|create|code|develop|design|"
    r"implement|write|generate|program|scaffold|construct|set up|put together)\s+)+",
    re.I)
_TAIL = re.compile(
    r"\s+(?:in|using|with|via)\s+(?:vs ?code|vscode|visual studio code|html|css|"
    r"javascript|js|typescript|react|next\.?js|vue|svelte|python|node|three\.?js|"
    r"tailwind|bootstrap|canvas|webgl)\b.*$", re.I)


def project_name(text):
    """A folder name out of the request, or a plain one when nothing usable is left.

    "open VS Code, make a new 3D website for me" becomes "3D website": the editor
    clause, the verb, the articles and the trailing "for me" are all phrasing, and a
    folder called "open VS Code, make a new 3D website" is not.
    """
    cleaned = " ".join(str(text or "").split())
    cleaned = _EDITOR_TAIL.sub("", _EDITOR_LEAD.sub("", cleaned)).strip()
    # What is being built is whatever follows the *last* build verb: "use this language
    # and this combination to make website" is a request whose subject is "website".
    verbs = list(_MAKE.finditer(cleaned))
    if verbs:
        cleaned = cleaned[verbs[-1].end():].strip(" .,!?:;")
    cleaned = _LEAD.sub("", cleaned).strip()
    cleaned = _TAIL.sub("", cleaned).strip(" .,!?:;")
    # Repeated, because "create me a new 3D site" has three of them in a row.
    for _ in range(4):
        shorter = re.sub(r"^(?:a|an|the|me|my|new|some|this|that|it)\s+", "", cleaned,
                         flags=re.I)
        shorter = re.sub(r"\s+(?:for me|please|now|to show me)\s*$", "", shorter, flags=re.I)
        if shorter == cleaned:
            break
        cleaned = shorter.strip(" .,!?:;")
    cleaned = " ".join(cleaned.split())[:60].strip(" .,!?:;")
    if cleaned and len(cleaned) >= 3 and files.NAME_OK.match(cleaned):
        return cleaned
    return "Coding Project"


def project_folder(name):
    """`Coding Tasks/<name>`, created and made unique. Returns (folder, error)."""
    folder, problem = files.create_project_folder(name)
    if folder is None:
        return None, problem
    if not folder.exists():
        return None, "I couldn't create the project folder."
    stem, seen = folder.name, 1
    while any(folder.iterdir()):
        seen += 1
        folder, problem = files.create_project_folder(f"{stem} {seen}")
        if folder is None:
            return None, problem
    return folder, ""


# ---------- what the model returns ------------------------------------------

_FENCE = re.compile(r"^```[^\n]*\n?|\n?```\s*$", re.M)


def clean_code(reply):
    """The file's contents out of a model reply, without the conversation around it.

    Small models like to answer with "Sure, here's the file:" and a fenced block, or
    with a fence and a closing sentence. The largest fenced block is the file when
    there is one; otherwise the reply is taken as it stands, with stray fences and the
    most common "here is the code" preamble removed.
    """
    text = str(reply or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    if blocks:
        text = max(blocks, key=len).strip("\n")
    else:
        text = _FENCE.sub("", text).strip("\n")
        text = re.sub(r"^(?:sure|okay|ok|certainly|here(?:'s| is)[^\n]*)\s*[:\n]\s*",
                      "", text, flags=re.I).strip("\n")
    return text[:MAX_CHARS]


def _safe_path(raw):
    """A relative file path that is safe to write, or None."""
    path = " ".join(str(raw or "").strip().strip("`'\"").split()).replace("\\", "/")
    while path.startswith("./"):          # a leading ./ is phrasing, not a move up
        path = path[2:]
    # `..` is refused, never rewritten: a path that tries to climb out of the project
    # is a path the model should not have written, and stripping the dots from it is
    # how a traversal quietly becomes a file inside the folder.
    if not path or ".." in path or path.startswith("/") or ":" in path:
        return None
    parts = [part for part in path.split("/") if part]
    if not parts or len(parts) > _MAX_DEPTH:
        return None
    name = parts[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot or not _EXT_OK.match(ext) or not stem:
        return None
    if not _PATH_OK.match(stem) and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$", stem):
        return None
    if any(part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def parse_plan(reply, limit=MAX_FILES):
    """[(path, purpose)] from the model's file list, cleaned and bounded."""
    planned, seen = [], set()
    for raw in str(reply or "").splitlines():
        line = raw.strip().lstrip("-*0123456789.) ").strip()
        if not line or "|" not in line:
            continue
        candidate, _, purpose = line.partition("|")
        path = _safe_path(candidate)
        if path is None or path.lower() in seen:
            continue
        seen.add(path.lower())
        planned.append((path, " ".join(purpose.split())[:70]))
        if len(planned) >= limit:
            break
    return planned


DEFAULT_WEB = (("index.html", "the page"), ("style.css", "the styling"),
               ("main.js", "the behaviour and the 3D scene"))
DEFAULT_CODE = (("main.py", "the program"), ("README.md", "how to run it"))


def default_plan(request):
    """A sane file list when the model's own list cannot be read."""
    if re.search(r"\b(?:web|html|site|page|ui|interface|dashboard|3d|canvas|react|vue|"
                 r"tailwind|css)\b", str(request or ""), re.I):
        return list(DEFAULT_WEB)
    return list(DEFAULT_CODE)


# ---------- talking to the model --------------------------------------------

LIST_RULE = """You are Retro, planning a real software project for your owner.
The project: {detail}

List the files the project needs, one per line, exactly in this form:

path | what it contains

Rules:
- One to {limit} files. Relative paths only, at most two folders deep.
- Include every file needed to run it: the page, the styles, the script, a README.
- Use the language and framework the owner named. If they named none, use plain HTML,
  CSS and JavaScript, with Three.js from a CDN only when they asked for 3D.
- No explanations, no numbering, no blank lines. Only the file lines."""

FILE_RULE = """You are Retro, writing one file of a working software project.
The project: {detail}
The files in it: {plan}

Rules:
- Output the file's contents and nothing else. No explanation, no markdown fences.
- It must run exactly as written: no placeholders, no TODO, no "add your code here".
- Real content, real styling, real behaviour. If the request names a subject, use it."""


def _ask(complete, messages, max_tokens):
    """One model call, tolerating a plain callable (the tests' fake models)."""
    try:
        return complete(messages, max_tokens)
    except TypeError:
        return complete(messages)


def _listing(request, complete):
    reply = _ask(complete, [{"role": "system",
                             "content": LIST_RULE.format(detail=request, limit=MAX_FILES)},
                            {"role": "user", "content": "Now"}], 320)
    plan = parse_plan(reply)
    return plan or default_plan(request)


# ---------- writing it ------------------------------------------------------

def _editor_candidates():
    spec = registry.spec_for("vscode")
    return list(registry._exe_candidates(spec)) if spec is not None else []


def open_in_editor(folder):
    """Opens a finished folder in VS Code. Returns (editor, anything).

    Called only after every file is written. The editor is the *reward* for the work,
    not the workspace for it - which is the difference between coding while the owner
    watches a window it cannot use, and coding and then showing them the result.
    """
    for candidate in _editor_candidates():
        if not candidate or "*" in candidate or not os.path.exists(candidate):
            continue
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if candidate.lower().endswith((".cmd", ".bat")):
                subprocess.Popen(["cmd", "/c", candidate, str(folder)],
                                 creationflags=flags)
            else:
                subprocess.Popen([candidate, str(folder)], creationflags=flags)
            return True, True
        except OSError as exc:
            log.event("editor_open_failed", error=type(exc).__name__)
            continue
    try:
        os.startfile(str(folder))         # noqa: S606 - a folder inside the workspace
        return False, True
    except OSError:
        return False, False


def build(request, complete, task=None, on_event=None, speak=None, editor=True):
    """Builds the project and reports it. Returns a Result. Never opens the editor early.

    `complete(messages, max_tokens)` is the model. `on_event(dict)` is the window's
    progress view; `speak(text)` is only used for the short line at the start of the
    job, because the rest of the work is deliberately quiet.
    """
    detail = " ".join(str(request or "").split())
    if not detail:
        return Result(False, "I need to know what to build.")
    if complete is None:
        return Result(False, "I need my language model to write code, and it isn't loaded.")

    def event(kind, **fields):
        if on_event is None:
            return
        try:
            on_event({"kind": kind, **fields})
        except Exception:                 # a window that cannot update is not fatal
            pass

    def check():
        if task is not None:
            task.check()

    check()
    name = project_name(detail)
    folder, problem = project_folder(name)
    if folder is None:
        return Result(False, problem)
    log.event("build_started", project=folder.name, chars=len(detail), files=0)
    event("build", title=folder.name, detail="planning the files")

    try:
        plan = _listing(detail, complete)
    except Stopped:
        return Result(False, "Stopped.")
    except Exception as exc:
        log.event("build_plan_failed", error=type(exc).__name__)
        plan = default_plan(detail)
    listing = ", ".join(path for path, _purpose in plan)
    event("build", title=folder.name, detail=listing[:80])

    written, failed = [], []
    for path, purpose in plan:
        check()
        if task is not None:
            task.touch(f"writing {path}")
        event("build", title=folder.name, detail=f"writing {path}")
        try:
            # The file's own name is the *user* turn, at the very end: a small model
            # follows the last thing it was asked far more reliably than a name buried
            # in a paragraph of instructions.
            body = clean_code(_ask(
                complete,
                [{"role": "system", "content": FILE_RULE.format(
                    detail=detail, plan=listing)},
                 {"role": "user", "content": f"Write the complete contents of {path} now."}],
                MAX_TOKENS))
        except Stopped:
            return Result(False, "Stopped.")
        except Exception as exc:
            log.event("build_file_failed", path=path, error=type(exc).__name__)
            failed.append(path)
            continue
        if not body.strip():
            log.event("build_file_empty", path=path)
            failed.append(path)
            continue
        result = files.write_text(folder / path, body)
        if not result.ok:
            log.event("build_write_failed", path=path)
            failed.append(path)
            continue
        target = folder / path
        if not target.exists() or target.stat().st_size == 0:
            failed.append(path)
            continue
        written.append((path, target.stat().st_size))
        if task is not None:
            task.steps += 1
            task.did_work = True
        check()

    summary = ", ".join(path for path, _size in written) or "nothing"
    log.event("build_finished", project=folder.name, written=len(written), failed=len(failed),
              bytes=sum(size for _path, size in written))
    if not written:
        return Result(False, f"I couldn't write any of the {len(plan)} files for "
                             f"{folder.name}, so there is nothing to open. "
                             f"The folder is at {folder}.")
    opening = ""
    if editor:
        opened, shown = open_in_editor(folder)
        if opened:
            opening = f" I've opened the folder in VS Code."
            log.event("editor_opened", project=folder.name)
        elif shown:
            opening = f" I couldn't start VS Code, so I've opened the folder instead."
    missing = f" {len(failed)} file(s) didn't come out: {', '.join(failed)}." if failed else ""
    return Result(True, f"Built {folder.name} - {summary}.{missing} It's in Coding Tasks, "
                        f"at {folder}.{opening}",
                  verified=not failed)
