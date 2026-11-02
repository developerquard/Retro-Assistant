"""Everything installed on this machine, so Retro can open anything - not a list.

The old `open_app` action knew seven programs. That is not a desktop assistant: it
could not open a game, a launcher, an editor or anything installed after it was
written. This module asks Windows what is installed instead:

  * Start Menu shortcuts - what the user actually sees and clicks, including most
    games and launchers;
  * the App Paths registry keys - the executables named on the Run dialog;
  * the shell's own app list (`Get-StartApps`) - Store and UWP apps, which have no
    file to point at.

Nothing here is built from what the user said: a name is only ever *matched*
against paths the system reported, so a spoken name can never become a command
line. The index is cached in the Retro data folder because the shell query costs a
second or two; `load(refresh=True)` rebuilds it.

Matching is deliberately generous - exact, prefix, substring, token overlap and
edit distance - because names arrive from speech, where "legacy" may come back as
"legacy", "legaci" or something the model rewrote.
"""
import difflib
import json
import os
import re
import subprocess
import sys
import time
from collections import namedtuple

from . import config

App = namedtuple("App", "name target kind source")
# kind: "shortcut" (.lnk, launched with startfile), "exe" (a program), "shell" (an AppID)

CACHE_PATH = config.LOG_DIR.parent / "apps.json"
CACHE_MAX_AGE_S = 12 * 3600

# Start Menu entries that are not the program itself.
JUNK_WORDS = ("uninstall", "readme", "release notes", "documentation", "license",
              "website", "help", "manual", "setup", "install", "repair",
              "remove", "配置", "アンインストール")

_index = None
_index_built_at = 0.0

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def normalize(text):
    """Lower case, no punctuation, single spaces. Used for every comparison."""
    cleaned = re.sub(r"[^a-z0-9\u0900-\u097f]+", " ", str(text).lower())
    return " ".join(cleaned.split())


def _is_junk(label):
    low = label.lower()
    return any(word in low for word in JUNK_WORDS) or len(label) < 3


def _start_menu_apps():
    roots = (os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs"))
    apps = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for filename in files:
                if not filename.lower().endswith(".lnk"):
                    continue
                label = os.path.splitext(filename)[0]
                if _is_junk(label):
                    continue
                apps.append(App(label, os.path.join(base, filename), "shortcut", "start menu"))
    return apps


def _registry_app_paths():
    if sys.platform != "win32":
        return []
    import winreg
    locations = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                 (winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"))
    apps = []
    for hive, path in locations:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub) as entry:
                        target, _kind = winreg.QueryValueEx(entry, "")
                except OSError:
                    continue
                label = os.path.splitext(sub)[0]
                if target and not _is_junk(label):
                    apps.append(App(label, str(target), "exe", "registry"))
        finally:
            key.Close()
    return apps


def _shell_apps():
    """Store, UWP and everything else the Windows Start menu lists.

    `Get-StartApps` is a read-only system query. It is the only way to reach apps
    that have no file to launch, and it is why the index is cached.
    """
    if sys.platform != "win32":
        return []
    # Store app names are not ASCII, and PowerShell writes the console code page
    # by default, so the output encoding is forced and decoded leniently.
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              "Get-StartApps | ForEach-Object { \"$($_.Name)`t$($_.AppID)\" }")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    output = (done.stdout or b"").decode("utf-8", errors="replace")
    apps = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        label, app_id = line.split("\t", 1)
        label, app_id = label.strip(), app_id.strip()
        if label and app_id and not _is_junk(label):
            apps.append(App(label, app_id, "shell", "shell"))
    return apps


def discover():
    """Every installed app this machine will admit to, best source first."""
    found = {}
    for app in _start_menu_apps() + _registry_app_paths() + _shell_apps():
        key = normalize(app.name)
        if key and key not in found:
            found[key] = app
    return sorted(found.values(), key=lambda a: a.name.lower())


def _read_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not payload.get("apps"):
        return None
    if time.time() - payload.get("built_at", 0) > CACHE_MAX_AGE_S:
        return None
    return [App(**item) for item in payload["apps"]]


def _write_cache(apps):
    try:
        os.makedirs(CACHE_PATH.parent, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"built_at": time.time(),
                       "apps": [app._asdict() for app in apps]}, handle)
    except OSError:
        pass


def load(refresh=False):
    """The app index, from memory, then the cache, then a fresh scan."""
    global _index, _index_built_at
    if _index is not None and not refresh:
        return _index
    if not refresh:
        cached = _read_cache()
        if cached is not None:
            _index, _index_built_at = cached, time.time()
            return _index
    _index = discover()
    _index_built_at = time.time()
    _write_cache(_index)
    return _index


def score(query, name):
    """How well a spoken name matches an installed one, 0..1."""
    q, n = normalize(query), normalize(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.92
    if q in n:
        return 0.88
    if n in q:
        return 0.82
    q_tokens, n_tokens = set(q.split()), set(n.split())
    shared = q_tokens & n_tokens
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    scores = [0.6 * ratio]
    if shared:
        # How much of what was said is in the name, and how much of the name it is.
        scores.append(0.5 * len(shared) / len(q_tokens) + 0.3 * len(shared) / len(n_tokens))
    if q_tokens and n_tokens and q_tokens <= n_tokens:
        scores.append(0.85)
    if ratio > 0.85:
        scores.append(0.9 * ratio)
    return max(scores)


def search(query, limit=8, apps=None):
    """Installed apps ranked against `query`; only plausible ones are returned."""
    scored = [(score(query, app.name), app) for app in (apps or load())]
    scored = [(value, app) for value, app in scored if value >= 0.55]
    scored.sort(key=lambda pair: (-pair[0], len(pair[1].name)))
    return scored[:limit]


def best(query, apps=None):
    """(app, others). `app` is None when nothing was close enough or it was a tie.

    An exact name is never in doubt, however many other apps share a word with it
    ("Spotify" next to "spotify_cli", "Steam" next to "Steam Support Center").
    Anything less certain is handed back as a shortlist for the model or the user.
    """
    ranked = search(query, apps=apps)
    if not ranked:
        return None, []
    top_score, top = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if top_score >= 1.0:
        return top, []
    if top_score >= 0.86 and top_score - runner_up >= 0.15:
        return top, []
    return None, [app for _value, app in ranked[:5]]


def launch(app):
    """Starts an installed app. Returns a pid when there is one to watch."""
    if app.kind == "shortcut":
        os.startfile(app.target)
        return None
    if app.kind == "shell":
        return subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.target}"]).pid
    return subprocess.Popen([app.target]).pid


def names(apps=None):
    return [app.name for app in (apps or load())]
