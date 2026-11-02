"""The choices that should survive a restart: model, voice, output device.

The store is pointed at a temporary file, so a real run's settings are never
touched, and a broken file is checked to be harmless.
"""
import json

import pytest

from core import config, settings
from core.session import Session


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    settings.forget()
    yield
    settings.forget()


def test_nothing_saved_reads_as_the_default():
    assert settings.get("voice") is None
    assert settings.get("nonsense") is None


def test_a_saved_choice_comes_back():
    settings.save(voice="Microsoft Zira Desktop", model="1b")
    settings.forget()                       # as if the assistant had restarted
    assert settings.get("voice") == "Microsoft Zira Desktop"
    assert settings.get("model") == "1b"


def test_unknown_keys_are_ignored():
    settings.save(api_key="should not be stored")
    assert settings.get("api_key") is None
    assert "api_key" not in json.loads(settings.PATH.read_text(encoding="utf-8"))


def test_a_broken_file_is_harmless():
    settings.PATH.write_text("{ this is not json", encoding="utf-8")
    settings.forget()
    assert settings.load() == {}
    assert settings.get("model") is None


def test_the_output_device_is_remembered():
    settings.save(output=None)              # None means the system default
    settings.forget()
    assert settings.get("output") is None
    settings.save(output=5)
    settings.forget()
    assert settings.get("output") == 5


def test_a_session_starts_from_the_saved_model():
    settings.save(model="500m")
    settings.forget()
    session = Session()
    assert session.model_choice == "500m"


def test_setting_a_model_saves_it(monkeypatch):
    session = Session()
    session.fallback = None                 # nothing to unload
    session.set_model("500m")
    assert settings.get("model") == "500m"


def test_the_default_model_is_used_when_nothing_is_saved(monkeypatch):
    assert Session().model_choice == config.MODEL_CHOICE


def test_the_cloud_model_that_was_picked_comes_back_after_a_restart(monkeypatch):
    """A picker row carries a real model id now, and that id is what is remembered."""
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    session = Session()
    session.fallback = None                 # nothing to unload
    session.set_model("qwen/qwen3.8-27b")
    assert settings.get("model") == "qwen/qwen3.8-27b"
    settings.forget()                       # as if the assistant had restarted
    assert Session().model_choice == "qwen/qwen3.8-27b"


# ---------- the device remembered between runs --------------------------------

DEVICES = ((16, "Headphones (Airdopes 212)"), (18, "Speakers (FxSound Audio Enhancer)"))


class FakeSpeaker:
    """A speaker that resolves a name against the devices offered and keeps the rest."""

    def __init__(self, devices=DEVICES):
        self.names = list(devices)
        self.device = None
        self.disabled = True          # as if it had already given up
        self.wedges = 3

    def choose_output(self, device):
        if device is not None and not isinstance(device, int):
            wanted = " ".join(str(device).split()).lower()
            found = None
            for exact in (True, False):
                for index, name in self.names:
                    if (name.lower() == wanted) if exact else (wanted in name.lower()):
                        found = index
                        break
                if found is not None:
                    break
            if found is None:
                return None
            device = found
        self.device = device
        self.disabled = False
        self.wedges = 0
        return device


def restored(monkeypatch, saved, usable=True):
    """A session whose device list is the one in this test, with `saved` restored."""
    from core import session as session_module
    settings.save(output=saved)
    session = Session()
    speaker = FakeSpeaker()
    session.speaker = speaker
    monkeypatch.setattr(Session, "outputs", lambda self: list(speaker.names))
    monkeypatch.setattr(session_module, "device_usable", lambda device: usable)
    session._restore_output(settings.get("output"))
    return session, speaker


def test_a_stale_device_index_falls_back_to_the_system_default(monkeypatch):
    """An index is not a device: 13 was a Bluetooth headset somebody switched off.

    That is what made the voice inaudible while the app reported it had spoken, so an
    index that is not in the list of devices offered now is not used.
    """
    _, speaker = restored(monkeypatch, 13)
    assert speaker.device is None, "a device that is not here must not be used"


def test_an_offered_index_is_migrated_to_a_name(monkeypatch):
    """Old settings files hold indices; they are accepted, then written back as names."""
    _, speaker = restored(monkeypatch, 18)
    assert speaker.device == 18
    assert settings.get("output") == "Speakers (FxSound Audio Enhancer)"


def test_a_remembered_name_is_resolved(monkeypatch):
    _, speaker = restored(monkeypatch, "Speakers (FxSound Audio Enhancer)")
    assert speaker.device == 18, "the device is found by name, not by a stale index"


def test_a_device_that_cannot_be_opened_is_not_used(monkeypatch):
    """The exact failure: the device is in the list and will not take the audio."""
    _, speaker = restored(monkeypatch, "Speakers (FxSound Audio Enhancer)", usable=False)
    assert speaker.device is None
    assert not speaker.disabled, "falling back must also bring the voice back"


def test_the_system_default_is_used_when_nothing_was_ever_saved(monkeypatch):
    _, speaker = restored(monkeypatch, None)
    assert speaker.device is None


def test_setting_an_output_remembers_its_name(monkeypatch):
    """A name survives a device list that reorders itself; an index does not."""
    session = Session()
    speaker = FakeSpeaker()
    session.speaker = speaker
    monkeypatch.setattr(Session, "outputs", lambda self: list(speaker.names))
    assert session.set_output("Speakers (FxSound Audio Enhancer)") == 18
    assert settings.get("output") == "Speakers (FxSound Audio Enhancer)"
    assert session.set_output(None) is None
    assert settings.get("output") is None, "the system default is remembered as the default"
