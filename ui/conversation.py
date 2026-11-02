"""The talking half of the panel: the conversation, and the line that says what Retro
is doing.

The panel used to keep the conversation in a single `QLabel` that every new reply
overwrote, so asking a second question erased the answer to the first. This module is
the record instead:

* **Turn** - one message, as a card: who said it, when, what was said, and what can be
  done with it - `COPY` always, `SPEAK` on a reply, `RETRY` on a request. Text is set
  as plain text, so a reply containing `<` is a reply containing `<` and never markup.
* **Conversation** - the scroll of turns, the empty state that suggests three real
  commands, and the working row with its ticking seconds. It owns the turn list, the
  cap that keeps it from growing forever, and the scroll position.
* **StatusStrip** - the state dot, the live detail, the model, the microphone and the
  five-bar meter beside it, and the STOP button that only exists while a task runs.
* **HistoryField** - the composer, remembering the commands that were sent.

Nothing here reaches the session: the window feeds these widgets and listens for the
signals (`suggested`, `speak_requested`, `retry_requested`, `stop_requested`) that mean
"the person pressed something".
"""
import time

from PySide6.QtCore import QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import theme

MAX_TURNS = 40
# The empty state's three chips are whole commands, not prompts: each one is handled
# by the session with no model involved, so they work on a machine whose key is wrong
# and still show what the panel can do.
SUGGESTIONS = (
    "What can you do?",
    "Play your intro",
    "Open your settings",
)

ROLE_LABEL = {"user": "YOU", "retro": "RETRO"}


class HistoryField(QLineEdit):
    """The composer, with the last commands one keypress away.

    A terminal where every command has to be typed again is a terminal people stop
    using. Up and Down walk the session's own commands, and the text being written
    when the walk starts is put back when it ends - so browsing the history never
    loses a half-typed sentence.
    """

    KEEP = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self._at = None             # None means "writing something new"
        self._draft = ""
        self._moving = False
        self.textEdited.connect(self._typed)

    def remember(self, text):
        """Keep a command that was actually sent."""
        text = str(text or "").strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
            del self._history[:-self.KEEP]
        self._at = None
        self._draft = ""

    @property
    def history(self):
        return list(self._history)

    def _typed(self, _text):
        if not self._moving:
            self._at = None

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Up and self._history:
            self._step(-1)
            return
        if key == Qt.Key.Key_Down and self._at is not None:
            self._step(1)
            return
        super().keyPressEvent(event)

    def _step(self, delta):
        if self._at is None:
            self._draft = self.text()
            self._at = len(self._history)
        self._at = max(0, min(len(self._history), self._at + delta))
        text = (self._history[self._at] if self._at < len(self._history)
                else self._draft)
        self._moving = True
        try:
            self.setText(text)
        finally:
            self._moving = False
        self.setCursorPosition(len(self.text()))


class Turn(QFrame):
    """One message in the record, with the thing you would want to do to it.

    `COPY` on both, because a good answer is worth keeping. On Retro's own turns there
    is `SPEAK`, which reads that answer out again - the voice is how Retro usually
    talks, and an answer read in silence is one you have to read. On a person's turn
    there is `RETRY`, which sends the same request again; that is what you want when
    the model's answer was weak, and it used to mean retyping the whole thing.
    """

    spoke = Signal(str)
    retried = Signal(str)

    def __init__(self, role, text, tone="normal", parent=None):
        super().__init__(parent)
        self.role = role if role in ROLE_LABEL else "retro"
        self.tone = str(tone or "normal")
        if self.tone == "error":
            self.setObjectName("turnError")
        else:
            self.setObjectName("turnUser" if self.role == "user" else "turnRetro")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setStyleSheet("")          # the object name carries the styling

        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 7, 9, 9)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        who = QLabel(ROLE_LABEL[self.role])
        who.setObjectName("role")
        when = QLabel(time.strftime("%H:%M"))
        when.setObjectName("role")
        head.addWidget(who)
        head.addWidget(when)
        head.addStretch(1)
        self.copy_btn = QPushButton("COPY")
        self.copy_btn.setObjectName("turnAction")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Copy this message")
        self.copy_btn.setAccessibleName(f"Copy the {ROLE_LABEL[self.role].lower()} message")
        self.copy_btn.clicked.connect(lambda _checked=False: self.copy())
        self.speak_btn = None
        self.retry_btn = None
        if self.role == "retro":
            self.speak_btn = self._action("SPEAK", "Read this answer out again",
                                          lambda: self.spoke.emit(self.text()))
            head.addWidget(self.speak_btn)
        else:
            self.retry_btn = self._action("RETRY", "Send this request again",
                                          lambda: self.retried.emit(self.text()))
            head.addWidget(self.retry_btn)
        head.addWidget(self.copy_btn)
        layout.addLayout(head)

        # A reply is somebody else's text: it is shown, never interpreted.
        self.body = QLabel(str(text or ""))
        self.body.setObjectName("body")
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.body)

    @staticmethod
    def _action(label, tip, slot):
        button = QPushButton(label)
        button.setObjectName("turnAction")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.clicked.connect(lambda _checked=False: slot())
        return button

    def text(self):
        return self.body.text()

    def copy(self):
        """Put this message on the clipboard and say so for a moment."""
        QApplication.clipboard().setText(self.body.text())
        self.copy_btn.setText("COPIED")
        QTimer.singleShot(1200, self._relabel)

    def _relabel(self):
        try:
            self.copy_btn.setText("COPY")
        except RuntimeError:
            pass                        # the turn was dropped from the cap meanwhile


class LevelMeter(QWidget):
    """Five bars that move while the microphone hears something.

    The state word says the microphone is on; this says it is *hearing*. Without it, a
    microphone that the operating system has muted looks exactly like one waiting for
    a voice, and the person talks louder at a machine that cannot hear them.
    """

    BARS = 5
    WIDTH = 4
    GAP = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.BARS * self.WIDTH + (self.BARS - 1) * self.GAP, 12)
        self.setToolTip("How much the microphone is hearing")
        self._level = 0.0
        self._target = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._ease)

    def set_level(self, value):
        try:
            self._target = max(0.0, min(1.0, float(value or 0.0)))
        except (TypeError, ValueError):
            self._target = 0.0
        if not self._timer.isActive():
            self._timer.start()

    def _ease(self):
        # Up quickly, down slowly: speech is spiky, and a meter that snaps to zero
        # between syllables reads as broken rather than as listening.
        step = 0.55 if self._target > self._level else 0.18
        self._level += (self._target - self._level) * step
        if self._level < 0.01 and self._target < 0.01:
            self._level = 0.0
            self._timer.stop()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        height = self.height()
        for index in range(self.BARS):
            # The bars are a level, not a waveform: each one needs a little more of
            # the signal than the one before it before it lights.
            share = (index + 1) / (self.BARS + 0.6)
            lit = self._level >= share
            colour = theme.ACCENT if lit else theme.TEXT_DIMMER
            if lit:
                colour = QColor(colour)
                colour.setAlpha(150 + int(105 * min(1.0, self._level)))
            bar = QRectF(index * (self.WIDTH + self.GAP),
                         (height - 8) / 2 if not lit else (height - 10) / 2,
                         self.WIDTH, 8 if not lit else 10)
            painter.setBrush(colour)
            painter.drawRoundedRect(bar, 1.5, 1.5)


class WorkingRow(QFrame):
    """A task is running, and here is how long it has been running.

    The orb already shows *that* something is happening; this shows it in the place
    the person is looking, next to the answer that has not arrived. The seconds are
    the honest part: a spinner alone cannot tell a 4-second answer from a stuck one.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("working")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)
        self.label = QLabel("WORKING")
        self.label.setObjectName("workingText")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.clock = QLabel("")
        self.clock.setObjectName("workingText")
        layout.addWidget(self.clock)
        self._started = 0.0
        self._label = "WORKING"
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self, label="WORKING"):
        self._label = " ".join(str(label or "WORKING").split()).upper()[:48]
        self._started = time.monotonic()
        self.label.setText(self._label)
        self.clock.setText("0s")
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def set_label(self, text):
        """Name the step that is running, without restarting the clock."""
        if not self._timer.isActive():
            return
        self._label = " ".join(str(text or "").split()).upper()[:48] or self._label
        self.label.setText(self._label)

    @property
    def running(self):
        return self._timer.isActive()

    def _tick(self):
        seconds = int(max(0.0, time.monotonic() - self._started))
        self.clock.setText(f"{seconds}s")


class StatusStrip(QWidget):
    """One line telling the truth about the session, in four parts."""

    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("strip")
        # Two short lines, not one long one: the state and the live detail on top, the
        # things that rarely change (model, microphone) underneath. On one line the
        # detail was squeezed to "reading the scr" the moment STOP appeared.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(7)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        bottom.addStretch(1)
        self.dot = QLabel("")
        self.dot.setObjectName("stateDot")
        self.dot.setFixedSize(8, 8)
        self.word = QLabel("OFFLINE")
        self.word.setObjectName("stateWord")
        self.detail = QLabel("")
        self.detail.setObjectName("stateDetail")
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setToolTip("Stop the running task")
        self.stop_btn.clicked.connect(lambda _checked=False: self.stop_requested.emit())
        self.stop_btn.hide()
        self.model = QLabel("")
        self.model.setObjectName("stateMeta")
        self.mic = QLabel("")
        self.mic.setObjectName("stateMeta")
        top.addWidget(self.dot)
        top.addWidget(self.word)
        top.addWidget(self.detail, 1)
        top.addWidget(self.stop_btn)
        separator = QLabel("\u00b7")
        separator.setObjectName("stateMeta")
        self.meter = LevelMeter()
        self.meter.hide()
        bottom.addWidget(self.model)
        bottom.addWidget(separator)
        bottom.addWidget(self.mic)
        bottom.addWidget(self.meter)
        layout.addLayout(top)
        layout.addLayout(bottom)
        self.set_state("offline")

    def set_state(self, state, detail=None):
        state = str(state or "offline").lower()
        colour = theme.STATE_COLOURS.get(state, theme.GREY)
        self.dot.setStyleSheet(
            f"background: {colour.name()}; border-radius: 4px;")
        self.word.setText(state.upper())
        self.word.setStyleSheet(
            f"font-size: 9px; font-weight: 700; letter-spacing: 1.8px;"
            f" color: {colour.name()};")
        if detail is not None:
            self.set_detail(detail)
        self.stop_btn.setVisible(state == "working")

    def set_detail(self, text):
        text = " ".join(str(text or "").split())
        self.detail.setText(text[:90])
        self.detail.setToolTip(text if len(text) > 90 else "")

    def set_model(self, name):
        name = " ".join(str(name or "").split())
        self.model.setText(name[:28] if name else "")
        self.model.setToolTip(name)

    def set_level(self, value):
        self.meter.set_level(value)

    def set_mic(self, on):
        if not on:
            # A meter that keeps its last bars while the microphone is off is a meter
            # that lies: it says "hearing you" about a closed microphone.
            self.meter.set_level(0)
        self.meter.setVisible(bool(on))
        self.mic.setText("MIC ON" if on else "MIC OFF")
        self.mic.setStyleSheet(
            "color: %s; font-size: 9px; letter-spacing: 0.8px;"
            % (theme.TEXT_DIM.name() if on else theme.TEXT_DIMMER.name()))


class Conversation(QWidget):
    """The scroll of turns, its empty state, and the working row."""

    suggested = Signal(str)
    speak_requested = Signal(str)
    retry_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(84)
        self.turns = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("transcript")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(self.scroll)

        self.stack = QWidget()
        self.stack.setStyleSheet("background: transparent;")
        column = QVBoxLayout(self.stack)
        column.setContentsMargins(0, 2, 2, 2)
        column.setSpacing(8)

        self.empty = self._build_empty()
        column.addWidget(self.empty, 1)

        self.working = WorkingRow(self.stack)
        column.addWidget(self.working)

        self._messages = QVBoxLayout()
        self._messages.setContentsMargins(0, 0, 0, 0)
        self._messages.setSpacing(8)
        column.addLayout(self._messages)
        column.addStretch(1)

        self.scroll.setWidget(self.stack)

    def _build_empty(self):
        """The first screen: what this is, and three things that work right now."""
        box = QWidget()
        # The empty state takes the whole record area and centres itself in it: a
        # first run with three chips pinned to the top and 300px of nothing under them
        # looked like a page that had failed to load.
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(2, 10, 2, 4)
        layout.setSpacing(6)
        layout.addStretch(1)
        title = QLabel("ASK RETRO ANYTHING")
        title.setObjectName("emptyTitle")
        self.empty_body = QLabel("Type below, or say \u201cRetro\u201d. A few that work now:")
        self.empty_body.setObjectName("emptyBody")
        self.empty_body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.empty_body)
        self.chips = []
        for text in SUGGESTIONS:
            chip = QPushButton(text)
            chip.setObjectName("chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setAccessibleName(f"Ask Retro: {text}")
            chip.setMaximumWidth(250)
            chip.clicked.connect(lambda _checked=False, value=text: self.suggested.emit(value))
            layout.addWidget(chip, 0, Qt.AlignmentFlag.AlignLeft)
            self.chips.append(chip)
        layout.addStretch(1)
        return box

    # ---- what the window calls -----------------------------------------

    def add(self, role, text, tone="normal"):
        """Append one turn, hide the empty state, and follow it to the bottom."""
        text = str(text or "").strip()
        if not text:
            return None
        turn = Turn(role, text, tone=tone, parent=self.stack)
        turn.spoke.connect(self.speak_requested)
        turn.retried.connect(self.retry_requested)
        self._messages.addWidget(turn)
        self.turns.append(turn)
        self.empty.setVisible(False)
        while len(self.turns) > MAX_TURNS:
            oldest = self.turns.pop(0)
            oldest.setParent(None)
            oldest.deleteLater()
        self._to_bottom()
        return turn

    def set_working(self, on, label="WORKING"):
        """The ticking row for a task in flight."""
        if on:
            self.working.start(label)
            self.empty.setVisible(False)
            self._to_bottom()
        else:
            self.working.stop()
            self.empty.setVisible(not self.turns)

    def set_progress(self, text):
        """The running row follows what the session says it is doing."""
        self.working.set_label(text)

    def set_compact(self, on):
        """Less room means less furniture: the suggestion chips give way, the one line
        that explains the panel does not."""
        on = bool(on)
        for chip in getattr(self, "chips", []):
            chip.setVisible(not on)

    def clear(self):
        """Start the record again, and put the suggestions back."""
        for turn in self.turns:
            turn.setParent(None)
            turn.deleteLater()
        self.turns = []
        self.working.stop()
        self.empty.setVisible(True)

    def _to_bottom(self):
        # After the layout has run, or the maximum is still the old one and the newest
        # turn stays below the fold - the one place a chat must never be.
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        try:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            pass


__all__ = ["Conversation", "Turn", "StatusStrip", "WorkingRow", "LevelMeter",
           "HistoryField", "MAX_TURNS", "SUGGESTIONS"]
