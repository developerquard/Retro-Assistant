"""Colours and sizes for the window, in one place.

The palette is deliberately small: one near-black panel, one blue accent for the
idle states, green while Retro is actually working, and grey when it isn't
running. Everything else is a dimmed version of the text colour.
"""
from pathlib import Path

from PySide6.QtGui import QColor

# One drawn arrow, instead of the border trick Qt renders as a small box.
CHEVRON = (Path(__file__).resolve().parent.parent / "assets" / "chevron.svg").as_posix()

# A slightly deeper panel and a quieter border than before: contrast belongs to the
# content, not to the frame, so the eye lands on the circle and the status line first.
BG = QColor("#0C1015")
BG_TRANSPARENT = QColor(0, 0, 0, 0)
BORDER = QColor("#1B232E")
PANEL_SOFT = QColor("#121821")
TEXT = QColor("#E6EAF2")
TEXT_DIM = QColor("#7C8798")
TEXT_DIMMER = QColor("#5A6474")

ACCENT = QColor("#4EA8FF")
GREEN = QColor("#3EE07A")
GREY = QColor("#6B7280")
AMBER = QColor("#E0A03E")

# The writing stage: a card that sits under the circle while a document is being
# written. Its own colours rather than a widget stylesheet, because it is painted
# by hand (ui/writing.py) - a default Qt widget beside a drawn circle looked like
# two different programs in one window.
CARD = QColor("#0B1017")
CARD_EDGE = QColor("#1B2534")
CARD_GLOW = QColor("#14202E")
INK_BRIGHT = QColor("#F2F6FF")

# ---------------------------------------------------------------------------
# The scale the panel is built from
#
# Four surfaces instead of one: a card separates itself from the plate by being a
# little lighter, not by carrying another border. The border is reserved for things
# you can press, which is what makes the panel readable at a glance - content is
# depth, controls are lines.
# ---------------------------------------------------------------------------
SURFACE_1 = QColor("#10161E")     # a card on the plate: turns, the drawer, the composer
SURFACE_2 = QColor("#141C26")     # Retro's own turns, between the composer rows
SURFACE_3 = QColor("#18222E")     # hover on a surface
STROKE = BORDER                   # the same line, named for what it does
ACCENT_EDGE = QColor("#2E5C82")   # the accent at the strength a border can carry
TEXT_STRONG = QColor("#F3F7FF")   # what the eye lands on first

RADIUS = 16
RADIUS_MD = 12                    # a content card
RADIUS_SM = 8                     # an inline action
RADIUS_PILL = 14                  # a suggestion chip

# Spacing: one scale, so no two gaps in the panel differ by a pixel for no reason.
SPACE = {"xs": 4, "sm": 6, "md": 8, "lg": 12, "xl": 16}
FLOAT_SIZE_DEFAULT = 220
FLOAT_SIZE_MIN = 120
FLOAT_SIZE_MAX = 420

# state -> (core colour, is the state "active")
# `loading` is the wake-up ramp: the orb walks from the grey it slept in to this
# blue over three seconds, so "loading" is the blue it is heading for. `asleep` is
# grey because the owner asked for a grey, motionless ball the moment it sleeps -
# not the blue of a machine that is quietly listening.
STATE_COLOURS = {
    "offline": GREY,
    "asleep": GREY,
    "loading": ACCENT,
    "listening": ACCENT,
    "working": GREEN,
    "speaking": GREEN,
    "error": AMBER,
}

STYLESHEET = f"""
QWidget#panel {{ background: transparent; }}
QToolTip {{
    background: {PANEL_SOFT.name()}; color: {TEXT.name()}; border: 1px solid {BORDER.name()};
    padding: 5px 8px; font-size: 11px;
}}
QComboBox {{
    background: transparent; border: none; color: {TEXT.name()};
    padding: 2px 16px 2px 0; font-size: 12px;
}}
QComboBox:hover {{ color: #F7FAFF; }}
QComboBox::drop-down {{ border: none; width: 14px; }}
QComboBox::down-arrow {{
    image: url("{CHEVRON}"); width: 9px; height: 6px; margin-right: 3px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL_SOFT.name()}; color: {TEXT.name()};
    border: 1px solid {BORDER.name()}; outline: none; padding: 2px;
    selection-background-color: #1E2A3A; selection-color: {TEXT.name()};
}}
QComboBox QAbstractItemView::item {{ min-height: 20px; padding: 2px 8px; }}
QLabel#caption {{
    color: {TEXT_DIMMER.name()}; font-size: 10px; letter-spacing: 1.2px;
}}
QLabel#hint {{ color: {TEXT_DIM.name()}; font-size: 11px; }}
QLineEdit#prompt {{
    background: transparent; border: none; border-bottom: 1px solid {BORDER.name()};
    color: {TEXT.name()}; font-size: 13px; padding: 6px 4px;
    selection-background-color: #24405F;
}}
QLineEdit#prompt:hover {{ border-bottom-color: #2B3948; }}
QLineEdit#prompt:focus {{ border-bottom: 1px solid {ACCENT.name()}; }}
QPushButton {{
    background: transparent; border: none; color: {TEXT_DIM.name()}; font-size: 11px;
}}
QPushButton#close {{ color: {TEXT_DIM.name()}; font-size: 15px; padding: 2px 6px; }}
QPushButton#close:hover {{ color: {TEXT.name()}; }}
QPushButton#micbtn {{
    border: 1px solid {BORDER.name()}; border-radius: 10px; color: {TEXT_DIMMER.name()};
    padding: 4px 10px; letter-spacing: 1px; font-size: 9px;
}}
QPushButton#micbtn:hover {{ color: {TEXT.name()}; border-color: #33404F; }}
QPushButton#micbtn:checked {{ color: {ACCENT.name()}; border-color: #27506F; }}
QPushButton#floatbtn {{
    border: 1px solid {BORDER.name()}; border-radius: 10px; color: {TEXT_DIM.name()};
    padding: 4px 13px; letter-spacing: 1.1px; font-size: 10px;
}}
QPushButton#floatbtn:hover {{
    color: {TEXT.name()}; border-color: #33404F; background: rgba(78, 168, 255, 16);
}}
QPushButton#floatbtn:pressed {{ background: rgba(78, 168, 255, 30); }}
QPushButton#floatbtn:checked {{ color: {ACCENT.name()}; border-color: #2E5C82; }}
QSlider::groove:horizontal {{ height: 2px; background: {BORDER.name()}; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: #2B4A66; border-radius: 1px; }}
QSlider::handle:horizontal {{
    background: {TEXT_DIM.name()}; width: 9px; height: 9px; margin: -4px 0; border-radius: 5px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT.name()}; }}

/* ---- the transcript: a record, not a log ------------ */
QScrollArea#transcript {{ background: transparent; border: none; }}
QScrollArea#transcript > QWidget > QWidget {{ background: transparent; }}
QFrame#turnUser, QFrame#turnRetro, QFrame#turnError {{
    border-radius: {RADIUS_MD}px;
}}
QFrame#turnUser {{ background: #101B28; border-left: 2px solid {ACCENT.name()}; }}
QFrame#turnRetro {{ background: {SURFACE_2.name()}; border-left: 2px solid #22303F; }}
QFrame#turnError {{ background: #1D1712; border-left: 2px solid {AMBER.name()}; }}
QLabel#role {{
    color: {TEXT_DIMMER.name()}; font-size: 9px; font-weight: 700;
    letter-spacing: 1.4px;
}}
QLabel#body {{ color: {TEXT.name()}; font-size: 12px; }}
QPushButton#turnAction {{
    color: {TEXT_DIM.name()}; font-size: 9px; letter-spacing: 1px;
    padding: 1px 6px; border-radius: 6px;
}}
QPushButton#turnAction:hover {{ color: {ACCENT.name()}; background: rgba(78, 168, 255, 22); }}
QPushButton#turnAction:focus {{ color: {ACCENT.name()}; border: 1px solid {ACCENT_EDGE.name()}; }}

/* ---- the empty state, and suggestions you can press --- */
QLabel#emptyTitle {{
    color: {TEXT_DIM.name()}; font-size: 10px; font-weight: 700; letter-spacing: 2.2px;
}}
QLabel#emptyBody {{ color: {TEXT_DIMMER.name()}; font-size: 11px; }}
QPushButton#chip {{
    border: 1px solid {BORDER.name()}; border-radius: {RADIUS_PILL}px;
    color: {TEXT_DIM.name()}; padding: 4px 12px; font-size: 10px; letter-spacing: 0.6px;
    text-align: left;
}}
QPushButton#chip:hover {{
    color: {TEXT_STRONG.name()}; border-color: {ACCENT_EDGE.name()};
    background: rgba(78, 168, 255, 16);
}}
QPushButton#chip:focus {{ border-color: {ACCENT.name()}; color: {TEXT_STRONG.name()}; }}

/* ---- the strip that says what Retro is doing ---------- */
QLabel#stateDot {{ border-radius: 4px; }}
QLabel#stateWord {{ font-size: 9px; font-weight: 700; letter-spacing: 1.8px; }}
QLabel#stateDetail {{ color: {TEXT_DIM.name()}; font-size: 10px; }}
QLabel#stateMeta {{ color: {TEXT_DIMMER.name()}; font-size: 9px; letter-spacing: 0.8px; }}
QPushButton#stopBtn {{
    border: 1px solid #6B3A3A; border-radius: 9px; color: #F2A6A6;
    padding: 2px 9px; font-size: 9px; letter-spacing: 1.2px;
}}
QPushButton#stopBtn:hover {{ background: rgba(224, 90, 90, 36); color: #FFD2D2; }}
QPushButton#stopBtn:focus {{ border-color: {AMBER.name()}; }}

/* ---- a row that is real work in progress ---------------- */
QFrame#working {{ background: #0E2418; border: 1px solid #1B3A2A; border-radius: 10px; }}
QLabel#workingText {{ color: #8FE3B4; font-size: 10px; letter-spacing: 0.6px; }}

/* ---- the composer ---------------------------------------- */
QFrame#composer {{
    background: {SURFACE_1.name()}; border: 1px solid {BORDER.name()};
    border-radius: {RADIUS_MD}px;
}}
QFrame#composer[focused="true"] {{ border-color: {ACCENT_EDGE.name()}; }}
QFrame#composer QLineEdit#prompt {{
    background: transparent; border: none; padding: 3px 0;
}}

QPushButton#send {{
    border: none; border-radius: 9px; background: rgba(78, 168, 255, 34);
    color: {ACCENT.name()}; padding: 4px 11px; font-size: 9px; font-weight: 700;
    letter-spacing: 1.2px;
}}
QPushButton#send:hover {{ background: rgba(78, 168, 255, 58); color: {TEXT_STRONG.name()}; }}
QPushButton#send:disabled {{ background: transparent; color: {TEXT_DIMMER.name()}; }}
QPushButton#send:focus {{ border: 1px solid {ACCENT.name()}; }}

/* ---- the drawer: what you touch once a week -------------- */
QFrame#drawer {{
    background: {SURFACE_1.name()}; border: 1px solid {BORDER.name()};
    border-radius: {RADIUS_MD}px;
}}
QLabel#drawerTitle {{
    color: {TEXT_DIMMER.name()}; font-size: 9px; font-weight: 700; letter-spacing: 1.8px;
}}
QLabel#drawerNote {{ color: {TEXT_DIMMER.name()}; font-size: 9px; }}
QPushButton#ghost {{
    border: 1px solid transparent; border-radius: 7px; color: {TEXT_DIM.name()};
    font-size: 9px; letter-spacing: 1.1px; padding: 2px 7px;
}}
QPushButton#ghost:hover {{ color: {TEXT_STRONG.name()}; background: rgba(255, 255, 255, 12); }}
QPushButton#ghost:focus {{ border-color: {ACCENT_EDGE.name()}; }}
QPushButton#ghost:checked {{ color: {ACCENT.name()}; }}
QFrame#drawer QComboBox {{ font-size: 11px; }}
QFrame#drawer QLineEdit {{
    background: {SURFACE_2.name()}; border: 1px solid {BORDER.name()};
    border-radius: 8px; color: {TEXT.name()}; font-size: 10px; padding: 4px 8px;
    selection-background-color: #24405F;
}}
QFrame#drawer QLineEdit:focus {{ border-color: {ACCENT_EDGE.name()}; }}

/* ---- thin scrollbars, so the record does not grow handles -- */
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px 0; }}
QScrollBar::handle:vertical {{ background: #26313F; border-radius: 3px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #34455A; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""
