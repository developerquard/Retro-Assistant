"""Retro's own pointer: visible on screen, and never the user's mouse.

The name of the game is that the assistant gets a hand of its own. So this module
holds a position - in the physical screen pixels that screenshots, window
rectangles and GetCursorPos all share - and moves *that* about. The user's cursor is
untouched by construction:

* nothing here calls SetCursorPos, and pyautogui's move/click functions are never
  used, because every one of them drives the real mouse;
* the click is delivered as window messages (WM_MOUSEMOVE, WM_LBUTTONDOWN,
  WM_LBUTTONUP) posted to the window under the point, which the target application
  handles as if the mouse had been there, while the physical mouse never leaves
  wherever the user left it;
* every move and click is announced through core.ui_hook, and the overlay in
  ui/pointer.py draws the marker - so the pointer is something the user can watch,
  which is what makes an assistant's decisions feel deliberate rather than magic.

Two honest caveats, both reported rather than hidden: a posted click is a message,
not a real input event, so an application is free to ignore it (browsers do, for
page content, which is why UI Automation remains the first route inside a window);
and whether the app reacted is checked by comparing the pixels near the point
before and after - evidence, not an assumption.
"""
import threading
import time

from . import screen, ui_hook
from .model import Result

_lock = threading.Lock()
_pos = None                     # (x, y) physical pixels, where the marker sits


def where():
    """The last place the virtual pointer was put, or None."""
    with _lock:
        return _pos


def _announce(kind, x, y):
    """Tell the window where the pointer is. Never raises into an action."""
    ui_hook.emit(("pointer", kind, int(x), int(y)))


def bounds():
    """The screen the pointer can move on, or None."""
    return screen.size()


def place(x, y):
    """Moves Retro's pointer. Returns a Result; the user's cursor is not touched."""
    global _pos
    area = bounds()
    if area is None:
        return Result(False, "I can't see the screen to point at it.")
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return Result(False, "A point needs two numbers.")
    width, height = area
    if not (0 <= x < width and 0 <= y < height):
        return Result(False, f"({x}, {y}) is off screen - it is {width} by {height}.")
    with _lock:
        _pos = (x, y)
    _announce("move", x, y)
    return Result(True, f"Retro's pointer is at ({x}, {y}) - your own mouse is untouched.",
                  verified=True)


def hide():
    """Fades the marker out; the last position stays known."""
    _announce("hide", *(where() or (0, 0)))
    return Result(True, "Pointer hidden.")


# ---------- the click -------------------------------------------------------

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def _lparam(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _window_at(x, y):
    """(hwnd, root_hwnd, title) for the window under a physical point, or None."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    point = wintypes.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(point)
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, 2) or hwnd          # GA_ROOT
    length = user32.GetWindowTextLengthW(root)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(root, buffer, length + 1)
    title = " ".join(buffer.value.split())
    return hwnd, root, title


def window_at(x, y):
    """The window under a point, for the rule that decides whether a click is allowed."""
    hit = _window_at(x, y)
    if hit is None:
        return None
    hwnd, root, title = hit
    return root, title


def _sample(x, y, reach=110):
    """Two thin bands above and below a point, as raw pixels.

    The bands are away from the middle on purpose: the marker Retro draws sits at
    the point itself, so comparing the centre would see its own pointer and call it
    a reaction.
    """
    area = bounds()
    if area is None:
        return None
    width, height = area
    band = 48
    left = max(0, min(width - 2, int(x) - reach))
    right = max(left + 1, min(width, int(x) + reach))
    top = max(0, int(y) - reach)
    bottom = min(height, int(y) + reach)
    pieces = []
    for region in ((left, top, right - left, band), (left, max(0, bottom - band),
                                                     right - left, band)):
        left2, top2, width2, height2 = region
        if width2 <= 0 or height2 <= 0:
            continue
        path, _size, problem = screen.shot("click-check", region=region)
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                pieces.append(handle.read())
            path.unlink(missing_ok=True)
        except OSError:
            return None
    return b"|".join(pieces) if pieces else None


def click(x, y, settle=0.35, verify=True):
    """Clicks at a physical point with Retro's pointer, not the user's mouse.

    The marker is moved first and given a moment to be seen, because the point of
    the pointer is that the owner can watch where the decision went. The click is
    then posted to the window under the point; the user's cursor position is read
    before and after so the message can say truthfully what did not move.
    """
    global _pos
    moved = place(x, y)
    if not moved.ok:
        return moved
    x, y = int(x), int(y)
    hit = _window_at(x, y)
    if hit is None:
        return Result(False, f"Nothing is under ({x}, {y}) to click.")
    hwnd, root, title = hit
    if settle:
        time.sleep(float(settle))            # let the marker arrive before the click
    before_pixels = _sample(x, y) if verify else None
    before_cursor = screen.cursor()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    point = wintypes.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    client = (point.x, point.y)
    try:
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, _lparam(*client))
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _lparam(*client))
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, _lparam(*client))
    except Exception as exc:
        return Result(False, f"I couldn't send the click ({type(exc).__name__}).")
    _announce("click", x, y)
    time.sleep(0.35)
    after_pixels = _sample(x, y) if verify else None
    after_cursor = screen.cursor()
    where_named = f"'{title}'" if title else f"window {root}"
    changed = bool(before_pixels and after_pixels and before_pixels != after_pixels)
    readable = bool(before_cursor and after_cursor)
    stayed = readable and before_cursor == after_cursor
    if stayed:
        mouse = "your own mouse never moved"
    elif readable:
        mouse = "your own mouse also moved in the meantime"
    else:
        mouse = "your mouse position could not be read"
    if changed and before_pixels is not None and after_pixels is not None:
        return Result(True, f"Clicked at ({x}, {y}) in {where_named} with Retro's pointer, "
                            f"and the screen changed there - {mouse}.", verified=True)
    if verify and before_pixels is not None and after_pixels is not None:
        return Result(True, f"Clicked at ({x}, {y}) in {where_named} with Retro's pointer "
                            f"({mouse}), but nothing on screen changed there - this app may "
                            f"ignore a click that did not come from the real mouse.",
                      verified=False)
    return Result(True, f"Clicked at ({x}, {y}) in {where_named} with Retro's pointer "
                        f"({mouse}).", verified=False)
