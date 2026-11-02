#!/usr/bin/env python3
"""core/logo.py - the RETRO wordmark, as the desktop window draws it.

There is exactly one wordmark in this project: the one in :mod:`retrocli.logo`, the
same block letters the terminal prints. This module keeps the names the window and
the older scripts already import, so nothing has to know where the art lives.
"""

from retrocli.logo import (                     # noqa: F401  (re-exported on purpose)
    ASCII_LINES,
    GLYPHS,
    LOGO_LINES,
    Ansi,
    get_logo_text,
    logo_text,
    paint_for,
    print_logo,
    render as render,
    render_logo as render_logo,
)

WORD = "RETRO"


def render_plain():
    """The wordmark without colour, ready for a QLabel or a text widget."""
    return list(LOGO_LINES)


def render_ascii():
    """The pure-ASCII rendition, for a font or console without block glyphs."""
    return list(ASCII_LINES)


def _should_use_color(stream=None) -> bool:
    """Whether a stream would get colour - kept for callers that ask."""
    return paint_for(stream) is not None


def _colorize_line(line: str, use_color: bool = True) -> str:
    """Paint one line of the wordmark, or leave it alone."""
    if not use_color:
        return line
    from retrocli.logo import _paint_line
    return _paint_line(line, Ansi())


__all__ = [
    "LOGO_LINES", "ASCII_LINES", "GLYPHS", "WORD",
    "render", "render_logo", "render_plain", "render_ascii",
    "print_logo", "get_logo_text", "logo_text", "paint_for", "Ansi",
]
