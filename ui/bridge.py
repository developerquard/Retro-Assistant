"""Bridges the core session to the widgets.

`Session` is plain Python and may run its listening loop on a worker thread; Qt
widgets may only be touched from the GUI thread. Every message therefore crosses
as a Qt signal, which Qt queues to the GUI thread automatically because these
signals are defined on an object that lives there.
"""
import threading

from PySide6.QtCore import QObject, Signal

from core import config, ui_hook
from core.session import Session


class Bridge(QObject):
    """Owns one Session and re-emits everything it reports as Qt signals."""

    stateChanged = Signal(str, str)      # state, detail
    replied = Signal(str, bool)          # message, ok
    levelChanged = Signal(float)         # microphone loudness, 0..1
    logged = Signal(str)
    progressChanged = Signal(str)        # one short line about a real event
    writingChanged = Signal(object)      # one event from a document being written
    modeChanged = Signal(str)            # "chat" or "work"
    settingsRequested = Signal()
    diagnosticsRequested = Signal()       # "Retro, diagnostics" - the latency panel
    introRequested = Signal()             # "Retro, play your intro" - the arrival again
    micChanged = Signal(bool)             # the microphone really is on/off now
    modelsListed = Signal()               # the Groq model listing was refreshed
    ready = Signal(bool, str)             # loaded ok, error text
    # The peak of the test line, and the loudest the Windows mixer saw this process
    # make while it played. -1 means "not known" for either of them, which is not the
    # same as zero and must never be reported as success.
    voiceTested = Signal(float, float)
    # The loudest thing the chosen microphone heard during TEST MIC, or -1 for a
    # device that could not be opened. It never means "no speech": it is a level.
    micTested = Signal(float)

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.session = session or Session(
            on_state=lambda state, detail="": self.stateChanged.emit(state, detail),
            on_reply=lambda text, ok: self.replied.emit(text, ok),
            on_level=lambda value: self.levelChanged.emit(float(value)),
            on_log=lambda text: self.logged.emit(text),
            on_progress=lambda text: self.progressChanged.emit(text),
            on_mode=lambda mode: self.modeChanged.emit(mode),
            on_writing=lambda event: self.writingChanged.emit(event),
            on_mic=lambda on: self.micChanged.emit(bool(on)),
        )
        self._unsubscribe = ui_hook.subscribe(self._on_core_event)

    def _on_core_event(self, event):
        if event == "show_settings":
            self.settingsRequested.emit()
        elif event == "show_diagnostics":
            self.diagnosticsRequested.emit()
        elif event == "play_intro":
            self.introRequested.emit()

    def start(self, voice=True):
        """Loads the session on the calling thread. Returns whether it worked."""
        return self.session.start(voice=voice)

    def start_async(self, voice=True):
        """Loads the speech model, the voice and the fallback off the GUI thread.

        Whisper takes a second or two to load, which would otherwise freeze the
        window before it has drawn once.
        """
        def load():
            try:
                ok = self.session.start(voice=voice)
            except Exception as exc:                  # start() already catches, but be sure
                ok = False
                self.session.error = f"{type(exc).__name__}: {exc}"
            self.ready.emit(ok, self.session.error or "")

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        return thread

    def refresh_models_async(self):
        """Asks Groq which models this key can call, off the GUI thread.

        The window is built from the cached listing, so the first paint never waits
        on the network; this is what fills the cache - and the MODEL picker, through
        `modelsListed` - a moment later. A listing is a nicety, so a failure is
        silent: the built-in rows stay, and every path that uses the models is
        already tested against them.
        """
        def load():
            try:
                config.refresh_model_ids()
            except Exception:
                pass
            self._announce_models()

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        return thread

    def _announce_models(self):
        """Tells the window its listing changed, if the window is still there.

        This runs on a daemon thread, so it can outlive the window it reports to:
        closing Retro while the listing is in flight deletes the object underneath
        it, and emitting into a deleted QObject raises RuntimeError out of that
        thread. A crash report at shutdown is not worth a refreshed dropdown.
        """
        try:
            self.modelsListed.emit()
        except RuntimeError:
            pass

    def start_voice_loop(self):
        return self.session.start_voice_loop()

    def submit(self, text):
        self.session.submit(text)

    def set_mic(self, on):
        """The microphone switch, in the one place that owns it."""
        return self.session.set_mic(on, save=True)

    def test_mic_async(self):
        """Records a second of the chosen microphone off the GUI thread."""
        def run():
            try:
                level = self.session.test_microphone()
            except Exception:
                level = None
            self.micTested.emit(-1.0 if level is None else float(level))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_voice_async(self):
        """Speaks a check line off the GUI thread: speaking blocks for seconds."""
        def run():
            result = self.session.test_voice()
            peak, mixer_peak = result if isinstance(result, tuple) else (result, None)
            self.voiceTested.emit(-1.0 if peak is None else float(peak),
                                 -1.0 if mixer_peak is None else float(mixer_peak))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def say_state(self, state, detail=""):
        self.stateChanged.emit(state, detail)

    def set_mode(self, mode):
        """Switches chat/work from the window. Reports the mode back to the UI."""
        chosen = self.session.set_mode(mode, save=True)
        self.modeChanged.emit(chosen)
        return chosen

    def stop(self):
        self._unsubscribe()
        self.session.stop()
