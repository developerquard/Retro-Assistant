"""The app workflows: playing music, sending a message, writing, building a project.

Every test here stands in for the desktop with a fake toolset, so the *workflow* is
what is under test - the order it does things in, what it checks before it acts, and
what it says when it could not verify the result. The parts that need a real machine
(the keystrokes, the window) are the toolset's job and are tested in test_tools.py.
"""
import pytest

from core import appfunc, files, registry
from core.model import Result


class FakeTools:
    """The toolset the workflows call, recording every step they take."""

    def __init__(self, labels=(), title="", windows=()):
        self.calls = []
        self.labels = list(labels)
        self.title = title
        self.windows = list(windows)
        self.open_ok = True
        self.focus_ok = True
        self.label_map = {}

    # ---- the tools appfunc uses -------------------------------------------
    def open_app(self, name):
        self.calls.append(("open_app", name))
        return Result(self.open_ok, f"Opened {name}." if self.open_ok else f"Couldn't find {name}.")

    def focus_window(self, title, wait_s=6.0):
        self.calls.append(("focus", title))
        return Result(self.focus_ok, f"'{title}' is in front now.")

    def type_into(self, text):
        self.calls.append(("type", text))
        return Result(True, f"Typed {len(str(text))} characters.")

    def press(self, keys):
        self.calls.append(("press", keys))
        return Result(True, f"Pressed {keys}.")

    def click_text(self, label):
        self.calls.append(("click", label))
        return Result(True, f"Clicked '{label}'.")

    def read_screen(self, limit=60):
        self.calls.append(("read_screen", limit))
        return Result(True, "Window 'x': " + " | ".join(self.labels))

    def wait(self, seconds, task=None):
        self.calls.append(("wait", seconds))
        return Result(True, "Waited.")

    def window_titles(self, limit=40):
        return [(1, title) for title in self.windows]

    def foreground_title(self):
        return self.title

    def open_website(self, url):
        self.calls.append(("open_website", url))
        return Result(True, f"Opened {url}.")

    def screen_labels(self, limit=70):
        return self.title, list(self.labels)

    def aim(self, title):
        self.calls.append(("aim", title))
        return title

    def folder_path(self, name):
        return self.label_map.get(name, "/nonexistent")


@pytest.fixture
def fake(monkeypatch):
    def build(**kwargs):
        tools = FakeTools(**kwargs)
        monkeypatch.setattr(appfunc, "_toolset", lambda: tools)
        return tools
    return build


def steps(tools, kind):
    return [call for call in tools.calls if call[0] == kind]


def test_playing_a_song_uses_the_search_box_route_and_checks_the_title(fake):
    tools = fake(labels=["Search", "Sunflower - Post Malone", "Top result"],
                 title="Sunflower - Post Malone - Spotify")
    result = appfunc.run("spotify", "search_play", detail="sunflower")
    assert result.ok and result.verified
    assert steps(tools, "press")[0] == ("press", "ctrl+l"), "the search box is reached by key"
    assert ("type", "sunflower") in tools.calls
    assert ("click", "Sunflower - Post Malone") in tools.calls
    assert "Spotify" in result.message


def test_a_web_player_is_driven_through_the_address_bar(fake):
    """On this machine Spotify is the web player in a browser window.

    A browser page exposes no controls to Automation here, so the workflow that
    works is the address bar and the site's own search URL - and when the page gives
    nothing to click, that is reported as one click away instead of as success.
    """
    tools = fake(labels=[], title="Spotify - Web Player: Music for everyone")
    result = appfunc.run("spotify", "search_play", detail="sunflower")
    assert result.ok and not result.verified
    assert ("press", "ctrl+l") in tools.calls
    assert any(call[0] == "type" and "open.spotify.com/search/sunflower" in call[1]
               for call in tools.calls), tools.calls
    assert "one click" in result.message


def test_a_web_player_that_shows_a_result_clicks_it(fake):
    tools = fake(labels=["Sunflower - Post Malone", "Home", "Search"],
                 title="Spotify - Web Player: Music for everyone")
    result = appfunc.run("spotify", "search_play", detail="sunflower")
    assert result.ok
    assert ("click", "Sunflower - Post Malone") in tools.calls


def test_playing_something_that_cannot_be_started_says_so(fake, monkeypatch):
    monkeypatch.setattr("core.mixer.peak_of", lambda *names: None)      # unreadable
    tools = fake(labels=["Search", "Nothing here"], title="Spotify")
    result = appfunc.run("spotify", "search_play", detail="an obscure track")
    assert not result.ok
    assert "can't tell whether it started" in result.message
    assert not result.verified


def test_the_apps_own_window_is_focused_not_whichever_browser_is_open(fake, monkeypatch):
    """Spotify has a url in the registry, but its window - when it has one - wins.

    Focusing "Chrome" for an app that is installed typed the search into whatever
    browser window was in front: the desktop app was driven as a web player, and the
    user's own tab was used as the page.
    """
    monkeypatch.setattr("core.mixer.peak_of", lambda *names: None)
    tools = fake(labels=["Search"], title="Spotify Free")
    appfunc.run("spotify", "search_play", detail="sunflower")
    assert ("focus", "spotify") in tools.calls or ("focus", "Spotify") in tools.calls
    assert ("focus", "Chrome") not in tools.calls, tools.calls


def test_the_sound_meter_is_the_evidence_when_the_title_never_changes(fake, monkeypatch):
    """The desktop app's title stays 'Spotify Free', so real sound is the proof."""
    monkeypatch.setattr("core.mixer.is_sounding", lambda *names, **kw: False)
    monkeypatch.setattr("core.mixer.peak_of", lambda *names: 0.04)
    tools = fake(labels=[], title="Spotify Free")
    result = appfunc.run("spotify", "search_play", detail="sunflower")
    assert result.ok and result.verified
    assert "audio output" in result.message


def test_music_that_was_already_playing_is_not_evidence_of_this_request(fake, monkeypatch):
    monkeypatch.setattr("core.mixer.is_sounding", lambda *names, **kw: True)
    monkeypatch.setattr("core.mixer.peak_of", lambda *names: 0.5)
    tools = fake(labels=[], title="Spotify Free")
    result = appfunc.run("spotify", "search_play", detail="sunflower")
    assert not result.verified, "old music must not be reported as this request's result"


def test_a_message_is_never_sent_to_a_contact_that_was_not_found(fake):
    tools = fake(labels=["Search", "Recent", "Archived"], title="WhatsApp")
    result = appfunc.run("whatsapp", "send_message", detail="hello", target="JyotiBoruah")
    assert not result.ok
    assert "didn't send anything" in result.message
    typed = [call for call in steps(tools, "type")]
    assert typed == [("type", "JyotiBoruah")], "only the search box was typed into"
    assert ("press", "enter") not in tools.calls


def test_a_message_asks_before_sending_and_names_the_person(fake):
    tools = fake(labels=["Search", "JyotiBoruah", "Type a message"], title="WhatsApp")
    asked = []

    def confirm(question):
        asked.append(question)
        return True

    result = appfunc.run("whatsapp", "send_message", detail="hello ma'am", target="JyotiBoruah",
                         confirm=confirm)
    assert asked, "the user is asked before a message is sent"
    assert "JyotiBoruah" in asked[0] and "hello ma'am" in asked[0]
    assert result.ok
    assert ("type", "hello ma'am") in tools.calls
    assert ("click", "JyotiBoruah") in tools.calls


def test_a_refused_confirmation_sends_nothing(fake):
    tools = fake(labels=["Search", "JyotiBoruah"], title="WhatsApp")
    result = appfunc.run("whatsapp", "send_message", detail="hello", target="JyotiBoruah",
                         confirm=lambda question: False)
    assert not result.ok and "Cancelled" in result.message
    assert ("type", "hello") not in tools.calls
    assert ("press", "enter") not in tools.calls


def test_a_message_that_was_sent_but_cannot_be_read_back_says_so(fake):
    tools = fake(labels=["Search", "JyotiBoruah"], title="WhatsApp")
    result = appfunc.run("whatsapp", "send_message", detail="on my way", target="JyotiBoruah",
                         confirm=lambda question: True)
    assert result.ok and not result.verified
    assert "can't be sure it arrived" in result.message


def test_writing_in_a_new_document_types_the_text_and_reports_it(fake):
    tools = fake(labels=[], title="Untitled - Notepad")
    result = appfunc.run("notepad", "write_text", detail="about AI, and how it works")
    assert result.ok
    assert ("type", "about AI, and how it works") in tools.calls
    assert not result.verified, "typing is not proof that the text is there"


def test_writing_refuses_to_type_into_a_document_that_is_already_open(fake):
    """A window title is not ownership.

    On the machine this was tested on, "Notepad" matched the owner's own chapter and
    the plan would have typed into 23,000 characters of their work. Now the file is
    written instead, and nothing is typed into somebody's document.
    """
    tools = fake(labels=[], title="*Ch 2 - Science ultimate summary - Notepad")
    result = appfunc.run("notepad", "write_text", detail="about AI")
    assert not result.ok
    assert "document in it already" in result.message
    assert ("type", "about AI") not in tools.calls


def test_writing_with_a_file_name_writes_and_verifies_the_file(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    # A note is not a project any more: notes live beside Coding Tasks, in their own
    # folder, so this patches that root as well.
    monkeypatch.setattr(files, "NOTES", tmp_path / "Notes")
    tools = fake(labels=[], title="*Ch 2 - Science ultimate summary - Notepad")
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path))
    result = appfunc.run("notepad", "write_text", detail="about AI", target="ai notes")
    path = tmp_path / "Notes" / "ai notes.txt"
    assert path.exists() and path.read_text(encoding="utf-8") == "about AI"
    assert result.ok and result.verified
    assert "ai notes.txt" in result.message and str(path) in opened[0]


def test_creating_a_project_writes_real_files_and_opens_the_folder(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    tools = fake(labels=[], title="Visual Studio Code")
    started = []
    monkeypatch.setattr(registry, "_exe_candidates", lambda spec: ["C:/fake/Code.exe"])
    monkeypatch.setattr("os.path.exists", lambda path: True)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args: started.append(args) or None)

    result = appfunc.run("vscode", "create_project", target="threejs site")
    folder = tmp_path / "Projects" / "threejs site"
    assert (folder / "index.html").exists()
    assert (folder / "main.js").exists()
    assert "three" in (folder / "main.js").read_text(encoding="utf-8").lower()
    assert result.ok and result.verified
    assert started and str(folder) in started[0]


def test_the_project_name_ignores_the_models_filler(fake, monkeypatch, tmp_path):
    """A real plan came back as `create_project | simple three.js websiteanswer | Done.`

    The name field held "Done." and the note field held the actual name, so the name
    is read from wherever it is and the filler is dropped - a folder called "Done."
    is not what anybody asked for.
    """
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    fake(labels=[], title="Visual Studio Code")
    monkeypatch.setattr(registry, "_exe_candidates", lambda spec: [])
    result = appfunc.run("vscode", "create_project", detail="simple three.js websiteanswer",
                         target="Done.")
    assert result.ok
    assert (tmp_path / "Projects" / "simple three.js website" / "index.html").exists()
    assert not (tmp_path / "Projects" / "Done.").exists()


def test_filler_in_every_field_asks_for_a_name(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    fake(labels=[], title="Visual Studio Code")
    result = appfunc.run("vscode", "create_project", detail="Done.", target="answer")
    assert not result.ok and "need a name" in result.message.lower()
    assert not (tmp_path / "Projects").exists()


def test_a_project_name_can_come_from_the_detail_field(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    fake(labels=[], title="Visual Studio Code")
    monkeypatch.setattr(registry, "_exe_candidates", lambda spec: [])
    result = appfunc.run("vscode", "create_project", detail="nebula website", target="")
    assert result.ok
    assert (tmp_path / "Projects" / "nebula website" / "index.html").exists()


def test_creating_a_project_without_the_editor_still_reports_the_files(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Projects")
    tools = fake(labels=[], title="")
    monkeypatch.setattr(registry, "_exe_candidates", lambda spec: [])
    result = appfunc.run("vscode", "create_project", target="demo")
    assert result.ok and not result.verified
    assert (tmp_path / "Projects" / "demo" / "index.html").exists()
    assert "couldn't start the editor" in result.message


def test_launching_a_game_presses_play_and_looks_for_the_game_window(fake):
    tools = fake(labels=["Play", "Settings"], title="Legacy Launcher",
                 windows=["Legacy Launcher", "Minecraft 1.21"])
    result = appfunc.run("legacy_launcher", "launch_game")
    assert result.ok and result.verified
    assert ("click", "Play") in tools.calls
    assert "Minecraft" in result.message


def test_an_app_that_is_not_installed_is_refused_by_name(fake):
    tools = fake()
    result = appfunc.run("nothing like this", "open")
    assert not result.ok
    assert "nothing like this" in result.message
    assert tools.calls == []


def test_an_action_the_app_cannot_do_lists_what_it_can(fake):
    tools = fake()
    result = appfunc.run("notepad", "send_message", detail="hi", target="someone")
    assert not result.ok
    assert "can't do" in result.message and "write_text" in result.message
    assert tools.calls == []


def test_media_keys_are_sent_without_a_window(fake, monkeypatch):
    from core import os_ops
    pressed = []
    monkeypatch.setattr(os_ops, "press_key", pressed.append)
    result = appfunc.run("spotify", "next")
    assert result.ok and pressed == ["next"] and not result.verified


def test_asking_a_chat_app_types_the_prompt_and_sends_it(fake):
    tools = fake(labels=["Message ChatGPT", "New chat"], title="ChatGPT")
    result = appfunc.run("chatgpt", "ask", detail="explain quantum tunnelling simply")
    assert ("type", "explain quantum tunnelling simply") in tools.calls
    assert ("press", "enter") in tools.calls
    assert result.ok


def test_a_settings_page_is_opened_by_uri(monkeypatch, fake):
    opened = []
    monkeypatch.setattr("os.startfile", lambda uri: opened.append(uri))
    fake(labels=[], title="Settings")
    result = appfunc.run("system_settings", "settings", detail="display")
    assert opened == ["ms-settings:display"]
    assert result.ok and result.verified
