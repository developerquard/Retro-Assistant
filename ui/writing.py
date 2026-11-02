"""The writing stage: the document, appearing while it is being written.

The complaint that produced this widget was that a long job was invisible. Ask for a
document and the circle sat there being green, nothing was said for a minute, and the
whole thing arrived at the end as one lump. The ring shows *that* something is
happening; this shows *what*, and it shows it growing.

So it is a card under the circle that is only on screen while a document is being
written, and everything on it is a fact about the writing:

* the title the outline produced;
* the section that is being written now, and how many there are, with a bar that
  fills as each one finishes;
* the last few lines of the text, the oldest dimmed and the newest bright, so the eye
  follows the leading edge of the prose rather than the block;
* a live word count, and the file it landed in once it is saved.

Nothing here pulses to please the eye: the only thing that moves on its own is the
caret at the end of the newest line and a slow breath on the document's dot, and
those are drawn from the clock the same way the ring is - a dropped frame shortens
them rather than stalling them. When the job ends, the finished card stays long
enough to be read and then folds itself away, so the panel goes back to being a
circle and a prompt.

There is a third ending now, and it is the one the user actually asked for: a
*stopped* document. Stopping used to throw the work away; the sections that were
finished are kept as a draft, and the card says so - PAUSED, in amber, with the word
count that was kept and the words that bring it back - instead of vanishing and
leaving the user to wonder whether the whole thing is gone. When no document is being
written at all, the same strip under the circle holds what Retro has already written
(ui/writing.py, `DocumentShelf`), so a document does not have to be remembered by
name in order to be found again.
"""
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import theme

HEIGHT = 108
LINES = 4                     # how many lines of prose are on screen at once
FOLD_AFTER_MS = 9000          # how long a finished document stays before folding away
SHELF_HEIGHT = 30             # the strip of already-written documents
SHELF_ITEMS = 3               # how many of them fit without becoming a file manager


def words_of(text):
    return len([word for word in str(text or "").split() if any(ch.isalnum() for ch in word)])


class WritingStage(QWidget):
    """A hand-drawn card: the title, the section, and the prose as it arrives."""

    # Emitted when the card puts itself away. The shelf that replaces it needs to know:
    # the strip under the circle holds a document being written *or* the documents that
    # were, and the handover happens on the card's own timer.
    folded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumHeight(HEIGHT)
        self.setMaximumHeight(HEIGHT)
        self._title = ""
        self._heading = ""
        self._index = 0
        self._total = 0
        self._text = ""
        self._words = 0
        self._file = ""
        self._live = False
        self._done = False
        self._paused = False
        self._started = 0.0
        self._fold = QTimer(self)
        self._fold.setSingleShot(True)
        self._fold.setInterval(FOLD_AFTER_MS)
        self._fold.timeout.connect(self._fold_away)
        self.hide()

    # ---------- what the session tells it -----------------------------------

    def begin(self, title="", total=0, words=0, done=0):
        """A document has started. The card comes out and shows the current section.

        `done` is how many sections were already written when it began - non-zero only
        when a stopped document is picked back up, in which case the bar and the arc
        start where the first sitting left them rather than at nothing.
        """
        self._title = " ".join(str(title or "").split())[:80] or "A document"
        self._total = int(total or 0)
        self._index = max(0, int(done or 0))
        self._heading = ""
        self._text = ""
        self._words = 0
        self._file = ""
        self._live = True
        self._done = False
        self._paused = False
        self._started = time.monotonic()
        self._fold.stop()
        self.show()
        self.update()

    def section(self, index=0, total=0, heading=""):
        """One section is starting: its words are cleared so the prose below is it."""
        if total:
            self._total = int(total)
        self._index = int(index or self._index)
        self._heading = " ".join(str(heading or "").split())[:50]
        self._text = ""
        self.update()

    def section_done(self, index=0, total=0, words=0):
        if total:
            self._total = int(total)
        if index:
            self._index = int(index)
        if words:
            self._words += int(words)
        self.update()

    def writing(self, text=""):
        """The section as it stands. Only the tail is kept: this card is not a page."""
        self._text = str(text or "")
        self.update()

    def finish(self, title="", words=0, file=""):
        """The document is saved. The card stays, marked done, then folds away."""
        if title:
            self._title = " ".join(str(title).split())[:80]
        if words:
            self._words = int(words)
        self._file = str(file or "")
        self._live = False
        self._done = True
        self._index = self._total or self._index
        self.update()
        self._fold.start()

    def pause(self, title="", done=0, total=0, words=0):
        """The document was stopped, and its finished sections were kept as a draft.

        The card does not vanish, because vanishing is exactly what made a stop look
        like a lost document: it stays, amber, marked PAUSED, with the words that were
        kept and the phrase that brings the rest of them back. It folds away on the
        usual timer like a finished one - it is a notice, not a permanent fixture.
        """
        if title:
            self._title = " ".join(str(title).split())[:80]
        self._total = int(total or self._total)
        self._index = int(done or self._index)
        self._words = int(words or self._words)
        self._heading = ""
        self._text = ""
        self._file = ""
        self._live = False
        self._done = False
        self._paused = True
        self.update()
        self._fold.start()

    def reset(self):
        """Out of the way: the panel goes back to a circle and a prompt.

        Everything about the document is forgotten, not just hidden - a card that kept
        its section count after being put away would put a stale arc back on the ring
        the next time anything asked it for progress.
        """
        self._fold.stop()
        self._live = False
        self._done = False
        self._paused = False
        self._text = ""
        self._title = ""
        self._heading = ""
        self._file = ""
        self._words = 0
        self._index = 0
        self._total = 0
        self.hide()

    def _fold_away(self):
        if not self._live:
            self.reset()
            self.folded.emit()

    @property
    def live(self):
        return self._live

    @property
    def paused(self):
        return self._paused

    @property
    def showing(self):
        """True while the card has something to say: writing, stopped, or just saved.

        The card puts itself away on its own timer rather than when the document ends,
        which is why the window asks this instead of asking whether anything is running -
        a finished document used to disappear the instant it was saved.
        """
        return self._live or self._paused or self._done

    @property
    def title(self):
        return self._title

    def progress(self):
        """How far through, 0..1, or None when nothing counted the sections."""
        if not self._total:
            return None
        return max(0.0, min(1.0, self._index / float(self._total)))

    # ---------- painting ----------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(card, 12, 12)
        painter.fillPath(path, theme.CARD)
        painter.setPen(QPen(theme.CARD_EDGE, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        accent = theme.AMBER if self._paused else (theme.GREEN if self._live else theme.ACCENT)
        now = time.monotonic()
        # The left edge is the document's spine: it breathes while writing, turns amber
        # when the writing was stopped and the draft kept, and blue once the file is
        # saved - so the card says which of the three it is without a word of label.
        spine = QColor(accent)
        spine.setAlphaF(0.85 if self._live else 0.45)
        pen = QPen(spine)
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(card.left() + 3.5, card.top() + 12, card.left() + 3.5, card.bottom() - 12)

        left = card.left() + 14
        right = card.right() - 12
        self._header(painter, left, right, card.top() + 6, accent, now)
        self._bar(painter, left, right, card.top() + 30, accent)
        body_top = card.top() + 40
        self._prose(painter, left, right, body_top, card.top() + 82, now)
        self._footer(painter, left, right, card.bottom() - 18, accent)

    def _header(self, painter, left, right, top, accent, now):
        font = QFont(painter.font())
        font.setPixelSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.3)
        painter.setFont(font)
        # The dot breathes on the clock; every other mark on this card is text.
        glow = QColor(accent)
        glow.setAlphaF(0.35 if (self._done or self._paused)
                       else 0.45 + 0.35 * abs((now * 0.9) % 2.0 - 1.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(left, top + 4, 6, 6))

        ink = QColor(accent if self._paused else theme.TEXT_DIMMER)
        painter.setPen(QPen(ink))
        painter.drawText(QRectF(left + 12, top, 120, 14),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         "PAUSED" if self._paused else ("SAVED" if self._done else "WRITING"))

        if self._total:
            label = (f"SECTION {min(self._index, self._total)} OF {self._total}"
                     if not self._done else f"{self._total} SECTIONS")
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(QRectF(right - 190, top, 190, 14),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             label)

        title = QFont(painter.font())
        title.setPixelSize(12)
        title.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        title.setBold(True)
        painter.setFont(title)
        painter.setPen(QPen(QColor(theme.INK_BRIGHT)))
        metrics = QFontMetrics(title)
        room = int(right - left - 14)
        painter.drawText(QRectF(left + 12, top + 14, room, 16),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         metrics.elidedText(self._title, Qt.TextElideMode.ElideRight, room))

    def _bar(self, painter, left, right, top, accent):
        """The sections that are finished, as a bar - not a guess and not a timer."""
        width = max(1.0, right - left)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(theme.CARD_EDGE)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(left, top, width, 2.0), 1.0, 1.0)
        fraction = self.progress()
        if fraction is None:
            fraction = 1.0 if self._done else 0.0
        if fraction <= 0:
            return
        head = QColor(accent)
        painter.setBrush(head)
        painter.drawRoundedRect(QRectF(left, top, max(2.0, width * fraction), 2.0), 1.0, 1.0)
        glow = QColor(accent)
        glow.setAlphaF(0.35)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(left + width * fraction - 3.0, top - 2.0, 7.0, 7.0))

    def _prose(self, painter, left, right, top, bottom, now):
        """The tail of the section, oldest line dimmest, with a caret on the newest."""
        text = self._text.strip()
        if not text and self._heading:
            text = f"{self._heading}…"
        elif not text and self._paused:
            text = "The sections that were finished are saved as a draft."
        font = QFont("Consolas") if QFont("Consolas").exactMatch() else QFont(painter.font())
        font.setPixelSize(10)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        room = int(right - left)
        line_height = 12.0
        lines = self._wrap(metrics, room, text)[-LINES:]
        base = bottom - line_height * len(lines)
        for index, line in enumerate(lines):
            newest = index == len(lines) - 1
            ink = QColor(theme.INK_BRIGHT if newest else theme.TEXT_DIM)
            ink.setAlphaF(0.95 if newest else 0.42 + 0.16 * index)
            painter.setPen(QPen(ink))
            y = base + line_height * index
            painter.drawText(QRectF(left, y, room, line_height),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             line)
        if self._live and lines:
            caret = QColor(theme.GREEN)
            caret.setAlphaF(0.85 if (now * 2.0) % 2.0 < 1.2 else 0.15)
            painter.setPen(QPen(caret))
            x = left + metrics.horizontalAdvance(lines[-1]) + 2
            y = base + line_height * (len(lines) - 1)
            painter.drawLine(int(x), int(y + 2), int(x), int(y + line_height - 1))

    @staticmethod
    def _wrap(metrics, room, text):
        """The text in lines that fit, keeping the tail rather than the head."""
        lines, current = [], ""
        for word in str(text).split():
            candidate = f"{current} {word}".strip()
            if current and metrics.horizontalAdvance(candidate) > room:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines or [""]

    def _footer(self, painter, left, right, top, accent):
        """The word count, and - while paused - the phrase that finishes the job."""
        font = QFont(painter.font())
        font.setPixelSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        painter.setFont(font)
        ink = QColor(accent if self._paused else (theme.TEXT_DIM if self._done else accent))
        painter.setPen(QPen(ink))
        painter.drawText(QRectF(left, top, 140, 14),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         f"{self._words} WORDS" + (" KEPT" if self._paused else ""))
        if self._paused:
            # The one thing a stopped document must say: the work is not lost, and this
            # is the phrase that finishes it.
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(QRectF(left + 150, top, max(20, right - left - 150), 14),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             "SAY \"CARRY ON WRITING\"")
        if self._file:
            painter.setPen(QPen(QColor(theme.TEXT_DIMMER)))
            room = int(right - left - 150)
            metrics = QFontMetrics(font)
            painter.drawText(QRectF(right - room, top, room, 14),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(self._file, Qt.TextElideMode.ElideLeft, room))


class DocumentShelf(QWidget):
    """What Retro has already written, in the strip the writing card uses.

    A document that has been written should not have to be remembered in order to be
    found again: the newest few are named here with their real word counts, and clicking
    one opens it. It is hand painted for the same reason the card above it is - a
    default Qt list beside a drawn circle reads as two different programs in one window.

    It is drawn only when no document is being written: the strip under the circle says
    what is happening now, or - when nothing is - what has been finished, never both.
    """

    open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumHeight(SHELF_HEIGHT)
        self.setMaximumHeight(SHELF_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._documents = []
        self._rects = []              # (name, QRectF) from the last paint
        self._hover = ""
        self.hide()

    # ---------- what the window tells it -------------------------------------

    def set_documents(self, documents):
        """The documents to offer, newest first. Anything without a name is dropped."""
        rows = []
        for row in (documents or []):
            if isinstance(row, dict) and str(row.get("name") or ""):
                rows.append({"name": str(row["name"]), "words": int(row.get("words") or 0)})
        self._documents = rows[:SHELF_ITEMS]
        if self._hover not in [row["name"] for row in self._documents]:
            self._hover = ""
        self.update()

    @property
    def documents(self):
        return [dict(row) for row in self._documents]

    def item_rects(self):
        """Where each document is drawn: so a test can click a real one."""
        return [(name, QRectF(rect)) for name, rect in self._rects]

    def hit(self, point):
        """The name of the document under this point, or \"\"."""
        for name, rect in self._rects:
            if rect.contains(point):
                return name
        return ""

    # ---------- the mouse ----------------------------------------------------

    def mouseMoveEvent(self, event):
        name = self.hit(event.position())
        if name != self._hover:
            self._hover = name
            self.update()

    def leaveEvent(self, _event):
        if self._hover:
            self._hover = ""
            self.update()

    def mousePressEvent(self, event):
        name = self.hit(event.position())
        if name and event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit(name)
            return
        event.ignore()               # a press that misses belongs to the window, to drag it

    # ---------- painting ------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(card, 9, 9)
        painter.fillPath(path, theme.CARD)
        painter.setPen(QPen(theme.CARD_EDGE, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        font = QFont(painter.font())
        font.setPixelSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.3)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_DIMMER)))
        painter.drawText(QRectF(card.left() + 12, card.top(), 84, card.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         "DOCUMENTS")

        self._rects = []
        left = card.left() + 98
        room = max(1.0, card.right() - 10 - left)
        column = room / max(1, len(self._documents))
        for index, row in enumerate(self._documents):
            rect = QRectF(left + column * index, card.top() + 2,
                          max(24.0, column - 10), card.height() - 4)
            self._rects.append((row["name"], rect))
            self._item(painter, row, rect)

    def _item(self, painter, row, rect):
        hovered = row["name"] == self._hover
        if hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.CARD_GLOW))
            painter.drawRoundedRect(rect, 6, 6)
        font = QFont(painter.font())
        font.setPixelSize(10)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        label = f"{row['name'].rsplit('.', 1)[0]}  ·  {row['words']} words"
        painter.setPen(QPen(QColor(theme.INK_BRIGHT if hovered else theme.TEXT_DIM)))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         metrics.elidedText(label, Qt.TextElideMode.ElideRight,
                                            int(rect.width())))
