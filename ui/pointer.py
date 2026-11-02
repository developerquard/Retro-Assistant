"""Retro's own pointer, drawn on screen: the thing the user watches it click with.

The point of a virtual mouse is that it is *visible* while the user's real cursor
stays wherever they left it. So this is a frameless, click-through window the size
of the screen that draws one small marker - a ring with a dot, and a ripple when
it clicks - and then fades away when Retro stops pointing at anything.

Two details make it safe to leave on screen: the window answers every input event
(WindowTransparentForInput) and never takes focus, so the user's clicks and keys
still go exactly where they were going; and the position it draws at is the one
core/pointer.py holds in physical pixels, converted once here through the screen's
device pixel ratio - because a display scaled to 125% draws in logical pixels
that are smaller than the physical ones screenshots and window rectangles use.
"""
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from core import ui_hook

# How long the marker stays after the last thing Retro did, and how long a move
# takes to glide to. Short on purpose: a leftover pointer on screen looks like a
# second mouse, and the whole point is that there is only one.
FADE_MS = 1400
GLIDE_MS = 260
RIPPLE_MS = 460

ACCENT = QColor(0x3A, 0xDF, 0x9B)      # the same green the circle draws


class PointerOverlay(QWidget):
    """The marker. Hidden until Retro points, fading out when it stops."""

    def __init__(self, parent=None):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Retro Pointer")

        self._target = None          # logical point the pointer is heading to
        self._shown = None           # logical point currently drawn (animated)
        self._ripple = None          # (logical point, monotonic ms)
        self._screen = None

        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.setInterval(FADE_MS)
        self._fade.timeout.connect(self.hide_marker)

        self._tick = QTimer(self)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._step)
        self.hide()

    # ---------- the channel from the core ---------------------------------

    def on_event(self, event):
        """Subscribed to core.ui_hook; ignores anything that is not a pointer event."""
        if not isinstance(event, tuple) or not event or event[0] != "pointer":
            return
        kind = str(event[1])
        x, y = event[2], event[3]
        if kind == "hide":
            self.hide_marker()
            return
        self.point_at(x, y, ripple=(kind == "click"))

    # ---------- drawing ----------------------------------------------------

    def _logical(self, x, y):
        """Physical screen pixels -> this widget's logical coordinates."""
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return QPointF(float(x), float(y))
        self._screen = screen
        ratio = screen.devicePixelRatio()
        origin = screen.geometry().topLeft()
        return QPointF(origin.x() + x / ratio, origin.y() + y / ratio)

    def point_at(self, x, y, ripple=False):
        """Shows the marker at a physical point, gliding there if it was visible."""
        target = self._logical(x, y)
        self._target = target
        if self._shown is None or not self.isVisible():
            self._shown = QPointF(target)
        if ripple:
            import time
            self._ripple = (QPointF(target), time.monotonic() * 1000.0)
        if not self.isVisible():
            self.setGeometry(self._screen_rect())
            self.show()
            self.raise_()
        self._fade.start()
        self._tick.start()
        self.update()

    def hide_marker(self):
        self._tick.stop()
        self._ripple = None
        self._shown = None
        self._target = None
        self.hide()

    def _screen_rect(self):
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return self.geometry()
        return screen.geometry()

    def _step(self):
        """One animation frame: glide towards the target, retire the ripple."""
        import time
        moved = False
        if self._target is not None and self._shown is not None:
            delta = QPointF(self._target - self._shown)
            if abs(delta.x()) + abs(delta.y()) < 1.0:
                self._shown = QPointF(self._target)
            else:
                # A constant fraction of what is left reads as a quick, sure move
                # rather than a slow drift, and needs no easing curves.
                self._shown += delta * 0.35
                moved = True
        if self._ripple is not None:
            if time.monotonic() * 1000.0 - self._ripple[1] > RIPPLE_MS:
                self._ripple = None
            moved = True
        if not moved:
            self._tick.stop()
        self.update()

    def paintEvent(self, _event):
        if self._shown is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = self._shown

        if self._ripple is not None:
            import time
            age = min(1.0, (time.monotonic() * 1000.0 - self._ripple[1]) / RIPPLE_MS)
            radius = 10 + 26 * age
            colour = QColor(ACCENT)
            colour.setAlpha(int(150 * (1.0 - age)))
            pen = QPen(colour, 2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(centre.x() - radius, centre.y() - radius,
                                       radius * 2, radius * 2))

        glow = QRadialGradient(centre, 16)
        glow.setColorAt(0.0, QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 120))
        glow.setColorAt(1.0, QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(centre.x() - 16, centre.y() - 16, 32, 32))

        painter.setPen(QPen(QColor(255, 255, 255, 235), 1.6))
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(QRectF(centre.x() - 7, centre.y() - 7, 14, 14))
        painter.setBrush(QColor(255, 255, 255, 245))
        painter.drawEllipse(QRectF(centre.x() - 2.0, centre.y() - 2.0, 4.0, 4.0))
        painter.end()

    def closeEvent(self, event):
        self._tick.stop()
        self._fade.stop()
        super().closeEvent(event)


def attach():
    """Creates the overlay and wires it to the core. Returns (overlay, unsubscribe)."""
    overlay = PointerOverlay()
    unsubscribe = ui_hook.subscribe(overlay.on_event)
    return overlay, unsubscribe
