"""The applications Retro knows about, and how each one is started.

Two things live here, and they answer two different questions:

* **what to open** - every entry carries a canonical name, the aliases a person
  might say, a category, the places its executable is normally installed, an AppID
  or a web address where that is the real way in, and the window title to look for
  so a launch can be *verified* rather than assumed;
* **what it can do** - the actions its adapter in core/appfunc.py implements, so
  the model is choosing from real capabilities instead of inventing a workflow for
  every request. This is the knowledge that stops Retro re-deriving "Spotify is a
  music player, music players have a search box" on every song.

Discovery is layered on purpose, because no single method finds everything: a
program may be a Start Menu shortcut, an App Paths entry, a Store app with only an
AppID, a Steam game, or a website. The executable candidate list is tried first
where one is known (a shortcut can be launched but not talked to), then the index
built by core/apps.py, then the shell AppID, then the URL.

Nothing here is built from what the user said, so nothing a person (or a model)
says can become a command line: an app is only ever opened through an entry in
this table, and the worst a wrong guess can do is open the wrong program.
"""
import glob
import os
import subprocess
import time
from collections import namedtuple

from . import apps as app_index
from . import os_ops
from .model import Result

AppSpec = namedtuple("AppSpec", "key name aliases category exe shell url names "
                               "capabilities default_for note verify")

# kind of every entry: how it is started, in order of preference, is derived from
# which of exe/shell/url are filled in. `exe` first, because a launched executable
# can be watched; the other two are for apps that have no file of their own.
REQUIRED = (
    "whatsapp", "discord", "chatgpt", "claude", "youtube", "chrome", "freebuff",
    "modrinth", "crunchyroll", "spotify", "file_explorer", "notepad", "vscode",
    "legacy_launcher", "shadow_fight_2", "system_settings", "github", "odaclass",
    "powerpoint", "excel", "wps_office", "speedtest", "task_manager",
)

# The picker's defaults. "the music app" is a real request, and the answer has to
# be one app with a name, not a category: this is where that choice is kept.
DEFAULTS = {
    "music": "spotify",
    "browser": "chrome",
    "minecraft": "legacy_launcher",
    "files": "file_explorer",
}

# What each key means when somebody says the category instead of a name. Kept
# small and explicit: matching a category to an app is a lookup, not a guess.
_CATEGORY_WORDS = {
    "music": ("music", "song", "songs", "gaana", "gana", "म्यूजिक", "गाना", "spotify"),
    "browser": ("browser", "internet", "google", "chrome", "web browser"),
    "video": ("video", "anime", "movie", "movies", "youtube", "crunchyroll", "वीडियो"),
    "chat": ("chatgpt", "chat gpt", "gpt", "ai", "claude", "chatbot"),
    "minecraft": ("minecraft", "mc", "माइनक्राफ्ट"),
}

_SPECS = (
    AppSpec("whatsapp", "WhatsApp", ("whatsapp", "whats app", "wa", "व्हाट्सएप"),
            "messaging",
            ("%LOCALAPPDATA%\\WhatsApp\\WhatsApp.exe",
             "%PROGRAMFILES%\\WindowsApps\\*WhatsApp*"),
            None, "https://web.whatsapp.com", ("WhatsApp",),
            ("open", "open_chat", "send_message", "search"),
            None,
            "messaging app. Automate through its window: search the contact, open the "
            "chat, type the exact message. Never send to a name that was not found.",
            ("whatsapp",)),
    AppSpec("discord", "Discord", ("discord", "डिस्कोर्ड"), "messaging",
            ("%LOCALAPPDATA%\\Discord\\app-*\\Discord.exe",
             "%LOCALAPPDATA%\\Discord\\Discord.exe"),
            None, "https://discord.com/app", ("Discord",),
            ("open", "open_chat", "send_message", "search"),
            None,
            "chat app for servers and friends. Search a DM or channel, open it, type "
            "the message. Sending needs the recipient confirmed first.",
            ("discord",)),
    AppSpec("chatgpt", "ChatGPT", ("chatgpt", "chat gpt", "gpt", "openai", "चैटजीपीटी"),
            "chat",
            None, None, "https://chatgpt.com", ("ChatGPT",),
            ("open", "ask"),
            None,
            "a chat assistant in the browser. Open it, click the message box, type the "
            "prompt, press enter.",
            ("chatgpt",)),
    AppSpec("claude", "Claude", ("claude", "claude ai", "claud"), "chat",
            None, None, "https://claude.ai/new", ("Claude",),
            ("open", "ask"),
            None,
            "a chat assistant in the browser. Same flow as ChatGPT: message box, type, enter.",
            ("claude",)),
    AppSpec("youtube", "YouTube", ("youtube", "you tube", "yt", "यूट्यूब"), "video",
            None, None, "https://www.youtube.com", ("YouTube",),
            ("open", "search_play", "play_pause", "next", "previous"),
            None,
            "video site. Type the search into the search box and press enter, then the "
            "first result. Playback can also be driven with the media keys.",
            ("youtube",)),
    AppSpec("chrome", "Chrome", ("chrome", "google chrome", "browser", "google", "क्रोम"),
            "browser",
            ("%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe",
             "%PROGRAMFILES(X86)%\\Google\\Chrome\\Application\\chrome.exe",
             "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
            None, None, ("Google Chrome",),
            ("open", "navigate", "search", "search_play", "play_pause", "next", "previous"),
            "browser",
            "the web browser. A URL or a search goes in the address bar; the address bar "
            "is reached with ctrl+l, which is more reliable than finding it by label.",
            ("chrome", "google chrome")),
    AppSpec("freebuff", "FreeBuff", ("freebuff", "free buff", "freebuf"), "dev",
            None, None, None, ("FreeBuff",),
            ("open", "navigate", "ask"),
            None,
            "the assistant's own desktop app. Open it like any other program; it is a "
            "chat window, so prompts go in its message box.",
            ("freebuff", "freebuff desktop")),
    AppSpec("modrinth", "Modrinth", ("modrinth", "modrinth app", "mod rin th"), "games",
            ("%APPDATA%\\ModrinthApp\\Modrinth App.exe",
             "%LOCALAPPDATA%\\Programs\\Modrinth App\\Modrinth App.exe"),
            None, "https://modrinth.com", ("Modrinth", "Modrinth App"),
            ("open", "search", "install"),
            None,
            "Minecraft mod browser and launcher. Search finds mods and modpacks; "
            "installing one can be started from its page.",
            ("modrinth",)),
    AppSpec("crunchyroll", "Crunchyroll", ("crunchyroll", "crunchy roll", "anime"),
            "video",
            None, None, "https://www.crunchyroll.com", ("Crunchyroll",),
            ("open", "search_play", "play_pause", "next", "previous"),
            None,
            "anime streaming. Search the title in the search box and open the series or "
            "episode; playback uses the player controls or the media keys.",
            ("crunchyroll",)),
    AppSpec("spotify", "Spotify", ("spotify", "spot", "स्पॉटिफाई"), "music",
            ("%APPDATA%\\Spotify\\Spotify.exe",),
            # This machine has the Store build, and its AppID is the canonical way in:
            # measured here, it brings up the real desktop app ('Spotify Free') in
            # about five seconds, where the old entry fell through to a browser page
            # whose controls UI Automation cannot reach.
            "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
            "https://open.spotify.com", ("Spotify",),
            ("open", "search_play", "play_pause", "next", "previous", "search"),
            "music",
            "music player. Ctrl+l focuses its search box; type the song, press enter, "
            "then enter again on the top result to play it. Media keys control playback.",
            ("spotify",)),
    AppSpec("file_explorer", "File Explorer", ("file explorer", "explorer", "files",
                                              "my files", "this pc", "फाइल"),
            "files",
            ("explorer.exe",), None, None, ("File Explorer",),
            ("open", "open_folder"),
            "files",
            "the file manager. It can be pointed at a known folder - downloads, documents, "
            "desktop, pictures, music, videos, the Retro workspace.",
            ("file explorer", "explorer")),
    AppSpec("notepad", "Notepad", ("notepad", "note pad", "editor", "नोटपैड"), "editor",
            ("notepad.exe",), None, None, ("Notepad",),
            ("open", "write_text", "save_file"),
            None,
            "plain text editor. Typing goes straight into its edit area once the window "
            "is in front; ctrl+s then a file name saves it.",
            ("notepad",)),
    AppSpec("vscode", "VS Code", ("vs code", "vscode", "visual studio code", "code editor",
                                 "वीएस कोड"),
            "dev",
            ("%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe",
             "%PROGRAMFILES%\\Microsoft VS Code\\Code.exe",
             "code.cmd", "code"),
            None, None, ("Visual Studio Code",),
            ("open", "open_folder", "create_project", "run", "search"),
            None,
            "code editor. Real work is done by writing files into the project folder and "
            "opening that folder in the editor, not by typing code into its window.",
            ("visual studio code", "vs code")),
    AppSpec("legacy_launcher", "Legacy Launcher",
            ("legacy launcher", "legacy", "tl legacy", "tlauncher", "minecraft launcher",
             "minecraft", "mc launcher"),
            "games",
            ("%APPDATA%\\.tlauncher\\*\\legacy.exe",
             "%LOCALAPPDATA%\\LegacyLauncher\\legacy.exe",
             "%PROGRAMFILES%\\Legacy Launcher\\legacy.exe"),
            None, None, ("Legacy Launcher", "TL Legacy", "TLauncher", "Minecraft Launcher"),
            ("open", "launch_game", "search"),
            "minecraft",
            "the Minecraft launcher the user actually has. A request for Minecraft means "
            "this, never the website. Its play button is in the launcher window.",
            ("legacy launcher", "tlauncher", "minecraft launcher")),
    AppSpec("shadow_fight_2", "Shadow Fight 2",
            ("shadow fight 2", "shadow fight", "shadowfight", "shadow fight ii"),
            "games",
            None, None, None, ("Shadow Fight 2", "Shadow Fight"),
            ("open", "launch_game"),
            None,
            "a game. Open it from the Start menu or the Store entry and let the game "
            "window come up; there is no shortcut into a match.",
            ("shadow fight",)),
    AppSpec("system_settings", "System Settings",
            ("system settings", "settings", "windows settings", "setings", "सेटिंग्स"),
            "system",
            None, "ms-settings:", None, ("Settings",),
            ("open", "settings"),
            None,
            "the Windows settings app. It takes a page, for example ms-settings:display, "
            "ms-settings:sound, ms-settings:bluetooth.",
            ("settings",)),
    AppSpec("github", "GitHub", ("github", "git hub", "गिटहब"), "web",
            None, None, "https://github.com", ("GitHub",),
            ("open", "navigate", "search"),
            None,
            "code hosting site, used in the browser. A repository name can be put in the "
            "search box or the address bar.",
            ("github",)),
    AppSpec("odaclass", "Odaclass", ("odaclass", "oda class", "odaclass app"), "web",
            None, None, "https://odaclass.com", ("Odaclass",),
            ("open", "navigate", "search"),
            None,
            "the user's online class site. Open it and navigate; anything further is done "
            "in the page itself.",
            ("odaclass",)),
    AppSpec("powerpoint", "PowerPoint", ("powerpoint", "power point", "ppt", "पावरपॉइंट"),
            "office",
            None, None, None, ("PowerPoint",),
            ("open", "write_text", "save_file"),
            None,
            "presentation editor. A new blank deck is ctrl+n; text goes into the title "
            "placeholder, and ctrl+s saves it with a name.",
            ("powerpoint",)),
    AppSpec("excel", "Excel", ("excel", "ms excel", "spreadsheet", "एक्सेल"), "office",
            None, None, None, ("Excel",),
            ("open", "write_text", "save_file"),
            None,
            "spreadsheet editor. A new blank sheet is ctrl+n; typed rows go into the first "
            "cell and move down with enter.",
            ("excel",)),
    AppSpec("wps_office", "WPS Office", ("wps office", "wps", "wps writer", "kingsoft"),
            "office",
            ("%LOCALAPPDATA%\\Kingsoft\\WPS Office\\*\\office6\\wps.exe",
             "%PROGRAMFILES%\\WPS Office\\*\\office6\\wps.exe"),
            None, None, ("WPS Office", "WPS Writer", "WPS"),
            ("open", "write_text", "save_file"),
            None,
            "the office suite the user has for documents, spreadsheets and slides. Opening "
            "it brings up its hub; a document opens from there.",
            ("wps office", "wps")),
    AppSpec("speedtest", "Speedtest", ("speedtest", "speed test", "internet speed",
                                      "ookla"), "system",
            None, None, "https://www.speedtest.net", ("Speedtest", "Speedtest by Ookla"),
            ("open", "run_test"),
            None,
            "measures the internet speed. In the browser the test starts with the GO "
            "button; the app starts it on its own.",
            ("speedtest",)),
    AppSpec("task_manager", "Task Manager", ("task manager", "taskmanager", "processes"),
            "system",
            ("taskmgr.exe",), None, None, ("Task Manager",),
            ("open", "system"),
            None,
            "shows what is running and how much it is using. It is a read-only view - "
            "ending a task is not something Retro does.",
            ("task manager",)),
)

SPECS = {spec.key: spec for spec in _SPECS}


def spec_for(key):
    """The entry for a known key, or None."""
    return SPECS.get(str(key or "").strip().lower().replace(" ", "_"))


def capabilities(key):
    spec = spec_for(key)
    return spec.capabilities if spec else ()


def has_action(key, action):
    return str(action or "").strip().lower() in capabilities(key)


def _window_hints(spec):
    return tuple(hint.lower() for hint in (spec.verify or ()))


def _exe_candidates(spec):
    found = []
    for pattern in spec.exe or ():
        path = os.path.expandvars(pattern)
        if "*" in path:
            hits = sorted(glob.glob(path))
            found.extend(reversed(hits[-3:]))       # newest first
            continue
        found.append(path)
    return found


def _launch_path(path):
    """Starts one executable or shortcut. Returns a pid when there is one to watch."""
    if path.lower().endswith(".lnk"):
        os.startfile(path)
        return None
    if os.path.isabs(path) and os.path.exists(path):
        return subprocess.Popen([path]).pid
    import shutil
    resolved = shutil.which(path)
    if resolved:
        return subprocess.Popen([resolved]).pid
    return None


def _start(spec):
    """Tries every way in, best first. Returns (kind, label, pid) or raises.

    The order matters: an executable or shortcut that this session started can be
    watched and typed into with confidence, while a URL is handed to the browser
    and nothing about it can be verified.
    """
    tried = []
    for path in _exe_candidates(spec):
        if "*" in path:
            continue
        exists = os.path.exists(path) if os.path.isabs(path) else bool(__import__("shutil").which(path))
        if not exists:
            continue
        try:
            pid = _launch_path(path)
            return ("exe", os.path.basename(path), pid)
        except OSError as exc:
            tried.append(f"{os.path.basename(path)} ({type(exc).__name__})")
    if spec.shell:
        pid = subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{spec.shell}"]).pid
        return ("shell", spec.shell, pid)
    hit, others = app_index.best(spec.name)
    if hit is None:
        for alias in spec.names or ():
            hit, extra = app_index.best(alias)
            others = others or extra
            if hit is not None:
                break
    if hit is not None:
        return ("installed", hit.name, app_index.launch(hit))
    if spec.url:
        opened = os_ops.open_url(spec.url)
        if not opened:
            raise OSError("the browser refused to open")
        return ("url", spec.url, None)
    raise FileNotFoundError(spec.name)


def _wait_for_app(spec, pid, timeout_s=6.0):
    """Waits a moment to see whether the program actually arrived.

    A pid that is alive, or a window with the expected title, is what counts as
    opened. Nothing here claims success just because a call returned.
    """
    hints = _window_hints(spec)
    deadline = time.time() + max(0.5, float(timeout_s))
    alive = False
    while time.time() < deadline:
        if pid is not None and os_ops.process_alive(pid):
            alive = True
        if hints:
            try:
                from . import tools as toolset
                titles = [title.lower() for _hwnd, title in toolset.window_titles(limit=40)]
            except Exception:
                titles = []
            for title in titles:
                if any(hint in title for hint in hints):
                    return True, title
        if alive and not hints:
            return True, ""
        time.sleep(0.35)
    return alive, ""


def open_app(name, wait_s=6.0):
    """Opens an application by the name the user said. Returns a Result.

    The message says what happened and how it was checked: "opened" when a window
    with the expected title appeared, "started" when only the process was seen, and
    a refusal when neither. A launch that cannot be verified is reported as such.
    """
    wanted = " ".join(str(name or "").strip().split())
    if not wanted:
        return Result(False, "I need a name to open.")
    spec, message = resolve(wanted)
    if spec is None:
        return Result(False, message or f"I couldn't find an app called '{wanted}'.")
    try:
        kind, label, pid = _start(spec)
    except FileNotFoundError:
        return Result(False, f"I know what {spec.name} is, but it isn't installed on this "
                             f"laptop - I couldn't find anything to start.")
    except OSError as exc:
        return Result(False, f"I couldn't start {spec.name} ({type(exc).__name__}).")
    if kind == "url":
        return Result(True, f"Opened {spec.name} in the browser ({spec.url}).",
                      verified=False)
    verified, title = _wait_for_app(spec, pid, wait_s)
    if verified:
        where = f"the window '{title}'" if title else "its process"
        return Result(True, f"Opened {spec.name} - I can see {where}.", verified=True)
    return Result(True, f"Started {spec.name}, but I can't see its window yet - it may still "
                        f"be loading.", verified=False)


def resolve(text):
    """(spec, message) for what the user said. `spec` is None when nothing fits.

    Names are matched against the registry's own aliases first, then the category
    words ("the music app"), then the installed programs on this machine - which is
    how an app nobody wrote an entry for can still be opened, as long as the user
    says a name Windows knows.
    """
    wanted = " ".join(str(text or "").strip().split())
    if not wanted:
        return None, "I need a name to open."
    low = app_index.normalize(wanted)
    if not low:
        return None, "I need a name to open."
    for spec in _SPECS:
        for alias in (spec.key.replace("_", " "), spec.name) + tuple(spec.aliases):
            if low == app_index.normalize(alias):
                return spec, ""
    # A category rather than a name: "the music app", "my browser".
    for category, words in _CATEGORY_WORDS.items():
        if any(word == low or (len(word) > 3 and word in low) for word in words):
            key = DEFAULTS.get(category)
            if key:
                return SPECS[key], ""
    hit, others = app_index.best(wanted)
    if hit is not None:
        for spec in _SPECS:
            for alias in (spec.key.replace("_", " "), spec.name) + tuple(spec.aliases):
                if app_index.normalize(alias) == app_index.normalize(hit.name):
                    return spec, ""
        return None, (f"'{wanted}' matches the installed program '{hit.name}', which has no "
                      f"adapter in Retro's app registry yet. I can open it by exact name: "
                      f"\"open {hit.name}\".")
    if others:
        names = ", ".join(app.name for app in others)
        return None, f"I couldn't tell which app you meant. Did you mean: {names}?"
    return None, f"I couldn't find an app called '{wanted}'."


def catalogue():
    """What the planner is told about applications: purpose, actions, and defaults.

    This is the compact form. Every line is one app, its real capabilities and what
    to use it for - enough for the model to pick an app and an action without
    rediscovering either, and small enough that it does not crowd out the tools.
    """
    lines = ["Applications (use open_app for the first, app_function for the rest):"]
    for spec in _SPECS:
        actions = ", ".join(spec.capabilities)
        tag = " [default music app]" if spec.default_for == "music" else ""
        lines.append(f"- {spec.name} ({spec.category}){tag}: {spec.note} "
                     f"Actions: {actions}.")
    return "\n".join(lines)
