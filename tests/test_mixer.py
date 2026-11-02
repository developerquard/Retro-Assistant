"""The mixer reader: unknown is a real answer, and it never raises.

The workflows use this to verify playback where a window gives nothing away, so its
worst case has to be "I don't know" rather than a crash in the middle of a task - and
it must never invent sound that is not there.
"""
from core import mixer


def test_a_process_with_no_audio_session_is_unknown_not_silent():
    """No session means no evidence either way: None, not 0.0."""
    assert mixer.peak_of("retro_process_that_does_not_exist") is None
    assert mixer.is_sounding("retro_process_that_does_not_exist", seconds=0.4) is None


def test_no_names_is_unknown():
    assert mixer.peak_of() is None
    assert mixer.is_sounding(seconds=0.4) is None


def test_an_unreadable_mixer_answers_unknown(monkeypatch):
    mixer.forget()
    monkeypatch.setattr(mixer, "_sessions", lambda: None)
    assert mixer.peak_of("spotify") is None
    assert mixer.is_sounding("spotify", seconds=0.4) is None


def test_reading_a_real_session_never_raises():
    """Whatever this machine has, the reader answers with a float or None."""
    mixer.forget()
    peak = mixer.peak_of("chrome", "explorer", "audiodg")
    assert peak is None or (isinstance(peak, float) and 0.0 <= peak <= 1.0)
    sounding = mixer.is_sounding("chrome", seconds=0.4)
    assert sounding in (True, False, None)
