"""Retro's hands: what it is allowed to do, and what it refuses to be told to do.

Nothing here types into a real window or clicks a real button - that is checked by
hand, on the desktop, because that is the only place it can be checked. What is
checked here is the surface: the registry, the arguments, the key whitelist, and
the rule that a spoken name is only ever matched against what the system reported.
"""
import pytest

from core import tools
from core.model import Result


# ---------- arguments -------------------------------------------------------

def test_a_missing_required_argument_is_refused():
    with pytest.raises(ValueError):
        tools.validate_args(tools.TOOLS["open_app"], {})


def test_arguments_are_bounded():
    clean = tools.validate_args(tools.TOOLS["read_screen"], {"limit": 5000})
    assert clean["limit"] == 80, "a silly limit should be clamped, not obeyed"


def test_a_choice_argument_falls_back_to_its_default():
    clean = tools.validate_args(tools.TOOLS["list_folder"], {"name": "downloads"})
    assert clean == {"name": "downloads", "sort": "recent", "limit": 10}


def test_an_unknown_choice_is_refused():
    with pytest.raises(ValueError):
        tools.validate_args(tools.TOOLS["list_folder"], {"name": "downloads", "sort": "mtime"})


def test_keys_are_checked_when_the_plan_is_read_not_when_it_is_pressed():
    clean = tools.validate_args(tools.TOOLS["press_keys"], {"keys": "ctrl+shift+t"})
    assert clean == {"keys": "ctrl+shift+t"}
    with pytest.raises(ValueError):
        tools.validate_args(tools.TOOLS["press_keys"], {"keys": "ctrl+rm"})


def test_letter_keys_resolve_to_their_virtual_code(monkeypatch):
    """Letters go through VkKeyScanW, which takes a character and not a code point.

    It was called with `ord(key)`, ctypes raised an ArgumentError, and every letter
    combination failed on this machine: ctrl+l reached no search box and ctrl+s saved
    nothing, while enter and the function keys worked. A plan could therefore open an
    app and then do nothing inside it.
    """
    assert tools._vk_for("l") == 0x4C
    assert tools._vk_for("s") == 0x53
    sent = []
    monkeypatch.setattr(tools, "_send_key", lambda code, up=False, unicode_char=None: sent.append(code))
    assert tools.press_keys("ctrl+l") == ["ctrl", "l"]
    assert sent[:2] == [0x11, 0x4C] and sent[2:] == [0x4C, 0x11]


@pytest.mark.parametrize("combo,expected", [
    ("ctrl+l", ["ctrl", "l"]), ("Enter", ["enter"]), ("ctrl shift t", ["ctrl", "shift", "t"]),
    ("f5", ["f5"]), ("pagedown", ["pagedown"]),
])
def test_combinations_are_parsed(combo, expected):
    assert tools.parse_keys(combo) == expected


@pytest.mark.parametrize("bad", ["", "ctrl+rm", "sudo", "launch"])
def test_only_real_keys_are_pressed(bad):
    with pytest.raises(ValueError):
        tools.parse_keys(bad)


# ---------- folders ---------------------------------------------------------

def test_the_workspace_is_the_workspace():
    from core import config
    assert tools.folder_path("RetroWorkspace") == config.WORKSPACE


def test_a_known_folder_is_asked_of_windows_not_guessed(monkeypatch):
    monkeypatch.setattr(tools, "_known_folder", lambda folder_id: r"D:\Downloads")
    assert tools.folder_path("downloads") == r"D:\Downloads"
    assert tools.folder_path("Downloads") == r"D:\Downloads"
    assert tools.folder_path("my downloads folder") == r"D:\Downloads"


def test_an_unknown_folder_is_refused():
    assert tools.folder_path("system32") is None
    assert tools.folder_path("C:/Windows") is None


def test_a_deep_path_never_reaches_the_file_system(monkeypatch):
    monkeypatch.setattr(tools, "_known_folder", lambda folder_id: r"C:\Users\HP\Downloads")
    result = tools.run("list_folder", {"name": "../../Windows/System32"})
    assert not result.ok, "a path in the folder name must not be accepted"


def test_listing_a_folder_reports_the_newest_first(tmp_path, monkeypatch):
    (tmp_path / "old.txt").write_text("x")
    (tmp_path / "new.txt").write_text("y")
    import os
    import time
    os.utime(tmp_path / "old.txt", (time.time() - 500, time.time() - 500))
    monkeypatch.setattr(tools, "folder_path", lambda name: str(tmp_path))
    result = tools.run("list_folder", {"name": "downloads"})
    assert result.ok and result.message.index("new.txt") < result.message.index("old.txt")


# ---------- the safety gate -------------------------------------------------

def test_an_invented_tool_is_refused():
    result = tools.run("delete_everything", {"name": "x"})
    assert not result.ok and "don't know how" in result.message


def test_bad_arguments_never_reach_the_tool(monkeypatch):
    ran = []
    monkeypatch.setitem(tools.TOOLS, "demo",
                        tools.Tool("demo", "read", "", {"text": ("text", 5)},
                                   lambda a, task=None: ran.append(a) or Result(True, "ok"), None))
    assert not tools.run("demo", {"text": "this is far too long"}).ok
    assert ran == []


def test_a_sensitive_tool_runs_only_after_a_yes(monkeypatch):
    ran = []
    monkeypatch.setitem(tools.TOOLS, "demo_danger",
                        tools.Tool("demo_danger", "sensitive", "Do the dangerous thing",
                                   {}, lambda a, task=None: Result(True, "done"), None))
    assert not tools.run("demo_danger", {}, confirm=lambda question: False).ok
    assert ran == []
    assert tools.run("demo_danger", {}, confirm=lambda question: True).ok


def test_refusing_a_sensitive_tool_says_cancelled(monkeypatch):
    monkeypatch.setitem(tools.TOOLS, "demo_danger",
                        tools.Tool("demo_danger", "sensitive", "Do it", {},
                                   lambda a, task=None: Result(True, "done"), None))
    result = tools.run("demo_danger", {}, confirm=lambda question: False)
    assert not result.ok and result.message == "Cancelled."


# ---------- opening things --------------------------------------------------

def test_a_site_name_becomes_an_address(monkeypatch):
    opened = []
    monkeypatch.setattr(tools.os_ops, "open_url", lambda url: opened.append(url) or True)
    tools.run("open_website", {"url": "chatgpt"})
    assert opened == ["https://chatgpt.com"]


def test_a_bare_domain_gets_a_scheme(monkeypatch):
    opened = []
    monkeypatch.setattr(tools.os_ops, "open_url", lambda url: opened.append(url) or True)
    tools.run("open_website", {"url": "example.com"})
    assert opened == ["https://example.com"]


def test_an_unknown_name_becomes_a_search(monkeypatch):
    opened = []
    monkeypatch.setattr(tools.os_ops, "open_url", lambda url: opened.append(url) or True)
    tools.run("open_website", {"url": "legacy launcher"})
    assert opened and "google.com/search" in opened[0]


def test_an_installed_app_is_launched_by_the_index_not_by_the_words(monkeypatch):
    launched = []
    monkeypatch.setattr(tools.app_index, "best",
                        lambda name: (tools.app_index.App(name.title(), r"C:\fake\x.exe",
                                                          "exe", "registry"), []))
    monkeypatch.setattr(tools.app_index, "launch", lambda app: launched.append(app.target) or 4242)
    monkeypatch.setattr(tools.os_ops, "process_alive", lambda pid: True)
    result = tools.run("open_app", {"name": "legacy launcher"})
    assert result.ok and launched == [r"C:\fake\x.exe"]
    assert "Legacy Launcher" in result.message, "the reply should use the installed name"


def test_an_app_that_is_not_installed_says_so_with_the_alternatives(monkeypatch):
    monkeypatch.setattr(tools.app_index, "best",
                        lambda name: (None, [tools.app_index.App("Legacy Launcher", "x", "exe", "s")]))
    result = tools.run("open_app", {"name": "legacy"})
    assert not result.ok and "Legacy Launcher" in result.message


# ---------- windows ---------------------------------------------------------

def test_focusing_reports_what_actually_came_to_the_front(monkeypatch):
    monkeypatch.setattr(tools, "window_titles", lambda limit=60: [(1, "Notepad - untitled")])
    monkeypatch.setattr(tools, "bring_to_front", lambda hwnd: True)
    monkeypatch.setattr(tools, "foreground_title", lambda: "Audacity")
    result = tools.run("focus_window", {"title": "notepad"})
    assert not result.ok or not result.verified
    assert "Audacity" in result.message, "it must report the real foreground, not assume"


def test_focusing_verifies_itself(monkeypatch):
    monkeypatch.setattr(tools, "window_titles", lambda limit=60: [(1, "Notepad - untitled")])
    monkeypatch.setattr(tools, "bring_to_front", lambda hwnd: True)
    monkeypatch.setattr(tools, "foreground_title", lambda: "Notepad - untitled")
    result = tools.run("focus_window", {"title": "notepad"})
    assert result.ok and result.verified


def test_a_window_that_is_not_open_is_reported(monkeypatch):
    monkeypatch.setattr(tools, "window_titles", lambda limit=60: [(1, "Notepad")])
    result = tools.run("focus_window", {"title": "spotify"})
    assert not result.ok and "No open window" in result.message


def test_the_only_command_tool_asks_first_and_refuses_the_destructive_shapes():
    """Running a development command is real work - and the one tool that can do
    whatever the user can, so it is sensitive, and the shapes that destroy things
    are refused before a confirmation is even offered."""
    assert tools.TOOLS["run_command"].tier == "sensitive"
    asked = []
    refused = tools.run("run_command", {"command": "echo hi"},
                        confirm=lambda question: (asked.append(question), False)[1])
    assert not refused.ok and "Cancelled" in refused.message
    assert asked, "the user is asked before a command runs"
    blocked = tools.run("run_command", {"command": "format D:"},
                        confirm=lambda question: True)
    assert not blocked.ok and "won't run" in blocked.message


# ---------- memory -----------------------------------------------------------

def _temp_memory(tmp_path, monkeypatch):
    from core import memory
    monkeypatch.setattr(memory, "folder", lambda: tmp_path)
    return memory


def test_forget_removes_the_note_it_matches_and_says_what_is_left(tmp_path, monkeypatch):
    memory = _temp_memory(tmp_path, monkeypatch)
    memory.remember("my sister is called Ria")
    memory.remember("prefers short answers")
    result = tools.run("forget", {"query": "my sister is called Ria"})
    assert result.ok and result.verified
    assert [entry["text"] for entry in memory.entries()] == ["prefers short answers"]


def test_forgetting_a_note_that_is_not_there_is_an_answer_not_an_error(tmp_path, monkeypatch):
    """A red failure for \"I don't have a note about that\" would read as a broken tool."""
    memory = _temp_memory(tmp_path, monkeypatch)
    memory.remember("prefers short answers")
    result = tools.run("forget", {"query": "my birthday"})
    assert result.ok
    assert "don't have a note" in result.message
    assert not result.verified, "nothing was removed, so nothing may be claimed"
    assert len(memory.entries()) == 1


def test_a_note_can_be_taken_back_in_chat_mode(tmp_path, monkeypatch):
    """Remembering is allowed there, so taking it back has to be too."""
    from core import modes
    assert modes.allows_tool(modes.CHAT, tools.TOOLS["forget"])
    assert modes.allows_tool(modes.CHAT, tools.TOOLS["remember"])
