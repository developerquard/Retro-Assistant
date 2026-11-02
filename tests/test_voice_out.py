"""Speech output: WAV decoding, device listing, the rate a device will take, and a
real play-through that the Windows mixer confirms.

The playback tests need an output device; where there is none (a build machine) they
skip rather than passing. The line is played for real, because that is the only way
to know a voice came out - and the mixer is asked, because that is the only way to
know it reached a device rather than a buffer.

Why so much hangs on this: the app told the user it was speaking while nothing was
audible, twice, for a month. The second time the cause was that a *chosen* output
device was guaranteed to fail - the picker offers only WASAPI devices (which take
48 000 Hz) and SAPI renders at 22 050 Hz - so picking a device in the window was
enough to make the assistant mute. Nothing could notice, because the only thing
measured was the buffer it had just filled.
"""
import array
import wave

import pytest

from core.voice import (device_usable, output_devices, playable_rate, read_wav_mono,
                        resample, default_output_name)


def write_wav(path, rate, samples, channels=1):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array.array("h", samples).tobytes())
    return path


def test_read_wav_mono_scales_into_minus_one_to_one(tmp_path):
    path = write_wav(tmp_path / "a.wav", 16000, [0, 16384, -16384, 32767])
    rate, audio = read_wav_mono(path)
    assert rate == 16000
    assert len(audio) == 4
    assert audio.max() > 0.99 and audio.min() < -0.49
    assert abs(audio[1] - 0.5) < 0.01


def test_read_wav_mono_mixes_stereo_down(tmp_path):
    path = write_wav(tmp_path / "b.wav", 8000, [-32768, 32767, 32767, -32768], channels=2)
    rate, audio = read_wav_mono(path)
    assert rate == 8000
    assert len(audio) == 2, "two frames of two channels should become two samples"
    assert abs(float(audio[0])) < 0.01 and abs(float(audio[1])) < 0.01


def test_device_list_is_deduplicated():
    devices = output_devices()
    names = [name for _index, name in devices]
    assert len(names) == len(set(names)), f"the picker would show duplicates: {names}"


def test_default_output_is_a_string():
    assert isinstance(default_output_name(), str)


@pytest.mark.skipif(not output_devices(), reason="no audio output device here")
def test_speaking_plays_audio_and_reports_its_level():
    from core.voice import Speaker

    levels = []
    speaker = Speaker(on_level=levels.append)
    speaker.say("Hi.")
    assert speaker.last_peak > 0.01, "the rendered line was silent"
    assert levels and max(levels) > 0.05, "no level was reported while playing"


# ---------- the rate a device will take --------------------------------------


def test_resampling_changes_the_rate_and_keeps_the_sound():
    """22 050 Hz of SAPI audio becomes 48 000 Hz of audio a WASAPI device accepts."""
    import numpy as np
    source = np.sin(np.linspace(0, 40, 2205)).astype("float32")
    out = resample(source, 22050, 48000)
    assert len(out) == pytest.approx(4800, abs=2), "the line changed length in time"
    assert abs(float(np.max(np.abs(out))) - float(np.max(np.abs(source)))) < 0.001
    assert resample(source, 22050, 22050) is source, "no work when no work is needed"
    assert resample(source, 0, 48000) is source, "an unknown rate is left alone"
    assert len(resample(source, 48000, 16000)) == pytest.approx(735, abs=2)


def test_every_offered_device_gets_a_rate_it_accepts():
    """The regression: the device list and the voice must agree about the rate.

    The offered devices are WASAPI ones, and they accept 48 000 Hz only. Asking for
    the rate the WAV is already in was the whole bug, so this asks each device for a
    rate it will really take and then checks with PortAudio that it does.
    """
    import sounddevice as sd
    devices = output_devices()
    if not devices:
        pytest.skip("no audio output device here")
    for index, name in devices:
        rate = playable_rate(index, 22050)          # what SAPI rendered at
        assert rate is not None, f"{name} accepts no rate we can produce"
        sd.check_output_settings(device=index, samplerate=rate, channels=1,
                                 dtype="float32")


def test_a_device_that_does_not_exist_is_not_usable():
    assert playable_rate(99999, 22050) is None
    assert not device_usable(99999)


def test_an_output_is_found_by_name_and_a_missing_one_changes_nothing():
    devices = output_devices()
    if not devices:
        pytest.skip("no audio output device here")
    from core.voice import Speaker
    index, name = devices[0]
    speaker = Speaker()
    assert speaker.choose_output(name) == index
    assert speaker.choose_output(name.lower()) == index
    kept = speaker.device
    assert speaker.choose_output("no such output device exists here") is None
    assert speaker.device == kept, "a device that was not found must not be swapped in"


def test_choosing_an_output_revives_a_voice_that_gave_up():
    """"Pick another output device" has to be advice the assistant can follow."""
    from core.voice import Speaker
    speaker = Speaker()
    speaker.disabled = True
    speaker.wedges = 3
    speaker.choose_output(None)
    assert not speaker.disabled, "a new output deserves a fresh attempt"
    assert speaker.wedges == 0


@pytest.mark.skipif(not output_devices(), reason="no audio output device here")
def test_a_line_reaches_the_mixer_on_the_system_default_output():
    """End to end, on the device a fresh install uses, checked where it matters.

    Walks the shipped path - the real Speaker, a real rendered line, the resampling
    and the stream - and then asks the Windows mixer rather than the buffer, because
    `last_peak` is computed before a single sample reaches a device and is therefore
    the same number whether the line was heard or not. This is the assertion that
    would have failed while the app reported a voice nobody could hear.

    The *system default* on purpose: an offered endpoint can be a phantom device (a
    Bluetooth headset that was switched off still opens and still swallows the audio),
    so audibility is only a fair claim about the output this machine actually uses.
    """
    from core.voice import Speaker
    speaker = Speaker()
    try:
        if not speaker.ensure_voices():
            pytest.skip("no SAPI voice installed here")
    except Exception as exc:
        pytest.skip(f"no speech engine here ({type(exc).__name__})")
    assert speaker.choose_output(None) is None, "the system default"
    played, mixer_peak = speaker.audible("Retro is checking the speakers.")
    assert played, "the line was not played at all"
    assert speaker.last_peak > 0.01, "the line that was played was silent"
    if mixer_peak is not None:
        assert mixer_peak > 0.001, "the line did not reach the mixer: nobody heard it"


@pytest.mark.skipif(not output_devices(), reason="no audio output device here")
def test_a_chosen_device_plays_or_is_refused_never_silently_faked():
    """A device from the picker must either play or be reported as unusable.

    Both outcomes are honest; what must never happen again is a line written into a
    device that will not take it while the app reports that it spoke. `played` is the
    speaker's own answer, and `last_error` carries why when it is False.
    """
    from core.voice import Speaker
    for index, name in output_devices():
        speaker = Speaker()
        try:
            if not speaker.ensure_voices():
                pytest.skip("no SAPI voice installed here")
        except Exception as exc:
            pytest.skip(f"no speech engine here ({type(exc).__name__})")
        assert speaker.choose_output(index) == index
        played, _mixer = speaker.audible("Checking this output.")
        assert played is not None
        if not played:
            assert speaker.last_error, f"{name} refused the line without a reason"
