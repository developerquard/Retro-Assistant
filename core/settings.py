"""The handful of choices the window remembers between runs.

Which model, which voice, which output device. Stored beside the app index in the
Retro data folder. Nothing here is secret, and nothing here is required: a missing
or unreadable file simply means the defaults, so a broken file can never stop the
assistant from starting.
"""
import json
import os

from . import config

PATH = config.LOG_DIR.parent / "settings.json"
# The choices that survive a restart. `mode` and `followup` are here because the
# user asked for both as settings: chat or work is how the assistant behaves, and
# "listen after Retro" is whether it keeps listening for a follow-up without the
# wake word. Neither is secret, and neither is required.
# `intro` and `intro_sound` are the arrival: whether the logo plays at launch, and
# whether it plays with its soundtrack. `startup` is whether Windows starts Retro at
# logon - the entry itself is the registry's, and this only remembers whose decision
# it was, so that a first run may offer and a later run may not impose. `visual_mode`
# is Music Mode or Media Mode: a look, remembered like the rest.
_KNOWN = ("model", "voice", "output", "mic_device", "mode", "followup", "background",
          "mic", "diagnostics", "music_app", "browser", "local_voice", "intro",
          "intro_sound", "startup", "visual_mode")
_cache = None


def load():
    """The saved settings, or an empty dict."""
    global _cache
    if _cache is None:
        try:
            with open(PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def get(key, default=None):
    return load().get(key, default)


def get_bool(key, default=True):
    """A switch, with the file's own notion of true. Absent means `default`.

    `settings.json` is written by hand as often as by the app, so "on"/"off"/"yes" are
    read as what they say; anything else is the Python truth of the value.
    """
    value = load().get(key, None)
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("on", "yes", "true", "1"):
            return True
        if text in ("off", "no", "false", "0", ""):
            return False
    return bool(value)


def save(**values):
    """Saves any of the known keys. Unknown keys are ignored."""
    data = dict(load())
    data.update({key: value for key, value in values.items() if key in _KNOWN})
    global _cache
    _cache = data
    try:
        os.makedirs(PATH.parent, exist_ok=True)
        with open(PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    except OSError:
        pass          # a choice that cannot be written is not worth failing over
    return data


def forget():
    """Drops the in-memory copy, so the next read comes from disk (used by tests)."""
    global _cache
    _cache = None
