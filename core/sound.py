"""Sound Retro makes itself: samples, generated here, played on the chosen output.

The voice is not the only thing this assistant says, and two moments had no sound at
all. Both were noticed:

* **The arrival.** The logo assembles itself at logon. Seconds of animation in
  silence over a black screen read as a machine that hung.
* **A timer reaching zero.** A timer that only writes a line into a log is a timer
  the owner has to keep watching for.

So there is one place that can make a tone, and one place that plays samples. No
audio files to ship, no network, nothing new to install: `numpy` is already here for
the microphone, and `sounddevice` is already here for the voice.

Playing is deliberately the *same* path the voice takes - the rate is negotiated with
the device (`voice.playable_rate`), the samples are resampled to it, and the system
default is tried when the chosen device refuses. Choosing an output in the window
once made the assistant mute, because speech was handed to a device that would not
open; a chime must not repeat that, so it inherits the cure rather than inventing a
second way to be silent.
"""
import math
import threading
import time

from . import log

DEFAULT_RATE = 44100
BLOCK = 1024
# How long past the end of the audio a stream may take to drain before it is called
# stuck. Generous, because this is a deadline for "did it play", not for the ears.
PLAY_MARGIN_S = 1.5


# ---------- generating samples ----------------------------------------------

def silence(seconds, rate=DEFAULT_RATE):
    """A buffer of nothing, the base every cue is written into."""
    import numpy as np
    return np.zeros(max(1, int(round(seconds * rate))), dtype="float32")


def seconds_of(audio, rate=DEFAULT_RATE):
    """How long a buffer lasts, which is what a playback is waited for."""
    return len(audio) / float(rate or 1)


def _mix(buffer, at, block):
    """Adds a block into the buffer at a sample offset, clipped to the buffer."""
    import numpy as np
    start = max(0, int(at))
    if start >= len(buffer) or len(block) == 0:
        return
    count = min(len(block), len(buffer) - start)
    buffer[start:start + count] += np.asarray(block[:count], dtype="float32")


def _envelope(count, rate, attack, decay):
    """A rise and an exponential fall, which is what keeps a tone from clicking."""
    import numpy as np
    env = np.ones(count, dtype="float32")
    rise = min(count, max(1, int(attack * rate)))
    env[:rise] *= np.linspace(0.0, 1.0, rise, dtype="float32")
    if decay:
        tail = np.linspace(0.0, 1.0, count, dtype="float32")
        env *= np.power(1.0 - tail, decay).astype("float32")
    return env


def tone(buffer, rate=DEFAULT_RATE, when=0.0, duration=0.4, freq=440.0,
         volume=0.2, attack=0.01, decay=3.0, end_freq=None):
    """One sine that can glide, added into the buffer at `when` seconds."""
    import numpy as np
    count = max(1, int(round(duration * rate)))
    time_axis = np.arange(count, dtype="float32") / float(rate)
    start, finish = float(freq), float(end_freq if end_freq else freq)
    if abs(finish - start) > 0.5:
        # An exponential glide sounds even to the ear where a linear one does not.
        sweep = start * np.power(finish / start, np.clip(time_axis / duration, 0, 1))
    else:
        sweep = np.full(count, start, dtype="float32")
    phase = np.cumsum(sweep, dtype="float64") * (2.0 * math.pi / rate)
    block = np.sin(phase).astype("float32") * _envelope(count, rate, attack, decay)
    _mix(buffer, int(round(when * rate)), block * float(volume))
    return buffer


def bell(buffer, rate=DEFAULT_RATE, when=0.0, duration=1.0, freq=880.0, volume=0.2):
    """A struck chime: a fundamental with two quiet, slightly detuned partials."""
    tone(buffer, rate, when, duration, freq, volume, attack=0.004, decay=4.2)
    tone(buffer, rate, when + 0.004, duration * 0.62, freq * 2.76, volume * 0.22,
         attack=0.003, decay=5.0)
    tone(buffer, rate, when + 0.008, duration * 0.34, freq * 5.41, volume * 0.06,
         attack=0.002, decay=6.0)
    return buffer


def noise_sweep(buffer, rate=DEFAULT_RATE, when=0.0, duration=0.6, from_hz=200.0,
                to_hz=2000.0, volume=0.12, seed=741):
    """Filtered noise whooshing from one band to another.

    The noise is deterministic, so a cue sounds the same on every machine and a test
    can compare two runs. It is filtered in blocks of a few hundred samples with the
    cutoff moving between them - a per-sample Python loop over a whole second of
    audio, which this does not need to be.
    """
    import numpy as np
    count = max(1, int(round(duration * rate)))
    # The same cheap linear congruential generator the test tolerances expect.
    step = 1103515245
    state = int(seed) & 0xFFFFFFFF
    values = np.empty(count, dtype="float32")
    for index in range(count):
        state = (state * step + 12345) & 0xFFFFFFFF
        values[index] = (state / 2147483647.5) - 1.0
    out = np.zeros(count, dtype="float32")
    window = 256
    level = 0.0
    for offset in range(0, count, window):
        chunk = values[offset:offset + window]
        cutoff = float(from_hz) * (float(to_hz) / float(from_hz)) ** (offset / float(count))
        coefficient = 1.0 - math.exp(-2.0 * math.pi * max(20.0, cutoff) / rate)
        smoothed = np.empty(len(chunk), dtype="float32")
        for index in range(len(chunk)):
            level += coefficient * (float(chunk[index]) - level)
            smoothed[index] = level
        out[offset:offset + len(chunk)] = smoothed
    # The filter eats most of the level, and how much depends on where the sweep is:
    # loud and thin at the top, quiet and thick at the bottom. Scaling to a known peak
    # first is what makes `volume` mean the same thing at both ends of the sweep.
    loudest = float(np.max(np.abs(out))) if count else 0.0
    if loudest > 1e-6:
        out = out / loudest
    envelope = _envelope(count, rate, 0.05, 1.6)
    _mix(buffer, int(round(when * rate)), out * envelope * float(volume))
    return buffer


def normalize(buffer, peak=0.85):
    """Scales a finished cue to a known loudness, so cues match each other."""
    import numpy as np
    loudest = float(np.max(np.abs(buffer))) if len(buffer) else 0.0
    if loudest <= 1e-6:
        return buffer
    return (buffer * (float(peak) / loudest)).astype("float32")


# ---------- playing them ----------------------------------------------------

def play(audio, rate=DEFAULT_RATE, device=None, volume=1.0, deadline_s=None,
         started=None):
    """Plays a mono buffer and waits for it. True when it really came out.

    The rate the device will accept is asked for first and the audio is resampled to
    it; a device that refuses at any rate is dropped in favour of the system default,
    exactly as the voice does it. Silence is never reported as success: a stream that
    opened and then never asked for a block returns False.

    `started` is a `threading.Event` set the moment the first block is really being
    played. Opening an output stream takes a tenth of a second or more - asking for
    the device, negotiating its rate, starting WASAPI - and anything that begins
    drawing at the same time as this call begins is a tenth of a second ahead of the
    sound. The arrival uses it to start its clock on the first sample instead.
    """
    import numpy as np
    from .voice import forget_rates, playable_rate, resample
    samples = np.clip(np.asarray(audio, dtype="float32") * float(volume), -1.0, 1.0)
    if len(samples) == 0:
        return False
    import sounddevice as sd
    tries = [None] if device is None else [device, None]
    for attempt in tries:
        target = playable_rate(attempt, rate)
        if target is None:
            forget_rates()
            continue
        try:
            played = _play_on(sd, attempt, resample(samples, rate, target), target,
                              deadline_s, started)
        except Exception as exc:
            log.event("cue_failed", device=attempt, error=f"{type(exc).__name__}: {exc}"[:120])
            continue
        if played:
            return True
    log.event("cue_unheard", device=device, samples=len(samples))
    return False


def _play_on(sd, device, samples, rate, deadline_s=None, started=None):
    """One attempt on one device, in the voice's own shape: a callback drives it."""
    import numpy as np
    cursor = [0]
    began = threading.Event()
    finished = threading.Event()

    def callback(outdata, frames, _time, _status):
        began.set()
        chunk = samples[cursor[0]:cursor[0] + frames]
        cursor[0] += len(chunk)
        outdata[:len(chunk), 0] = chunk
        if len(chunk) < frames:
            outdata[len(chunk):] = 0.0
            finished.set()
            raise sd.CallbackStop

    stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                             device=device, blocksize=BLOCK, callback=callback)
    with stream:
        if not began.wait(2.0):
            return False                      # opened, then never asked for a block
        if started is not None:
            try:
                started.set()
            except Exception:
                pass
        budget = deadline_s if deadline_s else seconds_of(samples, rate) + PLAY_MARGIN_S
        return bool(finished.wait(budget))


def play_async(audio, rate=DEFAULT_RATE, device=None, volume=1.0, started=None):
    """Plays a cue on a worker thread. Returns the thread, so a caller can wait.

    The arrival plays its soundtrack while the window is animating and a timer plays
    its chime from the watcher thread: neither can block on a device. `started` is
    passed through to `play`: it fires when the first block is really playing.
    """
    thread = threading.Thread(target=play, args=(audio, rate, device, volume, None, started),
                              daemon=True)
    thread.start()
    return thread


def wait(thread, timeout=0.8):
    """Waits for a cue's thread, briefly. Used before the voice starts talking."""
    if thread is None:
        return
    try:
        thread.join(timeout)
    except Exception:
        pass


# ---------- the cues Retro knows --------------------------------------------

def timer_done(volume=0.55):
    """The two-note chime a finished timer gets: a fifth, then the octave above."""
    buffer = silence(1.4)
    bell(buffer, when=0.0, duration=1.0, freq=659.25, volume=0.5)      # E5
    bell(buffer, when=0.18, duration=1.2, freq=987.77, volume=0.42)    # B5
    return normalize(buffer, 0.85), DEFAULT_RATE


def cue(name):
    """Plays one of the named cues. Returns the thread that is playing it."""
    makers = {"timer_done": timer_done}
    maker = makers.get(str(name))
    if maker is None:
        return None
    buffer, rate = maker()
    device = None
    try:
        from . import settings
        device = settings.get("output")
    except Exception:                     # settings must never stop a chime
        device = None
    return play_async(buffer, rate, device=device)


def preview(seconds=0.0):
    """Plays every cue once, spaced out - for a person checking the sound by ear."""
    for name in ("timer_done",):
        buffer, rate = {"timer_done": timer_done}[name]()
        print(f"{name}: {seconds_of(buffer, rate):.2f}s")
        play(buffer, rate)
        time.sleep(max(0.0, seconds))
