"""The assistant's eyes: screenshots, and finding a picture in what is on screen.

PyAutoGUI is the engine underneath - screenshots through pyscreeze and Pillow,
template matching through OpenCV when a confidence is asked for. Everything here is
read-only: taking a picture changes nothing on the machine, which is why the tools
built on it are allowed even in chat mode.

**Coordinates are physical screen pixels.** That is the space pyautogui.size(),
GetCursorPos, Win32 window rectangles and UI Automation all share, and it is what
the virtual pointer stores. On a scaled display - this machine runs 125% - the
logical pixels Qt draws in are smaller by exactly the screen's device pixel ratio
(1920 physical against 1536 logical here), and the one place that conversion
happens is the overlay in ui/pointer.py.

A screenshot is written into the workspace and its path returned, so the file is a
real artifact: the model can ask for it again, and the user can open it. The module
never raises for a missing dependency or a missing template - it answers with an
honest sentence, the same way the rest of the tools do.
"""
import os
import time

from . import files

# Where the pictures live. Inside the workspace, like every other file Retro keeps.
SHOTS = files.WORKSPACE / "Shots"


class ScreenError(RuntimeError):
    """Raised only where a caller asked for something impossible to answer."""


def _pyautogui():
    """The library, imported lazily so a machine without it still runs the rest."""
    try:
        import pyautogui
    except Exception:                     # pragma: no cover - depends on the machine
        return None
    return pyautogui


def available():
    return _pyautogui() is not None


def size():
    """(width, height) of the primary screen in physical pixels, or None."""
    gui = _pyautogui()
    if gui is None:
        return None
    try:
        width, height = gui.size()
        return int(width), int(height)
    except Exception:
        return None


def shots_dir():
    SHOTS.mkdir(parents=True, exist_ok=True)
    return SHOTS


def _safe_name(name, suffix=".png"):
    cleaned = " ".join(str(name or "screen").split())
    for bad in '<>:"/\\|?*':
        cleaned = cleaned.replace(bad, "_")
    cleaned = cleaned.strip(". ") or "screen"
    if not cleaned.lower().endswith(suffix):
        cleaned += suffix
    return cleaned[:80]


def shot(name="screen", region=None):
    """Takes a screenshot. Returns (path, (width, height), note).

    `region` is (left, top, width, height) in physical pixels and is how a template
    is cut out of the screen for a later find: take a picture of the button once,
    match it whenever it is needed again.
    """
    gui = _pyautogui()
    if gui is None:
        return None, None, "the screen-capture library is not installed"
    try:
        if region:
            left, top, width, height = (int(v) for v in region)
            image = gui.screenshot(region=(left, top, max(1, width), max(1, height)))
        else:
            image = gui.screenshot()
    except Exception as exc:
        return None, None, f"the screen could not be captured ({type(exc).__name__})"
    path = shots_dir() / _safe_name(f"{name} {time.strftime('%H%M%S')}")
    try:
        image.save(str(path))
    except Exception as exc:
        return None, None, f"the picture could not be saved ({type(exc).__name__})"
    return path, (image.width, image.height), ""


def resolve(name):
    """A template path from a name the user or the model gave.

    A bare name looks in the Shots folder; a relative path looks in the workspace.
    Nothing outside the workspace is ever read, the same rule the file tools use.
    """
    text = str(name or "").strip()
    if not text:
        return None, "I need the picture to look for."
    templates = ([os.path.abspath(text)] if os.path.isabs(text)
                 else [os.path.abspath(str(SHOTS / text)),
                       os.path.abspath(str(files.WORKSPACE / text))])
    workspace = os.path.abspath(str(files.WORKSPACE)).lower()
    for candidate in templates:
        if not candidate.lower().startswith(workspace):
            return None, "that picture is outside the Retro workspace, so I won't read it."
        for path in (candidate, candidate + ".png"):
            if os.path.isfile(path):
                try:
                    from PIL import Image
                    with Image.open(path) as image:
                        image.size
                except Exception:
                    return None, f"'{os.path.basename(path)}' is not a readable picture."
                return path, ""
    wanted = os.path.basename(templates[0])
    return None, (f"'{wanted}' is not in the workspace - take a screenshot first and "
                  f"the Shots folder it lands in becomes the template to match.")


def find(template, confidence=0.9, gray=True):
    """Looks for a picture on screen. Returns ((x, y) or None, note).

    The confidence option needs OpenCV; without it the match is exact and the note
    says so rather than silently pretending that a fuzzy match happened.
    """
    gui = _pyautogui()
    if gui is None:
        return None, "the screen-capture library is not installed"
    path, problem = resolve(template)
    if path is None:
        return None, problem
    if not size():
        return None, "the screen size could not be read"
    try:
        confidence = max(0.3, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.9
    note = ""
    try:
        point = gui.locateCenterOnScreen(path, confidence=confidence, grayscale=gray)
    except Exception as exc:
        name = type(exc).__name__
        if "ImageNotFound" in name:
            return None, f"I looked but couldn't see '{os.path.basename(path)}' on screen."
        if "NotImplementedError" in name or "cv" in str(exc).lower():
            # No OpenCV: an exact match is still a real answer, so try that and say so.
            try:
                point = gui.locateCenterOnScreen(path, grayscale=gray)
                note = " (exact match - the fuzzy-search library is not installed)"
            except Exception as inner:
                if "ImageNotFound" in type(inner).__name__:
                    return None, (f"I looked but couldn't see "
                                  f"'{os.path.basename(path)}' on screen.")
                return None, f"the search failed ({type(inner).__name__})"
        else:
            return None, f"the search failed ({name})"
    except Exception:
        # Anything that slips through (including pyscreeze internal errors) is
        # an honest failure, not a wrong match.
        return None, "the search failed"
    if point is None:
        return None, f"I looked but couldn't see '{os.path.basename(path)}' on screen."
    x, y = int(point[0]), int(point[1])
    return (x, y), note


def cursor():
    """Where the user's own mouse is, in physical pixels, or None."""
    import ctypes
    from ctypes import wintypes

    try:
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception:
        return None
