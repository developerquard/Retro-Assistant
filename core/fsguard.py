"""Every file path Retro touches goes through here.

Paths are resolved (symlinks, '..' and absolute paths included) before the
check, so a link inside the workspace cannot point outside it.
"""
import os
from pathlib import Path


class OutsideWorkspace(Exception):
    pass


def resolve_inside(root, relative):
    root_real = Path(os.path.realpath(root))
    candidate = Path(os.path.realpath(root_real / relative))
    try:
        candidate.relative_to(root_real)
    except ValueError:
        raise OutsideWorkspace(f"{relative!r} is outside the workspace") from None
    return candidate
