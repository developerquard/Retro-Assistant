"""The 20 typed actions.

Each action has keywords (English, Hinglish and Hindi), an extractor that pulls
arguments from the text (None means "not for this action"), a risk tier, and a
run function. Run functions report whether the result was verified.
"""
import datetime as dt
import os
import re
import threading
import urllib.parse

from . import apps, config, fsguard, log, os_ops, runtime, settings, ui_hook
from .model import Action, Result, norm, has_kw

APPS = {"notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
        "discord": "Discord.exe", "spotify": "Spotify.exe", "chrome": "chrome.exe", "edge": "msedge.exe"}
SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com", "github": "https://github.com"}
# Extra spellings that must resolve to a canonical key above: Devanagari names, since
# the user speaks Hindi, plus the mis-spellings the fallback model produces. Resolving
# to the canonical key keeps the spoken reply and the log in English.
APP_ALIASES = {"spotifai": "spotify", "spotifi": "spotify",
               "स्पॉटीफाई": "spotify", "स्पॉटिफाई": "spotify",
               "नोटपैड": "notepad", "कैलकुलेटर": "calculator", "कैल्कुलेटर": "calculator",
               "पेंट": "paint", "डिस्कॉर्ड": "discord", "क्रोम": "chrome", "एज": "edge"}
SITE_ALIASES = {"यूट्यूब": "youtube", "गूगल": "google", "गिटहब": "github", "yt": "youtube"}
OPEN_KW = ("open", "launch", "start", "run", "kholo", "खोलो", "चालू करो")
# Words around an app name in "please open legacy launcher for me".
OPEN_FILLERS = ("can you", "could you", "please", "launch", "start up", "start", "run", "open up",
                "retro", "खोल दो", "khol do", "चालू करो", "chalu karo", "को खोलो", "open",
                "kholo", "खोलो")
SEARCH_FILLERS = ("search for", "search", "khojo", "खोजो", "on the web", "the web", "वेब पर", "web par")
FIND_KW = ("find", "dhoondho", "dhundho", "ढूँढो", "ढूंढो")
FIND_FILLERS = (r"in (?:my )?retroworkspace", r"in (?:my )?workspace", r"retroworkspace\s*में", "में",
                r"\bretroworkspace\b", r"\bworkspace\b", r"\bmein\b", r"\bandar\b")
FIND_VERBS = r"\b(?:find|dhoondho|dhundho|and|open|a|file)\b"
MINUTES = re.compile(r"(?<![0-9])(\d{1,3})\s*(?:minutes?|mins?|मिनट)")


# ---------- extractors ------------------------------------------------------

def ex_always(raw):
    return {}


def ex_set_volume(raw):
    m = re.search(r"(?<![0-9])(\d{1,3})(?![0-9])", norm(raw))
    if not m or int(m.group(1)) > 100:
        return None
    return {"percent": int(m.group(1))}


def resolve(table, aliases, name):
    """Canonical key for a spelled-out name, or None if it isn't one of ours."""
    key = " ".join(str(name).strip().lower().split())
    if key in table:
        return key
    return aliases.get(key)


def _target(raw, table, aliases):
    t = norm(raw)
    for name, value in table.items():
        if has_kw(t, name):
            return name, value
    for alias, canonical in aliases.items():
        if has_kw(t, alias):
            return canonical, table[canonical]
    return None


def ex_open_app(raw):
    """The app name as spoken, with the words around it removed.

    This used to return None unless the name was one of seven known programs,
    which is why "open legacy launcher" answered that it had no such script. Now
    any name is passed on and matched against what is actually installed.
    
    Conjunctions and anything after them are dropped so that "open chatgpt and
    type this" extracts the name "chatgpt" rather than "chatgpt and type this".
    The conjunction is the boundary between the open-app request and the next step.
    """
    if _target(raw, SITES, SITE_ALIASES) is not None:
        return None                      # "open youtube" is the site action's business
    hit = _target(raw, APPS, APP_ALIASES)
    if hit is not None:
        return {"name": hit[0]}
    text = norm(raw)
    # Cut the sentence at the first conjunction that begins a second step.
    # Both English and Hinglish connectors are used by looks_multi().
    cut = re.search(r"\b(?:and|aur|phir|then|after that|uske baad|ke baad|for|for me|and then)\b", text)
    if cut is not None:
        text = text[:cut.start()]
    for filler in OPEN_FILLERS:
        if filler.isascii():
            # Word boundaries matter here: stripping "launch" with a plain replace
            # turned "open legacy launcher" into "legacy er".
            text = re.sub(r"(?<![a-z])" + re.escape(filler) + r"(?![a-z])", " ", text)
        else:
            text = text.replace(filler, " ")
    name = " ".join(text.split()).strip(" .?!:।,-")
    return {"name": name} if name else None


def ex_open_site(raw):
    hit = _target(raw, SITES, SITE_ALIASES)
    return {"name": hit[0], "url": hit[1]} if hit else None


def ex_search(raw):
    q = norm(raw)
    for filler in SEARCH_FILLERS:
        q = q.replace(filler, " ")
    q = " ".join(q.split()).strip(" .?!:।")
    return {"query": q} if q else None


def ex_find(raw):
    q = norm(raw)
    for kw in FIND_KW:
        q = q.replace(kw, " ")
    for filler in FIND_FILLERS:
        q = re.sub(filler, " ", q)
    q = re.sub(FIND_VERBS, " ", q)
    q = " ".join(q.split()).strip(" .?!:।")
    return {"query": q} if q else None


def ex_note(raw):
    body = raw.split(":", 1)[1].strip() if ":" in raw else ""
    return {"text": body}


def ex_timer(raw):
    m = MINUTES.search(norm(raw))
    if not m or not 1 <= int(m.group(1)) <= 180:
        return None
    return {"minutes": int(m.group(1))}


# ---------- run functions ---------------------------------------------------

def run_time(args):
    now = dt.datetime.now()
    return Result(True, f"It is {now:%I:%M %p}.", verified=True)


def run_date(args):
    return Result(True, f"Today is {dt.datetime.now():%A, %d %B %Y}.", verified=True)


def run_battery(args):
    import psutil
    b = psutil.sensors_battery()
    if b is None:
        return Result(False, "I can't read a battery on this device.")
    plug = "plugged in" if b.power_plugged else "on battery"
    return Result(True, f"Battery at {b.percent:.0f}%, {plug}.", verified=True)


def run_cpu_ram(args):
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    free_gb = mem.available / 2**30
    return Result(True, f"CPU {cpu:.0f}%. RAM {mem.percent:.0f}% used, {free_gb:.1f} GB free.", verified=True)


def run_internet(args):
    up = os_ops.internet_up()
    return Result(up, "Yes, you're connected." if up else "No internet connection.", verified=True)


def run_set_volume(args):
    ok, why = os_ops.set_volume_percent(args["percent"])
    if not ok:
        return Result(False, why)
    return Result(True, f"Volume set to {args['percent']}%.", verified=False)


def _volume_key(key, message):
    def run(args):
        os_ops.press_key(key)
        return Result(True, message + " I can't read the volume back, so please check it.", verified=False)
    return run


def _media_key(key, message):
    def run(args):
        os_ops.press_key(key)
        return Result(True, message + " (media keys are toggles, so I can't confirm the state.)", verified=False)
    return run


def run_open_app(args):
    """Opens a known program, or any application installed on this machine."""
    name = args["name"]
    key = resolve(APPS, APP_ALIASES, name)
    if key is not None:
        try:
            pid = os_ops.start_app(key, APPS[key])
        except FileNotFoundError:
            return Result(False, f"I couldn't find {key} on this laptop.")
        except OSError as exc:
            return Result(False, f"I couldn't start {key} ({type(exc).__name__}).")
        # Launched, but a launcher process (calc.exe) exits as soon as the real
        # window appears, so "still alive" is not a reliable check for every app.
        return Result(True, f"Opened {key}.", verified=bool(os_ops.process_alive(pid)))
    found, others = apps.best(name)
    if found is None:
        if others:
            return Result(False, f"More than one app matches '{name}'.", choices=[a.name for a in others])
        return Result(False, f"I couldn't find an app called '{name}'.")
    try:
        pid = apps.launch(found)
    except OSError as exc:
        return Result(False, f"I couldn't start {found.name} ({type(exc).__name__}).")
    alive = bool(pid and os_ops.process_alive(pid))
    return Result(True, f"Opened {found.name}.", verified=alive)


def run_open_site(args):
    opened = bool(os_ops.open_url(args["url"]))
    return Result(opened, f"Opened {args['name']}." if opened else "Couldn't open the browser.", verified=False)


def run_search(args):
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(args["query"])
    opened = bool(os_ops.open_url(url))
    return Result(opened, "Searching the web." if opened else "Couldn't open the browser.", verified=False)


_timers = []


def _timer_done(minutes):
    """A finished timer says so: a chime first, then the words.

    It used to print one line to standard output, which the packaged build does not
    have - so a timer set by voice in the real app went off in a place nobody could
    see or hear. The chime is what gets through when the machine is in another room,
    and the session's own voice says the rest; with no session running - a test, a tool
    check, a console - the chime still plays and the line is still written, because a
    timer that only works while the window is open is not a timer.
    """
    line = f"Your {minutes}-minute timer is done."
    chime = None
    try:
        from . import sound
        chime = sound.cue("timer_done")
    except Exception as exc:                     # no device, no numpy: still a timer
        log.event("timer_chime_failed", error=f"{type(exc).__name__}: {exc}"[:120])
    log.event("timer_done", minutes=int(minutes))
    active = runtime.session()
    if active is not None:
        # The chime is waited for, briefly, because two sounds at once is noise and
        # the chime is the one that carries through a closed door. A chime that never
        # finishes cannot hold the words back: the join has its own deadline.
        if chime is not None:
            try:
                chime.join(1.0)
            except Exception:
                pass
        active.say_async(line)
    else:
        print(f"\nretro> {line}", flush=True)


def run_timer_start(args):
    minutes = args["minutes"]
    t = threading.Timer(minutes * 60, _timer_done, args=(minutes,))
    t.daemon = True
    t.start()
    _timers.append(t)
    return Result(True, f"Timer set for {minutes} minutes.", verified=t.is_alive())


def run_timer_cancel(args):
    live = [t for t in _timers if t.is_alive()]
    for t in live:
        t.cancel()
    _timers.clear()
    if not live:
        return Result(False, "No timer is running.")
    return Result(True, "Timer cancelled.", verified=True)


def run_note(args):
    text = args["text"]
    if not text:
        return Result(False, "Say what the note should say after a colon, like: Create a note: ...")
    (config.WORKSPACE / "Notes").mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    n = 0
    while True:
        suffix = "" if n == 0 else f"_{n}"
        path = fsguard.resolve_inside(config.WORKSPACE, f"Notes/note_{stamp}{suffix}.txt")
        try:
            with open(path, "x", encoding="utf-8") as f:   # "x" never overwrites
                f.write(text + "\n")
            break
        except FileExistsError:
            n += 1
    return Result(True, f"Note saved as {path.name} in Notes.", verified=path.exists())


def find_files(query):
    root = config.WORKSPACE
    q = query.lower()
    hits = []
    for dirpath, _dirs, files in os.walk(root):          # followlinks is off by default
        for name in files:
            if q in name.lower():
                try:
                    hits.append(fsguard.resolve_inside(root, os.path.relpath(os.path.join(dirpath, name), root)))
                except fsguard.OutsideWorkspace:
                    pass
    return hits


def run_open_settings(args):
    """Asks a running window to show itself. Without one, only the reply is heard."""
    ui_hook.emit("show_settings")
    return Result(True, "Opening my settings.", verified=False)


def run_play_intro(args):
    """Plays the arrival again: the logo, the fade, and the line at the end of it.

    The same event the tray item sends, so there is one way in and one way it behaves -
    and with no window running it says so rather than claiming to have shown anything.
    """
    ui_hook.emit("play_intro")
    return Result(True, "Playing my intro.", verified=False)


def run_startup_on(args):
    """Puts Retro in the Windows startup list - the same entry the panel switch shows."""
    from . import startup
    ok, message = startup.enable()
    if ok:
        settings.save(startup=True)
    return Result(ok, message, verified=bool(ok))


def run_startup_off(args):
    from . import startup
    ok, message = startup.disable()
    if ok:
        settings.save(startup=False)
    return Result(ok, message, verified=bool(ok))


def run_startup_status(args):
    """Answers with what the registry says, not with what was asked for.

    `verified` is True because the answer was read back out of the registry, and the
    reading is the whole point: "I am in the startup list" and "this machine's startup
    list contains a path that is not me" are different answers, and only one of them
    means the logo will play tomorrow morning.
    """
    from . import startup
    message, _on = startup.status()
    return Result(True, f"Start with Windows: {message}.", verified=True)


def run_find(args):
    hits = find_files(args["query"])
    if not hits:
        return Result(False, f"No file matching '{args['query']}' in RetroWorkspace.")
    if len(hits) > 1:
        return Result(False, f"I found {len(hits)} matches. Which one?", choices=[str(h) for h in hits])
    os_ops.open_file(str(hits[0]))
    return Result(True, f"Opened {hits[0].name}.", verified=False)


# ---------- the registry ----------------------------------------------------

ACTIONS = (
    Action(name="time", tier="read",
           keywords=("what time", "time is it", "the time", "kitne baje", "कितने बजे", "समय"),
           extract=ex_always, run=run_time, description="Tell the time"),
    Action(name="date", tier="read", keywords=("today's date", "what date", "date", "tarikh", "तारीख"),
           extract=ex_always, run=run_date, description="Tell the date"),
    Action(name="battery", tier="read", keywords=("battery", "बैटरी"),
           extract=ex_always, run=run_battery, description="Battery status"),
    Action(name="cpu_ram", tier="read", keywords=("cpu", "ram", "memory usage", "सीपीयू", "रैम"),
           extract=ex_always, run=run_cpu_ram, description="CPU and RAM status"),
    Action(name="internet", tier="read", keywords=("internet", "इंटरनेट", "connected", "connection"),
           extract=ex_always, run=run_internet, description="Internet status"),
    Action(name="set_volume", tier="reversible", keywords=("volume", "वॉल्यूम"),
           extract=ex_set_volume, run=run_set_volume, description="Set volume to a percentage"),
    Action(name="volume_up", tier="reversible",
           keywords=("increase the volume", "increase volume", "volume up", "volume badhao",
                     "awaaz badhao", "आवाज़ बढ़ाओ", "आवाज बढ़ाओ"),
           extract=ex_always, run=_volume_key("vol_up", "Volume up one step."), description="Volume up"),
    Action(name="volume_down", tier="reversible",
           keywords=("decrease the volume", "decrease volume", "volume down", "volume kam",
                     "awaaz kam", "आवाज़ कम करो", "आवाज कम करो"),
           extract=ex_always, run=_volume_key("vol_down", "Volume down one step."), description="Volume down"),
    Action(name="toggle_mute", tier="reversible", keywords=("mute", "unmute", "म्यूट"),
           extract=ex_always, run=_volume_key("mute", "Mute toggled."), description="Mute or unmute"),
    Action(name="open_app", tier="reversible", keywords=OPEN_KW,
           extract=ex_open_app, run=run_open_app, description="Open an approved app"),
    Action(name="open_site", tier="reversible", keywords=OPEN_KW,
           extract=ex_open_site, run=run_open_site, description="Open an approved website"),
    Action(name="search_web", tier="reversible", keywords=("search", "khojo", "खोजो"),
           extract=ex_search, run=run_search, description="Search the web"),
    Action(name="pause", tier="reversible", keywords=("pause", "music roko", "म्यूज़िक रोक", "म्यूजिक रोक"),
           extract=ex_always, run=_media_key("play_pause", "Paused."), description="Pause media"),
    Action(name="resume", tier="reversible",
           keywords=("resume", "play the music", "music chalao", "phir se chalao", "फिर से चलाओ"),
           extract=ex_always, run=_media_key("play_pause", "Resumed."), description="Resume media"),
    Action(name="next_track", tier="reversible", keywords=("next track", "next song", "agla gaana", "अगला गाना"),
           extract=ex_always, run=_media_key("next", "Next track."), description="Next track"),
    Action(name="prev_track", tier="reversible",
           keywords=("previous track", "previous song", "pichla gaana", "पिछला गाना"),
           extract=ex_always, run=_media_key("prev", "Previous track."), description="Previous track"),
    Action(name="timer_start", tier="reversible",
           keywords=("set a timer", "set timer", "start timer", "start the timer",
                     "timer lagao", "टाइमर लगाओ"),
           extract=ex_timer, run=run_timer_start, description="Start a timer"),
    Action(name="timer_cancel", tier="reversible",
           keywords=("cancel the timer", "cancel timer", "timer cancel", "timer radd", "टाइमर रद्द"),
           extract=ex_always, run=run_timer_cancel, description="Cancel the timer"),
    Action(name="create_note", tier="reversible", keywords=("note", "नोट"),
           extract=ex_note, run=run_note, description="Create a new note in Notes"),
    Action(name="find_file", tier="read", keywords=FIND_KW,
           extract=ex_find, run=run_find, description="Find a file in RetroWorkspace"),
    # "settings" is longer than the "open" keyword the app and site actions use,
    # so it wins the score whenever the word appears.
    # The arrival has words of its own, because a feature the owner will want to show
    # somebody is a feature they will want to ask for by voice: "play your intro".
    Action(name="play_intro", tier="read",
           keywords=("play your intro", "your intro", "the intro", "show your logo",
                     "play the logo", "your logo again", "logo reveal"),
           extract=ex_always, run=run_play_intro,
           description="Play the Retro logo and the fade again"),
    # "start with windows" and "don't start with windows" are both about the same
    # entry; the longer phrase wins the match in the router, so the negative is not
    # read as the positive it contains.
    Action(name="startup_on", tier="reversible",
           keywords=("start with windows", "start with my laptop", "start with the laptop",
                     "run at startup", "open at startup", "start at login", "start when i log in",
                     "start automatically", "start mein khulo", "लॉगिन पर शुरू करो"),
           extract=ex_always, run=run_startup_on,
           description="Let Windows start Retro when the user logs in"),
    Action(name="startup_off", tier="reversible",
           keywords=("don't start with windows", "do not start with windows",
                     "stop starting with windows", "remove from startup", "don't open at startup",
                     "startup band karo", "स्टार्टअप बंद करो"),
           extract=ex_always, run=run_startup_off,
           description="Stop Windows starting Retro at logon"),
    Action(name="startup_status", tier="read",
           keywords=("start with windows status", "startup status", "am i in the startup list",
                     "do you open with windows", "will you open with windows"),
           extract=ex_always, run=run_startup_status,
           description="Say whether Retro opens with Windows"),
    Action(name="open_settings", tier="read",
           keywords=("your settings", "the settings", "my settings", "settings", "settings kholo",
                     "your window", "your gui", "show yourself", "सेटिंग्स", "सेटिंग्स खोलो"),
           extract=ex_always, run=run_open_settings, description="Show Retro's own settings window"),
)
