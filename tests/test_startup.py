"""The startup entry: Retro in the list after a password, and never behind a back.

The registry is the one thing here that cannot be tried for real in a test, so it is
faked - a dictionary pretending to be `HKCU\\...\\Run` - and the interesting cases are
the ones that are not the happy path: an entry pointing at a copy of Retro that has
since moved, a registry that refuses the write, and a decision that was never made.
"""
import os
import sys

import pytest

from core import startup


class FakeRegistry:
    """Just enough of `winreg` to be the startup key, and to be able to say no."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, refuse=False):
        self.values = {}
        self.refuse = refuse

    def OpenKey(self, _root, _path, _reserved, _access):
        if self.refuse:
            raise OSError("access denied")
        return FakeKey(self.values, self.refuse)

    def CreateKeyEx(self, _root, _path, _reserved, _access):
        if self.refuse:
            raise OSError("access denied")
        return FakeKey(self.values, self.refuse)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        if self.refuse:
            raise OSError("access denied")
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


class FakeKey:
    def __init__(self, values, refuse=False):
        self.values = values
        self.refuse = refuse

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def windows(monkeypatch):
    """A machine that is Windows, with a registry that accepts writes."""
    monkeypatch.setattr(startup, "is_windows", lambda: True)
    return FakeRegistry()


def test_the_command_is_quoted_and_runs_the_app(windows):
    line = startup.command()
    assert line.startswith('"') and line.endswith('"'), \
        f"{line!r} is not quoted - a path with a space in it would split in two"
    assert startup.app_path() in line or os.path.basename(startup.app_path()) in line


def test_enabling_writes_the_entry_and_says_so(windows):
    ok, message = startup.enable(windows)
    assert ok, message
    assert windows.values["Retro"] == startup.command()
    assert startup.is_enabled(windows), "the entry was written but not read back"
    assert startup.saved_command(windows) == startup.command()


def test_disabling_takes_it_back_out(windows):
    startup.enable(windows)
    ok, message = startup.disable(windows)
    assert ok and "won't open with Windows" in message
    assert startup.saved_command(windows) is None
    assert not startup.is_enabled(windows)
    ok, message = startup.disable(windows)         # and again is not an error
    assert ok and "wasn't starting" in message


def test_an_entry_pointing_at_another_copy_is_not_this_one(windows):
    """The stale entry is the failure this is here for: Task Manager says *enabled*.

    Windows would start a path that no longer exists, and Retro would never appear -
    with a startup list that swears it is there. Pointing somewhere else therefore
    reads as *not enabled*, and enabling rewrites it.
    """
    windows.values["Retro"] = '"D:\\older Retro\\Retro.exe"'
    assert startup.saved_command(windows) is not None
    assert not startup.is_enabled(windows)
    message, on = startup.status(windows)
    assert not on and "not this one" in message
    ok, _ = startup.enable(windows)
    assert ok and startup.is_enabled(windows)
    assert windows.values["Retro"] == startup.command()


def test_a_refused_write_is_reported_and_not_raised(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: True)
    blocked = FakeRegistry(refuse=True)
    ok, message = startup.enable(blocked)
    assert not ok and "could not add" in message
    assert startup.saved_command(blocked) is None, "a refusal must not read as success"


def test_status_says_when_the_list_does_not_have_it(windows):
    message, on = startup.status(windows)
    assert not on and message == "not in the startup list"
    startup.enable(windows)
    message, on = startup.status(windows)
    assert on and message == "opens with Windows"


def test_a_first_run_is_never_registered_by_accident(windows):
    """`ensure(None)` is "nobody has decided", and it decides nothing itself."""
    ok, message = startup.ensure(None, windows)
    assert not ok and "leave the startup list alone" in message
    assert windows.values == {}


def test_saying_no_does_not_delete_somebody_else_s_entry(windows):
    windows.values["Retro"] = '"D:\\someone else\\Retro.exe"'
    ok, message = startup.ensure(False, windows)
    assert ok and "wasn't starting" in message
    assert windows.values["Retro"] == '"D:\\someone else\\Retro.exe"', \
        "an entry of another copy was deleted"


def test_opt_out_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv(startup.OPT_OUT, raising=False)
    assert not startup.opted_out()
    for value in ("1", "true", "YES", "on"):
        assert startup.opted_out({startup.OPT_OUT: value})
    for value in ("0", "", "no"):
        assert not startup.opted_out({startup.OPT_OUT: value})


def test_on_a_system_that_is_not_windows_nothing_is_touched(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    ok, message = startup.enable()
    assert not ok and "Windows feature" in message
    ok, message = startup.disable()
    assert not ok and "Windows feature" in message
    message, on = startup.status()
    assert not on and message == "not available on this system"
    assert startup.saved_command() is None


def test_the_windowless_interpreter_is_preferred(monkeypatch, tmp_path):
    """A console window behind the logo at every logon is not the arrival anyone wants."""
    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert startup.interpreter().endswith("pythonw.exe")

    (tmp_path / "pythonw.exe").unlink()
    assert startup.interpreter() == str(fake), "without pythonw, python is better than nothing"


def test_a_source_run_registers_the_built_exe_and_not_the_venv(windows, monkeypatch, tmp_path):
    """The failure the owner hit, in one test.

    The logon entry said `pythonw main_gui.py`, so Windows started the development
    copy - while `dist\\Retro\\Retro.exe`, the build that was meant to open, sat
    there untouched and went stale. A build on disk is what the startup list gets.
    """
    core = tmp_path / "core"
    core.mkdir()
    built = tmp_path / "dist" / "Retro" / "Retro.exe"
    built.parent.mkdir(parents=True)
    built.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".venv" / "python.exe"))
    monkeypatch.setattr(startup, "__file__", str(core / "startup.py"))

    assert startup.app_path() == str(built)
    assert startup.command() == f'"{built}"'
    ok, _ = startup.enable(windows)
    assert ok
    assert windows.values["Retro"] == f'"{built}"', "the venv was registered instead"
    assert startup.is_enabled(windows)


def test_a_checkout_with_no_build_registers_the_script(windows, monkeypatch, tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(startup, "__file__", str(core / "startup.py"))
    assert startup.app_path() == str(tmp_path / "main_gui.py")
    assert "main_gui.py" in startup.command()


def test_a_frozen_build_registers_the_exe_itself(windows, monkeypatch, tmp_path):
    exe = tmp_path / "Retro.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert startup.command() == f'"{exe}"'
    startup.enable(windows)
    assert windows.values["Retro"] == f'"{exe}"'
    assert startup.is_enabled(windows)
