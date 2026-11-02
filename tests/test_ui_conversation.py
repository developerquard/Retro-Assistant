"""The conversation, the status strip and the working row, on their own.

The window is tested elsewhere; these are the three new widgets as widgets, so a
regression in the record - a swapped role, markup interpreted, a dropped old turn, a
STOP that only looks like one - fails here rather than in a screenshot.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6", reason="the window needs PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.conversation import (MAX_TURNS, SUGGESTIONS, Conversation,  # noqa: E402
                             StatusStrip, Turn, WorkingRow)


@pytest.fixture(scope="module")
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


# ---- a turn ------------------------------------------------------------------

def test_a_turn_keeps_who_said_it_and_what_was_said(qt):
    turn = Turn("user", "write a note")
    assert turn.role == "user" and turn.text() == "write a note"
    assert turn.objectName() == "turnUser"
    assert Turn("retro", "done").objectName() == "turnRetro"
    assert Turn("retro", "no", tone="error").objectName() == "turnError"


def test_a_reply_is_plain_text_and_never_markup(qt):
    """An answer containing markup must read as markup, not run it."""
    turn = Turn("retro", "<b>bold</b> & <script>x</script>")
    assert turn.body.text() == "<b>bold</b> & <script>x</script>"
    assert turn.body.textFormat() == Qt.TextFormat.PlainText


def test_copy_puts_the_message_on_the_clipboard_and_says_so(qt):
    turn = Turn("retro", "the answer")
    turn.copy()
    assert QApplication.clipboard().text() == "the answer"
    assert turn.copy_btn.text() == "COPIED"
    turn._relabel()
    assert turn.copy_btn.text() == "COPY"


# ---- the record --------------------------------------------------------------

def test_turns_are_kept_in_the_order_they_were_said(qt):
    talk = Conversation()
    talk.add("user", "one")
    talk.add("retro", "two")
    talk.add("user", "three")
    assert [turn.text() for turn in talk.turns] == ["one", "two", "three"]
    assert [turn.role for turn in talk.turns] == ["user", "retro", "user"]
    assert talk._messages.count() == 3, "a turn has to reach the layout, not just the list"


def test_the_empty_state_goes_away_and_comes_back(qt):
    talk = Conversation()
    assert not talk.empty.isHidden()
    talk.add("user", "hello")
    assert talk.empty.isHidden()
    talk.clear()
    assert talk.turns == []
    assert not talk.empty.isHidden()


def test_an_empty_message_is_not_a_turn(qt):
    talk = Conversation()
    assert talk.add("retro", "   ") is None
    assert talk.turns == []


def test_the_record_stops_at_the_cap_and_drops_the_oldest(qt):
    talk = Conversation()
    for index in range(MAX_TURNS + 5):
        talk.add("user", f"message {index}")
    assert len(talk.turns) == MAX_TURNS
    assert talk.turns[0].text() == "message 5", "the oldest turns are the ones dropped"
    assert talk.turns[-1].text() == f"message {MAX_TURNS + 4}"


def test_the_suggestions_are_whole_commands_that_can_be_pressed(qt):
    talk = Conversation()
    seen = []
    talk.suggested.connect(seen.append)
    assert [chip.text() for chip in talk.chips] == list(SUGGESTIONS)
    talk.chips[0].click()
    assert seen == [SUGGESTIONS[0]]
    assert all(chip.text() == chip.accessibleName().split(": ")[-1] for chip in talk.chips)


def test_a_short_record_puts_the_suggestions_away_but_keeps_the_line(qt):
    talk = Conversation()
    talk.set_compact(True)
    assert all(chip.isHidden() for chip in talk.chips)
    assert not talk.empty_body.isHidden(), "the line explaining the panel stays"


# ---- the working row --------------------------------------------------------

def test_the_working_row_ticks_and_stops(qt):
    row = WorkingRow()
    assert row.isHidden() and not row.running
    row.start("reading the screen")
    assert not row.isHidden() and row.running
    assert row.label.text() == "READING THE SCREEN"
    row.set_label("writing the file")
    assert row.label.text() == "WRITING THE FILE"
    row.stop()
    assert row.isHidden() and not row.running


# ---- the status strip -------------------------------------------------------

def test_the_strip_names_the_state_and_only_offers_stop_while_working(qt):
    strip = StatusStrip()
    strip.set_state("asleep", "")
    assert strip.word.text() == "ASLEEP"
    assert strip.stop_btn.isHidden(), "nothing is running, so nothing is stoppable"
    strip.set_state("working", "reading the screen")
    assert strip.word.text() == "WORKING"
    assert strip.detail.text() == "reading the screen"
    assert not strip.stop_btn.isHidden()
    strip.set_state("listening", "ready")
    assert strip.stop_btn.isHidden()


def test_stop_is_a_real_signal(qt):
    strip = StatusStrip()
    seen = []
    strip.stop_requested.connect(lambda: seen.append(True))
    strip.stop_btn.click()
    assert seen == [True]


def test_the_strip_keeps_the_model_and_the_microphone_apart(qt):
    strip = StatusStrip()
    strip.set_model("AUTO - quickest available")
    strip.set_mic(True)
    assert strip.model.text().startswith("AUTO")
    assert strip.mic.text() == "MIC ON"
    strip.set_mic(False)
    assert strip.mic.text() == "MIC OFF"


# ---- what a turn offers to do with itself -----------------------------------

def test_only_a_reply_can_be_read_out_and_only_a_request_can_be_retried(qt):
    reply = Turn("retro", "the answer")
    request = Turn("user", "the request")
    assert reply.speak_btn is not None and reply.retry_btn is None
    assert request.retry_btn is not None and request.speak_btn is None
    assert reply.copy_btn is not None and request.copy_btn is not None


def test_speak_and_retry_carry_the_text_of_that_turn(qt):
    reply = Turn("retro", "the answer")
    request = Turn("user", "make it better")
    spoken, retried = [], []
    reply.spoke.connect(spoken.append)
    request.retried.connect(retried.append)
    reply.speak_btn.click()
    request.retry_btn.click()
    assert spoken == ["the answer"]
    assert retried == ["make it better"]


def test_the_record_relays_the_two_actions(qt):
    talk = Conversation()
    spoken, retried = [], []
    talk.speak_requested.connect(spoken.append)
    talk.retry_requested.connect(retried.append)
    talk.add("user", "make it better")
    talk.add("retro", "it is better now")
    talk.turns[0].retry_btn.click()
    talk.turns[1].speak_btn.click()
    assert retried == ["make it better"]
    assert spoken == ["it is better now"]


# ---- the composer's history -------------------------------------------------

def test_up_walks_the_commands_and_down_gives_the_typing_back(qt):
    from PySide6.QtTest import QTest
    from ui.conversation import HistoryField

    field = HistoryField()
    field.remember("first task")
    field.remember("second task")
    assert field.history == ["first task", "second task"]

    field.setText("half typed")
    QTest.keyClick(field, Qt.Key.Key_Up)
    assert field.text() == "second task"
    QTest.keyClick(field, Qt.Key.Key_Up)
    assert field.text() == "first task"
    QTest.keyClick(field, Qt.Key.Key_Up)
    assert field.text() == "first task", "the oldest command is the end of the walk"
    QTest.keyClick(field, Qt.Key.Key_Down)
    assert field.text() == "second task"
    QTest.keyClick(field, Qt.Key.Key_Down)
    assert field.text() == "half typed", "the draft comes back, not an empty field"


def test_the_same_command_twice_is_remembered_once_and_the_list_is_capped(qt):
    from ui.conversation import HistoryField

    field = HistoryField()
    field.remember("same")
    field.remember("same")
    assert field.history == ["same"]
    for number in range(HistoryField.KEEP + 5):
        field.remember(f"task {number}")
    assert len(field.history) == HistoryField.KEEP
    assert field.history[-1] == f"task {HistoryField.KEEP + 4}"


def test_an_empty_command_is_not_kept(qt):
    from ui.conversation import HistoryField

    field = HistoryField()
    field.remember("   ")
    assert field.history == []


# ---- the microphone meter ---------------------------------------------------

def test_the_meter_is_hidden_until_the_microphone_is_on(qt):
    strip = StatusStrip()
    strip.set_mic(False)
    assert strip.meter.isHidden()
    strip.set_mic(True)
    assert not strip.meter.isHidden()


def test_the_meter_follows_the_level_and_settles_back_to_rest(qt):
    from ui.conversation import LevelMeter

    meter = LevelMeter()
    assert meter._level == 0.0
    meter.set_level(0.9)
    for _ in range(40):
        meter._ease()
    assert meter._level > 0.8
    meter.set_level(0.0)
    for _ in range(80):
        meter._ease()
    assert meter._level == 0.0
    assert meter.width() > 0 and meter.height() > 0


def test_a_wild_level_cannot_break_the_meter(qt):
    from ui.conversation import LevelMeter

    meter = LevelMeter()
    meter.set_level("not a number")
    meter.set_level(9)
    assert meter._target == 1.0
