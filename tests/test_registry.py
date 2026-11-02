"""The application registry: the 23 applications, and how each one is really started.

These are the acceptance checks for the registry itself - every required entry is
present, a spoken name resolves to the right app (including the category defaults
the user asked for: music is the configured music app, Minecraft is the launcher),
each app says what it is for and what it can do, and a launch reports only what it
could actually check. Nothing here opens anything.
"""
import pytest

from core import registry, tools


def test_the_registry_carries_every_required_application():
    """All 23 the user listed, in the order they listed them."""
    assert len(registry.REQUIRED) == 23
    missing = [key for key in registry.REQUIRED if key not in registry.SPECS]
    assert missing == [], f"the registry is missing {missing}"
    assert len(registry.SPECS) == len(registry.REQUIRED)


@pytest.mark.parametrize("spoken,key", [
    ("spotify", "spotify"),
    ("the music app", "spotify"),
    ("my music app", "spotify"),
    ("minecraft", "legacy_launcher"),
    ("minecraft launcher", "legacy_launcher"),
    ("legacy launcher", "legacy_launcher"),
    ("chrome", "chrome"),
    ("google chrome", "chrome"),
    ("the browser", "chrome"),
    ("wa", "whatsapp"),
    ("whats app", "whatsapp"),
    ("vs code", "vscode"),
    ("visual studio code", "vscode"),
    ("notepad", "notepad"),
    ("file explorer", "file_explorer"),
    ("my files", "file_explorer"),
    ("shadow fight", "shadow_fight_2"),
    ("settings", "system_settings"),
    ("windows settings", "system_settings"),
    ("task manager", "task_manager"),
    ("wps", "wps_office"),
    ("modrinth", "modrinth"),
    ("crunchyroll", "crunchyroll"),
    ("chatgpt", "chatgpt"),
    ("claude", "claude"),
    ("youtube", "youtube"),
    ("github", "github"),
    ("odaclass", "odaclass"),
    ("freebuff", "freebuff"),
    ("powerpoint", "powerpoint"),
    ("excel", "excel"),
    ("speedtest", "speedtest"),
    ("discord", "discord"),
])
def test_spoken_names_resolve_to_the_right_application(spoken, key):
    spec, message = registry.resolve(spoken)
    assert spec is not None, message
    assert spec.key == key


def test_every_application_says_what_it_is_for_and_what_it_can_do():
    """The registry is the assistant's knowledge, so each entry has to carry some."""
    for key in registry.REQUIRED:
        spec = registry.SPECS[key]
        assert spec.name and spec.category, key
        assert spec.note, f"{key} does not say what it is for"
        assert "open" in spec.capabilities, f"{key} cannot be opened"
        assert spec.verify, f"{key} has no window title to check that it opened"
        launchable = bool(spec.exe or spec.shell or spec.url or spec.names)
        assert launchable, f"{key} has no way in at all"


def test_the_defaults_point_at_real_entries():
    assert registry.DEFAULTS["music"] in registry.SPECS
    assert registry.DEFAULTS["browser"] in registry.SPECS
    assert registry.DEFAULTS["minecraft"] in registry.SPECS
    assert registry.SPECS["spotify"].default_for == "music"
    assert registry.SPECS["legacy_launcher"].default_for == "minecraft"


def test_an_unknown_name_is_refused_with_something_useful():
    spec, message = registry.resolve("clash of clans")
    assert spec is None
    assert "clash of clans" in message


def test_the_catalogue_tells_the_planner_about_every_app():
    text = registry.catalogue()
    for key in registry.REQUIRED:
        assert registry.SPECS[key].name in text
    # The default the user named explicitly has to be visible to the model.
    assert "default music app" in text
    # And the launcher has to be the launcher, not a download page.
    assert "never the website" in text


def test_a_launch_reports_only_what_it_could_check(monkeypatch):
    """A started process with no window is not the same as an app that opened."""
    monkeypatch.setattr(registry, "_start", lambda spec: ("exe", "notepad.exe", 4242))
    monkeypatch.setattr(registry, "_wait_for_app", lambda spec, pid, wait_s: (False, ""))
    result = registry.open_app("notepad")
    assert result.ok and not result.verified
    assert "can't see its window" in result.message

    monkeypatch.setattr(registry, "_wait_for_app",
                        lambda spec, pid, wait_s: (True, "Untitled - Notepad"))
    result = registry.open_app("notepad")
    assert result.ok and result.verified
    assert "Notepad" in result.message


def test_a_program_that_cannot_be_started_is_reported_not_faked(monkeypatch):
    def boom(spec):
        raise FileNotFoundError(spec.name)
    monkeypatch.setattr(registry, "_start", boom)
    result = registry.open_app("spotify")
    assert not result.ok and "isn't installed" in result.message


def test_the_open_app_tool_goes_through_the_registry(monkeypatch):
    called = []

    def fake_open(name, wait_s=6.0):
        called.append(name)
        return True

    from core import registry as reg
    monkeypatch.setattr(reg, "open_app", fake_open)
    tools.open_app("spotify")
    assert called == ["spotify"]
