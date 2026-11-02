"""The window while a document is being written, and the ring you can take hold of.

Two things are pinned here, both of them answers to the same complaint - that a long
job was invisible and the circle was something you could only look at:

* the writing stage comes out with a document, shows the section being written and the
  words as they arrive, grows the panel to hold itself, and folds away again - and it
  is out of the way in floating mode, where nothing but the circle belongs;
* the ring answers the mouse: hover lights it, dragging turns it in three dimensions,
  letting go leaves it spinning, and a press that misses the ring is handed back to
  the window so the panel can still be dragged around the circle.

Everything here runs on Qt's offscreen platform, so it works with no desktop.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6", reason="the window needs PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402
from ui.agent import AgentWidget  # noqa: E402
from ui.app import PANEL_H, PANEL_W, RetroWindow  # noqa: E402
from ui.writing import (HEIGHT as STAGE_H, SHELF_HEIGHT, WritingStage,  # noqa: E402
                        words_of)


@pytest.fixture(scope="module")
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def empty_workspace(tmp_path, monkeypatch):
    """Every window here reads an empty workspace.

    The window's shelf is built from the documents really in the workspace, so without
    this a developer's own documents would decide the panel's height and these tests
    would pass or fail according to what they had asked Retro to write.
    """
    from core import files
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    return tmp_path


def render(widget, size=None):
    size = size or widget.size()
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.BG_TRANSPARENT)
    widget.resize(size)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def lit(image):
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y).alpha() > 40)


def press(widget, point, button=Qt.MouseButton.LeftButton):
    event = QMouseEvent(QEvent.Type.MouseButtonPress, point, point, button, button,
                        Qt.KeyboardModifier.NoModifier)
    widget.mousePressEvent(event)
    return event


def move(widget, point, buttons=Qt.MouseButton.NoButton):
    event = QMouseEvent(QEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton,
                        buttons, Qt.KeyboardModifier.NoModifier)
    widget.mouseMoveEvent(event)
    return event


# ---------- the writing stage ------------------------------------------------

def test_the_card_is_out_of_the_way_until_there_is_a_document(qt):
    window = RetroWindow(floating=False, voice=False)
    assert window.writing.isHidden()
    assert window.size() == QSize(PANEL_W, PANEL_H)
    window.close()


def test_a_document_brings_the_card_out_and_the_panel_grows_for_it(qt):
    window = RetroWindow(floating=False, voice=False)
    window._on_writing({"kind": "begin", "title": "The most popular anime",
                        "total": 4, "words": 200})
    assert window.writing.isVisible()
    assert window.size() == QSize(PANEL_W, PANEL_H + STAGE_H + 12), \
        "the card must have somewhere to live"
    assert window.writing.progress() == 0.0

    window._on_writing({"kind": "section", "index": 2, "total": 4, "heading": "Why it travels"})
    window._on_writing({"kind": "section_done", "index": 2, "total": 4, "words": 60})
    assert window.agent._arc == 0.5, "two of four sections is half a ring"
    assert window.writing._heading == "Why it travels"

    window._on_writing({"kind": "writing", "text": "the prose as it arrives, one word at a time"})
    assert window.writing._text.startswith("the prose")

    window._on_writing({"kind": "done", "title": "The most popular anime", "words": 214,
                        "file": "The most popular anime.md"})
    assert window.agent._arc is None, "a finished document is not a running job"
    assert window.writing._file.endswith(".md")

    window.writing.reset()
    window._fit_panel()
    assert window.size() == QSize(PANEL_W, PANEL_H), "the panel goes back to its own size"
    window.close()


def prose_pixels(image):
    """Lit pixels in the band the prose is drawn in, where the card's own background
    is not - so what is counted is the writing, not the panel behind it."""
    top, bottom = 44, 84
    return sum(1 for y in range(top, bottom) for x in range(image.width())
               if image.pixelColor(x, y).alpha() > 40
               and image.pixelColor(x, y).green() > 60)


def test_the_card_draws_the_words_it_was_given(qt):
    stage = WritingStage()
    stage.setFixedWidth(PANEL_W - 40)
    empty = render(stage)
    stage.begin("A documentary about cats", 3, 200)
    stage.section(2, 3, "Where they sleep")
    stage.writing("A cat sleeps for most of the day. A cat also decides when the day ends.")
    drawn = render(stage)
    assert not stage.isHidden()
    assert prose_pixels(drawn) > prose_pixels(empty) + 200, \
        f"nothing was drawn for the document ({prose_pixels(drawn)} lit pixels)"
    assert words_of(stage._text) == 16
    stage.reset()
    assert stage.isHidden()
    assert stage.progress() is None, "no sections counted means no progress to show"


def test_the_card_folds_itself_away_after_a_finished_document(qt):
    stage = WritingStage()
    stage.begin("Something short", 1, 100)
    assert stage.live
    stage.finish("Something short", 96, "Something short.md")
    assert not stage.live and stage.isVisible()
    stage._fold_away()                     # what the timer does nine seconds later
    assert stage.isHidden()


def test_floating_mode_takes_the_card_with_it(qt):
    """Only the circle is on screen while floating - and a document being written is
    still there when the settings come back, because it has not finished."""
    window = RetroWindow(floating=False, voice=False)
    window._on_writing({"kind": "begin", "title": "A long one", "total": 2, "words": 200})
    assert window.writing.isVisible()
    window.set_floating(True)
    assert not window.writing.isVisible(), "only the circle belongs on screen"
    window.set_floating(False)
    assert window.writing.isVisible(), "the document was still being written"
    window.writing.finish("A long one", 180, "A long one.md")
    window.set_floating(True)
    window.set_floating(False)
    assert window.writing.isVisible(), \
        "a finished card is still readable, so it comes back with the settings"
    window.writing._fold_away()             # ...until its own timer puts it away
    window.set_floating(True)
    window.set_floating(False)
    assert not window.writing.isVisible(), \
        "a folded card comes back when there is another document, not with the settings"
    window.close()


def test_a_stopped_document_says_the_work_was_kept(qt):
    """The amber ending: PAUSED, with the words that survived, instead of vanishing."""
    window = RetroWindow(floating=False, voice=False)
    window._on_writing({"kind": "begin", "title": "The most popular anime",
                        "total": 4, "words": 200})
    window._on_writing({"kind": "section_done", "index": 2, "total": 4, "words": 60})
    window._on_writing({"kind": "paused", "title": "The most popular anime", "done": 2,
                        "total": 4, "words": 120})
    assert window.writing.isVisible() and window.writing.paused
    assert not window.writing.live, "a stopped document is not being written"
    assert abs((window.writing.progress() or 0) - 0.5) < 0.01
    assert window.writing._words == 120, "the words that were kept are counted"
    assert window.agent._arc is None, "nothing is running any more"
    window.writing.reset()
    window._fit_panel()
    assert window.size() == QSize(PANEL_W, PANEL_H)
    window.close()


def test_the_shelf_names_the_documents_that_exist_and_clicking_one_opens_it(qt, tmp_path,
                                                                           monkeypatch):
    """Where did that document go: named under the circle, with its real word count."""
    for index, (name, text) in enumerate((("Cats.md", "# Cats\n\nCats sleep all day.\n"),
                                          ("Dogs.md", "# Dogs\n\nDogs run about town.\n"),
                                          ("Ships.md", "# Ships\n\nShips go to sea.\n"))):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (1_000_000 + index * 1000, 1_000_000 + index * 1000))

    opened = []
    window = RetroWindow(floating=False, voice=False)
    monkeypatch.setattr(window.bridge.session, "open_document",
                        lambda name: opened.append(name) or os.path.join(str(tmp_path), name))
    window._refresh_shelf()
    assert window.shelf.isVisible(), "the documents that exist were not shown"
    names = [row["name"] for row in window.shelf.documents]
    assert names == ["Ships.md", "Dogs.md", "Cats.md"], f"newest first, three at most: {names}"
    assert window.shelf.documents[0]["words"] == 5
    render(window.shelf)                    # the rects are laid out by the paint
    assert window.size() == QSize(PANEL_W, PANEL_H + SHELF_HEIGHT + 8), \
        "the shelf needs its own room in the panel"

    # A click lands on the document that is drawn under the pointer, and on nothing else.
    rects = window.shelf.item_rects()
    assert len(rects) == 3
    name, rect = rects[1]
    press(window.shelf, rect.center())
    assert opened == [name], f"the click opened {opened}, not {name}"
    assert window._reply.startswith("Opened")
    press(window.shelf, QPointF(4, SHELF_HEIGHT + 4))
    assert len(opened) == 1, "a press that misses is the window's, to drag it by"

    # The shelf is the panel's, so it goes with the panel and comes back with it.
    window.set_floating(True)
    assert not window.shelf.isVisible(), "only the circle belongs on screen"
    window.set_floating(False)
    assert window.shelf.isVisible()
    window.close()


def test_the_shelf_waits_while_the_writing_card_is_on_screen(qt, tmp_path):
    """One strip, one story: what is being written, or what has been - and the handover
    happens when the finished card folds away, not a moment before."""
    (tmp_path / "Cats.md").write_text("# Cats\n\nCats sleep.\n", encoding="utf-8")
    window = RetroWindow(floating=False, voice=False)
    assert window.shelf.isVisible()
    window._on_writing({"kind": "begin", "title": "A new one", "total": 2, "words": 200})
    assert not window.shelf.isVisible(), "the strip says what is happening now, not both"
    window._on_writing({"kind": "done", "title": "A new one", "words": 180,
                        "file": "A new one.md"})
    assert not window.shelf.isVisible(), "the finished card is still being read"
    window.writing._fold_away()             # nine seconds later, on the card's own timer
    assert window.shelf.isVisible(), "the shelf did not come back after the card folded"
    window.close()


def test_opening_a_document_that_is_not_there_says_so(qt):
    window = RetroWindow(floating=False, voice=False)
    window._open_document("nowhere.md")
    assert "couldn't open" in window._reply
    window.close()


def test_a_document_started_while_floating_stays_out_of_the_circle(qt):
    """The card shows itself when a document begins, so the floating rule has to be
    enforced on the way in rather than only when the mode is switched."""
    window = RetroWindow(floating=True, voice=False)
    assert window._floating
    window._on_writing({"kind": "begin", "title": "A long one", "total": 2, "words": 200})
    assert not window.writing.isVisible(), "the card popped into the floating circle"
    assert window.writing.live, "...but the document is still being written"
    window.set_floating(False)
    assert window.writing.isVisible(), "the settings come back with the document"
    window.close()


def test_the_session_reaches_the_window_through_the_bridge(qt):
    """The path a real document takes: session -> bridge -> the window's card."""
    from ui.bridge import Bridge

    bridge = Bridge()
    seen = []
    bridge.writingChanged.connect(seen.append)
    window = RetroWindow(bridge=bridge, floating=False, voice=False)
    bridge.session._writing_event({"kind": "begin", "title": "The most popular anime",
                                   "total": 3, "words": 200})
    assert seen and seen[0]["kind"] == "begin", "the bridge dropped the document"
    bridge.session._writing_event({"kind": "section_done", "index": 1, "total": 3,
                                   "words": 70})
    assert abs((window.agent._arc or 0.0) - 1 / 3) < 0.01, \
        f"the ring's arc did not follow the session: {window.agent._arc}"
    bridge.session._writing_event({"kind": "done", "title": "The most popular anime",
                                   "words": 214, "file": "The most popular anime.md"})
    assert window.agent._arc is None
    window.close()


def test_the_phase_is_written_inside_the_circle(qt):
    window = RetroWindow(floating=False, voice=False)
    window._on_state("working", "planning")
    assert window.agent._caption == "PLANNING"
    window._on_state("asleep", "say 'Retro'")
    assert window.agent._caption == "SAY 'RETRO'"
    window.close()


# ---------- the ring, by hand ------------------------------------------------

def test_the_ring_is_a_turned_solid_not_a_flat_line(qt):
    """The near side of the band is drawn larger than the far side - and it closes."""
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    agent._yaw, agent._pitch = 0.9, 0.55
    points = agent._points(100, 100, 60, 0.0)
    depths = [depth for _x, _y, depth in points]
    assert max(depths) > 0.85 and min(depths) < 0.15, "the ring is not turning"
    # The projected radius has to differ across the turn, or it is a flat drawing.
    radii = [((x - 100) ** 2 + (y - 100) ** 2) ** 0.5 for x, y, _depth in points]
    assert max(radii) > min(radii) * 1.2, f"no depth in the projection: {min(radii):.1f}..{max(radii):.1f}"


def test_hover_lights_the_ring_and_leaving_lets_it_go(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    move(agent, QPointF(100, 100 + agent._ring_reach() * 0.4))
    assert agent._hover == 1.0
    agent.leaveEvent(None)
    assert agent._hover == 0.0


def test_dragging_turns_the_ring_and_a_flick_keeps_it_turning(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    on_ring = QPointF(100, 100 + agent._ring_reach() * 0.4)
    yaw, pitch = agent.turned
    assert press(agent, on_ring).isAccepted()
    move(agent, on_ring + QPointF(60, -20), buttons=Qt.MouseButton.LeftButton)
    turned = agent.turned
    assert turned[0] > yaw and turned[1] > pitch, f"the drag did not turn it: {turned}"
    assert agent._spin != 0.0, "a flick should leave something to settle"
    agent.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, on_ring, on_ring,
                                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier))
    spins = []
    for _ in range(40):
        agent._tick()
        spins.append(agent._spin)
    assert spins[0] > spins[-1], "the ring should settle rather than spin for ever"


def test_a_press_that_misses_the_ring_is_handed_back_to_the_window(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    corner = QPointF(3, 3)
    assert not press(agent, corner).isAccepted(), "the panel must still be draggable"
    assert press(agent, QPointF(100, 100)).isAccepted()


def test_the_arc_is_only_drawn_when_something_counted_the_steps(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    agent.set_state("working")
    agent._colour = agent._target
    quiet = render(agent)
    agent.set_arc(0.5)
    half = render(agent)
    assert lit(half) > lit(quiet), "a half-filled arc must actually draw"
    agent.set_arc(None)
    assert agent._arc is None
    agent.set_arc(5.0)
    assert agent._arc == 1.0, "an arc is a fraction, whatever it is handed"
    agent.set_arc(-2)
    assert agent._arc == 0.0


def test_the_spikes_are_the_real_level_and_silence_draws_none(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    agent.set_state("speaking")
    quiet = render(agent)
    for _ in range(20):
        agent.set_level(0.9)
        agent._tick()
    loud = render(agent)
    assert lit(loud) > lit(quiet), "the rim should bristle when there is real audio"
