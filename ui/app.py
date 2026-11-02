"""Retro's window: the circle that floats over the desktop, and the panel behind it.

Two shapes, one window. Floating, it is the orb and nothing else - transparent to the
mouse, on top of everything, no words at all. The panel is the same window with the
circle at the top and the controls under it: the prompt, the model and voice pickers,
the privacy switches, the writing card and the shelf of documents Retro has written.

Nothing in here does the work. The session owns the assistant, the bridge carries its
reports over on Qt signals, and the coding terminal is a separate process that this
window only hands a task to. That is why the window can be built and drawn offscreen
in a test with no model, no microphone and no desktop.
"""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence, QPainter,
                           QPainterPath, QPen, QShortcut)
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QLabel, QLineEdit,
                               QMainWindow, QPushButton, QSlider, QSystemTrayIcon,
                               QVBoxLayout, QHBoxLayout, QWidget)

from core import config, handoff, settings, startup, ui_hook
from ui import theme
from ui.agent import AgentWidget
from ui.bridge import Bridge
from ui.conversation import Conversation, HistoryField, StatusStrip
from ui.writing import HEIGHT as STAGE_H, SHELF_HEIGHT, DocumentShelf, WritingStage

APP_NAME = "Retro"
APP_VERSION = "2.1.0"

# The panel is a fixed size, so a label wrapping never moves the circle. Anything that
# comes and goes - the writing card, the shelf of documents - adds its own height.
PANEL_W = 430
PANEL_H = 680
ORB = 200                      # the circle's own square, inside the panel
HEADER_H = 34

# The floating window is bigger than the circle: the ring's glow, and the space the
# orb needs so that a window edge never cuts a square band around it.
FLOAT_WINDOW_SCALE = 1.45

ARRIVAL_LINE = "Retro's ready."

BASE_FLAGS = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
              | Qt.WindowType.Tool)


def asset_path(name):
    """A file from the packaged assets folder, wherever this build keeps it."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates += [Path(bundle) / "assets" / name, Path(bundle) / name]
    here = Path(__file__).resolve().parent.parent
    candidates += [here / "assets" / name, Path.cwd() / "assets" / name, Path.cwd() / name]
    for path in candidates:
        if path.exists():
            return str(path)
    return str(candidates[-1] if candidates else name)


def _style_button(button, checked_text=None, unchecked_text=None):
    """A small uppercase switch that says its own state."""
    button.setCheckable(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if checked_text is not None and unchecked_text is not None:
        button.setText(unchecked_text if not button.isChecked() else checked_text)
    return button


class RetroWindow(QMainWindow):
    """The panel and the floating circle, over one session."""

    # The core and the handoff report from their own threads. A Qt widget may only
    # be built or touched on the GUI thread, so every event crosses as a signal:
    # opening the discussion window directly from the voice thread is what froze the
    # whole app the moment somebody said "switch to code".
    coreEvent = Signal(str)
    handoffEvent = Signal(object)

    def __init__(self, bridge=None, floating=True, voice=True, argv=None):
        super().__init__()
        self.argv = list(argv if argv is not None else sys.argv[1:])
        self._voice = bool(voice)
        self.bridge = bridge or Bridge(parent=self)
        self._floating = bool(floating)
        self._circle_size = theme.FLOAT_SIZE_DEFAULT
        self._session_state = "offline"
        self._reply = ""
        self._coding_live = False
        self._code_chat = None
        self._arriving = False
        self._announce_when_ready = False
        self._ready_result = None
        self._ready_done = False
        self._mic_action = None
        self._tray = None
        self._drag_origin = None
        self._panel_widgets = []
        self._closing = False
        self._placed = False
        # Once the window has been dragged, its corner belongs to the person using
        # it: a size change must not snap it back to the screen's corner.
        self._free_position = False
        self._dragged = False

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setStyleSheet(theme.STYLESHEET)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(BASE_FLAGS)
        self.setMouseTracking(True)

        self._build()
        self._wire()
        self.agent.set_state("offline")
        self._fill_models()
        self._load_voices()
        self._load_outputs()
        self._sync_look_button()
        self._sync_background_button()
        self._label_mic_button()
        self._select_current_model()
        self.set_floating(self._floating, quiet=True)
        if not self._floating:
            self._refresh_shelf()
        self._fit_panel()
        self.show()
        # The composer has the focus on arrival, so typing lands where the person is
        # about to type and the first header button is not the thing wearing the
        # accent ring.
        self.prompt.setFocus()
        # The corner, not the middle: Retro lives beside the clock, where an
        # assistant is looked for, and the panel grows up from the same corner.
        self._place()
        self._placed = True

        self._room_timer = QTimer(self)
        self._room_timer.setInterval(120)
        self._room_timer.timeout.connect(self._poll_room)
        self._room_timer.start()

    # ------------------------------------------------------------------ build

    def _build(self):
        self.panel = QWidget(self)
        self.panel.setObjectName("panel")
        self.panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self.panel)
        outer.setContentsMargins(18, 12, 18, 14)
        outer.setSpacing(8)

        # ---- header
        header = QHBoxLayout()
        header.setSpacing(6)
        self.mark = QLabel("RETRO")
        self.mark.setStyleSheet(
            f"color: {theme.TEXT_DIM.name()}; font-size: 11px; font-weight: 700;"
            f" letter-spacing: 3px;")
        self.mark.setToolTip(f"{APP_NAME} {APP_VERSION}")
        header.addWidget(self.mark)
        header.addStretch(1)

        self.code_btn = QPushButton("CODING")
        self.code_btn.setToolTip("Open the coding discussion - a separate agent with "
                                 "its own model, its own terminal and its own key")
        self.code_btn.setObjectName("ghost")
        self.code_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        header.addWidget(self.code_btn)

        self.settings_btn = QPushButton("SETTINGS")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.setCheckable(True)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Model, voice, microphone and circle size (Ctrl+, )")
        header.addWidget(self.settings_btn)

        self.float_btn = QPushButton("FLOAT OFF")
        self.float_btn.setObjectName("floatbtn")
        self.float_btn.setCheckable(True)
        self.float_btn.setToolTip("Shrink to the circle and stay on top of other windows")
        header.addWidget(self.float_btn)

        self.close_btn = QPushButton("\u00d7")
        self.close_btn.setObjectName("close")
        self.close_btn.setToolTip("Close Retro")
        header.addWidget(self.close_btn)
        outer.addLayout(header)
        self._header = header

        # The circle is drawn by the window, not laid out by it, so its square has to
        # be reserved here. Without this the prompt and the reply line were painted
        # underneath the orb - present in the object tree, invisible on the screen.
        outer.addSpacing(ORB + 6)

        # ---- what the session is doing, in one line under the circle
        self.status = StatusStrip()
        outer.addWidget(self.status)

        # ---- the record: every turn of the conversation, newest at the bottom
        self.talk = Conversation()
        outer.addWidget(self.talk, 1)

        # ---- the circle (a sibling of the panel, placed by _layout)
        self.agent = AgentWidget(self)
        # Sized by the geometry below, not fixed: the floating window is taller than
        # the circle and a fixed size would pin it to the top-left corner.
        self.agent.setMinimumSize(120, 120)

        # ---- the composer: one field and one button, nothing to learn
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        composer_row = QHBoxLayout(self.composer)
        composer_row.setContentsMargins(10, 3, 4, 3)
        composer_row.setSpacing(6)
        # A field that remembers what was sent: Up is the previous command.
        self.prompt = HistoryField()
        self.prompt.setObjectName("prompt")
        self.prompt.setPlaceholderText("Type command for Retro...")
        self.prompt.setAccessibleName("Command for Retro")
        self.send_btn = QPushButton("SEND")
        self.send_btn.setObjectName("send")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setToolTip("Send this to Retro (Enter)")
        self.send_btn.setAccessibleName("Send the command")
        self.send_btn.setEnabled(False)
        composer_row.addWidget(self.prompt, 1)
        composer_row.addWidget(self.send_btn)
        outer.addWidget(self.composer)

        # The one-line note under the composer: system remarks, never the answer.
        # The answer lives in the record above, where it stays.
        self.reply_label = QLabel("")
        self.reply_label.setObjectName("hint")
        self.reply_label.setWordWrap(True)
        self.reply_label.setMinimumHeight(14)
        self.reply_label.setMaximumHeight(30)
        outer.addWidget(self.reply_label)

        # ---- what is being written, or what has been
        self.writing = WritingStage(self.panel)
        self.writing.setVisible(False)
        outer.addWidget(self.writing)

        self.shelf = DocumentShelf(self.panel)
        self.shelf.setVisible(False)
        outer.addWidget(self.shelf)

        # ---- the drawer: what is touched once a week, folded away by default
        self.drawer = QFrame()
        self.drawer.setObjectName("drawer")
        self.drawer.setMaximumHeight(200)
        draw = QVBoxLayout(self.drawer)
        draw.setContentsMargins(10, 8, 10, 9)
        draw.setSpacing(6)

        head_row = QHBoxLayout()
        head_row.setSpacing(6)
        drawer_title = QLabel("SETTINGS")
        drawer_title.setObjectName("drawerTitle")
        head_row.addWidget(drawer_title)
        head_row.addStretch(1)
        self.diag_btn = QPushButton("DIAGNOSTICS")
        self.diag_btn.setObjectName("ghost")
        self.diag_btn.setToolTip("Latency and request diagnostics")
        head_row.addWidget(self.diag_btn)
        draw.addLayout(head_row)

        # ---- the pickers, on two rows: four controls on one 378px line truncated
        # every one of them, model label and TEST VOICE included.
        row = QHBoxLayout()
        row.setSpacing(8)
        self.model_combo = QComboBox()
        self.model_combo.setToolTip("Which brain answers. AUTO follows the quickest.")
        self.test_btn = QPushButton("TEST VOICE")
        self.test_btn.setObjectName("ghost")
        self.test_btn.setToolTip("Play a short line through the chosen voice and speaker")
        row.addWidget(self.model_combo, 1)
        row.addWidget(self.test_btn)
        draw.addLayout(row)

        voices = QHBoxLayout()
        voices.setSpacing(8)
        self.voice_combo = QComboBox()
        self.voice_combo.setToolTip("Which voice speaks")
        self.output_combo = QComboBox()
        self.output_combo.setToolTip("Which speaker it speaks through")
        voices.addWidget(self.voice_combo, 1)
        voices.addWidget(self.output_combo, 1)
        draw.addLayout(voices)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("groq.com API key for the cloud brain")
        self.key_input.setToolTip("Stored in Retro's own folder, never in this project")
        self.key_action = QPushButton("SAVE KEY")
        self.key_action.setObjectName("floatbtn")
        key_row.addWidget(self.key_input, 1)
        key_row.addWidget(self.key_action)
        draw.addLayout(key_row)

        # ---- the switches
        switches = QHBoxLayout()
        switches.setSpacing(6)
        self.mic_btn = QPushButton("MIC ON")
        self.mic_btn.setObjectName("micbtn")
        self.mic_btn.setCheckable(True)
        self.mic_btn.setChecked(True)
        self.bg_btn = QPushButton("BACKGROUND HEARING OFF")
        self.bg_btn.setObjectName("micbtn")
        self.bg_btn.setCheckable(True)
        self.bg_btn.setToolTip("React to whatever this machine is playing - the "
                              "microphone stays off")
        self.look_btn = QPushButton("LOOK MUSIC")
        self.look_btn.setObjectName("micbtn")
        self.look_btn.setCheckable(True)
        self.look_btn.setChecked(True)
        self.look_btn.setToolTip("Music Mode walks the colours; Media Mode is white and blue")
        switches.addWidget(self.mic_btn)
        switches.addWidget(self.bg_btn)
        switches.addWidget(self.look_btn)
        switches.addStretch(1)
        draw.addLayout(switches)

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(theme.FLOAT_SIZE_MIN, theme.FLOAT_SIZE_MAX)
        self.size_slider.setValue(theme.FLOAT_SIZE_DEFAULT)
        self.size_slider.setToolTip("How big the floating circle is")
        size_row = QHBoxLayout()
        size_row.setSpacing(8)
        size_note = QLabel("CIRCLE")
        size_note.setObjectName("drawerNote")
        size_row.addWidget(size_note)
        size_row.addWidget(self.size_slider, 1)
        draw.addLayout(size_row)

        outer.addWidget(self.drawer)
        self.drawer.setVisible(False)

        # Everything the floating circle does not carry.
        self._panel_widgets = [self.panel, self.mark, self.settings_btn, self.code_btn,
                               self.diag_btn, self.float_btn, self.close_btn,
                               self.status, self.talk, self.composer, self.prompt,
                               self.send_btn, self.reply_label, self.drawer,
                               self.model_combo, self.voice_combo, self.output_combo,
                               self.test_btn, self.key_input, self.key_action,
                               self.mic_btn, self.bg_btn, self.look_btn,
                               self.size_slider]

    def _wire(self):
        bridge = self.bridge
        bridge.stateChanged.connect(self._on_state)
        bridge.replied.connect(self._on_reply)
        bridge.levelChanged.connect(self._on_level)
        bridge.progressChanged.connect(self._on_progress)
        bridge.writingChanged.connect(self._on_writing)
        bridge.modeChanged.connect(self._on_mode)
        bridge.micChanged.connect(self._on_mic)
        bridge.modelsListed.connect(self._on_models_listed)
        bridge.ready.connect(self._on_ready)
        bridge.voiceTested.connect(self._on_voice_tested)
        bridge.micTested.connect(self._on_mic_tested)
        bridge.settingsRequested.connect(self.open_settings)
        bridge.diagnosticsRequested.connect(self._show_diagnostics)
        bridge.introRequested.connect(self.play_intro)

        # `toggled`, not `clicked`: the switches are also flipped in code (and by
        # voice), and a switch that only listens to the mouse is a switch that lies
        # when something else moves it.
        self.float_btn.toggled.connect(self.set_floating)
        self.close_btn.clicked.connect(self.close)
        self.code_btn.clicked.connect(lambda _checked=False: self.open_code_chat())
        self.diag_btn.clicked.connect(lambda _checked=False: self._show_diagnostics())
        self.test_btn.clicked.connect(lambda _checked=False: self.bridge.test_voice_async())
        self.key_action.clicked.connect(lambda _checked=False: self._save_key())
        self.key_input.returnPressed.connect(self._save_key)
        self.prompt.returnPressed.connect(self._submit)
        self.prompt.textChanged.connect(self._on_prompt_changed)
        self.send_btn.clicked.connect(lambda _checked=False: self._submit())
        self.settings_btn.toggled.connect(self._toggle_settings)
        self.talk.suggested.connect(self._on_suggestion)
        self.talk.speak_requested.connect(self._speak_again)
        self.talk.retry_requested.connect(self._retry)
        self.status.stop_requested.connect(self._stop_task)
        self.mic_btn.toggled.connect(self._on_mic_clicked)
        self.bg_btn.toggled.connect(self._on_background_clicked)
        self.look_btn.toggled.connect(self._on_look_clicked)
        self.size_slider.valueChanged.connect(self._on_size_changed)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        self.output_combo.currentIndexChanged.connect(self._on_output_changed)
        self.writing.folded.connect(self._after_card_folded)
        self.shelf.open_requested.connect(self._open_document)

        self.coreEvent.connect(self._on_core_event)
        self.handoffEvent.connect(self._coding_event)
        self._unsubscribe = ui_hook.subscribe(self.coreEvent.emit)
        self._handoff_unsub = handoff.subscribe(self.handoffEvent.emit)
        self._tray = self._build_tray()
        # Three keys, and each one does the obvious thing rather than opening a menu.
        self._shortcuts = []
        for keys, slot in ((QKeySequence("Ctrl+,"), self._toggle_settings_key),
                           (QKeySequence("Ctrl+L"), self._clear_talk),
                           (QKeySequence(Qt.Key.Key_Escape), self._escape)):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _build_tray(self):
        """A tray icon, so the circle can be brought back after a Close.

        The Microphone action is built whether or not a tray exists on this machine:
        it is the one switch that must always mirror the session, and headless test
        runs have no tray at all.
        """
        self._mic_action = QAction("Microphone", self)
        self._mic_action.setCheckable(True)
        self._mic_action.setChecked(bool(getattr(self.bridge.session, "mic_on", True)))
        self._mic_action.toggled.connect(self._on_mic_clicked)
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return None
            icon = QIcon(asset_path("retro.ico"))
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
            from PySide6.QtWidgets import QMenu
            menu = QMenu()
            menu.addAction(self._mic_action)
            menu.addAction("Settings", self.open_settings)
            menu.addAction("Coding model...", self._setup_coding_model)
            menu.addAction("Replay intro", self.play_intro)
            menu.addAction("Quit", self.close)
            tray.setContextMenu(menu)
            tray.activated.connect(self._tray_activated)
            tray.show()
            return tray
        except Exception:
            return None

    def _tray_activated(self, reason):
        """A left click on the tray icon opens the panel - the only way in when the
        circle is click-through and Windows has hidden the icon in its overflow."""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_settings()

    # --------------------------------------------------------------- geometry

    def set_floating(self, on=True, quiet=False):
        """Swap between the circle and the panel."""
        on = bool(on)
        self._floating = on
        self.float_btn.blockSignals(True)
        self.float_btn.setChecked(on)
        self.float_btn.setText("FLOAT ON" if on else "FLOAT OFF")
        self.float_btn.blockSignals(False)
        for widget in self._panel_widgets:
            if widget is self.panel or widget is self.drawer:
                continue
            widget.setVisible(not on)
        self.panel.setVisible(not on)
        self.mark.setVisible(not on)
        # The drawer is the one thing that has its own state: the panel always shows
        # it closed, and it comes back open only if it was open before the circle took
        # the screen. Without this it inherited the loop above and opened itself on
        # every return from floating.
        self.drawer.setVisible((not on) and self.settings_btn.isChecked())
        self.agent.setVisible(True)
        self.agent.set_label_space(on)
        flags = BASE_FLAGS
        if on:
            flags = flags | Qt.WindowType.WindowTransparentForInput
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
            # Changing window flags recreates the native window, and the translucency
            # attribute does not survive that: the circle came back on a black plate.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.writing.setVisible((not on) and self.writing.showing)
        if on:
            self.shelf.setVisible(False)
        else:
            self._refresh_shelf()
        self._fit_panel()
        if not quiet:
            self.show()

    def _fit_panel(self):
        """The window is exactly what is on it, and no bigger."""
        if self._floating:
            grown = int(self._circle_size * FLOAT_WINDOW_SCALE)
            self.setFixedSize(QSize(grown, grown))
            self.agent.setGeometry(0, 0, grown, grown)
            self.panel.setGeometry(0, 0, grown, grown)
            self._place(QSize(grown, grown))
            return
        extra = 0
        if self.writing.isVisible():
            extra += STAGE_H + 12
        if self.shelf.isVisible():
            extra += SHELF_HEIGHT + 8
        height = PANEL_H + extra
        self.setFixedSize(QSize(PANEL_W, height))
        self.panel.setGeometry(0, 0, PANEL_W, height)
        self.agent.setGeometry((PANEL_W - ORB) // 2, HEADER_H + 8, ORB, ORB)
        self._place(QSize(PANEL_W, height))

    def _place(self, size=None):
        """Keep the bottom-right corner fixed as the window changes shape."""
        if self._free_position:
            return
        if not (self._placed or size is not None):
            return
        try:
            screen = self.screen() or QApplication.primaryScreen()
            area = screen.availableGeometry() if screen is not None else None
            if area is None:
                return
            size = size or self.size()
            x = area.right() - size.width() - 24
            y = area.bottom() - size.height() - 24
            self.move(max(area.left() + 12, x), max(area.top() + 12, y))
        except Exception:
            pass

    def open_settings(self):
        """Show the panel and put it in front - from the tray, the voice, or the menu."""
        if self._floating:
            self.set_floating(False)
        self._toggle_settings(True)
        self.show()
        self.raise_()
        self.activateWindow()
        return self

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_panel()

    def paintEvent(self, _event):
        """The panel's own rounded plate. Floating draws nothing but the orb."""
        if self._floating:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        plate = QRect(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(float(plate.x()), float(plate.y()),
                            float(plate.width()), float(plate.height()),
                            float(theme.RADIUS), float(theme.RADIUS))
        painter.fillPath(path, theme.BG)
        painter.setPen(QPen(theme.BORDER, 1))
        painter.drawPath(path)

    # ------------------------------------------------------------- the mouse

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            self._dragged = True
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_origin is not None and self._dragged:
            self._free_position = True
        self._drag_origin = None
        self._dragged = False
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------- the controls

    def _submit(self):
        """Send what is in the composer, and show it in the record straight away."""
        text = self.prompt.text().strip()
        if not text:
            return
        self.prompt.remember(text)
        self.prompt.clear()
        self._reply = text
        self.talk.add("user", text)
        self._say("")
        try:
            self.bridge.submit(text)
        except Exception as exc:
            self._say(f"that did not reach Retro ({exc})")
            self.talk.add("retro", f"that did not reach Retro ({exc})", tone="error")

    def _say(self, text):
        self._reply = str(text or "")
        self.reply_label.setText(self._reply)

    def _speak_again(self, text):
        """SPEAK on a turn: the same voice Retro answers with, saying it again."""
        text = str(text or "").strip()
        if not text:
            return
        try:
            self.bridge.session.speak_async(text)
        except Exception as exc:
            self._say(f"that could not be read out ({exc})")

    def _retry(self, text):
        """RETRY on a turn: send the same request again, as a new turn."""
        text = str(text or "").strip()
        if not text:
            return
        self.prompt.setText(text)
        self._submit()

    # ---- the drawer, the suggestions and the keyboard -------------------

    def _on_prompt_changed(self, text):
        """The send button is real: disabled until there is something to send."""
        self.send_btn.setEnabled(bool(str(text or "").strip()))

    def _toggle_settings(self, on=None):
        """Open or close the drawer - the chip, Ctrl+, the tray and the voice all land here."""
        opened = (not self.drawer.isVisible()) if on is None else bool(on)
        self.drawer.setVisible(opened)
        self.talk.set_compact(opened)
        self.settings_btn.blockSignals(True)
        self.settings_btn.setChecked(opened)
        self.settings_btn.blockSignals(False)
        if opened:
            self.prompt.clearFocus()
        else:
            self.prompt.setFocus()

    def _toggle_settings_key(self):
        self._toggle_settings(None)

    def _on_suggestion(self, text):
        """A chip is a whole command: put it in the composer and send it."""
        self.prompt.setText(str(text or ""))
        self._submit()

    def _clear_talk(self):
        self.talk.clear()
        self._say("record cleared")

    def _escape(self):
        """Escape takes back the smallest thing that is open."""
        if self.prompt.text():
            self.prompt.clear()
        elif self.drawer.isVisible():
            self._toggle_settings(False)

    def _stop_task(self):
        """STOP means stop: the running task is cancelled, not just hidden."""
        try:
            ok, message = self.bridge.session.cancel_task()
        except Exception as exc:
            self._say(f"nothing could be stopped ({exc})")
            return False
        self.talk.set_working(False)
        self.status.set_detail("stopped" if ok else "")
        if not ok and message:
            self._say(str(message))
        return bool(ok)

    def _save_key(self):
        typed = self.key_input.text().strip()
        if not typed:
            self._say("paste a key first")
            return
        if config.save_cloud_key(typed):
            self.key_input.clear()
            self._say("key saved in Retro's own folder")
            self._fill_models()
        else:
            self._say("the key could not be written")

    def _on_mic_clicked(self, checked):
        """A click reaches the session; the label waits for the session to answer."""
        try:
            self.bridge.set_mic(bool(checked))
        except Exception as exc:
            self._say(f"the microphone could not be switched ({exc})")
        self._poll_room()

    def _on_background_clicked(self, checked):
        """Background hearing is room audio, so it turns the microphone off."""
        session = self.bridge.session
        try:
            if checked and getattr(session, "mic_on", False):
                session.set_mic(False, save=True)
                self._on_mic(False)
            session.set_background(bool(checked), save=True)
        except Exception as exc:
            self._say(f"background hearing could not be switched ({exc})")
        self._sync_background_button()
        self._poll_room()

    def _on_look_clicked(self, checked):
        mode = "music" if checked else "media"
        self.agent.set_visual_mode(mode)
        try:
            settings.save(visual_mode=mode)
        except Exception:
            pass
        self.look_btn.setText(f"LOOK {mode.upper()}")
        self._sync_look_button()

    def _on_size_changed(self, value):
        self._circle_size = int(value)
        if self._floating:
            self._fit_panel()

    def _on_model_changed(self, _index):
        choice = self.model_combo.currentData()
        if not choice:
            return
        try:
            self.bridge.session.set_model(choice)
            self.status.set_model(self.model_combo.currentText())
        except Exception as exc:
            self._say(f"the model could not be switched ({exc})")

    def _on_voice_changed(self, _index):
        name = self.voice_combo.currentData()
        if not name:
            return
        try:
            self.bridge.session.set_voice(name)
        except Exception:
            pass

    def _on_output_changed(self, _index):
        device = self.output_combo.currentData()
        try:
            self.bridge.session.set_output(device)
        except Exception:
            pass

    def _on_size_label(self):
        return self._circle_size

    # ---- filling the pickers -------------------------------------------

    def _fill_models(self):
        current = self.model_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for row in config.model_choices():
            self.model_combo.addItem(row["label"], row["value"])
            item = self.model_combo.model().item(self.model_combo.count() - 1)
            if item is not None:
                item.setToolTip(row["tooltip"])
                item.setEnabled(row["kind"] != "nonchat")
        self.model_combo.blockSignals(False)
        if current:
            self._select_value(current)

    def _select_value(self, value):
        wanted = str(value or "").lower()
        for index in range(self.model_combo.count()):
            if str(self.model_combo.itemData(index) or "").lower() == wanted:
                self.model_combo.blockSignals(True)
                self.model_combo.setCurrentIndex(index)
                self.model_combo.blockSignals(False)
                return True
        return False

    def _select_current_model(self):
        """The saved choice, or AUTO - never a row that is not there."""
        choice = str(getattr(self.bridge.session, "model_choice", "") or "")
        target = config.cloud_model_for(choice) if choice else "auto"
        if choice.lower() in ("auto", "cloud") or not choice:
            target = "auto"
        if not target or not self._select_value(target):
            self._select_value("auto")
        self.status.set_model(self.model_combo.currentText())

    def _on_models_listed(self):
        self._fill_models()
        self._select_current_model()

    def _load_voices(self):
        names = []
        try:
            names = [str(name) for name in self.bridge.session.voices()]
        except Exception:
            names = []
        saved = str(settings.get("voice") or "")
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for name in names:
            self.voice_combo.addItem(name, name)
        if not names:
            self.voice_combo.addItem("System voice", saved or "")
        self.voice_combo.blockSignals(False)
        if saved:
            for index in range(self.voice_combo.count()):
                if str(self.voice_combo.itemData(index) or "") == saved:
                    self.voice_combo.setCurrentIndex(index)
                    break

    def _load_outputs(self):
        pairs = []
        try:
            pairs = list(self.bridge.session.outputs() or [])
        except Exception:
            pairs = []
        saved = settings.get("output")
        self.output_combo.blockSignals(True)
        self.output_combo.clear()
        self.output_combo.addItem("System default", None)
        seen = {"system default"}
        for entry in pairs:
            try:
                index, name = entry
            except (TypeError, ValueError):
                index, name = None, entry
            label = str(name or "").strip()
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            self.output_combo.addItem(label, index)
        self.output_combo.blockSignals(False)
        if saved is not None:
            for position in range(self.output_combo.count()):
                if str(self.output_combo.itemData(position)) == str(saved):
                    self.output_combo.setCurrentIndex(position)
                    break

    def _load_inputs(self):
        """Nothing to show yet; kept so the held-back work has one entry point."""
        return []

    # ---- the switches' labels -------------------------------------------

    def _label_mic_button(self):
        on = bool(getattr(self.bridge.session, "mic_on", True))
        self.mic_btn.blockSignals(True)
        self.mic_btn.setChecked(on)
        self.mic_btn.blockSignals(False)
        self.mic_btn.setText("MIC ON" if on else "MIC OFF")
        self.mic_btn.setToolTip("Retro can hear commands" if on
                                else "no audio is captured while this is off")
        self.status.set_mic(on)

    def _sync_background_button(self):
        on = bool(getattr(self.bridge.session, "background", False))
        self.bg_btn.blockSignals(True)
        self.bg_btn.setChecked(on)
        self.bg_btn.blockSignals(False)
        self.bg_btn.setText(f"BACKGROUND HEARING {'ON' if on else 'OFF'}")

    def _sync_look_button(self):
        mode = str(settings.get("visual_mode") or "music").lower()
        if mode not in ("music", "media"):
            mode = "music"
        self.agent.set_visual_mode(mode)
        self.look_btn.blockSignals(True)
        self.look_btn.setChecked(mode == "music")
        self.look_btn.blockSignals(False)
        self.look_btn.setText(f"LOOK {mode.upper()}")

    def _sync_follow_button(self):
        """The follow-up switch lives in settings only; nothing to paint yet."""
        return settings.get("followup")

    def _poll_room(self):
        """Feed the orb the machine's own audio while background hearing is on.

        Three conditions, and each is a separate bug if dropped: the switch has to be
        on, the microphone has to be off (the sampler only runs then, and the two
        cannot share one listening), and the session has to have actually started -
        the previews build this window without one.
        """
        session = self.bridge.session
        state = str(getattr(session, "_state", "") or "")
        wanted = bool(getattr(session, "background", False))
        mic = bool(getattr(session, "mic_on", True))
        live = wanted and not mic and state not in ("", "offline")
        if self.agent.visual != live:
            self.agent.set_visual(live)
        if not live:
            return
        sampler = getattr(session, "audience", None)
        if sampler is None:
            return
        try:
            level = float(getattr(sampler, "level", 0.0) or 0.0)
        except (TypeError, ValueError):
            level = 0.0
        bands = getattr(sampler, "bands", None)
        self.agent.set_audio(max(0.0, min(1.0, level)), bands)

    # ------------------------------------------------------- what the core says

    def _on_core_event(self, event):
        if event == "open_code_chat":
            self.open_code_chat()
        elif event == "show_settings":
            self.open_settings()

    def _on_state(self, state, detail=""):
        self._session_state = str(state or "offline")
        if self._coding_live:
            return                       # the coding task owns the orb while it runs
        mapped = {"offline": "offline", "asleep": "asleep", "loading": "loading",
                  "listening": "listening", "working": "working",
                  "speaking": "speaking"}.get(self._session_state, "listening")
        self.agent.set_state(mapped)
        self.agent.set_caption(str(detail or "").upper())
        self.status.set_state(self._session_state, detail)
        if self._session_state == "working":
            # The strip already carries the detail; the row carries the clock. One fact
            # said twice, two lines apart, reads as two problems.
            self.talk.set_working(True, "WORKING")
        elif self._session_state != "loading":
            self.talk.set_working(False)

    def _on_reply(self, text, ok=True):
        """Every answer goes into the record, and stays there."""
        self._reply = str(text or "")
        if self._reply.strip():
            self.talk.add("retro", self._reply, tone="normal" if ok else "error")
        self.talk.set_working(False)
        self.status.set_detail("")
        self._say("")
        self.agent.set_progress("" if ok else self._reply[:60])

    def _on_level(self, value):
        level = float(value or 0.0)
        self.agent.set_level(level)
        self.status.set_level(level)

    def _on_progress(self, text):
        if text:
            self.agent.set_progress(str(text))
            self.status.set_detail(str(text))
            self.talk.set_progress(str(text))

    def _on_mode(self, mode):
        self.agent.set_mode(str(mode or ""))

    def _on_mic(self, on):
        on = bool(on)
        self.status.set_mic(on)
        self.mic_btn.blockSignals(True)
        self.mic_btn.setChecked(on)
        self.mic_btn.blockSignals(False)
        self.mic_btn.setText("MIC ON" if on else "MIC OFF")
        self.mic_btn.setToolTip("Retro can hear commands" if on
                                else "no audio is captured while this is off")
        if self._mic_action is not None:
            self._mic_action.blockSignals(True)
            self._mic_action.setChecked(on)
            self._mic_action.blockSignals(False)
        self._poll_room()

    def _on_voice_tested(self, peak, mixer_peak):
        if peak is None or float(peak) < 0:
            self._say("the voice could not be played on this output")
        else:
            self._say(f"the voice played (peak {float(peak):.2f})")

    def _on_mic_tested(self, level):
        if level is None or float(level) < 0:
            self._say("that microphone could not be opened")
        else:
            self._say(f"the microphone heard {float(level):.2f}")

    def _show_diagnostics(self):
        try:
            from ui.diagnostics import DiagnosticsPanel
            panel = getattr(self, "_diagnostics", None)
            if panel is None:
                self._diagnostics = DiagnosticsPanel(self)
                panel = self._diagnostics
            panel.refresh()
            panel.show()
            panel.raise_()
        except Exception as exc:
            self._say(f"the diagnostics panel could not open ({exc})")

    # ------------------------------------------------------------ the writing

    def _on_writing(self, event):
        event = event if isinstance(event, dict) else {}
        kind = str(event.get("kind") or "")
        stage = self.writing
        if kind == "begin":
            stage.begin(event.get("title") or "", int(event.get("total") or 0),
                        int(event.get("words") or 0), int(event.get("done") or 0))
            self.agent.set_arc(stage.progress())
        elif kind == "section":
            stage.section(int(event.get("index") or 0), int(event.get("total") or 0),
                          str(event.get("heading") or ""))
            self.agent.set_arc(stage.progress())
        elif kind == "section_done":
            stage.section_done(int(event.get("index") or 0), int(event.get("total") or 0),
                               int(event.get("words") or 0))
            self.agent.set_arc(stage.progress())
        elif kind == "writing":
            stage.writing(str(event.get("text") or ""))
        elif kind == "done":
            stage.finish(str(event.get("title") or ""), int(event.get("words") or 0),
                         str(event.get("file") or ""))
            self.agent.set_arc(None)
        elif kind == "paused":
            stage.pause(str(event.get("title") or ""), int(event.get("done") or 0),
                        int(event.get("total") or 0), int(event.get("words") or 0))
            self.agent.set_arc(None)
        self.writing.setVisible((not self._floating) and stage.showing)
        self._fit_panel()
        self._refresh_shelf()

    def _after_card_folded(self):
        self.writing.setVisible(False)
        self._fit_panel()
        self._refresh_shelf()

    def _refresh_shelf(self):
        """The strip under the circle: what is being written, or what has been."""
        rows = []
        try:
            rows = list(self.bridge.session.documents(limit=8) or [])
        except Exception:
            rows = []
        self.shelf.set_documents(rows)
        show = bool(rows) and not self.writing.showing and not self._floating
        self.shelf.setVisible(show)
        self._fit_panel()

    def _open_document(self, name):
        name = str(name or "")
        try:
            path = self.bridge.session.open_document(name)
        except Exception:
            path = None
        if path:
            self._say(f"Opened {os.path.basename(str(path))}")
        else:
            self._say(f"I couldn't open {name}")

    def _on_writing_reply(self, text):
        self._say(text)

    # ------------------------------------------------------------- the coding

    def open_code_chat(self):
        """The discussion: a conversation about the project, and then a handoff."""
        from .code_chat import open_code_chat as build_chat
        if self._code_chat is None:
            chat = build_chat(parent=self)
            chat.on_handoff = self._hand_off
            chat.model_setup = self._setup_coding_model
            chat.closed.connect(self._forget_chat)
            self._code_chat = chat
        self._code_chat.show()
        self._code_chat.raise_()
        self._code_chat.activateWindow()
        return self._code_chat

    def _forget_chat(self):
        self._code_chat = None

    def _setup_coding_model(self):
        """The coding model's dialog - a dead endpoint is fixed from here."""
        try:
            from ui.code_setup import CodingSetup
            dialog = CodingSetup(self)
            dialog.exec()
            try:
                from retrocli import config
                config.forget()
            except Exception:
                pass
            return True
        except Exception as exc:
            self._say(f"the coding model setup could not open ({exc})")
            return False

    def _hand_off(self, snapshot, project):
        """The discussion hands its frozen snapshot to the terminal."""
        job = handoff.get_handoff()
        messages = [{"role": message.get("role"), "text": message.get("text"),
                     "content": message.get("text")}
                    for message in (snapshot or {}).get("messages", [])]
        ok, message = job.start(str(project or ""), messages)
        if ok:
            self._say("handing the discussion to the coding terminal...")
        return ok, message

    def _coding_event(self, event):
        """The handoff's events, on the orb and in the panel."""
        event = event if isinstance(event, dict) else {}
        kind = event.get("kind")
        if kind == "open_code_chat":
            self.open_code_chat()
            return
        if kind == "state":
            state = str(event.get("state") or "")
            if state == "working":
                self._coding_live = True
                self.agent.set_state("working")
                self.agent.set_caption("CODING")
            elif state == "cancelled":
                self._coding_live = False
                self.agent.set_state("listening")
            elif str(event.get("detail") or ""):
                self._say(str(event.get("detail")))
            return
        if kind == "ready":
            self._coding_live = True
            self.agent.set_state("working")
            self.agent.set_caption("CODING")
            self._say(f"the coding terminal is ready ({event.get('detail') or 'connected'})")
            return
        if kind == "tool":
            self.agent.set_progress(f"running {event.get('tool')}")
            return
        if kind == "failed":
            self._coding_live = False
            self.agent.set_state(self._calm_state())
            return
        if kind == "error":
            self._say(f"coding: {event.get('message')} ({event.get('stage')})")
            return
        if kind == "done":
            self._coding_live = False
            self.agent.set_state(self._calm_state())
            self.agent.set_caption("")
            self.agent.clear_progress()
            summary = str(event.get("summary") or event.get("error") or "")
            files = event.get("files") or []
            tail = f" ({len(files)} file(s) changed)" if files else ""
            self._say(("coding done: " if event.get("ok") else "coding failed: ")
                      + summary + tail)
            return

    def _calm_state(self):
        state = self._session_state or "listening"
        return "listening" if state in ("working", "") else state

    # ------------------------------------------------------------- the arrival

    def start(self):
        """Load the session off the GUI thread. Returns immediately."""
        try:
            self.bridge.start_async(voice=self._voice)
        except Exception as exc:
            self._on_ready(False, str(exc))
        return True

    def begin_arrival(self):
        """The logo takes the screen: nothing of the panel shows over it."""
        self._arriving = True
        self.hide()

    def finish_arrival(self):
        """The logo is over: run the work that was held back, and say the line."""
        self._arriving = False
        self.show()
        self.raise_()
        self.activateWindow()
        self._fit_panel()
        self._after_ready()
        self.announce_arrival()

    def announce_arrival(self):
        """Say the arrival line - now, or the moment the session has a voice."""
        session = self.bridge.session
        if getattr(session, "speaker", None) is None:
            self._announce_when_ready = True
            return
        self._say_arrival()

    def _say_arrival(self):
        def work():
            try:
                self.bridge.session.speak_async(ARRIVAL_LINE)
            except Exception:
                pass
        threading.Thread(target=work, name="retro-arrival", daemon=True).start()

    def play_intro(self):
        """Replay the arrival, and end it where the launch ends it."""
        from ui import intro as splash
        self._arriving = True
        self.hide()
        return splash.play(on_finished=self.finish_arrival, with_sound=True)

    def _on_ready(self, ok, error=""):
        """The session finished loading. Everything visual waits for the logo."""
        self._ready_result = (bool(ok), str(error or ""))
        if not ok:
            self._on_state("offline", error or "the model did not load")
            self._say(error or "Retro could not load its model")
            return
        if self._arriving:
            return
        self._after_ready()

    def _after_ready(self):
        """The work that must not appear in front of the logo."""
        if self._ready_done:
            if self._announce_when_ready:
                self._announce_when_ready = False
                self.announce_arrival()
            return
        self._ready_done = True
        self._load_voices()
        self._load_outputs()
        self._load_inputs()
        self._select_current_model()
        self._sync_follow_button()
        self._sync_background_button()
        self._sync_look_button()
        self._label_mic_button()
        try:
            self.bridge.refresh_models_async()
        except Exception:
            pass
        try:
            self.bridge.start_voice_loop()
        except Exception:
            pass
        if self._announce_when_ready:
            self._announce_when_ready = False
            self.announce_arrival()

    # ------------------------------------------------------------------- exit

    def closeEvent(self, event):
        """Leave at once. Shutting the session down is best-effort and never waited on.

        `Session.stop` joins a voice loop that may be holding a microphone or a model
        load, so calling it here made Quit hang with the circle still on screen. The
        window goes first, the session is asked to stop on a worker, and a short
        watchdog ends the process if something below refuses to let go.
        """
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        for stop in (self._unsubscribe, self._handoff_unsub):
            try:
                stop()
            except Exception:
                pass
        try:
            self._room_timer.stop()
        except Exception:
            pass
        self.hide()
        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception:
            pass
        offscreen = str(os.environ.get("QT_QPA_PLATFORM", "")).strip().lower() == "offscreen"
        if offscreen:
            # A test or the self-test: stop in order, so no worker outlives its test
            # and writes into the next one's sandbox.
            self._stop_session()
        else:
            threading.Thread(target=self._stop_session, name="retro-quit",
                             daemon=True).start()
            self._force_quit_soon()
        super().closeEvent(event)

    def _stop_session(self):
        try:
            self.bridge.stop()
        except Exception:
            pass

    @staticmethod
    def _force_quit_soon(seconds=2.5):
        """The last resort: a wedged audio or model thread must not keep the app alive.

        Never offscreen: the self-test and the test suite open and close this window
        in their own process, and a watchdog that ends it would truncate them.
        """
        if str(os.environ.get("QT_QPA_PLATFORM", "")).strip().lower() == "offscreen":
            return

        def work():
            time.sleep(seconds)
            try:
                QApplication.quit()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=work, name="retro-force-quit", daemon=True).start()


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def apply_startup(argv=None):
    """Put Retro in the logon list, once, on a machine that asked for it.

    Never in an offscreen run (the self-test, the previews and the test suite all run
    that way) and never when the environment says hands off - both would write a real
    registry entry from a check that was not supposed to touch the machine.
    """
    argv = list(argv or [])
    if str(os.environ.get("QT_QPA_PLATFORM", "")).strip().lower() == "offscreen":
        return None
    if startup.opted_out():
        return None
    if "--no-startup" in argv:
        ok, _message = startup.ensure(False)
        if ok:
            settings.save(startup=False)
        return ok
    decision = settings.get("startup")
    if decision is None:
        ok, _message = startup.ensure(True)
        if ok:
            settings.save(startup=True)
        return ok
    ok, _message = startup.ensure(bool(decision))
    return ok


def run(argv=None):
    """The launch path: the arrival, then the window, then the event loop."""
    argv = list(argv if argv is not None else sys.argv[1:])
    # Resolved at call time: a second QApplication can never exist in one process, so
    # the one already running is the one to drive, and tests replace the class.
    from PySide6.QtWidgets import QApplication as _QtApp
    builder = QApplication                     # the name the tests patch
    app = _QtApp.instance()
    if app is None:
        app = builder(sys.argv) if builder is not _QtApp else _QtApp(sys.argv)
    try:
        apply_startup(argv)
    except Exception:
        pass
    voice = "--no-voice" not in argv
    floating = "--window" not in argv
    window = RetroWindow(floating=floating, voice=voice)
    window.start()
    show_intro = (core_intro_wanted(argv))
    if show_intro:
        window.begin_arrival()
        try:
            from ui import intro as splash
            splash.play(on_finished=window.finish_arrival, with_sound=True)
        except Exception:
            window.finish_arrival()
    else:
        window.show()
        window.announce_arrival()
    return app.exec()


def core_intro_wanted(argv):
    """Whether the arrival plays on this run."""
    try:
        from core import intro as plan
        if not plan.wanted(argv=argv):
            return False
    except Exception:
        return False
    try:
        return settings.get_bool("intro", True)
    except Exception:
        return True


def selftest(argv=None):
    """Build, draw and exercise the window without a desktop. Exit 0 when it works."""
    argv = list(argv or [])
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report = []

    def note(name, ok, detail=""):
        report.append((name, bool(ok), detail))

    app = QApplication.instance() or QApplication([])
    window = None
    try:
        window = RetroWindow(floating=False, voice=False)
        window.resize(PANEL_W, PANEL_H)
        from PySide6.QtGui import QImage
        image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(theme.BG_TRANSPARENT)
        painter = QPainter(image)
        window.render(painter, QPoint(0, 0))
        painter.end()
        lit = sum(1 for y in range(image.height()) for x in range(image.width())
                  if image.pixelColor(x, y).alpha() > 10)
        note("window", lit > 1000, f"{lit} pixels drawn")

        from retrocli import logo as coding_logo
        note("coding terminal", len(coding_logo.render(None)) == 6, "wordmark renders")

        from core import appfunc  # noqa: F401
        note("apps", True, "toolset imports")

        try:
            # The real question is whether this machine's applications can be found
            # at all: `apps.load` walks the Start menu, the registry and the shell.
            from core import apps as app_index
            found = len(app_index.load() or [])
            note("registry", bool(found), f"{found} applications")
        except Exception as exc:
            note("registry", False, str(exc))

        if "--deep" in argv:
            try:
                ok = window.bridge.start(voice=False)
                note("session", ok, window.bridge.session.error or "loaded")
            except Exception as exc:
                note("session", False, str(exc))

        if "--say" in argv:
            try:
                said = window.bridge.session.test_voice()
                peak = said[0] if isinstance(said, tuple) else said
                note("spoken line", bool(peak), f"peak {peak}")
            except Exception as exc:
                note("spoken line", False, str(exc))
    except Exception:
        note("window", False, traceback.format_exc().strip().splitlines()[-1])
    finally:
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
    for name, ok, detail in report:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    return 0 if report and all(ok for _n, ok, _d in report) else 1


def main(argv=None):
    """Entry point the launcher and the packaged build call."""
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--selftest" in argv:
        return selftest(argv)
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
