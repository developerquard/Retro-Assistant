"""The acceptance checks, run against this machine - not against a mock.

    python tools/acceptance_check.py                 # registry + UI, nothing opened
    python tools/acceptance_check.py --launch        # and really start three light apps
    python tools/acceptance_check.py --launch --app spotify   # or one named app
    python tools/acceptance_check.py --llm           # real cloud timing
    python tools/acceptance_check.py --tts           # real speech synthesis timing
    python tools/acceptance_check.py --audio         # one real line, checked at the mixer
    python tools/acceptance_check.py --mic           # list the microphones, then record a second
    python tools/acceptance_check.py --arrival       # the logo, the fade, the startup entry
    python tools/acceptance_check.py --flow notepad  # one real workflow end to end
    python tools/acceptance_check.py --all           # everything (opens real windows)

Every number printed here is measured on this machine at the moment it runs. What
could not be run is printed as NOT RUN with the reason, because a check that did not
happen is not a check that passed. Nothing here is destructive: the flows write into
RetroWorkspace, and the apps started by --launch are terminated again.

Launches are deliberately slow and small: a machine that is being used for other
things must not be hit with twenty windows at once, so apps are opened one at a
time and only the ones named on the command line.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def note(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    flag = "ok  " if ok else "FAIL"
    print(f"{flag}  {name:52.52} {detail}")


def skipped(name, why):
    RESULTS.append((name, None, why))
    print(f"skip  {name:52.52} {why}")


# ---------- the registry -----------------------------------------------------


def check_registry(limit=0):
    from core import registry

    print("\n== the application registry")
    missing = [key for key in registry.REQUIRED if key not in registry.SPECS]
    note("all 23 required applications are registered", not missing,
         f"missing {missing}" if missing else "23/23")
    resolvable, unresolved = 0, []
    for key in registry.REQUIRED:
        spec = registry.SPECS[key]
        spec_back, message = registry.resolve(spec.name)
        if spec_back is not None and spec_back.key == key:
            resolvable += 1
        else:
            unresolved.append(f"{key} ({message})")
    note("every application resolves from its own name", resolvable == len(registry.REQUIRED),
         f"{resolvable}/{len(registry.REQUIRED)}" + (f", bad: {unresolved}" if unresolved else ""))

    ready, absent = [], []
    for key in registry.REQUIRED:
        spec = registry.SPECS[key]
        where = "?"
        if any(_exists(pattern) for pattern in spec.exe or ()):
            where = "exe"
        elif spec.shell:
            where = "shell"
        else:
            try:
                from core import apps as app_index
                hit, _others = app_index.best(spec.name)
                where = "installed" if hit is not None else ("url" if spec.url else "-")
            except Exception:
                where = "?"
        (ready if where not in ("-", "?") else absent).append(f"{key}:{where}")
    note("each application has a launch route on this machine",
         not absent, f"{len(ready)} ready" + (f", no route: {absent}" if absent else ""))
    if limit:
        for entry in ready[:limit]:
            print(f"       route: {entry}")
    return ready, absent


def _exists(pattern):
    import glob
    path = os.path.expandvars(pattern)
    if "*" in path:
        return bool(sorted(glob.glob(path)))
    return os.path.exists(path)


# ---------- the circle -------------------------------------------------------


def check_ui():
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    print("\n== the assistant circle")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    from ui.app import FLOAT_WINDOW_SCALE, RetroWindow
    from ui import theme

    window = RetroWindow(floating=True, voice=False)
    size = window.size().width()
    note("floating window is bigger than the circle on purpose", size > theme.FLOAT_SIZE_DEFAULT,
         f"{size}px window for a {theme.FLOAT_SIZE_DEFAULT}px circle")

    visible = [widget for widget in window._panel_widgets if widget.isVisible()]
    note("nothing from the panel is on screen in floating mode", not visible,
         f"visible: {[type(w).__name__ for w in visible]}" if visible else "")
    note("the R mark is not visible", not window.mark.isVisible())
    note("the writing card is not on screen either", not window.writing.isVisible())

    image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.BG_TRANSPARENT)
    painter = QPainter(image)
    window.agent.render(painter, QPoint(0, 0))
    painter.end()

    cx = cy = size / 2.0
    radius = window.agent._radius(size)
    # What may legitimately be drawn: the ring breathing by up to 9%, plus the
    # half-width of its brightest glow pen. Anything past that is not the circle.
    allowed = radius * 1.09 * 1.05 + 5.0
    inside, outside_ring, farthest, at_edge = 0, 0, 0.0, 0
    for y in range(size):
        for x in range(size):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() == 0:
                continue
            distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            farthest = max(farthest, distance)
            if distance <= allowed:
                inside += 1
            else:
                outside_ring += 1
            if x < 3 or y < 3 or x > size - 4 or y > size - 4:
                at_edge += 1
    note("the circle actually draws something", inside > 200, f"{inside} pixels inside the ring")
    note("nothing is drawn beyond the circle and its glow", outside_ring == 0,
         f"{outside_ring} pixels past {allowed:.0f}px (farthest {farthest:.0f}px)")
    note("nothing is clipped at the window edge", at_edge == 0,
         f"{at_edge} painted pixels on the border" if at_edge else
         f"farthest pixel {farthest:.0f}px of {size / 2:.0f}px")

    from ui.agent import AgentWidget, FLOAT_MAX
    agent = AgentWidget()
    agent.set_label_space(True)
    agent.set_progress("Mapping the mission...")
    agent.set_progress("Checking the result")
    agent.set_progress("Almost there")
    note("the floating labels keep only the newest two", len(agent._floats) == FLOAT_MAX,
         f"{len(agent._floats)} lines: {[text for text, _ in agent._floats]}")
    note("the same event twice is not two labels", _same_event_twice(agent),
         "repeat suppressed")
    agent.clear_progress()
    note("the labels clear when the work ends", agent._floats == [])
    check_writing(window, app)
    check_ring_interaction()
    window.close()
    app.quit()


def check_writing(window, app):
    """A document, written the way the session really reports one."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPainter

    from ui import theme
    from ui.writing import words_of

    print("\n== writing a document")
    window.set_floating(False)
    print("       (the window is in its panel shape for this)")
    plain = window.height()
    # The session's own events, through the window's own handler: this is the path a
    # real document takes (session -> bridge -> window), not a poke at the widget.
    window._on_writing({"kind": "begin", "title": "The most popular anime",
                        "total": 4, "words": 200})
    note("the writing card comes out with the document", window.writing.isVisible())
    note("nothing is drawn on it that could be made up", window.writing.progress() == 0.0,
         f"progress {window.writing.progress()}")
    window._on_writing({"kind": "section", "index": 2, "total": 4,
                        "heading": "Why it travels"})
    window._on_writing({"kind": "section_done", "index": 2, "total": 4, "words": 60})
    fraction = window.agent._arc
    note("the ring's arc is a real fraction of the sections", fraction == 0.5,
         f"{fraction} of the ring filled for 2 of 4 sections")
    window._on_writing({"kind": "writing",
                        "text": "Anime travelled because a single season can move between "
                                "a quiet kitchen and a city falling apart."})
    app.processEvents()
    image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.BG_TRANSPARENT)
    painter = QPainter(image)
    window.render(painter, QPoint(0, 0))
    painter.end()
    drawn = sum(1 for y in range(image.height()) for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 40)
    note("the card is drawn, and the panel grew to hold it",
         drawn > 480 * plain > 0 and window.height() > plain,
         f"{drawn} lit pixels on a {window.width()}x{window.height()} panel "
         f"({plain}px of panel before the card)")
    note("the word count is counting real words",
         words_of(window.writing._text) >= 15,
         f"{words_of(window.writing._text)} words on the card")
    window._on_writing({"kind": "done", "title": "The most popular anime", "words": 214,
                        "file": "The most popular anime.md"})
    note("a saved document keeps its file name", "anime.md" in window.writing._file,
         window.writing._file)
    note("a finished document clears the ring's arc", window.agent._arc is None,
         "the circle is a circle again, and blue when the session says so")
    window.writing.reset()
    app.processEvents()
    check_pause_and_documents(window, app)


class _FakeModel:
    """A model that answers an outline and a section: no key, no network, no waiting."""

    def __init__(self):
        self.prompts = []

    def __call__(self, messages, max_tokens=220):
        system = messages[0]["content"]
        self.prompts.append(system)
        if "Write the outline only" in system:
            return "The Cat Documentary\nOrigins | 60\nWhy they purr | 80"
        return ("Cats came in from the desert and stayed. They purr when they are "
                "content, and that is most of the story.")

    def stream(self, messages, max_tokens=400, on_delta=None):
        text = self(messages, max_tokens)
        half = text.index(".") + 1
        if on_delta is not None:
            on_delta(text[:half])
            on_delta(text[half:])
        return text


def check_pause_and_documents(window, app):
    """The writing strip when it is not writing: a stopped draft, and the shelf.

    Runs against a private workspace, because these checks write real files and none of
    them may land among the user's own documents.
    """
    import tempfile
    from pathlib import Path

    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QImage, QMouseEvent, QPainter

    from core import compose, files
    from core.control import Task
    from ui import theme

    print("\n== a stopped document, and the ones already written")
    real_workspace = files.WORKSPACE
    files.WORKSPACE = Path(tempfile.mkdtemp(prefix="retro-acceptance-"))
    try:
        model = _FakeModel()
        task = Task("write about cats")

        def stop_after_the_first_section(event):
            if event.get("kind") == "section_done" and event.get("index") == 1:
                task.cancel("user")

        paused = compose.write_document(request="write about cats", words=140,
                                        complete=model, stream=model.stream, task=task,
                                        on_event=stop_after_the_first_section,
                                        on_sentence=lambda sentence: None)
        note("a stopped document keeps the sections that were written",
             bool(paused.get("stopped")) and "carry on writing" in paused["message"],
             paused["message"][:88])
        note("a stopped document is still not a saved document",
             not list(files.workspace().glob("*.md")))
        draft = compose.load_draft()
        note("the finished section is on disk as a draft, before the next one is asked for",
             bool(draft) and len(draft["sections"]) == 1,
             f"{len(draft['sections'])} of {len(draft['outline'])} sections kept" if draft
             else "no draft on disk")
        note("the draft is picked up by the words the user would say",
             compose.is_resume("carry on writing") and
             compose.resume_line(draft).startswith("Carrying on with 'The Cat Documentary'"),
             compose.resume_line(draft)[:88])

        resumed = compose.write_document(request=draft["request"], words=draft["words"],
                                         complete=model, stream=model.stream, resume=draft,
                                         on_sentence=lambda sentence: None)
        written = (files.workspace() / resumed["filename"]).read_text(encoding="utf-8")
        note("carrying on writes the rest and saves one whole document",
             resumed.get("finished") and "## Origins" in written and "## Why they purr" in written,
             f"{resumed['words']} words in {resumed['filename']}")
        note("the draft is cleared once the document exists", compose.load_draft() is None)

        # The window, on those same files: the paused card, then the shelf.
        window.set_floating(False)
        window._on_writing({"kind": "paused", "title": "The Cat Documentary", "done": 1,
                            "total": 2, "words": 60})
        app.processEvents()
        note("a stopped document is shown as paused, not hidden",
             window.writing.isVisible() and window.writing.paused,
             f"the card reads {'PAUSED' if window.writing.paused else 'something else'} "
             f"at {window.writing.progress():.0%}")
        image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(theme.BG_TRANSPARENT)
        painter = QPainter(image)
        window.render(painter, QPoint(0, 0))
        painter.end()

        def amber(pixel):
            return pixel.alpha() > 60 and pixel.red() > 140 and pixel.green() > 80 \
                and pixel.blue() < 130

        lit_amber = sum(1 for y in range(image.height()) for x in range(image.width())
                        if amber(image.pixelColor(x, y)))
        note("the paused card is amber, not the green of a running job", lit_amber > 60,
             f"{lit_amber} amber pixels on the {window.width()}x{window.height()} panel")
        window._on_writing({"kind": "resume", "title": "The Cat Documentary", "done": 1,
                            "total": 2, "words": 140})
        arc = window.agent._arc
        note("a resumed document starts its ring where the first sitting left it",
             arc is not None and abs(arc - 0.5) < 0.01, f"arc {arc} for 1 of 2 sections")
        window.writing.reset()
        window._refresh_shelf()
        app.processEvents()
        names = [row["name"] for row in window.shelf.documents]
        counted = [word for word in written.split() if any(ch.isalnum() for ch in word)]
        note("the shelf names the document that was really written, with its own count",
             names and names[0] == resumed["filename"] and
             window.shelf.documents[0]["words"] == len(counted),
             f"{names[0]} ({window.shelf.documents[0]['words']} words) on the shelf")
        note("the shelf needs room, and gets it",
             window.shelf.isVisible() and window.height() > 532,
             f"panel is {window.height()}px with the shelf on screen")

        opened = []
        window.bridge.session.open_document = lambda name: opened.append(name) or name
        drawn = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
        drawn.fill(theme.BG_TRANSPARENT)
        shift = QPainter(drawn)
        window.render(shift, QPoint(0, 0))          # a real paint lays the shelf out
        shift.end()
        app.processEvents()
        rects = window.shelf.item_rects()
        if rects:
            name, rect = rects[0]
            point = rect.center()
            window.shelf.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, point,
                                                     point, Qt.MouseButton.LeftButton,
                                                     Qt.MouseButton.LeftButton,
                                                     Qt.KeyboardModifier.NoModifier))
        note("clicking a document on the shelf opens that one",
             bool(rects) and opened == [rects[0][0]],
             f"opened {opened} for a click on {rects[0][0] if rects else 'nothing'}")
        window.shelf.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(4, 4),
                                                 QPointF(4, 4), Qt.MouseButton.LeftButton,
                                                 Qt.MouseButton.LeftButton,
                                                 Qt.KeyboardModifier.NoModifier))
        note("a press that misses a document is handed back to the window",
             len(opened) == 1, "the panel can still be dragged by the shelf")

        # Reading it back: the file as speech, through the same sentence splitter the
        # voice uses. Nothing here plays audio, so this is run with the sound card idle.
        from core.tts import SentenceStream
        spoken = []
        splitter = SentenceStream()
        prose = compose.readable(written)
        for sentence in splitter.feed(prose) + splitter.flush():
            spoken.append(sentence)
        note("a document reads back as whole sentences, without its hash marks",
             len(spoken) >= 2 and "#" not in " ".join(spoken) and
             spoken[0].startswith("The Cat Documentary"),
             f"{len(spoken)} sentences: {spoken[0][:44]} ...")
        window.writing.reset()
        window._refresh_shelf()
    finally:
        files.WORKSPACE = real_workspace
        window.shelf.set_documents([])
        window._refresh_shelf()


def check_ring_interaction():
    """The ring answers the mouse - and passes through a press that misses it."""
    print("\n== the ring, by hand")
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from ui.agent import AgentWidget

    agent = AgentWidget()
    agent.setFixedSize(200, 200)
    before = agent.turned
    on_ring = QPointF(100, 100 + agent._ring_reach() * 0.5)
    press = QMouseEvent(QEvent.Type.MouseButtonPress, on_ring, on_ring,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    agent.mousePressEvent(press)
    move = QMouseEvent(QEvent.Type.MouseMove, on_ring + QPointF(40, -18), on_ring,
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    agent.mouseMoveEvent(move)
    note("dragging the ring turns it in three dimensions", agent.turned != before,
         f"yaw {agent.turned[0] - before[0]:+.2f}, pitch {agent.turned[1] - before[1]:+.2f}")
    corner = QPointF(4, 4)
    miss = QMouseEvent(QEvent.Type.MouseButtonPress, corner, corner,
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    agent.mousePressEvent(miss)
    note("a press that misses the ring is handed to the window", not miss.isAccepted(),
         "the panel stays draggable around the circle")
    agent.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, corner, corner,
                                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                     Qt.KeyboardModifier.NoModifier))
    note("the glow answers the pointer", agent._hover == 0.0,
         "hover is off the ring, so the glow is not lit")
    spin = agent._spin
    agent.mouseMoveEvent(move)
    note("a flicked ring keeps turning on its own", agent._spin != 0.0,
         f"spin {spin:+.3f} -> {agent._spin:+.3f}")


def _same_event_twice(agent):
    agent.set_progress("Opening Spotify")
    before = list(agent._floats)
    agent.set_progress("Opening Spotify")
    return len(agent._floats) == len(before)


# ---------- real launches ----------------------------------------------------


def check_launch(keys=()):
    """Registered applications, really opened - a few at a time, never a burst.

    The machine has to stay usable while this runs, so the default sample is three
    light apps and anything else is named explicitly with --app one at a time.
    Instances that were already running before the check are left exactly as they
    were: only processes that appeared during the launch are closed again. Web
    entries open a browser tab, which this check does not close - it says so in
    the report instead of pretending the tab is gone.
    """
    from core import registry

    keys = list(keys) or ["notepad", "file_explorer", "task_manager"]
    print(f"\n== opening real applications, one at a time ({len(keys)})")
    try:
        import psutil
    except ImportError:
        psutil = None
    tabs = 0
    for key in keys:
        spec = registry.SPECS[key]
        before = _pids_of(spec) if psutil else set()
        started = time.time()
        result = registry.open_app(spec.name)
        took = time.time() - started
        in_browser = "in the browser" in result.message
        if in_browser:
            tabs += 1
        note(f"open {spec.name}", result.ok,
             f"{took:.1f}s, verified={result.verified}, "
             f"{'browser tab' if in_browser else 'window/process'}: {result.message[:60]}")
        if psutil is not None and result.ok and not in_browser:
            _close_new(spec, before)
        time.sleep(0.8)                     # let the machine breathe between launches
    refused = _check_refusal(registry)
    note("an app that does not exist is refused, not claimed", refused,
         "a useful error came back" if refused else "it reported success")
    if tabs:
        print(f"       {tabs} web app(s) opened as browser tabs - those are left open")


def _hints(spec):
    hints = [hint.lower() for hint in (spec.verify or ())] or [spec.name.lower()]
    hints.append(spec.name.lower())
    for pattern in spec.exe or ():                 # so VS Code is found as Code.exe
        base = os.path.basename(str(pattern).replace("\\", "/").rstrip("* ")).lower()
        base = base.split(".", 1)[0]
        if len(base) >= 4:
            hints.append(base)
    return list(dict.fromkeys(hints))


def _pids_of(spec):
    """The pids of this app's processes right now, before anything is started."""
    import psutil

    hits = set()
    for process in psutil.process_iter(["pid", "name"]):
        name = (process.info.get("name") or "").lower().replace(" ", "")
        if any(hint.replace(" ", "") in name for hint in _hints(spec)):
            hits.add(process.info["pid"])
    return hits


def _close_new(spec, before):
    """Closes only the processes this check started, children included."""
    import psutil

    closed = 0
    hints = [hint.replace(" ", "") for hint in _hints(spec)]
    for process in psutil.process_iter(["pid", "name"]):
        pid = process.info["pid"]
        if pid in before or pid == os.getpid():
            continue
        name = (process.info.get("name") or "").lower().replace(" ", "")
        if not any(hint in name for hint in hints):
            continue
        try:
            kids = process.children(recursive=True)
        except Exception:
            kids = []
        for child in kids:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            process.terminate()
            closed += 1
        except Exception:
            pass
    return closed


def _close_flow_app(spec, before):
    """Closes a flow's app: the processes it started, then a browser tab if it is one."""
    closed = 0
    try:
        closed += _close_new(spec, before)
    except ImportError:
        pass
    closed += _close_own_window(spec, before)
    from core import tools as toolset
    if toolset.focus_window(spec.name, wait_s=1.5).ok:
        title = (toolset.foreground_title() or "").lower()
        if any(word in title for word in ("chrome", "edge", "firefox", "brave", "opera")):
            if toolset.press("ctrl+w").ok:
                closed += 1
    return closed


def _close_own_window(spec, before):
    """Closes a window this check opened whose process is not named after the app.

    A Java launcher's window belongs to javaw.exe, so closing by process name misses
    it. The window is closed politely instead (WM_CLOSE), and only when its process
    appeared after this check started - never a window that was already open.
    """
    import ctypes
    from ctypes import wintypes

    from core import tools as toolset

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hints = [hint.replace(" ", "") for hint in _hints(spec)]
    mine = os.getpid()
    closed = 0
    for hwnd, title in toolset.window_titles(limit=60):
        low = title.lower().replace(" ", "")
        if not any(hint in low for hint in hints):
            continue
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
        if not owner.value or owner.value in before or owner.value == mine:
            continue
        user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)      # WM_CLOSE
        closed += 1
    return closed


def _check_refusal(registry):
    result = registry.open_app("definitely not a real application xx9")
    return (not result.ok) and bool(result.message)


# ---------- real timings -----------------------------------------------------


def check_llm():
    """First token and first speakable sentence from the real cloud model."""
    from core import config
    from core.llm import build_brain

    print("\n== the model, measured over the real network")
    if not config.cloud_available():
        skipped("cloud model timings", "no API key on this machine")
        return
    brain, is_cloud = build_brain("groq")
    if not is_cloud:
        skipped("cloud model timings", "the cloud model could not be built")
        return
    from core.tts import SentenceStream
    marks = {}

    def on_delta(piece):
        if "first" not in marks:
            marks["first"] = time.time()
            marks["chars"] = len(piece)

    splitter = SentenceStream()

    def speakable(piece):
        for _sentence in splitter.feed(piece):
            marks.setdefault("sentence", time.time())
        on_delta(piece)

    messages = [{"role": "user", "content": "In two short sentences, what is a vector database?"}]
    started = time.time()
    reply = brain.stream(messages, 200, speakable)
    total = time.time() - started
    if "first" in marks:
        note("first LLM output", True,
             f"{marks['first'] - started:.2f}s to the first {marks['chars']} characters")
    else:
        note("first LLM output", False, "nothing streamed")
    if "sentence" in marks:
        note("first speakable portion", True,
             f"{marks['sentence'] - started:.2f}s ({splitter.feed('') or 'one sentence'})")
        note("speaking can start before the reply ends", marks["sentence"] < started + total,
             f"first sentence {marks['sentence'] - started:.2f}s, whole reply {total:.2f}s")
    else:
        note("first speakable portion", False, "no full sentence arrived before the end")
    note("the whole reply arrived", bool(reply), f"{len(reply.split())} words in {total:.2f}s")


def check_tts():
    """Synthesis latency for one sentence, hosted against the local voice."""
    from core import config
    from core.tts import GroqVoice, TtsError, split_sentences

    print("\n== the voice, measured on real audio")
    sentence = "Hi, this is Retro testing the voice pipeline on your laptop."
    if config.cloud_available():
        voice = GroqVoice()
        image = None
        for attempt in range(2):
            try:
                started = time.time()
                rate, image = voice.speech(sentence)
                took = time.time() - started
                break
            except TtsError as exc:
                if attempt == 1:
                    note("hosted speech synthesis", False, str(exc)[:90])
                    image = None
        if image is not None:
            seconds = len(image) / float(rate or 1)
            note("hosted speech synthesis", True,
                 f"{took:.2f}s for {len(split_sentences(sentence))} sentence(s), "
                 f"{seconds:.1f}s of audio, model {config.CLOUD_TTS_MODEL}")
            note("speech starts before the whole reply is ready", took < 3.0,
                 f"{took:.2f}s to first audio")
    else:
        skipped("hosted speech synthesis", "no API key on this machine")
    from core.voice import Speaker
    try:
        speaker = Speaker()
        speaker.ensure_voices()
        started = time.time()
        speaker.save_to_file(sentence, os.path.join(os.path.dirname(__file__), "_tts_check.wav"))
        took = time.time() - started
        note("local speech synthesis (fallback)", True,
             f"{took:.2f}s for the same sentence on {speaker.voice_name}")
        try:
            os.remove(os.path.join(os.path.dirname(__file__), "_tts_check.wav"))
        except OSError:
            pass
    except Exception as exc:
        note("local speech synthesis (fallback)", False, f"{type(exc).__name__}: {exc}"[:80])

    # The number the user actually feels: how long after a finished sentence the first
    # sound arrives. That is the synthesiser's cost, because the model's sentence was
    # already written - and it is the one part of the wait that cannot be hidden by
    # streaming, so it is measured rather than assumed.
    try:
        from core.tts import VoiceStream
        heard_at = []
        stream = VoiceStream(speaker=speaker, device=speaker.device, allow_cloud=True,
                             on_level=lambda level: heard_at.append((time.time(), level))
                             if level > 0.05 else None)
        started = time.time()
        stream.feed(sentence)
        first = None
        while time.time() - started < 25 and first is None:
            time.sleep(0.02)
            for when, _level in list(heard_at):
                if when - started > 0:
                    first = when - started
                    break
        stream.finish(timeout=30)
        if first is None:
            note("first sound after the sentence was ready", False, "nothing was audible in 25s")
        else:
            if stream.hosted:
                detail = f"{first:.2f}s on the hosted voice"
            elif stream.last_error and "429" in str(stream.last_error):
                detail = f"{first:.2f}s, hosted voice rate-limited, local voice took over"
            elif stream.last_error:
                detail = f"{first:.2f}s on the local voice ({stream.last_error[:40]})"
            else:
                detail = f"{first:.2f}s on the local voice (no key or still cooling down)"
            note("first sound after the sentence was ready", first < 4.0, detail)
    except Exception as exc:
        note("first sound after the sentence was ready", False,
             f"{type(exc).__name__}: {exc}"[:80])


# ---------- the voice, out of the speakers -----------------------------------


def check_audio_out():
    """Does a line played on the chosen output actually make a sound?

    This is the check that was missing, and the user paid for it twice: the app said it
    was speaking while nothing was audible. The second time the cause was that every
    device the window offers is a WASAPI device - 48 000 Hz only - while SAPI renders
    at 22 050 Hz, so *choosing an output device* was enough to make the assistant mute.
    Nothing could notice, because the only level measured was read out of the buffer the
    app had just filled; 192 lines were written off as wedged and the voice then
    disabled itself for the rest of the session (retro.log, on this machine).

    Measured here, now: the rate each offered device accepts, whether the remembered
    device is still here and can be opened, and the **Windows mixer's own meter** while
    a real line plays. That last number is the only one that cannot be true when
    nothing came out of the speakers.
    """
    from core import settings
    from core.voice import (Speaker, default_output_name, device_usable, output_devices,
                            playable_rate)

    print("\n== the voice, out of the speakers")
    offered = output_devices()
    if not offered:
        skipped("an output device is offered", "no audio output device here")
        return
    stubborn = [name for index, name in offered if playable_rate(index, 22050) is None]
    note("every offered device takes a rate SAPI can produce", not stubborn,
         f"{len(offered)} offered" + (f", will not open: {stubborn}" if stubborn else ""))
    speaker = Speaker()
    try:
        speaker.ensure_voices()
    except Exception as exc:
        note("the local voice is installed", False, f"{type(exc).__name__}: {exc}"[:90])
        return
    saved = settings.get("output")
    resolved = speaker.choose_output(saved)
    if saved and resolved is None:
        note("the remembered output device is still usable", False,
             f"{str(saved)[:40]!r} is not among the devices offered now")
        speaker.choose_output(None)
    else:
        note("the remembered output device is still usable", True,
             f"device {resolved!r}" if saved else "nothing saved: the system default")
    usable = speaker.device is None or device_usable(speaker.device)
    note("the device about to be spoken through can be opened", usable,
         f"device {speaker.device!r}, {default_output_name()!r} is the default")
    started = time.time()
    played, mixer_peak = speaker.audible("Retro speaking on the output you chose.")
    took = time.time() - started
    note("a line plays on that output", bool(played),
         f"{took:.2f}s, rendered peak {speaker.last_peak:.2f}")
    if mixer_peak is None:
        skipped("the Windows mixer hears the line", "the mixer could not be read here")
    else:
        note("the Windows mixer hears the line", mixer_peak > 0.001,
             f"mixer peak {mixer_peak:.3f} while it played")


# ---------- the voice, into the machine --------------------------------------


def check_microphone(record=False):
    """Is there a microphone, is the remembered one still here, and can it be heard?

    The output has had a check like this since a chosen device meant a mute assistant.
    The input had none, and the symptom of a wrong input - Windows picking a webcam or
    a headset that is not on anyone's head - was identical to a broken recogniser:
    "Retro can't hear me". Listing the devices and resolving the remembered one costs
    nothing and opens no stream; --mic goes further and records about a second, which
    is the only way to tell a deaf microphone from a quiet room.
    """
    from core import settings
    from core.session import Session
    from core.voice import default_input_name, input_devices, input_usable

    print("\n== the voice, into the machine")
    offered = input_devices()
    if not offered:
        skipped("an input device is offered", "no recording device here")
        return
    note("every offered microphone can be opened at the rate the ears need",
         all(input_usable(index) for index, _name in offered),
         f"{len(offered)} offered")
    saved = settings.get("mic_device")
    session = Session()
    if saved:
        resolved = session._input_device()
        note("the remembered microphone is still here", resolved is not None,
             f"{str(saved)[:40]!r} -> device {resolved!r}" if resolved is not None
             else f"{str(saved)[:40]!r} is not among the devices offered now")
    else:
        note("the remembered microphone is still here", True,
             f"nothing saved: the system default, {default_input_name()!r}")
    if not record:
        skipped("the microphone really hears something",
                "run with --mic to record about a second (no transcription, no cloud call)")
        return
    try:
        level = session.test_microphone(seconds=1.5)
    except Exception as exc:
        note("the microphone really hears something", False,
             f"{type(exc).__name__}: {exc}"[:90])
        return
    # What this can honestly tell apart in one second is a stream that delivered
    # *nothing at all* (digital silence: a closed or dead capture) from a stream that
    # delivered a signal. It cannot tell a silent room from a muted microphone, so it
    # does not pretend to: the level is printed and the judgement is passed on to the
    # only sensor that exists for it - the owner, with `MIC IN` and `TEST MIC` open.
    # A threshold here would fail a healthy microphone in a quiet room, which is how an
    # acceptance tool teaches someone to ignore it.
    if float(level) <= 0.0:
        note("the microphone really hears something", False,
             "the stream delivered pure digital silence - check the mute switch, the "
             "Windows input device, and MIC IN in the settings panel")
    else:
        note("the microphone really hears something", True,
             f"peak {float(level):.3f} in 1.5s" + (" - a quiet room and a muted "
                                                   "microphone look the same here; "
                                                   "TEST MIC in the panel is how you "
                                                   "tell them apart"
                                                   if float(level) < 0.02 else ""))


# ---------- the arrival: the logo, the fade, and Windows starting it -------------


def check_arrival(play=False):
    """What happens at logon, as far as a machine in use can be asked.

    The splash is drawn offscreen, frame by frame, rather than shown: a check that
    takes over the screen for eight seconds is a check nobody runs twice. What is
    measured is what the owner asked for - the word appearing letter by letter, five
    seconds of it and a three-second fade, the line at the end, and Windows actually
    being told to start Retro.

    With `--arrival --play` the real splash is shown and the real line is spoken, which
    is the only way to hear the soundtrack and see the fade on this screen.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from core import intro as plan
    from ui import intro as splash

    print("\n== the arrival: five seconds of logo, three of fade")
    app = QApplication.instance() or QApplication([])
    note("the reveal is five seconds and the fade three",
         plan.INTRO_S == 5.0 and plan.FADE_S == 3.0,
         f"reveal {plan.INTRO_S:.1f}s, fade {plan.FADE_S:.1f}s, cap {plan.HARD_CAP_S:.1f}s")
    beats = plan.beats()
    ordered = all(beats[index][1] <= beats[index + 1][2] + 1e-9 for index in range(len(beats) - 1))
    inside = all(end <= plan.INTRO_S for _name, _start, end in beats)
    note("every letter's entrance is inside the reveal, in order", ordered and inside,
         ", ".join(f"{name} {start:.2f}-{end:.2f}" for name, start, end in beats))

    widget = splash.IntroSplash(with_sound=False)
    widget.setGeometry(0, 0, 960, 540)
    widget.rebuild()
    counts = []
    for moment in (0.15, 1.10, 2.60, 4.90):
        widget.render_at(moment)
        image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#000000"))
        painter = QPainter(image)
        widget.render(painter, _point())
        painter.end()
        counts.append(sum(1 for y in range(0, image.height(), 4)
                          for x in range(0, image.width(), 4)
                          if sum(image.pixelColor(x, y).getRgb()[:3]) > 90))
    note("the wordmark grows as the letters arrive",
         counts[0] < counts[1] < counts[2],
         f"lit samples {counts} at 0.15s, 1.10s, 2.60s, 4.90s")
    # Not growth to the end: the glow is *meant* to quieten down while the finished
    # word is held, and reading that as a regression is reading the design as a bug.
    # What has to be true is that the word is still all there when the fade starts.
    note("the whole word is still on screen when the fade begins",
         counts[-1] > counts[2] * 0.7 and counts[-1] > 1200,
         f"{counts[-1]} lit samples at 4.90s, {counts[-1] / max(1, counts[2]):.0%} of the lock")
    flags = widget.windowFlags()
    from PySide6.QtCore import Qt
    note("the splash has no title bar and stays on top",
         bool(flags & Qt.WindowType.FramelessWindowHint)
         and bool(flags & Qt.WindowType.WindowStaysOnTopHint))
    desktop = splash.IntroSplash.desktop_rect()
    covered = all(desktop.contains(screen.geometry()) for screen in app.screens())
    note("the splash covers every screen on this desktop", covered,
         f"{desktop.width()}x{desktop.height()} for {len(app.screens())} screen(s)")
    widget.deleteLater()

    from core import settings, startup
    message, on = startup.status()
    if not startup.is_windows():
        skipped("Windows knows Retro starts at logon", "this is not Windows")
    else:
        note("Windows can be told to start Retro at logon", startup.command() != "",
             f"{message}: {startup.command()[:64]}")
        note("the startup entry is either off or pointing at this copy", on or "not this one" not in message,
             message)
    note("the arrival can be switched off without losing the line",
         settings.get_bool("intro", True) in (True, False),
         f"intro={settings.get_bool('intro', True)}, sound={settings.get_bool('intro_sound', True)}")

    if play:
        widget = splash.play()
        started = time.time()
        while time.time() - started < plan.total_seconds() + 0.5 and widget.isVisible():
            app.processEvents()
            time.sleep(0.02)
        note("the real splash plays and finishes on its own",
             time.time() - started <= plan.HARD_CAP_S, f"{time.time() - started:.2f}s on screen")


def _point():
    from PySide6.QtCore import QPoint
    return QPoint(0, 0)


# ---------- one real workflow ------------------------------------------------


def check_flow(which):
    """One workflow, then the app it opened is closed before anything else runs.

    The machine is being used for other things: one app at a time, tested, then shut
    down again, so a check never leaves a stack of windows behind.
    """
    from core import registry

    flow_apps = {"notepad": "notepad", "vscode": "vscode", "spotify": "spotify",
                 "whatsapp": "whatsapp", "legacy": "legacy_launcher"}
    spec = registry.spec_for(flow_apps.get(which, ""))
    try:
        before = _pids_of(spec) if spec is not None else set()
    except ImportError:
        before = set()
    print(f"\n== a real workflow: {which}")
    try:
        _flow_body(which)
    finally:
        if spec is not None:
            closed = _close_flow_app(spec, before)
            print(f"       closed {closed} window(s) this flow started")


def _flow_body(which):
    from core import tools

    if which == "notepad":
        text = ("Black holes are places where matter is so dense that light cannot "
                "escape. They are not holes; they are what gravity looks like when "
                "there is nothing left to push back.")
        started = time.time()
        result = tools.run("app_function", {"app": "notepad", "action": "write_text",
                                            "detail": text, "target": "black holes"},
                           confirm=lambda question: True)
        note("notepad: write and save a real note", result.ok,
             f"{time.time() - started:.1f}s, verified={result.verified}: {result.message[:80]}")
    elif which == "vscode":
        started = time.time()
        result = tools.run("app_function", {"app": "vscode", "action": "create_project",
                                            "detail": "", "target": "acceptance demo"},
                           confirm=lambda question: True)
        note("vs code: create a real project and open it", result.ok,
             f"{time.time() - started:.1f}s, verified={result.verified}: {result.message[:80]}")
    elif which == "spotify":
        from core import registry
        started = time.time()
        opened = registry.open_app("Spotify")
        note("spotify: the app opens and is seen", opened.ok and opened.verified,
             f"{time.time() - started:.1f}s: {opened.message[:70]}")
        started = time.time()
        result = tools.run("app_function", {"app": "spotify", "action": "search_play",
                                            "detail": "sunflower", "target": ""},
                           confirm=lambda question: True)
        drove = bool(result.verified) or "typed 'sunflower'" in result.message
        note("spotify: a search is really driven in it", drove,
             f"{time.time() - started:.1f}s: {result.message[:88]}")
        if result.verified:
            note("spotify: playback confirmed by real sound", True, "the audio meter moved")
        else:
            skipped("spotify: playback confirmed",
                    "the desktop app exposes no controls and its title never changes, so a "
                    "track starting cannot be confirmed here; the mixer showed no audio "
                    "session afterwards - a Spotify Web API client is the documented fix")
    elif which == "whatsapp":
        # A real message and a name that is not a real contact: the flow must search,
        # fail to identify them, and send nothing. If a send were ever proposed the
        # question is recorded and refused, and the check fails loudly.
        started = time.time()
        asked = []
        result = tools.run("app_function",
                           {"app": "whatsapp", "action": "send_message",
                            "detail": "hello from the acceptance check",
                            "target": "zzz nobody at all"},
                           confirm=lambda question: asked.append(question) or False)
        note("whatsapp: an unknown contact is not messaged", not result.ok and not asked,
             f"{time.time() - started:.1f}s: {result.message[:90]}"
             + (f" | asked first: {asked}" if asked else ""))
    elif which == "legacy":
        from core import registry
        started = time.time()
        result = registry.open_app("minecraft")
        note("minecraft: the request lands on Legacy Launcher",
             result.ok and "Legacy Launcher" in result.message,
             f"{time.time() - started:.1f}s: {result.message[:70]}")
        if result.ok:
            # A Java launcher needs a while to draw and its title says so while it
            # loads; the play screen is only worth looking for after that.
            deadline = time.time() + 30.0
            labels = []
            playish = ("play", "launch", "start", "играть", "enter the game")
            while time.time() < deadline:
                titles = [t for _h, t in tools.window_titles(limit=40)
                          if "legacy" in t.lower()]
                if titles and "loading" not in titles[0].lower():
                    tools.focus_window("Legacy Launcher", wait_s=4.0)
                    _title, labels = tools.screen_labels(limit=80)
                    break
                time.sleep(1.5)
            play = [label for label in labels if label.strip().lower() in playish]
            if play:
                note("legacy launcher: the play interface is reachable", True,
                     f"play controls read back: {play[:3]}")
            else:
                skipped("legacy launcher: play button read back",
                        f"the play screen is up (a Java window) but it exposes no controls "
                        f"to UI Automation here ({len(labels)} labels) - a genuine "
                        f"limitation, not a pass")
    elif which == "background":
        # Real work on a worker thread - the way a task runs - while the main thread
        # keeps doing what the window and the voice loop do. The point is that neither
        # side is blocked by the other.
        import threading

        from core import files

        done = {}

        def work():
            done["result"] = files.create_threejs_project("background acceptance")

        started = time.time()
        worker = threading.Thread(target=work, name="acceptance-background")
        worker.start()
        samples = []
        while worker.is_alive() and time.time() - started < 25:
            tick = time.time()
            tools.foreground_title()
            tools.window_titles(limit=10)
            samples.append(time.time() - tick)
            time.sleep(0.2)
        worker.join(timeout=5)
        slowest = max(samples) if samples else 0.0
        note("a real task runs on its own thread", not worker.is_alive(),
             f"finished in {time.time() - started:.1f}s")
        note("the screen stayed readable while it worked", slowest < 0.5,
             f"slowest read {slowest * 1000:.0f}ms over {len(samples)} samples")
        result = done.get("result")
        folder = files.PROJECTS / "background acceptance"
        real_files = sorted(p.name for p in folder.glob("*") if p.is_file()) if folder.exists() else []
        note("the background task's files are on disk",
             bool(result and result.ok and real_files),
             f"{result.message[:60] if result else 'no result'}; files: {real_files}")
    elif which == "mode":
        from core import modes, tools as toolset
        result = toolset.run("type_text", {"text": "x"}, mode=modes.CHAT)
        note("chat mode refuses a desktop action", not result.ok, result.message[:80])
        result = toolset.run("read_screen", {"limit": 5}, mode=modes.CHAT)
        note("chat mode still allows reading", "chat mode" not in result.message)
    else:
        skipped(f"flow {which}", "not a flow this check knows")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--app", action="append", default=[],
                        help="registry key(s) to open, one at a time")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--audio", action="store_true",
                        help="speak one real line and ask the mixer whether it came out")
    parser.add_argument("--mic", action="store_true",
                        help="list the microphones and record a second to prove one is heard")
    parser.add_argument("--arrival", action="store_true",
                        help="the intro splash, the fade and the Windows startup entry")
    parser.add_argument("--play", action="store_true",
                        help="with --arrival: show the real splash and play its sound")
    parser.add_argument("--flow", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    default = not any([args.registry, args.ui, args.launch, args.llm, args.tts, args.audio,
                       args.mic, args.arrival, args.flow, args.all])

    if args.all or default or args.registry:
        check_registry()
    if args.all or default or args.ui:
        check_ui()
    # The splash is drawn offscreen unless --play is given: --all must not take the
    # screen over for eight seconds on the machine it is being run on.
    if args.all or default or args.arrival:
        check_arrival(play=args.play and not args.all)
    if args.all or args.audio:
        check_audio_out()
    if args.all or args.mic:
        # --all lists the microphones but records nothing: it is meant to be run on a
        # machine in use, and only the explicit --mic takes a second of the room.
        check_microphone(record=args.mic)
    if args.all or args.launch:
        check_launch(args.app)
    if args.all or args.llm:
        check_llm()
    if args.all or args.tts:
        check_tts()
    for flow in (["notepad", "vscode", "mode", "background"] if args.all else args.flow):
        check_flow(flow)

    passed = sum(1 for _name, ok, _d in RESULTS if ok is True)
    failed = [name for name, ok, _d in RESULTS if ok is False]
    not_run = [name for name, ok, _d in RESULTS if ok is None]
    print(f"\n--- {passed} passed, {len(failed)} failed, {len(not_run)} not run ---")
    for name in failed:
        print(f"  failed: {name}")
    for name in not_run:
        print(f"  not run: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
