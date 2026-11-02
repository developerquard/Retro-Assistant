"""Retro, the window.

Start it and Retro listens in the background with only its agent circle on screen,
pinned on top of everything and click-through. Say "Retro, open your settings" or
use the tray icon to bring up the full window, and close that window to quit.

    python main_gui.py             # start as the floating circle
    python main_gui.py --window    # start with the settings panel already open
    python main_gui.py --no-voice  # no microphone: typed commands only
    python main_gui.py --selftest  # build and draw offscreen, then exit 0 or 1
                                   #   --deep also loads the voice and model stack,
                                   #   --say also plays one line so sound is proven
    python main_gui.py --plan "..."  # ask the model to plan one request, run nothing
    python main_gui.py --cli ...    # re-enter as the coding terminal (packaged builds)

Say "Retro" while it is working and it stops, carries on, or switches to what you
asked for instead. The agent, the tools and the watchdog are in docs/M6_AGENT.md.

The packaged build has no console, so an unhandled error is written to
%LOCALAPPDATA%\\Retro\\Logs\\ui-crash.log as well as being shown.
"""
import datetime as dt
import os
import sys
import traceback


def crash_log_path():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Retro", "Logs", "ui-crash.log")


def record_crash():
    text = traceback.format_exc()
    path = crash_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"\n--- {dt.datetime.now():%Y-%m-%d %H:%M:%S} ---\n{text}")
    except OSError:
        pass
    return path


def cli_arguments():
    """`--cli` re-enters this executable as the coding terminal.

    A packaged build normally has RetroCLI.exe beside it and the handoff starts that
    directly. This is the way in when it does not: the window and the terminal are the
    same frozen image, and only one of them needs Qt at a time.
    """
    argv = list(sys.argv[1:])
    if "--cli" not in argv:
        return None
    argv.remove("--cli")
    return argv


def main():
    arguments = cli_arguments()
    if arguments is not None:
        from retrocli.cli import main as cli_main
        return cli_main(arguments)
    try:
        from ui.app import main
        return main()
    except Exception:
        path = record_crash()
        try:                      # show a dialog when there is no console to read
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "Retro could not start",
                                 f"{traceback.format_exc().strip().splitlines()[-1]}\n\n"
                                 f"Details: {path}")
        except Exception:
            pass
        print(f"Retro could not start; details in {path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
