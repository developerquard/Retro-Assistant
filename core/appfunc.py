"""Using an application, not just opening it - the reusable workflows behind app_function.

`open_app` puts a window on screen. This module is what actually operates the app,
and the user asked for it explicitly: "Spotify, play Sunflower", "message X that
Y", "open Notepad and start writing", "make a Three.js site in VS Code". Those are
five different mechanisms, and pretending one click sequence covers all of them is
how an assistant ends up unable to do any of them.

Three rules shape every workflow here:

* **Use the best mechanism the app actually has.** Writing a project is file work
  (core/files.py), not a thousand keystrokes; launching a game is a button in a
  launcher; Spotify and browsers have a keyboard route to their search box (ctrl+l)
  that does not depend on finding a control by its label.
* **Verify or say you could not.** Every workflow ends by reading the window back
  (accessibility labels and the text in editable fields), looking for a file on
  disk, or checking the window title. A step that typed something is not reported
  as a step that worked.
* **Never message the wrong person.** A contact is matched against the labels that
  are really on screen; when the match is weak or ambiguous the workflow asks, and
  nothing is sent.

The workflows are ordinary Python rather than plans the model writes each time: a
request for music should not cost a fresh six-step plan, and the model's job stays
what the user said it should be - understanding the request, choosing the action,
and handling what the workflow could not.
"""
import os
import time

from . import files, log, registry
from .model import Result

# The actions the planner may ask for, with what each one needs. This is the list
# shown to the model next to the app categories, so it can choose an action without
# inventing one.
ACTIONS = {
    "open": "open the application (no other arguments needed)",
    "search_play": "search inside the app for music/video and start it: detail = what to play",
    "play_pause": "toggle playback in the app that is playing",
    "next": "skip to the next track or episode",
    "previous": "go back to the previous track or episode",
    "ask": "type a prompt into a chat assistant and send it: detail = the prompt",
    "send_message": "find a contact and send them a message: target = contact, detail = message",
    "write_text": "type text into the app's document: detail = the text, target = optional file name to save as",
    "create_project": "create a real project folder with files and open it in the editor: target = project name",
    "launch_game": "open a game or launcher and get it to its play button",
    "navigate": "open a web address, or search the web for a phrase: detail = address or phrase",
    "search": "type a query into the app's own search box: detail = what to search for",
    "settings": "open a Windows settings page: detail = page such as display, sound, bluetooth",
    "run_test": "run the app's own test or measurement",
    "system": "open a system view and read it: detail = anything to look for in it",
}

BLOCKLIST = ("home", "shorts", "subscriptions", "library", "you", "sign in", "log in",
             "settings", "search", "menu", "explore", "trending", "history", "help",
             "notifications", "share", "download", "install", "upgrade", "premium",
             "back", "close", "next", "previous", "play", "pause", "skip", "mute")

VIDEO_SITES = {"youtube": "https://www.youtube.com/results?search_query={q}",
               "crunchyroll": "https://www.crunchyroll.com/search?q={q}",
               "chrome": "https://www.google.com/search?q={q}"}

CHAT_BOX_LABELS = {
    "chatgpt": ("Message ChatGPT", "Ask anything", "Message", "Send a message"),
    "claude": ("Write a message", "Message Claude", "Ask anything", "Message"),
    "freebuff": ("Ask anything", "Message", "Type a message"),
}

SEARCH_LABELS = {
    # Kept small and specific: a search box found by the wrong label is worse than
    # reaching it with the keyboard, which is why CTRL_L_APPS is tried first.

    "spotify": ("Search", "What do you want to play?"),
    "discord": ("Search", "Find or start a conversation", "Search or start a new chat"),
    "whatsapp": ("Search", "Search or start a new chat", "Search or start new chat"),
    "chrome": ("Search Google or type a URL", "Search or type URL", "Address and search bar"),
    "modrinth": ("Search", "Search mods and modpacks"),
}

# Navigation labels for a launcher or a test page: what looks like a button but is
# not the one being looked for. Deliberately not the shared blocklist - that one
# exists to keep search results from being confused with controls, and it contains
# "play", which is exactly the button a launcher is asked for.
LAUNCHER_NAV = {"settings", "servers", "skins", "news", "donate", "store", "profile",
                "options", "help", "about", "results", "apps"}

# Keys that reach a search box directly. Ctrl+L is the browser and Spotify route;
# it is used before any label search because a control found by label is a guess and
# a keyboard shortcut is not.
CTRL_L_APPS = ("spotify", "chrome", "youtube", "github", "odaclass", "chatgpt", "claude",
               "crunchyroll", "speedtest", "freebuff", "modrinth")


def _toolset():
    """The tool functions, imported late: tools.py imports this module."""
    from . import tools
    return tools


def _check(task):
    if task is not None:
        task.check()


def _note(task, detail):
    if task is not None:
        task.touch(detail)
    log.event("appfunc_step", detail=str(detail)[:60])


def _focus(spec, task, wait_s=8.0):
    """Brings the app's window to the front, waiting for it to come up."""
    toolset = _toolset()
    titles = [hint for hint in (spec.verify or ())] or [spec.name]
    for title in titles:
        result = toolset.focus_window(title, wait_s=wait_s)
        if result.ok:
            return result
    return Result(False, f"{spec.name} isn't showing a window yet.")


def _open(spec, task=None, wait_s=8.0):
    """Opens and focuses an app, and says which of the two steps failed."""
    toolset = _toolset()
    opened = toolset.open_app(spec.name)
    if not opened.ok:
        return opened
    # The app's own window first, always. A url in the entry is a fallback for a
    # machine without the program, not a statement that the app is a web page:
    # focusing "Chrome" for Spotify put every later keypress into whichever browser
    # window happened to be in front, so the desktop app got driven as a web player
    # - and the user's own browser tab got typed into.
    focus = _focus(spec, task, wait_s=wait_s)
    if focus.ok:
        return focus
    if spec.url and spec.key not in ("system_settings",):
        focus = toolset.focus_window("Chrome", wait_s=min(4.0, wait_s))
        if focus.ok:
            return focus
    return Result(True, f"{spec.name} is starting up, but its window isn't in front yet.",
                  verified=False)


def _screen():
    """(title, labels) for the window in front - labels include editable text."""
    toolset = _toolset()
    getter = getattr(toolset, "screen_labels", None)
    if getter is None:
        return "", []
    return getter(limit=70)


def _pick(query, labels, extra_block=(), block=None):
    """The label that best matches a spoken name, or None.

    Scoring is the same fuzzy match used for application names, because both arrive
    through a microphone: the contact may come back with a different vowel or split
    in two. A match has to be clearly better than the runner-up, so two chats that
    look alike produce a question rather than a coin toss.
    """
    from . import apps as app_index
    wanted = app_index.normalize(query)
    if not wanted:
        return None
    blocked = set(BLOCKLIST if block is None else block) | {b.lower() for b in extra_block}
    scored = []
    for label in labels:
        low = app_index.normalize(label)
        if not low or low in blocked or len(low) < 2:
            continue
        value = app_index.score(wanted, low)
        if wanted in low or low in wanted:
            value = max(value, 0.95)
        if value >= 0.6:
            scored.append((value, label))
    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12:
        return None                     # too close to call: ask instead of guessing
    return scored[0][1]


def _type_into_app(spec, text, task=None, attempts=2):
    """Types into the app's window, bringing it back to the front if it drifted.

    A freshly opened program can spend a moment before it really holds the focus,
    and the tools refuse to type into a window that was not deliberately named -
    correctly, but it turns a one-second race into a failed task. So: name the
    window again and try once more, and only then report the failure.
    """
    toolset = _toolset()
    last = None
    for attempt in range(max(1, attempts)):
        last = toolset.type_into(text)
        if last.ok:
            return last
        if attempt == attempts - 1 or "name the window" not in last.message:
            break
        _note(task, f"typing again into {spec.name}")
        _focus(spec, task, wait_s=4.0)
        time.sleep(0.4)
    return last


def _type_query(text, task=None):
    """Types a query into whatever field has focus, then presses enter."""
    toolset = _toolset()
    typed = toolset.type_into(text)
    if not typed.ok:
        return typed
    return toolset.press("enter")


def _search_box(spec, task=None):
    """Puts the cursor in the app's search box, by the most reliable route it has."""
    toolset = _toolset()
    if spec.key in CTRL_L_APPS:
        pressed = toolset.press("ctrl+l")
        if pressed.ok:
            return pressed
    for label in SEARCH_LABELS.get(spec.key, ("Search",)):
        clicked = toolset.click_text(label)
        if clicked.ok:
            return clicked
    return Result(False, f"I couldn't find the search box in {spec.name}.")


def _first_result(spec, query, task=None, wait_s=3.0):
    """Clicks the first likely result in a list of search results.

    "Likely" is a real filter, not a guess: known navigation labels are skipped, and
    the title has to share a word with what was asked for. When nothing matches, the
    workflow says so rather than clicking whatever is first.
    """
    toolset = _toolset()
    toolset.wait(int(max(1, min(6, round(wait_s)))), task)
    title, labels = _screen()
    hit = _pick(query, labels)
    if hit is None:
        return Result(False, f"I searched for '{query}' but couldn't pick a result from "
                             f"what {spec.name} is showing.")
    clicked = toolset.click_text(hit)
    if not clicked.ok:
        return clicked
    return Result(True, f"Opened '{hit}' in {spec.name}.", verified=True)


def _sound_probe(spec):
    """The process names the mixer should watch for this app's sound."""
    names = [spec.key]
    for pattern in spec.exe or ():
        base = os.path.basename(str(pattern).replace("\\", "/").rstrip("* ")).lower()
        base = base.split(".", 1)[0]
        if len(base) >= 4:
            names.append(base)
    return tuple(dict.fromkeys(names))


def _playing(spec, query, wait_s=4.0, meter_ok=True):
    """True when the app is really playing: its title says so, or its meter moves.

    A title alone is not enough for every player. The Spotify desktop app's title
    never changes, so the mixer's per-app peak meter is the evidence there: a paused
    player reads silence and a playing one does not. `meter_ok` is False when the app
    was already making sound before this request - old music is not this request's
    result - and in the browser, where the sound belongs to every tab at once.
    """
    from . import mixer

    toolset = _toolset()
    words = [w for w in str(query).lower().split() if len(w) > 2]
    probe = _sound_probe(spec)
    deadline = time.time() + max(1.0, wait_s)
    while time.time() < deadline:
        front = toolset.foreground_title().lower()
        if any(word in front for word in words):
            return True, front
        peak = mixer.peak_of(*probe) if meter_ok else None
        if peak and peak >= 0.003:
            return True, "its audio output"
        time.sleep(0.4)
    return False, toolset.foreground_title()


# ---------- the workflows ----------------------------------------------------


def _browser_hosted(spec):
    """True when the window on screen is really a page in a browser.

    On this machine Spotify is the *web player* in a Chrome app window, not the
    Spotify desktop app - and a browser page exposes nothing useful to UI Automation
    here (its controls are not in the accessibility tree until the browser is told to
    publish one). Detecting that is what turns "click the search box" into the route
    that actually works: the address bar, which every browser has, and the site's own
    search URL.
    """
    title = " ".join(_toolset().foreground_title().lower().split())
    if not title:
        return False
    return any(word in title for word in ("chrome", "edge", "firefox", "brave", "opera",
                                          "web player", "web app"))


def _play_web(spec, query, task=None):
    """The route for a music or video app that is really a web page.

    The address bar takes a search URL - deterministic, no controls to find - and the
    first result is clicked if the page exposes one. When it does not, that is said
    plainly instead of being reported as success or as a broken request.
    """
    toolset = _toolset()
    url = VIDEO_SITES.get(spec.key, "https://open.spotify.com/search/{q}").format(
        q=query.replace(" ", "+"))
    if spec.key != "chrome":
        # The app's own window first: a Spotify page in its own window is the one
        # whose results will be scanned, and "Chrome" alone matches the wrong
        # window whenever several are open.
        focus = _focus(spec, task, wait_s=6.0)
        if not focus.ok:
            focus = toolset.focus_window("Chrome", wait_s=6.0)
        if not focus.ok:
            return focus
    if not toolset.press("ctrl+l").ok:
        return Result(False, f"I couldn't reach the browser's address bar to search "
                             f"{spec.name}.")
    typed = _type_into_app(spec, url, task)
    if not typed.ok:
        return typed
    toolset.press("enter")
    started = time.time()
    toolset.wait(3, task)
    result = _first_result(spec, query, task, wait_s=2.0)
    playing, front = _playing(spec, query, wait_s=2.0, meter_ok=False)
    if playing:
        return Result(True, f"Playing '{query}' in {spec.name} ({front}).", verified=True)
    if result.ok:
        return Result(True, f"Opened '{query}' in {spec.name} - the title is '{front}', so "
                            f"check the sound if nothing is playing.", verified=False)
    return Result(True, f"I opened the {spec.name} search results for '{query}' "
                        f"({time.time() - started:.1f}s). Starting the track needs one "
                        f"click in there: {spec.name} is the web player here and it "
                        f"exposes no controls I can press.", verified=False)


def _play(spec, query, task=None):
    """search_play: find something and start it."""
    if not query:
        return Result(False, "I need to know what to play.")
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    if spec.key not in VIDEO_SITES and _browser_hosted(spec):
        return _play_web(spec, query, task)
    if spec.key in VIDEO_SITES:
        url = VIDEO_SITES[spec.key].format(q=query.replace(" ", "+"))
        toolset = _toolset()
        toolset.focus_window("Chrome", wait_s=6.0)
        nav = toolset.open_website(url)
        if not nav.ok:
            return nav
        return _first_result(spec, query, task, wait_s=4.0)
    from . import mixer

    toolset = _toolset()
    probe = _sound_probe(spec)
    was_sounding = mixer.is_sounding(*probe, seconds=0.8) is True
    box = _search_box(spec, task)
    if not box.ok:
        return box
    typed = _type_query(query, task)
    if not typed.ok:
        return typed
    result = _first_result(spec, query, task, wait_s=2.5)
    if not result.ok:
        # Nothing clickable was readable - a Chromium app can keep its page out of
        # the accessibility tree. Its own search box puts the top hit under one more
        # keypress: down selects it, enter plays it.
        toolset.press("down")
        toolset.press("enter")
        result = _first_result(spec, query, task, wait_s=2.5)
    playing, front = _playing(spec, query, meter_ok=not was_sounding)
    if playing:
        how = ("I can see its audio output" if front == "its audio output"
               else f"the title is '{front}'")
        return Result(True, f"Playing '{query}' in {spec.name} - {how}.", verified=True)
    if result.ok:
        return Result(True, f"I opened '{query}' in {spec.name} and clicked its result, "
                            f"but nothing is audible yet - check again in a moment.",
                      verified=False)
    return Result(False, f"I typed '{query}' into {spec.name}'s search and pressed enter, "
                         f"but {spec.name}'s window doesn't expose anything I can read, so "
                         f"I can't tell whether it started - the results are on screen to "
                         f"pick from.")


def _ask(spec, prompt, task=None):
    """ask: put a prompt into a chat assistant and send it."""
    if not prompt:
        return Result(False, "I need the prompt to send.")
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    toolset = _toolset()
    box = None
    _title, labels = _screen()
    for label in CHAT_BOX_LABELS.get(spec.key, ("Message", "Ask anything")):
        chosen = _pick(label, labels, extra_block=("new chat", "history", "upgrade"))
        if chosen:
            box = toolset.click_text(chosen)
            break
    if box is None or not box.ok:
        box = Result(True, "typing into the focused box")     # the composer is often already focused
    typed = _type_into_app(spec, prompt, task)
    if not typed.ok:
        return typed
    toolset.press("enter")
    time.sleep(1.5)
    _title, after = _screen()
    fragment = " ".join(prompt.split()[:4]).lower()
    seen = any(fragment and fragment in label.lower() for label in after)
    if seen:
        return Result(True, f"Sent it to {spec.name} - I can see the prompt in the chat.",
                      verified=True)
    return Result(True, f"I typed the prompt into {spec.name} and pressed enter, but I "
                        f"couldn't read the conversation back to confirm it.", verified=False)


def _send_message(spec, contact, message, task=None, confirm=None):
    """send_message: find a contact, confirm, type, send - or send nothing at all."""
    if not contact or not message:
        return Result(False, "I need both a person and a message.")
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    toolset = _toolset()
    box = _search_box(spec, task)
    if not box.ok:
        return box
    typed = _type_into_app(spec, contact, task)
    if not typed.ok:
        return typed
    time.sleep(1.4)
    _title, labels = _screen()
    match = _pick(contact, labels, extra_block=("search", "new chat", "new group", "archive"))
    if match is None:
        return Result(False, f"I couldn't find a chat with '{contact}' in {spec.name}, so I "
                             f"didn't send anything. Say the name again, or open the chat "
                             f"yourself and I'll type into it.")
    clicked = toolset.click_text(match)
    if not clicked.ok:
        return Result(False, f"I found '{match}' but couldn't open it, so nothing was sent.")
    time.sleep(0.9)
    question = (f"Retro: send \"{message[:70]}\" to {match} in {spec.name}? Say yes or no.")
    if confirm is not None and not confirm(question):
        return Result(False, "Cancelled - I didn't send it.")
    toolset.press("enter")            # focus the message box of the open conversation
    time.sleep(0.2)
    typed = _type_into_app(spec, message, task)
    if not typed.ok:
        return typed
    time.sleep(0.4)
    toolset.press("enter")
    time.sleep(1.2)
    _title, after = _screen()
    fragment = " ".join(message.split()[:4]).lower()
    seen = any(fragment and fragment in label.lower() for label in after)
    if seen:
        return Result(True, f"Sent to '{match}' in {spec.name} - I can see the message in "
                            f"the chat.", verified=True)
    return Result(True, f"I typed the message to '{match}' and pressed enter, but {spec.name} "
                        f"didn't let me read it back, so I can't be sure it arrived.",
                  verified=False)


def _fresh_document(spec):
    """True when the app's window is a new, empty document rather than a real one.

    A window title is not ownership. While this was being tested on the machine it
    runs on, "Notepad" matched the owner's own chapter - 23,000 characters of their
    work - and a request to "write about AI" would have typed into it. So typing only
    happens into a document that is demonstrably new, and anything else is written to
    a file, which is safe, checkable, and what the development work needs anyway.
    """
    title = " ".join(_toolset().foreground_title().lower().split())
    if not title:
        return False
    if "untitled" in title:
        return True
    bare = title.strip(" *")
    return any(bare == hint.lower() for hint in (spec.verify or (spec.name,)))


def _open_file_in(spec, path):
    """Opens a real file in the app. Returns True when the call went through."""
    if spec.key == "vscode":
        for candidate in registry._exe_candidates(spec):
            if "*" in candidate or not os.path.exists(candidate):
                continue
            try:
                import subprocess
                subprocess.Popen([candidate, str(path)])
                return True
            except OSError:
                continue
    try:
        os.startfile(str(path))           # noqa: S606 - a path inside the workspace
        return True
    except OSError:
        return False


def _write_text(spec, text, save_as="", task=None):
    """write_text: the text becomes a real file in the workspace, and the app opens it.

    This is the nonintrusive route the user asked for on purpose: the file is written
    and checked on disk and the editor is pointed at it, instead of typing into
    whichever window happened to match the title. Typing into the window still
    happens when that window is a fresh, empty document - checked, never assumed.
    """
    if not text:
        return Result(False, "I need the text to write.")
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    if save_as:
        toolset = _toolset()
        toolset.wait(1, task)
        name = save_as if save_as.lower().endswith((".txt", ".md", ".html", ".js",
                                                    ".css", ".py", ".json")) \
            else save_as + ".txt"
        # A note is not a coding project: it goes in its own folder beside Coding
        # Tasks, not inside the folder the owner keeps their projects in.
        folder, problem = files.create_notes_folder()
        if folder is None:
            return Result(False, problem)
        written = files.write_text(folder / name, text)
        if not written.ok:
            return written
        target = folder / name
        if not _open_file_in(spec, target):
            return Result(True, f"Wrote {target} in the workspace, but I couldn't get "
                                f"{spec.name} to open it.", verified=True)
        return Result(True, f"Wrote {target} and opened it in {spec.name}.", verified=True)
    if not _fresh_document(spec):
        return Result(False, f"The {spec.name} window that is open has a document in it "
                             f"already, so I didn't type into it. Give me a file name and "
                             f"I'll write the text to a file and open that instead.")
    written = _type_into_app(spec, text, task)
    if not written.ok:
        return written
    return Result(True, f"Wrote {len(text)} characters into a new {spec.name} document.",
                  verified=False)


FILLER_NAMES = {"done", "answer", "ok", "okay", "sure", "yes", "ready", "finished"}


def _project_name(target, detail=""):
    """A usable project name out of whatever the model filled in, or (None, why not).

    The model sometimes puts its own filler in the wrong field - a real plan came
    back as `create_project | simple three.js websiteanswer | Done.`, which would have
    made a folder called "Done." and a note ending in "answer". Filler is refused and
    the user is asked for a name, instead of building something with a nonsense name.
    """
    for candidate in (str(target or ""), str(detail or "")):
        cleaned = " ".join(candidate.replace("answer", " ").split())
        words = [word.strip(" .,!?:;") for word in cleaned.split()]
        words = [word for word in words if word and word.lower() not in FILLER_NAMES]
        name = " ".join(words)[:60].strip()
        if len(name) >= 3 and files.NAME_OK.match(name):
            return name, ""
    return None, ("I need a name for the project - say it again and I'll build it, for "
                  "example 'make a three.js website called nebula'.")


def _create_project(spec, name, task=None):
    """create_project: real files in the workspace, then the folder in the editor."""
    if not name:
        return Result(False, "I need a name for the project.")
    _check(task)
    created = files.create_threejs_project(name)
    if not created.ok:
        return created
    folder, _problem = files.create_project_folder(name)
    toolset = _toolset()
    opened = False
    for candidate in registry._exe_candidates(spec):
        if "*" in candidate or not os.path.exists(candidate):
            continue
        try:
            import subprocess
            subprocess.Popen([candidate, str(folder)])
            opened = True
            break
        except OSError:
            continue
    if not opened:
        toolset.open_app(spec.name)
        toolset.focus_window("visual studio code", wait_s=8.0)
    _note(task, f"created {folder.name}")
    if opened:
        return Result(True, f"Created {folder.name} with a working Three.js page and opened "
                            f"the folder in {spec.name}.", verified=True)
    return Result(True, f"Created {folder.name} at {folder} - {spec.name} should open it, "
                        f"but I couldn't start the editor with the folder.", verified=False)


def _named_project(spec, target, detail, task=None):
    """create_project, with the name read out of whichever field holds it."""
    name, problem = _project_name(target, detail)
    if name is None:
        return Result(False, problem)
    return _create_project(spec, name, task)


def _launch_game(spec, task=None):
    """launch_game: open the launcher and press its play button."""
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    toolset = _toolset()
    toolset.wait(2, task)
    _title, labels = _screen()
    # The button we are looking for is called Play, and "play" is in the blocklist
    # used when picking search results - so this lookup carries its own list.
    for wanted in ("play", "launch", "start", "играть", "enter the game"):
        hit = _pick(wanted, labels, block=LAUNCHER_NAV)
        if hit:
            clicked = toolset.click_text(hit)
            if clicked.ok:
                time.sleep(2.0)
                titles = [title for _hwnd, title in toolset.window_titles(limit=30)]
                game = [t for t in titles if any(word in t.lower() for word in
                                                 ("minecraft", "shadow fight"))]
                if game:
                    return Result(True, f"Started the game - I can see '{game[0]}'.",
                                  verified=True)
                return Result(True, f"Pressed '{hit}' in {spec.name}; the game window isn't up "
                                    f"yet.", verified=False)
    return Result(True, f"{spec.name} is open, but I didn't find a play button to press in "
                        f"what it is showing.", verified=False)


def _navigate(spec, detail, task=None):
    """navigate: a web address if that is what it is, otherwise a search."""
    toolset = _toolset()
    if not detail:
        return Result(False, "I need an address or something to search for.")
    if detail.lower().startswith(("http://", "https://")):
        opened = toolset.open_website(detail)
        return opened
    opened = toolset.open_website(spec.url or detail)
    if not opened.ok:
        return opened
    toolset.focus_window("Chrome", wait_s=6.0)
    toolset.press("ctrl+l")
    typed = _type_query(detail, task)
    if not typed.ok:
        return typed
    return Result(True, f"Searched for '{detail}' in the browser.", verified=False)


def _search(spec, query, task=None):
    """search: the app's own search box, without starting anything."""
    if not query:
        return Result(False, "I need something to search for.")
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    box = _search_box(spec, task)
    if not box.ok:
        return box
    typed = _type_query(query, task)
    if not typed.ok:
        return typed
    time.sleep(1.2)
    _title, labels = _screen()
    hit = _pick(query, labels)
    if hit:
        return Result(True, f"Searched {spec.name} for '{query}' - top result is '{hit}'.",
                      verified=True)
    return Result(True, f"Typed '{query}' into {spec.name}'s search box.", verified=False)


def _settings(spec, page, task=None):
    """settings: a named Windows settings page."""
    toolset = _toolset()
    wanted = " ".join(str(page or "").lower().split())
    wanted = "".join(ch for ch in wanted if ch.isalnum() or ch == "-")
    uri = f"ms-settings:{wanted}" if wanted else "ms-settings:"
    try:
        os.startfile(uri)                      # noqa: S606 - a fixed shell URI, not a command
    except OSError as exc:
        return Result(False, f"I couldn't open that settings page ({type(exc).__name__}).")
    time.sleep(1.2)
    front = toolset.foreground_title()
    if "settings" in front.lower():
        return Result(True, f"Opened the settings page '{wanted or 'home'}' in '{front}'.",
                      verified=True)
    return Result(True, f"Asked Windows to open '{wanted or 'settings'}'; the window in "
                        f"front is '{front}'.", verified=False)


def _system(spec, detail, task=None):
    """system: open a read-only system view and report what it shows."""
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    toolset = _toolset()
    toolset.wait(1, task)
    return toolset.read_screen(limit=25)


# ---------- the entry point the planner calls --------------------------------


def run(app, action, detail="", target="", task=None, confirm=None):
    """One app function, carried out for real. Returns a Result.

    The app is resolved through the registry, the action against what that app can
    actually do, and anything else is refused with the list of what this app does
    support - which is how the model learns, instead of guessing again.
    """
    spec, problem = registry.resolve(app)
    if spec is None:
        return Result(False, problem or f"I don't know an app called '{app}'.")
    action = " ".join(str(action or "open").lower().split())
    if action in ("open",):
        return registry.open_app(spec.name)
    if not registry.has_action(spec.key, action):
        available = ", ".join(registry.capabilities(spec.key))
        return Result(False, f"{spec.name} can't do '{action}'. It can: {available}.")
    log.event("appfunc", app=spec.key, action=action,
              detail=f"{len(detail)} chars" if detail else "none",
              target=target or "none")
    if action in ("play_pause", "next", "previous"):
        from . import os_ops
        key = {"play_pause": "play_pause", "next": "next", "previous": "prev"}[action]
        try:
            os_ops.press_key(key)
        except OSError as exc:
            return Result(False, f"I couldn't send that media key ({type(exc).__name__}).")
        return Result(True, f"Sent the {action.replace('_', ' ')} key to {spec.name}.",
                      verified=False)
    handlers = {
        "search_play": lambda: _play(spec, detail, task),
        "create_project": lambda: _named_project(spec, target, detail, task),
        "ask": lambda: _ask(spec, detail, task),
        "write_text": lambda: _write_text(spec, detail, target, task),
        "launch_game": lambda: _launch_game(spec, task),
        "navigate": lambda: _navigate(spec, detail or target, task),
        "search": lambda: _search(spec, detail or target, task),
        "settings": lambda: _settings(spec, detail, task),
        "run_test": lambda: _run_test(spec, task),
        "system": lambda: _system(spec, detail, task),
        "send_message": lambda: _send_message(spec, target, detail, task, confirm),
    }
    handler = handlers.get(action)
    if handler is None:
        return Result(False, f"I don't know how to '{action}' in {spec.name} yet.")
    result = handler()
    log.event("appfunc_done", app=spec.key, action=action, ok=bool(result.ok),
              verified=bool(result.verified))
    return result


def _run_test(spec, task=None):
    """run_test: start the app's own measurement and report what it is doing."""
    opened = _open(spec, task)
    if not opened.ok:
        return opened
    toolset = _toolset()
    time.sleep(2.0)
    _title, labels = _screen()
    for wanted in ("go", "start", "begin test", "start test"):
        hit = _pick(wanted, labels, block=LAUNCHER_NAV)
        if hit:
            clicked = toolset.click_text(hit)
            if clicked.ok:
                return Result(True, f"Started the test in {spec.name}. It takes about half a "
                                    f"minute; the numbers appear in the window.", verified=False)
    return Result(True, f"{spec.name} is open, but I couldn't find its start button to press.",
                  verified=False)
