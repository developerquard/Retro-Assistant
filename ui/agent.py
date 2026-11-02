"""The animated agent: a sphere of translucent dots, and the words that float round it.

The owner asked for three things here, in this order, and this file is all three:

* **Transparent dots, and premium.** It is a sphere of glowing points, not a solid
  ball: every dot carries its own alpha, so the far side of the sphere is visible
  *through* the near side, and the eye reads a volume rather than a surface. The
  brightness of a dot is the light falling on that point of the sphere - a key light
  up and to the left, a rim light along the silhouette, and a handful of specular
  glints where the highlight sits. Depth drives size and alpha together, so the near
  dots are bigger and more solid and the far ones are small and faint, which is what
  makes it look like glass rather than a sticker.
* **Sleep means stopped.** Asleep, with background hearing off, the lattice is frozen
  where it was and greyed, and nothing moves at all - no spin, no breathing, no
  response to the microphone. That is the visual half of "unloaded": nothing is
  running behind it.
* **Two visual modes.** *Music Mode* is the full experience: the body takes a slow
  ambient walk round the hue wheel and answers the sound, and the frame around it does
  too. *Media Mode* is the calm one: the body stays white and still, and the frame is
  the only thing that answers - which is what the owner asked for when they said the
  frame should be the sound-reactive element and the orb itself should stay white.
* **Background voice hearing.** With that on, sleeping does not leave a dead grey
  ball: the frame and the body keep answering whatever the laptop is playing - a song,
  a video - as real audio. It is decoration while the assistant itself is off, and it
  never acts on any of it.

The sound-reactive element is the *wave* around the core, and there is no box. A
rounded-square outline used to sit here and it read as a widget border - a frame the
orb was trapped inside - which is exactly what the owner rejected. What is drawn
instead is a set of concentric waves that wrap the ball: each one is pushed outward
where its band is loud, carries a slow travelling crest, and is joined to the ball by
a translucent band of water, so the whole thing reads as one wave covering the core
rather than as a line drawn near it. Music Mode walks every ring through a different
part of the hue wheel, which is what makes the RGB vivid; Media Mode keeps them white
with a blue cast. Everything that moves is smoothed on a real clock, so it swells and
settles rather than jittering with the frames it happens to be drawn on.

There is no model file, no shader and no GPU. An icosphere of filled triangles was
here before this and it looked like plastic: facets are a surface, and the owner
wanted to see through it. Dots are also cheaper - a few hundred small ellipses
against eighty antialiased polygons twice that size - which is why the smoother
looking of the two is the faster one.

Everything that moves is driven by elapsed time, not by a frame counter, so a
dropped frame shortens a step instead of stalling the motion. Two things about it
are interactive, and both are for the panel rather than the floating orb (where the
window answers no input at all, by design):

* **hover** swells and brightens it, as if the light noticed you;
* **drag** takes hold of it and turns it - horizontally it yaws, vertically it
  tilts, and letting go leaves it spinning and settling on its own. A press that
  misses the orb is passed to the window, so the panel can still be dragged around
  by its empty space.
"""
import colorsys
import math
import time
from collections import deque

from PySide6.QtCore import QElapsedTimer, QLineF, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import QSizePolicy, QWidget

from core import log

from . import theme

FPS = 60                              # while Retro is listening, working or speaking
FPS_IDLE = 20                         # and while it is only sitting there
FPS_VISUAL = 30                       # music does not need sixty either
SEGMENTS = 72                         # points on one latitude line of the cage
ATTACK, RELEASE = 0.45, 0.12          # how fast the lattice swells and settles
FLOAT_LIFE_S = 6.0                    # how long one floating label stays on screen
FLOAT_MAX = 2                         # and how many are ever on screen at once
INERTIA = 0.94                        # how slowly a flicked orb settles
DOTS = 1500                           # points in the ball: dense enough to read as one body
# A crisp dot covers a fraction of the area a soft blob of the same nominal size did,
# so the count doubles when the blur goes away: the ball has to read as a lattice of
# points, not as a sparse swarm, and the whole point of the change was to keep the
# points *small*. Smaller *and* clearer costs nothing - a cached 4 px blit is a
# fraction of the 20 px one it replaced.
FAR_SKIP = 0.36                       # below this depth, every other dot is dropped
# How much of the ball is its own skin. The skin is what the eye reads as the outline,
# so it is spread with a Fibonacci lattice - the even one, with no poles and no seam -
# and every skin dot sits at exactly the same radius. See `_dot_sphere`.
SHELL_SHARE = 0.22
SHELL_REACH = 0.985                   # the skin sits a hair inside the ring, so no skin
                                      # dot pokes past the line `_rim_ring` draws
CORE_REACH = 0.86                     # the furthest in the volume reaches: short of the
                                      # skin, so nothing mottles the edge of it
CORE_FLOOR = 0.12                     # and the nearest it reaches: a dotted ball has air
                                      # in the middle of it, and a point at the origin
                                      # lands under the exact centre of the projection
                                      # where no other dot is, which makes the one dot
                                      # the eye is looking at the one that is opaque
SPREAD = 0.22                         # how much dot sizes vary: regular, not noisy
TONES = 16                            # brightness steps a dot can be drawn in
ALPHAS = 12                           # and opacities: both are sprite cache keys
SPRITE_CACHE_MAX = 1200               # small pixmaps, so a generous ceiling
SIZE_MAX = 7                          # dots stop growing here: area is the whole cost
# ---------- the two visual modes --------------------------------------------
# Music Mode is the full experience and Media Mode is the calm one, and both are the
# *look* only: neither touches the microphone, the settings or anything the assistant
# does. Switching between them changes no audio stream and restarts no animation.
MODE_MUSIC = "music"
MODE_MEDIA = "media"
VISUAL_MODES = (MODE_MUSIC, MODE_MEDIA)
FRAME_BLUE = QColor(150, 198, 255)     # Media Mode's frame, with the room playing
FRAME_WHITE = QColor(238, 242, 248)    # and with it quiet

# ---------- rotation ---------------------------------------------------------
# One rate, one turn per TURN_S seconds, and it belongs to the clock rather than to the
# audio: the orb is a companion, not a level meter with a spin on it. The phase is
# *accumulated* from elapsed time rather than computed as `now * rate`, which is what
# made the speed look like it changed - `now * rate` is a different position the
# instant the rate changes, so every state change teleported the lattice.
TURN_S = 30.0                         # one slow turn of the body
TILT_S = 13.0                         # and one slow open-and-close of its equator

# ---------- Music Mode's colour ----------------------------------------------
# A full walk round the hue wheel takes HUE_TURN_S, and audio may hurry it by at most
# HUE_AUDIO_SHARE of that. It used to be `0.004 + 0.03 * amplitude` per *frame*, which
# at sixty frames a second is a hue wheel every five seconds at rest and one every
# third of a second at full volume: a strobe, and it was loudness that drove it.
HUE_TURN_S = 42.0
HUE_AUDIO_SHARE = 0.55

# ---------- the waves --------------------------------------------------------
# Concentric waves around the core, and the sound-reactive element itself. They are
# rings, not a box: the rounded-square outline that used to sit here read as a widget
# border around the orb, which is exactly what the owner did not want. The rings are
# bent by the spectrum as they travel - loud bands push the line outward and give it
# its crests - so the picture reads as one wave wrapping the ball rather than as a
# frame the ball is drawn inside.
WAVE_RINGS = 4                        # the waves themselves, innermost first
WAVE_INNER = 0.97                     # of the orb's radius: the first wave laps over it
WAVE_GAP = 0.10                       # and each one after that is this much further out
WAVE_SEGMENTS = 192                   # samples round a wave: smooth at any size
WAVE_WIDTH = 1.70                     # the line, before the audio thickens it
WAVE_GLOW = 5.0                       # how much wider the soft pass behind it is
WAVE_PUSH = 0.20                      # how far a loud band pushes the wave outward
WAVE_RIPPLE = 0.10                    # the travelling crest, at full band level
WAVE_BEAT = 0.11                      # the kick a beat gives the whole wave
WAVE_VOICE = 0.24                     # the lift a sung line gives the top of the ring
WAVE_FLUTTER = 0.038                  # and the fast ripple a syllable puts along it
WAVE_TRAVEL_S = 7.0                   # one crest travels a ring in this many seconds
# Motion blur: how many past frames of a wave are kept, and how dark the freshest of
# them is. Six frames at thirty a second is a fifth of a second of history, which is
# exactly as long as a beat's swell lasts - one frame's worth was a pixel of softness
# nobody could see.
WAVE_TRAIL_FRAMES = 6
WAVE_TRAIL_INK = 0.34
# And how far the wake is stretched. One frame of movement is a couple of pixels, and
# at this size that is narrower than the wave's own glow - a physically exact trail is
# invisible. 2.6x is the stylisation that makes a swelling wave visibly leave
# something behind it, which is the whole point of drawing it.
WAVE_TRAIL_STRETCH = 3.2
WAVE_TRAIL_SPAN = 0.10                # of the orb's radius, at most
WAVE_FILL = 0.16                      # the water between the ball and the first wave
WAVE_SHELL = 0.86                     # the water reaches this far in, over the core
# Everything the composition needs room for, as one number: the halo around the ball
# and the outermost wave at its loudest. `_radius` divides by this, so a wave that is
# clipped by the edge of the window cannot happen - a cut wave reads as a bug.
WAVE_BUDGET = (WAVE_INNER + WAVE_GAP * (WAVE_RINGS - 1) + WAVE_PUSH + WAVE_VOICE
               + WAVE_FLUTTER)

# ---------- the audio envelope -----------------------------------------------
# Attack fast, release slow: a frame that follows the raw level steps about with every
# block, and one that follows it too slowly answers nothing. A band below ENV_FLOOR is
# the room rather than sound, and the whole envelope is divided by a slowly falling
# peak, so quiet speech and loud music both move the frame usefully instead of one of
# them doing nothing and the other one saturating.
ENV_ATTACK_S = 0.07
ENV_RELEASE_S = 0.34
# The room's audio is a measured spectrum, not a microphone level, and it is there to
# be *seen*: an attack this fast is what makes a drum hit arrive on the frame it
# happened, and a release short enough that the wave has fallen before the next one.
ENV_VIS_ATTACK_S = 0.03
ENV_VIS_RELEASE_S = 0.16
ENV_FLOOR = 0.02                      # anything below this is the room, not the record
# ...and below this overall level the room is only the room. A monitor input (Stereo
# Mix) has its own noise floor - measured here at about 0.01 with no music, whose
# bands still read 0.2 - and a visualiser that answers that is a ring that wobbles to
# nothing, which is exactly what "it just normally moves" looks like. The level is the
# honest test: below this the machine is not playing anything.
ENV_ROOM_FLOOR = 0.03
ENV_PEAK_FALL_S = 6.0                 # how slowly the normalising peak comes back down
ENV_MIN_PEAK = 0.22                   # and a floor under it, so silence is not scaled up
# A beat is a sudden rise above how loud the room has been, and it is gone quickly:
# this is the difference between a wave that pulses with the song and one that drifts.
ENV_BEAT_MARGIN = 0.02
ENV_BEAT_FALL_S = 0.14
CAGE_LATS = (-0.62, -0.21, 0.21, 0.62)  # surface lines that show the curvature
CAGE_STEPS = 8                        # depth buckets the lattice is drawn in
BREATH = 0.030                        # how far the idle shell wanders from the sphere
SWELL = 0.26                          # and how far your voice pushes it out
VIS_SWELL = 0.42                      # and how far the room's music does
LIGHT = (-0.46, -0.52, 0.72)          # one key light: above, left, in front of the eye
RIM = 0.34                            # how wide the lit edge of the sphere is
PERSPECTIVE = 0.16                    # how hard the projection pushes near over far
# ... and the edge that leaves: the widest latitude of a sphere seen through it. The
# projection scales a point by 1/(1 + k(1 - z)), which balances the latitude's own
# width at z = k/(1 + k); the ring of dots the eye reads as the outline sits there.
SILHOUETTE = (math.sqrt(1.0 - (PERSPECTIVE / (1.0 + PERSPECTIVE)) ** 2)
              * (1.0 + PERSPECTIVE) / (1.0 + 2.0 * PERSPECTIVE))
RING_DOTS = 108                       # the outline: this many dots, evenly spaced
RING_SIZE = 2.15                      # and their own width, before the widget scales
WAKE_LOAD_S = 3.0                     # the load: grey to blue, however fast the model is
SLEEP_HOLD_S = 0.6                    # asleep: nothing moves at all, after this long


def _even_sphere(count, radius):
    """`count` points spread evenly over a sphere of this radius.

    A Fibonacci lattice: golden-angle longitudes against evenly stepped latitudes.
    It is the cheap even spread - no poles that bunch up, no seam, no clustering - 
    and "even" is a property of the lattice, so the picture holds at every size
    instead of being tuned for one widget.
    """
    golden = math.pi * (3.0 - math.sqrt(5.0))
    points = []
    for index in range(max(1, count)):
        z = 1.0 - (2.0 * index + 1.0) / max(1, count)
        ring = math.sqrt(max(0.0, 1.0 - z * z))
        angle = golden * index
        points.append((ring * math.cos(angle) * radius,
                       ring * math.sin(angle) * radius, z * radius))
    return points


def _dot_sphere(count=DOTS):
    """A ball of points: an even skin, with a hashed volume inside it.

    The skin is the outline the owner looks at, and it is the first `SHELL_DOTS`
    points: a Fibonacci lattice at exactly one radius, so the dots round the edge are
    evenly spaced and the edge is a circle. It used to be a golden spiral whose radius
    was hashed *per dot*, which is why the outline was ragged - some dots outside the
    shape, a dense patch in one place and a thin one in another, an edge that was not
    a circle. Even spacing is not a tuning value; it is the lattice.

    The volume keeps the hashed spread it had, because the middle of it looked right:
    a line of sight through a ball is longest at its edge and shortest at its centre,
    so radii are leaned towards the skin (the same 0.42 exponent) to keep the middle
    from going thin. What has changed is where it *stops*. It used to reach 0.95 of the
    radius, so those dots projected a hair inside the skin and mottled the very edge
    the eye is checking; it stops short of it now, and the outline is left to the skin.

    The skin is a *fifth* of the ball and not more, which is the transparency rule from
    the other direction: dots at one radius that are dense enough to draw a clean edge
    are also dense enough to close the far side up behind them.
    """
    skin = max(1, int(count * SHELL_SHARE))
    points = _even_sphere(skin, SHELL_REACH)
    inner = max(1, count - skin)
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for step in range(inner):
        z = 1.0 - (2.0 * step + 1.0) / inner
        ring = math.sqrt(max(0.0, 1.0 - z * z))
        angle = golden * step
        radius = (step * 0.618033988749895 % 1.0) ** 0.42 * CORE_REACH
        if radius < CORE_FLOOR:
            continue                      # nothing sits at the very centre of the ball
        points.append((ring * math.cos(angle) * radius, ring * math.sin(angle) * radius,
                       z * radius))
    return points


DOTS_AT = _dot_sphere()
SHELL_DOTS = max(1, int(DOTS * SHELL_SHARE))
# Each dot gets its own phase and its own band of the spectrum, spread by the golden
# angle and by index. Without them the whole lattice would pulse as one lump and
# every dot would answer the bass at once, which is a level meter, not a visualiser.
PHASE = [(index * 2.399963229728653) % math.tau for index in range(len(DOTS_AT))]
BANDS = 12
BAND_OF = [index % BANDS for index in range(len(DOTS_AT))]


def phase_sin(index):
    """A stable -1..1 wobble per dot: the same value every frame, no randomness."""
    return math.sin(PHASE[index] * 1.7)


def phase_cos(index):
    return math.cos(PHASE[index] * 2.3)


class AgentWidget(QWidget):
    """A self-contained animated lattice. It draws what it is told and nothing else."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(120, 120)
        # A circle cannot be drawn in a rectangle that is not square, and a layout is
        # happy to stretch a widget to fill one. Fixed policy means the orb keeps its
        # own square geometry wherever it is placed; `paintEvent` still derives every
        # distance from `min(width, height)` so even a hand-set non-square size draws a
        # circle rather than an oval.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # The widget answers the mouse so the orb can be hovered and turned. A press
        # that misses it is ignored, which hands it back to the window - so the panel
        # is still draggable anywhere the orb is not.
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._state = "offline"
        self._amplitude = 0.0
        self._level = 0.0
        self._bands = [0.0] * BANDS          # the room's audio, one value per band
        self._visual = False                 # background voice hearing is running
        self._visual_level = 0.0
        self._visual_mode = MODE_MUSIC       # Music Mode or Media Mode
        # The audio the frame is drawn from: smoothed per band, floored, and scaled
        # against a peak that comes down slowly. See `_envelope`.
        self._env = [0.0] * BANDS
        self._env_peak = ENV_MIN_PEAK
        # The two things that make a wave look like it *feels* the music rather than
        # merely drifting: a beat pulse (fast rise, quick fall, fired when the room is
        # suddenly louder than it has been) and the vocal band's own level, taken from
        # the middle of the spectrum instead of the whole of it.
        self._pulse = 0.0
        self._vocal = 0.0
        # The vocal's own motion: how hard it changed since the previous frame (`flux`,
        # which is what makes a syllable read as a syllable) and the raw value it is
        # compared against. Both are reset when the room goes quiet, so nothing keeps
        # breathing on its own clock.
        self._vocal_flux = 0.0
        self._vocal_was = 0.0
        # The sampler's own vocal reading, when it sends one. See `set_audio`.
        self._vocal_in = None
        # The last frame's wave radii, per ring, and the few before it: a wave that is
        # expanding leaves a trail where it was, and a single frame's worth of that is
        # a pixel of softness nobody can see. The trail is the same shape drawn at the
        # radii it really had, one ghost per past frame.
        self._wave_prev = {}
        self._wave_trail = deque(maxlen=WAVE_TRAIL_FRAMES)
        self._ghosts = 0                      # motion-blur polylines drawn last frame
        self._slow_level = 0.0                # how loud the room has been, slowly
        self._hue = 0.0                      # Music Mode's colour, walked by the clock
        self._angle = 0.0                    # the body's turn, accumulated in seconds
        self._since_tick = time.monotonic()  # the clock the last frame was measured from
        self._colour = QColor(theme.GREY)
        self._target = QColor(theme.GREY)
        self._label_space = False         # floating mode: room for glow and labels
        self._floats = []                 # [(text, expires_at)] - real execution events
        self._mode = ""                   # "work" / "chat", shown under the orb
        self._caption = ""                # the phase, told in one or two words
        self._arc = None                  # 0..1 of a job that knows its own length
        self._yaw = 0.0                   # where the orb has been turned to
        self._pitch = 0.42                # and how far open it is
        self._spin = 0.0                  # leftover turn after a flick
        self._hover = 0.0                 # eased, so the glow swells rather than jumps
        self._dragging = False
        self._drag_origin = None
        self._drag_from = (0.0, 0.0)
        self._frozen_at = 0.0             # asleep: the clock the drawing is held at
        self._sprites = {}                # (colour, size, alpha) -> a soft dot
        self._load_from = None            # waking: the grey it started from, and when
        self._load_started = 0.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / FPS))

    # ---------- inputs ------------------------------------------------------

    def showEvent(self, event):
        """Restart the clock only while the orb is really on screen.

        A hidden widget has nobody looking at it. Stopping the timer on hide means a
        minimised Retro (or the panel's orb while the floating shape owns the screen)
        costs no frames at all, rather than drawing thirty times a second into a
        widget the compositor never asks for.
        """
        super().showEvent(event)
        self._pace()
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def set_state(self, state):
        if state not in theme.STATE_COLOURS:
            state = "offline"
        if state == self._state:
            return
        previous = self._state
        self._state = state
        self._target = QColor(theme.STATE_COLOURS[state])
        if state == "loading":
            # Waking up: the colour walks from the grey it slept in to the blue it
            # will be, over WAKE_LOAD_S, whatever the model does in the meantime.
            self._load_from = QColor(self._colour)
            self._load_started = time.monotonic()
        else:
            self._load_from = None
        if state == "asleep" and previous != "asleep":
            self._frozen_at = self.seconds()
        self._pace()
        if state == "offline":
            self._floats.clear()

    def _pace(self):
        """Sixty frames a second while something is happening, thirty while not.

        A lit lattice is more work than the ring it replaced, and this widget is on
        screen the whole time the assistant is running - so it should not spend a
        whole core drawing a ball that is only breathing. A still orb at thirty is
        indistinguishable, and the speed goes back up the moment the microphone, the
        voice, or the room has something to show.
        """
        # Asleep and not visualising is a frozen drawing: the clock is held, so every
        # frame would be identical to the one before it. Stop the timer instead of
        # painting that same image twenty times a second for as long as the machine
        # sits idle. Waking calls `_pace` again and the timer restarts.
        if self.asleep_still():
            interval = int(1000 / FPS_IDLE)
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            self._timer.stop()
            return
        if self._visual:
            wanted = FPS_VISUAL
        else:
            busy = self._state in ("listening", "working", "speaking", "loading")
            wanted = FPS if busy else FPS_IDLE
        interval = int(1000 / wanted)
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        if not self._timer.isActive():
            self._timer.start()

    def set_level(self, level):
        """The loudness just measured, 0..1. The microphone or the played audio."""
        self._level = max(0.0, min(1.0, float(level)))

    def set_visual_mode(self, mode):
        """Music Mode or Media Mode, and nothing else moves when it changes.

        The two differ in what the *body* does and in what colour the frame walks.
        Neither touches the microphone, the sampler, the clock or the rotation, so a
        switch cannot interrupt a capture, restart an animation or teleport the
        lattice - which is exactly what the owner asked for.
        """
        mode = str(mode or "").lower()
        if mode not in VISUAL_MODES:
            mode = MODE_MUSIC
        if mode == self._visual_mode:
            return mode
        log.event("visual_mode", mode=mode)
        self._visual_mode = mode
        self.update()
        return mode

    @property
    def visual_mode(self):
        """Which of the two is showing. Read by the window and by the tests."""
        return self._visual_mode

    @property
    def music(self):
        """True in Music Mode: the mode that walks the hue wheel and moves the body."""
        return self._visual_mode == MODE_MUSIC

    def colourful(self):
        """Whether the RGB look is on: Music Mode while background hearing runs.

        The owner's rule is exact, and both halves matter: **RGB belongs to Music
        Mode**, and the looks are a *background-hearing* activity - the microphone and
        the room sampler cannot run at once, so with the microphone on there is no
        audio for a look to answer and the waves are a still decoration in the state's
        own colour. Turn the microphone off with background hearing on and Music Mode
        walks the wheel; Media Mode stays white and blue in exactly the same moment.
        """
        return self.music and self._visual

    def set_visual(self, on):
        """Background voice hearing: asleep, but alive to whatever the room is playing.

        On, the lattice takes its motion and its colour from the system's own audio
        and it is pure decoration; off, sleeping is a stopped grey ball. Either way
        the *assistant* is asleep: nothing here can hear a command.
        """
        on = bool(on)
        if on == self._visual:
            return
        self._visual = on
        if not on:
            self._bands = [0.0] * BANDS
            self._visual_level = 0.0
        self._pace()
        self.update()

    def set_audio(self, level, bands=None, vocal=None):
        """What the laptop is playing right now, 0..1, and a level per band.

        This is real audio from the system's own output - not the microphone, and not
        a guess from a byte count - so the movement on screen is the music, not a
        performance of it. `vocal` is the sampler's own answer to "how much of this is
        a voice", measured in the 200-3500 Hz region against the rest of the mix; it
        is passed rather than re-derived here because the twelve bars have already had
        that ratio normalised out of them.
        """
        self._visual_level = max(0.0, min(1.0, float(level or 0.0)))
        self._vocal_in = None if vocal is None else max(0.0, min(1.0, float(vocal)))
        if bands:
            values = [max(0.0, min(1.0, float(value or 0.0))) for value in bands]
            self._bands = (values + [0.0] * BANDS)[:BANDS]

    def set_label_space(self, on):
        """Floating mode draws the orb smaller so its own glow and the floating
        labels have somewhere to go. In the panel there is no room for either."""
        self._label_space = bool(on)
        self.update()

    def set_progress(self, text):
        """One short line about what Retro is doing, from a real execution event.

        Two at a time at most, oldest first out, each fading on its own clock - so
        a long job cannot plaster the desktop with text nobody can read.
        """
        text = " ".join(str(text or "").split())
        if not text:
            return
        if self._floats and self._floats[-1][0] == text:
            self._floats[-1] = (text, time.monotonic() + FLOAT_LIFE_S)
            return                           # the same event twice is not news
        self._floats.append((text[:44], time.monotonic() + FLOAT_LIFE_S))
        del self._floats[:-FLOAT_MAX]
        self.update()

    def set_mode(self, mode):
        """"chat" or "work", shown in small type under the orb."""
        mode = " ".join(str(mode or "").lower().split())
        if mode == self._mode:
            return
        self._mode = mode
        self.update()

    def set_caption(self, text):
        """The phase in one or two words, drawn inside the orb.

        It comes from the same vocabulary the rest of the app reports (core/states.py),
        so what is written in the middle of the orb is never a guess at what Retro
        is doing - it is what Retro said it was doing.
        """
        text = " ".join(str(text or "").split())[:22].upper()
        if text == self._caption:
            return
        self._caption = text
        self.update()

    def set_arc(self, fraction):
        """How far through a job that knows its own length is, 0..1, or None.

        Only ever set from a real count - the sections of a document, one of which is
        finished - so an arc that is half full means half written, and a job with no
        known length draws no arc at all rather than one that invents progress.
        """
        if fraction is None:
            self._arc = None
        else:
            self._arc = max(0.0, min(1.0, float(fraction)))
        self.update()

    def clear_progress(self):
        """The task ended: nothing should keep claiming it is still running."""
        self._floats.clear()
        self.set_arc(None)
        self.set_caption("")
        self.update()

    @property
    def state(self):
        return self._state

    @property
    def turned(self):
        """(yaw, pitch) - how far the orb has been turned, for the interaction test."""
        return (self._yaw, self._pitch)

    @property
    def visual(self):
        """True while the background visualiser is drawing."""
        return self._visual

    def seconds(self):
        return self._clock.elapsed() / 1000.0

    # ---------- interaction -------------------------------------------------

    def _ring_reach(self):
        """How far from the centre still counts as holding the orb, in pixels."""
        span = min(self.width(), self.height())
        return max(24.0, self._radius(span) * 1.22)

    def _on_ring(self, point):
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)
        return math.hypot(point.x() - centre.x(), point.y() - centre.y()) <= self._ring_reach()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._on_ring(event.position()):
            event.ignore()                   # not ours: the window may drag itself
            return
        self._dragging = True
        self._drag_origin = event.position()
        self._drag_from = (self._yaw, self._pitch)
        self._spin = 0.0
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_origin is not None:
            delta = event.position() - self._drag_origin
            self._yaw = self._drag_from[0] + delta.x() * 0.012
            self._pitch = max(-1.45, min(1.45, self._drag_from[1] - delta.y() * 0.010))
            self._spin = delta.x() * 0.0016
            self.update()
            return
        self._hover = 1.0 if self._on_ring(event.position()) else 0.0
        self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover else
                       Qt.CursorShape.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        event.accept()

    def leaveEvent(self, _event):
        self._hover = 0.0
        self.update()

    # ---------- animation ---------------------------------------------------

    def asleep_still(self):
        """True when the drawing must not move at all: asleep, and not visualising."""
        return self._state == "asleep" and not self._visual

    def _tick(self):
        target = self._level if self._state in ("listening", "speaking") else 0.0
        if self._visual:
            target = self._visual_level        # the room is the level now
        rate = ATTACK if target > self._amplitude else RELEASE
        self._amplitude += (target - self._amplitude) * rate
        if self._amplitude < 0.004:
            self._amplitude = 0.0
        goal = self._ramp_target()
        step = 0.12 if self._load_from is None else 0.05   # the load is a slow walk
        if all(abs(self._colour.getRgb()[channel] - goal.getRgb()[channel]) * step < 0.5
               for channel in (0, 1, 2)):
            # Rounding each channel to an integer makes a fixed point a few levels
            # short of the colour it is walking to: the step becomes smaller than
            # one level and the orb sat on a slightly-wrong green for ever, never
            # quite reaching the state's own colour. Close enough snaps.
            self._colour = QColor(goal)
        else:
            self._colour = QColor(
                round(self._colour.red() + (goal.red() - self._colour.red()) * step),
                round(self._colour.green() + (goal.green() - self._colour.green()) * step),
                round(self._colour.blue() + (goal.blue() - self._colour.blue()) * step),
            )
        now = time.monotonic()
        # Everything below is scaled by the time this frame really took, so a dropped
        # frame shortens a step instead of making the motion stutter.
        elapsed = max(0.0, min(0.25, now - self._since_tick))
        self._since_tick = now
        self._envelope(elapsed)
        # The turn is a phase that only ever *accumulates*. It used to be `now * rate`,
        # which is a different position the moment the rate changes - so every state
        # change, and every toggle of background hearing, teleported the lattice and
        # read as the speed changing on its own.
        if not self.asleep_still():
            self._angle += elapsed / TURN_S
        if self.colourful():
            # Music Mode's colour: a slow walk round the hue wheel, hurried a little by
            # the audio and never raced by it, and only while the assistant is drawing
            # the room at all.
            self._hue = (self._hue + elapsed * (1.0 + HUE_AUDIO_SHARE * self._amplitude)
                         / HUE_TURN_S) % 1.0
        if not self._dragging:
            self._yaw += self._spin
            self._spin *= INERTIA
            if abs(self._spin) < 1e-5:
                self._spin = 0.0
        if self._floats and any(expires <= now for _text, expires in self._floats):
            self._floats = [(text, expires) for text, expires in self._floats if expires > now]
        self.update()

    def _ramp_target(self):
        """Where the colour is heading right now: grey -> blue during a wake."""
        if self._load_from is None:
            return self._target
        done = min(1.0, (time.monotonic() - self._load_started) / WAKE_LOAD_S)
        eased = done * done * (3.0 - 2.0 * done)          # smoothstep, so it settles
        start, end = self._load_from, self._target
        return QColor(
            round(start.red() + (end.red() - start.red()) * eased),
            round(start.green() + (end.green() - start.green()) * eased),
            round(start.blue() + (end.blue() - start.blue()) * eased),
        )

    def load_fraction(self):
        """How far through the wake-up ramp the colour is, 0..1 (for the tests)."""
        if self._load_from is None:
            return 1.0
        return min(1.0, (time.monotonic() - self._load_started) / WAKE_LOAD_S)

    def _envelope(self, elapsed):
        """The audio the frame is drawn from: smoothed, floored, and normalised.

        Three decisions, and each one is a fault the owner could see.

        *Smoothed*: the sampler's bands arrive fifty times a second and the app polls
        them thirty, so a frame that followed them raw stepped about with whatever
        block happened to be last. Attack is quick enough to feel immediate and release
        is slow enough that a fragment does not become a flicker.

        *Floored*: below `ENV_FLOOR` a band is the room - a fan, a hiss, the noise
        floor of the device - and a frame that answers the room is a frame that is
        never still. There is one floor for all of them, so quiet music still draws.

        *Normalised*: the envelope is divided by a peak that rises instantly and falls
        over `ENV_PEAK_FALL_S`, with a floor under it, so speaking quietly and playing
        a song loudly both fill the frame usefully - rather than one doing nothing and
        the other pinned at maximum.
        """
        if elapsed <= 0.0:
            return
        peak = max(self._env)
        self._env_peak = max(ENV_MIN_PEAK, peak,
                             self._env_peak * math.exp(-elapsed / ENV_PEAK_FALL_S))
        scale = 1.0 / self._env_peak
        live = self._visual or self._state in ("listening", "speaking")
        attack = ENV_VIS_ATTACK_S if self._visual else ENV_ATTACK_S
        release = ENV_VIS_RELEASE_S if self._visual else ENV_RELEASE_S
        for index in range(BANDS):
            raw = float(self._bands[index] or 0.0) if self._visual else 0.0
            if self._visual and self._visual_level < ENV_ROOM_FLOOR:
                raw = 0.0                     # the monitor input's own hiss, not music
            if not self._visual and live:
                # The microphone's own loudness, spread flat across the bands: it is a
                # level, and the frame is told so rather than pretending otherwise.
                raw = self._level * 0.55
            level = 0.0 if raw < ENV_FLOOR else min(1.0, raw * scale)
            tau = attack if level > self._env[index] else release
            step = 1.0 - math.exp(-elapsed / tau)
            self._env[index] += (level - self._env[index]) * step
        if self._visual:
            # The vocal region: bands 3 to 8 of twelve cover roughly 150 Hz to 3.5 kHz,
            # where a singer's fundamental and first formants live. It is measured
            # against the *rest of the mix* rather than against a fixed floor, because
            # a floor tuned to a loud chorus made a quiet verse's vocal disappear - and
            # that, plus a smoothing of 0.09 s, is what made the waves look as if they
            # were moving to the music rather than to the voice in it.
            if self._vocal_in is not None:
                # The sampler has already measured this in hertz, on the raw mix, where
                # a voice 25 dB under the bassline is still visible. These bars have
                # been normalised against the loudest band, so the ratio derived here
                # is the fallback - it is right, but it is the same information after
                # the loudness has been divided out of it.
                vocal = self._vocal_in
            else:
                vocal_band = max(self._env[3:9]) if BANDS > 9 else max(self._env)
                others = sorted(self._env[:3] + self._env[9:])
                rest = others[len(others) // 2] if others else 0.0
                vocal = max(0.0, min(1.0, (vocal_band - rest * 1.05) / 0.55))
            # Fast up, quick down: a syllable is 0.1 to 0.3 s, so a 0.09 s smoothing
            # followed the words instead of the syllables and read as "almost live".
            tau = 0.035 if vocal > self._vocal else 0.11
            self._vocal += (vocal - self._vocal) * (1.0 - math.exp(-elapsed / tau))
            # How hard the vocal is *changing* - the shape of a consonant at the start
            # of a syllable. The wave uses it to put a small, fast edge on the lift, so
            # words read as words instead of as one slow swell.
            jump = min(1.0, abs(vocal - self._vocal_was) * 3.5)
            self._vocal_flux = max(0.0, self._vocal_flux * math.exp(-elapsed / 0.14))
            self._vocal_flux = max(self._vocal_flux, jump)
            self._vocal_was = vocal
            level = min(1.0, self._visual_level / max(ENV_MIN_PEAK, self._env_peak))
            self._slow_level += (level - self._slow_level) * (1.0 - math.exp(-elapsed / 0.55))
            if level > self._slow_level * 1.28 + ENV_BEAT_MARGIN:
                self._pulse = 1.0
            self._pulse *= math.exp(-elapsed / ENV_BEAT_FALL_S)
        else:
            # With no room audio there is nothing to beat to: both go away rather than
            # leaving a still wave breathing on the clock.
            self._vocal *= math.exp(-elapsed / 0.20)
            self._vocal_flux *= math.exp(-elapsed / 0.20)
            self._vocal_was = 0.0
            self._pulse *= math.exp(-elapsed / 0.20)
            self._slow_level = 0.0

    def _band_at(self, position):
        """The envelope at a point round the frame, 0..1 of the way round it.

        The bands are *folded* onto the frame rather than laid along it: both sides of
        the shape take the same band at the same distance from the middle, so the frame
        swells symmetrically the way a lit object does. Bass at the sides, the top end
        at the top and the bottom - which is what makes the movement read as the shape
        answering the sound instead of as a row of bars wearing it as a costume.
        """
        if not any(self._env):
            return 0.0
        folded = 1.0 - abs(2.0 * position - 1.0)
        index = min(BANDS - 1, max(0, int(folded * BANDS)))
        # *Contrast*, not level. Every band of a loud mix sits near the top of the
        # normalised range, so returning the band alone bent the ring into one almost
        # even lump: the wave breathed with the song but kept the same outline, which is
        # the "it just normally moves" the owner reported. What the eye needs is the
        # contour - which part of the spectrum is loud *compared with the rest* - so
        # the loudest band reaches full push and the average one only a third of it.
        # The shape then changes as the music changes, which is what reacting means.
        mean = sum(self._env) / float(BANDS)
        spread = max(0.08, max(self._env) - mean)
        return min(1.0, max(0.0, 0.30 + 0.70 * (self._env[index] - mean) / spread))

    def _wave_reach(self, ring, position, now, overall):
        """How far one point of one wave sits from the centre, in orb radii.

        Two motions add together. The *bands* bend the ring by angle: `_band_at` folds
        the spectrum onto the circle, so the wave swells where the music is loud and
        stays calm where it is not - that is what makes it answer a song instead of
        merely glowing with it. The *crest* is a slow travelling ripple round the same
        ring, and it is multiplied by the band, so in silence the wave is a still
        circle and in music it visibly rolls outward.
        """
        band = self._band_at(position)
        angle = position * math.tau
        base = WAVE_INNER + WAVE_GAP * ring
        if not self._visual:
            # Still, and by design. While the microphone is on there is no room audio
            # and no look to answer, so the waves are a fixed shape around the core:
            # no clock in this branch at all, and no audio in it either. A decoration
            # that moved on its own was the thing the owner asked to be rid of.
            return base + 0.016 * math.sin(angle * 3.0 + ring * 0.9)
        # Two crests rather than one, at three and seven lobes and moving at different
        # speeds: one sine reads as an ellipse being squeezed, and the pair is what
        # makes a closed ring look like water rather than like a wobbling outline.
        crest = (math.sin(angle * 3.0 - now * math.tau / WAVE_TRAVEL_S + ring * 1.1)
                 + 0.55 * math.sin(angle * 7.0 + now * math.tau / (WAVE_TRAVEL_S * 0.6)
                                   - ring * 0.7)) / 1.55
        # Where a sung line lands. The vocal bands already bend the ring, but a voice is
        # not a direction: it is one shape arriving and leaving, and the owner wants to
        # see it as that. So the top of every wave lifts with the vocal envelope - a
        # soft bump covering roughly the upper third, strongest on the inner wave -
        # while the bass keeps the sides moving. Silence leaves nothing to lift.
        # `top` is 1 at the top of the window and 0 at the waist, cubed so the bump has
        # an edge rather than a gradient round the whole ring.
        top = max(0.0, -math.sin(angle)) ** 2.4
        voice = self._vocal * top * (1.0 - 0.16 * ring)
        # A voice is not one smooth bump: every syllable starts and stops, and that is
        # the difference between a wave that follows the singing and one that merely
        # swells with the mix. The flutter rides the lift, driven by how fast the vocal
        # band is changing, and only while there is a voice to hear.
        flutter = (self._vocal * 0.55 + self._vocal_flux * 0.75) * top
        ripple = WAVE_FLUTTER * flutter * math.sin(
            angle * 9.0 + now * 19.0 + ring * 0.7)
        # Four motions, in the order they are felt: the band bends the ring where the
        # sound is, the sung line lifts its top, the beat kicks it outward, and the
        # crest rolls through it. Every one of them is gated by the audio, so a quiet
        # room leaves the waves perfectly still instead of drifting on their own clock.
        swell = band * (0.45 + 0.55 * overall)
        gate = min(1.0, band * 1.35 + self._vocal * 0.8)
        return (base + WAVE_PUSH * swell + WAVE_VOICE * voice + ripple
                + WAVE_BEAT * self._pulse * (0.35 + 0.65 * band)
                + WAVE_RIPPLE * crest * gate)

    def _ring_hue(self, ring):
        """Music Mode's colour for one wave: a slice of the wheel per ring.

        The body already walks the hue, and giving every ring the same colour made the
        waves read as one thick outline. A fixed offset per ring turns them into a
        spectrum - the inner wave red where the body is red, the outer ones running
        through amber, green and violet - which is the "RGB must be clearer" the owner
        asked for, and it is still driven by the same single hue clock, so the whole
        object moves through colour together instead of flickering independently.
        """
        hue = (self._hue + 0.06 + ring * 0.11) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
        return QColor(round(red * 255), round(green * 255), round(blue * 255))

    def _waves(self, painter, cx, cy, radius, now, colour):
        """The waves around the core, and the sound-reactive element itself.

        Concentric waves, not a border and not a ring of bars: the innermost sits
        against the ball's own skin and a translucent band of water joins it to the
        core, the others ripple outward. Each wave is pushed outward where its band is
        loud and carries a travelling crest, so it reads as the music moving through
        water around Retro rather than as a line drawn near a ball. Music Mode runs a
        different part of the hue wheel on every ring - that is the vivid RGB - while
        Media Mode keeps them white with a blue cast. With no audio the waves are calm
        and faint, so a microphone that is off never looks like one that is listening.
        """
        if radius <= 6:
            return
        energy = max(self._env) if self._env else 0.0
        visual = self._visual
        # No microphone level and no clock in the still case: the waves answer the room
        # or they do not answer anything. Using the microphone level here is what made
        # them shimmer while the owner was simply talking to Retro.
        overall = self._amplitude if visual else 0.0
        # The loudest wave must still fit inside the window: a cut wave reads as a bug
        # rather than as a boundary.
        limit = (min(self.width(), self.height()) * 0.5 - 3.0) / radius
        shapes = []
        reaches = []                     # the same points as radii, for the motion blur
        for ring in range(WAVE_RINGS):
            points, radii = [], []
            for index in range(WAVE_SEGMENTS + 1):
                position = index / float(WAVE_SEGMENTS)
                angle = math.tau * position
                reach = min(limit, self._wave_reach(ring, position, now, overall))
                radii.append(reach)
                points.append(QPointF(cx + math.cos(angle) * reach * radius,
                                      cy + math.sin(angle) * reach * radius))
            shapes.append(points)
            reaches.append(radii)

        # The water that covers the core: a translucent band from inside the ball's own
        # edge out to the first wave, and a thinner band between each pair of waves on
        # top of that. It is what turns a set of lines into one wave *covering* Retro,
        # and every band swells with the beat because its outer edge is the wave itself.
        # Drawn before the lines, so the water is behind them and the crests stay crisp.
        if visual and energy > 0.02:
            painter.setPen(Qt.PenStyle.NoPen)
            for ring, points in enumerate(shapes):
                if ring == 0:
                    inner = []
                    for index in range(WAVE_SEGMENTS, -1, -1):
                        angle = math.tau * index / float(WAVE_SEGMENTS)
                        inner.append(QPointF(cx + math.cos(angle) * WAVE_SHELL * radius,
                                             cy + math.sin(angle) * WAVE_SHELL * radius))
                else:
                    inner = list(reversed(shapes[ring - 1]))
                path = QPainterPath()
                path.moveTo(points[0])
                for point in points[1:]:
                    path.lineTo(point)
                for point in inner:
                    path.lineTo(point)
                path.closeSubpath()
                water = QColor(self._ring_hue(ring) if self.colourful() else colour)
                depth = 1.0 - 0.22 * ring
                water.setAlphaF(min(0.42, WAVE_FILL * depth * (0.35 + 0.65 * energy)
                                    * (0.45 + 0.85 * overall)))
                painter.setBrush(water)
                painter.drawPath(path)

        # Motion blur. A wave that is expanding was *inside* its current radius a few
        # frames ago, and the eye reads that history as a wake. The ring is drawn again
        # at the radii it really had, one ghost per past frame, older ones fainter and
        # a touch wider - so a swell leaves a soft trail behind it. Only outward
        # movement is drawn: a trail behind a wave moving inward would be a lie. A
        # still wave keeps no history at all, because nothing is remembered without
        # room audio.
        self._ghosts = 0
        if visual:
            oldest = self._wave_trail[0] if self._wave_trail else None
            for ring, radii in enumerate(reaches):
                here = sum(radii) / len(radii)
                # How far this ring has travelled since the oldest frame still kept.
                # That is the length of the wake; each ghost is drawn between where the
                # wave is and where it was, so a swell is a soft band inside the line
                # and a ring that has not moved leaves nothing at all.
                was = here
                if oldest is not None and ring < len(oldest) and oldest[ring] \
                        and len(oldest[ring]) == len(radii):
                    was = sum(oldest[ring]) / len(oldest[ring])
                speed = min(WAVE_TRAIL_SPAN, max(0.0, here - was) * WAVE_TRAIL_STRETCH)
                if speed < 0.006:
                    continue
                trail = QColor(self._ring_hue(ring) if self.colourful()
                               else self._frame_colour(colour, energy))
                step = 2 if WAVE_SEGMENTS >= 120 else 1
                for share in (0.28, 0.52, 0.76, 1.0):
                    ink = (WAVE_TRAIL_INK * (1.0 - 0.62 * share)
                           * (0.45 + 0.55 * (0.4 * energy + 0.6 * self._vocal)))
                    if ink < 0.02:
                        continue
                    wake = QColor(trail)
                    wake.setAlphaF(min(0.55, ink))
                    painter.setPen(QPen(wake, WAVE_WIDTH * (1.0 + 1.5 * share),
                                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))
                    ghosts = []
                    for index in range(0, len(radii), step):
                        angle = math.tau * index / float(WAVE_SEGMENTS)
                        at = radii[index] - speed * share
                        ghosts.append(QPointF(cx + math.cos(angle) * at * radius,
                                              cy + math.sin(angle) * at * radius))
                    if len(ghosts) > 1:
                        painter.drawPolyline(ghosts)
                        self._ghosts += 1
            self._wave_prev = {ring: list(radii) for ring, radii in enumerate(reaches)}
            self._wave_trail.append([list(radii) for radii in reaches])
        else:
            self._wave_prev = {}
            self._wave_trail.clear()

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for ring, points in enumerate(shapes):
            outer = ring / float(max(1, WAVE_RINGS - 1))
            if self.colourful():
                line = self._ring_hue(ring)
            elif visual:
                line = QColor(self._frame_colour(colour, energy))
            else:
                line = QColor(colour)          # a still wave in the state's own colour
            # The outer rings are quieter, so the wave reads as depth: the near one is
            # the bright one, and the others are its echo.
            fade = 1.0 - 0.20 * outer
            if visual:
                alpha = (0.34 + 0.50 * overall + 0.26 * energy + 0.22 * self._pulse) * fade
                glow_width = WAVE_WIDTH * WAVE_GLOW * (0.8 + 1.2 * overall) * fade
                ink_width = WAVE_WIDTH * (0.9 + 0.9 * overall + 0.5 * self._pulse) * fade
            else:
                alpha = 0.20 * fade
                glow_width = WAVE_WIDTH * WAVE_GLOW * 0.55 * fade
                ink_width = WAVE_WIDTH * 0.82 * fade
            glow = QColor(line)
            glow.setAlphaF(min(0.60, alpha * 0.60))
            painter.setPen(QPen(glow, glow_width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(points)
            ink = QColor(line)
            ink.setAlphaF(min(1.0, alpha))
            painter.setPen(QPen(ink, ink_width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(points)
        # One bright crest riding the innermost wave: a single moving highlight is what
        # tells the eye the ring is travelling rather than merely pulsing. Only while
        # the room is being heard: a still wave has no crest to travel.
        if visual:
            index = int((now / WAVE_TRAVEL_S) % 1.0 * WAVE_SEGMENTS)
            crest = shapes[0][index]
            spark = QColor(255, 255, 255)
            spark.setAlphaF(min(1.0, 0.35 + 0.65 * energy))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(spark)
            painter.drawEllipse(crest, 1.6 + 2.4 * energy, 1.6 + 2.4 * energy)
        painter.setPen(Qt.PenStyle.NoPen)

    def _frame_colour(self, colour, energy):
        """What colour a wave is: the mode's answer, in one place."""
        if self.colourful():
            # Music Mode: a vivid slice of the wheel, a shade ahead of the body, so the
            # two are unmistakably one object rather than two effects together.
            red, green, blue = colorsys.hsv_to_rgb((self._hue + 0.06) % 1.0, 0.95, 1.0)
            return QColor(round(red * 255), round(green * 255), round(blue * 255))
        if energy > 0.0:
            return QColor(FRAME_BLUE)
        return QColor(FRAME_WHITE)

    def _radius(self, span):
        """The orb's radius, with room for its glow and (floating) for labels.

        The glow reaches 1.12 x the radius and the lattice itself wanders a few per
        cent beyond it, so the radius has to leave that much clear inside the window.
        """
        if self._label_space:
            return span * 0.24
        return span * 0.5 / WAVE_BUDGET - 5.0 / WAVE_BUDGET

    # ---------- the geometry ------------------------------------------------

    def _now(self):
        """The clock the drawing uses. Frozen while asleep, so nothing moves."""
        return self._frozen_at if self.asleep_still() else self.seconds()

    def _turn(self, x, y, z, now):
        """The lattice's own three-dimensional turn: pitch first, then yaw.

        Pitch opens and closes the equator (the near half grows, the far half
        shrinks); yaw walks the far side around behind the near one. Both are the
        same two angles the drag writes, plus the slow idle motion of a body that is
        alive - which is what lets the object be *held* and turned.
        """
        spin = self._angle * math.tau + self._yaw
        pitch = self._pitch + 0.16 * math.sin(now * (math.tau / TILT_S))
        sin_p, cos_p = math.sin(pitch), math.cos(pitch)
        sin_y, cos_y = math.sin(spin), math.cos(spin)
        y1 = y * cos_p - z * sin_p
        z1 = y * sin_p + z * cos_p
        return (x * cos_y + z1 * sin_y, y1, -x * sin_y + z1 * cos_y)

    def _project(self, x, y, z, cx, cy, radius):
        """One turned point, divided by depth: near genuinely larger than far.

        Returns (screen x, screen y, depth 0..1). The perspective is gentle: harder
        than this and the near side swells into a teardrop, and the ratio between the
        halves of the object has to read as depth rather than as damage.
        """
        depth = max(0.0, min(1.0, (z + 1.0) / 2.0))
        near = 1.0 / (1.0 + PERSPECTIVE * (1.0 - z))
        return (cx + x * radius * near, cy + y * radius * near, depth)

    def _ring_points(self, cx, cy, radius, now, lat=0.0):
        """One latitude line of the surface, projected, with a depth per point."""
        shell = math.sqrt(max(0.0, 1.0 - lat * lat))
        points = []
        for index in range(SEGMENTS + 1):
            angle = math.tau * index / SEGMENTS
            x, y, z = self._turn(shell * math.cos(angle), shell * math.sin(angle), lat, now)
            points.append(self._project(x, y, z, cx, cy, radius))
        return points

    def _points(self, cx, cy, radius, now, phase=None):
        """The orb's equator, projected by hand, with a depth per point.

        Kept as the widget's public piece of geometry because the interaction checks
        hold the object by it: it is a real circle on the surface of the sphere, so a
        point on it is a point on the orb.
        """
        return self._ring_points(cx, cy, radius, now, lat=0.0)

    # ---------- painting ----------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        span = min(self.width(), self.height())
        radius = self._radius(span)
        if radius <= 4:
            return
        now = self._now()
        breath = 1.0
        if not self.asleep_still():
            breath = 1.0 + 0.02 * math.sin(now * 1.6) + 0.07 * self._amplitude
        drawn = radius * breath * (1.0 + 0.035 * self._hover)
        if drawn * WAVE_BUDGET > span * 0.5:     # loud and already shrinking: clamp
            drawn = span * 0.5 / WAVE_BUDGET
        colour = self._colour

        self._halo(painter, cx, cy, drawn, colour)
        # The waves go under the lattice: the orb sits *inside* them, so they read as
        # the room the ball is moving in rather than as lines drawn over it.
        self._waves(painter, cx, cy, drawn, now, colour)
        self._lattice(painter, cx, cy, drawn, now, colour)
        self._cage(painter, cx, cy, drawn, now, colour)
        self._rim_ring(painter, cx, cy, drawn * SILHOUETTE * self._skin(now), now, colour)
        self._spikes(painter, cx, cy, drawn, now, colour)
        if self._arc is not None:
            self._progress_arc(painter, cx, cy, drawn, colour)
        self._caption_text(painter, cx, cy, drawn, colour)
        if self._label_space:
            self._floating_text(painter, cx, cy, drawn)

    def _halo(self, painter, cx, cy, radius, colour):
        """The soft bloom behind the lattice, without which it looks like dust.

        Its gradient reaches zero exactly at its own outer edge, so wherever the
        window happens to clip it there is nothing left to cut.
        """
        reach = radius * 1.12
        grad = QRadialGradient(QPointF(cx, cy), reach)
        clear = QColor(colour)
        clear.setAlphaF(0.0)
        # The bloom follows the body's own colour. In Media Mode that is the state's
        # colour and nothing else, which is what keeps the body white while the frame
        # does the reacting.
        base = (QColor.fromHsvF(self._hue, 0.75, 1.0)
                if (self._visual and self.music) else colour)
        body = QColor(base)
        # Weaker than it was: the bloom sits behind dots that are now hard-edged, and
        # a strong one softens exactly the crispness that was asked for.
        body.setAlphaF(0.10 + 0.15 * self._amplitude + 0.07 * self._hover)
        # A soft light *inside* the ball as well as the bloom around it: a sphere of
        # points with nothing behind them looks like a hole in the desktop, and the
        # middle of a lit object is the brightest part of it.
        core = QColor(base)
        # The middle of a lit object is the brightest part of it, and this is the only
        # light *behind* the lattice. It carries more than it used to, because the far
        # side of the ball is what covers the middle of the screen - a sphere seen from
        # outside shows its back half in the centre - and small hard dots there left a
        # hole where soft blobs used to smear across it.
        core.setAlphaF(0.13 + 0.13 * self._amplitude)
        grad.setColorAt(0.0, core)
        grad.setColorAt(0.45, clear)
        grad.setColorAt(0.86, body)
        grad.setColorAt(1.0, clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(grad)
        painter.drawEllipse(QPointF(cx, cy), reach, reach)

    def _tones(self):
        """The few brightness steps a dot may be drawn in, built once per frame.

        A colour per dot means a colour *object* per dot, five hundred times a frame,
        and a gradient brush per dot on top of that - which measured at eighteen
        milliseconds for the frame, more than everything else together. Twelve steps
        are indistinguishable at this size, and they let each step be drawn once into
        a small soft sprite and then blitted.
        """
        if self.colourful():
            # Music Mode: the body walks the same slow hue as the waves, spread across
            # the brightness steps so the ball has depth in colour as well as in
            # opacity.
            steps = []
            for index in range(TONES):
                hue = (self._hue + index / float(TONES) * 0.70) % 1.0
                red, green, blue = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
                steps.append(QColor(round(red * 255), round(green * 255), round(blue * 255)))
            return steps
        return [QColor(min(255, round(min(self._colour.red(), 236) * (0.34 + 0.086 * index))),
                       min(255, round(min(self._colour.green(), 236) * (0.34 + 0.086 * index))),
                       min(255, round(min(self._colour.blue(), 236) * (0.34 + 0.086 * index))))
                for index in range(TONES)]

    def _sprite(self, colour, size, alpha_step):
        """One crisp dot, drawn once and reused.

        This was a soft radial gradient reaching out to three times the dot's own
        width, which is a glowing point and, at four hundred of them, a blur: the
        owner's words were "very blurry and unsettling" and they were right - a 5 px
        dot was really a 20 px blob and the ball was overlapping mush. A dot is a dot
        now: a filled circle with an antialiased rim, exactly as wide as it claims to
        be, and the depth of the ball is carried by its size and its opacity instead of
        by a falloff. It is still a cached pixmap blit, which is a fraction of the cost
        of an ellipse call, so the sharper picture is also the cheaper one.
        """
        key = (colour.rgb(), size, alpha_step)
        sprite = self._sprites.get(key)
        if sprite is not None:
            return sprite
        edge = 2                              # room for the antialiased rim, no more
        span = max(3, int(size) + edge * 2)
        sprite = QPixmap(span, span)
        sprite.fill(Qt.GlobalColor.transparent)
        painter = QPainter(sprite)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        ink = QColor(colour)
        ink.setAlphaF(min(1.0, (alpha_step + 1) / float(ALPHAS + 1)))
        painter.setBrush(ink)
        reach = max(0.5, int(size) / 2.0)
        painter.drawEllipse(QPointF(span / 2.0, span / 2.0), reach, reach)
        painter.end()
        if len(self._sprites) > SPRITE_CACHE_MAX:
            self._sprites.clear()          # a colour ramp: drop the old steps
        self._sprites[key] = sprite
        return sprite

    def _lattice(self, painter, cx, cy, radius, now, colour):
        """The sphere itself: every dot turned, lit, and drawn far side first.

        Depth order is what makes translucency read as volume instead of noise: the
        far dots go down first, and the near ones - bigger, brighter, more solid -
        land on top of them. Each dot's alpha is low enough on its own to see
        through, so the two halves of the ball overlap into a body with a middle.
        """
        amplitude = self._amplitude
        visual = self._visual
        # The skin's own displacement, and there is exactly one of it: every skin dot
        # moves by this same factor, so the outline stays a circle while the ball
        # breathes and answers the audio. It used to be per dot - `BREATH * sin(now +
        # phase)` and a swell keyed to the dot's own phase - which moved neighbouring
        # dots of the edge by up to a quarter of the radius in different directions:
        # a wobbling ring, which is the other half of "not a dotted circle".
        skin = self._skin(now)
        rows = []
        for index, (x, y, z) in enumerate(DOTS_AT):
            phase = PHASE[index]
            if index < SHELL_DOTS:
                shell = skin                    # the edge: one motion, for all of it
            else:
                shell = 1.0 + BREATH * math.sin(1.6 * now + phase)
                if visual:
                    band = self._bands[BAND_OF[index]]
                    shell += VIS_SWELL * band * (0.45 + 0.55 * math.sin(5.2 * now + 2.7 * phase))
                if amplitude:
                    shell += SWELL * amplitude * (0.45 + 0.55 * math.sin(5.4 * now + 2.7 * phase))
                shell = min(shell, 1.06)
            nx, ny, nz = self._turn(x * shell, y * shell, z * shell, now)
            px, py, depth = self._project(nx, ny, nz, cx, cy, radius)
            rows.append((px, py, depth, nx, ny, nz, index))
        rows.sort(key=lambda row: row[2])
        scale = radius / 110.0                       # dot sizes follow the widget
        tones = self._tones()
        glint = self._sprite(QColor(255, 255, 255), 2, ALPHAS)
        painter.setPen(Qt.PenStyle.NoPen)
        for px, py, depth, nx, ny, nz, index in rows:
            # The back of the ball is the dimmest, smallest and most overlapped part
            # of it, so half of those dots are left out: the frame is a fifth cheaper
            # and the picture is the same, because the near dots are lying on top.
            if depth < FAR_SKIP and index % 2:
                continue
            facing = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
            # Three lights in one: the key light on the facing side, a rim where the
            # surface turns away from the eye - which is what makes glass look like
            # glass - and the state colour's own brightness.
            rim = max(0.0, 1.0 - abs(facing) / RIM) * (0.35 + 0.65 * depth)
            lit = 0.42 + 0.58 * (0.5 + 0.5 * facing) + 0.55 * rim
            # A little variety per dot, from its own phase: a lattice of identical
            # points looks printed, and the unevenness is what makes glass sparkle.
            jitter = 1.0 + SPREAD * (phase_sin(index) * 0.5 + phase_cos(index) * 0.5)
            if index < SHELL_DOTS:
                # A third of that variety on the edge. Sparkle on the inside reads as
                # glass; sparkle on the outline reads as a shape that cannot hold a
                # line, and the edge is the one place the eye is checking.
                jitter = 1.0 + SPREAD * 0.35 * phase_sin(index)
            tone = min(1.0, lit / 1.45)
            if visual:
                tone = min(1.0, tone + 0.5 * self._bands[BAND_OF[index]])
            colour = tones[min(TONES - 1, int(tone * TONES))]
            alpha = (0.20 + 0.38 * depth) * lit * jitter \
                + (0.16 if visual else 0.10) * self._amplitude
            step = max(1, min(ALPHAS, int(alpha * ALPHAS)))
            # Capped, because a blit costs its *area*: the panel draws the orb at twice
            # the floating size, and uncapped dots there cost eighteen milliseconds a
            # frame to look blurrier than small ones do. A particle field wants fine
            # points anyway, so the cap is the better picture as well as the cheap one.
            size = max(2, min(SIZE_MAX, int(round((1.30 + 2.40 * depth)
                                                  * (0.85 + 0.45 * lit) * jitter * scale))))
            sprite = self._sprite(colour, size, step)
            painter.drawPixmap(int(px - sprite.width() / 2), int(py - sprite.height() / 2),
                               sprite)
            if lit > 1.05:                           # the glints stay few and small
                painter.drawPixmap(int(px - glint.width() / 2),
                                   int(py - glint.height() / 2), glint)

    def _skin(self, now):
        """The one displacement every point of the sphere's surface shares.

        Extracted so the ring is drawn at exactly the radius the skin is drawn at:
        two copies of that sum would drift apart the moment one of them gained a
        term, and the outline would sit imperceptibly off the ball it belongs to.
        """
        skin = 1.0 + BREATH * math.sin(1.6 * now)
        if self._visual:
            # The room's music swells the body in Music Mode. Media Mode keeps it calm
            # by design - the frame is where the sound shows - so the body only takes
            # the overall level there, at a fraction of the depth.
            room = sum(self._bands) / float(max(1, len(self._bands)))
            skin += (VIS_SWELL if self.music else VIS_SWELL * 0.28) * room
            if self.music:
                # And the sung line on top of the mix: the ball swells as the vocal
                # swells, which is the pulse the owner reads as Retro singing along.
                # Media Mode leaves the body alone and puts all of it in the waves.
                skin += VIS_SWELL * 0.55 * self._vocal
        if self._amplitude:
            skin += SWELL * self._amplitude * (0.45 + 0.55 * math.sin(5.4 * now))
        return min(skin, 1.06)

    def _rim_ring(self, painter, cx, cy, radius, now, colour):
        """The edge of the ball: one dot per equal slice of the circle, and nothing else.

        The sphere's own dots cannot draw this line. They are spread evenly over the
        *surface*, so projected they bunch where the surface turns edge-on and thin
        where it faces the eye, and each dot's own size and brightness moves the
        outermost pixel of the shape - which is why the edge read as a scatter that
        nearly formed a circle rather than as a circle. This is the circle itself:
        at least `RING_DOTS` dots at exactly the silhouette radius the projection has,
        at exactly equal angles, all the same size - more of them on a larger orb, so
        the spacing stays constant as the circle grows. It shares the sphere's spin,
        so the line holds at every angle, and it is lit by the same key light, so it
        belongs to the same object instead of floating around it.
        """
        if radius <= 6:
            return
        # Drawn as vector dots at floating-point positions rather than blitted
        # sprites. A sprite has to land on a whole pixel, so a ring of them has one
        # or two pixels of jitter in its spacing at small sizes - which is exactly
        # what "does not look like a *perfect* dotted circle" looks like. An
        # antialiased ellipse is placed at the real coordinate, so the spacing is the
        # geometry and not the pixel grid.
        #
        # The dot's own width follows the orb, and the count is derived from the
        # circumference: the spacing stays the same at 120 px and at 420 px instead of
        # the ring thinning out as it grows. Every dot is exactly the same size, on
        # exactly the same radius, at exactly equal angles - the three things that
        # make the eye read a circle rather than a scatter.
        dot = max(0.9, min(2.6, RING_SIZE * 0.46 * max(0.8, radius / 110.0)))
        count = max(RING_DOTS, min(220, int(round(math.tau * radius / (dot * 3.4)))))
        tones = self._tones()
        spin = self._angle * math.tau + self._yaw
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(count):
            angle = math.tau * index / count + spin
            x, y = math.cos(angle), math.sin(angle)
            # The same key light the ball has, read on the rim: the upper-left arc is
            # the brightest. A slow shimmer walks the line so it is alive - it is a
            # function of the angle, so it closes on itself and never breaks the ring.
            lit = 0.55 + 0.45 * (0.5 + 0.5 * (x * LIGHT[0] + y * LIGHT[1]))
            shimmer = 0.5 + 0.5 * math.sin(angle * 3.0 - now * 2.0)
            tone = min(1.0, 0.55 * lit + 0.45 * shimmer)
            ink = QColor(tones[min(TONES - 1, int(tone * TONES))])
            ink.setAlphaF(min(1.0, 0.52 + 0.48 * lit))
            painter.setBrush(ink)
            painter.drawEllipse(QPointF(cx + x * radius, cy + y * radius), dot, dot)

    def _cage(self, painter, cx, cy, radius, now, colour):
        """Four latitude lines across the *near* half: the curvature made visible.

        Only the segments whose depth is on the near side are drawn, and drawn after
        the lattice, because a thin line laid over the dots is what turns a swarm of
        points back into a sphere. The far halves are left out rather than drawn and
        covered: that is both cheaper and honest.

        The segments are gathered into depth buckets first and each bucket is stroked
        in one call. Per-segment pens were costing more than everything else in the
        frame put together - two hundred and eighty-eight of them, sixty times a
        second, to draw lines that only differ in brightness.
        """
        if self.asleep_still():
            return                       # asleep draws no lattice lines: nothing lives
        buckets = [[] for _ in range(CAGE_STEPS)]
        for lat in CAGE_LATS:
            points = self._ring_points(cx, cy, radius, now, lat=lat)
            for index in range(SEGMENTS):
                x0, y0, d0 = points[index]
                x1, y1, d1 = points[index + 1]
                depth = (d0 + d1) / 2.0
                if depth < 0.55:
                    continue                    # the far half: behind the solid
                step = min(CAGE_STEPS - 1, int((depth - 0.55) / 0.45 * CAGE_STEPS))
                buckets[step].append(QLineF(x0, y0, x1, y1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step, lines in enumerate(buckets):
            if not lines:
                continue
            depth = 0.55 + (step + 0.5) / CAGE_STEPS * 0.45
            ink = QColor(QColor.fromHsvF(self._hue, 0.90, 1.0) if self.colourful()
                         else colour)
            ink.setAlphaF(min(1.0, 0.10 + 0.26 * (depth - 0.55) / 0.45
                              + 0.10 * self._hover))
            pen = QPen(ink)
            pen.setWidthF(0.5 + 0.8 * depth)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLines(lines)

    def _spikes(self, painter, cx, cy, radius, now, colour):
        """The level meter, standing out of the surface: the real audio, and nothing
        when silent.

        It used to be a row of bars drawn straight across the middle of the circle,
        which read as a line through an empty shape and fought the ring for the eye.
        Spikes standing out of the surface are the same honest reading of the
        microphone or the played audio, in the one direction the drawing already has
        - outward - so loud speech makes the orb bristle instead of filling it in.
        Silence still draws nothing at all.
        """
        amplitude = self._amplitude
        if amplitude < 0.02 or self.asleep_still():
            return          # silence draws nothing, and sleep draws nothing at all
        if self._visual:
            # The microphone's meter has no business in the background look. Its lines
            # start *inside* the ball, so with the room playing they read as red
            # scratches across it - and there is no microphone in this mode anyway.
            # The waves and the body carry the room's audio; nothing else should.
            return
        points = self._points(cx, cy, radius, now)
        step = max(3, SEGMENTS // 18)
        base = QColor(QColor.fromHsvF(self._hue, 0.95, 1.0) if self.colourful() else colour)
        for index in range(0, SEGMENTS, step):
            x, y, depth = points[index]
            dx, dy = x - cx, y - cy
            reach = math.hypot(dx, dy) or 1.0
            flicker = 0.35 + 0.65 * abs(math.sin(now * 17 + index * 0.8))
            length = amplitude * radius * 0.20 * flicker * (0.55 + 0.45 * depth)
            spike = QColor(base)
            spike.setAlphaF(min(1.0, (0.30 + 0.70 * amplitude) * (0.5 + 0.5 * depth)))
            pen = QPen(spike)
            pen.setWidthF(max(1.1, 0.9 + 1.5 * depth))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, y),
                             QPointF(x + dx / reach * length, y + dy / reach * length))

    def _progress_arc(self, painter, cx, cy, radius, colour):
        """How far through a job of known length is, as a ring you can read at a glance.

        Only drawn when something really counted the steps - the sections of a
        document, say - so a half-filled arc means half written. The head of the arc
        carries a brighter dot, because a taper is easier to read from across a desk
        than a change in brightness.
        """
        reach = min(radius * 1.14, min(self.width(), self.height()) * 0.5 - 3.0)
        box = QRectF(cx - reach, cy - reach, reach * 2, reach * 2)
        track = QColor(colour)
        track.setAlphaF(0.13)
        pen = QPen(track)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(box, 90 * 16, -360 * 16)
        sweep = max(0.02, self._arc) * 360.0
        head = QColor(colour)
        head.setAlphaF(0.95)
        pen = QPen(head)
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(box, 90 * 16, int(-sweep * 16))
        angle = math.radians(90.0 - sweep)
        dot = QPointF(cx + math.cos(angle) * reach, cy - math.sin(angle) * reach)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(head)
        painter.drawEllipse(dot, 2.6, 2.6)

    def _caption_text(self, painter, cx, cy, radius, colour):
        """What Retro is doing, over the middle of the orb, in small capitals.

        Fed by the phase the session reports, so it is a statement of fact about the
        running request. It is skipped on an orb too small to hold a word without
        shouting, and while the visualiser is running - a music visualiser with
        "SAY 'RETRO'" written across it is not decoration, it is a wrong sign.

        The transparent display is text-free. A circle floating over somebody's
        desktop is an ornament, and words laid across it read as clutter on top of
        their work; the colour and the motion already say whether Retro is asleep,
        listening, working or speaking. The settings panel keeps the caption, because
        there the words are the interface.

        The word is drawn twice, a dark copy a pixel low and then a bright one,
        because the middle of a lit ball is the brightest part of it.
        """
        if self._label_space or not self._caption or radius < 34 or self._visual:
            return
        font = QFont(painter.font())
        font.setPixelSize(max(8, int(radius * 0.115)))
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(0.6, radius * 0.035))
        painter.setFont(font)
        box = QRectF(cx - radius * 0.92, cy - radius * 0.24, radius * 1.84, radius * 0.48)
        shadow = QColor(0, 0, 0)
        shadow.setAlphaF(0.42)
        painter.setPen(QPen(shadow))
        painter.drawText(box.translated(0.0, max(1.0, radius * 0.012)),
                         int(Qt.AlignmentFlag.AlignCenter), self._caption)
        ink = QColor(min(255, round(colour.red() * 0.30 + 190)),
                     min(255, round(colour.green() * 0.30 + 190)),
                     min(255, round(colour.blue() * 0.30 + 190)))
        ink.setAlphaF(0.95)
        painter.setPen(QPen(ink))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), self._caption)

    def _floating_text(self, painter, cx, cy, radius):
        """Short labels from real execution events, above and below the orb.

        Not speech and not decoration: each line is put here by something that
        actually happened - a step planned, a window opened, a file written - and
        it fades out on its own. Static labels for the UI would be fine here; fake
        activity would not, which is why the panel is the only place that shows
        the "running" kind of word.
        """
        if self._visual or self._label_space:
            # Asleep and visualising, or the transparent display: no words at all.
            # The floating shape is the orb and nothing else - the mode label and the
            # progress lines belong to the panel, where there is room to read them.
            return
        font = QFont(painter.font())
        font.setPixelSize(max(9, int(radius * 0.105)))
        painter.setFont(font)
        now = time.monotonic()
        for index, (text, expires) in enumerate(self._floats):
            left = expires - now
            if left <= 0:
                continue
            alpha = min(1.0, left / 1.2) * min(1.0, (FLOAT_LIFE_S - left) / 0.35 + 0.35)
            ink = QColor(self._colour)
            ink.setAlphaF(max(0.0, min(0.85, alpha * 0.85)))
            painter.setPen(QPen(ink))
            row = index + 1
            y = cy - radius * (1.16 + 0.26 * (row - 1))
            painter.drawText(QRectF(cx - radius * 1.6, y - radius * 0.16,
                                    radius * 3.2, radius * 0.32),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                             text)
        if self._mode:
            ink = QColor(self._colour)
            ink.setAlphaF(0.55)
            painter.setPen(QPen(ink))
            small = QFont(font)
            small.setPixelSize(max(8, int(radius * 0.085)))
            small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
            painter.setFont(small)
            painter.drawText(QRectF(cx - radius * 1.6, cy + radius * 1.08,
                                    radius * 3.2, radius * 0.28),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                             self._mode.upper())
