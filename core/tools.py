"""Retro's hands: the things it can actually do to this computer.

This is the whole surface the model is allowed to touch. It is a small set of
*capabilities* - open an application, bring a window to the front, type, press
keys, click a control by its label, read what is on screen, look in a folder,
wait - rather than a list of commands. The model decides which of them answer a
request; it cannot invent a new one, and no word it produces ever becomes a
command line. Every tool takes named arguments that are validated here first.

The old actions were fixed: seven programs known by name, one command per
sentence. That is why "it has no such script" was a real answer. There is no
script now - Retro asks Windows what is installed, looks at the windows that are
open, and drives the application the user names.

Reading the screen and clicking a control use UI Automation, which is the same
accessibility tree a screen reader uses. That is what makes "open ChatGPT, type
this and send it" possible without teaching Retro anything about ChatGPT: find
the control whose label matches, click it, type into it, press the key that
submits.

Nothing here blocks for long: `wait` is measured in seconds and checks the task
between slices, and every call returns a Result whose message is *observed* - the
window that came to the front, the control that was clicked, the files that are
there - so the planner can see what happened instead of assuming.
"""
import ctypes
import os
import re
import sys
import time
from collections import namedtuple

from . import apps as app_index
from . import config, os_ops
from .model import Result

IS_WIN = sys.platform == "win32"

# ---------- Windows, without a toolkit --------------------------------------

if IS_WIN:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
else:                                             # keeps the module importable off Windows
    _user32 = _kernel32 = None
    _EnumProc = None


if IS_WIN:
    # Every argument type is declared. Without these, ctypes guesses, and guessing
    # cost a real failure: a window handle was passed as a signed int, Windows
    # rejected the call, and "bring Spotify to the front" came back as ArgumentError.
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.EnumWindows.argtypes = [_EnumProc, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
    _user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    _user32.BringWindowToTop.restype = ctypes.c_bool
    _user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    _user32.AttachThreadInput.restype = ctypes.c_bool
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    _user32.SendInput.restype = ctypes.c_uint
    _user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    _user32.VkKeyScanW.restype = ctypes.c_short
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

SW_RESTORE = 9
SW_SHOW = 5
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1
EXTENDED_KEYS = {"up", "down", "left", "right", "home", "end", "pageup", "pagedown",
                 "insert", "delete", "del"}
VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
      "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
      "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D,
      "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "slash": 0xBF}
VK.update({f"f{n}": 0x6F + n for n in range(1, 13)})


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send_key(vk, up=False, scan=0, unicode_char=False):
    """One key event through SendInput, which is what a real keyboard does."""
    flags = (KEYEVENTF_KEYUP if up else 0)
    if unicode_char:
        flags |= KEYEVENTF_UNICODE
    elif vk in (0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E):
        flags |= KEYEVENTF_EXTENDEDKEY
    item = _INPUT(type=INPUT_KEYBOARD)
    item.u.ki = _KEYBDINPUT(wVk=0 if unicode_char else vk, wScan=scan, dwFlags=flags,
                            time=0, dwExtraInfo=None)
    sent = _user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
    return sent == 1


# Applications this run started. A window owned by one of these is the one the
# user just asked for - not the copy of Notepad that is already open with their
# own work in it, which is what a loose title match found during testing.
LAUNCHED = set()


def remember_launched(pid):
    if pid:
        LAUNCHED.add(int(pid))
    return pid


def _pid_of(hwnd):
    if not IS_WIN or not hwnd:
        return 0
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_titles(limit=60):
    """(handle, title) for every visible window that has a title, front-most first."""
    if not IS_WIN:
        return []
    found = []

    def visit(hwnd, _param):
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = " ".join(buffer.value.split())
                # Retro's own marker is a window, but never a window to work in.
                if title and not title.lower().startswith("retro pointer"):
                    found.append((hwnd, title))
        return len(found) < limit

    _user32.EnumWindows(_EnumProc(visit), None)
    return found[:limit]


def _title_of(hwnd):
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return " ".join(buffer.value.split())


def foreground_title():
    """The title of the window the user is looking at, or ''."""
    if not IS_WIN:
        return ""
    hwnd = _user32.GetForegroundWindow()
    return _title_of(hwnd) if hwnd else ""


def find_window(fragment):
    """(handle, title) for the best visible window matching `fragment`.

    Best, not first, and the difference matters: asking for "notepad" while a
    Notepad window with the user's own document is already open used to return
    that window, and the next step typed into it. A window belonging to a program
    this run started wins; after that a title that *starts* with what was asked
    beats one that merely contains it; only then does Z-order decide.
    """
    wanted = " ".join(str(fragment).lower().split())
    if not wanted:
        return None, ""
    best, best_score = None, -1
    for hwnd, title in window_titles(limit=200):
        low = title.lower()
        if wanted not in low:
            continue
        score = 1
        if _pid_of(hwnd) in LAUNCHED:
            score += 3
        if low.startswith(wanted):
            score += 2
        stem = low.split("-")[0].strip()
        if stem == wanted:
            score += 2
        if score > best_score:
            best, best_score = (hwnd, title), score
    if best is None:
        return None, ""
    return best


def bring_to_front(hwnd):
    """Restores and activates a window, including when Windows refuses the first try.

    Windows only lets the foreground process steal focus, so the usual trick is
    used here: attach to the thread that owns the current foreground window, ask
    for the focus, then detach again.
    """
    if not IS_WIN or not hwnd:
        return False
    _user32.ShowWindow(hwnd, SW_RESTORE)
    if _user32.SetForegroundWindow(hwnd):
        return True
    current = _kernel32.GetCurrentThreadId()
    target = _user32.GetWindowThreadProcessId(hwnd, None)
    front = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None)
    attached = []
    try:
        for other in (target, front):
            if other and other != current:
                _user32.AttachThreadInput(current, other, True)
                attached.append(other)
        _user32.BringWindowToTop(hwnd)
        ok = bool(_user32.SetForegroundWindow(hwnd))
    finally:
        for other in attached:
            _user32.AttachThreadInput(current, other, False)
    return ok


def type_text(text):
    """Types `text` into whatever window has the focus, as unicode keys.

    Unicode key events are what let Hindi and Hinglish text reach an application;
    the old way, key codes per character, could only produce what the keyboard
    layout has.
    """
    if not IS_WIN:
        raise OSError("typing needs Windows")
    sent = 0
    for character in str(text):
        unit = ord(character)
        if unit > 0xFFFF:                     # outside the basic plane: send both halves
            unit -= 0x10000
            pair = (0xD800 + (unit >> 10), 0xDC00 + (unit & 0x3FF))
        else:
            pair = (unit,)
        for code in pair:
            if _send_key(0, up=False, scan=code, unicode_char=True):
                sent += 1
            _send_key(0, up=True, scan=code, unicode_char=True)
    return sent


def parse_keys(combo):
    """'ctrl+l' -> ['ctrl', 'l']. Raises ValueError for a key Retro won't press."""
    parts = [p for p in str(combo).replace("+", " ").split() if p]
    if not parts:
        raise ValueError("no keys given")
    keys = []
    for part in parts:
        low = part.lower()
        if low in VK:
            keys.append(low)
        elif len(part) == 1 and part.isprintable():
            keys.append(part)
        else:
            raise ValueError(f"unknown key {part!r}")
    return keys


def _vk_for(key):
    """The virtual-key code for one key.

    `VkKeyScanW` takes the character itself, not its code point, and it is declared
    that way above - so calling it with `ord(key)` raised a ctypes ArgumentError and
    every *letter* combination failed on this machine: ctrl+l never reached a search
    box and ctrl+s never saved anything, while enter, tab and the function keys (all
    in VK) worked. That is why a plan could open an app and then do nothing in it.
    """
    if key in VK:
        return VK[key]
    if not IS_WIN:
        raise ValueError(f"cannot press {key!r}")
    code = _user32.VkKeyScanW(str(key)[:1])
    if code == -1:
        raise ValueError(f"cannot press {key!r}")
    return code & 0xFF


def press_keys(combo):
    """Presses a key or a combination such as 'ctrl+l' or 'enter'."""
    if not IS_WIN:
        raise OSError("key presses need Windows")
    keys = parse_keys(combo)
    codes = [_vk_for(k) for k in keys]
    for code in codes:
        _send_key(code, up=False)
    for code in reversed(codes):
        _send_key(code, up=True)
    return keys


# ---------- the screen reader -----------------------------------------------

CLICKABLE = ("Button", "Hyperlink", "MenuItem", "ListItem", "TabItem", "CheckBox",
             "RadioButton", "SplitButton", "Edit", "ComboBox", "TreeItem", "Text")


def _uia():
    """UI Automation, or None where it cannot be used.

    Imported on demand because it is only needed for reading the screen and
    clicking; a machine without it can still open applications and type.
    """
    try:
        import uiautomation
        return uiautomation
    except Exception:
        return None


def _in_uia():
    """COM has to be initialised on whichever thread uses UI Automation."""
    auto = _uia()
    return auto.UIAutomationInitializerInThread() if auto else None


def _front_control(auto):
    hwnd = _user32.GetForegroundWindow() if IS_WIN else None
    if not hwnd:
        return None
    try:
        return auto.ControlFromHandle(hwnd)
    except Exception:
        return None


def read_screen(limit=60):
    """A plain text sketch of the window in front: label and kind, top to bottom.

    This is how Retro checks its own work. It is the accessibility tree, so it
    sees the same buttons a screen reader would, and nothing at all in windows
    that expose nothing.
    """
    auto = _uia()
    if auto is None:
        return Result(False, "I can't read the screen on this machine.")
    lines = []
    title = ""
    try:
        with _in_uia():
            control = _front_control(auto)
            if control is None:
                return Result(False, "I couldn't see a window in front.")
            title = control.Name or foreground_title()
            stack = [(control, 0)]
            while stack and len(lines) < limit:
                node, depth = stack.pop(0)
                try:
                    children = node.GetChildren()
                except Exception:
                    children = []
                if depth >= 4:
                    children = []
                for child in children:
                    try:
                        stack.append((child, depth + 1))
                    except Exception:
                        pass
                if node is control:
                    continue
                try:
                    name = (node.Name or "").strip()
                    kind = node.ControlTypeName or "Control"
                except Exception:
                    continue
                if name:
                    lines.append(f"{kind.replace('Control', '')}: {name}")
    except Exception as exc:
        return Result(False, f"I couldn't read that window ({type(exc).__name__}).")
    if not lines:
        return Result(True, f"'{title}' shows no readable controls.", verified=True)
    return Result(True, f"Window '{title}': " + " | ".join(lines), verified=True)


def screen_labels(limit=70):
    """(window title, labels) for the window in front.

    The labels are the accessibility names, and for editable fields the text inside
    them as well - that is what makes verification possible: after typing, the text
    that was typed can be read back out of the control it went into.
    """
    auto = _uia()
    if auto is None:
        return "", []
    labels = []
    title = ""
    try:
        with _in_uia():
            control = _front_control(auto)
            if control is None:
                return "", []
            title = control.Name or foreground_title()
            stack = [(control, 0)]
            while stack and len(labels) < limit:
                node, depth = stack.pop(0)
                try:
                    children = node.GetChildren()
                except Exception:
                    children = []
                if depth >= 5:
                    children = []
                for child in children:
                    stack.append((child, depth + 1))
                if node is control:
                    continue
                try:
                    name = (node.Name or "").strip()
                except Exception:
                    name = ""
                if name and name not in labels:
                    labels.append(name)
                value = ""
                try:
                    pattern = node.GetValuePattern()
                    if pattern is not None:
                        value = (pattern.Value or "").strip()
                except Exception:
                    value = ""
                if value and value not in labels and len(value) < 400:
                    labels.append(value)
    except Exception:
        pass          # a window that will not be read is reported by its labels, or none
    return title, labels


def screenshot(name="screen", left=0, top=0, width=0, height=0):
    """Takes a picture of the screen - or one region - and saves it in the workspace.

    This is Retro's eyes where the accessibility tree has nothing to give: a canvas
    in a browser, a picture, a game. The file lands in RetroWorkspace/Shots, so the
    same picture can be asked for again and used as the template for find_on_screen,
    which is what turns "look at this button" into something a later step can find.
    """
    from . import screen as eyes
    region = (left, top, width, height) if width > 0 and height > 0 else None
    path, size, problem = eyes.shot(name, region=region)
    if path is None:
        return Result(False, f"I couldn't take the picture: {problem}.")
    what = (f"the region at ({left}, {top}), {width} by {height}" if region
            else "the whole screen")
    return Result(True, f"Saved {what} as {path} - {size[0]} by {size[1]} pixels.",
                  verified=True)


def find_on_screen(image, confidence=90):
    """Looks for a saved picture on screen. Returns its centre, or says it isn't there."""
    from . import screen as eyes
    point, note = eyes.find(image, confidence=confidence / 100.0)
    if point is None:
        return Result(False, note)
    return Result(True, f"I can see it at ({point[0]}, {point[1]}).{note}", verified=True)


def _point_allowed(root, title, named=""):
    """Whether Retro may click at a point: the same rule typing has.

    The point has to be over a window this session started, a window the request
    already aimed at, or the window this very step named - and when a window is
    named, the point has to be over *that* one. Without this, "click at 900, 500"
    would happily click whatever the user happens to have open.
    """
    low = " ".join(str(title or "").split()).lower()
    if not low:
        return Result(False, "The window under that point has no name I can check it "
                             f"against (window {root}).")
    wanted = " ".join(str(named or "").split()).lower()
    if wanted:
        if wanted in low or any(word in low for word in wanted.split() if len(word) > 2):
            return None
        return Result(False, f"({low}) is under that point, not '{named}' - I won't click "
                             f"there.")
    if any(target in low for target in AIMED):
        return None
    if _pid_of(root) in LAUNCHED:
        return None
    return Result(False, f"I haven't been told which window to click in. Aim it with "
                         f"focus_window first, or name it in the step - '{title}' may be "
                         f"your own work.")


def click_at(x, y, window=""):
    """Clicks a screen point with Retro's pointer. The user's mouse never moves."""
    from . import pointer
    hit = pointer.window_at(x, y)
    if hit is None:
        return Result(False, f"Nothing is under ({x}, {y}) to click.")
    root, title = hit
    refused = _point_allowed(root, title, window)
    if refused is not None:
        return refused
    return pointer.click(x, y)


def point_at(x, y):
    """Moves Retro's own pointer so the next step can be watched happening."""
    from . import pointer
    return pointer.place(x, y)


def _labels_in(control, limit=60):
    """The name of every control under `control`, breadth first and deduplicated."""
    names, stack = [], [(control, 0)]
    while stack and len(names) < limit:
        node, depth = stack.pop(0)
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        if depth >= 4:
            children = []
        for child in children:
            stack.append((child, depth + 1))
        if node is control:
            continue
        try:
            name = (node.Name or "").strip()
        except Exception:
            continue
        if name and name not in names:
            names.append(name)
    return names


def _labels_hint(window, wanted):
    """The labels that are really in the window, so a retry can use a real one.

    This exists because of what a small model kept doing: for "play the song
    sunflower" it planned `click_text | song`, searched for a control labelled
    "song", and got only "I couldn't find 'song'". Told which labels ARE there,
    the next attempt can pick the search box instead of a word from the request.
    """
    names = _labels_in(window)
    if not names:
        return ""
    words = [w for w in re.split(r"\W+", str(wanted).lower()) if len(w) > 2]
    near = [n for n in names if any(word in n.lower() for word in words)]
    shown = (near or names)[:8]
    return " Labels in that window include: " + ", ".join(shown) + "."


def _point_marker_at(control):
    """Shows Retro's pointer at a control just before clicking it.

    UI Automation clicks never move a mouse, which used to make the click invisible:
    the user saw a button change with nothing leading up to it. This puts the marker
    where the click is about to go, and never gets in the way of the click itself.
    """
    try:
        rect = control.BoundingRectangle
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return
    if right <= left or bottom <= top:
        return
    try:
        from . import pointer
        pointer.place((left + right) // 2, (top + bottom) // 2)
    except Exception:
        pass


def click_text(label, kind=None):
    """Clicks the control in the front window whose label matches `label`."""
    auto = _uia()
    if auto is None:
        return Result(False, "I can't click controls on this machine.")
    wanted = " ".join(str(label).split())
    if not wanted:
        return Result(False, "I need a label to click.")
    blocked = _unaimed("click")
    if blocked is not None:
        return blocked
    try:
        with _in_uia():
            window = _front_control(auto)
            if window is None:
                return Result(False, "I couldn't see a window in front.")
            control = window.Control(searchDepth=12, SubName=wanted, timeout=1.5)
            if control is None or not getattr(control, "Exists", lambda: False)():
                control = window.Control(searchDepth=12, SubName=wanted, timeout=0.3,
                                         ControlType=kind) if kind else None
            if control is None or not getattr(control, "Exists", lambda: False)():
                return Result(False, f"I couldn't find '{wanted}' in '{window.Name}'."
                                     + _labels_hint(window, wanted))
            clicked_kind = (control.ControlTypeName or "").replace("Control", "")
            label_text = control.Name or wanted
            _point_marker_at(control)          # show where this click goes
            control.Click(simulateMove=False, waitTime=0.2)
    except Exception as exc:
        return Result(False, f"I couldn't click '{wanted}' ({type(exc).__name__}).")
    return Result(True, f"Clicked {clicked_kind} '{label_text}'.", verified=clicked_kind in CLICKABLE)


# ---------- files and folders ----------------------------------------------

FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos",
           "workspace")

# The official ids of the user's folders. Windows moves Downloads to another drive
# often enough that guessing %USERPROFILE% is not good enough - on the machine this
# was written on it lives on D:, and "go to my downloads" opened an empty folder.
_FOLDER_IDS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                ("d", ctypes.c_ubyte * 8)]


def _known_folder(folder_id):
    """Where Windows says a user folder is, or None."""
    if not IS_WIN:
        return None
    import uuid
    guid = _GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    pointer = ctypes.c_wchar_p()
    try:
        code = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                         ctypes.byref(pointer))
    except Exception:
        return None
    if code != 0 or not pointer.value:
        return None
    path = pointer.value
    try:
        ctypes.windll.ole32.CoTaskMemFree(pointer)
    except Exception:
        pass
    return path


def folder_path(name):
    """The real path of a well known folder, as the shell reports it."""
    key = " ".join(str(name).lower().split()).strip("/\\")
    if key in ("workspace", "retroworkspace", "retro workspace", "my workspace"):
        return config.WORKSPACE
    if key not in FOLDERS:
        words = key.split()
        for candidate in FOLDERS:
            if not key:
                break
            if candidate in words or candidate in key or candidate.rstrip("s") in key:
                key = candidate
                break
        else:
            return None
    if key not in FOLDERS:
        return None
    if key == "workspace":
        return config.WORKSPACE
    real = _known_folder(_FOLDER_IDS[key])
    if real:
        return real
    user = os.environ.get("USERPROFILE", str(os.path.expanduser("~")))
    return os.path.join(user, key.capitalize())


def list_folder(name, sort="recent", limit=10):
    """The files in a well known folder, newest first by default."""
    path = folder_path(name)
    if path is None or not os.path.isdir(path):
        return Result(False, f"I don't know a folder called '{name}'.")
    entries = []
    for base, _dirs, files in os.walk(path):
        for filename in files:
            full = os.path.join(base, filename)
            try:
                info = os.stat(full)
            except OSError:
                continue
            entries.append((info.st_mtime, filename, info.st_size, full))
        break                                     # one level: a Downloads folder is not a tree
    if not entries:
        return Result(True, f"'{os.path.basename(path)}' is empty.", verified=True)
    if str(sort).lower().startswith("name"):
        entries.sort(key=lambda row: row[1].lower())
    elif str(sort).lower().startswith("size"):
        entries.sort(key=lambda row: -row[2])
    else:
        entries.sort(key=lambda row: -row[0])
    limit = max(1, min(int(limit), 25))
    shown = entries[:limit]
    described = ", ".join(
        f"{name} ({size / 1024:.0f} KB, {time.strftime('%d %b %H:%M', time.localtime(mtime))})"
        for mtime, name, size, _full in shown)
    return Result(True, f"{os.path.basename(path)} ({len(entries)} files): {described}.",
                  verified=True)


def open_folder(name):
    path = folder_path(name)
    if path is None or not os.path.isdir(path):
        return Result(False, f"I don't know a folder called '{name}'.")
    if not IS_WIN:
        return Result(False, "Opening folders needs Windows.")
    os.startfile(path)
    return Result(True, f"Opened {os.path.basename(path)}.", verified=True)


def my_documents(limit=8):
    """The documents Retro has written, newest first, with their real word counts.

    Written documents used to be findable only by remembering the file name. This is
    the answer to "what have you written for me": every markdown document in the
    workspace, counted from the text that is really in it, and the folder they are in.
    """
    from . import compose
    found = compose.documents(limit)
    if not found:
        return Result(True, "I haven't written any documents yet - ask me for one.",
                      verified=True)
    described = ", ".join(
        f"{row['name'].rsplit('.', 1)[0]} ({row['words']} words, "
        f"{time.strftime('%d %b %H:%M', time.localtime(row['when']))})"
        for row in found)
    # The folder is read off the files that were really found rather than from a constant:
    # a result that says where something is has to be looking at the thing itself.
    folder = os.path.dirname(found[0]["path"])
    return Result(True, f"{len(found)} document(s), newest first: {described}. "
                        f"They are in {folder}.", verified=True)


def remember_fact(text):
    """Keeps something about the owner, in their own desktop file.

    The one tool that writes outside the workspace - on purpose, and only here: the
    owner asked for what Retro learns about them to live on the desktop, in a file
    they can read and delete. Nothing else in this module may write outside the
    workspace.
    """
    from . import memory
    saved, message = memory.remember(text)
    return Result(saved, message, verified=saved)


def forget_fact(query):
    """Removes a note about the owner - only the ones whose words all match.

    Nothing matching is an answer, not a failure: "I don't have a note about that" is
    what the owner needs to hear, and a red error for it would read as the tool being
    broken. What is *not* claimed is that anything was verified when nothing was
    removed.
    """
    from . import memory
    removed, message = memory.forget(query)
    return Result(True, message, verified=bool(removed))


def recall_facts(query=""):
    """What Retro already knows: the newest notes, or the ones matching `query`."""
    from . import memory
    found = memory.recall(query)
    if not found:
        return Result(True, "I don't have any notes about that yet." if query
                      else "I don't have any notes yet.", verified=True)
    message = f"{len(found)} note(s): " + "; ".join(found[:4])
    if len(message) > 120:                 # this is read out loud as the reply
        message = message[:117].rstrip() + "..."
    return Result(True, message, verified=True)


def wait(seconds, task=None):
    """Waits, in slices, so 'Retro, stop' is heard during the wait."""
    seconds = max(0.1, min(float(seconds), 20.0))
    deadline = time.time() + seconds
    while time.time() < deadline:
        if task is not None:
            task.check()
        time.sleep(0.1)
    return Result(True, f"Waited {seconds:.0f} seconds.", verified=True)


# ---------- opening applications -------------------------------------------

def open_app(name):
    """Opens an application by the name the user said, through core/registry.py.

    The registry knows how each app is really started - an executable, a shortcut,
    a Store AppID or a web address - and how to check that it arrived, so this is a
    lookup rather than a guess at an executable path. Names with no entry still work
    when Windows itself knows the program, because the installed-application index
    is searched too.
    """
    from . import registry
    return registry.open_app(name)


def app_function(app, action, detail="", target="", task=None, confirm=None):
    """Uses an application, through the workflows in core/appfunc.py."""
    from . import appfunc
    return appfunc.run(app, action, detail=detail, target=target, task=task, confirm=confirm)


def set_mode(mode):
    """Switches between chat mode and work mode."""
    from . import runtime
    ok, message = runtime.set_mode(mode)
    return Result(ok, message, verified=True)


def set_followup(state):
    """Turns listening for follow-ups (without saying 'Retro' again) on or off."""
    from . import runtime
    wanted = str(state).lower() in ("on", "true", "yes", "enable", "enabled")
    ok, message = runtime.set_followup(wanted)
    return Result(ok, message, verified=True)


def write_file(path, text):
    """Writes a text file inside the Retro workspace."""
    from . import files
    return files.write_text(path, text)


def read_file(path):
    """Reads a text file back, so a result can be checked."""
    from . import files
    return files.read_text(path)


def run_command(command, timeout=60):
    """Runs one development command in the workspace and captures its output.

    A command is a real risk - it can do anything the user can - so the tool is
    marked sensitive and the confirmation names the command. The working directory
    is always the workspace: a build or a test runs where the project is, and a
    command that tried to wander out of it would still start there.
    """
    import subprocess
    import sys
    line = " ".join(str(command).split())
    if not line:
        return Result(False, "I need a command to run.")
    blocked = FORBIDDEN_COMMANDS.search(line)
    if blocked:
        return Result(False, f"I won't run a command that does '{blocked.group(0)}'.")
    cwd = str(config.WORKSPACE)
    try:
        os.makedirs(cwd, exist_ok=True)
    except OSError:
        cwd = None
    shell = subprocess.run(line, shell=True, cwd=cwd, capture_output=True,
                           timeout=max(2, min(int(timeout), 300)), text=True,
                           creationflags=(0x08000000 if IS_WIN else 0))
    output = ((shell.stdout or "") + (shell.stderr or "")).strip()
    tail = output[-600:] if len(output) > 600 else output
    head = f"exit {shell.returncode}"
    if tail:
        return Result(shell.returncode == 0,
                      f"Command finished ({head}). Output: {tail}",
                      verified=shell.returncode == 0)
    return Result(shell.returncode == 0, f"Command finished ({head}), with no output.",
                  verified=shell.returncode == 0)


def list_tasks():
    """What Retro is working on, and what it recently did."""
    from . import runtime
    entries = runtime.tasks(limit=10)
    if not entries:
        return Result(True, "Nothing is running, and nothing has been run yet this session.",
                      verified=True)
    parts = []
    for index, task in enumerate(entries):
        seconds = task.get("seconds")
        when = f" ({seconds:.0f}s)" if seconds else ""
        parts.append(f"{index + 1}. {task['text'][:60]} - {task['status']}{when}")
    return Result(True, "Tasks: " + "; ".join(parts) + ".", verified=True)


def cancel_task(which=""):
    """Stops the running task, or one from the recent list by number or words."""
    from . import runtime
    ok, message = runtime.cancel_task(str(which).strip())
    return Result(ok, message, verified=ok)


def open_website(url):
    """Opens a web address, or a name Retro can turn into one."""
    raw = " ".join(str(url).split())
    if not raw:
        return Result(False, "I need a site to open.")
    known = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
             "github": "https://github.com", "chatgpt": "https://chatgpt.com",
             "gmail": "https://mail.google.com", "maps": "https://maps.google.com",
             "whatsapp": "https://web.whatsapp.com"}
    target = known.get(raw.lower().rstrip("."), raw)
    if not target.startswith(("http://", "https://")):
        if "." not in target:
            target = "https://www.google.com/search?q=" + target.replace(" ", "+")
        else:
            target = "https://" + target
    opened = bool(os_ops.open_url(target))
    return Result(opened, f"Opened {target}." if opened else "Couldn't open the browser.",
                  verified=False)


def web_search(query):
    import urllib.parse
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(str(query))
    opened = bool(os_ops.open_url(url))
    return Result(opened, f"Searched for '{query}'." if opened else "Couldn't open the browser.",
                  verified=False)


# Commands that are refused outright, whatever the confirmation said. A voice
# assistant running a shell is the one place where a misheard word can be costly,
# so the destructive shapes are not offered at all.
FORBIDDEN_COMMANDS = re.compile(
    r"\b(?:format|diskpart|bcdedit|reg\s+delete|shutdown|logoff|taskkill\s+/f|cipher\s+/w|"
    r"rd\s+/s|del\s+/f\s+/s|rm\s+-rf\s+/|vssadmin|netsh\s+interface|attrib\s+-s|"
    r"takeown|icacls\b|schtasks\s+/delete)\b", re.I)

# ---------- the registry the planner reads ---------------------------------

# `wants_confirm` is for a tool that has to ask about something it only knows from
# its own arguments - sending a message needs the recipient and the text in the
# question, which a generic "proceed?" cannot say. The confirmation rules themselves
# stay in this module.
Tool = namedtuple("Tool", "name tier description args run check wants_confirm",
                  defaults=(False,))


def _args(value, name, max_len=300):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    cleaned = " ".join(value.split())
    if not 1 <= len(cleaned) <= max_len:
        raise ValueError(f"{name} must be 1 to {max_len} characters")
    return cleaned


def _int_arg(value, name, lo, hi):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    return max(lo, min(hi, number))


def validate_args(tool, args):
    """Clean arguments for a tool, or raise ValueError. Unknown keys are dropped.

    Every tool's spec is (kind, ...): text takes a maximum length, int takes its
    bounds, choice takes the allowed values, and any of them may carry a default as
    the last element.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    clean = {}
    for key, spec in tool.args.items():
        kind = spec[0]
        value = args.get(key)
        missing = value is None or value == ""
        if kind == "text":
            if missing:
                if len(spec) > 2:
                    clean[key] = spec[2]
                    continue
                raise ValueError(f"{key} is required")
            clean[key] = _args(str(value), key, spec[1])
        elif kind == "int":
            if missing:
                if len(spec) > 3:
                    clean[key] = spec[3]
                    continue
                raise ValueError(f"{key} is required")
            clean[key] = _int_arg(value, key, spec[1], spec[2])
        elif kind == "choice":
            if missing:
                clean[key] = spec[2] if len(spec) > 2 else spec[1][0]
                continue
            text = str(value).strip().lower()
            if text not in spec[1]:
                raise ValueError(f"{key} must be one of {', '.join(spec[1])}")
            clean[key] = text
    if tool.check is not None:
        tool.check(clean)                     # arguments can be well-formed but unusable
    return clean


TOOLS = {}


def _register(name, tier, description, args, run, check=None, wants_confirm=False):
    TOOLS[name] = Tool(name, tier, description, args, run, check, wants_confirm)
    return TOOLS[name]


_register("open_app", "reversible",
          "Open an installed application by name. Use app_function afterwards to do something in it",
          {"name": ("text", 80)}, lambda a, task=None: open_app(a["name"]))
_register("app_function", "reversible",
          "Use an application: play music or video, send a message, type into a document, "
          "create a project, launch a game, search inside an app",
          {"app": ("text", 40), "action": ("text", 40), "detail": ("text", 600, ""),
           "target": ("text", 200, "")},
          lambda a, task=None, confirm=None: app_function(a["app"], a["action"], a["detail"],
                                                          a["target"], task=task,
                                                          confirm=confirm),
          wants_confirm=True)
_register("write_file", "reversible",
          "Write a text file into the Retro workspace (Projects folder for projects)",
          {"path": ("text", 120), "text": ("text", 4000)},
          lambda a, task=None: write_file(a["path"], a["text"]))
_register("read_file", "read", "Read a text file back from the workspace",
          {"path": ("text", 120)}, lambda a, task=None: read_file(a["path"]))
_register("run_command", "sensitive",
          "Run one development command inside the workspace and read its output",
          {"command": ("text", 300), "timeout": ("int", 2, 300, 60)},
          lambda a, task=None: run_command(a["command"], a["timeout"]))
_register("set_mode", "reversible",
          "Switch between chat mode (talking only) and work mode (runs tools)",
          {"mode": ("choice", ("chat", "work"))},
          lambda a, task=None: set_mode(a["mode"]))
_register("set_followup", "reversible",
          "Turn listening for follow-up commands without the wake word on or off",
          {"state": ("choice", ("on", "off"))},
          lambda a, task=None: set_followup(a["state"]))
_register("list_tasks", "read", "What Retro is working on, and what it recently did",
          {}, lambda a, task=None: list_tasks())
_register("cancel_task", "reversible",
          "Stop the running task, or one from the recent list (by number or by its words)",
          {"which": ("text", 80, "")},
          lambda a, task=None: cancel_task(a["which"]))
_register("open_website", "reversible", "Open a web address or site name",
          {"url": ("text", 200)}, lambda a, task=None: open_website(a["url"]))
_register("web_search", "reversible", "Search the web",
          {"query": ("text", 200)}, lambda a, task=None: web_search(a["query"]))
_register("open_folder", "reversible", "Open a folder: workspace (Retro's own documents and "
          "projects), downloads, documents, desktop, pictures, music, videos",
          {"name": ("text", 40)}, lambda a, task=None: open_folder(a["name"]))
_register("my_documents", "read",
          "The documents Retro has written for the user, newest first, with word counts "
          "(for 'what have you written', 'my documents'). Use open_folder with 'workspace' "
          "to show them in Explorer",
          {"limit": ("int", 1, 20, 8)},
          lambda a, task=None: my_documents(a["limit"]))
_register("recall", "read",
          "What Retro already remembers about the user (preferences, people, dates, "
          "plans - kept in Retro memory.md on their desktop). Ask before answering a "
          "question about them, or when they ask what you know",
          {"query": ("text", 120, "")},
          lambda a, task=None: recall_facts(a["query"]))
_register("remember", "reversible",
          "Remember something the user told you about themselves - a preference, a "
          "person, a plan, a date - as one short line. It is saved to Retro memory.md "
          "on their desktop and shown in your prompt from then on",
          {"text": ("text", 300)},
          lambda a, task=None: remember_fact(a["text"]))
_register("forget", "reversible",
          "Remove a note about the user from Retro memory.md - when they correct "
          "something, or ask you to forget a fact. Only notes whose words all match "
          "are removed, so be specific about which fact",
          {"query": ("text", 160)},
          lambda a, task=None: forget_fact(a["query"]))
_register("list_folder", "read", "List what is in a known folder, newest first",
          {"name": ("text", 40), "sort": ("choice", ("recent", "name", "size")),
           "limit": ("int", 1, 25, 10)},
          lambda a, task=None: list_folder(a["name"], a["sort"], a["limit"]))
_register("list_windows", "read", "Titles of the windows that are open now",
          {}, lambda a, task=None: list_windows())
_register("focus_window", "reversible", "Bring an open window to the front by part of its title",
          {"title": ("text", 80)}, lambda a, task=None: focus_window(a["title"]))
_register("type_text", "reversible", "Type text into the window in front. Focus the window first",
          {"text": ("text", 600)}, lambda a, task=None: type_into(a["text"]))
_register("press_keys", "reversible", "Press a key or combination, such as enter or ctrl+l",
          {"keys": ("text", 40)}, lambda a, task=None: press(a["keys"]),
          check=lambda a: parse_keys(a["keys"]))
_register("click_text", "reversible", "Click a button, link or box by its label in the window in front. Focus the window first",
          {"label": ("text", 80)}, lambda a, task=None: click_text(a["label"]))
_register("read_screen", "read", "Read the labels in the window in front",
          {"limit": ("int", 10, 80, 60)}, lambda a, task=None: read_screen(a["limit"]))
_register("wait", "read", "Wait a moment for a window or page to load",
          {"seconds": ("int", 1, 20, 2)}, lambda a, task=None: wait(a["seconds"], task))
_register("screenshot", "read",
          "Take a picture of the screen (or one region) and save it to the workspace - "
          "Retro's eyes for windows that show no labels",
          {"name": ("text", 60, "screen"), "left": ("int", -8000, 8000, 0),
           "top": ("int", -8000, 8000, 0), "width": ("int", 0, 8000, 0),
           "height": ("int", 0, 8000, 0)},
          lambda a, task=None: screenshot(a["name"], a["left"], a["top"],
                                          a["width"], a["height"]))
_register("find_on_screen", "read",
          "Find a picture from the workspace on screen; returns its centre point to click",
          {"image": ("text", 120), "confidence": ("int", 30, 100, 90)},
          lambda a, task=None: find_on_screen(a["image"], a["confidence"]))
_register("point_at", "read",
          "Move Retro's own pointer (not the user's mouse) to a screen point, so the next "
          "step can be watched happening",
          {"x": ("int", -8000, 8000), "y": ("int", -8000, 8000)},
          lambda a, task=None: point_at(a["x"], a["y"]))
_register("click_at", "reversible",
          "Click a screen point with Retro's own pointer - never the user's mouse. The "
          "window must be aimed with focus_window first, or named in window",
          {"x": ("int", -8000, 8000), "y": ("int", -8000, 8000),
           "window": ("text", 60, "")},
          lambda a, task=None: click_at(a["x"], a["y"], a["window"]))


def list_windows(limit=40):
    titles = [title for _hwnd, title in window_titles(limit=limit + 10)]
    if not titles:
        return Result(False, "I can't list windows on this machine.")
    shown = titles[:limit]
    return Result(True, f"Open windows ({len(titles)}): " + "; ".join(shown) + ".", verified=True)


def focus_window(title, wait_s=6.0):
    """Brings a window to the front, waiting for it to exist first.

    The wait is not a nicety: a program that was just opened takes a few seconds to
    put a window on screen, and the first version of this returned "no open window
    matches" about a program it had started itself two seconds earlier.
    """
    deadline = time.time() + wait_s
    while True:
        hwnd, found = find_window(title)
        if hwnd is not None:
            break
        if time.time() >= deadline:
            return Result(False, f"No open window matches '{title}'.")
        time.sleep(0.4)
    ok = bring_to_front(hwnd)
    time.sleep(0.2)
    front = foreground_title()
    if found.lower() in front.lower():
        aim(front)
        # A title that is about a document rather than the program - "*Ch 3 Notes -
        # Notepad" for "notepad" - is worth pointing out: Windows 11 Notepad keeps
        # the user's other work in the same window, and a step that types into it
        # would be typing into their document.
        warning = ""
        if not found.lower().startswith(str(title).lower()) and " - " in found:
            warning = " (that window is showing a document, so make sure it is the one you meant)"
        return Result(True, f"'{found}' is in front now.{warning}", verified=True)
    return Result(ok, f"Tried to bring '{found}' to the front; '{front}' is in front.",
                  verified=False)


def foreground_is_ours(launched=None):
    """True when the window in front belongs to a program this run started."""
    if not IS_WIN:
        return False
    pids = LAUNCHED if launched is None else launched
    return _pid_of(_user32.GetForegroundWindow()) in pids


# Windows this session deliberately brought to the front. Typing and clicking only
# happen in one of these, or in a program this session started. Without that rule
# there is nothing between "type this" and the user's own work: during testing, a
# plan for Notepad typed into a browser tab that happened to be in front.
AIMED = set()


def aim(title):
    """Records the window the user's request is aimed at."""
    cleaned = " ".join(str(title or "").lower().split())
    if cleaned:
        AIMED.add(cleaned)
    return title


def aimed_at_foreground():
    if foreground_is_ours():
        return True
    front = " ".join(foreground_title().lower().split())
    if not front:
        return False
    return any(target in front for target in AIMED)


def _unaimed(action):
    """A refusal when nothing has said which window to act on, else None."""
    if aimed_at_foreground():
        return None
    return Result(False, f"I haven't been told which window to {action} in - name the window "
                         f"and I'll bring it to the front first.")


def type_into(text):
    if not IS_WIN:
        return Result(False, "Typing needs Windows.")
    blocked = _unaimed("type")
    if blocked is not None:
        return blocked
    try:
        count = type_text(text)
    except Exception as exc:
        return Result(False, f"I couldn't type that ({type(exc).__name__}).")
    if not count:
        return Result(False, "The text didn't reach the window in front.")
    return Result(True, f"Typed {count} characters into '{foreground_title()}'", verified=False)


def press(keys):
    if not IS_WIN:
        return Result(False, "Key presses need Windows.")
    blocked = _unaimed("press keys")
    if blocked is not None:
        return blocked
    try:
        pressed = press_keys(keys)
    except ValueError as exc:
        return Result(False, f"I can't press that: {exc}.")
    except Exception as exc:
        return Result(False, f"That key press failed ({type(exc).__name__}).")
    return Result(True, f"Pressed {'+'.join(pressed)} in '{foreground_title()}'", verified=False)


def _accepts_confirm(fn):
    """True when a tool's own run wants the confirmation callback.

    Checked from the signature rather than by trying the call: a TypeError raised
    *inside* a tool would otherwise look like a tool that does not take the
    argument, and the step would be run a second time without a confirmation.
    """
    import inspect
    try:
        return "confirm" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def run(name, args, task=None, confirm=None, mode=None):
    """Runs one tool by name. Sensitive tools ask first; chat mode refuses.

    The mode gate is here as well as in the planner, because this is the function
    that actually does things: a step that reached the machine by any other route
    still finds the door shut in chat mode unless the tool only reads.
    """
    from . import modes
    tool = TOOLS.get(name)
    if tool is None:
        return Result(False, f"I don't know how to '{name}'.")
    if mode is not None and not modes.allows_tool(mode, tool):
        return Result(False, f"I'm in chat mode, so I didn't {tool.name}. Say 'work mode' "
                             f"and I'll do it.")
    try:
        clean = validate_args(tool, args)
    except ValueError as exc:
        return Result(False, f"Bad arguments for {name}: {exc}.")
    if tool.tier == "sensitive" and confirm is not None:
        if not confirm(f"Retro: {tool.description}. Proceed?"):
            return Result(False, "Cancelled.")
    try:
        if tool.wants_confirm and _accepts_confirm(tool.run):
            return tool.run(clean, task, confirm)
        return tool.run(clean, task)
    except Exception as exc:                     # one failed step must not end the run
        return Result(False, f"{name} failed ({type(exc).__name__}).")


def catalogue(with_tiers=True):
    """The tool list the planner is shown: name, what it does, its arguments.

    The tier is part of the description on purpose. The model is the one choosing
    the step, and it should be the one that knows which steps will ask the user
    first - a plan that ends on a question is fine, a plan that surprises its owner
    is not.
    """
    lines = []
    for tool in TOOLS.values():
        spec = ", ".join(f"{key}: {value[0]}" for key, value in tool.args.items()) or "no arguments"
        mark = f" [{tool.tier}]" if with_tiers else ""
        lines.append(f"- {tool.name} ({spec}){mark}: {tool.description}")
    return "\n".join(lines)
