"""Retro in the Windows startup list, so it is there after the password.

The owner asked for one thing here, in as many words: *open laptop, type the
password, the logo plays, Retro is ready*. That means Retro has to be started by
Windows itself, and the way to be started by Windows is to be in the startup list -
which on Windows is one value under `HKCU\\...\\CurrentVersion\\Run`, the same
entry Task Manager shows under **Startup apps**.

Two rules shape the rest:

* **The user's registry is theirs.** Nothing is written unless they asked for it -
  the first run offers it once and records the answer, the panel switch and the
  tray item change it afterwards, and `RETRO_NO_STARTUP` in the environment stops
  even the offer (which is how the tests and the offscreen self-test stay clean).
* **A refused write is reported, never raised.** A registry that will not accept
  the value is a thing to say out loud - the app keeps running either way.

The command is the frozen executable when there is one - and, from a source
checkout, the built `dist\\Retro\\Retro.exe` beside it if that exists - always
quoted, because the path under `Program Files` or a user folder can contain a
space. Only a checkout with no build at all registers `pythonw main_gui.py`.
"""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Retro"
# The switch that stops Retro registering itself at all. Set by the offscreen
# self-test, by the test suite, and by anyone who simply does not want it touched.
OPT_OUT = "RETRO_NO_STARTUP"


def is_windows():
    return sys.platform == "win32"


def opted_out(environ=None):
    """True when something in the environment says hands off the startup list."""
    env = os.environ if environ is None else environ
    return str(env.get(OPT_OUT, "")).strip().lower() in ("1", "true", "yes", "on")


PACKAGED = os.path.join("dist", "Retro", "Retro.exe")


def app_path():
    """The thing Windows should run at logon.

    Frozen, that is the exe itself. Running from a source checkout it is the built
    copy beside it, if one has been made - not this interpreter. The two builds live
    side by side here, and registering `pythonw main_gui.py` made the logon start the
    development copy while `dist\\Retro\\Retro.exe` sat there unopened: the owner then
    ran the exe by hand and got whichever build was packed last, which is the same
    program only by name. The packaged one is also the one that starts without a venv
    or a console behind it, so it is what belongs in the startup list.

    With no build on disk there is nothing else to run, so the script is used.
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    built = os.path.join(here, PACKAGED)
    if os.path.exists(built):
        return built
    return os.path.join(here, "main_gui.py")


def interpreter():
    """The interpreter to start the script with - and one with no console.

    `python.exe` opens a black window behind Retro at every logon. The windowless
    twin sits next to it under the same name with a `w`, so it is used when it is
    there; when it is not, a console window is a small price for starting at all.
    """
    exe = os.path.abspath(sys.executable)
    if os.path.basename(exe).lower() == "pythonw.exe":
        return exe
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.exists(candidate) else exe


def command():
    """The full command line Windows will run at logon, quoted for its spaces."""
    script = app_path()
    if getattr(sys, "frozen", False) or script.lower().endswith(".exe"):
        return f'"{script}"'
    return f'"{interpreter()}" "{script}"'


# The registry calls, named here so every one of them is a single thing to fake in
# a test. Nothing above this line touches the system.
def _run_key(winreg, create=False):
    """The startup key, opened for reading - or created, which a fresh profile needs.

    `OpenKey` on a key that is not there raises, and a brand new Windows profile is
    exactly where somebody first runs this. Written with `CreateKeyEx` so the entry can
    be written on a machine whose `Run` key has never existed, and read with `OpenKey`
    so a read never leaves an empty key behind.
    """
    if create:
        return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                  winreg.KEY_READ | winreg.KEY_SET_VALUE)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)


def saved_command(winreg=None):
    """What is registered now, or None. Never raises."""
    if not is_windows():
        return None
    if winreg is None:
        import winreg                      # noqa: PLC0415 - Windows only, imported here
    try:
        with _run_key(winreg) as key:
            return str(winreg.QueryValueEx(key, VALUE_NAME)[0])
    except OSError:
        return None
    except Exception:                      # a registry that answers with something else
        return None


def is_enabled(winreg=None):
    """True when the entry is there and still points at this copy of Retro.

    Moving the app or rebuilding it into a new folder leaves the old path behind,
    and an entry that runs a path which is not there is worse than none: Windows
    shows Retro as enabled in Task Manager and nothing happens at logon. Pointing
    somewhere else therefore counts as *not* enabled, and `enable()` rewrites it.
    """
    saved = saved_command(winreg)
    return saved is not None and _same(saved, command())


def _same(left, right):
    return str(left or "").strip().strip('"').lower() == str(right or "").strip().strip('"').lower()


def enable(winreg=None):
    """Puts Retro in the startup list. Returns (ok, what to say about it)."""
    if not is_windows():
        return False, "starting with Windows is a Windows feature"
    if winreg is None:
        import winreg                      # noqa: PLC0415
    line = command()
    try:
        with _run_key(winreg, create=True) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, line)
    except Exception as exc:
        return False, f"could not add Retro to startup ({type(exc).__name__})"
    return True, "I'll open with Windows from now on."


def disable(winreg=None):
    """Takes Retro back out of the startup list. Returns (ok, what to say)."""
    if not is_windows():
        return False, "starting with Windows is a Windows feature"
    if winreg is None:
        import winreg                      # noqa: PLC0415
    try:
        with _run_key(winreg, create=True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, "I wasn't starting with Windows anyway."
    except Exception as exc:
        return False, f"could not remove Retro from startup ({type(exc).__name__})"
    return True, "I won't open with Windows any more."


def status(winreg=None):
    """One line about the startup entry, for the panel, the log and the answer."""
    if not is_windows():
        return "not available on this system", False
    if not is_enabled(winreg):
        other = saved_command(winreg)
        if other:
            # Somebody else registered Retro, or a previous install did and moved.
            return f"registered to a path that is not this one ({other[:60]})", False
        return "not in the startup list", False
    return "opens with Windows", True


def ensure(decision, winreg=None):
    """Applies a remembered decision. `decision` is what settings.json holds.

    Three states, and they are not two:

    * **True** - the owner asked for it: make it so, and repair a stale path.
    * **False** - the owner said no: if an entry of ours is there, it was put there
      by an older build, so only a decision of ours is removed and a foreign one is
      left alone.
    * **None** - nobody has ever been asked. Nobody is registered behind their back,
      and the answer is not written down: `ui/app.py` asks for real, on the first
      run, and the reply is what gets saved.
    """
    if decision is True:
        return enable(winreg)
    if decision is False:
        # Only our own entry: an entry named Retro pointing somewhere else belongs
        # to another copy, and deleting it would be us tidying up someone else's
        # machine. It is reported instead, by `status()`.
        if not is_enabled(winreg):
            return True, "I wasn't starting with Windows anyway."
        return disable(winreg)
    return False, "leave the startup list alone"
