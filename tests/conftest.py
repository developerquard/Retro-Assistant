"""Tests keep their own settings and their own log.

Nothing here is about the code under test - it is about the machine it runs on.
`core.settings` reads and writes `settings.json` in the user's real Retro data folder,
and `core.log` writes `retro.log` beside it, so a test that starts a session inherits
whatever the person running the suite happened to choose: background hearing, the mode,
the microphone switch. That made results depend on the order the files were run in, and
on the developer's own settings - and it put test sessions into the log somebody reads
when the app misbehaves for real.

Both paths are pointed at a temporary folder for every test, and the settings cache is
dropped on the way in and on the way out.
"""
import pytest

from core import config, settings
from retrocli import config as coding_config


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    # The coding agent's own settings and key live in the same real user folder, and a
    # test that saved a model would change what the next real task uses.
    root = tmp_path / "coding"
    # monkeypatch only: a plain assignment here would be restored to the previous
    # test's temporary folder instead of the real one.
    monkeypatch.setattr(coding_config, "ROOT", root)
    monkeypatch.setattr(coding_config, "SETTINGS", root / "settings.json")
    monkeypatch.setattr(coding_config, "KEY_FILE", root / "coding.key")
    monkeypatch.setattr(coding_config, "STATE_FILE", root / "state.json")
    monkeypatch.setattr(coding_config, "TASKS", root / "tasks")
    monkeypatch.setattr(coding_config, "LAST_BRIEF", root / "last_brief.md")
    monkeypatch.setenv("RETRO_CODING_KEY", "test-key")
    settings.forget()
    coding_config.forget()
    yield
    settings.forget()
    coding_config.forget()
