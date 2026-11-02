"""The arrival: Retro's wordmark assembles itself, then the screen fades away again.

The owner asked for exactly this, and described the moment: *I open the laptop, type
my password, and the logo plays - then Retro says "Retro's ready" and I have my
control back.* So this is not a browser page and not a video file: it is a frameless
full-screen window drawn by the same toolkit the rest of the app uses, over the
desktop, at logon, before the floating circle.

The shape of it, and why:

* **Six point four seconds of reveal, then a brisk one-and-a-half fade.** The reveal
  is the timeline in `BEATS`: five letters, each with its own entrance, then the whole
  word held for a long breath - with a second swell of light partway through, so the
  hold is not a still picture - before the window fades to nothing over `FADE_S`, so
  the desktop - and Retro's own circle, which is already there underneath - comes back
  gradually rather than snapping.
* **Nothing needs the volume, honestly.** Every sound is generated as samples by
  `core.sound`; there is no file to ship and no browser autoplay policy to fight. If
  the machine has no output device at all, the animation still plays and only the
  sound is missing.
* **It cannot overstay.** The animation is driven by a clock, not by frames, and the
  countdown to the end is one timer: a slow or busy machine gets fewer frames, never
  a longer splash. `HARD_CAP_S` is the last word.
* **It never takes the machine hostage.** No input grab, no window activation: the
  splash is shown above everything and then goes away. Escape also ends it early, and
  `RETRO_NO_INTRO` skips it entirely.

The letters are the same five the HTML sketch draws, in the same order and colours,
with the same construction - a single stroke weight, a white R, a blue E that
assembles out of particles, a gold T that is wiped in, an electric-blue R with an
outline, and a red O traced round - because that sketch is where this came from.
"""
import os

from . import log

# The arrival, timed. The owner asked for longer: the first version held the finished
# wordmark for two seconds and then spent three fading, which read as a five-second
# splash with a long goodbye. The reveal is unchanged - those beats are tuned to the
# soundtrack - and the *hold* is what grew, with a second breath of light at 4.2 s so
# six seconds of logo is not six seconds of a still picture.
INTRO_S = 6.4                               # reveal + hold
FADE_S = 1.6                                # then the window goes away, briskly
SOUND_S = 8.1                               # a little past the reveal, so the last
                                            # chime decays into the fade
# A whole second of grace on top of the two, so a stalled frame can never make the
# splash outlast them.
HARD_CAP_S = INTRO_S + FADE_S + 1.0
# Sixty frames a second: the reveal is a light moving across a dark screen, which is
# exactly the kind of motion thirty frames makes look stepped. The letters are cached
# pixmaps, so a frame is a handful of blits either way.
FPS_MS = 16

# --- the five letters, as the sketch draws them ------------------------------
LETTERS = (
    {"key": "R", "color": "#F5F7FF", "width": 0.60},
    {"key": "E", "color": "#4B8DFF", "width": 0.52},
    {"key": "T", "color": "#F5C451", "width": 0.58},
    {"key": "R", "color": "#3985FF", "width": 0.60},
    {"key": "O", "color": "#FF4D5F", "width": 0.70},
)
GAPS = (0.15, 0.09, 0.08, 0.12)
STROKE = 0.17          # stroke weight as a fraction of cap height
CAP_OF_SCREEN = 0.20   # cap height as a fraction of the screen's height
WORD_WIDE = 0.78       # ...and never wider than this share of the screen

# name, start, end. Every beat lands inside INTRO_S, and the test says so.
BEATS = (
    ("r_first", 0.00, 0.42),
    ("e_particles", 0.42, 0.98),
    ("t_wipe", 0.98, 1.40),
    ("r_second", 1.40, 1.84),
    ("o_trace", 1.84, 2.34),
    ("lock", 2.34, 2.80),
    # The hold is four seconds of finished wordmark now, and the window's second
    # breath of light at 4.2 s lives in the picture, not in this list: the beats are
    # the letters' entrances, and they are sequential by design.
    ("hold", 2.80, 6.40),
)


def beats():
    """The timeline, as (name, start, end) - for tests and for the preview tool."""
    return tuple(BEATS)


def total_seconds():
    """How long the splash is on screen, reveal plus fade."""
    return INTRO_S + FADE_S


def wanted(environ=None, argv=()):
    """Is the arrival wanted on this run?

    `--intro` forces it (which is how it is replayed from the tray), `--no-intro`
    and `--selftest` skip it, and `RETRO_NO_INTRO` skips it for anything that drives
    the app headlessly.
    """
    try:
        env = os.environ if environ is None else environ
        # Be defensive - env might be passed as argv by mistake
        if isinstance(env, (list, tuple)):
            argv = env
            env = os.environ
        if hasattr(env, 'get'):
            if str(env.get("RETRO_NO_INTRO", "")).strip().lower() in ("1", "true", "yes", "on"):
                return False
        elif isinstance(env, dict):
            if str(env.get("RETRO_NO_INTRO", "")).strip().lower() in ("1", "true", "yes", "on"):
                return False
        flags = set(str(a) for a in argv)
        if "--no-intro" in flags or "--selftest" in flags:
            return False
        return True
    except Exception:
        return False


def progress(t):
    """What the five letters are doing at time `t`, with no drawing involved.

    Kept separate from the painting so the timeline can be tested - and previewed -
    on a machine with no display: each letter gets a 0..1 reveal, a glow, and whether
    the word is being held yet.
    """
    def between(start, end):
        if end <= start:
            return 1.0 if t >= end else 0.0
        return max(0.0, min(1.0, (t - start) / (end - start)))

    beats_at = {name: (start, end) for name, start, end in BEATS}
    reveals = [
        between(beats_at["r_first"][0], beats_at["r_first"][1] + 0.10),
        between(beats_at["e_particles"][0], beats_at["e_particles"][1] - 0.08),
        between(beats_at["t_wipe"][0], beats_at["t_wipe"][1] - 0.10),
        between(beats_at["r_second"][0], beats_at["r_second"][1] - 0.06),
        between(beats_at["o_trace"][0], beats_at["o_trace"][1] - 0.04),
    ]
    locked = t >= beats_at["lock"][0]
    # The glow of the finished word, quiet by the end so the fade is the brightest
    # thing left on screen.
    settle = 0.42 if locked else 0.0
    if t >= beats_at["hold"][0]:
        settle = max(0.24, settle - 0.18 * between(beats_at["hold"][0], INTRO_S))
    return {"t": t, "reveals": reveals, "locked": locked, "settle": settle,
            "particles": max(0.0, min(1.0, 1.0 - between(beats_at["e_particles"][1] - 0.4,
                                                          beats_at["e_particles"][1] + 0.5))),
            "shake": between(beats_at["lock"][0], beats_at["lock"][0] + 0.5)}


# ---------- the soundtrack --------------------------------------------------

def soundtrack(rate=None, seconds=SOUND_S):
    """The whole arrival as one buffer of samples.

    Beat for beat with the picture: a low power-up under the first R, a rising breath
    and a scatter of bells while the E comes together, a metallic shimmer for the gold
    T, a bright streak for the second R, a rising trace for the red O, and then the
    lock - a bass note with a warm chord over it - followed by a settling chime into
    the hold. It is written to be quiet: a splash that is loud at logon is a splash
    people turn off.
    """
    from . import sound
    rate = rate or sound.DEFAULT_RATE
    buffer = sound.silence(seconds, rate)

    # 0.00 | the first R: air coming up, a low body under it, one clean bell.
    sound.noise_sweep(buffer, rate, when=0.02, duration=0.5, from_hz=70.0, to_hz=1400.0,
                      volume=0.16, seed=741)
    sound.tone(buffer, rate, when=0.03, duration=0.45, freq=56.0, end_freq=77.0, volume=0.30,
               attack=0.06, decay=2.0)
    sound.bell(buffer, rate, when=0.14, duration=0.5, freq=392.0, volume=0.09)

    # 0.42 | the E assembles: a breath, a rising body, small crystal pings.
    sound.noise_sweep(buffer, rate, when=0.40, duration=0.62, from_hz=240.0, to_hz=2300.0,
                      volume=0.14, seed=1801)
    sound.tone(buffer, rate, when=0.42, duration=0.5, freq=174.6, end_freq=392.0, volume=0.16,
               attack=0.04, decay=2.4)
    for index, freq in enumerate((740.0, 988.0, 830.0, 1174.0, 880.0, 1046.0)):
        sound.bell(buffer, rate, when=0.44 + index * 0.075, duration=0.16, freq=freq, volume=0.045)

    # 0.98 | the gold T: a metallic wipe.
    sound.noise_sweep(buffer, rate, when=0.98, duration=0.36, from_hz=1900.0, to_hz=820.0,
                      volume=0.12, seed=2207)
    sound.bell(buffer, rate, when=1.00, duration=0.46, freq=587.33, volume=0.10)
    sound.bell(buffer, rate, when=1.05, duration=0.34, freq=880.0, volume=0.05)

    # 1.40 | the electric R: a controlled streak.
    sound.noise_sweep(buffer, rate, when=1.40, duration=0.40, from_hz=1550.0, to_hz=430.0,
                      volume=0.14, seed=3301)
    sound.tone(buffer, rate, when=1.41, duration=0.30, freq=230.0, end_freq=520.0, volume=0.13,
               attack=0.02, decay=2.6)

    # 1.84 | the red O: the trace rises and closes on a soft pulse.
    sound.noise_sweep(buffer, rate, when=1.84, duration=0.42, from_hz=620.0, to_hz=1550.0,
                      volume=0.11, seed=4409)
    sound.tone(buffer, rate, when=1.86, duration=0.40, freq=293.66, end_freq=392.0, volume=0.13,
               attack=0.04, decay=2.4)
    sound.tone(buffer, rate, when=2.26, duration=0.30, freq=82.41, end_freq=55.0, volume=0.24,
               attack=0.01, decay=2.0)

    # 2.34 | the lock: one bass note, a warm major chord, the chime that lands it.
    sound.noise_sweep(buffer, rate, when=2.34, duration=0.24, from_hz=210.0, to_hz=65.0,
                      volume=0.16, seed=5511)
    sound.tone(buffer, rate, when=2.35, duration=0.9, freq=55.0, end_freq=43.65, volume=0.32,
               attack=0.02, decay=1.6)
    for freq, volume in ((220.0, 0.15), (329.63, 0.11), (440.0, 0.08), (659.25, 0.05)):
        sound.tone(buffer, rate, when=2.36, duration=0.95, freq=freq, volume=volume,
                   attack=0.04, decay=1.8)
    sound.bell(buffer, rate, when=2.46, duration=0.62, freq=880.0, volume=0.035)

    # 2.80 | the hold: almost nothing, which is what makes the fade feel calm.
    sound.noise_sweep(buffer, rate, when=2.90, duration=1.1, from_hz=900.0, to_hz=160.0,
                      volume=0.06, seed=6613)
    sound.bell(buffer, rate, when=3.30, duration=0.9, freq=523.25, volume=0.03)

    # 4.20 | the second breath: the same warm chord an octave up, quiet, under a slow
    # rise of air. It is what tells the eye - and the ear - that the held word is
    # still alive rather than a frozen frame waiting to be dismissed.
    sound.noise_sweep(buffer, rate, when=4.18, duration=0.9, from_hz=180.0, to_hz=1150.0,
                      volume=0.05, seed=7717)
    for freq, volume in ((440.0, 0.055), (659.25, 0.042), (880.0, 0.030)):
        sound.tone(buffer, rate, when=4.22, duration=1.3, freq=freq, volume=volume,
                   attack=0.18, decay=1.5)
    sound.bell(buffer, rate, when=4.46, duration=0.7, freq=1318.5, volume=0.022)

    # 5.60 | settling: one low note under a distant bell, so the sound is still there
    # when the fade begins and stops with the picture rather than before it.
    sound.tone(buffer, rate, when=5.60, duration=1.9, freq=110.0, end_freq=82.41, volume=0.12,
               attack=0.30, decay=1.2)
    sound.bell(buffer, rate, when=5.85, duration=1.2, freq=659.25, volume=0.020)
    return sound.normalize(buffer, 0.72)
