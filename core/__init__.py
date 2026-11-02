# core/__init__.py - core package init

# Explicitly export the logo module
from .logo import (
    render_logo,
    print_logo,
    get_logo_text,
    render_plain,
    LOGO_LINES,
    _should_use_color,
    _colorize_line,
)

# Re-export at package level for easy access
__all__ = [
    "render_logo",
    "print_logo",
    "get_logo_text",
    "render_plain",
    "LOGO_LINES",
    "_should_use_color",
    "_colorize_line",
]
