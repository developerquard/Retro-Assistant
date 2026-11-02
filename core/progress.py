"""Short labels for the words that float around the circle.

The user wanted the floating text to be playful and useful, and - in the same
breath - not to be invented. So every label here is built from an event that really
happened: the tool that is running, the name of the app being opened, the file being
written, the number of steps the plan actually has. There is no timer that makes up
activity, and a label is never a claim about the result - "Opening Spotify" is what
is happening, not "Spotify is playing".

The labels are deliberately short: they are drawn beside a circle, not in a panel,
and anything long enough to need reading twice is too long. Quiet labels are chosen
for work that would otherwise look like nothing at all - a plan, a wait, a check -
because those are exactly the moments the user asked to see something happening.
"""
import random

# One line per tool, with the argument that makes it specific where there is one.
TOOL_LABELS = {
    "open_app": "Opening {name}",
    "app_function": "Using {app}",
    "open_website": "Opening a page",
    "web_search": "Looking that up",
    "open_folder": "Opening {name}",
    "list_folder": "Looking through {name}",
    "list_windows": "Seeing what's open",
    "focus_window": "Bringing {title} up",
    "type_text": "Typing it in",
    "press_keys": "Pressing {keys}",
    "click_text": "Clicking {label}",
    "read_screen": "Checking the screen",
    "wait": "Waiting a moment",
    "write_file": "Writing {path}",
    "read_file": "Reading {path}",
    "run_command": "Running the command",
    "list_tasks": "Checking my tasks",
    "cancel_task": "Stopping that",
    "set_mode": "Switching modes",
    "set_followup": "Changing how I listen",
    "screenshot": "Looking at the screen",
    "find_on_screen": "Looking for it on screen",
    "point_at": "Pointing at {x},{y}",
    "click_at": "Clicking there",
}

# The same idea for app functions, where the action says more than the tool does.
APP_LABELS = {
    "open": "Opening {app}",
    "search_play": "Finding {detail}",
    "ask": "Asking {app}",
    "send_message": "Opening the chat with {target}",
    "write_text": "Writing in {app}",
    "create_project": "Building {target}",
    "launch_game": "Starting the game",
    "navigate": "Opening the page",
    "search": "Searching {app}",
    "settings": "Opening settings",
    "run_test": "Starting the speed test",
    "system": "Reading the system view",
    "play_pause": "Toggling playback",
    "next": "Skipping ahead",
    "previous": "Going back a track",
}

# Said the moment a job that will take real work starts, while the plan is still
# being written. It is deliberately not a claim: it says "starting", never "done",
# and it is the answer to a silence that used to last five to eight seconds.
ACKNOWLEDGING = ("On it.", "Okay, on it.", "Right, on that now.", "Got it - on it.",
                 "On it - give me a moment.")

PLANNING = ("Mapping the mission", "Working out the steps", "Thinking it through")
CHECKING = ("Checking the result", "Seeing how that went", "Verifying that")
FINISHING = ("Wrapping up", "Almost there", "Finishing off")
READING = ("Reading what's there", "Looking closely")


def _pick(pool):
    return random.choice(pool)


def _short(text, limit=34):
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit - 1].rstrip() + "…"


def label_for(tool, args=None, app=None):
    """One short line for one real step. Never raises, never invents.

    An unknown tool still gets a truthful label - it says the tool's own name, which
    is a fact about what is running - rather than a decorative phrase that would
    describe nothing.
    """
    args = args if isinstance(args, dict) else {}
    if tool == "app_function":
        action = str(args.get("action") or "").lower()
        words = {"app": _short(args.get("app"), 18), "detail": _short(args.get("detail"), 24),
                 "target": _short(args.get("target"), 22)}
        template = APP_LABELS.get(action)
        if template:
            try:
                return template.format(**words)
            except (KeyError, IndexError):
                return "Using the app"
        return f"{_short(args.get('app'), 20)}: {action}"[:44]
    template = TOOL_LABELS.get(str(tool))
    if template:
        words = {key: _short(value, 26) for key, value in args.items()}
        try:
            return template.format(**words)
        except (KeyError, IndexError):
            return template.split(" ")[0]
    # A step nobody wrote a template for still gets the tool's own name, and a step
    # with no name at all still gets a true word rather than an empty line to draw.
    return str(tool or "working").replace("_", " ")[:44]


_last_ack = {"text": None}


def acknowledgement():
    """One short line for the start of a job. Never the same one twice in a row."""
    choices = [line for line in ACKNOWLEDGING if line != _last_ack["text"]] or list(ACKNOWLEDGING)
    chosen = random.choice(choices)
    _last_ack["text"] = chosen
    return chosen


def planning_label(steps):
    """The label for a plan that was just written, before the first step runs."""
    if not steps:
        return _pick(PLANNING)
    first = steps[0][0] if isinstance(steps[0], (tuple, list)) else str(steps[0])
    return f"{len(steps)} step{'s' if len(steps) != 1 else ''} - {label_for(first)}"


def checking_label():
    return _pick(CHECKING)


def finishing_label():
    return _pick(FINISHING)
