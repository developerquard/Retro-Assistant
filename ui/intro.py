"""The window the logo plays in: full screen, above everything, then gone.

`core/intro.py` holds the timeline and the soundtrack; this holds the picture. The
split is the same one the rest of the app uses - the drawing is Qt's business, the
timing and the sound are not - and it is what lets the timeline be tested with no
display at all.

How a frame is built, and why it is built this way:

* **Every letter is drawn once and then composited.** The five letters are stroked
  from a skeleton into a `QPixmap` when the splash opens, with a bloom and (for the
  second R) a thin outline pixmap beside it. A frame at thirty a second then does
  nothing but draw pixmaps, which is what keeps the reveal smooth on a laptop that is
  still starting up.
* **The glow is a real blur, not a shadow.** The bloom is the letter scaled right down
  and straight back up - the cheap, honest way to blur without a filter - then tinted
  through its own alpha. Two passes, a tight one and a wide dim one, which is what
  makes the letters look lit rather than outlined.
* **Nothing here is a video or a web view.** There is no Qt WebEngine in the build and
  there is not going to be: the sketch this came from was an HTML page, and the point
  of the exercise was to *not* play it in a browser.
* **The fade is the window's.** After the reveal the logo is simply held, and the
  window's opacity is taken to zero over three seconds - so what comes back is the
  desktop and Retro's own circle, which is behind this the whole time.
"""
import math
import threading

from PySide6.QtCore import QElapsedTimer, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from core import intro as plan
from core import log, settings, sound

FPS_MS = plan.FPS_MS
FADE_EDGE = 0.055          # how wide a wipe's bright edge is, in fractions of the letter


def _rng(seed):
    """A small deterministic generator, so a letter assembles the same way twice."""
    state = [seed & 0xFFFFFFFF]

    def next_value():
        state[0] = (state[0] * 1103515245 + 12345) & 0xFFFFFFFF
        return state[0] / 4294967296.0

    return next_value


def _ease_out(k):
    return 1.0 - (1.0 - k) ** 3


def _ease_in_out(k):
    return 4 * k * k * k if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2


def _between(t, start, end):
    if end <= start:
        return 1.0 if t >= end else 0.0
    return max(0.0, min(1.0, (t - start) / (end - start)))


def letter_path(key, width, height, stroke):
    """One letter's skeleton, stroked - the sketch's own construction.

    A flat-capped, mitred pen of one weight for every letter is what makes the five
    read as one wordmark; drawing filled shapes instead is what makes home-made
    lettering look like five different typefaces.
    """
    path = QPainterPath()
    half = stroke / 2.0
    if key == "E":
        for y, end in ((half, width), (height / 2.0, width * 0.88), (height - half, width)):
            path.moveTo(0.0, y)
            path.lineTo(end, y)
        path.moveTo(half, 0.0)
        path.lineTo(half, height)
    elif key == "T":
        path.moveTo(0.0, half)
        path.lineTo(width, half)
        path.moveTo(width / 2.0, half)
        path.lineTo(width / 2.0, height)
    elif key == "R":
        bowl_h = height * 0.56
        radius = max(1.0, (bowl_h - stroke) / 2.0)
        top = half
        bottom = top + 2.0 * radius
        centre = width - half - radius
        path.moveTo(half, 0.0)
        path.lineTo(half, height)
        path.moveTo(half, top)
        path.lineTo(centre, top)
        path.arcTo(QRectF(centre - radius, top, 2.0 * radius, 2.0 * radius), 90.0, -180.0)
        path.lineTo(half, bottom)
        run_x = width * 0.80 - width * 0.40
        run_y = height - bottom
        length = max(1.0, math.hypot(run_x, run_y))
        reach = stroke * 1.6                 # the leg overshoots the baseline, as drawn
        path.moveTo(width * 0.40, bottom)
        path.lineTo(width * 0.80 + run_x / length * reach, height + run_y / length * reach)
    elif key == "O":
        inset = stroke / 2.0
        path.addEllipse(QRectF(inset, inset, width - stroke, height - stroke))
    return path


def letter_layout(screen_w, screen_h):
    """Where each letter sits, in logical pixels, on a screen of this size."""
    cap = min(screen_h * plan.CAP_OF_SCREEN, screen_w * plan.WORD_WIDE / 3.52)
    stroke = max(2.0, cap * plan.STROKE)
    widths = [entry["width"] * cap for entry in plan.LETTERS]
    gaps = [value * cap for value in plan.GAPS]
    left = (screen_w - (sum(widths) + sum(gaps))) / 2.0
    top = (screen_h - cap) / 2.0
    out = []
    for index, entry in enumerate(plan.LETTERS):
        out.append({"key": entry["key"], "color": entry["color"], "x": left, "y": top,
                    "w": widths[index], "h": cap})
        left += widths[index] + (gaps[index] if index < len(gaps) else 0.0)
    return {"cap": cap, "stroke": stroke, "letters": out}


def e_particles(spec, stroke):
    """The points the blue E assembles from: its own skeleton, sampled.

    Taken from the geometry rather than from the pixels, so it is exact, instant, and
    the same every time - and so a test can count them without a screen.
    """
    rand = _rng(11)
    width, height = spec["w"], spec["h"]
    half = stroke / 2.0
    segments = [((half, 0.0), (half, height)),
                ((0.0, half), (width, half)),
                ((0.0, height / 2.0), (width * 0.88, height / 2.0)),
                ((0.0, height - half), (width, height - half))]
    points = []
    per_segment = 70
    for (x0, y0), (x1, y1) in segments:
        run_x, run_y = x1 - x0, y1 - y0
        length = max(1.0, math.hypot(run_x, run_y))
        normal = (-run_y / length, run_x / length)
        for index in range(per_segment):
            along = (index + rand() * 0.7) / per_segment
            across = (rand() - 0.5) * stroke * 0.9
            target = QPointF(spec["x"] + x0 + run_x * along + normal[0] * across,
                             spec["y"] + y0 + run_y * along + normal[1] * across)
            angle = rand() * math.tau
            radius = spec["h"] * (0.35 + 0.75 * rand())
            start = QPointF(spec["x"] + width / 2.0 + math.cos(angle) * radius * 1.2,
                            spec["y"] + height / 2.0 + math.sin(angle) * radius)
            control = QPointF((start.x() + target.x()) / 2.0 + normal[0] * length * 0.18 * rand(),
                              (start.y() + target.y()) / 2.0 + normal[1] * length * 0.18 * rand())
            points.append({"from": start, "to": target, "via": control,
                           "delay": 0.16 * along + rand() * 0.14,
                           "duration": 0.40 + rand() * 0.14,
                           "size": max(1.0, stroke * (0.18 + 0.16 * rand())),
                           "tint": 0 if rand() < 0.65 else 1})
    return points


class Letter:
    """One drawn letter: its pixels, its bloom, and its outline where it has one."""

    def __init__(self, index, spec, stroke, pad, blur, dpr):
        self.index = index
        self.spec = spec
        self.color = QColor(spec["color"])
        self.pad = pad
        self.crisp = self._stroke(spec, stroke, 0.0, self.color, dpr)
        self.glow = self._bloom(self.crisp, self.color, dpr)
        self.outline = None
        if index == 3:
            # The second R wears a thin illuminated edge, and it is a ring: the crisp
            # letter is punched out of the wider stroke, so the fill inside it stays
            # the electric blue and only the rim lights up.
            ring = self._stroke(spec, stroke, stroke * 0.42, QColor("#8EC1FF"), dpr)
            painter = QPainter(ring)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            painter.drawPixmap(0, 0, self.crisp)
            painter.end()
            self.outline = ring
        self.particles = e_particles(spec, stroke) if index == 1 else []

    def _stroke(self, spec, stroke, extra, color, dpr):
        """The letter's own skeleton, stroked once, into its own pixmap."""
        width = int(round((spec["w"] + 2 * self.pad) * dpr))
        height = int(round((spec["h"] + 2 * self.pad) * dpr))
        pixmap = QPixmap(max(1, width), max(1, height))
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(dpr, dpr)
        painter.translate(self.pad, self.pad)
        pen = QPen(color)
        pen.setWidthF(stroke + 2 * extra)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(letter_path(spec["key"], spec["w"], spec["h"], stroke))
        painter.end()
        return pixmap

    def _bloom(self, crisp, color, dpr):
        """A soft halo: the letter shrunk and grown back, twice, then tinted."""
        size = crisp.size()
        halo = QPixmap(size)
        halo.fill(Qt.GlobalColor.transparent)
        halo.setDevicePixelRatio(dpr)
        painter = QPainter(halo)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for shrink, alpha in ((22, 0.34), (12, 0.55), (6, 0.85)):
            small = crisp.scaled(max(1, crisp.width() // shrink), max(1, crisp.height() // shrink),
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            painter.setOpacity(alpha)
            painter.drawPixmap(QRectF(0, 0, size.width() / dpr, size.height() / dpr),
                               small, QRectF(small.rect()))
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.fillRect(halo.rect(), color)
        painter.end()
        return halo


class IntroSplash(QWidget):
    """The splash itself. Shown full screen, finishes after the reveal and the fade.

    `finished` is emitted once, with True when the reveal and the fade both ran their
    course and False when it was cut short (Escape, or the hard cap).
    """

    finished = Signal(bool)

    def __init__(self, on_finished=None, with_sound=True, parent=None):
        super().__init__(parent)
        self._on_finished = on_finished
        self._with_sound = bool(with_sound)
        self._done = False
        self._ran_full = False
        self._frozen = None
        self._sound_thread = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle("Retro")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(self.desktop_rect())
        self._letters = []
        self._stars = self._ambient_stars()
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(FPS_MS)
        self._timer.timeout.connect(self._tick)
        self.rebuild()

    # ---------- geometry ------------------------------------------------------

    @staticmethod
    def desktop_rect():
        """The whole desktop, so the splash covers every screen and not just one."""
        from PySide6.QtGui import QGuiApplication
        width, height = 0, 0
        for screen in QGuiApplication.screens():
            rect = screen.geometry()
            width = max(width, rect.x() + rect.width())
            height = max(height, rect.y() + rect.height())
        if not width or not height:
            return QRect(0, 0, 1280, 800)
        return QRect(0, 0, width, height)

    def rebuild(self):
        """Draws the five letters once, at this size, before anything is shown.

        Called again by a resize: the letters are only ever rasterised at one size,
        and a frame that had to scale them would either be soft or be slow.
        """
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        dpr = 1.0
        if screen is not None:
            dpr = max(1.0, min(2.0, float(screen.devicePixelRatio() or 1.0)))
        self._dpr = dpr
        self._info = letter_layout(self.width(), self.height())
        stroke = self._info["stroke"]
        pad = self._info["cap"] * 0.34
        blur = max(2.0, self._info["cap"] * 0.02) + pad * 0.2
        self._pad = pad
        self._blur = blur
        self._letters = [Letter(index, spec, stroke, pad, blur, dpr)
                         for index, spec in enumerate(self._info["letters"])]
        self._bursts = [(0.00, 0, 0.74), (0.42, 1, 1.10), (0.98, 2, 0.92),
                        (1.40, 3, 1.04), (1.84, 4, 0.96)]

    def resizeEvent(self, event):
        """Letters are rasterised for one size only, so a new size means new pixels."""
        super().resizeEvent(event)
        if getattr(self, "_clock", None) is not None and self._letters is not None:
            self.rebuild()

    def showEvent(self, event):
        """Re-rasterise once the window has the size it will really have.

        Windows settles a full-screen frame a moment after `show()`. The letters are
        cached pixels, so one rasterised against the pre-show size arrived small and
        then scaled itself into place on the next frame - the O that jumped.
        """
        super().showEvent(event)
        QTimer.singleShot(0, self._settle_size)

    def _settle_size(self):
        if self._letters:
            self.rebuild()

    @staticmethod
    def _ambient_stars():
        """A faint field of drifting motes, so a black screen has some depth to it."""
        rand = _rng(9221)
        return [{"x": rand(), "y": rand(), "size": 0.6 + rand() * 1.8, "phase": rand() * 6.28,
                 "speed": 0.2 + rand() * 0.6, "tint": 0 if rand() < 0.6 else 1}
                for _ in range(78)]

    # ---------- playback ------------------------------------------------------

    def start(self):
        """Begins the reveal, with its soundtrack, and starts the clock on the sound.

        The picture and the music share one zero. The soundtrack is generated and
        played on another thread, and opening an output device - asking for it,
        negotiating its rate, starting WASAPI - takes a tenth of a second or more. So
        the clock is not started here: it is started by the first block that is
        really being played, and the black window waits for it. That is why the
        first chime now lands under the first letter instead of a beat behind it.
        """
        self.show()
        self.raise_()
        self.activateWindow()
        began = threading.Event()
        if self._with_sound:
            try:
                self._sound_thread = sound.play_async(plan.soundtrack(),
                                                      device=settings.get("output"),
                                                      started=began)
            except Exception as exc:               # no device, no library: still a picture
                log.event("intro_sound_failed", error=f"{type(exc).__name__}: {exc}"[:120])
        # Never wait on a device for longer than a blink: a machine with no output, or
        # one whose driver is still waking up, gets the picture on time and no sound.
        synced = began.wait(0.45) if self._with_sound else False
        self._clock.start()
        self._timer.start()
        QTimer.singleShot(int(round(plan.HARD_CAP_S * 1000)), lambda: self.finish(False))
        log.event("intro_started", seconds=plan.total_seconds(), sound=self._with_sound,
                  synced=synced)
        return self

    def render_at(self, t):
        """Freezes the picture at `t` seconds - for tests, previews and screenshots."""
        self._frozen = max(0.0, float(t))
        self.update()

    def elapsed(self):
        if self._frozen is not None:
            return self._frozen
        return self._clock.elapsed() / 1000.0 if self._clock.isValid() else 0.0

    def _tick(self):
        t = self.elapsed()
        if t >= plan.total_seconds():
            self.finish(True)
            return
        if t > plan.INTRO_S:
            # The reveal is over and the picture is held; everything left is the
            # window going away. Opacity, not a fade to black: the desktop is behind.
            fade = min(1.0, (t - plan.INTRO_S) / plan.FADE_S)
            self.setWindowOpacity(max(0.0, 1.0 - fade))
        self.update()

    def finish(self, ran_full=True):
        """Ends the splash now. Idempotent, so the cap and the last frame can race."""
        if self._done:
            return
        self._done = True
        self._ran_full = bool(ran_full)
        self._timer.stop()
        self._frozen = None
        self.setWindowOpacity(1.0)
        self.hide()
        log.event("intro_finished", ran_full=self._ran_full,
                  seconds=round(self.elapsed(), 2))
        sound.wait(self._sound_thread, 0.4)
        self.finished.emit(self._ran_full)
        if self._on_finished is not None:
            try:
                self._on_finished()
            except Exception as exc:               # the app must start whatever else happens
                log.event("intro_callback_failed", error=f"{type(exc).__name__}: {exc}"[:120])
        self.deleteLater()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.finish(False)
            return
        super().keyPressEvent(event)

    # ---------- painting ------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor("#000000"))
        t = self.elapsed()
        self._paint_stars(painter, t)
        if not self._letters:
            painter.end()
            return
        # No zoom on the wordmark, deliberately. The held letters used to be scaled by
        # two per cent across the hold, and scaling a cached pixmap by a fraction of a
        # per cent every frame is exactly what makes a sharp stroke crawl: that WAS the
        # "text dislocating" the owner saw. The letters stay at one size; the life in
        # the hold comes from light - the sweep, the rings, the bloom - which is drawn
        # fresh at any size, not resampled.
        state = plan.progress(t)
        for index, letter in enumerate(self._letters):
            self._paint_letter(painter, index, letter, t, state)
        self._paint_effects(painter, t, state)
        painter.end()

    def _paint_stars(self, painter, t):
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        width, height = float(self.width()), float(self.height())
        for star in self._stars:
            x = (star["x"] * width + t * 5.0 * star["speed"]) % width
            y = star["y"] * height + math.sin(t * 0.75 + star["phase"]) * 5.0
            pulse = 0.14 + 0.22 * (0.5 + 0.5 * math.sin(t * 1.4 + star["phase"]))
            painter.setOpacity(pulse * (0.7 if star["tint"] else 0.5))
            painter.fillRect(QRectF(x, y, star["size"], star["size"]),
                             QColor("#4B8DFF") if star["tint"] else QColor("#DDE7FF"))
        painter.restore()

    def _blit(self, painter, pixmap, spec, alpha, mode=None):
        if alpha <= 0.004:
            return
        painter.setOpacity(min(1.0, alpha))
        if mode is not None:
            painter.setCompositionMode(mode)
        # Whole device pixels. A cached letter drawn half a pixel off the grid is
        # resampled by the compositor on every frame, which softens the stroke and
        # makes its edge crawl - the owner's "the text dislocates sometimes". The
        # layout never lands on a half pixel now, and if it ever did, this rounds it.
        dpr = max(1.0, float(self._dpr))
        left = round((spec["x"] - self._pad) * dpr) / dpr
        top = round((spec["y"] - self._pad) * dpr) / dpr
        painter.drawPixmap(QPointF(left, top), pixmap)
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def _paint_letter(self, painter, index, letter, t, state):
        reveals = state["reveals"]
        settle = state["settle"]
        if index == 0:
            self._paint_first_r(painter, letter, t, reveals[0], settle)
        elif index == 1:
            self._paint_e(painter, letter, t, reveals[1], settle)
        elif index == 2:
            self._paint_t(painter, letter, t, reveals[2], settle)
        elif index == 3:
            self._paint_second_r(painter, letter, t, reveals[3], settle)
        else:
            self._paint_o(painter, letter, t, reveals[4], settle)

    def _glow_alpha(self, t, start, peak_at, base):
        """The halo of a settled letter: up fast, then down to a resting brightness."""
        if t < start:
            return 0.0
        rise = _ease_out(_between(t, start, peak_at))
        if t < peak_at:
            return 0.85 * rise
        return 0.85 + (base - 0.85) * _ease_in_out(_between(t, peak_at, peak_at + 1.1))

    def _paint_first_r(self, painter, letter, t, reveal, settle):
        spec = letter.spec
        if t < 1.9:                                  # it arrives through a wide soft light
            k = math.sin(math.pi * _between(t, 0.0, 1.9))
            if k > 0.01:
                centre = QPointF(spec["x"] + spec["w"] / 2.0, spec["y"] + spec["h"] / 2.0)
                radius = spec["h"] * 0.95
                gradient = QRadialGradient(centre, radius)
                gradient.setColorAt(0.0, QColor(245, 247, 255, int(60 * k)))
                gradient.setColorAt(1.0, QColor(245, 247, 255, 0))
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                painter.setBrush(gradient)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(centre, radius, radius)
                painter.restore()
        self._blit(painter, letter.glow, spec,
                   self._glow_alpha(t, 0.04, 0.30, 0.30 + settle),
                   QPainter.CompositionMode.CompositionMode_Plus)
        if reveal <= 0.0:
            return
        if reveal >= 1.0:
            self._blit(painter, letter.crisp, spec, 1.0)
            return
        # The first letter opens outward from its middle rather than fading in.
        centre = QPointF(spec["x"] + spec["w"] / 2.0, spec["y"] + spec["h"] / 2.0)
        radius = spec["h"] * (0.06 + 1.5 * _ease_out(reveal))
        clip = QPainterPath()
        clip.addEllipse(centre, radius, radius)
        painter.save()
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        self._blit(painter, letter.crisp, spec, _ease_out(_between(reveal, 0.0, 0.6)))
        painter.restore()

    def _paint_e(self, painter, letter, t, reveal, settle):
        spec = letter.spec
        self._blit(painter, letter.glow, spec,
                   self._glow_alpha(t, 0.42, 0.72, 0.30 + settle),
                   QPainter.CompositionMode.CompositionMode_Plus)
        self._blit(painter, letter.crisp, spec, _ease_in_out(_between(t, 0.78, 1.02)))
        self._paint_particles(painter, letter, t)

    def _paint_t(self, painter, letter, t, reveal, settle):
        spec = letter.spec
        if t < 0.98:
            return
        self._blit(painter, letter.glow, spec,
                   self._glow_alpha(t, 1.14, 1.34, 0.30 + settle),
                   QPainter.CompositionMode.CompositionMode_Plus)
        self._wipe(painter, letter, reveal, horizontal=True)

    def _paint_second_r(self, painter, letter, t, reveal, settle):
        spec = letter.spec
        if t < 1.40:
            return
        self._blit(painter, letter.glow, spec,
                   self._glow_alpha(t, 1.40, 1.70, 0.32 + settle),
                   QPainter.CompositionMode.CompositionMode_Plus)
        self._wipe(painter, letter, reveal, horizontal=False)
        outline_reveal = _between(t, 1.42, 1.78)
        if outline_reveal > 0.0:
            self._blit(painter, letter.outline, spec,
                       0.95 * _ease_out(outline_reveal) - 0.45 * _between(t, 1.9, 3.2),
                       QPainter.CompositionMode.CompositionMode_Plus)

    def _paint_o(self, painter, letter, t, reveal, settle):
        """The O arrives as a traced arc, then becomes the letter without a pop.

        The old handoff was one frame: the thin lit arc vanished and the full letter
        appeared whole, which read as the O jumping into place. The arc now stays lit
        while the crisp letter fades up underneath it.
        """
        spec = letter.spec
        if t < 1.84:
            return
        trace_fade = 1.0 - _between(t, 2.28, 2.52)
        if trace_fade > 0.01:
            # The trace is given the same light curve the finished letter's bloom
            # follows, so the halo is already there when the crisp letter takes over.
            self._trace_o(painter, letter, _ease_in_out(reveal),
                          _ease_out(_between(t, 1.84, 2.34)),
                          self._glow_alpha(t, 1.84, 2.40, 0.30 + settle) * trace_fade)
        if t < 2.24:
            return
        crisp = _ease_out(_between(t, 2.24, 2.46))
        self._blit(painter, letter.glow, spec,
                   self._glow_alpha(t, 1.84, 2.40, 0.30 + settle) * (0.4 + 0.6 * crisp),
                   QPainter.CompositionMode.CompositionMode_Plus)
        self._blit(painter, letter.crisp, spec, crisp)

    def _wipe(self, painter, letter, reveal, horizontal):
        """A letter revealed by a travelling edge, with a bright band on the edge."""
        spec = letter.spec
        if reveal >= 1.0:
            self._blit(painter, letter.crisp, spec, 1.0)
            return
        if reveal <= 0.0:
            return
        rect = QRectF(spec["x"] - self._pad, spec["y"] - self._pad,
                      spec["w"] + 2 * self._pad, spec["h"] + 2 * self._pad)
        if horizontal:
            edge = rect.left() + rect.width() * reveal
            clip = QRectF(rect.left(), rect.top(), max(0.0, rect.width() * reveal), rect.height())
            band = QRectF(edge - spec["h"] * 0.10, rect.top(), spec["h"] * 0.20, rect.height())
            gradient = QLinearGradient(band.left(), 0.0, band.right(), 0.0)
        else:
            edge = rect.top() + rect.height() * reveal
            clip = QRectF(rect.left(), rect.top(), rect.width(), max(0.0, rect.height() * reveal))
            band = QRectF(rect.left(), edge - spec["h"] * 0.10, rect.width(), spec["h"] * 0.20)
            gradient = QLinearGradient(0.0, band.top(), 0.0, band.bottom())
        gradient.setColorAt(0.0, QColor(255, 255, 255, 0))
        gradient.setColorAt(0.5, QColor(255, 255, 255, 150))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.save()
        painter.setClipRect(clip)
        self._blit(painter, letter.crisp, spec, 1.0)
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(band)
        painter.restore()

    def _trace_o(self, painter, letter, reveal, wide, halo=0.0):
        """The red O is drawn round, from the top, clockwise, with a lit head."""
        spec = letter.spec
        stroke = self._info["stroke"]
        width = (spec["w"] - stroke) / 2.0
        height = (spec["h"] - stroke) / 2.0
        centre = QPointF(spec["x"] + spec["w"] / 2.0, spec["y"] + spec["h"] / 2.0)
        rect = QRectF(centre.x() - width, centre.y() - height,
                      width * 2.0, height * 2.0)
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        span = -360.0 * reveal
        # Qt measures arcs in sixteenths of a degree, counter-clockwise from 3 o'clock;
        # starting at a quarter turn up and going negative draws it the way the eye
        # expects a circle to be drawn.
        if halo > 0.01:
            # Two nested rims rather than one wide band: a flat pen wide enough to
            # read as bloom also fills the bowl of the O, which looks like a wound-
            # up disc. Fainter and wider on the outside gives the falloff.
            for spread, strength in ((0.7, 64), (1.35, 30)):
                tint = QColor(letter.color)
                tint.setAlpha(int(strength * halo))
                soft = QPen(tint)
                soft.setWidthF(max(2.0, stroke * (0.5 + spread * wide)))
                soft.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                painter.setPen(soft)
                painter.drawArc(rect, 90 * 16, int(span * 16))
        pen = QPen(letter.color)
        pen.setWidthF(max(1.5, stroke * (0.45 + 0.55 * wide)))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.drawArc(rect, 90 * 16, int(span * 16))
        if reveal < 1.0:
            angle = math.radians(90.0 + span)
            head = QPointF(centre.x() + math.cos(angle) * width,
                           centre.y() - math.sin(angle) * height)
            # The head is a lit dot on the pen, not a lamp above it: it is invisible
            # until the line starts moving, grows with the stroke, and goes out just
            # before the finished letter lands. A constant-size ball at full brightness
            # from the first frame was the blob that seemed to shrink into the line.
            radius = max(2.0, stroke * (0.18 + 0.72 * wide))
            fade = _ease_out(min(1.0, reveal * 4.0)) * (1.0 - _between(reveal, 0.93, 1.0))
            if fade > 0.01:
                gradient = QRadialGradient(head, radius)
                gradient.setColorAt(0.0, QColor(255, 205, 210, int(230 * fade)))
                gradient.setColorAt(1.0, QColor(255, 77, 95, 0))
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                painter.setBrush(gradient)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(head, radius, radius)
        painter.restore()

    def _paint_particles(self, painter, letter, t):
        spec = letter.spec
        particles = letter.particles
        if not particles or t < 0.38 or t > 1.55:
            return
        fade = 1.0 - _between(t, 0.98, 1.55)
        if fade <= 0.0:
            return
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        tints = (QColor("#4B8DFF"), QColor("#5FD8FF"))
        for particle in particles:
            u = _between(t, 0.42 + particle["delay"],
                         0.42 + particle["delay"] + particle["duration"])
            if u <= 0.0:
                continue
            eased = _ease_in_out(u)
            position = _bezier(particle["from"], particle["via"], particle["to"], eased)
            alpha = min(1.0, u * 5.0) * fade
            size = particle["size"]
            color = tints[particle["tint"]]
            painter.setOpacity(alpha * 0.18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(position, size, size)
            painter.setOpacity(alpha * 0.95)
            painter.drawEllipse(position, size / 2.0, size / 2.0)
        painter.restore()

    def _paint_effects(self, painter, t, state):
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for at, index, power in self._bursts:
            age = t - at
            if age < 0 or age > 0.62:
                continue
            spec = self._info["letters"][index]
            self._burst(painter, spec["x"] + spec["w"] / 2.0, spec["y"] + spec["h"] / 2.0,
                        age, QColor(plan.LETTERS[index]["color"]), power)
        self._lock_effects(painter, t)
        self._second_breath(painter, t)
        painter.restore()

    def _burst(self, painter, cx, cy, age, color, power, rays=10):
        """A short spoke of light where a letter arrived, plus one thin ring."""
        cap = self._info["cap"]
        progress = min(1.0, age / 0.62)
        radius = cap * (0.12 + _ease_out(progress) * 0.92)
        fade = (1.0 - progress) ** 1.35 * power
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), int(150 * fade)))
        for index in range(rays):
            angle = math.tau * index / rays + math.sin(index * 12.31) * 0.12
            inner = radius * (0.58 + 0.12 * math.sin(index * 2.7))
            outer = radius + cap * (0.08 + 0.21 * ((index * 7 % 13) / 13))
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(),
                                       int(145 * fade * (1.0 if index % 4 == 0 else 0.45))),
                                max(1.0, cap * (0.010 if index % 4 == 0 else 0.004))))
            painter.drawLine(QPointF(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                             QPointF(cx + math.cos(angle) * outer, cy + math.sin(angle) * outer))
        ring = QPen(QColor(color.red(), color.green(), color.blue(), int(95 * fade)),
                    max(1.0, cap * 0.008 * (1.0 - progress)))
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.72, radius * 0.31)

    def _lock_effects(self, painter, t):
        """The wordmark is complete: a light sweep across it and two shockwaves."""
        if t < 2.34 or t > 4.2:
            return
        letters = self._info["letters"]
        cap = self._info["cap"]
        left = letters[0]["x"] - cap * 0.45
        right = letters[4]["x"] + letters[4]["w"] + cap * 0.45
        sweep = _between(t, 2.45, 3.05)
        if 0.0 < sweep < 1.0:
            self._sweep(painter, sweep, 1.0)
        centre = QPointF((letters[0]["x"] + letters[4]["x"] + letters[4]["w"]) / 2.0,
                         letters[0]["y"] + cap / 2.0)
        for ring in range(2):
            progress = _between(t, 2.36 + ring * 0.16, 3.30 + ring * 0.16)
            if progress <= 0.0 or progress >= 1.0:
                continue
            painter.setPen(QPen(QColor("#4B8DFF") if ring else QColor("#A8C8FF"),
                                max(1.0, cap * (0.009 if ring else 0.014) * (1.0 - progress))))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setOpacity((1.0 - progress) * (0.20 if ring else 0.34))
            painter.drawEllipse(centre, cap * (0.68 + progress * 2.15),
                                cap * (0.22 + progress * 0.68))
        painter.setOpacity(1.0)

    def _second_breath(self, painter, t):
        """The held wordmark breathes once more, halfway through the hold.

        The reveal ends at 2.8 s and the word is held until 6.4 s; without this the
        last three and a half seconds were a still picture, which is what made the
        arrival feel shorter than it was. At 4.2 s a second, brighter sweep crosses
        the word, the letters' bloom swells and settles, and two slow rings drift out
        from behind it - all light, no movement of the letters themselves.
        """
        if t < 4.2 or t > plan.INTRO_S:
            return
        letters = self._info["letters"]
        cap = self._info["cap"]
        centre = QPointF((letters[0]["x"] + letters[4]["x"] + letters[4]["w"]) / 2.0,
                         letters[0]["y"] + cap / 2.0)
        # The sweep, the same shape as the lock's, a little wider and brighter.
        sweep = _between(t, 4.20, 4.86)
        if 0.0 < sweep < 1.0:
            self._sweep(painter, sweep, 0.8)
        # The wordmark itself brightens with the breath and settles back by the fade.
        # The letter is re-lit, not its halo: adding the blurred copy again puts a grey
        # rim around every edge, which reads as a ghost of the letter beside it.
        breath = math.sin(math.pi * _between(t, 4.20, 5.55))
        if breath > 0.01:
            for letter in self._letters:
                self._blit(painter, letter.crisp, letter.spec, 0.12 * breath,
                           QPainter.CompositionMode.CompositionMode_Plus)
        for ring in range(2):
            progress = _between(t, 4.34 + ring * 0.20, 6.10 + ring * 0.20)
            if 0.0 < progress < 1.0:
                self._airy_ring(painter, centre, cap, progress,
                                QColor("#4B8DFF") if ring else QColor("#8EC1FF"))

    def _sweep(self, painter, progress, intensity=1.0):
        """A bar of light crossing the wordmark.

        A squeezed radial gradient, not a rectangle with a horizontal one. The first
        version drew a rect, and a rect has hard top and bottom edges: on screen it was
        a grey slab standing behind the T rather than light moving across a logo. An
        ellipse has no edges in any direction, and squeezing the painter is how a
        radial gradient becomes the narrow band a sweep wants.
        """
        letters = self._info["letters"]
        cap = self._info["cap"]
        left = letters[0]["x"] - cap * 0.5
        right = letters[4]["x"] + letters[4]["w"] + cap * 0.5
        x = left + (right - left) * _ease_in_out(max(0.0, min(1.0, progress)))
        centre_y = letters[0]["y"] + cap / 2.0
        reach = cap * 1.25
        peak = int(108 * intensity * (1.0 - abs(progress - 0.5) * 1.1))
        if peak <= 2:
            return
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.translate(x, centre_y)
        painter.scale(0.10, 1.0)
        gradient = QRadialGradient(QPointF(0.0, 0.0), reach)
        # Blue-white, not white: pure white added over black is grey, which is what made
        # the first version look like a slab of smoke rather than light.
        gradient.setColorAt(0.0, QColor(228, 240, 255, min(255, peak)))
        gradient.setColorAt(0.55, QColor(120, 175, 255, int(peak * 0.40)))
        gradient.setColorAt(1.0, QColor(80, 140, 255, 0))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(0.0, 0.0), reach, reach)
        painter.restore()

    def _airy_ring(self, painter, centre, cap, progress, colour):
        """One slow ellipse of light expanding from behind the wordmark."""
        ink = QColor(colour)
        ink.setAlphaF(max(0.0, 0.30 * (1.0 - progress) ** 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ink, max(1.0, cap * 0.010 * (1.0 - 0.7 * progress))))
        painter.drawEllipse(centre, cap * (0.74 + progress * 2.40),
                            cap * (0.26 + progress * 0.80))


def _bezier(start, control, end, k):
    one = 1.0 - k
    return QPointF(one * one * start.x() + 2 * one * k * control.x() + k * k * end.x(),
                   one * one * start.y() + 2 * one * k * control.y() + k * k * end.y())


def play(on_finished=None, with_sound=True):
    """Shows the splash and returns it. The caller does not wait."""
    splash = IntroSplash(on_finished=on_finished, with_sound=with_sound)
    return splash.start()


def frames(times, width=640, height=360):
    """Renders the reveal offscreen at the given times: (t, QImage) pairs.

    The preview tool and the tests use this - it is the only way to look at the
    splash on a machine that must not be interrupted by it, and the only way to
    check a frame in the middle of the timeline without waiting for it.
    """
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    if app is None:                       # a tool run on its own still needs one
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
    splash = IntroSplash(with_sound=False)
    splash.setGeometry(0, 0, int(width), int(height))
    splash.rebuild()
    out = []
    for moment in times:
        splash.render_at(moment)
        image = QImage(splash.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#000000"))
        painter = QPainter(image)
        splash.render(painter, QPoint(0, 0))
        painter.end()
        out.append((moment, image))
    splash.deleteLater()
    return out
