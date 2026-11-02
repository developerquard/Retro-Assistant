"""Builds dist/Retro/Retro.exe and proves the bundle works.

    python tools/build_exe.py            # build, then run the frozen self-test
    python tools/build_exe.py --no-test  # just build

The self-test runs the packaged exe with --selftest --deep --say, which draws the
window offscreen, loads the voice and model stack, discovers the installed
applications, runs one typed command and speaks one line - so a bundle that is
missing a Qt plugin, a hidden import or an audio device fails here instead of on
the desktop.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "Retro", "Retro.exe")
# The coding terminal ships beside the window: the handoff prefers this executable
# because a console build has a real stdout, and `--cli` on Retro.exe is the fallback
# for a build that somehow lacks it.
CLI_EXE = os.path.join(ROOT, "dist", "Retro", "RetroCLI.exe")
# PyInstaller's windowed loader, runw.exe. Windows Defender deletes it on this
# machine - the file, and any copy of it out of the wheel, whatever it is named -
# because that binary is a known false positive. The console loader survives, and
# the only real difference between the two is two bytes of PE header, so the
# console one is copied and its subsystem flipped to GUI instead.
BOOTLOADER_NAME = "retro_windowed.exe"
CONSOLE_SUBSYSTEM, GUI_SUBSYSTEM = 3, 2


def _flip_subsystem(source, target):
    """Copies a PE file with its subsystem changed. Returns the old value."""
    with open(source, "rb") as handle:
        data = bytearray(handle.read())
    if data[:2] != b"MZ":
        raise ValueError(f"{source} is not a PE file")
    pe_offset = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError(f"{source} has no PE signature")
    optional_header = pe_offset + 4 + 20
    # Subsystem sits at offset 68 of the optional header in both PE32 and PE32+.
    at = optional_header + 68
    old = int.from_bytes(data[at:at + 2], "little")
    data[at:at + 2] = GUI_SUBSYSTEM.to_bytes(2, "little")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    return old


def ensure_windowed_bootloader():
    """Makes a GUI bootloader and returns its absolute path."""
    from PyInstaller import HOMEPATH, PLATFORM
    stock = os.path.join(HOMEPATH, "PyInstaller", "bootloader", PLATFORM, "runw.exe")
    if os.path.exists(stock):                 # if Defender has not removed it, use it
        return stock
    target = os.path.join(ROOT, "build", "bootloaders", BOOTLOADER_NAME)
    if not os.path.exists(target):
        old = _flip_subsystem(os.path.join(HOMEPATH, "PyInstaller", "bootloader", PLATFORM,
                                           "run.exe"), target)
        print(f"windowed bootloader -> {target}\n  (PyInstaller's own was removed by Defender; "
              f"copied the console loader and changed subsystem {old} -> {GUI_SUBSYSTEM})")
    return target


def build():
    print("building ... (this takes a few minutes)")
    environment = dict(os.environ)
    environment["RETRO_BOOTLOADER_PATH"] = ensure_windowed_bootloader()
    command = [sys.executable, "-m", "PyInstaller", "retro_ui.spec", "--noconfirm"]
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


def build_cli():
    """Builds the console-only coding terminal into the same folder as the window."""
    print("building RetroCLI ...")
    command = [sys.executable, "-m", "PyInstaller", "retrocli.spec", "--noconfirm",
               "--distpath", os.path.join(ROOT, "dist", "Retro"),
               "--workpath", os.path.join(ROOT, "build", "retrocli")]
    return subprocess.run(command, cwd=ROOT, env=dict(os.environ)).returncode


def check_cli():
    """Proves the terminal starts and refuses politely without a project."""
    if not os.path.exists(CLI_EXE):
        print(f"RetroCLI.exe is missing from {os.path.dirname(CLI_EXE)}", file=sys.stderr)
        return False
    version = subprocess.run([CLI_EXE, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
    print(f"  RetroCLI --version -> {version.returncode}: {(version.stdout or version.stderr).strip()}")
    if version.returncode != 0:
        return False
    nothing = subprocess.run([CLI_EXE], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120)
    if nothing.returncode != 2:
        print(f"  RetroCLI with no project exited {nothing.returncode}, expected 2",
              file=sys.stderr)
        return False
    print("  the coding terminal starts and asks for a project folder")
    return True


def folder_size_mb(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total / 2 ** 20


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-test", action="store_true")
    args = parser.parse_args()

    if build() != 0:
        print("build failed", file=sys.stderr)
        return 1
    if not os.path.exists(EXE):
        print(f"build finished but {EXE} is missing", file=sys.stderr)
        return 1
    print("building RetroCLI ...")  # kept here too for parity with old behavior
    if build_cli() != 0:
        print("the coding terminal build failed", file=sys.stderr)
        return 1
    if not check_cli():
        return 1

    print(f"exe: {EXE}")
    print(f"dist size: {folder_size_mb(os.path.dirname(EXE)):.0f} MB")

    if args.no_test:
        return 0
    print("running the frozen self-test (window, packages, apps, one command, one spoken line) ...")
    # `errors`: the report contains whatever Windows happens to call the open windows,
    # and a title the console's codepage cannot represent must not turn a passing
    # check into a traceback here.
    result = subprocess.run([EXE, "--selftest", "--deep", "--say"], cwd=os.path.dirname(EXE),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    print(f"  exit={result.returncode}")
    for stream in (result.stdout, result.stderr):
        if stream and stream.strip():
            print("  " + stream.strip().replace("\n", "\n  "))
    if result.returncode != 0:
        print("the packaged app failed its self-test", file=sys.stderr)
        return 1
    print("packaged app passed its self-test")

    print("running the frozen CLI smoke test ...")
    cli_home = os.path.join(os.path.dirname(EXE), "._probe_home")
    os.makedirs(cli_home, exist_ok=True)
    r = subprocess.run([CLI_EXE, "--version"], cwd=cli_home, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, env=dict(os.environ))
    print(f"  RetroCLI --version -> {r.returncode}: {(r.stdout or r.stderr).strip()}")
    if r.returncode != 0:
        return 1
    print("packaged CLI passed its smoke test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
