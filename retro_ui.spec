# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Retro's window.

    .venv/Scripts/python.exe -m PyInstaller retro_ui.spec --noconfirm

One directory rather than one file: the stack already carries Qt, ctranslate2 and
llama.cpp, so a single packed file would start much more slowly for no benefit.

The GGUF and Whisper models are deliberately NOT bundled. They are hundreds of
megabytes each, they are found on disk at run time (core/config.py), and bundling
them would rebuild them into the app every time it changed.
"""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# Windows Defender deletes PyInstaller's own windowed loader on some machines, so
# tools/build_exe.py builds an equivalent one (the console loader with its PE
# subsystem set to GUI) and passes its path here. Without the variable, the stock
# bootloader is used.
_bootloader_path = os.environ.get("RETRO_BOOTLOADER_PATH")
if _bootloader_path:
    from PyInstaller.building.api import EXE as _EXE

    _stock_bootloader_file = _EXE._bootloader_file

    def _bootloader_file(self, exe, extension=None):
        if not self.console:                  # only the windowed build is replaced
            return _bootloader_path
        return _stock_bootloader_file(self, exe, extension)

    _EXE._bootloader_file = _bootloader_file

hiddenimports = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    # Speech is SAPI through comtypes now (pyttsx3 hangs on the second line in a row),
    # and UI Automation is comtypes as well, so the generated type-library modules
    # have to be found at run time rather than guessed from the imports.
    "comtypes",
    "comtypes.client",
    "pythoncom",
    "pywintypes",
    "win32com",
    "win32com.client",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "huggingface_hub",
    "llama_cpp",
    "sounddevice",
    "pycaw",
    "psutil",
    # The agent's hands (core/tools.py) read the screen and click controls through
    # UI Automation. It is imported inside a function so a machine without it still
    # runs, which is also why PyInstaller has to be told about it here.
    "uiautomation",
    "comtypes.gen",
    "comtypes.gen.UIAutomationClient",
]
# The coding terminal is normally its own executable (retrocli.spec). A build that
# has to re-enter this exe with `--cli` needs the package inside this one too, and
# PyInstaller cannot see an import that only happens after a command-line flag.
hiddenimports += collect_submodules("retrocli")
hiddenimports += collect_submodules("ui")
hiddenimports += collect_submodules("core")

hiddenimports += collect_submodules("faster_whisper")
hiddenimports += collect_submodules("uiautomation")
# Japanese and Hindi (core/neuralvoice.py). edge-tts is imported inside a function so
# a machine without it still speaks English, which is exactly why PyInstaller has to
# be told: the import it cannot see is the one that would be missing from the bundle.
# `aiohttp`/`certifi` come with it and are what the HTTPS call really needs.
hiddenimports += collect_submodules("edge_tts")
hiddenimports += ["edge_tts", "aiohttp", "certifi"]

# The eyes: PyAutoGUI screenshots through pyscreeze and Pillow, and OpenCV for
# the confidence option in find_on_screen. PIL was excluded before this - the
# screen reader never needed it - so it is deliberately allowed back in, and the
# packages are named because pyautogui imports its platform backend lazily.
for _name in ("pyautogui", "pyscreeze", "pygetwindow", "pytweening", "pymsgbox"):
    hiddenimports += collect_submodules(_name)
hiddenimports += ["PIL", "PIL.Image", "PIL.ImageGrab", "PIL.ImageTk",
                  "cv2", "numpy"]

binaries = collect_dynamic_libs("llama_cpp")
datas = [("assets/retro.ico", "assets"), ("assets/chevron.svg", "assets"),
         ("ui", "ui"), ("core", "core")]

excludes = [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython",
    "PyQt5", "PyQt6", "PySide2",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtSerialPort", "PySide6.QtTest",
    "setuptools", "pip", "pytest",
]

a = Analysis(
    ["main_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Retro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="assets/retro.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Retro",
)
