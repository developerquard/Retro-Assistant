"""What the machine is playing, for the background visualiser.

The owner asked for the orb, asleep, to come alive to whatever the laptop is playing -
a song, a video - with the movement and the colour taken from *real* audio. So this
reads the system's own output rather than the microphone: hearing the speakers through
the microphone would pick up the room, the fan and anyone talking, and would put the
music at whatever distance the listener happens to be.

Three ways in, in order of quality, because this machine needed all three to be tried:

* **WASAPI loopback** - the output device opened as a capture stream. That is the
  actual mix, sample for sample, so the visualiser gets a level *and* a spectrum, and
  twelve bands are what make the dots answer the bass and the hi-hat differently
  rather than the whole ball pulsing. The sounddevice build here does not expose the
  loopback flag, so in practice this path is attempted and then declined.
* **The machine's own "Stereo Mix" input** - the sound card's monitor of what it is
  playing, opened as an ordinary capture device. Every input was measured while a line
  played: the microphone and the virtual devices heard nothing, while Stereo Mix (and
  MME's "Primary Sound Capture Driver", which is the same signal) carried the line at
  a peak of 0.22 to 0.33. That is the real mix with a real spectrum, which is what the
  waves need to answer a vocal rather than merely pulse.
* **The Windows mixer's meter** - a peak reading for the default output device, polled.
  No spectrum, so every band shares one level, but it needs no audio stream at all and
  it still reports a real number: silence reads zero.

`available` and `reason` say which of the three is running, and no source is ever
reported as a level it did not measure: a machine where none works gets zeroes and says
so, rather than a visualiser that dances to nothing.
"""
import math
import threading
import time

BANDS = 12
# 1024 samples is ~21 ms at 48 kHz. At 2048 the sampler only looked at the mix every
# 43 ms, so a syllable shorter than that could pass between two readings - the waves
# moved with the bar, not with the words.
WINDOW = 1024
POLL_S = 0.05                 # how often the mixer fallback is read
DECAY = 0.72                  # how fast a band falls: fast enough to look like music
FLOOR = 0.004                 # below this, it is silence, not quiet music
LEVEL_GAIN = 3.4              # RMS to the 0..1 the display wants
# The frequency range the twelve bands are spread across, logarithmically. Fixed in
# hertz rather than in FFT bins, so "band 0" is the kick drum on every device and at
# every sample rate, instead of being whatever the window size happened to make it.
BAND_LOW_HZ = 60.0
BAND_HIGH_HZ = 14000.0
# How far below the loudest band still counts. A vocal is routinely 20-30 dB under the
# bassline; the old linear ratio put it at a tenth of full height, which is why the
# waves answered the beat and never the voice.
BAND_RANGE_DB = 36.0
# Where a sung line lives: a singer's fundamental and first two formants.
VOCAL_LOW_HZ = 200.0
VOCAL_HIGH_HZ = 3500.0
# Names the sound card's own monitor goes by. Matched case-insensitively against every
# input device, because the index and the host API are not stable across machines.
MONITOR_HINTS = ("stereo mix", "what u hear", "wave out mix", "waveout mix",
                 "loopback", "monitor of")


class Sampler:
    """A level and a small spectrum for the machine's own output.

    `level` and `bands` are read by the window on its own thread and written by the
    capture callback on another. That is safe here without a lock: floats and list
    rebinds are atomic in CPython, and a frame that catches the previous list is one
    frame of slightly older music, which nobody can see.
    """

    def __init__(self, device=None, bands=BANDS):
        self.device = device
        self.count = max(1, int(bands))
        self.level = 0.0
        self.bands = [0.0] * self.count
        # How much of this block is the voice: the 200-3500 Hz region measured against
        # the rest of the mix. Published next to the bands so the window lifts its waves
        # for a singer rather than re-deriving that from twelve normalised bars, where
        # the ratio that made the voice visible has already been divided out.
        self.vocal = 0.0
        self.available = False
        self.reason = "not started"
        # When a block last arrived. A capture stream can simply stop delivering while
        # the machine is quiet - WDM-KS does - and every reading then keeps the value it
        # had, which is a visualiser dancing to a song that ended. See `_fade_out`.
        self._heard_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._stream = None

    # ---------- lifecycle ---------------------------------------------------

    def start(self):
        """Begins sampling. Returns at once; the capture runs on its own thread."""
        if self._thread is not None:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Stops sampling and lets go of the stream, leaving the level at zero."""
        self._stop.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self.level = 0.0
        self.bands = [0.0] * self.count
        self.vocal = 0.0
        self.available = False
        self.reason = "stopped"

    # ---------- the two ways in ---------------------------------------------

    def _serve(self):
        try:
            if self._loopback():
                return                       # only comes back when stopped
        except Exception as exc:
            self.reason = f"loopback: {type(exc).__name__}"
        if self._stop.is_set():
            return
        try:
            if self._monitor_input():
                return
        except Exception as exc:
            self.reason = f"monitor input: {type(exc).__name__}"
        if self._stop.is_set():
            return
        self._mixer_loop()

    def _monitor_input(self):
        """Captures the sound card's own Stereo Mix. Returns False if there is none.

        This is the path that works on the machine this was written on: the WASAPI
        loopback flag is not exposed by the installed sounddevice, and the mixer meter
        reads zero for output played through a virtual device, so the visualiser had no
        audio at all and its waves could only drift. Stereo Mix is the card's sum of
        every output, it is an ordinary input stream, and it is sample-for-sample the
        mix - measured here at a peak of 0.22 while a line played, against 0.0005 in
        silence, which is the separation a visualiser needs.
        """
        import numpy as np
        import sounddevice as sd

        # Every device whose name says "this is the card's monitor", in the order
        # PortAudio lists them. There is usually more than one (Stereo Mix through
        # WDM-KS, the same signal through MME, sometimes a virtual cable), and which
        # of them will actually open changes with what else holds the card - the old
        # code took the first name that matched and gave up if that one refused.
        candidates = []
        for index, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) <= 0:
                continue
            name = " ".join(str(info.get("name", "")).lower().split())
            if any(hint in name for hint in MONITOR_HINTS):
                candidates.append((index, info, name))
        refused = None
        for index, info, name in candidates:
            rate = int(info.get("default_samplerate") or 48000)
            channels = 2 if int(info.get("max_input_channels", 1)) >= 2 else 1

            def callback(indata, _frames, _time, _status, rate=rate):
                if self._stop.is_set():
                    raise sd.CallbackAbort
                mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
                # A real copy: PortAudio reuses the callback buffer, and analysing a view
                # of it after the callback returns is the same fault that made the
                # microphone deaf (see `Down16.feed` in core/voice.py).
                self._analyse(np.array(mono, dtype="float32", copy=True), rate)

            try:
                stream = sd.InputStream(device=index, channels=channels, samplerate=rate,
                                        dtype="float32", blocksize=WINDOW, callback=callback)
                stream.start()
            except Exception as exc:
                refused = f"{name[:20]}: {type(exc).__name__}"
                continue
            self._stream = stream
            self.available = True
            self.reason = f"system output ({name[:32]})"
            try:
                while not self._stop.is_set():
                    self._fade_out()
                    time.sleep(0.1)
            finally:
                self._stream = None
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            return True
        if refused:
            self.reason = f"monitor input ({refused})"
        return False

    def _loopback(self):
        """Captures the output device itself. Returns False if this machine won't."""
        import numpy as np
        import sounddevice as sd

        device = self.device
        if device is None:
            try:
                device = sd.query_devices(kind="output")["index"]
            except Exception:
                device = None
        settings = None
        factory = getattr(sd, "WasapiSettings", None)
        if factory is None:
            return False
        settings = factory(loopback=True)
        try:
            info = sd.query_devices(device)
            rate = int(info.get("default_samplerate") or 48000)
        except Exception:
            rate = 48000
        channels = 2

        def callback(indata, _frames, _time, status):
            if self._stop.is_set():
                raise sd.CallbackAbort
            mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
            # A copy, for the same reason the monitor path makes one: this buffer is
            # PortAudio's, and it is reused the moment the callback returns.
            self._analyse(np.array(mono, dtype="float32", copy=True), rate)

        stream = sd.InputStream(device=device, channels=channels, samplerate=rate,
                                dtype="float32", blocksize=WINDOW, callback=callback,
                                extra_settings=settings)
        with stream:
            self._stream = stream
            self.available = True
            self.reason = "system output (loopback)"
            while not self._stop.is_set():
                self._fade_out()
                time.sleep(0.1)
        self._stream = None
        return True

    def _analyse(self, mono, rate=48000):
        """One block of the system's audio: a level, a level per band, and the voice."""
        import numpy as np

        if len(mono) < 64:
            return
        level = float(np.sqrt(np.mean(mono ** 2))) * LEVEL_GAIN   # RMS -> 0..1-ish
        spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) / (len(mono) / 2.0)
        bins = len(spectrum)
        if bins < self.count * 2:
            self._publish(level, None)
            return
        # Bands spread logarithmically in hertz, because music is: an even split of the
        # linear spectrum puts almost every band in the top two octaves and leaves the
        # bass in one bucket, so the bass drum and the melody look identical. Edges are
        # computed from the sample rate, so they mean the same thing at 44.1 and 48 kHz.
        nyquist = max(1.0, float(rate or 48000) / 2.0)
        top_hz = min(BAND_HIGH_HZ, nyquist * 0.92)
        freqs = [BAND_LOW_HZ * (top_hz / BAND_LOW_HZ) ** (index / float(self.count))
                 for index in range(self.count + 1)]
        edges = [min(bins - 1, max(1, int(freq / nyquist * (bins - 1)))) for freq in freqs]
        for index in range(1, len(edges)):
            edges[index] = max(edges[index], edges[index - 1] + 1)
        energies = []
        for index in range(self.count):
            part = spectrum[edges[index]:min(bins, edges[index + 1])]
            # The band's energy, not its loudest single bin: one narrow harmonic used to
            # fill a whole band, which is what made a violin and a drum look alike.
            energies.append(float(np.sqrt(np.mean(part ** 2))) if len(part) else 0.0)
        loudest = max(energies) or 1e-9
        # Relative loudness in dB: 1.0 at the loudest band of this block, 0.0 at
        # BAND_RANGE_DB below it. Linear ratios compressed everything into the top of
        # the range - a vocal 24 dB under the bass read as 6% - so the wave answered
        # the kick and sat still for the words. The *height* still follows the block's
        # own loudness, so a quiet verse stays calm and a chorus pushes outward.
        shaped = [max(0.0, 1.0 + 20.0 * math.log10(max(value / loudest, 1e-6))
                      / BAND_RANGE_DB) for value in energies]
        loudness = min(1.0, level * 2.2)
        bands = [min(1.0, value ** 0.8 * (0.25 + 0.75 * loudness)) for value in shaped]
        # The voice itself: the 200-3500 Hz bands over the median of everything else.
        # Measured in dB above the rest of the mix, so it follows a singer over a
        # bassline and drops back to zero the moment the singing stops.
        voiced = [bands[index] for index in range(self.count)
                  if VOCAL_LOW_HZ <= freqs[index] <= VOCAL_HIGH_HZ]
        rest = sorted(bands[index] for index in range(self.count)
                      if not (VOCAL_LOW_HZ <= freqs[index] <= VOCAL_HIGH_HZ))
        floor = rest[len(rest) // 2] if rest else 0.0
        self.vocal = 0.0
        if voiced:
            self.vocal = max(0.0, min(1.0, (max(voiced) - floor * 1.05) / 0.55))
        self._publish(level, bands)

    def _mixer_loop(self):
        """The fallback: the Windows mixer's own meter for the output device."""
        from . import mixer

        self.reason = "system output (mixer meter)"
        while not self._stop.is_set():
            peak = mixer.master_peak()
            if peak is None:
                self.available = False
                self.reason = "the mixer could not be read"
                self._publish(0.0, None)
                time.sleep(0.5)
                continue
            self.available = True
            self._publish(float(peak) * 3.0, None)
            time.sleep(POLL_S)

    def _fade_out(self, after=0.30):
        """Forgets a reading that stopped arriving: a paused song is not a held note.

        Called from the capture loop between blocks. If nothing has been analysed for
        `after` seconds the mix is silent - or the device stopped handing over buffers -
        and every number is walked down to zero instead of being kept for ever.
        """
        if not self._heard_at or time.monotonic() - self._heard_at <= after:
            return
        self.level *= 0.5
        self.vocal *= 0.5
        self.bands = [value * 0.5 for value in self.bands]
        if self.level < 0.01 and self.vocal < 0.01 and max(self.bands or [0.0]) < 0.01:
            self.level = 0.0
            self.vocal = 0.0
            self.bands = [0.0] * self.count

    def _publish(self, level, bands):
        """Publishes one reading, with the fast rise and slow fall a meter wants."""
        self._heard_at = time.monotonic()
        level = 0.0 if level < FLOOR else min(1.0, level)
        self.level = level
        if bands is None:
            # No spectrum: every band follows the level, so the ball still moves with
            # the music. It is a level, and the window is told that is all it is.
            bands = [level] * self.count
        old = self.bands
        self.bands = [max(new, old[index] * DECAY) if index < len(old) else new
                      for index, new in enumerate(bands)]
