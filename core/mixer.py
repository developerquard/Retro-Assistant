"""Who is making sound right now, read from the Windows mixer.

Each audio session in the mixer carries a peak meter, and that is the only honest
way to answer "is it actually playing?" for an application whose window cannot be
read: the Spotify desktop app keeps its page out of the accessibility tree and its
title never changes, so real sound leaving the process is the evidence. A paused
player reads zero, which is exactly the difference the check needs to tell.

Everything here is read-only and best-effort: on a machine without pycaw, or when
the mixer refuses the query, the answer is `None` - unknown - and callers say so
instead of guessing in either direction.
"""
import time

_SESSION = "reset"


def _com():
    """COM, on the thread that is about to talk to the mixer.

    COM belongs to the thread that uses it, and the readers here run on whatever
    thread asks the question - the speech verification uses a watcher thread, a UI
    workflow uses the task's thread. Without this, the first call from a fresh thread
    raised "CoInitialize has not been called" and the whole mixer looked unreadable.
    """
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _sessions():
    """The mixer's sessions, or None when they cannot be read."""
    global _SESSION
    if _SESSION == "reset":
        try:
            from pycaw.pycaw import AudioUtilities
            from pycaw.api.endpointvolume import IAudioMeterInformation
        except Exception:                     # pycaw is optional
            _SESSION = None
        else:
            _SESSION = (AudioUtilities, IAudioMeterInformation)
            try:
                _com()
                AudioUtilities.GetAllSessions()    # a first call warms COM up
            except Exception:
                # Deliberately *not* remembered as "unavailable". A first call can fail
                # for a reason that passes, and caching that turned "the mixer could not
                # be read this once" into "the mixer cannot be read at all, ever again,
                # in this process" - which is how the speech check reported silence it
                # had never actually asked about.
                pass
    if _SESSION is None:
        return None
    AudioUtilities, _meter = _SESSION
    try:
        _com()
        return AudioUtilities.GetAllSessions()
    except Exception:
        return None


def peak_of(*names):
    """The loudest peak now among the named processes, or None if unreadable.

    A process name is matched without its extension, case-insensitively, so
    "spotify" finds Spotify.exe.
    """
    wanted = {str(name).lower().split(".")[0] for name in names if name}
    if not wanted:
        return None
    sessions = _sessions()
    if sessions is None:
        return None
    best = 0.0
    read = False
    for session in sessions:
        process = getattr(session, "Process", None)
        if process is None:
            continue
        try:
            name = (process.name() or "").lower().split(".")[0]
        except Exception:
            continue
        if name not in wanted:
            continue
        try:
            meter = session._ctl.QueryInterface(_SESSION[1])
            best = max(best, float(meter.GetPeakValue()))
            read = True
        except Exception:
            continue
    return best if read else None


def is_sounding(*names, seconds=1.6, floor=0.003):
    """True when one of the processes is really producing sound right now.

    Sampled over a short window because a peak is momentary: a single read can
    land in a gap between beats and report silence from a playing player.
    """
    if peak_of(*names) is None:
        return None
    deadline = time.time() + max(0.3, seconds)
    while True:
        peak = peak_of(*names)
        if peak is None:
            return None
        if peak >= floor:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def master_peak():
    """The peak meter of the *default output device*: everything being played.

    `peak_of` answers "is this application making sound"; this answers "is the machine
    making sound", which is what the background visualiser asks. pycaw's AudioDevice
    wraps the COM device rather than being it - hence `_dev`, with the wrapped object
    itself tried as well, because that wrapper has moved between pycaw versions and a
    version test here would be a guess about a future one. Returns None when the
    meter cannot be read at all, which the caller reports rather than guessing.
    """
    if _sessions() is None:
        return None
    try:
        _com()
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        for candidate in (getattr(device, "_dev", None), device):
            if candidate is None or not hasattr(candidate, "Activate"):
                continue
            meter = candidate.Activate(_SESSION[1]._iid_, 23, None)   # CLSCTX_ALL
            return float(meter.QueryInterface(_SESSION[1]).GetPeakValue())
    except Exception:
        return None
    return None


def forget():
    """Forgets the cached import state, for tests and for a fresh process."""
    global _SESSION
    _SESSION = "reset"
