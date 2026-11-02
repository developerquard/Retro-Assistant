"""Shared types and the keyword matcher."""
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple


@dataclass
class Result:
    ok: bool
    message: str
    verified: bool = False          # True only when the result was read back from the system
    choices: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Action:
    name: str
    tier: str                        # "read" | "reversible" | "sensitive"
    keywords: Tuple[str, ...]
    extract: Callable[[str], Optional[dict]]   # None means "this text isn't for this action"
    run: Callable[[dict], Result]
    description: str = ""


def norm(text):
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def has_kw(normalized, kw):
    """ASCII keywords must match whole words. Hindi keywords match as substrings,
    because Devanagari vowel signs break Python's word boundaries."""
    if kw.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", normalized) is not None
    return kw in normalized
