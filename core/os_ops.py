"""The only place that touches Windows. Every function here is patched in tests.

Fixed program names only: no shell, no user-built command lines.
"""
import socket
import subprocess
import sys
import webbrowser

IS_WIN = sys.platform == "win32"

# Windows virtual-key codes for the media and volume keys
VK = {"mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF, "next": 0xB0, "prev": 0xB1, "play_pause": 0xB3}


def press_key(name):
    """Send one media or volume key press. The system doesn't report back, so the caller can't verify it."""
    if not IS_WIN:
        raise OSError("media and volume keys need Windows")
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(VK[name], 0, 0, 0)
    user32.keybd_event(VK[name], 0, 0x0002, 0)  # key up


# Extra fixed locations for approved apps that aren't on PATH. Nothing here is
# ever built from user or model text - it is a lookup table of known install paths.
APP_HINTS = {
    "Discord.exe": (r"%LOCALAPPDATA%\Discord\Discord.exe",
                    r"%LOCALAPPDATA%\Discord\app-*\Discord.exe"),
    "Spotify.exe": (r"%APPDATA%\Spotify\Spotify.exe",),
    "chrome.exe": (r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
                   r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    "msedge.exe": (r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
                   r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
}


def resolve_exe(exe, hints=None):
    """Finds an approved app's executable. Returns None when it isn't installed."""
    import glob
    import os as _os
    import shutil
    table = APP_HINTS if hints is None else hints
    for candidate in (exe,) + tuple(table.get(exe, ())):
        path = _os.path.expandvars(candidate)
        if "*" in path:
            hits = sorted(glob.glob(path))
            if hits:
                return hits[-1]           # the newest app-<version> folder
        elif _os.path.isabs(path):
            if _os.path.exists(path):
                return path
        else:
            found = shutil.which(path)
            if found:
                return found
    return None


def start_app(name, exe=None):
    """Launches an approved app by its fixed name. Raises if it isn't installed."""
    exe = exe or name
    path = resolve_exe(exe)
    if path is None:
        raise FileNotFoundError(exe)
    return subprocess.Popen([path]).pid


def process_alive(pid):
    import psutil
    return psutil.pid_exists(pid)


def open_url(url):
    return webbrowser.open(url)


def open_file(path):
    if not IS_WIN:
        raise OSError("opening files needs Windows")
    import os
    os.startfile(path)


def internet_up():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return True
    except OSError:
        return False


def set_volume_percent(percent):
    """Exact volume needs the optional pycaw package. Returns (ok, message)."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return False, "Setting an exact volume needs the pycaw package. Use 'volume up' or 'volume down' for now."
    speakers = AudioUtilities.GetSpeakers()
    # pycaw 2023+ returns an AudioDevice that already exposes the endpoint.
    # Older versions return a raw IMMDevice, which has to be activated first.
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is None:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = cast(interface, POINTER(IAudioEndpointVolume))
    endpoint.SetMasterVolumeLevelScalar(percent / 100.0, None)
    return True, ""
